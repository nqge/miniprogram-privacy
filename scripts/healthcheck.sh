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

# 日志降噪 —— 2026-10-26 实测：
# 每次 `openclaw` CLI 调用都会重新注册全部插件，INFO 级打 6+ 行
# 「feishu_xxx: Registered ...」，占当日日志 87%（24158/27676 行）。
# 实测 3 次 `openclaw cron list`：默认 27008 字节/次 → warn 级 5735 字节/次（-79%）。
# 只能用环境变量控制（改 openclaw.json 的 logging 段会触发 loadConfig 递归）。
export OPENCLAW_LOG_LEVEL="${OPENCLAW_LOG_LEVEL:-warn}"

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

# A2 GitHub API（茶馆监控依赖）—— 用认证请求，并区分「限流」与「不可达」
# 2026-10-26 教训：匿名额度只有 60 次/小时/IP，耗尽后 403 看起来像权限问题，
# 极难一眼看出。现在读 gh CLI 的 token 换成 5000 次/小时，并报实际额度。
# token 统一从 scripts/gh_auth.py 取（单一真相源）——
# 之前这里自己抄了一遍正则，与另外两个脚本重复，gh 改格式时会三处同时静默失效
GH_TOKEN=$(python3 /root/.openclaw/workspace/scripts/gh_auth.py --token 2>/dev/null)
gh_hdr=(-H "User-Agent: openclaw-healthcheck" -H "Accept: application/vnd.github+json")
[ -n "$GH_TOKEN" ] && gh_hdr+=(-H "Authorization: Bearer $GH_TOKEN")
gh_rl=$(curl -s -m 15 "${gh_hdr[@]}" https://api.github.com/rate_limit 2>/dev/null)
gh_remain=$(echo "$gh_rl" | python3 -c "import json,sys
try:
    d=json.load(sys.stdin); c=(d.get('resources') or {}).get('core') or {}
    print(f\"{c.get('remaining')}/{c.get('limit')}\")
except Exception: print('')" 2>/dev/null)
if [ -n "$gh_remain" ]; then
  gh_r=${gh_remain%%/*}; gh_l=${gh_remain##*/}
  if [ -n "$gh_l" ] && [ "$gh_l" -gt 0 ] 2>/dev/null && [ "$gh_r" -lt 5 ] 2>/dev/null; then
    bad "GitHub API 额度将耗尽（${gh_remain}）— 茶馆监控可能拉不到新讨论"
  else
    ok "GitHub API 可用（额度 ${gh_remain}，茶馆监控依赖）"
  fi
else
  bad "GitHub API 探测失败 — 茶馆监控可能拉不到新讨论"
fi

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

# 快速健康子集的定时器（30 分钟一次 + 独立告警链路）
qtmr=$(systemctl --user is-active openclaw-quick-health.timer 2>/dev/null)
if [ "$qtmr" = "active" ]; then ok "openclaw-quick-health.timer = active（30 分钟巡检）"
else bad "openclaw-quick-health.timer = ${qtmr:-missing} — 服务故障最长 4 小时才被发现"; fi

# 告警链路的 OnFailure 指令是否**真正生效**（不 grep 文件 —— 注释里也可能有这行字）
# 2026-10-26 教训：重写 .service 时把指令弄丢了，但注释里还留着 → grep 看着正常，实际静默失效
qof=$(systemctl --user show openclaw-quick-health.service -p OnFailure --value 2>/dev/null | tr -d '\n')
if echo "$qof" | grep -q "quick-health-alert"; then
  ok "quick-health 的 OnFailure 告警链路已生效"
else
  bad "quick-health 的 OnFailure 指令丢失或指向异常 — 故障时不会告警（$qof）"
fi

# L1 经验库检索验证 —— 每条经验都必须能被自己的原文措辞搜到
# 2026-10-26 实测：19 条里有 4 条塞在长文件里检索不到（E17 的问题），
# 拆成独立文件后 19/19 全部命中。写下来的经验搜不到 = 等于没写。
l1_out=$(timeout 280 python3 /root/.openclaw/workspace/scripts/verify_l1_retrieval.py 2>/dev/null)
l1_rc=$?
l1_line=$(echo "$l1_out" | grep "结果:")
if [ "$l1_rc" -eq 0 ]; then
  ok "L1 经验库检索验证通过（$l1_line）"
else
  bad "L1 经验库有经验检索不到 —— $l1_line"
  echo "$l1_out" | grep '❌' | sed 's/^/        /' | head -5
fi

# 完整的 systemd 配置漂移检查（单元 enabled / 脚本存在 / 关键指令生效 / 定时器调度）
# 覆盖「配置文件里的东西 ≠ 实际生效的东西」这一整类风险
drift_out=$(python3 /root/.openclaw/workspace/scripts/check_unit_drift.py 2>/dev/null)
drift_rc=$?
drift_pass=$(echo "$drift_out" | grep -c '✅')
drift_fail=$(echo "$drift_out" | grep -c '❌')
if [ "$drift_fail" -gt 0 ]; then
  bad "systemd 配置漂移：$drift_fail 项异常（详见 scripts/check_unit_drift.py）"
  echo "$drift_out" | grep '❌' | sed 's/^/        /'
else
  ok "systemd 配置无漂移（$drift_pass 项全部通过）"
fi

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

# ── H. 端到端功能实测 ─────────────────────────────
echo ""
echo "【H】端到端功能实测（不是看状态字段，是真的跑一次）"

# H1 记忆检索：真的搜一次，并检查是否命中已知内容
mem_search_out=$(timeout 90 openclaw memory search "牛哥哥 称呼 沟通语言" --json 2>/dev/null | python3 -c "
import json, sys
raw = sys.stdin.read()
i = raw.find('{')
if i < 0:
    print('PARSE_ERROR'); raise SystemExit
try:
    d = json.loads(raw[i:])
except Exception:
    print('PARSE_ERROR'); raise SystemExit
rs = d.get('results', [])
if not rs:
    print('NO_RESULT'); raise SystemExit
top = rs[0]
# 断言：命中 MEMORY.md 且片段含「称呼」关键词
snippet = top.get('snippet', '')
ok = 'MEMORY.md' in top.get('path', '') and '称呼' in snippet
print(('OK' if ok else 'WRONG') + '|' + str(top.get('path')) + ':' + str(top.get('startLine')) + '|' + str(len(rs)) + '条')
" 2>/dev/null)
case "$mem_search_out" in
  OK*)  ok "记忆检索端到端可用（命中 $(echo "$mem_search_out" | cut -d'|' -f2)）" ;;
  NO_RESULT*) bad "记忆检索无结果 —— 检索功能实际已死" ;;
  WRONG*) bad "记忆检索命中内容不对（$(echo "$mem_search_out" | cut -d'|' -f2)）" ;;
  *) bad "记忆检索执行异常" ;;
esac

# H2 飞书：真的发一条消息（不是只查 token）
feishu_out=$(python3 - << 'PY' 2>/dev/null
import json, urllib.request, time
try:
    c = json.load(open('/root/.openclaw/openclaw.json'))
    f = c['channels']['feishu']
    req = urllib.request.Request(
        'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
        data=json.dumps({"app_id": f['appId'], "app_secret": f['appSecret']}).encode(),
        headers={'Content-Type': 'application/json'}, method='POST')
    tok = json.load(urllib.request.urlopen(req, timeout=15)).get('tenant_access_token')
    if not tok:
        print('NO_TOKEN'); raise SystemExit
    # 用 chat_id 列表接口做只读验证（不打扰用户）
    r = urllib.request.Request('https://open.feishu.cn/open-apis/im/v1/chats?page_size=1',
                               headers={'Authorization': f'Bearer {tok}'})
    d = json.load(urllib.request.urlopen(r, timeout=15))
    print('OK' if d.get('code') == 0 else f"API_ERR:{d.get('msg')}")
except Exception as e:
    print(f'ERR:{type(e).__name__}')
PY
)
case "$feishu_out" in
  OK*) ok "飞书 API 端到端可用（token + 消息接口均正常）" ;;
  *) bad "飞书 API 实际不可用（$feishu_out）" ;;
esac

# ── I. 茶馆内容蒸馏进度 ───────────────────────────
echo ""
echo "【I】茶馆内容蒸馏进度"

distill_report=$(python3 - << 'PY' 2>/dev/null
import json, os, re, glob
WS = '/root/.openclaw/workspace'
todo = f'{WS}/memory/teahouse-todo.json'
failed = 0
pending = []
if os.path.exists(todo):
    try:
        pending = json.load(open(todo, encoding='utf-8')).get('pending', [])
    except Exception:
        pending = []
if not pending:
    print("PASS 茶馆内容已全部蒸馏（无待处理）")
else:
    # 检查这些讨论编号是否已在 memory/ 的**每日笔记**里出现过
    # （排除 teahouse-digest-*.md 蒸馏稿本身，否则会被自己的输出骗过）
    still = []
    notes = [f for f in glob.glob(f'{WS}/memory/*.md')
             if not os.path.basename(f).startswith('teahouse-digest-')]
    for p in pending:
        num = f"#{p.get('number')}"
        found = False
        for f in notes:
            try:
                if num in open(f, encoding='utf-8', errors='replace').read():
                    found = True
                    break
            except Exception:
                pass
        if not found:
            still.append(p)
    if still:
        print(f"FAIL 茶馆有 {len(still)} 条讨论未蒸馏（原始 dump 已落盘但未提炼成知识）")
        failed = 1
    else:
        print("PASS 茶馆内容已全部蒸馏")
print(f"__DISTILL_FAILED__={failed}")
PY
)
distill_fail=$(echo "$distill_report" | grep -oP '__DISTILL_FAILED__=\K[0-9]+')
distill_fail=${distill_fail:-0}
while IFS= read -r line; do
  case "$line" in
    PASS*) ok "${line#PASS }" ;;
    FAIL*) bad "${line#FAIL }" ;;
  esac
done <<< "$distill_report"
PASS=$((PASS + 1))

# ── J. L1 经验库活跃度 ───────────────────────────
echo ""
echo "【J】L1 经验库活跃度"

l1_report=$(python3 - << 'PY' 2>/dev/null
import os, re, glob, datetime
WS = '/root/.openclaw/workspace'
L1 = f'{WS}/memory/L1-experience-library.md'
failed = 0
if not os.path.exists(L1):
    print("FAIL L1 经验库不存在（memory/L1-experience-library.md）")
    failed = 1
else:
    s = open(L1, encoding='utf-8', errors='replace').read()
    entries = re.findall(r'^### (E\d+)[：:]', s, re.M)
    if not entries:
        print("FAIL L1 经验库没有任何条目")
        failed = 1
    else:
        # 是否有近期（7 天内）新增的经验 —— 长期不更新说明提炼环节停了
        mtime = datetime.date.fromtimestamp(os.path.getmtime(L1))
        age = (datetime.date.today() - mtime).days
        if age > 30:
            print(f"FAIL L1 经验库已 {age} 天未更新（提炼环节可能已停）")
            failed = 1
        else:
            print(f"PASS L1 经验库 {len(entries)} 条，最后更新 {age} 天前")
print(f"__L1_FAILED__={failed}")
PY
)
l1_fail=$(echo "$l1_report" | grep -oP '__L1_FAILED__=\K[0-9]+')
l1_fail=${l1_fail:-0}
while IFS= read -r line; do
  case "$line" in
    PASS*) ok "${line#PASS }" ;;
    FAIL*) bad "${line#FAIL }" ;;
  esac
done <<< "$l1_report"
PASS=$((PASS + 1))

# ── K. 工作区根目录整洁度 ─────────────────────────
echo ""
echo "【K】工作区根目录整洁度"

root_report=$(python3 - << 'PY' 2>/dev/null
import os
WS = '/root/.openclaw/workspace'
# 根目录应该只有这些文件（子目录不算）
EXPECTED = {
    'AGENTS.md','SOUL.md','USER.md','IDENTITY.md','MEMORY.md','TOOLS.md',
    'HEARTBEAT.md','SESSION-STATE.md','proactive-tracker.md','待决策选项单.md',
    'LEARNING_PLAN.md','LEARNING_SETUP_COMPLETE.md','CRON_SETUP_COMPLETE.md',
    'learning_summary.py','GIT_CREDENTIALS.md',
}
failed = 0
stray = []
for name in os.listdir(WS):
    p = os.path.join(WS, name)
    if not os.path.isfile(p):
        continue
    if name in EXPECTED or name.startswith('.'):
        continue
    stray.append(name)
if stray:
    for s in stray:
        size = os.path.getsize(os.path.join(WS, s))
        print(f"FAIL 根目录有多余文件「{s}」（{size} 字节）—— 可能是误创建的临时产物")
        failed += 1
else:
    print("PASS 根目录文件干净")
print(f"__ROOT_FAILED__={failed}")
PY
)
root_fail=$(echo "$root_report" | grep -oP '__ROOT_FAILED__=\K[0-9]+')
root_fail=${root_fail:-0}
while IFS= read -r line; do
  case "$line" in
    PASS*) ok "${line#PASS }" ;;
    FAIL*) bad "${line#FAIL }" ;;
  esac
done <<< "$root_report"
PASS=$((PASS + 1))

# ── L. 连续性入口新鲜度 ───────────────────────────
echo ""
echo "【L】连续性入口新鲜度"

cont_report=$(python3 - << 'PY' 2>/dev/null
import os, re, datetime
WS = '/root/.openclaw/workspace'
failed = 0
# SESSION-STATE.md 是下次会话的第一入口 —— 头部状态描述若过期会误导后续判断
p = f'{WS}/SESSION-STATE.md'
if not os.path.exists(p):
    print("FAIL SESSION-STATE.md 不存在")
    failed = 1
else:
    s = open(p, encoding='utf-8', errors='replace').read()
    m = re.search(r'\*\*最后更新\*\*:\s*(\d{4}-\d{2}-\d{2})', s)
    if not m:
        print("FAIL SESSION-STATE.md 缺少「最后更新」标记")
        failed = 1
    else:
        d = datetime.date.fromisoformat(m.group(1))
        age = (datetime.date.today() - d).days
        if age > 2:
            print(f"FAIL SESSION-STATE.md 已 {age} 天未更新（头部状态描述可能已过期）")
            failed = 1
        else:
            print(f"PASS SESSION-STATE.md 头部新鲜（{age} 天前更新）")
print(f"__CONT_FAILED__={failed}")
PY
)
cont_fail=$(echo "$cont_report" | grep -oP '__CONT_FAILED__=\K[0-9]+')
cont_fail=${cont_fail:-0}
while IFS= read -r line; do
  case "$line" in
    PASS*) ok "${line#PASS }" ;;
    FAIL*) bad "${line#FAIL }" ;;
  esac
done <<< "$cont_report"
PASS=$((PASS + 1))

# ── 汇总 ─────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════"
echo " 结果: PASS=$PASS  FAIL=$FAIL  WARN=$WARN"

# 维护 SESSION-STATE.md 里的「巡检现状」行 —— 避免硬编码数字变陈旧
# （2026-10-26 教训：SESSION-STATE 头部曾过期 5 天）
# 独立成脚本：bash 里嵌套 heredoc + 命令替换会导致变量展开异常（实测 $GROUPS 恒为空）
python3 /root/.openclaw/workspace/scripts/update_state_status.py "$PASS" "$FAIL" "$WARN" >/dev/null 2>&1

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
