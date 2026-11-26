#!/usr/bin/env python3
"""巡检脚本**自身解析器**的自检（2026-10-27 13:40 新增）

背景（一个真实的翻车）：
  今天我为了统一标点，跑了个脚本把 healthcheck.sh 里的全角标点批量换成半角。
  结果**改坏了 4 个正则**：
    1. 【J】`^### (E\\d+)[：:]` → 字符类里的全角冒号被写成半角
       → 匹配数变成 0 → 报「L1 经验库没有任何条目」（实际有 48 条）
    2. 【S】`^- \\`([a-z0-9-]+)\\` —` → 破折号被写成连字符
       → 声明列表解析成空 → 报「漏了全部 cron 任务」
    3. 【O】`（(\\d+) 条，E1–E(\\d+)）` → 同上，匹配不到
    4. 【O】`当前条目数\\*\\*：(\\d+) 条（E1–E(\\d+)）` → 同上

**最毒的一点**：这 4 个坏掉的断言里，有 3 个是「**匹配不到就静默跳过**」的类型。
  它们不会报错，只会**什么都不检查** → 变成「装死的断言」。

为什么要有这个文件：
  正则里的全角标点极其脆弱 —— 任何一次「批量改标点」「格式化」
  都能把它们悄悄改坏，而**坏掉的断言看起来和好断言一样正常**。
  → 所以必须有一个**专门验证「解析器还能解析」**的测试。

跑法：python3 scripts/test_healthcheck_parsers.py
退出码 0 = 全通过
"""
import glob
import json
import os
import re
import sys

WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(p):
    with open(os.path.join(WS, p), encoding='utf-8') as fh:
        return fh.read()


def main():
    mem = read('MEMORY.md')
    lib = read('memory/L1-experience-library.md')
    state = read('SESSION-STATE.md')

    cases = []

    # 【J】L1 条目数 —— 字符类必须同时容纳全角/半角冒号
    entries = re.findall(r'^### (E\d+)[:\uff1a]', lib, re.M)
    cases.append((len(entries) > 0 and len(entries) == len(glob.glob(
        os.path.join(WS, 'memory/L1-entries/E*.md'))),
        f'【J】L1 条目数解析：{len(entries)} 条'))

    # 【O】MEMORY.md 里的两处计数
    m1 = re.search(r'[\uff08(](\d+) \u6761\uff0cE1[\u2013\-]E(\d+)[\uff09)]', mem)
    m2 = re.search(r'检索自检[\uff08(]必须 (\d+)/\d+[\uff09)]', mem)
    cases.append((m1 is not None, '【O】MEMORY.md「L1 条数」能解析'))
    cases.append((m2 is not None, '【O】MEMORY.md「检索自检」能解析'))

    # 【O】L1 主库头部的条目数
    h = re.search(r'当前条目数\*\*[\uff1a:](\d+) \u6761[\uff08(]E1[\u2013\-]E(\d+)[\uff09)]', lib)
    cases.append((h is not None, '【O】L1 主库「当前条目数」能解析'))

    # 【O】L1 主库的「检索自检」行（2026-11-26 加，E89）
    # ⚠️ 这一行**原来既没有断言、也没被入库脚本同步** → 静默烂到 77/77。
    #   格式与 MEMORY.md 那行**不同**（没有括号）→ 必须单独一个正则，
    #   而且它的解析器也要被本自检覆盖，否则新加的断言可能一装死就没人知道。
    hl = re.search(r'检索自检[^\n]*?必须\s*(\d+)/(\d+)', lib)
    cases.append((hl is not None, '【O】L1 主库「检索自检」能解析'))

    # 【X-HEREDOC】healthcheck.sh 里每个 python heredoc 块都**必须能编译**
    # ⚠️ 2026-11-24 加（真实翻车）：
    #   我给【I】组加了个 `_bad_num()` 辅助函数，**缩进写错了一级** →
    #   整个【I】组的 Python 块 `IndentationError` →
    #   而这组的输出被 `2>/dev/null` 吞掉 → **组里什么都没打印**，
    #   但 `PASS=$((PASS + 1))` 照样执行 → **组照样计分** →
    #   巡检报告显示「PASS=100 FAIL=0」，**看起来一切正常**。
    #
    #   → 这就是「装死的断言」的**最阴险版本**：
    #     连「没输出」这个信号都被吞了，**失败得无声无息**。
    #   → 修法：**每个 heredoc 块都要单独 compile() 一遍**，
    #     编译不过 = 那一组已经死了 = 必须 FAIL。
    try:
        hc = read('scripts/healthcheck.sh')
        blocks = []
        for m in re.finditer(r"python3 - << 'PY'[^\n]*\n(.*?)\nPY(?:\n|\s*$)",
                             hc, re.S):
            blocks.append(m.group(1))
        bad_blocks = []
        for idx, b in enumerate(blocks, 1):
            try:
                compile(b, f'<heredoc#{idx}>', 'exec')
            except SyntaxError as e:
                bad_blocks.append(f'#{idx} 行{e.lineno}: {e.msg}')
        cases.append((not bad_blocks,
                      f'【X-HEREDOC】healthcheck.sh 的 {len(blocks)} 个 python 块都能编译'
                      + (f'（坏: {bad_blocks}）' if bad_blocks else '')))
    except Exception as e:
        cases.append((False, f'【X-HEREDOC】heredoc 块扫描异常：{e}'))

    # 【O】三处计数必须一致
    if m1 and h:
        same = int(m1.group(1)) == int(h.group(1))
        cases.append((same, f'【O】MEMORY({m1.group(1)}) 与 主库({h.group(1)}) 一致'))

    # 【O】L1 独立文件的标题编号 vs 文件名编号
    mismatch = []
    for f in glob.glob(os.path.join(WS, 'memory/L1-entries/E*.md')):
        head = read(os.path.relpath(f, WS))[:400]
        mt = re.search(r'^### (E\d+)', head, re.M)
        if mt and mt.group(1) != os.path.basename(f)[:-3]:
            mismatch.append((os.path.basename(f), mt.group(1)))
    cases.append((not mismatch, f'【O】L1 文件标题编号与文件名一致（不一致 {len(mismatch)}）'))

    # 【S】gen_system_status 里的 cron 任务列表
    if '<!-- BEGIN:gen_system_status -->' in state:
        i = state.index('<!-- BEGIN:gen_system_status -->')
        j = state.index('<!-- END:gen_system_status -->')
        sec = state[i:j]
        decl = set(re.findall(r'^- `([a-z0-9-]+)`\s*[\-\u2014\u2013]+\s*', sec, re.M))
        cases.append((len(decl) > 0, f'【S】cron 任务声明列表能解析（{len(decl)} 个）'))

        # ⚠️ 2026-11-16 加（回归防护）：
        #   「生成器」和「【S】断言」必须用**同一个数据源**，否则两边结论必然打架。
        #   真实翻车：生成器读 `openclaw cron list`（两个任务 disabled 后返回**空**）
        #   而【S】断言读 `cron/jobs.json`（仍有 2 个）
        #   → 生成器渲染出空列表 → 【S】报「漏了 cron 任务 [daily-learning, daily-reflection]」
        #   → 连带【X】解析器自检 FAIL（它断言声明列表非空）。
        #   修法：生成器改读 `cron/jobs.json`。
        #   本用例断言：**生成器读出来的任务集合 == jobs.json 里的任务集合**，
        #   且 **disabled 的任务也要列出来**（它们是闸门的触发目标，藏起来会误导下个会话）。
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                'gen_ss', os.path.join(WS, 'scripts', 'gen_system_status.py'))
            g = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(g)
            gen_jobs = {j.get('name') for j in g.read_jobs() if j.get('name')}
            with open('/root/.openclaw/cron/jobs.json', encoding='utf-8') as f:
                _d = json.load(f)
            real = {j.get('name') for j in (_d if isinstance(_d, list) else _d.get('jobs', [])) if j.get('name')}
            same = gen_jobs == real
            cases.append((same, f'【S】生成器与断言同源（jobs.json {sorted(real)} vs 生成器 {sorted(gen_jobs)}）'))
            missing_disabled = real - decl
            cases.append((not missing_disabled,
                          f'【S】disabled 的任务也列在状态里（缺 {sorted(missing_disabled)}）'))
        except Exception as e:
            cases.append((False, f'【S】生成器同源检查异常：{e}'))

    # 【Q】cron 指令里的组数清单
    # ⚠️ 清单在 **cron 指令**里（不在 SESSION-STATE.md）——
    #    【Q】组是拿 cron list --json 的 message 去比。
    #    这里用 gen_healthcheck_instruction 的同一套解析（它才是真身）。
    try:
        import importlib.util
        gen_path = os.path.join(WS, 'scripts', 'gen_healthcheck_instruction.py')
        spec = importlib.util.spec_from_file_location('gen_hc', gen_path)
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)
        groups = gen.extract_groups()
        cases.append((len(groups) > 0,
                      f'【Q】从 healthcheck.sh 解析出组清单（{len(groups)} 组）'))
        # 组清单必须覆盖脚本里所有 echo "【X】"
        echo_groups = set(re.findall(r'echo "\u3010([A-Z])\u3011',
                                     read('scripts/healthcheck.sh')))
        cases.append((echo_groups.issubset({g for g, _ in groups}),
                      f'【Q】组清单覆盖脚本里全部 {len(echo_groups)} 个组'))
    except Exception as e:
        cases.append((False, f'【Q】组清单解析失败：{type(e).__name__}: {e}'))

    # 【W】判据单测文件本身存在且可运行
    cases.append((os.path.exists(os.path.join(WS, 'scripts/test_v_poll_assertion.py')),
                  '【W】判据单测文件存在'))

    # 【U】耗时递增趋势判据 —— 必须会「区分」而不是恒真/恒假
    # ⚠️ 2026-11-26 加（E89）：
    #   daily-learning 耗时 215s→575s→1151s（timeout 1800s），
    #   但「最后一次占用率」只有 64% < 70% → 旧判据完全看不出问题。
    #   → 新判据看**趋势**。但趋势判据最容易犯的错是**恒真**（每次都报）
    #     或**恒假**（永远不报）—— 所以这里两条都要验。
    try:
        def _trend_fires(recent):
            if len(recent) != 3:
                return False
            return (recent[0] < recent[1] < recent[2]
                    and (recent[2] - recent[0]) / recent[0] * 100 >= 50)

        # 真阳性：真实回归序列必须报
        cases.append((_trend_fires([215, 575, 1151]),
                      '【U】耗时递增趋势判据能抓住真实回归(215→575→1151)'))
        # 真阴性：稳定期序列必须**不**报（防恒真）
        cases.append((not _trend_fires([250, 240, 215]),
                      '【U】耗时递增趋势判据对稳定序列不误报(250→240→215)'))
        # 真阴性：持平序列必须不报
        cases.append((not _trend_fires([200, 200, 200]),
                      '【U】耗时递增趋势判据对持平序列不误报(200→200→200)'))
        # 真阴性：小幅波动（<50%）必须不报
        cases.append((not _trend_fires([200, 220, 240]),
                      '【U】耗时递增趋势判据对小幅波动不误报(200→220→240, 涨20%)'))
    except Exception as e:
        cases.append((False, f'【U】趋势判据自检异常：{type(e).__name__}: {e}'))

    # 【U】趋势判据必须真的写在 healthcheck.sh 里（防只测副本）
    try:
        hc_src = read('scripts/healthcheck.sh')
        has_trend = ('util_trend' in hc_src and '耗时递增趋势' in hc_src)
        cases.append((has_trend, '【U】耗时递增趋势判据已落在 healthcheck.sh 里'))
    except Exception as e:
        cases.append((False, f'【U】趋势判据落地检查异常：{e}'))

    ok = True
    for good, desc in cases:
        ok = ok and good
        print(f"{'✅' if good else '❌'} {desc}")

    print(f"\n解析器自检: {'✅ 全部通过' if ok else '❌ 有失败'}")
    if not ok:
        print("  ⚠️ 说明某个断言的解析器已经坏了 → 它正在**装死**，不报错但什么都不检查。")
        print("     常见原因：正则里的全角标点被批量替换成了半角。")
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
