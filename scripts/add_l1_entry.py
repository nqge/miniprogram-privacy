#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
【L1 经验入库器】—— 加一条经验时，把**所有**该改的地方一次改完。

**为什么需要这个脚本**（2026-10-27 20:45 实测）：
我加了 E51 之后，巡检 FAIL 从 3 直接涨到 **9**，9 个失败**全部**指向同一件事：
「新增经验只写了独立文件，没同步主库和声明」。

涉及 4 个地方，漏一个就会触发一组 FAIL：
  ① memory/L1-entries/E{n}.md 首行格式
     `### E{n}：{标题}`  +  `> 本条讲：**{关键词}**`
  ② memory/L1-experience-library.md 追加 `### E{n}：{标题}` 条目
  ③ memory/L1-experience-library.md 头部「当前条目数：N 条（E1–E{n}）」
  ④ MEMORY.md 两处声明（「N 条，E1–E{n}」和「检索自检（必须 N/N）」）

**用法**：
    python3 scripts/add_l1_entry.py --id E52 --title "标题" --keyword "关键词" \
        --file memory/L1-entries/E52.md
    # 或从已有文件自动提取标题/关键词：
    python3 scripts/add_l1_entry.py --id E52 --file memory/L1-entries/E52.md

**它做什么**：
  1. 校验/修正独立文件的首行格式
  2. 把条目追加进主库（若已在主库则替换，不重复追加）
  3. 同步主库头部声明
  4. 同步 MEMORY.md 两处声明
  5. 跑一遍 verify_l1_retrieval.py 验证

**它不会做什么**：不改正文内容。只做「格式 + 索引」的同步。
"""


import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

WS = Path("/root/.openclaw/workspace")
ENTRIES = WS / "memory/L1-entries"
MAIN_LIB = WS / "memory/L1-experience-library.md"
MEMORY = WS / "MEMORY.md"
VERIFY = WS / "scripts/verify_l1_retrieval.py"


def eid_of(n: int) -> str:
    return f"E{n}"


def extract_title_kw(text: str):
    """从正文里提取标题和关键词。优先用已有的规范格式；没有就退化到首个 H1/H2。"""
    m = re.search(r"^###\s*E\d+[：:]\s*(.+)$", text, re.M)
    title = m.group(1).strip() if m else None

    m = re.search(r"本条讲：\*\*(.+?)\*\*", text)
    kw = m.group(1).strip() if m else None

    if title is None:
        m = re.search(r"^#{1,2}\s*(?:E\d+[：:]\s*)?(.+)$", text, re.M)
        title = m.group(1).strip() if m else None

    return title, kw


def ensure_entry_file(path: Path, eid: str, title: str, kw: str) -> bool:
    """确保独立文件首行是规范格式。返回是否改动过。"""
    if not path.exists():
        print(f"❌ 文件不存在: {path}")
        return False
    text = path.read_text(encoding="utf-8")
    orig = text

    # 规范化标题行
    want_title_line = f"### {eid}：{title}"
    m = re.search(r"^#{1,3}\s*E\d+[：:].*$", text, re.M)
    if m:
        if m.group(0).strip() != want_title_line:
            text = text[:m.start()] + want_title_line + text[m.end():]
    else:
        # 没有标题行 → 在最前面加
        text = want_title_line + "\n\n" + text

    # 规范化「本条讲」行
    want_kw_line = f"> 本条讲：**{kw}**"
    m2 = re.search(r"^> 本条讲：\*\*.+?\*\*\s*$", text, re.M)
    if m2:
        if m2.group(0).strip() != want_kw_line:
            text = text[:m2.start()] + want_kw_line + text[m2.end():]
    else:
        # 插到标题行之后
        lines = text.split("\n")
        # 找到标题行位置
        idx = next((i for i, l in enumerate(lines) if l.startswith(f"### {eid}")), -1)
        insert_at = idx + 1 if idx >= 0 else 0
        lines.insert(insert_at, "")
        lines.insert(insert_at + 1, want_kw_line)
        text = "\n".join(lines)

    if text != orig:
        path.write_text(text, encoding="utf-8")
        return True
    return False


def sync_main_lib(eid: str, title: str, entry_body: str | None) -> bool:
    """把条目同步进主库 + 更新头部声明。"""
    if not MAIN_LIB.exists():
        print(f"❌ 主库不存在: {MAIN_LIB}")
        return False
    text = MAIN_LIB.read_text(encoding="utf-8")
    orig = text

    heading = f"### {eid}：{title}"
    body = entry_body.strip() if entry_body else ""
    # 组装主库条目：标题 + 「本条讲」行 + 正文
    block = f"{heading}\n\n> 本条讲：**{title}**\n\n{body}\n" if body else f"{heading}\n\n> 本条讲：**{title}**\n"

    # 若主库已有该条目 → 替换；否则追加
    pat = re.compile(rf"^###\s*{re.escape(eid)}[：:].*?(?=^###\s*E\d+[：:]|\Z)",
                     re.M | re.S)
    if pat.search(text):
        text = pat.sub(block + "\n", text, count=1)
    else:
        text = text.rstrip() + "\n\n" + block

    # 更新头部声明「当前条目数：N 条（E1–E{max}）」
    n_files = len(list(ENTRIES.glob("E*.md")))
    max_e = max((int(p.stem[1:]) for p in ENTRIES.glob("E*.md")), default=0)
    text = re.sub(r"当前条目数\*\*[：:]\s*\d+\s*条[（(]E1[–\-]E\d+[）)]",
                  f"当前条目数**：{n_files} 条（E1–E{max_e}）", text)

    if text != orig:
        MAIN_LIB.write_text(text, encoding="utf-8")
        return True
    return False


def sync_memory(n_files: int, max_e: int) -> bool:
    """同步 MEMORY.md 的两处声明。"""
    if not MEMORY.exists():
        return False
    text = MEMORY.read_text(encoding="utf-8")
    orig = text
    text = re.sub(r"[（(]\d+\s*条[，,]E1[–\-]E\d+[）)]",
                  f"（{n_files} 条，E1–E{max_e}）", text)
    text = re.sub(r"检索自检[（(]必须\s*\d+/\d+[）)]",
                  f"检索自检（必须 {n_files}/{n_files}）", text)
    if text != orig:
        MEMORY.write_text(text, encoding="utf-8")
        return True
    return False


def main():
    ap = argparse.ArgumentParser(description="L1 经验入库器（自动同步索引）")
    ap.add_argument("--id", required=True, help="条目编号，如 E52")
    ap.add_argument("--title", help="标题（不给则从文件提取）")
    ap.add_argument("--keyword", help="首行关键词（不给则从文件提取）")
    ap.add_argument("--file", help="独立文件路径")
    ap.add_argument("--no-verify", action="store_true", help="跳过验证")
    args = ap.parse_args()

    eid = args.id.upper()
    n = int(eid[1:])
    path = Path(args.file) if args.file else ENTRIES / f"{eid}.md"
    if not path.is_absolute():
        path = WS / path

    if not path.exists():
        print(f"❌ 找不到独立文件: {path}")
        sys.exit(1)

    text = path.read_text(encoding="utf-8")
    f_title, f_kw = extract_title_kw(text)
    title = args.title or f_title
    kw = args.keyword or f_kw
    if not title:
        print("❌ 无法确定标题：请用 --title 指定")
        sys.exit(1)
    if not kw:
        # 用标题做关键词（必须和标题略有区分度才能检索到）
        kw = title

    print(f"【L1 入库】{eid}")
    print(f"  标题: {title}")
    print(f"  关键词: {kw}")
    print(f"  文件: {path}")

    changed = []
    if ensure_entry_file(path, eid, title, kw):
        changed.append("独立文件首行")

    # 正文（去掉首行格式部分，作为主库条目正文）
    body = re.sub(rf"^###\s*{re.escape(eid)}[：:].*$\n?^> 本条讲：\*\*.+?\*\*\s*$",
                  "", text, flags=re.M).strip()
    if sync_main_lib(eid, title, body):
        changed.append("主库条目 + 头部声明")

    n_files = len(list(ENTRIES.glob("E*.md")))
    max_e = max((int(p.stem[1:]) for p in ENTRIES.glob("E*.md")), default=0)
    if sync_memory(n_files, max_e):
        changed.append("MEMORY.md 声明")

    if changed:
        print(f"  ✅ 已同步: {', '.join(changed)}")
    else:
        print("  ℹ️  无需改动（已是最新）")

    if not args.no_verify:
        print("\n【验证】跑 verify_l1_retrieval.py …")
        env = dict(os.environ)
        env["OPENCLAW_LOG_LEVEL"] = "warn"
        r = subprocess.run([sys.executable, str(VERIFY)],
                           capture_output=True, text=True, env=env, timeout=300)
        for line in (r.stdout or "").splitlines():
            if "结果:" in line or "FAIL" in line:
                print("  " + line.strip())
        if r.returncode != 0:
            print(f"  ⚠️  验证未通过（退出码 {r.returncode}）")
            sys.exit(r.returncode)
        print("  ✅ 验证通过")


if __name__ == "__main__":
    main()
