#!/usr/bin/env python3
"""告警去重 / 抑制 —— 避免持续性故障造成飞书告警轰炸。

为什么需要它（2026-10-26 实测设计缺陷）：
  quick-health 每 30 分钟跑一次。如果某个故障**持续存在**（比如 apiKey 真的过期了，
  或者磁盘一直满），你会每 30 分钟收到一条**内容完全相同**的飞书告警。
  一天 48 条一样的消息 —— 这不是告警，这是噪音。
  噪音的真实代价：真正的新问题被淹没，然后你开始忽略告警。

设计：
  - 用**故障指纹**（失败项集合的哈希）去重
  - 同一指纹 **6 小时内只发一次**，但会更新「已抑制 N 次」计数
  - 故障**恢复时**发一条「已恢复」通知（这个必须发，否则你不知道问题好了）
  - 指纹变化（故障内容变了）→ 立即发送

状态文件：/root/.openclaw/workspace/.alert-state.json
"""

import datetime
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

STATE = Path("/root/.openclaw/workspace/.alert-state.json")
SUPPRESS_SECONDS = 6 * 3600  # 同一故障 6 小时内只告警一次
# 结果文件超过这个时长就当作「陈旧」，不信任它（E26：崩溃在写文件前 → 读到上次的成功结果 → 告警被静默吞掉）
STALE_AFTER_SECONDS = 15 * 60
ALERT = "/root/.openclaw/workspace/scripts/quick_health_alert.py"


RESULT_FILE = Path("/root/.openclaw/workspace/logs/quick-health-result.json")
RUNNING_FILE = Path("/root/.openclaw/workspace/logs/quick-health-running.json")


def current_failures():
    """读取 quick-health 写下的结果文件。

    为什么不再自己跑一遍：原来 dedup 会重跑 quick_health.py，
    导致每次成功巡检实际跑两遍（8 秒 → 16 秒），
    而且两次结果可能不一致（竞态会让状态错乱）。

    退化路径：结果文件不存在/陈旧 → 自己跑一次。

    ⚠️ 新鲜度校验（2026-10-26 加，E26）：
    结果文件可能是「上一次成功运行」留下的 —— 当 quick_health.py
    在写文件「之前」就崩了/被 kill/超时，文件根本没被更新。
    这时直接读它会得到「无故障」，**告警被静默吞掉，
    恰恰发生在系统最坏的时候**（实测已复现）。
    """
    # ⚠️ 先看「运行中」标记（E26）：如果它还在，说明上次巡检没跑完
    #（崩溃/被 kill/超时）—— 这本身就是一种失败，必须告警，
    # 不能因为结果文件里留着上次的成功态就当没事发生。
    if RUNNING_FILE.exists():
        try:
            r = json.loads(RUNNING_FILE.read_text(encoding="utf-8"))
            age = time.time() - RUNNING_FILE.stat().st_mtime
            if 0 <= age <= STALE_AFTER_SECONDS:
                print("[alert-dedup] 检测到未完成的巡检运行（running 标记仍在）")
                return [
                    f"巡检脚本未完成运行（可能崩溃/超时/被 kill）"
                    f" — startedAt={r.get('startedAt')} pid={r.get('pid')}"
                ], 1
            else:
                print(f"[alert-dedup] running 标记已过期（{int(age)}s 前），忽略")
        except Exception as e:
            print(f"[alert-dedup] running 标记读取失败（{type(e).__name__}）")

    try:
        data = json.loads(RESULT_FILE.read_text(encoding="utf-8"))
        started = data.get("startedAt")
        age_ok = True
        if started:
            try:
                ts = datetime.datetime.strptime(started, "%Y-%m-%d %H:%M:%S")
                age = (datetime.datetime.now() - ts).total_seconds()
                age_ok = 0 <= age <= STALE_AFTER_SECONDS
            except Exception:
                age_ok = False
        else:
            try:
                age = time.time() - RESULT_FILE.stat().st_mtime
                age_ok = 0 <= age <= STALE_AFTER_SECONDS
            except Exception:
                age_ok = False
        if age_ok:
            return list(data.get("failures") or []), 1 if data.get("failed") else 0
        print(f"[alert-dedup] 结果文件已陈旧（startedAt={started}），改为自行重跑")
    except Exception as e:
        print(f"[alert-dedup] 读取结果文件失败（{type(e).__name__}），改为自行重跑")
    r = subprocess.run(
        ["/usr/bin/python3", "/root/.openclaw/workspace/scripts/quick_health.py"],
        capture_output=True, text=True, timeout=180,
    )
    fails = [ln.strip() for ln in (r.stdout or "").split("\n") if "❌" in ln]
    return fails, r.returncode


def fingerprint(fails):
    """失败项集合的稳定指纹（忽略顺序与空白）。"""
    norm = sorted(set(" ".join(f.split()) for f in fails))
    return hashlib.sha256("\n".join(norm).encode()).hexdigest()[:16], norm


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(st):
    try:
        STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[alert-dedup] 写状态失败: {e}")


def send(text):
    r = subprocess.run(
        ["/usr/bin/python3", ALERT, text], capture_output=True, text=True, timeout=90
    )
    print(f"[alert-dedup] 发送: {(r.stdout or '').strip()}")


def main():
    fails, rc = current_failures()
    st = load_state()
    prev_fp = st.get("fingerprint")
    prev_fails = st.get("failures", [])
    last_sent = st.get("lastSentAt", 0)
    suppressed = st.get("suppressedCount", 0)

    if not fails:
        # 恢复（或从未失败过）
        if prev_fp and prev_fails:
            send(
                f"✅ 快速健康检查已恢复\n"
                f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"此前故障（{len(prev_fails)} 项）：\n"
                + "\n".join(f"  · {f}" for f in prev_fails[:8])
                + f"\n\n（期间同一故障已抑制 {suppressed} 次重复告警）"
            )
        save_state({"fingerprint": None, "failures": [], "lastSentAt": 0, "suppressedCount": 0})
        print("[alert-dedup] 无故障")
        return 0

    fp, norm = fingerprint(fails)
    summary = "; ".join(norm[:8])
    now = time.time()

    if fp == prev_fp and (now - last_sent) < SUPPRESS_SECONDS:
        # 同一故障且在抑制窗口内 → 不发，只计数
        suppressed += 1
        save_state(
            {
                "fingerprint": fp,
                "failures": norm,
                "lastSentAt": last_sent,
                "suppressedCount": suppressed,
            }
        )
        print(f"[alert-dedup] 已抑制（同一故障 {int((now-last_sent)/60)} 分钟前已告警，累计 {suppressed} 次）")
        return 0

    # 新故障，或抑制窗口已过 → 发送
    extra = ""
    if fp == prev_fp and suppressed:
        extra = f"\n（此前同一故障已抑制 {suppressed} 次重复告警）"
    send(
        f"🔴 快速健康检查失败\n"
        f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"问题（{len(norm)} 项）：\n"
        + "\n".join(f"  · {f}" for f in norm[:8])
        + f"{extra}\n\n"
        f"完整巡检：bash scripts/healthcheck.sh\n"
        f"日志：journalctl --user -u openclaw-quick-health.service -n 30"
    )
    save_state(
        {
            "fingerprint": fp,
            "failures": norm,
            "lastSentAt": now,
            "suppressedCount": 0,
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
