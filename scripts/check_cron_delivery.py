#!/usr/bin/env python3
"""cron 投递状态检查 —— 由 scripts/healthcheck.sh 调用。

输出每行一个断言结果，格式：
  PASS <name> ...
  FAIL <name> ...
"""

import json
import os
import re
import subprocess
import sys
import datetime
from pathlib import Path


# 「本该发什么」—— E55 的核心维度
#
# 问题背景（2026-10-29）：
#   `delivered=false` 有两种完全相反的性质：
#     a) 有内容却没送到手上 → **真故障**（10-28 那批）
#     b) 根本没内容，不该打扰人 → **投递正确**（茶馆 cron「新增 0 条」那批）
#   字段本身没有这一维，光看 delivered 会把 (b) 误报成故障。
#
# 解法不是放宽断言，而是**加一维**：读 run 的产出摘要，判断「本该发什么」，
# 无产出 → 标注「有意不发」，不判 FAIL。
#
# ⚠️ 判据必须**保守**：宁可漏判也不误判 —— 只在**明确**匹配「无产出」语义时才放行，
# 匹配不到就按原来的严格判据走。

# 「本轮无产出」的语义标记（任一命中即视为「无产出」）
# 用「明确的无产出表述」，而不是「摘要里没有关键词」—— 后者会把长摘要误判成无产出。
_NO_OUTPUT_PATTERNS = [
    "无新内容",
    "无新议题",
    "没有新议题",
    "新增 0 条",
    "新增0条",
    "无新增",
    "无新蒸馏",
    "无内容",
    "无变更",
    "无异常",
    "nothing new",
    "no new content",
    "no new items",
    "nothing to report",
    "无待处理项",
    "无新增异常",
]

# 明确「有产出」的语义标记（优先级高于「无产出」）
# 若命中这些，说明这一轮**是有东西要发的**，即使含「无新增」也不能放行。
#
# ⚠️ 坑（实测 2026-10-29，单测当场抓出）：「有新增」是「没有新增」的**子串**！
#   「本轮没有新增议题」里含有「有新增」→ 会把明确的无产出误判成有产出。
#   → 这里只保留**不可能出现在否定句里**的标记；
#     「新增 N 条」这类用**数字限定**的形式（「新增 1 条」「新增1条」），
#     不要写裸的「新增 N 条」（会命中「新增 0 条」以外的模糊形式）。
_HAS_OUTPUT_PATTERNS = [
    "投递失败",
    "未送达",
    "未投递",
    "已修复",
    "修复了",
    "已生成",
    "已写入",
    "报告如下",
    "本轮新增",          # 「本轮新增 3 条」；但「本轮没有新增」不含「本轮新增」
    "本次新增",
    "新增 1 条",
    "新增1条",
    "新增 2 条",
    "新增2条",
    "新增 3 条",
    "新增3条",
    "新增 4 条",
    "新增4条",
    "新增 5 条",
    "新增5条",
]


def _classify_output(summary):
    """判断这次 run「本该发什么」。

    返回三态：
      "none"  → 明确无产出，不投递是正确的
      "has"   → 明确有产出
      "unknown" → 判不准 → 按严格判据处理（不放松断言）

    保守优先：只有**明确**匹配无产出语义、且**不**匹配有产出语义时，才返回 "none"。
    """
    if not summary:
        return "unknown"
    text = summary.lower()

    # 先看「有产出」标记 —— 它的优先级更高
    for p in _HAS_OUTPUT_PATTERNS:
        if p.lower() in text:
            return "has"

    # 「无产出」判定：必须命中**明确的**无产出表述。
    # 额外排除否定式歧义：「没有新增」这类句子里可能同时含「无新增」的变体，
    # 只有命中下面这些**肯定式**表述才算「明确无产出」。
    for p in _NO_OUTPUT_PATTERNS:
        if p.lower() in text:
            return "none"

    return "unknown"


def _max_consecutive_errors(runs):
    """返回**当前仍在持续的连续失败次数**（runs 按时间倒序，最新在前）。

    为什么要这个维度（2026-11-03 实测）：
      同样是「3 次 error」，散落在 20 次里 vs **连续 3 天**，
      运维含义完全不同 —— 后者是**静默中断**，用户完全无感。
      daily-learning 10-30/10-31/11-01 连挂 3 天，
      而巡检只因 lastRunStatus=ok 就报「投递正常」。

    ⚠️ 2026-11-17 修正（真 bug，巡检报错了数字）：
      原实现取**窗口内的最长连续段**（`best = max(best, cur)`），
      **包括早就恢复过的历史段**。

      实测翻车（daily-reflection，limit=20 窗口）：
        窗口序列 = E E o E o o E o E E E E E E E E E E E E
        11-15 E / 11-08 E / 11-03 o / … / 09-01 E … 08-02 E
        → 原实现算出「**最长连续失败 12 次**」，那一段其实是
          **2026-08-02 → 09-01 的历史旧账**（早已恢复）。
        → 但报出来的是「daily-reflection 连续失败 12 次，任务已中断约 12 天」，
          **这是两件不同的事被拼在一起**：
            · 「连续失败 12 次」= 8 月那笔旧账（已恢复）
            · 「已中断约 12 天」= 11-03 成功 → 11-15 失败 的**真实跨度**
          → **次数和跨度来自两个不同的段**，拼成一句话就成了假消息。

      修法：**只数「从最新一次往回、还没被成功打断的那一段」**——
      也就是**当前是否还在持续失败**。这正是「连续失败」该问的问题：
      「它现在还在挂吗？挂了多久？」

      → 历史旧段改由 `_max_consecutive_errors_history()` 单独报
        （【D】组已有「历史上曾连续失败 N 次」的 WARN 通道，不该混进来）。
    """
    cur = 0
    for r in runs:
        if r.get("status") == "error":
            cur += 1
        else:
            break
    return cur


def _max_consecutive_errors_history(runs):
    """返回窗口内**历史上最长**的一段连续失败次数（不要求是当前这段）。

    用途：给「历史上曾连续失败 N 次（现已恢复）」那条 WARN 提供数字 ——
    **当前段**归 `_max_consecutive_errors()` 管，**历史段**归这里管，
    两个数字不再互相串味（2026-11-17 实测的 bug 就是它们串了）。

    ⚠️ E79（2026-11-19 实测）：**只返回次数是不够的** ——
    次数必须和「天数」来自**同一段**，否则会拼出事实正确、整体荒谬的消息。
    → 这里额外提供 `_max_consecutive_errors_segment()`，
      返回**那一段本身**（含首末时间戳），供天数也从同一段取。
    """
    best = 0
    cur = 0
    for r in runs:
        if r.get("status") == "error":
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def _max_consecutive_errors_segment(runs):
    """返回窗口内**历史上最长**的那段连续失败**本身**。

    返回 dict：
      `count`      —— 这段有多少次失败
      `first_ms`   —— 这段里**最旧**一次失败的时间戳
      `last_ms`    —— 这段里**最新**一次失败的时间戳
      `has_success_before` —— 这段之前是否还有一次成功（有的话跨度才「有界」）

    ⚠️ 为什么需要它（E79）：
    `_max_consecutive_errors_history()` 给出「最长段的**次数**」，
    `_outage_span_days()` 给出「**最近段**的天数」——
    两者**指向不同的段**，拼出来的话就是假的。
    → 报一句话之前，**所有数字必须来自同一段**。
    """
    best = 0
    cur = 0
    # ⚠️ runs 是**倒序**（最新在前）→ 一段连续失败里，
    #   **第一个遇到的 error 是这段最新的一次**，之后遇到的**越来越旧**。
    #   → 段末 = 第一个 error；段首 = 这段的**最后一个** error。
    #   （别和「段首/段末」的直觉反着写 —— 这里容易搞反，
    #     2026-11-19 实测就因此把 first/last 装反了。）
    seg_head = None      # 当前段的**最新**一次失败（段的第一个 error）
    seg_tail = None      # 当前段的**最旧**一次失败（段的最后一个 error）
    best_head = None
    best_tail = None
    best_ok_before = None
    newest_ms = None

    for idx, r in enumerate(runs):
        ms = r.get("runAtMs")
        if idx == 0:
            newest_ms = ms
        if r.get("status") == "error":
            if cur == 0:
                seg_head = ms       # 新的一段 → 第一个 error = 最新
            seg_tail = ms          # 每遇到一次就更旧 → 最后一次 = 最旧
            cur += 1
            if cur > best:
                best = cur
                best_head = seg_head
                best_tail = seg_tail
                # 这段之前（更旧的）是否有成功？runs 是**倒序**（最新在前），
                # 所以「更旧」= 更大的 idx
                best_ok_before = None
                for x in runs[idx + 1:]:
                    if x.get("status") != "error":
                        best_ok_before = x.get("runAtMs")
                        break
        else:
            cur = 0
            seg_head = None
            seg_tail = None

    return {
        "count": best,
        "first_ms": best_tail,     # 段首 = 最旧一次
        "last_ms": best_head,      # 段末 = 最新一次
        "success_before_ms": best_ok_before,
        "has_success_before": best_ok_before is not None,
        "newest_ms": newest_ms,
    }


def _segment_span_days(seg):
    """算**指定段**的持续天数（按日历天算）。

    ⚠️ E79：这个函数存在的意义就是**替代**「拿另一个段的天数来配这段的次数」。
    段内首末的跨度 = (最后一次失败 − 段前最后一次成功)，
    与 `_outage_span_days` 的算法一致，但**只针对传入的这一段**。

    没有「段前成功」时（全程都在挂）→ 用「最早一次失败 → 现在」。
    """
    if not seg or seg.get("count", 0) <= 0:
        return 0
    first_ms = seg.get("first_ms")
    last_ms = seg.get("last_ms")
    if not isinstance(first_ms, (int, float)) or not isinstance(last_ms, (int, float)):
        return 0
    if seg.get("has_success_before"):
        # 段前那次成功的时间戳 —— 由调用方通过 runs 找不到时，退化为段内跨度
        ok_ms = seg.get("success_before_ms")
        if isinstance(ok_ms, (int, float)):
            return max(1, round((last_ms - ok_ms) / 86400000))
    # 全程都在挂 → 用「最早一次失败到现在」
    newest = seg.get("newest_ms")
    if isinstance(newest, (int, float)):
        return max(1, round((newest - first_ms) / 86400000) + 1)
    return max(1, round((last_ms - first_ms) / 86400000) + 1)


def _outage_span_days(runs):
    """连续失败已经持续了几天（按日历天算）。

    ⚠️ 为什么不用「次数」当严重性指标（E71）：
    一个每天跑 1 次的任务，连续 6 次 = 中断 6 天；
    但一个每小时跑 24 次的任务，连续 6 次 = 只中断了 15 分钟 ——
    **同样的次数，运维含义差了两个数量级**。
    → 所以断言的是**日历跨度**，不是次数。

    判据：找到**最近的一段连续失败**，量「它前面最后一次成功 → 这段里最后一次失败」
    跨了多少天。

    ⚠️ 关键（2026-11-16 实测 bug）：**终点必须是故障本身，不能是「现在」**。
    旧写法取「最新一次运行 − 最后一次成功」，结果：
      11-09 成功 → 11-10~11-15 连挂 6 次 → 11-16 恢复
      旧算法：11-16(最新, ok) − 11-09 = **1 天** ← 把 6 天的中断报成 1 天
      正确：  11-15(最后失败) − 11-09 = **6 天**
    → 一旦任务恢复，旧的算法就会把历史中断**缩到最短** ——
      正好抹掉最该记住的那段。所以要**独立地找失败段**，而不是「顺着最近一次成功算」。

    → 更通用的教训：**算「持续了多久」时，终点必须是故障本身**。
      用「现在」当终点，恢复后就归零 —— 而那正是最该留痕的时刻。
    """
    if not runs:
        return 0
    times = []
    for r in runs:
        ms = r.get("runAtMs")
        if isinstance(ms, (int, float)):
            times.append((ms, r.get("status")))
    if not times:
        return 0
    # times 是倒序（最新在前）→ 从新到旧扫，找出**最近的一段连续失败**
    i = 0
    while i < len(times) and times[i][1] != "error":
        i += 1
    if i >= len(times):
        return 0   # 最近一次是成功，且往前没有失败段
    # 失败段 = times[i:j]，j 是这段之后（更旧）的第一个成功
    j = i
    while j < len(times) and times[j][1] == "error":
        j += 1
    last_fail = times[i][0]          # 失败段里**最新**的一次
    first_fail = times[j - 1][0]     # 失败段里**最旧**的一次
    if j < len(times):
        last_ok = times[j][0]        # 失败段之前的那次成功
        return max(1, round((last_fail - last_ok) / 86400000))
    # 失败段之前没有成功 → 全程都在挂 → 用「最早一次失败到现在」
    # ⚠️ +1：这是**日历天数**，不是时间差。
    #   每天挂一次、共 6 次 → 覆盖的是 6 个日历日（11-10,11,12,13,14,15）
    #   而 11-15 − 11-10 = 5 个「日期间隔」→ 不 +1 会少算 1 天。
    #   「任务已经坏了几天」问的是**有几天没产出**，那是日历天的个数。
    newest = times[0][0]
    return max(1, round((newest - first_fail) / 86400000) + 1)


def _is_quota_exhausted(err: str) -> bool:
    """判断这个 rate limit 是不是**账号级额度耗尽**（需要牛哥哥充值）。

    为什么不认字面量（2026-11-09 实测）：
      额度耗尽的报错**措辞会变**。11-02 是「Token Plan 用量上限」，
      11-08 变成「⚠️ API rate limit reached. Please try again later. (rate_limit)」。
      → 认字面量的匹配器会**静默漏判**，把长期项误报成「可处理」。

    为什么认「全部模型同时中招」：
      - 账号级额度耗尽 → 该账号下**所有**候选模型一起 429（实测 3/3 全挂）
      - 每分钟/并发限流 → 通常只打掉其中一两个模型，且很快恢复
      所以「同时挂的模型数量」比「报错里写了什么词」更能区分根因。

    判据（任一成立即认为是额度耗尽）：
      1) 报错里点名「Token Plan 用量上限」
      2) **全部**候选模型同时失败（All models failed 且 N≥2）且带限流/额度语义
         —— 这一条**不依赖具体措辞**：「全部模型同时挂」本身就是账号级信号
      3) 报错点名了限流/配额语义（rate limit / quota / 429 / 用量上限），
         且**没有**说明只挂了单个模型

    ⚠️ 第 2 条刻意**不要求**出现 "rate limit" 字样：
      措辞一变（今天 "rate limit"、明天 "quota exhausted"）就漏判，
      正是本次要修的病。特征（同时挂几个模型）比字面量稳定。
    """
    if not err:
        return False
    if "Token Plan" in err:
        return True
    low = err.lower()
    # 「All models failed (N)」里的 N = 同时失败的候选模型数
    m = re.search(r"All models failed \((\d+)\)", err)
    n_failed = int(m.group(1)) if m else 0
    # 限流/配额语义（措辞无关的一组同义词）
    has_quota_semantics = any(
        k in low for k in ("rate limit", "rate_limit", "quota", "429", "用量上限", "限额")
    )
    if n_failed >= 2 and has_quota_semantics:
        return True
    if n_failed >= 3:
        # 全部候选模型一起挂，且没有别的语义 —— 仍然是账号级信号
        return True
    if has_quota_semantics and "all models failed" not in low:
        # 单模型失败但明说限流：可能是每分钟抖动，**不算**额度耗尽
        return False
    return False


def _load_history(env, job_id, limit=20):
    """拉取某个 job 的历史运行记录（用于检查历史投递失败）。

    `cron list --json` 只给「最近一次」的状态，看不到历史。
    历史里藏着「ok 但没投出去」的静默失败 —— 断言只看 lastRun 就会漏。
    """
    try:
        r = subprocess.run(
            ["openclaw", "cron", "runs", "--id", job_id, "--limit", str(limit)],
            capture_output=True, text=True, timeout=90, env=env,
        )
        d = json.loads(r.stdout[r.stdout.find("{"):])
        return d.get("runs", d.get("entries", []))
    except Exception:
        return None  # 取不到不算失败，但不能当成「没问题」


def main() -> int:
    # 日志降噪：每次 CLI 调用会重新注册全部插件，INFO 级打 6+ 行
    # 「feishu_xxx: Registered ...」，占当日日志 87%。
    # 实测 27008 字节/次 → 5735 字节/次（-79%）。
    # 注意：不能改 openclaw.json 里的 logging 段（那会触发 loadConfig 递归），
    # 只能用环境变量，且只影响本子进程。
    env = dict(os.environ)
    env.setdefault("OPENCLAW_LOG_LEVEL", "warn")

    # ⚠️ 2026-11-16 修正（防「断言装死」）：
    #   原来用 `openclaw cron list --json` 取任务 —— 但它**不列出已禁用的任务**。
    #   2026-11-16 起 daily-learning / daily-reflection 都改由 **systemd 闸门触发**
    #   （cron 里 disabled，只作为被触发的目标）→ `cron list` 返回 "No cron jobs"
    #   → 这个循环**一次都不跑** → 【D】组**什么都没检查却显示通过**。
    #
    #   这正是「断言装死」：报告全绿，但检查对象已经不在视野里了。
    #   （同族教训：【X】组专门防「解析器坏了却不报错」。）
    #
    #   修法：直接读 cron 的**任务存储**（同一份数据源，只是不按 enabled 过滤）。
    #   读不到再回退到 `cron list`。
    jobs = None
    try:
        jobs_file = Path("/root/.openclaw/cron/jobs.json")
        if jobs_file.exists():
            d = json.loads(jobs_file.read_text(encoding="utf-8"))
            jobs = d if isinstance(d, list) else d.get("jobs", d.get("data", []))
    except Exception as e:
        print(f"WARN cron 任务存储读取失败（{type(e).__name__}）→ 回退到 cron list")
        jobs = None

    if jobs is None:
        try:
            r = subprocess.run(
                ["openclaw", "cron", "list", "--json"],
                capture_output=True, text=True, timeout=90, env=env,
            )
            d = json.loads(r.stdout[r.stdout.find("{"):])
            jobs = d if isinstance(d, list) else d.get("jobs", d.get("data", []))
        except Exception as e:
            print(f"FAIL cron 状态读取 — {type(e).__name__}")
            return 1

    failed = 0
    # ⚠️ 一个任务都没读到 → 必须在报告里**显式说出来**，
    #    否则「检查了 0 个任务」和「检查了 2 个任务全部通过」长得一模一样。
    if not jobs:
        print("FAIL 读不到任何 cron 任务 —— 【D】组本次**什么都没检查**（断言装死）")
        print("__FAILED__=1")
        return 1

    for j in jobs:
        name = j.get("name", "?")
        dl = j.get("delivery", {}) or {}
        st = j.get("state", {}) or {}
        mode = dl.get("mode")
        run_status = st.get("lastRunStatus")
        # reason 在下面按需填充；先初始化，避免「历史上失败过但最近一次好了」
        # 的任务走到 E71 分支时 NameError（E11：不抄判据，用真身，且不许崩）。
        reason = ""

        # 从未运行过的新任务不算失败（没有历史 ≠ 出故障）
        if run_status is None and not st.get("lastRunAtMs"):
            print(f"WARN {name} 尚未运行过（新建任务，等待首次执行）")
            continue

        if run_status != "ok":
            # 2026-11-02：把**失败原因**带进 FAIL 文本。
            # 原因决定分类（额度耗尽 = 等牛哥哥充值；脚本报错 = 我能修），
            # 只有状态码的话，两种完全不同的故障长得一模一样。
            reason = ""
            job_id = j.get("id") or j.get("jobId")
            if job_id:
                try:
                    hist = _load_history(env, job_id, limit=1)
                    if hist:
                        err = str(hist[0].get("error") or "")
                        # 额度耗尽的**措辞会变**，认字面量就会漏判。
                        # 2026-11-09 实测：11-02 的报错是「Token Plan 用量上限」，
                        # 11-08 已变成「⚠️ API rate limit reached (rate_limit)」——
                        # 措辞一变，healthcheck 的 WAITING_PATTERNS("Token Plan")
                        # 就再也匹配不上 → 额度问题被误报成「可处理 - 现在就该修」。
                        #
                        # 真正的判据不是字面量，而是**特征**：
                        # 全部候选模型同时报 rate limit = 账号级额度耗尽（计费周期），
                        # 而每分钟限流只会打掉其中一两个模型、且很快就恢复。
                        if "Token Plan" in err:
                            reason = "，原因：Token Plan 用量上限(需充值)"
                        elif _is_quota_exhausted(err):
                            reason = "，原因：额度耗尽(API rate limit，需充值)"
                        elif "rate limit" in err.lower():
                            reason = "，原因：接口限流"
                        elif "timed out" in err:
                            reason = "，原因：执行超时"
                        elif err:
                            reason = f"，原因：{err[:60]}"
                except Exception:
                    pass
            # ── E71：连续失败 ≥3 次 → 只报**持续中断**那一条，不重复报「单次失败」──
            #
            # 为什么合并成一条：两条 FAIL 说的是**同一个任务**的**同一个事实**
            #   （a）"daily-learning 运行失败"        ← 单次视角
            #   （b）"daily-learning 连续失败 6 次，已中断 6 天" ← 持续视角
            # (b) 完全**包含** (a) 的信息（还多带了中断时长）→
            # 两条都报 = 同一个任务在报告里出现两次 = 噪音（E66）。
            # → 所以 consec ≥3 时**只报 (b)**，并把它计入 failed。
            consec_now = _max_consecutive_errors(_load_history(env, job_id, limit=20)) \
                if job_id else 0
            if consec_now >= 3:
                span_now = _outage_span_days(_load_history(env, job_id, limit=20))
                print(
                    f"FAIL {name} 连续失败 {consec_now} 次，任务已中断约 {span_now} 天！"
                    f"（lastRunStatus={run_status}{reason}）"
                )
                failed += 1
                sustained_reported = True
            else:
                print(f"FAIL {name} 运行失败（lastRunStatus={run_status}{reason}）")
                failed += 1
        elif mode == "announce":
            job_id = j.get("id") or j.get("jobId")
            # E55：先问「本该发什么」—— 无产出则不投递是正确的
            output_kind = "unknown"
            if job_id:
                runs_now = _load_history(env, job_id, limit=1)
                if runs_now:
                    output_kind = _classify_output(runs_now[0].get("summary"))
            if output_kind == "none":
                print(f"PASS {name} 无产出，未投递是正确的（E55：本该发什么）")
            elif st.get("lastDelivered"):
                print(f"PASS {name} 投递正常（delivered=True）")
            else:
                print(
                    f"FAIL {name} 投递未送达！"
                    f"（delivery={st.get('lastDeliveryStatus')}, to={dl.get('to', '-')}）"
                )
                failed += 1
        else:
            print(f"PASS {name} 运行正常（no-deliver）")

        # ---- 历史投递检查 ----
        # lastRunStatus 只反映「最近一次」，历史上「ok 但没投出去」的静默失败会被漏掉。
        # 实测（2026-10-27）：daily-reflection 有 2 次 ok-but-not-delivered
        # （10-26 00:35、10-21 04:27），另有 40 次历史 error —— lastRun 看起来全绿。
        job_id = j.get("id") or j.get("jobId")
        if mode == "announce" and job_id:
            runs = _load_history(env, job_id)
            if runs is None:
                print(f"WARN {name} 历史投递状态读取失败（无法判断，非失败）")
                continue
            undelivered = [
                r for r in runs
                if r.get("status") == "ok" and r.get("delivered") is False
            ]
            error_runs = [r for r in runs if r.get("status") == "error"]

            # ── 2026-11-03 更正：error 计数必须【无条件】报告 ──
            #
            # 原 bug：error 统计写在 `else:` 分支里（undelivered 为空时）。
            # 只要历史上存在任何一次「ok 但没投递」，就永远走不到 else →
            # **error 次数被完全吞掉**。
            # 实测（daily-learning，20 次运行里 7 次 error，含 10-30/10-31/11-01
            # 三次连续失败）：报告只说「3/20 次运行成功但未投递」，
            # **3 天连续全挂只字未提**，而 lastRunStatus=ok 让它看起来一切正常。
            # → 这正是 E57「监控路径与被监控路径共用会死的依赖」的变体：
            #   巡检自己的判据有盲区，于是故障静悄悄通过。
            #
            # 修法：error 统计与 undelivered 统计【正交】，两边都要报。
            # ── 2026-11-16 加（E71）：连续失败 ≥3 次 → 必须升级，不能停在 WARN ──
            #
            # 背景（实测）：daily-learning 从 11-10 起**连续 6 天**每天 09:00 失败，
            # 11-12 ~ 11-16 的 memory/YYYY-MM-DD.md **全部不存在**。
            # 而巡检每天早上都把这个事实算出来、写进报告 —— 然后归档。
            #
            # 问题不在于「测得不准」，而在于**分类把它降级了**：
            #   「连续 6 次失败」被放进 WARN 区 → 不进「现在就该修」→ **没人动**
            #   → 失败照旧再叠一天 → 明天再报一次同样的 WARN
            # → 一个已经死了 6 天的任务，在报告里看起来和一次偶发失败一样重。
            #
            # 修法（不是把 WARN 改标签就完事）：
            #   ① 连续失败 ≥3 次 → **额外**产出一条 FAIL，断言的是
            #      「**持续中断时长**」而不是「这一次失败了」—— 两件事，不重复。
            #   ② 额度类原因 → FAIL 文本里带上「额度耗尽」字样
            #      → healthcheck 的 is_waiting() 把它归进「⏳ 等你」
            #      → **不会**变成「可处理 - 现在就该修」的假警报（额度我改不了）
            #   ③ 同时**抑制**原来那条 WARN —— 同一个事实报两遍就是噪音，
            #      而 E66 已经教会我们：假警报会淹没真信号。
            if error_runs:
                err_ts = []
                for r in error_runs[:5]:
                    ms = r.get("runAtMs")
                    err_ts.append(
                        datetime.datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M")
                        if isinstance(ms, (int, float)) else "?"
                    )
                # 连续失败比零散失败严重得多：连续 N 天挂 = 静默中断 N 天
                # ⚠️ 2026-11-17：这里问的是「**历史上**曾连续挂过多长」，
                #   因为这条分支只在 run_status == "ok"（**已恢复**）时才走到。
                #   → 必须用**历史段**的函数；用「当前段」的话，
                #     任务一恢复当前段就是 0 → 这条 WARN 会**永远不报**。
                consec = _max_consecutive_errors_history(runs)
                # ⚠️ E79（2026-11-19 实测）：**次数和天数必须来自同一段**。
                #   旧写法：次数取「窗口内最长段」，天数取「最近段」——
                #   两者**指向不同的段**，拼出的话事实正确、整体荒谬
                #   （例：5 次跨 4 天，却报「约 11 天」）。
                #   → 改用「最长段本身」，天数也从**那一段**量。
                seg = _max_consecutive_errors_segment(runs)
                span_days = _segment_span_days(seg)
                # 段的首末时间戳 —— 报告里带上，读的人能自己核对跨度
                seg_from = seg.get("first_ms")
                seg_to = seg.get("last_ms")
                seg_ts = ""
                if isinstance(seg_from, (int, float)) and isinstance(seg_to, (int, float)):
                    fmt = "%Y-%m-%d %H:%M"
                    seg_ts = (f"{datetime.datetime.fromtimestamp(seg_from / 1000).strftime(fmt)}"
                              f" → {datetime.datetime.fromtimestamp(seg_to / 1000).strftime(fmt)}")
                # ⚠️ E71：上面（run_status != "ok" 分支）如果已经报了
                #    「连续失败 N 次，已中断 N 天」这条 FAIL，
                #    这里**不要**再报一遍同样的事实 —— 同一个任务出现两次
                #    就是噪音（E66），而噪音会淹没真信号。
                #
                # 分两种情况：
                #   · 任务**现在还在挂** → 上面已经报了 FAIL，这里不重复
                #   · 任务**已经恢复**（最近一次 ok）→ 历史遗留，不该记 FAIL，
                #     但「曾经连续挂过 N 天」这个事实值得留痕 → WARN
                if consec >= 3:
                    if run_status == "ok":
                        print(
                            f"WARN {name} 历史上曾连续失败 {consec} 次"
                            f"（约 {span_days} 天，现已恢复）"
                            + (f" —— 该段 {seg_ts}" if seg_ts else "")
                        )
                    # 现在还挂着 → 上面已报 FAIL，这里不重复
                elif consec >= 2:
                    print(
                        f"WARN {name} 最近 {len(runs)} 次里有 {len(error_runs)} 次 error"
                        f"，其中最多【连续 {consec} 次】失败（最近一次正常）："
                        f"{', '.join(err_ts)}"
                    )
                else:
                    print(
                        f"WARN {name} 最近 {len(runs)} 次里有 {len(error_runs)} 次 error"
                        f"（最近一次正常）：{', '.join(err_ts)}"
                    )

            if undelivered:
                ts = []
                for r in undelivered[:5]:
                    ms = r.get("runAtMs")
                    ts.append(
                        datetime.datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M")
                        if isinstance(ms, (int, float)) else "?"
                    )
                # E55：逐条判「本该发什么」—— 无产出的那些不算故障
                silent_faults = []
                intentional = []
                for r in undelivered:
                    kind = _classify_output(r.get("summary"))
                    if kind == "none":
                        intentional.append(r)
                    else:
                        silent_faults.append(r)

                # 最近一次仍是坏的 → FAIL；历史遗留但最近已好 → WARN
                if runs and runs[0].get("status") == "ok" and runs[0].get("delivered") is False:
                    if silent_faults:
                        print(
                            f"FAIL {name} 最近一次运行未投递！"
                            f"（历史 {len(silent_faults)}/{len(runs)} 次未投递）"
                        )
                        failed += 1
                    else:
                        print(
                            f"PASS {name} 最近一次未投递但无产出"
                            f"（有意不发，E55；历史 {len(intentional)} 次均为无产出）"
                        )
                else:
                    if silent_faults:
                        print(
                            f"WARN {name} 历史上有 {len(silent_faults)}/{len(runs)} 次"
                            f"「运行成功但未投递」（最近一次正常）：{', '.join(ts)}"
                        )
                    else:
                        print(
                            f"PASS {name} 历史未投递均为无产出（有意不发，E55）"
                            f"（{len(intentional)} 次）"
                        )
            else:
                # 2026-11-03：error 统计已提到【无条件】分支（见上）。
                # 这里保留原有的「历史投递均正常」PASS 判定。
                if not error_runs:
                    print(f"PASS {name} 历史投递均正常（近 {len(runs)} 次）")

    # 供 shell 统计失败数
    print(f"__FAILED__={failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
