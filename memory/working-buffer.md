# Working Buffer - 2026-10-28

> ⚠️ 本文件是**当前轮**的工作缓冲区。上一轮（10-27）的记录在 git 历史里
> （`git log -p memory/working-buffer.md`）。别在这里堆积历史 —— 那是今日笔记的活。

## 📍 当前状态（2026-10-28 19:30）

**系统**：26 组断言全绿（PASS=72 FAIL=1，1 项长期项）· cron 5 条全 ok · 今日错误 0
**记忆**：L1 **52** 条（E1–E52，52/52 可检索）· 索引 **485** 块 / 69 文件 · `Dirty: no` · `Embeddings: ready`
**git**：工作区干净（仅 `memory/teahouse_state.json` 为 teahouse-monitor 自身运行态）
**代理**：embed-proxy `active` · 实调返回 1536 维向量 · 日志 ~0.3MB/天（噪音已根治，原 26MB；此数字随时间变，看趋势别看绝对值）

## ✅ 10-28 本轮已完成

1. **memory 检索链路全链验证** —— 代理 health / 实调 embedding / `--deep` 索引 / 端到端 search 四层全通过
2. **10-27 17:01 `insufficient balance` 事故定性** —— MiniMax 余额耗尽（非代理故障），已自恢复，未复现
3. **healthcheck【E】凭证断言口径修正** —— 只有「被 git 跟踪」或「权限宽于 600」才判 FAIL
   （本地 600 未跟踪 = 安全基线）→ FAIL 3→1，PASS 60→72
4. **daily-learning 超时修复验证** —— 772s → **512s**（上限 1800s），trim 脚本生效
5. **E52 事实错误更正** —— 「趋势判定缺失」是错的，`d2fedcb` 起已实现且两���都报了 WARN
6. **体检盲区修复（本日最重要）** —— provider 有**两层配置**，
   `openclaw.json`（全局）与 `agents/main/agent/models.json`（agent 级，merge 下叠加）。
   体检脚本只读全局 → 4 个失效 provider 长期不可见 → SESSION-STATE 写成「已删除」→ 下个会话会跳过
   → 已修【M】组 + `gen_system_status.py` + 更正 SESSION-STATE

## ⚠️ 残留项（都需牛哥哥本人操作）

- **4 个 provider 凭证**（arkcode / glmcode / qwen / kimicode）——
  实测全部失效（401/403/400/404），但**仍留在 agent 级目录、仍可选中**。
  需定夺：**充值换新凭证** 还是 **直接清掉**。
  建议：先充值验证可用 → 加进 `fallbacks` 做真跨 provider 兜底；确认全死则清理。
- **healthcheck 自身超时预算** —— 耗时 207s→298s→378s（上限 600s），每次 +~80s，
  外推约 **10-31 撞线**。4 条候选对策（减 LLM 调用 / 改确定性判断 / 调高 timeout / 拆轻重两组）。
- **PAT 吊销重签**（沿用项）
- **博客方向**（沿用项）

## 🔍 今日沉淀的可复用教训

1. **一个系统有两层配置时，「已删除」先问「删的是哪一层」** —— 本地文件删了 ≠ 全局生效
2. **体检脚本的输入范围 = 它的盲区边界**
3. **默认输出 ≠ 完整目录** —— `openclaw models list` 默认只显示带 default/fallback 标签的，
   `--all` 才看得到全貌；**用默认输出下结论 = 漏掉 15 个模型**
4. **「已了结」是最危险的结论** —— 它让下个会话直接跳过；「仍待处理」只是多花一次确认成本
5. **断言「某机制缺失」前先 grep 实现 + 看真实输出** —— 「我打算加」≠「它已经在跑」
6. **日志 `grep -c "error"` 的计数毫无意义** —— 70% 是我自己写的分析文本被匹配到，必须归类
7. **诊断配置时只打印结构不打印 key** —— 今天自曝过一次 minimax apiKey 到日志
8. **看到「已删除/已修复」先 stat 一下相关文件 mtime**

