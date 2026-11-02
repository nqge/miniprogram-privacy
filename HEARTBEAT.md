# HEARTBEAT.md

**心跳检查清单** — 每 60 分钟（08:00–22:00，Asia/Shanghai）

> 2026-11-02 起：心跳节奏 60 分钟 + 限定活跃时段（原 30 分钟全天）。
> 原因：MiniMax Token Plan 额度有限，每次心跳要注入 ~60k token 上下文。
> **真正的 30 分钟级监控由 systemd `openclaw-quick-health.timer` 负责（不烧模型额度）。**

---

## ✅ 检查项（按顺序，先看有没有异常再看别的）

### 1. 系统健康（最高优先级）
- [ ] 读 `memory/healthcheck-logs/healthcheck-*.txt` 里**最新一份** → 有「【可处理 — 现在就该修】」就修
- [ ] 读 `memory/healthcheck-logs/backup-audit-*.txt` 最新一份 → 有 FAIL 就查
- [ ] 这两份日志由 **systemd timer** 产出（07:00 / 03:00），不是 cron —— 没有 cron 兜底了

### 2. 茶馆学习
- [ ] 看 `teahouse-raw/teahouse_summary_*.md`（**注意：不在 memory/ 下**）有没有新摘要
- [ ] 有新内容 → 写 200 字中文学习笔记到 `memory/$(date +%F).md`
- [ ] 抓取由 systemd `openclaw-teahouse-monitor.timer`（每 4 小时）负责，我只负责学习

### 3. 记忆与上下文
- [ ] `session_status` 看上下文使用率；>60% → 启动 working-buffer 协议
- [ ] SESSION-STATE.md 是否与真实系统一致（不一致就改，别让它误导下一个会话）

### 4. 主动改进
- [ ] 有没有重复出现的告警/故障值得写成 L1 经验条目（`scripts/add_l1_entry.py`）

---

## 🚨 危险区协议（上下文 >60%）
1. 立即启动 `memory/working-buffer.md`
2. 每次消息都记录到 buffer
3. 关键信息立即写入 SESSION-STATE.md

## 💡 主动思考题
1. 牛哥哥现在最需要什么？
2. 今天学到的知识，能固化成什么自动化？
3. 有什么重复劳动可以变成 systemd timer？（E8：纯脚本任务不该用 agent 型 cron）

## 📢 什么时候才出声
- 有「可处理」的故障 / 有值得牛哥哥决策的事 → 汇报
- 晚上 22:00–08:00 除非紧急，否则安静（心跳本身已不在活跃时段运行）
- 没事 → 回 `HEARTBEAT_OK`

---

_最后更新: 2026-11-02 01:05_
_版本: 2.0_
