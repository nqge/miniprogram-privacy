#!/usr/bin/env python3
"""快速健康检查失败时通过飞书告警 —— 由 systemd 单元调用。

为什么不靠 cron：
  cron 的 delivery 是「任务跑完才投递」。如果任务自己挂了（没跑完），
  投递环节同样不会触发 —— 这正是 2026-10-26 发现的飞书投递静默失效。
  用 systemd 的 OnFailure= 独立触发，**告警路径和被监控路径互不依赖**。

用法：python3 scripts/quick_health_alert.py "<失败摘要>"
"""

import json
import sys
import urllib.request

CFG = "/root/.openclaw/openclaw.json"
TO = "ou_9227741a8ff35fa4e8e83027743dc305"  # 牛哥哥的 open_id


def main():
    reason = sys.argv[1] if len(sys.argv) > 1 else "未知原因"
    try:
        cfg = json.load(open(CFG, encoding="utf-8"))
        f = cfg["channels"]["feishu"]
        req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps(
                {"app_id": f["appId"], "app_secret": f["appSecret"]}
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        tok = json.load(urllib.request.urlopen(req, timeout=15)).get("tenant_access_token")
        if not tok:
            print("[alert] 取 token 失败")
            return 1

        text = (
            "🔴 快速健康检查失败\n"
            f"时间：{__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"原因：{reason[:400]}\n\n"
            "完整巡检：bash scripts/healthcheck.sh\n"
            "日志：journalctl --user -u openclaw-quick-health.service -n 30"
        )
        body = {
            "receive_id": TO,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        r = urllib.request.Request(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
            data=json.dumps(body, ensure_ascii=False).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {tok}",
            },
            method="POST",
        )
        d = json.load(urllib.request.urlopen(r, timeout=15))
        print(f"[alert] 发送结果 code={d.get('code')} msg={d.get('msg')}")
        return 0 if d.get("code") == 0 else 1
    except Exception as e:
        print(f"[alert] 发送异常: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
