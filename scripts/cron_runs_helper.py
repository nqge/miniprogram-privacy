"""读取 cron 运行历史 —— 直接读 JSONL，绕开 Node 进程启动。

2026-10-29 新增（性能）:
    `openclaw cron runs --id <id>` 每次都要拉起一个 Node 进程
    （本机 2 核，实测 **~4 秒/次**）。healthcheck 里有两个块在**循环里**调它
    （5 个 cron 任务 × 2 处 = 10 次 → ~40 秒纯等待）。

    而运行历史本来就是 **JSONL-backed**（`openclaw cron runs --help` 明说），
    直接读文件即可，**0.4 秒读完 10 个文件**。

    ⚠️ 字段必须与 CLI 返回一致，否则断言会拿到空数据却「看起来通过」。
       —— 这就是「断言装死」的经典形态。已加自检（见 `self_test()`）。
"""

import json
import os

RUNS_DIR = '/root/.openclaw/cron/runs'


def read_cron_runs(job_id, limit=20):
    """读某个 job 的运行历史，返回 newest-first 的 entries 列表。

    与 `openclaw cron runs --id <id> --limit <n>` 的 entries 语义对齐:
      - 只取 action == 'finished' 的记录（CLI 也是这么筛的）
      - ts 降序（新的在前）
      - 最多 limit 条
    """
    path = os.path.join(RUNS_DIR, f'{job_id}.jsonl')
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get('action') != 'finished':
                continue
            out.append(e)
    out.sort(key=lambda e: e.get('ts') or 0, reverse=True)
    return out[:limit]


def read_all_cron_runs(limit_per_job=20, jobs=None):
    """一次读所有 job 的运行历史（用于替代循环里的多次 CLI 调用）。

    jobs: 可选，list of job_id；不给就读 RUNS_DIR 下全部。
    """
    if jobs is None:
        try:
            jobs = [f[:-6] for f in os.listdir(RUNS_DIR) if f.endswith('.jsonl')]
        except Exception:
            jobs = []
    res = {}
    for jid in jobs:
        try:
            res[jid] = read_cron_runs(jid, limit_per_job)
        except Exception:
            res[jid] = []
    return res


def self_test():
    """自检: 直接读 JSONL 必须与 CLI 返回一致（否则断言会装死）。"""
    import subprocess
    jid = None
    try:
        d = json.load(open('/root/.openclaw/cron/jobs.json', encoding='utf-8'))
        jobs = d['jobs'] if isinstance(d, dict) and 'jobs' in d else d
        enabled = [j for j in jobs if j.get('enabled') and j.get('payload', {}).get('kind') == 'agentTurn']
        if enabled:
            jid = enabled[0]['id']
    except Exception:
        pass
    if not jid:
        return False, 'no enabled job found'

    mine = read_cron_runs(jid, 5)
    if not mine:
        return False, f'jsonl empty for {jid} (but CLI would have data?)'

    r = subprocess.run(['openclaw', 'cron', 'runs', '--id', jid, '--limit', '5'],
                       capture_output=True, text=True, timeout=60)
    try:
        cli = (json.loads(r.stdout) or {}).get('entries', [])
    except Exception as e:
        return False, f'cli parse failed: {e}'

    if len(cli) != len(mine):
        return False, f'count mismatch: cli={len(cli)} jsonl={len(mine)}'

    # 比对关键字段
    for a, b in zip(cli, mine):
        for k in ('ts', 'status', 'durationMs', 'sessionId'):
            if a.get(k) != b.get(k):
                return False, f'field {k} mismatch: cli={a.get(k)} jsonl={b.get(k)}'
    return True, f'ok: {len(mine)} entries match'


if __name__ == '__main__':
    ok, msg = self_test()
    print(('PASS' if ok else 'FAIL'), msg)
    raise SystemExit(0 if ok else 1)
