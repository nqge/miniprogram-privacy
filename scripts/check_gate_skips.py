#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【G】组判据真身 —— 额度闸门的「跳过」信号有没有人接住。

2026-11-16 (E74) 新增。

## 为什么需要这一组

2026-11-16 上了**额度闸门架构**：systemd timer → `daily_learning_gate.py`（纯脚本，
不烧模型额度）→ 预检通过才 `openclaw cron run` 触发 agentTurn。
闸门把「额度耗尽」从「跑 300s 空转、零产出」变成「秒级跳过、不启动 agent」。

**但闸门太安静了**：它把 `GATE_RESULT=skipped reason=quota` 写进
`memory/healthcheck-logs/<name>-gate-*.txt`，**而巡检从来不读那些文件**。

→ 于是：如果额度**长期**枯竭，闸门会一天一天地跳过，
**巡检一条都不会提** —— 这正是 E71 那个坑的翻版：
「信号算出来了、写进日志了，但没接到『该修』的那根线上」。
E71 是「连续失败被判成 WARN」，本条是「**连续跳过连日志都没人读**」。

→ 判据：读闸门日志，统计**最近连续跳过次数**，
连续 ≥3 次 → FAIL（额度枯竭，需要牛哥哥充值）；
只有 1-2 次 → 正常波动（WARN 或不报）。

## 判据细节

- 日志文件名：`memory/healthcheck-logs/{daily-learning,daily-reflection}-gate-YYYY-MM-DD-HHMMSS.txt`
- 每份日志里有一行 `GATE_RESULT=<ok|skipped|blocked|triggered|error> ...`
- **按时间升序**读（文件名里的时间戳），只取**最新 N 份**（默认 14）
- 「连续跳过」= 从最新一份往回数，连续是 `skipped` 的份数
  （中间断一个 `ok`/`triggered` 就停 —— 跟 E71 的「连续失败」同口径）
- **没有日志** → 不报（闸门还没跑过，不是故障）

## 用法

    python3 scripts/check_gate_skips.py            # 判据
    python3 scripts/check_gate_skips.py --self-test # 自检
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

LOG_DIR = Path("/root/.openclaw/workspace/memory/healthcheck-logs")
# 连续跳过多少次算「需要有人管」
# ⚠️ 单位要说清楚：**3 条记录**，不是 3 天。
#    阈值语义其实是「连续枯竭 3 天」—— 但只看条数会把
#    「同一起事故被重试 3 遍」误判成「枯竭 3 天」（E78）。
#    → 真正的判定还要过 SPAN_MIN_DAYS / SPAN_MIN_HOURS 两道跨度下限。
FAIL_CONSECUTIVE = 3
# ---- E78：跨度下限（把「次数」还原成「时长」）----
# 连续跳过那一段必须**跨 ≥2 个自然日**，或首末条间隔 **>6 小时**，
# 才算「持续枯竭」（可以占用额度告警名额）。
# 同一天内的多次跳过 = 上游过载重试 → 只报 WARN，不占额度名额。
SPAN_MIN_DAYS = 2
SPAN_MIN_HOURS = 6
# 最多读最近多少份日志
MAX_LOGS = 14
# 认哪些闸门（文件名前缀）
GATE_NAMES = ("daily-learning-gate", "daily-reflection-gate")

# ⚠️ 不能用 `^GATE_RESULT=` —— 真实日志行是
#    `[daily-learning-gate] GATE_RESULT=skipped reason=quota`
#    行首是前缀，不是 GATE_RESULT。
#    2026-11-16 写第一版时用了 `^` + re.M → 故障注入时判据**装死**
#    （注入 3 份 skipped 日志，判据仍报「尚未运行过」）→ 当场抓到。
_RE_RESULT = re.compile(r"GATE_RESULT=(\S+)")
# ⚠️ 2026-11-16 加：reason 也要抓出来。
#   跳过有**三种原因**（quota / upstream_busy / rate_limited），
#   处置完全不同：quota 要充值，upstream_busy 等 1-5 分钟就好。
#   只看 result → 529 过载连续 3 次也会被报成「额度长期枯竭，需要充值」
#   = 把「上游抖」误报成「牛哥哥该掏钱」（E66 假警报）。
_RE_REASON = re.compile(r"reason=(\S+)")


def _norm_ts(ts: str) -> str:
    """把两种时间戳格式归一成**可比较**的同一形式。

    ⚠️ 2026-11-16 实测 bug：两个数据源的 ts 格式**不同**：
        日志文件名：  2026-11-16-174715      （'-' 分隔时分秒）
        历史 jsonl：  2026-11-16T18:09:12    （'T' 分隔）
    直接按字符串排序会**交错错位** ——
    因为 ASCII 里 '-' (0x2D) < 'T' (0x54)，
    所以 `2026-11-16-174715` 排在 `2026-11-16T09:00:00` **前面**，
    而实际上 17:47 比 09:00 **晚**。
    → 结果：「连续跳过」的计数建立在**错误的顺序**上，
      一次真实的跳过可能被后面的成功记录「挤掉」→ **漏报**。

    修法：统一转成 `YYYY-MM-DD-HHMMSS`（定长、可字典序比较）。
    """
    t = (ts or "").strip()
    if not t:
        return ""
    t = t.replace("T", " ")
    # 2026-11-16 18:09:12 → 2026-11-16-180912
    t = t.replace(":", "").replace(" ", "-")
    # 已经是 2026-11-16-174715 的形态则原样
    return t


# 结构化 reason 标签（与闸门侧 daily_learning_gate._classify_reason 同规则）
_STRUCTURED_REASONS = ("quota", "upstream_busy", "rate_limited", "unknown")


def _classify_reason_text(reason: str) -> str:
    """把原始预检结论文本归一成结构化 reason 标签。

    ⚠️ 规则**必须和闸门侧一致**（daily_learning_gate._classify_reason），
    否则「旧记录说 upstream_busy、判据说 quota」→ 措辞自相矛盾。
    → 这里不 import 闸门模块（判据要能独立跑），而是**复制同一套规则**，
      并用单测断言两者一致（见 test_gate_skip_reasons.py）。
    """
    r = (reason or "").strip()
    if not r:
        # 与闸门侧一致：空 reason → "unknown"（**不是** quota）
        # ⚠️ 空 reason 绝不能当成 quota —— 那是凭空指控「该充值」（E74/E66）
        return "unknown"
    if r in _STRUCTURED_REASONS:
        return r
    low = r.lower()
    if "529" in low or "过载" in low or "繁忙" in low or "overload" in low:
        return "upstream_busy"
    if "普通限流" in low or "rate limit" in low or "限流" in low:
        return "rate_limited"
    if "额度" in low or "quota" in low or "token plan" in low:
        return "quota"
    return "unknown"


def _ts_to_epoch(ts: str):
    """把归一化后的 ts（YYYY-MM-DD-HHMMSS）转成 epoch 秒；失败返回 None。

    用于跨源去重时比较「两条记录是不是同一次决策」。
    """
    t = (ts or "").strip()
    if len(t) != 17:      # len("2026-11-16-090000")
        return None
    try:
        import datetime
        return datetime.datetime.strptime(t, "%Y-%m-%d-%H%M%S").timestamp()
    except Exception:
        return None


def parse_result(text: str) -> str | None:
    """从一份闸门日志里取 GATE_RESULT 的值；取不到返回 None。"""
    m = _RE_RESULT.search(text or "")
    return m.group(1) if m else None


def parse_reason(text: str) -> str | None:
    """从一份闸门日志里取 reason=xxx 的值；取不到返回 None。

    ⚠️ 老日志（本次修复之前写的）**没有**结构化 reason → 返回 None。
    → 判据必须能处理 None（当成「原因未知」），不能崩，也不能当成 quota。
    """
    m = _RE_REASON.search(text or "")
    return m.group(1) if m else None


def collect_logs(log_dir: Path = LOG_DIR, names=GATE_NAMES, limit: int = MAX_LOGS):
    """返回 [(闸门名, 时间戳字符串, GATE_RESULT), ...]，按时间升序。

    ⚠️ 第一位是**闸门名**（如 `daily-learning-gate`），不是文件名 ——
    分组要按「哪个闸门」而不是「哪份日志」，
    否则每份日志自成一组，「连续跳过」永远算成 1 次
    （2026-11-16 写第一版时踩了这个坑，自检当场抓住）。

    时间戳取文件名里的 `YYYY-MM-DD-HHMMSS`（TOOLS.md 已记：
    日志轮转按大小不按天，mtime 判龄不可靠 → 只信文件名里的槽位日期）。
    """
    out = []
    if not log_dir.exists():
        return out
    for p in log_dir.iterdir():
        if not p.is_file():
            continue
        fname = p.name
        gate = None
        for n in names:
            if fname.startswith(n + "-"):
                gate = n
                break
        if gate is None:
            continue
        m = re.search(r"-(\d{4}-\d{2}-\d{2}-\d{6})\.txt$", fname)
        if not m:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        res = parse_result(text)
        if res is None:
            continue
        out.append((gate, _norm_ts(m.group(1)), res, parse_reason(text)))
    out.sort(key=lambda x: x[1])  # 按时间升序（已归一化，可字典序比较）
    return out[-limit:]


def collect_history(path: Path = None, limit: int = 200):
    """读持久化的闸门历史 `memory/gate-history.jsonl`。

    为什么要有这个：闸门日志在 `memory/healthcheck-logs/` 下，
    那个目录**在 .gitignore 里**（日志不是源码）且只保留 30 天
    → 机器重建或清理会把闸门历史抹掉，而【G2】要「连续 3 次跳过」才报警
    → 3 次 = 3 天，期间丢一次就再也攒不出来。

    → 闸门另存一份精简的结构化记录（日期 + result + reason），可进 git。

    返回 [(闸门名, ts字符串, result, reason), ...]，按时间升序。
    """
    if path is None:
        path = Path("/root/.openclaw/workspace/memory/gate-history.jsonl")
    out = []
    if not path.exists():
        return out
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            job = r.get("job")
            res = r.get("result")
            ts = r.get("ts") or ""
            if not job or not res or not ts:
                continue
            # 闸门名统一成 daily-learning-gate / daily-reflection-gate
            gate = job if job.endswith("-gate") else f"{job}-gate"
            out.append((gate, _norm_ts(ts), res, r.get("reason") or ""))
    except Exception:
        return out
    out.sort(key=lambda x: x[1])
    return out[-limit:]


def judge(logs):
    """判定闸门跳过情况。

    logs: [(name, ts, result), ...]（时间升序）
    返回 (problems, warns, detail)
    """
    problems = []
    warns = []
    detail = {}

    # 按闸门名字分别统计「最近一段连续跳过」
    by_name: dict[str, list] = {}
    for row in logs:
        name, ts, res = row[0], row[1], row[2]
        reason = row[3] if len(row) > 3 else None
        by_name.setdefault(name, []).append((ts, res, reason))

    for name, rows in sorted(by_name.items()):
        # ⚠️ 2026-11-17 修正：**同一次闸门决策可能留下两条记录**
        #   （旧版闸门一次失败会写 `blocked` + `skipped` 两条，时间戳相同）。
        #   → 序列变成 skipped, blocked, skipped, blocked, ...
        #   → 「连续跳过」被 blocked 打断 → 真实的 3 次连跳被算成 1 次 → **漏报**。
        #   → 判据层也要能读**历史遗留**的旧记录：
        #     把 `blocked` 归一化成 `skipped`（两者都表示「闸门没让 agentTurn 跑」）。
        #     闸门侧已改成「一次决策一条记录」，但旧历史还在，归一化让两者都算对。
        # rows 里每条是 (ts, res, reason)
        #
        # 归一化 + **去重**：
        #   旧版闸门一次失败写两条（同时间戳）：
        #     blocked (原始预检文本) + skipped (分类后的 reason)
        #   两者都表示「闸门没让 agentTurn 跑」→ 都算 skipped，
        #   但**同一次决策只能算一次** → 同时间戳的重复要合并。
        #   合并时**优先保留 skipped 那条**（它的 reason 是分类过的，
        #   比 blocked 的原始预检文本干净 —— 原始文本会污染措辞）。
        _by_ts: dict = {}
        _order: list = []
        for (ts, res, reason) in rows:
            if res == "blocked":
                res = "skipped"
                # 旧版 blocked 记录里的 reason 是**原始预检结论文本**
                # （如「结论：**上游瞬时过载**（不是额度问题）—— 重试后仍不通」），
                # 措辞很脏。→ 用**同一套分类规则**归一成结构化标签。
                reason = _classify_reason_text(reason)
            if ts in _by_ts:
                # 同时间戳已有一条 → 合并（保留有 reason 的那条）
                prev_res, prev_reason = _by_ts[ts]
                if not prev_reason and reason:
                    _by_ts[ts] = (res, reason)
                continue
            _by_ts[ts] = (res, reason)
            _order.append(ts)
        rows = [(ts, _by_ts[ts][0], _by_ts[ts][1]) for ts in _order]

        # 从最新往回数连续 skipped（**当前**还在跳的这段）
        consecutive = 0
        reasons: list[str] = []
        for _ts, res, reason in reversed(rows):
            if res == "skipped":
                consecutive += 1
                if reason:
                    reasons.append(reason)
            else:
                break

        # ⚠️ 2026-11-16 18:25 补（关键修正）：
        #   上面那段只覆盖「**现在还在跳**」的情况。
        #   但闸门连跳 3 天、**第 4 天恢复了**的形态更常见 ——
        #   而那时 `consecutive` = 0 → **完全不报** →
        #   「额度枯竭了 3 天」这件事被恢复**掩盖**了。
        #
        #   这正是我 09:15 自己犯过的那个 bug（`_outage_span_days` 用
        #   「最新一次运行 − 最后一次成功」当跨度 → 任务一恢复，
        #   6 天的中断被缩成 1 天，正好抹掉最该留痕的那段）。
        #   → **连续段必须独立地找，不能只从最新一条往回数。**
        #
        #   判据：扫**整段历史**，找**最近的一段**连续 skipped（长度 ≥ 阈值即报），
        #   并区分「还在跳」和「已恢复」两种措辞。
        best_streak = 0          # 窗口内最近的一段连续跳过长度
        best_end_idx = -1        # 那段的结束下标（最新那条在最后）
        cur = 0
        for i, (_ts, res, _r) in enumerate(rows):
            if res == "skipped":
                cur += 1
            else:
                cur = 0
            if cur >= best_streak:
                best_streak = cur
                best_end_idx = i
        # 该段里的原因
        streak_reasons: list[str] = []
        if best_streak > 0:
            for (_ts, res, reason) in rows[best_end_idx - best_streak + 1: best_end_idx + 1]:
                if res == "skipped" and reason:
                    streak_reasons.append(reason)
        recovered = best_streak >= FAIL_CONSECUTIVE and consecutive < FAIL_CONSECUTIVE

        # ⚠️ 2026-11-18（E78）：**次数必须换算成时长**。
        #   只数条数 → 「同一起上游 529 被 timer 重试 3 遍」= 27 分钟，
        #   却被当成「连续 3 天枯竭」→ 占用「该充值」的告警名额 → 信号稀释。
        #   → 这里算出那段连续跳过的**首末时间戳 + 跨度**，
        #     供下面判断「是持续枯竭，还是一次事故的重试」。
        streak_span_days = 0
        streak_span_hours = 0.0
        streak_start_ts = None
        streak_end_ts = None
        if best_streak > 0 and best_end_idx >= 0:
            seg = rows[best_end_idx - best_streak + 1: best_end_idx + 1]
            streak_start_ts = seg[0][0]
            streak_end_ts = seg[-1][0]
            e0 = _ts_to_epoch(streak_start_ts)
            e1 = _ts_to_epoch(streak_end_ts)
            if e0 is not None and e1 is not None and e1 >= e0:
                streak_span_hours = (e1 - e0) / 3600.0
                # 自然日跨度：首末日期不同则至少跨 1 天；
                # 要「≥2 个自然日」需要首末日期差 ≥1 天（end_date - start_date ≥ 1）
                # ⚠️ ts 格式是 `YYYY-MM-DD-HHMMSS`（带连字符），不是 YYYYMMDD
                d0 = streak_start_ts[:10].replace("-", "")   # YYYYMMDD
                d1 = streak_end_ts[:10].replace("-", "")
                try:
                    import datetime
                    dd0 = datetime.datetime.strptime(d0, "%Y%m%d").date()
                    dd1 = datetime.datetime.strptime(d1, "%Y%m%d").date()
                    streak_span_days = (dd1 - dd0).days + 1
                except Exception:
                    streak_span_days = 0

        # 达到条数阈值 **且** 跨度够长 → 才是「持续枯竭」
        span_ok = (streak_span_days >= SPAN_MIN_DAYS) or (streak_span_hours > SPAN_MIN_HOURS)
        sustained = best_streak >= FAIL_CONSECUTIVE and span_ok
        # 条数够但跨度不够 → 一次事故的重试，不是持续枯竭
        retry_only = best_streak >= FAIL_CONSECUTIVE and not span_ok

        detail[name] = {
            "n_logs": len(rows),
            "consecutive_skipped": consecutive,
            "best_streak": best_streak,
            "best_streak_end_ts": rows[best_end_idx][0] if best_end_idx >= 0 else None,
            "best_streak_start_ts": streak_start_ts,
            "streak_span_days": streak_span_days,
            "streak_span_hours": round(streak_span_hours, 2),
            "span_ok": span_ok,
            "sustained": sustained,
            "retry_only": retry_only,
            "recovered": recovered,
            "last_result": rows[-1][1],
            "last_ts": rows[-1][0],
            "reasons": sorted(set(reasons)),
            "streak_reasons": sorted(set(streak_reasons)),
        }

        # ⚠️ 2026-11-16：措辞必须**忠实反映跳过原因**。
        #   三种原因的处置完全不同：
        #     · quota         → 需要牛哥哥充值（长期枯竭）
        #     · upstream_busy → 上游过载，等 1-5 分钟就恢复（不是充值问题）
        #     · rate_limited  → 普通限流，等一会就好（不是充值问题）
        #   全写成「额度不足 → 需要充值」= 把上游抖动误报成要掏钱（E66 假警报）。
        #   → **全部是 quota** 才说「需要充值」；否则说「上游侧原因」。
        # ⚠️ 老日志（修复前写的）没有结构化 reason → reasons 为空。
        #   **不能**当成 quota（那是凭空指控「该充值」），也不能说成「上游侧」（那是凭空归因）。
        #   → 老实说「原因未记录」。
        uniq = sorted(set(streak_reasons or reasons))
        all_quota = bool(uniq) and all(r == "quota" for r in uniq)
        any_quota = "quota" in uniq
        if sustained:
            if all_quota:
                why = "额度长期枯竭，需要充值（E74）"
            elif any_quota:
                why = f"原因混合（额度 + 其他：{','.join(uniq)}），其中含额度不足（E74）"
            elif not uniq:
                why = ("跳过原因**未记录**（旧格式日志没有结构化 reason）"
                       "→ 不能据此断言该充值，请先看预检结论（E74）")
            elif uniq == ["unknown"]:
                why = "跳过原因**未知**（reason=unknown）→ 先看预检结论，暂不判充值（E74）"
            else:
                # ⚠️ 措辞要**自证分类**（2026-11-17 实测踩到）：
                #   这条 FAIL 会被 healthcheck 的 is_waiting() 分栏。
                #   原来只写「不用充值，先看上游是否恢复」→
                #   既不含「Token Plan」也不含「额度耗尽」→ 被归进「**可处理**」→
                #   但上游过载**谁都不用做什么**（等 1-5 分钟就好，而且已经恢复了）→
                #   → 长期性假警报。
                #   → 明确写「**无需处理**」，并让 is_waiting() 能认出来。
                why = (f"跳过原因**不是额度**（上游侧：{','.join(uniq)}）"
                       "→ 不用充值，先看上游是否恢复（E74）"
                       "；**上游瞬时过载，无需处理**，等上游恢复即可")
            if recovered:
                # 措辞要区分：**已经恢复了**，但历史里有一段连续跳过
                problems.append(
                    f"{name} 最近曾连续 {best_streak} 次被闸门跳过"
                    f"（{streak_start_ts} → {streak_end_ts}，"
                    f"跨 {streak_span_days} 天/{streak_span_hours}小时，"
                    f"截至 {detail[name]['best_streak_end_ts']}，现已恢复）"
                    f" -- {why}")
            else:
                problems.append(
                    f"{name} 最近连续 {consecutive} 次被闸门跳过"
                    f"-- 最后一次 {rows[-1][0]}，已连续跳过 {consecutive} 次；{why}"
                )
        elif retry_only:
            # ⚠️ E78：条数够但**跨度不够** → 这不是持续枯竭，
            #   是一次事故被 timer 重试了 N 遍 → 只报 WARN，**不占用额度告警名额**。
            #   措辞必须把**首末时间戳**打出来（E78 教训③），
            #   让读的人一眼看出这是「27 分钟」还是「3 天」。
            rs = ",".join(sorted(set(streak_reasons)))
            span_desc = (f"{streak_start_ts} → {streak_end_ts}"
                         f"（仅 {round(streak_span_hours, 2)} 小时 / "
                         f"{streak_span_days} 个自然日）")
            warns.append(
                f"{name} 曾连续 {best_streak} 次被闸门跳过，但**跨度仅 {span_desc}**"
                f" → 这是**一次事故的重试**，不是持续枯竭，不占用额度告警名额（E78）"
                + (f"；原因 {rs}" if rs else "")
            )
        elif best_streak >= 2:
            # 曾经连续跳过 2 次（不到阈值），但**已经恢复** → 值得提一句
            rs = ",".join(sorted(set(streak_reasons))) if streak_reasons else "未记录"
            warns.append(
                f"{name} 最近曾连续 {best_streak} 次被闸门跳过（原因 {rs}），现已恢复"
            )
        elif consecutive >= 1:
            rs = ",".join(sorted(set(reasons))) if reasons else "未记录"
            warns.append(
                f"{name} 最近一次被闸门跳过（原因 {rs}），连续 {consecutive} 次"
            )

    return problems, warns, detail


def self_test() -> int:
    """自检：确保判据没被改坏。返回 0 = 通过。"""
    # 1) 解析 GATE_RESULT
    assert parse_result("foo\nGATE_RESULT=skipped reason=quota\n") == "skipped"
    assert parse_result("GATE_RESULT=ok reason=gate_passed") == "ok"
    assert parse_result("nothing here") is None

    # 2) 连续 3 次跳过 → FAIL
    logs = [
        ("daily-learning-gate", "2026-11-16-090000", "skipped"),
        ("daily-learning-gate", "2026-11-17-090000", "skipped"),
        ("daily-learning-gate", "2026-11-18-090000", "skipped"),
    ]
    p, w, d = judge(logs)
    assert p, "连续 3 次跳过应 FAIL"
    assert "连续 3 次" in p[0], p[0]
    # E78：这条必须是「持续枯竭」（跨度够）而不是「一次事故的重试」
    dd = d["daily-learning-gate"]
    assert dd["sustained"] is True, dd
    assert dd["span_ok"] is True, dd
    assert dd["streak_span_days"] >= SPAN_MIN_DAYS, dd

    # 2b) ⚠️ **E78 核心用例**：条数够但跨度不够 → 只报 WARN，**不占额度名额**
    #     这三条是**同一天早上、27 分钟内**的 3 次重试（真实翻车数据）。
    #     旧实现会把它当成「连续 3 天枯竭」→ 占用额度告警名额。
    logs_retry = [
        ("daily-learning-gate", "2026-11-17-090152", "skipped"),
        ("daily-learning-gate", "2026-11-17-092039", "skipped"),
        ("daily-learning-gate", "2026-11-17-092823", "skipped"),
    ]
    pr, wr, dr = judge(logs_retry)
    assert not pr, f"27 分钟内的 3 次重试**不应**占用额度告警名额: {pr}"
    assert wr, "同一天内的重试应给 WARN"
    drr = dr["daily-learning-gate"]
    assert drr["best_streak"] == 3, drr
    assert drr["sustained"] is False, drr
    assert drr["retry_only"] is True, drr
    assert drr["span_ok"] is False, drr
    # 措辞必须把首末时间戳打出来（E78 教训③）
    assert "2026-11-17-090152" in wr[0] and "2026-11-17-092823" in wr[0], wr[0]
    assert "E78" in wr[0], wr[0]

    # 2c) 跨度刚过 6 小时线 → 算持续（阈值另一条路径）
    logs_span_ok = [
        ("daily-learning-gate", "2026-11-16-090000", "skipped"),
        ("daily-learning-gate", "2026-11-16-160000", "skipped"),
        ("daily-learning-gate", "2026-11-16-170000", "skipped"),
    ]
    ps, ws, ds = judge(logs_span_ok)
    assert ps, f"首末间隔 >6 小时应算持续枯竭: {ps}"
    assert ds["daily-learning-gate"]["span_ok"] is True, ds

    # 3) 中间断一个 ok → 不算连续 → 不 FAIL
    logs2 = [
        ("daily-learning-gate", "2026-11-16-090000", "skipped"),
        ("daily-learning-gate", "2026-11-17-090000", "ok"),
        ("daily-learning-gate", "2026-11-18-090000", "skipped"),
    ]
    p2, w2, d2 = judge(logs2)
    assert not p2, f"断了就不算连续: {p2}"
    assert d2["daily-learning-gate"]["consecutive_skipped"] == 1

    # 4) 只有 1-2 次 → 不 FAIL
    logs3 = [
        ("daily-learning-gate", "2026-11-17-090000", "skipped"),
        ("daily-learning-gate", "2026-11-18-090000", "skipped"),
    ]
    p3, w3, _ = judge(logs3)
    assert not p3, p3
    assert w3, "1-2 次跳过应给 WARN"

    # 5) 完全没有跳过 → 不报
    logs4 = [
        ("daily-learning-gate", "2026-11-16-090000", "triggered"),
        ("daily-learning-gate", "2026-11-17-090000", "ok"),
    ]
    p4, w4, _ = judge(logs4)
    assert not p4 and not w4, (p4, w4)

    # 6) 两个闸门分开统计（互不干扰）
    logs5 = [
        ("daily-learning-gate", "2026-11-16-090000", "skipped"),
        ("daily-reflection-gate", "2026-11-16-090000", "triggered"),
    ]
    p5, w5, d5 = judge(logs5)
    assert not p5, p5
    assert d5["daily-learning-gate"]["consecutive_skipped"] == 1
    assert d5["daily-reflection-gate"]["consecutive_skipped"] == 0

    # 7) collect_logs 对不存在的目录不炸
    assert collect_logs(Path("/nonexistent/dir")) == []

    # 8) ⚠️ **故障注入**：造真的日志文件，验证 collect_logs + judge 全链路能认出来
    #    （2026-11-16 第一版的 bug：正则用了 `^GATE_RESULT=` + re.M，
    #     而真实行是 `[daily-learning-gate] GATE_RESULT=skipped ...` → 行首有前缀，
    #     `^` 匹配不上 → **注入 3 份 skipped 日志，判据仍报「尚未运行过」= 装死**。
    #     这个用例就是为了防止它再装死。）
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for d in ("2026-11-16", "2026-11-17", "2026-11-18"):
            (tmp / f"daily-learning-gate-{d}-090000.txt").write_text(
                f"===== daily-learning-gate {d} 09:00:00 =====\n"
                f"[daily-learning-gate] GATE_RESULT=skipped reason=quota\n"
                f"EXIT=0\n",
                encoding="utf-8")
        got = collect_logs(log_dir=tmp)
        assert len(got) == 3, f"应读到 3 份日志，实际 {got}"
        assert all(g[2] == "skipped" for g in got), got
        pp, ww, dd = judge(got)
        assert pp, "注入的连续跳过必须被认出来（否则断言装死）"
        assert dd["daily-learning-gate"]["consecutive_skipped"] == 3, dd

    # 9) 同样注入但只有 2 次 → 不 FAIL（阈值 3）
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for d in ("2026-11-17", "2026-11-18"):
            (tmp / f"daily-learning-gate-{d}-090000.txt").write_text(
                f"[daily-learning-gate] GATE_RESULT=skipped reason=quota\n", encoding="utf-8")
        pp, ww, dd = judge(collect_logs(log_dir=tmp))
        assert not pp, pp
        assert ww, "2 次跳过应给 WARN"

    # 10) 持久化历史：能读出来 + 名字归一 + 坏行不炸
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        hp = Path(td) / "gate-history.jsonl"
        hp.write_text(
            '{"ts": "2026-11-16T09:00:00", "job": "daily-learning", "result": "skipped", "reason": "quota"}\n'
            '{"ts": "2026-11-17T09:00:00", "job": "daily-learning", "result": "skipped", "reason": "quota"}\n'
            '{"ts": "2026-11-18T09:00:00", "job": "daily-learning", "result": "skipped", "reason": "quota"}\n'
            '这行是坏的\n'
            '{"ts": "2026-11-19T09:00:00", "job": "daily-reflection", "result": "triggered"}\n',
            encoding="utf-8")
        h = collect_history(path=hp)
        assert len(h) == 4, f"坏行应被跳过，应读到 4 条，实际 {len(h)}"
        assert h[0][0] == "daily-learning-gate", h[0]
        assert h[0][2] == "skipped" and h[0][3] == "quota", h[0]
        assert h[3][0] == "daily-reflection-gate", h[3]
        assert h[3][3] == "", h[3]
        # 连续 3 次跳过 → FAIL（和日志来源同口径）
        pp, ww, dd = judge(h)
        assert pp, "历史里的连续 3 次跳过也必须被认出来"
        assert dd["daily-learning-gate"]["consecutive_skipped"] == 3, dd

    # 11) 不存在的历史文件不炸
    assert collect_history(path=Path("/nonexistent/gate-history.jsonl")) == []

    # 12) ⚠️ 关键回归：**连跳 N 次后恢复**，不能因为恢复就不报
    #   这正是我 09:15 自己犯过的 bug（`_outage_span_days` 用
    #   「最新一次 − 最后一次成功」当跨度 → 任务一恢复，6 天的中断被缩成 1 天，
    #   正好抹掉最该留痕的那段）。
    #   → 连续段必须**独立地扫整段历史**找出来，不能只从最新一条往回数。
    rows_recover = [
        ("daily-learning-gate", "2026-11-17-090000", "skipped", "quota"),
        ("daily-learning-gate", "2026-11-18-090000", "skipped", "quota"),
        ("daily-learning-gate", "2026-11-19-090000", "skipped", "quota"),
        ("daily-learning-gate", "2026-11-20-090000", "ok", "gate_passed"),
    ]
    pr, wr, dr = judge(rows_recover)
    assert pr, f"连跳 3 次后恢复也必须报（别让恢复掩盖中断）: {pr} {wr}"
    assert "现已恢复" in pr[0], pr[0]
    assert dr["daily-learning-gate"]["best_streak"] == 3, dr
    assert dr["daily-learning-gate"]["recovered"] is True, dr
    assert dr["daily-learning-gate"]["consecutive_skipped"] == 0, dr

    # 13) 恢复后的措辞不能指控充值，除非 reason 确实是 quota
    rows_busy = [
        ("daily-learning-gate", "2026-11-17-090000", "skipped", "upstream_busy"),
        ("daily-learning-gate", "2026-11-18-090000", "skipped", "upstream_busy"),
        ("daily-learning-gate", "2026-11-19-090000", "skipped", "upstream_busy"),
        ("daily-learning-gate", "2026-11-20-090000", "ok", "gate_passed"),
    ]
    pb, wb, _ = judge(rows_busy)
    assert pb, "连跳 3 次（上游原因）也要报"
    assert "不用充值" in pb[0] or "不是额度" in pb[0], pb[0]
    assert "需要充值" not in pb[0], f"上游原因不该指控充值: {pb[0]}"

    # 14) 连跳 2 次后恢复 → 不到阈值 → WARN 而不是 FAIL
    rows_two = [
        ("daily-learning-gate", "2026-11-19-090000", "skipped", "quota"),
        ("daily-learning-gate", "2026-11-20-090000", "skipped", "quota"),
        ("daily-learning-gate", "2026-11-21-090000", "ok", "gate_passed"),
    ]
    p2, w2, d2 = judge(rows_two)
    assert not p2, f"2 次不到阈值不该 FAIL: {p2}"
    assert w2, "2 次连跳后恢复应给 WARN"

    print("【G】闸门跳过判据自检: ✅ 全部通过")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(self_test())
    logs = collect_logs()
    # 合并**持久化历史**（memory/gate-history.jsonl）：
    #   日志目录在 .gitignore 里 + 只留 30 天 → 机器重建/清理会丢历史，
    #   而「连续 3 次跳过」= 3 天，丢一次就再也攒不出来。
    #   → 两个来源**合并去重**（按 闸门名+ts+result）后一起判。
    hist = collect_history()
    # 去重 key = (闸门名, 归一化后的 ts)
    # ⚠️ **不带 result** —— 同一个决策可能在两个源里各留一条
    #   （旧版闸门一次失败写 blocked + skipped 两条）。
    #   → 只按 (name, ts) 去重，才不会把**一次决策数成两次**。
    #   → 合并时优先保留 reason 更「干净」的那条（skipped 的分类 reason，
    #     比 blocked 的原始预检文本好用）。
    _by_key: dict = {}
    _order: list = []
    for row in list(logs) + list(hist):
        key = (row[0], row[1])
        reason = row[3] if len(row) > 3 else None
        if key in _by_key:
            prev = _by_key[key]
            # 优先保留「有 reason 且 reason 不是原始预检文本」的那条
            if not prev[3] and reason:
                _by_key[key] = (prev[0], prev[1], prev[2], reason)
            continue
        _by_key[key] = (row[0], row[1], row[2], reason)
        _order.append(key)
    merged = [_by_key[k] for k in _order]

    # ⚠️ 2026-11-17：**跨源**的同一决策去重（旧历史遗留）。
    #   旧版闸门的历史记录用「**写入时刻**」，日志文件名用「**启动时刻**」，
    #   预检跑 ~80s → 同一个决策在两个源里差 ~80 秒 → 上面按 ts 去重**去不掉**
    #   → 一次决策被数成两次（今天实测：日志 09:00:32 vs 历史 09:01:52）。
    #   → 闸门侧已改成统一用启动时刻；但**旧记录还在**，
    #     所以判据侧再加一层：**同一闸门、同一「未放行」结果、且相隔 < 5 分钟**
    #     的两条记录 → 判为同一次决策，合并。
    #     （5 分钟：够覆盖一次预检的耗时，又不至于把连续几天的跳过误合并）
    _DEDUP_WINDOW_SEC = 300
    _merged2: list = []
    for row in merged:
        g, ts, res, reason = row[0], row[1], row[2], (row[3] if len(row) > 3 else None)
        folded = False
        for i, prev in enumerate(_merged2):
            if prev[0] != g or prev[2] != res:
                continue
            # 前一条也是「未放行」类结果才需要合并
            if prev[2] not in ("skipped", "blocked", "error"):
                continue
            try:
                a = _ts_to_epoch(prev[1])
                b = _ts_to_epoch(ts)
            except Exception:
                continue
            if a is None or b is None:
                continue
            if abs(b - a) <= _DEDUP_WINDOW_SEC:
                # 合并：保留 reason 更干净的那条
                if not prev[3] and reason:
                    _merged2[i] = (g, prev[1], prev[2], reason)
                folded = True
                break
        if not folded:
            _merged2.append((g, ts, res, reason))
    merged = _merged2
    # ⚠️ 排序必须用**归一化后**的 ts —— 两个数据源原本格式不同，
    #    直接字符串排序会交错错位（见 _norm_ts 的注释）。
    merged.sort(key=lambda x: x[1])

    problems, warns, detail = judge(merged)
    for p in problems:
        print(f"FAIL {p}")
    for w in warns:
        print(f"WARN {w}")
    if not problems and not warns:
        if merged:
            src = f"（{len(logs)} 条日志 + {len(hist)} 条历史）"
            print(f"PASS 闸门最近 {len(merged)} 次运行无连续跳过{src}（额度正常）")
        else:
            print("PASS 闸门尚未运行过（无日志），无可判定内容")
    print(f"__G_FAILED__={1 if problems else 0}")
    print(f"__G_WARNED__={1 if warns else 0}")
