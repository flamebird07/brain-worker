#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""brain-worker 本地能力台账（local 后端）。

纯 Python 标准库实现，数据存本地 JSONL。评分、步长与幂等语义与
scripts/capability_ledger.py（飞书后端）保持一致：
  - 每个已验收阶段一条事件；同一 event-id 重复提交内容一致则幂等返回，不重复计分。
  - 未确认模型身份的评价先记 identity_pending，不计分；后续 resolve 归因为 assessment。
  - 状态 = 初始值（评分 50 / 步长 1 / 样本数 0）按记录时间依次应用所有 assessment 事件。

存储路径：环境变量 BRAIN_WORKER_LEDGER，否则 ~/.brain-worker/ledger.jsonl。
写操作用文件锁串行化（POSIX 用 fcntl.flock，Windows 用 msvcrt.locking）。
"""

import argparse
import errno
import io
import json
import os
import sys
import time
from datetime import datetime

OUTCOMES = {
    "无问题": (10, 1),
    "轻微问题": (-5, -1),
    "重大问题": (-15, -2),
    "严重问题": (-30, None),
    "证据不足": (0, 0),
}

INIT_SCORE = 50
INIT_STEP = 1
INIT_SAMPLES = 0
SCORE_MIN, SCORE_MAX = 0, 100
STEP_MIN, STEP_MAX = 1, 5
RECOMMEND_RECENT_BAN = ("重大问题", "严重问题")

EVENT_ASSESSMENT = "assessment"
EVENT_IDENTITY_PENDING = "identity_pending"


class LedgerError(Exception):
    """可预期的停止条件：不重试、不猜测，直接报告。"""


# ---------------------------------------------------------------- 锁与存储

def ledger_path():
    env = os.environ.get("BRAIN_WORKER_LEDGER")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(os.path.expanduser("~"), ".brain-worker", "ledger.jsonl")


class FileLock(object):
    """跨进程互斥锁；同一台主机上串行化不同对话的写入。"""

    def __init__(self, path):
        self.path = path + ".lock"
        self._fh = None

    def __enter__(self):
        parent = os.path.dirname(self.path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        self._fh = io.open(self.path, "a+b")
        self._acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self._release()
        finally:
            self._fh.close()
            self._fh = None
        return False

    def _acquire(self):
        if os.name == "nt":  # pragma: no cover - Windows 分支
            import msvcrt

            deadline = time.time() + 30.0
            while True:
                try:
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
                    return
                except OSError as exc:
                    if time.time() >= deadline:
                        raise LedgerError("获取台账锁超时：%s" % self.path)
                    time.sleep(0.1)
        try:
            import fcntl
        except ImportError:
            return  # 无可用锁原语的平台：单进程仍安全
        self._fh.seek(0)
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)

    def _release(self):
        if os.name == "nt":  # pragma: no cover - Windows 分支
            import msvcrt

            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK)
            return
        try:
            import fcntl
        except ImportError:
            return
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)


def read_events(path):
    events = []
    if not os.path.isfile(path):
        return events
    with io.open(path, "r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError as exc:
                raise LedgerError("台账第 %d 行不是合法 JSON：%s" % (lineno, exc))
            if not isinstance(obj, dict) or "event_id" not in obj or "type" not in obj:
                raise LedgerError("台账第 %d 行缺少 event_id/type，拒绝在损坏文件上继续。" % lineno)
            obj["_seq"] = lineno
            events.append(obj)
    return events


def append_event(path, event):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with io.open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


# ---------------------------------------------------------------- 语义核心

def model_key(agent, model):
    return "%s / %s" % (agent.strip(), model.strip())


def clamp(value, low, high):
    return max(low, min(high, value))


def transition(score, step, samples, outcome):
    """返回 (评分, 步长, 样本数, 本次评分变化)。"""
    if outcome not in OUTCOMES:
        raise LedgerError("未知结论：%s；允许值为 %s" % (outcome, "、".join(sorted(OUTCOMES))))
    delta, step_delta = OUTCOMES[outcome]

    if score is None and outcome == "证据不足":
        return (None, step, samples, 0)
    if score is None:
        score = INIT_SCORE

    new_score = clamp(score + delta, SCORE_MIN, SCORE_MAX)
    if step_delta is None:
        new_step = STEP_MIN
    else:
        base = INIT_STEP if step is None else step
        new_step = clamp(base + step_delta, STEP_MIN, STEP_MAX)
    new_samples = samples + (0 if outcome == "证据不足" else 1)
    return (new_score, new_step, new_samples, delta)


def parse_time(value, field):
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise LedgerError("%s 不是合法 ISO 时间：%s" % (field, value))
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise LedgerError("%s 必须带时区偏移，例如 2026-09-30T12:00:00+08:00；当前为 %s" % (field, value))
    return dt


def sort_key(event):
    return (event.get("recorded_at") or "", event.get("evaluated_at") or "", event.get("_seq", 0))


def assessments_for(events, key):
    hits = [e for e in events if e.get("type") == EVENT_ASSESSMENT and e.get("model_key") == key]
    return sorted(hits, key=sort_key)


def state_for(events, key):
    score, step, samples = INIT_SCORE, INIT_STEP, INIT_SAMPLES
    applied = []
    for event in assessments_for(events, key):
        score, step, samples, _delta = transition(score, step, samples, event.get("outcome"))
        applied.append(event)
    return {"score": score, "step": step, "samples": samples, "events": applied}


def level_of(score, samples, recent):
    if score is None:
        base = "未定级"
    elif score >= 80 and samples >= 3 and not any(r in RECOMMEND_RECENT_BAN for r in recent):
        base = "推荐"
    elif score >= 60:
        base = "可用但需逐阶段审查"
    elif score >= 40:
        base = "观察"
    else:
        base = "受限或暂不推荐"
    if samples < 3:
        base += "（试用中）"
    return base


def status_payload(events, agent, model):
    key = model_key(agent, model)
    st = state_for(events, key)
    recent = [e.get("outcome") for e in st["events"]][-3:]
    pending = [e for e in events if e.get("type") == EVENT_IDENTITY_PENDING and e.get("model_key") == key]
    return {
        "模型键": key,
        "共享评分": st["score"],
        "当前步长": st["step"],
        "累计样本数": st["samples"],
        "能力等级": level_of(st["score"], st["samples"], recent),
        "最近结论": st["events"][-1].get("outcome") if st["events"] else None,
        "事件数": len(st["events"]),
        "待确认身份事件数": len(pending),
        "最近三次结论": recent,
    }


def emit(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def content_of(event):
    """幂等比较用的内容指纹；不含写入时间等每次都会变的字段。"""
    keys = ("event_id", "type", "agent", "model", "model_key", "task", "stage",
            "evaluated_at", "outcome", "evidence", "issues", "identity_confirmed", "resolved_from")
    return {k: event.get(k) for k in keys}


def find_event(events, event_id):
    return [e for e in events if e.get("event_id") == event_id]


def merged(base, extra):
    """Python 3.8 兼容的字典合并（不能用 dict | dict）。"""
    out = dict(base)
    out.update(extra)
    return out


# ---------------------------------------------------------------- 命令

def cmd_status(args, path):
    with FileLock(path):
        events = read_events(path)
    emit(status_payload(events, args.agent, args.model))
    return 0


def build_event(event_id, etype, args, agent, model, outcome, issues, identity_confirmed, resolved_from=None):
    return {
        "event_id": event_id,
        "type": etype,
        "agent": agent,
        "model": model,
        "model_key": model_key(agent, model),
        "task": args.task,
        "stage": args.stage,
        "evaluated_at": args.evaluated_at.strip(),
        "outcome": outcome,
        "issues": list(issues or []),
        "evidence": args.evidence,
        "identity_confirmed": bool(identity_confirmed),
        "resolved_from": resolved_from,
        "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def cmd_record(args, path):
    agent = args.agent.strip()
    model = args.model.strip()
    outcome = args.outcome.strip()
    if not agent or not model:
        raise LedgerError("--agent 与 --model 不能为空。")
    if outcome not in OUTCOMES:
        raise LedgerError("未知结论：%s；允许值为 %s" % (outcome, "、".join(sorted(OUTCOMES))))
    if not args.evidence:
        raise LedgerError("--evidence 不能为空。")
    parse_time(args.evaluated_at, "--evaluated-at")
    identity_confirmed = bool(args.identity_confirmed)

    etype = EVENT_ASSESSMENT if (outcome == "证据不足" or identity_confirmed) else EVENT_IDENTITY_PENDING
    event = build_event(args.event_id, etype, args, agent, model, outcome, args.issue, identity_confirmed)

    with FileLock(path):
        events = read_events(path)
        same = find_event(events, args.event_id)
        if len(same) > 1:
            raise LedgerError("台账内存在重复 event-id：%s，先人工核对事件链再继续。" % args.event_id)
        if same:
            found = same[0]
            if content_of(found) != content_of(event):
                raise LedgerError(
                    "event-id %s 已存在且内容不一致；拒绝改写历史事件。请用新的稳定唯一标识。" % args.event_id
                )
            emit(merged(status_payload(events, agent, model), {
                "idempotent": True,
                "action": "record",
                "event_id": args.event_id,
                "event_type": found.get("type"),
            }))
            return 0
        append_event(path, event)
        events = read_events(path)
    emit(merged(status_payload(events, agent, model), {
        "idempotent": False,
        "action": "record",
        "event_id": args.event_id,
        "event_type": etype,
        "计分": etype == EVENT_ASSESSMENT,
    }))
    return 0


def cmd_resolve(args, path):
    if not args.identity_confirmed:
        raise LedgerError("resolve 必须带 --identity-confirmed 才允许补计分。")
    if not args.identity_evidence or not args.identity_evidence.strip():
        raise LedgerError("--identity-evidence 不能为空。")
    agent = args.agent.strip()
    model = args.model.strip()
    if not agent or not model:
        raise LedgerError("--agent 与 --model 不能为空。")

    with FileLock(path):
        events = read_events(path)
        pending = [
            e for e in events
            if e.get("type") == EVENT_IDENTITY_PENDING and e.get("event_id") == args.pending_event_id
        ]
        if not pending:
            raise LedgerError("找不到待确认事件：%s" % args.pending_event_id)
        if len(pending) > 1:
            raise LedgerError("待确认事件重复：%s" % args.pending_event_id)
        target = pending[0]
        if target.get("agent") != agent:
            raise LedgerError(
                "待确认事件 %s 的桌面应用为 %s，与本次 --agent %s 不匹配；拒绝错误归因。"
                % (args.pending_event_id, target.get("agent"), agent)
            )

        new_id = "resolve:%s" % args.pending_event_id
        resolved = {
            "event_id": new_id,
            "type": EVENT_ASSESSMENT,
            "agent": agent,
            "model": model,
            "model_key": model_key(agent, model),
            "task": target.get("task"),
            "stage": target.get("stage"),
            "evaluated_at": target.get("evaluated_at"),
            "outcome": target.get("outcome"),
            "issues": target.get("issues") or [],
            "evidence": args.identity_evidence.strip(),
            "identity_confirmed": True,
            "resolved_from": args.pending_event_id,
            "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        same = [e for e in events if e.get("event_id") == new_id]
        if same:
            if content_of(same[0]) != content_of(resolved):
                raise LedgerError("补评事件 %s 已存在且内容不一致。" % new_id)
            emit(merged(status_payload(events, agent, model), {
                "idempotent": True,
                "action": "resolve",
                "event_id": new_id,
                "event_type": same[0].get("type"),
            }))
            return 0
        append_event(path, resolved)
        events = read_events(path)
    emit(merged(status_payload(events, agent, model), {
        "idempotent": False,
        "action": "resolve",
        "event_id": new_id,
        "event_type": EVENT_ASSESSMENT,
        "归因自": args.pending_event_id,
    }))
    return 0


# ---------------------------------------------------------------- CLI

def build_parser():
    parser = argparse.ArgumentParser(
        prog="local_ledger.py",
        description="brain-worker 本地 JSONL 能力台账（local 后端，语义与飞书后端一致）。",
    )
    sub = parser.add_subparsers(dest="command")

    def common(sp):
        sp.add_argument("--agent", required=True, help="桌面应用")
        sp.add_argument("--model", required=True, help="模型名称/版本")

    sp_status = sub.add_parser("status", help="读取共享评分、步长、样本数与能力等级")
    common(sp_status)

    sp_record = sub.add_parser("record", help="追加一次阶段评价事件")
    common(sp_record)
    sp_record.add_argument("--event-id", required=True, dest="event_id")
    sp_record.add_argument("--task", required=True)
    sp_record.add_argument("--stage", required=True)
    sp_record.add_argument("--evaluated-at", required=True, dest="evaluated_at")
    sp_record.add_argument("--outcome", required=True)
    sp_record.add_argument("--evidence", required=True)
    sp_record.add_argument("--issue", action="append", default=[])
    sp_record.add_argument("--identity-confirmed", action="store_true", dest="identity_confirmed")

    sp_resolve = sub.add_parser("resolve", help="把待确认身份事件补计到已确认模型")
    common(sp_resolve)
    sp_resolve.add_argument("--pending-event-id", required=True, dest="pending_event_id")
    sp_resolve.add_argument("--identity-evidence", required=True, dest="identity_evidence")
    sp_resolve.add_argument("--identity-confirmed", action="store_true", dest="identity_confirmed")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help(sys.stderr)
        return 2
    path = ledger_path()
    handlers = {"status": cmd_status, "record": cmd_record, "resolve": cmd_resolve}
    try:
        return handlers[args.command](args, path)
    except LedgerError as exc:
        sys.stderr.write("错误：%s\n" % exc)
        return 1
    except OSError as exc:
        if exc.errno == errno.EACCES:
            sys.stderr.write("错误：台账文件不可写：%s（%s）\n" % (path, exc.strerror))
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
