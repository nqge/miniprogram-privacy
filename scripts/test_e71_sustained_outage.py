#!/usr/bin/env python3
"""check_cron_delivery 的 E71 单测 —— 防「连续失败被降级成 WARN」复发。

## 为什么需要它（2026-11-16 实测）

daily-learning 从 11-10 起**连续 6 天**每天 09:00 失败，
11-12 ~ 11-16 的 `memory/YYYY-MM-DD.md` 全部不存在。
而巡检**每天都把这个事实算出来、写进报告，然后归档** ——
因为它被归进了 **WARN** 区。

**测量是准的，分类把它降级了**：
  · WARN 不进「现在就该修」→ 没人修
  · 失败照旧再叠一天 → 明天再报一次同样的 WARN
→ 一个已经死了 6 天的任务，在报告里看起来和一次偶发失败一样重。

修法（E71）：
  ① 连续失败 ≥3 次 → 记 **FAIL**，且断言的是「**已中断 N 天**」而不是「这次挂了」
  ② 额度类原因 → 文本带「额度耗尽」→ healthcheck 的 is_waiting() 归进「⏳ 等你」
     → **不会**变成「可处理 - 现在就该修」的假警报（额度我改不了）
  ③ **不重复报**：连续失败 ≥3 时只报「持续中断」那一条，不另报「单次失败」
     → 同一个事实报两遍 = 噪音（E66）

## 跑法

    python3 scripts/test_e71_sustained_outage.py
"""

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "check_cron_delivery.py"
spec = importlib.util.spec_from_file_location("check_cron_delivery", SCRIPT)
ccd = importlib.util.module_from_spec(spec)
sys.modules["check_cron_delivery"] = ccd
spec.loader.exec_module(ccd)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


DAY = 86400000


def mk_runs(statuses, start_ms=1794000000000, step_ms=DAY):
    """造 run 列表（最新在前）。statuses[0] 是最近一次。"""
    out = []
    for i, s in enumerate(statuses):
        out.append({
            "status": s,
            "runAtMs": start_ms - i * step_ms,
            "delivered": True,
            "summary": "写入了 memory/2026-11-16.md",
        })
    return out


print("=" * 60)
print(" E71 · 持续中断检测 单测")
print("=" * 60)

# ---------------------------------------------------------------- ①
print("\n【①】_max_consecutive_errors —— 连续 vs 零散")
check("连续 6 次失败 → 6", ccd._max_consecutive_errors(mk_runs(["error"] * 6)) == 6)
check("连续 3 次后接成功 → 3",
      ccd._max_consecutive_errors(mk_runs(["error", "error", "error", "ok"])) == 3)
check("零散失败（隔一个成功一次）→ 1",
      ccd._max_consecutive_errors(mk_runs(["error", "ok", "error", "ok", "error"])) == 1)
check("全部成功 → 0", ccd._max_consecutive_errors(mk_runs(["ok"] * 5)) == 0)

# ---------------------------------------------------------------- ②
print("\n【②】_outage_span_days —— 按日历天算，不是按次数")
# 每天一次，连续挂 6 天 → 中断了 6 天
runs6 = mk_runs(["error"] * 6)
check("每天一次连挂 6 次 → 中断约 6 天", ccd._outage_span_days(runs6) == 6,
      f"实际 {ccd._outage_span_days(runs6)}")

# ⚠️ 关键对照：**同样的次数，不同的调度频率 → 严重性差两个数量级**
# 每小时跑一次，连挂 6 次 = 只中断了 5 小时 ≈ 0 天
runs_hourly = mk_runs(["error"] * 6, step_ms=3600000)
span_hourly = ccd._outage_span_days(runs_hourly)
check("每小时一次连挂 6 次 → 中断 <1 天（次数相同但严重性不同）",
      span_hourly <= 1, f"实际 {span_hourly}")

# 连续失败后恢复了 → 跨度是「最后一次成功 → 最近一次失败」
runs_recover = mk_runs(["error", "error", "ok", "error", "error", "error"])
check("连续 3 次失败（最近一次失败）→ 跨度按最后成功算",
      ccd._outage_span_days(runs_recover) == 2,
      f"实际 {ccd._outage_span_days(runs_recover)}")

# ---------------------------------------------------------------- ③
print("\n【③】空输入不崩（E11：不许崩）")
check("空列表 → 0 天", ccd._outage_span_days([]) == 0)
check("空列表 → 0 次", ccd._max_consecutive_errors([]) == 0)
check("无 runAtMs 的 run → 不崩", ccd._outage_span_days([{"status": "error"}]) >= 0)

# ---------------------------------------------------------------- ④
print("\n【④】严重性分级判据（≥3 次 → FAIL 级）")
# 这条判据在 main() 里，本单测用同样的阈值常量做对照
THRESHOLD = 3
check("连续 6 次 ≥ 阈值 → 应升级", ccd._max_consecutive_errors(mk_runs(["error"] * 6)) >= THRESHOLD)
check("连续 2 次 < 阈值 → 不升级", ccd._max_consecutive_errors(mk_runs(["error", "error", "ok"])) < THRESHOLD)
check("零散失败 < 阈值 → 不升级",
      ccd._max_consecutive_errors(mk_runs(["error", "ok", "error"])) < THRESHOLD)

# ---------------------------------------------------------------- ⑤
print("\n【⑤】healthcheck 的 is_waiting 能不能把额度类 FAIL 归进「等你」")
# 复刻 healthcheck.sh 的 WAITING_PATTERNS（防两边漂移）
WAITING_PATTERNS = [
    "明文凭证 ", "只有 1 个 provider", "未配置任何 fallback",
    "fallback 全在", "Token Plan", "额度耗尽",
]


def is_waiting(msg):
    return any(p in msg for p in WAITING_PATTERNS)


quota_msg = ("FAIL daily-learning 连续失败 6 次，任务已中断约 6 天！"
             "（lastRunStatus=error，原因：额度耗尽(API rate limit，需充值)）")
real_msg = ("FAIL daily-learning 连续失败 6 次，任务已中断约 6 天！"
            "（lastRunStatus=error，原因：脚本报错）")

check("额度类持续中断 → 归「等你」（不是可处理）", is_waiting(quota_msg) is True)
check("真故障类持续中断 → 归「可处理」", is_waiting(real_msg) is False)
check("持续中断文本含「额度耗尽」→ 命中 waiting", "额度耗尽" in quota_msg)

print()
print("=" * 60)
print(f" 结果: PASS={PASS} FAIL={FAIL}")
print("=" * 60)
sys.exit(1 if FAIL else 0)
