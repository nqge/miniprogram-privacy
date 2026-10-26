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

WS = "/root/.openclaw/workspace"
FAILED = []
PASSED = 0

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
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
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

    # GitHub（茶馆监控依赖）
    try:
        with urllib.request.urlopen(
            "https://api.github.com/repos/ythx-101/openclaw-qa/issues", timeout=12
        ) as r:
            r.read(1)
        ok("GitHub API 可达")
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


def main():
    print("════════════════════════════════════════════")
    print(" 快速健康子集  " + __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("════════════════════════════════════════════")
    check_external_apis()
    check_services()
    check_memory_index()
    check_provider_health()
    check_cron_delivery()
    print("════════════════════════════════════════════")
    print(f" 结果: PASS={PASSED}  FAIL={len(FAILED)}")
    if FAILED:
        for f in FAILED:
            print(f"   - {f}")
        print(" 提示：完整巡检请跑 scripts/healthcheck.sh")
    print("════════════════════════════════════════════")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
