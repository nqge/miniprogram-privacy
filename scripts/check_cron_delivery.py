#!/usr/bin/env python3
"""cron 投递状态检查 —— 由 scripts/healthcheck.sh 调用。

输出每行一个断言结果，格式：
  PASS <name> ...
  FAIL <name> ...
"""

import json
import os
import subprocess
import sys
import datetime


def _load_history(env, job_id, limit=20):
    """拉取某个 job 的历史运行记录（用于检查历史投递失败）。

    `cron list --json` 只给「最近一次」的状态，看不到历史。
    历史里藏着「ok 但没投出去」的静默失败 —— 断言只看 lastRun 就会漏。
    """
    try:
        r = subprocess.run(
            ["openclaw", "cron", "runs", "--id", job_id, "--limit", str(limit)],
            capture_output=True, text=True, timeout=90, env=env,
        )
        d = json.loads(r.stdout[r.stdout.find("{"):])
        return d.get("runs", d.get("entries", []))
    except Exception:
        return None  # 取不到不算失败，但不能当成「没问题」


def main() -> int:
    # 日志降噪：每次 CLI 调用会重新注册全部插件，INFO 级打 6+ 行
    # 「feishu_xxx: Registered ...」，占当日日志 87%。
    # 实测 27008 字节/次 → 5735 字节/次（-79%）。
    # 注意：不能改 openclaw.json 里的 logging 段（那会触发 loadConfig 递归），
    # 只能用环境变量，且只影响本子进程。
    env = dict(os.environ)
    env.setdefault("OPENCLAW_LOG_LEVEL", "warn")
    try:
        r = subprocess.run(
            ["openclaw", "cron", "list", "--json"],
            capture_output=True, text=True, timeout=90, env=env,
        )
        d = json.loads(r.stdout[r.stdout.find("{"):])
    except Exception as e:
        print(f"FAIL cron 状态读取 — {type(e).__name__}")
        return 1

    jobs = d if isinstance(d, list) else d.get("jobs", d.get("data", []))
    failed = 0

    for j in jobs:
        name = j.get("name", "?")
        dl = j.get("delivery", {}) or {}
        st = j.get("state", {}) or {}
        mode = dl.get("mode")
        run_status = st.get("lastRunStatus")

        # 从未运行过的新任务不算失败（没有历史 ≠ 出故障）
        if run_status is None and not st.get("lastRunAtMs"):
            print(f"WARN {name} 尚未运行过（新建任务，等待首次执行）")
            continue

        if run_status != "ok":
            print(f"FAIL {name} 运行失败（lastRunStatus={run_status}）")
            failed += 1
        elif mode == "announce":
            if st.get("lastDelivered"):
                print(f"PASS {name} 投递正常（delivered=True）")
            else:
                print(
                    f"FAIL {name} 投递未送达！"
                    f"（delivery={st.get('lastDeliveryStatus')}, to={dl.get('to', '-')}）"
                )
                failed += 1
        else:
            print(f"PASS {name} 运行正常（no-deliver）")

        # ---- 历史投递检查 ----
        # lastRunStatus 只反映「最近一次」，历史上「ok 但没投出去」的静默失败会被漏掉。
        # 实测（2026-10-27）：daily-reflection 有 2 次 ok-but-not-delivered
        # （10-26 00:35、10-21 04:27），另有 40 次历史 error —— lastRun 看起来全绿。
        job_id = j.get("id") or j.get("jobId")
        if mode == "announce" and job_id:
            runs = _load_history(env, job_id)
            if runs is None:
                print(f"WARN {name} 历史投递状态读取失败（无法判断，非失败）")
                continue
            undelivered = [
                r for r in runs
                if r.get("status") == "ok" and r.get("delivered") is False
            ]
            if undelivered:
                ts = []
                for r in undelivered[:5]:
                    ms = r.get("runAtMs")
                    ts.append(
                        datetime.datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M")
                        if isinstance(ms, (int, float)) else "?"
                    )
                # 最近一次仍是坏的 → FAIL；历史遗留但最近已好 → WARN
                if runs and runs[0].get("status") == "ok" and runs[0].get("delivered") is False:
                    print(
                        f"FAIL {name} 最近一次运行未投递！"
                        f"（历史 {len(undelivered)}/{len(runs)} 次未投递）"
                    )
                    failed += 1
                else:
                    print(
                        f"WARN {name} 历史上有 {len(undelivered)}/{len(runs)} 次"
                        f"「运行成功但未投递」（最近一次正常）：{', '.join(ts)}"
                    )
            else:
                recent_errors = sum(1 for r in runs if r.get("status") == "error")
                if recent_errors:
                    print(
                        f"WARN {name} 最近 {len(runs)} 次里有 {recent_errors} 次 error"
                        f"（最近一次正常）"
                    )
                else:
                    print(f"PASS {name} 历史投递均正常（近 {len(runs)} 次）")

    # 供 shell 统计失败数
    print(f"__FAILED__={failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
