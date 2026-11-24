#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 journald 回填额度探测历史 —— 别让判据从零开始攒一周。

2026-11-23 加（E84 的落地侧）。

## 为什么需要回填

`check_quota_sustained.py` 靠 `memory/quota-history.jsonl` 判断
「最近 7 天里有几天额度耗尽」。
但这个文件是**从今天才开始记**的 —— 如果不回填，
新判据要**攒满一周**才能第一次有话说。

而 journald 里**早就存着历史**：`openclaw-quick-health.service` 每 30 分钟
探测一次 provider，每次都会把 provider 的报错明细打进日志。
→ 那些日志就是**现成的探测历史**，只是没人把它结构化。

## 回填口径

从 journald 抓 `openclaw-quick-health.service` 的日志，
按**每次运行的边界**切成一条条记录：

- 每次运行里有 `✅ PASS  <provider> 可用` → 该次 state=`ok`
- 没有可用 provider 且含额度特征 → state=`quota_exhausted`
- 没有可用 provider 且不含额度特征 → state=`other_fail`

### ⚠️ 边界必须按「运行」切，不能按「行」切

一个运行的日志里可能同时出现：
- 一行 `⚠️ WARN minimax 限流（HTTP 429）…Token Plan…`（额度特征）
- 一行 `✅ PASS minimax 可用（MiniMax-M3，HTTP 200）`（正常）

**如果按行切**，会把同一次运行既记成「额度耗尽」又记成「ok」，
统计出来的「耗尽天数」会被**严重虚高** → 阈值形同虚设 → 假警报。

→ 正确做法：**一次运行 = 一条记录**，按「结果行」（`结果: PASS=...`）切分，
  取该次运行的**最后状态**。

## 用法

    python3 scripts/backfill_quota_history.py [--days 60]
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from quota_ts import parse_ts, to_local_date, to_utc_iso  # noqa: E402  (E85：时间戳基准统一)

WS = Path("/root/.openclaw/workspace")
HIST = WS / "memory" / "quota-history.jsonl"

QUOTA_MARKERS = (
    "Token Plan", "用量上限", "insufficient balance", "余额", "额度", "rate_limit_error",
)
# 一次运行的结束标志
RESULT_RE = re.compile(r"结果:\s*PASS=(\d+)\s+FAIL=(\d+)")


def fetch_journal(days: int):
    since = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    cmd = [
        "journalctl", "--user", "-u", "openclaw-quick-health.service",
        "--since", since, "--no-pager", "-o", "short-iso",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return r.stdout or ""
    except Exception:
        return ""


def parse_runs(text: str):
    """把 journald 文本切成 [(start_ts, body)] —— 一次 systemd 运行一条"""
    runs = []
    cur_ts = None
    cur = []
    for line in (text or "").split("\n"):
        m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)[^ ]*\s+(.*)$", line)
        if m:
            ts, body = m.group(1), m.group(2)
        else:
            ts, body = None, line
        # 一次运行的开始
        if "Starting openclaw-quick-health.service" in body:
            if cur_ts and cur:
                runs.append((cur_ts, "\n".join(cur)))
            cur_ts, cur = ts, []
            continue
        if cur_ts is not None:
            cur.append(body)
            # 一次运行的结束
            if "Finished openclaw-quick-health.service" in body:
                runs.append((cur_ts, "\n".join(cur)))
                cur_ts, cur = None, []
    if cur_ts and cur:
        runs.append((cur_ts, "\n".join(cur)))
    return runs


def classify_run(body: str):
    """一次运行 → (state, detail)

    ⚠️ **只扫「【Provider 凭证】」那一段**，不能扫整个运行 body。
    实测踩坑（2026-11-23）：整段扫时，【Provider 韧性】里那句
      「只有 1 个 provider，但配了 2 项 fallback —— provider 级故障(欠费/**限流**/宕机)时兜底无效」
      本身就含「限流」「欠费」等额度词 → **11-17 被误判成额度耗尽**
      （实测那天额度耗尽探测 = 0 次，47 次探测全部正常）。
    → 这就是「误报方向」的老坑（E66）：分类看错方向 → 统计虚高 → 阈值形同虚设。
    """
    # 截取 provider 凭证段：从「【Provider 凭证】」到下一个「【」标题
    section = ""
    in_sec = False
    for ln in (body or "").split("\n"):
        if "【Provider 凭证】" in ln:
            in_sec = True
            continue
        if in_sec:
            if re.search(r"【[^】]+】", ln):
                break            # 下一个段标题 → 结束
            section += ln + "\n"
    if not section:
        section = body or ""

    # 有可用 provider → 正常
    if re.search(r"✅ PASS\s+\S+\s+可用", section):
        return "ok", "有可用 provider"
    # 没有可用 provider 的 FAIL 行
    m = re.search(r"❌ FAIL\s+(.*)", section)
    detail = m.group(1).strip()[:200] if m else ""
    if any(mk in section for mk in QUOTA_MARKERS):
        return "quota_exhausted", detail or "额度特征命中"
    if "529" in section or "overload" in section.lower():
        return "other_fail", detail or "上游瞬时过载"
    return "other_fail", detail or "无可用 provider"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    args = ap.parse_args()

    text = fetch_journal(args.days)
    if not text.strip():
        print("journald 无输出，跳过回填")
        return 0
    runs = parse_runs(text)
    if not runs:
        print("没解析出运行记录，跳过回填")
        return 0

    existing = set()
    if HIST.exists():
        for ln in HIST.read_text(encoding="utf-8", errors="replace").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                d = json.loads(ln)
                # ⚠️ 归日基准必须与写入侧一致（E85）：统一走 `to_local_date`
                ts = str(d.get("ts") or "")
                day = to_local_date(ts)
                existing.add((day.isoformat() if day else ts[:10], d.get("state")))
            except Exception:
                continue

    added = 0
    states = {"ok": 0, "quota_exhausted": 0, "other_fail": 0}
    out = []
    for ts, body in runs:
        state, detail = classify_run(body)
        states[state] = states.get(state, 0) + 1
        # ⚠️ **写入前必须统一时区基准**（E85）
        #   journald `-o short-iso` 给的是**本地时间**（无时区标记），
        #   而实时记录写的是 UTC + `Z` —— 两者混在同一文件里，
        #   消费方按 `ts[:10]` 归日就会把两套日历基准当同一套用
        #   （实测：7 天窗口字面 6 天耗尽 vs 正确 UTC 5 天）。
        #   → 这里把 journald 的本地时间串转成 UTC + `Z` 再入库。
        dt = parse_ts(ts, naive_is_local=True)
        ts_utc = to_utc_iso(dt) if dt is not None else ts
        # 每天只保留**一条**代表记录（取当天最后一次探测）
        day = ts_utc[:10]
        if (day, state) in existing:
            continue
        out.append({
            "ts": ts_utc,
            "state": state,
            "detail": detail,
        })
        added += 1

    # 同日多次 → **只要当天有任一次探测撞上额度耗尽，这天就算「耗尽日」**
    # ⚠️ 不能只取「当天最后一次」——
    #   实测 11-19：当天有 **7 次**额度耗尽，但 23:39 那次探测恢复了（HTTP 200）
    #   → 若取末次状态，这天被记成 "ok" → **耗尽事实被抹掉** → 统计虚低 → 阈值形同虚设
    #   → 判据问的是「这天系统有没有过不能工作的时候」，不是「这天结束时是什么状态」
    by_day = {}
    for rec in out:
        day = rec["ts"][:10]   # ⚠️ 已是 UTC+Z，切片与写入基准一致（E85）
        prev = by_day.get(day)
        # 优先级：quota_exhausted > other_fail > ok
        rank = {"quota_exhausted": 3, "other_fail": 2, "ok": 1}
        if prev is None or rank.get(rec["state"], 0) > rank.get(prev["state"], 0):
            by_day[day] = rec
    final = sorted(by_day.values(), key=lambda r: r["ts"])

    # 保留已有的记录（今天/近期的实时记录更新），把回填的并进去
    # ⚠️ 合并时**不能简单跳过已有记录**：
    #   当天可能先记了一条 "ok"（额度恢复时探测），后面又出现额度耗尽
    #   → 若按「已有就跳过」，那天的耗尽事实会被抹掉（实测 11-23 就是这样）
    #   → 优先级：quota_exhausted > other_fail > ok（与上面同日归并同一套口径）
    rank = {"quota_exhausted": 3, "other_fail": 2, "ok": 1}
    merged = {}
    if HIST.exists():
        for l in HIST.read_text(encoding="utf-8", errors="replace").splitlines():
            l = l.strip()
            if not l:
                continue
            try:
                rec = json.loads(l)
            except Exception:
                continue
            # ⚠️ 归日基准统一（E85）：显式解析后转本地日历日，不做字面切片
            _d = to_local_date(str(rec.get("ts") or ""))
            day = _d.isoformat() if _d else str(rec.get("ts", ""))[:10]
            if not day:
                continue
            prev = merged.get(day)
            if prev is None or rank.get(rec.get("state"), 0) > rank.get(prev.get("state"), 0):
                merged[day] = rec
    for rec in final:
        day = rec["ts"][:10]   # ⚠️ 已是 UTC+Z（E85）
        prev = merged.get(day)
        if prev is None or rank.get(rec.get("state"), 0) > rank.get(prev.get("state"), 0):
            merged[day] = rec
    lines = [json.dumps(merged[d], ensure_ascii=False) for d in sorted(merged)]

    HIST.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"回填完成: 运行 {len(runs)} 次 · 原始分布 {states} · 写入 {len(final)} 天")
    return 0


if __name__ == "__main__":
    sys.exit(main())
