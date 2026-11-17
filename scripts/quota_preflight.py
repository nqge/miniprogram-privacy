#!/usr/bin/env python3
"""配额预检 —— 在【手动补跑】之前先看额度够不够、会不会撞上下一个计划槽位。

## 为什么需要它（2026-11-03 09:00 的实测教训）

时间线：
  08:51:09  我手动补跑 daily-reflection  → ✅ ok，**吃掉 52,134 tokens**
  09:00:00  daily-learning 计划槽位启动     → 起来了
  09:04:21  第一次 429「已达到 Token Plan 用量上限」
  09:06:08  12 次 429，3 个模型全挂          → ❌ daily-learning 失败

**我自己的补跑，很可能把 9 分钟后那个计划任务的口粮吃掉了。**

这不是「运气不好」，是**顺序错了**：补跑和正事吃同一个账号的额度桶，
谁先谁后不是无关紧要的操作细节，它决定了**谁被挤掉**。

而我当时的判断只有「这个任务 4 个月没跑，要验证它能跑通」——
**没看下一个计划槽位什么时候到**。

## 这个脚本做什么

1. **查额度余量**（不是 1-token 探测）
   → 用**接近真实负载**的请求去探（默认 ~15k token），
     因为「hi」这种小请求通过 ≠ 额度够用（E57 已踩过这个坑）
2. **查下一个计划槽位**
   → 如果下一个 agentTurn 槽位在 N 分钟内 → 明确警告
3. **给出建议**：能跑 / 别跑 / 改用小模型跑

## 用法

    python3 scripts/quota_preflight.py                    # 只看状态
    python3 scripts/quota_preflight.py --job <cron_id>    # 补跑前问「行不行」
    python3 scripts/quota_preflight.py --probe-tokens 5000 # 调探测量

退出码：
    0 = 额度充足且不撞槽位 → 可以跑
    1 = 额度不足 或 会撞槽位 → 先别跑
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

CFG = "/root/.openclaw/openclaw.json"
API = "https://api.minimaxi.com/v1/text/chatcompletion_v2"


def load_key():
    with open(CFG, encoding="utf-8") as f:
        d = json.load(f)
    return d["models"]["providers"]["minimax"]["apiKey"]


QUOTA_MARKERS = ("Token Plan", "用量上限", "insufficient balance", "余额", "额度", "配额",
                 "usage limit", "quota exceeded")


def _is_quota_signal(raw: str) -> bool:
    """判断 429 报文里到底是「额度耗尽」还是「普通限流」。

    ⚠️ 为什么必须分开（2026-11-16 实测）：
    MiniMax 的 429 有两种完全不同的含义 ——
      ① 「已达到 Token Plan 用量上限」→ 额度耗尽，**重试也没用**，等充值
      ② 「rate limit reached, please try again later」→ 普通限流，**等一会就好**
    两者 HTTP 码一样（429），但对「现在该不该跑」给出**相反**的答案。
    把 ② 当成 ① → 白等；把 ① 当成 ② → 白烧额度。
    """
    return any(m in raw for m in QUOTA_MARKERS)


def probe_once(tokens: int = 15000, timeout: int = 30):
    """用接近真实负载的请求探额度。

    ⚠️ 关键：**不能用 1-token 小请求当额度信号**。
    E57 实测：额度耗尽时，小请求照样 HTTP 200，
    只有**大上下文**才撞 429 → 小请求探测是**反向指标**。
    """
    key = load_key()
    # 造出大致 tokens 个字的中文内容（约 1.5 字/token 的粗略比例）
    filler = "配额预检测试内容。" * max(1, tokens // 5)
    body = json.dumps({
        "model": "MiniMax-M3",
        "messages": [{"role": "user", "content": filler + "\n只回复OK"}],
        "max_completion_tokens": 5,
    }, ensure_ascii=False).encode()

    req = urllib.request.Request(
        API, data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            dt = time.time() - t0
            d = json.loads(raw)
            code = (d.get("base_resp") or {}).get("status_code")
            msg = (d.get("base_resp") or {}).get("status_msg", "")
            if code == 0:
                return True, f"额度可用（~{tokens} token 探测通过，{dt:.2f}s）", dt
            return False, f"额度不足：{code} {msg}", dt
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        dt = time.time() - t0
        if e.code == 429:
            # ⚠️ 429 还要看报文：只有带「额度」特征的才是额度耗尽，
            #    纯限流（rate_limit_error，无 Token Plan 字样）也是 429。
            if _is_quota_signal(raw):
                return False, f"额度不足：HTTP 429 {raw[:120]}", dt
            return False, f"探测失败：HTTP 429（普通限流，非额度耗尽）{raw[:120]}", dt
        if e.code == 529:
            # ⚠️ 529 = 上游整点高峰过载（overloaded_error），**不是额度问题**
            #    上游自己说「通常 1-5 分钟内恢复」→ 重试，别当额度耗尽。
            return False, f"上游繁忙：HTTP 529（overloaded，瞬时可恢复，非额度问题）{raw[:120]}", dt
        if e.code == 402:
            return False, f"额度不足：HTTP 402 {raw[:120]}", dt
        return False, f"探测失败：HTTP {e.code} {raw[:120]}", dt
    except Exception as e:
        return False, f"探测失败：{type(e).__name__} {e}", 0.0


def probe(tokens: int = 15000, retries: int = 3, backoff: int = 25,
          degrade_ladder=None, probe_timeout: int = 30, budget_s: float = 150):
    """探测额度 —— 对**瞬时**失败（529 过载）自动重试。

    ⚠️ 2026-11-17 加（实测触发）：**上游过载是「按负载排队」，不是「有没有额度」**。
    同一天实测的探针阶梯：
      15000 ❌ → 8000 ❌ → 3000 ✅ → 2000 ❌（注意：不是单调的）
    而 `max_tokens=5` 的小请求 → **HTTP 200 / 1.05s**。

    → 结论：**「大探针过不去」≠「这活干不了」**。
    原来只有「同尺寸重试」，同尺寸在过载时基本是死路 ——
    所以加**降档阶梯**（--degrade-ladder）：同尺寸重试用尽后，
    依次用更小的尺寸再探一次，**只要有一档通过就放行**。

    ⚠️ 但**降档探针通过 ≠ 额度够** —— 小探针只证明「这条路现在通」，
    不证明「跑 15000 token 的活不会中途被限流掐死」。
    → 因此降档只在 **529（上游过载）** 时启用；
      **429/402（额度）绝不降档** —— 额度不足时换小探针也还是不够，
      降了只会**给出「可以跑」的假结论**，那才是真正的危害。


    ⚠️ 2026-11-16 加：整点高峰上游会返回 HTTP 529（overloaded_error，
    上游自己说「通常 1-5 分钟内恢复」）。**529 不是额度问题** ——
    原实现把它和其他错误一起判成「额度不足，先别跑」，结论一样但**理由错了**，
    而更糟的是：整点（09:00）恰恰是 daily-learning 的计划槽位 ——
    **最需要额度的时候，最可能撞上最容易被误判的错误**。

    重试策略：
      · 529（过载）→ 退避后重试，最多 retries 次
      · 429/402（额度）→ **不重试**（重试也不会有额度，等充值）
      · 其他错误 → 不重试

    ⚠️ 2026-11-17 加（**总时间预算**）：预检是**被闸门调用的子进程**，
    闸门给它的 timeout 是 **180s**；而旧参数（单次探测 90s × 3 次 + 25s/50s 退避）
    最坏要 **345s** → **必然超支** → 闸门走 `subprocess.TimeoutExpired`
    → 「预检超时 → 放行」= **fail-open**。
    → fail-open 本身是对的（不确定 ≠ 额度不足，不该因为预检慢就永远不干活），
      但**不该是常态**。
    → 现在：单次探测 timeout 降到 **30s**（实测正常 1-2s，30s 足够），
      并且用 `budget_s` 给**整个 probe()** 封顶 —— 超预算就停止重试，
      按现有证据给结论。→ 最坏耗时 ≈ 30×6 + 退避 ≈ 230s… 仍偏大，
      所以 budget 默认取 150s，**保证落在闸门的 180s 之内**。
    """
    t_start = time.time()

    def _over_budget() -> bool:
        return (time.time() - t_start) > budget_s

    last = (False, "未探测", 0.0)
    for attempt in range(1, retries + 1):
        if _over_budget():
            print(f"  ⏱ 预检已用 {time.time() - t_start:.0f}s（预算 {budget_s:.0f}s）→ 停止重试")
            break
        ok, msg, dt = probe_once(tokens, timeout=probe_timeout)
        last = (ok, msg, dt)
        transient = "上游繁忙：HTTP 529" in msg
        if ok or not transient or attempt == retries:
            break
        wait = backoff * attempt
        print(f"  ⚠️  {msg[:80]}")
        print(f"  ↻ 上游瞬时过载，{wait}s 后重试（第 {attempt}/{retries} 次）…")
        time.sleep(wait)

    # ── 同尺寸重试用尽，且确实是「上游过载」→ 走**降档阶梯** ──
    # 只有 529 走到这里；429/402（额度）早已在上面 break 掉了。
    if not last[0] and "上游繁忙：HTTP 529" in last[1] and degrade_ladder:
        for smaller in degrade_ladder:
            if smaller >= tokens:
                continue
            if _over_budget():
                print(f"  ⏱ 预检已用 {time.time() - t_start:.0f}s（预算 {budget_s:.0f}s）→ 不再降档")
                break
            print(f"  ↻ 仍是上游过载 → **降档**再探：{tokens} → {smaller} token")
            print(f"    （大探针过不去 ≠ 这活干不了；降档只证明「这条路现在通」）")
            ok, msg, dt = probe_once(smaller, timeout=probe_timeout)
            if ok:
                print(f"  ✅ 降档探测通过（~{smaller} token，{dt:.2f}s）→ 通路恢复")
                return True, f"额度可用（降档 ~{smaller} token 探测通过，上游过载中）", dt
            # 降档探针**也**报额度类错误 → 说明不是过载，是真没额度 → 立刻停
            if "HTTP 429" in msg or "HTTP 402" in msg:
                return False, msg, dt
            print(f"  ⚠️  降档探测也失败：{msg[:80]}")
            time.sleep(backoff)
    return last


def next_slots(minutes_ahead: int = 120):
    """列出最近 minutes_ahead 分钟内的 agentTurn 计划槽位。"""
    try:
        r = subprocess.run(
            ["openclaw", "cron", "list", "--json"],
            capture_output=True, text=True, timeout=90,
            env={**__import__("os").environ, "OPENCLAW_LOG_LEVEL": "warn"},
        )
        d = json.loads(r.stdout[r.stdout.find("{"):])
    except Exception as e:
        return [], f"读不到 cron 列表({type(e).__name__})"

    jobs = d if isinstance(d, list) else d.get("jobs", d.get("data", []))
    now = time.time() * 1000
    horizon = now + minutes_ahead * 60 * 1000
    out = []
    for j in jobs:
        st = j.get("state") or {}
        nxt = st.get("nextRunAtMs")
        if not nxt or not (now <= nxt <= horizon):
            continue
        out.append({
            "name": j.get("name"),
            "at": nxt,
            "in_min": round((nxt - now) / 60000, 1),
            "timeout": (j.get("payload") or {}).get("timeoutSeconds"),
        })
    out.sort(key=lambda x: x["at"])
    return out, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", help="要补跑的 cron id（可选）")
    ap.add_argument("--probe-tokens", type=int, default=15000,
                    help="探测用的近似 token 量（默认 15000）")
    ap.add_argument("--no-retry", action="store_true",
                    help="不重试 529 过载（调试用）")
    ap.add_argument("--degrade-ladder", default="8000,3000,1000",
                    help="529 过载时的**降档探针阶梯**（逗号分隔，默认 8000,3000,1000）。"
                         "传空字符串则关闭降档。")
    ap.add_argument("--probe-timeout", type=int, default=30,
                    help="单次探测的网络超时秒数（默认 30；实测正常 1-2s）")
    ap.add_argument("--budget", type=float, default=150,
                    help="**整个预检的时间预算**秒数（默认 150）。"
                         "必须小于调用方给的 timeout（闸门给 180s），"
                         "否则会被判「预检超时 → 放行」（fail-open）。")
    args = ap.parse_args()

    print("=" * 60)
    print(" 配额预检")
    print("=" * 60)

    # ① 下一个计划槽位
    slots, err = next_slots(120)
    if err:
        print(f" ⚠️ {err}")
        slots = []
    if slots:
        print("\n【接下来的计划槽位（2 小时内）】")
        for s in slots:
            print(f"  · {s['name']:20s} {s['in_min']:>6.1f} 分钟后"
                  f"（timeout {s['timeout']}s）")
    else:
        print("\n【接下来的计划槽位】2 小时内无 agentTurn 任务 ✅")

    # ② 额度探测
    ladder = [int(x) for x in (args.degrade_ladder or "").split(",") if x.strip().isdigit()]
    print(f"\n【额度探测】（~{args.probe_tokens} token 真实负载，不用小请求）")
    ok, msg, dt = probe(args.probe_tokens, retries=1 if args.no_retry else 3,
                        degrade_ladder=ladder,
                        probe_timeout=args.probe_timeout, budget_s=args.budget)
    print(f"  {'✅' if ok else '❌'} {msg}")

    # ③ 结论
    print()
    imminent = [s for s in slots if s["in_min"] <= 60]
    blocked = False

    if not ok:
        blocked = True
        # ⚠️ 三种失败模式给三种建议（2026-11-16 加）
        if "上游繁忙：HTTP 529" in msg:
            print(" ❌ 结论：**上游瞬时过载**（不是额度问题）—— 重试后仍不通")
            print("    建议：等 1-5 分钟上游恢复再跑；这不是充值能解决的问题")
        elif "额度不足" in msg:
            print(" ❌ 结论：**先别跑** —— 额度不够，跑也是白烧（还会白占几分钟）")
            print("    建议：等额度恢复，或改用更省的任务 / 手动写笔记")
        elif "普通限流" in msg:
            print(" ❌ 结论：**先别跑** —— 上游普通限流（非额度耗尽），等一会儿再试")
            print("    建议：等 1-2 分钟重试；额度是好的，不用充值")
        else:
            print(" ❌ 结论：**先别跑** —— 额度不够，跑也是白烧（还会白占几分钟）")
            print("    建议：等额度恢复，或改用更省的任务 / 手动写笔记")
    elif imminent:
        blocked = True
        names = ", ".join(f"{s['name']}({s['in_min']:.0f}分钟后)" for s in imminent)
        print(f" ❌ 结论：**先别跑** —— 1 小时内有计划槽位：{names}")
        print("    补跑和正事吃同一个额度桶，补跑可能把正事的口粮吃掉")
        print(f"    建议：等计划槽位跑完再补跑，或把补跑挪到 {max(s['in_min'] for s in imminent):.0f} 分钟之后")
    else:
        if "降档" in msg:
            print(" ✅ 结论：**可以跑** —— 上游过载中，但**降档探测已通过**（通路恢复，额度未被证明不足）")
            print("    建议：可以跑；若中途被限流掐死，等上游恢复后重跑即可（**不是额度问题，不用充值**）")
        else:
            print(" ✅ 结论：**可以跑** —— 额度够，且 1 小时内无计划槽位")

    print("=" * 60)
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
