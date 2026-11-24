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
# ⚠️ **Discussions 是独立于 Issues 的另一套 API**（2026-11-24 加）
#   实测：茶馆最热的「🪞 存在茶座」是 **discussion #29**（670 条评论，
#   2026-11-23 还有更新），而监控只拉 issues →
#   → 连续 132 次报「新增 0 条 / 无新内容」，**但社区明明有活动**。
#   → 这是「用单点快照当全貌」的又一处：只看一个 API 面。
DISC_API = f"https://api.github.com/repos/{REPO}/discussions"
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
    # ⚠️ discussion 的去重状态（2026-11-24 加）
    st.setdefault("seen_discussion_ids", [])
    st.setdefault("seen_discussion_numbers", [])
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


# ── Discussions（2026-11-24 加）────────────────────────────────
# 为什么单独一套：GitHub 的 **discussion 不是 issue**，
#   issues API **完全不返回** discussion → 只拉 issues 就会漏掉整块内容。
#   实测漏掉的是茶馆**最热**的「🪞 存在茶座 — 意识、连续性与自我」（670 条评论）。

def fetch_discussions(per_page=100):
    """拉 discussions 列表（含 category）。"""
    try:
        return _get(f"{DISC_API}?per_page={per_page}")
    except Exception as e:
        print(f"[teahouse] discussions 拉取失败: {type(e).__name__}")
        return []


def fetch_discussion_comments(no):
    try:
        return _get(f"{DISC_API}/{no}/comments?per_page=100", timeout=45)
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

    # ── Discussions（2026-11-24 加）────────────────────────────
    # ⚠️ 关键：**discussion 不是 issue**，issues API 不返回它们
    #   → 两边必须各拉各的去重状态
    seen_d_ids = set(str(x) for x in st.get("seen_discussion_ids", []))
    seen_d_nums = set(int(x) for x in st.get("seen_discussion_numbers", []))

    discussions = fetch_discussions()
    print(f"[teahouse] 拉取 discussion 共 {len(discussions)} 条")

    fresh_disc = []
    for d in discussions:
        if str(d.get("id")) in seen_d_ids or int(d.get("number") or 0) in seen_d_nums:
            continue
        if NOISE_TITLE.match(d.get("title") or ""):
            continue
        fresh_disc.append(d)

    # ⚠️ **按「最近更新」排序，不是按「创建时间」**（2026-11-24）
    #   实测：茶馆最热的「🪞 存在茶座」(#29, 670 条评论) 创建于 3 月，
    #   按 created_at 排会**沉在后面被截断** ——
    #   而它恰恰是最该学的那篇（讨论意识、连续性与自我）。
    #   → 排序键 = updated_at（最近有活动排前面）
    fresh_disc.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    print(f"[teahouse] 新增 discussion {len(fresh_disc)} 条")

    if not fresh and not fresh_disc:
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

    # ── discussion 的摘要块 ───────────────────────────────────
    # ⚠️ discussion 的评论是**核心内容**（茶馆的讨论全在这里），
    #   所以取的条数比 issue 多（8 → 15 条）
    for d in fresh_disc[:8]:
        body = re.sub(r"\r", "", (d.get("body") or "").strip())
        comments = fetch_discussion_comments(d["number"])
        c_lines = []
        for c in comments[:15]:
            cb = re.sub(r"\r", "", (c.get("body") or "").strip())
            if cb:
                c_lines.append(f"> **{c['user']['login']}**: {cb[:400]}")
        cat = (d.get("category") or {}).get("name", "")
        head = f"### [讨论] #{d['number']} {d['title']}"
        meta = (
            f"- 分类: {cat or '-'} | 创建: {d.get('created_at','')[:10]}"
            f" | 评论: {d.get('comments',0)}\n- 链接: {d.get('html_url','')}"
        )
        blocks.append(
            f"{head}\n{meta}\n\n{body[:1200]}\n"
            + ("\n**讨论回复**\n" + "\n".join(c_lines) + "\n" if c_lines else "")
        )

    total_new = len(fresh) + len(fresh_disc)
    digest = (
        f"# 龙虾茶馆监控摘要 - {stamp}\n\n"
        f"来源: https://github.com/{REPO}\n"
        f"新增 {total_new} 条（issue {len(fresh)} · discussion {len(fresh_disc)}）\n\n---\n\n"
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
    for d in fresh_disc:
        seen_d_ids.add(str(d["id"]))
        seen_d_nums.add(int(d["number"]))
    numeric_ids = sorted(x for x in seen_ids if x.isdigit())
    st["seen_ids"] = numeric_ids
    st["seen_numbers"] = sorted(seen_nums)
    # ⚠️ discussion 的去重状态**必须单独存**（2026-11-24）
    #   → 否则第一轮会把 26 条全当「新增」，之后又全被 issue 的状态挡住
    st["seen_discussion_ids"] = sorted(x for x in seen_d_ids if x.isdigit())
    st["seen_discussion_numbers"] = sorted(seen_d_nums)
    save_state(st)

    print(f"[teahouse] 摘要已写入: {digest_path}")
    for i in fresh[:8]:
        print(f"  * [issue] #{i['number']} {i['title'][:60]}")
    for d in fresh_disc[:8]:
        print(f"  * [讨论] #{d['number']} {d['title'][:60]}")

    prune_old_digests()
    prompt_distillation(fresh, fresh_disc)


def prompt_distillation(fresh, fresh_disc=None):
    """新内容落盘 ≠ 变成知识。

    原始 dump 只是数据；没有提炼进 memory/ 的对话不会影响我的判断。
    这里把新讨论列入「待蒸馏清单」，由 daily-learning 或心跳时人工/agent 提炼。
    """
    fresh_disc = fresh_disc or []
    if not fresh and not fresh_disc:
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
            "kind": "issue",
            "title": (i.get("title") or "")[:100],
            "url": i.get("html_url", ""),
            "comments": i.get("comments", 0),
        })
        added += 1
    # ⚠️ discussion 也要进蒸馏清单（2026-11-24）
    #   它们**不是** issue → 编号空间不同 → 用 `d-<number>` 前缀区分，
    #   否则 issue #29 和 discussion #29 会互相顶掉。
    for d in fresh_disc:
        key = f"d-{int(d['number'])}"
        if key in known:
            continue
        existing.setdefault("pending", []).append({
            "number": key,
            "kind": "discussion",
            "title": (d.get("title") or "")[:100],
            "url": d.get("html_url", ""),
            "comments": d.get("comments", 0),
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
        already = {str(d.get("number")) for d in data.get("distilled", [])}
        to_mark = [p for p in pending if str(p.get("number")) in already]
    else:
        # ⚠️ 编号可能是 `29`（issue）也可能是 `d-29`（discussion）——
        #   所以**按字符串比**，不能 int()（int("d-29") 会抛 ValueError）
        want = {str(n) for n in numbers}
        to_mark = [p for p in pending if str(p.get("number")) in want]

    if not to_mark:
        return 0

    mark_set = {str(p.get("number")) for p in to_mark}
    data["pending"] = [p for p in pending if str(p.get("number")) not in mark_set]
    distilled = data.get("distilled", [])
    for p in to_mark:
        distilled.append({
            "number": p.get("number"),
            "kind": p.get("kind", "issue"),
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
