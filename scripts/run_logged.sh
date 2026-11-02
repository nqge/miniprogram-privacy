#!/usr/bin/env bash
# 带日志包装器 —— 给 systemd oneshot 用
# 用法: run_logged.sh <日志名前缀> <要执行的命令...>
# 做的事: 建日志目录 → 写带时间戳的日志 → 记退出码 → 清理 30 天前的旧日志
#
# 为什么单独一个脚本（而不是把整串命令写进 unit 文件）：
#   systemd 会把 ExecStart 里的 `%` 当成**自己的 specifier**（%F → "Invalid slot" 报错），
#   所以 `date +%F` 这类命令不能直接写在 unit 里（2026-11-02 实测踩到）。
#   逻辑放脚本里，unit 只留一条干净的调用。
set -uo pipefail
WS=/root/.openclaw/workspace
LOG_DIR="$WS/memory/healthcheck-logs"
name="${1:-run}"; shift || true
mkdir -p "$LOG_DIR"
f="$LOG_DIR/${name}-$(date +%F-%H%M%S).txt"
{
  echo "===== ${name} $(date '+%F %T') ====="
  if [ "$#" -gt 0 ]; then
    "$@"
    echo "EXIT=$?"
  else
    echo "no command given"
    echo "EXIT=2"
  fi
} > "$f" 2>&1
find "$LOG_DIR" -name "${name}-*.txt" -mtime +30 -delete 2>/dev/null
exit 0
