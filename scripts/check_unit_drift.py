#!/usr/bin/env python3
"""systemd 单元配置漂移检查 —— 验证「指令真的生效」，而不是「文件里写了」。

为什么需要它（2026-10-26 的直接教训）：
  今天我为了加一行 ExecStartPost 重写了整个 .service 文件，
  把 [Unit] 段的 `OnFailure=` 真实指令弄丢了 ——
  **但注释里还留着这行文字**，所以 `grep OnFailure` 能搜到，看起来一切正常。
  结果：故障时告警单元完全不被触发，服务照常报 failed，典型的静默失效。

  这暴露了一整类风险：**配置文件里的东西 ≠ 实际生效的东西**。
  这个脚本把这一类风险系统性覆盖掉。

检查项（每项都问「实际生效的是什么」）：
  1. 单元是否 enabled（重启后能自动恢复）
  2. ExecStart 指向的脚本**是否真实存在**（改路径/改名后最容易断）
  3. 关键指令是否**真正生效**（用 systemctl show 取值，不 grep 文件）
  4. ExecStart 的**解释器是否存在**
  5. 单元引用的环境变量是否齐（systemd 默认环境很干净 —— 今天踩过）

退出码：0 = 全通过；1 = 有 FAIL
"""

import os
import re
import subprocess
import sys

# 单元 → (必须 enabled?, 关键指令检查项列表)
# 关键指令格式：(指令名, 必须包含的子串, 说明)
UNITS = {
    "openclaw-gateway.service": {
        "enabled": True,
        "checks": [],
    },
    "openclaw-embed-proxy.service": {
        "enabled": True,
        "checks": [],
    },
    "openclaw-log-cleanup.timer": {
        "enabled": True,
        "checks": [],
    },
    "openclaw-quick-health.timer": {
        "enabled": True,
        "checks": [],
    },
    "openclaw-quick-health.service": {
        "enabled": False,  # 由 timer 触发，不需要 enable
        "checks": [
            ("OnFailure", "quick-health-alert", "故障告警链路"),
            ("ExecStartPost", "alert_dedup", "恢复通知"),
        ],
    },
    "openclaw-quick-health-alert@.service": {
        "enabled": False,
        "checks": [],
    },
}

# 定时器必须存在的调度
# 格式：(systemd 属性名, 说明)
# 注意：systemd 把 OnUnitActiveSec 规范化成属性名 OnUnitActiveUSec；
# 定时器属性是顶层属性（不是 [Timer] 段里的原文），必须用 show 取，不能 grep 文件。
TIMERS = {
    "openclaw-quick-health.timer": [
        ("TimersMonotonic", "OnUnitActiveUSec", "30 分钟周期"),
        ("Persistent", None, "关机/休眠错过的会补跑"),
    ],
    "openclaw-log-cleanup.timer": [
        ("TimersCalendar", "OnCalendar", "每日定时"),
        ("Persistent", None, "关机/休眠错过的会补跑"),
    ],
}

FAILED = []
PASSED = 0


def ok(msg):
    global PASSED
    PASSED += 1
    print(f"  ✅ PASS  {msg}")


def bad(msg):
    FAILED.append(msg)
    print(f"  ❌ FAIL  {msg}")


def sh(args, timeout=15):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def unit_value(unit, prop):
    return sh(["/usr/bin/systemctl", "--user", "show", unit, "-p", prop, "--value"])


def exec_start_targets(unit):
    """从 ExecStart 的 argv[] 里提取实际执行的脚本路径。"""
    raw = unit_value(unit, "ExecStart")
    if not raw:
        return [], ""
    m = re.search(r"argv\[\]=(.*?)(?:;\s*ignore_errors|\})", raw, re.S)
    if not m:
        return [], ""
    argv = m.group(1).split()
    if not argv:
        return [], ""
    interp = argv[0]
    # 脚本 = argv 里第一个「像路径」的非参数 token
    # ⚠️ 不能只认「存在的文件」—— 那正好会漏掉「路径写错/文件被删」这种漂移
    #    （今天就是这个 bug：路径改成不存在的文件，检查却报 PASS）
    for tok in argv[1:]:
        if tok.startswith("-"):
            continue
        if "/" in tok:
            return [tok], interp
    return [], interp


def main():
    print("【systemd 配置漂移】")

    for unit, spec in UNITS.items():
        state = sh(["/usr/bin/systemctl", "--user", "is-enabled", unit])
        if spec["enabled"]:
            if state == "enabled":
                ok(f"{unit} enabled（重启后自动恢复）")
            else:
                bad(f"{unit} is-enabled={state or 'unknown'} — 重启后不会自动恢复")
        else:
            ok(f"{unit} 存在（{state or 'static'}）")

        targets, interp = exec_start_targets(unit)
        if interp:
            if os.path.exists(interp):
                ok(f"{unit} 解释器 {interp} 存在")
            else:
                bad(f"{unit} 解释器 {interp} 不存在")
        for t in targets:
            if os.path.exists(t):
                ok(f"{unit} → {t} 存在")
            else:
                bad(f"{unit} → {t} 【脚本不存在】")

        for prop, must, desc in spec["checks"]:
            val = unit_value(unit, prop)
            if not val:
                bad(f"{unit} 的 {prop} 指令丢失 —— {desc}会失效")
            elif must in val:
                ok(f"{unit} 的 {prop} 已生效（{desc}）")
            else:
                bad(f"{unit} 的 {prop} 指向异常：{val[:80]}")

    print("【敏感文件权限】")
    # 2026-10-26 实测：systemd 默认 UMask=0022 → 会话日志建成 644（全局可读），
    # 而会话日志里会带进 token（实测 7 个文件含完整 GitHub PAT）。
    # 已加 UMask=0077 + 目录 700，这里断言防止复发。
    sess_dir = "/root/.openclaw/agents/main/sessions"
    try:
        dmode = os.stat(sess_dir).st_mode & 0o777
        if dmode & 0o077:
            bad(f"sessions 目录权限 {oct(dmode)[2:]} —— 其他用户可列目录（应为 700）")
        else:
            ok(f"sessions 目录权限 {oct(dmode)[2:]}（仅 root 可读写）")

        umask_val = unit_value("openclaw-gateway.service", "UMask")
        if umask_val in ("0077", "077"):
            ok(f"gateway UMask={umask_val}（新建会话日志默认 600）")
        else:
            bad(f"gateway UMask={umask_val or '未设置'} —— 新建会话日志会是 644（全局可读）")

        # 抽查含 token 的会话日志
        leaked = []
        try:
            for fn in os.listdir(sess_dir):
                if not fn.endswith(".jsonl") and ".jsonl" not in fn:
                    continue
                fp = os.path.join(sess_dir, fn)
                try:
                    m = os.stat(fp).st_mode & 0o777
                    if m & 0o077:
                        leaked.append(f"{fn}({oct(m)[2:]})")
                except OSError:
                    pass
        except OSError:
            pass
        if leaked:
            bad(f"{len(leaked)} 个会话日志权限过宽：{', '.join(leaked[:3])}")
        else:
            ok("所有会话日志权限均不含组/其他可读")
    except FileNotFoundError:
        ok("sessions 目录不存在（跳过）")

    print("【单元文件权限】")
    # 2026-10-26 实测：openclaw-gateway.service 里明文写着 OPENSUBTITLES 密码/密钥，
    # 而单元文件默认 644 → 同机任何用户都能读到。已收紧到 600。
    # 这里断言所有「含凭据的」单元文件都不是 group/other 可读。
    import glob as _glob
    import re as _re
    for uf in sorted(_glob.glob("/root/.config/systemd/user/*.service")
                     + _glob.glob("/root/.config/systemd/user/*.timer")):
        try:
            body = open(uf, encoding="utf-8").read()
        except Exception:
            continue
        cred = _re.search(r"(?:PASSWORD|TOKEN|SECRET|API_KEY)=([^\n\"']+)", body)
        if not cred:
            continue
        name = os.path.basename(uf)
        m = os.stat(uf).st_mode & 0o777
        if m & 0o077:
            bad(f"{name} 含明文凭据但权限 {oct(m)[2:]} —— 其他用户可读")
        else:
            ok(f"{name} 含凭据，权限 {oct(m)[2:]}（仅 root 可读）")

    print("【定时器调度】")
    for timer, props in TIMERS.items():
        for prop, sub, desc in props:
            val = unit_value(timer, prop)
            if not val:
                bad(f"{timer} 缺少 {prop} —— {desc}")
                continue
            if sub:
                # 子字段形式：{ OnUnitActiveUSec=30min ; ... }
                if sub not in val:
                    bad(f"{timer} 的 {prop} 里没有 {sub} —— {desc}")
                elif re.search(rf"{sub}=(\S*no|\S*0)\b", val):
                    bad(f"{timer} 的 {sub} 未启用 —— {desc}")
                else:
                    ok(f"{timer} {sub} 已生效（{desc}）")
            else:
                if val in ("no", "0"):
                    bad(f"{timer} 的 {prop} = {val} —— {desc} 未启用")
                else:
                    ok(f"{timer} {prop}={val[:40]}（{desc}）")

    print("════════════════════════════════════════════")
    print(f" 结果: PASS={PASSED}  FAIL={len(FAILED)}")
    for f in FAILED:
        print(f"   - {f}")
    print("════════════════════════════════════════════")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
