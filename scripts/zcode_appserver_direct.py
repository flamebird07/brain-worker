"""ZCode 官方 app-server 传输直连派工（BW-MAC-COMPAT-20261114-S2，macOS 优先）。

macOS 桌面端自带的官方 CLI（~/.local/share/zcode-cli/zcode.cjs）以
`app-server` 子命令提供 ZCode Protocol stdio 服务器（实测确认，协议证据见
references/zcode-appserver-direct.md）。本入口把它作为**独立正式传输**驱动：
同一进程外的 stdio JSON-RPC（**无 `jsonrpc` 键**的行分隔 JSON），完成
`runtime/capabilities → session/create → session/setMode → session/setModel →
session/send → (turnEnded) → session/messages → session/close` 的完整生命周期。

复用既有门，不另起炉灶（与 zcode_direct 同一口径）：
- `dispatch_pool.consume_for_entry`：Popen 前原子消费容量 claim（同一 `zcode:GLM-5.3`
  池，绝不新增池/容量数字）；
- `quota_control.zcode_availability_gate`：Popen 前权威最后原子闸；
- `quota_control.gate_dispatch`：额度门（ZCode 手动额度政策照旧：无自动冷却，
  并发占位保留）；
- `prompt_contract`：九节报告契约逐字前置；
- `qoder_direct.analyze_report/finalize_binding`：报告正文绑定；
- 真实调用者身份（Z2）：Popen 前以本进程 PID + 创建时刻（macOS 为 libproc
  身份探针）锚定 owner，bind_child/finish 全程同一身份。

权限语义 fail-closed：官方会话经 `interaction/requestPermission` 向客户端请求授权；
本入口默认**一律选 deny 选项**并逐请求落证据；只有显式 `--allow-tools` 名单内的
toolName 才放行（kind=allowOnce，逐请求记录）。`interaction/requestUserInput`
一律拒绝（headless 无交互）。`--tools` 不受支持（那是 SDK 载体的整工具目录开关），
非空即拒，绝不冒称已生效。

超时 ≠ 429：wall-clock 超时按真实失败结算，绝不写 healthy、绝不伪装限额。
Windows 宿主明确拒绝（Windows 走既有 SDK bootstrap 传输）。

边界（与 references/zcode-appserver-direct.md 一致）：
- 配置缺失、命令不可执行、阶段为空、mode/reasoning 非法 → 派工前拒绝，不建证据目录；
- setModel 被官方拒绝 → 会话已建但从未提交 → start_failed 安全结算；
- `--preflight-only`（BW-WEAK-RESTRUCTURE-20261116-R2 收紧）：只做 `runtime/capabilities`
  只读预检——**零共享状态写入**（不调 availability/容量/额度门，不创建/迁移/WAL 共享
  数据库），缺库不建库；预检成功绝不代替正式派发或业务验收；
- 共享状态数据库无法打开/写入（环境权限、路径问题）→ 结构化拒绝（sent=false、
  shared_state_error=true、失败阶段/目标路径/原始异常），退出码 2——**绝不误分类为
  认证、额度、时段或模型错误**，绝不输出裸 traceback；
- 协议终态、正文绑定、业务验收三者分离：退出码只反映协议终态，绑定看
  report-state.json，`business_verified` 恒为 false，由主脑独立判定。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / 'zcode-appserver-entry.json'
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from qoder_direct import analyze_report, finalize_binding  # noqa: E402
import prompt_contract as pc  # noqa: E402 三入口共享九节契约
import quota_control as qc  # noqa: E402 持久额度冷却与路由门禁
import dispatch_pool as dp  # noqa: E402 跨会话并发容量池
import zcode_direct as zd  # noqa: E402 复用校验/常量/默认值，不复制规则

DEFAULT_PROVIDER = zd.DEFAULT_PROVIDER
DEFAULT_MODEL = zd.DEFAULT_MODEL
REASONING_LEVELS = zd.REASONING_LEVELS
MODES = zd.MODES  # ('plan', 'edit')；官方还有 yolo/guarded/auto，本入口绝不提供
# 服务器→客户端请求的应答策略（协议形状经 2026-11-14 有界探针实测确认）。
RUNTIME_PREFS_RESULT = {
    'nativeSearchEnhancementsEnabled': False,
    'toolSearchMode': 'off',
    'memoryEnabled': False,
    'askUserQuestionAutoResolutionEnabled': True,
    'modelContextBudgetStrategy': 'preflight-v1',
}
PERMISSION_DENY_KIND = 'deny'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_appserver_config(path=None) -> dict:
    """本机入口配置：只存官方 CLI 命令与可选环境，不存凭据值。

    必填 `command`：非空字符串数组（argv 逐元素，不经 shell）；第 0 元素必须是
    存在的绝对路径可执行文件；其余元素为非空字符串（如 zcode.cjs 绝对路径、
    'app-server' 子命令）。可选 `environment`（str→str 透传）、`version`（str）。
    """
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    if not cfg_path.is_absolute():
        raise ValueError(f'entry config path must be absolute: {cfg_path}')
    if not cfg_path.is_file():
        raise FileNotFoundError(f'entry config file not found: {cfg_path}')
    cfg_path = cfg_path.resolve()
    cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
    if not isinstance(cfg, dict):
        raise ValueError(f'entry config must be a JSON object: {cfg_path}')
    command = cfg.get('command')
    if (not isinstance(command, list) or not command
            or any(not isinstance(a, str) or not a for a in command)):
        raise ValueError('entry config command must be a non-empty list of '
                         'non-empty strings')
    exe = Path(command[0])
    if not exe.is_absolute() or not exe.is_file():
        raise FileNotFoundError(f'entry config command[0] must be an existing '
                                f'absolute file: {command[0]}')
    environment = cfg.get('environment', {})
    if not isinstance(environment, dict) or any(
            not isinstance(k, str) or not isinstance(v, str)
            for k, v in environment.items()):
        raise ValueError('entry config environment must be a string->string object')
    if 'version' in cfg and not isinstance(cfg['version'], str):
        raise ValueError('entry config version must be a string when present')
    cfg['_entry_config_path'] = str(cfg_path)
    return cfg


def _find_first(obj, key):
    """在嵌套 JSON 里找第一个字符串取值的 key（sessionId/turnId 形状多态的宽容读取）。
    字面量 'unknown' 不算命中（实测 create 结果 projection.sessionId 就是 'unknown'，
    真实 id 在 session/lifecycle/changed 通知里）。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and isinstance(v, str) and v and v != 'unknown':
                return v
        for v in obj.values():
            hit = _find_first(v, key)
            if hit is not None:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = _find_first(v, key)
            if hit is not None:
                return hit
    return None


def _is_turn_ended(obj) -> bool:
    """识别 turn 结束信号：通知或事件行的 method == 'turnEnded'（顶部或 params 内）。"""
    if not isinstance(obj, dict):
        return False
    if obj.get('method') == 'turnEnded':
        return True
    params = obj.get('params')
    return isinstance(params, dict) and params.get('method') == 'turnEnded'


def _assistant_texts(obj):
    """从 session/messages 结果里按 role=='assistant' 依序收集文本块（形状宽容），
    找不到任何助手文本即返回 []（调用方如实按 carrier_missing 处理，绝不臆造）。"""
    texts = []

    def walk(node):
        if isinstance(node, dict):
            role = node.get('role')
            content = node.get('content')
            if role == 'assistant':
                if isinstance(content, str) and content:
                    texts.append(content)
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict):
                            t = part.get('text')
                            if isinstance(t, str) and t:
                                texts.append(t)
                        elif isinstance(part, str) and part:
                            texts.append(part)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(obj)
    return texts


class AppServerClient:
    """stdio JSON-RPC 客户端（无 `jsonrpc` 键的行分隔 JSON；一行一消息）。

    只做三件事：逐行收发、把全部报文写入 events.jsonl 原件、对服务器→客户端请求
    按 fail-closed 策略应答。不解析业务语义（那在调用方）。

    读取用后台线程 + 队列：绝不对缓冲管道做 select（readline 预读会把后续行留在
    缓冲里，select 从此永远看不到数据——离线回归已固化该死锁负例）。
    """

    def __init__(self, proc, events_fh, allow_tools=(), log_sink=None):
        self.proc = proc
        self.events_fh = events_fh
        self.allow_tools = frozenset(allow_tools or ())
        self.next_id = 1
        self.permission_denied = 0
        self.permission_auto_allowed = 0
        self.permission_events = []
        self.unsupported_server_requests = 0
        # 真实会话身份/状态观测（session/lifecycle/changed 通知是权威来源；create
        # 结果里的 projection.sessionId 实测是字面量 'unknown'）。
        self.lifecycle_session_id = None
        self.lifecycle_state = None
        self._log_sink = log_sink
        self._lines = queue.Queue()
        self._reader_dead = False
        self._reader = threading.Thread(target=self._pump_lines, daemon=True)
        self._reader.start()

    def _pump_lines(self):
        try:
            for raw in iter(self.proc.stdout.readline, b''):
                self._lines.put(raw)
        except (OSError, ValueError):
            pass
        finally:
            self._reader_dead = True
            self._lines.put(None)

    def _log(self, direction, obj):
        row = {'dir': direction, 'at': _now()}
        try:
            row['msg'] = obj
            self.events_fh.write(json.dumps(row, ensure_ascii=False) + '\n')
        except (TypeError, ValueError):
            row['msg'] = {'_raw': str(obj)[:500]}
            self.events_fh.write(json.dumps(row, ensure_ascii=False) + '\n')
        self.events_fh.flush()

    def send_request(self, method, params):
        rid = self.next_id
        self.next_id += 1
        msg = {'id': rid, 'method': method}
        if params is not None:
            msg['params'] = params
        self._log('send', msg)
        self.proc.stdin.write((json.dumps(msg, ensure_ascii=False) + '\n')
                              .encode('utf-8'))
        self.proc.stdin.flush()
        return rid

    def _reply(self, rid, payload):
        msg = {'id': rid}
        msg.update(payload)
        self._log('send', msg)
        self.proc.stdin.write((json.dumps(msg, ensure_ascii=False) + '\n')
                              .encode('utf-8'))
        self.proc.stdin.flush()

    def read_until_response(self, rid, deadline):
        """读行直到 rid 对应的 result/error；期间处理服务器→客户端请求与通知。
        返回值：(kind, obj, extra)，kind ∈ {'result','error','notification',
        'timeout','eof'}。泵模式（rid=None）把通知/事件行原样交还调用方观测
        turnEnded；rpc 等待模式把通知留证后继续等目标响应。"""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return 'timeout', None, None
            try:
                raw = self._lines.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                if self._reader_dead and self._lines.empty():
                    return 'eof', None, None
                continue
            if raw is None:
                return 'eof', None, None
            try:
                obj = json.loads(raw)
            except ValueError:
                self._log('recv_raw', {'_raw': raw[:1000]})
                continue
            self._log('recv', obj)
            if self._log_sink is not None:
                self._log_sink(obj)
            if isinstance(obj, dict) and obj.get('method') == \
                    'session/lifecycle/changed':
                params = obj.get('params') or {}
                sid = params.get('sessionId')
                if isinstance(sid, str) and sid and sid != 'unknown':
                    self.lifecycle_session_id = sid
                if isinstance(params.get('state'), str):
                    self.lifecycle_state = params['state']
            if isinstance(obj, dict) and obj.get('id') == rid \
                    and ('result' in obj or 'error' in obj):
                kind = 'result' if 'result' in obj else 'error'
                return kind, obj, None
            if isinstance(obj, dict) and obj.get('method') \
                    and obj.get('id') is not None:
                self._answer_server_request(obj)
                continue
            # 通知/事件行已落 events.jsonl 原件；泵模式（rid=None）把整行交还调用方
            # 观测 turnEnded，rpc 等待模式则继续等目标响应（通知已留证）。
            if rid is None:
                return 'notification', obj, None
            continue

    def _answer_server_request(self, obj):
        """fail-closed 应答服务器→客户端请求；每个应答都落证据。绝不放行交互。"""
        method = obj.get('method')
        rid = obj.get('id')
        if method == 'session/requestRuntimePreferences':
            self._reply(rid, {'result': dict(RUNTIME_PREFS_RESULT)})
            return
        if method == 'interaction/requestPermission':
            params = obj.get('params') or {}
            tool_name = params.get('toolName')
            options = params.get('options') or []
            deny = next((o for o in options
                         if isinstance(o, dict)
                         and o.get('kind') == PERMISSION_DENY_KIND), None)
            allow_once = None
            if tool_name in self.allow_tools:
                allow_once = next((o for o in options
                                   if isinstance(o, dict)
                                   and o.get('kind') == 'allowOnce'), None)
            if allow_once:
                option = allow_once
                kind = 'allowOnce'
                self.permission_auto_allowed += 1
            elif deny:
                option = deny
                kind = PERMISSION_DENY_KIND
                self.permission_denied += 1
            else:
                # 形状不符/没有 deny 选项：拒绝应答（error result），绝不猜选项。
                self.unsupported_server_requests += 1
                self.permission_events.append({'request': obj, 'reply': 'error',
                                               'reason': 'no deny option found'})
                self._reply(rid, {'error': {'code': -32000,
                                            'message': 'headless dispatch: '
                                            'permission request refused '
                                            '(no deny option in request)'}})
                return
            reply = {'result': {'optionId': option.get('optionId'), 'kind': kind}}
            self.permission_events.append({'request': obj, 'reply': reply})
            self._reply(rid, reply)
            return
        # 其余（interaction/requestUserInput、未知方法）一律 error 应答：headless
        # 无交互、不支持的请求绝不假装成功。
        self.unsupported_server_requests += 1
        self._reply(rid, {'error': {'code': -32601,
                                    'message': f'headless dispatch client does '
                                    f'not support server request: {method}'}})


def main(argv=None):
    if sys.platform.startswith('win'):
        print(json.dumps({'refused_before_dispatch': True, 'sent': False,
                          'exit_code': 2,
                          'reasons': ['windows host must use the SDK bootstrap '
                                      'transport (scripts/zcode_direct.py); '
                                      'app-server transport is macOS/Linux only']},
                         ensure_ascii=False))
        return 2
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--workspace', required=True)
    ap.add_argument('--prompt-file', required=True)
    ap.add_argument('--output-dir', required=True)
    ap.add_argument('--stage', required=True)
    ap.add_argument('--provider', default=DEFAULT_PROVIDER)
    ap.add_argument('--model', default=DEFAULT_MODEL,
                    help=f'Requested model id; defaults to {DEFAULT_MODEL}. Never falls back.')
    ap.add_argument('--reasoning', default='low', choices=REASONING_LEVELS)
    ap.add_argument('--mode', default='plan', choices=MODES,
                    help='plan is read-only; edit requires project-modification '
                         'authorization. yolo/guarded/auto are never offered.')
    ap.add_argument('--allow-tools', dest='allow_tools', default='',
                    help='Comma separated toolName list that may be auto-allowed '
                         '(allowOnce) when the session requests permission. '
                         'Default empty: every permission request is denied and '
                         'recorded. This transport has no SDK tool-catalog gate; '
                         'tool-level control happens through official permission '
                         'requests only.')
    ap.add_argument('--tools', default='',
                    help='Unsupported on this transport; any non-empty value is '
                         'refused (the SDK whole-tool catalog gate does not exist here).')
    ap.add_argument('--config', default=str(DEFAULT_CONFIG),
                    help='Local app-server entry config JSON.')
    ap.add_argument('--timeout', type=float, default=1800.0,
                    help='Wall-clock budget for the whole protocol session in seconds.')
    ap.add_argument('--preflight-only', dest='preflight_only', action='store_true',
                    help='Run runtime/capabilities only; creates no session and submits no prompt.')
    ap.add_argument('--dispatch-claim', dest='dispatch_claim', default=None)
    ap.add_argument('--dispatch-store', dest='dispatch_store', default=None)
    ap.add_argument('--quota-store', dest='quota_store', default=None)
    ap.add_argument('--quota-routes', dest='quota_routes', default=None)
    ap.add_argument('--chat-id', dest='chat_id', default=None)
    ap.add_argument('--task-id', dest='task_id', default=None)
    ap.add_argument('--session-mode-override', dest='session_mode_override',
                    default=None, choices=MODES,
                    help='Advanced: override the official session mode instead of '
                         'mapping --mode directly. plan stays read-only.')
    args = ap.parse_args(argv)

    reasons = []
    if args.tools and args.tools.strip():
        reasons.append('--tools is not supported on the app-server transport '
                       '(SDK whole-tool catalog gate does not exist here); '
                       'use --allow-tools for permission-level grants or omit it')
    reasons.extend(zd.validate_selection(
        stage=args.stage, provider=args.provider, model=args.model,
        reasoning=args.reasoning, mode=args.mode, tools=[]))
    if reasons:
        print(json.dumps({'refused_before_dispatch': True, 'sent': False,
                          'exit_code': 2, 'reasons': reasons}, ensure_ascii=False))
        return 2

    try:
        cfg = load_appserver_config(args.config)
    except (OSError, ValueError, KeyError, FileNotFoundError) as exc:
        print(json.dumps({'refused_before_dispatch': True, 'sent': False,
                          'exit_code': 2,
                          'reasons': [f'entry config invalid: {exc}']},
                         ensure_ascii=False))
        return 2

    allow_tools = []
    if args.allow_tools.strip():
        for piece in args.allow_tools.split(','):
            piece = piece.strip()
            if not piece:
                continue
            allow_tools.append(piece)
    if allow_tools and args.mode == 'plan':
        # plan 模式官方原生只读；放行名单只会被 plan 会话的只读权限请求触发，
        # 保留但如实记录（不宣称可写）。
        pass

    work = Path(args.workspace)
    if not work.is_dir():
        print(json.dumps({'refused_before_dispatch': True, 'sent': False,
                          'exit_code': 2,
                          'reasons': [f'workspace is not a directory: {work}']},
                         ensure_ascii=False))
        return 2
    work_real = str(Path(work).resolve())

    prompt_path = Path(args.prompt_file)
    if not prompt_path.is_file():
        print(json.dumps({'refused_before_dispatch': True, 'sent': False,
                          'exit_code': 2,
                          'reasons': [f'prompt file not found: {prompt_path}']},
                         ensure_ascii=False))
        return 2
    prompt_bytes = prompt_path.read_bytes()
    try:
        prompt_text = prompt_bytes.decode('utf-8')
    except UnicodeDecodeError:
        prompt_text = prompt_bytes.decode('utf-8', 'replace')
    prompt_text = prompt_text.replace('\r\n', '\n').replace('\r', '\n')
    prompt_sha256 = hashlib.sha256(prompt_bytes).hexdigest()

    out = Path(args.output_dir)
    quota_store = args.quota_store or str(qc.default_store_path())
    dispatch_store = args.dispatch_store or str(dp.default_store_path())
    task_id = args.task_id or args.stage
    wrapper_pid = os.getpid()
    wrapper_created = dp.process_identity(wrapper_pid).get('created')
    availability_gate = None
    claim_token = None
    pool_gate = None
    quota_verified_claim = None

    def _settle_availability(executed, success=False):
        if availability_gate is None or availability_gate.get('quota_group') is None:
            return None
        try:
            return qc.settle_zcode_attempt(
                quota_store, provider=args.provider,
                quota_group=availability_gate.get('quota_group'),
                attempt_id=availability_gate.get('attempt_id'),
                epoch=availability_gate.get('availability_epoch'),
                success=success, executed=executed,
                channel_key=availability_gate.get('granted_channel_key'))
        except Exception as exc:  # noqa: BLE001
            return {'settled': False, 'error': str(exc)}

    def _release_capacity(terminal, success=None):
        if claim_token is None:
            return {'released': False, 'terminal': terminal, 'reason': 'no_claim'}
        try:
            return dp.finish(dispatch_store, claim_token, terminal=terminal,
                             success=success, wrapper_pid=wrapper_pid,
                             wrapper_created=wrapper_created)
        except Exception as exc:  # noqa: BLE001
            return {'released': False, 'terminal': terminal, 'error': str(exc)}

    def _shared_state_refusal(exc, phase):
        """共享状态数据库无法打开/写入的结构化拒绝（BW-WEAK-RESTRUCTURE-20261116-R2）：
        环境权限/路径问题，**不是**认证、额度、时段或模型错误——如实分类、给出目标
        路径与原始异常，零提交、零占位、绝不输出裸 traceback。"""
        target = (quota_store if phase != 'capacity_claim' else dispatch_store)
        return _print_json({
            'refused_before_dispatch': True, 'sent': False, 'exit_code': 2,
            'shared_state_error': True,
            'error_class': 'shared_state_unwritable',
            'phase': phase,
            'store_path': target,
            'reasons': [f'{type(exc).__name__}: {exc}',
                        'the shared state database cannot be opened/written in this '
                        'session (environment permission or path problem) — this is '
                        'NOT an auth/quota/time-window/model error; nothing was '
                        'submitted and no placeholder was taken'],
            }, 2)

    def _print_json(payload, code):
        print(json.dumps(payload, ensure_ascii=False))
        return code

    quota_gate = {'allowed': True, 'purpose': 'registry_preflight'}
    if not args.preflight_only:
        # ---- ZCode 持久 availability 门（权威最后原子闸；与 zcode_direct 同口径）----
        try:
            availability_gate = qc.zcode_availability_gate(
                quota_store, provider=args.provider, routes_path=args.quota_routes,
                identity={'provider': args.provider}, purpose='dispatch')
        except (sqlite3.OperationalError, OSError) as exc:
            return _shared_state_refusal(exc, 'availability_gate')
        if not availability_gate['allowed']:
            return _print_json({'refused_before_dispatch': True, 'sent': False,
                                'exit_code': 2, 'zcode_availability_rejected': True,
                                'reasons': availability_gate['reasons'],
                                'channel_key': availability_gate.get('channel_key')}, 2)

        # ---- 跨会话并发容量门（Popen 前原子消费；同一 zcode 池，绝不新增池）----
        try:
            pool_gate = dp.consume_for_entry(
                dispatch_store, task_id=task_id, runtime='zcode', model=args.model,
                workspace=work_real, prompt_sha256=prompt_sha256, stage=args.stage,
                chat_id=args.chat_id, claim_token=args.dispatch_claim,
                wrapper_pid=wrapper_pid, wrapper_created=wrapper_created,
                executor='zcode', quota_store=quota_store,
                quota_routes=args.quota_routes)
        except (sqlite3.OperationalError, OSError) as exc:
            return _shared_state_refusal(exc, 'capacity_claim')
        if not pool_gate['allowed']:
            _settle_availability(executed=False)
            return _print_json({'refused_before_dispatch': True,
                                'capacity_gate_rejected': True,
                                'sent': False, 'exit_code': 2,
                                'routing_required': bool(pool_gate.get('routing_required')),
                                'reason': pool_gate.get('reason'),
                                'selected': pool_gate.get('selected'),
                                'pool_key': pool_gate.get('pool_key'),
                                'domestic_full': pool_gate.get('domestic_full'),
                                'reasons': pool_gate['reasons']}, 2)
        claim_token = pool_gate['token']
        quota_verified_claim = {'token': claim_token, 'store': dispatch_store,
                                'pool_key': pool_gate.get('pool_key'),
                                'capacity': dp.capacity_for(pool_gate.get('pool_key'))}

        # ---- 额度门（ZCode 手动额度政策照旧：无自动冷却，并发占位保留）----
        try:
            quota_gate = qc.gate_dispatch(
                quota_store, runtime='zcode',
                identity={'provider': args.provider},
                workspace=work_real,
                purpose='dispatch',
                routes_path=args.quota_routes,
                verified_pool_claim=quota_verified_claim)
        except (sqlite3.OperationalError, OSError) as exc:
            _settle_availability(executed=False)
            _release_capacity('start_failed')
            return _shared_state_refusal(exc, 'quota_gate')
        if not quota_gate['allowed']:
            _settle_availability(executed=False)
            _release_capacity('start_failed')
            return _print_json({'refused_before_dispatch': True, 'sent': False,
                                'quota_gate_rejected': True, 'exit_code': 2,
                                'reasons': quota_gate['reasons'],
                                'quota_group': quota_gate.get('quota_group')}, 2)

    def _settle_quota(terminal):
        # BW-WEAK-RESTRUCTURE-20261116-R2：preflight 无真实额度门（占位）→ 不结算、
        # 零共享状态写入。
        if args.preflight_only:
            return {'settled': False, 'released': False,
                    'terminal_state': 'no_attempt'}
        return qc.settle_attempt(quota_store, quota_gate, terminal=terminal)

    def _abort_before_start(payload, code):
        payload = dict(payload)
        payload['quota_settlement'] = _settle_quota('start_failed')
        payload['zcode_availability_settled'] = _settle_availability(executed=False)
        if claim_token is not None:
            rel = _release_capacity('start_failed')
            payload['capacity_released'] = bool(rel.get('released'))
        payload.setdefault('sent', False)
        print(json.dumps(payload, ensure_ascii=False))
        return code

    try:
        out.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        return _abort_before_start(
            {'refused_before_dispatch': True, 'exit_code': 2,
             'reasons': [f'output dir already exists: {out}']}, 2)
    except OSError as exc:
        return _abort_before_start(
            {'refused_before_dispatch': True, 'exit_code': 3,
             'reasons': [f'output dir could not be created before start: {exc!r}']}, 3)

    # 报告格式指令拼在原任务之前（与 SDK 传输同一契约，逐字保留任务）。
    report_contract = pc.build_contract(args.stage, work_real, runtime='zcode',
                                        model=args.model)
    sent_prompt = pc.compose_task_payload(report_contract, prompt_text)
    sent_payload_bytes = sent_prompt.encode('utf-8')
    (out / 'sent-task-payload.bin').write_bytes(sent_payload_bytes)
    sent_readback = (out / 'sent-task-payload.bin').read_bytes()
    prompt_payload = pc.payload_evidence(
        raw_prompt_bytes=prompt_bytes, sent_task_text=prompt_text,
        contract=report_contract, sent_payload_bytes=sent_payload_bytes,
        newline_caliber='CRLF/CR-to-LF; task=request content to session/send, '
                        'contract prepended')
    prompt_payload['readback_match'] = sent_readback == sent_payload_bytes
    prompt_payload['channels'] = {
        'request_prompt_sha256': pc.sha256_hex(sent_readback),
        'submit_channel': 'appserver-session-send',
        'payload_role': 'submission_prompt',
        'contract_prepended': True,
    }

    request = {
        'carrier': 'zcode-appserver',
        'stage': args.stage,
        'workspace': work_real,
        'provider_requested': args.provider,
        'model_requested': args.model,
        'reasoning_requested': args.reasoning,
        'mode': args.mode,
        'session_mode': args.session_mode_override or args.mode,
        'allow_tools': allow_tools,
        'timeout_seconds': args.timeout,
        'preflight_only': args.preflight_only,
        'prompt_sha256': prompt_sha256,
        'prompt_payload': prompt_payload,
        'entry_config_path': cfg['_entry_config_path'],
        'command': list(cfg['command']),
        'executor_identity': {
            'runtime': 'zcode', 'request_model': args.model,
            'provider_requested': args.provider, 'reasoning': args.reasoning,
            'source': 'entry runtime + requested model; not a verified backend identity',
            'contract_sha256': pc.sha256_hex(report_contract.encode('utf-8'))},
        'quota_gate': quota_gate,
        'quota_auto_cooldown_disabled': True,
        'quota_auto_cooldown_policy': (
            'ZCode auto quota cooldown disabled by user policy (manual quota '
            'management); concurrency placeholders retained; no claim of '
            'remaining quota, recovery, or free availability'),
        'permission_policy': {
            'default': 'deny every interaction/requestPermission (choose the '
                       'official deny option; record request and reply)',
            'allow_tools': allow_tools,
            'user_input': 'refused (headless)'},
    }
    (out / 'request.json').write_text(
        json.dumps(request, ensure_ascii=False, indent=2), encoding='utf-8')

    env = {**os.environ, **cfg['environment']}
    env.setdefault('PYTHONUTF8', '1')
    child = None
    submitted = False
    turn_ended = False
    timed_out = False
    session_id = None
    turn_id = None
    transport_errors = []
    events_path = out / 'events.jsonl'
    deadline = time.monotonic() + max(1.0, float(args.timeout))
    try:
        with events_path.open('w', encoding='utf-8') as events_fh, \
                (out / 'stderr.log').open('wb') as stderr_fh:
            child = subprocess.Popen(
                list(cfg['command']), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=stderr_fh,
                cwd=work_real, env=env)
            if claim_token is not None:
                bind = dp.bind_child(dispatch_store, claim_token, child.pid,
                                     wrapper_pid=wrapper_pid,
                                     wrapper_created=wrapper_created)
                if not bind.get('bound'):
                    dp.mark_unknown(dispatch_store, claim_token)
                    qc.settle_attempt(quota_store, quota_gate, terminal='unknown')
                    print(json.dumps({'dispatch_failed': True, 'sent': False,
                                      'terminal_state': 'unknown',
                                      'capacity_held': True,
                                      'reasons': [f'could not bind child to '
                                                  f'claim: {bind.get("reasons") or bind.get("reason")}']},
                                     ensure_ascii=False))
                    return 3
            (out / 'process.json').write_text(
                json.dumps({'pid': child.pid, 'state': 'running',
                            'created': wrapper_created,
                            'started_at_utc': _now()}), encoding='utf-8')
            client = AppServerClient(child, events_fh, allow_tools=allow_tools)

            def rpc(method, params, budget):
                rid = client.send_request(method, params)
                kind, obj, _ = client.read_until_response(
                    rid, min(deadline, time.monotonic() + budget))
                return kind, obj

            # 1) runtime/capabilities：只读预检；失败零提交。
            kind, obj = rpc('runtime/capabilities', None, 15)
            capabilities = None
            if kind != 'result':
                transport_errors.append(
                    f'runtime/capabilities {kind}: '
                    f'{json.dumps((obj or {}).get("error"), ensure_ascii=False)[:300]}')
            else:
                capabilities = obj['result']
            (out / 'preflight.json').write_text(json.dumps(
                {'carrier': 'zcode-appserver', 'capabilities': capabilities,
                 'capabilities_kind': kind, 'ok': kind == 'result',
                 'transport': 'stdio-zcode-protocol', 'at': _now()},
                ensure_ascii=False, indent=2), encoding='utf-8')
            if kind != 'result':
                return _abort_after_start(
                    out, child, dispatch_store, claim_token, quota_store,
                    quota_gate, _settle_availability, _release_capacity,
                    wrapper_pid, wrapper_created,
                    {'dispatch_failed': True, 'sent': False, 'exit_code': 3,
                     'reasons': ['runtime/capabilities failed before session '
                                 'create; never submitted']},
                    terminal='start_failed')
            if args.preflight_only:
                # 预检不建会话、不提交；杀进程回收，按成功结算（从未占额度/容量）。
                child.kill()
                child.wait(timeout=10)
                _write_process_exit(out, child)
                summary = _summary(args, work_real, out, child.returncode,
                                   ok=True, submitted=False, session_id=None,
                                   turn_id=None, turn_ended=False,
                                   transport_errors=[],
                                   client=None, preflight_only=True,
                                   protocol_success=True)
                _write_summary(out, summary)
                # BW-WEAK-RESTRUCTURE-20261116-R2：preflight 不再过额度门/拿占位——
                # 本分支零共享状态写入（无门、无结算、无锁），缺库也绝不建库。
                return 0

            # 2) session/create（服务器异步启动：结果可能滞后，窗口放宽）。
            kind, obj = rpc('session/create',
                            {'workspace': {'workspacePath': work_real,
                                           'workspaceKey': work_real}}, 60)
            if kind != 'result':
                transport_errors.append(
                    f'session/create {kind}: '
                    f'{json.dumps((obj or {}).get("error"), ensure_ascii=False)[:300]}')
                return _abort_after_start(
                    out, child, dispatch_store, claim_token, quota_store,
                    quota_gate, _settle_availability, _release_capacity,
                    wrapper_pid, wrapper_created,
                    {'dispatch_failed': True, 'sent': False, 'exit_code': 3,
                     'reasons': ['session/create failed; never submitted']},
                    terminal='start_failed')
            # 实测：create 结果的 projection.sessionId 是字面量 'unknown'，真实 id
            # 在 session/lifecycle/changed 通知里；且 setMode 前必须等 lifecycle
            # state=ready（否则 runtimeNotReady）。窗口内泵通知直到 ready。
            while client.lifecycle_state != 'ready' \
                    and time.monotonic() < deadline:
                kind_n, obj_n, _ = client.read_until_response(
                    None, min(deadline, time.monotonic() + 30))
                if kind_n in ('eof', 'timeout'):
                    break
            session_id = (client.lifecycle_session_id
                          or _find_first(obj['result'], 'sessionId'))
            if not session_id:
                return _abort_after_start(
                    out, child, dispatch_store, claim_token, quota_store,
                    quota_gate, _settle_availability, _release_capacity,
                    wrapper_pid, wrapper_created,
                    {'dispatch_failed': True, 'sent': False, 'exit_code': 3,
                     'reasons': ['session/create result carries no real '
                                 'sessionId (lifecycle never identified the '
                                 'session; honest refusal, never guessed)']},
                    terminal='start_failed')
            if client.lifecycle_state != 'ready':
                transport_errors.append(
                    f'session lifecycle never reached ready '
                    f'(state={client.lifecycle_state!r})')

            # 3) setMode（官方模式枚举 plan/build/edit/yolo/guarded/auto；
            #    本入口只提供 plan/edit，与 --mode 一一映射）。
            session_mode = args.session_mode_override or args.mode
            kind, obj = rpc('session/setMode',
                            {'sessionId': session_id, 'mode': session_mode}, 15)
            if kind != 'result':
                transport_errors.append(f'session/setMode {kind}')
                return _abort_after_start(
                    out, child, dispatch_store, claim_token, quota_store,
                    quota_gate, _settle_availability, _release_capacity,
                    wrapper_pid, wrapper_created,
                    {'dispatch_failed': True, 'sent': False, 'exit_code': 3,
                     'reasons': [f'session/setMode failed: {session_mode}'],
                     'session_id': session_id},
                    terminal='start_failed')

            # 4) setModel：显式绑定请求模型（与容量 claim 同一 model），官方拒绝即不提交。
            model_sel = {'providerId': args.provider, 'modelId': args.model,
                         'options': {'reasoningLevel': args.reasoning}}
            kind, obj = rpc('session/setModel',
                            {'sessionId': session_id, 'model': model_sel}, 15)
            if kind != 'result':
                transport_errors.append(
                    f'session/setModel {kind}: '
                    f'{json.dumps((obj or {}).get("error"), ensure_ascii=False)[:300]}')
                return _abort_after_start(
                    out, child, dispatch_store, claim_token, quota_store,
                    quota_gate, _settle_availability, _release_capacity,
                    wrapper_pid, wrapper_created,
                    {'dispatch_failed': True, 'sent': False, 'exit_code': 3,
                     'reasons': [f'session/setModel refused for '
                                 f'{args.provider}/{args.model}; never submitted'],
                     'session_id': session_id},
                    terminal='start_failed')

            # 5) session/send：唯一提交点。
            kind, obj = rpc('session/send',
                            {'sessionId': session_id, 'content': sent_prompt}, 30)
            if kind != 'result':
                transport_errors.append(
                    f'session/send {kind}: '
                    f'{json.dumps((obj or {}).get("error"), ensure_ascii=False)[:300]}')
                return _abort_after_start(
                    out, child, dispatch_store, claim_token, quota_store,
                    quota_gate, _settle_availability, _release_capacity,
                    wrapper_pid, wrapper_created,
                    {'dispatch_failed': True, 'sent': False, 'exit_code': 3,
                     'reasons': ['session/send failed; no turn started'],
                     'session_id': session_id},
                    terminal='start_failed')
            submitted = True
            turn_id = _find_first(obj['result'], 'turnId')
            (out / 'process.json').write_text(json.dumps(
                {'pid': child.pid, 'state': 'running', 'submitted': True,
                 'session_id': session_id, 'turn_id': turn_id,
                 'submitted_at_utc': _now()}), encoding='utf-8')

            # 6) 泵到 turnEnded（wall-clock 预算内）；全部报文已实时落 events.jsonl。
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                kind, obj, _ = client.read_until_response(None, deadline)
                if kind == 'eof':
                    transport_errors.append('app-server closed stdout before turnEnded')
                    break
                if kind == 'timeout':
                    timed_out = True
                    break
                if obj is not None and _is_turn_ended(obj):
                    turn_ended = True
                    break

            # 7) 收响应：turnEnded 后读 session/messages；超时/EOL 不读，如实失败。
            response_text = None
            if turn_ended:
                kind, obj = rpc('session/messages', {'sessionId': session_id}, 30)
                if kind == 'result':
                    (out / 'messages.json').write_text(
                        json.dumps(obj['result'], ensure_ascii=False, indent=2),
                        encoding='utf-8')
                    texts = _assistant_texts(obj['result'])
                    if texts:
                        response_text = '\n\n'.join(t for t in texts if t.strip())
                else:
                    transport_errors.append(f'session/messages {kind}')
            elif not timed_out and child.poll() is None:
                pass  # EOF：进程已死，真实退出码在下面统一结算。

            # 8) 关会话（尽力而为，各 5 秒预算），再回收子进程。
            if session_id and child.poll() is None:
                try:
                    rpc('session/stop', {'sessionId': session_id}, 5)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    rpc('session/close', {'sessionId': session_id}, 5)
                except Exception:  # noqa: BLE001
                    pass
            if child.poll() is None:
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        pass
    except KeyboardInterrupt:
        if claim_token is not None:
            dp.mark_unknown(dispatch_store, claim_token)
        qc.settle_attempt(quota_store, quota_gate, terminal='unknown')
        raise
    except Exception as exc:  # noqa: BLE001
        if child is None:
            _settle_availability(executed=False)
            rel = _release_capacity('start_failed')
            settlement = qc.settle_attempt(quota_store, quota_gate,
                                           terminal='start_failed')
            print(json.dumps({'dispatch_failed': True, 'sent': False,
                              'capacity_released': bool(rel.get('released')),
                              'reasons': [f'child launch failed before start: {exc}'],
                              'quota_settlement': settlement}, ensure_ascii=False))
            return 3
        if claim_token is not None:
            dp.mark_unknown(dispatch_store, claim_token)
        settlement = qc.settle_attempt(quota_store, quota_gate, terminal='unknown')
        print(json.dumps({'dispatch_failed': True, 'sent': submitted,
                          'terminal_state': 'unknown',
                          'reasons': [f'subprocess terminal state could not be '
                                      f'confirmed: {exc}',
                                      'workspace/channel placeholders retained '
                                      '(fail-closed)'],
                          'quota_settlement': settlement}, ensure_ascii=False))
        return 3

    _write_process_exit(out, child)
    ok = (child.returncode == 0 and submitted and turn_ended
          and response_text is not None and not transport_errors)
    protocol_success = bool(ok and not timed_out)
    summary = _summary(args, work_real, out, child.returncode, ok=ok,
                       submitted=submitted, session_id=session_id, turn_id=turn_id,
                       turn_ended=turn_ended, transport_errors=transport_errors,
                       client=client, preflight_only=False,
                       protocol_success=protocol_success, timed_out=timed_out)
    if response_text is not None:
        resp_path = out / 'response.md'
        resp_path.write_text(response_text, encoding='utf-8')
        disk = resp_path.read_bytes()
        summary['response_sha256'] = hashlib.sha256(disk).hexdigest()
        summary['response_bytes'] = len(disk)
        try:
            text = disk.decode('utf-8')
            body = analyze_report(text, args.stage, work_real)
            binding = finalize_binding(
                body['body_ok'], body['reasons'],
                protocol_success=protocol_success,
                session_id=session_id, requested_session_id=None,
                readback_match=True)
            summary['report_state'] = {**body, 'response_sha256': summary['response_sha256'],
                                       'protocol_success': protocol_success,
                                       'session_id': session_id,
                                       'binding': binding}
        except UnicodeDecodeError as exc:
            summary['report_state'] = {'bound': False, 'body_ok': False,
                                       'reasons': [f'response.md is not valid '
                                                   f'UTF-8: {exc}']}
    else:
        summary['report_state'] = {'bound': False, 'body_ok': False,
                                   'carrier_missing': True,
                                   'reasons': ['no assistant text extracted '
                                               '(turn not ended or messages empty)']}
    _write_summary(out, summary)

    # ---- 终态结算：availability（真实执行）→ 容量释放（owner 身份）→ 额度 ----
    # RELEASED_STATES 只有 finished/start_failed/cancelled/reconciled_exit；timeout/failed
    # 属真实退出（child 已死），与 zcode_direct 同口径以 finished+success 标志释放。
    _settle_availability(executed=True, success=protocol_success)
    terminal = 'timeout' if timed_out else ('finished' if protocol_success
                                            else 'failed')
    rel = _release_capacity('finished', success=protocol_success)
    summary['capacity_release'] = rel
    summary['capacity_released'] = bool(rel.get('released'))
    summary['capacity_terminal'] = rel.get('terminal')
    _write_summary(out, summary)
    qc.settle_attempt(
        quota_store, quota_gate, terminal='confirmed_exit',
        success=protocol_success,
        source='zcode_appserver_direct terminal (exit '
               f'{child.returncode}, protocol_success={protocol_success}, '
               f'timed_out={timed_out})')
    print(json.dumps({'sent': submitted, 'exit_code': 0 if protocol_success else 3,
                      'terminal_state': terminal, 'session_id': session_id,
                      'turn_id': turn_id, 'turn_ended': turn_ended,
                      'timed_out': timed_out,
                      'protocol_success': protocol_success,
                      'capacity_released': bool(rel.get('released'))},
                     ensure_ascii=False))
    return 0 if protocol_success else 3


def _abort_after_start(out, child, dispatch_store, claim_token, quota_store,
                       quota_gate, _settle_availability, _release_capacity,
                       wrapper_pid, wrapper_created, payload, terminal='start_failed'):
    """Popen 后、提交前的拒绝路径：进程回收 + start_failed 结算 + 容量释放。
    子进程确实启动过，但其终态可被确认（graceful kill + wait），不落 unknown。"""
    if child.poll() is None:
        try:
            child.kill()
            child.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass
    _write_process_exit(out, child)
    # BW-WEAK-RESTRUCTURE-20261116-R2：preflight 占位额度门 → 不结算（零共享状态写入）。
    if quota_gate.get('purpose') == 'registry_preflight':
        settlement = {'settled': False, 'released': False,
                      'terminal_state': 'no_attempt'}
    else:
        settlement = qc.settle_attempt(quota_store, quota_gate, terminal=terminal)
    availability_settled = _settle_availability(executed=False)
    rel = _release_capacity(terminal)
    payload = dict(payload)
    payload['quota_settlement'] = settlement
    payload['zcode_availability_settled'] = availability_settled
    payload['capacity_released'] = bool(rel.get('released'))
    payload.setdefault('sent', False)
    print(json.dumps(payload, ensure_ascii=False))
    return 3


def _write_process_exit(out, child):
    (out / 'process.json').write_text(json.dumps(
        {'pid': child.pid, 'state': 'exited', 'exit_code': child.returncode,
         'finished_at_utc': _now()}), encoding='utf-8')


def _summary(args, workspace, out, returncode, *, ok, submitted, session_id,
             turn_id, turn_ended, transport_errors, client, preflight_only,
             protocol_success, timed_out=False):
    summary = {
        'carrier': 'zcode-appserver', 'exit_code': returncode,
        'finished_at_utc': _now(), 'stage': args.stage, 'workspace': workspace,
        'output_dir': str(out), 'provider_requested': args.provider,
        'model_requested': args.model, 'reasoning_requested': args.reasoning,
        'mode': args.mode, 'session_mode': args.session_mode_override or args.mode,
        'allow_tools': [t for t in (args.allow_tools.split(',') if args.allow_tools else '') if t],
        'preflight_only': preflight_only, 'submitted': submitted,
        'session_id': session_id, 'turn_id': turn_id, 'turn_ended': turn_ended,
        'timed_out': timed_out, 'transport_errors': transport_errors,
        'protocol_success': protocol_success, 'business_verified': False,
        'free_quota_verified': False, 'capacity_released': False,
        'capacity_terminal': None,
        'quota_auto_cooldown_disabled': True,
        'quota_auto_cooldown_policy': (
            'ZCode auto quota cooldown disabled by user policy (manual quota '
            'management); concurrency placeholders retained; no claim of '
            'remaining quota, recovery, or free availability'),
    }
    if client is not None:
        summary['permission_denied'] = client.permission_denied
        summary['permission_auto_allowed'] = client.permission_auto_allowed
        summary['unsupported_server_requests'] = client.unsupported_server_requests
        summary['permission_events'] = client.permission_events
    return summary


def _write_summary(out, summary):
    (out / 'summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    sys.exit(main())
