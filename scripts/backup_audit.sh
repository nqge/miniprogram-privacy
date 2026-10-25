#!/usr/bin/env bash
# 灾备完整性审计
# 背景：2026-10-26 发现 SOUL.md / TOOLS.md / USER.md 未入库，
#       而"push 成功"并不代表"备份完整"。本脚本把验收标准固化成可执行检查。
#
# 用法: bash scripts/backup_audit.sh
# 退出码: 0 = 全部通过, 1 = 存在 FAIL

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

REMOTE="${REMOTE:-origin}"
BRANCH="${BRANCH:-master}"

PASS=0
FAIL=0
WARN=0

ok()   { printf "  \033[32m✅ PASS\033[0m  %s\n" "$1"; PASS=$((PASS+1)); }
bad()  { printf "  \033[31m❌ FAIL\033[0m  %s\n" "$1"; FAIL=$((FAIL+1)); }
warn() { printf "  \033[33m⚠️  WARN\033[0m  %s\n" "$1"; WARN=$((WARN+1)); }

# 核心身份文件：这些丢了就无法重建"我是谁"，必须每个都在远端
CORE_FILES=(
  AGENTS.md SOUL.md USER.md IDENTITY.md
  MEMORY.md TOOLS.md HEARTBEAT.md SESSION-STATE.md
)

echo "══════════════════════════════════════════════"
echo " 灾备完整性审计 — $(date '+%Y-%m-%d %H:%M:%S')"
echo " 远端: $REMOTE/$BRANCH"
echo "══════════════════════════════════════════════"

echo ""
echo "【1】核心身份文件是否在远端"
for f in "${CORE_FILES[@]}"; do
  if git cat-file -e "$REMOTE/$BRANCH:$f" 2>/dev/null; then
    ok "$f"
  else
    bad "$f 缺失于远端 — 机器损毁将无法重建身份"
  fi
done

echo ""
echo "【2】远端是否含真实凭证"
# 只扫远端 tree，不扫工作区（工作区允许有本地凭证副本）
SECRET_RE='ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9]{40,}|sk-[A-Za-z0-9]{40,}'
HITS=$(git grep -lE "$SECRET_RE" "$REMOTE/$BRANCH" -- 2>/dev/null | grep -v 'GIT_CREDENTIALS.md' || true)
if [ -z "$HITS" ]; then
  ok "远端无真实 PAT / API Key"
else
  bad "远端疑似含凭证: $HITS"
fi

echo ""
echo "【3】凭证类文件是否被 git 跟踪"
for f in GIT_CREDENTIALS.md; do
  if git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
    bad "$f 仍被版本控制跟踪 — 必须移出并重写历史"
  else
    ok "$f 未被跟踪"
  fi
done

echo ""
echo "【4】本地与远端同步状态"
AHEAD=$(git rev-list --count "$REMOTE/$BRANCH..HEAD" 2>/dev/null || echo "?")
BEHIND=$(git rev-list --count "HEAD..$REMOTE/$BRANCH" 2>/dev/null || echo "?")
if [ "$AHEAD" = "0" ] && [ "$BEHIND" = "0" ]; then
  ok "本地与远端一致"
else
  if [ "$AHEAD" != "0" ]; then
    bad "本地领先远端 $AHEAD 个 commit（未推送）"
  fi
  if [ "$BEHIND" != "0" ]; then
    warn "远端领先本地 $BEHIND 个 commit"
  fi
fi

DIRTY=$(git status --porcelain 2>/dev/null | grep -v '^??' | wc -l)
if [ "$DIRTY" = "0" ]; then
  ok "工作区无未提交改动"
else
  warn "工作区有 $DIRTY 个未提交改动"
fi

echo ""
echo "【5】关键目录备份覆盖"
for d in memory scripts skills; do
  REMOTE_COUNT=$(git ls-tree -r --name-only "$REMOTE/$BRANCH" "$d/" 2>/dev/null | wc -l)
  LOCAL_COUNT=$(find "$d" -type f 2>/dev/null | wc -l)
  if [ "$REMOTE_COUNT" -eq 0 ]; then
    bad "$d/ 未入库"
  elif [ "$REMOTE_COUNT" -lt "$LOCAL_COUNT" ]; then
    warn "$d/ 远端 $REMOTE_COUNT 个 < 本地 $LOCAL_COUNT 个（可能漏文件）"
  else
    ok "$d/ 已备份 $REMOTE_COUNT 个文件"
  fi
done

echo ""
echo "【6】配置哈希基线"
BASE="$HOME/.openclaw/.config-baseline.sha256"
if [ -f "$BASE" ]; then
  if sha256sum -c "$BASE" >/dev/null 2>&1; then
    ok "配置基线校验通过"
  else
    bad "配置基线不匹配 — openclaw.json 被修改过"
  fi
else
  warn "配置基线未建立"
fi

echo ""
echo "【7】敏感文件权限"
for f in "$HOME/.openclaw/openclaw.json" "$HOME/.openclaw/devices/paired.json" "$HOME/.ssh/id_ed25519"; do
  [ -f "$f" ] || continue
  PERM=$(stat -c '%a' "$f")
  if [ "$PERM" = "600" ]; then
    ok "$(basename "$f") 权限 600"
  else
    bad "$(basename "$f") 权限 $PERM（应为 600）"
  fi
done

echo ""
echo "══════════════════════════════════════════════"
echo " 结果: PASS=$PASS  FAIL=$FAIL  WARN=$WARN"
echo "══════════════════════════════════════════════"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
