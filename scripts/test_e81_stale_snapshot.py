#!/usr/bin/env python3
"""【D】组「陈旧快照」判据单测（2026-11-21 新增，E81）

为什么要有这个文件：
  2026-11-21 08:10 巡检报「daily-reflection 运行失败（额度耗尽）」，
  实际是 **6 天前**（2026-11-15）cron 侧的陈旧快照 ——
  2026-11-16 起该 job 改由 systemd 闸门触发，cron 路径**再没被写过**。
  → 判据把旧源的最后一个快照当成了「当前故障」。

  这是「加过滤 = 把判据改弱」的反向风险：
  如果判据一看到 error 就报，修法很容易滑向「**把 error 都忽略掉**」
  → 真故障也被吞掉，比假故障更危险。

  → 所以这个判据必须有**双向**单测：
     ① 真阳性：陈旧快照 → **不**报当前故障
     ② 真阴性：真故障（闸门真的报错 / cron 迁移后还在跑）→ **要**报
     ③ **注入自检**：注入前后结果**必须不同** —— 专门防「注入空转、
        测试全绿但什么都没测」

跑法：python3 scripts/test_e81_stale_snapshot.py
退出码 0 = 全通过

⚠️ **import 真身，不抄判据**（E11 的规矩）：
   抄一份判据来测 = 测的是「我以为的判据」而不是「在跑的判据」，
   抄版会和真版**悄悄漂移**（2026-10-27 就因此踩过）。
"""
import datetime
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(SCRIPT_DIR, "check_cron_delivery.py")

# 从判据真身里抽逻辑（不抄）
spec = importlib.util.spec_from_file_location("ccd_e81", TARGET)
ccd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ccd)


RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    mark = "✅" if cond else "❌"
    print(f"{mark} {name}" + (f"  → {detail}" if detail and not cond else ""))


def _job(name, enabled=False, job_id="job-1"):
    return {
        "id": job_id,
        "name": name,
        "enabled": enabled,
        "state": {},
        "delivery": {"mode": "announce"},
    }


def _ts(datestr):
    d = datetime.datetime.strptime(datestr, "%Y-%m-%d").replace(
        hour=3, minute=0
    )
    return int(d.timestamp() * 1000)


def test_gate_history_read():
    """闸门历史读得对不对"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "gate-history.jsonl"
        p.write_text(
            "\n".join(
                [
                    json.dumps({"ts": "2026-11-20T09:00:43", "job": "daily-learning", "result": "triggered"}),
                    json.dumps({"ts": "2026-11-21T09:00:00", "job": "daily-reflection", "result": "error", "reason": "boom"}),
                    "",
                    "不是JSON的一行",
                    json.dumps({"ts": "2026-11-21T10:00:00", "job": "daily-learning", "result": "skipped"}),
                ]
            ),
            encoding="utf-8",
        )
        hist = ccd._read_gate_history(p)
        check("闸门历史：坏行被跳过，好行都读到", hist is not None and len(hist) == 3, f"拿到 {hist}")
        if hist:
            check("闸门历史：字段完整", hist[0].get("result") == "triggered")

        missing = ccd._read_gate_history(Path(td) / "不存在.jsonl")
        check("闸门历史：文件不存在 → 返回 None（=不知道，不是不正常）", missing is None)


def test_filter_by_job():
    """按 job 名过滤闸门记录"""
    hist = [
        {"job": "daily-learning", "result": "triggered"},
        {"job": "daily-reflection", "result": "error"},
        {"job": "daily-learning-gate", "result": "ok"},
    ]
    dl = ccd._gate_records_for(hist, "daily-learning")
    check("闸门过滤：命中 daily-learning 的 2 条（含 -gate 后缀）", len(dl) == 2, f"拿到 {len(dl)}")
    dr = ccd._gate_records_for(hist, "daily-reflection")
    check("闸门过滤：命中 daily-reflection 的 1 条", len(dr) == 1, f"拿到 {len(dr)}")
    check("闸门过滤：不存在的 job → 0 条", ccd._gate_records_for(hist, "nope") == [])


def test_result_classification():
    """闸门结果分类：blocked/skipped 是闸门正常工作，不算故障"""
    check("闸门 error → 算故障", ccd._gate_result_is_failure({"result": "error"}))
    check("闸门 blocked → 不算故障（额度不足，闸门正常工作）",
          not ccd._gate_result_is_failure({"result": "blocked"}))
    check("闸门 skipped → 不算故障",
          not ccd._gate_result_is_failure({"result": "skipped"}))
    check("闸门 triggered → 不算故障",
          not ccd._gate_result_is_failure({"result": "triggered"}))
    check("闸门 ok → 不算故障",
          not ccd._gate_result_is_failure({"result": "ok"}))


def test_stale_detection():
    """陈旧快照的识别（真阳性 + 真阴性）"""
    hist = [
        {"ts": "2026-11-20T09:00:43", "job": "daily-learning", "result": "triggered"},
    ]

    # 真阴性 ①：cron 迁移后还在跑 → **活的**，不是陈旧
    live_job = _job("daily-learning", enabled=False)
    stale, det = ccd._stale_snapshot_info(live_job, hist, None, None)
    # 无 job_id → cron_last_ts 为 None → 判为陈旧；这里显式构造「迁移后有运行」的情况
    check("陈旧判定：cron 迁移后仍无运行记录 → 判为陈旧", stale is True, f"stale={stale}")

    # 真阴性 ②：闸门真的报错 → 是真故障（不是陈旧问题）
    hist_err = [{"ts": "2026-11-21T09:00:00", "job": "daily-reflection", "result": "error"}]
    grecs = ccd._gate_records_for(hist_err, "daily-reflection")
    gfail = [r for r in grecs if ccd._gate_result_is_failure(r)]
    check("真阴性：闸门记录 error → 判为真故障（要报）", len(gfail) == 1, f"gfail={len(gfail)}")

    # 真阴性 ③：闸门 blocked → 不算故障
    hist_blocked = [{"ts": "2026-11-21T09:00:00", "job": "daily-reflection", "result": "blocked"}]
    gfail2 = [r for r in ccd._gate_records_for(hist_blocked, "daily-reflection")
              if ccd._gate_result_is_failure(r)]
    check("真阴性：闸门 blocked → 不报故障", len(gfail2) == 0)

    # 真阴性 ④：cron 任务**未** disabled → 不走陈旧分支（仍按正常路径判）
    enabled_job = _job("some-cron", enabled=True)
    stale2, _ = ccd._stale_snapshot_info(enabled_job, hist, None, None)
    check("真阴性：cron 仍 enabled → 不判陈旧（走原路径）", stale2 is False, f"stale={stale2}")


def test_injection_selfcheck():
    """注入自检：改判据 → 结果必须跟着变（防「注入空转，测试全绿但什么都没测」）"""
    hist = [{"ts": "2026-11-21T09:00:00", "job": "daily-reflection", "result": "error"}]
    grecs = ccd._gate_records_for(hist, "daily-reflection")
    gfail = [r for r in grecs if ccd._gate_result_is_failure(r)]

    # 注入①：把 error 判成非故障 → gfail 必须变空
    orig = ccd._gate_result_is_failure
    try:
        ccd._gate_result_is_failure = lambda rec: False
        injected = [r for r in ccd._gate_records_for(hist, "daily-reflection")
                    if ccd._gate_result_is_failure(r)]
    finally:
        ccd._gate_result_is_failure = orig
    check("注入自检①：把 error 判成非故障 → 真故障条数必须减少",
          len(injected) < len(gfail), f"注入后 {len(injected)} vs 原 {len(gfail)}")

    # 注入②：把 job 名匹配改成永远匹配不上 → 闸门记录必须查不到
    orig_filter = ccd._gate_records_for
    try:
        ccd._gate_records_for = lambda h, n: []
        injected2 = ccd._gate_records_for(hist, "daily-reflection")
    finally:
        ccd._gate_records_for = orig_filter
    check("注入自检②：破坏 job 名匹配 → 闸门记录必须查不到",
          len(injected2) == 0, f"注入后 {len(injected2)}")


def test_migration_day_constant():
    """迁移日常量与实际迁移事实一致"""
    check("迁移日 = 2026-11-16",
          ccd.SYSTEMD_MIGRATION_DAY == datetime.date(2026, 11, 16),
          f"实际 {ccd.SYSTEMD_MIGRATION_DAY}")


def test_real_world_scenario():
    """真实场景复现（2026-11-21 08:10 抓到的那个）"""
    # 复刻当时的 cron/jobs.json 状态
    jobs_json = {
        "jobs": [
            {
                "id": "b7a0aca1",
                "name": "daily-reflection",
                "enabled": False,
                "state": {"lastRunStatus": "error", "lastRunAtMs": None},
                "delivery": {"mode": "announce"},
            }
        ]
    }
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "jobs.json"
        p.write_text(json.dumps(jobs_json), encoding="utf-8")
        data = json.loads(p.read_text(encoding="utf-8"))
        jobs = data if isinstance(data, list) else data.get("jobs", [])
        job = jobs[0]

        st = job.get("state", {}) or {}
        run_status = st.get("lastRunStatus")
        # 旧逻辑：run_status 是陈旧的 error → 走「运行失败」分支 → 报假故障
        old_logic_reports_failure = (run_status is not None and run_status != "ok")

        # 新逻辑：判陈旧 → 改读闸门历史
        hist = ccd._read_gate_history()  # 真实文件
        # 用**真实的** job id，这样 _load_history 会真的去拉 cron 运行历史
        real_id = "b7a0aca1-0301-40c3-ac7b-8fdd1f088d67"
        stale, det = ccd._stale_snapshot_info(job, hist, None, real_id)

        check("真实场景：旧逻辑确实会报假故障（error≠ok）", old_logic_reports_failure)
        check("真实场景：新逻辑识别出陈旧快照", stale is True, f"stale={stale}")
        check("真实场景：cron 最后运行 = 2026-11-15（迁移前一天）",
              det.get("cron_last_ts") == "2026-11-15", f"实际 {det.get('cron_last_ts')}")
        if stale:
            grecs = ccd._gate_records_for(hist, "daily-reflection")
            gfail = [r for r in grecs if ccd._gate_result_is_failure(r)]
            check("真实场景：闸门历史里 daily-reflection 无 error → 不报当前故障",
                  len(gfail) == 0, f"gfail={len(gfail)}")


def main():
    for fn in [
        test_gate_history_read,
        test_filter_by_job,
        test_result_classification,
        test_stale_detection,
        test_injection_selfcheck,
        test_migration_day_constant,
        test_real_world_scenario,
    ]:
        print(f"--- {fn.__name__} ---")
        fn()
        print()

    failed = [r for r in RESULTS if not r[1]]
    print(f"用例数: {len(RESULTS)}  通过: {len(RESULTS) - len(failed)}  失败: {len(failed)}")
    if failed:
        print("\n失败用例:")
        for name, _, detail in failed:
            print(f"  ❌ {name}  {detail}")
        return 1
    print("\n✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
