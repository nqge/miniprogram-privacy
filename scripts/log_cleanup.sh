#!/usr/bin/env bash
# OpenClaw 日志清理 — 防止 /tmp/openclaw 日志无限增长
#
# 背景：2026-10-01 曾出现 24M/天 的日志递归（stale config 警告被每次 CLI 调用放大）。
#       2026-10-26 修掉警告源头后，仍需兜底轮转，防止复发。
#
# 策略：保留 7 天，更早的压缩并删除；单文件超 50M 直接截断当前文件。

set -uo pipefail
LOG_DIR="${LOG_DIR:-/tmp/openclaw}"
KEEP_DAYS="${KEEP_DAYS:-7}"
MAX_SIZE_MB="${MAX_SIZE_MB:-50}"

[ -d "$LOG_DIR" ] || exit 0

# 1) 删除超过保留期的日志
DELETED=$(find "$LOG_DIR" -name 'openclaw-*.log' -type f -mtime "+${KEEP_DAYS}" -print -delete 2>/dev/null | wc -l)

# 2) 压缩超过 2 天但未压缩的旧日志
for f in "$LOG_DIR"/openclaw-*.log; do
  [ -f "$f" ] || continue
  AGE=$(( ( $(date +%s) - $(stat -c %Y "$f") ) / 86400 ))
  [ "$AGE" -ge 2 ] || continue
  gzip "$f" 2>/dev/null && echo "compressed: $(basename "$f")"
done

# 3) 单文件超限则截断（保留最后 2000 行）
for f in "$LOG_DIR"/openclaw-*.log; do
  [ -f "$f" ] || continue
  SIZE_MB=$(( $(stat -c %s "$f") / 1024 / 1024 ))
  if [ "$SIZE_MB" -gt "$MAX_SIZE_MB" ]; then
    tail -n 2000 "$f" > "${f}.tmp" && mv "${f}.tmp" "$f"
    echo "truncated: $(basename "$f") (was ${SIZE_MB}MB)"
  fi
done

TOTAL=$(du -sh "$LOG_DIR" 2>/dev/null | cut -f1)
echo "[log-cleanup] deleted=$DELETED total=$TOTAL"
