"""Shared, append-only Feishu capability ledger for brain-worker.

Use `status` immediately before a prompt and `record` after independent review.
The local lock serializes updates from separate conversations on this host.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

APP = "YedtbFYKZatu2QsGti9ch7xbnGc"
SUMMARY = "tbl2Ewf7aE2H2ffi"
EVENTS = "tblPM6oPS5g3gNhr"
BASE = f"bitable/v1/apps/{APP}/tables"
API = "https://open.feishu.cn/open-apis"
TZ = timezone(timedelta(hours=8))
LEGACY = Path(r"E:\obsidian\obs\AI知识库\wiki\主题\牛马 AI 能力评估记录.md")
OUTCOMES = {"无问题": (10, 1), "轻微问题": (-5, -1), "重大问题": (-15, -2), "严重问题": (-30, None), "证据不足": (0, 0)}


def credentials():
    app_id, secret = os.getenv("FEISHU_APP_ID"), os.getenv("FEISHU_APP_SECRET")
    if app_id and secret:
        return app_id, secret
    try:
        import yaml
        path = Path(os.getenv("HERMES_CONFIG", Path.home() / "AppData/Local/hermes/config.yaml"))
        extra = yaml.safe_load(path.read_text(encoding="utf-8"))["platforms"]["feishu"]["extra"]
        return extra["app_id"], extra["app_secret"]
    except (ImportError, OSError, KeyError, TypeError) as exc:
        raise RuntimeError("无法读取本机 Hermes 飞书机器人配置") from exc


def request(method, path, token=None, body=None, params=None):
    url = f"{API}/{path}"
    if params:
        url += "?" + urlencode(params)
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    try:
        with urlopen(Request(url, payload, headers, method=method), timeout=30) as response:
            result = json.load(response)
    except (HTTPError, URLError) as exc:
        raise RuntimeError("飞书请求失败；请核对权限、网络和当前写入状态") from exc
    if result.get("code") != 0:
        raise RuntimeError(f"飞书错误 {result.get('code')}: {result.get('msg')}")
    return result.get("data") or result


def token():
    app_id, secret = credentials()
    return request("POST", "auth/v3/tenant_access_token/internal", body={"app_id": app_id, "app_secret": secret})["tenant_access_token"]


def records(table, bearer):
    result, cursor = [], None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["page_token"] = cursor
        data = request("GET", f"{BASE}/{table}/records", bearer, params=params)
        result.extend(data.get("items") or [])
        if not data.get("has_more"):
            return result
        cursor = data.get("page_token")
        if not cursor:
            raise RuntimeError("飞书分页游标缺失")


def canonical(agent, model):
    agent = agent.strip()
    model = model.strip()
    if agent.startswith("ZCode"):
        agent = "ZCode"
    elif agent.startswith("Xiaomi MiMo"):
        agent = "Xiaomi MiMo"
    if "官方 GLM-5.3-Flash" in model:
        model = "ZCode 官方 GLM-5.3-Flash"
    elif "火山 GLM-5.3" in model:
        model = "火山 GLM-5.3"
    elif "kimi-k2.8-preview" in model:
        model = "火山 kimi-k2.8-preview"
    elif "MiMo V2.6 Pro" in model:
        model = "MiMo V2.6 Pro"
    if not agent or not model or "未确认" in model or "未知" in model:
        raise RuntimeError("Agent 或模型身份不明确，不能合并评分")
    return agent, model, f"{agent} / {model}"


def number(value):
    return None if value in (None, "") else int(value)


def transition(score, step, samples, outcome):
    delta, step_delta = OUTCOMES[outcome]
    if score is None:
        if outcome == "证据不足":
            return None, step, samples, 0
        score = 50
    new_score = max(0, min(100, score + delta))
    new_step = 1 if step_delta is None else max(1, min(5, step + step_delta))
    new_samples = samples + (outcome != "证据不足")
    return new_score, new_step, new_samples, delta


def model_events(all_events, key):
    found = [r for r in all_events if r["fields"].get("模型键") == key]
    baselines = [r for r in found if r["fields"].get("事件类型") == "baseline"]
    if len(baselines) > 1:
        raise RuntimeError("同一模型存在多个基线；停止评分")
    return found, baselines[0] if baselines else None


def state(all_events, key):
    found, baseline = model_events(all_events, key)
    if baseline:
        bf = baseline["fields"]
        score, step, samples = number(bf.get("评分后")), number(bf.get("步长后")) or 1, number(bf.get("样本数后")) or 0
    else:
        score, step, samples = 50, 1, 0
    changes = sorted((r["fields"] for r in found if r["fields"].get("事件类型") == "assessment"),
                     key=lambda f: (f.get("记录时间", ""), f.get("事件ID", "")))
    for event in changes:
        outcome = event.get("主脑结论")
        if outcome not in OUTCOMES:
            raise RuntimeError("未知评分结论；停止评分")
        score, step, samples, delta = transition(score, step, samples, outcome)
        if any((number(event.get("评分后")) != score,
                number(event.get("步长后")) != step,
                number(event.get("样本数后")) != samples,
                number(event.get("评分变化")) != delta)):
            raise RuntimeError("评分事件链不一致；停止评分")
    return {"模型键": key, "共享评分": score, "当前步长": step, "累计样本数": samples,
            "最近评估时间": changes[-1].get("评估时间") if changes else baseline["fields"].get("评估时间") if baseline else None,
            "事件数": len(changes), "最近结论": changes[-1].get("主脑结论") if changes else None,
            "最近三次结论": [x.get("主脑结论") for x in changes[-3:]]}


def check_legacy_drift(all_events):
    """Report later legacy writes without invalidating the Feishu event chain."""
    if not LEGACY.is_file():
        return None
    hashes = {r["fields"].get("迁移源哈希") for r in all_events
              if r["fields"].get("事件类型") == "baseline" and r["fields"].get("迁移源哈希")}
    if len(hashes) > 1:
        return "旧台账迁移基线哈希不一致；请核对旧数据，当前评分仍以飞书事件链为准"
    try:
        changed = bool(hashes) and hashlib.sha256(LEGACY.read_bytes()).hexdigest() not in hashes
    except OSError:
        return "旧台账暂不可读；当前评分仍以飞书事件链为准"
    if changed:
        return "旧 Markdown 台账有迁移后的写入，尚未计入飞书共享评分；请另行核对同步"
    return None


def level(score, samples, recent=()):
    if score is None:
        return "未定级"
    recommended = score >= 80 and samples >= 3 and len(recent) >= 3 and not {"重大问题", "严重问题"}.intersection(recent)
    name = "推荐" if recommended else "可用但需逐阶段审查" if score >= 60 else "观察" if score >= 40 else "受限或暂不推荐"
    return name + ("，试用中" if samples < 3 else "")


def summary_upsert(bearer, agent, model, current, issue):
    existing = [r for r in records(SUMMARY, bearer) if r["fields"].get("模型键") == current["模型键"]]
    if len(existing) > 1:
        raise RuntimeError("模型总账出现重复行；停止更新")
    payload = {"模型键": current["模型键"], "桌面应用": agent, "模型/版本": model,
               "共享评分": current["共享评分"], "累计样本数": current["累计样本数"],
               "当前步长": current["当前步长"], "能力等级": level(current["共享评分"], current["累计样本数"], current["最近三次结论"]),
               "最近评估时间": current["最近评估时间"] or ""}
    if issue:
        payload["已知限制"] = issue
    if existing:
        request("PUT", f"{BASE}/{SUMMARY}/records/{existing[0]['record_id']}", bearer, {"fields": payload})
    else:
        request("POST", f"{BASE}/{SUMMARY}/records", bearer, {"fields": payload})
    check = [r["fields"] for r in records(SUMMARY, bearer) if r["fields"].get("模型键") == current["模型键"]]
    if len(check) != 1 or any(number(check[0].get(k)) != current[k] for k in ("共享评分", "当前步长", "累计样本数")):
        raise RuntimeError("模型总账回读未通过")


@contextmanager
def local_lock():
    root = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "brain-worker"
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / "capability-ledger.lock"
    lock_path.touch(exist_ok=True)
    with lock_path.open("r+b") as file:
        if os.name == "nt":
            import msvcrt
            deadline = time.monotonic() + 30
            while True:
                try:
                    file.seek(0)
                    msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("其他对话正在更新台账，请稍后重读")
                    time.sleep(0.1)
            try:
                yield
            finally:
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(file, fcntl.LOCK_UN)


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status")
    status.add_argument("--agent", required=True)
    status.add_argument("--model", required=True)
    record = sub.add_parser("record")
    record.add_argument("--event-id", required=True, help="Stable report/phase identifier; retry with the same ID")
    record.add_argument("--agent", required=True)
    record.add_argument("--model", required=True)
    record.add_argument("--task", required=True)
    record.add_argument("--stage", required=True)
    record.add_argument("--evaluated-at", required=True, help="Actual evaluation timestamp including timezone")
    record.add_argument("--outcome", choices=OUTCOMES, required=True)
    record.add_argument("--evidence", required=True)
    record.add_argument("--issue", default="")
    record.add_argument("--identity-confirmed", action="store_true")
    args = parser.parse_args(argv)
    agent, model, key = canonical(args.agent, args.model)
    if args.command == "record" and args.outcome != "证据不足" and not args.identity_confirmed:
        raise RuntimeError("未确认实际执行模型，不能计分")
    if args.command == "record":
        try:
            datetime.fromisoformat(args.evaluated_at)
        except ValueError as exc:
            raise RuntimeError("评估时间须使用带时区的 ISO 时间") from exc
        if datetime.fromisoformat(args.evaluated_at).tzinfo is None:
            raise RuntimeError("评估时间缺少时区")
    with local_lock():
        bearer = token()
        all_events = records(EVENTS, bearer)
        legacy_note = check_legacy_drift(all_events)
        before = state(all_events, key)
        if args.command == "status":
            print(json.dumps({**before, "能力等级": level(before["共享评分"], before["累计样本数"], before["最近三次结论"]), "读取时间": datetime.now(TZ).isoformat(timespec="seconds"), "旧台账提示": legacy_note}, ensure_ascii=False))
            return
        event_id = args.event_id.strip()
        matches = [r for r in all_events if r["fields"].get("事件ID") == event_id]
        if len(matches) > 1:
            raise RuntimeError("事件 ID 重复；停止更新")
        if matches:
            old = matches[0]["fields"]
            expected = (key, args.task, args.stage, args.outcome, args.evaluated_at)
            actual = tuple(old.get(k) for k in ("模型键", "任务类型", "阶段编号", "主脑结论", "评估时间"))
            if actual != expected:
                raise RuntimeError("事件 ID 已用于不同内容；停止更新")
            summary_upsert(bearer, agent, model, before, args.issue)
            print(json.dumps({"idempotent": True, **before, "旧台账提示": legacy_note}, ensure_ascii=False))
            return
        if not any(r["fields"].get("事件类型") == "baseline" for r in model_events(all_events, key)[0]):
            baseline = {"事件ID": "baseline:" + key, "模型键": key, "桌面应用": agent, "模型/版本": model,
                        "任务类型": "新模型基线", "阶段编号": "initial", "评估时间": args.evaluated_at,
                        "记录时间": datetime.now(TZ).isoformat(timespec="microseconds"), "主脑结论": "新模型初始状态",
                        "事件类型": "baseline", "评分后": 50, "步长后": 1, "样本数后": 0}
            request("POST", f"{BASE}/{EVENTS}/records", bearer, {"fields": baseline})
            all_events = records(EVENTS, bearer)
            before = state(all_events, key)
        after_score, after_step, after_samples, delta = transition(before["共享评分"], before["当前步长"], before["累计样本数"], args.outcome)
        event = {"事件ID": event_id, "模型键": key, "桌面应用": agent, "模型/版本": model,
                 "任务类型": args.task, "阶段编号": args.stage, "评估时间": args.evaluated_at,
                 "记录时间": datetime.now(TZ).isoformat(timespec="microseconds"), "主脑结论": args.outcome,
                 "证据": args.evidence, "事件类型": "assessment", "评分变化": delta,
                 "评分后": after_score, "步长后": after_step, "样本数后": after_samples}
        if args.issue:
            event["问题与限制"] = args.issue
        request("POST", f"{BASE}/{EVENTS}/records", bearer, {"fields": {k: v for k, v in event.items() if v is not None}})
        reread = records(EVENTS, bearer)
        matching = [r for r in reread if r["fields"].get("事件ID") == event_id]
        if len(matching) != 1:
            raise RuntimeError("事件写入后回读不唯一；停止更新")
        after = state(reread, key)
        summary_upsert(bearer, agent, model, after, args.issue)
        print(json.dumps({"idempotent": False, **after, "旧台账提示": legacy_note}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
