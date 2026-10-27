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
    """统计 healthcheck.sh 里的断言组数。

    ⚠️ 2026-10-27 (E38)：原来这里是 `【[A-L]】` —— 只匹配 A 到 L，
    M/N/O/P/Q/R/S 全部数不到 → 「巡检现状」长期显示 12 组，实际已 19 组。
    这正是本脚本要防的那类「硬编码数字会过期」，却出现在脚本自己身上。
    修法：匹配任意单个大写字母 `【([A-Z])】`，不再枚举范围。
    """
    try:
        s = SELF.read_text(encoding="utf-8", errors="replace")
        return len(set(re.findall(r"【([A-Z])】", s)))
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
    try:
        lines = STATE.read_text(encoding="utf-8").split("\n")
        # 2026-10-27 08:20 修（自增行 bug）：
        # 原来靠「找一行以 **巡检现状**: 开头且含『每日 07:00 自动运行』的」来定位活行，
        # 但脚本自己又会把所有**其它**同名前缀的行改成「（快照）」——
        # 于是活行越来越少 → 某次找不到 → `lines.insert(3, newline)`
        # → **每次巡检都多插一行**，重复累积到 13 行。
        #
        # 根因：**没有稳定的锚点**，活行的身份靠「长得像不像活行」来推断。
        # 修法：用**显式标记**（BEGIN/END 注释块）——就像 gen_system_status.py 那样，
        # 标记永远在、内容永远被替换，不会漂移也不会重复。
        NEWLINE = (
            "**巡检现状**: " + f"{groups} 组断言，每日 07:00 自动运行 + 飞书告警"
            f"｜PASS={p} FAIL={f} WARN={w}"
        )
        begin = "<!-- BEGIN:gen_healthcheck_status -->"
        end = "<!-- END:gen_healthcheck_status -->"

        out = []
        i = 0
        replaced = False
        while i < len(lines):
            ln = lines[i]
            if ln.strip() == begin:
                # 跳过整个旧块，插入新块
                j = i
                while j < len(lines) and lines[j].strip() != end:
                    j += 1
                out.append(begin)
                out.append(NEWLINE)
                out.append(end)
                i = j + 1
                replaced = True
                continue
            out.append(ln)
            i += 1

        if not replaced:
            # 第一次写：插到标题分隔线之后
            insert_at = 0
            for k, ln in enumerate(out):
                if ln.strip() == "---":
                    insert_at = k + 1
                    break
            else:
                insert_at = 3
            out[insert_at:insert_at] = [begin, NEWLINE, end]

        # 清理**遗留**的旧格式行（没有标记的散落行）——只留标记块里那一行
        cleaned = []
        in_block = False
        for ln in out:
            if ln.strip() == begin:
                in_block = True
                cleaned.append(ln); continue
            if ln.strip() == end:
                in_block = False
                cleaned.append(ln); continue
            if in_block:
                cleaned.append(ln); continue
            # 块外的旧格式行：丢掉
            if ln.startswith("**巡检现状"): 
                continue
            cleaned.append(ln)

        STATE.write_text("\n".join(cleaned), encoding="utf-8")
        print(f"[state-status] 已更新：{NEWLINE}")
    except Exception as e:
        print(f"[state-status] 更新失败（忽略）: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
