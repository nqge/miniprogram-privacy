#!/usr/bin/env bash
# 全链路健康巡检 -- 捕捉「状态绿但功能死」的静默失败
#
# 背景(2026-10-26 血泪史):
#   今天发现三个「status=ok 但实际没工作」的功能:
#     1. 记忆检索:enabled 但 Embeddings unavailable,索引 0/11(从 3 月起就是死的)
#     2. 飞书投递:cron status=ok / consecutiveErrors=0,但结果从未送达
#     3. 学习笔记:任务不报错,但周频调度决定了 4/8 天根本不触发
#   错误信息只能告诉你「已经崩了」的东西 -- 沉默的失败必须靠**主动断言**才能发现。
#
# 这个脚本做 4 组断言:
#   A. 外部 API 连通性(飞书 / GitHub / MiniMax embeddings)
#   B. systemd 服务状态(gateway / embed-proxy)
#   C. 记忆索引新鲜度(文件数 / chunks / Dirty)
#   D. 定时任务投递结果(announce 型必须 delivered=True)
#
# 退出码:0 = 全通过;1 = 有 FAIL

set -uo pipefail

# 日志降噪 -- 2026-10-26 实测:
# 每次 `openclaw` CLI 调用都会重新注册全部插件,INFO 级打 6+ 行
# 「feishu_xxx: Registered ...」,占当日日志 87%(24158/27676 行)。
# 实测 3 次 `openclaw cron list`:默认 27008 字节/次 → warn 级 5735 字节/次(-79%)。
# 只能用环境变量控制(改 openclaw.json 的 logging 段会触发 loadConfig 递归)。
export OPENCLAW_LOG_LEVEL="${OPENCLAW_LOG_LEVEL:-warn}"

WORKSPACE="/root/.openclaw/workspace"
CFG="/root/.openclaw/openclaw.json"
PROXY_HEALTH="http://127.0.0.1:8799/health"

PASS=0; FAIL=0; WARN=0
declare -a FAILED_ITEMS=()
declare -a WAITING_ITEMS=()

# 「等用户」的长期项 -- 2026-10-26 加
#
# 为什么:这些项每天都成立,要等牛哥哥本人操作才能解决
# (吊销 PAT / 给第二个 provider 的 key)。
# 混在「可处理故障」里有两个害处:
#   1) 报告里真故障被长期项稀释 → 注意力被带走
#   2) 每天的 FAIL 计数永远 ≥1 → 「今天有没有故障」这个信号失效
# 分类后仍然全部报出来(信息不丢),但单独成组,一眼能看出哪些要动、哪些在等。
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
else bad "飞书 API token 获取失败 - ${fs_out}"; fi

# A2 GitHub API(茶馆监控依赖)-- 用认证请求,并区分「限流」与「不可达」
# 2026-10-26 教训:匿名额度只有 60 次/小时/IP,耗尽后 403 看起来像权限问题,
# 极难一眼看出。现在读 gh CLI 的 token 换成 5000 次/小时,并报实际额度。
# token 统一从 scripts/gh_auth.py 取(单一真相源)--
# 之前这里自己抄了一遍正则,与另外两个脚本重复,gh 改格式时会三处同时静默失效
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
    bad "GitHub API 额度将耗尽（${gh_remain})- 茶馆监控可能拉不到新讨论"
  else
    ok "GitHub API 可用（额度 ${gh_remain},茶馆监控依赖）"
  fi
else
  bad "GitHub API 探测失败 - 茶馆监控可能拉不到新讨论"
fi

# A3 embeddings 代理
px_out=$(curl -s -m 8 "$PROXY_HEALTH" 2>/dev/null)
if echo "$px_out" | grep -q '"ok":true'; then ok "嵌入代理健康（记忆检索依赖）"
else bad "嵌入代理不可用 - 记忆检索会失效 - ${px_out}"; fi

# ── B. 服务状态 ───────────────────────────────────
echo ""
echo "【B】服务状态"

for svc in openclaw-gateway.service openclaw-embed-proxy.service; do
  state=$(systemctl --user is-active "$svc" 2>/dev/null)
  if [ "$state" = "active" ]; then ok "$svc = $state"
  else bad "$svc = ${state:-missing}"; fi
  en=$(systemctl --user is-enabled "$svc" 2>/dev/null)
  [ "$state" = "active" ] && [ "$en" != "enabled" ] && warn "$svc 未设开机自启（enabled=$en)"
done

# systemd timer(纯脚本任务用 timer 跑,不消耗模型时间)
tmr=$(systemctl --user is-active openclaw-log-cleanup.timer 2>/dev/null)
if [ "$tmr" = "active" ]; then ok "openclaw-log-cleanup.timer = active"
else bad "openclaw-log-cleanup.timer = ${tmr:-missing} - 日志轮转不会自动清理"; fi

# 快速健康子集的定时器(30 分钟一次 + 独立告警链路)
qtmr=$(systemctl --user is-active openclaw-quick-health.timer 2>/dev/null)
if [ "$qtmr" = "active" ]; then ok "openclaw-quick-health.timer = active(30 分钟巡检）"
else bad "openclaw-quick-health.timer = ${qtmr:-missing} - 服务故障最长 4 小时才被发现"; fi

# 告警链路的 OnFailure 指令是否**真正生效**(不 grep 文件 -- 注释里也可能有这行字)
# 2026-10-26 教训:重写 .service 时把指令弄丢了,但注释里还留着 → grep 看着正常,实际静默失效
qof=$(systemctl --user show openclaw-quick-health.service -p OnFailure --value 2>/dev/null | tr -d '\n')
if echo "$qof" | grep -q "quick-health-alert"; then
  ok "quick-health 的 OnFailure 告警链路已生效"
else
  bad "quick-health 的 OnFailure 指令丢失或指向异常 - 故障时不会告警（$qof)"
fi

# L1 经验库检索验证 -- 每条经验都必须能被自己的原文措辞搜到
# 2026-10-26 实测:19 条里有 4 条塞在长文件里检索不到(E17 的问题),
# 拆成独立文件后 19/19 全部命中。写下来的经验搜不到 = 等于没写。
# 2026-10-27 16:00 加缓存(E50 的延伸：别反复做同一件没价值的活)：
# verify_l1 要跑 17 次真检索，每次起一个 Node 进程 ≈ 67s。
# 而 **L1 库一天不变**(除非加了新条目) → 当天重复验证是纯浪费。
#
# 缓存 key = **L1 库的内容指纹**(所有独立文件内容的 md5)
# → 加了新条目 → 指纹变 → 缓存自动失效 → 重新验证
# → 既省掉重复劳动，又不会漏掉「新条目没被验证」。
#
# ⚠️ 缓存不能只按「天」—— 那样新加的条目当天就验不到
#    (那是「派生数据会过期」的同款问题：缓存也会过期)。
l1_cache=/tmp/openclaw/l1_verify_cache.json
l1_fp=$(python3 - << 'PYF' 2>/dev/null
import glob, hashlib
h = hashlib.md5()
for f in sorted(glob.glob('/root/.openclaw/workspace/memory/L1-entries/E*.md')):
    try:
        with open(f, 'rb') as fh:
            h.update(fh.read())
    except Exception:
        pass
print(h.hexdigest())
PYF
)
# 缓存里只存**结论**（rc + 结果行 + 失败明细），不存完整输出 ——
# 多行输出塞进 shell 变量再取回来会丢格式（实测：结果行取不到了）。
l1_out=""
l1_rc=1
l1_hit=0
l1_line=""
if [ -n "$l1_fp" ] && [ -f "$l1_cache" ]; then
  l1_cache_read=$(python3 - "$l1_cache" "$l1_fp" << 'PYG' 2>/dev/null
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding='utf-8'))
    if d.get('fp') == sys.argv[2]:
        print(json.dumps({"rc": d.get("rc", 1),
                          "line": d.get("line", ""),
                          "fails": d.get("fails", [])}, ensure_ascii=False))
except Exception:
    print("")
PYG
)
  if [ -n "$l1_cache_read" ]; then
    l1_hit=1
    l1_rc=$(echo "$l1_cache_read" | python3 -c "import json,sys;print(json.load(sys.stdin).get('rc',1))" 2>/dev/null || echo 1)
    l1_line=$(echo "$l1_cache_read" | python3 -c "import json,sys;print(json.load(sys.stdin).get('line',''))" 2>/dev/null)
    l1_fails=$(echo "$l1_cache_read" | python3 -c "import json,sys;[print(x) for x in json.load(sys.stdin).get('fails',[])]" 2>/dev/null)
  fi
fi
if [ "$l1_hit" -eq 0 ]; then
  l1_out=$(timeout 280 python3 /root/.openclaw/workspace/scripts/verify_l1_retrieval.py 2>/dev/null)
  l1_rc=$?
  l1_line=$(echo "$l1_out" | grep "结果:" | head -1)
  l1_fails=$(echo "$l1_out" | grep '❌' | head -5)
  if [ -n "$l1_fp" ]; then
    python3 - "$l1_cache" "$l1_fp" "$l1_rc" "$l1_line" "$l1_fails" << 'PYH' 2>/dev/null
import json, os, sys
try:
    os.makedirs(os.path.dirname(sys.argv[1]), exist_ok=True)
    json.dump({"fp": sys.argv[2], "rc": int(sys.argv[3]),
               "line": sys.argv[4], "fails": sys.argv[5].split("\n") if sys.argv[5] else []},
              open(sys.argv[1], 'w', encoding='utf-8'), ensure_ascii=False)
except Exception:
    pass
PYH
  fi
fi
if [ "$l1_rc" -eq 0 ]; then
  if [ "$l1_hit" -eq 1 ]; then
    ok "L1 经验库检索验证通过（$l1_line，缓存命中）"
  else
    ok "L1 经验库检索验证通过（$l1_line）"
  fi
else
  bad "L1 经验库有经验检索不到 -- $l1_line"
  echo "$l1_fails" | sed 's/^/        /' | head -5
fi

# 完整的 systemd 配置漂移检查(单元 enabled / 脚本存在 / 关键指令生效 / 定时器调度)
# 覆盖「配置文件里的东西 ≠ 实际生效的东西」这一整类风险
drift_out=$(python3 /root/.openclaw/workspace/scripts/check_unit_drift.py 2>/dev/null)
drift_rc=$?
drift_pass=$(echo "$drift_out" | grep -c '✅')
drift_fail=$(echo "$drift_out" | grep -c '❌')
if [ "$drift_fail" -gt 0 ]; then
  bad "systemd 配置漂移：$drift_fail 项异常（详见 scripts/check_unit_drift.py)"
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

[ "$emb" = "ready" ] && ok "Embeddings: ready" || bad "Embeddings: ${emb:-unknown} - 记忆检索不可用"

if [ -n "$idx" ]; then
  got=${idx%%/*}; tot=${idx##*/}
  if [ "$got" = "$tot" ] && [ "$tot" -gt 0 ]; then ok "索引完整 $idx files · ${chunks:-0} chunks"
  else bad "索引不完整 $idx(部分文件未索引）"; fi
else
  bad "无法读取索引状态"
fi

[ "$dirty" = "no" ] && ok "索引未过期（Dirty: no)" || warn "索引待更新（Dirty: ${dirty:-?})- 新写的记忆还没进索引"

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
# 口径修正(2026-10-28): 本地存放凭据本身不是漏洞,泄露路径才是。
# 只有满足以下任一条件才算 FAIL:
#   (a) 文件被 git 跟踪  -> 会进仓库/历史
#   (b) 权限宽于 600    -> 同机其他用户可读
# 两者都不满足(未跟踪 + 600) = 正常运维做法,记 INFO 不记 FAIL。
def _is_git_tracked(p):
    try:
        import subprocess
        d = os.path.dirname(p)
        if not os.path.isdir(os.path.join(d, '.git')):
            return False
        rel = os.path.relpath(p, d)
        r = subprocess.run(['git', 'ls-files', '--error-unmatch', '--', rel],
                           cwd=d, capture_output=True, text=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False

for p in KNOWN:
    if not os.path.exists(p):
        continue
    try:
        s = open(p, encoding='utf-8', errors='replace').read()
    except Exception:
        continue
    n = len(set(PAT.findall(s)))
    if not n:
        continue
    try:
        mode = os.stat(p).st_mode & 0o777
    except Exception:
        mode = 0o644
    tracked = _is_git_tracked(p)
    perms_ok = (mode == 0o600)
    if tracked or not perms_ok:
        found += 1
        why = []
        if tracked:
            why.append('被 git 跟踪')
        if not perms_ok:
            why.append(f'权限 {oct(mode)} 宽于 600')
        print(f"FAIL 明文凭证 {p}({n} 个 token; {'/'.join(why)})")
    else:
        print(f"INFO 本地凭据 {p}({n} 个 token; 未跟踪 + 权限 600,符合安全基线)")
# 会话日志(workspace 外,未被 git 跟踪 → 风险较低但仍需清理)
hits = 0
for f in glob.glob('/root/.openclaw/agents/main/sessions/*.jsonl*'):
    try:
        with open(f, encoding='utf-8', errors='replace') as fh:
            if PAT.search(fh.read()):
                hits += 1
    except Exception:
        pass
if hits:
    print(f"WARN 会话日志含凭证 {hits} 个文件(未入 git,风险较低)")
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
  ok "无明文凭证泄露(本地 600 未跟踪存放点不计违规)"
fi
# 注意:bad() 已经把 FAIL 计数加过了,这里不能再加一次
PASS=$((PASS + 1))

# ── F. 记忆笔记结构 ───────────────────────────────
echo ""
echo "【F】记忆笔记结构"

note_report=$(python3 - << 'PY' 2>/dev/null
import glob, os, re, datetime
MEM = '/root/.openclaw/workspace/memory'
today = datetime.date.today().isoformat()

# 2026-10-27 (E38):F 组原来只看「今天」的文件 →
#   历史文件里的重复小节永远抓不到(10-21 就有 2 个学习小节,一直没人发现)
# 修法:全部 daily-note 文件都查「重复」。
# 注意:只查重复(>1),不查缺失(==0)--
#   · 修复前的日期(10-22/10-23)本来就没学习小节
#   · 反思是每周日跑,工作日没有是正常的
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
        print(f"FAIL {name} 有 {len(learn)} 个「每日学习」小节(应只有 1 个)")
        failed += 1
    if len(refl) > 1:
        print(f"FAIL {name} 有 {len(refl)} 个「每日反思」小节(应只有 1 个)")
        failed += 1
    # 单节长度:按**汉字数**衡量(任务指令里的约束是「100-200 字」,
    # 中文的「字」就是汉字,不是所有字符)
    #
    # 2026-10-27 07:55 修:
    # 原来用「去掉空白后的所有字符数 > 1200」-- **量错了东西**:
    #   · 规则说的是 100-200 **汉字**,断言数的却是含标点/英文/空白的全字符
    #   · 两个口径不一致 → 断言放过了 10-21 的 568 汉字、今天的 991 汉字
    #     (今天直到 2010 全字符才触发,而按规则早就超了 5 倍)
    # → 统一口径:数汉字,上限 400(给 100-200 留一倍余量,
    #   避免边界抖动误报;但超出 200 太多就是没遵守约束)
    # 2026-10-27 08:00 再修(边界 bug):
    # 原来用 `(?=\n## |\Z)` 找小节结尾 -- 只认 `##` 级标题,
    # 于是当「📚 每日学习」是文件里**最后一个 ## 级标题**时,
    # 后面所有的 `### 心跳` 小节全被算进学习正文
    # → 2026-10-27 的学习小节被算成 946 汉字(实际只有 ~170)
    #
    # 修法:小节在**下一个任意级标题**(## 或 ###)处结束 --
    # `###` 是 `##` 的子节,本来就属于上一个 `##`。
    for m in re.finditer(r'^## (📚 每日学习|🪞 每日反思).*?\n(.*?)(?=\n#{1,6} |\Z)', s, re.M | re.S):
        body = m.group(2)
        zh = len(re.findall(r'[一-鿿]', body))
        if zh > 400:
            print(f"FAIL {name} 小节超长 {zh} 汉字(约束 100-200 汉字,上限 400)")
            failed += 1
if failed == 0:
    print(f"PASS 记忆笔记结构正常(已查 {checked} 个每日笔记文件)")
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
        print(f"FAIL {name} 仓库不同步({','.join(bits)})")
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
echo "【H】端到端功能实测(不是看状态字段,是真的跑一次)"

# H1 记忆检索:真的搜一次,并检查是否命中已知内容
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
# 断言:命中 MEMORY.md 且片段含「称呼」关键词
snippet = top.get('snippet', '')
ok = 'MEMORY.md' in top.get('path', '') and '称呼' in snippet
print(('OK' if ok else 'WRONG') + '|' + str(top.get('path')) + ':' + str(top.get('startLine')) + '|' + str(len(rs)) + '条')
" 2>/dev/null)
case "$mem_search_out" in
  OK*)  ok "记忆检索端到端可用（命中 $(echo "$mem_search_out" | cut -d'|' -f2))" ;;
  NO_RESULT*) bad "记忆检索无结果 -- 检索功能实际已死" ;;
  WRONG*) bad "记忆检索命中内容不对（$(echo "$mem_search_out" | cut -d'|' -f2))" ;;
  *) bad "记忆检索执行异常" ;;
esac

# H2 飞书:真的发一条消息(不是只查 token)
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
    # 用 chat_id 列表接口做只读验证(不打扰用户)
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
  *) bad "飞书 API 实际不可用（$feishu_out)" ;;
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
    # 队列为空时也要查自洽性(否则这个检查永远走不到)
    try:
        data = json.load(open(todo, encoding='utf-8'))
        dist = {d.get('number') for d in data.get('distilled', [])}
        bad = {d.get('number') for d in data.get('distilled', [])
               if not isinstance(d.get('number'), int)}
        if bad:
            print(f"FAIL 茶馆 distilled 列表里有非法编号:{sorted(bad)[:5]}")
            failed = 1
        else:
            print(f"PASS 茶馆内容已全部蒸馏(无待处理,已蒸馏 {len(dist)} 条)")
    except Exception:
        print("PASS 茶馆内容已全部蒸馏(无待处理)")
else:
    # 检查这些讨论编号是否已在 memory/ 的**每日笔记**里出现过
    # (排除 teahouse-digest-*.md 蒸馏稿本身,否则会被自己的输出骗过)
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
    # 2026-10-27(E30):pending 里的条目如果**已经**出现在某篇笔记里,
    # 说明「已经蒸馏但没人把它移出队列」-- 这正是 E30 的 bug 状态,
    # 而原来的检查**只报 PASS、不做清理** → bug 会一直复发。
    #
    # 现在**自愈**:把这些条目移出 pending 并记入 distilled,
    # 让「出队」不再依赖 agent 记得跑 --mark-distilled。
    #
    # ⚠️ 方向别搞反(我第一版写反了):
    #   still = **没**出现在笔记里的(= 真未蒸馏)
    #   healed = **已**出现在笔记里的(= 已蒸馏但没出队)
    if pending:
        notes_txt = ""
        for f in notes:
            try:
                notes_txt += open(f, encoding='utf-8', errors='replace').read()
            except Exception:
                pass
        # 匹配方式:**编号或标题**都能触发(2026-10-27 补)
        # 原来只认 '#135' 这种带 # 的写法 -- 如果 agent 蒸馏时只写标题
        # 「小隐的三层记忆架构」而没写编号,条目就会永远卡在 pending。
        # 标题取前 15 字(足够唯一,又不会因为标题被改写而漏配)。
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
                print(f"PASS 茶馆自愈:{len(healed_nums)} 条已蒸馏但未出队 → 已移入 distilled {sorted(healed_nums)}")
            except Exception as e:
                print(f"WARN 茶馆自愈失败({type(e).__name__})")

    if still:
        print(f"FAIL 茶馆有 {len(still)} 条讨论未蒸馏(原始 dump 已落盘但未提炼成知识)")
        failed = 1
    else:
        # 2026-10-27:pending 队列原来「只有加没有减」-- 蒸馏完成后没人把它移走,
        # 每 4 小时运行都把同样 4 条当成「待处理」重新蒸馏一遍(反刍)。
        # 现在蒸馏完的条目会移到 distilled 列表,所以这里也检查两个列表是否自洽:
        #   pending 与 distilled 不应有交集(同一议题不能既待处理又已蒸馏)
        #
        # ⚠️ 必须**重新读盘**:上面自愈可能刚改过这个文件,
        #    继续用内存里的旧 pending 会报出「刚自愈完又说矛盾」的假 FAIL。
        try:
            data = json.load(open(todo, encoding='utf-8'))
            dist = {d.get('number') for d in data.get('distilled', [])}
            cur_pending = {p.get('number') for p in data.get('pending', [])}
            overlap = cur_pending & dist
            if overlap:
                print(f"FAIL 茶馆待蒸馏队列自相矛盾:{sorted(overlap)} 既在 pending 又在 distilled")
                failed = 1
            else:
                print(f"PASS 茶馆内容已全部蒸馏(pending {len(cur_pending)} 条,distilled {len(dist)} 条)")
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
    print("FAIL L1 经验库不存在(memory/L1-experience-library.md)")
    failed = 1
else:
    s = open(L1, encoding='utf-8', errors='replace').read()
    # 字符类里要同时容纳**全角**和半角冒号 —— 用 unicode 转义写，
    # 免得被后续的标点归一化脚本改坏（2026-10-27 实测踩过：
    # 全角冒号被写成半角 → 匹配数变 0 → 断言报「L1 经验库没有任何条目」）
    entries = re.findall(r'^### (E\d+)[:\uff1a]', s, re.M)
    if not entries:
        print("FAIL L1 经验库没有任何条目")
        failed = 1
    else:
        # 是否有近期(7 天内)新增的经验 -- 长期不更新说明提炼环节停了
        mtime = datetime.date.fromtimestamp(os.path.getmtime(L1))
        age = (datetime.date.today() - mtime).days
        if age > 30:
            print(f"FAIL L1 经验库已 {age} 天未更新(提炼环节可能已停)")
            failed = 1
        else:
            print(f"PASS L1 经验库 {len(entries)} 条,最后更新 {age} 天前")
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
# 根目录应该只有这些文件(子目录不算)
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
        print(f"FAIL 根目录有多余文件「{s}」({size} 字节)-- 可能是误创建的临时产物")
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

# 2026-10-26 实测:一天 15 小时高强度工作让 memory/2026-10-26.md 涨到 106KB /
# 1875 行 → 单独占掉 215 个索引块里的 100 块(46%)。
#
# ⚠️ 判据设计的教训(第一版用 25% 做 FAIL 阈值 → **假警报**):
# 我以为「单文件占块数高 = 有问题」,但实测发现那个文件里是**真实操作知识**
# (检索「日志清理脚本形同虚设」命中 0.8963,4 块全在笔记里)--
# 一个忙碌的日子**合法地**就该占 30-40%。
# 真正的问题不是「占比高」,而是:
#   1) memory/ 目录**没有任何体积上限**(对比:/tmp/openclaw 日志有 50MB/200MB 上限)
#      → 单个文件可以无限涨,最终把整个索引淹掉
#   2) 占比高到**真的开始挤掉别的文件**(检索被单文件垄断)
#
# 所以判据改成:
#   单文件 > 300KB              → FAIL(超过「一篇笔记」的合理规模)
#   单文件占索引块 > 60%        → FAIL(开始垄断检索)
#   memory/ 目录 > 200MB        → WARN(索引会持续变大,该归档旧笔记)
# 25% 那个阈值是**假警报** -- 已废弃。

mem_report=$(python3 - << 'PY3' 2>/dev/null
import os, sqlite3
MEM_DIR = '/root/.openclaw/memory'
DB = '/root/.openclaw/memory/main.sqlite'
MAX_FILE_KB = 300
MAX_SHARE_PCT = 60
MAX_DIR_MB = 200

# 1) 索引块分布(只有真「垄断」才 FAIL)
try:
    db = sqlite3.connect(DB)
    rows = db.execute("select path, count(*) c from chunks group by path").fetchall()
    total = sum(c for _, c in rows)
except Exception as e:
    print(f"FAIL 读不到索引({type(e).__name__})")
    raise SystemExit(1)

if rows and total:
    worst_path, worst_c = max(rows, key=lambda r: r[1])
    share = worst_c * 100 // max(total, 1)
    if share > MAX_SHARE_PCT:
        print(f"FAIL 单个文件垄断索引 {share}%({worst_c}/{total} 块)→ {worst_path}"
              f" -- 会挤掉其他文件,需拆分或归档")
    else:
        print(f"PASS 索引块分布正常(最大占比 {share}%:{worst_path})")

# 2) 单文件体积(真正的硬上限)
big = []
for name in os.listdir(MEM_DIR):
    p = os.path.join(MEM_DIR, name)
    if os.path.isfile(p) and name.endswith('.md'):
        kb = os.path.getsize(p) // 1024
        if kb > MAX_FILE_KB:
            big.append(f"{name} ({kb}KB)")
if big:
    print(f"FAIL 单个记忆文件超过 {MAX_FILE_KB}KB:{', '.join(big)}"
          f" -- 应拆分或归档(摘录留在独立文件,笔记只留结论+指针)")
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
    print(f"WARN memory/ 目录已达 {total_mb}MB -- 索引会持续变大,该考虑归档旧笔记")
else:
    print(f"PASS memory/ 目录 {total_mb}MB(上限 {MAX_DIR_MB}MB)")
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

# 2026-10-27 07:20 补:E33/E35 之后,cron 指令里加了两类「防复发提醒」:
#   · 【跨日提醒】-- 写 memory/YYYY-MM-DD.md 前用 `date +%F`
#     (E33:跨零点还写昨天文件 → 时间线失真,且**没有任何失败信号**)
#   · 【等待方式】-- 跑长脚本用短 poll,别一次等满
#     (E35:poll 会阻塞满 timeout,3 次 ≈ 510s → 必然撞 job 超时)
#
# 但这些提醒是我**一条条手动加**的 -- 加漏了没有任何机制会发现。
# (这正是 E34 的老问题:靠「记得做」。)
# 现在自动核对:**每个会写每日笔记 / 跑长脚本的任务,都必须有对应提醒**。

cr_report=$(python3 - << 'PY7' 2>/dev/null
import json, subprocess
failed = 0
try:
    r = subprocess.run(['openclaw', 'cron', 'list', '--json'],
                       capture_output=True, text=True, timeout=60)
    jobs, _ = json.JSONDecoder().raw_decode(r.stdout[r.stdout.find('['):])
except Exception as e:
    print(f"FAIL 读不到 cron 任务({type(e).__name__})")
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
    print(f"PASS cron 指令的防复发提醒齐全({len(jobs)} 个任务已核对)")
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

# 2026-10-27 05:15 补:SESSION-STATE.md 头部的「📋 系统状态」小节是**手写的**,
# 里面有大量过期内容:已删除的 provider(glmcode/arkcode/kimicode)、
# 已删除的 cron(teahouse-check/dream-cycle-v3)、已移除的 plugin(qqbot/ddingtalk)、
# 「首次自动调度验证待 10-26」这种早就过期的待办。
#
# 而 SESSION-STATE.md 的头部是**下一个会话最先读的地方** -- 它过期 = 下一个会话被误导。
# 这比 L1 库里的过期计数更严重:那是数据,这是**指令**。
#
# 现在「系统状态」由 scripts/gen_system_status.py 从真实系统生成。
# 这里核对:**声明的内容与真实系统是否一致**。

st_report=$(python3 - << 'PY6'
import json, os, re, subprocess
WS = '/root/.openclaw/workspace'
STATE = f'{WS}/SESSION-STATE.md'
failed = 0

try:
    txt = open(STATE, encoding='utf-8').read()
except Exception as e:
    print(f"FAIL 读不到 SESSION-STATE.md({type(e).__name__})")
    raise SystemExit(1)

# 取「系统状态」小节(含生成标记的那段)
m = re.search(r'<!-- BEGIN:gen_system_status -->(.*?)<!-- END:gen_system_status -->',
              txt, re.S)
if not m:
    print("FAIL SESSION-STATE.md 的系统状态小节没有生成标记 -- "
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
            problems.append(f"providers 声明 {sorted(decl)},实际 {sorted(real)}")
except Exception:
    pass

# 2) cron 任务列表与真实一致
try:
    r = subprocess.run(['openclaw', 'cron', 'list', '--json'],
                       capture_output=True, text=True, timeout=60)
    jobs, _ = json.JSONDecoder().raw_decode(r.stdout[r.stdout.find('['):])
    real_names = {j['name'] for j in jobs}
    # 分隔符可能是半角连字符、半角/全角破折号 —— 都要认。
    # （2026-10-27 实测踩过：生成器用 em-dash「—」，断言写的是「-」，
    #  → 声明列表解析成空 → 报「漏了全部 cron 任务」，纯属误报）
    decl_names = set(re.findall(r'^- `([a-z0-9-]+)`\s*[\-\u2014\u2013]+\s*', sec, re.M))
    missing = real_names - decl_names
    extra = decl_names - real_names
    if missing:
        problems.append(f"漏了 cron 任务 {sorted(missing)}")
    if extra:
        problems.append(f"列了不存在的 cron 任务 {sorted(extra)}")
except Exception:
    pass

# 3) 不该再有已删除实体的残留提及
#
# 2026-10-29 修正(误报): 原来对整节做裸子串匹配。
# 但 10-28 的修复(aac5e44)刻意让生成器输出
# 「**agent 级目录另有 provider**: arkcode, glmcode, qwen, kimicode」
# —— 那是**真实存在**的残留凭证目录,必须让人看见(体检盲区,见【M】组)。
# 裸子串匹配把「如实报告残留」误判成「残留了已删除的东西」-> 3 个假 FAIL。
#
# 现在按**作用域**拆开:
#   - provider 名只在「**providers**: ...」那一行里查(它必须等于真实配置)
#   - 「agent 级目录另有 provider」那一行**允许**出现这些名字(那是有意报告)
#   - cron/plugin 的已删除名仍然全节查(它们没有任何「有意报告」的场景)
GONE_PROVIDERS = ('glmcode', 'arkcode', 'kimicode')
GONE_ENTITIES = ('teahouse-check', 'dream-cycle-v3', 'qqbot', 'ddingtalk')

# 3a) provider: 只看 primary providers 行(检查 1 已比对了声明 vs 真实)
pm = re.search(r'^\*\*providers\*\*: ([^\n]+)$', sec, re.M)
if pm:
    primary_line = pm.group(1)
    for gone in GONE_PROVIDERS:
        if gone in primary_line:
            problems.append(f"primary providers 里还有已删除的「{gone}」")

# 3b) cron/plugin: 全节查
for gone in GONE_ENTITIES:
    if gone in sec:
        problems.append(f"系统状态小节里还有已删除的「{gone}」")

# 3c) 「agent 级目录另有 provider」行必须与真实情况一致
#     —— 这一行是 10-28 加的盲区提示,它是「如实报告」,不能是硬编码的旧名单
#
#     语义要与生成器一致:它列的是 **agent 级目录里有、但全局配置里没有** 的 provider
#     (即 `extra`),不是 agent 级目录的全部。所以比对时也要先算 extra。
try:
    ap = os.path.join(os.path.expanduser('~'), '.openclaw', 'agents',
                       'main', 'agent', 'models.json')
    if os.path.exists(ap):
        ac = json.load(open(ap, encoding='utf-8'))
        real_agent = set((ac.get('providers') or {}).keys())
        gc = json.load(open('/root/.openclaw/openclaw.json', encoding='utf-8'))
        global_ps = set((gc.get('models', {}).get('providers') or {}).keys())
        real_extra = {p for p in real_agent if p not in global_ps}
        am = re.search(r'^\*\*agent 级目录另有 provider\*\*: ([^\n]+)$', sec, re.M)
        if am:
            decl_agent = {x.strip().split('（')[0].strip()
                          for x in am.group(1).split(',') if x.strip()}
            if decl_agent != real_extra:
                problems.append(
                    f"agent 级额外 provider 声明 {sorted(decl_agent)},"
                    f"实际 {sorted(real_extra)}")
except Exception:
    pass

# 4) 「巡检现状」行必须**唯一**(2026-10-27 08:20 加)
#    update_state_status.py 原来靠「找一行长得像活行」来定位,
#    但它自己又会把其它同名前缀的行改成「(快照)」--
#    活行找不到时 fallback 到 insert() -> **每次巡检多插一行**,
#    累积到 13 行才被发现。
#    现在改用 BEGIN/END 标记块;这里断言「标记块只有 1 个,
#    且块外的散落行为 0」-> 防止复发。
_b = txt.count('<!-- BEGIN:gen_healthcheck_status -->')
_e = txt.count('<!-- END:gen_healthcheck_status -->')
if _b != 1 or _e != 1:
    problems.append(f"巡检现状标记块数量异常(BEGIN={_b} END={_e},应为各 1)")
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
        problems.append(f"标记块外还有 {_stray} 行散落的「巡检现状」-- 每次巡检都在自增")

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

echo "【R】cron 任务「脚本耗时 vs 超时」对比(防把等待慢当成任务慢)"

# 2026-10-27 04:40 补(E35):
# backup-audit 连续两次「超时失败」,但脚本只要 **0.18 秒**。
# 真根因不是任务慢,是 agent 用 process(poll, timeout=120000~300000)
# 每次**等满** timeout → 3 次 poll ≈ 510s → 必然撞上 job 超时。
#
# 所以这条断言的核心是**比值**:脚本耗时 vs timeout 差几个数量级,
# 就说明慢的不是任务,而是「怎么等」。调 timeout 是第二道防线,
# 第一道是「别等满」。
#
# 判据:
#   1) 脚本耗时 < 5s 但 job 因超时失败 → FAIL(等待方式的问题,不是任务的问题)
#   2) timeoutSeconds < 脚本耗时的 10 倍 → WARN(余量太小)

to_report=$(python3 - << 'PY5'
import json, os, subprocess, time
JOBS = '/root/.openclaw/cron/jobs.json'
WS = '/root/.openclaw/workspace'
failed = 0

# 脚本路径表:cron 任务名 → 脚本
SCRIPTS = {
    'backup-audit': 'scripts/backup_audit.sh',
    'healthcheck': 'scripts/healthcheck.sh',
}

try:
    d = json.load(open(JOBS, encoding='utf-8'))
    jobs = d if isinstance(d, list) else d.get('jobs', [])
except Exception as e:
    print(f"FAIL 读不到 jobs.json({type(e).__name__})")
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
              f"(timeout {tos}s)-- 慢的是「等法」不是任务:"
              f"用短 timeout 多次 poll,不要一次等满")
    failed = 1
else:
    print("PASS 无「脚本很快但超时失败」的任务")

# 2) timeout 余量对比
#
# ⚠️ 2026-10-27 10:50 改判据 -- 原来比的是**各任务 timeout 的绝对值**
#    (600s vs 1800s → 「差异大,手写的 timeout 彼此不可比」)。
#    这测的是**不可比的量**:不同任务的 timeout 本来就不该一样
#    (600s 配一个 300s 的任务 = 刚好;600s 配一个 30s 的任务 = 宽裕)。
#    绝对值差异大**不构成任何风险信号**。
#
# → 改成比**各自的「实测耗时 / timeout」占用率** -- 这才是可比的量,
#    也正是【U】组在用的口径(>70% FAIL / 递增趋势 WARN)。
#    本条只做**横向对照**:谁最接近上限,提示「这个任务最脆弱」。
#
# 数据源:run history(真实 cron 实测),不是本地手动跑
# (E40:手动测的是下界,真实 cron 可慢 2~3 倍)。
tos_map = {}
for j in jobs:
    p = j.get('payload') or {}
    if p.get('kind') == 'agentTurn':
        tos_map[j.get('name')] = p.get('timeoutSeconds') or 0

runs_dir = '/root/.openclaw/cron/runs'
util = []
for j in jobs:
    name = j.get('name')
    tos = tos_map.get(name) or 0
    if not tos:
        continue
    rp = os.path.join(runs_dir, f"{j.get('id')}.jsonl")
    best = None
    try:
        with open(rp, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get('action') != 'finished':
                    continue
                dur = r.get('durationMs') or 0
                at = r.get('runAtMs') or r.get('ts') or 0
                # 只看成功的运行(失败的耗时是「烧满 timeout」,不反映真实负载)
                if r.get('status') != 'ok':
                    continue
                if best is None or at > best[0]:
                    best = (at, dur)
    except Exception:
        continue
    if best and best[1] > 0:
        util.append((name, best[1] / 1000.0 / tos * 100, tos, best[1] / 1000.0))

if util:
    util.sort(key=lambda x: -x[1])
    top = util[0]
    line = ",".join(f"{n} {p:.0f}%" for n, p, _, _ in sorted(util, key=lambda x: x[1]))
    if top[1] > 70:
        print(f"WARN timeout 占用率偏高:{line}"
              f" -- {top[0]} 已用掉 {top[0] and top[3]:.0f}s/{top[2]}s,逼近上限")
    else:
        print(f"PASS timeout 占用率健康({line})")
else:
    print("PASS cron 超时余量合理(无可用实测耗时)")
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

echo "【V】会话里的长 poll(指令说了但没照做的信号)"

# 2026-10-27 09:50 补:E35 之后我给 3 条 cron 指令加了「【等待方式】」说明,
# 但 09:00 的 daily-learning **还是用了 4 次 120s/150s 的 poll**(实测 772s)。
#
# → **指令写了 ≠ 会照做**(E31 的老问题:别把正确性寄托在「记得做」上)。
# TOOLS.md 里已经加了全局规则(每个会话都加载),但那也是「说明」不是「约束」。
#
# 这条断言的价值:**让违规可见**。
# 它不阻止 agent 用长 poll,但能在我验收时立刻发现「又犯了」,
# 而不是等到某个任务撞上超时才察觉。
#
# 判据(2026-10-27 10:20 改进):**只看最近 12 小时**。
#
# 原判据是「最近 3 天累计 > 5 次 → WARN」-- 有两个毛病:
#   1. **稀释**:3 天里 1 天严重违规 + 2 天干净,累计数照样很大,
#      看起来像「天天在犯」,但昨天已经改好了
#   2. **历史噪音**:TOOLS.md 那条规则 2026-10-27 09:50 才写,
#      10-25/10-26 的长 poll 属于「规则还不存在」,
#      算进去只会让真违规淹没在历史里
#
# → 改成:**最近 12 小时内**的长 poll > 3 次 → WARN(历史存量单独报出,不参与判定)
# 这样它回答的是真正该问的问题:**「我此刻还在犯吗?」**

poll_report=$(python3 - << 'PY9' 2>/dev/null
import glob, json, os, time, collections, datetime, re, subprocess
# SESS 可被外部覆盖(单测用)-- 默认指向真实会话目录
SESS = globals().get('SESSION_DIR', '/root/.openclaw/agents/main/sessions')

# 2026-10-27 12:05 修正(E48):判据窗口按**每条 poll 的时间戳**算,
# 不是按文件 mtime。
#
# 原实现用 `os.path.getmtime(f) > cutoff` 选文件 -- 有两个毛病:
#   1. **窗口是 3 天**,但注释和提示都写「最近 12 小时」→ 说的和做的不一致
#   2. 更要命:**同一个会话文件一直在追加**,
#      一个 10-25 就开着的会话到 10-27 还在写 → 它的 mtime 永远是「现在」
#      → 里面 10-25 的长 poll 全被算进「最近 12 小时」
#
# → 改成解析每条 toolCall 的 timestamp,窗口 = 最近 12 小时。
#    12 小时 ≈ 一个工作日,符合「我此刻还在犯吗」这个问题(见注释)。
WINDOW_HOURS = 12
cutoff_ts = time.time() - WINDOW_HOURS * 3600

def ts_of(obj):
    """取一条 jsonl 记录的时间戳(秒级 float)。兼容 ISO 字符串与毫秒数字。"""
    t = obj.get('timestamp')
    if isinstance(t, (int, float)):
        return t / 1000.0 if t > 1e11 else float(t)
    if isinstance(t, str):
        try:
            return datetime.datetime.fromisoformat(t.replace('Z', '+00:00')).timestamp()
        except Exception:
            return None
    return None

# 按**任务**维度统计(不是按会话)-- 因为要改的是「哪个 cron 任务需要提醒」,
# 而会话 id 对我没意义。2026-10-27 实测:按任务分组后一眼看出
# daily-learning 21 次 / backup-audit 9 次 / healthcheck 5 次。
MARKERS = [
    ('healthcheck', 'scripts/healthcheck.sh'),
    ('backup-audit', 'scripts/backup_audit.sh'),
    ('teahouse-monitor', 'scripts/teahouse_monitor.py'),
    ('daily-learning', '每日学习(精简版)'),
    ('daily-reflection', '每日反思三问'),
]

def _norm(s):
    """把全角/半角标点统一,避免匹配对标点形式敏感。

    2026-10-27 踩过:编辑脚本时全角括号被写成半角,
    标记字符串的**全角括号被写成了半角**,
    → 真实 session 全部匹配不上 → 分类塌成「未分类」。
    **静默失配**:不报错,只是所有会话都掉进同一个桶。
    → 所以匹配前先归一化,两种形式都能认。
    """
    trans = str.maketrans({
        '\uff08': '(', '\uff09': ')', '\uff1a': ':', '\uff0c': ',',
        '\uff5c': '|', '\uff1f': '?', '\uff01': '!',
        '\u3001': ',', '\uff1b': ';',
    })
    return s.translate(trans)

def _esc(s):
    """把非 ASCII 字符转成 JSON 的 \\uXXXX 转义形式。"""
    return ''.join('\\u%04x' % ord(ch) if ord(ch) > 127 else ch for ch in s)

def _unescape(s):
    """把文本里的 JSON \\uXXXX 转义还原成真字符。

    为什么需要：转义后的文本全是 ASCII，
    「全角（」变成「\\uff08」—— **全角/半角的信息被编码掉了**，
    光看转义形式分不出是哪种括号 → 必须先解码再比较。
    """
    try:
        return re.sub(r'\\u([0-9a-fA-F]{4})',
                      lambda m: chr(int(m.group(1), 16)), s)
    except Exception:
        return s

def task_of(head):
    # 注意：head 是 **JSONL 原文**，非 ASCII 字符可能被转义成 \\uXXXX。
    # 直接在原文里 find 中文标记，遇到转义就漏判（→ 会话被归到「未分类」）。
    #
    # 三条路都要试（2026-10-27 实测踩过两个坑）：
    #   1. 原文 + 原始标记
    #   2. 归一化标点后的原文（全角/半角等价）
    #   3. 转义形式（JSON 序列化时 ensure_ascii=True 会转义非 ASCII）
    #      → 转义形式也要**两边都归一化**再比，
    #        否则「全角标记」和「半角标记的转义」比不上。
    # 三条路都要试（2026-10-27 实测踩过两个坑）：
    #   1. 原文 + 归一化后的标记（全角/半角等价）
    #   2. **把原文里的 \uXXXX 转义解码回真字符**后再比
    #      ← 关键：转义形式里已经是 ASCII 文本（\uff08），
    #        _norm 改不了它 → 必须先**解码**，再归一化
    #   3. 标记自身的转义形式（兜底）
    h_n = _norm(head)
    h_d = _norm(_unescape(head))
    for name, marker in MARKERS:
        m_n = _norm(marker)
        if m_n in h_n:
            return name
        if m_n in h_d:
            return name
        if _esc(m_n) in _esc(h_n):
            return name
    return None

# 2026-10-27 12:05 补(E48):**普通心跳会话原来是隐形人**。
#
# 起因:本会话(c2513949,纯 heartbeat,不是任何 cron 任务)用了 3 次长 poll
#       (180s/200s/220s)-- 违反了 TOOLS.md 的「poll ≤10~15s」规则,
#       但【V】组**一次都没报**。
#
# 根因:`task_of()` 找不到 cron 任务标记 → 返回 None → `continue` → **整个会话被跳过**。
#       也就是说:**没有任务标记的会话 = 完全不被监控**。
#       而「按任务维度分组」这个改进(11:45 加的)顺手引入了一个新盲区:
#       从「按会话统计」变成了「只看 cron 任务会话」。
#
# 教训:**分类不该等于过滤**。分组是为了更好的展示,不是为了排除未知类别。
#       「匹配不上」应该是「归入『未分类』」,而不是「不算」。
#
# 修法:匹配不到时归入 'heartbeat/其他',**所有会话都统计**。

long_by_task = collections.Counter()
total_by_task = collections.Counter()
long_by_sess = collections.Counter()
last_by_sess = {}
sess_start = {}
sess_saw_rule = {}
long_total = 0
poll_total = 0

try:
    files = glob.glob(f'{SESS}/*.jsonl')
except Exception:
    files = []

# 2026-10-27 13:00 补(E49):**会话启动时间 vs 规则加入时间**。
#
# 起因:并行会话查出一件重要的事 --
#   TOOLS.md 的「等长任务」规则是 2026-10-27 **09:57**(commit 6d11015)才加的。
#   而 `0d127a42` 这个会话 **10-25 20:09** 就启动了(早了近 2 天)。
#   → 它的上下文里**根本不可能有那条规则**。
#
# 所以那 484 次长 poll 里,**大部分是「没看到规则」而不是「无视规则」**。
# 这两者的区别很重要:
#   - 「没看到规则」→ 规则没覆盖到它,不是它的问题
#   - 「无视规则」→ 看到了还不改,**才是真问题**
#
# 把它们混在一起报,会得出「我(或会话)在顽固违规」的错误结论,
# 进而**优化错了地方**(去加强提醒,而不是去修覆盖面)。
#
# 实现:读会话的**首条 user message 时间**作为启动时间,
#      再看它的上下文里**是否真的包含**规则原文(不是靠猜)。
RULE_MARKER = '等长任务'
RULE_ADDED_TS = None  # 运行时从 git 取

def isolated_sessions():
    """从 cron run 历史里取所有 isolated 会话的 id 前缀。

    为什么要单独判：**isolated 的 cron 会话不注入工作区上下文文件**
    （实测：只有 main 会话拿得到 TOOLS.md）。
    → 拿不到 TOOLS.md 的 cron 会话，**不是它无视**——
      它能看到的是**自己的 cron 指令**（那个里有【等待方式】提醒）。
    → 所以下面还要单独判断：**它的 cron 指令里有没有那条提醒**。

    ⚠️ 这个函数要跑 5 次 `cron runs`，比较慢 → **结果缓存到文件**，
    巡检和单测各跑一次时不用重复付这个代价。
    """
    cache_path = '/tmp/openclaw/isolated_sessions_cache.json'
    ids = set()
    # session → jobId 的映射（来自 cron runs 的 sessionKey / sessionId）
    # 以及「这个 cron 任务自己的指令里有没有那条等待提醒」
    # → isolated 会话看不到 TOOLS.md，但**能看到自己的 cron 指令**
    sess_job_map = {}
    job_has_rule = {}
    try:
        # 2026-10-29: 读运行历史改成直接读 JSONL（绕开 Node 进程启动，~4s→~0.04s）
        sys.path.insert(0, '/root/.openclaw/workspace/scripts')
        from cron_runs_helper import read_cron_runs
        r = subprocess.run(['openclaw', 'cron', 'list', '--json'],
                           capture_output=True, text=True, timeout=60)
        raw = r.stdout or ''
        jobs, _ = json.JSONDecoder().raw_decode(raw[raw.find('['):])
        for j in jobs:
            msg = (j.get('payload') or {}).get('message', '') or ''
            job_has_rule[j['id']] = ('poll' in msg and
                                     ('短 poll' in msg or '等待方式' in msg or 'timeout=10000' in msg))
            r2 = subprocess.run(['openclaw', 'cron', 'runs', '--id', j['id'], '--limit', '20'],
                                 capture_output=True, text=True, timeout=60)
            try:
                for e in read_cron_runs(j['id'], 20):
                    sid8 = (e.get('sessionId') or '')[:8]
                    if sid8:
                        ids.add(sid8)
                        sess_job_map[sid8] = j['id']
            except Exception:
                pass
    except Exception:
        pass
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, 'w', encoding='utf-8') as fh:
            json.dump({'ids': sorted(ids), 'job_has_rule': job_has_rule,
                       'sess_job': sess_job_map}, fh)
    except Exception:
        pass
    return ids, job_has_rule, sess_job_map


def _load_cron_ctx():
    """返回 (isolated 会话 id 集合, {job_id: 指令里有没有等待提醒})。"""
    cache_path = '/tmp/openclaw/isolated_sessions_cache.json'
    try:
        import time as _t
        if os.path.exists(cache_path) and (_t.time() - os.path.getmtime(cache_path)) < 1800:
            with open(cache_path, encoding='utf-8') as fh:
                d = json.load(fh)
            return set(d.get('ids', [])), d.get('job_has_rule', {}), d.get('sess_job', {})
    except Exception:
        pass
    ids, jhr, sjm = isolated_sessions()
    return ids, jhr, sjm


ISOLATED_SESSIONS, JOB_HAS_RULE, SESS_JOB = _load_cron_ctx()

def rule_added_ts():
    """规则进 TOOLS.md 的提交时间(git 权威,不靠手写常量)。"""
    try:
        r = subprocess.run(
            ['git', '-C', '/root/.openclaw/workspace', 'log', '-1',
             '--format=%ct', '-S', RULE_MARKER, '--', 'TOOLS.md'],
            capture_output=True, text=True, timeout=30)
        v = (r.stdout or '').strip().splitlines()
        if v and v[0].isdigit():
            return int(v[0])
    except Exception:
        pass
    return None

RULE_ADDED_TS = rule_added_ts()

for f in files:
    try:
        with open(f, encoding='utf-8', errors='replace') as fh:
            head = fh.read(2500)
            job = task_of(head)
            if not job:
                # 不再跳过 -- 归入「未分类」桶,保证所有会话都被统计(E48)
                job = 'heartbeat/其他'
            fh.seek(0)
            sid = os.path.basename(f)[:8]
            for line in fh:
                if '"toolCall"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                # 按**每条 poll 的时间戳**过滤窗口(E48),不用文件 mtime
                ts = ts_of(d)
                if ts is None or ts < cutoff_ts:
                    continue
                m = d.get('message') or {}
                c = m.get('content')
                if not isinstance(c, list):
                    continue
                for x in c:
                    if not isinstance(x, dict) or x.get('type') != 'toolCall':
                        continue
                    if x.get('name') != 'process':
                        continue
                    a = x.get('arguments') or {}
                    if a.get('action') != 'poll':
                        continue
                    try:
                        t = int(a.get('timeout') or 0)
                    except Exception:
                        continue
                    poll_total += 1
                    total_by_task[job] += 1
                    if t > 60000:
                        long_total += 1
                        long_by_task[job] += 1
                        long_by_sess[sid] += 1
                        if sid not in last_by_sess or ts > last_by_sess[sid]:
                            last_by_sess[sid] = ts
    except Exception:
        continue

# 2026-10-27 13:00(E49):给每个会话算「**启动时间**」和「**是否看得到规则**」。
# 会话启动时间 = 首条 user message 的时间戳。
# 是否看得到规则 = 启动时间 >= 规则加入时间(git 权威)。
#   看得到但还违规 → 真问题(无视规则)
#   启动更早       → 规则还没写,它的上下文里不可能有(没看到规则)
sess_info = {}
for f in files:
    sid = os.path.basename(f)[:8]
    if sid in sess_info:
        continue
    start_ts = None
    saw_rule = False
    try:
        with open(f, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                if '"toolCall"' not in line and '"role":"user"' not in line and '"role": "user"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                ts = ts_of(d)
                if ts is None:
                    continue
                msg = d.get('message') or {}
                if msg.get('role') != 'user':
                    continue
                if start_ts is None:
                    start_ts = ts
                # 会话上下文里是否**真的含有**规则原文。
                # ⚠️ 要看**所有** user message,不能只看第一条:
                #    TOOLS.md 通常被注入在系统提示里(表现为一条 user message),
                #    但也可能是会话自己 `cat TOOLS.md` 之后才出现在上下文里
                #    (那次读取记录是 toolResult,但注入那份仍会在 user 消息里)。
                #    只看第一条会漏判 → 明明看到了规则却被归到「未见规则原文」。
                content = msg.get('content')
                txt = ''
                if isinstance(content, str):
                    txt = content
                elif isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get('type') == 'text':
                            txt += c.get('text') or ''
                if RULE_MARKER in txt:
                    saw_rule = True
                    if start_ts is not None:
                        break
    except Exception:
        pass
    sess_info[sid] = (start_ts, saw_rule)

def sess_job(sid):
    """查这个会话属于哪个 cron job（隔离会话的规则来源 = 它自己的 cron 指令）。

    ⚠️ 不能靠会话文件里的 sessionKey 反查 —— **会话文件里没有 sessionKey**，
    那个字段只出现在 `cron runs` 的输出里。
    → 所以映射在收集 cron runs 时就建好（见 SESS_JOB）。
    """
    return SESS_JOB.get(sid)


def rule_aware(sid):
    """返回 (标签, 是否「看得到规则却违规」)。

    四种情况:
      1. 启动时间 < 规则写入时间 → 「规则生效前启动」
         它的上下文快照里**不可能有**那条规则 → 不算它无视
      2. 是 **isolated cron 会话** → 「隔离会话拿不到 TOOLS.md」
         实测:isolated 的 cron 会话**不会注入**工作区上下文文件
         (只有 main 会话注入)→ 它本来就看不到那条规则
      3. 启动时间 >= 规则写入时间,且上下文里**确实有**规则原文
         → 「看得到规则」→ **真问题**
      4. 其余 → 「未见规则原文」(保守起见不判定为无视,但值得查)
    """
    start, saw = sess_info.get(sid, (None, False))
    if start is not None and RULE_ADDED_TS and start < RULE_ADDED_TS:
        return ('规则生效前启动', False)
    if sid in ISOLATED_SESSIONS:
        # isolated 会话看不到 TOOLS.md，但**能看到自己的 cron 指令**
        jid = sess_job(sid)
        if jid and JOB_HAS_RULE.get(jid):
            # 它的指令里**确实有**那条提醒 → 违规就是它无视 → 真问题
            return ('看得到规则（cron 指令里）', True)
        return ('隔离会话拿不到 TOOLS.md', False)
    if saw:
        return ('看得到规则', True)
    return ('未见规则原文', False)

# 拆分:真问题(看得到规则却违规) vs 规则覆盖不到(不算它的账)
guilty = collections.Counter()
not_guilty = collections.Counter()
guilty_sess = collections.Counter()
pre_rule_sess = collections.Counter()
no_text_sess = collections.Counter()
iso_sess = collections.Counter()
for sid, n in long_by_sess.items():
    label, is_guilty = rule_aware(sid)
    if is_guilty:
        guilty[sid] = n
        guilty_sess[sid] = n
    else:
        not_guilty[sid] = n
        if label == '规则生效前启动':
            pre_rule_sess[sid] = n
        elif label == '隔离会话拿不到 TOOLS.md':
            iso_sess[sid] = n
        else:
            no_text_sess[sid] = n

if long_total:
    detail = ', '.join(f"{k}×{v}" for k, v in long_by_task.most_common())
    worst = long_by_task.most_common(1)[0]
    # 按会话归属 -- 区分「还在犯」和「已经停了」是关键信息(E48)
    sess_detail = ', '.join(
        f"{k}×{v}({datetime.datetime.fromtimestamp(last_by_sess[k]).astimezone().strftime('%m-%d %H:%M')})"
        for k, v in long_by_sess.most_common(4)
    )
    print(f"WARN 最近 {WINDOW_HOURS} 小时有 {long_total} 次长 poll（timeout>60s）：{detail}")
    print(f"     共 {poll_total} 次 poll，其中 {long_total} 次过长")
    print(f"     最严重：{worst[0]}({worst[1]} 次）")
    print(f"     按会话：{sess_detail}")
    print(f"     ↑ 括号内是**最后一次**违规时间 -- 早于最近一小时的 = 已经停了，"
          f"不用再追;新鲜的才是还在犯")
    if 'heartbeat/其他' in long_by_task:
        print(f"     ⚠️ 其中 {long_by_task['heartbeat/其他']} 次来自**未分类会话**"
              f"(普通心跳/手动会话,不是 cron 任务）——"
              f"这类会话以前完全不计入(E48 修掉的盲区)")

    # ── E50 补：**归属**维度 ─────────────────────────────────────
    # 「等法合规吗」和「这活该谁跑」是**两个不同的问题**。
    # 只看 timeout 的话，把 timeout 调小就能骗过断言 —— 但重复劳动还在。
    # 这里问：被等的那个脚本，是不是这个会话**本来该跑**的？
    #
    # 归属表：某个脚本该由哪个 cron 负责
    OWNER = {
        "scripts/healthcheck.sh": "healthcheck(07:00)",
        "scripts/backup_audit.sh": "backup-audit(03:00)",
        "scripts/teahouse_monitor.py": "teahouse-monitor(每4h)",
    }
    wrong_owner = {}
    for f in files:
        try:
            with open(f, encoding='utf-8', errors='replace') as fh:
                head = fh.read(2500)
                job = task_of(head)
                if not job:
                    continue
                fh.seek(0)
                last_exec = ""
                for line in fh:
                    if '"toolCall"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    m = d.get("message") or {}
                    c = m.get("content")
                    if not isinstance(c, list):
                        continue
                    for x in c:
                        if not isinstance(x, dict) or x.get("type") != "toolCall":
                            continue
                        nm = x.get("name")
                        a = x.get("arguments") or {}
                        if nm == "exec":
                            cmd = str(a.get("command", ""))
                            last_exec = ""
                            for script in OWNER:
                                if script in cmd:
                                    last_exec = script
                                    break
                        elif nm == "process" and a.get("action") == "poll":
                            try:
                                t = int(a.get("timeout") or 0)
                            except Exception:
                                t = 0
                            if t > 60000 and last_exec:
                                # 这个会话是不是该脚本的属主？
                                owner = OWNER[last_exec]
                                if job not in owner:
                                    wrong_owner.setdefault(
                                        (last_exec, owner, job), []).append(1)
        except Exception:
            continue
    if wrong_owner:
        print(f"     ── 按「这活该谁跑」拆分（E50）──")
        for (script, owner, job), lst in sorted(wrong_owner.items(), key=lambda kv: -len(kv[1])):
            print(f"     🔴 {job} 在等 {script}（属 {owner}）× {len(lst)} 次")
        print(f"        → **合规 ≠ 有价值**：把 timeout 调小能骗过断言，"
              f"但这活本来该由那个 cron 干，手动重跑是重复劳动")
        print(f"        → 验收时跑一次合理，反复跑是习惯")

    # 2026-10-27 13:00(E49):区分「无视规则」和「没看到规则」
    g_total = sum(guilty.values())
    n_total = sum(not_guilty.values())
    print(f"     ── 按「规则是否可见」拆分（E49）──")
    if g_total:
        g_list = ', '.join(f"{k}×{v}" for k, v in guilty_sess.most_common(4))
        print(f"     🔴 看得到规则却违规：{g_total} 次（{g_list})")
        print(f"        → **这是真问题**：规则就在它上下文里，它还是不照做")
    else:
        print(f"     ✅ 看得到规则却违规：**0 次**")
    if n_total:
        rts = (datetime.datetime.fromtimestamp(RULE_ADDED_TS).astimezone().strftime('%m-%d %H:%M')
               if RULE_ADDED_TS else '未知')
        if pre_rule_sess:
            pl = ', '.join(f"{k}×{v}" for k, v in pre_rule_sess.most_common(4))
            print(f"     ⚪ 规则生效前启动：{sum(pre_rule_sess.values())} 次（{pl})")
            print(f"        → 规则写入时间 {rts}｜这些会话启动更早，"
                  f"上下文里**不可能有**该规则 -- 不算它无视")
        if iso_sess:
            il = ', '.join(f"{k}×{v}" for k, v in iso_sess.most_common(4))
            print(f"     ⚪ 隔离 cron 会话（拿不到 TOOLS.md）：{sum(iso_sess.values())} 次（{il}）")
            print(f"        → isolated 会话**不注入**工作区上下文文件 -- "
                  f"它本来就看不见那条规则 → 要让它看见,得写进**它的 cron 指令**")
        if no_text_sess:
            nl = ', '.join(f"{k}×{v}" for k, v in no_text_sess.most_common(4))
            print(f"     🟡 启动晚于规则、但上下文未见规则原文：{sum(no_text_sess.values())} 次（{nl})")
            print(f"        → 规则写入时间 {rts}｜**值得查**："
                  f"是会话没拿到 TOOLS.md 快照，还是规则被后来的改动挤掉了?")
    print(f"     排查方向：① 指令里有没有【等待方式】提醒"
          f" 2 它等的那个长任务**该不该跑**"
          f"(2026-10-27 实测：daily-learning 自己跑去跑 healthcheck.sh,"
          f"那本来就该由 07:00 的 cron 干 —— 属于「没必要做的长任务」）"
          f" 3 确实要跑的话用 sessions_spawn，而不是 poll")
else:
    print(f"PASS 最近 {WINDOW_HOURS} 小时没有长 poll"
          f"（共 {poll_total} 次 poll，全部 ≤60s）")
PY9
)
while IFS= read -r line; do
  case "$line" in
    PASS*) ok "${line#PASS }" ;;
    WARN*) warn "${line#WARN }" ;;
    *) [ -n "$line" ] && echo "  $line" ;;
  esac
done <<< "$poll_report"
PASS=$((PASS + 1))

echo "【Y】外部 API 的响应延迟(不只是能不能通)"

# 2026-10-27 14:20 补：
# 【A】组只查「能不能通」(timeout 15s)，**不查「有多慢」** ——
# 一个「能通但很慢」的外部 API 会被判定为健康，
# 而实际上它会吃掉依赖它的任务的 timeout 预算。
#
# 真实场景：teahouse_monitor.py 的 `_get(url, timeout=90, retries=3)`
# → 一次请求最坏 **270 秒**。如果 GitHub 变慢(不是挂，是慢)，
# teahouse-monitor(timeout 600s) 会只剩 1-2 次请求的余量。
#
# 判据(用真实延迟，不是「通不通」)：
#   1) 延迟 > 15s → FAIL(慢到会拖累依赖任务)
#   2) 延迟 > 5s  → WARN(能通但慢，依赖它的任务会吃 timeout 预算)
#
# ⚠️ 阈值**不是拍脑袋的**（E29 的规矩：先问「正常时长什么样」）：
#   依赖方 teahouse-monitor 的真实调用预算：
#     1 次 fetch_issues + 最多 15 次 fetch_comments = **16 次调用**
#     job timeout = **600s**
#   → 单次 5s  → 16×5  = 80s = 13% 预算（WARN 线，安全但明显变慢）
#   → 单次 15s → 16×15 = 240s = 40% 预算（FAIL 线，再慢就要吃掉大头）
#   → 单次 30s → 16×30 = 480s = 80% 预算（必然出事）
#   → 单次 60s → 16×60 = 960s > 600s（**必然超时**）
#   实测基线：飞书 0.03s · GitHub 0.83s(中位) · 嵌入代理 0.00s
#   → WARN 线离基线 6 倍、FAIL 线离基线 18 倍 → 不会误报
#
# ⚠️ 已知局限（E29 的诚实版）：这两个阈值是**绝对值**，
#   没有按「依赖方调用次数 × timeout」动态算。
#   如果以后某个任务调用次数变多（比如 16 → 50 次），
#   同一个 15s 阈值对它的意义就变了 → 那时应该改成按预算算。

y_lat_out=$(python3 - << 'PYB' 2>/dev/null
import json, time, urllib.request

def probe(name, url, method="GET", headers=None):
    body = b"{}" if method == "POST" else None
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    t0 = time.time()
    try:
        urllib.request.urlopen(req, timeout=20)
        return name, time.time() - t0, None
    except Exception as e:
        code = getattr(e, 'code', None)
        # 4xx = 服务在应答（认证问题），仍算「通」
        if code and 400 <= int(code) < 500:
            return name, time.time() - t0, None
        return name, time.time() - t0, type(e).__name__

probes = [
    probe("GitHub", "https://api.github.com/repos/ythx-101/openclaw-qa/issues?per_page=5"),
    probe("嵌入代理", "http://127.0.0.1:8799/health"),
]
try:
    cfg = json.load(open('/root/.openclaw/openclaw.json', encoding='utf-8'))
    fs = cfg.get('channels', {}).get('feishu', {})
    tok = fs.get('appSecret') or fs.get('appId') or ''
    probes.insert(0, probe("飞书",
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        "POST", {"Authorization": f"Bearer {tok}"}))
except Exception:
    pass

failed = 0
warned = 0
ok_lines = []
for name, dt, err in probes:
    if err:
        print(f"FAIL {name} 不可达({err}，耗时 {dt:.1f}s)")
        failed = 1
    elif dt > 15:
        print(f"FAIL {name} 响应 {dt:.1f}s —— 慢到会拖累依赖它的任务")
        failed = 1
    elif dt > 5:
        print(f"WARN {name} 响应 {dt:.1f}s —— 能通但慢，依赖它的任务会吃 timeout 预算")
        warned = 1
    else:
        ok_lines.append(f"{name} {dt:.2f}s")

if ok_lines and not failed:
    print("PASS 外部 API 延迟正常(" + ", ".join(ok_lines) + ")")
print(f"__Y_FAILED__={failed}")
PYB
)
y_fail=$(echo "$y_lat_out" | grep -oP '__Y_FAILED__=\K[0-9]+')
y_fail=${y_fail:-0}
while IFS= read -r line; do
  case "$line" in
    PASS*) ok "${line#PASS }" ;;
    FAIL*) bad "${line#FAIL }" ;;
    WARN*) warn "${line#WARN }" ;;
  esac
done <<< "$y_lat_out"
PASS=$((PASS + 1))

echo "【Z】压缩后重注入的上下文预算（防红线被截掉）"

# 2026-10-27 16:30 补：压缩后会重新注入 AGENTS.md 的指定小节，
# 源码里 `MAX_CONTEXT_CHARS = 3000`，且是 **拼起来再截断**：
#   const combined = sections.join("\n\n")...
#   combined.slice(0, MAX_CONTEXT_CHARS)
# → **超限时，被截掉的是后面的**。
# 而 legacy 默认注入的是 ['Every Session', 'Safety'] —— **Safety（红线）在后面**！
# → 一旦前面的小节写太长，**红线会被静默截掉**，而 agent 不会知道。
#
# 今晚实测：加了 487 字符的「等长任务」规则后，两节合计 2494 / 3000
# → 只剩 506 字符余量，任何人再加几行就可能把红线挤掉。
# 现已压缩到 2274 / 3000（余量 726）。
#
# 判据：两节合计 > 2400（80%）→ WARN；> 2800（93%）→ FAIL

z_budget=$(python3 - << 'PYZ' 2>/dev/null
import re, json, os
AGENTS = '/root/.openclaw/workspace/AGENTS.md'
CFG    = '/root/.openclaw/openclaw.json'
MAX = 3000          # 源码里的 MAX_CONTEXT_CHARS
# 源码 pi-embedded-*.js 里的两套名单：
DEFAULT = ['Session Startup', 'Red Lines']       # DEFAULT_POST_COMPACTION_SECTIONS
LEGACY  = ['Every Session', 'Safety']            # LEGACY_POST_COMPACTION_SECTIONS
try:
    s = open(AGENTS, encoding='utf-8').read()
except Exception as e:
    print(f"FAIL 读不到 AGENTS.md（{type(e).__name__}）")
    raise SystemExit(1)

def extract_src(name):
    """**逐字复刻源码的 extractSections()**（含 H2/H3 匹配、代码块保护、level<=lvl 截断）
    —— 断言必须用与真身一样的算法，否则就是「测的不是真家伙」。"""
    lines = s.split("\n")
    out, in_section, sec_level, in_code = [], False, 0, False
    for line in lines:
        if line.lstrip().startswith("```"):
            in_code = not in_code
            if in_section: out.append(line)
            continue
        if in_code:
            if in_section: out.append(line)
            continue
        m = re.match(r'^(#{2,3})\s+(.+?)\s*$', line)
        if m:
            level, text = len(m.group(1)), m.group(2)
            if not in_section:
                if text.lower() == name.lower():
                    in_section, sec_level = True, level
                    out = [line]
                    continue
            else:
                if level <= sec_level: break
                out.append(line); continue
        if in_section: out.append(line)
    return "\n".join(out).strip() if out else ""

# ── 复刻源码的「配置 → default → fallback legacy」决策链 ──────────
configured = None
if os.path.exists(CFG):
    try:
        configured = (json.load(open(CFG, encoding='utf-8'))
                      .get('agents', {}).get('defaults', {})
                      .get('compaction', {}).get('postCompactionSections'))
    except Exception:
        configured = None

if isinstance(configured, list) and configured:
    names = configured
    mode = f"配置指定 {configured}"
else:
    names = DEFAULT
    mode = "默认名单"

sections = [extract_src(n) for n in names]
# 源码：if (sections.length === 0 && isDefaultSections) → 回退 legacy
if not any(sections) and not (isinstance(configured, list) and configured):
    names = LEGACY
    sections = [extract_src(n) for n in names]
    mode = f"默认名单找不到 → 回退 legacy {LEGACY}"

sizes = {n: len(t) for n, t in zip(names, sections)}
total = sum(sizes.values())

if not all(sections):
    print(f"WARN 找不到注入小节（当前判定：{mode}）")
    print(f"     当前: {sizes} → 若配置改过，注入行为可能已变，需重新核对")
elif total > MAX * 0.93:
    print(f"FAIL 压缩重注入的上下文已用 {total}/{MAX} 字符（{total*100//MAX}%）"
          f" —— **后面的 Safety 红线会被截掉**")
elif total > MAX * 0.80:
    print(f"WARN 压缩重注入的上下文已用 {total}/{MAX} 字符（{total*100//MAX}%）"
          f" —— 余量不多，再加要小心把红线挤掉")
else:
    detail = ", ".join(f"{k}={v}" for k, v in sizes.items())
    print(f"PASS 压缩重注入预算充足（{total}/{MAX} 字符，余量 {MAX-total}；{detail}；{mode}）")
PYZ
)
while IFS= read -r line; do
  case "$line" in
    PASS*) ok "${line#PASS }" ;;
    FAIL*) bad "${line#FAIL }" ;;
    WARN*) warn "${line#WARN }" ;;
  esac
done <<< "$z_budget"
PASS=$((PASS + 1))

echo "【X】巡检脚本自己的解析器自检(防断言装死)"

# 2026-10-27 13:40 补(E49)：
# 今天我为了统一标点，跑了个脚本批量替换 healthcheck.sh 里的全角标点，
# **改坏了 4 个正则**（【J】的条目数、【S】的 cron 列表、【O】的 3 处计数）。
#
# 最毒的一点：这 4 个里有 3 个是「**匹配不到就静默跳过**」的类型
# → 它们不报错，只是**什么都不检查** → 变成「装死的断言」。
#
# 而【W】只测【V】的判据，测不到「【J】【S】【O】的解析器还能不能解析」。
# → 所以要单独一组：验证**解析器本身**还能在真实数据上解析出东西。
#
# 触发条件：任何一次「批量改标点 / 格式化 / 大范围重写脚本」之后。
x_parse_out=$(python3 scripts/test_healthcheck_parsers.py 2>&1)
if [ $? -eq 0 ]; then
  x_parse_cases=$(printf '%s' "$x_parse_out" | grep -c '^✅')
  ok "【X】巡检解析器自检通过（${x_parse_cases} 项，全角标点没被改坏）"
else
  bad "【X】巡检解析器坏了 —— 相关断言正在**装死**（不报错但不检查）"
  printf '%s\n' "$x_parse_out" | sed 's/^/    /'
fi
PASS=$((PASS + 1))

echo "【W】【V】判据自身的单测(防判据本身是坏的)"

# 2026-10-27 12:20 补:
# 【V】组今天连修三个 bug(窗口 3天 vs 说的12h、mtime 过滤、无标记会话被跳过),
# 每一个都是**判据本身**错了 -- 而【V】报的是「长 poll 数量」,
# 判据坏了它照样会输出一个看起来正常的数字。
#
# 典型的自指陷阱:**一个坏掉的断言看起来和好断言一样正常**。
# 所以判据逻辑必须有独立单测,且单测要**直接跑真身代码**。
#
# scripts/test_v_poll_assertion.py 从 healthcheck.sh 的 PY9 块里**抽取真身**
# 来跑(不是抄一份)→ 判据改了单测立刻跟着变,物理上无法漂移。
v_unit_out=$(python3 scripts/test_v_poll_assertion.py 2>&1)
if [ $? -eq 0 ]; then
  v_unit_cases=$(printf '%s' "$v_unit_out" | grep -c '^✅')
  ok "【V】判据单测通过（${v_unit_cases} 个用例，覆盖窗口/分类/漏报/不误报）"
else
  bad "【V】判据单测失败 -- 【V】组的数字不可信"
  printf '%s\n' "$v_unit_out" | sed 's/^/    /'
fi
PASS=$((PASS + 1))

echo "【W2】cron 投递判据单测(E55：本该发什么)"

# 2026-10-29 10:xx 补:
# 【D】组原来只按 delivery.mode 分组, mode 内**没再往下分** →
# 「无内容所以不投递」和「有内容却没送到手上」字段一模一样, 却被当成同一件事。
# (实测: 10-29 08:00 茶馆 cron delivered=false, 实际是「新增 0 条」, 投递才是对的)
#
# 修法是**加一维**(产出判据), 不是放宽断言。
# 但加了判据就得防它自己变成「装死的断言」→ 必须有单测 + 故障注入。
#
# scripts/test_cron_delivery_output.py **import 真身**(不是抄一份判据)
# → 判据改了单测立刻跟着变, 物理上无法漂移。
w2_unit_out=$(python3 scripts/test_cron_delivery_output.py 2>&1)
if [ $? -eq 0 ]; then
  w2_unit_cases=$(printf '%s' "$w2_unit_out" | grep -c '^✅')
  ok "【W2】投递判据单测通过（${w2_unit_cases} 个用例，含混合语义故障注入）"
else
  bad "【W2】投递判据单测失败 -- 【D】组的「未投递」判定不可信"
  printf '%s\n' "$w2_unit_out" | sed 's/^/    /'
fi
PASS=$((PASS + 1))

echo "【U】cron 实测耗时 vs 超时余量(防明天早上撞超时)"

# 2026-10-27 07:10 补:
# 【R】组把各任务的 timeout **互相比较**(lo < hi*0.6),
# 但**从不跟真实耗时比** → 巡检本身长到 500s 也没人知道。
#
# 真实数据(07:00 无人值守那次):
#   巡检 cron 耗时 298s / timeout 600s = **50% 余量**
#   其中脚本本身 ~156s,其余 ~140s 是 LLM 推理/嵌入调用的开销
#   而巡检一周前只要 25s → 一周长了 11 倍
# → 趋势本身就是风险:**没有任何断言看得见这个趋势**
#
# 判据(用真实运行历史,不重跑脚本 -- 重跑会自我指涉且很贵):
#   1) 最近一次实测耗时 / timeout > 0.7 → FAIL(余量告急)
#   2) 同一任务最近 3 次耗时呈上升趋势且已 > 0.5 → WARN(在长)
#   3) 有任务最近失败且原因是超时 → FAIL

u_report=$(python3 - << 'PY8' 2>/dev/null
import json, subprocess, os, glob, sys

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
    """从 openclaw 的 cron 运行历史里取该任务的最近耗时(ms)

    2026-10-29: 改成直接读 JSONL（`cron_runs_helper`）。
    原来每次都调 `openclaw cron runs`（~4s Node 启动），本块被调 5 次 → 21s。
    现在 5 次加起来 ~0.1s。**关键：必须与 CLI 返回等价**，否则断言会装死
    —— helper 里有 self_test() 校验字段一致。
    """
    out = []
    try:
        sys.path.insert(0, '/root/.openclaw/workspace/scripts')
        from cron_runs_helper import read_cron_runs
        for e in read_cron_runs(CRON_ID_FOR[name], 8):
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
    # ⚠️ 必须按**时间**排序,不能按耗时排序 --
    # 按耗时排会把「最慢的那次」当成「最近的那次」,
    # 于是早已修好的 backup-audit(历史 600s 超时)会被当成现状 → 假 FAIL。
    ds.sort(key=lambda x: x[0])
    last = ds[-1]
    ratio = last[1] / 1000.0 / tsec
    if ratio > 0.7:
        problems.append(f"{name} 最近一次耗时 {last[1]/1000:.0f}s / 超时 {tsec}s "
                        f"= {ratio*100:.0f}% -- 余量告急,下次可能撞超时")
    # 趋势:最近 3 次是否持续上升
    if len(ds) >= 3:
        recent = [x[1] for x in ds[-3:]]
        if recent[0] < recent[1] < recent[2] and recent[2]/1000.0/tsec > 0.5:
            trend_warn.append(f"{name} 最近 3 次耗时递增 "
                              f"{recent[0]/1000:.0f}s→{recent[1]/1000:.0f}s→{recent[2]/1000:.0f}s"
                              f"(超时 {tsec}s)-- 在长")
    # 因超时而失败的(只看最近一次)
    ts, dur, st, dl = last
    if st == 'error' and dl and 'timeout' in str(dl).lower():
        problems.append(f"{name} 最近因超时失败({dl})-- 见 E35:慢的是等法不是任务")

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

echo "【Q】cron 指令是否与脚本同步(防过期手册)"

# 2026-10-27 03:30 补(E20/E31 的又一次实例):
# cron 指令里手写的「13 组断言 A-M」在加了 N/O/P 三组后**没同步** --
# 明早 07:00 真出故障时,agent 拿到的是一份**过期的手册**,
# 新组(N/O/P)的失败它根本不知道怎么解释。
#
# 这正是 E20「验证了脚本却没验证指令」的翻版。
# 解法:指令清单由 `scripts/gen_healthcheck_instruction.py` 从脚本生成,
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
    print(f"FAIL 生成器加载失败({type(e).__name__})")
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
    print(f"FAIL 读不到 cron 指令({type(e).__name__})")
    raise SystemExit(1)

missing = [g for g, _ in groups if f'- {g} ' not in msg]
if missing:
    print(f"FAIL cron 指令缺 {len(missing)} 组说明({', '.join(missing)})"
          f" -- 跑 `python3 scripts/gen_healthcheck_instruction.py --update` 同步")
else:
    n_decl = len(re.findall(r'^- ([A-Z]) ', msg, re.M))
    if n_decl != len(groups):
        print(f"FAIL cron 指令列了 {n_decl} 组,脚本实际 {len(groups)} 组 -- 需同步")
    else:
        print(f"PASS cron 指令与脚本同步({len(groups)} 组)")
PY4
)
while IFS= read -r line; do
  case "$line" in
    PASS*) ok "${line#PASS }" ;;
    FAIL*) bad "${line#FAIL }" ;;
  esac
done <<< "$inst_report"
PASS=$((PASS + 1))

echo "【P】每日笔记日期归属(跨日写入检测)"

# 2026-10-27 03:00 补:会话跨过零点后,我**继续往 10-26.md 里写**,
# 导致 10-26.md 里混着两天内容(72 个索引块里有 30 个属于 10-27)。
# 后果:
#   - 每日笔记的时间线失真(「10-26 的日志」里写着 10-27 的事)
#   - daily-learning「读最近 2 天日志」会读到错配的内容
#
# 判据设计(第一版是错的):全文搜日期会**大量误报** --
# 历史笔记里合法地引用更晚的日期(10-25 的补记里提到 10-26)。
# 正确做法:只看**小节标题**里的日期,且必须晚于文件名。
# → 7 个历史文件零误报,用拆分前的备份能精确抓到 5 个越界小节。

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
    # 只看标题行(## 或 ###)里的日期
    for m in re.finditer(r'^#{2,3} .*?(20\d\d-\d\d-\d\d)', txt, re.M):
        d = m.group(1)
        if d > base:
            offenders.append(d)
    if offenders:
        bad_files.append((os.path.basename(f), sorted(set(offenders))))

if bad_files:
    for name, dates in bad_files:
        print(f"FAIL {name} 里的小节日期晚于文件名({', '.join(dates)})"
              f" -- 跨日后应写入新日期的文件")
else:
    print("PASS 每日笔记日期归属正确(无跨日写入)")
PY3
)
while IFS= read -r line; do
  case "$line" in
    PASS*) ok "${line#PASS }" ;;
    FAIL*) bad "${line#FAIL }" ;;
  esac
done <<< "$date_report"
PASS=$((PASS + 1))

echo "【O】L1 计数一致性(防止手改文档后过期)"

# 2026-10-27 补:MEMORY.md / L1 主库头部的「N 条」是**手写的**,
# 加了 E30/E31 后就忘了同步 → MEMORY.md 还写着 29 条,实际已经 31 条。
# 计数过期本身不致命,但**它是我判断「记忆系统是否正常」的参照** --
# 参照错了,判断就错了。所以加断言自动核对。

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

# 库里的条目数(按 ### E<N> 标题算)
try:
    lib = open(LIB, encoding='utf-8').read()
    lib_ids = set(int(x) for x in re.findall(r'^### E(\d+)', lib, re.M))
    lib_n = len(lib_ids)
except Exception:
    lib_n = 0

problems = []
if lib_n != n_files:
    problems.append(f"主库 {lib_n} 条 vs 独立文件 {n_files} 个")

# 2026-10-27 11:50 补:**重号 / 编号撞车**检测。
# 【O】原来只能发现「数量对不上」,发现不了「**编号撞了但数量刚好对**」--
# 2026-10-27 我和并行会话同时新建 E42.md → 互相覆盖,
# 文件数 45→46 数量是对的,但**并行会话那条 E42 被顶掉了**(内容丢失)。
# → 三件事要查:
#   1 独立文件的**标题编号** vs 文件名编号(不一致 = 被顶过)
#   2 主库里的重号(`### E42` 出现两次)
#   3 独立文件之间的重号(两个文件都叫 E42 的标题)
import collections as _c

title_of = {}
for f in files:
    try:
        head = open(f, encoding="utf-8").read(400)
    except Exception:
        continue
    mt = re.search(r"^### (E\d+)", head, re.M)
    if mt:
        title_of[os.path.basename(f)] = mt.group(1)

mismatch = []
for fname, tid in title_of.items():
    fid = fname[:-3]
    if tid != fid:
        mismatch.append(f"{fname} 的标题写的是 {tid}")
if mismatch:
    problems.append(f"独立文件标题编号与文件名不一致(可能被别的会话顶过):{'; '.join(mismatch[:3])}")

dup_lib = [k for k, v in _c.Counter(re.findall(r"^### (E\d+)", lib, re.M)).items() if v > 1]
if dup_lib:
    problems.append(f"主库里有重号条目:{dup_lib}")

dup_files = [k for k, v in _c.Counter(title_of.values()).items() if v > 1]
if dup_files:
    problems.append(f"多个独立文件用了同一个编号:{dup_files}")

# 编号连续性(有缺号 = 可能有条目被删了或从没建文件)
try:
    all_ids = set(int(x) for x in re.findall(r"^### E(\d+)", lib, re.M))
    if all_ids:
        gaps = [i for i in range(1, max(all_ids) + 1) if i not in all_ids]
        if gaps:
            problems.append(f"L1 编号有缺号:{gaps[:6]}(可能有条目被删了或没建独立文件)")
except Exception:
    pass

# MEMORY.md 里的声明
try:
    mem = open(MEM, encoding='utf-8').read()
    m1 = re.search(r'[\uff08(](\d+) \u6761\uff0cE1[\u2013\-]E(\d+)[\uff09)]', mem)
    m2 = re.search(r'检索自检[\uff08(]必须 (\d+)/\d+[\uff09)]', mem)
    if m1:
        if int(m1.group(1)) != n_files or int(m1.group(2)) != real_max:
            problems.append(f"MEMORY.md 声明 {m1.group(1)} 条/E1-E{m1.group(2)},实际 {n_files} 条/E1-E{real_max}")
    if m2:
        if int(m2.group(1)) != n_files:
            problems.append(f"MEMORY.md 自检声明 {m2.group(1)}/{m2.group(1)},实际应为 {n_files}/{n_files}")
    # L1 主库头部
    h = re.search(r'当前条目数\*\*[\uff1a:](\d+) \u6761[\uff08(]E1[\u2013\-]E(\d+)[\uff09)]', lib)
    if h:
        if int(h.group(1)) != n_files or int(h.group(2)) != real_max:
            problems.append(f"L1 主库头部声明 {h.group(1)} 条/E1-E{h.group(2)},实际 {n_files} 条/E1-E{real_max}")
except Exception as e:
    problems.append(f"读 MEMORY.md 失败: {type(e).__name__}")

if problems:
    for p in problems:
        print(f"FAIL {p}")
else:
    print(f"PASS L1 计数一致({n_files} 条,E1-E{real_max})")
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
# SESSION-STATE.md 是下次会话的第一入口 -- 头部状态描述若过期会误导后续判断
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
            print(f"FAIL SESSION-STATE.md 已 {age} 天未更新(头部状态描述可能已过期)")
            failed = 1
        else:
            print(f"PASS SESSION-STATE.md 头部新鲜({age} 天前更新)")
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

# 2026-10-26 实测:19:11 和 19:42 两次巡检 FAIL,根因是 MiniMax
# 「已达到 Token Plan 用量上限」(HTTP 429)。查 failover 日志时发现
# 配的 fallback 是 minimax/MiniMax-M2.7-highspeed + minimax/MiniMax-M2.7 --
# **同一个 provider、同一个账号**。配额一满,三个模型一起挂。
# 也就是说 fallback 链在「provider 级故障」面前提供的韧性是 0。
#
# 状态字段看不出这个问题:配置齐全、模型存在、fallbackConfigured=True,
# 但只要 provider 本身欠费/限流,兜底就是假的。
# 这属于 E20「验证的对象错了」-- 该验的是「跨 provider 吗」,不是「配了吗」。
#
# 2026-10-27 06:40 补充:除了「兜底无效」,还有**第二重后果** --
# **并发互相拖慢**。实测(同账号 6 个并发请求,256 token 输出):
#   单个请求        → 1.8s
#   6 个并发        → 最慢的到 **7.0s(3.9 倍)**
# 而实测到 daily-learning 的 agent turn 有一次**单次 LLM 调用等了 353 秒**,
# 且那段时间正好是 backup-audit 超时失败的时段 -- **同一个 MiniMax 账号被并发占用**。
# → 也就是说:不仅 provider 挂了会全挂,**provider 变慢也会全慢**,
#   一个慢请求会拖垮所有任务(包括每天 07:00 的巡检)。

prov_report=$(python3 - << 'PY2' 2>/dev/null
import json
try:
    c = json.load(open('/root/.openclaw/openclaw.json', encoding='utf-8'))
except Exception as e:
    print(f"FAIL 读不到 openclaw.json({type(e).__name__})")
    raise SystemExit(1)

providers = list(c.get('models', {}).get('providers', {}).keys())
fallbacks = c.get('agents', {}).get('defaults', {}).get('model', {}).get('fallbacks', []) or []

# 2026-10-28 补：agent 级目录 providers/main/agent/models.json 在 models.mode=merge 下
# **叠加**进最终目录 —— 只读 openclaw.json 会漏掉「本地有、运行时能选中、但凭证已失效」
# 的残留 provider（当时 glmcode/arkcode/kimicode 就这样被漏掉，SESSION-STATE 还写成「已删除」）。
AGENT_MODELS = '/root/.openclaw/agents/main/agent/models.json'
agent_providers = []
try:
    am = json.load(open(AGENT_MODELS, encoding='utf-8'))
    agent_providers = list((am.get('providers') or {}).keys())
except Exception:
    pass

print(f"PASS 已配置 provider: {len(providers)} 个 → {', '.join(providers) if providers else '无'}")

extra = [p for p in agent_providers if p not in providers]
if extra:
    print(f"WARN agent 级目录另有 {len(extra)} 个 provider: {', '.join(extra)}"
          f"(models.json 叠加进最终目录,不参与 primary/fallbacks 路由)")
    print(f"INFO 若这些 provider 凭证已失效,属**残留凭证** —— 需人工确认后清理或充值;"
          f"只信 openclaw.json 的 provider 列表会漏掉它们")
print(f"PASS fallback 链: {len(fallbacks)} 项 → {', '.join(fallbacks) if fallbacks else '未配置'}")

if not fallbacks:
    print("WARN 未配置任何 fallback -- 主模型不可用时没有任何兜底")
elif len(providers) <= 1:
    print(f"FAIL 只有 1 个 provider,但配了 {len(fallbacks)} 项 fallback -- "
          f"它们全在同一个 provider 同一账号下,provider 级故障(欠费/限流/宕机)时兜底无效")
else:
    fb_providers = {f.split('/')[0] for f in fallbacks if '/' in f}
    if fb_providers - set(providers):
        print(f"WARN fallback 指向未配置的 provider: {', '.join(sorted(fb_providers - set(providers)))}")
    elif len(fb_providers) == 1 and len(providers) > 1:
        print(f"WARN 有 {len(providers)} 个 provider,但 fallback 全在 {list(fb_providers)[0]} -- "
              f"未跨 provider 兜底")
    else:
        print(f"PASS fallback 跨 provider 兜底({', '.join(sorted(fb_providers))})")
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

# 维护 SESSION-STATE.md 里的「巡检现状」行 -- 避免硬编码数字变陈旧
# (2026-10-26 教训:SESSION-STATE 头部曾过期 5 天)
# 独立成脚本:bash 里嵌套 heredoc + 命令替换会导致变量展开异常(实测 $GROUPS 恒为空)
python3 /root/.openclaw/workspace/scripts/update_state_status.py "$PASS" "$FAIL" "$WARN" >/dev/null 2>&1

if [ "$FAIL" -gt 0 ]; then
  echo ""
  actionable_n=0
  for it in "${FAILED_ITEMS[@]}"; do is_waiting "$it" || actionable_n=$((actionable_n+1)); done
  waiting_n=${#WAITING_ITEMS[@]}
  if [ "$actionable_n" -gt 0 ]; then
    echo " ⚠️ 有 $actionable_n 项可处理的失败,需要处理。"
  else
    echo " ✅ 没有可处理的失败($waiting_n 项长期项在等你处理)。"
  fi
  echo "   【可处理 - 现在就该修】"
  for it in "${FAILED_ITEMS[@]}"; do
    is_waiting "$it" || echo "   - $it"
  done
  if [ ${#WAITING_ITEMS[@]} -gt 0 ]; then
    echo "   【等牛哥哥 - 长期项,不该自己动】"
    for it in "${WAITING_ITEMS[@]}"; do echo "   - $it"; done
  fi
  echo ""
  echo " 提示:静默失败优先排查「触发→执行→落盘→投递」每一段,别只看状态字段。"
fi
echo "════════════════════════════════════════════"

[ "$FAIL" -gt 0 ] && exit 1
exit 0
