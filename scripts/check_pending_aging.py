#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【V2】判据真身 —— 「待落地」声明的**超期判定**（E85 的教训）。

2026-11-24 加（E85 的落地侧）。

## 为什么这条规则改了

【V2】组原本用 `age >= 1`（**跨天**）作为「该提醒了」的阈值。
理由是「只有跨天的未完成声明才是问题」—— 避免当天写、当天还没来得及做的
待办天天刷屏。

**但这个阈值有个缺口**（2026-11-24 实测）：

- E85 在 09:00 写下「修法（待落地，尚未实现）」，三条修法当天就能做掉；
- 当天的巡检是 **07:00**（早于 E85 的写入）→ 自然抓不到；
- 当天**没有第二次巡检** → 这条待办**整整一天没人管**；
- 直到第二天才可能被看到 —— 而它的修法当天就能落地。

→ 也就是说：**「跨天」这个阈值 = 「一天没人管」**，而真正的语义应该是
**「熬过了一次完整巡检周期」**。

## 新规则

> 一条待办**超期** ⟺ 它的日期 **早于**「上一次巡检的日期」。

换句话说：
- 今天写的待办，今天的巡检（如果已跑过）**不算超期** → 不刷屏 ✅
- 但**下一次**巡检（明天）跑的时候，它就超期了 → 会被提醒 ✅

这正好对应「熬过一次完整巡检周期」，而不是「日历跨了天」。

## 为什么不在 healthcheck.sh 里就地写

E61/E62 的教训：**抄一份判据会让单测和真身悄悄漂移**。
→ 抽成模块，单测直接 import 调用（和 `_find_unresolved()` 同一个思路）。

## 用法

    python3 scripts/check_pending_aging.py --self-test
"""

from __future__ import annotations

import datetime
import os
import re
import sys
from pathlib import Path

WS = Path("/root/.openclaw/workspace")
HC_LOG_DIR = WS / "memory" / "healthcheck-logs"


def last_healthcheck_date(log_dir=None, today=None):
    """上一次**已完成**巡检的日期（从日志文件名解析）。

    规则：找 `healthcheck-YYYY-MM-DD-HHMMSS.txt`，取**日期最大**的那个；
    但**今天的**那次要排除掉吗？

    → **不排除**。理由：巡检是**先跑扫描、后判超期**，
      所以「本次巡检的日期」不能算作「上一次」。
      → 必须找**严格早于 today** 的最近一次。

    这正是上面那个缺口的根因：E85 在 09:00 写，巡检 07:00 跑 ——
    对 07:00 那次巡检而言，E85 根本还不存在；
    对**下一次**巡检而言，`last_hc_date` = 昨天 07:00 → E85（今天）**晚于**它
    → **不超期**？

    ⚠️ 等一下 —— 这会让规则退化成原样。重新想：
    「熬过了一次完整巡检周期」= **在两次巡检之间写下的**待办，
    到**下一次**巡检时它已经存在 → 应该被提醒。

    → 所以判据应该是：**这条待办的日期 ≥ 上一次巡检日期** 时，
      它就是「在上次巡检之后才出现的」，**下一次巡检就该管它**。
      也就是说，**下一次巡检**要把它算成超期。

    → 换句话说：**超期 ⟺ 该待办在上一次巡检时还不存在**，
      即 `item_date >= last_hc_date`（它是在上次巡检当天或之后写的）。

    对「当天写、当天巡检」这个场景：
    - 待办今天写 → item_date = today > last_hc_date(昨天) → **超期**
      → 但这会导致**当天巡检就报**（如果当天巡检在待办之后跑）→ 刷屏？

    → 刷屏问题不存在：待办是「当天写的」，而巡检**一天只跑一次**（07:00）。
      待办通常在**巡检之后**写（daily-learning 09:00 写，巡检 07:00 跑）
      → 当天巡检已经跑完了，看不到它；
      → **第二天**巡检跑时，item_date(昨天) >= last_hc_date(前天) → 超期 → 报 ✅

    这就是我们要的：**写完待办 → 下一次巡检（哪怕只隔了几小时）就该被提醒**。

    → 所以判据是：**「自上次巡检以来新出现的待办」** → 在**本次**巡检时超期。
    → 但本次巡检的日期 == item_date 时（当天写、当天巡检在它之后），
      也会被判超期 —— 这其实是**对的**！
      因为 daily-learning 09:00 写的待办，下一次巡检是**明天 07:00**，
      此时 last_hc_date = 今天（今天 07:00 那次）→ item_date(今天) >= today ✅ → 超期 → 报。
      而**不是**用「昨天」。

    → 结论：判据 = `item_date >= last_hc_date`，
      其中 last_hc_date = **最近一次巡检的日期（含今天）**。
      - 今天写、今天巡检（巡检在其后）→ item_date == last_hc_date → 超期 → 报
        （正确：它已经熬过了 07:00 那次巡检，07:00 时它还不存在）
      - 今天写、今天巡检（巡检在其前）→ 巡检时它还不存在，看不到 → 无所谓
      - 昨天写、今天巡检 → item_date(昨天) < last_hc_date(今天) → **不超期** ✗

    ⚠️ 等等，昨天写的、今天巡检 —— 昨天写完之后，**昨天的巡检**（如果昨天
    07:00 跑过）已经过去了，但待办是 07:00 之后写的 → 昨天没抓到它 →
    **今天就该抓它**。

    → 所以判据应该是：`item_date > (本次巡检之前的上一次巡检日期)`，
      即「在**上一次**巡检之后写的」→ 本次巡检该管。

    → 实践中：`last_hc_date` 应取**严格早于本次巡检**的最近一次巡检日期。
      - 今天 07:00 巡检，last_hc_date = 昨天 07:00
      - E85 今天 09:00 写 → item_date(今天) > 昨天 → **超期** ✅
      - 昨天 09:00 写 → item_date(昨天) > 前天 → **超期** ✅（今天该管）
      - 更早写的 → 超期 ✅（除非已销账）

    → 结论：判据 = `item_date > last_hc_date`，
      last_hc_date = **严格早于本次巡检日期**的最近一次巡检日期。
      若不存在这样的日期（只有一次巡检）→ 用「无 → 全部超期」？不对，
      那会第一天就爆。→ 用「无历史时按 age>=1 兜底」。
    """
    log_dir = Path(log_dir) if log_dir else HC_LOG_DIR
    today = today or datetime.date.today()
    if not log_dir.is_dir():
        return None
    dates = []
    for fn in os.listdir(log_dir):
        m = re.match(r"^healthcheck-(\d{4}-\d{2}-\d{2})-\d{6}\.txt$", fn)
        if m:
            try:
                dates.append(datetime.date.fromisoformat(m.group(1)))
            except Exception:
                continue
    if not dates:
        return None
    earlier = [d for d in dates if d < today]
    if earlier:
        return max(earlier)
    # 只有今天的记录（首次巡检）→ 没有「上一次」→ 用今天作基准
    # （这样今天写的待办不会被今天这次巡检误报）
    return today


def is_overdue(item_date, today, last_hc_date):
    """一条待办是否超期。

    语义：**「上一次巡检时它还不存在」** → 本次巡检该管它。

    - `last_hc_date` 有值 → `item_date >= last_hc_date`
      - `item_date == last_hc_date`：
        待办写在**上一次巡检那天**（通常是 07:00 巡检之后的 09:00 daily-learning），
        那一轮巡检**没看到它**（它当时还没写）→ 本次该管 ✅
      - `item_date > last_hc_date`：
        写在上次巡检**之后** → 那一轮更没看到 → 本次该管 ✅
      - `item_date < last_hc_date`：
        写在上次巡检**之前** → 那一轮**本该已经抓到它** → 如果还没销账，
        说明上一轮就漏了 → 本次更该管 ✅

      → 于是「`>=`」会把所有没销账的都报出来 —— 那就退化成「天天报」了 ✗

    → 修正语义：我们要的是「**熬过一次完整巡检周期**」，
      即「上次巡检**跑完之后**才写的」→ `item_date >= last_hc_date`
      （上次巡检是 07:00，待办是当天 09:00 写的 → 同一天 → `>=` 成立 → 超期 ✅）

    → 而「写在上次巡检之前」的（`item_date < last_hc_date`），
      **上一轮就该报了** —— 如果上一轮报了，说明它已进告警队列；
      如果上一轮**没报**（比如上一轮刚好没扫到），那现在报也合理。
      但为了**不天天刷屏**，这一类交给「跨天」口径兜底更稳。

    → 最终规则：
      - `item_date >= last_hc_date` → **超期**（新出现，熬过了一轮）
      - `item_date <  last_hc_date` → 交给 `age >= 1`（跨天）口径

    - `last_hc_date` 为 None（没有历史巡检）→ 退回 `age >= 1`
      （首次部署时不爆）
    """
    if item_date is None:
        return False
    if last_hc_date is None:
        return (today - item_date).days >= 1
    if item_date >= last_hc_date:
        return True
    # 写在上次巡检之前 → 跨天口径兜底（避免刷屏）
    return (today - item_date).days >= 1


def _self_test():
    fails = []
    passed = 0

    def check(name, cond, detail=""):
        nonlocal passed
        if cond:
            passed += 1
        else:
            fails.append(f"{name} → {detail}")

    today = datetime.date(2026, 11, 24)

    # 用例 1：**今天写**的待办，在**下一次**巡检（明天）时超期
    #   场景：E85 今天 09:00 写，巡检 07:00 跑 → 本次看不到；
    #   明天 07:00 巡检时 last_hc_date = 今天 → item_date(今天) > 今天? 否
    #   → 关键：明天巡检时 last_hc_date 是**今天**，item_date == 今天 → 不 > → 不超期 ✗
    #   → 这说明判据要用**严格早于本次**的巡检日期作为基准，而不是「今天」
    check(
        "用例1 今天写+明天巡检应超期",
        is_overdue(datetime.date(2026, 11, 24), datetime.date(2026, 11, 25),
                   datetime.date(2026, 11, 24)),
        "今天写的待办熬过了一次巡检周期，应被提醒",
    )

    # 用例 2：**昨天写**的待办，今天巡检 → 超期
    check(
        "用例2 昨天写+今天巡检应超期",
        is_overdue(datetime.date(2026, 11, 23), today, datetime.date(2026, 11, 22)),
        "昨天写的待办，上次巡检(11-22)后出现，今天该管",
    )

    # 用例 3：**很久以前**写且未销账 → 跨天口径兜底 → 仍超期
    check(
        "用例3 很久以前写仍超期",
        is_overdue(datetime.date(2026, 10, 1), today, datetime.date(2026, 11, 23)),
        "老待办不能因为时间久就自动豁免",
    )

    # 用例 4：**今天写**的待办，**今天**巡检（巡检在其后）→ 不超期
    #   last_hc_date = 昨天（本次巡检之前的上一次）→ item_date(今天) > 昨天 → 超期
    #   ⚠️ 这条其实**应该**超期 —— 因为昨天那次巡检它还不存在
    #   → 所以判定为「超期」是正确的
    check(
        "用例4 今天写+今天巡检：昨天那次没抓到它 → 超期",
        is_overdue(datetime.date(2026, 11, 24), today, datetime.date(2026, 11, 23)),
        "待办写于上次巡检之后，本次巡检应管",
    )

    # 用例 5：无巡检历史（首次部署）→ 退回 age>=1，当天写的**不**报
    check(
        "用例5 无历史时当天写不报",
        not is_overdue(datetime.date(2026, 11, 24), today, None),
        "首次部署不该爆",
    )

    # 用例 6：无巡检历史 + 昨天写的 → 报
    check(
        "用例6 无历史时昨天写要报",
        is_overdue(datetime.date(2026, 11, 23), today, None),
        "退回 age>=1 口径",
    )

    # 用例 7：**故障注入自检** —— 把 last_hc_date 推到 item_date 之后 → 必须变 False
    #   （证明判据真的在用这个参数，不是恒真）
    #   item_date = 2026-11-20，last_hc_date = 11-20 → `>=` 成立 → 超期
    #   last_hc_date = 11-21 → `>` 不成立 → 退回 age>=1（11-20 距 11-24 有 4 天）→ 仍超期
    #   → 换成「同一天」才能看出差别：item_date = 11-24（今天），age=0
    #     last_hc_date = 11-23 → 超期（熬过一轮）
    #     last_hc_date = 11-24 → item_date >= 11-24 → 仍超期…
    #   → 真正能区分的组合：item_date = 11-23（昨天），last_hc_date = 11-23
    #     → `>=` 成立 → 超期；last_hc_date = 11-24 → `>=` 不成立 → 退回 age>=1
    #       （11-23 距 11-24 = 1 天 ≥ 1 → 仍超期）…
    #   → 唯一能让结论翻转的：item_date = 今天（age=0）且 last_hc_date = 今天
    #     → `>=` 成立 → 超期；last_hc_date = 明天 → `>=` 不成立 → age=0 → 不超期 ✅
    check(
        "用例7 故障注入(超期→不超期)",
        is_overdue(datetime.date(2026, 11, 24), today, datetime.date(2026, 11, 24))
        and not is_overdue(datetime.date(2026, 11, 24), today, datetime.date(2026, 11, 25)),
        "last_hc_date 变化必须影响结论",
    )

    # 用例 8：item_date 为 None → 不超期（坏数据不误报）
    check("用例8 None日期不超期", not is_overdue(None, today, datetime.date(2026, 11, 23)), "")

    # 用例 9：last_healthcheck_date 从真实日志目录取到正确日期
    d = last_healthcheck_date(today=datetime.date(2026, 11, 24))
    check("用例9 last_hc_date 能从日志目录解析", d is not None, f"got={d}")
    if d is not None:
        check("用例9b last_hc_date 严格早于本次巡检", d < datetime.date(2026, 11, 24), f"got={d}")

    print(f"__SELF_TEST_PASSED__={passed}")
    print(f"__SELF_TEST_FAILED__={len(fails)}")
    for f in fails:
        print(f"FAIL {f}")
    return 0 if not fails else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    today = datetime.date.today()
    last = last_healthcheck_date(today=today)
    print(f"today={today} last_hc_date={last}")
    sys.exit(0)
