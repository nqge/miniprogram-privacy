#!/usr/bin/env python3
"""cron 投递状态检查 —— 由 scripts/healthcheck.sh 调用。

输出每行一个断言结果，格式：
  PASS <name> ...
  FAIL <name> ...
"""

import json
import subprocess
import sys


def main() -> int:
    try:
        r = subprocess.run(
            ["openclaw", "cron", "list", "--json"],
            capture_output=True, text=True, timeout=90,
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

    # 供 shell 统计失败数
    print(f"__FAILED__={failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
