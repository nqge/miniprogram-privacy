#!/usr/bin/env python3
"""quota_preflight 的单测 —— 防「三种失败模式被混为一谈」复发。

## 为什么需要它（2026-11-16 实测）

09:00 的 daily-learning 计划槽位跑配额预检，探针返回 **HTTP 529**
（`overloaded_error`：当前为整点高峰时段，服务器短暂繁忙，通常 1-5 分钟内恢复）。

原实现把所有非 200 的结果一律判成「额度不足：HTTP 529 ...」→ 结论「先别跑」。

**结论碰巧没错，理由是错的** —— 而理由错会连累别的判断：
  · 「额度不足」会被当成「要充值」→ 牛哥哥收到错误信号
  · 「先别跑」在 529 场景下是**过度保守**：上游说 1-5 分钟就恢复，
    而 09:00 恰恰是最该跑的整点 → 整点被「整点高峰」自己挡住

更危险的是 429 的**两义性**：MiniMax 的 429 既可能是
  ① 「已达到 Token Plan 用量上限」（额度耗尽，重试无用，等充值）
  ② 「rate limit reached, please try again later」（普通限流，等 1-2 分钟就好）
**HTTP 码完全一样，但对「现在该不该跑」给出相反的答案。**

→ 判据必须看**报文特征**，不能只看 HTTP 码。

## 跑法

    python3 scripts/test_quota_preflight.py
"""

import importlib.util
import sys
import time
import urllib.error
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "quota_preflight.py"
spec = importlib.util.spec_from_file_location("quota_preflight", SCRIPT)
qp = importlib.util.module_from_spec(spec)
sys.modules["quota_preflight"] = qp
spec.loader.exec_module(qp)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


class FakeHTTPError(urllib.error.HTTPError):
    """构造一个可控的 HTTPError（body 是要读出来的报文）。"""

    def __init__(self, code, body):
        super().__init__(url="https://x", code=code, msg="err", hdrs=None, fp=None)
        self._body = body.encode("utf-8")

    def read(self):
        return self._body


print("=" * 60)
print(" quota_preflight 单测")
print("=" * 60)

# ---------------------------------------------------------------- ①
print("\n【①】_is_quota_signal —— 429 的两义性")
QUOTA_RAW = '{"error":{"message":"已达到 Token Plan 用量上限：请升级 Token Plan 套餐或购买积分补充用量。 (2056)","type":"rate_limit_error"}}'
RATE_RAW = '{"error":{"message":"API rate limit reached. Please try again later.","type":"rate_limit_error"}}'

check("额度耗尽报文 → 判定为额度信号", qp._is_quota_signal(QUOTA_RAW) is True)
check("普通限流报文 → 不判定为额度信号", qp._is_quota_signal(RATE_RAW) is False)
check("insufficient balance → 额度信号",
      qp._is_quota_signal('{"error":{"message":"insufficient balance"}}') is True)
check("usage limit → 额度信号",
      qp._is_quota_signal('{"message":"usage limit reached"}') is True)
check("quota exceeded → 额度信号",
      qp._is_quota_signal('{"message":"quota exceeded"}') is True)
check("空报文 → 不判定为额度信号", qp._is_quota_signal("") is False)

# ---------------------------------------------------------------- ②
print("\n【②】probe_once —— 三种 HTTP 码各自归类正确")
import urllib.request

_real_urlopen = urllib.request.urlopen
_sent = []


def fake_urlopen_factory(code, body):
    def _f(req, timeout=None):
        _sent.append(req)
        if code == 0:
            import io
            payload = b'{"base_resp":{"status_code":0,"status_msg":"ok"}}'
            r = io.BytesIO(payload)
            r.status = 200
            r.read = r.read  # keep
            class _R:
                def __enter__(self): return self
                def __exit__(self, *a): return False
                def read(self): return payload
            return _R()
        raise FakeHTTPError(code, body)
    return _f


def run_probe_once(code, body):
    urllib.request.urlopen = fake_urlopen_factory(code, body)
    try:
        return qp.probe_once(100)
    finally:
        urllib.request.urlopen = _real_urlopen


ok, msg, dt = run_probe_once(429, QUOTA_RAW)
check("429+Token Plan → 额度不足", (not ok) and "额度不足" in msg, msg)
ok, msg, dt = run_probe_once(429, RATE_RAW)
check("429+普通限流 → 明说非额度耗尽", (not ok) and "普通限流" in msg, msg)
ok, msg, dt = run_probe_once(529, '{"error":{"type":"overloaded_error","message":"当前为整点高峰时段"}}')
check("529 → 上游繁忙且明说非额度问题",
      (not ok) and "上游繁忙" in msg and "非额度问题" in msg, msg)
ok, msg, dt = run_probe_once(402, '{"error":{"message":"quota exhausted"}}')
check("402 → 额度不足", (not ok) and "额度不足" in msg, msg)
ok, msg, dt = run_probe_once(500, 'oops')
check("500 → 探测失败（不是额度结论）", (not ok) and "探测失败" in msg, msg)

# ---------------------------------------------------------------- ③
print("\n【③】probe —— 529 会重试，429 不会")
calls = {"n": 0}


def fake_always_529(req, timeout=None):
    calls["n"] += 1
    return fake_urlopen_factory(529, '{"error":{"type":"overloaded_error"}}')(req, timeout)


urllib.request.urlopen = fake_always_529
try:
    t0 = time.time()
    ok, msg, dt = qp.probe(100, retries=3, backoff=0)   # backoff=0 → 不真的睡
    elapsed = time.time() - t0
finally:
    urllib.request.urlopen = _real_urlopen

check("529 → 重试了 3 次", calls["n"] == 3, f"实际 {calls['n']} 次")
check("529 → 最终仍判为不通", ok is False)
check("529 → 文案是「上游繁忙」不是「额度不足」",
      "上游繁忙" in msg and "额度不足" not in msg, msg)

calls2 = {"n": 0}


def fake_always_429(req, timeout=None):
    calls2["n"] += 1
    return fake_urlopen_factory(429, QUOTA_RAW)(req, timeout)


urllib.request.urlopen = fake_always_429
try:
    ok, msg, dt = qp.probe(100, retries=3, backoff=0)
finally:
    urllib.request.urlopen = _real_urlopen

check("429 额度耗尽 → 不重试（重试也没用）", calls2["n"] == 1, f"实际 {calls2['n']} 次")
check("429 → 文案是「额度不足」", "额度不足" in msg, msg)

# ---------------------------------------------------------------- ④
print("\n【④】回归 —— 成功路径仍然通")
ok, msg, dt = run_probe_once(0, "")
check("200 → 额度可用", ok is True, msg)
check("200 → 文案含「额度可用」", "额度可用" in msg, msg)

print()
print("=" * 60)
print(f" 结果: PASS={PASS} FAIL={FAIL}")
print("=" * 60)
sys.exit(1 if FAIL else 0)
