#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""额度历史的时间戳基准 —— **单一事实来源**（E85 的落地侧）。

2026-11-24 加（E85）。

## 为什么要单独一个模块

E85 诊断出的问题：`memory/quota-history.jsonl` 里**混着两种时间戳** ——

- 实时记录（`record_quota_probe.py`）→ **UTC 带 `Z`**
- 回填记录（`backfill_quota_history.py`）→ **本地时间不带 `Z`**（照抄 journald）

而消费方（`check_quota_sustained.py`）按 `ts[:10]` 归日 ——
等于把**两套日历基准当同一套用**。

实测：7 天窗口字面算是 6 天耗尽，正确 UTC 口径是 5 天；
全部历史里 12 天有 10 天两种口径结论不同。

## 数据契约（本模块就是这条契约的实现）

> **所有写入方写 UTC + `Z`；消费方显式解析后归日，不做字面切片。**

「哪个时区」属于**数据契约**，应该在**第一个写入方**那里定死，
而不是让每个消费方各自猜 —— 猜错了不报错，只会一路算错。

## 三条硬规则

1. **写入侧**：任何要落库的时间戳，先过 `to_utc_iso()`。
   裸字符串（无时区标记）**默认按本地时间理解**（因为 journald 给的就是本地时间），
   再转成 UTC + `Z`。
2. **消费侧**：任何归日/比较，先过 `parse_ts()`，拿到带时区的 datetime，
   再 `.astimezone(tz)` 到目标时区后取日期。
3. **自检侧**：过 `basis_mismatch()`，扫描同一文件里带 `Z` 与不带 `Z` 的行数 ——
   不一致就报（E82 同类：别只检查数据本身，要检查数据的元信息）。

## 为什么消费侧不能只靠「写入侧修好」

历史数据里**已经**混着两种基准了。哪怕写入侧修好，
存量数据不带 `Z` 的那些行仍会被误判 → 所以消费侧**也要**显式解析
（这是 E85 修法里的「兜底」层）。
"""

from __future__ import annotations

import datetime
import json
import re

# 本机的目标归日时区 —— 判据的「日历」应该跟牛哥哥看到的一致
LOCAL_TZ = datetime.timezone(datetime.timedelta(hours=8))

_UTC_FMT = "%Y-%m-%dT%H:%M:%SZ"
_Z_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$")
_OFFSET_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d([+-]\d{2}:?\d{2})$")
_NAIVE_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?$")
_SPACE_RE = re.compile(r"^\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d")


def to_utc_iso(dt: datetime.datetime, naive_is_local: bool = True) -> str:
    """任意 datetime → **UTC + `Z`** 的标准字符串。

    参数
    ----
    dt:
        要转换的时间。带 tzinfo 的直接转；naive 的按 `naive_is_local` 解释
        （默认 True —— 因为 journald 的 `-o short-iso` 给的是**本地时间**）。
    naive_is_local:
        naive 时间戳按哪个时区解释。写库侧默认 True（journald 本地时间）。
        消费侧如果只是想归日，用 `parse_ts()` 更直接。

    返回
    ----
    形如 `2026-11-24T01:02:03Z` 的字符串。
    """
    if dt.tzinfo is None:
        if naive_is_local:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        else:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc).strftime(_UTC_FMT)


def parse_ts(ts: str, naive_is_local: bool = True):
    """解析任意形态的时间戳 → **带时区的** datetime（UTC）。

    支持：
    - `2026-11-24T01:02:03Z`          （UTC）
    - `2026-11-24T01:02:03+08:00`      （带偏移）
    - `2026-11-24 01:02:03`            （journald short-iso，**本地时间**）
    - `2026-11-24T01:02:03`            （裸 ISO，naive）
    - `2026-11-24T01:02:03.123456`     （带微秒）

    解析不了返回 `None`（**不抛异常** —— 消费侧要能跳过坏行而不是崩）。
    """
    if not ts:
        return None
    s = str(ts).strip()
    if not s:
        return None
    try:
        if _Z_RE.match(s):
            return datetime.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=datetime.timezone.utc
            )
        if _OFFSET_RE.match(s):
            return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if _NAIVE_RE.match(s):
            base = datetime.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
            if naive_is_local:
                base = base.replace(tzinfo=LOCAL_TZ)
            else:
                base = base.replace(tzinfo=datetime.timezone.utc)
            return base
        if _SPACE_RE.match(s):
            base = datetime.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
            if naive_is_local:
                base = base.replace(tzinfo=LOCAL_TZ)
            else:
                base = base.replace(tzinfo=datetime.timezone.utc)
            return base
    except Exception:
        return None
    # 最后兜底：交给 fromisoformat（Python 3.11+ 能吃更多格式）
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None and naive_is_local:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        return dt
    except Exception:
        return None


def to_local_date(ts: str, naive_is_local: bool = True):
    """时间戳 → **本地日历日**（`datetime.date`），解析不了返回 None。

    这是归日的**唯一正确入口** —— 不要用 `ts[:10]`。
    """
    dt = parse_ts(ts, naive_is_local=naive_is_local)
    if dt is None:
        return None
    return dt.astimezone(LOCAL_TZ).date()


def basis_mismatch(path, ts_fields=("ts",)):
    """扫描一个 JSONL 文件，统计「带 Z」与「不带 Z」的行数。

    参数
    ----
    path:
        要扫的 JSONL 文件。
    ts_fields:
        要检查的时间戳字段名（默认只查 `ts`）。
        ⚠️ **可以传多个**（如 `("ts", "ts_end")`）——
        实测 2026-11-24：`gate-history.jsonl` 的 `ts` 已修好，
        但 `ts_end` 还留着裸本地时间（虽然当前**没人读**它，
        但「没人读」不是「写对了」的理由）→ 一起扫，避免留下同样的坑。

    返回 dict：
        total    总行数（有效 JSON 行）
        with_z   带 UTC `Z` 标记的行数
        no_z     不带时区标记的行数
        mixed    两种都有 → True（**基准不统一**）
        samples  不带 Z 的行样例（最多 5 条 ts）

    E85 的第三条修法：把「元信息不一致」变成可巡检的东西。
    """
    total = with_z = no_z = 0
    samples = []
    inconsistent = 0   # **行内**基准不一致的行数（同一行的字段互相矛盾）
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {
            "total": 0, "with_z": 0, "no_z": 0, "mixed": False,
            "inconsistent": 0, "samples": [],
        }
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        # 该行**任一**被检查字段有合法时间戳 → 这行参与统计
        vals = [str(d.get(f) or "") for f in ts_fields]
        vals = [v for v in vals if len(v) >= 10]
        if not vals:
            continue
        total += 1
        has_z = any(v.endswith("Z") for v in vals)
        all_z = all(v.endswith("Z") for v in vals)
        if has_z and not all_z:
            # ⚠️ **行内**不一致：同一行里有的字段带 Z、有的不带
            #   → 这一行本身就混了两套基准 → 必须报 FAIL
            inconsistent += 1
            no_z += 1
            for v in vals:
                if not v.endswith("Z") and len(samples) < 5:
                    samples.append(v)
        elif all_z:
            with_z += 1
        else:
            # 整行都不带 Z（自洽但有歧义）
            no_z += 1
            for v in vals:
                if len(samples) < 5:
                    samples.append(v)
    return {
        "total": total,
        "with_z": with_z,
        "no_z": no_z,
        # mixed：文件级 —— 有带 Z 的行也有不带 Z 的行
        "mixed": bool(with_z and no_z),
        # inconsistent：行级 —— 同一行内部字段互相矛盾
        "inconsistent": inconsistent,
        "samples": samples,
    }


def normalize_file_inplace(path, dry_run: bool = False):
    """把一个 JSONL 文件里所有 `ts` 规范化成 UTC + `Z`（就地改写）。

    裸时间戳按**本地时间**解释（journald 语义）→ 转 UTC。

    返回 (changed_lines, total_lines)。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return 0, 0
    out_lines = []
    changed = 0
    total = 0
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            d = json.loads(s)
        except Exception:
            out_lines.append(s)
            continue
        ts = str(d.get("ts") or "")
        if len(ts) < 10:
            out_lines.append(s)
            continue
        total += 1
        if ts.endswith("Z"):
            out_lines.append(s)
            continue
        dt = parse_ts(ts, naive_is_local=True)
        if dt is None:
            out_lines.append(s)
            continue
        d["ts"] = to_utc_iso(dt)
        changed += 1
        out_lines.append(json.dumps(d, ensure_ascii=False))
    if changed and not dry_run:
        path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return changed, total


if __name__ == "__main__":
    import sys
    from pathlib import Path

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    target = Path(args[0]) if args else Path(
        "/root/.openclaw/workspace/memory/quota-history.jsonl"
    )
    if "--check" in sys.argv:
        info = basis_mismatch(target)
        print(f"total={info['total']} with_z={info['with_z']} no_z={info['no_z']} mixed={info['mixed']}")
        for s in info["samples"]:
            print(f"  no-Z sample: {s}")
        sys.exit(0)
    dry = "--dry-run" in sys.argv
    c, t = normalize_file_inplace(target, dry_run=dry)
    print(f"{'dry-run ' if dry else ''}规范化: {c}/{t} 行改为 UTC+Z")
