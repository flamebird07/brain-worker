"""Shared, append-only Feishu capability ledger for brain-worker.

Use `status` immediately before a prompt and `record` after independent review.
The local lock serializes updates from separate conversations on this host.

飞书资源与凭证全部由用户显式配置，不含任何内置应用/表标识或个人路径：
  - 凭证（应用身份）：app_id / app_secret，用于换取 tenant_access_token。
  - 多维表格标识：app_token（bitable app）；表标识：summary_table / events_table。
  - 环境变量优先，其次 ~/.config/brain-worker/platforms.yaml 的 platforms.feishu.extra。
  - 缺少任一必需配置时，在发起任何网络请求之前明确拒绝。
  - 旧台账漂移检查默认关闭，仅当显式配置 legacy_ledger 路径时启用。
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

try:
    import ledger_core as core
except ImportError:  # 以 python -m scripts.capability_ledger 运行时
    from scripts import ledger_core as core

API = "https://open.feishu.cn/open-apis"
TZ = timezone(timedelta(hours=8))
OUTCOMES = core.OUTCOMES

# 配置字段与环境变量映射；优先级：环境变量 > YAML（每个字段独立回退）。
ENV_KEYS = {
    "app_id": "FEISHU_APP_ID",
    "app_secret": "FEISHU_APP_SECRET",
    "app_token": "FEISHU_APP_TOKEN",
    "summary_table": "FEISHU_SUMMARY_TABLE",
    "events_table": "FEISHU_EVENTS_TABLE",
    "legacy_ledger": "FEISHU_LEGACY_LEDGER",
}
REQUIRED_FIELDS = ("app_id", "app_secret", "app_token", "summary_table", "events_table")
# YAML 配置文件位置；用 expanduser 解析，Windows/Linux 均按用户目录。
CONFIG_FILE = "~/.config/brain-worker/platforms.yaml"


def _mapping(value, what, path):
    """YAML 节点类型校验：None 视为该层缺失（返回空映射），非映射类型给明确错误。"""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RuntimeError(
            "飞书平台配置的 %s 应为映射（键值结构），当前是其他类型：%s。请按 SKILL.md“飞书后端接入（可选）”修改。"
            % (what, path)
        )
    return value


def _yaml_extra():
    """读取 platforms.yaml 的 platforms.feishu.extra。

    仅在需要 YAML 回退时调用（见 load_config）；PyYAML 在此处按需加载，
    纯环境变量方式不依赖 PyYAML。逐层校验节点类型，配置内容不回显。
    """
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError(
            "读取飞书 YAML 配置需要 PyYAML；请先 `pip install pyyaml`，"
            "或改用环境变量（FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_APP_TOKEN / "
            "FEISHU_SUMMARY_TABLE / FEISHU_EVENTS_TABLE）配置。"
        ) from exc
    raw_path = os.environ.get("BRAIN_WORKER_PLATFORMS_CONFIG", CONFIG_FILE)
    path = Path(os.path.expanduser(raw_path))
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if "BRAIN_WORKER_PLATFORMS_CONFIG" not in os.environ:
            # 默认配置路径本就不存在：视为没有 YAML，交由 load_config 报缺少必需字段。
            return {}
        raise RuntimeError("无法读取飞书平台配置文件：%s" % path)
    except OSError as exc:
        raise RuntimeError("无法读取飞书平台配置文件：%s" % path) from exc
    try:
        data = yaml.safe_load(text)
    except Exception as exc:
        raise RuntimeError("飞书平台配置文件不是合法 YAML：%s" % path) from exc
    data = _mapping(data, "根节点", path)
    platforms = _mapping(data.get("platforms"), "platforms", path)
    feishu = _mapping(platforms.get("feishu"), "platforms.feishu", path)
    extra = feishu.get("extra")
    if extra is None:
        raise RuntimeError("飞书平台配置缺少 platforms.feishu.extra；请按 SKILL.md“飞书后端接入（可选）”填写。")
    if not isinstance(extra, dict):
        raise RuntimeError(
            "飞书平台配置的 platforms.feishu.extra 应为映射（键值结构），当前是其他类型：%s。"
            "请按 SKILL.md“飞书后端接入（可选）”修改。" % path
        )
    return extra


def load_config():
    """解析飞书配置，返回 dict；每个字段环境变量优先，其次 YAML。

    读取 YAML 的条件（满足其一）：
      1. 任一必需字段未由环境变量提供（读默认 CONFIG_FILE 或显式指定文件）；
      2. 显式设置了 BRAIN_WORKER_PLATFORMS_CONFIG（此时即使必需字段已由环境变量
         提供，可选字段如 legacy_ledger 也从该文件回退；文件不可读/结构错误
         会明确报错，不静默忽略）。
    五个必需环境变量齐全且未显式指定 YAML 时，不读取任何 YAML、不依赖 PyYAML。
    缺少任一必需字段时立即抛错——在发起任何网络请求之前。
    """
    env = {key: (os.environ.get(name) or "").strip() for key, name in ENV_KEYS.items()}
    explicit_yaml = "BRAIN_WORKER_PLATFORMS_CONFIG" in os.environ
    if explicit_yaml or any(not env[key] for key in REQUIRED_FIELDS):
        extra = _yaml_extra()
    else:
        extra = {}
    merged = {}
    for key in list(REQUIRED_FIELDS) + ["legacy_ledger"]:
        merged[key] = env[key] or str(extra.get(key) or "").strip()
    missing = [key for key in REQUIRED_FIELDS if not merged[key]]
    if missing:
        raise RuntimeError(
            "缺少必需飞书配置：%s。请通过环境变量（%s）或 %s 配置。"
            % ("、".join(missing),
               "、".join(ENV_KEYS[k] for k in missing),
               CONFIG_FILE)
        )
    merged["legacy_ledger"] = merged["legacy_ledger"] or None
    return merged


_CONFIG = None


def cfg():
    """缓存的配置；首次访问时解析。供 token/records/summary_upsert 等使用。"""
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config()
    return _CONFIG


def base():
    """多维表格 API 前缀（按当前配置的 app_token 动态构造）。"""
    return "bitable/v1/apps/%s/tables" % cfg()["app_token"]


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
    c = cfg()
    return request("POST", "auth/v3/tenant_access_token/internal",
                   body={"app_id": c["app_id"], "app_secret": c["app_secret"]})["tenant_access_token"]


def records(table, bearer):
    result, cursor = [], None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["page_token"] = cursor
        data = request("GET", f"{base()}/{table}/records", bearer, params=params)
        result.extend(data.get("items") or [])
        if not data.get("has_more"):
            return result
        cursor = data.get("page_token")
        if not cursor:
            raise RuntimeError("飞书分页游标缺失")


def canonical(agent, model, allow_pending=False):
    """共享核心的严格身份归一；错误转为本地 RuntimeError。"""
    try:
        return core.canonical_triple(agent, model, allow_pending=allow_pending)
    except core.LedgerCoreError as exc:
        raise RuntimeError(str(exc)) from exc


def number(value):
    return None if value in (None, "") else int(value)


def transition(score, step, samples, outcome):
    """共享核心的评分/步长/样本转换；错误转为本地 RuntimeError。"""
    try:
        return core.transition(score, step, samples, outcome)
    except core.LedgerCoreError as exc:
        raise RuntimeError(str(exc)) from exc


def _recomputed_key(fields):
    """按事件自身的 桌面应用/模型版本 以核心规则重算规范键（历史兼容匹配）。

    与 local 的 event_key 使用同一规则：单一历史存储键可兼容匹配，
    多个存储键映射到同一规范身份时判碰撞（见 guard_key_collision）。
    """
    return core.lenient_key(fields.get("桌面应用"), fields.get("模型/版本"))


def stored_keys(all_events, key):
    """同一规范身份下已存在的存储键（模型键）集合（按事件重算匹配）。"""
    return {r["fields"].get("模型键") for r in all_events if _recomputed_key(r["fields"]) == key}


def guard_key_collision(all_events, key):
    """历史碰撞守卫（与 local 一致）：多存储键映射同一规范身份时拒绝。

    在任何外部写入之前调用；禁止静默汇总、迁移、重算或改写历史。
    """
    stored = stored_keys(all_events, key)
    if len(stored) > 1:
        raise RuntimeError(core.collision_message(key, stored))


def plan_store_key(all_events, key):
    """写前规划：新事件/总账应使用的存储键；多个历史存储键时零写入拒绝。

    单一历史别名对象沿用其既有存储键（规范键只用于匹配），不制造第二个存储
    身份；既有真实碰撞直接拒绝。必须在任何 request 写入之前调用。
    """
    try:
        return core.plan_stored_key(key, stored_keys(all_events, key))
    except core.LedgerCoreError as exc:
        raise RuntimeError(str(exc)) from exc


def joint_identity_precheck(summary_rows, canonical_key, store_key, event_chain_present):
    """飞书联合身份预检（BW-DUAL-05C）：首次外部写入前联合核对事件侧与总账。

    summary_rows：总账表当前全部行；canonical_key：按本次事件 agent/model
    重算的规范键；store_key：事件侧写前规划的存储键；event_chain_present：
    事件表中是否存在该规范身份的事件行。总账行按同一精确身份规则识别——
    模型键占用计划存储键，或其身份字段（桌面应用/模型版本）重算后等于规范
    键；匹配时行身份字段与计划存储键必须一致。任何冲突都以 RuntimeError 在
    写入前拒绝（事件表与总账均零写入；不自动迁移、改键、合并或删除已有
    总账）：
      - 总账行占用计划存储键但身份字段指向其他身份：矛盾，拒绝；
      - 同一规范身份存在多个总账行：重复，拒绝；
      - 唯一匹配行的模型键与计划存储键不一致（两表键不一致）：拒绝；
      - 总账已有该身份评分但事件表没有对应事件链：证据缺口，拒绝
        （不新建基线覆盖已有评分）。
    """
    matched = []
    for row in summary_rows:
        fields = row.get("fields") or {}
        row_key = fields.get("模型键")
        row_identity = core.lenient_key(fields.get("桌面应用"), fields.get("模型/版本"))
        if row_key == store_key and row_identity != canonical_key:
            raise RuntimeError(
                "总账身份矛盾：某总账行的模型键为 “%s”，但其桌面应用/模型版本指向 "
                "“%s”，与本次身份 “%s” 矛盾；拒绝写入，请人工核对总账"
                % (store_key, row_identity, canonical_key))
        if row_identity == canonical_key or row_key == store_key:
            matched.append(fields)
    if len(matched) > 1:
        raise RuntimeError(
            "模型总账重复：同一身份 “%s” 在总账存在多行（%s）；拒绝写入，"
            "请人工核对总账，不自动合并或删除"
            % (canonical_key, "、".join(sorted({f.get("模型键") or "" for f in matched}))))
    if matched and matched[0].get("模型键") != store_key:
        raise RuntimeError(
            "两表键不一致：事件侧存储键为 “%s”，总账已有键为 “%s”（同一身份 “%s”）；"
            "拒绝写入，不自动迁移/改键/合并，请人工核对"
            % (store_key, matched[0].get("模型键"), canonical_key))
    if matched and not event_chain_present:
        raise RuntimeError(
            "证据缺口：总账已有模型 “%s” 的评分，但事件表没有对应事件链；拒绝写入，"
            "不新建基线覆盖已有评分，请人工核对" % canonical_key)
    return matched[0] if matched else None


def model_events(all_events, key):
    guard_key_collision(all_events, key)
    found = [r for r in all_events if _recomputed_key(r["fields"]) == key]
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
    """Report later legacy writes without invalidating the Feishu event chain.

    默认关闭：仅在配置提供 legacy_ledger 路径时读取该文件；路径来自显式配置，
    不读取任何个人知识库默认位置。显式启用时保持原有哈希比对语义。
    """
    legacy = cfg().get("legacy_ledger")
    if not legacy:
        return None
    path = Path(legacy)
    if not path.is_file():
        return None
    hashes = {r["fields"].get("迁移源哈希") for r in all_events
              if r["fields"].get("事件类型") == "baseline" and r["fields"].get("迁移源哈希")}
    if len(hashes) > 1:
        return "旧台账迁移基线哈希不一致；请核对旧数据，当前评分仍以飞书事件链为准"
    try:
        changed = bool(hashes) and hashlib.sha256(path.read_bytes()).hexdigest() not in hashes
    except OSError:
        return "旧台账暂不可读；当前评分仍以飞书事件链为准"
    if changed:
        return "旧 Markdown 台账有迁移后的写入，尚未计入飞书共享评分；请另行核对同步"
    return None


def level(score, samples, recent=()):
    return core.level(score, samples, recent)


def _feishu_norm(fields):
    """飞书事件字段（中文列名）→ 核心规范 dict（幂等指纹用）。

    model_key 统一取按事件自身 桌面应用/模型版本 重算的规范键——与新事件
    构造使用同一规则，历史存储键差异不影响原样重试幂等。issues 单值转
    列表由核心统一处理；recorded_at 等生成字段不进入指纹。
    """
    return {
        "event_id": fields.get("事件ID"),
        "type": fields.get("事件类型"),
        "model_key": core.lenient_key(fields.get("桌面应用"), fields.get("模型/版本")),
        "task": fields.get("任务类型"),
        "stage": fields.get("阶段编号"),
        "evaluated_at": fields.get("评估时间"),
        "outcome": fields.get("主脑结论"),
        "evidence": fields.get("证据"),
        "issues": fields.get("问题与限制") or "",
    }


def summary_upsert(bearer, agent, model, current, issue, store_key=None):
    table = cfg()["summary_table"]
    key = store_key if store_key else current["模型键"]
    all_rows = records(table, bearer)
    # 联合身份预检（BW-DUAL-05C）：PUT/POST 前最后一刻按规范身份复核总账，
    # 与 main 写前预检同一规则；此时事件链已存在或已经 main 预检校验。
    joint_identity_precheck(all_rows, core.lenient_key(agent, model), key, True)
    existing = [r for r in all_rows if r["fields"].get("模型键") == key]
    if len(existing) > 1:
        raise RuntimeError("模型总账出现重复行；停止更新")
    payload = {"模型键": key, "桌面应用": agent, "模型/版本": model,
               "共享评分": current["共享评分"], "累计样本数": current["累计样本数"],
               "当前步长": current["当前步长"], "能力等级": level(current["共享评分"], current["累计样本数"], current["最近三次结论"]),
               "最近评估时间": current["最近评估时间"] or ""}
    if issue:
        payload["已知限制"] = issue
    if existing:
        request("PUT", f"{base()}/{table}/records/{existing[0]['record_id']}", bearer, {"fields": payload})
    else:
        request("POST", f"{base()}/{table}/records", bearer, {"fields": payload})
    check = [r["fields"] for r in records(table, bearer) if r["fields"].get("模型键") == key]
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
    resolve = sub.add_parser("resolve", help="Attribute one pending stage to a subsequently confirmed model")
    resolve.add_argument("--pending-event-id", required=True)
    resolve.add_argument("--agent", required=True)
    resolve.add_argument("--model", required=True)
    resolve.add_argument("--identity-evidence", required=True)
    resolve.add_argument("--identity-confirmed", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "resolve" and not args.identity_confirmed:
        raise RuntimeError("补评必须确认实际执行模型")
    if args.command == "resolve" and not args.identity_evidence.strip():
        raise RuntimeError("补评缺少模型身份依据")
    pending = args.command == "record" and args.outcome != "证据不足" and not args.identity_confirmed
    agent, model, key = canonical(args.agent, args.model, allow_pending=pending)
    if args.command == "record":
        try:
            datetime.fromisoformat(args.evaluated_at)
        except ValueError as exc:
            raise RuntimeError("评估时间须使用带时区的 ISO 时间") from exc
        if datetime.fromisoformat(args.evaluated_at).tzinfo is None:
            raise RuntimeError("评估时间缺少时区")
    # 配置校验在获取锁与任何网络调用之前；缺少必需配置时零网络请求直接拒绝。
    cfg()
    events_table = cfg()["events_table"]
    with local_lock():
        bearer = token()
        all_events = records(events_table, bearer)
        legacy_note = check_legacy_drift(all_events)
        # 统一契约：历史碰撞在任何外部写入之前拒绝（status/record/resolve 全路径）。
        guard_key_collision(all_events, key)
        # 写前规划存储键（record/resolve）：单一历史别名对象沿用其既有键，
        # 既有碰撞零写入拒绝——不会写入后才发现身份冲突。
        store_key = key if args.command == "status" else plan_store_key(all_events, key)
        if args.command != "status":
            # 联合身份预检（BW-DUAL-05C）：record/resolve（含幂等重试）在第一
            # 次外部写入之前，按同一精确身份规则联合核对事件侧规划键与模型
            # 总账；两表键不一致、总账重复、身份矛盾或证据缺口均零写入拒绝，
            # 事件表与总账均无副作用。
            joint_identity_precheck(
                records(cfg()["summary_table"], bearer), key, store_key,
                any(_recomputed_key(r["fields"]) == key for r in all_events))
        if args.command == "status":
            before = state(all_events, key)
            print(json.dumps({**before, "能力等级": level(before["共享评分"], before["累计样本数"], before["最近三次结论"]), "读取时间": datetime.now(TZ).isoformat(timespec="seconds"), "旧台账提示": legacy_note}, ensure_ascii=False))
            return
        if args.command == "resolve":
            pending_id = args.pending_event_id.strip()
            source = [r["fields"] for r in all_events if r["fields"].get("事件ID") == pending_id]
            if len(source) != 1 or source[0].get("事件类型") != "identity_pending":
                raise RuntimeError("未找到唯一的待确认阶段；旧版证据不足事件不能自动补评")
            source = source[0]
            # 统一契约：桌面应用按核心归一规则比较（已知别名视为同一应用）
            if (core.canonical_agent(source.get("桌面应用")) != core.canonical_agent(agent)
                    or source.get("主脑结论") not in OUTCOMES
                    or source.get("主脑结论") == "证据不足"):
                raise RuntimeError("待确认阶段的应用或评价不匹配")
            resolved_id = core.resolve_id(pending_id)
            existing = [r["fields"] for r in all_events if r["fields"].get("事件ID") == resolved_id]
            if len(existing) > 1:
                raise RuntimeError("补评事件 ID 重复；停止更新")
            if existing:
                # 统一契约：resolve 幂等同样按指纹比较（evidence 含补证文本）。
                retry_norm = {
                    "event_id": resolved_id, "type": "assessment", "model_key": key,
                    "task": source["任务类型"], "stage": source["阶段编号"],
                    "evaluated_at": source["评估时间"], "outcome": source["主脑结论"],
                    "evidence": source.get("证据", "") + "；模型身份补证：" + args.identity_evidence,
                    "issues": source.get("问题与限制", ""),
                }
                if core.idempotency_fingerprint(_feishu_norm(existing[0])) != core.idempotency_fingerprint(retry_norm):
                    raise RuntimeError("该阶段已归属其他模型或结论，或补证内容不一致；停止重复计分")
                current = state(all_events, key)
                summary_upsert(bearer, agent, model, current, source.get("问题与限制", ""), store_key)
                print(json.dumps({"idempotent": True, "resolvedFrom": pending_id, **current, "旧台账提示": legacy_note}, ensure_ascii=False))
                return
            event_id = resolved_id
            task, stage, outcome = source["任务类型"], source["阶段编号"], source["主脑结论"]
            evaluated_at = source["评估时间"]
            evidence = source.get("证据", "") + "；模型身份补证：" + args.identity_evidence
            issue = source.get("问题与限制", "")
        else:
            event_id = args.event_id.strip()
            task, stage, outcome = args.task, args.stage, args.outcome
            evaluated_at, evidence, issue = args.evaluated_at, args.evidence, args.issue
        matches = [r for r in all_events if r["fields"].get("事件ID") == event_id]
        if len(matches) > 1:
            raise RuntimeError("事件 ID 重复；停止更新")
        if matches:
            old = matches[0]["fields"]
            expected_type = "identity_pending" if pending else "assessment"
            # 统一契约：幂等指纹含 evidence/issues（issues 顺序不敏感）；
            # 证据或问题内容改变不得静默冒充同内容重试。
            new_norm = {
                "event_id": event_id, "type": expected_type, "model_key": key,
                "task": task, "stage": stage, "evaluated_at": evaluated_at,
                "outcome": outcome, "evidence": evidence, "issues": issue,
            }
            if (old.get("事件类型") != expected_type
                    or core.idempotency_fingerprint(_feishu_norm(old))
                    != core.idempotency_fingerprint(new_norm)):
                raise RuntimeError("事件 ID 已用于不同内容；停止更新")
            if pending:
                print(json.dumps({"idempotent": True, "pendingIdentity": True, "eventId": event_id, "拟评分变化": OUTCOMES[outcome][0]}, ensure_ascii=False))
            else:
                before = state(all_events, key)
                summary_upsert(bearer, agent, model, before, issue, store_key)
                print(json.dumps({"idempotent": True, **before, "旧台账提示": legacy_note}, ensure_ascii=False))
            return
        if pending:
            event = {"事件ID": event_id, "模型键": store_key, "桌面应用": agent, "模型/版本": model,
                     "任务类型": task, "阶段编号": stage, "评估时间": evaluated_at,
                     "记录时间": datetime.now(TZ).isoformat(timespec="microseconds"), "主脑结论": outcome,
                     "证据": evidence, "事件类型": "identity_pending", "评分变化": OUTCOMES[outcome][0]}
            if issue:
                event["问题与限制"] = issue
            request("POST", f"{base()}/{events_table}/records", bearer, {"fields": event})
            reread = [r["fields"] for r in records(events_table, bearer) if r["fields"].get("事件ID") == event_id]
            if len(reread) != 1 or reread[0].get("事件类型") != "identity_pending":
                raise RuntimeError("待确认阶段写入后回读失败")
            print(json.dumps({"idempotent": False, "pendingIdentity": True, "eventId": event_id, "拟评分变化": OUTCOMES[outcome][0]}, ensure_ascii=False))
            return
        before = state(all_events, key)
        if not any(r["fields"].get("事件类型") == "baseline" for r in model_events(all_events, key)[0]):
            baseline = {"事件ID": "baseline:" + key, "模型键": store_key, "桌面应用": agent, "模型/版本": model,
                        "任务类型": "新模型基线", "阶段编号": "initial", "评估时间": evaluated_at,
                        "记录时间": datetime.now(TZ).isoformat(timespec="microseconds"), "主脑结论": "新模型初始状态",
                        "事件类型": "baseline", "评分后": 50, "步长后": 1, "样本数后": 0}
            request("POST", f"{base()}/{events_table}/records", bearer, {"fields": baseline})
            all_events = records(events_table, bearer)
            before = state(all_events, key)
        after_score, after_step, after_samples, delta = transition(before["共享评分"], before["当前步长"], before["累计样本数"], outcome)
        event = {"事件ID": event_id, "模型键": store_key, "桌面应用": agent, "模型/版本": model,
                 "任务类型": task, "阶段编号": stage, "评估时间": evaluated_at,
                 "记录时间": datetime.now(TZ).isoformat(timespec="microseconds"), "主脑结论": outcome,
                 "证据": evidence, "事件类型": "assessment", "评分变化": delta,
                 "评分后": after_score, "步长后": after_step, "样本数后": after_samples}
        if issue:
            event["问题与限制"] = issue
        request("POST", f"{base()}/{events_table}/records", bearer, {"fields": {k: v for k, v in event.items() if v is not None}})
        reread = records(events_table, bearer)
        matching = [r for r in reread if r["fields"].get("事件ID") == event_id]
        if len(matching) != 1:
            raise RuntimeError("事件写入后回读不唯一；停止更新")
        after = state(reread, key)
        summary_upsert(bearer, agent, model, after, issue, store_key)
        print(json.dumps({"idempotent": False, "resolvedFrom": pending_id if args.command == "resolve" else None, **after, "旧台账提示": legacy_note}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
