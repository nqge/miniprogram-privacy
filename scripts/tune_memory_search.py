#!/usr/bin/env python3
"""memorySearch 参数调优 —— 扫描 minScore / hybrid 权重组合，评估中文检索命中率。

背景：
  MiniMax embo-01 的余弦分数尺度与 OpenAI 不同（整体偏低），
  且 OpenClaw 的 BM25 关键词路径用 AND 拼接带引号的短语，
  对「牛哥哥 怎么称呼 中文交流」这类多词中文查询直接返回 0 命中，
  因此检索几乎完全依赖向量分数，阈值必须重新标定。

用法：python3 scripts/tune_memory_search.py
"""

import json
import subprocess
import sys
import time

CFG = "/root/.openclaw/openclaw.json"

# (查询词, 期望命中的文件, 期望行区间)
CASES = [
    ("牛哥哥 怎么称呼 中文交流", "MEMORY.md", (1, 60)),
    ("物理教学 学生兴趣", "MEMORY.md", (1, 60)),
    ("供应链投毒 预警", "MEMORY.md", (57, 130)),
    ("渗透测试 工具开发", "MEMORY.md", (190, 260)),
    ("WAF 绕过 sqlmap", "MEMORY.md", (495, 545)),
]

COMBOS = [
    (0.15, 0.85, 0.15),
    (0.10, 0.90, 0.10),
    (0.20, 0.90, 0.10),
    (0.08, 1.00, 0.00),
]


def set_cfg(min_score, vw, tw):
    with open(CFG, encoding="utf-8") as f:
        c = json.load(f)
    ms = c["agents"]["defaults"]["memorySearch"]
    ms["query"]["minScore"] = min_score
    ms["query"]["hybrid"] = {"enabled": True, "vectorWeight": vw, "textWeight": tw}
    with open(CFG, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False, indent=2)


def restart():
    subprocess.run(["openclaw", "gateway", "restart"], capture_output=True, timeout=90)
    time.sleep(9)


def search(q):
    r = subprocess.run(
        ["openclaw", "memory", "search", q, "--json"],
        capture_output=True, text=True, timeout=150,
    )
    out = r.stdout
    i = out.find("{")
    if i < 0:
        return []
    try:
        return json.loads(out[i:]).get("results", [])
    except json.JSONDecodeError:
        return []


def evaluate():
    hits = 0
    lines = []
    for q, exp_path, (lo, hi) in CASES:
        rs = search(q)
        ok = False
        top = "(无)"
        if rs:
            r0 = rs[0]
            top = f"{r0['path']}:{r0['startLine']}-{r0['endLine']}({r0['score']:.3f})"
            try:
                ok = (r0["path"] == exp_path and int(r0["startLine"]) <= hi and int(r0["endLine"]) >= lo)
            except (TypeError, ValueError):
                ok = False
        hits += ok
        lines.append(f'   {"HIT " if ok else "MISS"} "{q[:16]}" -> {len(rs)}条 top={top}')
    return hits, lines


def main():
    best = None
    for ms, vw, tw in COMBOS:
        print(f'=== minScore={ms} vectorWeight={vw} textWeight={tw} ===', flush=True)
        set_cfg(ms, vw, tw)
        restart()
        hits, lines = evaluate()
        for ln in lines:
            print(ln, flush=True)
        print(f'   命中 {hits}/{len(CASES)}\n', flush=True)
        if best is None or hits > best[0]:
            best = (hits, ms, vw, tw)
    print(f'>>> 最佳: 命中={best[0]}/{len(CASES)}  minScore={best[1]} vectorWeight={best[2]} textWeight={best[3]}')
    if best[0] < len(CASES):
        print('!!! 未达满分，需要人工调优', file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
