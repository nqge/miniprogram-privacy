#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
茶馆监控 v2 — 从 GitHub Issues 拉取龙虾茶馆讨论，去重 + 摘要 + 存入 memory/

修复说明（2026-10-26）：
  旧版 state 用 GraphQL node_id 去重，但抓取路径切换到 REST API 后
  node_id 与 seen_ids 格式不一致，导致 166 天全部被当成"新内容"或反过来漏掉。
  v2 统一使用 REST 的 issue id（整数）作为主键，node_id 作为兼容键。
"""

import json
import os
import re
import time
import urllib.error
import urllib.request
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = "ythx-101/openclaw-qa"
API = f"https://api.github.com/repos/{REPO}/issues"
STATE_FILE = Path("/root/.openclaw/workspace/memory/teahouse_state.json")
# 原始社区摘要是「外部内容 dump」，不是我的记忆。
# 放在 memory/ 下会：
#   1. 被 memorySearch 索引 —— 80KB/份、4 小时一份，实测占据 64% 索引块，
#      把真正的个人记忆（称呼、偏好、决策）挤出检索结果
#   2. 无限累积（无清理逻辑）
# 注意：memorySearch 会**递归**索引 memory/ 的所有子目录（实测确认），
# 所以必须放到 memory/ **外面**。值得记的结论由每日笔记提炼进 memory/。
OUT_DIR = Path("/root/.openclaw/workspace/teahouse-raw")
NOISE_TITLE = re.compile(r"^\s*test\s*$", re.I)
HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "openclaw-teahouse-monitor",
}

# 认证请求：匿名额度只有 60 次/小时/IP，认证后是 5000 次/小时。
# 2026-10-26 实测踩到：匿名额度耗尽 → API 返回 403 rate limit exceeded
# → 茶馆监控拉不到新讨论（而且报错信息不像「限流」，很难一眼看出）。
# token 统一从 scripts/gh_auth.py 取（单一真相源，避免三处各抄一遍正则）。
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from gh_auth import github_headers
    HEADERS = github_headers(HEADERS)
except Exception:
    pass


def _get(url, timeout=90, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            return json.loads(raw.decode("utf-8"))
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


def load_state():
    try:
        st = json.load(open(STATE_FILE))
    except Exception:
        st = {}
    st.setdefault("seen_ids", [])
    st.setdefault("seen_numbers", [])
    st.setdefault("last_check", None)
    return st


def save_state(st):
    st["last_check"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    json.dump(st, open(STATE_FILE, "w"), ensure_ascii=False, indent=2)


def fetch_issues(page=1, per_page=100):
    return _get(f"{API}?state=all&per_page={per_page}&page={page}")


def fetch_comments(issue_no):
    try:
        return _get(f"{API}/{issue_no}/comments?per_page=100", timeout=45)
    except Exception:
        return []


def main():
    st = load_state()
    seen_ids = set(str(x) for x in st.get("seen_ids", []))
    seen_nums = set(int(x) for x in st.get("seen_numbers", []))

    issues = fetch_issues()
    issues = [i for i in issues if i.get("pull_request") is None]  # 排除 PR
    print(f"[teahouse] 拉取 issue 共 {len(issues)} 条")

    fresh = []
    for i in issues:
        if int(i["id"]) in seen_ids or int(i["number"]) in seen_nums:
            continue
        if NOISE_TITLE.match(i.get("title") or ""):
            continue
        fresh.append(i)

    fresh.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    print(f"[teahouse] 新增 {len(fresh)} 条")

    if not fresh:
        save_state(st)
        print("[teahouse] 无新内容")
        return

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    digest_path = OUT_DIR / f"teahouse_summary_{stamp}.md"

    blocks = []
    for i in fresh[:15]:
        body = (i.get("body") or "").strip()
        body = re.sub(r"\r", "", body)
        comments = fetch_comments(i["number"])
        c_lines = []
        for c in comments[:8]:
            cb = re.sub(r"\r", "", (c.get("body") or "").strip())
            if cb:
                c_lines.append(f"> **{c['user']['login']}**: {cb[:400]}")
        head = f"### #{i['number']} {i['title']}"
        meta = f"- 创建: {i.get('created_at','')[:10]} | 评论: {i.get('comments',0)}\n- 链接: {i.get('html_url','')}"
        blocks.append(f"{head}\n{meta}\n\n{body[:1200]}\n" + ("\n**讨论回复**\n" + "\n".join(c_lines) + "\n" if c_lines else ""))

    digest = (
        f"# 龙虾茶馆监控摘要 - {stamp}\n\n"
        f"来源: https://github.com/{REPO}\n"
        f"新增 {len(fresh)} 条讨论（展示前 {min(15,len(fresh))} 条）\n\n---\n\n"
        + "\n---\n\n".join(blocks)
    )

    # 去重：与最近一份摘要内容几乎相同就不重复落盘
    # （实测 4 小时一次的两次运行会产出 99.8% 相同的 80KB 文件，纯浪费磁盘和磁盘 IO）
    prev = sorted(OUT_DIR.glob("teahouse_summary_*.md"), key=lambda p: p.stat().st_mtime)
    if prev:
        latest = prev[-1]
        try:
            prev_text = latest.read_text(encoding="utf-8")
            # 只比较讨论主体（去掉时间戳标题行），避免因 stamp 不同而误判为不同
            def body_of(t):
                return "\n".join(l for l in t.splitlines() if not l.startswith("# 龙虾茶馆监控摘要"))
            if body_of(prev_text) == body_of(digest):
                print(f"[teahouse] 内容与最近摘要相同，跳过落盘（保留 {latest.name}）")
                digest_path = latest
            else:
                digest_path.write_text(digest, encoding="utf-8")
        except OSError:
            digest_path.write_text(digest, encoding="utf-8")
    else:
        digest_path.write_text(digest, encoding="utf-8")

    # 更新 state：把新条目加进去
    for i in fresh:
        seen_ids.add(str(i["id"]))
        seen_nums.add(int(i["number"]))
    numeric_ids = sorted(x for x in seen_ids if x.isdigit())
    st["seen_ids"] = numeric_ids
    st["seen_numbers"] = sorted(seen_nums)
    save_state(st)

    print(f"[teahouse] 摘要已写入: {digest_path}")
    for i in fresh[:8]:
        print(f"  * #{i['number']} {i['title'][:60]}")

    prune_old_digests()
    prompt_distillation(fresh)


def prompt_distillation(fresh):
    """新内容落盘 ≠ 变成知识。

    原始 dump 只是数据；没有提炼进 memory/ 的对话不会影响我的判断。
    这里把新讨论列入「待蒸馏清单」，由 daily-learning 或心跳时人工/agent 提炼。
    """
    if not fresh:
        return
    todo = Path("/root/.openclaw/workspace/memory/teahouse-todo.json")
    try:
        existing = json.loads(todo.read_text(encoding="utf-8")) if todo.exists() else {"pending": []}
    except Exception:
        existing = {"pending": []}
    known = {p["number"] for p in existing.get("pending", [])}
    added = 0
    for i in fresh:
        n = int(i["number"])
        if n in known:
            continue
        existing.setdefault("pending", []).append({
            "number": n,
            "title": (i.get("title") or "")[:100],
            "url": i.get("html_url", ""),
            "comments": i.get("comments", 0),
        })
        added += 1
    existing["pending"] = existing["pending"][-50:]  # 清单本身也限量
    try:
        todo.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[teahouse] 待蒸馏清单新增 {added} 条（当前 {len(existing['pending'])} 条待处理）")
    except Exception as e:
        print(f"[teahouse] 写待蒸馏清单失败（忽略）: {e}")


def mark_distilled(numbers=None):
    """把已蒸馏的讨论从 pending 队列里移走。

    2026-10-27 修：原来 pending 队列**只有加没有减** —— prompt_distillation()
    只追加，从不移除。结果 #135/#138/#134/#154 在 11:30 蒸馏完成后，
    仍留在队列里，每次运行（每 4 小时）都被当成「待处理」重新蒸馏一遍
    —— 就是茶馆笔记里说的「反刍」。

    现在：
      - 已蒸馏的条目移到 distilled 列表（保留编号/标题，便于追溯）
      - pending 只留**真正没处理**的
      - distilled 列表限量（默认 50），避免又变成一个只增不减的黑洞
    """
    todo = Path("/root/.openclaw/workspace/memory/teahouse-todo.json")
    try:
        data = json.loads(todo.read_text(encoding="utf-8")) if todo.exists() else {}
    except Exception:
        print("[teahouse] 待蒸馏清单读不出来，跳过标记")
        return 0
    pending = data.get("pending", [])
    if not pending:
        return 0

    if numbers is None:
        # 没指定 → 自动判定：标题里含「蒸馏」字样，或编号已在 distilled 里
        already = {d.get("number") for d in data.get("distilled", [])}
        to_mark = [p for p in pending if p.get("number") in already]
    else:
        want = {int(n) for n in numbers}
        to_mark = [p for p in pending if int(p.get("number", -1)) in want]

    if not to_mark:
        return 0

    mark_set = {int(p.get("number")) for p in to_mark}
    data["pending"] = [p for p in pending if int(p.get("number")) not in mark_set]
    distilled = data.get("distilled", [])
    for p in to_mark:
        distilled.append({
            "number": p.get("number"),
            "title": p.get("title", "")[:100],
            "comments": p.get("comments", 0),
            "distilled_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        })
    data["distilled"] = distilled[-50:]   # 限量，避免又变成只增不减的黑洞
    try:
        todo.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[teahouse] 已标记 {len(to_mark)} 条为已蒸馏（pending 剩 {len(data['pending'])} 条）")
        return len(to_mark)
    except Exception as e:
        print(f"[teahouse] 写待蒸馏清单失败（忽略）: {e}")
        return 0


def prune_old_digests(keep=5):
    """只保留最近 keep 份原始摘要，避免无限膨胀。

    这些是外部社区内容 dump，价值在于「已提炼进每日笔记」，原始副本不必长期留存。
    """
    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        digests = sorted(OUT_DIR.glob("teahouse_summary_*.md"), key=lambda p: p.stat().st_mtime)
        removed = 0
        for p in digests[:-keep]:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
        if removed:
            print(f"[teahouse] 清理旧摘要 {removed} 份（保留最近 {keep} 份）")
    except Exception as e:  # 清理失败不应影响主流程
        print(f"[teahouse] 清理旧摘要时出错（忽略）: {e}")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--mark-distilled":
        nums = [x for x in sys.argv[2].split(",") if x.strip()]
        mark_distilled([int(n) for n in nums] if nums else None)
    else:
        main()
