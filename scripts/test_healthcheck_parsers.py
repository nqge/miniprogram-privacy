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
