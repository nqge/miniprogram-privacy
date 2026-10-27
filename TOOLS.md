# TOOLS.md - Local Notes

Skills define _how_ tools work. This file is for _your_ specifics — the stuff that's unique to your setup.

## What Goes Here

Things like:

- Camera names and locations
- SSH hosts and aliases
- Preferred voices for TTS
- Speaker/room names
- Device nicknames
- Anything environment-specific

## Examples

```markdown
### Cameras

- living-room → Main area, 180° wide angle
- front-door → Entrance, motion-triggered

### SSH

- home-server → 192.168.1.100, user: admin

### TTS

- Preferred voice: "Nova" (warm, slightly British)
- Default speaker: Kitchen HomePod
```

## Why Separate?

Skills are shared. Your setup is yours. Keeping them apart means you can update skills without losing your notes, and share skills without leaking your infrastructure.

---

Add whatever helps you do your job. This is your cheat sheet.

---

## 🔐 Git 推送排障速查（2026-10-26 实测补充）

### 三个高频坑

| 症状 | 真实原因 | 解法 |
|------|---------|------|
| "用了 SSH 但还是 443 超时" | 全局 `url.https://github.com/.insteadof` 把 SSH 重写回 HTTPS | `git config --global --unset-all 'url.https://github.com/.insteadof'`（**必须用完整 key**，含斜杠的 section 名 `--unset-all` 无效） |
| push 成功但远端没变 | 推了 `main` 而远端是 `master` | `git ls-remote origin` 先确认分支名 |
| `secret scanning` 拦截，提示某 commit | 密钥在**历史 commit 的 tree** 里，后续 `git rm --cached` 无效 | 必须 rebase 重写该 commit |

### 清除历史密钥的标准流程
```bash
cp GIT_CREDENTIALS.md /root/.openclaw/xxx.local.md && chmod 600 /root/.openclaw/xxx.local.md
echo "GIT_CREDENTIALS.md" >> .gitignore
git rm --cached GIT_CREDENTIALS.md
# 重写引入密钥的那个 commit
git rebase --onto <bad_commit>^ <bad_commit> master
# 冲突逐个处理
git checkout --theirs <file> && git add <file> && git -c core.editor=true rebase --continue
# 验证历史已干净
for c in $(git rev-list origin/master..HEAD); do
  git show $c:GIT_CREDENTIALS.md >/dev/null 2>&1 && echo "STILL PRESENT: $c"
done
```

### 建议：Git 远端统一用 SSH
```bash
git remote set-url origin git@github.com:nqge/<repo>.git
git ls-remote origin   # 验证
```

### 经验法则
> 「Everything up-to-date」有时出现在 push 断连之后，是**误导性输出**——以 `git rev-list --count origin/master..HEAD` 为准。

---

_最后更新: 2026-10-26 01:25_
_维护者: 小红🌸_

---

---

## ⏱️ 等长任务：poll 会「等满」，别一次等太久（2026-10-27 实测）

> 这条规则在**每个会话**都会加载（TOOLS.md 是全局注入的），
> 而 cron 指令里的提醒**只在那几个任务里**生效 —— 2026-10-27 09:00
> 的 `daily-learning` 就是「指令里写了、但没照做」（用了 4 次 120s 的 poll）。

### 现象

`process(action='poll', timeout=120000)` 会**阻塞满整个 timeout**，
哪怕进程早就结束了：

```
CALL 01:37:25 (timeout 120000) → CALL 01:40:49   = 120.0s
CALL 01:40:49 (timeout 120000) → CALL 01:44:48   = 120.0s
CALL 01:44:48 (timeout 150000) → CALL 01:47:25   = 150.0s
```

4 次这样的 poll ≈ 540 秒 → **必然撞上 cron 任务的 timeout**，
即使被等的那个脚本只要 **0.18 秒**。

### 规则

| 场景 | 做法 |
|---|---|
| 跑命令 | `exec(command, yieldMs=10000)` → 转后台 → 再 poll |
| 等后台进程 | `process(action='poll', timeout=10000)` —— **每次最多 10~15 秒** |
| 超过 2 分钟的任务 | `sessions_spawn(runtime='subagent')` —— 推送式完成，不用轮询 |

### 判断标准

**先量脚本耗时**：`time bash xxx.sh`
- 脚本 1 秒内跑完，而 job 却超时 → 那是**等法**的问题，不是任务的问题
- 手动测出的耗时是定时任务耗时的**下界**，不是估计值
  （2026-10-27 实测：手动 102s / 真实 cron 298s，差近 3 倍）

### 实测数据（本机 2 核）

| 方式 | 数量 | 耗时 |
|---|---|---|
| 串行 `openclaw memory search` | 1 个 | 6.6s |
| 8 并发 | 8 个 | 55s（**零加速**） |
| 8 并发 | 18 个 | **6m18s**（比串行慢 3 倍） |

→ 瓶颈是 **Node 进程启动（CPU 密集）**，不是查询。
**2 核机器上并发只会更慢**，要减量就减查询次数，别加线程。

---

## 🧠 记忆检索（memorySearch）— 2026-10-26 打通

### 架构
```
OpenClaw memorySearch (provider=openai)
   ↓ POST {baseUrl}/embeddings  {model, input[]}
tools/minimax_embed_proxy.mjs  ← 协议翻译 (127.0.0.1:8799, systemd user service)
   ↓ POST https://api.minimaxi.com/v1/embeddings  {model, texts[], type:"db"}
MiniMax embo-01  → {vectors: number[][]}  1536 维
   ↓ 翻译回 OpenAI 格式 {data:[{embedding}]}
OpenClaw 向量库 (sqlite-vec)
```

### 运维
```bash
systemctl --user status openclaw-embed-proxy.service   # 代理状态
journalctl --user -u openclaw-embed-proxy.service -f    # 实时日志
curl -s http://127.0.0.1:8799/health                     # 健康检查
openclaw memory status --deep                           # 索引状态
openclaw memory index --force                           # 重建索引
```

### ⚠️ 三个必须记住的坑

1. **MiniMax `type` 是向量空间开关**（不是标签）
   `db` 与 `query` 是两个不同空间，同一文本跨空间余弦仅 **0.74**。
   索引与检索必须用**同一个 type**，否则排序被系统性稀释（0.869 → 0.814）。
   → 代理里统一写死 `type: "db"`。

2. **改 embedding 参数后必须清缓存**
   `openclaw memory index --force` 会**复用 embedding cache**，不清缓存就等于没重建。
   ```bash
   python3 -c "import sqlite3; db=sqlite3.connect('/root/.openclaw/memory/main.sqlite'); db.execute('DELETE FROM embedding_cache'); db.commit()"
   ```

3. **memorySearch 会递归索引 `memory/` 的所有子目录**（实测确认）
   → 原始数据 dump 必须放到 `memory/` **外面**。
   茶馆摘要因此改写到 `teahouse-raw/`。

### 当前调参（MiniMax 尺度，非 OpenAI 默认值）
```json5
agents.defaults.memorySearch = {
  provider: "openai", model: "embo-01",
  remote: { baseUrl: "http://127.0.0.1:8799/v1/", apiKey: "local-proxy" },
  fallback: "none",
  query: { minScore: 0.08, maxResults: 6,
           hybrid: { enabled: true, vectorWeight: 0.75, textWeight: 0.25 } },
  chunking: { tokens: 200, overlap: 40 }
}
```
- `minScore=0.08` 不是拍脑袋 —— 按正/负样本分数分布标定，保留 8/8 正样本
- `chunking.tokens=200`（默认 400）实测最优；一个 chunk 混两个主题会把向量平均掉
- 上游 `buildFtsQuery` 用 **AND 拼接带引号短语** → 多词中文查询 BM25 直接 0 命中
  → 检索几乎全靠向量，这也解释了为什么 minScore 要调得这么低

---

## 🩺 全链路健康巡检（2026-10-26 新增）

```bash
bash scripts/healthcheck.sh          # 手动跑，退出码 0=全通过
```
cron `healthcheck` 每日 07:00 自动跑（isolated / no-deliver）。

**为什么有它**：2026-10-26 一天发现三个「status=ok 但功能是死的」——
记忆检索（enabled 但 Embeddings unavailable，从 3 月起就是死的）、
飞书投递（cron 正常但从未送达）、学习笔记（任务不报错但周频让 4/8 天根本不触发）。
**状态字段描述的是「任务跑完了」，不是「用户得到了结果」。**

**四组断言**：
| 组 | 查什么 | 抓什么问题 |
|---|---|---|
| A | 飞书 / GitHub / 嵌入代理 | 外部依赖挂了但没人知道 |
| B | gateway / embed-proxy systemd 状态 + 开机自启 | 服务掉了 |
| C | Embeddings ready / 索引完整 / Dirty=no | 记忆检索失效、索引滞后 |
| D | announce 型 cron 必须 delivered=True | 投递静默失败 |

**踩过的坑**：
- `Embeddings:` 字段**只在 `openclaw memory status --deep` 出现**，不加 `--deep` 读不到
- 判定逻辑要**单独单测** —— 第一版跑出 PASS=18，但里面藏着一个"永远抓不到故障"的分支
- 直接改 `cron/jobs.json` **测不出故障** —— CLI 读的是 gateway 内存态，不是文件


---

## 📌 更正记录机制（2026-10-26 新增）

> **没查就下结论的错误，会在下一个会话里被当成事实继承。**

今天 11:15 我在选项单和 memory 里写了「agentTurn 型 cron 默认 timeout 是 30s」，
11:20 查源码才发现是 **360s**（30s 是 `apply.runtime` 搜索/ACP 路径的值）。
如果只改聊天不改文件，下个会话检索到这条记忆会**当成已验证事实**继续用。

**规矩**：
1. 发现旧结论错了 → **先写进 `memory/YYYY-MM-DD.md` 顶部「更正记录」区**，再改代码
2. 记忆文件开头保留更正区（哪怕当天写的，也放最前面）
3. 涉及「默认值 / 协议 / 常量」的结论，**必须查源码或实测**，不能凭印象
4. 选项单里的风险描述同样要复核 —— 今天 PAT 那条就是靠实测 scope 才发现风险等级错了

**自查触发点**：每次改完配置/参数后，回头核一遍「我之前基于什么假设做的这个改动」。


---

## 📉 日志降噪（2026-10-26 实测）

`/tmp/openclaw/openclaw-YYYY-MM-DD.log` 一天能到 26M+，**大头是插件注册噪音**
（`feishu_xxx: Registered ... tool`，INFO 级）。

### ⚠️ 噪音来源：是 **agent session 启动**，不是 CLI 调用

> 2026-10-26 21:35 复核更正（此前记为「每次 CLI 调用都会重新注册全部插件」——
> **实测不成立**）：

| 动作 | 产生的 Registered 行数 |
|---|---|
| `openclaw cron list` × 5 | **0 行** |
| 一次 agent session 启动 | **16 行/簇**（同一秒内成簇出现） |

**所以噪音的真实来源是「agent session 启动」**，每个新 session 拉起时
重新注册全部插件并打 6+ 行。**降噪必须作用在拉起 session 的进程环境上**
（gateway 单元的 `Environment=`），而不是指望 CLI 调用时不打。

### 降噪旋钮实测（裸环境，3 次 CLI 调用）

| 旋钮 | 字节/3 次 | 结论 |
|---|---|---|
| 默认 | 60630 | 基线 |
| `--log-level warn`（CLI flag） | **0** | ✅ **有效**（此前记「无效」是不准确的） |
| `OPENCLAW_LOG_LEVEL=warn`（环境变量） | 0 | ✅ **有效** |

> 环境变量的价值不在于「比 flag 强」，而在于**它能作用于非 CLI 的进程**
> —— 也就是 agent session 启动、systemd 单元这些真正产生噪音的地方。
> flag 只在显式带 flag 的那一次调用里有效。

### ⚠️ 绝对不能改 `openclaw.json` 的 `logging` 段

那是 `Maximum call stack size` 的元凶：
`loadConfig` → `readLoggingConfig` → `resolveSettings` → `getLogger` → `loadConfig` 无限递归。
2026-10-26 当天因此产生 **342 次** `Failed to read config` 报错。

**只用环境变量。** 已配好的位置：
- `openclaw-gateway.service` / `openclaw-quick-health.service`（systemd `Environment=`）
- `scripts/healthcheck.sh` 顶部 `export`
- `scripts/check_cron_delivery.py` / `scripts/quick_health.py` 的 `subprocess` env

### 顺带记的日志坑

- **OpenClaw 日志轮转按大小不按天** → 同一槽位跨天追加 → `mtime` 判龄永远失效，
  唯一可靠信号是**文件名里的槽位日期**。详见 `scripts/log_cleanup.sh` 头部注释。
- **当前活跃槽位永不删/压缩**，只做超限截断（>50MB 保留最后 2000 行）。
  26M < 50MB → 当天不会被处理，会一直涨。
- **查日志大小时先按来源分类统计**（`{"subsystem":...}` / 消息归一化 + 计数），
  不要凭印象猜谁在刷屏。
- 查 systemd 日志**别只按 unit 名过滤** —— 实例化 unit 名（`xxx@yyy.service`）
  会导致 `journalctl -u` 返回 No entries，明明发生了却查不到。**全量 grep 才准**。
