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
    for name, p in providers.items():
        status, detail = probe(name, p)
        if status == "ok":
            print(f"  ✅ PASS  {name} {detail}")
            alive.append(name)
        elif status == "warn":
            print(f"  ⚠️  WARN  {name} {detail}")
            warned.append(name)
        elif status == "skip":
            print(f"  ⏭  SKIP  {name} {detail}")
            skipped.append(name)
        else:
            print(f"  ❌ FAIL  {name} {detail}")
            dead.append(name)

    # 最关键的判断：还有能用的吗？
    if not alive:
        print("  ❌ FAIL  **没有任何可用 provider** —— 整个系统无法推理")
        print("           （今天上午三个 provider 全失效时，状态字段依然显示正常）")
        return 1
    if dead:
        print(f"  ⚠️  WARN  {len(dead)} 个 provider 已失效但配置里还在：{', '.join(dead)}")
        print("           建议删除 —— 留着会在调用时反复报错")
    return 0


if __name__ == "__main__":
    sys.exit(main())
