#!/usr/bin/env python3
"""【V2】「待落地」标记扫描的判据单测（含故障注入）。

⚠️ 为什么必须有这个单测（W/W2/W3/W4/W5 组的老教训）：
   判据会「装死」—— 改坏了、不生效了，巡检报告里却全绿。
   → 每条过滤规则都必须有**故障注入**：故意把判据改坏，看用例会不会红。

⚠️ 更重要的：**过滤规则本身就是「把判据改弱」的操作**。
   误伤过滤一旦写得太宽 → 真待办**不再被报出来** → 断言装死，
   而且**比原来的假警报更危险**（因为报告全绿，看起来没问题）。
   → 所以单测必须**双向**覆盖：
     ① 真待办**必须**仍被报出来（不能被过滤掉）
     ② 元引用**应该**被排除
     ③ 故障注入：过滤放宽 → ①会红

判据真身从 scripts/healthcheck.sh 的 V2 段里**抽取**（不抄一份）。
"""

import os
import re
import sys

WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEALTHCHECK = os.path.join(WS, "scripts", "healthcheck.sh")

PASS, FAIL = 0, 0


def check(cond, label, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ PASS  {label}")
    else:
        FAIL += 1
        print(f"  ❌ FAIL  {label}  {detail}")


def _extract_v2_block():
    """从 healthcheck.sh 里抽出 V2 段的 Python 代码（判据真身）。"""
    src = open(HEALTHCHECK, encoding="utf-8").read()
    marker = "pending_report=$(python3 - << 'PY'"
    i = src.find(marker)
    assert i != -1, "找不到 V2 段的 python heredoc"
    # 跳过 marker 后面的 shell 重定向（2>/dev/null 之类），直到行尾
    line_end = src.find("\n", i)
    header = src[i:line_end]
    assert "PY" in header, "heredoc 标记异常"
    j = src.find("\nPY\n", line_end)
    assert j != -1, "找不到 V2 段 heredoc 的结尾"
    return src[line_end + 1:j]


def _build_scan():
    """执行真身代码，取出判据函数与常量。"""
    ns = {}
    code = _extract_v2_block()
    # 只取定义部分（跳过最后的 print/raise），用 exec 拿到函数
    exec(compile(code, "healthcheck_V2", "exec"), ns)
    return ns


try:
    V2 = _build_scan()
except Exception as e:
    print(f"❌ 无法从 healthcheck.sh 抽取 V2 判据真身: {type(e).__name__}: {e}")
    sys.exit(1)

_is_meta_reference = V2["_is_meta_reference"]
_find_unresolved = V2["_find_unresolved"]
PENDING_PAT = V2["PENDING_PAT"]
FALSE_POS_PAT = V2["FALSE_POS_PAT"]
DONE_PAT = V2["DONE_PAT"]


def unresolved_in(text, ns=None):
    """调用**判据真身**算出「这段文本里还没解决的未完成声明」。

    ⚠️ ns 用于**故障注入**（把真身里的某个零件换成坏实现），
       默认用真身 —— 不抄判据（E61/E62 的教训）。
    """
    fn = _find_unresolved
    if ns is not None:
        fn = ns["_find_unresolved"]
    return fn(text)


# ── 1. 真待办**必须**仍被报出来（过滤不能把判据改弱）───────────
print("\n【1】真待办必须仍被报出来（过滤不得改弱判据）")

# E65 的真待办形态（落地前的原样）
E65_PENDING = """## 待落地（未实现）

【D】组的判据目前输出的是聚合数字。建议后续改成三段式：
- `按错误类型分组的历史计数`
- `按调度频率换算时间窗`
"""
check(len(unresolved_in(E65_PENDING)) == 1,
      "标题行「## 待落地（未实现）」= 真待办，必须被报出来",
      f"got {len(unresolved_in(E65_PENDING))}")

check(len(unresolved_in("本条讲：修法还没做，明天改。")) == 1,
      "正文里直接声明「修法还没做」= 真待办")

check(len(unresolved_in("这一项待落地，需要改判据代码。")) == 1,
      "正文里「这一项待落地」= 真待办")

check(len(unresolved_in("判据尚未修改，等牛哥哥决定。")) == 1,
      "正文里「尚未修改」= 真待办")

# 关键：带「**」粗体的真待办也**必须**被报出来
check(len(unresolved_in("## 待落地（未实现）\n\n**这一项还没改。**\n")) >= 1,
      "粗体里的真待办也必须被报出来",
      f"got {len(unresolved_in('## 待落地（未实现）\\n\\n**这一项还没改。**\\n'))}")

# ⚠️ 关键：**同一节里**出现「已落地」也算销账
check(len(unresolved_in("这一项待落地，但已落地。")) == 0,
      "同节内「已落地」→ 已销账")


# ── 2. 元引用应该被排除（这是这次要修的假警报）──────────────────
print("\n【2】元引用（讲机制本身）应被排除")

META_SAMPLES = [
    ("「修法（还没做）」「尚未修改」「待落地」这类**明确的未完成声明**。",
     "枚举「这类未完成声明」"),
    ("### P14：「带标记」不等于「有人会扫」—— 待落地标记需要专门的断言",
     "标题里带「待落地标记」"),
    ("如果那天我没读那条日志，E79 的「修法（还没做）」会再躺一天。",
     "引号里引用 E79 的标记"),
    ("## 🛡️ 10:45 —— 把「待落地」变成**机制**，不靠人记（新增【V2】组）",
     "标题里引用「待落地」机制"),
    ("上一段我写了「**待办：给『待落地』标记加一条专门扫它的断言**」——",
     "嵌套引号里的待办（指派了断言这件事本身，已落地）"),
    ("扫 L1 条目 + SESSION-STATE + 每日笔记里的「修法（还没做）」这类声明",
     "扫哪些文件时的枚举"),
    # ⚠️ 2026-11-24 加：短语**嵌套在引号内**时，正则匹配到的是**引号内部的子串**，
    #    「紧邻引号」检查两边都不成立 → 元引用漏判 → 假警报。
    #    实测：E85 落地那轮，我在笔记里写
    #      「修法（待落地，尚未实现）」。这轮心跳去把它做掉了，三条都落地：
    #    → PENDING_PAT 匹配到 `待落地`（不是整个 `修法（待落地，尚未实现）`）
    #    → before=`「修法（` 不以 `「` 结尾、after=`，尚未实现）` 不以 `」` 开头
    #    → 真·元引用被当成真待办 → 【V2】报了假警报。
    ("09:00 的 daily-learning 写下 E85，并诚实地标了「修法（待落地，尚未实现）」。\n"
     "这轮心跳我去把它做掉了，三条都落地：",
     "引号内嵌套子串的元引用（E85 落地那轮的真实假警报）"),
    ("它自己那句「修法（还没做）」当天没人管，暴露出扫描时机设计错了。",
     "引号内嵌套「修法（还没做）」"),
]
for text, label in META_SAMPLES:
    check(len(unresolved_in(text)) == 0,
          f"元引用应被排除：{label}",
          f"got {len(unresolved_in(text))} -> {[m.group() for m in unresolved_in(text)]}")

# ── 3. 已落地的声明不应再被报（销账）────────────────────────────
print("\n【3】已销账的声明不再报")

RESOLVED = """## 待落地（未实现）

## ✅ 落地状态（2026-11-20）

✅ **已于 2026-11-20 落地**。
"""
check(len(unresolved_in(RESOLVED)) == 0,
      "同段落里有「✅ 落地状态」→ 已销账")

RESOLVED2 = """## 待落地（未实现）

## ✅ 落地状态（2026-11-20）

三个诉求全部落地。
"""
check(len(unresolved_in(RESOLVED2)) == 0,
      "后续小节有「✅ 落地状态」→ 已销账")

# ⚠️ 边界：销账标记只在**紧邻的下一节**才算数 ——
#   「文件开头的声明」不能被「文件结尾的销账」覆盖
#   （否则整篇一刀切 → 断言装死）。
RESOLVED_FAR = """## 待落地（未实现）

正文说还没改。

## 别的小节

随便写点。

## 很后面的地方

✅ 落地状态
"""
check(len(unresolved_in(RESOLVED_FAR)) >= 1,
      "销账标记隔了**两节** → 不算销账（防「整篇一刀切」把开头的真待办覆盖掉）",
      f"got {len(unresolved_in(RESOLVED_FAR))}")

check(len(unresolved_in("这一项已落地。")) == 0,
      "「已落地」→ 已销账")


# ── 4. 故障注入：判据被改坏时用例必须红 ─────────────────────────
print("\n【4】故障注入 —— 过滤放宽 / 判据装死时用例必须红")

# ⚠️ 注入的正确读法：**把真身里的某个零件换成坏实现**，
#    然后断言「**原来的用例会红**」。
#
# ⚠️⚠️ 关键实现细节（2026-11-20 实测踩坑）：
#   注入必须**直接改 `V2` 这个命名空间**（真身函数的 `__globals__` 就是它），
#   **不能**用 `dict(V2)` 复制一份 —— 复制出来的函数仍然查**原**命名空间，
#   → 注入**根本没生效**，而用例却「通过」了 → **断言装死**，
#      而且是最危险的那种：**测试全绿、但什么都没测**。
#   → 所以每个注入都必须在 finally 里**还原**，
#      并且下面有一条「注入确实生效了」的**自检**（见【4-SELFTEST】）。

def _inject(key, value):
    """临时替换真身里的一个零件（并还原）。"""
    old = V2.get(key)
    V2[key] = value
    return old


# 注入 A：元引用过滤**完全失效** → 假警报回来
_old = _inject("_is_meta_reference", lambda s, a, b: False)
try:
    n = len(unresolved_in("「修法（还没做）」这类未完成声明"))
    check(n == 1, "【注入A】去掉元引用过滤 → 假警报复现 → 【2】的用例会红", f"got {n}")
finally:
    V2["_is_meta_reference"] = _old

# 注入 B：元引用过滤**过度放宽**（一切皆元引用）→ 真待办漏报
_old = _inject("_is_meta_reference", lambda s, a, b: True)
try:
    n = len(unresolved_in(E65_PENDING))
    check(n == 0, "【注入B】过滤放宽到一切皆元引用 → 真待办漏报 → 【1】的用例会红", f"got {n}")
finally:
    V2["_is_meta_reference"] = _old

# 注入 C：DONE_PAT 装死（销账判定失效）→ 已落地的又会被报出来
_old = _inject("DONE_PAT", re.compile(r"永远不会匹配的字符串xyzzy"))
try:
    n = len(unresolved_in(RESOLVED))
    check(n == 1, "【注入C】销账判定失效 → 已落地的又被报出来 → 【3】的用例会红", f"got {n}")
finally:
    V2["DONE_PAT"] = _old

# 注入 D：PENDING_PAT 装死（什么都扫不到）→ 断言完全装死
_old = _inject("PENDING_PAT", re.compile(r"永远不会匹配的字符串xyzzy"))
try:
    n = len(unresolved_in(E65_PENDING))
    check(n == 0, "【注入D】判据装死 → 什么都扫不到 → 【1】的用例会红", f"got {n}")
finally:
    V2["PENDING_PAT"] = _old

# 注入 E：FALSE_POS_PAT 失效 → 描述性用法被误报
#
# ⚠️ 这里必须**同时**把 PENDING_PAT 放宽成「能匹配『未落地』」——
#   因为当前的 PENDING_PAT（`待落地`/`尚未落地`…）**根本匹配不到**「未落地」，
#   FALSE_POS_PAT 里那些条目现在是**前瞻性护栏**（防将来有人把模式放宽）。
#   只注入 FALSE_POS_PAT 而不放宽 PENDING_PAT → 用例测不到任何东西（空转）。
_broad_pat = re.compile(r'修法（?还没做）?|尚未修改|待落地（?(?:未实现)?）?|尚未补|还没改|尚未落地|未落地（?(?:未实现|需定夺|本来)?）?')
_old_pp = _inject("PENDING_PAT", _broad_pat)
_old_fp = _inject("FALSE_POS_PAT", re.compile(r"永远不会匹配的字符串xyzzy"))
try:
    n = len(unresolved_in("这个机制从未落地。"))
    check(n >= 1, "【注入E】描述性误伤过滤失效 → 「从未落地」被误报 → 断言会红", f"got {n}")
finally:
    V2["PENDING_PAT"] = _old_pp
    V2["FALSE_POS_PAT"] = _old_fp

# 注入 E2：**只**放宽 PENDING_PAT（FALSE_POS_PAT 完好）→ 描述性用法仍应被排除
_old_pp = _inject("PENDING_PAT", _broad_pat)
try:
    n = len(unresolved_in("这个机制从未落地。"))
    check(n == 0, "【注入E2】模式放宽但误伤过滤完好 → 「从未落地」仍被排除", f"got {n}")
finally:
    V2["PENDING_PAT"] = _old_pp

# 【4-SELFTEST】注入机制本身的自检 ——
# ⚠️ 这一条是本组最重要的用例：
#   它验证「**注入确实生效了**」。
#   没有它，上面 5 条注入可能全部是**空转**（测试全绿、什么都没测），
#   而那正是「断言装死」的典型形态。
print("\n【4-SELFTEST】注入机制自检（防「注入空转」）")

# ⚠️⚠️ **先取基线，再注入** ——
#   第一版把 before/after 都写在注入**之后** → 两个值当然相同 → 用例恒红。
#   这正是「测试写错了」而不是「判据写错了」——
#   → 教训（与 2026-10-28 的那次一样）：
#     **一个断言「看着在测 A、实际测的是 B」的失败，先怀疑测试数据本身。**
_baseline = len(unresolved_in(E65_PENDING))

_old = _inject("_is_meta_reference", lambda s, a, b: True)
try:
    after = len(unresolved_in(E65_PENDING))
    check(_baseline != after,
          "注入「一切皆元引用」后，真待办的条数**必须变化**（否则注入空转）",
          f"baseline={_baseline} after={after}")
finally:
    V2["_is_meta_reference"] = _old

# 还原后必须恢复原状
check(len(unresolved_in(E65_PENDING)) == _baseline,
      "注入还原后，真待办必须重新被报出来（还原逻辑没坏）",
      f"baseline={_baseline} now={len(unresolved_in(E65_PENDING))}")


print("\n" + "=" * 60)
print(f" 结果: PASS={PASS} FAIL={FAIL}")
print("=" * 60)
if FAIL:
    sys.exit(1)
