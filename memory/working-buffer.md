# Working Buffer - 2026-10-26

## ✅ 本轮已修复（2026-10-26 00:30-00:45）

### 1. 模型回退链断裂 → cron 双任务全线 error
- **症状**：`daily-reflection` error — `All models failed (2): minimax/MiniMax-M3 rate_limit | kimicode/kimi-kimi-for-coding HTTP 403 auth_permanent`
- **根因（逐层实测，不是猜）**：
  - `minimax` provider 全部 3 个模型（MiniMax-M3 / M2.7 / M2.7-highspeed）**实测 200 OK**（anthropic-messages 接口），只是当时撞了限流
  - `kimicode` — endpoint `https://api.kimi.com/coding/v1` + `/v1/messages` → **404**；模型名 `kimi-kimi-for-coding` 也是错的（实际 id 是 `kimi-for-coding`），且订阅已失效
  - `glmcode` — `https://open.bigmodel.cn/api/anthropic` → **401 身份验证失败**（key 过期），**注意**：直连返回 HTML 而非 JSON，容易被误判成 200
  - `arkcode` — `https://ark.cn-beijing.volces.com/api/coding` → 所有列出的模型均 **404 UnsupportedModel**（coding plan 不覆盖），`ark-code-latest` 返回 500
- **修复**：`agents.defaults.model.fallbacks` 从 `[kimicode/kimi-kimi-for-coding]` 改为
  `[minimax/MiniMax-M2.7-highspeed, minimax/MiniMax-M2.7]`（同 provider 互为备份，实测均 200）
- **备份**：`~/.openclaw/openclaw.json.bak-20261026`，权限保持 600
- **验证**：手动 `cron run` 两个任务 → 均 `status: ok` ✅

### 2. 茶馆监控停更 ≈166 天 → 已复活
- **症状**：`memory/teahouse_state.json` 最后更新 2026-04-29，脚本用 GraphQL `node_id` 去重，但抓取路径已切到 REST API，ID 格式不一致 → 全量漏判
- **修复**：新写 `scripts/teahouse_monitor.py`（统一用 REST 整数 issue id + number 双主键，兼容旧 node_id；带 3 次重试防 IncompleteRead）
- **成果**：一次性捞回 **95 条历史讨论**，摘要写入 `memory/teahouse_summary_2026-10-26_0040.md`
- **调度**：新增 cron `teahouse-monitor`，每 4 小时（`0 */4 * * *` @ Asia/Shanghai），isolated + no-deliver（只做内部分习，不外发）

## ⚠️ 残留项

### 已确认失效的凭证（等牛哥哥决策，未擅自改）
- `glmcode` API key（49 位，`d4334718...`）→ 401 过期
- `arkcode` 订阅 → 所有模型 UnsupportedModel
- `kimicode` → 订阅失效 + 模型名配置错误

### 老问题（未动）
- `plugins.entries.qqbot` / `ddingtalk` 残留配置 → 每次 CLI 刷屏 cosmetic 警告
- 配置哈希基线（`.config-baseline.sha256`）从未落地
- 磁盘 75%（29G/40G），日志目录已自愈

### 待牛哥哥决策（积压 6+ 周）
1. glmcode / arkcode / kimicode 三个 provider 凭证是否续费或从配置移除
2. qqbot / ddingtalk 残留配置是否清理
3. 是否补建配置哈希基线
4. `daily-learning` 频率是否从每周一改回每日（学习笔记 10-24/10-25 两天完全缺失，周频零容错）
5. 是否把 `daily-reflection` 也改回每日

## 状态
- 上下文: 4%（健康）
- 牛哥哥: 凌晨睡眠时段，全程未外发任何消息
- 模式: heartbeat only，无 outbound
