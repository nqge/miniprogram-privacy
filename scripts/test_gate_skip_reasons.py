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

# ---------------------------------------------------------------- ⑧
print("\n【⑧】判据侧与闸门侧的分类规则必须一致")
# ⚠️ 2026-11-17 实测：旧历史里 blocked 记录的 reason 是**原始预检文本**，
#   判据侧要把它归一成结构化标签。如果两侧规则不一样 →
#   「旧记录说 upstream_busy、判据说 quota」→ 措辞自相矛盾。
_raw_samples = [
    "结论：**上游瞬时过载**（不是额度问题）—— 重试后仍不通",
    "HTTP 529 overloaded",
    "已达到 Token Plan 用量上限",
    "普通限流，等 1-2 分钟",
    "",
]
for _r in _raw_samples:
    _a = cgs._classify_reason_text(_r)
    _b = gate._classify_reason(_r)
    check(f"两侧对 {_r[:26]!r} 分类一致（{_a}）",
          _a == _b, f"判据侧={_a} 闸门侧={_b}")

# 已结构化的标签要原样保留
for _t in ("quota", "upstream_busy", "rate_limited"):
    check(f"结构化标签 {_t} 原样保留",
          cgs._classify_reason_text(_t) == _t, cgs._classify_reason_text(_t))

# ---------------------------------------------------------------- ⑨
print("\n【⑨】旧版双记录（blocked+skipped 同 ts）不能被数成两次")
_rows = [
    ("daily-learning-gate", "2026-11-17-090032", "blocked", "结论：**上游瞬时过载**（不是额度问题）—— 重试后仍不通"),
    ("daily-learning-gate", "2026-11-17-090152", "skipped", "upstream_busy"),
    ("daily-learning-gate", "2026-11-17-092039", "blocked", "结论：**上游瞬时过载**（不是额度问题）—— 重试后仍不通"),
    ("daily-learning-gate", "2026-11-17-092039", "skipped", "upstream_busy"),
    ("daily-learning-gate", "2026-11-17-093831", "triggered", ""),
]
_p, _w, _d = cgs.judge(_rows)
check("blocked 归一化为 skipped 后算成连续",
      _d.get("daily-learning-gate", {}).get("best_streak", 0) >= 2, _d)
check("消息里出现 upstream_busy",
      bool(_p) and "upstream_busy" in _p[0], _p[0] if _p else "")
check("消息**不含**原始预检文本",
      bool(_p) and "结论：" not in _p[0], _p[0] if _p else "")

print("\n" + "=" * 60)
print(f" 结果: PASS={PASS} FAIL={FAIL}")
print("=" * 60)
if FAIL:
    raise SystemExit(1)

# ---------------------------------------------------------------- ⑩
print("\n【⑩】上游原因的消息必须能被 healthcheck 归进「等你」（别当假警报）")
# ⚠️ 2026-11-17 实测踩到：消息里既不含「Token Plan」也不含「额度耗尽」
#   → healthcheck 的 is_waiting() 认不出来 → 被归进「**可处理 - 现在就该修**」
#   → 但上游过载**谁都不用做什么**（1-5 分钟自己好），而且已经恢复了
#   → 长期性假警报。
#   → 修法：措辞里明确写「**无需处理**」，并让 WAITING_PATTERNS 认这个词。
_rows_busy = [
    ("daily-learning-gate", "2026-11-17-090152", "skipped", "upstream_busy"),
    ("daily-learning-gate", "2026-11-17-092039", "skipped", "upstream_busy"),
    ("daily-learning-gate", "2026-11-17-092823", "skipped", "upstream_busy"),
    ("daily-learning-gate", "2026-11-17-093831", "triggered", ""),
]
_p2, _w2, _d2 = cgs.judge(_rows_busy)
check("上游原因连跳 3 次 → 有 FAIL", bool(_p2), _p2)
_msg = _p2[0] if _p2 else ""
check("消息含「无需处理」（让 is_waiting 认得出来）", "无需处理" in _msg, _msg)
check("消息不含「需要充值」", "需要充值" not in _msg, _msg)

# 额度原因**不能**说「无需处理」（那是反过来的错）
_rows_quota = [
    ("daily-learning-gate", "2026-11-17-090152", "skipped", "quota"),
    ("daily-learning-gate", "2026-11-17-092039", "skipped", "quota"),
    ("daily-learning-gate", "2026-11-17-092823", "skipped", "quota"),
    ("daily-learning-gate", "2026-11-17-093831", "triggered", ""),
]
_p3, _w3, _d3 = cgs.judge(_rows_quota)
check("额度原因连跳 3 次 → 有 FAIL", bool(_p3), _p3)
_msg3 = _p3[0] if _p3 else ""
check("额度原因的消息说「需要充值」", "需要充值" in _msg3, _msg3)
check("额度原因的消息**不含**「无需处理」", "无需处理" not in _msg3, _msg3)

print()
print("=" * 60)
print(f" 结果: PASS={PASS} FAIL={FAIL}")
print("=" * 60)
sys.exit(1 if FAIL else 0)
