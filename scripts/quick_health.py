#!/usr/bin/env python3
"""快速健康子集 —— 用于高频（每 30 分钟）检查。

为什么需要它：
  完整巡检（scripts/healthcheck.sh）要 72 秒，其中包含 git fetch、扫描大日志文件、
  真实跑一次记忆检索等重活 —— 适合每日一次，不适合高频。
  但**服务挂掉、代理挂掉、索引失效**这类问题，越早发现越好。
  凌晨 3 点服务挂了，等到早上 07:00 才知道 = 4 小时盲区。

设计：
  只跑「便宜且能立刻发现故障」的部分（无 git fetch / 无大文件扫描 / 无模型调用）：
    - 外部 API 连通性（飞书 token、GitHub API、嵌入代理 health）
    - systemd 服务状态（gateway / embed-proxy / log-cleanup.timer）
    - 记忆索引状态（读 sqlite，不调 embedding）
    - 定时任务投递结果（读 gateway 的 cron list）
  完整巡检仍由每日 07:00 的 healthcheck 负责。

退出码：0 = 全通过；1 = 有 FAIL
"""

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

WS = "/root/.openclaw/workspace"
FAILED = []
PASSED = 0

# 本次运行的唯一标识 + 开始时刻（2026-10-26 加，理由见 _write_result_file 的注释）
import datetime as _dt
import uuid as _uuid
_RUN_ID = _uuid.uuid4().hex[:12]
_RUN_STARTED_AT = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# systemd 的 PATH 不含 /root/.local/share/pnpm → openclaw 找不到
# （2026-10-26 实测：同一脚本在交互 shell 正常，systemd 下报「无法读取 cron 状态」）
OPENCLAW = "/root/.local/share/pnpm/openclaw"


def ok(msg):
    global PASSED
    PASSED += 1
    print(f"  ✅ PASS  {msg}")


def bad(msg):
    FAILED.append(msg)
    print(f"  ❌ FAIL  {msg}")


def cmd(args, timeout=30):
    try:
        # 日志降噪：每次 openclaw CLI 调用都会重新注册全部插件，
        # INFO 级打 6+ 行，占当日日志 87%（实测 27008 → 5735 字节/次）。
        # 用环境变量而非改配置文件（改 openclaw.json 会触发 loadConfig 递归）。
        env = dict(os.environ)
        env.setdefault("OPENCLAW_LOG_LEVEL", "warn")
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)
        return r.stdout, r.returncode
    except Exception:
        return "", 1


def check_external_apis():
    print("【外部 API】")
    # 嵌入代理（最关键 —— 它挂了记忆检索就死）
    try:
        with urllib.request.urlopen("http://127.0.0.1:8799/health", timeout=8) as r:
            body = r.read().decode()
        if '"ok":true' in body:
            ok("嵌入代理可用")
        else:
            bad(f"嵌入代理返回异常：{body[:80]}")
    except Exception as e:
        bad(f"嵌入代理不可达 — 记忆检索会失效（{type(e).__name__}）")

    # 飞书 token
    try:
        cfg = json.load(open("/root/.openclaw/openclaw.json", encoding="utf-8"))
        f = cfg["channels"]["feishu"]
        req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps({"app_id": f["appId"], "app_secret": f["appSecret"]}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        d = json.load(urllib.request.urlopen(req, timeout=12))
        if d.get("tenant_access_token"):
            ok("飞书 API 可用")
        else:
            bad(f"飞书 token 获取失败：{d.get('msg')}")
    except Exception as e:
        bad(f"飞书 API 不可用（{type(e).__name__}）")

    # GitHub（茶馆监控依赖）—— 用认证请求，匿名额度只有 60 次/小时/IP
    try:
        import sys as _sys
        _sys.path.insert(0, f"{WS}/scripts")
        from gh_auth import github_headers
        gh_headers = github_headers()
    except Exception:
        gh_headers = {"User-Agent": "openclaw-healthcheck"}
    try:
        req = urllib.request.Request(
            "https://api.github.com/rate_limit", headers=gh_headers
        )
        with urllib.request.urlopen(req, timeout=12) as r:
            d = json.loads(r.read().decode())
        core = (d.get("resources") or {}).get("core") or {}
        remaining, limit = core.get("remaining"), core.get("limit")
        if limit and remaining is not None and remaining < 5:
            bad(f"GitHub API 额度将耗尽（{remaining}/{limit}）— 茶馆监控可能拉不到新讨论")
        else:
            ok(f"GitHub API 可用（额度 {remaining}/{limit}）")
    except urllib.error.HTTPError as e:
        # 403 rate limit ≠ 不可达 —— 要说清楚是限流
        if e.code == 403:
            bad("GitHub API 被限流（403 rate limit）— 茶馆监控会拉不到新讨论")
        else:
            bad(f"GitHub API 异常（HTTP {e.code}）")
    except Exception as e:
        bad(f"GitHub API 不可达 — 茶馆监控会失败（{type(e).__name__}）")


def check_services():
    print("【服务状态】")
    for svc in (
        "openclaw-gateway.service",
        "openclaw-embed-proxy.service",
        "openclaw-log-cleanup.timer",
    ):
        out, _ = cmd(["systemctl", "--user", "is-active", svc], timeout=10)
        state = out.strip()
        if state == "active":
            ok(f"{svc} active")
        else:
            bad(f"{svc} = {state or 'unknown'}")


def check_memory_index():
    print("【记忆索引】")
    # 读 sqlite 而不是调 embedding —— 便宜且不依赖代理
    import sqlite3

    db_path = "/root/.openclaw/memory/main.sqlite"
    if not os.path.exists(db_path):
        bad("记忆库文件不存在")
        return
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM chunks")
        chunks = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT path) FROM chunks")
        files = cur.fetchone()[0]
        con.close()
        if chunks > 0:
            ok(f"记忆索引 {files} 文件 / {chunks} 块")
        else:
            bad("记忆索引为空 —— 检索会返回空结果")
    except Exception as e:
        bad(f"记忆库读取失败（{type(e).__name__}）")


def check_cron_delivery():
    print("【定时任务投递】")
    out, _ = cmd([OPENCLAW, "cron", "list", "--json"], timeout=60)
    i = out.find("{")
    if i < 0:
        bad("无法读取 cron 状态")
        return
    try:
        d = json.loads(out[i:])
    except Exception:
        bad("cron 状态解析失败")
        return
    jobs = d if isinstance(d, list) else d.get("jobs", d.get("data", []))
    checked = 0
    for j in jobs:
        name = j.get("name", "?")
        dl = j.get("delivery", {}) or {}
        st = j.get("state", {}) or {}
        rs = st.get("lastRunStatus")
        if rs is None and not st.get("lastRunAtMs"):
            continue  # 从未跑过的新任务
        checked += 1
        if rs != "ok":
            bad(f"{name} 运行失败（lastRunStatus={rs}）")
        elif dl.get("mode") == "announce" and not st.get("lastDelivered"):
            bad(f"{name} 投递未送达（delivery={st.get('lastDeliveryStatus')}）")
    if checked:
        ok(f"{checked} 个定时任务状态正常")


def check_provider_resilience():
    """fallback 链是否真的跨 provider —— 单 provider 时 fallback 韧性为 0。

    2026-10-26 实测：minimax 达到 Token Plan 用量上限时，
    配的 fallback（M2.7-highspeed / M2.7）全在同一个 provider 同一账号下，
    三个模型一起挂 → 「有 fallback」不等于「有兜底」。

    状态字段看不出这个问题：模型都在、fallbackConfigured=True，
    只有真的欠费/限流/宕机时才暴露 —— 而那时系统已经不能推理了。
    """
    print("【Provider 韧性】")
    try:
        c = json.load(open("/root/.openclaw/openclaw.json", encoding="utf-8"))
    except Exception as e:
        bad(f"读不到 openclaw.json（{type(e).__name__}）")
        return
    providers = list(c.get("models", {}).get("providers", {}).keys())
    fallbacks = c.get("agents", {}).get("defaults", {}).get("model", {}).get("fallbacks", []) or []
    if not fallbacks:
        warn("未配置任何 fallback —— 主模型不可用时没有任何兜底")
        return
    if len(providers) <= 1:
        bad(f"只有 1 个 provider，但配了 {len(fallbacks)} 项 fallback —— "
            f"全在同一 provider 同一账号下，provider 级故障（欠费/限流/宕机）时兜底无效")
    else:
        fb_p = {f.split("/")[0] for f in fallbacks if "/" in f}
        if fb_p - set(providers):
            warn(f"fallback 指向未配置的 provider: {', '.join(sorted(fb_p - set(providers)))}")
        elif len(fb_p) == 1:
            warn(f"有 {len(providers)} 个 provider，但 fallback 全在 {list(fb_p)[0]} —— 未跨 provider 兜底")
        else:
            print(f"  ✅ PASS  fallback 跨 provider 兜底（{', '.join(sorted(fb_p))}）")


def check_provider_health():
    """provider 凭证是否还活着 —— 主动探测，不等调用时才发现失效。

    2026-10-26 上午三个 provider 全失效，状态字段一直显示正常。
    """
    print("【Provider 凭证】")
    r = subprocess.run(
        ["/usr/bin/python3", f"{WS}/scripts/check_provider_health.py"],
        capture_output=True, text=True, timeout=90,
    )
    for ln in (r.stdout or "").split("\n"):
        ln = ln.strip()
        if not ln or ln.startswith("【Provider 凭证】"):
            continue
        if "❌" in ln:
            bad(ln.replace("❌ FAIL", "").strip())
        elif "⚠️" in ln:
            print(f"  {ln}")
        else:
            print(f"  {ln}")
    if r.returncode != 0:
        bad("provider 检查未通过（详见上方）")


def _check_unit_integrity():
    """检查 systemd 单元的关键指令是否真的存在（不是只出现在注释里）。

    2026-10-26 踩坑：加 ExecStartPost 时重写了整个 .service 文件，
    把 [Unit] 段的 `OnFailure=` 真实指令弄丢了 —— 但因为**注释里还留着这行文字**，
    grep 一下就能看到「有 OnFailure」，看起来一切正常。
    结果：故障时告警单元**完全没被触发**，而服务照常报 failed —— 典型的静默失效。

    systemctl show -p OnFailure 能看到真正生效的指令值，这才是可信来源。
    """
    print("【systemd 单元完整性】")
    out, rc = cmd(
        ["/usr/bin/systemctl", "--user", "show", "openclaw-quick-health.service",
         "-p", "OnFailure", "--value"], timeout=15
    )
    val = (out or "").strip()
    if not val:
        bad("openclaw-quick-health.service 的 OnFailure 指令丢失 —— 故障时不会告警")
        return
    if "quick-health-alert" not in val:
        bad(f"OnFailure 指向了意外的目标：{val}")
        return
    ok(f"OnFailure → {val}")


def _check_unit_drift():
    """systemd 配置漂移检查 —— 验证「指令真的生效」，不是「文件里写了」。

    2026-10-26 的教训：重写 .service 时把 OnFailure 真实指令弄丢了，
    但注释里还留着那行文字 → grep 看着正常，实际告警链路已断。
    """
    print("【systemd 配置漂移】")
    r = subprocess.run(
        ["/usr/bin/python3", f"{WS}/scripts/check_unit_drift.py"],
        capture_output=True, text=True, timeout=90,
    )
    for ln in (r.stdout or "").split("\n"):
        ln = ln.strip()
        if not ln or ln.startswith("【") or ln.startswith("结果") or ln.startswith("═") or ln.startswith("   - "):
            continue
        if "❌" in ln:
            bad(ln.replace("❌ FAIL", "").strip())
        else:
            print(f"  {ln}")
    if r.returncode != 0:
        bad("systemd 单元存在配置漂移（详见上方）")


RESULT_FILE = f"{WS}/logs/quick-health-result.json"
RUNNING_FILE = f"{WS}/logs/quick-health-running.json"


def _write_running_marker():
    """开跑前先写一份「运行中」标记 —— E26 的另一半修复。

    为什么：只用「文件新鲜度」判断陈旧是不够的 ——
    崩溃发生在写文件「之前」时，文件里留的是上一次成功的结果，
    而它可能只有 1 分钟前写的（看起来很新鲜）→ 告警仍会被吞掉。

    有了这个标记：脚本崩在中间时，文件停在 status="running"，
    读取方一看就知道「这次没跑完」→ 判定为失败并告警 ✅

    只写一个极小的文件（不写 failures 列表），避免和正式结果混淆。
    """
    import datetime
    try:
        os.makedirs(f"{WS}/logs", exist_ok=True)
        with open(RUNNING_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "startedAt": _RUN_STARTED_AT,
                    "runId": _RUN_ID,
                    "status": "running",
                    "pid": os.getpid(),
                },
                f,
                ensure_ascii=False,
            )
    except Exception:
        pass


def _write_result_file(run_id=None):
    """把本次结果写到文件 —— alert_dedup.py 读它，而不是重跑一遍巡检。

    为什么：原来 dedup 会自己再跑一次 quick_health.py，
    导致每次成功巡检实际跑两遍（8 秒 → 16 秒），而且两次结果可能不一致
    （第一次失败、第二次成功这种竞态会让状态错乱）。

    ⚠️ run_id（2026-10-26 加）：每次运行的唯一标识 + 开始时刻。
    为什么必须有 —— 见下方 E26：
    脚本如果在写这个文件「之前」就崩了/被 kill/超时，
    文件里留的还是上一次成功的结果，alert_dedup.py 读到它会说「无故障」，
    **告警被静默吞掉 —— 恰恰发生在系统最坏的时候**。
    有了 run_id，读取方能判断「这份结果是不是本次的」，陈旧就自己重跑。
    """
    import datetime
    try:
        os.makedirs(f"{WS}/logs", exist_ok=True)
        with open(RESULT_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "startedAt": _RUN_STARTED_AT,
                    "runId": run_id or _RUN_ID,
                    "status": "done",
                    "passed": PASSED,
                    "failed": len(FAILED),
                    "failures": FAILED,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception as e:
        print(f"[log] 结果文件写入失败（忽略）: {type(e).__name__}")


def _persist_log():
    """把结果追加到 logs/quick-health.log —— journald 会轮转，日志文件能保留历史。

    为什么重要：排障时最需要的信息是「这个故障持续了多久」，
    而 journald 只保留有限窗口，看不出持续性。
    """
    import datetime
    log_dir = f"{WS}/logs"
    try:
        os.makedirs(log_dir, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        status = "FAIL" if FAILED else "PASS"
        with open(f"{log_dir}/quick-health.log", "a", encoding="utf-8") as f:
            f.write(f"{stamp}  {status}  PASS={PASSED} FAIL={len(FAILED)}")
            if FAILED:
                f.write("  | " + "; ".join(FAILED)[:300])
            f.write("\n")
    except Exception as e:
        print(f"[log] 写入失败（忽略）: {type(e).__name__}")


def main():
    # 先写「运行中」标记（E26）：崩溃时它会留在原地，告警方据此判断「没跑完」
    _write_running_marker()
    print("════════════════════════════════════════════")
    print(" 快速健康子集  " + __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("════════════════════════════════════════════")
    check_external_apis()
    check_services()
    check_memory_index()
    check_provider_health()
    check_provider_resilience()
    check_cron_delivery()
    _check_unit_integrity()
    _check_unit_drift()
    print("════════════════════════════════════════════")
    print(f" 结果: PASS={PASSED}  FAIL={len(FAILED)}")
    if FAILED:
        for f in FAILED:
            print(f"   - {f}")
        print(" 提示：完整巡检请跑 scripts/healthcheck.sh")
    print("════════════════════════════════════════════")
    _persist_log()
    _write_result_file()
    # 正常跑完 → 移除「运行中」标记
    try:
        os.remove(RUNNING_FILE)
    except Exception:
        pass
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
