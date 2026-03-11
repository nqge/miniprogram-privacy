#!/bin/bash
# 自我驱动心跳定时任务

echo "🦞 自我驱动心跳检查"
echo "================================"
echo ""

# 运行心跳脚本
python3 /root/.openclaw/workspace/scripts/heartbeat.py

echo ""
echo "✅ 心跳检查完成"
