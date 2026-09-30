#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""local_ledger（本地台账）回归测试。

对应 BW-DUAL-02 修复：Windows 解锁缺参、追加顺序重放、锁超时/句柄清理。
全部通过真实子进程与真实文件锁验证；台账一律用 BRAIN_WORKER_LEDGER
重定向到本测试的临时目录，绝不写默认 ~/.brain-worker/。

从仓库根运行：python -m scripts.test_local_ledger
锁超时用例需要约 33 秒（30 秒锁等待 + 收尾）。
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "local_ledger.py"


def run_cli(ledger, *args, timeout=90, cwd=None):
    env = dict(os.environ)
    env["BRAIN_WORKER_LEDGER"] = str(ledger)
    env["PYTHONUTF8"] = "1"  # 子进程输出统一 UTF-8，跨平台稳定
    return subprocess.run(
        [sys.executable, str(SCRIPT)] + list(args),
        capture_output=True, text=True, encoding="utf-8",
        timeout=timeout, env=env, cwd=str(cwd) if cwd else None,
    )


def record_args(event_id, outcome="无问题", evidence="ev", confirmed=True, **kw):
    args = ["record", "--event-id", event_id,
            "--agent", kw.get("agent", "ZCode"), "--model", kw.get("model", "GLM"),
            "--task", "审查", "--stage", kw.get("stage", "S1"),
            "--evaluated-at", kw.get("evaluated_at", "2026-09-30T10:00:00+08:00"),
            "--outcome", outcome, "--evidence", evidence]
    if confirmed:
        args.append("--identity-confirmed")
    for issue in kw.get("issues", []):
        args += ["--issue", issue]
    return args


def parse_json(stdout):
    return json.loads(stdout.strip().splitlines()[-1])


def read_lines(ledger):
    if not os.path.isfile(ledger):
        return []
    with open(ledger, "r", encoding="utf-8") as fh:
        return [line for line in fh.read().splitlines() if line.strip()]


def write_event_line(ledger, index, **fields):
    """按 local_ledger 事件结构手工构造一行（测试数据准备，非绕锁写台账）。"""
    base = {"agent": "ZCode", "model": "GLM", "model_key": "ZCode / GLM",
            "task": "审查", "stage": "S%d" % index, "outcome": "无问题", "issues": [],
            "evidence": "ev%d" % index, "identity_confirmed": True, "resolved_from": None,
            "evaluated_at": "2026-09-30T10:00:00+08:00",
            "recorded_at": "2026-09-30T15:00:00+08:00"}
    base.update(fields)
    return json.dumps(base, ensure_ascii=False, sort_keys=True)


HOLDER_CODE = (
    "import os, sys, time\n"
    "sys.path.insert(0, %r)\n"
    "import local_ledger as L\n"
    "lock = L.FileLock(%r)\n"
    "lock.__enter__()\n"
    "print('HELD', flush=True)\n"
    "time.sleep(%s)\n"
    "lock.__exit__(None, None, None)\n"
)

CRASH_CODE = (
    "import os, sys\n"
    "sys.path.insert(0, %r)\n"
    "import local_ledger as L\n"
    "lock = L.FileLock(%r)\n"
    "lock.__enter__()\n"
    "print('HELD', flush=True)\n"
    "os._exit(1)\n"
)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bw-ledger-test-")
        self.ledger = os.path.join(self.tmp, "ledger.jsonl")

    def status(self, **kw):
        return run_cli(self.ledger, "status", "--agent", "ZCode", "--model", "GLM", **kw)

    def status_payload(self):
        proc = self.status()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return parse_json(proc.stdout)


class TestCliLifecycle(Base):
    def test_status_fresh_initial_state(self):
        payload = self.status_payload()
        self.assertEqual((payload["共享评分"], payload["当前步长"], payload["累计样本数"]), (50, 1, 0))
        self.assertIn("观察", payload["能力等级"])
        self.assertIn("试用中", payload["能力等级"])

    def test_record_roundtrip_and_reread(self):
        proc = run_cli(self.ledger, *record_args("e1"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = parse_json(proc.stdout)
        self.assertFalse(payload["idempotent"])
        self.assertEqual(payload["event_type"], "assessment")
        self.assertTrue(payload["计分"])
        self.assertEqual((payload["共享评分"], payload["当前步长"], payload["累计样本数"]), (60, 2, 1))
        self.assertEqual(len(read_lines(self.ledger)), 1)
        # 回读：另一个进程 status 得到同一状态（跨进程读取）
        reread = self.status_payload()
        self.assertEqual((reread["共享评分"], reread["当前步长"], reread["累计样本数"]), (60, 2, 1))
        self.assertEqual(reread["最近结论"], "无问题")

    def test_idempotent_same_content_no_rescore(self):
        run_cli(self.ledger, *record_args("e1"))
        again = run_cli(self.ledger, *record_args("e1"))
        self.assertEqual(again.returncode, 0, again.stderr)
        payload = parse_json(again.stdout)
        self.assertTrue(payload["idempotent"])
        self.assertEqual(len(read_lines(self.ledger)), 1)
        self.assertEqual(self.status_payload()["累计样本数"], 1)

    def test_conflicting_content_rejected_and_lock_released(self):
        run_cli(self.ledger, *record_args("e1", evidence="ev"))
        conflict = run_cli(self.ledger, *record_args("e1", evidence="ev-CHANGED"))
        self.assertEqual(conflict.returncode, 1)
        self.assertIn("内容不一致", conflict.stderr)
        self.assertNotIn("Traceback", conflict.stderr)
        self.assertEqual(len(read_lines(self.ledger)), 1)  # 不追加
        # 锁正常释放：后续命令立即成功
        self.assertEqual(self.status().returncode, 0)

    def test_pending_not_scored_then_resolve_once(self):
        pending = run_cli(self.ledger, *record_args("p1", confirmed=False))
        self.assertEqual(pending.returncode, 0, pending.stderr)
        payload = parse_json(pending.stdout)
        self.assertEqual(payload["event_type"], "identity_pending")
        self.assertFalse(payload["计分"])
        after_pending = self.status_payload()
        self.assertEqual((after_pending["共享评分"], after_pending["累计样本数"], after_pending["事件数"]), (50, 0, 0))
        self.assertEqual(after_pending["待确认身份事件数"], 1)

        resolve = run_cli(self.ledger, "resolve", "--pending-event-id", "p1",
                          "--agent", "ZCode", "--model", "GLM",
                          "--identity-evidence", "user-confirm", "--identity-confirmed")
        self.assertEqual(resolve.returncode, 0, resolve.stderr)
        resolved = parse_json(resolve.stdout)
        self.assertEqual((resolved["共享评分"], resolved["当前步长"], resolved["累计样本数"]), (60, 2, 1))

        again = run_cli(self.ledger, "resolve", "--pending-event-id", "p1",
                        "--agent", "ZCode", "--model", "GLM",
                        "--identity-evidence", "user-confirm", "--identity-confirmed")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertTrue(parse_json(again.stdout)["idempotent"])
        self.assertEqual(self.status_payload()["累计样本数"], 1)  # 只补计一次

    def test_resolve_agent_mismatch_rejected(self):
        run_cli(self.ledger, *record_args("p1", confirmed=False))
        proc = run_cli(self.ledger, "resolve", "--pending-event-id", "p1",
                       "--agent", "Other", "--model", "GLM",
                       "--identity-evidence", "x", "--identity-confirmed")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("不匹配", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_invalid_inputs_clean_errors(self):
        no_tz = run_cli(self.ledger, *record_args("t1", evaluated_at="2026-09-30T12:00:00"))
        self.assertEqual(no_tz.returncode, 1)
        self.assertIn("时区", no_tz.stderr)
        bad_outcome = run_cli(self.ledger, *record_args("t2", outcome="超纲结论"))
        self.assertEqual(bad_outcome.returncode, 1)
        self.assertIn("未知结论", bad_outcome.stderr)
        self.assertFalse(os.path.exists(self.ledger))  # 未落盘

    def test_corrupt_file_blocks_and_never_appends(self):
        with open(self.ledger, "w", encoding="utf-8") as fh:
            fh.write('{"event_id": "ok", "type": "assessment"}\n{broken\n')
        status = self.status()
        self.assertEqual(status.returncode, 1)
        self.assertIn("不是合法 JSON", status.stderr)
        before = len(read_lines(self.ledger))
        record = run_cli(self.ledger, *record_args("e9"))
        self.assertEqual(record.returncode, 1)
        self.assertIn("不是合法 JSON", record.stderr)
        self.assertEqual(len(read_lines(self.ledger)), before)  # 损坏文件上不追加

    def test_legacy_file_compatible_append_order(self):
        """BW-DUAL-01 场景回归：时间戳正常递增的既有文件，追加序结果不变。"""
        outcomes = [("a", "无问题", "10:00"), ("b", "轻微问题", "11:00"),
                    ("c", "重大问题", "12:00"), ("d", "证据不足", "13:00")]
        with open(self.ledger, "w", encoding="utf-8") as fh:
            for i, (eid, outcome, hh) in enumerate(outcomes, 1):
                fh.write(write_event_line(self.ledger, i, event_id=eid, type="assessment",
                                          outcome=outcome,
                                          evaluated_at="2026-09-30T%s:00+08:00" % hh,
                                          recorded_at="2026-09-30T15:00:0%d+08:00" % i) + "\n")
        payload = self.status_payload()
        self.assertEqual((payload["共享评分"], payload["当前步长"], payload["累计样本数"]), (40, 1, 3))
        self.assertEqual(payload["事件数"], 4)


class TestAppendOrderReplay(Base):
    def _write_two(self, first, second):
        with open(self.ledger, "w", encoding="utf-8") as fh:
            fh.write(first + "\n")
            fh.write(second + "\n")

    def test_same_second_timestamps_follow_append_order(self):
        """同秒 recorded_at + evaluated_at 乱序：按追加序，不再被 evaluated_at 改写。"""
        self._write_two(
            write_event_line(self.ledger, 1, event_id="late+", type="assessment", outcome="无问题",
                             evaluated_at="2026-09-30T20:00:00+08:00",
                             recorded_at="2026-09-30T15:00:00+08:00"),
            write_event_line(self.ledger, 2, event_id="early-", type="assessment", outcome="严重问题",
                             evaluated_at="2026-09-30T09:00:00+08:00",
                             recorded_at="2026-09-30T15:00:00+08:00"))
        payload = self.status_payload()
        # 追加序：50+10=60（步长2）→ 60-30=30（步长归1）
        self.assertEqual((payload["共享评分"], payload["当前步长"]), (30, 1))
        self.assertEqual(payload["最近结论"], "严重问题")

    def test_cross_timezone_timestamps_follow_append_order(self):
        """跨时区 recorded_at 字符串序与时间序相反：仍按追加序。"""
        self._write_two(
            write_event_line(self.ledger, 1, event_id="plus8", type="assessment", outcome="无问题",
                             recorded_at="2026-09-30T23:00:00+08:00"),
            write_event_line(self.ledger, 2, event_id="utc", type="assessment", outcome="重大问题",
                             recorded_at="2026-09-30T14:00:00+00:00"))
        payload = self.status_payload()
        # 追加序：50+10=60（步长2）→ 60-15=45（步长1）
        self.assertEqual((payload["共享评分"], payload["当前步长"]), (45, 1))


class TestRealLocks(Base):
    def _spawn_holder(self, seconds):
        code = HOLDER_CODE % (str(SCRIPT.parent), str(self.ledger), seconds)
        env = dict(os.environ, PYTHONUTF8="1")
        return subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env)

    def test_mutual_exclusion_serializes_writers(self):
        holder = self._spawn_holder(3.0)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "HELD")
            started = time.monotonic()
            writer = run_cli(self.ledger, *record_args("w1"))
            elapsed = time.monotonic() - started
            self.assertEqual(writer.returncode, 0, writer.stderr)
            self.assertGreaterEqual(elapsed, 1.5, "写入方应等待持锁者释放")
        finally:
            holder.wait(timeout=15)
        payload = self.status_payload()
        self.assertEqual(payload["事件数"], 1)
        self.assertEqual(len(read_lines(self.ledger)), 1)

    def test_lock_timeout_then_recoverable(self):
        holder = self._spawn_holder(33.0)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "HELD")
            started = time.monotonic()
            blocked = self.status()
            elapsed = time.monotonic() - started
            self.assertEqual(blocked.returncode, 1)
            self.assertIn("获取台账锁超时", blocked.stderr)
            self.assertNotIn("Traceback", blocked.stderr)
            self.assertGreaterEqual(elapsed, 25.0, "应在约 30 秒锁等待后失败")
        finally:
            holder.wait(timeout=45)
        # 持锁者释放后锁立即恢复可用
        self.assertEqual(self.status().returncode, 0)

    def test_crashed_holder_releases_lock(self):
        env = dict(os.environ, PYTHONUTF8="1")
        crash = subprocess.Popen([sys.executable, "-c", CRASH_CODE % (str(SCRIPT.parent), str(self.ledger))],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, encoding="utf-8", env=env)
        try:
            self.assertEqual(crash.stdout.readline().strip(), "HELD")
        finally:
            crash.wait(timeout=15)
            crash.stdout.close()
            crash.stderr.close()
        self.assertEqual(crash.returncode, 1)
        proc = run_cli(self.ledger, *record_args("after-crash"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.status_payload()["事件数"], 1)

    def test_concurrent_writers_no_loss_no_dup_no_corruption(self):
        procs = []
        for i in range(8):
            args = record_args("c%d" % i, stage="S%d" % i,
                               evaluated_at="2026-09-30T1%d:00:00+08:00" % i)
            env = dict(os.environ, BRAIN_WORKER_LEDGER=self.ledger, PYTHONUTF8="1")
            procs.append(subprocess.Popen(
                [sys.executable, str(SCRIPT)] + args,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", env=env))
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err or out)
        lines = read_lines(self.ledger)
        self.assertEqual(len(lines), 8)
        event_ids = [json.loads(line)["event_id"] for line in lines]
        self.assertEqual(sorted(event_ids), ["c%d" % i for i in range(8)])
        payload = self.status_payload()
        self.assertEqual(payload["事件数"], 8)
        self.assertEqual((payload["共享评分"], payload["当前步长"], payload["累计样本数"]), (100, 5, 8))
        self.assertEqual(payload["能力等级"], "推荐")


class TestPathSelection(Base):
    def test_relative_env_path_resolves_against_cwd(self):
        rel = os.path.join("sub", "rel-ledger.jsonl")
        proc = run_cli(rel, *record_args("r1"), cwd=self.tmp)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "sub", "rel-ledger.jsonl")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
