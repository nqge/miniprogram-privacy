#!/usr/bin/env python3
"""add_l1_entry.py 的单测（E61）。

## 为什么需要它

这个脚本**没有单测**，而它干的活是「把一条经验写进两个地方（独立文件 + 主库）」——
一旦它的替换逻辑和它写入的结构不一致，**一条会变成三条**，而且**主库是渲染源**，
错的内容会被下一个会话直接继承。

2026-11-04 实测到 3 个真实缺陷（E55/E56/E57/E59/E60 主库出现重复「本条讲」行），
根因有两个，都在这里：

### 缺陷 A：`sync_main_lib` 无条件注入「本条讲」行

```python
block = f"{heading}\n\n> 本条讲：**{title}**\n\n{body}\\n"
```

但 `body`（来自独立文件）**本来就带自己的「本条讲」行** →
主库条目变成「注入一行 + 作者一行」= 2 行。

### 缺陷 B：正文剥离正则只剥第一行「本条讲」

```python
body = re.sub(rf"^###\\s*{eid}[：:].*$\\n+\\s*^> 本条讲：\\*\\*.+?\\*\\*\\s*$", "", text, ...)
```

`re.sub` 不带 count → 但这个模式**锚定在标题行之后**，所以只能命中标题后**紧邻**的那一行。
作者若在文件里写了第二行（E60 就是这样），第二行会**残留进 body**，
再叠加缺陷 A → 主库 3 行。

## 判据真身

本文件**直接 import 被测模块**，测它真实的函数 ——
不抄一份判据（抄版会悄悄漂移，2026-10-27 就因此踩过：
抄版写 `if not job: job = ...`，真版改成了两行写法）。

跑法：

    python3 scripts/test_add_l1_entry.py
"""

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "add_l1_entry.py"

spec = importlib.util.spec_from_file_location("add_l1_entry", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

PASS, FAIL = 0, 0
FAILURES = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"✅ {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"❌ {name}" + (f" — {detail}" if detail else ""))


# ─────────────────────────────────────────────────────────────
# 缺陷 B：正文剥离必须剥掉**所有**「本条讲」行
# ─────────────────────────────────────────────────────────────
def test_body_strips_all_kw_lines():
    text = (
        "### E99：测试标题\n"
        "\n"
        "> 本条讲：**关键词一**\n"
        "\n"
        "> 本条讲：**关键词二**\n"
        "\n"
        "## 场景\n"
        "正文内容。\n"
    )
    body, kw = mod.peel_body(text, "E99")
    check(
        "缺陷B：正文剥离剥掉所有「本条讲」行",
        "本条讲" not in body,
        f"body 里仍残留: {body!r}",
    )
    check(
        "缺陷B：正文剥离保留真正正文",
        "正文内容" in body and "## 场景" in body,
        f"body 丢了正文: {body!r}",
    )
    check(
        "缺陷B：正文剥离不残留标题行",
        not re.search(r"^###\s*E99[：:]", body, re.M),
        f"body 里仍残留标题行: {body!r}",
    )
    check(
        "缺陷B：剥下来的关键词要传下去（作者原话不能丢）",
        kw == "关键词一",
        f"kw={kw!r}",
    )


# ─────────────────────────────────────────────────────────────
# 缺陷 A：主库条目只能有**一行**「本条讲」
# ─────────────────────────────────────────────────────────────
def test_main_lib_block_single_kw_line():
    body = "> 本条讲：**作者的关键词**\n\n## 场景\n正文。"
    block = mod.build_block("E99", "测试标题", body)
    n = len(re.findall(r"^> 本条讲：\*\*", block, flags=re.M))
    check(
        "缺陷A：主库块只有 1 行「本条讲」",
        n == 1,
        f"实际 {n} 行: {block!r}",
    )
    check(
        "缺陷A：主库块保留作者的关键词（不被标题覆盖）",
        "作者的关键词" in block,
        f"作者关键词丢了: {block!r}",
    )


def test_main_lib_block_no_body_kw_line():
    """body 里没有「本条讲」行时，应该注入一行（不能出现 0 行）。"""
    body = "## 场景\n只有正文。"
    block = mod.build_block("E99", "测试标题", body)
    n = len(re.findall(r"^> 本条讲：\*\*", block, flags=re.M))
    check(
        "缺陷A：无 body 关键词时注入 1 行",
        n == 1,
        f"实际 {n} 行: {block!r}",
    )


def test_main_lib_block_keeps_author_keyword():
    """关键词被 peel_body 剥下来后，必须原样传回 build_block —— 不能退回标题。"""
    text = "### E99：标题文字\n\n> 本条讲：**作者原话**\n\n正文。"
    body, kw = mod.peel_body(text, "E99")
    block = mod.build_block("E99", "标题文字", body, kw)
    check(
        "作者原话优先于标题",
        "作者原话" in block and "> 本条讲：**标题文字**" not in block,
        f"block={block!r}",
    )
    n = len(re.findall(r"^> 本条讲：\*\*", block, flags=re.M))
    check("带 keyword 参数时仍只有 1 行", n == 1, f"实际 {n} 行")


def test_main_lib_block_idempotent():
    """同一份 body 反复组装，结果必须一致（幂等）。"""
    body = "> 本条讲：**K**\n\n正文。"
    b1 = mod.build_block("E99", "T", body)
    b2 = mod.build_block("E99", "T", body)
    check("幂等：重复组装结果一致", b1 == b2)


# ─────────────────────────────────────────────────────────────
# 回归：现有 60 条主库条目必须都是「恰好 1 行」
# ─────────────────────────────────────────────────────────────
def test_existing_library_clean():
    lib = ROOT / "memory" / "L1-experience-library.md"
    if not lib.exists():
        check("回归：主库存在", False, f"找不到 {lib}")
        return
    t = lib.read_text(encoding="utf-8")
    blocks = re.split(r"(?=^### E\d+[：:])", t, flags=re.M)
    bad = []
    n_entries = 0
    for b in blocks:
        m = re.match(r"### (E\d+)[：:]", b)
        if not m:
            continue
        n_entries += 1
        n = len(re.findall(r"^> 本条讲：\*\*", b, flags=re.M))
        if n != 1:
            bad.append((m.group(1), n))
    check(
        "回归：主库每个条目恰好 1 行「本条讲」",
        not bad,
        f"异常条目: {bad}",
    )
    # 条目数不该写死 —— 写死就会在新增条目后**假失败**（装死的一种）
    ents = ROOT / "memory" / "L1-entries"
    n_files = len(list(ents.glob("E*.md")))
    check(
        "回归：主库条目数与独立文件数一致",
        n_entries == n_files,
        f"主库 {n_entries} 条 vs 独立文件 {n_files} 份",
    )


def test_entry_files_single_kw_line():
    ents = ROOT / "memory" / "L1-entries"
    bad = []
    for p in sorted(ents.glob("E*.md")):
        n = len(re.findall(r"^> 本条讲：\*\*", p.read_text(encoding="utf-8"), flags=re.M))
        if n != 1:
            bad.append((p.stem, n))
    check(
        "回归：独立文件每个恰好 1 行「本条讲」",
        not bad,
        f"异常文件: {bad}",
    )


# ─────────────────────────────────────────────────────────────
# 判据自身不能装死（防止断言永远通过）
# ─────────────────────────────────────────────────────────────
def test_assertion_is_not_dead():
    """确认断言**能抓坏输入** —— 否则上面的 PASS 都可能是假象。

    做法：用**故意写坏的输入**跑一遍断言，断言必须报出「抓到了」。
    这里不走被测模块的修复后路径，而是直接验断言的表达能力：
    统计行数的表达式本身要能把 2 行判成「不合格」。
    """
    # 构造：同一块里有 2 行「本条讲」→ 用断言同样的表达式数行 → 必须能数出 2
    bad_block = "### E99：T\n\n> 本条讲：**A**\n\n> 本条讲：**B**\n\n正文。"
    n_bad = len(re.findall(r"^> 本条讲：\*\*", bad_block, flags=re.M))
    check(
        "判据自检：断言能数出重复行（2≠1 → 必须判不合格）",
        n_bad == 2 and n_bad != 1,
        f"实际数出 {n_bad} 行 —— 断言表达式装死？",
    )

    # 反向：修复后的路径对**同一个坏输入**必须判合格
    fixed, _kw = mod.peel_body(bad_block, "E99")
    n_fixed = len(re.findall(r"^> 本条讲：\*\*", fixed, flags=re.M))
    check(
        "判据自检：修复路径对同一输入判合格",
        n_fixed == 0,
        f"修复后仍残留 {n_fixed} 行",
    )

    # 组装路径：body 有 2 行时，结果必须**仍只有 1 行**（不能把作者的 2 行一起搬进主库）
    block = mod.build_block("E99", "T", "> 本条讲：**A**\n\n> 本条讲：**B**\n\n正文。")
    n_block = len(re.findall(r"^> 本条讲：\*\*", block, flags=re.M))
    check(
        "判据自检：组装路径对重复 body 仍只有 1 行",
        n_block == 1,
        f"实际 {n_block} 行",
    )


def main():
    print("=" * 60)
    print(" add_l1_entry.py 单测（E61）")
    print("=" * 60)
    test_body_strips_all_kw_lines()
    test_main_lib_block_single_kw_line()
    test_main_lib_block_no_body_kw_line()
    test_main_lib_block_idempotent()
    test_existing_library_clean()
    test_entry_files_single_kw_line()
    test_assertion_is_not_dead()
    print("=" * 60)
    print(f" 结果: PASS={PASS} FAIL={FAIL}")
    if FAILURES:
        print(f" 失败项: {FAILURES}")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
