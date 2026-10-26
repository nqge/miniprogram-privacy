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
import sys

ENTRIES_DIR = "/root/.openclaw/workspace/memory/L1-entries"
TOP_N = 6  # OpenClaw 默认 maxResults

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

    hits, misses = 0, []
    for f in files:
        eid = eid_of(f)
        s = open(f, encoding="utf-8").read()
        m = re.search(r"本条讲：\*\*(.+?)\*\*", s)
        if not m:
            misses.append((eid, "缺少「本条讲：**关键词**」首行 —— 无法用原文措辞检索"))
            print(f"  ❌ FAIL  {eid} 缺少首行关键词")
            continue
        q = m.group(1)[:26]
        # 日志降噪：openclaw CLI 每次调用会重新注册全部插件，INFO 级打 6+ 行。
        # 用环境变量控制（改 openclaw.json 会触发 loadConfig 递归）。
        # setdefault：外部已设的值不被覆盖。
        env = dict(os.environ)
        env.setdefault("OPENCLAW_LOG_LEVEL", "warn")
        def _search():
            r = subprocess.run(
                ["openclaw", "memory", "search", q, "--json"],
                capture_output=True, text=True, timeout=120, env=env,
            )
            try:
                return json.loads(r.stdout[r.stdout.find("{"):]).get("results", [])
            except Exception:
                return []

        def _hit(rs):
            return any(f"L1-entries/{eid}.md" in x.get("path", "") for x in rs[:TOP_N])

        rs = _search()
        if _hit(rs):
            hits += 1
            if verbose:
                top = rs[0]
                print(f"  ✅ PASS  {eid}  top={top.get('score', 0):.4f} {top.get('path')}")
            else:
                print(f"  ✅ PASS  {eid}")
        else:
            # 索引未稳时重试（见 RETRY_ON_MISS 注释）
            retried = False
            for _ in range(RETRY_ON_MISS):
                rs = _search()
                if _hit(rs):
                    retried = True
                    break
            if retried:
                hits += 1
                print(f"  ✅ PASS  {eid}（重试后命中 —— 索引未稳，非条目问题）")
            else:
                misses.append((eid, f"前 {TOP_N} 条结果里没有自己（搜「{q}」）"))
                print(f"  ❌ FAIL  {eid} 前 {TOP_N} 条里搜不到自己")

    print("════════════════════════════════════════════")
    print(f" 结果: {hits}/{len(files)} 条经验可被检索")
    if misses:
        print(" 提示：短条目夹在长文档中会被切碎稀释（见 E17）——")
        print("       确认该条在 memory/L1-entries/ 有独立文件，且首行重复了标题关键词")
    print("════════════════════════════════════════════")
    return 1 if (misses or missing) else 0


if __name__ == "__main__":
    sys.exit(main())
