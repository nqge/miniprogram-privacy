#!/usr/bin/env python3
"""闸门「跳过原因」的单测 —— 防「三种原因被说成同一种话」。

## 为什么需要它（2026-11-16 实测）

额度闸门有**三种**跳过原因，处置完全不同：

| reason | 含义 | 处置 |
|---|---|---|
| `quota` | 额度耗尽（429 + Token Plan） | **需要牛哥哥充值** |
| `upstream_busy` | 上游整点过载（529 overloaded） | 等 1-5 分钟就好，**不用充值** |
| `rate_limited` | 普通限流（429 但无额度特征） | 等 1-2 分钟，**不用充值** |

而【G2】断言在「连续跳过 ≥3 次」时会报一条 FAIL 消息。
**如果这条消息不区分原因**，那么：
上游连续抖 3 次 → 被报成「额度长期枯竭，需要充值」→ **假警报**（E66）：
让牛哥哥为一个不是他的问题去掏钱。

→ 断言必须**忠实反映原因**。

## 跑法

    python3 scripts/test_gate_skip_reasons.py
"""

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "check_gate_skips.py"
spec = importlib.util.spec_from_file_location("check_gate_skips", SCRIPT)
cgs = importlib.util.module_from_spec(spec)
sys.modules["check_gate_skips"] = cgs
spec.loader.exec_module(cgs)

GATE = Path(__file__).resolve().parent / "daily_learning_gate.py"
spec2 = importlib.util.spec_from_file_location("daily_learning_gate", GATE)
gate = importlib.util.module_from_spec(spec2)
sys.modules["daily_learning_gate"] = gate
spec2.loader.exec_module(gate)

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


def mk(reason, n=3, name="daily-learning-gate"):
    return [(name, f"2026-11-1{i}-090000", "skipped", reason) for i in range(n)]


print("=" * 60)
print(" 闸门跳过原因 · 单测")
print("=" * 60)

# ---------------------------------------------------------------- ①
print("\n【①】闸门侧的 reason 分类（预检结论文本 → 结构化标签）")
cases = [
    ("结论：**先别跑** —— 额度不够，跑也是白烧", "quota"),
    ("结论：**上游瞬时过载**（不是额度问题）—— 重试后仍不通", "upstream_busy"),
    ("结论：**先别跑** —— 上游普通限流（非额度耗尽），等一会儿再试", "rate_limited"),
    ("额度不足：HTTP 429 Token Plan 用量上限", "quota"),
    ("上游繁忙：HTTP 529（overloaded，瞬时可恢复，非额度问题）", "upstream_busy"),
    ("普通限流：HTTP 429（普通限流，非额度耗尽）", "rate_limited"),
    ("完全没见过的原因", "unknown"),
]
for text, expected in cases:
    got = gate._classify_reason(text)
    check(f"{expected:14s} ← {text[:36]}", got == expected, f"实际 {got}")

# ---------------------------------------------------------------- ②
print("\n【②】断言侧：三种原因的措辞必须不同，且指向不同处置")
p_q, _, _ = cgs.judge(mk("quota"))
p_u, _, _ = cgs.judge(mk("upstream_busy"))
p_r, _, _ = cgs.judge(mk("rate_limited"))

check("quota → 消息说「需要充值」",
      p_q and "需要充值" in p_q[0], p_q[0] if p_q else "(无)")
check("upstream_busy → 消息说「不是额度」且「不用充值」",
      p_u and "不是额度" in p_u[0] and "不用充值" in p_u[0], p_u[0] if p_u else "(无)")
check("rate_limited → 消息说「不是额度」且「不用充值」",
      p_r and "不是额度" in p_r[0] and "不用充值" in p_r[0], p_r[0] if p_r else "(无)")
check("三种原因的消息互不相同",
      len({p_q[0], p_u[0], p_r[0]}) == 3)
check("upstream_busy 的消息**不含**「需要充值」",
      p_u and "需要充值" not in p_u[0], p_u[0] if p_u else "(无)")

# ---------------------------------------------------------------- ③
print("\n【③】老日志（无结构化 reason）不能崩，也不能凭空指控充值")
p_old, _, d_old = cgs.judge(mk(None))
check("None reason → 不崩", bool(p_old))
check("None reason → 不说「需要充值」",
      p_old and "需要充值" not in p_old[0], p_old[0] if p_old else "(无)")
check("None reason → 说「未记录」",
      p_old and "未记录" in p_old[0], p_old[0] if p_old else "(无)")
check("None reason → detail 里 reasons 为空",
      d_old.get("daily-learning-gate", {}).get("reasons") == [],
      d_old)

# ---------------------------------------------------------------- ④
print("\n【④】parse_reason 能从真实日志行里取到值")
check("标准行 → quota",
      cgs.parse_reason("[daily-learning-gate] GATE_RESULT=skipped reason=quota") == "quota")
check("带前缀的行 → upstream_busy",
      cgs.parse_reason("[x-gate] GATE_RESULT=skipped reason=upstream_busy") == "upstream_busy")
check("没有 reason → None", cgs.parse_reason("GATE_RESULT=ok reason=gate_passed") == "gate_passed")
check("完全没有 reason → None", cgs.parse_reason("GATE_RESULT=skipped") is None)

# ---------------------------------------------------------------- ⑤
print("\n【⑤】解析 result 不受影响（老逻辑不能被改坏）")
check("skipped", cgs.parse_result("GATE_RESULT=skipped reason=quota") == "skipped")
check("ok", cgs.parse_result("GATE_RESULT=ok reason=gate_passed") == "ok")
check("无内容 → None", cgs.parse_result("nothing here") is None)

print()
print("=" * 60)
print(f" 结果: PASS={PASS} FAIL={FAIL}")
print("=" * 60)
sys.exit(1 if FAIL else 0)
