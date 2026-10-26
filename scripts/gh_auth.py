#!/usr/bin/env python3
"""集中管理 GitHub 认证 —— 单一真相源。

为什么需要它（2026-10-26）：
  GitHub API 匿名额度只有 **60 次/小时/IP**，认证后是 **5000 次/小时**。
  我先是在 teahouse_monitor.py 里加了 token 读取，随后 quick_health.py 和
  healthcheck.sh 各自又抄了一遍正则 —— **三份同样的逻辑，两种语言**。
  典型的问题：gh CLI 改了 hosts.yml 格式（比如换 oauth_token → token），
  三处会**同时悄悄失效**，而且每一处都会以「API 不可用」的形式表现出来，
  看起来像三个独立故障。

做法：
  统一从这里取 token，三种方式依次尝试：
    1. `gh auth token`（官方方式，格式变化时它自己会跟上）
    2. 环境变量 GITHUB_TOKEN / GH_TOKEN
    3. 解析 ~/.config/gh/hosts.yml（兜底）
  拿不到就返回 None —— 调用方退回匿名，并**明确告知额度只有 60 次/小时**。

用法（Python）:
    from gh_auth import github_headers
    headers = github_headers()

用法（shell）:
    GH_TOKEN=$(python3 scripts/gh_auth.py --token)
"""

import os
import re
import subprocess
import sys
from pathlib import Path

HOSTS = Path("/root/.config/gh/hosts.yml")
CACHE = {}


def _from_gh_cli():
    """优先用 gh CLI —— 它的输出格式跟着 gh 自己变，最不会过时。"""
    try:
        r = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None


def _from_env():
    for k in ("GITHUB_TOKEN", "GH_TOKEN"):
        v = os.environ.get(k)
        if v and v.strip():
            return v.strip()
    return None


def _from_hosts_file():
    """兜底：解析 hosts.yml。兼容 oauth_token / token 两种键名。"""
    try:
        txt = HOSTS.read_text(encoding="utf-8")
    except Exception:
        return None
    # 只取 user: 段落下的 token，避免拿到 oauth 应用的 client secret
    m = re.search(r"^\s*oauth_token:\s*(\S+)", txt, re.M) or re.search(
        r"^\s*token:\s*(\S+)", txt, re.M
    )
    return m.group(1) if m else None


def github_token():
    """返回 GitHub token；拿不到返回 None。"""
    if "token" in CACHE:
        return CACHE["token"]
    tok = _from_gh_cli() or _from_env() or _from_hosts_file()
    CACHE["token"] = tok
    return tok


def github_headers(extra=None):
    """返回适合 GitHub API 请求的 headers 字典。"""
    h = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "openclaw-healthcheck",
    }
    tok = github_token()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    if extra:
        h.update(extra)
    return h


def quota_note():
    """给日志用的一句话：当前是认证还是匿名，额度差多少。"""
    return (
        "认证请求（5000 次/小时）" if github_token() else "匿名请求（仅 60 次/小时）"
    )


if __name__ == "__main__":
    if "--token" in sys.argv:
        t = github_token()
        if t:
            print(t)
        else:
            sys.exit(1)
    elif "--quota-note" in sys.argv:
        print(quota_note())
    else:
        t = github_token()
        src = (
            "gh CLI" if _from_gh_cli() else
            "环境变量" if _from_env() else
            "hosts.yml" if _from_hosts_file() else
            "未找到"
        )
        print(f"来源: {src} | 状态: {'已获取（长度 ' + str(len(t)) + '）' if t else '未获取 → 将退回匿名'}")
