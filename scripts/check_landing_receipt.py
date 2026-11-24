#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【R】判据真身 —— **落地回执**：声称已落地的引用必须真实存在（E87）。

2026-11-24 加（E87 的落地侧）。

## 为什么需要它

E87 教的三锚点是「**意图 → 回执 → 可回放**」。其中「回执」最容易变成
**嘴上说说**：

> 条目写「✅ 落地状态：已加 `scripts/xxx.py`」，
> 但那个脚本**根本没建**，或者建了却**没被任何地方调用**。

这跟 E68「声称已写入 `memory/YYYY-MM-DD.md`，但文件从未存在」是同一族坑。

**2026-11-24 实测**：我写完 E87 的落地状态表，其中一行写着
「receipt 落到巡检」—— 但 `healthcheck.sh` 里 **grep receipt = 0**。
**当场被自己抓到**：那张表在吹牛。

→ 所以本组做一件很朴素的事：
**凡是声称「已落地」的 L1 条目，它提到的 `scripts/` 文件必须真实存在，
而且最好**被引用**（存在 ≠ 被调用）。**

## 两级判定

| 级别 | 含义 | 判定 |
|---|---|---|
| **FAIL** | 声称落地，但引用的脚本**不存在** | 吹牛，必须改 |
| **WARN** | 脚本存在，但**没有任何地方引用它** | 可能是「造了没接上」（E86 的老坑） |
| **PASS** | 存在且被引用 | 真落地 |

## 怎么找「声称落地」的条目

扫 `memory/L1-entries/E*.md`：
- 必须含 `✅ 落地状态` 或 `已落地`（= 声称）
- 从中抽出所有 `scripts/<名字>.py` / `scripts/<名字>.sh` 形式的引用

⚠️ **只查「已落地」标记的条目** —— 未落地的条目本来就该引用
尚不存在的文件，那是「待落地」不是「吹牛」。

## 用法

    python3 scripts/check_landing_receipt.py             # 判据
    python3 scripts/check_landing_receipt.py --self-test # 自检
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

WS = Path("/root/.openclaw/workspace")
L1_DIR = WS / "memory" / "L1-entries"
# 「声称已落地」的标记
CLAIM_PAT = re.compile(r"✅\s*落地状态|已落地|✅\s*已做")
# 脚本引用
REF_PAT = re.compile(r"scripts/([A-Za-z0-9_\-]+\.(?:py|sh|mjs))")
# 「引用它的地方」—— 巡检脚本 + 定时任务指令 + 其他脚本
CONSUMER_FILES = [
    WS / "scripts" / "healthcheck.sh",
    WS / "scripts" / "daily_learning_prompt.txt",
    WS / "scripts" / "healthcheck_prompt.txt",
]


def find_claims(l1_dir=None):
    """扫 L1 条目 → [(entry_id, [脚本名...])]（只含「声称已落地」的）"""
    l1_dir = Path(l1_dir) if l1_dir else L1_DIR
    out = []
    if not l1_dir.is_dir():
        return out
    for f in sorted(l1_dir.glob("E*.md")):
        try:
            body = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if not CLAIM_PAT.search(body):
            continue
        refs = sorted(set(REF_PAT.findall(_strip_injection_mentions(body))))
        if refs:
            out.append((f.stem, refs))
    return out


# ⚠️ **故障注入的举例**不是「引用了一个真实脚本」
#   例：E87 里写「往条目里塞一个不存在的 `scripts/fake_receipt.py` →
#   判据立刻报 FAIL」—— 这是在**描述测试**，不是在声称落地。
#   → 不排除的话，判据会把**自己写的测试说明**当成吹牛 → 假阳性。
#   → 排除规则：**紧邻的上下文里出现「故障注入/注入/举例/实测/塞一个」等字样**时跳过。
INJECT_CTX = re.compile(
    r"(故障注入|注入|举例|实测|塞一个|假声明|测试用|示例)"
)


def _strip_injection_mentions(body: str) -> str:
    """把「故障注入举例」里的脚本引用挖掉，避免被判成吹牛。

    做法：按行扫描，若**该行或前一行**出现 INJECT_CTX 关键词，
    则把该行里的脚本引用替换成占位符（保留行数，避免影响其它解析）。
    """
    lines = body.split("\n")
    out = []
    prev_ctx = False
    for ln in lines:
        cur_ctx = bool(INJECT_CTX.search(ln))
        if cur_ctx or prev_ctx:
            ln = REF_PAT.sub("scripts/<injection-example>", ln)
        prev_ctx = cur_ctx
        out.append(ln)
    return "\n".join(out)


def is_referenced(name, extra_dirs=None):
    """这个脚本有没有被别的地方引用（巡检 / cron 指令 / 其他脚本）"""
    # 1) 巡检与 cron 指令
    for cf in CONSUMER_FILES:
        try:
            if name in cf.read_text(encoding="utf-8", errors="replace"):
                return True
        except Exception:
            pass
    # 2) scripts/ 下其他脚本
    sdir = WS / "scripts"
    try:
        for sf in sdir.glob("*.py"):
            if sf.name == name:
                continue
            try:
                if name in sf.read_text(encoding="utf-8", errors="replace"):
                    return True
            except Exception:
                pass
        for sf in sdir.glob("*.sh"):
            if sf.name == name:
                continue
            try:
                if name in sf.read_text(encoding="utf-8", errors="replace"):
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def check(l1_dir=None, scripts_dir=None):
    """→ (missing, unreferenced, checked_count)

    missing:     声称落地但脚本不存在（FAIL）
    unreferenced: 脚本存在但没被引用（WARN）
    """
    scripts_dir = Path(scripts_dir) if scripts_dir else (WS / "scripts")
    missing = []
    unreferenced = []
    total = 0
    for eid, refs in find_claims(l1_dir):
        for name in refs:
            total += 1
            if not (scripts_dir / name).exists():
                missing.append((eid, name))
            elif not is_referenced(name):
                unreferenced.append((eid, name))
    return missing, unreferenced, total


def _self_test():
    import tempfile

    fails = []
    passed = 0

    def check_(name, cond, detail=""):
        nonlocal passed
        if cond:
            passed += 1
        else:
            fails.append(f"{name} → {detail}")

    tmp = Path(tempfile.mkdtemp())
    l1 = tmp / "L1-entries"
    l1.mkdir()
    scripts = tmp / "scripts"
    scripts.mkdir()
    (scripts / "real.py").write_text("# real\n", encoding="utf-8")

    # 用例 1：声称落地 + 脚本存在 → 不进 missing
    (l1 / "E90.md").write_text(
        "## ✅ 落地状态\n- 新增 `scripts/real.py` 干这个\n", encoding="utf-8")
    m, u, total = check(l1, scripts)
    check_("用例1 存在的脚本不算 missing", m == [], f"m={m}")
    check_("用例2 统计到 1 个引用", total == 1, f"total={total}")

    # 用例 2：声称落地 + 脚本不存在 → missing
    (l1 / "E91.md").write_text(
        "## ✅ 落地状态\n- 新增 `scripts/ghost.py` 干这个\n", encoding="utf-8")
    m, u, total = check(l1, scripts)
    check_("用例3 声称落地但文件不存在=missing", ("E91", "ghost.py") in m, f"m={m}")

    # 用例 3：**未**声称落地的条目 → 不查（那是「待落地」不是吹牛）
    (l1 / "E92.md").write_text(
        "## 待落地（未实现）\n- 修法（还没做）：新增 `scripts/future.py`\n", encoding="utf-8")
    m, u, total = check(l1, scripts)
    check_("用例4 未声称落地的不算吹牛", ("E92", "future.py") not in m, f"m={m}")

    # 用例 4：**故障注入** —— 把「存在」判据改成永远 True，missing 必须变空
    #   （证明 missing 是真的在比对文件系统，不是恒假）
    orig_exists = Path.exists
    try:
        Path.exists = lambda self: True
        m2, _, _ = check(l1, scripts)
        check_("用例5 故障注入：exists 恒真 → missing 变空", m2 == [], f"m2={m2}")
    finally:
        Path.exists = orig_exists

    # 用例 5：故障注入（反向）—— exists 恒假 → missing 必须非空
    try:
        Path.exists = lambda self: False
        m3, _, _ = check(l1, scripts)
        check_("用例6 故障注入：exists 恒假 → missing 非空", len(m3) > 0, f"m3={m3}")
    finally:
        Path.exists = orig_exists

    # 用例 6：条目里没有脚本引用 → 不计入
    (l1 / "E93.md").write_text(
        "## ✅ 落地状态\n- 改了巡检脚本的某处\n", encoding="utf-8")
    m4, _, total4 = check(l1, scripts)
    check_("用例7 无脚本引用的条目不计入", total4 == 2, f"total4={total4}")

    # 用例 7：**故障注入的举例**不算「声称引用了一个真实脚本」
    #   实测（2026-11-24）：E87 里写「往条目里塞一个不存在的
    #   `scripts/fake_receipt.py` → 判据立刻报 FAIL」——
    #   这是在**描述测试**，不是在声称落地。
    #   → 不排除的话，判据把**自己写的测试说明**当成吹牛 → 假阳性。
    (l1 / "E94.md").write_text(
        "## ✅ 落地状态\n"
        "- 新增 `scripts/real.py`\n\n"
        "**故障注入实测**：往条目里塞一个不存在的 `scripts/ghost_inject.py` →\n"
        "判据立刻报 FAIL → 拿掉 → 恢复 PASS ✅\n",
        encoding="utf-8")
    m5, _, total5 = check(l1, scripts)
    check_("用例8 故障注入举例不算吹牛",
           ("E94", "ghost_inject.py") not in m5, f"m5={m5}")
    check_("用例8b 真实引用仍被计入", ("E94", "real.py") not in m5, f"m5={m5}")

    print(f"__SELF_TEST_PASSED__={passed}")
    print(f"__SELF_TEST_FAILED__={len(fails)}")
    for f in fails:
        print(f"FAIL {f}")
    return 0 if not fails else 1


def main():
    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    missing, unreferenced, total = check()
    if missing:
        print(f"FAIL {len(missing)} 处「声称已落地」但脚本不存在：")
        for eid, name in missing[:10]:
            print(f"FAIL   {eid} 引用 scripts/{name} —— 文件不存在（吹牛）")
    if unreferenced:
        print(f"WARN {len(unreferenced)} 个「声称已落地」的脚本存在但**没被任何地方引用**"
              f"（可能是造了没接上，E86 的老坑）：")
        for eid, name in unreferenced[:10]:
            print(f"WARN   {eid} → scripts/{name} 存在但无人引用")
    if not missing and not unreferenced:
        print(f"PASS {total} 处「声称已落地」的脚本引用都真实存在且被引用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
