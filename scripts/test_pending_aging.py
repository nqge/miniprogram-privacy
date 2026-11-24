#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【W11】判据单测 —— 「待落地」**超期阈值**（E85 的教训，2026-11-24 加）。

2026-11-24 加。

## 为什么单独一个文件

`scripts/test_v2_pending_scan.py` 测的是【V2】组的**扫描与销账**逻辑
（哪些是「真待办」、哪些已销账）。

这一份测的是**另一个维度**：一条**已经被扫出来的真待办**，
**该不该被提醒**（阈值）。两者正交，分开测更清楚。

## 这一组的危险方向

**「永远不报」** —— 阈值定得太严，待办永远沉在底下。

这正是 2026-11-24 的真实教训：
【V2】原本用 `age >= 1`（跨天），而 daily-learning 在 09:00 写待办、
巡检 07:00 跑 → **当天永远抓不到**，待办整整悬空一天。

→ 单测必须包含**故障注入**：把判据换成「永远 False」，
用例必须红（证明它真的在判，不是恒假）。
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_pending_aging as M  # noqa: E402

passed = 0
fails = []


def check(cond, label, detail=""):
    global passed
    if cond:
        passed += 1
        print(f"  ✅ PASS  {label}")
    else:
        fails.append(f"{label} → {detail}")
        print(f"  ❌ FAIL  {label}  {detail}")


TODAY = datetime.date(2026, 11, 24)

print("\n【1】超期阈值的基本语义")
# 昨天写的待办 → 今天该管
check(
    M.is_overdue(datetime.date(2026, 11, 23), TODAY, datetime.date(2026, 11, 22)),
    "昨天写 + 上次巡检在更早 → 超期",
)
# 今天写的待办 + 上次巡检是昨天 → 熬过了一轮 → 超期
check(
    M.is_overdue(datetime.date(2026, 11, 24), TODAY, datetime.date(2026, 11, 23)),
    "今天写 + 上次巡检是昨天 → 熬过一轮 → 超期（E85 的真实场景）",
)

print("\n【2】首次部署不该爆")
check(
    not M.is_overdue(TODAY, TODAY, None),
    "无巡检历史 + 今天写 → 不报",
)
check(
    M.is_overdue(datetime.date(2026, 11, 23), TODAY, None),
    "无巡检历史 + 昨天写 → 报（退回 age>=1）",
)

print("\n【3】老待办不能自动豁免")
check(
    M.is_overdue(datetime.date(2026, 10, 1), TODAY, datetime.date(2026, 11, 23)),
    "很久以前写且未销账 → 仍超期",
)

print("\n【4】坏数据不误报")
check(not M.is_overdue(None, TODAY, datetime.date(2026, 11, 23)), "item_date=None → 不超期")

print("\n【5】last_healthcheck_date 从真实日志目录解析")
d = M.last_healthcheck_date(today=TODAY)
check(d is not None, "能从日志目录解析出日期", f"got={d}")
if d is not None:
    check(d < TODAY, "严格早于本次巡检（不含今天）", f"got={d}")

print("\n【6】故障注入 —— 判据装死时用例必须红")
_orig = M.is_overdue
try:
    M.is_overdue = lambda *a, **k: False
    dead = M.is_overdue(datetime.date(2026, 11, 23), TODAY, datetime.date(2026, 11, 22))
    check(not dead, "注入「永远不报」后，断言会红（判据不是恒假）", f"got={dead}")
finally:
    M.is_overdue = _orig

_orig2 = M.is_overdue
try:
    M.is_overdue = lambda *a, **k: True
    alive = M.is_overdue(datetime.date(2026, 11, 23), TODAY, datetime.date(2026, 11, 22))
    check(alive, "注入「永远报」后，断言会红（判据不是恒真）", f"got={alive}")
finally:
    M.is_overdue = _orig2

print("\n【7】判据真身自检（check_pending_aging.py --self-test）")
import subprocess  # noqa: E402

r = subprocess.run(
    [sys.executable, str(Path(__file__).resolve().parent / "check_pending_aging.py"), "--self-test"],
    capture_output=True, text=True, timeout=60,
)
check(r.returncode == 0, "判据真身自带单测通过", r.stdout.strip().splitlines()[-1] if r.stdout else r.stderr[:100])

print("\n" + "=" * 60)
print(f" 结果: PASS={passed} FAIL={len(fails)}")
print("=" * 60)
for f in fails:
    print(f"FAIL {f}")
sys.exit(0 if not fails else 1)
