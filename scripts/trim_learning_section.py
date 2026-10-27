#!/usr/bin/env python3
"""每日学习小节的**长度校正器** —— 把 agent 的「反复试字数」变成一次确定性操作。

2026-10-27 09:20 补（E44）：
09:00 那次 daily-learning 跑了 **772s**（timeout 900s，余量只剩 14%），
翻会话时间线发现根因不是任务重、不是 provider 慢，而是：

  **agent 在手工迭代字数**：272 → 259 → 233 → 232 → 227 → 225 → 222 → 222
  → 223 → 221 → 219 → 190 → 190
  = **13 轮**，每轮 = 一次完整 LLM 推理（上下文 ~58K tokens，带缓存也要 20-30s）
  → 光这一件事就吃掉了几百秒

为什么会这样：长度约束写的是「100-200 字」「写完自己数一遍，超了就砍」——
但**「自己数一遍」是由 LLM 完成的**，而 LLM 数不准中文汉字数，
所以它只能**写一版→数一遍→不对→再改→再数**，
形成一个**靠对话收敛**的循环，而收敛速度取决于模型，不是取决于算法。

**修法：把计数和裁剪交给确定性脚本**。
agent 写完草稿 → 跑一次本脚本 → 拿到「合规版本」或「该砍哪几段」→
写进文件。**1-2 次调用收敛**，而不是 13 次。

用法：
  # 检查长度（不改文件）
  python3 scripts/trim_learning_section.py --check memory/2026-10-27.md

  # 从草稿生成合规版本（写入 stdout）
  python3 scripts/trim_learning_section.py --from /tmp/draft.md

  # 直接替换文件里的学习小节
  python3 scripts/trim_learning_section.py --apply memory/2026-10-27.md --from /tmp/draft.md

退出码：0 = 合规；1 = 仍超标（且无法自动裁到合规）
"""

import argparse
import re
import sys
from pathlib import Path

MIN_ZH = 100
MAX_ZH = 200

# 「📚 每日学习」「🪞 每日反思」小节
SEC_RE = re.compile(
    r'^(## (?:📚 每日学习|🪞 每日反思)[^\n]*\n)(.*?)(?=^#{1,6} |\Z)',
    re.M | re.S)

# 反思小节的约束更宽（healthcheck 的【F】组对反思用 400 上限）——
# 三个问题各要一段展开，200 汉字不够。
LIMITS = {
    '📚 每日学习': (100, 200),
    '🪞 每日反思': (100, 400),
}


def limit_for(kind: str):
    for k, v in LIMITS.items():
        if k in kind:
            return v
    return (MIN_ZH, MAX_ZH)


def zh_count(text: str) -> int:
    """汉字数 —— 中文语境里「字」= 汉字。

    ⚠️ 不要改成 len(text)：那是「所有字符」（含标点/英文/空白），
    与任务指令里的「100-200 字」不是一回事（见 E42）。
    """
    return len(re.findall(r'[一-鿿]', text))


def split_paragraphs(body: str):
    """把正文切成 (标题行, 段落列表, 尾部元信息)。"""
    lines = body.split('\n')
    head = []
    i = 0
    # 首行可能是 '> 本条讲：**关键词**'
    while i < len(lines) and (not lines[i].strip() or lines[i].lstrip().startswith('>')):
        head.append(lines[i])
        i += 1
    paras = []
    cur = []
    for ln in lines[i:]:
        if ln.strip() == '':
            if cur:
                paras.append('\n'.join(cur))
                cur = []
            continue
        cur.append(ln)
    if cur:
        paras.append('\n'.join(cur))
    return head, paras


def trim_body(body: str, kind: str = '📚 每日学习'):
    """把正文裁到 <= MAX_ZH 汉字。返回 (新正文, 说明)。

    策略（确定性，不用 LLM）：
      1. 先去掉「元信息尾巴」（_最后更新: ..._ / _已提升至 L1: E<N>_）—— 这些不算正文
      2. **保留首段（铺垫）和末段（可执行的对策）**，删中间段
         —— ⚠️ 早先版本从尾部删，结果保住了铺垫却删掉了**核心论点**
            （教训通常在中间那段）—— 这正是 LLM 手工裁剪会犯的错，
            换成脚本后如果还犯，就白自动化了
      3. 还超标就按句截断中间剩余段落
    """
    # 去掉尾部元信息行（_..._ 单行）
    body = re.sub(r'\n_[^\n_]*_\s*$', '', body)
    lo, hi = limit_for(kind)
    head, paras = split_paragraphs(body)
    if not paras:
        return body, "无正文段落"

    removed = []
    # 首行「> 本条讲：**关键词**」也占字数预算 —— 早先版本只算正文段落，
    # 导致「段落合起来合规 + 首行 25 字 = 实际超标」（206 vs 200）
    head_zh = zh_count('\n'.join(head))
    budget = max(30, hi - head_zh)

    # 识别「可立即执行的对策」段（必须保留）
    def _is_action(p):
        return ('对策' in p) or ('下一步' in p) or ('立即执行' in p)

    # 核心论点 = **最长的非对策段**（教训通常在最长那段）
    # 铺垫 = 较短的段（信息量最低，先删）
    while len(paras) > 2 and zh_count('\n\n'.join(paras)) > budget:
        action_idx = next((i for i, p in enumerate(paras) if _is_action(p)), None)
        # 候选删除：非对策段里**最短**的（铺垫信息量最低）
        cands = [(zh_count(p), i) for i, p in enumerate(paras) if i != action_idx]
        if not cands:
            break
        _, idx = min(cands)
        dropped = paras.pop(idx)
        removed.append(dropped.strip()[:40])

    if paras:
        # 段落删完仍超标 -> 按句截断**最长**的段落（保留信息量最大的那部分）
        # 反复截，直到整体 <= MAX_ZH
        def _total():
            return zh_count('\n\n'.join(paras))

        def _truncate_longest():
            """把最长的非对策段按句砍掉尾部。返回是否成功砍掉一句。"""
            action_idx = next((i for i, p in enumerate(paras) if _is_action(p)), None)
            cands = [(zh_count(p), i) for i, p in enumerate(paras) if i != action_idx]
            if not cands:
                return False
            _, idx = max(cands)
            p = paras[idx]
            if zh_count(p) <= 10:
                return False
            sents = re.split(r'(?<=[。！？；])', p)
            if len(sents) <= 1:
                # 没有句号 -> 直接按字符砍
                paras[idx] = p[:max(10, int(len(p) * 0.7))]
                return True
            keep = ''
            for s in sents:
                if zh_count(keep + s) > max(10, zh_count(p) - 20):
                    break
                keep += s
            if not keep.strip():
                keep = sents[0][:max(10, len(sents[0]) // 2)]
            paras[idx] = keep.rstrip()
            return True

        guard = 0
        while _total() > budget and guard < 200:
            guard += 1
            if not _truncate_longest():
                break
        joined = '\n\n'.join(paras)
    else:
        joined = ''

    new_body = '\n'.join(head).rstrip('\n') + '\n\n' + joined + '\n'
    note = f"删掉 {len(removed)} 段" if removed else "无需裁剪"
    return new_body, note


def check_file(path):
    txt = Path(path).read_text(encoding='utf-8')
    m = SEC_RE.search(txt)
    if not m:
        print(f"FAIL {path} 里找不到「每日学习/每日反思」小节")
        return 1
    n = zh_count(m.group(2))
    lo, hi = limit_for(m.group(1))
    ok = lo <= n <= hi
    flag = "✅" if ok else ("⚠️  偏短" if n < lo else "❌ 超长")
    print(f"{flag} {Path(path).name}: {n} 汉字（约束 {lo}-{hi}）")
    return 0 if ok else 1


def apply_file(path, draft_path):
    p = Path(path)
    txt = p.read_text(encoding='utf-8')
    m = SEC_RE.search(txt)
    if not m:
        print(f"FAIL {path} 里找不到目标小节")
        return 1
    draft = Path(draft_path).read_text(encoding='utf-8')
    # 草稿可能自带标题行，去掉，只留正文
    dm = re.match(r'^##\s+(?:📚 每日学习|🪞 每日反思)[^\n]*\n(.*)$', draft, re.S)
    body = dm.group(1) if dm else draft
    n_before = zh_count(body)
    kind = m.group(1)
    lo, hi = limit_for(kind)
    new_body, note = trim_body(body, kind)
    n_after = zh_count(new_body)
    print(f"草稿 {n_before} 汉字 -> 处理后 {n_after} 汉字（{note}）")
    if n_after > hi:
        print(f"❌ 仍超标（{n_after} > {MAX_ZH}），请手工再砍")
        return 1
    new_txt = txt[:m.start(2)] + new_body + txt[m.end(2):]
    p.write_text(new_txt, encoding='utf-8')
    print(f"✅ 已写入 {path}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('file', nargs='?', help='每日笔记文件')
    ap.add_argument('--check', action='store_true', help='只检查长度')
    ap.add_argument('--from', dest='draft', help='草稿文件')
    ap.add_argument('--apply', action='store_true', help='把处理后的草稿写回文件')
    ap.add_argument('--kind', default='📚 每日学习', help='小节类型：📚 每日学习 / 🪞 每日反思')
    a = ap.parse_args()

    if not a.file and not a.draft:
        ap.print_help()
        return 1
    if a.draft and a.apply:
        return apply_file(a.file, a.draft)
    if a.draft:
        body = Path(a.draft).read_text(encoding='utf-8')
        dm = re.match(r'^##\s+(?:📚 每日学习|🪞 每日反思)[^\n]*\n(.*)$', body, re.S)
        if dm:
            body = dm.group(1)
        out, note = trim_body(body, a.kind or '📚 每日学习')
        print(out)
        print(f"--- {zh_count(out)} 汉字（{note}）", file=sys.stderr)
        return 0
    return check_file(a.file)


if __name__ == '__main__':
    sys.exit(main())
