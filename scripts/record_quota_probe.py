#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""额度探测历史记录器 —— 给 `check_quota_sustained.py` 喂数据。

2026-11-23 加（E84 的落地侧）。

## 为什么需要它

`check_provider_health.py` 是**无状态**的 —— 每次 30 分钟跑一次，
每次都是独立进程，每次都只看得见「此刻」。
所以它能报「现在没额度」，但报不出「最近一直在没额度」。

而额度耗尽在下游被归进「⏳ 等你」→ **永远不告警**
（`alert_dedup.py`：只有 waiting 没有 actionable → 不告警）。
→ 实测近 30 天额度耗尽 **589 次、24 天里有 24 天**，而**一次都没被告知**。

→ 必须有人**把历史记下来**，才能问出「你到底多久没额度了」。

## 为什么不用 cron run 的历史

cron run 只在 agentTurn 任务**真跑起来**的时候才有记录；
额度耗尽时**任务压根没跑** → 没有 run 记录 → **看不见**（E83 同一个坑）。
→ 探测是**独立于任务是否执行**的，所以它能看见「本该跑但跑不了」。

## 它记什么

每次探测追加一行到 `memory/quota-history.jsonl`：

    {"ts": "2026-11-23T07:23:18Z", "state": "ok"|"quota_exhausted"|"other_fail", "detail": "..."}

`state` 的判定口径**必须和 `check_provider_health.py` 一致**：

- `quota_exhausted`：报错明细里带额度特征
  （Token Plan / 用量上限 / insufficient balance / 余额 / 额度 / rate_limit_error）
  —— **注意**：HTTP 429 **本身不等于**额度耗尽！
  429 也可能是普通限流。而 MiniMax 的额度耗尽报错里**带 `rate_limit_error`**，
  所以 `rate_limit_error` 归到额度；但只有裸 429 且**没有**这些特征词 → 归 `other_fail`
  （否则普通限流会被算成额度耗尽 → 统计虚高 → 阈值形同虚设）。
- `ok`：有可用 provider
- `other_fail`：凭证失效 / 服务端错误 / 上游过载等

## 用法

    python3 scripts/record_quota_probe.py
"""

from __future__ import annotations

import datetime
import re
import json
import subprocess
import sys
from pathlib import Path

WS = Path("/root/.openclaw/workspace")
HIST = WS / "memory" / "quota-history.jsonl"
CHECKER = WS / "scripts" / "check_provider_health.py"

# 额度特征（与 check_provider_health.py 的 quota_markers 保持一致）
QUOTA_MARKERS = (
    "Token Plan", "用量上限", "insufficient balance", "余额", "额度", "rate_limit_error",
)


def classify(stdout: str, returncode: int):
    """把探测输出归成 ok / quota_exhausted / other_fail

    ⚠️ **只扫「【Provider 凭证】」那一段**。
    实测踩坑（2026-11-23）：整段扫时，【Provider 韧性】里那句
    「…provider 级故障(欠费/**限流**/宕机)时兜底无效」本身就含额度词
    → 明明探测正常也被记成「额度耗尽」→ 统计虚高 → 阈值形同虚设（E66 误报方向）。
    """
    text = stdout or ""
    # 截取 provider 凭证段
    section = ""
    in_sec = False
    for ln in text.split("\n"):
        if "【Provider 凭证】" in ln:
            in_sec = True
            continue
        if in_sec:
            if re.search(r"【[^】]+】", ln):
                break
            section += ln + "\n"
    if not section:
        section = text

    # 有可用 provider → 正常
    if "✅ PASS" in section and "可用" in section:
        return "ok", "有可用 provider"
    if any(mk in section for mk in QUOTA_MARKERS):
        detail = ""
        for ln in section.split("\n"):
            if any(mk in ln for mk in QUOTA_MARKERS):
                detail = ln.strip()[:200]
                break
        return "quota_exhausted", detail or "额度特征命中"
    # 没有可用 provider，但没命中额度特征 → 其他故障（凭证失效/过载等）
    detail = ""
    for ln in section.split("\n"):
        if "❌" in ln:
            detail = ln.strip()[:200]
            break
    return "other_fail", detail or f"returncode={returncode}"


def main():
    try:
        r = subprocess.run(
            [sys.executable, str(CHECKER)],
            capture_output=True, text=True, timeout=90,
        )
        stdout = r.stdout or ""
        state, detail = classify(stdout, r.returncode)
    except Exception as e:
        state, detail = "other_fail", f"探测脚本异常: {type(e).__name__}"

    rec = {
        "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "state": state,
        "detail": detail,
    }
    HIST.parent.mkdir(parents=True, exist_ok=True)
    with HIST.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # 顺手裁剪：只保留最近 60 天，避免文件无限膨胀
    try:
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=60)
        lines = HIST.read_text(encoding="utf-8", errors="replace").splitlines()
        keep = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                d = json.loads(ln)
                ts = d.get("ts") or ""
                if len(ts) < 10:
                    continue
                dt = datetime.datetime.strptime(ts[:10], "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
                if dt >= cutoff:
                    keep.append(ln)
            except Exception:
                continue
        if len(keep) != len(lines):
            HIST.write_text("\n".join(keep) + "\n", encoding="utf-8")
    except Exception:
        pass

    print(f"记录: state={state} detail={detail[:80]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
