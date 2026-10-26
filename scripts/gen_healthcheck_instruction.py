#!/usr/bin/env python3
"""从 healthcheck.sh 自动生成 cron 指令里的「FAIL 含义」清单。

2026-10-27 补（E20/E31 的又一次实例）：
cron 指令里手写的「13 组断言 A–M」在加了 N/O/P 三组后**没同步** ——
明早 07:00 真出故障时，agent 拿到的是一份**过期的手册**。
（这正是 E20「验证了脚本却没验证指令」的翻版。）

解法：指令清单**由脚本生成**，不手写。
- 每次改 healthcheck.sh 后重跑本脚本并用 CLI 更新 cron
- 或者让 healthcheck.sh 自己在运行时校验指令是否过期

用法：
  python3 scripts/gen_healthcheck_instruction.py          # 打印生成的清单
  python3 scripts/gen_healthcheck_instruction.py --update # 直接更新 cron 指令
"""
import re, json, subprocess, sys
from pathlib import Path

WS = Path('/root/.openclaw/workspace')
SH = WS / 'scripts/healthcheck.sh'
CRON_ID = '5415b59e-2d20-44b0-bc4e-1a65501776f4'   # healthcheck

# 长期项（脚本里会自动归入「等你」组，不触发告警）
LONG_TERM = {'E', 'M'}

# 人工补充的说明（脚本里的组名太简略时用）
NOTES = {
    'A': '飞书 token / GitHub 额度（认证 5000 次/小时）/ 嵌入代理',
    'B': 'gateway / embed-proxy / log-cleanup.timer / quick-health.timer',
    'C': '索引完整性 + Embeddings 可用',
    'D': 'announce 型必须 delivered=True（这是曾经静默失效的环节）',
    'E': 'GIT_CREDENTIALS.md / gh hosts.yml（**需牛哥哥本人吊销重签，不要自己改**）',
    'F': '「每日学习」小节是否只有 1 个',
    'G': '本地与远端一致',
    'H': '真实跑一次记忆检索 + API',
    'I': '茶馆蒸馏进度 + 队列自洽性',
    'J': 'L1 经验库活跃度 + 每条经验是否都能被检索到',
    'K': '根目录整洁度',
    'L': 'SESSION-STATE 头部日期',
    'M': '只有一个 provider 时，fallback 全在同一账号下 → provider 级故障时兜底无效',
    'N': '单个记忆文件 > 300KB / 索引块垄断 > 60% / memory/ 目录 > 200MB',
    'O': 'MEMORY.md 与 L1 主库声明的条目数是否与实际文件数一致',
    'P': '某个每日笔记里的小节日期晚于文件名（跨零点写错文件）',
    'Q': 'cron 指令里的 FAIL 含义清单是否与 healthcheck.sh 的实际组数一致（防过期手册）',
    'R': 'cron 任务超时余量：某任务最近因超时失败（脚本 0.2s 但 agent turn 跑了 300s，'
         '时间全耗在 LLM 推理）→ 提高该任务 timeoutSeconds；手写的 timeout 值彼此不可比',
}


def extract_groups():
    txt = SH.read_text(encoding='utf-8')
    found = re.findall(r'echo "【([A-Z])】([^"]+)"', txt)
    order = {g: i for i, (g, _) in enumerate(found)}
    # 按字母排序输出（和脚本里的顺序无关，但可读性好）
    return sorted(found, key=lambda x: x[0])


def build_block(groups):
    n = len(groups)
    lines = [
        f'**当前 FAIL 的 {n} 类含义**（由 `scripts/gen_healthcheck_instruction.py` '
        f'从 healthcheck.sh 自动生成，**不要手写**）：',
        '（以下是「可处理」类的含义；E 类明文凭证、M 类 provider 韧性属长期项，',
        '已在脚本里自动归入「等你」组，不触发告警）',
    ]
    for g, name in groups:
        note = NOTES.get(g, name)
        lines.append(f'- {g} {note}')
    return '\n'.join(lines)


def get_current_msg():
    r = subprocess.run(['openclaw', 'cron', 'list', '--json'],
                       capture_output=True, text=True, timeout=60)
    jobs, _ = json.JSONDecoder().raw_decode(r.stdout[r.stdout.find('['):])
    for j in jobs:
        if j.get('id') == CRON_ID:
            return (j.get('payload') or {}).get('message', '')
    return None


def update_cron(new_msg):
    r = subprocess.run(['openclaw', 'cron', 'edit', CRON_ID, '--message', new_msg],
                       capture_output=True, text=True, timeout=120)
    return r.returncode == 0


def main():
    groups = extract_groups()
    block = build_block(groups)
    if '--update' in sys.argv:
        cur = get_current_msg()
        if cur is None:
            print('找不到 healthcheck cron 任务')
            sys.exit(1)
        # 替换掉旧的「当前 FAIL 的 N 类含义」整块
        start = cur.find('**当前 FAIL 的')
        if start == -1:
            print('指令里找不到「当前 FAIL 的」块')
            sys.exit(1)
        end = cur.find('\n\n', cur.find('- M ', start))
        if end == -1:
            end = len(cur)
        new = cur[:start] + block + cur[end:]
        ok = update_cron(new)
        print(f'{"✅ 已更新" if ok else "❌ 失败"}（{len(groups)} 组）')
    else:
        print(block)


if __name__ == '__main__':
    main()
