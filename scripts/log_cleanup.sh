#!/usr/bin/env bash
# OpenClaw 日志清理 — 防止 /tmp/openclaw 日志无限增长
#
# 背景：
#   1. 2026-10-01 曾出现 24M/天 的日志递归（stale config 警告被每次 CLI 调用放大）。
#   2. 2026-10-26 修掉警告源头，但当日仍残留 77M 历史日志（其中 39M 是 10-01 的僵尸槽位）。
#
# ⚠️ 关键：OpenClaw 日志轮转是「按大小」而非「按天」——
#    同一个文件名（槽位）会被跨天持续追加，直到超过大小阈值才切换到下一个槽位。
#    因此：
#      - mtime 永远是新的       → 用 mtime 判断年龄，规则永不触发（v1 脚本的 bug）
#      - 文件内最后一条 date  ≈ 今天 → 用内容尾部判断年龄，同样失效
#    唯一可靠信号是「文件名里的槽位日期」＝ 该槽位开启的那一天。
#
# 策略（基于槽位日期）：
#   - 槽位日期距今 > KEEP_DAYS → 删除
#   - 槽位日期距今 > COMPRESS_DAYS 且非当前活跃文件 → gzip
#   - 单文件 > MAX_SIZE_MB → 截断保留最后 2000 行
#   - 目录总量 > MAX_TOTAL_MB → 从最老的槽位开始删

set -uo pipefail
LOG_DIR="${LOG_DIR:-/tmp/openclaw}"
KEEP_DAYS="${KEEP_DAYS:-7}"
COMPRESS_DAYS="${COMPRESS_DAYS:-2}"
MAX_SIZE_MB="${MAX_SIZE_MB:-50}"
MAX_TOTAL_MB="${MAX_TOTAL_MB:-200}"

[ -d "$LOG_DIR" ] || exit 0

now_epoch=$(date +%s)
today=$(date +%F)

# 槽位年龄（天）：来自文件名 openclaw-YYYY-MM-DD.log
slot_age_days() {
  local f="$1" slot
  slot=$(basename "$f" | grep -o '[0-9]\{4\}-[0-9]\{2\}-[0-9]\{2\}')
  [ -n "$slot" ] || { echo "-1"; return; }
  local ts
  ts=$(date -d "$slot" +%s 2>/dev/null) || { echo "-1"; return; }
  echo $(( (now_epoch - ts) / 86400 ))
}

DELETED=0
COMPRESSED=0
TRUNCATED=0

for f in "$LOG_DIR"/openclaw-*.log; do
  [ -f "$f" ] || continue

  base=$(basename "$f")
  age=$(slot_age_days "$f")
  size_mb=$(( $(stat -c %s "$f") / 1024 / 1024 ))

  # 当前活跃槽位（今天的日期）永不删除/压缩，只做超限截断
  slot=$(basename "$f" | grep -o '[0-9]\{4\}-[0-9]\{2\}-[0-9]\{2\}')
  is_active=0
  [ "$slot" = "$today" ] && is_active=1

  # 1) 槽位太老 → 直接删除
  if [ "$age" -ge 0 ] && [ "$age" -gt "$KEEP_DAYS" ]; then
    rm -f "$f"
    DELETED=$((DELETED+1)); echo "deleted: $base (slot age ${age}d, ${size_mb}MB)"
    continue
  fi

  # 2) 非活跃且偏老 → 压缩
  if [ "$is_active" -eq 0 ] && [ "$age" -ge 0 ] && [ "$age" -gt "$COMPRESS_DAYS" ] && [ "$size_mb" -gt 1 ]; then
    if gzip "$f" 2>/dev/null; then
      COMPRESSED=$((COMPRESSED+1)); echo "compressed: $base (${size_mb}MB, slot age ${age}d)"
    fi
    continue
  fi

  # 3) 单文件超限 → 截断保留最后 2000 行
  if [ "$size_mb" -gt "$MAX_SIZE_MB" ]; then
    tail -n 2000 "$f" > "${f}.tmp" && mv "${f}.tmp" "$f"
    TRUNCATED=$((TRUNCATED+1)); echo "truncated: $base (was ${size_mb}MB)"
  fi
done

# 4) 兜底：目录总量超阈值 → 从最老的槽位开始删
total_mb=$(du -sm "$LOG_DIR" 2>/dev/null | cut -f1)
if [ "${total_mb:-0}" -gt "$MAX_TOTAL_MB" ]; then
  echo "[log-cleanup] total ${total_mb}MB > ${MAX_TOTAL_MB}MB, trimming oldest slots"
  for f in $(ls -1 "$LOG_DIR"/openclaw-*.log 2>/dev/null | sort); do
    [ "${total_mb:-0}" -le "$MAX_TOTAL_MB" ] && break
    sz=$(stat -c %s "$f" 2>/dev/null || echo 0)
    rm -f "$f"
    total_mb=$((total_mb - sz / 1024 / 1024))
    DELETED=$((DELETED+1)); echo "trimmed: $(basename "$f")"
  done
fi

TOTAL=$(du -sh "$LOG_DIR" 2>/dev/null | cut -f1)
echo "[log-cleanup] deleted=$DELETED compressed=$COMPRESSED truncated=$TRUNCATED total=$TOTAL"
