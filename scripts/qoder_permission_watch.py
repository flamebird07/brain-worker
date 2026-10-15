"""Qoder CN 会话流权限拒绝监视器（BW-CN-PERMISSION-STOP-20261015T155930Z-S1）。

背景（原始故障，保留不改写）：一次 CN 派工只带裸工具名 allow 规则，dont_ask 下
Write 新建嵌套目标被拒，模型随后改用 Bash touch/ls/rm 与重定向反复试写（再次被拒），
主脑 1 分 55 秒后人工终止。本模块给 CN 正式入口提供**有界机械监视**：

- 新调用由入口生成独立 UUID 并经官方 `--session-id` 传给 CN（官方 help 已列该参数；
  本阶段未联网复验）；resume 沿用已验证原 session（`--resume`），基线跳过旧事件。
- 现场路径已核实为 `config_dir/projects/<workspace-key>/<精确sessionId>.jsonl`（**不是**
  `projects/<sessionId>.jsonl`）：只按**精确 UUID 文件名**在 `projects` 各 workspace-key 目录下
  定位这**一个**文件，只读目录项；绝不打开或扫描其它会话正文。新会话文件可能在 Popen 之后
  才出现，定位是动态的（无文件时不宣称已观察到会话）；重复候选或 IO 异常明确降级、绝不任意
  挑一个；resume 基线在启动前冻结旧文件 EOF，后续新增事件绝不被首次 poll 吞掉。
- 仅接受 `sessionId` 精确一致、`cwd` 真实路径归一（含 Windows 8.3 短名）与本工作区一致、
  `type=user` 的真实 `message.content` tool_result；命中**事件顶层或 tool_result 块内**的
  结构化 `toolDenialKind=permission-rule`，或 tool_result 直接结果的固定拒绝文案——现场真实的
  `This action needs approval, and the "Don't ask" permission mode does not prompt.` 与既有
  codebuddy-direct 档案记录的同系 CLI 文案 `Permission to use … has been denied`——即请求停止本
  child。固定文案**只在工具结果起始处**（允许可选 `Error:` 前缀与前置空白）直接构成该拒绝形状时
  命中；被夹带/引用在正常 stdout 中间（如 "echoed diagnostic: Permission to use Write has been
  denied …"）绝不误停，也不对任意 shell 输出做宽松 substring 扫描。
- 绝不扫描用户/助手叙述或嵌套引用；普通工具错误、402/429、取消都不是审批拒绝；
  `is_error=false` 不掩盖真实拒绝。
- 停止只经**本次 Popen 对象** terminate→等待真实退出（必要时 kill 升级），绝不按进程
  名杀、绝不碰别的 attempt/槽；终态无法确认时调用方应 mark_unknown 保留占位。
- communicate 仍由子线程**恰好一次**携带 input 调用（正确送 stdin）；超时/停止后绝不
  重发 input。主循环约 0.2 秒短轮询（threading，纯标准库）。
- 读错/基线失败只记录事实（facts），绝不悄悄当保护成功；本模块不宣称零延迟或零后续
  工具竞态——拒绝发生后、终止生效前，child 仍可能执行有限动作。

本模块只做观察与请求停止；是否放行业务仍由主脑按原始工具结果验收。
"""
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

POLL_INTERVAL_SECONDS = 0.2
STOP_GRACE_SECONDS = 5.0

# 会话 ID 只允许安全文件名字符（防路径逃逸；官方 session id 为 UUID 形状）。
_SESSION_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')

# tool_result 固定拒绝文案：只接受工具结果**起始处**直接构成的固定拒绝形状（允许可选 `Error:`
# 前缀与前置空白），其后必须立即是固定拒绝句；被引用/夹带在正常输出中间不算，绝不对任意
# 输出做宽松 substring 扫描。
# 1) 现场真实的 CN dont_ask 文案（2026-10-15 现场回读到的本次事件）：
#    "Error: This action needs approval, and the \"Don't ask\" permission mode does not
#     prompt. Switch modes to approve it."
# 2) 既有 codebuddy-direct 档案记录的同系 CLI 文案：
#    "Permission to use <tool> … has been denied"（见 references/codebuddy-direct.md）。
# 两者都要求两段固定搭配同时出现；普通错误、402/429、取消文案不会命中。
_LEADING_ERR_PREFIX = r'^\s*(?:error:\s*)?'
_TOOL_RESULT_DENIED_RE = re.compile(
    _LEADING_ERR_PREFIX + r'permission\s+to\s+use\b.{0,200}?has\s+been\s+denied',
    re.IGNORECASE | re.DOTALL)
_ACTION_NEEDS_APPROVAL_RE = re.compile(
    _LEADING_ERR_PREFIX + r'this\s+action\s+needs\s+approval\b.{0,120}?does\s+not\s+prompt',
    re.IGNORECASE | re.DOTALL)

_FIXED_DENIAL_RES = (_ACTION_NEEDS_APPROVAL_RE, _TOOL_RESULT_DENIED_RE)

_PROJECTS_DIRNAME = 'projects'
_SESSION_FILE_SUFFIX = '.jsonl'
_EXCERPT_LIMIT = 200


def is_safe_session_id(session_id) -> bool:
    return isinstance(session_id, str) and bool(_SESSION_ID_RE.match(session_id))


def is_safe_workspace_key(workspace_key) -> bool:
    """workspace-key 是 projects 下的单层目录名：只允许安全文件名字符，绝不接受路径分隔。"""
    return isinstance(workspace_key, str) and bool(_SESSION_ID_RE.match(workspace_key))


def session_jsonl_path(config_dir, session_id, workspace_key) -> Path:
    """按现场核实布局拼出精确会话文件：
    `config_dir/projects/<workspace-key>/<sessionId>.jsonl`。只拼路径、不扫描。"""
    if not is_safe_session_id(session_id):
        raise ValueError(f'unsafe session id for CN session watch: {session_id!r}')
    if not is_safe_workspace_key(workspace_key):
        raise ValueError(f'unsafe workspace key for CN session watch: {workspace_key!r}')
    return (Path(config_dir) / _PROJECTS_DIRNAME / workspace_key
            / (session_id + _SESSION_FILE_SUFFIX))


def find_session_jsonl(config_dir, session_id) -> tuple[Path | None, str | None]:
    """在 `projects/<workspace-key>/` 各子目录下按**精确文件名** `<sessionId>.jsonl` 定位。

    只读目录项名与 `is_file()`，绝不打开/扫描任何会话正文（也不看别的 session 文件）。
    返回 (path, error)：
    - 恰好一个候选 → (该路径, None)；
    - 零个候选（含 projects 目录尚不存在）→ (None, None)：会话尚未观察到；
    - 多个候选 → (None, 说明)：重复候选明确降级，绝不任意挑一个；
    - IO 异常 → (None, 说明)：明确降级，不冒充保护成功。"""
    if not is_safe_session_id(session_id):
        raise ValueError(f'unsafe session id for CN session watch: {session_id!r}')
    root = Path(config_dir) / _PROJECTS_DIRNAME
    name = session_id + _SESSION_FILE_SUFFIX
    try:
        keys = sorted(p for p in root.iterdir() if p.is_dir())
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f'projects dir listing failed: {exc!r}'
    hits = []
    for key_dir in keys:
        candidate = key_dir / name
        try:
            if candidate.is_file():
                hits.append(candidate)
        except OSError as exc:
            return None, f'candidate probe failed for {candidate}: {exc!r}'
    if len(hits) > 1:
        return None, (f'ambiguous session files: {len(hits)} candidates named {name!r} '
                      f'under {root}; refusing to pick one arbitrarily')
    if not hits:
        return None, None
    return hits[0], None


def _norm_path(p: str) -> str:
    """真实路径 + 平台大小写语义归一（与 qoder_direct._norm_path 同口径）。

    先 os.path.realpath 把 Windows 8.3 短名/符号链接解析成真实长路径（ADMINI~1 与
    Administrator 指向同一工作区时必须归一一致），再按平台语义规范大小写
    （os.path.normcase：Windows 小写并统一分隔符，POSIX 保留大小写以维持大小写敏感的
    身份核验）。绝不通过删除 cwd 校验来放宽工作区身份。"""
    try:
        raw = str(p).strip().strip('"')
        return os.path.normcase(os.path.realpath(raw)).rstrip('\\/')
    except Exception:  # noqa: BLE001
        return str(p).strip().lower()


def _iter_block_text(content):
    """只取 tool_result 自身的一层文本：str、[{type:text,text}]、{text:str}。
    绝不递归深挖嵌套引用，也不看消息叙述。"""
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get('type') == 'text' \
                    and isinstance(item.get('text'), str):
                yield item['text']
    elif isinstance(content, dict) and isinstance(content.get('text'), str):
        yield content['text']


def denial_from_line(obj, session_id, workspace_norm):
    """判定一行原生 jsonl 事件是否为本会话、本工作区的真实审批拒绝。

    绑定条件缺一不可：sessionId 精确一致、cwd 真实路径归一一致、type=user、
    message.content 里存在 tool_result 块。命中返回最小拒绝记录，否则 None。

    结构化标记的取值层级与现场一致：**事件顶层** `toolDenialKind`（本次真实事件即如此），
    以及兼容 tool_result 块内/块 content 内的同名字段；`is_error` 为 false 也不掩盖真实拒绝。"""
    if not isinstance(obj, dict):
        return None
    if obj.get('sessionId') != session_id:
        return None
    cwd = obj.get('cwd')
    if not isinstance(cwd, str) or _norm_path(cwd) != workspace_norm:
        return None
    if obj.get('type') != 'user':
        return None
    message = obj.get('message')
    if not isinstance(message, dict):
        return None
    content = message.get('content')
    if not isinstance(content, list):
        return None
    root_kind = obj.get('toolDenialKind')
    for block in content:
        if not isinstance(block, dict) or block.get('type') != 'tool_result':
            continue
        # 结构化信号优先（事件顶层 → 块内 → 块 content 内）；is_error=false 不掩盖真实拒绝。
        kind = block.get('toolDenialKind')
        nested = block.get('content')
        nested_kind = nested.get('toolDenialKind') if isinstance(nested, dict) else None
        for candidate in (root_kind, kind, nested_kind):
            if isinstance(candidate, str) and candidate.strip() == 'permission-rule':
                return {'kind': 'permission-rule',
                        'tool_use_id': block.get('tool_use_id'),
                        'is_error': block.get('is_error'),
                        'toolDenialKind': candidate,
                        'text_excerpt': None,
                        'raw_event': {'type': obj.get('type'),
                                      'sessionId': obj.get('sessionId'),
                                      'cwd': obj.get('cwd'),
                                      # 顶层拒绝标记随最小原始事件一并保留（原件不改）。
                                      'toolDenialKind': root_kind,
                                      'tool_result': block}}
        for text in _iter_block_text(nested):
            if not isinstance(text, str):
                continue
            # 起始锚定（match）：仅当工具结果自身以固定拒绝句开头（允许 Error: 前缀）才命中。
            if any(rx.match(text) for rx in _FIXED_DENIAL_RES):
                return {'kind': 'fixed-text',
                        'tool_use_id': block.get('tool_use_id'),
                        'is_error': block.get('is_error'),
                        'toolDenialKind': (root_kind if isinstance(root_kind, str)
                                           else (kind if isinstance(kind, str) else None)),
                        'text_excerpt': text[:_EXCERPT_LIMIT],
                        'raw_event': {'type': obj.get('type'),
                                      'sessionId': obj.get('sessionId'),
                                      'cwd': obj.get('cwd'),
                                      'toolDenialKind': root_kind,
                                      'tool_result': block}}
    return None


class SessionDenialWatcher:
    """单会话增量监视：动态定位 `projects/<workspace-key>/<sessionId>.jsonl`，从基线偏移
    只读新增字节，按完整行解析（容忍分段写入）。

    定位只按精确 UUID 文件名（只读目录项，不扫描其它会话正文）；文件可能在 Popen 之后才
    出现，故定位在每个 poll 里重试，无文件时绝不宣称已观察到会话；重复候选/IO 异常明确
    降级、不任意挑一个。resume 场景基线在启动前冻结现存文件 EOF（旧事件绝不重扫触发）；
    文件缩短（轮转/重写）只记录事实并重新基线，被跳过的内容不宣称已观察。

    `line_number` 是**会话文件里的绝对行号**（基线前的历史行计入位置但绝不冒充已解析：
    它们只体现在 `baseline_line_number`/`history_lines_skipped`，不增加 `lines_seen`）；
    `lines_seen` 只计基线之后真正读到的非空行。所有就绪/错误/停止事实进 status，
    供调用方如实留证；读错绝不冒充保护成功。"""

    def __init__(self, config_dir, session_id, workspace):
        self._config_dir = str(config_dir)
        self.session_id = session_id
        self._workspace = str(workspace)
        self._workspace_norm = _norm_path(workspace)
        self.path = None
        self.status = []
        self.denial = None
        self.baseline_bytes = 0
        self.baseline_line_number = 0
        self._buffer = b''
        self._offset = 0
        self._raw_lines_consumed = 0
        self._lines_seen = 0
        self._parse_errors = 0
        self._polls = 0
        self._error_notes = 0
        self._locate_degraded = False
        self._locate(initial=True)

    def _locate(self, *, initial: bool) -> None:
        """定位本会话文件并冻结基线；已定位则不再重找（不追踪路径漂移）。"""
        if self.path is not None:
            return
        try:
            path, error = find_session_jsonl(self._config_dir, self.session_id)
        except ValueError:
            raise
        if error is not None:
            if not self._locate_degraded:
                self._locate_degraded = True
                self._note('error', f'session file location degraded: {error}')
            return
        if path is None:
            if initial:
                self._note('ready', 'no session jsonl for this session under '
                                    'projects/<workspace-key>/ yet; the session is NOT '
                                    'claimed as observed; watching for creation')
            return
        try:
            if initial and path.is_file():
                size = path.stat().st_size
                self._offset = size
                self.baseline_bytes = size
                self.baseline_line_number = self._count_lines(path)
                self._note('ready', f'existing session file baselined at {size} bytes / '
                                    f'{self.baseline_line_number} pre-existing line(s); '
                                    f'resume history is never re-scanned or triggered')
            else:
                # 文件在基线之后才出现（新会话）：从 0 开始读，绝不吞掉已写入的新增事件。
                self._offset = 0
                self.baseline_bytes = 0
                self.baseline_line_number = 0
                self._note('ready', f'session file located at {path} after start '
                                    f'(baseline 0 bytes); reading from the beginning')
        except OSError as exc:
            self._note('error', f'baseline stat failed: {exc!r}')
            return
        self.path = path

    @staticmethod
    def _count_lines(path) -> int:
        """基线前的完整行数（只为本会话文件的绝对行号计位，不解析内容）。"""
        try:
            return path.read_bytes().count(b'\n')
        except OSError:
            return 0

    def _note(self, fact: str, detail: str) -> None:
        if fact == 'error':
            self._error_notes += 1
        self.status.append({'fact': fact, 'at_utc': datetime.now(timezone.utc).isoformat(),
                            'detail': detail})

    def facts(self) -> dict:
        return {'config_dir': self._config_dir,
                'session_id': self.session_id,
                'projects_file': str(self.path) if self.path is not None else None,
                'workspace': self._workspace,
                'baseline_bytes': self.baseline_bytes,
                'baseline_line_number': self.baseline_line_number,
                'history_lines_skipped': self.baseline_line_number,
                'session_observed': self.path is not None,
                'bytes_consumed': self._offset,
                'lines_seen': self._lines_seen,
                'parse_errors': self._parse_errors,
                'polls': self._polls,
                'degraded': bool(self._error_notes),
                'events': self.status}

    def poll(self):
        """增量读取一次；返回新发现的拒绝记录（此后幂等返回同一记录）或 None。"""
        self._polls += 1
        if self.denial is not None:
            return self.denial
        self._locate(initial=False)
        if self.path is None:
            return None
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return None
        except OSError as exc:
            self._note('error', f'stat failed: {exc!r}')
            return None
        if size < self._offset:
            self._note('error', f'session file shrank {self._offset}->{size} bytes '
                                f'(rotated/rewritten); re-baselined, skipped content '
                                f'NOT observed')
            self._offset = size
            self._buffer = b''
            self.baseline_line_number = self._count_lines(self.path)
            self._raw_lines_consumed = 0
            return None
        if size == self._offset:
            return None
        try:
            with self.path.open('rb') as fh:
                fh.seek(self._offset)
                chunk = fh.read(size - self._offset)
        except OSError as exc:
            self._note('error', f'read failed: {exc!r}')
            return None
        self._offset = size
        data = self._buffer + chunk
        self._buffer = b''
        while b'\n' in data:
            raw, _, data = data.partition(b'\n')
            denial = self._consume_line(raw)
            if denial is not None:
                self._buffer = data
                return denial
        self._buffer = data
        return None

    def _absolute_line_number(self) -> int:
        return self.baseline_line_number + self._raw_lines_consumed

    def _consume_line(self, raw: bytes):
        self._raw_lines_consumed += 1  # 绝对行号计位（含空行），与"已解析"分开
        if not raw.strip():
            return None
        self._lines_seen += 1  # 基线之后真正读到的非空行（不含被跳过的历史）
        line_number = self._absolute_line_number()
        try:
            obj = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, ValueError) as exc:
            self._parse_errors += 1
            self._note('error', f'line {line_number} is not valid UTF-8 JSON: '
                                f'{exc!r}; line skipped, watch continues')
            return None
        record = denial_from_line(obj, self.session_id, self._workspace_norm)
        if record is None:
            return None
        record.update({'source': str(self.path),
                       'line_number': line_number,
                       'observed_at_utc': datetime.now(timezone.utc).isoformat()})
        self.denial = record
        self._note('stop', f"observed {record['kind']} denial for tool_use_id "
                           f"{record.get('tool_use_id')!r} at line {line_number}")
        return record


def communicate_with_permission_watch(child, payload, watcher, *,
                                      poll_interval=POLL_INTERVAL_SECONDS,
                                      stop_grace=STOP_GRACE_SECONDS) -> dict:
    """CN 专用 communicate：子线程恰好一次 `child.communicate(input=payload)` 正确送
    stdin（超时/停止后绝不重发 input）；主线程约 poll_interval 短轮询会话 jsonl。

    发现审批拒绝 → 若 child 尚未自然退出，则**只经本次 Popen 对象** terminate（必要时
    kill 升级）并等待真实退出；模型已自然退出的拒绝如实分类为 observed_after_exit，
    绝不伪称已终止。终态无法确认时 exit_confirmed=False（调用方 mark_unknown 保留
    占位）。不宣称零延迟或零后续工具竞态：终止生效前 child 仍可能执行有限动作。"""
    outcome = {'denial': None, 'terminated_by_watch': False, 'observed_after_exit': False,
               'model_exited_before_stop': False, 'stop_requested': False,
               'exit_confirmed': False, 'child_returncode': None, 'polls': 0,
               'stop_signal': None, 'stop_signal_error': None,
               'communicate_error': None, 'exit_wait_error': None}
    box = {}

    def _communicate_once():
        try:
            child.communicate(input=payload)
            box['communicate_returned'] = True
        except Exception as exc:  # noqa: BLE001 - 如实记录，绝不吞成成功
            box['communicate_error'] = f'{type(exc).__name__}: {exc}'

    comm = threading.Thread(target=_communicate_once, name='cn-permission-watch',
                            daemon=True)
    comm.start()
    stop_attempted = False
    while True:
        comm.join(timeout=poll_interval)
        outcome['polls'] += 1
        if not comm.is_alive():
            break
        denial = watcher.poll()
        if denial is None:
            continue
        outcome['denial'] = denial
        if child.poll() is not None:
            # 模型在停止动作落地前已自然退出：如实分类，不伪称由监视器终止。
            outcome['model_exited_before_stop'] = True
            break
        stop_attempted = True
        try:
            child.terminate()
            outcome['stop_signal'] = 'terminate'
        except OSError as exc:
            outcome['stop_signal_error'] = f'terminate: {exc!r}'
        comm.join(timeout=stop_grace)
        if comm.is_alive():
            try:
                child.kill()
                outcome['stop_signal'] = 'kill_after_terminate'
            except OSError as exc:
                outcome['stop_signal_error'] = f'kill: {exc!r}'
            comm.join(timeout=stop_grace)
        break
    if outcome['denial'] is None:
        # 自然退出后的最终增量读取：模型可能在轮询间隙已结束，但会话流里已有拒绝。
        late = watcher.poll()
        if late is not None:
            outcome['denial'] = late
            outcome['model_exited_before_stop'] = True
    outcome['stop_requested'] = stop_attempted
    outcome['observed_after_exit'] = bool(outcome['denial'] is not None
                                          and not stop_attempted)
    outcome['communicate_error'] = box.get('communicate_error')
    rc = child.poll()
    if rc is None:
        try:
            rc = child.wait(timeout=stop_grace)
        except Exception as exc:  # noqa: BLE001 - 假 child 可能没有 wait/仍超时：如实未确认
            outcome['exit_wait_error'] = f'{type(exc).__name__}: {exc}'
            rc = None
    outcome['child_returncode'] = rc
    outcome['exit_confirmed'] = rc is not None
    outcome['terminated_by_watch'] = bool(stop_attempted and outcome['exit_confirmed'])
    return outcome
