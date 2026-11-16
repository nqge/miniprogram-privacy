#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**额度闸门**调度器 —— 额度够才启动 agentTurn（通用的，多任务共用）。

## 支持的任务

    python3 scripts/daily_learning_gate.py --job daily-learning    # 默认
    python3 scripts/daily_learning_gate.py --job daily-reflection

⚠️ 2026-11-16 扩展：原先只服务 daily-learning。
   实测 daily-reflection 的失败形态**完全相同**
   （`All models failed (3)` + 148~312s 空转 + 零产出），
   而它当时还留在 cron 里自触发 → 同样会空转。
   → **同一个脚本服务两个任务**，不复制第二份（E11：不抄判据/逻辑）。

## 为什么需要它（2026-11-16 实测）

daily-learning 原先是 OpenClaw cron 里一个**纯 agentTurn** 任务（每天 09:00），
cron 指令里虽然写着「开始干活前先跑 `scripts/quota_preflight.py`」，
但这条指令**只在 agent 已经跑起来之后才生效** —— 而 agent 能不能跑起来，
取决于**模型 API 能不能回话**。

实测（11-15 那次失败的会话，21 行记录）：
- 09:00:00  cron prompt 注入
- 09:01:31  MiniMax-M3            stopReason=error
- 09:01:35  MiniMax-M3            stopReason=error
- 09:04:19  MiniMax-M3            stopReason=error
- 09:04:29  MiniMax-M3            stopReason=error
- 09:04:30  ← **整个 cron prompt 被重新注入**（换模型 M2.7-highspeed）
- ... 再两轮，共 **3 个模型 × 4 次尝试 = 12 次**，全部 stopReason=error
- 全程 **0 次工具调用** —— preflight **一次都没跑到**

→ **闸门装在了它要守的东西里面。**
  额度耗尽时，agent 连「第一个工具调用」都发不出去（模型 API 本身被限流），
  于是 preflight 永远没机会执行 → 任务照样启动、照样烧 ~180~316s、照样零产出。

## 闸门放在哪

**放在 OpenClaw 之外（systemd timer）**：
闸门本身是个纯 shell/python 脚本，**不消耗任何模型额度**，
所以「模型额度耗尽」这个故障**不会影响闸门本身**。
这正是它能起作用的原因 —— 闸门必须在被监控对象**之外**。

（OpenClaw 2026.3.24 的 cron payload 只有 `systemEvent` / `agentTurn` 两种，
**没有** `command` 之类可以在 agentTurn 之前执行的钩子，
所以闸门没法做在 cron 内部，只能放 systemd。）

## 触发方式

闸门通过后，用 `openclaw cron run <job-id>` 触发**原来的** agentTurn 任务。
不新建任务、不改原任务指令 —— 原任务已经被验证能跑通（11-16 09:00 status=ok）。

## 用法

    python3 scripts/daily_learning_gate.py                  # 闸门 + 触发（默认 daily-learning）
    python3 scripts/daily_learning_gate.py --job daily-reflection
    python3 scripts/daily_learning_gate.py --check          # 只跑闸门，不触发
    python3 scripts/daily_learning_gate.py --force          # 跳过闸门，强制触发
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

WS = Path("/root/.openclaw/workspace")
DEFAULT_JOB = "daily-learning"
PREFLIGHT = WS / "scripts" / "quota_preflight.py"

# 当前生效的任务名（由 main() 从 --job 设置；模块级默认值保证函数可单测）
JOB_NAME = DEFAULT_JOB


def log(msg):
    print(f"[{JOB_NAME}-gate] {msg}", flush=True)


# ── 持久化的闸门历史（2026-11-16 加）────────────────────────────────
# 为什么需要它：闸门日志写在 `memory/healthcheck-logs/` 下，而那个目录
# **在 .gitignore 里**（日志不是源码），并且 `run_logged.sh` 只保留 30 天。
# → 后果：① 机器重建 → 闸门历史全丢；② 【G2】断言要「连续 3 次跳过」
#   才报警，而 3 次 = 3 天 —— 期间任何一次清理/重建都会把信号抹掉。
# → 所以另存一份**精简的结构化记录**：一行 JSON，只要日期 + result + reason。
#   小、可进 git、能长期看趋势。
GATE_HISTORY = WS / "memory" / "gate-history.jsonl"


def record(result: str, reason: str = "", extra: dict | None = None):
    """写一条闸门决策到 stdout **和** 持久化历史。

    失败**不能影响闸门本身**（它只是记账，写不进去就跳过）——
    闸门的主职责是「决定要不要启动 agent」，记账是次要的。
    """
    line = f"GATE_RESULT={result}"
    if reason:
        line += f" reason={reason}"
    log(line)
    try:
        import datetime
        rec = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "job": JOB_NAME,
            "result": result,
        }
        if reason:
            rec["reason"] = reason
        if extra:
            rec.update(extra)
        GATE_HISTORY.parent.mkdir(parents=True, exist_ok=True)
        with GATE_HISTORY.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        # 记账失败只提示，不影响闸门决策
        print(f"[{JOB_NAME}-gate] ⚠️ 历史记录写入失败（不影响决策）: {e}", flush=True)


def find_job_id():
    """按名字找 job id（不写死 id —— id 会变）。

    ⚠️ 2026-11-16 实测坑：`openclaw cron list` **不列出已禁用的任务**。
    而闸门的前提正是「任务处于 disabled（不自己触发）」状态
    —— 调度改由 systemd 负责，cron 只作为**被触发的目标**。
    → 所以**不能只靠 `cron list`**，必须直接读 cron 的任务存储。
    （存储路径以 `cron list` 的输出为准 —— 同一份数据源，只是不过滤 disabled。）

    读不到就回退到 `cron list`（万一存储格式变了，至少还能按名字找）。
    """
    # ① 直接读任务存储（包含 disabled 的任务）
    try:
        jobs_file = Path("/root/.openclaw/cron/jobs.json")
        if jobs_file.exists():
            d = json.loads(jobs_file.read_text(encoding="utf-8"))
            jobs = d if isinstance(d, list) else d.get("jobs", d.get("data", []))
            for j in jobs:
                if j.get("name") == JOB_NAME:
                    return j.get("id")
    except Exception as e:
        log(f"⚠️ 读 cron 任务存储失败（{type(e).__name__}）→ 回退到 cron list")

    # ② 回退：`cron list`（不含 disabled 任务，仅作兜底）
    env = {**os.environ, "OPENCLAW_LOG_LEVEL": "warn"}
    try:
        r = subprocess.run(
            ["openclaw", "cron", "list", "--json"],
            capture_output=True, text=True, timeout=90, env=env,
        )
        d = json.loads(r.stdout[r.stdout.find("{"):])
    except Exception as e:
        log(f"❌ 读不到 cron 列表：{type(e).__name__}: {e}")
        return None
    jobs = d if isinstance(d, list) else d.get("jobs", d.get("data", []))
    for j in jobs:
        if j.get("name") == JOB_NAME:
            return j.get("id")
    log(f"❌ cron 里找不到名为「{JOB_NAME}」的任务")
    return None


def _classify_reason(reason: str) -> str:
    """把预检的结论文本映射成**结构化**的 reason 标签。

    ⚠️ 为什么不能只写 "quota"（2026-11-16 实测）：
      预检失败有三种完全不同的原因，而【G2】断言看到连续 3 次 skipped
      会报「额度长期枯竭，需要充值（E74）」——
      **如果 529 上游过载也写成 quota，就会把「上游抖」误报成「该充值」**。
      两种情况的处置完全不同：前者等 1-5 分钟，后者要牛哥哥掏钱。

    → reason 必须**忠实反映预检说了什么**。
    """
    r = (reason or "").lower()
    if "529" in r or "过载" in r or "繁忙" in r or "overload" in r:
        return "upstream_busy"
    if "普通限流" in r or "rate limit" in r or "限流" in r:
        return "rate_limited"
    if "额度" in r or "quota" in r or "token plan" in r:
        return "quota"
    return "unknown"


def run_gate():
    """跑配额预检。返回 (是否放行, 说明文字)。"""
    if not PREFLIGHT.exists():
        # 预检脚本不在 → 放行（别因为闸门本身坏了而让正事永远跑不了）
        log("⚠️ 预检脚本不存在 → 放行（闸门不应成为新的单点故障）")
        return True, "预检脚本缺失，放行"
    env = {**os.environ, "OPENCLAW_LOG_LEVEL": "warn"}
    try:
        r = subprocess.run(
            [sys.executable, str(PREFLIGHT)],
            capture_output=True, text=True, timeout=180, env=env,
        )
    except subprocess.TimeoutExpired:
        log("⚠️ 预检超时（180s）→ 放行（不确定 ≠ 额度不足）")
        return True, "预检超时，放行"
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode == 0:
        log("✅ 额度预检通过 → 放行")
        return True, "额度充足"
    # 闸门拦下了 → 把预检的结论抓出来，用于日志和 run summary
    concl = ""
    for line in out.splitlines():
        if "结论" in line:
            concl = line.strip().lstrip("❌✅ ").strip()
            break
    reason = concl or "额度不足"
    log(f"🚫 额度预检未通过 → 闸门关闭（{reason}）")
    # 放行判断写在最后一行，方便被外部脚本 grep
    record("blocked", reason)
    return False, reason


def trigger(job_id):
    env = {**os.environ, "OPENCLAW_LOG_LEVEL": "warn"}
    log(f"🚀 触发 cron 任务 {JOB_NAME} ({job_id})…")
    try:
        r = subprocess.run(
            ["openclaw", "cron", "run", job_id],
            capture_output=True, text=True, timeout=180, env=env,
        )
    except subprocess.TimeoutExpired:
        log("⚠️ 触发命令超时（180s）")
        return False
    tail = (r.stdout or "").strip().splitlines()[-3:]
    for t in tail:
        log(f"  | {t}")
    record("triggered", extra={"rc": r.returncode})
    return r.returncode == 0


def main():
    global JOB_NAME
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只跑闸门，不触发任务")
    ap.add_argument("--force", action="store_true", help="跳过闸门，强制触发")
    ap.add_argument("--job", default=DEFAULT_JOB,
                    help=f"要闸门化的 cron 任务名（默认 {DEFAULT_JOB}）")
    args = ap.parse_args()

    JOB_NAME = args.job
    job_id = find_job_id()
    if not job_id:
        record("error", "cron_job_not_found")
        return 2

    if args.force:
        log("⏭️  --force：跳过闸门")
        ok, reason = True, "forced"
    else:
        ok, reason = run_gate()

    if not ok:
        # 闸门关闭：**故意以 exit 0 结束** ——
        # 这不是故障，是「今天额度不够，今天不学」。
        # 用 exit 1 会让 systemd 把它记成失败并可能触发告警（假警报）。
        # ⚠️ 2026-11-16 修正：reason **不能写死 quota**。
        #   预检失败有多种原因（实测三种）：
        #     · 额度耗尽（HTTP 429 + Token Plan）→ reason=quota
        #     · 上游瞬时过载（HTTP 529 overloaded）→ reason=upstream_busy
        #     · 普通限流（HTTP 429 但无额度特征）→ reason=rate_limited
        #   写死成 quota 的话，**529 连续 3 次也会被报成「额度长期枯竭，需要充值」**
        #   → 把「上游抖」误报成「牛哥哥该充值」= 假警报（E66）。
        #   → reason 必须**忠实反映预检的结论**。
        record("skipped", _classify_reason(reason))
        return 0

    if args.check:
        record("ok", "gate_passed", extra={"check_only": True})
        return 0

    trigger(job_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
