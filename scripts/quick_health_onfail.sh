#!/usr/bin/env bash
# 快速健康检查失败时的告警触发器 —— 由 systemd OnFailure= 调用
# 独立于被监控的脚本：告警路径挂了也不会影响检测，检测挂了也能告警
#
# 走 alert_dedup.py 而不是直接发送：持续性故障每 30 分钟会重复触发，
# 去重后同一故障 6 小时内只发一次，恢复时再发一条「已恢复」。
/usr/bin/python3 /root/.openclaw/workspace/scripts/alert_dedup.py
