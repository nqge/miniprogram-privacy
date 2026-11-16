#!/usr/bin/env python3
"""check_cron_delivery 的「断言装死」单测 —— 防「检查了 0 个任务却显示通过」。

## 为什么需要它（2026-11-16 实测）

2026-11-16 把 daily-learning / daily-reflection 都改成 **systemd 闸门触发**
（cron 里 disabled，只作为被触发的目标）。
结果 `openclaw cron list` 返回 **"No cron jobs"**。

而 `check_cron_delivery.py` 原来是从 `cron list` 取任务列表的：

```python
for j in jobs:      # jobs == [] → 循环体一次都不跑
    ...
print(f"__FAILED__={failed}")   # → 0 → 报告全绿
```

→ **【D】组一条断言都没跑，却显示「全部通过」。**
这是「断言装死」：报告说没问题，但**检查对象已经不在视野里了**。

同族教训：
  · 【X】组专门防「解析器坏了却不报错」
  · E69：探针端点撒谎（探针说 ok，被探的对象坏了）
  · E71：测得准但没接到「该修」的那根线上
→ 本条：**没检查 ≠ 检查通过**。报告必须能区分这两者。

## 跑法

    python3 scripts/test_cron_delivery_no_jobs.py
"""

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "check_cron_delivery.py"
spec = importlib.util.spec_from_file_location("check_cron_delivery", SCRIPT)
ccd = importlib.util.module_from_spec(spec)
sys.modules["check_cron_delivery"] = ccd
spec.loader.exec_module(ccd)

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


print("=" * 60)
print(" 【D】组 · 断言装死 单测")
print("=" * 60)

# ---------------------------------------------------------------- ①
print("\n【①】源码层面：不能再只依赖 `cron list`")

src = SCRIPT.read_text(encoding="utf-8")
check("读 cron/jobs.json 的代码存在", "cron/jobs.json" in src)
check("存在「读不到任何任务」的显式失败分支", "什么都没检查" in src)

# ---------------------------------------------------------------- ②
print("\n【②】行为层面：任务存储为空 → 必须显式失败，不能静默通过")

# 造一个「任务存储为空」的场景：把存储路径指到一个不存在的文件，
# 并让 `cron list` 回退路径也拿不到东西。
# 直接调用真身（E11：不抄判据），观察返回值。
import subprocess
import os

orig_run = subprocess.run
orig_jobs = Path("/root/.openclaw/cron/jobs.json")


def fake_run(cmd, *a, **kw):
    """让 `cron list --json` 返回空数组（模拟「No cron jobs」）。"""
    class R:
        stdout = json.dumps({"jobs": []})
        stderr = ""
        returncode = 0
    return R()


try:
    # 用真实路径但临时改名 → 存储读不到 → 回退到 cron list → 也是空
    bak = orig_jobs.with_suffix(".json.bak_test")
    orig_jobs.rename(bak)
    subprocess.run = fake_run
    try:
        rc = ccd.main()
    finally:
        subprocess.run = orig_run
        bak.rename(orig_jobs)
except Exception as e:
    rc = None
    print(f"   （异常：{type(e).__name__}: {e}）")

check("空任务列表 → 返回非 0（失败），而不是静默 0",
      rc != 0, f"实际返回 {rc}")
check("空任务列表 → 不返回 0（'__FAILED__=0' 就是假绿）",
      rc != 0, "返回 0 = 报告会说『全部通过』")

# ---------------------------------------------------------------- ③
print("\n【③】回归：正常情况下不能因为这次改动而误报")

jobs = json.loads(orig_jobs.read_text(encoding="utf-8")).get("jobs", [])
names = {j.get("name") for j in jobs}
check("任务存储里能读到 daily-learning", "daily-learning" in names, sorted(names))
check("任务存储里能读到 daily-reflection", "daily-reflection" in names, sorted(names))
check("两个任务都是 disabled（由 systemd 闸门触发）",
      all(j.get("enabled") is False for j in jobs if j.get("name", "").startswith("daily-")),
      "闸门架构要求 cron 侧不自行触发")

print()
print("=" * 60)
print(f" 结果: PASS={PASS} FAIL={FAIL}")
print("=" * 60)
sys.exit(1 if FAIL else 0)
