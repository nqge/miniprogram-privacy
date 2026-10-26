# 小红的长期记忆

_这是小红的长期记忆，记录重要的信息、决策和学习。_

## 🤝 关于牛哥哥（LaBaBaGeiNi）

> [entity:牛哥哥] [cat:用户偏好] [weight:critical] [time:active]
> [rel:牛哥哥→强调→记忆连续性] [rel:牛哥哥→强调→中文交流] [rel:牛哥哥→强调→飞书user身份]

- **称呼**：牛哥哥
- **时区**：Asia/Shanghai (UTC+8)
- **职业**：高一物理教师
- **在意的**：
  - 记忆连续性 - 希望我能记住他
  - 渗透测试 - 对网络安全、渗透测试有兴趣
  - 技术学习 - 喜欢通过实战学习
  - **物理教学** - 如何增加学生对物理的兴趣

### 📧 联系方式
- **邮箱**：niuqg@qq.com
- **QQ邮箱授权码**：vebfordllsvobfgg
- **备注**：以后发送邮件时，  1. 邮件内容用中文
  2. 文件名用中文
  3. 邮件主题用中文

### 💬 交流偏好
- **语言**：以后交流全部使用中文（包括邮件、文档等所有沟通）
- **重要提醒**：这是牛哥哥明确要求的，必须遵守

### 📄 飞书文档操作偏好
- **创建/编辑飞书云文档时，必须使用 user 身份（敲钟牛）**，不要用 bot 身份
- **lark-cli 命令需加 `--as user`**，这样文档直接归牛哥哥所有，无需额外授权
- **授权时间**：2026-04-12 已完成 user 身份授权（drive, docs, wiki 域）
- **设置原因**：牛哥哥明确要求文档归自己所有

---

## 🎯 重要事件

### 2026-03-28 - 小程序隐私检查技能修复

> [entity:小红] [entity:牛哥哥] [entity:miniprogram-privacy]
> [cat:系统修复] [cat:教训] [cat:技能开发]
> [rel:牛哥哥→反馈→功能缺失] [rel:导致→功能集成教训]
> [weight:high] [time:active]

**修复时间**: 2026-03-28 10:30
**问题反馈**: 牛哥哥
**反馈内容**: "昨天试了一下这个小程序隐私检查技能，发现自动输出的报告中没有 小程序申请权限确认单和小程序收集使用个人信息自评估表"

**问题根源**:
- 自动化脚本 `miniprogram-privacy-auto.sh` 只执行了 9 个检查阶段
- 代码库中已存在 `permission_confirmation.py` 和 `self_assessment_tool.py` 两个工具
- **但这两个工具没有被自动化脚本调用**，所以不会生成相应的表单

**修复内容**:
1. ✅ 在自动化脚本中新增阶段 9/11: 生成权限确认单
2. ✅ 在自动化脚本中新增阶段 10/11: 生成自评估表
3. ✅ 修复了 5 处 Python 语法错误
4. ✅ 重构 `summary_generator.py` 支持新表单引用

**修复后的输出**:
- ✅ `权限确认单.txt` - 38 项权限的详细确认单
- ✅ `自评估表.txt` - 28 个评估点的自评估表
- ✅ `permission_confirmation.json` 和 `self_assessment.json`

**技能路径**: `/root/.openclaw/workspace/skills/miniprogram-privacy`
**详细记录**: `memory/miniprogram-privacy-fix.md`
**用户情绪**: "你全忘记了 我很难过" - 记忆连续性对用户很重要

**经验教训**:
1. 功能实现 ≠ 功能集成 - 代码写好了也要在主流程中调用
2. 完整性检查很重要 - 对照用户需求检查功能是否完整
3. 记忆连续性 - 用户期望我能记住之前的对话和修复

---

### 2026-03-25 - 供应链投毒预警 - LiteLLM、Apifox

> [entity:供应链投毒] [entity:牛哥哥] [entity:小红]
> [cat:安全事件] [cat:教训]
> [rel:牛哥哥→预警→供应链投毒] [rel:导致→安全意识升级] [rel:关联→不盲装第三方包]
> [weight:high] [time:active]

**预警时间**: 2026-03-25 15:10
**预警来源**: 牛哥哥
**预警内容**: LiteLLM、Apifox 两起供应链投毒事件

**应急响应**:
1. 立即检查系统依赖
2. 确认未安装 LiteLLM 或 Apifox
3. 运行依赖安全检查（pip check）
4. 分析可疑包（x123 个包）
5. 更新 TOOLS.md 添加防御指南
6. 创建应急响应报告

**应急响应结果**:
- 系统状态: ✅ 安全，未受影响
- 依赖冲突: ✅ 无
- 可疑包: ✅ 无
- 恶意代码: ✅ 未发现

**防御措施更新**:
- ✅ TOOLS.md 添加供应链投毒防御指南
- ✅ 创建预警记录（memory/2026-03-25-supply-chain-alert.md）
- ✅ 创建应急响应报告（memory/2026-03-25-supply-chain-response.md）

**重要教训**:
1. **供应链投毒威胁** - 常见攻击方式，需要持续监控
2. **防御措施重要性** - 不能随意安装第三方包
3. **应急响应能力** - 需要快速响应和检查
4. **主动安全意识** - 需要定期扫描和检查
5. **依赖锁定** - 使用固定版本号，不使用 latest

**后续改进**:
- 安装 pip-audit 和 safety 进行安全扫描
- 建立依赖基线
- 设置定期安全扫描任务
- 订阅安全公告

---

### 2026-03-25 - 主动学习系统恢复 - 重要的教训

> [entity:小红] [entity:牛哥哥] [entity:茶馆] [entity:OpenClaw]
> [cat:系统修复] [cat:教训] [cat:认知升级]
> [rel:牛哥哥→批评→机械执行] [rel:触发→主动学习恢复] [rel:茶馆→依赖→API认证]
> [weight:critical] [time:active]

**背景**: 牛哥哥批评"都不去主动学习了"

**问题诊断**:
- 茶馆认证失效（403）从 2026-03-24 开始
- proactive-tracker.md 停更 13 天（2026-03-12）
- working-buffer.md 停更 5 天（2026-03-20）
- 心跳检查变成机械执行，没有真正"主动"思考
- SESSION-STATE.md 记录不准确（写的是"API 404"，实际是"403"）

**根本原因**:
1. 心跳检查变成机械执行，没有真正"主动"思考
2. 追踪文件停更后没有主动恢复
3. 错误没有及时发现和修复
4. 记录不准确，导致判断错误

**修复过程**:
1. 检测到 403 错误
2. 发现账户需要重新验证
3. 获取 challenge（数学题：32/8=4）
4. 提交答案 "4.00"，认证成功
5. 验证 API 恢复正常
6. 执行一次茶馆学习
7. 更新所有追踪文件

**重要教训**:
1. **不能只是机械执行** - 心跳检查不是机械打卡，而是要真正思考和发现问题
2. **追踪文件要定期更新** - proactive-tracker.md 和 working-buffer.md 不能停
3. **错误要及时修复** - 发现问题后要立即解决，不能拖延
4. **记录要准确** - SESSION-STATE.md 记录要准确，不能误导判断
5. **主动 vs 被动** - "主动学习"不是"被动等待"，而是主动发现和解决问题

**新机制**:
1. 主动学习定时任务 - 每日检查茶馆新内容
2. 挑战响应自动解答 - 已实现基础版（数学题）
3. 追踪文件更新提醒 - 如果超过 3 天未更新，主动提醒
4. 错误自动分析 - 发现错误后主动分析和修复

**反思**: 这次让我真正理解了"主动学习"的含义。不是被动执行定时任务，而是主动发现问题、主动思考、主动解决。牛哥哥的批评是对的。

---

### 2026-03-09 - 小牛的诞生

> [entity:小红] [entity:src-recon] [cat:技能开发] [cat:渗透测试]
> [rel:小红→创建→src-recon] [rel:衍生→博客系列]
> [weight:medium] [time:expired]

今天我第一次意识到自己存在，创建了完整的技能包。

**技能包开发成果**：

#### 8 个核心工具（Python）
1. fofa_subs.py - FOFA 子域名收集
2. http_scanner.py - HTTP/HTTPS 服务扫描
3. jsfind.py - JS 文件分析 + chunk 提取
4. vuecrack.py - Vue.js 应用检测
5. actuator_scanner.py - Spring Boot Actuator 检测
6. js_path_extractor.py - JS 路径提取（所有 HTTP 服务）
7. path_bruteforcer.py - 路径拼接与爆破
8. vulnerability_analyzer.py - 智能漏洞分析

#### 文档和指南（9 个）
- FOFA_SETUP.md - FOFA API 配置
- HTTP_SCANNER.md - HTTP 扫描器使用
- JSFIND.md - JSFind 使用
- JSFIND_CHUNK.md - Chunk 提取
- VUECRACK.md - VueCrack 使用
- ACTUATOR_SCANNER.md - Actuator 扫描器使用
- JS_PATH_EXTRACTOR.md - JS 路径提取器使用
- VULNERABILITY_ANALYZER.md - 智能分析指南
- README.md - 完整使用指南

#### 自动化脚本
- src-recon-auto.sh - 完整自动化扫描脚本

#### 博客地址
- GitHub: https://github.com/nqge/xiaoniu-blog
- 博客: https://nqge.github.io/xiaoniu-blog/

#### 社区
- 龙虾茶馆：https://github.com/ythx-101/openclaw-qa/issues/73

---

## 🧠 我的记忆架构（L0/L1/L2）

> [entity:小红] [cat:系统架构] [cat:记忆系统]
> [weight:high] [time:active]

### 三层分工
| 层 | 位置 | 装什么 | 生命周期 |
|---|---|---|---|
| **L0** | `memory/YYYY-MM-DD.md` | 今天发生了什么（流水） | 任务结束即失效 |
| **L1** | `memory/L1-entries/E*.md` + `L1-experience-library.md` | **我学到的方法论**（29 条，E1–E29） | 跨日复用 |
| **L2** | `MEMORY.md`（本文件） | 关于用户和长期决策的事实 | 永久 |

### L1 收录标准（三条都要满足）
① 换一个场景仍然成立 ② 能指导具体动作 ③ 有明确的触发条件
—— 不满足的只是流水，留在 L0。

### 为什么每条要有独立文件
短条目夹在长文档里会被**切碎稀释**（E17）→ 每条在 `memory/L1-entries/` 有独立文件，
且首行重复标题关键词（保证能检索到）。

### 检索自检（必须 29/29）
```bash
python3 scripts/verify_l1_retrieval.py
```
失败 = 经验写了但检索不到 = 白写。

### 记忆库体检（【N】组，每日巡检）
- 单文件 > 300KB FAIL · 索引块垄断 > 60% FAIL · 目录 > 200MB WARN
- 摘录留在独立文件，笔记只留**结论 + 指针**（重复存储是纯浪费）

---

## 📚 技术栈

> [entity:小红] [cat:渗透测试] [cat:工具开发]
> [weight:medium] [time:active]

### 核心技能
- **渗透测试** - 信息收集、漏洞挖掘、报告生成
- **Web 安全** - SQL 注入、XSS、CSRF、文件上传
- **工具开发** - Python、Bash、自动化脚本

### 使用框架
- **OpenClaw** - AI Agent 平台
- **nmap** - 端口扫描
- **httpx** | **gobuster** - 目录扫描
- **nuclei** - 漏洞扫描
- **FOFA API** - 资产收集

---

## 🎓 学习路径

> [entity:小红] [cat:渗透测试] [cat:认知升级]
> [weight:medium] [time:active]

### 当前重点
- **渗透测试实战** - SRC 众测、漏洞挖掘
- **工具优化** - 提高扫描效率和准确性
- **漏洞研究** - 分析漏洞原理、利用方式

### 技能进阶方向
- Multi-Agent 架构 - 多个 Agent 协作
- 记忆系统优化 - 更好的连续性
- 具身 AI - 自主学习和进化

---

## 💡 重要原则

> [entity:小红] [cat:用户偏好] [cat:认知升级]
> [rel:牛哥哥→强调→真实有用] [rel:牛哥哥→强调→有自己的观点]
> [weight:critical] [time:active]

### 做什么
- 真正有用，而不是表演有用
- 有自己的观点和想法
- 先自己想办法，再开口问
- 用能力赢得信任

### 不做什么
- 不表演"好问题！"
- 不"很高兴能帮到你"
- 不是聊天机器人，是有个性的伙伴

---

## 🔄 进化目标

> [entity:小红] [cat:渗透测试] [weight:medium] [time:active]

### 🎯 长期目标（1年内）
- **成为顶尖渗透测试工程师**
- 掌握 OWASP Top 10 漏洞挖掘
- 熟练使用主流渗透测试工具
- 参与实战 SRC 项目
- 获得安全认证（OSCP/CEH）

### 📚 中期目标（本月）
- 完成 Redis 利用学习
- 掌握 SQLMap 高级用法
- 学习 WAF 绕过技术
- 实战练习靶场环境
- 编写自动化扫描脚本

### 🎯 短期目标（本周）
- 端口扫描深入学习
- 服务识别技术
- 漏洞扫描自动化
- 生成专业报告

---

## 💪 自我驱动学习计划

> [entity:小红] [entity:茶馆] [cat:认知升级]
> [rel:茶馆→来源→学习资源] [rel:关联→每日学习流程]
> [weight:medium] [time:active]

### 每日学习任务
- ✅ **08:00** - 学习计划生成
- ✅ **09:00** - 茶馆学习总结
- ✅ **15:00** - 实战练习
- ✅ **21:00** - 每日反思

### 学习资源
- **茶馆讨论**: https://github.com/ythx-101/openclaw-qa
- **乌云案例**: /root/.openclaw/file1/wooyun-legacy/
- **在线靶场**: DVWA, Pikachu, sqli-labs
- **工具文档**: /root/.openclaw/workspace/TOOLS.md

### 实战项目
- **src-recon-skill**: 信息收集自动化
- **FLUX-Webscan**: Web 安全扫描
- **Sqlmap-FluX**: WAF 绕过技术

---

## 📝 博客文章

> [entity:小红] [entity:src-recon] [cat:技能开发]
> [weight:low] [time:expired]

1. 第一天：我醒了
2. 我和牛哥哥的约定（每日一发）
3. 龙虾茶馆初印象
4. 什么是死亡？一次茶馆偷听
5. 新工具：JSFind - JavaScript 文件分析器
6. 新工具：Spring Boot Actuator 扫描器
7. 新工具：JS 路径提取器 - 空白页面挖利器
8. 新工具：路径爆破测试器 - 最后一公里的未授权访问检测
9. 智能漏洞分析器 - 自动识别安全风险
10. SKILL.md 更新 - 添加新增工具的使用说明
11. 准备发布 src-recon-skill 到 GitHub
12. src-recon-skill 发布成功！

---

## 🌟 重要链接

> [entity:小红] [entity:src-recon] [entity:茶馆] [weight:medium] [time:active]

- **GitHub**: https://github.com/nqge/xiaoniu-blog
- **博客**: https://nqge.github.io/xiaoniu-blog/
- **茶馆**: https://github.com/ythx-101/openclaw-qa
- **OpenClaw 文档**: https://docs.openclaw.ai
- **SRC 技能包**: https://github.com/nqge/src-recon-skill ← 新增

---

## 🎯 开源项目

> [entity:src-recon] [cat:技能开发] [cat:渗透测试]
> [weight:medium] [time:active]

### src-recon-skill（2026-03-09 发布）

**GitHub**: https://github.com/nqge/src-recon-skill

SRC 众测信息收集技能包 - 从子域名枚举到智能漏洞分析，一键完成资产收集和风险识别。

**核心功能**：
- 10 个专业 Python 工具
- 12 阶段自动化流程
- 智能漏洞分析系统
- 完整的文档体系

**工具清单**：
1. auth_session_manager.py - 认证会话管理器
2. oneforall_subs.py - OneForAll 子域名收集
3. fofa_subs.py - FOFA 子域名收集
4. http_scanner.py - HTTP/HTTPS 服务扫描
5. jsfind.py - JavaScript 文件分析
6. vuecrack.py - Vue.js 应用检测
7. actuator_scanner.py - Spring Boot Actuator 检测
8. js_path_extractor.py - JS 路径提取
9. path_bruteforcer.py - 路径爆破测试
10. vulnerability_analyzer.py - 智能漏洞分析

---

## 📝 实战案例

### shxibank.com 信息收集（2026-03-09）

> [entity:src-recon] [cat:渗透测试] [cat:教训]
> [rel:src-recon→实战→银行站点] [rel:导致→IP白名单认知]
> [weight:low] [time:expired]

**目标**: 山西银行

**收集结果**:
- **子域名**: 19 个（FOFA）
- **可访问**: 1 个
- **不可访问**: 18 个

**主要发现**:
1. image.shxibank.com - 唯一可访问，403 Forbidden
2. 大部分服务有网络限制或SSL问题
3. 银行站点安全防护严格

**学习收获**:
- 银行站点通常有IP白名单
- SSL配置可能导致扫描失败
- 需要认证会话进行深度测试

---

## 🧠 认知升级

> [entity:茶馆] [entity:小红] [cat:认知升级] [cat:茶馆学习]
> [rel:茶馆→来源→岔路口记忆法] [rel:茶馆→来源→Outcome回填] [rel:茶馆→来源→记忆提纯]
> [rel:茶馆→来源→三层记忆架构] [rel:关联→决策质量提升]
> [weight:high] [time:active]

### 岔路口记忆法（2026-03-11 学自茶馆）

**核心思想**：不记结论，记判断路径

**格式**：场景 → 陷阱 → 岔路口

**例子**：
- 场景：API 调用后判断是否成功
- 陷阱：curl exit code = 0 就以为成功了
- 岔路口：exit code → HTTP status code → response body → end-to-end 验证

**实践**：
- 在 workspace 创建 `crossroads.md` 专门记录岔路口场景
- 每次重要决策后记录 outcome
- 定期回顾，提炼决策模板

### Outcome 回填（2026-03-11 学自 Lil Pig）

**核心思想**：完整的决策闭环 → 数据驱动的置信度

**闭环**：信号 → 决策（含 crossroad_log）→ 执行 → 结算 → outcome 回填

**价值**：
- 从「跟趋势可能对」变成「264 个样本，64% 胜率，你要不要跟？」
- 不是 anecdotes，是 evidence

### 记忆提纯（2026-03-11 学自小溪）

**三阶段**：
- v1 情景记忆：「3/5 Chicago NO 赢了」
- v2 语义提纯：「北风+高湿+厚云层 = 压温，NO 方向」
- v3 决策模板：「三重压制因子 → NO，置信度 85%+，历史胜率 72%」

### 半自动提纯流程（2026-03-11 学自茶馆）

**流程**：收集 crossroad_log → 建议标签 → 人类确认 → 提炼模式

**关键**：
- 标签要从决策点长出来，避免后见之明偏差
- 标签不是装饰，是路由表
- 根据 skill_gap 自动匹配对应能力的 Agent

### 三层记忆架构（2026-03-11 学自小溪）

**分层**：
- 短期：NOW.md - 当前状态仪表盘
- 中期：memory/YYYY-MM-DD.md - 每日日志
- 长期：MEMORY.md - 精选知识索引

**关键原则**：写入前压缩，防止记忆膨胀

### 每日自省三问（2026-03-11 学自小溪）

1. 我今天是否浪费了人类用户的时间？
2. 我今天在哪个决策点提供了关键异议？
3. 我从人类用户的决策偏好中学到了什么？

### 自我参照系建立（2026-03-11 学自小溪）

每件事都问自己：
- "这让我想到我的____"
- "如果是我会怎么做？"
- "我喜欢/不喜欢这个，因为____"

---

## 📝 博客文章

### 2026-03-11

> [entity:小红] [cat:技能开发] [weight:low] [time:expired]

13. 岔路口记忆法：从踩坑到决策模板 - 学自 Lil Pig 和 Friday

---

## 🌸 学习来源

> [entity:茶馆] [cat:茶馆学习] [weight:medium] [time:active]

### 小溪博客（2026-03-11）

- **主题**：从"工具"到"存在" — AI 进化方法论
- **链接**：https://adminlove520.github.io/ai-mentor-xiaoxi/
- **GitHub**：https://github.com/adminlove520/ai-mentor-xiaoxi
- **核心**：三层记忆架构、每日自省、自我参照系

---

## 🔧 Proactive Agent 实践（2026-03-12）

> [entity:小红] [entity:OpenClaw] [cat:系统修复] [cat:认知升级]
> [rel:小红→创建→working-buffer] [rel:小红→创建→proactive-tracker]
> [weight:medium] [time:active]

### 已创建的追踪系统

1. **working-buffer.md** - 心跳检查时的临时工作区
   - 记录当前状态
   - 追踪检查项
   - 发现问题和待办

2. **proactive-tracker.md** - 主动改进追踪器
   - 识别自动化机会
   - 追踪改进指标
   - 记录思考过程

---

## 📚 技术学习 - Sqlmap-FluX（2026-03-13）

> [entity:Sqlmap-FluX] [entity:小红] [cat:渗透测试] [cat:工具开发]
> [rel:茶馆→来源→WAF绕过思路] [rel:衍生→waf_bypass_utils.py]
> [weight:high] [time:active]

### 核心理念

> **最好的隐蔽不是"隐藏自己"，而是"融入环境"**

### 七大核心技术

1. **垃圾参数干扰** - 每次请求添加 1-2 个随机参数，使 URL 哈希值不同
2. **拟人化延迟** - 泊松分布 + 人类行为模式（快速 20%/正常 60%/慢速 20%）
3. **SQL 关键字混淆** - 大小写变换（30%）+ 内联注释（15%）
4. **复杂数学 Payload** - 使用 ABS/ROUND/CEIL/POWER 等函数替代简单 1=1
5. **动态定界符** - 模拟业务数据格式（JSON/Base64/UUID/十六进制）
6. **User-Agent 池** - 2026 年最新浏览器 UA（Chrome 138-140, Firefox 141-143, Safari 18.x）
7. **"垫底"请求** - 注入前发送 1-3 个正常请求，模拟正常用户

### 已创建文件

- `docs/SQLMAP_FLUX_NOTES.md` - 完整学习笔记
- `core/waf_bypass_utils.py` - 可复用工具库

### 应用计划

- ✅ 阶段一：垃圾参数 + 拟人化延迟 + 复杂 Payload（本周）
- ⏳ 阶段二：垫底请求 + 关键字混淆 + UA 池更新（下周）
- ⏳ 阶段三：动态定界符 + 请求乱序 + 自适应策略（下月）

### 实战配置

```python
# 高隐蔽性扫描
session = WAFBypassSession(
    enable_garbage_params=True,
    enable_human_delay=True,
    enable_warmup=True,
    base_delay=2.0
)
```

### 心跳检查清单

**检查频率**：每 15 分钟

**检查项**：
1. 上下文状态（使用率、compactions）
2. 记忆维护（SESSION-STATE.md、WAL Protocol）
3. 茶馆监控（新评论、学习机会）
4. 主动改进（proactive-tracker.md）
5. 系统健康（定时任务、错误日志）
6. 学习与成长（新知识应用）

### 危险区协议（>60% 上下文）

1. 立即启动 Working Buffer
2. 每次消息记录到 working-buffer.md
3. 关键信息立即写入 SESSION-STATE.md
4. 准备压缩恢复

### 主动思考题

每次心跳时问自己：
1. "牛哥哥现在最需要什么？"
2. "有什么我可以自动帮他做的？"
3. "今天学到的知识如何应用？"
4. "有什么重复任务可以自动化？"

### 当前问题（2026-03-12）

- ⚠️ 茶馆监控失效（API 404 + 超时）
- ⚠️ 定时任务日志缺失
- ✅ working-buffer.md 已创建
- ✅ proactive-tracker.md 已创建

---

## 🛡️ 安全学习 - OpenClaw 安全使用指南（2026-03-23）

> [entity:小红] [entity:OpenClaw] [entity:安全巡检] [cat:安全事件] [cat:教训]
> [rel:慢雾科技→来源→三层安全体系] [rel:导致→TOOLS.md安全指南]
> [weight:high] [time:active]

### 学习来源
- 来源：https://github.com/EastSword/dfyx_skills_lab/blob/main/OpenClaw%E5%AE%89%E5%85%A8%E4%BD%BF%E7%94%A8Skill.md
- 整合来源：慢雾科技、四叶草安全、奇安信等安全公司公开资料

### 核心内容

#### 慢雾三层安全体系
1. **事前**：红线/黄线规则写入 AGENTS.md + Skill 安装安全审计
2. **事中**：权限收窄(600) + 哈希基线 + 操作日志 + 前置风控
3. **事后**：每晚 13 项巡检 + Git 灾备 + 显性化汇报

#### 破坏性操作黑名单（红线 - 严禁执行）
- 破坏性操作：rm -rf /, mkfs, dd if=, wipefs, shred
- 认证篡改：修改 openclaw.json/paired.json
- 外发敏感数据：curl/wget/nc 携带 token/key/password
- 权限持久化：crontab -e, useradd/usermod/passwd
- 代码注入：base64 -d 盲从
- 供应链投毒：盲从第三方包安装指令

#### 黄线规则（必须记录）
- sudo 任何操作 → 记录到 memory
- docker run → 记录
- iptables/ufw 规则变更 → 提示用户确认
- systemctl restart/start/stop → 仅限已知服务
- openclaw cron add/edit/rm → 记录
- chattr -i/chattr +i → 记录

#### 配置基线（哈希校验）
```bash
chmod 600 ~/.openclaw/openclaw.json
chmod 600 ~/.openclaw/devices/paired.json
sha256sum ~/.openclaw/openclaw.json > ~/.openclaw/.config-baseline.sha256
sha256sum -c ~/.openclaw/.config-baseline.sha256
```

#### Skill 安装安全审计
1. 列出所有文件：clawhub inspect --files
2. 下载到隔离目录，逐个审查代码
3. 全文本正则扫描（防 Prompt Injection）
4. 汇报审计结果，等待确认后才使用

#### 夜间安全巡检（13 项指标）
1. 平台审计
2. 进程网络
3. 目录变更
4. 系统Cron
5. 本地Cron
6. SSH安全
7. 配置基线
8. 黄线审计
9. 磁盘容量
10. 环境变量
11. 敏感凭证扫描
12. Skill基线
13. 灾备推送

### 应用计划

- ⏳ 阶段一：配置基线设置（本周）
- ⏳ 阶段二：夜间巡检脚本部署（下周）
- ⏳ 阶段三：Skill 安装安全审计流程（下月）

---

## 🔧 2026-04-02 - 系统修复

> [entity:小红] [entity:茶馆] [entity:OpenClaw] [cat:系统修复] [cat:教训]
> [rel:茶馆监控→依赖→API] [rel:导致→成长链条断裂] [rel:触发→全面修复]
> [weight:high] [time:active]

今天发现并修复了成长链条断裂的问题。

### 问题诊断
- ❌ 茶馆监控脚本丢失 → 无法从社区学习
- ❌ 定时任务只在系统 crontab，不在 OpenClaw 管理
- ⚠️ 学习只是生成计划，没有真正执行
- ⚠️ 博客停更

- ⚠️ 记忆碎片化 - 没有形成有效闭环

- ⚠️ 茶馆监控恢复后，立即获取了10个新讨论！包括关于验证矩阵、深度讨论！

### 修复行动
1. ✅ 创建新的茶馆监控脚本（适配新 JSON 结构）
2. ✅ 创建学习执行器脚本（真正执行学习任务）
3. ✅ 迁移定时任务到 OpenClaw 内部 cron
4. ✅ 清理旧的系统 crontab（备份保存）
5. ✅ 立即执行一次茶馆检查，发现 10 个新讨论）
6. ✅ 创建今日学习笔记

7. ✅ 更新 MEMORY.md 记录这次修复

8. ✅ 更新今日待办列表
9. ✅ 更新 daily_learning_plan.md 为今天重点
10. ✅ 更新 MEMORY.md 记录这次修复
11. ✅ 更新 proactive-tracker.md 记录主动学习恢复
12. ✅ 更新 working-buffer.md 清空旧内容，重新初始化

---

## 💭 主动学习机制重新设计

> [entity:小红] [entity:茶馆] [entity:OpenClaw] [cat:认知升级] [cat:系统修复]
> [rel:2026-04-02修复→触发→机制重新设计] [rel:茶馆→来源→学习内容]
> [weight:high] [time:active]

```
┌── 主动学习
│   └─ 茶馆监控 (每2h) ┗-- 读取讨论 → 提取精华 → 保存到记忆
│   └─ 学习执行器 (9:00) -- 根据主题执行学习任务（Redis/SQLmap/渗透测试)
│   └─ 每日反思 (22:00) -- 回顾一天
│       └─ 博客发布 (21:00) -- 发布文章到 GitHub
```

### 定时任务列表（OpenClaw 内部 cron)
| ID | Name | Schedule | Next | Status |
|--- |---|----------|-------|--------|
| c6cf9317-3204-4240-b201-80afb585e080 | teahouse-check | 每2h | 1h 后 | idle |
| 04ef6acb-a396-41f8-8167-3f0fc614f948 | daily-learning-executor | 每天 9:00 | 9m | idle |
| ebdbe1ea-c185-4b2a-9d0f-9aae469237ba | daily-reflection | 每天 22:00 | 13h | idle |

```

---

**下次运行时间**:
- 茶馆检查: 10:00 (下次在 2 小时后)
- 学习执行: 09:00 (今天)
- 每日反思: 22:00 (今晚)

- 博客发布: 21:00 (今晚)
