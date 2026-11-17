#!/usr/bin/env python3
"""Provider 凭证健康检查 —— 主动探测，而非等它静默失效。

为什么需要它（2026-10-26 的直接教训）：
  今天上午有三个 provider（glmcode / arkcode / kimicode）**已经失效**，
  但系统状态一直显示正常 —— 因为「配置里还有这个 provider」≠「它还能用」。
  失效的表现是：调用时才报错，而那时可能已经连续失败很多次了。

检测两件事：
  1. **每个 provider 是否还活着** —— 发一个最小请求（max_tokens=1），看 HTTP 状态
  2. **是否还有可用的 provider** —— 这是最关键的：
     如果一个都不剩，整套系统就没有推理能力了，
     但状态字段依然会显示一切正常（这正是今天踩的坑）

成本：每个 provider 一次 1-token 请求。频率：每 30 分钟一次（跟 quick-health 一起）。

注意：探测本身会消耗极少量配额，但对**已经失效**的 provider 调用会返回 401/402，
这正是我们要的信号。
"""

import json
import sys
import urllib.error
import urllib.request

CFG = "/root/.openclaw/openclaw.json"


def probe(name, p):
    """发一个最小请求探测 provider 是否还活着。"""
    base = (p.get("baseUrl") or "").rstrip("/")
    key = p.get("apiKey")
    models = p.get("models") or []
    if not base or not key or not models:
        return "skip", "配置不全（缺 baseUrl/apiKey/models）"

    model = models[0].get("id")
    api = p.get("api", "anthropic-messages")

    if api == "anthropic-messages":
        url = base + "/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        }
        body = {
            "model": model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
        }
    else:  # openai-completions
        url = base + "/v1/chat/completions"
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
        body = {
            "model": model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
        }

    try:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), headers=headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status == 200:
                return "ok", f"可用（{model}，HTTP 200）"
            return "bad", f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        code = e.code
        detail = ""
        try:
            detail = e.read().decode()[:160].replace("\n", " ")
        except Exception:
            pass
        if code in (401, 403):
            return "bad", f"**凭证无效/被拒**（HTTP {code}）{detail}"
        if code == 402:
            return "bad", f"**配额耗尽**（HTTP 402）{detail}"
        if code == 429:
            return "warn", f"限流（HTTP 429）{detail}"
        # ⚠️ 2026-11-17 加（HTTP 529 = 上游瞬时过载，**不是**故障也不是额度问题）：
        #   实测 16:07 quick-health 报「**没有任何可用 provider** —— 整个系统无法推理」
        #   → 被下游归进「**可处理 - 现在就该修**」→ **假警报**：
        #   上游过载**谁都不用做什么**（等 1-5 分钟就好，实测直连 1.5s 就恢复了）。
        #   → 单独识别 529，措辞带上「**无需处理**」，
        #     让下游的 is_waiting_on_user() 认得出来（与闸门【G2】同一套做法）。
        if code == 529:
            return "warn", f"上游瞬时过载（HTTP 529 overloaded，**无需处理**，等 1-5 分钟自愈）{detail}"
        if code >= 500:
            return "warn", f"服务端错误（HTTP {code}）{detail}"
        return "bad", f"HTTP {code} {detail}"
    except Exception as e:
        return "bad", f"请求异常：{type(e).__name__}: {str(e)[:120]}"


def main():
    print("【Provider 凭证】")
    try:
        cfg = json.load(open(CFG, encoding="utf-8"))
    except Exception as e:
        print(f"  ❌ FAIL  读不到配置：{type(e).__name__}")
        return 1

    providers = (cfg.get("models", {}) or {}).get("providers", {}) or {}
    if not providers:
        print("  ❌ FAIL  配置里没有任何 provider —— 系统没有推理能力")
        return 1

    alive, dead, warned, skipped = [], [], [], []
    quota_markers = ("Token Plan", "用量上限", "insufficient balance", "余额", "额度")
    quota_hit = False  # 是否撞上「额度耗尽」特征（只看**报错明细**，不看 provider 名）
    overload_hit = False  # 是否撞上「上游瞬时过载」（HTTP 529）—— 与额度**完全不同的处置**
    for name, p in providers.items():
        status, detail = probe(name, p)
        if status == "ok":
            print(f"  ✅ PASS  {name} {detail}")
            alive.append(name)
        elif status == "warn":
            print(f"  ⚠️  WARN  {name} {detail}")
            warned.append(name)
            if any(mk in detail for mk in quota_markers):
                quota_hit = True
            if "529" in detail or "overload" in detail.lower():
                overload_hit = True
        elif status == "skip":
            print(f"  ⏭  SKIP  {name} {detail}")
            skipped.append(name)
        else:
            print(f"  ❌ FAIL  {name} {detail}")
            dead.append(name)
            if any(mk in detail for mk in quota_markers):
                quota_hit = True

    # 最关键的判断：还有能用的吗？
    if not alive:
        # ⚠️ 2026-11-10：区分「真的全死了」和「只是额度耗尽」。
        #   额度耗尽时（HTTP 429 + Token Plan 上限）系统**暂时**不能推理，
        #   但根因是牛哥哥要充值，不是配置坏了 —— 措辞必须带上，否则下游
        #   会把它当成「可处理 - 现在就该修」的假警报（E66/E69 同源问题）。
        #
        # ⚠️ 同日修正：条件必须看**报错明细**，不能看 provider 名字 ——
        #   `warned` 里存的是 name（"minimax"），永远不含 "Token Plan"
        #   → 分支永远不触发（19:06 实测漏判一条）。
        if quota_hit:
            print("  ❌ FAIL  **没有任何可用 provider** —— 整个系统暂时无法推理（额度耗尽，需充值）")
        elif overload_hit:
            # ⚠️ 2026-11-17：上游 529 过载 → **谁都不用做什么**（等 1-5 分钟自愈）。
            #   措辞必须带「**无需处理**」，否则下游归「可处理 - 现在就该修」= 长期性假警报
            #   （与闸门【G2】的 upstream_busy 同一套处置逻辑）。
            print("  ❌ FAIL  **没有任何可用 provider** —— 暂时无法推理"
                  "（**上游瞬时过载**，HTTP 529 · **无需处理**，等 1-5 分钟自愈）")
        else:
            print("  ❌ FAIL  **没有任何可用 provider** —— 整个系统无法推理")
        print("           （今天上午三个 provider 全失效时，状态字段依然显示正常）")
        return 1
    if dead:
        print(f"  ⚠️  WARN  {len(dead)} 个 provider 已失效但配置里还在：{', '.join(dead)}")
        print("           建议删除 —— 留着会在调用时反复报错")
    return 0


if __name__ == "__main__":
    sys.exit(main())
