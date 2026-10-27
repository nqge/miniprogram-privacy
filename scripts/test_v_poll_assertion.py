#!/usr/bin/env python3
"""【V】长 poll 断言的判据单测（2026-10-27 12:15 新增）

为什么要有这个文件：
  【V】组的判据逻辑曾经同时犯了两个错，
  而且**两个错都「看起来很合理」**：
    1. 窗口是 3 天，但注释和提示都写「最近 12 小时」→ 说的和做的不一致
    2. 用文件 mtime 过滤窗口 → 一个一直在追加的会话，
       它里面几天前的长 poll 全被算进「最近 12 小时」
  加上 E48 修掉的盲区（无任务标记的会话被整段跳过），
  这一组的判据已经复杂到**必须单测**了。

跑法：python3 scripts/test_v_poll_assertion.py
退出码 0 = 全通过
"""
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile

# 被测逻辑：**直接从 healthcheck.sh 的 PY9 块里抽出来**，不在这里另抄一份。
#
# 为什么要这样：之前这个文件里写的是一份「抄版」判据，
# 而 healthcheck.sh 里的实际代码改成了两行写法
# （`if not job: job = ...`）—— 抄版和真版就**悄悄漂移**了。
# 抄一份判据来测，等于测的是「我以为的判据」而不是「在跑的判据」。
#
# → 改成抽取真身，漂移在物理上不可能发生。
def extract_snippet():
    sh_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'healthcheck.sh')
    with open(sh_path, encoding='utf-8') as fh:
        sh = fh.read()
    try:
        start = sh.index("poll_report=$(python3 - << 'PY9'")
        end = sh.index("\nPY9\n)", start)
    except ValueError:
        raise RuntimeError(
            "在 healthcheck.sh 里找不到 PY9 块 —— "
            "【V】组判据被重构了，这个单测需要同步更新")
    block = sh[start:end]
    # 去掉 shell 包装（`poll_report=$(python3 - << 'PY9' 2>/dev/null`），
    # 只留 python 部分
    body = block.split("2>/dev/null\n", 1)[1]
    return body


def run(sess_dir):
    """跑真身判据，把它的文本输出解析回结构化结果。"""
    code = f"SESSION_DIR = {sess_dir!r}\n" + extract_snippet()
    p = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"判据执行失败: {p.stderr}")
    return parse_report(p.stdout)


def parse_report(out):
    """把 PY9 的文本报告解析成 dict。

    真身判据输出的是给人看的文本（WARN/PASS + 明细），
    单测要断言具体数字，所以这里把它解析回结构化。
    """
    res = {'long_total': 0, 'poll_total': 0, 'detail': {}, 'verdict': 'PASS',
           'guilty': 0, 'pre_rule': 0, 'no_text': 0}
    for line in out.splitlines():
        line = line.strip()
        m = re.match(r'^WARN 最近 (\d+) 小时有 (\d+) 次长 poll', line)
        if m:
            res['verdict'] = 'WARN'
            res['long_total'] = int(m.group(2))
            # 明细在整行的「：」之后 —— 要用**整行**切，不能用 m.group(0)
            # （group(0) 只覆盖到 "长 poll" 为止，明细不在里面）
            tail = line.split('：', 1)[-1] if '：' in line else ''
            for k, v in re.findall(r'([^,\s：]+)×(\d+)', tail):
                res['detail'][k] = int(v)
        m = re.match(r'^PASS 最近 (\d+) 小时没有长 poll（共 (\d+) 次 poll', line)
        if m:
            res['verdict'] = 'PASS'
            res['long_total'] = 0
            res['poll_total'] = int(m.group(2))
        m = re.match(r'^共 (\d+) 次 poll，其中 (\d+) 次过长', line)
        if m:
            res['poll_total'] = int(m.group(1))
        m = re.search(r'其中 (\d+) 次来自', line)
        if m:
            res['unclassified'] = int(m.group(1))
        # E49 的三段拆分
        m = re.search(r'看得到规则却违规：\*{0,2}(\d+) 次', line)
        if m:
            res['guilty'] = int(m.group(1))
        m = re.search(r'规则生效前启动：(\d+) 次', line)
        if m:
            res['pre_rule'] = int(m.group(1))
        m = re.search(r'上下文未见规则原文：(\d+) 次', line)
        if m:
            res['no_text'] = int(m.group(1))
    return res


def mk_session(d, name, polls, when_iso, marker=None, ensure_ascii=True,
               rule_text=None, user_text=None):
    """造一个假的 session jsonl。polls = [(timeout_ms, offset_sec), ...]

    rule_text: 首条 user message 里带上规则原文 → 模拟「看得到规则」
    user_text: 首条 user message 的正文

    ⚠️ offset_sec 是**相对会话启动时间**的秒数。
      如果会话启动时间离 now 超过 12 小时窗口，
      那么「靠 offset 相对偏移」造出来的 poll 一定落在窗口外 →
      判据会返回 0（这个用例就退化成了「窗口外」）。
      需要「会话很老、但 poll 发生在窗口内」时，
      请用 **abs_polls**（绝对时间戳），别靠 offset 硬凑。
    """
    return mk_session_abs(d, name, polls, when_iso, marker=marker,
                          ensure_ascii=ensure_ascii, rule_text=rule_text,
                          user_text=user_text, abs_polls=None)


def mk_session_abs(d, name, polls, when_iso, marker=None, ensure_ascii=True,
                   rule_text=None, user_text=None, abs_polls=None):
    """同 mk_session，但额外支持 abs_polls = [(timeout_ms, abs_ts), ...]。

    abs_polls 里的时间戳是**绝对 epoch 秒**，用来精确控制
    「poll 发生在窗口内」而「会话启动在窗口外」。
    """
    os.makedirs(d, exist_ok=True)
    base_ts = datetime.datetime.fromisoformat(when_iso.replace('Z', '+00:00')).timestamp()
    head_rec = {"type": "session", "id": name, "timestamp": when_iso}
    if marker:
        head_rec["note"] = marker
    body = user_text if user_text is not None else 'Read HEARTBEAT.md'
    if rule_text:
        body = body + '\n\n' + rule_text
    recs = [json.dumps(head_rec, ensure_ascii=ensure_ascii)]
    recs.append(json.dumps({
        "type": "message", "id": "u0", "timestamp": when_iso,
        "message": {"role": "user", "content": [{"type": "text", "text": body}]},
    }, ensure_ascii=ensure_ascii))
    for i, (to, off) in enumerate(polls):
        ts = datetime.datetime.fromtimestamp(
            base_ts + off, datetime.timezone.utc).isoformat().replace('+00:00', 'Z')
        recs.append(json.dumps({
            "type": "message", "id": str(i), "timestamp": ts,
            "message": {"role": "assistant", "content": [{
                "type": "toolCall", "id": f"c{i}", "name": "process",
                "arguments": {"action": "poll", "sessionId": "x", "timeout": to}}]},
        }, ensure_ascii=ensure_ascii))
    for i, (to, abs_ts) in enumerate(abs_polls or []):
        ts = datetime.datetime.fromtimestamp(
            abs_ts, datetime.timezone.utc).isoformat().replace('+00:00', 'Z')
        recs.append(json.dumps({
            "type": "message", "id": f"a{i}", "timestamp": ts,
            "message": {"role": "assistant", "content": [{
                "type": "toolCall", "id": f"ac{i}", "name": "process",
                "arguments": {"action": "poll", "sessionId": "x", "timeout": to}}]},
        }, ensure_ascii=ensure_ascii))
    with open(os.path.join(d, name + '.jsonl'), 'w') as fh:
        fh.write('\n'.join(recs) + '\n')


def main():
    base = tempfile.mkdtemp(prefix='v_poll_test_')
    now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00', 'Z')
    old = (datetime.datetime.now(datetime.timezone.utc)
           - datetime.timedelta(hours=13)).isoformat().replace('+00:00', 'Z')
    MARKER = '每日学习（精简版）'

    cases = []

    d = os.path.join(base, 's1'); mk_session(d, 'a', [(180000, 1), (200000, 2)], now)
    cases.append((run(d), {'long_total': 2, 'poll_total': 2},
                  '现在 + 2 次长 poll → 全部计入'))

    d = os.path.join(base, 's2'); mk_session(d, 'b', [(180000, 1), (200000, 2)], old)
    cases.append((run(d), {'long_total': 0, 'poll_total': 0},
                  '13 小时前 + 2 次长 poll → 窗口外，不计入'))

    d = os.path.join(base, 's3'); mk_session(d, 'c', [(10000, 1), (15000, 2)], now)
    cases.append((run(d), {'long_total': 0, 'poll_total': 2},
                  '现在 + 2 次短 poll → 不算长 poll（不误报）'))

    d = os.path.join(base, 's4'); mk_session(d, 'd', [(180000, 1)], now)
    cases.append((run(d), {'long_total': 1, 'poll_total': 1},
                  '无任务标记的普通心跳会话 → 也要计入（E48 修掉的盲区）'))

    d = os.path.join(base, 's5'); mk_session(d, 'e', [(180000, 1)], now, marker=MARKER)
    r = run(d)
    cases.append((r, {'long_total': 1, 'poll_total': 1}, '转义标记 → 计入'))
    cases.append((r, {'detail_job': 'daily-learning'}, '转义标记 → 归类到 daily-learning'))

    d = os.path.join(base, 's6'); mk_session(d, 'f', [(180000, 1)], now,
                                              marker=MARKER, ensure_ascii=False)
    r = run(d)
    cases.append((r, {'long_total': 1, 'poll_total': 1}, '原始中文标记 → 计入'))
    cases.append((r, {'detail_job': 'daily-learning'}, '原始中文标记 → 归类到 daily-learning'))

    # 场景6b：**半角括号**的标记 → 也要归类到 daily-learning
    # （2026-10-27 实测踩过：编辑脚本时全角括号被写成半角，
    #   标记字符串「每日学习（精简版）」变成「每日学习(精简版)」，
    #   → 真实 session 全部匹配不上，分类静默塌成「未分类」——
    #   **不报错**，只是所有会话都掉进同一个桶）
    d = os.path.join(base, 's6b')
    mk_session(d, 'g', [(180000, 1)], now,
               marker='每日学习(精简版)', ensure_ascii=False)
    r = run(d)
    cases.append((r, {'detail_job': 'daily-learning'},
                  '半角括号标记 → 仍归类到 daily-learning（标点形式无关）'))

    # 时间戳缺失的记录 → 应被忽略而不是崩
    d = os.path.join(base, 's7'); mk_session(d, 'g', [(180000, 1)], now)
    with open(os.path.join(d, 'h.jsonl'), 'w') as fh:
        fh.write(json.dumps({"type": "message", "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": "c0", "name": "process",
             "arguments": {"action": "poll", "sessionId": "x", "timeout": 180000}}]}}) + '\n')
    cases.append((run(d), {'long_total': 1, 'poll_total': 1},
                  '无时间戳的记录 → 忽略不崩，且不影响正常统计'))

    # ---- E49：规则可见性拆分 ----
    # 场景8：会话上下文里**有**规则原文 + 长 poll → 应算「看得到规则却违规」
    d = os.path.join(base, 's8')
    mk_session(d, 'saw', [(180000, 1)], now, rule_text='⏱️ 等长任务：poll 会等满')
    r = run(d)
    cases.append((r, {'guilty': 1}, '上下文含规则原文 → 算「看得到规则却违规」'))

    # 场景9：规则写入**之前**启动的会话 + 长 poll → 不算它无视
    # 注意：这个会话必须**在 12 小时窗口内**有 poll 才会被统计，
    # 但它的**启动时间**要早于规则写入时间 —— 这正是要验证的区分点。
    # 用「规则写入时间 - 1 小时」作为启动时间，poll 落在窗口内。
    pre_start = None
    try:
        r = subprocess.run(
            ['git', '-C', os.path.dirname(os.path.abspath(__file__)) + '/..',
             'log', '-1', '--format=%ct', '-S', '等长任务', '--', 'TOOLS.md'],
            capture_output=True, text=True, timeout=30)
        v = (r.stdout or '').strip().splitlines()
        if v and v[0].isdigit():
            pre_start = datetime.datetime.fromtimestamp(int(v[0]), datetime.timezone.utc)
    except Exception:
        pass
    if pre_start is None:
        # 拿不到规则写入时间 → 退化用「13 小时前」，但那样 poll 会在窗口外
        # → 这个用例就退化成「窗口外」，用 pre_rule=0 表示
        pre_iso = old
        expect_pre = 0
    else:
        pre_iso = (pre_start - datetime.timedelta(minutes=5)).isoformat().replace('+00:00', 'Z')
        expect_pre = 1
    d = os.path.join(base, 's9')
    # ⚠️ 2026-10-28 修正：这个 poll 的时间戳**不能**相对会话启动时间算。
    # 规则写入时间距今可能已经**超过 12 小时窗口**（实测：21 小时），
    # 那么「会话启动 + offset」无论 offset 取多大都落不进窗口
    # （offset 太小 → 窗口外；offset 够大 → poll 跑到未来，
    #   但会话启动时间也会跟着往后推，反而不再早于规则写入时间）。
    # → 必须用**绝对时间戳**把 poll 放到窗口内（now - 60s），
    #    而会话启动时间单独保持在规则写入时间之前。
    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
    mk_session_abs(d, 'pre', [], pre_iso,
                   abs_polls=[(180000, now_ts - 60)])
    r = run(d)
    cases.append((r, {'pre_rule': expect_pre, 'guilty': 0},
                  '规则生效前启动的会话 → 不算「无视规则」'))

    # 场景10：启动晚于规则，但上下文里没有规则原文 → 单独归类，不算无视
    d = os.path.join(base, 's10')
    mk_session(d, 'notext', [(180000, 1)], now)
    r = run(d)
    cases.append((r, {'no_text': 1, 'guilty': 0},
                  '启动晚于规则但上下文无规则原文 → 单独归类，不算无视'))

    ok = True
    for got, want, desc in cases:
        good = True
        if 'long_total' in want:
            good = got['long_total'] == want['long_total']
        if 'poll_total' in want:
            good = good and got['poll_total'] == want['poll_total']
        if 'detail_job' in want:
            good = good and got['detail'].get(want['detail_job']) == got['long_total']
        # E49 的三段拆分
        for key in ('guilty', 'pre_rule', 'no_text'):
            if key in want:
                good = good and got.get(key, 0) == want[key]
        ok = ok and good
        print(f"{'✅' if good else '❌'} {desc}")
        if not good:
            print(f"     实际: {got}")

    print(f"\n单测结果: {'✅ 全部通过' if ok else '❌ 有失败'}")
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
