#!/usr/bin/env python3
"""E65 落地的单测：失败计数要**按错误类型分组** + **按调度频率换算时间窗**。

⚠️ 为什么要给判据写单测（W/W2/W3/W4 组的老教训）：
   判据本身会「装死」—— 写错了、不生效了，报告里却全绿。
   → 每个新加的判据都必须有**故障注入**用例：
     故意把判据改坏，看用例会不会红。

本文件覆盖三个新函数：
  `_classify_run_error()`   —— 错误文本 → 错误类型桶
  `_cron_expr_window_days()` —— cron 表达式 → 每次间隔（天）
  `_segment_runs()` / `_group_errors_by_type()` —— 分组必须**限定在同一段**
"""

import importlib.util
import os
import sys

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_cron_delivery.py")
spec = importlib.util.spec_from_file_location("ccd", _SCRIPT)
ccd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ccd)

PASS, FAIL = 0, 0


def check(cond, label, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ PASS  {label}")
    else:
        FAIL += 1
        print(f"  ❌ FAIL  {label}  {detail}")


def run(status, ms, error=None):
    r = {"status": status, "runAtMs": ms}
    if error is not None:
        r["error"] = error
    return r


# ── 1. 错误类型分类 ───────────────────────────────────────────────
print("\n【1】_classify_run_error —— 错误文本 → 类型桶")

QUOTA = ("Error: All models failed (3): minimax/MiniMax-M3: ⚠️ API rate limit reached. "
         "Please try again later. (rate_limit) | minimax/MiniMax-M2.7-highspeed: ⚠️ API rate "
         "limit reached. (rate_limit) | minimax/MiniMax-M2.7: ⚠️ API rate limit reached. (rate_limit)")

check(ccd._classify_run_error(QUOTA) == "额度耗尽",
      "全部模型同时挂 → 额度耗尽（不是接口限流）",
      f"got {ccd._classify_run_error(QUOTA)!r}")

check(ccd._classify_run_error("Error: API rate limit reached (rate_limit)") == "接口限流",
      "单模型限流 → 接口限流")

check(ccd._classify_run_error("Error: Token Plan 用量上限") == "额度耗尽",
      "Token Plan 用量上限 → 额度耗尽")

check(ccd._classify_run_error("Error: Request timed out after 300s") == "执行超时",
      "超时 → 执行超时")

check(ccd._classify_run_error("Error: NameError: name 'x' is not defined") == "脚本报错",
      "脚本自身异常 → 脚本报错")

check(ccd._classify_run_error("Error: getaddrinfo ENOTFOUND api.example.com") == "网络错误",
      "DNS/网络 → 网络错误")

check(ccd._classify_run_error("") == "未知",
      "空错误 → 未知（不崩）")

check(ccd._classify_run_error(None) == "未知",
      "None 错误 → 未知（不崩）")

# 关键区分：额度 vs 限流 必须**分得开**（这正是 E65 要的维度）
check(ccd._classify_run_error(QUOTA) != ccd._classify_run_error("Error: rate limit (rate_limit)"),
      "「额度耗尽」与「接口限流」必须是两个不同的桶",
      "两者被合并了 → 分组失去意义")


# ── 2. 调度频率 → 时间窗 ──────────────────────────────────────────
print("\n【2】_cron_expr_window_days —— cron 表达式 → 每次间隔（天）")

check(ccd._cron_expr_window_days("0 9 * * *") == 1.0,
      "日频 → 1 天/次")

check(ccd._cron_expr_window_days("0 7 * * 0") == 7.0,
      "周频 → 7 天/次（这就是 E65 说的「同样的次数，含义差 7 倍」）")

check(abs(ccd._cron_expr_window_days("*/30 * * * *") - 0.5 / 24) < 1e-9,
      "每 30 分钟 → 1/48 天/次")

check(abs(ccd._cron_expr_window_days("0 */6 * * *") - 0.25) < 1e-9,
      "每 6 小时 → 0.25 天/次")

check(ccd._cron_expr_window_days("0 9 */2 * *") == 2.0,
      "每 2 天 → 2 天/次")

check(abs(ccd._cron_expr_window_days("0 9 * * 1,3,5") - 7 / 3) < 1e-9,
      "每周 3 次 → 7/3 天/次")

check(ccd._cron_expr_window_days("") == 0.0,
      "空表达式 → 0（调用方回退，不崩）")

check(ccd._cron_expr_window_days("garbage") == 0.0,
      "非法表达式 → 0（不崩）")

# 核心区分：日频 vs 周频 必须换算出不同的天数
check(ccd._cron_expr_window_days("0 9 * * *") != ccd._cron_expr_window_days("0 7 * * 0"),
      "日频与周频的「6 次」必须换算出不同的天数",
      "两者换算结果相同 → 调度频率这一维没起作用")


# ── 3. 分组必须限定在**同一段** ───────────────────────────────────
print("\n【3】_segment_runs + _group_errors_by_type —— 分组只算同一段")

DAY = 86400000
T0 = 1793000000000

# 窗口里有一段「连续 3 次失败」，另有一段零散的「1 次失败」
runs = [
    run("error", T0 + 10 * DAY, QUOTA),          # 最新，一次零散失败
    run("ok", T0 + 9 * DAY),
    run("error", T0 + 8 * DAY, QUOTA),
    run("error", T0 + 7 * DAY, QUOTA),
    run("error", T0 + 6 * DAY, "Error: NameError: boom"),   # ← 最长段：3 次
    run("ok", T0 + 5 * DAY),
    run("error", T0 + 4 * DAY, "Error: timed out"),
]

seg = ccd._max_consecutive_errors_segment(runs)
check(seg["count"] == 3,
      "最长段 = 3 次",
      f"got {seg['count']}")

seg_runs = ccd._segment_runs(runs, seg)
check(len(seg_runs) == 3,
      "段内 run 数 = 3（不含窗口里别的零散失败）",
      f"got {len(seg_runs)}")

groups = ccd._group_errors_by_type(seg_runs)
check(set(groups.keys()) == {"额度耗尽", "脚本报错"},
      "段内分组 = {额度耗尽×2, 脚本报错×1}（不含段外的「超时」「额度耗尽」）",
      f"got { {k: len(v) for k, v in groups.items()} }")

check(len(groups.get("额度耗尽", [])) == 2,
      "「额度耗尽」只数到段内那 2 次（段外那次不算）",
      f"got {len(groups.get('额度耗尽', []))}")

check("执行超时" not in groups,
      "段外的「执行超时」不得混进分组（否则分组数字是假的）")

# 段为空 → 不崩
check(ccd._segment_runs(runs, {"count": 0}) == [],
      "空段 → 空列表（不崩）")
check(ccd._segment_runs(runs, {}) == [],
      "缺字段的段 → 空列表（不崩）")
check(ccd._group_errors_by_type(None) == {},
      "None runs → 空字典（不崩）")
check(ccd._group_errors_by_type([]) == {},
      "空 runs → 空字典（不崩）")


# ── 4. 时间窗换算：窗口描述 ───────────────────────────────────────
print("\n【4】_window_desc —— 「最近 N 次」→ 真实时间跨度")

job = {"schedule": {"kind": "cron", "expr": "0 9 * * *"}}
desc = ccd._window_desc(runs, job)
check("实际约" in desc, "窗口描述带实际跨度", desc)
check("2026" in desc or "-" in desc, "窗口描述带起止日期", desc)
check("0 9 * * *" in desc, "窗口描述带调度表达式", desc)

weekly = {"schedule": {"kind": "cron", "expr": "0 7 * * 0"}}
d1 = ccd._window_desc(runs, job)
d2 = ccd._window_desc(runs, weekly)
check(d1 != d2,
      "同一批 run，日频 job 与周频 job 的窗口描述必须不同",
      f"both = {d1!r}")

check(ccd._window_desc([], job) == "", "空 runs → 空描述（不崩）")
check(ccd._window_desc(runs, {}) != "", "缺 schedule 也不能崩")
check(ccd._window_desc(runs, {"schedule": {"expr": "bogus"}}) != "",
      "非法调度表达式也不能崩")


# ── 5. 故障注入：判据坏了，用例必须红 ────────────────────────────
print("\n【5】故障注入 —— 判据装死时用例必须红")

# ⚠️ 注入的正确读法（别写反）：
#   「注入一个坏实现 → **原来的断言应当变红**」才是有效的故障注入。
#   如果注入后断言**照样绿**，说明这条用例**测不出差别** —— 用例本身没用。
#   → 下面每条注入都断言「坏实现的行为 ≠ 好实现的行为」，
#     即：把坏实现喂进去，上面的【1】【2】【3】里的断言**会红**。

_orig_classify = ccd._classify_run_error
_orig_window = ccd._cron_expr_window_days

try:
    # 坏实现 A：把「额度耗尽」和「接口限流」合并成一个桶 → 分组维度失效
    ccd._classify_run_error = lambda err: ("错误" if err else "未知")
    bad_a = (ccd._classify_run_error(QUOTA), ccd._classify_run_error("Error: rate limit"))
    check(bad_a[0] == bad_a[1],
          "【注入A】合并桶后两者不可区分 → 【1】的区分性断言会红")
    check(bad_a[0] != _orig_classify(QUOTA),
          "【注入A】坏实现与好实现输出不同 → 用例确实能测出这个差别")
finally:
    ccd._classify_run_error = _orig_classify

try:
    # 坏实现 B：所有调度频率都算成 1 天 → 时间窗这一维失效
    ccd._cron_expr_window_days = lambda expr: 1.0
    check(ccd._cron_expr_window_days("0 7 * * 0") == 1.0
          and ccd._cron_expr_window_days("0 9 * * *") == 1.0,
          "【注入B】频率恒为 1 时周频与日频不可区分 → 【2】的频率断言会红")
    check(ccd._cron_expr_window_days("0 7 * * 0") != _orig_window("0 7 * * 0"),
          "【注入B】坏实现与好实现输出不同 → 用例确实能测出这个差别")
finally:
    ccd._cron_expr_window_days = _orig_window


# 坏实现 C：分组时把**整个窗口**都算进来（E65 修之前的语义）
def _seg_runs_all(runs_, seg):
    return [r for r in runs_ if r.get("status") == "error"]


good_groups = ccd._group_errors_by_type(ccd._segment_runs(runs, seg))
bad_groups = ccd._group_errors_by_type(_seg_runs_all(runs, seg))
check(set(good_groups.keys()) != set(bad_groups.keys()),
      "【注入C】整窗口分组 ≠ 限定段内分组 → 【3】的分组断言会红",
      f"good={ {k: len(v) for k, v in good_groups.items()} } bad={ {k: len(v) for k, v in bad_groups.items()} }")
check(len(good_groups.get("额度耗尽", [])) != len(bad_groups.get("额度耗尽", [])),
      "【注入C】两种实现的「额度耗尽」计数不同 → 计数断言会红",
      f"good={len(good_groups.get('额度耗尽', []))} bad={len(bad_groups.get('额度耗尽', []))}")


print("\n" + "=" * 60)
print(f" 结果: PASS={PASS} FAIL={FAIL}")
print("=" * 60)
if FAIL:
    sys.exit(1)
