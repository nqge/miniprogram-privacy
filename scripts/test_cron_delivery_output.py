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

    # ── 8. E58：连续失败次数（2026-11-03 新增）────────────
    #
    # 背景：daily-learning 有 7 次 error（含 10-30/10-31/11-01 连续 3 天全挂），
    # 而巡检只报「投递正常」—— 因为 error 统计写在 else 分支里，
    # 只要历史上存在任何一次「ok 但没投递」就永远走不到 → error 被吞掉。
    # 修法：error 统计与未投递统计正交，并新增「最长连续失败次数」维度。
    #
    # ⚠️ 造数据铁律（E58 实测踩坑）：必须用**绝对时间戳**造「连续」，
    #    不要用相对 offset —— 连续与否是按 runs 数组**顺序**判的，
    #    但「计划槽位失败 vs 手动补跑成功」要靠时间戳对齐 cron 表达式来区分。

    def mk(status, delivered=None):
        return {"status": status, "delivered": delivered, "runAtMs": 1790000000000}

    # 8.1 全成功 → 连续失败 0
    cases.append(
        (ccd._max_consecutive_errors([mk("ok"), mk("ok"), mk("ok")]) == 0,
         "全成功时连续失败次数应为 0")
    )

    # 8.2 连续 3 次 error → 应报 3
    cases.append(
        (ccd._max_consecutive_errors([mk("error"), mk("error"), mk("error"), mk("ok")]) == 3,
         "连续 3 次 error 应识别为连续 3 次")
    )

    # 8.3 零散失败 → 不是连续（这是关键区分）
    cases.append(
        (ccd._max_consecutive_errors([mk("ok"), mk("error"), mk("ok"), mk("error"), mk("ok")]) == 1,
         "零散 error 不应被当成连续失败")
    )

    # 8.4 真实数据复现：daily-learning 的 error 序列 → 连续 3
    dl_runs = [
        mk("ok", True),          # 11-02
        mk("error"),             # 11-01
        mk("error"),             # 10-31
        mk("error"),             # 10-30
        mk("ok", True),          # 10-29
    ]
    cases.append(
        (ccd._max_consecutive_errors(dl_runs) == 3,
         "daily-learning 真实序列应识别出连续 3 次失败")
    )

    # 8.5 真实数据复现：daily-reflection 计划槽位连挂 → 连续 14+
    dr_runs = [mk("ok", True)] + [mk("error")] * 14
    cases.append(
        (ccd._max_consecutive_errors(dr_runs) == 14,
         "daily-reflection 真实序列应识别出连续 14 次失败")
    )

    # 8.6 单个 error → 连续 1
    cases.append(
        (ccd._max_consecutive_errors([mk("ok"), mk("error")]) == 1,
         "单个 error 的连续次数为 1")
    )

    # 8.7 空列表 → 0（不崩）
    cases.append(
        (ccd._max_consecutive_errors([]) == 0,
         "空 runs 列表应返回 0 而不抛异常")
    )

    # 8.8 【故障注入】判据要能抓住「中间断了」的情况：
    #     失败-成功-失败-失败-失败 → 最长连续 3（在数组尾部）
    cases.append(
        (ccd._max_consecutive_errors(
            [mk("error"), mk("ok"), mk("error"), mk("error"), mk("error")]) == 3,
         "应取所有连续段里最长的那个，而不是第一段或最后一段")
    )

    # 8.9 【故障注入】runs 为 None → 不该抛异常（历史读取失败的场景）
    #      真实代码里 runs is None 会被上层 continue 掉，但函数本身要稳
    try:
        ccd._max_consecutive_errors([None])
        none_safe = False
    except Exception:
        none_safe = True
    cases.append((none_safe, "遇到异常元素不应静默返回错误结果"))

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
