#!/usr/bin/env python3
"""cron 投递判据（E55 维度）的自检 —— 2026-10-29 新增

背景（一个真实的误报）：
  今天 08:00 茶馆 cron 跑完是 `delivered=false`。按老判据该报 FAIL，
  但那次输出是「新增 0 条 / 无新内容」—— **没内容就不该打扰牛哥哥，不投递才是正确的**。
  10-28 那批「运行成功但未投递」才是真故障。两者字段一模一样，性质相反。

洞在：`check_cron_delivery.py` 只按 `delivery.mode` 分组，
  **同一 mode 内没再往下分** —— 字段里没有「本该发什么」这一维。

补法：**加一维**（`_classify_output`），而不是放宽断言。

⚠️ 这个测试的三个铁律（源自 E52 / E53）：
  1. **测真身代码** —— 直接 import `check_cron_delivery`，不抄一份判据
     （抄版会和真版悄悄漂移，2026-10-27 就因此踩过）
  2. **必须做故障注入** —— 光跑「正常用例通过」证明不了判据抓得住故障
  3. **判据本身要保守** —— 判不准时必须回到严格判据（unknown → 不放行）

跑法：python3 scripts/test_cron_delivery_output.py
退出码 0 = 全通过
"""
import os
import sys

WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(WS, "scripts"))

import check_cron_delivery as ccd  # ← 真身，不是抄版


def main():
    cases = []

    # ── 1. 明确的「无产出」→ none ────────────────────
    no_output_summaries = [
        "teahouse-monitor 巡检完成，无外部消息发送。\n- 拉取 issue 共 96 条，新增 0 条\n- 无新蒸馏内容",
        "**无新内容。** 茶馆监控本轮没有新议题。",
        "脚本报告「无新内容」——本轮没有新增议题，无需蒸馏。",
        "Script reports 无新内容 — nothing new distilled, no learning note appended.",
        "巡检完成：无异常，无需处理。",
    ]
    for s in no_output_summaries:
        cases.append(
            (ccd._classify_output(s) == "none",
             f"无产出摘要应判 none: {s[:34]}...")
        )

    # ── 2. 明确的「有产出」→ has ─────────────────────
    has_output_summaries = [
        "巡检通过，PASS=72，FAIL=1（长期项），WARN=6。",
        "已修复索引不完整问题，重建完成。",
        "茶馆新增 3 条议题，已蒸馏入库。",
        "投递失败：飞书返回 403。",
    ]
    for s in has_output_summaries:
        cases.append(
            (ccd._classify_output(s) in ("has", "unknown"),
             f"有产出摘要不得判 none（has 或 unknown 都算不放行）: {s[:30]}...")
        )
    # 但这些必须明确判 has —— 含「未投递/已修复」等强信号
    for s in ["已修复索引不完整问题，重建完成。",
              "茶馆新增 3 条议题，已蒸馏入库。",
              "投递失败：飞书返回 403。"]:
        cases.append(
            (ccd._classify_output(s) == "has",
             f"强信号摘要应明确判 has: {s[:30]}...")
        )

    # ── 3. 判不准 → unknown（保守，不放行）────────────
    cases.append(
        (ccd._classify_output("") == "unknown", "空摘要应判 unknown")
    )
    cases.append(
        (ccd._classify_output(None) == "unknown", "None 摘要应判 unknown")
    )
    cases.append(
        (ccd._classify_output("一段没有任何已知标记的普通描述文本") == "unknown",
         "无标记摘要应判 unknown（保守）")
    )

    # ── 4. 【故障注入】混合语义：「无新增」但「已修复」→ 必须 has ──
    # 这是最危险的一种误放行：摘要里同时含「无产出」和「有产出」标记。
    # 若判成 none，真故障会被放过 —— 断言漏掉的那一类往往正是最该被看见的。
    mixed = "本轮无新增议题，但巡检发现索引不完整，已修复并重建索引。"
    cases.append(
        (ccd._classify_output(mixed) == "has",
         "【注入】混合语义必须判 has（有产出标记优先），不得误放行")
    )

    mixed2 = "无新内容，但投递失败，需要人工处理。"
    cases.append(
        (ccd._classify_output(mixed2) == "has",
         "【注入】『无新内容 + 投递失败』必须判 has")
    )

    # ── 5. 【故障注入】真实故障样本必须判 has/silent ──
    # 这两条是 10-28 真实故障的摘要形态（当轮确实有内容，却没送到手上）
    real_fault = (
        "巡检完成。两项「可处理」失败都已修复，现在 PASS=72 / FAIL=3 / WARN=4，"
        "可处理组已清零。\n\n## 【可处理】— 已修复\n**1. 【C】索引不完整 67/68 → 已自愈**"
    )
    cases.append(
        (ccd._classify_output(real_fault) != "none",
         "【注入】10-28 真实故障摘要不得被判为「无产出」")
    )

    # ── 6. 模式列表非空且互不冲突的 sanity ────────────
    cases.append(
        (len(ccd._NO_OUTPUT_PATTERNS) > 0 and len(ccd._HAS_OUTPUT_PATTERNS) > 0,
         "模式列表非空")
    )
    overlap = set(ccd._NO_OUTPUT_PATTERNS) & set(ccd._HAS_OUTPUT_PATTERNS)
    cases.append(
        (not overlap, f"两组模式不应重叠（重叠: {overlap}）")
    )

    # ── 7. 英文大小写不敏感 ──────────────────────────
    cases.append(
        (ccd._classify_output("NOTHING NEW distilled this round") == "none",
         "英文标记应大小写不敏感")
    )

    failed = 0
    for ok, name in cases:
        if ok:
            print(f"✅ PASS  {name}")
        else:
            print(f"❌ FAIL  {name}")
            failed += 1

    print("=" * 60)
    if failed:
        print(f" 结果: {len(cases) - failed}/{len(cases)} 通过，{failed} 个失败")
    else:
        print(f" 结果: {len(cases)}/{len(cases)} 全部通过")
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
