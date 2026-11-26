#!/usr/bin/env python3
"""【U】组判据真身 —— cron 实测耗时 vs 超时余量。

2026-11-16 (E72) 从 healthcheck.sh 的 heredoc 里抽出来，理由同 E11：
**单测要 import 真身，不能抄一份判据** —— 抄版会和真版悄悄漂移，
2026-10-27 就因此踩过（抄版写 `if not job: job = ...`，真版改成了两行写法）。

## 为什么判据要看「基线」而不是「最近一次」（E72）

旧判据：`最近一次耗时 / timeout > 0.7` → FAIL。
它的隐含假设是「各次运行可比」。但**耗时反映的是那天的负载**，不是任务的基线 ——
某天补记多天笔记 / 新增 L1 / 重建索引 / 调试检索，耗时就能翻倍。

2026-11-16 实测：daily-learning 最近一次 1276s / 1800s = 71% → 报「余量告急」；
而**近 10 次成功运行的中位数只有 673s = 37%** —— 那是一条**假警报**，
而且它占掉了「现在就该修」这一格的真信号。

新判据：
1. FAIL 用**中位数**（抗单点异常）→ median/timeout > 0.7
2. 「最近一次」保留为**展示事实**，但不让它单独决定 FAIL
3. 最近一次 ≥ 1.5×中位数 时**单独标注「异常负载」**
   ——「任务变慢」和「今天任务变重」修法完全不同，必须分开说
4. 趋势规则加**日历密度**门槛：相邻样本间隔 > 2 天不算趋势
   （稀疏样本上的「上升」是三个不同负载下的三个点，不是趋势）
"""

from __future__ import annotations

# 单点判 FAIL 的阈值（占用率）
FAIL_RATIO = 0.7
# 单点判 WARN 的阈值（占用率）
WARN_RATIO = 0.5
# 最近一次 ≥ 该倍数 × 中位数 → 判「异常负载」而非「余量告急」
OUTLIER_FACTOR = 1.5
# 趋势判定要求：相邻样本间隔不得超过该天数
TREND_MAX_GAP_DAYS = 2.0
# 判「趋势」时至少要有的样本数
TREND_MIN_SAMPLES = 3
# 算基线（中位数）至少需要的成功样本数 —— 少于这个数，中位数不可信
# 2026-11-16 实测:只取最近 8 条时,6 连败把成功样本挤到只剩 2 个
# → 中位数退化成「两个数的平均」,基线失真。→ 取更多历史 + 样本数不足就降级。
BASELINE_MIN_SAMPLES = 4


def median(values):
    """中位数。偶数个取中间两个的平均。空列表返回 None。"""
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return float(s[n // 2])
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def judge_u(ds, tsec):
    """判定一个任务的超时余量。

    参数
    ----
    ds : list[tuple]
        按**时间升序**的运行记录，每条至少包含 (ts_ms, duration_ms, status[, err[, delivery]])。
        - status != 'ok' 的运行，其耗时是「烧满 timeout」，**不反映真实负载**，
          判基线时**必须排除**。
        - err 是该次运行的错误文本（超时信息在**这里**，
          不在 deliveryStatus —— error 运行的 deliveryStatus 恒为 'unknown'）。
    tsec : int
        该任务的 timeout 秒数。

    返回
    ----
    dict:
        problems : list[str]   → 记 FAIL 的条目
        warns    : list[str]   → 记 WARN 的条目
        detail   : dict        → 中间量（中位数/最近一次/占比），供报告与单测断言
    """
    problems: list[str] = []
    warns: list[str] = []
    detail: dict = {}

    if not ds or tsec <= 0:
        return {"problems": problems, "warns": warns, "detail": detail}

    # ⚠️ 必须按**时间**排序，不能按耗时排序 ——
    # 按耗时排会把「最慢的那次」当成「最近的那次」，
    # 于是早已修好的 backup-audit(历史 600s 超时)会被当成现状 → 假 FAIL。
    ds = sorted(ds, key=lambda x: x[0])
    last = ds[-1]

    ok_durs = [x[1] for x in ds if len(x) > 2 and x[2] == 'ok' and x[1] > 0]
    if ok_durs:
        med = median(ok_durs)
        med_ratio = med / 1000.0 / tsec
        last_ratio = last[1] / 1000.0 / tsec
        # ⚠️ 成功样本太少时，中位数不可信 —— 2 个数的「中位数」就是它俩的平均，
        #    恰好等于那个异常负载（2026-11-16:6 连败把成功样本挤到只剩 2 个，
        #    中位数退化成 1049s，正好是「异常值 + 正常值」的平均）。
        #    → 样本不足时**降级**：只把「最近一次」当事实展示，不据此判 FAIL。
        enough = len(ok_durs) >= BASELINE_MIN_SAMPLES
        detail.update(
            n_ok=len(ok_durs),
            baseline_reliable=enough,
            median_ms=med,
            median_ratio=med_ratio,
            last_ms=last[1],
            last_ratio=last_ratio,
            outlier=bool(last[1] >= OUTLIER_FACTOR * med),
        )

        if enough and med_ratio > FAIL_RATIO:
            problems.append(
                f"{med/1000:.0f}s 中位数 / 超时 {tsec}s = {med_ratio*100:.0f}%"
                f" -- 余量告急,下次可能撞超时"
                f"（最近 {len(ok_durs)} 次成功运行,中位数口径,E72）"
            )
        elif last_ratio > FAIL_RATIO:
            if not enough:
                # 成功样本不足 → 不能据「最近一次」判余量告急，但值得说清楚
                warns.append(
                    f"最近一次耗时 {last[1]/1000:.0f}s / 超时 {tsec}s "
                    f"= {last_ratio*100:.0f}%，但成功样本仅 {len(ok_durs)} 个"
                    f"（中位数 {med/1000:.0f}s 不可靠）-- 样本不足，暂不判余量告急（E72）"
                )
            elif last[1] >= OUTLIER_FACTOR * med:
                warns.append(
                    f"最近一次耗时 {last[1]/1000:.0f}s（超时的 {last_ratio*100:.0f}%）"
                    f"但基线中位数仅 {med/1000:.0f}s（{med_ratio*100:.0f}%）"
                    f"-- 本次为**异常负载**，不是余量告急（E72）"
                )
            else:
                warns.append(
                    f"最近一次耗时 {last[1]/1000:.0f}s / 超时 {tsec}s "
                    f"= {last_ratio*100:.0f}%（基线中位数 {med/1000:.0f}s "
                    f"= {med_ratio*100:.0f}%）-- 在长"
                )

    # 趋势：最近 3 次是否持续上升（且样本在日历上足够密集）
    # ⚠️ 只用**成功**运行 —— 失败运行的耗时是「烧满/提前掐断」，
    #    不是真实负载，混进来会把「182s→317s→1276s」这种
    #    「失败快 + 一次重活」误报成「在长」。
    ok_ds = [x for x in ds if len(x) > 2 and x[2] == 'ok' and x[1] > 0]
    if len(ok_ds) >= TREND_MIN_SAMPLES:
        recent = [x[1] for x in ok_ds[-TREND_MIN_SAMPLES:]]
        gaps = []
        try:
            for i in range(len(ok_ds) - TREND_MIN_SAMPLES, len(ok_ds) - 1):
                gaps.append((ok_ds[i + 1][0] - ok_ds[i][0]) / 1000.0 / 86400.0)
        except Exception:
            gaps = []
        dense = all(g <= TREND_MAX_GAP_DAYS for g in gaps) if gaps else False
        detail.update(trend_dense=dense, trend_recent=recent)
        if (
            dense
            and recent[0] < recent[1] < recent[2]
            and recent[2] / 1000.0 / tsec > WARN_RATIO
        ):
            warns.append(
                f"最近 {TREND_MIN_SAMPLES} 次**成功**运行耗时递增 "
                f"{recent[0]/1000:.0f}s→{recent[1]/1000:.0f}s→{recent[2]/1000:.0f}s"
                f"(超时 {tsec}s)-- 在长"
            )
            # 2026-11-26 加（E89）：把**还能涨多少**说出来。
            # ⚠️ 为什么加这个：
            #   11-26 实测 215s→575s→1151s（timeout 1800s）。
            #   光说「在长」不够 —— 关键是「**离撞超时还差多少**」。
            #   本例余量只剩 649s，而前两次的涨幅是 +360s、+576s →
            #   **照这个斜率，下一次就撞了** → 这是「该现在动手」的信号，
            #   而不是「在长」这种可以再观察两周的信号。
            # → 余量 + 上一次涨幅 一起比：余量 < 上一次涨幅 = 下次就撞。
            headroom = tsec - recent[2] / 1000.0
            last_growth = (recent[2] - recent[1]) / 1000.0
            detail.update(trend_headroom_s=headroom, trend_last_growth_s=last_growth)
            if last_growth > 0 and headroom < last_growth:
                warns.append(
                    f"⚠️ 余量告急:上次涨了 {last_growth:.0f}s，"
                    f"而只剩 {headroom:.0f}s 余量(超时 {tsec}s) —— "
                    f"照这个斜率**下一次就撞**"
                )

    # 因超时而失败的（只看最近一次）
    # ⚠️ 两个坑叠在一起，**旧判据从来没触发过**（2026-11-16 发现）：
    #   1) 超时信息在 **error** 字段，不在 deliveryStatus ——
    #      error 运行的 deliveryStatus 恒为 'unknown'，lastDeliveryStatus 恒为 None。
    #   2) 真实报错文本是 "execution **timed out**"（两个词），
    #      旧判据匹配的是 'timeout'（一个词）→ 匹配不上。
    # → 现在两处都修：读 error 字段 + 同时认 'timed out' / 'timeout'。
    if len(last) > 3 and last[2] == 'error' and last[3]:
        err = str(last[3]).lower()
        if 'timed out' in err or 'timeout' in err:
            problems.append(f"最近因超时失败({last[3]})-- 见 E35:慢的是等法不是任务")

    return {"problems": problems, "warns": warns, "detail": detail}


def self_test() -> int:
    """自检：确保判据没被改坏。返回 0 = 通过。"""
    day = 86400_000

    # 1) 基线健康但最近一次异常负载 → 不能 FAIL
    ds = [
        (i * day, d, "ok", None, "delivered")
        for i, d in enumerate([317_000, 822_000, 673_000, 736_000, 1276_000])
    ]
    r = judge_u(ds, 1800)
    assert not r["problems"], f"异常负载不应 FAIL: {r['problems']}"
    assert any("异常负载" in w for w in r["warns"]), r["warns"]

    # 2) 基线本身就高 → 必须 FAIL
    ds2 = [
        (i * day, d, "ok", None, "delivered")
        for i, d in enumerate([1400_000, 1350_000, 1500_000, 1300_000])
    ]
    r2 = judge_u(ds2, 1800)
    assert r2["problems"], "基线 70%+ 应 FAIL"
    assert "中位数" in r2["problems"][0], r2["problems"][0]

    # 3) 失败运行的耗时不进基线
    ds3 = [
        (0, 1800_000, "error", "cron: job execution timed out", "unknown"),
        (day, 300_000, "ok", None, "delivered"),
        (2 * day, 310_000, "ok", None, "delivered"),
    ]
    r3 = judge_u(ds3, 1800)
    assert not r3["problems"], r3["problems"]

    # 4) 最近一次确实超时失败 → 必须 FAIL
    #    真实数据形状: (ts, dur, status, error, deliveryStatus)
    #    超时信息在 **error** 字段；deliveryStatus 对 error 恒为 'unknown'
    ds4 = [
        (0, 300_000, "ok", None, "delivered"),
        (day, 1800_000, "error", "cron: job execution timed out", "unknown"),
    ]
    r4 = judge_u(ds4, 1800)
    assert any("超时失败" in p for p in r4["problems"]), r4["problems"]

    # 4b) 回归:错误文本只在 deliveryStatus 里(error 恒 'unknown')→ 不该误报
    ds4b = [
        (0, 300_000, "ok", None, "delivered"),
        (day, 1800_000, "error", "AxiosError: 500", "unknown"),
    ]
    r4b = judge_u(ds4b, 1800)
    assert not any("超时失败" in p for p in r4b["problems"]), r4b["problems"]

    # 4c) 措辞变体:真实报错是 "execution timed out"(两个词)，
    #     旧判据只认 'timeout'(一个词) → 必须能认出来
    for txt in ("cron: job execution timed out",
                "Error: cron: job execution timed out",
                "job timeout exceeded"):
        r = judge_u([(0, 1800_000, "error", txt, "unknown")], 1800)
        assert any("超时失败" in p for p in r["problems"]), f"没认出来: {txt}"

    # 5) 稀疏样本（间隔 7 天）的「上升」不算趋势
    ds5 = [
        (0, 317_000, "ok", None, "delivered"),
        (7 * day, 822_000, "ok", None, "delivered"),
        (14 * day, 1276_000, "ok", None, "delivered"),
    ]
    r5 = judge_u(ds5, 1800)
    assert not any("递增" in w for w in r5["warns"]), r5["warns"]

    # 6) 密集样本（间隔 1 天）的上升算趋势
    ds6 = [
        (0, 900_000, "ok", None, "delivered"),
        (day, 1000_000, "ok", None, "delivered"),
        (2 * day, 1100_000, "ok", None, "delivered"),
    ]
    r6 = judge_u(ds6, 1800)
    assert any("递增" in w for w in r6["warns"]), r6["warns"]

    # 7) 中位数函数
    assert median([1, 3, 2]) == 2
    assert median([4, 1, 3, 2]) == 2.5
    assert median([]) is None

    # 8) 成功样本不足 → 不能判 FAIL（2 个成功样本时中位数不可信）
    ds8 = [
        (0, 822_349, "ok", None, "delivered"),
        (day, 1_276_180, "ok", None, "delivered"),
    ]
    r8 = judge_u(ds8, 1800)
    assert not r8["problems"], f"样本不足不应 FAIL: {r8['problems']}"
    assert any("样本不足" in w for w in r8["warns"]), r8["warns"]

    # 9) 成功样本足够但中间夹着大量失败 → 基线仍应是「成功样本」的中位数
    ds9 = [
        (0, 822_349, "ok", None, "delivered"),
        (day, 230_083, "error", "rate_limit", "unknown"),
        (2 * day, 245_297, "error", "rate_limit", "unknown"),
        (3 * day, 181_717, "error", "rate_limit", "unknown"),
        (4 * day, 673_000, "ok", None, "delivered"),
        (5 * day, 1_276_180, "ok", None, "delivered"),
    ]
    r9 = judge_u(ds9, 1800)
    # 成功样本 = [822349, 673000, 1276180] → 中位数 822349 → 46% → 不 FAIL
    assert not r9["problems"], r9["problems"]
    assert abs(r9["detail"]["median_ms"] - 822_349) < 1, r9["detail"]

    # 10) 趋势规则**只用成功运行**：失败快 + 一次重活 ≠ 在长
    ds10 = [
        (0, 822_349, "ok", None, "delivered"),
        (day, 181_717, "error", "rate_limit", "unknown"),
        (2 * day, 316_670, "error", "rate_limit", "unknown"),
        (3 * day, 1_276_180, "ok", None, "delivered"),
    ]
    r10 = judge_u(ds10, 1800)
    assert not any("递增" in w for w in r10["warns"]), r10["warns"]

    # 12) 余量告急（2026-11-26 加，E89）：斜率快到「下一次就撞」时要单独说
    # ⚠️ 为什么要单独判：
    #   「在长」是个**观察信号**（可以再观察两周）；
    #   而「余量 < 上次涨幅」是**行动信号** —— 照这个斜率下一次就撞超时，
    #   得现在动手。两个强度混在一句话里，人会当成同一件事。
    ds12 = [
        (0, 575_000, "ok", None, "delivered"),
        (day, 1_151_000, "ok", None, "delivered"),
        (2 * day, 1_700_000, "ok", None, "delivered"),
    ]
    r12 = judge_u(ds12, 1800)
    assert any("余量告急" in w for w in r12["warns"]), r12["warns"]
    assert abs(r12["detail"]["trend_headroom_s"] - 100.0) < 1, r12["detail"]
    assert abs(r12["detail"]["trend_last_growth_s"] - 549.0) < 1, r12["detail"]

    # 13) 反向：余量还够 → **不能**报「余量告急」（防恒真）
    #     11-26 真实序列：215→575→1151，余量 649 > 上次涨幅 576 → 不报
    ds13 = [
        (0, 215_000, "ok", None, "delivered"),
        (day, 575_000, "ok", None, "delivered"),
        (2 * day, 1_151_000, "ok", None, "delivered"),
    ]
    r13 = judge_u(ds13, 1800)
    assert not any("余量告急" in w for w in r13["warns"]), r13["warns"]
    # 但「在长」仍要报（趋势本身是真的）
    assert any("递增" in w for w in r13["warns"]), r13["warns"]

    # 11) 基线高 → 样本充足时必须 FAIL
    ds11 = [
        (i * day, d, "ok", None, "delivered")
        for i, d in enumerate([1400_000, 1350_000, 1500_000, 1300_000, 1450_000])
    ]
    r11 = judge_u(ds11, 1800)
    assert r11["problems"], "基线 70%+ 且样本充足应 FAIL"

    print("【U】判据自检: ✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
