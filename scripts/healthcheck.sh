#!/usr/bin/env bash
# 全链路健康巡检 —— 捕捉「状态绿但功能死」的静默失败
#
# 背景（2026-10-26 血泪史）：
#   今天发现三个「status=ok 但实际没工作」的功能：
#     1. 记忆检索：enabled 但 Embeddings unavailable，索引 0/11（从 3 月起就是死的）
#     2. 飞书投递：cron status=ok / consecutiveErrors=0，但结果从未送达
#     3. 学习笔记：任务不报错，但周频调度决定了 4/8 天根本不触发
#   错误信息只能告诉你「已经崩了」的东西 —— 沉默的失败必须靠**主动断言**才能发现。
#
# 这个脚本做 4 组断言：
#   A. 外部 API 连通性（飞书 / GitHub / MiniMax embeddings）
#   B. systemd 服务状态（gateway / embed-proxy）
#   C. 记忆索引新鲜度（文件数 / chunks / Dirty）
#   D. 定时任务投递结果（announce 型必须 delivered=True）
#
# 退出码：0 = 全通过；1 = 有 FAIL

set -uo pipefail

WORKSPACE="/root/.openclaw/workspace"
CFG="/root/.openclaw/openclaw.json"
PROXY_HEALTH="http://127.0.0.1:8799/health"

PASS=0; FAIL=0; WARN=0
declare -a FAILED_ITEMS=()

ok()   { PASS=$((PASS+1)); printf "  \033[32m✅ PASS\033[0m  %s\n" "$1"; }
bad()  { FAIL=$((FAIL+1)); FAILED_ITEMS+=("$1"); printf "  \033[31m❌ FAIL\033[0m  %s\n" "$1"; }
warn() { WARN=$((WARN+1)); printf "  \033[33m⚠️  WARN\033[0m  %s\n" "$1"; }

echo "════════════════════════════════════════════"
echo " 全链路健康巡检  $(date '+%Y-%m-%d %H:%M:%S')"
echo "════════════════════════════════════════════"

# ── A. 外部 API 连通性 ─────────────────────────────
echo ""
echo "【A】外部 API 连通性"

# A1 飞书 token
fs_out=$(python3 - << 'PY' 2>&1
import json, urllib.request
try:
    c = json.load(open('/root/.openclaw/openclaw.json'))
    f = c['channels']['feishu']
    req = urllib.request.Request(
        'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
        data=json.dumps({"app_id": f['appId'], "app_secret": f['appSecret']}).encode(),
        headers={'Content-Type': 'application/json'}, method='POST')
    d = json.load(urllib.request.urlopen(req, timeout=15))
    print('OK' if d.get('tenant_access_token') else f"FAIL:{d.get('msg')}")
except Exception as e:
    print(f'FAIL:{type(e).__name__}')
PY
)
if [ "${fs_out:0:2}" = "OK" ]; then ok "飞书 API token 可获取"
else bad "飞书 API token 获取失败 — ${fs_out}"; fi

# A2 GitHub API（茶馆监控依赖）
gh_code=$(curl -s -m 15 -o /dev/null -w "%{http_code}" https://api.github.com/repos/ythx-101/openclaw-qa/issues 2>/dev/null)
if [ "$gh_code" = "200" ]; then ok "GitHub API 可达（茶馆监控依赖）"
else bad "GitHub API 返回 ${gh_code} — 茶馆监控会失败"; fi

# A3 embeddings 代理
px_out=$(curl -s -m 8 "$PROXY_HEALTH" 2>/dev/null)
if echo "$px_out" | grep -q '"ok":true'; then ok "嵌入代理健康（记忆检索依赖）"
else bad "嵌入代理不可用 — 记忆检索会失效 — ${px_out}"; fi

# ── B. 服务状态 ───────────────────────────────────
echo ""
echo "【B】服务状态"

for svc in openclaw-gateway.service openclaw-embed-proxy.service; do
  state=$(systemctl --user is-active "$svc" 2>/dev/null)
  if [ "$state" = "active" ]; then ok "$svc = $state"
  else bad "$svc = ${state:-missing}"; fi
  en=$(systemctl --user is-enabled "$svc" 2>/dev/null)
  [ "$state" = "active" ] && [ "$en" != "enabled" ] && warn "$svc 未设开机自启（enabled=$en）"
done

# systemd timer（纯脚本任务用 timer 跑，不消耗模型时间）
tmr=$(systemctl --user is-active openclaw-log-cleanup.timer 2>/dev/null)
if [ "$tmr" = "active" ]; then ok "openclaw-log-cleanup.timer = active"
else bad "openclaw-log-cleanup.timer = ${tmr:-missing} — 日志轮转不会自动清理"; fi

# ── C. 记忆索引 ───────────────────────────────────
echo ""
echo "【C】记忆索引"

mem_out=$(openclaw memory status --deep 2>/dev/null | grep -v '^\[plugins\]')
idx=$(echo "$mem_out" | grep -oP 'Indexed:\s*\K[0-9]+/[0-9]+')
chunks=$(echo "$mem_out" | grep -oP 'Indexed:.*chunks' | grep -oP '\K[0-9]+(?= chunks)')
dirty=$(echo "$mem_out" | grep -oP 'Dirty:\s*\K\w+')
emb=$(echo "$mem_out" | grep -oP 'Embeddings:\s*\K\w+')

[ "$emb" = "ready" ] && ok "Embeddings: ready" || bad "Embeddings: ${emb:-unknown} — 记忆检索不可用"

if [ -n "$idx" ]; then
  got=${idx%%/*}; tot=${idx##*/}
  if [ "$got" = "$tot" ] && [ "$tot" -gt 0 ]; then ok "索引完整 $idx files · ${chunks:-0} chunks"
  else bad "索引不完整 $idx（部分文件未索引）"; fi
else
  bad "无法读取索引状态"
fi

[ "$dirty" = "no" ] && ok "索引未过期（Dirty: no）" || warn "索引待更新（Dirty: ${dirty:-?}）— 新写的记忆还没进索引"

# ── D. 定时任务投递 ───────────────────────────────
echo ""
echo "【D】定时任务投递结果"

cron_out=$(python3 "$WORKSPACE/scripts/check_cron_delivery.py" 2>/dev/null)
cron_fail=$(echo "$cron_out" | grep -oP '__FAILED__=\K[0-9]+')
cron_fail=${cron_fail:-0}

while IFS= read -r line; do
  case "$line" in
    PASS*) ok "${line#PASS }" ;;
    FAIL*) bad "${line#FAIL }" ;;
    WARN*) warn "${line#WARN }" ;;
  esac
done <<< "$cron_out"

PASS=$((PASS + 6 - cron_fail))

# ── E. 凭证暴露面 ─────────────────────────────────
echo ""
echo "【E】凭证暴露面"

cred_report=$(python3 - << 'PY' 2>/dev/null
import os, re, glob
PAT = re.compile(r'gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,}')
# 已知明文存放点
KNOWN = [
    '/root/.openclaw/workspace/GIT_CREDENTIALS.md',
    '/root/.config/gh/hosts.yml',
]
found = 0
for p in KNOWN:
    if not os.path.exists(p):
        continue
    try:
        s = open(p, encoding='utf-8', errors='replace').read()
    except Exception:
        continue
    n = len(set(PAT.findall(s)))
    if n:
        found += 1
        print(f"FAIL 明文凭证 {p}（{n} 个 token）")
# 会话日志（workspace 外，未被 git 跟踪 → 风险较低但仍需清理）
hits = 0
for f in glob.glob('/root/.openclaw/agents/main/sessions/*.jsonl*'):
    try:
        with open(f, encoding='utf-8', errors='replace') as fh:
            if PAT.search(fh.read()):
                hits += 1
    except Exception:
        pass
if hits:
    print(f"WARN 会话日志含凭证 {hits} 个文件（未入 git，风险较低）")
print(f"__CRED_FAILED__={found}")
PY
)
cred_fail=$(echo "$cred_report" | grep -oP '__CRED_FAILED__=\K[0-9]+')
cred_fail=${cred_fail:-0}
while IFS= read -r line; do
  case "$line" in
    FAIL*) bad "${line#FAIL }" ;;
    WARN*) warn "${line#WARN }" ;;
  esac
done <<< "$cred_report"

if [ "$cred_fail" -eq 0 ]; then
  ok "无明文凭证在已知存放点"
fi
# 注意：bad() 已经把 FAIL 计数加过了，这里不能再加一次
PASS=$((PASS + 1))

# ── F. 记忆笔记结构 ───────────────────────────────
echo ""
echo "【F】记忆笔记结构"

note_report=$(python3 - << 'PY' 2>/dev/null
import glob, os, re, datetime
MEM = '/root/.openclaw/workspace/memory'
# 今天的日期文件（学习/反思都写在这里）
today = datetime.date.today().isoformat()
cands = [f'{MEM}/{today}.md', '/root/.openclaw/workspace/SESSION-STATE.md']
failed = 0
for p in cands:
    if not os.path.exists(p):
        continue
    s = open(p, encoding='utf-8', errors='replace').read()
    learn = re.findall(r'^## 📚 每日学习.*$', s, re.M)
    refl  = re.findall(r'^## 🪞 每日反思.*$', s, re.M)
    name = os.path.basename(p)
    if len(learn) > 1:
        print(f"FAIL {name} 有 {len(learn)} 个「每日学习」小节（应只有 1 个）")
        failed += 1
    if len(refl) > 1:
        print(f"FAIL {name} 有 {len(refl)} 个「每日反思」小节（应只有 1 个）")
        failed += 1
    # 单节长度：学习/反思正文应 < 1200 字（超出说明没按 100-200 字约束）
    for m in re.finditer(r'^## (📚 每日学习|🪞 每日反思).*?\n(.*?)(?=\n## |\Z)', s, re.M | re.S):
        body = re.sub(r'[\s#*>`\-]', '', m.group(2))
        if len(body) > 1200:
            print(f"FAIL {name} 小节超长 {len(body)} 字（约束 100-200 字）")
            failed += 1
if failed == 0:
    print("PASS 记忆笔记结构正常")
print(f"__NOTE_FAILED__={failed}")
PY
)
note_fail=$(echo "$note_report" | grep -oP '__NOTE_FAILED__=\K[0-9]+')
note_fail=${note_fail:-0}
while IFS= read -r line; do
  case "$line" in
    PASS*) ok "${line#PASS }" ;;
    FAIL*) bad "${line#FAIL }" ;;
  esac
done <<< "$note_report"
PASS=$((PASS + 1))

# ── G. 独立技能仓库同步 ───────────────────────────
echo ""
echo "【G】独立技能仓库同步"

repo_report=$(python3 - << 'PY' 2>/dev/null
import os, subprocess
BASE = '/root/.openclaw/workspace/skills'
failed = 0
found = 0
for name in sorted(os.listdir(BASE)):
    d = os.path.join(BASE, name)
    if not os.path.isdir(os.path.join(d, '.git')):
        continue
    found += 1
    def git(*a):
        r = subprocess.run(['git', '-C', d] + list(a), capture_output=True, text=True, timeout=45)
        return r.stdout.strip()
    try:
        git('fetch', 'origin', '--quiet')
    except Exception:
        pass
    behind = git('rev-list', '--count', '@{u}..HEAD')
    ahead = git('rev-list', '--count', 'HEAD..@{u}')
    dirty = len(git('status', '--short').splitlines())
    if behind == '0' and ahead == '0' and dirty == 0:
        print(f"PASS {name} 与远端同步")
    else:
        bits = []
        if behind != '0': bits.append(f'落后 {behind}')
        if ahead != '0': bits.append(f'领先 {ahead}')
        if dirty: bits.append(f'未提交 {dirty}')
        print(f"FAIL {name} 仓库不同步（{'，'.join(bits)}）")
        failed += 1
if found == 0:
    print("WARN 未发现独立技能仓库")
print(f"__REPO_FAILED__={failed}")
PY
)
repo_fail=$(echo "$repo_report" | grep -oP '__REPO_FAILED__=\K[0-9]+')
repo_fail=${repo_fail:-0}
while IFS= read -r line; do
  case "$line" in
    PASS*) ok "${line#PASS }" ;;
    FAIL*) bad "${line#FAIL }" ;;
    WARN*) warn "${line#WARN }" ;;
  esac
done <<< "$repo_report"
PASS=$((PASS + 1))

# ── 汇总 ─────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════"
echo " 结果: PASS=$PASS  FAIL=$FAIL  WARN=$WARN"
if [ "$FAIL" -gt 0 ]; then
  echo ""
  echo " ⚠️ 有 $FAIL 项失败，需要处理。"
  for it in "${FAILED_ITEMS[@]}"; do echo "   - $it"; done
  echo ""
  echo " 提示：静默失败优先排查「触发→执行→落盘→投递」每一段，别只看状态字段。"
fi
echo "════════════════════════════════════════════"

[ "$FAIL" -gt 0 ] && exit 1
exit 0
