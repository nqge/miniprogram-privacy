#!/usr/bin/env python3
"""更新 SESSION-STATE.md 的「巡检现状」行 —— 由 healthcheck.sh 调用。

为什么要自动维护：
  2026-10-26 发现 SESSION-STATE.md 头部描述过期 5 天（说三个 provider 还待决策，
  实际当天上午已删除）。硬编码的数字会再次变陈旧 —— 改成每次巡检自动写入。

用法：python3 scripts/update_state_status.py <PASS> <FAIL> <WARN>
"""

import re
import sys
from pathlib import Path

STATE = Path("/root/.openclaw/workspace/SESSION-STATE.md")
SELF = Path("/root/.openclaw/workspace/scripts/healthcheck.sh")


def count_groups() -> int:
    try:
        s = SELF.read_text(encoding="utf-8", errors="replace")
        return len(set(re.findall(r"【[A-L]】", s)))
    except Exception:
        return 0


def main() -> int:
    if len(sys.argv) < 4:
        print("用法: update_state_status.py <PASS> <FAIL> <WARN>", file=sys.stderr)
        return 1
    p, f, w = sys.argv[1], sys.argv[2], sys.argv[3]
    if not STATE.exists():
        return 0
    groups = count_groups()
    newline = (
        f"**巡检现状**: {groups} 组断言，每日 07:00 自动运行 + 飞书告警"
        f"｜PASS={p} FAIL={f} WARN={w}"
    )
    try:
        lines = STATE.read_text(encoding="utf-8").split("\n")
        done = False
        for i, ln in enumerate(lines):
            if ln.startswith("**巡检现状**:"):
                lines[i] = newline
                done = True
                break
        if not done:
            lines.insert(3, newline)
        STATE.write_text("\n".join(lines), encoding="utf-8")
        print(f"[state-status] 已更新：{newline}")
    except Exception as e:
        print(f"[state-status] 更新失败（忽略）: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
