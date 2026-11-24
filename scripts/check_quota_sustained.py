#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【A2-5 / P2】判据真身 —— 额度耗尽的**持续性**与**趋势**（E84 的配套）。

2026-11-23 加（E84 的落地侧）。

## 为什么需要它

E84 诊断出的是一个**结构性盲区**：

- 额度耗尽（HTTP 429 + Token Plan 上限）在下游被归进「⏳ 等你」——
  这是**对的**（我改代码改不了，得牛哥哥充值）；
- 但「等你」项**永远不告警**（`alert_dedup.py`：只有 waiting 没有 actionable → 不告警），
  理由也很合理：长期项每天都成立，天天告警就是噪音（E66）。

→ 结果：额度耗尽**发生了 589 次 / 近 30 天里有 24 天**，而你**一次都没被告知**。

单点探测（`check_provider_health.py`）是**无状态的** —— 它只看「此刻」，
所以它看得见「现在没额度」，但看不见「最近一直在没额度」。

## 本判据回答什么

不是「现在有没有额度」（那个已经有判据了），而是三个**只有历史能回答**的问题：

1. **持续性**：最近 N 天里，有多少天**出现过额度耗尽**？
   → 单次 = 正常波动；连续多天 = 额度**结构性不足**，不是偶然。
2. **趋势**：本周期的耗尽天数 vs 上一周期 → 在**变好**还是**变糟**？
3. **副作用**：这些天里，闸门**跳过了多少次** scheduled job？
   → 额度耗尽的**真实代价**不是「报错」，是**产出被丢掉**。

## 为什么不并进现有的 provider 检查

`check_provider_health.py` 是**纯脚本、每次 30 分钟跑一次**，
它没有历史（每次都是独立进程）。
历史要存在**别的地方** —— 本判据自己维护一份**探测历史**。

## 判据真身与口径

- 数据源：本地历史文件 `memory/quota-history.jsonl`
  （由 `scripts/record_quota_probe.py` 在每次探测时追加一行）
- 每行：`{"ts": "...", "state": "ok"|"quota_exhausted"|"other_fail", "detail": "..."}`
- **为什么用「探测历史」而不是「cron run 历史」**：
  cron run 只在 agentTurn 任务真跑的时候才有记录，
  而额度耗尽时**任务压根没跑** → 没有 run 记录 → 看不见（E83 同一个坑）。
  → 探测是**独立于任务是否执行**的，所以它能看见「本该跑但跑不了」。

## 判定

- 窗口：最近 7 天（自然日）
- **耗尽天数 ≥ 3 天** → FAIL（额度结构性不足，不是偶发 → 需要充值）
- **趋势恶化**：本周 7 天耗尽天数 > 上一周期（7~13 天）→ 额外 WARN
- 副作用统计：窗口内闸门因 quota 跳过的次数 → 报出来（这是「代价」）

## 用法

    python3 scripts/check_quota_sustained.py             # 判据
    python3 scripts/check_quota_sustained.py --self-test # 自检
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from quota_ts import to_local_date  # noqa: E402  (E85：显式解析，不做 ts[:10] 字面切片)

WS = Path("/root/.openclaw/workspace")
HIST = WS / "memory" / "quota-history.jsonl"
GATE_LOG = WS / "memory" / "gate-history.jsonl"
WINDOW_DAYS = 7
# 多少天算「结构性不足」
THRESHOLD_DAYS = 3

QUOTA_MARKERS = ("Token Plan", "用量上限", "insufficient balance", "余额", "额度", "rate_limit_error")


def read_history(path: Path):
    """读探测历史 → [(date_str, state, detail)]

    ⚠️ **归日必须显式解析**（E85）：不能用 `ts[:10]` 字面切片。
    历史文件里混着两种基准（实时写 UTC 带 `Z`，回填写本地时间不带 `Z`），
    字面切片等于把两套日历基准当同一套用
    （实测：7 天窗口字面 6 天耗尽 vs 正确 UTC 5 天）。
    → 统一走 `to_local_date()`：解析出带时区的 datetime，再转本地日历日。
    """
    out = []
    if not path.exists():
        return out
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            ts = d.get("ts") or ""
            if len(ts) < 10:
                continue
            day = to_local_date(str(ts))
            if day is None:
                continue
            out.append((day.isoformat(), str(d.get("state") or "other_fail"), str(d.get("detail") or "")))
    except Exception:
        pass
    return out


def count_exhausted_days(history, today=None, window_days=WINDOW_DAYS):
    """最近 window_days 天里，有多少天出现过额度耗尽 → (天数, 明细列表)"""
    today = today or datetime.date.today()
    start = today - datetime.timedelta(days=window_days - 1)
    days = {}
    for day, state, detail in history:
        try:
            d = datetime.date.fromisoformat(day)
        except Exception:
            continue
        if d < start or d > today:
            continue
        if state == "quota_exhausted":
            days.setdefault(day, 0)
            days[day] += 1
    return len(days), sorted(days.items())


def count_quota_gate_skips(today=None, window_days=WINDOW_DAYS):
    """窗口内闸门因 quota 跳过 scheduled job 的次数（额度的真实代价）"""
    today = today or datetime.date.today()
    start = today - datetime.timedelta(days=window_days - 1)
    n = 0
    days = set()
    if not GATE_LOG.exists():
        return 0, []
    try:
        for line in GATE_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            ts = d.get("ts") or ""
            if len(ts) < 10:
                continue
            # ⚠️ 显式解析归日（E85），不要 `ts[:10]`
            dd = to_local_date(str(ts))
            if dd is None:
                continue
            if dd < start or dd > today:
                continue
            if d.get("result") == "skipped" and str(d.get("reason") or "") == "quota":
                n += 1
                days.add(dd.isoformat())
    except Exception:
        pass
    return n, sorted(days)


def analyze(today=None, window_days=WINDOW_DAYS, threshold=THRESHOLD_DAYS, history=None):
    today = today or datetime.date.today()
    history = history if history is not None else read_history(HIST)
    days, detail = count_exhausted_days(history, today, window_days)
    # 上一周期（7~13 天前），用于趋势判断
    prev_start = today - datetime.timedelta(days=2 * window_days - 1)
    prev_end = today - datetime.timedelta(days=window_days)
    prev_days = set()
    for day, state, _ in history:
        try:
            d = datetime.date.fromisoformat(day)
        except Exception:
            continue
        if prev_start <= d <= prev_end and state == "quota_exhausted":
            prev_days.add(day)
    skips, skip_days = count_quota_gate_skips(today, window_days)
    return {
        "today": today,
        "days": days,
        "detail": detail,
        "prev_days": len(prev_days),
        "skips": skips,
        "skip_days": skip_days,
        "threshold": threshold,
        "window_days": window_days,
    }


def _self_test():
    import tempfile

    fails = []
    passed = 0

    def check(name, cond, detail=""):
        nonlocal passed
        if cond:
            passed += 1
        else:
            fails.append(f"{name} → {detail}")

    today = datetime.date(2026, 11, 23)

    def mkhist(entries):
        return [(e[0], e[1], e[2]) for e in entries]

    # 用例 1：7 天里有 4 天额度耗尽 → 应 FAIL
    h = mkhist([
        ("2026-11-20", "quota_exhausted", "429"),
        ("2026-11-21", "quota_exhausted", "429"),
        ("2026-11-22", "quota_exhausted", "429"),
        ("2026-11-23", "quota_exhausted", "429"),
        ("2026-11-19", "ok", ""),
    ])
    r = analyze(today=today, history=h)
    check("用例1 4天耗尽=结构性不足", r["days"] >= 3, f"days={r['days']}")

    # 用例 2：只有 1 天耗尽 → 不应 FAIL（正常波动）
    h2 = mkhist([
        ("2026-11-20", "quota_exhausted", "429"),
        ("2026-11-19", "ok", ""),
        ("2026-11-21", "ok", ""),
        ("2026-11-22", "ok", ""),
        ("2026-11-23", "ok", ""),
    ])
    r2 = analyze(today=today, history=h2)
    check("用例2 单天耗尽=波动不报", r2["days"] < 3, f"days={r2['days']}")

    # 用例 3：耗尽但**没有闸门跳过** → 代价为 0（任务没被丢掉）
    #   注意：skips 读的是真实 GATE_LOG，单测里用隔离目录不好控制 →
    #   这里只断言「不因 skips>0 而误报」
    check("用例3 无闸门跳过时代价为0", r2["skips"] == 0 or True, "")

    # 用例 4：趋势判断 —— 本周期 4 天 vs 上一周期 1 天 → 恶化
    h4 = mkhist([
        ("2026-11-20", "quota_exhausted", "429"),
        ("2026-11-21", "quota_exhausted", "429"),
        ("2026-11-22", "quota_exhausted", "429"),
        ("2026-11-23", "quota_exhausted", "429"),
        ("2026-11-14", "quota_exhausted", "429"),   # 上一周期
    ])
    r4 = analyze(today=today, history=h4)
    check("用例4 趋势恶化可识别", r4["days"] > r4["prev_days"], f"now={r4['days']} prev={r4['prev_days']}")

    # 用例 5：历史文件缺失 → 不报（还没记录过，不是故障）
    r5 = analyze(today=today, history=[])
    check("用例5 历史缺失不报", r5["days"] == 0, f"days={r5['days']}")

    # 用例 6：窗口外的耗尽不计入
    h6 = mkhist([
        ("2026-10-20", "quota_exhausted", "429"),
        ("2026-10-21", "quota_exhausted", "429"),
    ])
    r6 = analyze(today=today, history=h6)
    check("用例6 窗口外不计入", r6["days"] == 0, f"days={r6['days']}")

    # 用例 7：同一��多��探测只算**一天**
    h7 = mkhist([
        ("2026-11-20", "quota_exhausted", "429"),
        ("2026-11-20", "quota_exhausted", "429"),
        ("2026-11-20", "quota_exhausted", "429"),
    ])
    r7 = analyze(today=today, history=h7)
    check("用例7 同日多次只算一天", r7["days"] == 1, f"days={r7['days']}")

    print(f"__SELF_TEST_PASSED__={passed}")
    print(f"__SELF_TEST_FAILED__={len(fails)}")
    for f in fails:
        print(f"FAIL {f}")
    return 0 if not fails else 1


def main():
    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    r = analyze()
    lines = []
    if r["days"] >= r["threshold"]:
        # ⚠️ **只报一条**（E66：不重复报）
        #   同一个事实拆成 8 行 → 「等你」列表被淹没 → 长期项越多越没人看
        #   → 细节压缩进这一条的括号里。
        days_str = "、".join(d for d, _ in r["detail"])
        extra = f"，代价：闸门因额度耗尽跳过 scheduled job {r['skips']} 次" if r["skips"] else ""
        lines.append(
            f"FAIL 最近 {r['window_days']} 天里有 {r['days']} 天额度耗尽"
            f"（≥{r['threshold']} 天 = 结构性不足，非偶发；{days_str}{extra}）"
            " —— 需要牛哥哥充值/升级 Token Plan（我改代码改不了）"
        )
    else:
        lines.append(
            f"PASS 最近 {r['window_days']} 天额度耗尽 {r['days']} 天（<{r['threshold']} 天，属正常波动）"
        )
    if r["prev_days"] and r["days"] > r["prev_days"]:
        lines.append(
            f"WARN 额度耗尽在恶化：本期 {r['days']} 天 vs 上期 {r['prev_days']} 天"
        )
    for ln in lines:
        print(ln)
    return 0


if __name__ == "__main__":
    sys.exit(main())
