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
declare -a WAITING_ITEMS=()

# 「等用户」的长期项 —— 2026-10-26 加
#
# 为什么：这些项每天都成立，要等牛哥哥本人操作才能解决
# （吊销 PAT / 给第二个 provider 的 key）。
# 混在「可处理故障」里有两个害处：
#   1) 报告里真故障被长期项稀释 → 注意力被带走
#   2) 每天的 FAIL 计数永远 ≥1 → 「今天有没有故障」这个信号失效
# 分类后仍然全部报出来（信息不丢），但单独成组，一眼能看出哪些要动、哪些在等。
WAITING_PATTERNS=(
  "明文凭证 "
  "只有 1 个 provider"
  "未配置任何 fallback"
  "fallback 全在"
)

is_waiting() {
  local msg="$1"
  for pat in "${WAITING_PATTERNS[@]}"; do
    [[ "$msg" == *"$pat"* ]] && return 0
  done
  return 1
}

ok()   { PASS=$((PASS+1)); printf "  \033[32m✅ PASS\033[0m  %s\n" "$1"; }
bad()  {
  FAIL=$((FAIL+1))
  FAILED_ITEMS+=("$1")
  if is_waiting "$1"; then
    WAITING_ITEMS+=("$1")
    printf "  \033[33m⏳ 等你\033[0m  %s\n" "$1"
  else
    printf "  \033[31m❌ FAIL\033[0m  %s\n" "$1"
  fi
}
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
today = datetime.date.today().isoformat()

# 2026-10-27 (E38)：F 组原来只看「今天」的文件 →
#   历史文件里的重复小节永远抓不到（10-21 就有 2 个学习小节，一直没人发现）
# 修法：全部 daily-note 文件都查「重复」。
# 注意：只查重复（>1），不查缺失（==0）——
#   · 修复前的日期（10-22/10-23）本来就没学习小节
#   · 反思是每周日跑，工作日没有是正常的
#   · 今天的学习小节要等 09:00 的 cron 才会生成
daily_files = sorted(glob.glob(f'{MEM}/20??-??-??.md'))
cands = daily_files + ['/root/.openclaw/workspace/SESSION-STATE.md']
failed = 0
checked = 0
for p in cands:
    if not os.path.exists(p):
        continue
    s = open(p, encoding='utf-8', errors='replace').read()
    learn = re.findall(r'^## 📚 每日学习.*$', s, re.M)
    refl  = re.findall(r'^## 🪞 每日反思.*$', s, re.M)
    name = os.path.basename(p)
    is_daily = bool(re.match(r'^\d{4}-\d{2}-\d{2}\.md$', name))
    if is_daily:
        checked += 1
    if len(learn) > 1:
        print(f"FAIL {name} 有 {len(learn)} 个「每日学习」小节（应只有 1 个）")
        failed += 1
    if len(refl) > 1:
        print(f"FAIL {name} 有 {len(refl)} 个「每日反思」小节（应只有 1 个）")
        failed += 1
    # 单节长度：按**汉字数**衡量（任务指令里的约束是「100-200 字」，
    # 中文的「字」就是汉字，不是所有字符）
    #
    # 2026-10-27 07:55 修：
    # 原来用「去掉空白后的所有字符数 > 1200」—— **量错了东西**：
    #   · 规则说的是 100-200 **汉字**，断言数的却是含标点/英文/空白的全字符
    #   · 两个口径不一致 → 断言放过了 10-21 的 568 汉字、今天的 991 汉字
    #     （今天直到 2010 全字符才触发，而按规则早就超了 5 倍）
    # → 统一口径：数汉字，上限 400（给 100-200 留一倍余量，
    #   避免边界抖动误报；但超出 200 太多就是没遵守约束）
    # 2026-10-27 08:00 再修（边界 bug）：
    # 原来用 `(?=\n## |\Z)` 找小节结尾 —— 只认 `##` 级标题，
    # 于是当「📚 每日学习」是文件里**最后一个 ## 级标题**时，
    # 后面所有的 `### 心跳` 小节全被算进学习正文
    # → 2026-10-27 的学习小节被算成 946 汉字（实际只有 ~170）
    #
    # 修法：小节在**下一个任意级标题**（## 或 ###）处结束 ——
    # `###` 是 `##` 的子节，本来就属于上一个 `##`。
    for m in re.finditer(r'^## (📚 每日学习|🪞 每日反思).*?\n(.*?)(?=\n#{1,6} |\Z)', s, re.M | re.S):
        body = m.group(2)
        zh = len(re.findall(r'[一-鿿]', body))
        if zh > 400:
            print(f"FAIL {name} 小节超长 {zh} 汉字（约束 100-200 汉字，上限 400）")
            failed += 1
if failed == 0:
    print(f"PASS 记忆笔记结构正常（已查 {checked} 个每日笔记文件）")
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
    # 队列为空时也要查自洽性（否则这个检查永远走不到）
    try:
        data = json.load(open(todo, encoding='utf-8'))
        dist = {d.get('number') for d in data.get('distilled', [])}
        bad = {d.get('number') for d in data.get('distilled', [])
               if not isinstance(d.get('number'), int)}
        if bad:
            print(f"FAIL 茶馆 distilled 列表里有非法编号：{sorted(bad)[:5]}")
            failed = 1
        else:
            print(f"PASS 茶馆内容已全部蒸馏（无待处理，已蒸馏 {len(dist)} 条）")
    except Exception:
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
    # 2026-10-27（E30）：pending 里的条目如果**已经**出现在某篇笔记里，
    # 说明「已经蒸馏但没人把它移出队列」—— 这正是 E30 的 bug 状态，
    # 而原来的检查**只报 PASS、不做清理** → bug 会一直复发。
    #
    # 现在**自愈**：把这些条目移出 pending 并记入 distilled，
    # 让「出队」不再依赖 agent 记得跑 --mark-distilled。
    #
    # ⚠️ 方向别搞反（我第一版写反了）：
    #   still = **没**出现在笔记里的（= 真未蒸馏）
    #   healed = **已**出现在笔记里的（= 已蒸馏但没出队）
    if pending:
        notes_txt = ""
        for f in notes:
            try:
                notes_txt += open(f, encoding='utf-8', errors='replace').read()
            except Exception:
                pass
        # 匹配方式：**编号或标题**都能触发（2026-10-27 补）
        # 原来只认 '#135' 这种带 # 的写法 —— 如果 agent 蒸馏时只写标题
        # 「小隐的三层记忆架构」而没写编号，条目就会永远卡在 pending。
        # 标题取前 15 字（足够唯一，又不会因为标题被改写而漏配）。
        def _is_distilled(item):
            num = f"#{item.get('number')}"
            if num in notes_txt:
                return True
            title = (item.get('title') or '').strip()
            if len(title) >= 10:
                head = title[:15]
                if head in notes_txt:
                    return True
            return False

        healed_items = [p for p in pending if _is_distilled(p)]
        if healed_items:
            try:
                data = json.load(open(todo, encoding='utf-8'))
                dist = list(data.get('distilled', []))
                known = {d.get('number') for d in dist}
                for p in healed_items:
                    if p.get('number') in known:
                        continue
                    dist.append({
                        "number": p.get("number"),
                        "title": (p.get("title") or "")[:100],
                        "comments": p.get("comments", 0),
                        "distilled_at": __import__("datetime").datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                        "healed_by": "healthcheck",
                    })
                data["distilled"] = dist[-50:]
                healed_nums = {p.get('number') for p in healed_items}
                data["pending"] = [p for p in data.get("pending", [])
                                   if p.get('number') not in healed_nums]
                json.dump(data, open(todo, 'w', encoding='utf-8'),
                          ensure_ascii=False, indent=2)
                print(f"PASS 茶馆自愈：{len(healed_nums)} 条已蒸馏但未出队 → 已移入 distilled {sorted(healed_nums)}")
            except Exception as e:
                print(f"WARN 茶馆自愈失败（{type(e).__name__}）")

    if still:
        print(f"FAIL 茶馆有 {len(still)} 条讨论未蒸馏（原始 dump 已落盘但未提炼成知识）")
        failed = 1
    else:
        # 2026-10-27：pending 队列原来「只有加没有减」—— 蒸馏完成后没人把它移走，
        # 每 4 小时运行都把同样 4 条当成「待处理」重新蒸馏一遍（反刍）。
        # 现在蒸馏完的条目会移到 distilled 列表，所以这里也检查两个列表是否自洽：
        #   pending 与 distilled 不应有交集（同一议题不能既待处理又已蒸馏）
        #
        # ⚠️ 必须**重新读盘**：上面自愈可能刚改过这个文件，
        #    继续用内存里的旧 pending 会报出「刚自愈完又说矛盾」的假 FAIL。
        try:
            data = json.load(open(todo, encoding='utf-8'))
            dist = {d.get('number') for d in data.get('distilled', [])}
            cur_pending = {p.get('number') for p in data.get('pending', [])}
            overlap = cur_pending & dist
            if overlap:
                print(f"FAIL 茶馆待蒸馏队列自相矛盾：{sorted(overlap)} 既在 pending 又在 distilled")
                failed = 1
            else:
                print(f"PASS 茶馆内容已全部蒸馏（pending {len(cur_pending)} 条，distilled {len(dist)} 条）")
        except Exception:
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
echo "【N】记忆库体积与索引均衡"

# 2026-10-26 实测：一天 15 小时高强度工作让 memory/2026-10-26.md 涨到 106KB /
# 1875 行 → 单独占掉 215 个索引块里的 100 块（46%）。
#
# ⚠️ 判据设计的教训（第一版用 25% 做 FAIL 阈值 → **假警报**）：
# 我以为「单文件占块数高 = 有问题」，但实测发现那个文件里是**真实操作知识**
# （检索「日志清理脚本形同虚设」命中 0.8963，4 块全在笔记里）——
# 一个忙碌的日子**合法地**就该占 30-40%。
# 真正的问题不是「占比高」，而是：
#   1) memory/ 目录**没有任何体积上限**（对比：/tmp/openclaw 日志有 50MB/200MB 上限）
#      → 单个文件可以无限涨，最终把整个索引淹掉
#   2) 占比高到**真的开始挤掉别的文件**（检索被单文件垄断）
#
# 所以判据改成：
#   单文件 > 300KB              → FAIL（超过「一篇笔记」的合理规模）
#   单文件占索引块 > 60%        → FAIL（开始垄断检索）
#   memory/ 目录 > 200MB        → WARN（索引会持续变大，该归档旧笔记）
# 25% 那个阈值是**假警报** —— 已废弃。

mem_report=$(python3 - << 'PY3' 2>/dev/null
import os, sqlite3
MEM_DIR = '/root/.openclaw/memory'
DB = '/root/.openclaw/memory/main.sqlite'
MAX_FILE_KB = 300
MAX_SHARE_PCT = 60
MAX_DIR_MB = 200

# 1) 索引块分布（只有真「垄断」才 FAIL）
try:
    db = sqlite3.connect(DB)
    rows = db.execute("select path, count(*) c from chunks group by path").fetchall()
    total = sum(c for _, c in rows)
except Exception as e:
    print(f"FAIL 读不到索引（{type(e).__name__}）")
    raise SystemExit(1)

if rows and total:
    worst_path, worst_c = max(rows, key=lambda r: r[1])
    share = worst_c * 100 // max(total, 1)
    if share > MAX_SHARE_PCT:
        print(f"FAIL 单个文件垄断索引 {share}%（{worst_c}/{total} 块）→ {worst_path}"
              f" —— 会挤掉其他文件，需拆分或归档")
    else:
        print(f"PASS 索引块分布正常（最大占比 {share}%：{worst_path}）")

# 2) 单文件体积（真正的硬上限）
big = []
for name in os.listdir(MEM_DIR):
    p = os.path.join(MEM_DIR, name)
    if os.path.isfile(p) and name.endswith('.md'):
        kb = os.path.getsize(p) // 1024
        if kb > MAX_FILE_KB:
            big.append(f"{name} ({kb}KB)")
if big:
    print(f"FAIL 单个记忆文件超过 {MAX_FILE_KB}KB：{', '.join(big)}"
          f" —— 应拆分或归档（摘录留在独立文件，笔记只留结论+指针）")
else:
    print(f"PASS 记忆文件体积均在 {MAX_FILE_KB}KB 以内")

# 3) 目录总量
total_mb = 0
for root, _, files in os.walk(MEM_DIR):
    for f in files:
        try:
            total_mb += os.path.getsize(os.path.join(root, f))
        except OSError:
            pass
total_mb //= 1024 * 1024
if total_mb > MAX_DIR_MB:
    print(f"WARN memory/ 目录已达 {total_mb}MB —— 索引会持续变大，该考虑归档旧笔记")
else:
    print(f"PASS memory/ 目录 {total_mb}MB（上限 {MAX_DIR_MB}MB）")
PY3
)
while IFS= read -r line; do
  case "$line" in
    PASS*) ok "${line#PASS }" ;;
    FAIL*) bad "${line#FAIL }" ;;
    WARN*) warn "${line#WARN }" ;;
    *) [ -n "$line" ] && echo "  $line" ;;
  esac
done <<< "$mem_report"
PASS=$((PASS + 1))

echo "【T】cron 指令的防复发提醒是否齐全"

# 2026-10-27 07:20 补：E33/E35 之后，cron 指令里加了两类「防复发提醒」：
#   · 【跨日提醒】—— 写 memory/YYYY-MM-DD.md 前用 `date +%F`
#     （E33：跨零点还写昨天文件 → 时间线失真，且**没有任何失败信号**）
#   · 【等待方式】—— 跑长脚本用短 poll，别一次等满
#     （E35：poll 会阻塞满 timeout，3 次 ≈ 510s → 必然撞 job 超时）
#
# 但这些提醒是我**一条条手动加**的 —— 加漏了没有任何机制会发现。
# （这正是 E34 的老问题：靠「记得做」。）
# 现在自动核对：**每个会写每日笔记 / 跑长脚本的任务，都必须有对应提醒**。

cr_report=$(python3 - << 'PY7' 2>/dev/null
import json, subprocess
failed = 0
try:
    r = subprocess.run(['openclaw', 'cron', 'list', '--json'],
                       capture_output=True, text=True, timeout=60)
    jobs, _ = json.JSONDecoder().raw_decode(r.stdout[r.stdout.find('['):])
except Exception as e:
    print(f"FAIL 读不到 cron 任务（{type(e).__name__}）")
    raise SystemExit(1)

# 会写 memory/YYYY-MM-DD.md 的任务 → 必须有跨日提醒
need_date = {'healthcheck', 'backup-audit', 'daily-learning', 'daily-reflection'}
# 会跑可能较久的脚本 → 必须有等待方式说明
need_wait = {'healthcheck', 'backup-audit', 'teahouse-monitor'}

problems = []
for j in jobs:
    name = j.get('name')
    msg = (j.get('payload') or {}).get('message', '')
    if name in need_date and '跨日提醒' not in msg:
        problems.append(f"{name} 会写每日笔记但指令里没有【跨日提醒】")
    if name in need_wait and '等待方式' not in msg:
        problems.append(f"{name} 会跑脚本但指令里没有【等待方式】")

if problems:
    for p in problems:
        print(f"FAIL {p}")
    failed = 1
else:
    print(f"PASS cron 指令的防复发提醒齐全（{len(jobs)} 个任务已核对）")
print(f"__CR_FAILED__={failed}")
PY7
)
cr_fail=$(echo "$cr_report" | grep -oP '__CR_FAILED__=\K[0-9]+')
cr_fail=${cr_fail:-0}
while IFS= read -r line; do
  case "$line" in
    PASS*) ok "${line#PASS }" ;;
    FAIL*) bad "${line#FAIL }" ;;
  esac
done <<< "$cr_report"
PASS=$((PASS + 1))

echo "【S】连续性入口的「当前状态」是否过期"

# 2026-10-27 05:15 补：SESSION-STATE.md 头部的「📋 系统状态」小节是**手写的**，
# 里面有大量过期内容：已删除的 provider（glmcode/arkcode/kimicode）、
# 已删除的 cron（teahouse-check/dream-cycle-v3）、已移除的 plugin（qqbot/ddingtalk）、
# 「首次自动调度验证待 10-26」这种早就过期的待办。
#
# 而 SESSION-STATE.md 的头部是**下一个会话最先读的地方** —— 它过期 = 下一个会话被误导。
# 这比 L1 库里的过期计数更严重：那是数据，这是**指令**。
#
# 现在「系统状态」由 scripts/gen_system_status.py 从真实系统生成。
# 这里核对：**声明的内容与真实系统是否一致**。

st_report=$(python3 - << 'PY6'
import json, os, re, subprocess
WS = '/root/.openclaw/workspace'
STATE = f'{WS}/SESSION-STATE.md'
failed = 0

try:
    txt = open(STATE, encoding='utf-8').read()
except Exception as e:
    print(f"FAIL 读不到 SESSION-STATE.md（{type(e).__name__}）")
    raise SystemExit(1)

# 取「系统状态」小节（含生成标记的那段）
m = re.search(r'<!-- BEGIN:gen_system_status -->(.*?)<!-- END:gen_system_status -->',
              txt, re.S)
if not m:
    print("FAIL SESSION-STATE.md 的系统状态小节没有生成标记 —— "
          "跑 `python3 scripts/gen_system_status.py --update`")
    raise SystemExit(1)
sec = m.group(1)

problems = []

# 1) providers 与真实配置一致
try:
    c = json.load(open('/root/.openclaw/openclaw.json', encoding='utf-8'))
    real = set(c['models']['providers'].keys())
    mm = re.search(r'\*\*providers\*\*: ([^\n]+)', sec)
    if mm:
        decl = {x.strip() for x in mm.group(1).split(',')}
        if decl != real:
            problems.append(f"providers 声明 {sorted(decl)}，实际 {sorted(real)}")
except Exception:
    pass

# 2) cron 任务列表与真实一致
try:
    r = subprocess.run(['openclaw', 'cron', 'list', '--json'],
                       capture_output=True, text=True, timeout=60)
    jobs, _ = json.JSONDecoder().raw_decode(r.stdout[r.stdout.find('['):])
    real_names = {j['name'] for j in jobs}
    decl_names = set(re.findall(r'^- `([a-z0-9-]+)` —', sec, re.M))
    missing = real_names - decl_names
    extra = decl_names - real_names
    if missing:
        problems.append(f"漏了 cron 任务 {sorted(missing)}")
    if extra:
        problems.append(f"列了不存在的 cron 任务 {sorted(extra)}")
except Exception:
    pass

# 3) 不该再有已删除实体的残留提及
for gone in ('glmcode', 'arkcode', 'kimicode', 'teahouse-check', 'dream-cycle-v3',
             'qqbot', 'ddingtalk'):
    if gone in sec:
        problems.append(f"系统状态小节里还有已删除的「{gone}」")

# 4) 「巡检现状」行必须**唯一**（2026-10-27 08:20 加）
#    update_state_status.py 原来靠「找一行长得像活行」来定位，
#    但它自己又会把其它同名前缀的行改成「（快照）」——
#    活行找不到时 fallback 到 insert() -> **每次巡检多插一行**，
#    累积到 13 行才被发现。
#    现在改用 BEGIN/END 标记块；这里断言「标记块只有 1 个，
#    且块外的散落行为 0」-> 防止复发。
_b = txt.count('<!-- BEGIN:gen_healthcheck_status -->')
_e = txt.count('<!-- END:gen_healthcheck_status -->')
if _b != 1 or _e != 1:
    problems.append(f"巡检现状标记块数量异常（BEGIN={_b} END={_e}，应为各 1）")
else:
    _inblock = False
    _stray = 0
    for _ln in txt.split(chr(10)):
        if _ln.strip() == '<!-- BEGIN:gen_healthcheck_status -->':
            _inblock = True
            continue
        if _ln.strip() == '<!-- END:gen_healthcheck_status -->':
            _inblock = False
            continue
        if _inblock:
            continue
        if _ln.startswith('**巡检现状'):
            _stray += 1
    if _stray:
        problems.append(f"标记块外还有 {_stray} 行散落的「巡检现状」—— 每次巡检都在自增")

if problems:
    for p in problems:
        print(f"FAIL {p}")
    failed = 1
else:
    print("PASS 连续性入口的当前状态与真实系统一致")

print(f"__ST_FAILED__={failed}")
PY6
)
st_fail=$(echo "$st_report" | grep -oP '__ST_FAILED__=\K[0-9]+')
st_fail=${st_fail:-0}
while IFS= read -r line; do
  case "$line" in
    PASS*) ok "${line#PASS }" ;;
    FAIL*) bad "${line#FAIL }" ;;
  esac
done <<< "$st_report"
PASS=$((PASS + 1))

echo "【R】cron 任务「脚本耗时 vs 超时」对比（防把等待慢当成任务慢）"

# 2026-10-27 04:40 补（E35）：
# backup-audit 连续两次「超时失败」，但脚本只要 **0.18 秒**。
# 真根因不是任务慢，是 agent 用 process(poll, timeout=120000~300000)
# 每次**等满** timeout → 3 次 poll ≈ 510s → 必然撞上 job 超时。
#
# 所以这条断言的核心是**比值**：脚本耗时 vs timeout 差几个数量级，
# 就说明慢的不是任务，而是「怎么等」。调 timeout 是第二道防线，
# 第一道是「别等满」。
#
# 判据：
#   1) 脚本耗时 < 5s 但 job 因超时失败 → FAIL（等待方式的问题，不是任务的问题）
#   2) timeoutSeconds < 脚本耗时的 10 倍 → WARN（余量太小）

to_report=$(python3 - << 'PY5'
import json, os, subprocess, time
JOBS = '/root/.openclaw/cron/jobs.json'
WS = '/root/.openclaw/workspace'
failed = 0

# 脚本路径表：cron 任务名 → 脚本
SCRIPTS = {
    'backup-audit': 'scripts/backup_audit.sh',
    'healthcheck': 'scripts/healthcheck.sh',
}

try:
    d = json.load(open(JOBS, encoding='utf-8'))
    jobs = d if isinstance(d, list) else d.get('jobs', [])
except Exception as e:
    print(f"FAIL 读不到 jobs.json（{type(e).__name__}）")
    raise SystemExit(1)

# 1) 超时失败 + 脚本很快 → 典型「等法」问题
timed_out = []
for j in jobs:
    st = j.get('state') or {}
    if st.get('lastRunStatus') != 'error':
        continue
    if 'timed out' not in str(st.get('lastError') or ''):
        continue
    name = j.get('name')
    rel = SCRIPTS.get(name)
    if not rel:
        continue
    path = os.path.join(WS, rel)
    if not os.path.exists(path):
        continue
    t0 = time.time()
    try:
        subprocess.run(['bash', path], capture_output=True, timeout=120)
    except Exception:
        pass
    dur = time.time() - t0
    tos = (j.get('payload') or {}).get('timeoutSeconds') or 0
    if dur < 5:
        timed_out.append((name, dur, tos))

if timed_out:
    for name, dur, tos in timed_out:
        print(f"FAIL {name} 脚本只要 {dur:.2f}s 却因等待超时失败"
              f"（timeout {tos}s）—— 慢的是「等法」不是任务："
              f"用短 timeout 多次 poll，不要一次等满")
    failed = 1
else:
    print("PASS 无「脚本很快但超时失败」的任务")

# 2) timeout 余量对比
tos_map = {}
for j in jobs:
    p = j.get('payload') or {}
    if p.get('kind') == 'agentTurn':
        tos_map[j.get('name')] = p.get('timeoutSeconds') or 0
if tos_map:
    vals = sorted(tos_map.values())
    lo, hi = vals[0], vals[-1]
    if hi > 0 and lo < hi * 0.6:
        tight = [f"{k}={v}s" for k, v in tos_map.items() if v == lo]
        print(f"WARN cron 超时余量差异大（最小 {lo}s vs 最大 {hi}s）：{', '.join(tight)}"
              f" —— 手写的 timeout 彼此不可比")
    else:
        print(f"PASS cron 超时余量合理（{lo}s–{hi}s）")
print(f"__TO_FAILED__={failed}")
PY5
)
to_fail=$(echo "$to_report" | grep -oP '__TO_FAILED__=\K[0-9]+')
to_fail=${to_fail:-0}
while IFS= read -r line; do
  case "$line" in
    PASS*) ok "${line#PASS }" ;;
    FAIL*) bad "${line#FAIL }" ;;
    WARN*) warn "${line#WARN }" ;;
  esac
done <<< "$to_report"
PASS=$((PASS + 1))

echo "【U】cron 实测耗时 vs 超时余量（防明天早上撞超时）"

# 2026-10-27 07:10 补：
# 【R】组把各任务的 timeout **互相比较**（lo < hi*0.6），
# 但**从不跟真实耗时比** → 巡检本身长到 500s 也没人知道。
#
# 真实数据（07:00 无人值守那次）：
#   巡检 cron 耗时 298s / timeout 600s = **50% 余量**
#   其中脚本本身 ~156s，其余 ~140s 是 LLM 推理/嵌入调用的开销
#   而巡检一周前只要 25s → 一周长了 11 倍
# → 趋势本身就是风险：**没有任何断言看得见这个趋势**
#
# 判据（用真实运行历史，不重跑脚本 —— 重跑会自我指涉且很贵）：
#   1) 最近一次实测耗时 / timeout > 0.7 → FAIL（余量告急）
#   2) 同一任务最近 3 次耗时呈上升趋势且已 > 0.5 → WARN（在长）
#   3) 有任务最近失败且原因是超时 → FAIL

u_report=$(python3 - << 'PY8' 2>/dev/null
import json, subprocess, os, glob

failed = 0
warned = 0

def cron_jobs():
    p = '/root/.openclaw/cron/jobs.json'
    try:
        d = json.load(open(p, encoding='utf-8'))
        return d if isinstance(d, list) else d.get('jobs', [])
    except Exception:
        return []

jobs = cron_jobs()
tos = {}
for j in jobs:
    p = j.get('payload') or {}
    if p.get('kind') == 'agentTurn':
        tos[j.get('name')] = p.get('timeoutSeconds') or 0

# 从各任务的 session/运行记录里找实测耗时
def durations(name):
    """从 openclaw 的 cron 运行历史里取该任务的最近耗时(ms)"""
    out = []
    try:
        r = subprocess.run(['openclaw', 'cron', 'runs', '--limit', '8',
                            '--id', CRON_ID_FOR[name]],
                           capture_output=True, text=True, timeout=60)
        d = json.loads(r.stdout[r.stdout.find('{'):])
        for e in d.get('entries', []):
            dur = e.get('durationMs')
            ts = e.get('runAtMs') or e.get('ts') or 0
            if dur:
                out.append((ts, dur, e.get('status'),
                            e.get('lastDeliveryStatus') or e.get('deliveryStatus')))
    except Exception:
        pass
    return out

CRON_ID_FOR = {}
for j in jobs:
    CRON_ID_FOR[j.get('name')] = j.get('id')

problems = []
trend_warn = []
for name, tsec in tos.items():
    if tsec <= 0:
        continue
    ds = durations(name)
    if not ds:
        continue
    # ⚠️ 必须按**时间**排序，不能按耗时排序 ——
    # 按耗时排会把「最慢的那次」当成「最近的那次」，
    # 于是早已修好的 backup-audit（历史 600s 超时）会被当成现状 → 假 FAIL。
    ds.sort(key=lambda x: x[0])
    last = ds[-1]
    ratio = last[1] / 1000.0 / tsec
    if ratio > 0.7:
        problems.append(f"{name} 最近一次耗时 {last[1]/1000:.0f}s / 超时 {tsec}s "
                        f"= {ratio*100:.0f}% —— 余量告急，下次可能撞超时")
    # 趋势：最近 3 次是否持续上升
    if len(ds) >= 3:
        recent = [x[1] for x in ds[-3:]]
        if recent[0] < recent[1] < recent[2] and recent[2]/1000.0/tsec > 0.5:
            trend_warn.append(f"{name} 最近 3 次耗时递增 "
                              f"{recent[0]/1000:.0f}s→{recent[1]/1000:.0f}s→{recent[2]/1000:.0f}s"
                              f"（超时 {tsec}s）—— 在长")
    # 因超时而失败的（只看最近一次）
    ts, dur, st, dl = last
    if st == 'error' and dl and 'timeout' in str(dl).lower():
        problems.append(f"{name} 最近因超时失败（{dl}）—— 见 E35：慢的是等法不是任务")

for p in problems:
    print(f"FAIL {p}")
    failed = 1
for w in trend_warn:
    print(f"WARN {w}")
    warned = 1
if not problems and not trend_warn:
    print("PASS 所有 agentTurn 任务的实测耗时都留有余量")
print(f"__U_FAILED__={failed}")
print(f"__U_WARNED__={warned}")
PY8
)
u_fail=$(echo "$u_report" | grep -oP '__U_FAILED__=\K[0-9]+'); u_fail=${u_fail:-0}
u_warn=$(echo "$u_report" | grep -oP '__U_WARNED__=\K[0-9]+'); u_warn=${u_warn:-0}
while IFS= read -r line; do
  case "$line" in
    PASS*) ok "${line#PASS }" ;;
    FAIL*) bad "${line#FAIL }" ;;
    WARN*) warn "${line#WARN }" ;;
  esac
done <<< "$u_report"
PASS=$((PASS + 1))

echo "【Q】cron 指令是否与脚本同步（防过期手册）"

# 2026-10-27 03:30 补（E20/E31 的又一次实例）：
# cron 指令里手写的「13 组断言 A–M」在加了 N/O/P 三组后**没同步** ——
# 明早 07:00 真出故障时，agent 拿到的是一份**过期的手册**，
# 新组（N/O/P）的失败它根本不知道怎么解释。
#
# 这正是 E20「验证了脚本却没验证指令」的翻版。
# 解法：指令清单由 `scripts/gen_healthcheck_instruction.py` 从脚本生成，
# 这里**自动核对**生成结果与 cron 里的实际内容是否一致。

inst_report=$(python3 - << 'PY4' 2>/dev/null
import json, re, subprocess, sys
sys.path.insert(0, '/root/.openclaw/workspace/scripts')
try:
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "gen", "/root/.openclaw/workspace/scripts/gen_healthcheck_instruction.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    groups = gen.extract_groups()
    block = gen.build_block(groups)
except Exception as e:
    print(f"FAIL 生成器加载失败（{type(e).__name__}）")
    raise SystemExit(1)

try:
    r = subprocess.run(['openclaw', 'cron', 'list', '--json'],
                       capture_output=True, text=True, timeout=60)
    jobs, _ = json.JSONDecoder().raw_decode(r.stdout[r.stdout.find('['):])
    msg = None
    for j in jobs:
        if j.get('id') == gen.CRON_ID:
            msg = (j.get('payload') or {}).get('message', '')
            break
    if msg is None:
        print("FAIL 找不到 healthcheck cron 任务")
        raise SystemExit(1)
except Exception as e:
    print(f"FAIL 读不到 cron 指令（{type(e).__name__}）")
    raise SystemExit(1)

missing = [g for g, _ in groups if f'- {g} ' not in msg]
if missing:
    print(f"FAIL cron 指令缺 {len(missing)} 组说明（{', '.join(missing)}）"
          f" —— 跑 `python3 scripts/gen_healthcheck_instruction.py --update` 同步")
else:
    n_decl = len(re.findall(r'^- ([A-Z]) ', msg, re.M))
    if n_decl != len(groups):
        print(f"FAIL cron 指令列了 {n_decl} 组，脚本实际 {len(groups)} 组 —— 需同步")
    else:
        print(f"PASS cron 指令与脚本同步（{len(groups)} 组）")
PY4
)
while IFS= read -r line; do
  case "$line" in
    PASS*) ok "${line#PASS }" ;;
    FAIL*) bad "${line#FAIL }" ;;
  esac
done <<< "$inst_report"
PASS=$((PASS + 1))

echo "【P】每日笔记日期归属（跨日写入检测）"

# 2026-10-27 03:00 补：会话跨过零点后，我**继续往 10-26.md 里写**，
# 导致 10-26.md 里混着两天内容（72 个索引块里有 30 个属于 10-27）。
# 后果：
#   - 每日笔记的时间线失真（「10-26 的日志」里写着 10-27 的事）
#   - daily-learning「读最近 2 天日志」会读到错配的内容
#
# 判据设计（第一版是错的）：全文搜日期会**大量误报** ——
# 历史笔记里合法地引用更晚的日期（10-25 的补记里提到 10-26）。
# 正确做法：只看**小节标题**里的日期，且必须晚于文件名。
# → 7 个历史文件零误报，用拆分前的备份能精确抓到 5 个越界小节。

date_report=$(python3 - << 'PY3' 2>/dev/null
import glob, os, re
WS = '/root/.openclaw/workspace'
bad_files = []
for f in sorted(glob.glob(f'{WS}/memory/2026-*.md')):
    base = os.path.basename(f)[:-3]          # 2026-10-26
    try:
        txt = open(f, encoding='utf-8', errors='replace').read()
    except Exception:
        continue
    offenders = []
    # 只看标题行（## 或 ###）里的日期
    for m in re.finditer(r'^#{2,3} .*?(20\d\d-\d\d-\d\d)', txt, re.M):
        d = m.group(1)
        if d > base:
            offenders.append(d)
    if offenders:
        bad_files.append((os.path.basename(f), sorted(set(offenders))))

if bad_files:
    for name, dates in bad_files:
        print(f"FAIL {name} 里的小节日期晚于文件名（{', '.join(dates)}）"
              f" —— 跨日后应写入新日期的文件")
else:
    print("PASS 每日笔记日期归属正确（无跨日写入）")
PY3
)
while IFS= read -r line; do
  case "$line" in
    PASS*) ok "${line#PASS }" ;;
    FAIL*) bad "${line#FAIL }" ;;
  esac
done <<< "$date_report"
PASS=$((PASS + 1))

echo "【O】L1 计数一致性（防止手改文档后过期）"

# 2026-10-27 补：MEMORY.md / L1 主库头部的「N 条」是**手写的**，
# 加了 E30/E31 后就忘了同步 → MEMORY.md 还写着 29 条，实际已经 31 条。
# 计数过期本身不致命，但**它是我判断「记忆系统是否正常」的参照** ——
# 参照错了，判断就错了。所以加断言自动核对。

count_report=$(python3 - << 'PY2' 2>/dev/null
import glob, re, os
WS = '/root/.openclaw/workspace'
ENT = f'{WS}/memory/L1-entries'
LIB = f'{WS}/memory/L1-experience-library.md'
MEM = f'{WS}/MEMORY.md'

files = sorted(glob.glob(f'{ENT}/E*.md'))
n_files = len(files)
if not n_files:
    print("FAIL 找不到任何 L1 独立文件")
    raise SystemExit(1)

# 真实最大编号
nums = []
for f in files:
    m = re.search(r'E(\d+)\.md$', os.path.basename(f))
    if m:
        nums.append(int(m.group(1)))
real_max = max(nums)

# 库里的条目数（按 ### E<N> 标题算）
try:
    lib = open(LIB, encoding='utf-8').read()
    lib_ids = set(int(x) for x in re.findall(r'^### E(\d+)', lib, re.M))
    lib_n = len(lib_ids)
except Exception:
    lib_n = 0

problems = []
if lib_n != n_files:
    problems.append(f"主库 {lib_n} 条 vs 独立文件 {n_files} 个")

# MEMORY.md 里的声明
try:
    mem = open(MEM, encoding='utf-8').read()
    m1 = re.search(r'（(\d+) 条，E1–E(\d+)）', mem)
    m2 = re.search(r'检索自检（必须 (\d+)/\d+）', mem)
    if m1:
        if int(m1.group(1)) != n_files or int(m1.group(2)) != real_max:
            problems.append(f"MEMORY.md 声明 {m1.group(1)} 条/E1–E{m1.group(2)}，实际 {n_files} 条/E1–E{real_max}")
    if m2:
        if int(m2.group(1)) != n_files:
            problems.append(f"MEMORY.md 自检声明 {m2.group(1)}/{m2.group(1)}，实际应为 {n_files}/{n_files}")
    # L1 主库头部
    h = re.search(r'当前条目数\*\*：(\d+) 条（E1–E(\d+)）', lib)
    if h:
        if int(h.group(1)) != n_files or int(h.group(2)) != real_max:
            problems.append(f"L1 主库头部声明 {h.group(1)} 条/E1–E{h.group(2)}，实际 {n_files} 条/E1–E{real_max}")
except Exception as e:
    problems.append(f"读 MEMORY.md 失败: {type(e).__name__}")

if problems:
    for p in problems:
        print(f"FAIL {p}")
else:
    print(f"PASS L1 计数一致（{n_files} 条，E1–E{real_max}）")
PY2
)
while IFS= read -r line; do
  case "$line" in
    PASS*) ok "${line#PASS }" ;;
    FAIL*) bad "${line#FAIL }" ;;
  esac
done <<< "$count_report"
PASS=$((PASS + 1))

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

echo "【M】Provider 韧性与 fallback 有效性"

# 2026-10-26 实测：19:11 和 19:42 两次巡检 FAIL，根因是 MiniMax
# 「已达到 Token Plan 用量上限」(HTTP 429)。查 failover 日志时发现
# 配的 fallback 是 minimax/MiniMax-M2.7-highspeed + minimax/MiniMax-M2.7 ——
# **同一个 provider、同一个账号**。配额一满，三个模型一起挂。
# 也就是说 fallback 链在「provider 级故障」面前提供的韧性是 0。
#
# 状态字段看不出这个问题：配置齐全、模型存在、fallbackConfigured=True，
# 但只要 provider 本身欠费/限流，兜底就是假的。
# 这属于 E20「验证的对象错了」—— 该验的是「跨 provider 吗」，不是「配了吗」。
#
# 2026-10-27 06:40 补充：除了「兜底无效」，还有**第二重后果** ——
# **并发互相拖慢**。实测（同账号 6 个并发请求，256 token 输出）：
#   单个请求        → 1.8s
#   6 个并发        → 最慢的到 **7.0s（3.9 倍）**
# 而实测到 daily-learning 的 agent turn 有一次**单次 LLM 调用等了 353 秒**，
# 且那段时间正好是 backup-audit 超时失败的时段 —— **同一个 MiniMax 账号被并发占用**。
# → 也就是说：不仅 provider 挂了会全挂，**provider 变慢也会全慢**，
#   一个慢请求会拖垮所有任务（包括每天 07:00 的巡检）。

prov_report=$(python3 - << 'PY2' 2>/dev/null
import json
try:
    c = json.load(open('/root/.openclaw/openclaw.json', encoding='utf-8'))
except Exception as e:
    print(f"FAIL 读不到 openclaw.json（{type(e).__name__}）")
    raise SystemExit(1)

providers = list(c.get('models', {}).get('providers', {}).keys())
fallbacks = c.get('agents', {}).get('defaults', {}).get('model', {}).get('fallbacks', []) or []

print(f"PASS 已配置 provider: {len(providers)} 个 → {', '.join(providers) if providers else '无'}")
print(f"PASS fallback 链: {len(fallbacks)} 项 → {', '.join(fallbacks) if fallbacks else '未配置'}")

if not fallbacks:
    print("WARN 未配置任何 fallback —— 主模型不可用时没有任何兜底")
elif len(providers) <= 1:
    print(f"FAIL 只有 1 个 provider，但配了 {len(fallbacks)} 项 fallback —— "
          f"它们全在同一个 provider 同一账号下，provider 级故障（欠费/限流/宕机）时兜底无效")
else:
    fb_providers = {f.split('/')[0] for f in fallbacks if '/' in f}
    if fb_providers - set(providers):
        print(f"WARN fallback 指向未配置的 provider: {', '.join(sorted(fb_providers - set(providers)))}")
    elif len(fb_providers) == 1 and len(providers) > 1:
        print(f"WARN 有 {len(providers)} 个 provider，但 fallback 全在 {list(fb_providers)[0]} —— "
              f"未跨 provider 兜底")
    else:
        print(f"PASS fallback 跨 provider 兜底（{', '.join(sorted(fb_providers))}）")
PY2
)
while IFS= read -r ln; do
  case "$ln" in
    PASS*) ok "${ln#PASS }" ;;
    FAIL*) bad "${ln#FAIL }" ;;
    WARN*) warn "${ln#WARN }" ;;
    *) [ -n "$ln" ] && echo "  $ln" ;;
  esac
done <<< "$prov_report"

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
  actionable_n=0
  for it in "${FAILED_ITEMS[@]}"; do is_waiting "$it" || actionable_n=$((actionable_n+1)); done
  waiting_n=${#WAITING_ITEMS[@]}
  if [ "$actionable_n" -gt 0 ]; then
    echo " ⚠️ 有 $actionable_n 项可处理的失败，需要处理。"
  else
    echo " ✅ 没有可处理的失败（$waiting_n 项长期项在等你处理）。"
  fi
  echo "   【可处理 — 现在就该修】"
  for it in "${FAILED_ITEMS[@]}"; do
    is_waiting "$it" || echo "   - $it"
  done
  if [ ${#WAITING_ITEMS[@]} -gt 0 ]; then
    echo "   【等牛哥哥 — 长期项，不该自己动】"
    for it in "${WAITING_ITEMS[@]}"; do echo "   - $it"; done
  fi
  echo ""
  echo " 提示：静默失败优先排查「触发→执行→落盘→投递」每一段，别只看状态字段。"
fi
echo "════════════════════════════════════════════"

[ "$FAIL" -gt 0 ] && exit 1
exit 0
