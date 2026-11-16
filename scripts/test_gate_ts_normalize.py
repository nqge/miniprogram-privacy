#!/usr/bin/env python3
"""【G2】的「双数据源时间戳」单测 —— 防「合并排序错位导致漏报」。

## 为什么需要它（2026-11-16 实测）

【G2】断言读**两个**数据源：

| 来源 | 时间戳格式 | 例子 |
|---|---|---|
| `memory/healthcheck-logs/*-gate-*.txt`（文件名） | `-` 分隔 | `2026-11-16-174715` |
| `memory/gate-history.jsonl`（jsonl 字段） | `T` 分隔 | `2026-11-16T18:09:12` |

**直接按字符串排序会交错错位** —— ASCII 里 `'-'` (0x2D) < `'T'` (0x54)：

    sorted(['2026-11-16-174715', '2026-11-16T09:00:00'])
    → ['2026-11-16-174715', '2026-11-16T09:00:00']
    → 17:47 排到了 09:00 **前面**，而实际 17:47 更晚

→ 「连续跳过」的计数建立在**错误的顺序**上
→ 一次真实的跳过可能被后面的成功记录「挤掉」→ **漏报**
→ 而漏报的方向恰恰是「额度枯竭了却不报警」。

→ 修法：`_norm_ts()` 把两种格式统一成 `YYYY-MM-DD-HHMMSS`（定长、可字典序比较）。

## 跑法

    python3 scripts/test_gate_ts_normalize.py
"""

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "check_gate_skips.py"
spec = importlib.util.spec_from_file_location("check_gate_skips", SCRIPT)
cgs = importlib.util.module_from_spec(spec)
sys.modules["check_gate_skips"] = cgs
spec.loader.exec_module(cgs)

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


print("=" * 60)
print(" 【G2】· 双数据源时间戳归一化 单测")
print("=" * 60)

# ---------------------------------------------------------------- ①
print("\n【①】_norm_ts 把两种格式归一到同一形式")
check("'-' 格式 → 归一",
      cgs._norm_ts("2026-11-16-174715") == "2026-11-16-174715",
      cgs._norm_ts("2026-11-16-174715"))
check("'T' 格式 → 归一",
      cgs._norm_ts("2026-11-16T18:09:12") == "2026-11-16-180912",
      cgs._norm_ts("2026-11-16T18:09:12"))
check("空串 → 空串", cgs._norm_ts("") == "")
check("None → 空串", cgs._norm_ts(None) == "")

# ---------------------------------------------------------------- ②
print("\n【②】归一后字典序 == 时间序（这是修复的核心）")
mixed = ["2026-11-16-174715", "2026-11-16T09:00:00", "2026-11-15T09:00:00"]
raw_sorted = sorted(mixed)
norm_sorted = sorted(cgs._norm_ts(t) for t in mixed)

check("**未归一时**顺序是错的（17:47 排在 09:00 前面）",
      raw_sorted.index("2026-11-16-174715") < raw_sorted.index("2026-11-16T09:00:00"),
      "若这条失败，说明 ASCII 行为变了，需重新评估")
check("归一后 09:00 排在 17:47 前面",
      norm_sorted.index("2026-11-16-090000") < norm_sorted.index("2026-11-16-174715"),
      norm_sorted)

# ---------------------------------------------------------------- ③
print("\n【③】真实数据源混合时，顺序必须正确")
logs = [("daily-learning-gate", "2026-11-16-174715", "triggered", None)]
hist = [("daily-learning-gate", "2026-11-16-090000", "skipped", "quota"),
        ("daily-learning-gate", "2026-11-15-090000", "skipped", "quota"),
        ("daily-learning-gate", "2026-11-14-090000", "skipped", "quota")]
merged = sorted(logs + hist, key=lambda x: x[1])
check("合并后 3 次 skipped 在 triggered 之前",
      [m[2] for m in merged] == ["skipped", "skipped", "skipped", "triggered"],
      [m[2] for m in merged])

# ---------------------------------------------------------------- ④
print("\n【④】「连续」判定必须建立在正确顺序上")
# 正确顺序下：11-14/15/16 09:00 连跳 3 次，11-16 17:47 才 triggered
# → 当前 streak（从最新往回数）= 0，但**历史上有一段连续 3 次的跳过**
#
# ⚠️ 2026-11-16 18:30 修正语义（这一条我一开始写反了）：
#   原断言写「最新是成功 → 不报 FAIL（连续被打断）」——
#   但那正是我 09:15 自己犯过的 bug（`_outage_span_days` 用
#   「最新一次运行 − 最后一次成功」当跨度 → **任务一恢复，
#   6 天的中断被缩成 1 天，正好抹掉最该留痕的那段**）。
#
#   → 正确语义：**连续段必须独立地扫整段历史找出来**，
#     「已经恢复」不该让「额度枯竭了 3 天」这件事消失。
#     报告措辞要区分「还在跳」和「已恢复」两种情况。
p, w, d = cgs.judge(merged)
check("连跳 3 次后恢复 → 仍要报 FAIL（恢复不该掩盖中断）",
      bool(p), f"没报: problems={p} warns={w}")
check("措辞区分「现已恢复」",
      bool(p) and "现已恢复" in p[0], p[0] if p else "")
check("窗口内 best_streak = 3",
      d.get("daily-learning-gate", {}).get("best_streak") == 3, d)
check("当前 streak（从最新往回数）= 0",
      d.get("daily-learning-gate", {}).get("consecutive_skipped") == 0, d)
check("recovered 标记为 True",
      d.get("daily-learning-gate", {}).get("recovered") is True, d)

# 同一份数据，**只差归一化**这一件事 → 对比两种排序的结论。
#
# 真实场景：日志源用 '-' 格式（17:47），历史源用 'T' 格式（09:00 / 15 日 / 14 日）
raw_mixed = [("daily-learning-gate", "2026-11-16-174715", "triggered", None),
             ("daily-learning-gate", "2026-11-16T09:00:00", "skipped", "quota"),
             ("daily-learning-gate", "2026-11-15T09:00:00", "skipped", "quota"),
             ("daily-learning-gate", "2026-11-14T09:00:00", "skipped", "quota")]

raw_rows = sorted(raw_mixed, key=lambda x: x[1])                    # 未归一
norm_rows = sorted([(r[0], cgs._norm_ts(r[1]), r[2], r[3]) for r in raw_mixed],
                   key=lambda x: x[1])                               # 已归一

# 错在哪：真实时间序应是 11-14 → 11-15 → 11-16 09:00 → 11-16 17:47(triggered)
# 未归一时 '-' 格式的 17:47 会被塞进 'T' 格式那堆的**中间**，
# 于是「最后一次成功」和「最后一次跳过」的相对位置全乱了。
_real_order = ["2026-11-14", "2026-11-15", "2026-11-16T09", "2026-11-16-174715"]
check("**未归一时**顺序与真实时间序不一致",
      [r[1] for r in raw_rows] != _real_order
      and raw_rows.index(next(r for r in raw_rows if r[2] == "triggered")) != 3,
      [f"{r[1]}:{r[2]}" for r in raw_rows])
check("**未归一时**连续跳过被算成 1（真实是 3 次连续）→ 漏报",
      cgs.judge(raw_rows)[2]["daily-learning-gate"]["consecutive_skipped"] == 1,
      cgs.judge(raw_rows)[2]["daily-learning-gate"])
check("**未归一时**不报 FAIL（阈值 3）→ 额度枯竭却不报警",
      not cgs.judge(raw_rows)[0], cgs.judge(raw_rows)[0][:70] if cgs.judge(raw_rows)[0] else "")
check("**归一后**顺序正确：triggered 在最后",
      norm_rows[-1][2] == "triggered", [f"{r[1]}:{r[2]}" for r in norm_rows])
# 归一后：最新一条是 triggered（成功）→ 连续跳过被打断 = 0
check("**归一后**连续跳过 = 0（最新一次是 triggered，streak 被打断）",
      cgs.judge(norm_rows)[2]["daily-learning-gate"]["consecutive_skipped"] == 0,
      cgs.judge(norm_rows)[2]["daily-learning-gate"])
check("**归一后**last_ts 是最新的那条（2026-11-16 17:47）",
      cgs.judge(norm_rows)[2]["daily-learning-gate"]["last_ts"] == "2026-11-16-174715",
      cgs.judge(norm_rows)[2]["daily-learning-gate"]["last_ts"])

# ---------------------------------------------------------------- ⑤
print("\n【⑤】连续 3 次跳过（时间递增、格式混用）必须报 FAIL")
rows = [("daily-learning-gate", "2026-11-14-090000", "skipped", "quota"),
        ("daily-learning-gate", "2026-11-15T09:00:00", "skipped", "quota"),
        ("daily-learning-gate", "2026-11-16T09:00:00", "skipped", "quota")]
p3, _, _ = cgs.judge(rows)
check("格式混用的 3 次连续跳过 → FAIL", bool(p3), rows)
check("FAIL 消息含「需要充值」（quota 原因）",
      p3 and "需要充值" in p3[0], p3[0] if p3 else "")

print()
print("=" * 60)
print(f" 结果: PASS={PASS} FAIL={FAIL}")
print("=" * 60)
sys.exit(1 if FAIL else 0)
