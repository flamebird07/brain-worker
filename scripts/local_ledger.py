#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""brain-worker 本地能力台账（local 后端）。

纯 Python 标准库实现，数据存本地 JSONL。评分/步长/等级/身份归一/幂等指纹
与飞书后端（scripts/capability_ledger.py）共用同一纯逻辑核心
scripts/ledger_core.py（BW-DUAL-05 统一语义契约，详见该模块 docstring）：
  - 每个已验收阶段一条事件；同一 event-id 重复提交指纹一致则幂等返回，不重复计分。
  - 未确认模型身份的评价先记 identity_pending，不计分；后续 resolve 归因为 assessment。
  - 身份未知不可计分：identity_confirmed 且身份不明确时明确拒绝。
  - 名称归一仅按核心已知别名表；事件匹配按事件 agent/model 以核心规则重算
    （历史事件不迁移，归一后相同才合并）。
  - 状态 = 初始值（评分 50 / 步长 1 / 样本数 0）按文件追加顺序依次应用所有
    assessment 事件；时间字段（evaluated_at/recorded_at）只作记录，不参与排序，
    避免同秒或跨时区时间戳字符串改变重放顺序。

存储路径：环境变量 BRAIN_WORKER_LEDGER，否则 ~/.brain-worker/ledger.jsonl。
写操作用文件锁串行化（POSIX 用 fcntl.flock，Windows 用 msvcrt.locking）；
等待超过 30 秒视为获取锁失败；无可用锁原语的平台直接报错，不静默无锁继续。
"""

import argparse
import errno
import io
import json
import os
import sys
import time
from datetime import datetime

try:
    import ledger_core as core
except ImportError:  # 以 python -m scripts.local_ledger 运行时
    from scripts import ledger_core as core

OUTCOMES = core.OUTCOMES

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
        try:
            self._acquire()
        except Exception:
            self._fh.close()
            self._fh = None
            raise
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
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                    return
                except OSError as exc:
                    if time.time() >= deadline:
                        raise LedgerError("获取台账锁超时：%s" % self.path)
                    time.sleep(0.1)
        try:
            import fcntl
        except ImportError:
            raise LedgerError(
                "当前平台无可用文件锁原语（fcntl），拒绝在无互斥的情况下读写台账：%s" % self.path
            )
        deadline = time.time() + 30.0
        while True:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError:
                if time.time() >= deadline:
                    raise LedgerError("获取台账锁超时：%s" % self.path)
                time.sleep(0.1)

    def _release(self):
        if os.name == "nt":  # pragma: no cover - Windows 分支
            import msvcrt

            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
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


# ---------------------------------------------------------------- 语义核心（共享核心的本地适配）

def model_key(agent, model):
    """归一后的模型键（宽松版：不校验身份，用于存储与匹配，含 pending 事件）。"""
    return core.lenient_key(agent, model)


def event_key(event):
    """按事件的 agent/model 以核心规则重算键；历史事件不迁移。

    单一历史存储键经重算兼容匹配；归一后仍不同（不同模型版本等）保持
    分离，不静默合并。
    """
    return core.lenient_key(event.get("agent"), event.get("model"))


def stored_keys_for(events, key):
    """同一规范身份下已存在的存储键集合（按事件 agent/model 重算匹配）。

    仅统计计分事件（assessment）：碰撞与存储身份针对计分对象，pending 事件
    不参与计分，其存储键不构成碰撞。
    """
    return {e.get("model_key") for e in events
            if e.get("type") == EVENT_ASSESSMENT and event_key(e) == key}


def guard_key_collision(events, key):
    """历史碰撞守卫（统一契约）：多个历史存储键映射到同一规范身份时拒绝。

    禁止静默汇总、迁移、重算或改写历史；相关读写（status/record/resolve）
    均须先通过本守卫。零追加、零外部写入。
    """
    stored = stored_keys_for(events, key)
    if len(stored) > 1:
        raise LedgerError(core.collision_message(key, stored))


def plan_store_key(events, key):
    """写前规划：返回新事件应使用的存储键；多个历史存储键时零写入拒绝。

    单一历史别名对象沿用其既有存储键（规范键只用于匹配），从而不制造第二个
    存储身份；既有真实碰撞直接拒绝。必须在 append 之前调用。
    """
    try:
        return core.plan_stored_key(key, stored_keys_for(events, key))
    except core.LedgerCoreError as exc:
        raise LedgerError(str(exc))


def transition(score, step, samples, outcome):
    """共享核心转换；未知结论转为本地停止条件。"""
    try:
        return core.transition(score, step, samples, outcome)
    except core.LedgerCoreError as exc:
        raise LedgerError(str(exc))


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
    """追加式 JSONL 的唯一重放顺序是文件追加顺序（行号 _seq）。

    时间字段不参与排序：recorded_at 秒级精度存在同秒并列，ISO 字符串在
    不同时区偏移下字典序也不等于时间先后；按时间排序会改写追加顺序。
    """
    return (event.get("_seq", 0),)


def assessments_for(events, key):
    hits = [e for e in events if e.get("type") == EVENT_ASSESSMENT and event_key(e) == key]
    return sorted(hits, key=sort_key)


def state_for(events, key):
    score, step, samples = core.INIT_SCORE, core.INIT_STEP, core.INIT_SAMPLES
    applied = []
    for event in assessments_for(events, key):
        score, step, samples, _delta = transition(score, step, samples, event.get("outcome"))
        applied.append(event)
    return {"score": score, "step": step, "samples": samples, "events": applied}


def level_of(score, samples, recent):
    return core.level(score, samples, recent)


def status_payload(events, agent, model):
    key = model_key(agent, model)
    guard_key_collision(events, key)
    st = state_for(events, key)
    recent = [e.get("outcome") for e in st["events"]][-3:]
    pending = [e for e in events if e.get("type") == EVENT_IDENTITY_PENDING and event_key(e) == key]
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
    """幂等指纹（共享核心）：model_key 统一取按事件 agent/model 重算的
    规范键（新旧事件同一规则，历史存储键差异不影响原样重试幂等）；
    issues 排序后比较（顺序不敏感）；recorded_at、行号等生成字段不参与。
    """
    norm = dict(event)
    norm["model_key"] = event_key(event)
    return core.idempotency_fingerprint(norm)


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
    # 统一契约：身份未知不可计分；pending 允许“待确认模型”占位。
    try:
        core.canonical_triple(agent, model, allow_pending=(etype == EVENT_IDENTITY_PENDING))
    except core.LedgerCoreError as exc:
        raise LedgerError(str(exc))
    event = build_event(args.event_id, etype, args, agent, model, outcome, args.issue, identity_confirmed)

    with FileLock(path):
        events = read_events(path)
        # 写前规划存储键：单一历史别名对象沿用其既有存储键（规范键只用于匹配），
        # 既有碰撞在此零写入拒绝——不再"先追加再报碰撞"。
        event["model_key"] = plan_store_key(events, model_key(agent, model))
        same = find_event(events, args.event_id)
        if len(same) > 1:
            raise LedgerError("台账内存在重复 event-id：%s，先人工核对事件链再继续。" % args.event_id)
        if same:
            found = same[0]
            if content_of(found) != content_of(event):
                raise LedgerError(
                    "event-id %s 已存在且内容不一致；拒绝改写历史事件。"
                    "请保留原事件 ID，核对原事件内容后修正重试参数再重试，"
                    "或走明确补证流程；不得换新 ID 重复计分。" % args.event_id
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
    # resolve 是补计分：目标身份必须明确（统一契约：身份未知不可计分）。
    try:
        core.canonical_triple(agent, model, allow_pending=False)
    except core.LedgerCoreError as exc:
        raise LedgerError(str(exc))

    with FileLock(path):
        events = read_events(path)
        # 写前规划存储键（与 record 同一规则）：单一历史别名对象沿用其既有键，
        # 既有碰撞在此零写入拒绝。
        store_key = plan_store_key(events, model_key(agent, model))
        pending = [
            e for e in events
            if e.get("type") == EVENT_IDENTITY_PENDING and e.get("event_id") == args.pending_event_id
        ]
        if not pending:
            raise LedgerError("找不到待确认事件：%s" % args.pending_event_id)
        if len(pending) > 1:
            raise LedgerError("待确认事件重复：%s" % args.pending_event_id)
        target = pending[0]
        # 统一契约：桌面应用按核心归一规则比较（已知别名前缀视为同一应用）。
        if core.canonical_agent(target.get("agent")) != core.canonical_agent(agent):
            raise LedgerError(
                "待确认事件 %s 的桌面应用为 %s，与本次 --agent %s 不匹配；拒绝错误归因。"
                % (args.pending_event_id, target.get("agent"), agent)
            )

        new_id = core.resolve_id(args.pending_event_id)
        resolved = {
            "event_id": new_id,
            "type": EVENT_ASSESSMENT,
            "agent": agent,
            "model": model,
            "model_key": store_key,
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
                raise LedgerError(
                    "补评事件 %s 已存在且内容不一致。请保留原补评 ID，"
                    "核对原补证内容后修正重试；不得换新 ID 重复计分。" % new_id
                )
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
