#!/usr/bin/env python3
"""cron 投递状态检查 —— 由 scripts/healthcheck.sh 调用。

输出每行一个断言结果，格式：
  PASS <name> ...
  FAIL <name> ...
"""

import json
import os
import subprocess
import sys
import datetime


# 「本该发什么」—— E55 的核心维度
#
# 问题背景（2026-10-29）：
#   `delivered=false` 有两种完全相反的性质：
#     a) 有内容却没送到手上 → **真故障**（10-28 那批）
#     b) 根本没内容，不该打扰人 → **投递正确**（茶馆 cron「新增 0 条」那批）
#   字段本身没有这一维，光看 delivered 会把 (b) 误报成故障。
#
# 解法不是放宽断言，而是**加一维**：读 run 的产出摘要，判断「本该发什么」，
# 无产出 → 标注「有意不发」，不判 FAIL。
#
# ⚠️ 判据必须**保守**：宁可漏判也不误判 —— 只在**明确**匹配「无产出」语义时才放行，
# 匹配不到就按原来的严格判据走。

# 「本轮无产出」的语义标记（任一命中即视为「无产出」）
# 用「明确的无产出表述」，而不是「摘要里没有关键词」—— 后者会把长摘要误判成无产出。
_NO_OUTPUT_PATTERNS = [
    "无新内容",
    "无新议题",
    "没有新议题",
    "新增 0 条",
    "新增0条",
    "无新增",
    "无新蒸馏",
    "无内容",
    "无变更",
    "无异常",
    "nothing new",
    "no new content",
    "no new items",
    "nothing to report",
    "无待处理项",
    "无新增异常",
]

# 明确「有产出」的语义标记（优先级高于「无产出」）
# 若命中这些，说明这一轮**是有东西要发的**，即使含「无新增」也不能放行。
#
# ⚠️ 坑（实测 2026-10-29，单测当场抓出）：「有新增」是「没有新增」的**子串**！
#   「本轮没有新增议题」里含有「有新增」→ 会把明确的无产出误判成有产出。
#   → 这里只保留**不可能出现在否定句里**的标记；
#     「新增 N 条」这类用**数字限定**的形式（「新增 1 条」「新增1条」），
#     不要写裸的「新增 N 条」（会命中「新增 0 条」以外的模糊形式）。
_HAS_OUTPUT_PATTERNS = [
    "投递失败",
    "未送达",
    "未投递",
    "已修复",
    "修复了",
    "已生成",
    "已写入",
    "报告如下",
    "本轮新增",          # 「本轮新增 3 条」；但「本轮没有新增」不含「本轮新增」
    "本次新增",
    "新增 1 条",
    "新增1条",
    "新增 2 条",
    "新增2条",
    "新增 3 条",
    "新增3条",
    "新增 4 条",
    "新增4条",
    "新增 5 条",
    "新增5条",
]


def _classify_output(summary):
    """判断这次 run「本该发什么」。

    返回三态：
      "none"  → 明确无产出，不投递是正确的
      "has"   → 明确有产出
      "unknown" → 判不准 → 按严格判据处理（不放松断言）

    保守优先：只有**明确**匹配无产出语义、且**不**匹配有产出语义时，才返回 "none"。
    """
    if not summary:
        return "unknown"
    text = summary.lower()

    # 先看「有产出」标记 —— 它的优先级更高
    for p in _HAS_OUTPUT_PATTERNS:
        if p.lower() in text:
            return "has"

    # 「无产出」判定：必须命中**明确的**无产出表述。
    # 额外排除否定式歧义：「没有新增」这类句子里可能同时含「无新增」的变体，
    # 只有命中下面这些**肯定式**表述才算「明确无产出」。
    for p in _NO_OUTPUT_PATTERNS:
        if p.lower() in text:
            return "none"

    return "unknown"


def _load_history(env, job_id, limit=20):
    """拉取某个 job 的历史运行记录（用于检查历史投递失败）。

    `cron list --json` 只给「最近一次」的状态，看不到历史。
    历史里藏着「ok 但没投出去」的静默失败 —— 断言只看 lastRun 就会漏。
    """
    try:
        r = subprocess.run(
            ["openclaw", "cron", "runs", "--id", job_id, "--limit", str(limit)],
            capture_output=True, text=True, timeout=90, env=env,
        )
        d = json.loads(r.stdout[r.stdout.find("{"):])
        return d.get("runs", d.get("entries", []))
    except Exception:
        return None  # 取不到不算失败，但不能当成「没问题」


def main() -> int:
    # 日志降噪：每次 CLI 调用会重新注册全部插件，INFO 级打 6+ 行
    # 「feishu_xxx: Registered ...」，占当日日志 87%。
    # 实测 27008 字节/次 → 5735 字节/次（-79%）。
    # 注意：不能改 openclaw.json 里的 logging 段（那会触发 loadConfig 递归），
    # 只能用环境变量，且只影响本子进程。
    env = dict(os.environ)
    env.setdefault("OPENCLAW_LOG_LEVEL", "warn")
    try:
        r = subprocess.run(
            ["openclaw", "cron", "list", "--json"],
            capture_output=True, text=True, timeout=90, env=env,
        )
        d = json.loads(r.stdout[r.stdout.find("{"):])
    except Exception as e:
        print(f"FAIL cron 状态读取 — {type(e).__name__}")
        return 1

    jobs = d if isinstance(d, list) else d.get("jobs", d.get("data", []))
    failed = 0

    for j in jobs:
        name = j.get("name", "?")
        dl = j.get("delivery", {}) or {}
        st = j.get("state", {}) or {}
        mode = dl.get("mode")
        run_status = st.get("lastRunStatus")

        # 从未运行过的新任务不算失败（没有历史 ≠ 出故障）
        if run_status is None and not st.get("lastRunAtMs"):
            print(f"WARN {name} 尚未运行过（新建任务，等待首次执行）")
            continue

        if run_status != "ok":
            print(f"FAIL {name} 运行失败（lastRunStatus={run_status}）")
            failed += 1
        elif mode == "announce":
            job_id = j.get("id") or j.get("jobId")
            # E55：先问「本该发什么」—— 无产出则不投递是正确的
            output_kind = "unknown"
            if job_id:
                runs_now = _load_history(env, job_id, limit=1)
                if runs_now:
                    output_kind = _classify_output(runs_now[0].get("summary"))
            if output_kind == "none":
                print(f"PASS {name} 无产出，未投递是正确的（E55：本该发什么）")
            elif st.get("lastDelivered"):
                print(f"PASS {name} 投递正常（delivered=True）")
            else:
                print(
                    f"FAIL {name} 投递未送达！"
                    f"（delivery={st.get('lastDeliveryStatus')}, to={dl.get('to', '-')}）"
                )
                failed += 1
        else:
            print(f"PASS {name} 运行正常（no-deliver）")

        # ---- 历史投递检查 ----
        # lastRunStatus 只反映「最近一次」，历史上「ok 但没投出去」的静默失败会被漏掉。
        # 实测（2026-10-27）：daily-reflection 有 2 次 ok-but-not-delivered
        # （10-26 00:35、10-21 04:27），另有 40 次历史 error —— lastRun 看起来全绿。
        job_id = j.get("id") or j.get("jobId")
        if mode == "announce" and job_id:
            runs = _load_history(env, job_id)
            if runs is None:
                print(f"WARN {name} 历史投递状态读取失败（无法判断，非失败）")
                continue
            undelivered = [
                r for r in runs
                if r.get("status") == "ok" and r.get("delivered") is False
            ]
            if undelivered:
                ts = []
                for r in undelivered[:5]:
                    ms = r.get("runAtMs")
                    ts.append(
                        datetime.datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M")
                        if isinstance(ms, (int, float)) else "?"
                    )
                # E55：逐条判「本该发什么」—— 无产出的那些不算故障
                silent_faults = []
                intentional = []
                for r in undelivered:
                    kind = _classify_output(r.get("summary"))
                    if kind == "none":
                        intentional.append(r)
                    else:
                        silent_faults.append(r)

                # 最近一次仍是坏的 → FAIL；历史遗留但最近已好 → WARN
                if runs and runs[0].get("status") == "ok" and runs[0].get("delivered") is False:
                    if silent_faults:
                        print(
                            f"FAIL {name} 最近一次运行未投递！"
                            f"（历史 {len(silent_faults)}/{len(runs)} 次未投递）"
                        )
                        failed += 1
                    else:
                        print(
                            f"PASS {name} 最近一次未投递但无产出"
                            f"（有意不发，E55；历史 {len(intentional)} 次均为无产出）"
                        )
                else:
                    if silent_faults:
                        print(
                            f"WARN {name} 历史上有 {len(silent_faults)}/{len(runs)} 次"
                            f"「运行成功但未投递」（最近一次正常）：{', '.join(ts)}"
                        )
                    else:
                        print(
                            f"PASS {name} 历史未投递均为无产出（有意不发，E55）"
                            f"（{len(intentional)} 次）"
                        )
            else:
                recent_errors = sum(1 for r in runs if r.get("status") == "error")
                if recent_errors:
                    print(
                        f"WARN {name} 最近 {len(runs)} 次里有 {recent_errors} 次 error"
                        f"（最近一次正常）"
                    )
                else:
                    print(f"PASS {name} 历史投递均正常（近 {len(runs)} 次）")

    # 供 shell 统计失败数
    print(f"__FAILED__={failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
