#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【A2-6 / P3】判据真身 —— **时间戳基准一致性**自检（E85 的落地侧）。

2026-11-24 加（E85）。

## 为什么需要它

E85 诊断出：`memory/quota-history.jsonl` 里**混着两种时间戳** ——

- 实时记录（`record_quota_probe.py`）→ **UTC 带 `Z`**
- 回填记录（`backfill_quota_history.py`）→ **本地时间不带 `Z`**（照抄 journald）

而消费方按 `ts[:10]` 归日 —— 等于把**两套日历基准当同一套用**。

**为什么难发现**：
1. 写入方各自看都正确；
2. `ts[:10]` 对**任何单一基准**都成立；
3. 只有**两种基准混在同一个文件**时才出错；
4. **不报错** —— 格式都合法、日期都能解析，只有「按天聚合」这层悄悄算错。

→ 所以需要一条**专门扫元信息**的断言（E82 同类：
**别只检查数据本身，要检查数据的元信息**）。

## 本判据回答什么

> **同一个文件里，所有 `ts` 是不是同一个时区基准？**

- 全部带 `Z` → ✅ PASS（统一 UTC）
- 全部不带 `Z` → ⚠️ WARN（裸时间戳**有歧义**，但至少内部自洽）
- **两种都有** → ❌ FAIL（基准不统一 → 归日统计会算错）

## 顺带扫的文件

- `memory/quota-history.jsonl` —— 主目标
- `memory/gate-history.jsonl` —— 闸门历史（同一个坑的孪生兄弟）

## 用法

    python3 scripts/check_ts_basis.py             # 判据
    python3 scripts/check_ts_basis.py --self-test # 自检
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from quota_ts import basis_mismatch  # noqa: E402

WS = Path("/root/.openclaw/workspace")
TARGETS = [
    ("额度探测历史", WS / "memory" / "quota-history.jsonl"),
    ("闸门历史", WS / "memory" / "gate-history.jsonl"),
]


def check_target(name: str, path: Path):
    """单个文件的基准一致性 → (level, msg)

    level: "PASS" | "WARN" | "FAIL" | "SKIP"
    """
    if not path.exists():
        return "SKIP", f"{name}: 文件不存在（跳过）"
    info = basis_mismatch(path)
    total = info["total"]
    if total == 0:
        return "SKIP", f"{name}: 无有效记录（跳过）"
    if info["mixed"]:
        return "FAIL", (
            f"{name}: 时间戳基准不统一 —— {info['with_z']} 行带 UTC `Z`、"
            f"{info['no_z']} 行不带时区标记（裸串有歧义）"
            f"；同一文件混用两套日历基准，按天聚合会算错（E85）"
            f"；样例 no-Z: {', '.join(info['samples'][:3])}"
            f" → 修法: python3 scripts/quota_ts.py {path.name}"
        )
    if info["no_z"] and info["with_z"] == 0:
        return "WARN", (
            f"{name}: {total} 行**全部不带时区标记** —— 裸时间串有歧义"
            f"（E85：格式的歧义比格式的缺失更危险）；"
            f"修法: python3 scripts/quota_ts.py {path.name}"
        )
    return "PASS", f"{name}: {total} 行基准统一（全部 UTC `Z`）"


def _self_test():
    import tempfile

    fails = []
    passed = 0

    def check(name, cond, detail=""):
        nonlocal passed
        if cond:
            passed += 1
        else:
            fails.append(f"{name} → {detail}")

    tmpdir = Path(tempfile.mkdtemp())

    # 用例 1：全带 Z → PASS
    p = tmpdir / "a.jsonl"
    p.write_text(
        '{"ts": "2026-11-24T01:00:00Z", "state": "ok"}\n'
        '{"ts": "2026-11-23T01:00:00Z", "state": "ok"}\n',
        encoding="utf-8",
    )
    lvl, _ = check_target("t", p)
    check("用例1 全Z=PASS", lvl == "PASS", f"level={lvl}")

    # 用例 2：混合 → FAIL
    p = tmpdir / "b.jsonl"
    p.write_text(
        '{"ts": "2026-11-24T01:00:00Z", "state": "ok"}\n'
        '{"ts": "2026-11-23T01:00:00", "state": "ok"}\n',
        encoding="utf-8",
    )
    lvl, msg = check_target("t", p)
    check("用例2 混合=FAIL", lvl == "FAIL", f"level={lvl}")
    check("用例2 消息含样本", "no-Z" in msg or "样例" in msg, f"msg={msg[:80]}")

    # 用例 3：全裸串 → WARN（自洽但有歧义）
    p = tmpdir / "c.jsonl"
    p.write_text(
        '{"ts": "2026-11-24T01:00:00", "state": "ok"}\n'
        '{"ts": "2026-11-23T01:00:00", "state": "ok"}\n',
        encoding="utf-8",
    )
    lvl, _ = check_target("t", p)
    check("用例3 全裸=WARN", lvl == "WARN", f"level={lvl}")

    # 用例 4：文件不存在 → SKIP
    lvl, _ = check_target("t", tmpdir / "nope.jsonl")
    check("用例4 不存在=SKIP", lvl == "SKIP", f"level={lvl}")

    # 用例 5：空文件 → SKIP
    p = tmpdir / "d.jsonl"
    p.write_text("", encoding="utf-8")
    lvl, _ = check_target("t", p)
    check("用例5 空文件=SKIP", lvl == "SKIP", f"level={lvl}")

    # 用例 6：坏 JSON 行被跳过，不影响判定
    p = tmpdir / "e.jsonl"
    p.write_text(
        '{"ts": "2026-11-24T01:00:00Z", "state": "ok"}\n'
        "not json at all\n"
        '{"ts": "2026-11-23T01:00:00", "state": "ok"}\n',
        encoding="utf-8",
    )
    lvl, _ = check_target("t", p)
    check("用例6 坏行被跳过仍=FAIL", lvl == "FAIL", f"level={lvl}")

    # 用例 7：**故障注入自检** —— 判据不能是「永远 PASS」
    #   把混合文件全改成带 Z → 必须从 FAIL 变 PASS（证明它真的在看内容）
    p = tmpdir / "f.jsonl"
    p.write_text(
        '{"ts": "2026-11-24T01:00:00Z", "state": "ok"}\n'
        '{"ts": "2026-11-23T01:00:00", "state": "ok"}\n',
        encoding="utf-8",
    )
    lvl_before, _ = check_target("t", p)
    p.write_text(
        '{"ts": "2026-11-24T01:00:00Z", "state": "ok"}\n'
        '{"ts": "2026-11-23T01:00:00Z", "state": "ok"}\n',
        encoding="utf-8",
    )
    lvl_after, _ = check_target("t", p)
    check("用例7 故障注入(FAIL→PASS)", lvl_before == "FAIL" and lvl_after == "PASS",
          f"before={lvl_before} after={lvl_after}")

    print(f"__SELF_TEST_PASSED__={passed}")
    print(f"__SELF_TEST_FAILED__={len(fails)}")
    for f in fails:
        print(f"FAIL {f}")
    return 0 if not fails else 1


def main():
    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    for name, path in TARGETS:
        lvl, msg = check_target(name, path)
        print(f"{lvl} {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
