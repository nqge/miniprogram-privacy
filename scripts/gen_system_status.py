#!/usr/bin/env python3
"""从真实系统生成 SESSION-STATE.md 的「📋 系统状态」小节。

2026-10-27 补（E34 的延伸应用）：
SESSION-STATE.md 头部的「系统状态」小节是**手写的**，里面有大量过期内容：
- 已删除的 provider（glmcode/arkcode/kimicode）
- 已删除的 cron 任务（teahouse-check / dream-cycle-v3）
- 已移除的 plugin（qqbot / ddingtalk）
- 「首次自动调度验证待 10-26」这种早就过期的待办

而 SESSION-STATE.md 的头部是**下一个会话最先读的地方** ——
它过期 = 下一个会话被误导。

解法：**从真实系统生成**，不手写。
- providers / cron 任务 / plugins 都从 openclaw.json + cron list 实时读
- 每次改配置后重跑 `--update`

用法：
  python3 scripts/gen_system_status.py           # 打印生成的小节
  python3 scripts/gen_system_status.py --update  # 写回 SESSION-STATE.md
"""
import json, re, subprocess, sys
from pathlib import Path

WS = Path('/root/.openclaw/workspace')
CONFIG = Path('/root/.openclaw/openclaw.json')
STATE = WS / 'SESSION-STATE.md'
HEADER = '## 📋 系统状态'
BEGIN = '<!-- BEGIN:gen_system_status -->'
END = '<!-- END:gen_system_status -->'


def read_config():
    return json.load(open(CONFIG, encoding='utf-8'))


AGENT_MODELS = Path('/root/.openclaw/agents/main/agent/models.json')


def read_agent_catalog():
    """读取 agent 级模型目录（models.json）。

    2026-10-28 补：`openclaw.json` 的 `models.providers` 只是**全局**配置；
    agent 级目录 `agents/<id>/agent/models.json` 会在 `models.mode=merge` 下
    **叠加**到最终目录上，而 `openclaw models list` 的默认输出只看
    default/fallback 标签 —— 于是「本地配置里有、运行时也能选中、
    但体检脚本看不见」的 provider 会成为盲区
    （当时 glmcode/arkcode/kimicode 三家就是这样被漏掉的）。
    """
    try:
        d = json.load(open(AGENT_MODELS, encoding='utf-8'))
        return list((d.get('providers') or {}).keys())
    except Exception:
        return []


def read_jobs():
    r = subprocess.run(['openclaw', 'cron', 'list', '--json'],
                       capture_output=True, text=True, timeout=60)
    try:
        jobs, _ = json.JSONDecoder().raw_decode(r.stdout[r.stdout.find('['):])
        return jobs
    except Exception:
        return []


def build():
    c = read_config()
    jobs = read_jobs()
    L = []
    L.append(HEADER)
    L.append('')
    L.append('> ⚠️ 本节由 `scripts/gen_system_status.py` 从**真实系统**生成 —— 不要手改。')
    L.append('> 改配置后重跑 `python3 scripts/gen_system_status.py --update`。')
    L.append('')
    L.append(f'**providers**: {", ".join(c["models"]["providers"].keys())}')

    # agent 级目录里的 provider（可能与全局不一致 —— 这就是盲区）
    agent_ps = read_agent_catalog()
    global_ps = list(c["models"]["providers"].keys())
    extra = [p for p in agent_ps if p not in global_ps]
    if extra:
        L.append('')
        L.append(f'**agent 级目录另有 provider**: {", ".join(extra)}'
                 f'（来自 `agents/main/agent/models.json`，在 `models.mode=merge` 下会叠加进最终目录）')
        L.append('')
        L.append('> ⚠️ 上面这些 provider **不参与** `primary`/`fallbacks` 路由；'
                 '若其凭证已失效（如 401/403/余额不足），属于**残留凭证**，'
                 '需人工确认后清理或充值 —— 别只信 `openclaw.json` 的 provider 列表。')
    L.append('')
    L.append('**cron 任务**:')
    for j in jobs:
        st = j.get('state') or {}
        p = j.get('payload') or {}
        sch = j.get('schedule') or {}
        expr = sch.get('expr') or sch.get('at') or ''
        last = st.get('lastRunStatus', '?')
        dur = (st.get('lastDurationMs') or 0) / 1000
        L.append(f'- `{j["name"]}` — {sch.get("kind", "")} {expr} · '
                 f'timeout {p.get("timeoutSeconds")}s · last: {last} ({dur:.0f}s)')
    L.append('')
    L.append('**plugins**: ' + ', '.join(c.get('plugins', {}).get('entries', {}).keys()))
    L.append('')
    return '\n'.join(L)


def update():
    block = build()
    txt = STATE.read_text(encoding='utf-8')
    # 优先用标记；没有标记就替换「## 📋 系统状态」到下一个 ## 之间
    if BEGIN in txt and END in txt:
        new = re.sub(re.escape(BEGIN) + r'.*?' + re.escape(END),
                     BEGIN + '\n' + block + END, txt, flags=re.S)
    else:
        i = txt.find(HEADER)
        if i == -1:
            print(f'找不到「{HEADER}」小节')
            return 1
        j = txt.find('\n## ', i + 1)
        if j == -1:
            j = len(txt)
        new = txt[:i] + BEGIN + '\n' + block + END + '\n' + txt[j:]
    STATE.write_text(new, encoding='utf-8')
    print('✅ SESSION-STATE.md 系统状态已更新')
    return 0


def main():
    if '--update' in sys.argv:
        sys.exit(update())
    print(build())


if __name__ == '__main__':
    main()
