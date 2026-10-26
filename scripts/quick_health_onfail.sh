#!/usr/bin/env bash
# 快速健康检查失败时的告警触发器 —— 由 systemd OnFailure= 调用
# 独立于被监控的脚本：告警路径挂了也不会影响检测，检测挂了也能告警
LOG_OUT=$(journalctl --user -u openclaw-quick-health.service -n 30 --no-pager 2>/dev/null \
  | grep -E '❌ FAIL' | sed 's/.*FAIL  //' | head -8 | tr '\n' ';')
[ -z "$LOG_OUT" ] && LOG_OUT="见 journalctl 日志"
/usr/bin/python3 /root/.openclaw/workspace/scripts/quick_health_alert.py "$LOG_OUT"
