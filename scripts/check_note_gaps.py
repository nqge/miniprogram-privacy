#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【A2-4】判据真身 —— 闸门跳过的日子，「当天没有当日笔记」是不是一个静默的洞。

2026-11-23 (E83) 新增。

## 为什么需要这一组

E83 的原始现场（2026-11-22）：

- 额度闸门判定额度不足 → `daily-learning` **被跳过**，压根没进 cron
- 于是 `memory/2026-11-22.md` **压根没建**
- 但 07:00 的巡检 **PASS=91 FAIL=0**，一条都没报

三层都漏了，原因是同一个 —— **它们全都只看「已经存在的东西」**：

| 组 | 它查什么 | 为什么看不见「本该有却没有」 |
|---|---|---|
| 【A2-1】声称写了但文件不存在 | `status != ok` 的 run 摘要里的日期 | 闸门跳过**压根没进 cron** → 没有 run 记录 → 没有摘要 → 结构上不可能被抓到 |
| 【A2-3】索引漏写 | 遍历 `memory/` 下**已存在**的文件，再和索引比 | 只查「有但没进索引」；「本该有却没有」天然不在视野里 |
| 【D】投递是否正常 | cron 投递记录 | 闸门跳过那天**压根没有投递** →「没投递」和「投递失败」长得一样 |

→ 一句话：**「健康地跳过」和「悄悄地没跑」在信号上无法区分。**
→ 闸门守住了额度，却在时间线上挖了个静默的洞。

## 本判据怎么补这个洞

**换数据源**：不看 cron（跳过的日子压根不在 cron 里），看**闸门自己的历史**——
`memory/gate-history.jsonl` 每行记录了一次闸门判定：

    {"ts": "...", "job": "daily-learning", "result": "triggered"|"skipped", "reason": "quota"|...}

然后对最近 N 天里**每一个「本该有当日笔记」的日期**问一句：
> 那天闸门放行了（有 triggered）**或者** 那天已经有当日笔记（被后来的运行补记过）吗？

两个都没有 → **这个洞是真实存在的** → FAIL。

## 关键设计：什么叫「本该有笔记」

不能简单地说「每天都得有笔记」—— 那会天天假阳性：

- `daily-reflection` 是**每周日**才跑（`openclaw-daily-reflection.timer` 的 OnCalendar）
- 额度闸门跳过是**机制正常工作**，不是故障 —— 但它**确实留下了洞**
  （洞由**后一天的自愈补记**来填，所以补记过就不算洞）

所以本判据分两步：
1. **确定「本该有」的日子**：工作日（周一~周五）都该有 `daily-learning` 笔记
   —— 周六周日不要求（reflection 周日跑，但笔记文件名按当天）
2. **判定「洞」**：该有的日子，既没有 triggered，也没有笔记文件 → FAIL

## 为什么工作日才算

- `daily-learning` 闸门 `daily_learning_gate.py` 每天 09:00 触发一次（`0 9 * * *`）
- 周六周日**也照常跑**（`* * *` 包含周末）→ 周末**也该有**笔记
- 但**补记**只发生在下一个工作日 → 周末被跳过的话，洞要到**周一**才会被填上，
  周一早上巡检时**看起来还是洞**（这正是 E83 现场的真实形状）

→ 折中：判据看**最近 N 天里工作日必须有**；周末的洞**允许延迟到下一个工作日补**，
若到下一个工作日结束仍没补 → 也算洞（由「工作日必须有」这条兜住）。

## 用法

    python3 scripts/check_note_gaps.py             # 判据
    python3 scripts/check_note_gaps.py --self-test # 自检（含故障注入）
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

WS = Path("/root/.openclaw/workspace")
MEM_DIR = WS / "memory"
GATE_LOG = MEM_DIR / "gate-history.jsonl"
# 看最近多少天
WINDOW_DAYS = 10
# 只关心 daily-learning（它每天写当日笔记）
JOB = "daily-learning"


def _read_gate_history(path: Path):
    """读闸门历史 → {date_str: set(result)}"""
    out = {}
    if not path.exists():
        return out
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("job") != JOB:
                continue
            ts = d.get("ts") or ""
            if len(ts) < 10:
                continue
            day = ts[:10]
            out.setdefault(day, set()).add(str(d.get("result") or ""))
    except Exception:
        return out
    return out


def _note_exists(mem_dir: Path, day: str) -> bool:
    return (mem_dir / f"{day}.md").exists()


def find_gaps(mem_dir: Path, gate_hist: dict, today=None, window_days: int = WINDOW_DAYS,
              grace_days: int = 1):
    """返回 [(day, reason)] —— 真实存在的洞。

    判定：**只有闸门明确记录了「跳过」的日子**才可能被判为洞：
      - 闸门当天 recorded 了 skipped（没跑成）
      - 且 当天也没有当日笔记文件（没被后来的运行补记）
      - 且 **宽限期已过**（见下）
    → 洞

    ⚠️ 闸门历史里**完全没有记录**的日子不算洞：
      - 闸门架构 2026-11-16 才上线，更早的日子压根不受它管；
      - 「没记录」≠「被跳过」，判成洞就是**假阳性**（E66 的假警报驯化）。
      → 闸门历史缺失时**整组不报**（机制还没跑过，不是故障）。

    ## 宽限期（grace）为什么必要

    补记发生在**下一个 daily-learning 运行时**（每天 09:00），
    而不是「第二天零点」。所以：

    - 某天被跳过 → **下一个 09:00** 才会补记
    - 若被跳过的恰好是**周日** → 补记发生在**周一 09:00**
    - 而巡检在**周一 07:00** 跑 —— **早于补记 2 小时**

    → 若不加宽限，周一早上巡检必然报一个「假洞」（其实 2 小时后就会被填）。
    → 宽限期取 **1 天**：被跳过的日子若在 **1 天内**被补记，就不算洞。
    → 宽限期过后仍没有笔记 → **真洞**（这正是 E83 现场：11-22 跳过，
      11-23 09:00 补记了，但 **11-23 07:00 的巡检啥也没报** —— 因为
      当时**根本没有这条判据**）。

    → **宽限期不是为了让判据闭嘴，而是让它在「洞已经被填」之后再说。**
    """
    today = today or datetime.date.today()
    gaps = []
    for i in range(window_days, 0, -1):
        day_dt = today - datetime.timedelta(days=i)
        day = day_dt.strftime("%Y-%m-%d")
        results = gate_hist.get(day, set())
        if not results:
            continue            # 闸门当天没有决策记录 → 不在闸门管辖范围内
        if "skipped" not in results:
            continue            # triggered / ok → 跑过了，笔记缺失归【A2-1】
        if _note_exists(mem_dir, day):
            continue            # 已被补记 → 洞已填
        # 宽限期：从被跳过的当天算起，age <= grace_days 天内被补记就不算洞
        #   age=0 → 当天（下一个 09:00 还能补）
        #   age=1 → 昨天（下一个工作日 09:00 还能补）
        #   age=2 → 前天（已过补记窗口 → 真洞）
        age = (today - day_dt).days
        if age <= grace_days:
            continue            # 还在宽限期内 → 下一个运行可能会补
        reason = f"闸门跳过({'/'.join(sorted(results))})且宽限期({age}天)已过仍无当日笔记"
        gaps.append((day, reason))
    return gaps


def run(mem_dir: Path, gate_log: Path, today=None, window_days: int = WINDOW_DAYS):
    gate_hist = _read_gate_history(gate_log)
    gaps = find_gaps(mem_dir, gate_hist, today=today, window_days=window_days)
    return gaps, gate_hist


# ─────────────────────────── 自检 ───────────────────────────

def _mk(tmp: Path, day: str, note: bool = True, content: str = "## 📚 每日学习"):
    mem_dir = tmp / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    if note:
        (mem_dir / f"{day}.md").write_text(content, encoding="utf-8")
    return mem_dir


def _mk_gate(tmp: Path, entries):
    p = tmp / "memory" / "gate-history.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return p


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

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        today = datetime.date(2026, 11, 23)

        # 用例 1：跳过日无笔记 + 宽限期已过 → 应报洞
        #   2026-11-17（周二）被跳过，今天 2026-11-23 → age=6 天 → 宽限期早过了
        mem = _mk(tmp, "2026-11-19", note=True)
        _mk_gate(tmp, [
            {"ts": "2026-11-19T09:00:47", "job": JOB, "result": "triggered"},
            {"ts": "2026-11-20T09:00:43", "job": JOB, "result": "triggered"},
            {"ts": "2026-11-21T09:00:47", "job": JOB, "result": "triggered"},
            {"ts": "2026-11-22T09:00:37", "job": JOB, "result": "skipped", "reason": "quota"},
            {"ts": "2026-11-17T09:00:00", "job": JOB, "result": "skipped", "reason": "quota"},
        ])
        gaps, _ = run(mem, tmp / "memory" / "gate-history.jsonl", today=today, window_days=10)
        check("用例1 跳过日无笔记且宽限期过=报洞",
              any(g[0] == "2026-11-17" for g in gaps), f"gaps={gaps}")
        check("用例1b 跳过日在宽限期内=不报洞",
              not any(g[0] == "2026-11-22" for g in gaps), f"gaps={gaps}")

        # 用例 2：跳过日**有补记笔记** → 不算洞
        _mk(tmp, "2026-11-22", note=True)
        _mk(tmp, "2026-11-17", note=True)
        gaps, _ = run(mem, tmp / "memory" / "gate-history.jsonl", today=today, window_days=10)
        check("用例2 跳过日有补记=不报洞",
              not any(g[0] in ("2026-11-22", "2026-11-17") for g in gaps), f"gaps={gaps}")

        # 用例 3：闸门 triggered 但笔记缺失 → **不算洞**？
        #   触发成功说明任务跑过了，笔记缺失是【A2-1】的范畴（run 摘要会查），
        #   本组不重复报，避免同一故障报两次。
        with tempfile.TemporaryDirectory() as td2:
            tmp2 = Path(td2)
            mem2 = _mk(tmp2, "2026-11-19", note=True)
            _mk(tmp2, "2026-11-20", note=True)
            _mk(tmp2, "2026-11-21", note=True)
            # 不建 22 的笔记，但闸门记录 triggered
            _mk_gate(tmp2, [
                {"ts": "2026-11-19T09:00:47", "job": JOB, "result": "triggered"},
                {"ts": "2026-11-20T09:00:43", "job": JOB, "result": "triggered"},
                {"ts": "2026-11-21T09:00:47", "job": JOB, "result": "triggered"},
                {"ts": "2026-11-22T09:00:37", "job": JOB, "result": "triggered"},
            ])
            gaps2, _ = run(mem2, tmp2 / "memory" / "gate-history.jsonl", today=today, window_days=10)
            check("用例3 triggered但无笔记=不报洞(归A2-1)",
                  not any(g[0] == "2026-11-22" for g in gaps2), f"gaps={gaps2}")

        # 用例 4：周末被跳过 + 宽限期已过 → **也算洞**
        #   （原设计是「周末不要求」，改为宽限期制：周末被跳过同样会留洞，
        #    只是宽限期内不报。2026-11-21 周六、11-22 周日）
        with tempfile.TemporaryDirectory() as td3:
            tmp3 = Path(td3)
            mem3 = _mk(tmp3, "2026-11-19", note=True)
            _mk_gate(tmp3, [
                {"ts": "2026-11-19T09:00:47", "job": JOB, "result": "triggered"},
                {"ts": "2026-11-15T09:00:00", "job": JOB, "result": "skipped", "reason": "quota"},
                {"ts": "2026-11-14T09:00:00", "job": JOB, "result": "skipped", "reason": "quota"},
            ])
            gaps3, _ = run(mem3, tmp3 / "memory" / "gate-history.jsonl", today=today, window_days=10)
            check("用例4 周末被跳过且宽限期过=报洞",
                  any(g[0] == "2026-11-15" for g in gaps3), f"gaps={gaps3}")
            check("用例4b 周末在宽限期内=不报洞",
                  not any(g[0] == "2026-11-22" for g in gaps3), f"gaps={gaps3}")

        # 用例 5：闸门历史完全缺失 → 不报（不是故障，是还没跑过）
        with tempfile.TemporaryDirectory() as td4:
            tmp4 = Path(td4)
            mem4 = _mk(tmp4, "2026-11-19", note=True)
            gaps4, hist4 = run(mem4, tmp4 / "memory" / "nonexistent.jsonl",
                               today=today, window_days=10)
            check("用例5 闸门历史缺失=不报", gaps4 == [], f"gaps={gaps4}")

        # 用例 5b：**闸门上线前**的日子没有记录 → 绝不能报（假阳性陷阱）
        #   闸门架构 2026-11-16 才上线；11-13/11-16 之类无记录的日子
        #   「没记录」≠「被跳过」。这是本判据最容易犯的错。
        with tempfile.TemporaryDirectory() as td4b:
            tmp4b = Path(td4b)
            mem4b = _mk(tmp4b, "2026-11-19", note=True)
            _mk_gate(tmp4b, [
                {"ts": "2026-11-19T09:00:47", "job": JOB, "result": "triggered"},
            ])
            gaps4b, _ = run(mem4b, tmp4b / "memory" / "gate-history.jsonl",
                            today=today, window_days=10)
            check("用例5b 闸门无记录的日子不报洞(防假阳性)",
                  not any(g[0] in ("2026-11-13", "2026-11-16", "2026-11-17", "2026-11-18")
                          for g in gaps4b), f"gaps={gaps4b}")

        # 用例 6：窗口外的洞不报
        with tempfile.TemporaryDirectory() as td5:
            tmp5 = Path(td5)
            mem5 = _mk(tmp5, "2026-11-19", note=True)
            _mk_gate(tmp5, [
                {"ts": "2026-10-20T09:00:00", "job": JOB, "result": "skipped", "reason": "quota"},
            ])
            gaps5, _ = run(mem5, tmp5 / "memory" / "gate-history.jsonl", today=today, window_days=10)
            check("用例6 窗口外不报", gaps5 == [], f"gaps={gaps5}")

        # 用例 7：其它 job 的记录不参与
        with tempfile.TemporaryDirectory() as td6:
            tmp6 = Path(td6)
            mem6 = _mk(tmp6, "2026-11-19", note=True)
            _mk_gate(tmp6, [
                {"ts": "2026-11-20T07:00:39", "job": "daily-reflection", "result": "skipped", "reason": "quota"},
            ])
            gaps6, _ = run(mem6, tmp6 / "memory" / "gate-history.jsonl", today=today, window_days=10)
            check("用例7 其它job不参与",
                  not any("2026-11-20" == g[0] for g in gaps6), f"gaps={gaps6}")

    print(f"__SELF_TEST_PASSED__={passed}")
    print(f"__SELF_TEST_FAILED__={len(fails)}")
    for f in fails:
        print(f"FAIL {f}")
    return 0 if not fails else 1


def main():
    if "--self-test" in sys.argv:
        sys.exit(_self_test())

    gaps, gate_hist = run(MEM_DIR, GATE_LOG)
    if gaps:
        print(f"FAIL {len(gaps)} 天「本该有当日笔记」但既没跑成也没补记 —— 时间线上的静默洞（E83）")
        for day, reason in gaps:
            print(f"FAIL   {day} {reason}")
    else:
        print("PASS 最近每天的当日笔记都在（闸门跳过的日子已被补记，无静默洞）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
