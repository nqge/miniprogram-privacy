#!/usr/bin/env python3
"""额度耗尽分类器的单测 —— 防「措辞一变就漏判」。

## 为什么要有这个测试（2026-11-09 实测）

11-08 daily-learning 失败，healthcheck 报「可处理 - 现在就该修，原因：接口限流」。
但独立复核 cron runs 原始记录后发现：

- 报错是 `⚠️ API rate limit reached (rate_limit)`，**3 个候选模型全部同时中招**
- 额度探测（~15k token 真实负载）在 23 小时后**通过** → 是**账号级额度耗尽**（计费周期），
  不是每分钟抖动
- 而额度耗尽**只能靠牛哥哥充值解决** —— 我改代码改不了

根因：`check_cron_delivery.py` 只认字面量 `"Token Plan"`，
而 11-08 的报错早已变成 `API rate limit reached` → 匹配不上 → 落到「可处理」组。

**这是 E62 的老毛病（派生副本漂移不产生错误信号）换了个马甲**：
分类器与故障形态之间没有任何测试，于是措辞一变就静默漏判。

## 断言

1. 3 模型同时 rate limit → 判为「额度耗尽」（长期项，需充值）
2. 旧措辞「Token Plan 用量上限」→ 仍判为额度耗尽（不能修坏老路径）
3. 单模型 rate limit → **不**判为额度耗尽（那是每分钟限流，能自愈）
4. 判据必须是**特征**而非字面量：换一种措辞但保留「全部模型同时中招」→ 仍判对
5. 分类结果要能被 healthcheck.sh 的 WAITING_PATTERNS 真的接住（端到端）
6. 额度耗尽**不能**被算进「可处理」计数（E59：长期项不该让退出码变红）
7. 非额度类故障（脚本错误、超时）**仍然**算「可处理」（别把真故障一起藏起来）

单测要 import **真身**（check_cron_delivery._is_quota_exhausted），
不抄一份判据 —— 抄版会和真版悄悄漂移。
"""

import re
import sys
from pathlib import Path

WS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WS / "scripts"))

from check_cron_delivery import _is_quota_exhausted  # noqa: E402

FAILED = []
PASSED = 0


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ✅ PASS  {name}")
    else:
        FAILED.append(name)
        print(f"  ❌ FAIL  {name}" + (f"  → {detail}" if detail else ""))


# ── 真实报错样本（来自 /root/.openclaw/cron/runs/*.jsonl）────────────────
ERR_3MODELS = (
    "Error: All models failed (3): minimax/MiniMax-M3: ⚠️ API rate limit reached. "
    "Please try again later. (rate_limit) | minimax/MiniMax-M2.7-highspeed: "
    "⚠️ API rate limit reached. Please try again later. (rate_limit) | "
    "minimax/MiniMax-M2.7: ⚠️ API rate limit reached. Please try again later. (rate_limit)"
)
ERR_TOKEN_PLAN = (
    "Error: All models failed (3): minimax/MiniMax-M3: 429 已达到 Token Plan 用量上限"
)
ERR_1MODEL = (
    "Error: All models failed (1): minimax/MiniMax-M3: ⚠️ API rate limit reached. "
    "Please try again later. (rate_limit)"
)
ERR_TIMEOUT = "Error: execution timed out after 180000ms"
ERR_SCRIPT = "Error: Traceback (most recent call last): File \"x.py\", line 3"


def test_classifier():
    print("【1】分类器判据")
    check(
        "3 模型同时 rate limit → 额度耗尽",
        _is_quota_exhausted(ERR_3MODELS),
        "实测 11-08 就是这个形态",
    )
    check(
        "旧措辞 Token Plan → 仍判为额度耗尽（没修坏老路径）",
        _is_quota_exhausted(ERR_TOKEN_PLAN),
    )
    check(
        "单模型 rate limit → 不判额度耗尽（每分钟限流能自愈）",
        not _is_quota_exhausted(ERR_1MODEL),
    )
    check("超时 → 不判额度耗尽", not _is_quota_exhausted(ERR_TIMEOUT))
    check("脚本报错 → 不判额度耗尽", not _is_quota_exhausted(ERR_SCRIPT))
    check("空报错 → 不判额度耗尽", not _is_quota_exhausted(""))


def test_wording_independence():
    """判据必须是**特征**而非字面量：措辞再变，只要「全部模型同时中招」就该判对。"""
    print("【2】措辞无关性（防「措辞一变就漏判」）")
    variant = (
        "Error: All models failed (2): minimax/SomeModel: quota exhausted "
        "(429). Please try again later."
    )
    check(
        "换了措辞但保留「全部模型同时中招」→ 仍判额度耗尽",
        _is_quota_exhausted(variant),
    )
    # 逐字面量匹配的老实现会在这个样本上漏判
    check(
        "老实现（只认字面量）在这个样本上确实会漏判",
        "Token Plan" not in variant,
        "样本里没有 Token Plan 字样",
    )


def test_end_to_end_pattern():
    """端到端：分类结果要真的被 healthcheck.sh 的 WAITING_PATTERNS 接住。"""
    print("【3】端到端：healthcheck.sh 的 WAITING_PATTERNS 接得住吗")
    sh = (WS / "scripts" / "healthcheck.sh").read_text(encoding="utf-8")
    m = re.search(r"WAITING_PATTERNS=\((.*?)\n\)", sh, re.S)
    check("能解析出 WAITING_PATTERNS", bool(m))
    if not m:
        return
    pats = re.findall(r'"([^"]+)"', m.group(1))
    check(
        'WAITING_PATTERNS 含 "额度耗尽"（新措辞）',
        "额度耗尽" in pats,
        f"当前 patterns: {pats}",
    )
    check(
        'WAITING_PATTERNS 含 "Token Plan"（老措辞）',
        "Token Plan" in pats,
    )
    # 真身产出的 reason 文本必须命中
    reason = "，原因：额度耗尽(API rate limit，需充值)"
    check(
        "分类器产出的 reason 文本命中 WAITING_PATTERNS",
        any(p in reason for p in pats),
        f"reason={reason!r} pats={pats}",
    )


def test_quick_health_pattern():
    print("【4】端到端：quick_health.py 的 WAITING_ON_USER 接得住吗")
    py = (WS / "scripts" / "quick_health.py").read_text(encoding="utf-8")
    check(
        'quick_health.py 含 "额度耗尽"',
        '"额度耗尽"' in py,
    )
    check(
        'quick_health.py 复用真身判据（import 而非抄一份）',
        "from check_cron_delivery import" in py,
        "抄版判据会和真版漂移（E11）",
    )


def test_not_hiding_real_failures():
    """E59 反面：不能为了降假警报把**真故障**一起藏进长期项。"""
    print("【5】别把真故障藏起来")
    py = (WS / "scripts" / "quick_health.py").read_text(encoding="utf-8")
    # 超时/脚本报错的 reason 文本不能含「额度耗尽」
    check(
        "超时 reason 不含「额度耗尽」",
        "额度耗尽" not in "，原因：执行超时",
    )
    check(
        "脚本错误 reason 不含「额度耗尽」",
        "额度耗尽" not in "，原因：Traceback",
    )
    # 且真的保留了「可处理」分类逻辑
    check(
        "quick_health.py 仍有 actionable/waiting 双分组",
        "actionable" in py and "waiting" in py,
    )


def main():
    print("=" * 62)
    print(" 额度耗尽分类器单测（2026-11-09）")
    print("=" * 62)
    test_classifier()
    test_wording_independence()
    test_end_to_end_pattern()
    test_quick_health_pattern()
    test_not_hiding_real_failures()
    print("=" * 62)
    print(f" 结果: PASS={PASSED}  FAIL={len(FAILED)}")
    if FAILED:
        for f in FAILED:
            print(f"   ❌ {f}")
        return 1
    print(" 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
