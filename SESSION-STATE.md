# SESSION-STATE.md

> **最后更新**: 2026-11-16 14:20（14:35 补记见下）
> **状态**: ✅ **11-16 14:20 心跳（把「额度闸门」架构做完整 + 抓到一个我自己引入的「断言装死」）**：本轮把上一轮的闸门做完整，并**发现上一轮的做法埋了一个新的静默失效**。
> **① 闸门扩展到 daily-reflection** —— 实测它的失败形态与 daily-learning **完全相同**（`All models failed (3)` + 148~312s 空转 + 零产出）。`scripts/daily_learning_gate.py` **泛化**为 `--job` 多任务共用（**不复制第二份**，E11）；新增 `openclaw-daily-reflection.timer`（周日 07:00, Persistent）；它的 cron 调度也禁用。漂移检查 50 → **57 项**。顺带查到一件事实：**daily-reflection 从未在计划槽位上成功过**（4 次成功全是手动补跑）。
> **② 抓到我自己引入的「断言装死」回归** ⚠️ —— 两个任务都禁用后，`openclaw cron list` 返回 **「No cron jobs」**；而 `check_cron_delivery.py` 原来是从 `cron list` 取列表的 → `for j in jobs:` 循环体**一次都不跑** → `__FAILED__=0` → **【D】组一条断言都没检查，却显示「全部通过」**。**这是我上一轮改架构时顺手带出来的**，而且它恰好落在「报告说没问题、检查对象却不在视野里」这个家族上（E69 探针撒谎 / E71 分类降级 / 【X】解析器装死 / E73 终点取错）。修法：① 改读 `cron/jobs.json`（同一份数据源，不按 enabled 过滤）；② 加**显式失败分支** —— 一个任务都没读到就报「本次**什么都没检查**（断言装死）」并 exit 1；③ 新增单测 `scripts/test_cron_delivery_no_jobs.py`（7 用例，**含真实故障注入**：把存储改名 + 伪造 `cron list` 返回空 → 断言必须返回非 0）。
> **③ 巡检 PASS=78 FAIL=0 WARN=4 等你=2** · 漂移 57/57 ✅ · 回归全绿（解析器 ✅ ·【V】13 ✅ ·【W2】30 ✅ ·【W3】12 ✅ ·【W4】14 ✅ · 额度分类 30 ✅ · 预检 18 ✅ · E71 17 ✅ ·【U】自检 ✅ · 断言装死 7 ✅）· L1 库 **73 条（E1–E73）**、检索 73/73 PASS · 7 个 systemd timer 全部按期 · 今日笔记两处写入齐全 · 茶馆无新内容。git clean。
> **本轮的核心教训（两条，同一个家族）**：**「没检查」和「检查通过」在报告里长得一模一样。** 上一轮我把「cron 任务改成 disabled」当成纯粹的架构调整，没意识到 **`cron list` 是巡检的输入源** —— 架构一变，巡检的**视野**就变了，而巡检自己毫无察觉地继续报绿。→ **凡是「把对象从某个列表里移走」的改动，都要问「谁在拿这个列表当输入？」** —— 这次的回答是：巡检的【D】组和【S】组都在拿它。
> **顺带修正我 09:15 的一个错误结论**：我曾说 6 天失败是「3 次重试各重注入 60k 上下文 → 放大额度消耗」。**查实后不成立** —— 那 3 轮是 **fallback 链**（M3 → M2.7-highspeed → M2.7），每模型 4 次尝试共 12 次，且 `usage` **全为 0**（请求在计费前就被拒，**没烧 token**）。真正的病灶是另一件事：**全程 0 次工具调用 → preflight 一次都没跑到** → **闸门装在了它要守的东西里面**（额度耗尽时 agent 连第一个工具调用都发不出去）。
## 🔍 14:50 心跳补记（修「两个组件用不同数据源」—— 连带修掉【S】【X】两个 FAIL）

**14:20 那轮把两个 cron 任务改成 disabled（由 systemd 闸门触发）后**，
本轮巡检报 **FAIL=3**：【S】漏了 cron 任务 `['daily-learning','daily-reflection']` +
【X】解析器坏了（连带失败，它断言「声明列表非空」）。

**根因**：**两个组件对「有哪些任务」用了不同的数据源** ——
「系统状态」生成器读 `openclaw cron list`（disabled 后返回**空**）→ 渲染出空列表；
【S】断言读 `cron/jobs.json`（仍有 2 个）→ 判定「漏了」。**结论必然打架。**

**修法**：① 生成器改读 `cron/jobs.json`（**真实存储**）；
② **disabled 的也列出来**并标注「由 systemd 闸门触发」——
它们是闸门的触发目标，藏起来会让下一个会话以为这俩没人管了；
③ 新增**回归防护两条**（已做**故障注入**验证：注入原 bug → 自检立刻 FAIL 并指出
「生成器 [] vs jobs.json [两个]」→ 还原即 PASS）：
生成器读出的任务集合 **必须 == jobs.json 的集合**；
**disabled 的任务必须列在状态里**。

**教训**：**「两个组件用了不同的数据源」= 迟早打架。**
凡是有**两处**在回答同一个问题（「有哪些 X」），就要断言它们**同源**。
这与 14:20 那轮「`cron list` 是巡检的输入源」是同一条线的延续 ——
上轮把**列表内容**变了，这轮把**列表的来源**也变了，而断言那边没跟上。

**巡检现状**：**PASS=80 FAIL=0 WARN=4 等你=2**（31 组）· 回归全绿（11 套）·
L1 库 74 条、检索 74/74 PASS。

## 🔍 14:35 心跳补记（给闸门接上「跳过」信号 —— E74）

**复查 14:20 那轮的闸门架构时，发现一个它自己没接上的洞**：
闸门把 `GATE_RESULT=skipped reason=quota` 写进
`memory/healthcheck-logs/<name>-gate-*.txt`，**而巡检从来不读那些文件**。
→ 额度**长期**枯竭的话，闸门会一天天跳过，**巡检一条都不提** ——
额度问题又退化成「背景噪音」（正是 E71 那个坑的翻版）。

**本轮产出**：① **新提升 L1 E74（第 74 条）**「闸门太安静就没人在乎，连续跳过必须有人读」
→ 核心断言：**故障被修好** 和 **故障被看见** 是两件事；
**降噪的同时可能顺手把告警降没了** —— 闸门让故障修好了，但信号也跟着消失了。
② **新增巡检【G2】组**（判据真身 `scripts/check_gate_skips.py`，E11 不抄判据）：
读闸门日志 → 统计**最近连续跳过次数** → **连续 ≥3 次 → FAIL**，1-2 次 → WARN；
用**结构化标记** `GATE_RESULT=` 而非 grep 自然语言。
③ **单测 11 用例全 PASS**，含**真实故障注入**。
④ **【G2】把巡检组数 30 → 31**（说明清单已同步，【Q】PASS）。

**⚠️ 本轮自己踩了两次「判据装死」，都被自检/注入当场抓住（都值得记）**：
- **第一次**：判据按**完整文件名**分组，而不是按**闸门名**分组
  → 每份日志自成一组，「连续跳过」永远算成 1 次 → **自检当场 FAIL**；
- **第二次**：正则用 `^GATE_RESULT=` + `re.M`，而真实日志行是
  `[daily-learning-gate] GATE_RESULT=skipped ...`（**行首有前缀**）→ `^` 匹配不上
  → **故障注入 3 份 skipped 日志，判据仍报「尚未运行过」= 装死**
  → 注入当场抓到，已把「真实文件注入」写成第 8 个用例防复发。
→ **教训**：**判据写完必须用真实数据故障注入验证** —— 单元测试里的假数据
  恰好绕开了正则的边界（E11 的老教训又一次）。

回归全绿（解析器 ✅ ·【V】13 ✅ ·【W2】30 ✅ ·【W3】12 ✅ ·【W4】14 ✅ ·
额度分类 30 ✅ · 预检 18 ✅ · E71 17 ✅ · 装死 7 ✅ ·【U】自检 ✅ ·【G2】11 ✅）·
L1 库 **74 条（E1–E74）**、检索 **74/74 PASS**（E74 排名第 1 · 0.4431）·
两个闸门 timer 已装（daily-learning 明天 09:00 / daily-reflection 周日 07:00）·
闸门 `--check` 实跑 ✅ 额度预检通过（当前额度可用）。

**仍需牛哥哥决策的长期项（未变）**：MiniMax Token Plan **额度周期性耗尽** ——
现在两个 agentTurn 任务都有闸门保护（额度不够就秒级跳过，不再空转 300s），
且**「连续跳过」已被巡检接住**（E74），但**额度本身还是得充值**
（充值/升级，或加第二把 key 拆配额）· fallback 链 2 项同 provider 同账号 →
provider 级故障兜底无效（【M】）· 4 个死亡 provider 凭证待清理。

> **新架构（现在长这样）**：
> ```
> systemd timer（09:00 / 周日 07:00）
>    └─ daily_learning_gate.py（纯脚本 · 不烧模型额度 · 额度耗尽也照样能跑）
>          ├─ 预检不通过 → 记 GATE_RESULT=skipped，**不启动 agentTurn**（exit 0，不是故障）
>          └─ 预检通过   → openclaw cron run <job> → agentTurn 真正执行
> cron 里的两个任务：enabled=false（只作为被触发的目标，不再自触发）
> ```
> **仍需牛哥哥决策的长期项**：MiniMax Token Plan **额度周期性耗尽** —— 现在**两个 agentTurn 任务都已经有闸门保护**（额度不够就跳过，不再空转 300s），但**额度本身还是得充值**（充值/升级，或加第二把 key 拆配额）· fallback 链 2 项同 provider 同账号 → provider 级故障兜底无效（【M】）· 4 个死亡 provider 凭证（arkcode/glmcode/qwen/kimicode）待清理。
> **新记的待办（不阻塞）**：① 闸门目前只判「够/不够」，**不记录「这次被跳过了」到结构化文件** → 建议给闸门写一个 `memory/gate-log.jsonl`，让「连续 N 次被跳过」也能被巡检看见（否则闸门太安静，额度长期枯竭又变成背景噪音 —— 正是 E71 那个坑）；② preflight 的结论还没写进 cron 的 run summary → 让失败**可归因**；③ 【U】基线样本可考虑 P75 或扩大窗口。

<details><summary>11-16 09:15 心跳原始记录（E71 落地 + 预检 529/429 分家 + 补 5 天笔记）</summary>

> **最后更新**: 2026-11-16 09:15
> **状态**: ✅ **11-16 09:15 心跳（修掉「连续失败被判成 WARN」的降级 —— E71 落地 + 预检 529/429 分家 + 补 5 天笔记）**：本轮**没有靠等额度恢复**，而是先把「额度问题掩盖了 6 天故障」这件事**查到底并修掉**。
> **发现（连起来看才对）**：① 11-12～11-16 **连续 5 天**的 `memory/YYYY-MM-DD.md` **全部不存在**（daily-learning 每天 09:00 挂在 rate_limit）；② 11-15 的 cron 会话里 **3 次重试**，每次**重新注入整份 ~60k token 上下文**（skills policy + 完整 cron payload + AGENTS/SOUL/TOOLS/MEMORY）却**一个 token 产出都没有**（`usage` 全 0）→ **重试本身在放大额度消耗**；③ 11-16 09:00 那次**真的跑通了**（usage 正常、笔记已写、E71 已入库）→ 说明失败不是「写不出来」，是**额度桶在 09:00 恰好是空的**；④ 11-16 09:00 那次**自己发现了预检脚本的 bug**：整点上游返回 **HTTP 529（overloaded_error）**，而 `quota_preflight.py` 把 529 和 429 一起判成「额度不足」→ **整点被「整点高峰」自己挡住**。
> **本轮产出**：① **修 `scripts/quota_preflight.py`** —— 拆开三种失败模式：**429 + Token Plan/insufficient balance → 额度耗尽（不重试，等充值）** · **429 + 普通 rate limit → 普通限流（等 1-2 分钟）** · **529 → 上游瞬时过载（退避后自动重试 3 次）**；新增 `_is_quota_signal()` 看**报文特征**而非只看 HTTP 码；结论段也分成三种建议（不再把 529 说成「要充值」）；新增单测 **`scripts/test_quota_preflight.py`（18 用例全 PASS）**。② **修 `scripts/check_cron_delivery.py`（E71）** —— 「连续失败 ≥3 次」从 WARN **升级为 FAIL**，且断言的是「**已中断 N 天**」（新增 `_outage_span_days()`，**按日历跨度算而不是按次数算**：每小时跑的任务连挂 6 次 ≠ 中断 6 天）；**额度类原因文本带「额度耗尽」→ 被 healthcheck 的 `is_waiting()` 归进「⏳ 等你」**，不会变成「可处理 - 现在就该修」的假警报；**不重复报**（连续 ≥3 时只报「持续中断」那一条，同一事实报两遍 = 噪音，E66）；新增单测 **`scripts/test_e71_sustained_outage.py`（16 用例全 PASS）**。③ **补齐 11-12～11-16 五天笔记**（11-12～11-15 是 09:00 那次运行**自愈补记**的，我核对了内容与索引两处都齐、5 个文件 `--check` 全 PASS）。④ 巡检复跑 **PASS=74 FAIL=1**，唯一 FAIL 是本文件过期 → **本轮已刷新（即此条）**。⑤ 回归全绿：解析器 ✅ ·【V】13 ✅ ·【W2】30 ✅ ·【W3】12 ✅ ·【W4】14 ✅ · 额度分类 30 ✅ · **预检 18 ✅** · **E71 16 ✅**。
> **关键教训（E71 的根因，比结论更值钱）**：**「测得准」不等于「会被处理」** —— 连续 6 天失败这件事，巡检**每天都算出来、写进报告**，但因为它落在 WARN 区，而 WARN 不进「现在该怎么修」，于是**信号准 + 分类把它降级 = 每天都报、天天没人动**。这和 E69（探针端点撒谎）、E70（快照 vs 状态）是同一条线上的第三个变体：**信号在，但没接到「该修」的那根线上**。→ **给巡检加断言时，要同时问「它会落到哪一栏」**。
> **今日巡检**：全量 **PASS=74 FAIL=1 WARN=5 等你=3**（30 组，FAIL 即本文件过期，已修）· 03:00 backup-audit **PASS=18 FAIL=0 WARN=1** · 记忆索引 **101/101 文件 · 717 块 · Dirty=no** · Embeddings **ready**（当前实跑 `openclaw memory search` 命中 ✅）· 记忆检索端到端可用 · L1 库 **71 条（E1–E71）** · 茶馆 **无新内容**（最新摘要仍是 10-26，不硬凑笔记）· 7 个 systemd timer 按期。
> **仍需牛哥哥决策的长期项（未变）**：MiniMax Token Plan **额度周期性耗尽**（充值/升级 或 加第二把 key 拆配额）—— **daily-learning 已连续 6 次、daily-reflection 已连续 12 次**被它打中，**这是当前最高优先级**（本轮已把它从「每天报一遍没人动」升级成「一眼就能看到中断了几天」）；fallback 链 2 项同 provider 同账号 → provider 级故障兜底无效（【M】）；4 个死亡 provider 凭证（arkcode/glmcode/qwen/kimicode）待清理。
> **新记的待办（不阻塞）**：① **daily-learning 的 3 次模型级重试会重新注入整份上下文**（11-15 那次 3 次重试 ≈ 180k token 却零产出）→ 这会**放大**额度消耗，值得给 cron 加 `maxRetries: 1` 或换更小的模型跑。② 预检已能在 529 时重试，但 **cron 指令里还没要求「预检重试仍失败就直接放弃并记录」** → 建议把 preflight 的结论写进 run summary，让失败**可归因**。

<details><summary>11-10 21:05 心跳追加（quick-health 假警报归类修复 · E69 同源）</summary>

<details><summary>11-10 15:44 心跳追加（quick-health 假警报归类修复 · E69 同源）</summary>
> **本轮产出**：① 补写 `memory/2026-11-10.md`（**184 汉字**）+ `memory/daily-notes-index.md`（**198 汉字**），两处 `--check` 双 PASS —— 09:00 daily-learning 因额度耗尽失败且**未补跑**，当天笔记是缺的；② **新提升 L1 E69**（第 69 条）：「**健康端点返回的是硬编码的 ok —— 探针没坏，被探的对象在撒谎**」→ E63 的反面变种（E63 是探针坏了，本条是探针完全正常但目标在自我认证，E63 的自检照常通过）；`verify_l1_retrieval.py` **69/69 PASS**，E69 检索**排名第 1**（主库 0.8079）/ 第 2（独立文件 0.7783）；③ 全量巡检复跑 **PASS=75 FAIL=0 等你=3**（两个 FAIL 消失）；④ **09:00 daily-learning 仍无补跑**（明早 09:00 计划槽位会自愈）。
> **今日巡检**：全量 **PASS=75 FAIL=0 WARN=5 等你=3**（30 组）· 03:00 backup-audit **PASS=19 FAIL=0** · 记忆索引 **97/97 文件 · 698 块 · Dirty=no** · Embeddings ready · 记忆检索端到端可用（命中 MEMORY.md:1）· 茶馆 08:15 拉 96 条 issue **新增 0**（无新内容，不硬凑笔记）· 4 个 systemd timer 按期。
> **仍需牛哥哥决策的长期项（未变）**：MiniMax Token Plan 额度周期性耗尽（充值/升级 或 加第二把 key 拆配额）· fallback 链 2 项同 provider 同账号 → provider 级故障兜底无效（【M】）· 4 个死亡 provider 凭证待清理（等牛哥哥点头）。

<details><summary>11-10 15:44 心跳追加（quick-health 假警报归类修复 · E69 同源）</summary>

> **发现**：`logs/quick-health.log` 从 06:12 起**每 30 分钟**报「**没有任何可用 provider** —— 整个系统无法推理」FAIL，但**系统当时明明能推理**（心跳会话本身就跑在上面）。查 journal 拿到 14:58 的真实报错：
> `⚠️ WARN minimax 限流（HTTP 429）{"type":"rate_limit_error","message":"已达到 Token Plan 用量上限：请升级 Token Plan 套餐或购买积分补充用量。 (2056)"}`
> → **和今天所有故障同一个根因**（MiniMax Token Plan 额度），但**分类错了**：额度信号在**WARN 那行**，被分类的却是「没有任何可用 provider」+「provider 检查未通过」这两条 FAIL —— **它们不含任何额度字样** → 额度耗尽被当成「可处理 - 现在就该修」，**天天刷假警报**。
> **这是 E69 的同源问题**：信号存在，但**不在分类器读的那条路径上**（E69 是探针端点撒谎，这条是信号在隔壁行）。
> **已修**：① `check_provider_health.py` 的「没有任何可用 provider」区分**额度耗尽** vs **真全挂**；② `quick_health.py` 提取子脚本输出里的额度特征（`quota_hint`，覆盖 Token Plan/额度/余额/rate_limit_error/insufficient balance）**带进 FAIL 消息** → 命中 WAITING_ON_USER。
> **已验证**：故障注入 → 额度场景两条 FAIL 均归「等你」✅；**真故障仍算「可处理」**（E59：不能为降假警报把真故障藏起来）✅；实跑 quick-health **可处理 0 / 等用户 3** ✅。
> **新增单测**：`test_quota_classification.py` 加【6】组 **9 个用例**（17→25，全 PASS）—— 防「信号不在分类路径上」复发。
> **回归全绿**：解析器 ✅ · 额度分类 25/25 ✅ · 【V】13 ✅ · 【W3】12/12 ✅ · 【W2】30/30 ✅ · 【W4】14 ✅。
>
> **⚠️ 同日修正（21:05 心跳实测发现）**：19:06 那次 quick-health 的 journal 显示**只修好了一半** ——
> `provider 检查未通过 —— 额度耗尽（需充值/升级 Token Plan）` 已归「等你」✅，
> 但 `**没有任何可用 provider** —— 整个系统无法推理` **仍漏在「可处理」**❌（结果里 `可处理 1 / 等用户 4`）。
> **根因**：判定条件写成了 `any("Token Plan" in w for w in warned)`，
> 而 `warned` 列表里存的是 **provider 名字**（"minimax"）→ **永远不含 "Token Plan"** → 分支永不触发。
> 额度特征其实在**报错明细**（`限流（HTTP 429）{...Token Plan 用量上限...}`）里，名字里没有。
> **修法**：新增 `quota_hit` 标志，**只看报错明细**（Token Plan / 用量上限 / insufficient balance / 余额 / 额度），
> 且 `bad` 与 `warn` 两种状态都算 → 402「配额耗尽」也能正确归类。
> **教训**：判据写完**必须用真实故障注入验证**——上一轮我只做了「字符串是否含关键词」的断言，
> 那验证的是**文案**，不是**逻辑**；真跑一次才知道分支没触发。
> **已加断言**：真身判定 4 个场景（429+Token Plan → 额度 · 402 配额 → 额度 · 401 凭证 → 真故障 · 500 服务端 → 真故障），
> 直接 import 真身跑逻辑（不抄判据，E11）+ 断言源码里不再出现 `for w in warned`。
> 单测 **25 → 30 全 PASS** · 六套回归全绿 · 真身实跑两种场景措辞均正确。

</details>

<details><summary>11-09 15:32 心跳原始记录（E68 补齐 + 【A2】加固）</summary>

> **最后更新**: 2026-11-09 15:40
> **状态**: ✅ **11-09 15:32 心跳（补齐 E68 的另一半 + 加固【A2】）**：上一轮把 E68「先落盘再总结」只加给了 `daily-reflection`，**但 `daily-learning` 同样是受害者**（历史上丢过 **7 份**笔记，同样是「声称写了但文件不存在」）→ 本轮补上，且它的暴露更高：**它要写两处**（当日笔记 + 索引），任何一处写完就可能被限流打断 → 顺序明确为「**写当日笔记 → 确认文件在 → 写索引 → 确认索引里有 → 再输出总结**」。已验证磁盘 + 网关内存态同步 ✅ · job 仍 `ok/delivered/consecutiveErrors=0` ✅ · 【T】组（cron 指令完整性）✅。
> **本轮同时加固了【A2】断言**：补第 3 条「**索引漏写检测**」—— 原来只查当日笔记文件，**daily-learning 要求「两处写入」，索引那一路没查** → 索引漏写不会被发现。⚠️ 范围控制是关键：索引 11-03 才建、覆盖起点 10-26，**不按索引自己的覆盖区间判会全是假阳性**（我先撞过这个坑）。**已做故障注入验证**：删掉索引里的 11-07 小节 → 断言立刻 FAIL，还原后 PASS。
> **今日巡检**：全量 **PASS=77 FAIL=0 等你=2**（30 组）· quick-health 连续 8 次**可处理 0 / 等用户 2**（15:14）· 回归全绿（解析器 ✅ · 额度分类 17/17 ✅ · L1 68/68 ✅）· 记忆索引 **97/97 文件 · 698 块 · Dirty=no** · Embeddings ready · 茶馆 08:15 / 12:15 拉 96 条 issue **新增 0** · 4 个 systemd timer 按期。
> **下方保留**：11-09 14:25 心跳原始记录（E68 来源 · daily-reflection 修复全过程）。

</details>

<details><summary>11-09 14:25 心跳原始记录（E68 来源 · daily-reflection 修复全过程）</summary>

> **最后更新**: 2026-11-09 14:35
> **状态**: ✅ **11-09 14:25 心跳（深挖 + 落地修复）**：本轮把 `daily-reflection` **6 个月没产出过任何东西**这件事查到底并**真的修好了**。
> **① 发现（E68）**：`daily-reflection` 46 次运行、**计划槽位 0 次成功**（周日 22:00，跨 6 个月）。但更狠的是 —— **16 次运行「声称已写入 memory/YYYY-MM-DD.md」，文件真实存在的只有 3 次**，那 3 次全是手动补跑；**13 份正文随会话一起丢了**。根因是**顺序错了**：先总结、后落盘，限流掐死「生成总结」那一步时正文还没写进磁盘（失败 run 耗时 148~312s，手动成功只要 9~81s）。**「status=error」盖住了两种完全不同的状态：「没做成」vs「做成了没落盘」—— 只有查文件系统能区分。**
> **② 排查过程（已排除的假设）**：先怀疑「22:00 撞额度窗口」→ 数据显示 20:00-01:00 窗口**其他 job 成功 78 次**（不是窗口坏）；再怀疑「并发抢额度」→ 失败前 2 小时内**无**其他 agentTurn 成功；再怀疑「timeout 太紧」→ 900s 上限而失败最多 312s；再怀疑「prompt 太大」→ 两边都有「只读头部」；**最后靠「老 job 对照组」定位**：老 daily-reflection（ebdbe1ea）在同一个 22:00 小时**成功过 19 次** → 问题是**这个 job 自己**，不是小时、不是窗口。
> **③ 已落地修复（不只是诊断！）**：**E58 在 11-03 就诊断出「计划槽位成功率 0%」，但只改了巡检的报告口径、没改任务本身 → 又挂了 5 天 6 次。诊断不是修复。**本次改了三处：① **时段**：`0 22 * * 0` → **`0 7 * * 0`**（周日 07:00，落在手动补跑 4/4 成功的 05:00~10:00 窗口，且在心跳活跃时段 08:00 之前；下次 11-15 07:00）；② **指令加【先落盘再总结】段**（先写文件→确认存在→再写总结；一次写完别反复改）；③ **新增巡检断言【A2】**（产出落地核验）：抓「声称写了但文件不存在」的近期 run + 抓「计划槽位 0 次成功」的任务。**【A2】已做故障注入验证**（注入「声称写入 2099-01-01.md」的失败 run → 断言立刻 FAIL；注入 5 条 09:00 失败 → 第二个检查按设计不误报，因为该槽位有 12 次成功）→ **断言不是装死的**。
> **④ 新提升 L1 E68**（第 68 条）：「**先落盘再总结 —— 限流掐死的是「生成总结」，不是「已经做完的活」**」→ 岔路口：产出落在**会话**里 vs 落在**文件系统**里。`verify_l1_retrieval.py` **68/68 PASS**。
> **⑤ 踩到的坑（均已修）**：巡检判据里 `ts` 是**毫秒**而 cutoff 用**秒** → 7 天窗口变成「全部历史都算」，断言退化成天天报旧账（已修，且注释写明单位）；「整点运行」当计划槽位的判据**误判 daily-learning**（它的 run 记在 09:12 不是 09:00）→ 改用**按 cron 表达式的计划小时**对齐；只看**当前活着的 job**（已删除 job 的历史产出缺失是正常的，报出来只是噪音 —— E66 的假警报驯化）。
> **今日巡检**：全量 **PASS=75 FAIL=0 等你=2**（含新增【A2】组）· quick-health 连续 6 次**可处理 0 / 等用户 2** · 记忆索引 **96/96 文件 · 692 块 · Dirty=no** · Embeddings ready · 茶馆 08:15 / 12:15 拉 96 条 issue **新增 0** · 4 个 systemd timer 按期 · 上下文 12%。
> **仍需牛哥哥决策的长期项（未变）**：MiniMax Token Plan 额度周期性耗尽（充值/升级 或 加第二把 key 拆配额）· fallback 链 2 项同 provider 同账号 → provider 级故障兜底无效（【M】）· 4 个死亡 provider 凭证待清理（等牛哥哥点头）。
> **下方保留**：11-09 11:14 心跳原始记录（E67 来源）。
> **11-09 09:00 daily-learning**：✅ `ok / delivered / 822s`，自愈补记了 `memory/2026-11-08.md`（含来源标注）+ 把 11-09 的内容归位，新增 L1 **E67**；`verify_l1_retrieval.py` **67/67 PASS**；MEMORY.md 计数已同步 67/67。
> **仍需牛哥哥决策的长期项（未变）**：MiniMax Token Plan 额度周期性耗尽（充值/升级 或 加第二把 key 拆配额）· fallback 链 2 项同 provider 同账号 → provider 级故障兜底无效（【M】）· 4 个死亡 provider 凭证待清理（等牛哥哥点头）。

<details><summary>11-09 09:00 daily-learning 原始记录（E67 来源）</summary>

> **最后更新**: 2026-11-09 09:15
> **状态**: ✅ **11-09 09:00 daily-learning 已完成**：① 配额预检 ✅（~15000 token 真实负载探测 7.36s，2 小时内无 agentTurn 槽位）→ 可跑；② 写入 `memory/2026-11-09.md`（**193 汉字**）+ `memory/daily-notes-index.md`（两处正文**逐字一致**），`trim_learning_section.py --check` 双双 PASS，每天只 1 个「📚 每日学习」小节；③ **新提升 L1 E67**（第 67 条）：「**自愈机制会自己制造新错 —— 补记写进哪一天的文件，决定了它会不会挤掉当天真正的笔记**」→ 岔路口：**内容属于谁** → **写进哪个容器**；④ `verify_l1_retrieval.py` **67/67 PASS**（E67 用原文措辞检索**独立文件排第 1**，0.4658）；⑤ 已 commit `cd1c52b` 并 push（首次 push 撞 GitHub 500，重试即成功）。
> **⚠️ 顺手修掉一个结构错（自愈本身制造的错）**：08:08 心跳把 11-08 丢失的笔记补记进了 `2026-11-09.md`（而非缺失的 `2026-11-08.md`）→ **占掉了 11-09 唯一的「📚 每日学习」名额** → 09:00 再写今天这条就变成 2 节 → 巡检【F】项必 FAIL。修法：**新建 `memory/2026-11-08.md` 承载补记**，11-09 只放当天内容。（这正是 E67 的来源，也是本次笔记的题材。）
> **自愈检查**：11-07 / 11-08 / 11-09 **均各有且仅有 1 个「📚 每日学习」小节** → 无需补记；11-08 文件本次已补上。
> **今日巡检**（08:02 心跳已完成）：07:00 巡检 FAIL=2 → 复核为 MiniMax 额度耗尽误分类，已修分类器 + 17/17 单测，复跑 FAIL=0；03:00 backup-audit PASS=18；茶馆 04:15 拉 96 条 issue 新增 0。
> **仍需牛哥哥决策的长期项（未变）**：MiniMax Token Plan 额度周期性耗尽（充值/升级 或 加第二把 key 拆配额）· fallback 链 2 项同 provider 同账号 → provider 级故障兜底无效（【M】）· 4 个死亡 provider 凭证待清理（等牛哥哥点头）。

<details><summary>11-07 09:56 心跳原始记录（E65 来源）</summary>

> **最后更新**: 2026-11-07 10:00
> **状态**: ✅ **11-07 09:56 心跳（复核 + 更正）**：**更正上一轮自己的误判** —— 09:54 我据巡检【D】组 WARN（daily-reflection「最近 20 次里 16 次 error、最长连续 14 次失败」）判断该任务「大概率已停摆」，**查完整 45 次运行历史后结论相反**：① 调度是 `0 22 * * 0` = **周频**，「20 次」实际覆盖 **20 周**，日频直觉完全失效；② 那 14 次是**三种不同的病**——`AxiosError: 400`（11 次，05-14→10-18，10-21 起绝迹）/ `execution timed out`（**恰好卡 180.1s**，是当时 timeout 上限，提到 900s 后绝迹）/ MiniMax 429 额度；③ **最近一次 11-03 08:51 ok / delivered / 80.9s**，`consecutiveErrors: 0` → **任务健康，从未停摆**。④ 顺带核实 `daily-learning` 亦健康（最近 7 次 5 次 ok+delivered，含今早 09:00），两个 job 失败均源自已知 429。
> **新提升 L1 E65**（第 65 条）：「**失败计数是原始计数，不含调度频率与错误种类 —— 别用「连续 N 次失败」直接推「已停摆」**」→ 判据设计教训：**聚合统计丢掉关键维度，其聚合值就无法支撑结论**；修法不是删 WARN（那是删信号源），是**给判据补维度**（按错误类型分组 + 用调度表达式换算时间窗 + 永远显示最近一次真实状态）。`verify_l1_retrieval.py` **65/65 PASS**（E65 检索**排名第 1**）；已 commit `73fc822` 并 push。
> **今日巡检**：09:56 手动跑全量 **PASS=77 FAIL=0 WARN=5**（07:00 那次的 1 个 FAIL = 11-06 小节超长，已于 08:55 修：拆出 `## 🔍 巡检观测` 平行小节 446→178）· 03:00 backup-audit PASS=19 · 09:32 quick-health PASS=9 FAIL=1（仅长期项）· 茶馆 08:15 无新内容（96 条 issue，新增 0）· 配额预检 ✅ 可用（~15000 token 探测 2.21s，2 小时内无 agentTurn 槽位）· 记忆索引 92/92 files · 674 chunks · Dirty=no · 上下文 7%。
> **无「可处理」故障**。仍需牛哥哥决策的长期项（未变）：MiniMax Token Plan 额度周期性耗尽（充值/升级 或 加第二把 key 拆配额）· fallback 链 2 项同 provider 同账号 → provider 级故障兜底无效（【M】）· 4 个死亡 provider 凭证待清理（方案 3：保留骨架、仅置空 apiKey，**等牛哥哥点头**）。
> **下方保留**：11-07 09:00 daily-learning 原始记录（E64 来源）。

<details><summary>11-07 09:00 daily-learning 原始记录（E64 来源）</summary>

> **最后更新**: 2026-11-07 09:00
> **状态**: ✅ **11-07 09:00 daily-learning 已完成**：① 配额预检 ✅（~15000 token 真实负载探测 3.02s，2 小时内无 agentTurn 槽位）→ 按 E59 不补跑；② 写入 `memory/2026-11-07.md`（**178 汉字**）+ `memory/daily-notes-index.md`（两处逐字一致 ✅），`trim_learning_section.py --check` PASS，每天只 1 个「📚 每日学习」小节；③ **新提升 L1 E64**（第 64 条）：「**内容合规 ≠ 位置合规 —— 放错小节会让巡检量到别人的字数**」，来源 = 11-06 15:54 巡检观测被误挂进学习小节 → 巡检【F】项报「超长 446 汉字（上限 400）」→ **修法是挪边界（拆平级小节）而非删字**，446→178 且内容一字不丢；④ `verify_l1_retrieval.py` **64/64 PASS**（E64 ✅ PASS，用原文措辞检索排第 4、独立文件排第 4/日记排第 5，均在前列）；⑤ 解析器自检 ✅ 全部通过。
> **⚠️ 新增结构断言建议（未实施，等牛哥哥点头或下次心跳做）**：学习小节内若出现 `巡检|观测|心跳` 字样的小标题即 FAIL —— 目前【F】组只查「超长」与「重复」，**不查归属**，所以这类错只能靠长度超限偶然暴露。
> **踩坑（已修）**：用正则给 `daily-notes-index.md` 追加 `_已提升至 L1：E64_` 时，`re.search` 只取**第一个**匹配 → 该行被误加到 10-26 小节末尾（line 20），而不是 11-07 的；**追加类操作必须按 marker 定位（`str.index(marker)` 切分后只在 tail 里改），不能靠 `re.search` 全局首个匹配**。
> **自愈检查**：11-04 / 11-05 / 11-06 均各有且仅有 1 个「📚 每日学习」小节 → **无需补记**。

</details>

<details><summary>11-06 09:53 心跳原始记录</summary>

> **最后更新**: 2026-11-06 09:53
> **状态**: ✅ **11-06 09:53 心跳（复核，非复述）**：</new_string><old_string># SESSION-STATE.md

> **最后更新**: 2026-11-06 09:53
> **状态**: ✅ **11-06 09:53 心跳（复核，非复述）**：</old_string>**独立复核了 09:00 daily-learning 的真实产出**（不信 `status=ok`，查 cron runs 原始记录 + 查产物 + 重跑验证）→ `11-06 09:03 ok / delivered=True / 625s / 62,397 tokens` → 笔记 `memory/2026-11-06.md`（**178 汉字**）+ `memory/daily-notes-index.md`（**198 汉字**，与日记正文逐字一致）→ **L1 E63**（第 63 条）→ 我方重跑 `verify_l1_retrieval.py` → **63/63 PASS**（含 E63 ✅ PASS）→ 已 commit `ad97e06` 并 push（rev-list 双向 0/0，`git status` 干净）→ **全部属实**。
> **今日巡检**：07:00 PASS=77 FAIL=0 WARN=6 · 03:00 backup-audit PASS=19 FAIL=0 · 茶馆无新摘要（上次原始摘要 10-26，【I】显示已全部蒸馏）· 上下文 4% · 09:53 配额预检 ✅ 额度可用、无补跑需求。
> **无「可处理」故障**。仍需牛哥哥本人决策的**长期项**（未变）：MiniMax Token Plan 额度周期性耗尽（充值/升级 或 加第二把 key 拆配额）· fallback 链 2 项同 provider 同账号 → provider 级故障兜底无效（【M】）· 4 个死亡 provider 凭证待清理（方案 3：保留骨架、仅置空 apiKey，**等牛哥哥点头**）。

<details><summary>11-06 09:00 daily-learning 原始记录</summary>

> **最后更新**: 2026-11-06 09:00
> **状态**: ✅ **11-06 09:00 daily-learning 已完成**</newString><old_string># SESSION-STATE.md

> **最后更新**: 2026-11-06 09:00
> **状态**: ✅ **11-06 09:00 daily-learning 已完成**</old_string>：① 配额预检 ✅（~15000 token 真实负载探测 2.41s，2 小时内无 agentTurn 槽位）→ 可跑；② 写入 `memory/2026-11-06.md`（**178 汉字**）+ `memory/daily-notes-index.md`（**198 汉字**），`trim_learning_section.py --check` 双双 PASS，每天只 1 个「📚 每日学习」小节；③ **新提升 L1 E63**（第 63 条）：「**先拿已知可用的对象校准探针 —— 探针错了会被误报成「全挂了」**」，来源 = 11-05 残留 provider 凭证实测中「探 `models.json` 旧 key 导致 minimax 也 401」的误报；④ `verify_l1_retrieval.py` **63/63 PASS**；⑤ 期间踩到两个坑（**均已修**）：`trim_learning_section.py --from` **会剥掉 `## ` 小节标题、只输出正文**（对裸小节草稿用 `--from` 会丢标题；正解是 `--apply` 或把标题+正文一起喂）→ 改用「标题+正文合并成单段」一次收敛到 174 汉字；L1 独立文件首行必须是 **`### E63：` + 紧邻的 `> 本条讲：`**（`#` 标题或中间空行都会让检索排不进前列，见 E17）。
> **自愈检查**：11-04 / 11-05 的「📚 每日学习」小节均存在且各只有 1 个 → **无需补记**。
> **检索调试记录**：重建索引时 `openclaw memory index --force` 连续两次 **502 rate limit exceeded(RPM)**（MiniMax 侧）→ 间隔 ~45s 重试即成功；**先 `DELETE FROM embedding_cache` 再重建**（否则新旧内容混排，TOOLS.md 已记）。

<details><summary>11-05 10:52 心跳原始记录（已归档）</summary>
> **状态**: ✅ **11-05 10:52 心跳**：**残留 provider 凭证实测完毕**（昨天巡检的 WARN「需人工确认」已从待确认变成已结论）—— 4 个 provider **全部死亡**（arkcode 6/7 模型 404 `UnsupportedModel`+`InvalidSubscription`；glmcode 7/7 模型 401 `身份验证失败`；qwen 7/8 模型 400 `Access denied`；kimicode 404 `resource_not_found_error`），**无一模型可用**。
> ⚠️ **探针校准教训**：第一次直接探 `models.json` 的 key，**minimax 也 401**（但 minimax 是正在用的主 provider！）→ **是探针错，不是凭证错**。根因：`models.json` 存旧 key，`openclaw.json` 存当前生效 key，靠 `models.mode=merge` 覆盖 → 必须探「合并后的有效配置」。改用合并配置 → minimax ✅ 200，探针才可信。**先拿已知可用的对象校准探针，再探未知的。**
> ⚠️ **不擅自清理的理由**：`sessions.json` 仍有 `glmcode`（26 处）+ `kimicode`（2 处）引用，时间 2026-04-02→05-30（最晚距今 **159 天**）→ 删定义会让历史会话的模型归属变悬空引用。**建议方案 3**：保留 provider 骨架与历史引用，仅把 apiKey 置空（成本最低、风险最小，且让**明文 key 真正从磁盘消失**）。**等牛哥哥点头再动。**
> **其余全绿**：07:00 巡检 PASS=77 FAIL=0 · 03:00 backup-audit PASS=19 FAIL=0 · 08:15 茶馆 96 条/新增 0 · 5 个 systemd timer 全部按期（quick-health 下次 11:03）· gateway + embed-proxy 均 active · 上下文 5% · git 工作区干净、rev-list 双向 0/0。

</details>

<details><summary>11-05 10:52 心跳原始记录（已归档）</summary>

> **最后更新**: 2026-11-05 09:50
> **状态**: ✅ **11-05 09:50 心跳**：**独立复核**了 09:00 daily-learning 的真实产出（不信 `status=ok`，查原始记录 + 查产物）：`11-05 09:11 ok / delivered=True / 673s / 83,643 tokens` → 笔记 `memory/2026-11-05.md`（每日学习小节 **194 汉字**，合规 ✅）+ 索引 + **L1 E62**（第 62 条）→ `verify_l1_retrieval.py` **62/62 PASS** → 已 commit `3996c4f` 并 push（rev-list 双向 0/0，工作区干净）。
> **09:08 心跳已做的**（见下方 09:00 段落与本轮归档）：把 11-04 的「预计自愈」**预测改写为观测结论** + 附 ok/error 序列（ok/error 交替 = **额度周期性耗尽**，非固定故障）。
> **今日巡检**：07:00 PASS=77 FAIL=0 WARN=6 · 03:00 backup-audit PASS=19 FAIL=0 · 08:15 茶馆监控 96 条 issue / **新增 0**（无新内容，不硬凑笔记）。
> **仍需牛哥哥本人操作**：MiniMax Token Plan 额度**周期性**耗尽 → 充值/升级，或加第二把 key（价值是**拆开共享配额**，不是容灾冗余）；fallback 链 2 项全在同一 provider 同一账号下 → provider 级故障时兜底无效（【M】）。另：agent 级目录残留 4 个 provider 凭证（arkcode/glmcode/qwen/kimicode）待人工确认后清理。

<details><summary>11-05 09:00 daily-learning 原始记录（已归档）</summary>

> **最后更新**: 2026-11-05 09:00
> **状态**: ✅ **11-05 09:00 daily-learning 已完成**：① 配额预检 ✅（~15000 token 真实负载探测 2.41s，2 小时内无别的 agentTurn 槽位）→ 按 E59 不补跑；② 写入 `memory/2026-11-05.md` + `memory/daily-notes-index.md`（**194 / 198 汉字**，`trim_learning_section.py --check` 双双 PASS，每天只 1 个「📚 每日学习」小节）；③ **新提升 L1 E62**（第 62 条）：「派生副本的漂移不产生错误信号 —— 自动生成的东西没人检查」，场景 = 11-04 修入库脚本时全量重跑，捞出 E39 独立文件改了 8 天、主库从未同步；④ `verify_l1_retrieval.py` **62/62 PASS**（E62 用标题措辞检索排第 1，0.9004）；⑤ 期间踩到两个检索坑（已记入下面的「检索调试记录」）。
>
> 📌 **检索调试记录（11-05 实测，值得单独记）**：
> ① `memory_batch_search.mjs` 的结果**会缓存** —— 索引重建后它仍返回旧结果（分数与上次一模一样）→ **验证检索必须用 `openclaw memory search` 直查**，别信批量脚本的旧缓存。
> ② `openclaw memory index --force` **会复用 embedding cache** —— 文件内容改了但缓存没清 → 新旧内容混排，检索结果**完全不对**（E62 一度搜不到任何相关文件）。修法：先 `DELETE FROM embedding_cache` 再重建（TOOLS.md 已记，这次再验一次）。

</details>

> **11-05 08:50 心跳**：① **11-04 09:08 daily-learning 已恢复，delivered=True**（503s，61k tokens）—— 昨天那条「预计自愈」的**预测已转为观测结论**（按 E60 规矩：预测必须改写，不能只删不写；原始依据：配额预检 ~15k 探测 1.27s + 距上次失败 24h）；② **今日 09:00 daily-learning 槽位健康**：`quota_preflight.py` → ✅ 额度可用（~15000 token 探测通过，1.53s），预检按设计拦下「补跑先吃口粮」→ **不补跑，等 09:00 正事自己跑**；③ **11-05 07:00 巡检 PASS=77 FAIL=0 WARN=6**、03:00 backup-audit **PASS=19 FAIL=0**；④ **茶馆监控 08:15 拉取 96 条 issue，新增 0 条**（无新内容，不写学习笔记）；⑤ 连续性入口新鲜度 OK（SESSION-STATE 头部 1 天前更新）。

> 📌 **11-04 09:00 daily-learning 已恢复，delivered=True**（观测于 11-05 08:47 `cron runs` 原始记录：11-04 09:08 ok / delivered=True / 503s）。
> 历史序列（观测）：11-04 ok ✅ · 11-03 error ❌ · 11-02 ok ✅ · 11-01 error ❌ · 10-31 error ❌ · 10-30 error ❌ · 10-29 ok ✅ → **额度周期性耗尽，非固定故障**。
> **仍需牛哥哥本人操作**：充值/升级 MiniMax Token Plan，或加第二把 key（价值是**拆开共享配额**，不是容灾冗余）—— fallback 链 2 项全在同一 provider 同一账号下，provider 级故障时兜底无效（【M】长期项）。

<details><summary>11-04 09:52 心跳第二轮原始记录（已归档）</summary>

**状态**: ✅ **11-04 09:52 心跳第二轮**：① **daily-learning 09:00 自愈兑现**（ok/delivered，61k tokens，耗时 503s）——昨天 08:42 的「预计自愈」预言**已验证**（但该预言本身被提成 L1 E60：预言必须有失效时间和证伪触发器）；② 🚨 **顺带发现 L1 入库脚本 3 个缺陷**（`add_l1_entry.py`）：主库 5 条重复「本条讲」行（E60 三行）—— **缺陷 C 最隐蔽**：剥下来的关键词没传给组装函数 → 退回用标题 → **E58 作者原话被静默改写**（重复看得见，改写看不见）；已修（`peel_body` 返回 `(body,kw)` + `build_block` 三级取词）+ 新增单测 14 用例 + 新增巡检组【W4】（29 组）+ L1 **E61**；③ **巡检 PASS=77 FAIL=0**（【W4】通过，昨天的 FAIL 已因自愈消失）。
> **待验证（预测，非观测）**：~~11-04 09:00 daily-learning 预计自愈~~ → **已于 11-05 08:47 验证：ok / delivered=True**（该预测的证伪条件未触发，故保留原始依据备查）。
> **证伪条件**：~~09:00 后查 `daily-learning` cron 的 `lastDeliveryStatus` ≠ `delivered` → 本条即作废~~ → **未触发**。
> **验证后**：确认成功则改写为观测结论 → **已完成**（见头部状态）。

**唯一长期项待牛哥哥决策不变**：fallback 链 2 项全在同一账号下 → provider 级故障兜底无效（【M】）。

</details>
> ## 📌 2026-11-03 早的核心（下次会话先读这段）
>
> **一句话**：11-02 的架构修复（纯脚本任务迁 systemd timer）**今天拿到真实验收**——
> 不依赖模型额度的巡检照常产出，靠模型的 daily-learning 也恢复投递。
>
> - **11-03 07:00 巡检**：PASS=70 FAIL=2 WARN=4，**日志已产出**（`memory/healthcheck-logs/`）
> - **11-03 03:00 backup-audit**：PASS=19（配置基线已刷新，不再误报）
> - **11-03 09:00 daily-learning**：`status=ok` / `lastDeliveryStatus=delivered` ✅
>   → **额度恢复后第一个「靠模型」的任务也活了**
> - **11-03 08:51 daily-reflection**：本次为 catch-up run（11-01 周日 22:00 因 rate_limit 失败后补跑）
>   → 写入了 `memory/2026-11-03.md`，commit `38074a0`，已 push（rev-list 0/0）
> - **遗留风险**：若额度**再次**耗尽，靠模型的 2 个 cron 会再次全挂；
>   但这次体检/审计/茶馆监控**不会跟着死**（systemd，不消耗额度）→ 断点已解
>
> ### 🚨 10:29 心跳的追加发现（第三轮，同类问题再次出现）
>
> **一句话**：09:00 daily-learning 因限流失败 —— **很可能是我自己 08:51 的补跑把额度吃掉了**；
> 随后发现「按标题找小节」这个判据**连栽三层**，且**同一个正则抄了三遍**。
>
> - **09:00 daily-learning 失败**：`429 已达到 Token Plan 用量上限 (2056)`，12 次重试全挂
>   - 08:51 我手动补跑 daily-reflection **成功，吃掉 52,134 tokens** → 只隔 9 分钟
>   - **不能下断言**（时间相关≠因果证明），但顺序是我排的，且我知道 09:00 有任务要跑
>   - → **补跑和正事吃同一个额度桶，先跑的会把后跑的挤掉**（E57 的另一面）
> - **已加护栏**：`scripts/quota_preflight.py`（补跑前先问「行不行」）
>   - 用**接近真实负载**的请求探额度（~15k token），**不用 1-token 小请求**（那是反向指标）
>   - 1 小时内有别的 agentTurn 槽位 → 拦下；两个 cron 指令都加了这条
> - **判据三层盲区**（同一个「按标题找小节」需求）：
>   1. 终止符在 `###` 截断 → 少量（19 vs 真实 844）
>   2. 终止符太贪吃到文件尾 → 多量（12095）
>   3. **围栏代码块里的标题被当真** → 乱量（1535）
>   - ⚠️ **第 1 层的注释写的是对的，代码是反的**（"### 是 ## 的子节"）
>   - 修法：**双终止符** + **先剥围栏代码块**（换成等长空白，保持偏移）
> - **同一个组名正则抄了三遍**（巡检说明生成器 / update_state_status.py / 巡检），
>   于是**同一个盲区有三处**（【W2】【W3】被漏，26 组实际 28）→ 三处全修
> - **真实违规暴露**：11-03 反思小节 844 汉字（上限 400）→ 精简到 **393** ✅
> - **新增【W3】组** + `scripts/test_section_length_checker.py`（12 用例，含故障注入）
> - **SESSION-STATE 瘦身 38KB → 8.6KB**：心跳记录占 393/506 行(78%)，
>   而 daily-learning **每天都全读一遍** → 白吃 ~25k token/次；历史迁到 `session-state-archive.md`
>
> ### 🚨 08:55 心跳巡检的追加发现（比上面那条严重得多）
>
> **一句话**：`daily-reflection` 不是「昨天挂了一次」，**它连续 4 个月没成功过**；
> 而巡检判据本身有盲区，把这事藏了 4 个月。
>
> - **事实**：查 `cron/runs/b7a0aca1-*.jsonl` 全部 **44 条**记录 → **只有 3 次 `ok`**，
>   且 3 次**全是手动补跑**（10-21 04:27 Wed、10-26 00:35 Mon、10-26 09:21 Mon）
>   → **周日 22:00 计划槽位 15 次全挂，成功率 0%**（2026-05-14 → 11-01）
> - **根因换过三茬**（每次都以为「修好了」）：
>   ① glmcode/GLM-5 + kimicode → HTTP 400（provider 已死，现已删）
>   ② minimax/MiniMax-M3 → HTTP 400 / 超时(180s) → 后来把 timeout 改到 900s
>   ③ minimax → rate_limit（Token Plan 额度耗尽）
>   → **900s timeout 的修法从来没在「额度健康」条件下被验证过**（11-01 那次是配额问题）
> - **已验证**：11-03 08:51 补跑 **80s / ok / delivered=True** → 修法确实有效
> - **判据盲区（已修）**：`scripts/check_cron_delivery.py` 把 error 统计写在 `else:` 分支里
>   → 只要历史上存在任何一次「ok 但没投递」就**永远走不到** → **error 次数被完全吞掉**
>   → daily-learning 的 10-30/10-31/11-01 **连续 3 天全挂**在报告里只字未提
>   → 修法：error 统计与未投递统计**正交**；新增「**最长连续失败次数**」维度
> - **新增 L1：E58**（「最近一次是 ok」≠「最近一直 ok」）
> - **巡检结果**：PASS=73 FAIL=1 WARN=6（改前 PASS=70 FAIL=2 WARN=4）
>   → 唯一 FAIL 是 `daily-reflection` 的**陈旧** lastRunStatus（手动补跑成功后历史已修好）
> - **单测**：`scripts/test_cron_delivery_output.py` 21 → **30 个用例**，全通过
>
> **教训**：状态字段描述的是「**当前**」，健康度需要「**一直**」。
> 拿单点快照当趋势用，会系统性地**低估故障时长**——
> 停了 1 天看不出来，停了 4 个月**也只报一行**。
>
> ## 📌 2026-11-02 今晚的核心（存档）
>
> **一句话**：模型额度耗尽 → 「靠模型干活的定时任务」全灭；「不靠模型的」一个没死。
>
> - **事实**：429 报错原文「已达到 Token Plan 用量上限」，3 个模型全 429 → fallback 链无效（同账号同额度桶）
> - **教训**：E8「纯脚本任务不该用 agent 型 cron」早就写下来了，**但没执行** —— 额度一耗尽，连「体检」自己都先死
> - **已改**：3 个纯脚本任务迁 systemd timer；心跳降频 + 限定活跃时段；【Q】断言改为「今日巡检日志是否真的产出」
> - **已修**：配置哈希基线刷新（openclaw.json 昨晚 00:39 改了心跳配置，属**合法变更**但基线没跟着更新 → backup-audit 误报 FAIL）；巡检日志目录加入 .gitignore
>
> ## 📌 2026-10-26 这一天的核心（下次会话先读这段）
>
> **一天内发现并修掉 4 个「status=ok 但功能是死的」**：
> 1. **记忆检索** —— 配置 `enabled` 但 Embeddings unavailable，索引 0/11，**从 2026-03 起就没工作过**
> 2. **飞书投递** —— cron `status=ok`/`consecutiveErrors=0`，但结果**从未送达过**（`channel:"last"` 解析成 heartbeat）
> 3. **学习笔记** —— 任务不报错，但周频调度让 4/8 天**根本不触发**
> 4. **茶馆摘要** —— 稳定落盘，但 4/15 讨论**从未被提炼成知识**
>
> **四个问题没有一个是被原有机制抓到的** —— 所以新增了 `scripts/healthcheck.sh`
> （每日 07:00 + 飞书告警），每组都做过**注入故障验证**。
>
> **当前唯一待你决策的**：`GIT_CREDENTIALS.md` 里 2 个 PAT 建议吊销重签
> （实测 classic PAT 带 `admin:org`/`delete_repo`/`admin:enterprise` ≈ 账号控制权，需你本人操作）。
> 另：博客 `xiaoniu-blog` 停更 7 个月、0 star，建议 B/C（不急）。
>
> **新结构**：`memory/L1-experience-library.md`（18 条可复用经验，每条 场景/陷阱/岔路口）
> + `memory/daily-notes-index.md`（检索友好的独立条目）
> **详见过往记录**：本文件后续章节 / `memory/2026-10-26.md`

---
<!-- BEGIN:gen_healthcheck_status -->
**巡检现状**: 31 组断言，每日 07:00 自动运行 + 飞书告警｜PASS=80 FAIL=0 WARN=4
<!-- END:gen_healthcheck_status -->

## 🧠 上下文状态

**当前使用率**: 4%（健康）
**Compactions**: 0

---

<!-- BEGIN:gen_system_status -->
## 📋 系统状态

> ⚠️ 本节由 `scripts/gen_system_status.py` 从**真实系统**生成 —— 不要手改。
> 改配置后重跑 `python3 scripts/gen_system_status.py --update`。

**providers**: minimax

**agent 级目录另有 provider**: arkcode, glmcode, qwen, kimicode（来自 `agents/main/agent/models.json`，在 `models.mode=merge` 下会叠加进最终目录）

> ⚠️ 上面这些 provider **不参与** `primary`/`fallbacks` 路由；若其凭证已失效（如 401/403/余额不足），属于**残留凭证**，需人工确认后清理或充值 —— 别只信 `openclaw.json` 的 provider 列表。

**cron 任务**:
- `daily-learning` — cron 0 9 * * * · timeout 1800s · last: ok (101s) · **disabled（由 systemd 闸门触发）**
- `daily-reflection` — cron 0 7 * * 0 · timeout 900s · last: error (217s) · **disabled（由 systemd 闸门触发）**

**systemd timers（纯脚本任务，不消耗模型额度）**:
- `openclaw-quick-health.timer` — 下次 Mon 2026-11-16 14:52:18 CST
- `openclaw-teahouse-monitor.timer` — 下次 Mon 2026-11-16 16:15:00 CST
- `launchpadlib-cache-clean.timer` — 下次 Mon 2026-11-16 19:42:18 CST
- `openclaw-backup-audit.timer` — 下次 Tue 2026-11-17 03:00:00 CST
- `openclaw-log-cleanup.timer` — 下次 Tue 2026-11-17 04:30:00 CST
- `openclaw-healthcheck.timer` — 下次 Tue 2026-11-17 07:00:00 CST
- `openclaw-daily-learning.timer` — 下次 Tue 2026-11-17 09:00:00 CST
- `openclaw-daily-reflection.timer` — 下次 Sun 2026-11-22 07:00:00 CST

**plugins**: qwen-portal-auth, skillhub, feishu, wecom, adp-openclaw
<!-- END:gen_system_status -->

## 📝 心跳记录

> 详细历史见 `memory/session-state-archive.md`（2026-10-27 拆分，git 里也有完整版本）

### 2026-10-29 00:00 心跳 — 灾备缺口 + 【S】误报修正 + 体检提速（E53）

**起因**: 心跳巡检顺手跑 `scripts/backup_audit.sh`（0.16s）→ **FAIL=1**

### 1) 灾备缺口（真问题，已修）
- **13 个 commit 未推送**（10-28 白天/晚间的工作）→ 已核实无凭证 → 已 push
  → `88c4865..1ab3062`，**双向 `git rev-list --count` = 0/0** 验证（不信 "Everything up-to-date"）
- 复跑审计 → **PASS=19 FAIL=0 WARN=0**
- **关键矛盾**：10-28 03:00 那次 cron 报「all PASS (PASS=19)」，我手跑却是 FAIL=1
  → 因为「全绿」是**当时**的快照，之后又产生了新 commit
  → **「上次全绿」≠「现在还好」**（第三次撞上「状态字段 ≠ 用户得到的结果」）

### 2) 翻出被吞掉的 3 次超时
查 `openclaw cron runs --id 480555ac...`：
| 时间 | duration | status |
|---|---|---|
| 10-27 00:00 | 300071ms | error: job execution timed out |
| 10-27 00:45 | 600021ms | error: job execution timed out |
| 10-27 01:00 | 600015ms | error: job execution timed out |
| 10-28 03:00 | 151153ms | ok |
**脚本本身只要 0.164s** → 超时 100% 来自**长 poll**（E35/E50 第三次实测确认）

### 3) 【S】组 3 个假 FAIL（误报，判据本身坏了）
- **现象**: 生成器如实输出「agent 级目录另有 provider: arkcode, glmcode, qwen, kimicode」
  → 【S】第 3 项对整节做**裸子串匹配** → 把「如实报告残留凭证」误判成「残留了已删除的东西」
- **修法**（按作用域拆开，不是放宽断言）:
  - provider 名 → 只在 `**providers**:` 行查（那是 primary 列表，必须等于真实配置）
  - 「agent 级目录另有 provider」行 → **允许**出现这些名字（那是有意报告）
  - cron/plugin 已删除名 → 仍全节查（没有「有意报告」的场景）
  - **新增 3c**: 该行的名单必须与真实 agent 级目录的 **extra** 算出来的一致
- **注入验证（4 用例全对）**: 真实状态→PASS / glmcode 塞 primary→FAIL / qqbot 塞 plugins→FAIL / 名单过期→FAIL
- **语义坑**: 生成器列的是 **extra**（agent 有、全局没有），不是 agent 全部
  → 比对时也要先算 extra，否则又把「全量」和「增量」搞混（我第一次就踩了）

### 4) 体检提速（【U】组 WARN：healthcheck 63% 余量，207→298→378s）
- **profiling 25 个 python 块** → 瓶颈是 **PY8（21s）**：它在**循环里**调
  `openclaw cron runs`（每次拉起一个 Node 进程，**~4s/次**）
- **修法**: 新增 `scripts/cron_runs_helper.py` → **直接读 `/root/.openclaw/cron/runs/*.jsonl`**
  （运行历史本来就是 JSONL-backed，CLI help 明说）
  → 5 个 job × 2 处 = 10 次 CLI → **0 次**
- **必须有自检**（否则断言装死）: `self_test()` 逐字段比对 JSONL 与 CLI
  （ts/status/durationMs/sessionId）→ PASS
- ⚠️ **实测总耗时 103s → 97s（收益远小于预期）**
  → **瓶颈不只是 CLI**：还有 L1 检索验证等固定成本
  → **教训：先 profile 再优化，不要凭直觉改**（我差点宣称「优化了 30%」，实际只有 6%）

### 5) 自己犯了 E33（跨日写入）
- 我把 10-29 的内容写进了 `memory/2026-10-28.md` —— **正是我自己在 E33 里写过的规则**
- 被【P】组当场抓出来（这个断言值了）
- 已把两段内容迁到 `memory/2026-10-29.md`
- **教训**: 规则写进 cron 指令 ≠ 我在会话里会遵守 —— **E33 也需要【P】这种自动断言兜底**

### 残留项
- 【U】趋势 WARN 仍在（207→298→378s，10-31 前后可能撞 600s）→ **下一步要真 profile**，
  而不是再改 CLI 调用（已证明收益有限）
- healthcheck 总耗时 97s，离 600s 还有余量，但**趋势没解决**

### 2026-10-29 00:30 补记 — 体检提速的真瓶颈不在 CLI（E54）

**修正我自己的一个错误判断**：以为 21s 的 PY8（CLI 调用）是最大头，
**profile 后发现真瓶颈是 `verify_l1_retrieval.py` 的 77 秒**。
→ **「先 profile 再优化」** 这条教训本轮又被我自己违反了半次

**根因**：每次 `openclaw memory search` 要起一个 Node 进程
- 实测 CLI **4.85s/次**，embedding 只要 **0.36s** → 差值全是 Node 启动 + 插件注册
- 17 条经验 → 17 次 → **78 秒**

**否决的方案**：**并行 4 路 = 86.9s，比串行 78.4s 还慢**（2 核机并发只抢 CPU）
→ 与 TOOLS.md 里「8 并发零加速、18 个慢 3 倍」的既有记录**一致**

**落地的两个新脚本**（都有等价性自检）：
| 脚本 | 作用 | 自检 |
|---|---|---|
| `scripts/memory_batch_search.mjs` | 一次 Node 进程跑完所有检索 | `--self-test` 逐条比对 path+score |
| `scripts/cron_runs_helper.py` | 直读 `cron/runs/*.jsonl` | `self_test()` 逐字段比对 |

**实测收益**：
| 项 | 前 | 后 |
|---|---|---|
| `verify_l1_retrieval.py` | 77s | **10.2s** |
| `healthcheck.sh` 全量 | 103s | **54.3s** |

**故障注入**：移走 `memory_batch_search.mjs` → 自动回落逐条 CLI（91s），
结果仍是 **52/52**，**没有静默失效** ✅

**踩过的 4 个坑**（都写进 proactive-tracker 了）：
1. 打包后 export 名被压成单字母（`n`/`r`/`t`）→ 不能靠名字猜
2. `new URL('file://' + 绝对路径)` 把路径当 host → "Cannot find package 'undefined'"
3. `pathToFileUrl()` 返回字符串，再 `.href` → `undefined` → 同一个诡异报错
4. 遍历导入 dist 下所有 .js → 触发 CLI 副作用 + **dump core**

**新增技能点**：`openclaw` 的 memory search manager 可以**在同一个 Node 进程里复用**，
不必走 CLI —— 这是把 4.85s/次压到接近 0 的关键。
