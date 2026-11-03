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


def probe(tokens: int = 15000):
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
        with urllib.request.urlopen(req, timeout=90) as r:
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
            return False, f"额度不足：HTTP 429 {raw[:120]}", dt
        return False, f"探测失败：HTTP {e.code} {raw[:120]}", dt
    except Exception as e:
        return False, f"探测失败：{type(e).__name__} {e}", 0.0


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
    print(f"\n【额度探测】（~{args.probe_tokens} token 真实负载，不用小请求）")
    ok, msg, dt = probe(args.probe_tokens)
    print(f"  {'✅' if ok else '❌'} {msg}")

    # ③ 结论
    print()
    imminent = [s for s in slots if s["in_min"] <= 60]
    blocked = False

    if not ok:
        blocked = True
        print(" ❌ 结论：**先别跑** —— 额度不够，跑也是白烧（还会白占几分钟）")
        print("    建议：等额度恢复，或改用更省的任务 / 手动写笔记")
    elif imminent:
        blocked = True
        names = ", ".join(f"{s['name']}({s['in_min']:.0f}分钟后)" for s in imminent)
        print(f" ❌ 结论：**先别跑** —— 1 小时内有计划槽位：{names}")
        print("    补跑和正事吃同一个额度桶，补跑可能把正事的口粮吃掉")
        print(f"    建议：等计划槽位跑完再补跑，或把补跑挪到 {max(s['in_min'] for s in imminent):.0f} 分钟之后")
    else:
        print(" ✅ 结论：**可以跑** —— 额度够，且 1 小时内无计划槽位")

    print("=" * 60)
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
