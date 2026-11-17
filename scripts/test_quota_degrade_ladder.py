#!/usr/bin/env python3
"""降档阶梯的单测（2026-11-17 加）。

**为什么要有这个测试**：2026-11-17 09:00 真实事故 ——
上游 HTTP 529（overloaded）连续 3 次，闸门按设计拦下了 daily-learning。
但实测发现：**大探针过不去 ≠ 这活干不了**
（15000 ❌ / 8000 ❌ / 3000 ✅ / 2000 ❌，而 max_tokens=5 → 200）。

→ 加了「降档阶梯」：同尺寸重试用尽后，用更小的尺寸再探，
   有一档通过就放行。

**本测试要防的三个坑**（每个都做过故障注入）：
  1. 降档**不能**在额度类错误（429/402）时触发
     —— 额度不足时换小探针也还是不够，
        降了只会给出「可以跑」的**假结论**，那才是真正的危害。
  2. 降档**必须**真的调用 probe_once（小尺寸），不能只是改文案。
  3. 降档通过后，msg 里要带「降档」标记
     —— 否则调用方（预检 main / 闸门）无法区分
        「额度真的够」和「上游过载中但通路恢复」。
"""
import importlib.util
import sys
from pathlib import Path

WS = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("qp", WS / "scripts" / "quota_preflight.py")
qp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qp)

FAILS = []


def check(cond, label, why=""):
    if cond:
        print(f"  ✅ {label}")
    else:
        print(f"  ❌ {label}" + (f" —— {why}" if why else ""))
        FAILS.append(label)


OVERLOADED = "上游繁忙：HTTP 529 (overloaded)"
QUOTA = "额度不足：HTTP 429 Token Plan"


def mk(seq):
    """造一个按 seq 依次返回的 probe_once。"""
    calls = []

    def fake(tokens, timeout=None):
        calls.append(tokens)
        item = seq[min(len(calls) - 1, len(seq) - 1)]
        return item

    return fake, calls


print("=" * 60)
print(" 降档阶梯单测")
print("=" * 60)

print("\n【①】降档只在 529 过载时触发")
# 顺序：15000×3 全挂 → 8000 挂 → 3000 过
fake, calls = mk([
    (False, OVERLOADED, 1.0),
    (False, OVERLOADED, 1.0),
    (False, OVERLOADED, 1.0),
    (False, OVERLOADED, 1.0),   # 8000 也挂
    (True, "额度可用", 2.0),      # 3000 过
])
qp.probe_once = fake
qp.time.sleep = lambda *_: None
ok, msg, dt = qp.probe(15000, retries=3, backoff=0, degrade_ladder=[8000, 3000, 1000])
check(ok, "15000 连挂 → 降档后通过 → 放行")
check(calls[:3] == [15000, 15000, 15000], "先按原尺寸重试 3 次")
check(calls[3] == 8000 and calls[4] == 3000, "再依次降档 8000 → 3000")
check("降档" in msg, "msg 带「降档」标记（调用方要能区分）")
check(1000 not in calls, "3000 通过后就不再试 1000（够用就停）")

print("\n【②】额度类错误绝不降档（最关键的一条）")
fake, calls = mk([(False, QUOTA, 1.0)])
qp.probe_once = fake
ok, msg, dt = qp.probe(15000, retries=3, backoff=0, degrade_ladder=[8000, 3000, 1000])
check(not ok, "429 → 仍然拦下")
check(calls == [15000], "只探了一次原尺寸，**没有**降档重探")
check("额度" in msg and "降档" not in msg, "结论仍是「额度不足」，没被降档污染")

print("\n【③】402 也不降档")
fake, calls = mk([(False, "额度不足：HTTP 402", 1.0)])
qp.probe_once = fake
ok, msg, dt = qp.probe(15000, retries=3, backoff=0, degrade_ladder=[8000, 3000])
check(not ok and calls == [15000], "402 → 不降档，只探一次")

print("\n【④】降档探针也报额度错误 → 立刻停，不再往下试")
fake, calls = mk([(False, OVERLOADED, 1.0), (False, QUOTA, 1.0)])
qp.probe_once = fake
ok, msg, dt = qp.probe(15000, retries=1, backoff=0, degrade_ladder=[8000, 3000, 1000])
check(not ok, "降档时撞上额度错误 → 拦下")
check(1000 not in calls, "没有继续往下试（发现是真额度问题就停）")
check("额度" in msg, "结论改成额度问题")

print("\n【⑤】降档阶梯为空 → 行为与旧版一致")
fake, calls = mk([(False, OVERLOADED, 1.0)])
qp.probe_once = fake
ok, msg, dt = qp.probe(15000, retries=1, backoff=0, degrade_ladder=[])
check(not ok and calls == [15000], "空阶梯 → 不降档")

print("\n【⑥】第一次就通过 → 不降档，不多花一次请求")
fake, calls = mk([(True, "额度可用（~15000 token 探测通过，14.17s）", 14.0)])
qp.probe_once = fake
ok, msg, dt = qp.probe(15000, retries=3, backoff=0, degrade_ladder=[8000, 3000, 1000])
check(ok and calls == [15000], "一次就过 → 只探了一次")
check("降档" not in msg, "msg 不带「降档」（确实是额度够，不是降档通过）")

print("\n【⑦】降档全部失败 → 仍判不通，且结论说「上游过载」")
fake, calls = mk([(False, OVERLOADED, 1.0)])
qp.probe_once = fake
ok, msg, dt = qp.probe(15000, retries=1, backoff=0, degrade_ladder=[8000, 3000])
check(not ok, "全挂 → 拦下")
check("上游繁忙" in msg or "过载" in msg, "结论仍是「上游过载」，不是「额度不足」")

print("\n【⑧】时间预算：预检必须在闸门的 timeout 内结束（防 fail-open）")
# 2026-11-17 实测发现的隐患：
#   闸门给预检 timeout=180s，而预检最坏耗时 =
#     3 次探测 × 90s + 退避 25s/50s = **345s**（还没算降档阶梯）
#   → 必然超支 → 闸门走 subprocess.TimeoutExpired → 「预检超时 → 放行」= **fail-open**。
#   fail-open 本身没错（不确定 ≠ 额度不足），但**不该是常态**。
#   → 修法：预检自己管住时间（budget），且 **budget < 闸门 timeout**。
import re
from pathlib import Path as _P

_gate_src = (_P(WS) / "scripts" / "daily_learning_gate.py").read_text(encoding="utf-8")
_pf_src = (_P(WS) / "scripts" / "quota_preflight.py").read_text(encoding="utf-8")

m_gate = re.search(r"GATE_PREFLIGHT_TIMEOUT\s*=\s*(\d+)", _gate_src)
m_budget = re.search(r"PREFLIGHT_BUDGET_S\s*=\s*(\d+)", _gate_src)
check(m_gate is not None, "闸门有 GATE_PREFLIGHT_TIMEOUT 常量")
check(m_budget is not None, "闸门有 PREFLIGHT_BUDGET_S 常量")

if m_gate and m_budget:
    gt, bg = int(m_gate.group(1)), int(m_budget.group(1))
    check(bg < gt, f"预检预算({bg}s) < 闸门 timeout({gt}s)",
          "预算必须小于 timeout，否则闸门会 fail-open")
    check(gt - bg >= 20, f"留有余量（timeout - 预算 = {gt - bg}s）",
          "要给进程启动和收尾留余量")

check('"--budget"' in _gate_src, "闸门把 --budget 传给预检",
      "不传的话预检仍按自己的最坏耗时跑 → 又超支")
check("timeout=GATE_PREFLIGHT_TIMEOUT" in _gate_src, "闸门用常量而不是字面量 180",
      "字面量 180 和预算 150 很容易改一个不改另一个")
check("budget_s" in _pf_src, "预检有 budget_s 参数")
check("_over_budget" in _pf_src, "预检有超时预算检查")
m_pt = re.search(r"def probe_once\(tokens: int = \d+, timeout: int = (\d+)\)", _pf_src)
check(m_pt is not None, "probe_once 的单次 timeout 可配")
if m_pt:
    check(int(m_pt.group(1)) <= 30, f"单次探测 timeout={m_pt.group(1)}s ≤ 30",
          "实测正常 1-2s；90s 的话 3 次就 270s")

print("\n" + "=" * 60)
if FAILS:
    print(f" 结果: PASS={7 * 2 - len(FAILS)} FAIL={len(FAILS)}")
    for f in FAILS:
        print(f"   ❌ {f}")
    print("=" * 60)
    sys.exit(1)
print(" 结果: PASS=全部通过")
print("=" * 60)
sys.exit(0)
