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

