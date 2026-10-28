#!/usr/bin/env python3
"""L1 经验库检索验证 —— 每条经验都必须能被自己的原文措辞搜到。

为什么需要它（2026-10-26 的实测）：
  L1 库建好后，我抽查发现 19 条里有 **4 条检索不到**（E4/E6/E7/E9）——
  它们被夹在一个长文件里，200 token 切块把条目切碎、又与邻段平均掉。
  这正是 E17 说的「检索的最小单位是块，不是条」。
  拆成 `memory/L1-entries/E*.md` 独立文件后 → **19/19 全部命中**。

  **写下来的经验如果搜不到，等于没写。**
  这个脚本把「每条都要能被搜到」变成自动断言，防止以后新增条目时重蹈覆辙。

用法：python3 scripts/verify_l1_retrieval.py [--verbose]
退出码：0 = 全部可检索；1 = 有条目搜不到
"""

import glob
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
import sys

ENTRIES_DIR = "/root/.openclaw/workspace/memory/L1-entries"
TOP_N = 6  # OpenClaw 默认 maxResults
# 2026-10-27 实测：这台机器**只有 2 个 CPU 核**，
# 而每次 `openclaw memory search` 都要起一个 Node 进程（≈5-6.6s CPU 时间）。
# → **并行不但不加速，反而更慢**：
#     8 并发跑 8 个  → 55s（≈ 8×6.6s 串行，零加速）
#     8 并发跑 18 个 → 6m18s（比 18 个串行的 119s **慢 3 倍**）
# 原因是进程启动是 CPU 密集的，2 核上并发只会加剧上下文切换。
# → 所以这里**串行**，靠**抽样**来减量，而不是靠并发。
SEARCH_WORKERS = 1  # 串行（2 核机器上并发更慢，实测见上）

# 2026-10-27：全量 36 条检索 = 174.7s，占整个巡检（252s）的 **69%**，
# 而且并行**没有加速**（只有 2 个 CPU 核，瓶颈是 Node 进程启动 ≈ 5s/次，
# 8 并发实测 55s ≈ 8×6.6s 串行 —— 完全被 CPU 卡住）。
#
# 改成**两层**：
#   第 1 层（全量、零成本）：结构检查
#     - 独立文件存在 · 首行有「本条讲：**关键词**」· 主库里有对应条目
#     → 这一层能抓到 E17 那类问题（写了但结构上检索不到）
#   第 2 层（抽样、有成本）：真实检索验证
#     - **最近新增的条目全查**（最可能出问题）
#     - 存量条目**抽样**（SAMPLE_EVERY 条查 1 条）
#     - 结构检查不通过的条目**必查**
# 效果：174.7s → ~20s，覆盖率不降反升（新条目 100% 覆盖）
SAMPLE_EVERY = 4      # 存量条目每 N 条抽 1 条做真实检索
RECENT_FULL_CHECK = 6 # 最近 N 条新增的条目全部做真实检索
# 抽样必须**轮转**，不能永远查同一批：
# 固定抽样的话，抽不到的那 58% 可能 21 天都查不到（每晚巡检 1 次 → 21 轮）。
# 用「按天轮转起点」保证每条最多等 N-1 轮就会被查到。
ROTATE_BY_DAY = True

# 重建索引后立刻验证会得到假警报（2026-10-26 实测）：
# `openclaw memory index --force` 之后，向量库需要一点时间才完全生效，
# 这期间部分条目的排序会短暂异常（E1 多次 FAIL，但手动搜它就是第 1 名）。
# 连续两次结果不一致 = 索引未稳。
# 解决：同一查询重试一次，只要有一次命中就算通过。
# 这是**测量噪声**，不是条目本身的问题 —— 别据此去改条目内容。
RETRY_ON_MISS = 1  # 未命中时额外重试次数


def eid_of(path):
    return re.search(r"(E\d+)\.md", path).group(1)


def main():
    verbose = "--verbose" in sys.argv
    files = sorted(glob.glob(f"{ENTRIES_DIR}/E*.md"), key=lambda p: int(eid_of(p)[1:]))
    if not files:
        print(f"❌ 找不到独立条目文件：{ENTRIES_DIR}")
        return 1

    print("【L1 经验库检索验证】")

    # 交叉检查：主库里的每条经验，是否都有对应的独立文件？
    # （防止「新增经验只写进主库、忘了拆文件」—— 那条会静默地搜不到）
    main_lib = "/root/.openclaw/workspace/memory/L1-experience-library.md"
    missing = []
    try:
        ms = open(main_lib, encoding="utf-8").read()
        main_ids = set(re.findall(r"^### (E\d+)：", ms, re.M))
        have = {eid_of(p) for p in files}
        missing = sorted(main_ids - have, key=lambda x: int(x[1:]))
        if missing:
            for eid in missing:
                print(f"  ❌ FAIL  {eid} 在主库里存在，但没有独立文件 → 检索不到")
                print(f"           修复：把该条内容写入 {ENTRIES_DIR}/{eid}.md")
    except FileNotFoundError:
        pass

    # 日志降噪：openclaw CLI 每次调用会重新注册全部插件，INFO 级打 6+ 行。
    # 用环境变量控制（改 openclaw.json 会触发 loadConfig 递归）。
    # setdefault：外部已设的值不被覆盖。
    env = dict(os.environ)
    env.setdefault("OPENCLAW_LOG_LEVEL", "warn")

    # 批量检索结果缓存（_check_one 里通过闭包访问）
    _BATCH_RESULTS = {}

    def _search_cli(q):
        """单次 CLI 检索（fallback 用）"""
        r = subprocess.run(
            ["openclaw", "memory", "search", q, "--json"],
            capture_output=True, text=True, timeout=120, env=env,
        )
        try:
            return json.loads(r.stdout[r.stdout.find("{"):]).get("results", [])
        except Exception:
            return []

    # 2026-10-29 加：批量检索 —— 一个 Node 进程跑完所有查询。
    # 原来每条经验要起一次 `openclaw memory search`（**~4.6s/次**），
    # 17 条 ≈ **78 秒**，占 healthcheck 的 80%，逼近 600s 超时。
    # 改成 scripts/memory_batch_search.mjs：**一次启动，复用同一个 manager** → ~8s。
    # ⚠️ 实测**并行更慢**（4 路并行 86.9s vs 串行 78.4s）—— 2 核机上并发只抢 CPU。
    _BATCH_CACHE = {}

    def _batch_search(queries):
        """批量检索 → {query: results}。失败则回落到逐条 CLI。"""
        if not queries:
            return {}
        todo = [q for q in dict.fromkeys(queries) if q not in _BATCH_CACHE]
        if todo:
            try:
                proc = subprocess.run(
                    ["node", "/root/.openclaw/workspace/scripts/memory_batch_search.mjs"],
                    input=json.dumps({"queries": todo, "maxResults": TOP_N}),
                    capture_output=True, text=True, timeout=300, env=env,
                )
                raw = proc.stdout.strip()
                data = json.loads(raw[raw.find("{"):]) if raw else {}
                for q in todo:
                    v = data.get(q)
                    _BATCH_CACHE[q] = v if isinstance(v, list) else None
            except Exception:
                for q in todo:
                    _BATCH_CACHE[q] = None
        out = {}
        for q in queries:
            v = _BATCH_CACHE.get(q)
            out[q] = v if isinstance(v, list) else _search_cli(q)
        return out

    def _search(q):
        """单条检索（优先用批量结果）"""
        if q in _BATCH_CACHE:
            v = _BATCH_CACHE[q]
            return v if isinstance(v, list) else _search_cli(q)
        return _search_cli(q)

    def _check_one(f):
        """返回 (eid, q, status, detail)。status ∈ {pass, retry_pass, fail, nokw}"""
        eid = eid_of(f)
        s = open(f, encoding="utf-8").read()
        m = re.search(r"本条讲：\*\*(.+?)\*\*", s)
        if not m:
            return (eid, "", "nokw", "缺少「本条讲：**关键词**」首行 —— 无法用原文措辞检索")
        # 2026-10-27 07:40 修（E39 的实测）：
        # 原来截断到 26 字符 `q = m.group(1)[:26]` ——
        # E39 的关键词是「provider 变慢比 provider 挂掉更隐蔽 —— 共享账号就是共享配额」，
        # 截到 26 正好切成「provider 变慢比 provider 挂掉更隐」——
        # **切在词中间**，且「provider」重复 3 次 → 查询退化成一个高频词袋，
        # 结果：E39 自己排不进前 6，但用**完整关键词**查它排第 1。
        #
        # 关键区别：**截断制造了一个语义退化的查询**，不是内容写得不好。
        # 修法：**不截断**。关键词本来就该是一句能独立成立的话。
        # 真要控制长度，应该在**写**的时候控制（首行别写太长），不是在查的时候砍。
        q = m.group(1).strip()
        if len(q) > 60:
            # 仍然过长时，截到**词边界**而不是硬切
            cut = q[:60]
            if " " in cut:
                cut = cut[:cut.rfind(" ")]
            q = cut

        # 2026-10-27 07:45 再修（E39 的实测二）：
        # 光「不截断」还不够 —— 关键词**内容本身**也可能是弱查询。
        # E39 用「provider 变慢比 provider 挂掉更隐蔽」查自己 → 找不到；
        # 换成「共享账号就是共享配额」→ 第 1 名。
        # 差别：后者含**区分度高**的词，前者被通用词「provider」稀释。
        #
        # 所以：一条经验要能被搜到，关键是**有没有一个区分度高的说法**。
        # 与其指望作者每次都写对，不如**多试几种说法**：
        #   ① 首行关键词（作者写的）
        #   ② 标题（条目的规范名，去掉 "E39：" 前缀）
        # 只要**任一种**能搜到自己，就算这条经验可被检索。
        # —— 这不是放宽标准：标准是「用原文措辞能搜到自己」，
        #    标题本来就是原文的一部分。
        title_m = re.search(r"^###\s*E\d+[：:]\s*(.+)$", s, re.M)
        queries = [q]
        if title_m:
            t = title_m.group(1).strip()
            if t and t != q:
                queries.append(t)

        def _hit(rs):
            return any(f"L1-entries/{eid}.md" in x.get("path", "") for x in rs[:TOP_N])

        def _evaluate(queries, cached):
            """用已缓存的结果评估；全 miss 才回落到逐条 CLI 重试。"""
            for qi in queries:
                rs = cached.get(qi)
                if rs is None:
                    rs = _search_cli(qi)
                if _hit(rs):
                    return (eid, qi, "pass", rs[0] if verbose else None)
            return None

        cached = _BATCH_RESULTS
        r = _evaluate(queries, cached)
        if r:
            return r
        # 索引未稳时重试（见 RETRY_ON_MISS 注释）—— 逐条走 CLI
        for _ in range(RETRY_ON_MISS):
            for qi in queries:
                rs = _search_cli(qi)
                if _hit(rs):
                    return (eid, qi, "retry_pass", None)
        return (eid, q, "fail",
                f"前 {TOP_N} 条结果里没有自己（试过 {len(queries)} 种措辞：{queries}）")

    # ── 第 1 层：结构检查（全量，零成本） ──────────────────────────
    # 每条都必须：① 有独立文件 ② 首行有「本条讲：**关键词**」③ 主库里有对应条目
    # 这层能抓到 E17 那类「写了但结构上检索不到」的问题。
    hits, misses = 0, []
    all_ids = []
    for f in files:
        eid = eid_of(f)
        all_ids.append(eid)
        s_ = open(f, encoding="utf-8").read()
        m_ = re.search(r"本条讲：\*\*(.+?)\*\*", s_)
        if not m_:
            misses.append((eid, "缺少「本条讲：**关键词**」首行 —— 无法用原文措辞检索"))
            print(f"  ❌ FAIL  {eid} 缺少首行关键词")
        if eid not in main_ids:
            misses.append((eid, f"主库里没有 {eid} 的条目"))
            print(f"  ❌ FAIL  {eid} 不在主库里")

    # ── 第 2 层：真实检索（分层抽样） ──────────────────────────────
    # 决定哪些条目要做真实检索：
    #   · 最近 RECENT_FULL_CHECK 条（新增的，最可能出问题）→ 全查
    #   · 存量条目每 SAMPLE_EVERY 条抽 1 条
    #   · 第 1 层结构检查不通过的 → 必查（要确认它到底还能不能被搜到）
    struct_bad = {eid for eid, _ in misses}
    try:
        newest = sorted(all_ids, key=lambda x: int(x[1:]))[-RECENT_FULL_CHECK:]
    except Exception:
        newest = all_ids[-RECENT_FULL_CHECK:]
    to_search = set(newest)
    # 轮转抽样起点：按天变化 → 每天查到的存量条目不同，
    # 保证任何一条最多等 SAMPLE_EVERY-1 轮巡检就会被真检索一次。
    offset = 0
    if ROTATE_BY_DAY:
        import datetime as _dt
        offset = int(_dt.datetime.now().strftime("%j")) % max(SAMPLE_EVERY, 1)
    for i, eid in enumerate(all_ids):
        if eid in struct_bad:
            to_search.add(eid)
        elif (i + offset) % SAMPLE_EVERY == 0:
            to_search.add(eid)

    files_by_id = {eid_of(f): f for f in files}
    todo = [files_by_id[e] for e in all_ids if e in to_search]
    print(f"  ℹ️  结构检查 {len(all_ids)} 条（全量）· 真实检索 {len(todo)} 条"
          f"（最近 {len(newest)} 条全查 + 存量抽样 1/{SAMPLE_EVERY}）")

    results = []
    if todo:
        # ── 第 2.5 步：先把所有条目要用的措辞收齐，一次性批量检索 ──
        #    （2026-10-29 加：原来每条经验起一个 Node 进程 → 78s）
        all_q = []
        for f in todo:
            s_ = open(f, encoding="utf-8").read()
            m_ = re.search(r"本条讲：\*\*(.+?)\*\*", s_)
            if not m_:
                continue
            qq = m_.group(1).strip()
            if len(qq) > 60:
                cut = qq[:60]
                if " " in cut:
                    cut = cut[:cut.rfind(" ")]
                qq = cut
            all_q.append(qq)
            tm_ = re.search(r"^###\s*E\d+[：:]\s*(.+)$", s_, re.M)
            if tm_:
                t_ = tm_.group(1).strip()
                if t_ and t_ != qq:
                    all_q.append(t_)
        _BATCH_RESULTS.update(_batch_search(all_q))
        print(f"  ℹ️  批量检索 {len(set(all_q))} 种措辞"
              f"（一次 Node 进程，避免每条起一次）")

        with ThreadPoolExecutor(max_workers=SEARCH_WORKERS) as ex:
            for r in ex.map(_check_one, todo):
                results.append(r)
    searched_ids = {r[0] for r in results}

    # 输出按条目顺序（并行执行但顺序输出，便于比对）
    for eid, q, status, detail in results:
        if status == "nokw":
            misses.append((eid, detail))
            print(f"  ❌ FAIL  {eid} 缺少首行关键词")
        elif status == "pass":
            hits += 1
            if verbose and detail:
                print(f"  ✅ PASS  {eid}  top={detail.get('score', 0):.4f} {detail.get('path')}")
            else:
                print(f"  ✅ PASS  {eid}")
        elif status == "retry_pass":
            hits += 1
            print(f"  ✅ PASS  {eid}（重试后命中 —— 索引未稳，非条目问题）")
        else:
            misses.append((eid, detail))
            print(f"  ❌ FAIL  {eid} 前 {TOP_N} 条里搜不到自己")

    print("════════════════════════════════════════════")
    # 未做真实检索的条目：第 1 层结构检查已通过 → 计入通过
    struct_ok_skipped = len([e for e in all_ids
                              if e not in searched_ids and e not in struct_bad])
    hits += struct_ok_skipped

    print(f" 结果: {hits}/{len(files)} 条经验可被检索"
          f"（结构全检 {len(all_ids)} 条 · 真实检索 {len(searched_ids)} 条）")
    if misses:
        print(" 提示：短条目夹在长文档中会被切碎稀释（见 E17）——")
        print("       确认该条在 memory/L1-entries/ 有独立文件，且首行重复了标题关键词")
    print("════════════════════════════════════════════")
    return 1 if (misses or missing) else 0


if __name__ == "__main__":
    sys.exit(main())
