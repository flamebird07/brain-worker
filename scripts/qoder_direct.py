"""Direct Qoder invocation: transport layer for the brain-worker skill (direct mode).

从已验证的 validation/QODER-DIRECT-01/qoder_direct.py 最小改动而来：
- 运行时不再假设与脚本同目录，改由本机入口配置（--config，默认脚本旁
  local-entry.json）提供 node 与 qodercli 绝对路径；
- response.md 以字节原样落盘（不做换行翻译），新增 report-state.json 汇总
  报告验收证据（标记独占行/阶段字段/项目路径/九节/哈希回读）；
- 协议成功不等于业务验收：退出码只反映协议终态，报告绑定结论以
  report-state.json 为准，缺失或不符一律不宣称已绑定。
- 正文格式检查与最终绑定分离：analyze_report 只做机械正文核对（body_ok），
  finalize_binding 再综合协议终态、session_id、resume 精确一致与哈希回读
  得出最终 bound；机械正文合格不冒充已绑定。
- BW-CN-PERMISSION-STOP-20261015T155930Z-S1（仅 CN 分支）：Popen 前静态拒绝裸/整工具
  通配 Edit/Write/NotebookEdit/Bash allow 规则；新会话经官方 --session-id 下发独立 UUID，
  qoder_permission_watch 增量监视本会话原生 jsonl，真实审批拒绝即经本次 Popen 对象终止
  并等待真实退出（终态不明确保 unknown 占位；国际路径 argv/通信不变）。
- BW-CN-PERMISSION-STOP-20261015T155930Z-S2（仅 CN 分支）：监视器改为按现场核实的
  `config_dir/projects/<workspace-key>/<sessionId>.jsonl` 精确文件名定位（兼容文件在 Popen
  之后才出现），`_norm_path` 改按真实路径 + 平台大小写语义归一（Windows 8.3 短名与长名
  指向同一工作区时必须一致）；CN 生成的 session id 与 envelope.session_id 不一致时如实记录
  并拒绝绑报告（不当认证/额度事实）。国际路径与既有行为不变。
- BW-CN-PERMISSION-STOP-20261015T155930Z-S3（仅 CN 分支）：固定拒绝文案改为只接受工具结果
  **起始处**（允许可选 `Error:` 前缀）直接构成拒绝的锚定形状，绝不误停输出中间对同一句的引用；
  CN 所有已知终态都随 `summary.cn_watch_facts`/unknown 输出留本轮监视事实（无拒绝的正常结束也
  可回读，未找到文件如实 `session_observed=false`）。国际路径与既有行为不变。
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
import execution_control as ec  # noqa: E402 任务级控制面（同目录，标准库）
import prompt_contract as pc  # noqa: E402 三入口共享九节契约（标准库，不反向导入本模块）
import dispatch_pool as dp  # noqa: E402 跨会话并发容量池（同目录，标准库 sqlite3）
import dispatch_mode as dmode  # noqa: E402 档位/用户禁用开关（同目录，标准库）
import qoder_permission_watch as qpw  # noqa: E402 CN 会话权限拒绝监视（同目录，标准库）
import qoder_permissions as qp  # noqa: E402 从任务 scope 生成首次派发的精确规则

DEFAULT_CONFIG = _SCRIPTS_DIR / 'local-entry.json'
DEFAULT_CN_CONFIG = _SCRIPTS_DIR / 'local-entry-cn.json'
# 运行时名称（报告/池/预检共用口径）：国际 Node-bundle = 'qoder'；本机原生 CN = 'qodercn'。
RUNTIME_INTERNATIONAL = 'qoder'
RUNTIME_CN = 'qodercn'
# CN 池友好名（BW-POOL-SPLIT-20261010-S2）：正常池新组合是 **内置** Qwen3.8-Max /
# Qwen3.8-Flash，官方 CLI `--list-models` 显示字面串与 SDK 直接下发的 `--model` 值一致，
# 是内置模型而非自定义 Token Plan。为兼容既有引用名 CN_MODEL 指向主力组合 Qwen3.8-Max；
# 自定义 DeepSeek-Flash 从正常池 retired（不再 AUTO/新点单派工），但保留客户端本机自定义
# model_ids 映射，用于人工/显式已授权的历史任务，绝不删除配置。
CN_MODEL = 'Qwen3.8-Max'
# CN 内置官方 Qwen 白名单：argv `--model` 直接使用这两个字面串（不查 model_ids、绝不
# 猜 UUID、也不把自定义名当成内置）。任何其它 CN 名字必须走 config.model_ids 映射，映射
# 缺失即拒绝启动（零 Popen）。
CN_BUILTIN_MODELS = ('Qwen3.8-Max', 'Qwen3.8-Flash')
# argparse 缺省模型 = 并发容量池指定的 qoder 主力组合 Qwen3.8-Max（每会话持久 1:1 两名）。
# 不传 --model 的自主派工即请求主力 Max；Qwen3.8-Flash 在池里是溢出组合，仅当主力满员时
# 由容量池按溢出策略改道。控制面回归 tests/test_execution_control.py 的 fixture 已同步为
# Max（该文件在本轮编辑白名单内），dispatch-plan 的 model 逐项比对与缺省一致。
DEFAULT_MODEL = 'Qwen3.8-Max'
# 契约单点定义在 prompt_contract；此处再导出同名常量，保持既有引用（analyze_report、
# codebuddy/zcode 复用本模块的分析器）与 test_parallel_execution 的 qd.SECTION_HEADERS 不变。
REPORT_START = pc.REPORT_START
REPORT_END = pc.REPORT_END
STAGE_FIELD_NAMES = pc.STAGE_FIELD_NAMES
PATH_FIELD_NAMES = pc.PATH_FIELD_NAMES
COLONS = ('：', ':')  # U+FF1A full-width and ASCII colon
SECTION_HEADERS = pc.SECTION_HEADERS


def load_entry_config(path=None) -> dict:
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    if not cfg_path.is_absolute():
        raise ValueError(f'entry config path must be absolute: {cfg_path}')
    if not cfg_path.is_file():
        raise FileNotFoundError(f'entry config file not found: {cfg_path}')
    cfg_path = cfg_path.resolve()
    cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
    if not isinstance(cfg, dict):
        raise ValueError(f'entry config must be a JSON object: {cfg_path}')
    for key in ('node', 'qodercli'):
        if key not in cfg:
            raise KeyError(f'entry config missing key {key!r}: {cfg_path}')
        p = Path(cfg[key])
        if not p.is_absolute() or not p.is_file():
            raise FileNotFoundError(f'entry config {key!r} must be an existing absolute file: {p}')
    return cfg


def load_cn_config(path=None) -> dict:
    """Qoder CN 原生 EXE 入口配置（与国际 Node-bundle 配置并存、互不覆盖）。要求：
    - `cli`：qodercn 原生可执行文件的绝对且存在路径；
    - `config_dir`：绝对且存在的配置目录（显式 --config-dir 可覆盖）；
    - `model_ids`：友好名→真实模型 ID 的映射（缺映射即拒绝启动，绝不回退同名的
      内建/Qwen 模型，也不把友好名当 ID）。
    配置只留占位示例、绝不含真实登录态或凭据。"""
    cfg_path = Path(path) if path else DEFAULT_CN_CONFIG
    if not cfg_path.is_absolute():
        raise ValueError(f'CN entry config path must be absolute: {cfg_path}')
    if not cfg_path.is_file():
        raise FileNotFoundError(f'CN entry config file not found: {cfg_path}')
    cfg_path = cfg_path.resolve()
    cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
    if not isinstance(cfg, dict):
        raise ValueError(f'CN entry config must be a JSON object: {cfg_path}')
    for key in ('cli', 'config_dir'):
        if key not in cfg:
            raise KeyError(f'CN entry config missing key {key!r}: {cfg_path}')
        p = Path(cfg[key])
        if not p.is_absolute():
            raise ValueError(f'CN entry config {key!r} must be an absolute path: {p}')
    if not Path(cfg['cli']).is_file():
        raise FileNotFoundError(f'CN entry config cli must be an existing file: {cfg["cli"]}')
    if not Path(cfg['config_dir']).is_dir():
        raise FileNotFoundError(f'CN entry config config_dir must be an existing dir: '
                                f'{cfg["config_dir"]}')
    model_ids = cfg.get('model_ids')
    if not isinstance(model_ids, dict) or not model_ids:
        raise ValueError(f'CN entry config model_ids must be a non-empty object: {cfg_path}')
    for name, mid in model_ids.items():
        if not isinstance(name, str) or not isinstance(mid, str) or not name.strip() \
                or not mid.strip():
            raise ValueError(f'CN entry config model_ids entries must be non-empty '
                             f'strings: {name!r}->{mid!r}')
    return cfg


def resolve_cn_model_id(cfg: dict, model: str):
    """返回要下发给原生 CN EXE 的 `--model` 值；两种合法来源，任一不满足返回 None（调用
    方拒绝启动、零 Popen）：
    - **内置官方 Qwen 白名单**（`CN_BUILTIN_MODELS`：Qwen3.8-Max / Qwen3.8-Flash）：
      字面串直接作为 argv 值（官方 CLI `--list-models` 精确显示该名，SDK 直传合法），
      不查 model_ids、不猜 UUID、不把内置名当成自定义；
    - **自定义 Token Plan**：必须走 config.model_ids 映射到真实服务端 ID；缺映射返回
      None（绝不猜同名的内建/Qwen 模型、也绝不把友好名当 ID 直发）。
    任何其它值一律 None。"""
    if isinstance(model, str) and model in CN_BUILTIN_MODELS:
        return model
    return cfg['model_ids'].get(model)


def _validate_rule_list(value, *, field: str):
    """严格校验可选规则/目录列表：None 直通；否则必须为 list/tuple，元素必须是非空
    非空白字符串，且原样保留（不按逗号拆分、不做去空白）。非法项一律拒绝，不静默
    过滤，因为静默过滤会让"看起来传了权限"实际变成"什么权限都没给"。"""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError(f'{field} entries must be strings, got '
                                 f'{type(item).__name__}: {item!r}')
            if item == '' or item.isspace():
                raise ValueError(f'{field} entries must be non-empty and not '
                                 f'whitespace-only: {item!r}')
            out.append(item)
        return out
    raise ValueError(f'{field} must be a list of strings or None, got '
                     f'{type(value).__name__}')


def _expand_tools_to_rules(tools) -> list[str]:
    """兼容回退：把旧 --tools 逗号列表逐项转成 --allowed-tools 规则；空 tools 表示
    零授权。这里只处理"工具名列表"的可见性参数，不代表细粒度权限规则；因此每个
    分段必须非空且自身没有前后空白，避免静默丢规则或产生歧义条目。"""
    if tools is None:
        return []
    if not isinstance(tools, str):
        raise ValueError(f'tools must be a string, got {type(tools).__name__}')
    if tools == '':
        return []
    out = []
    for piece in tools.split(','):
        if piece == '' or piece.isspace():
            raise ValueError(f'legacy tools contains empty/whitespace item: {tools!r}')
        if piece != piece.strip():
            raise ValueError(f'legacy tools item has surrounding whitespace: {tools!r}')
        out.append(piece)
    return out


def effective_permission_rules(tools=None, allowed_tools=None,
                               disallowed_tools=None, add_dirs=None) -> dict:
    """计算实际下发给 Qoder CLI 的权限规则与目录列表。

    - --tools 只影响可见性；未显式给出 allowed_tools 时才按 tools 逗号逐项回退；
    - 显式 allowed_tools（包括空列表）完全替代回退，不追加 Write/Edit/Bash 等默认；
    - 规则、目录按原样保留：不按逗号拆分，不做去空白，非字符串/空/仅空白项一律拒绝；
    - 无 shell 拼接：调用方拿到的是最终 argv 的逐元素值。"""
    if allowed_tools is None:
        allowed = _expand_tools_to_rules(tools)
    else:
        allowed = _validate_rule_list(allowed_tools, field='allowed_tools') or []
    disallowed = _validate_rule_list(disallowed_tools, field='disallowed_tools') or []
    dirs = _validate_rule_list(add_dirs, field='add_dirs') or []
    return {'allowed_tools': allowed,
            'disallowed_tools': disallowed,
            'add_dirs': dirs}


def build_argv(cfg: dict, workspace: str, model: str, tools: str,
               session_id: str | None, *,
               allowed_tools=None, disallowed_tools=None,
               add_dirs=None) -> list[str]:
    """与 QODER-DIRECT-01 已验证调用形状一致的安全参数数组（无 shell）。

    --tools 是可见性参数；--allowed-tools/--disallowed-tools/--add-dir 在官方 CLI
    是可重复单值参数，一条规则/目录一项，不按逗号拆分。未显式提供 allowed_tools
    时才按 tools 逗号列表逐项回退为 --allowed-tools；显式列表（含空列表）完全替代
    回退，不会偷偷附加 Write/Edit/Bash 默认权限。permission-mode 固定 dont_ask，
    永不启用 bypass_permissions。"""
    perms = effective_permission_rules(tools, allowed_tools,
                                        disallowed_tools, add_dirs)
    argv = [cfg['node'], cfg['qodercli'],
            '--cwd', workspace, '--model', model,
            '--tools', tools, '--permission-mode', 'dont_ask',
            '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
            '--output-format', 'json', '-p']
    for rule in perms['allowed_tools']:
        argv += ['--allowed-tools', rule]
    for rule in perms['disallowed_tools']:
        argv += ['--disallowed-tools', rule]
    for d in perms['add_dirs']:
        argv += ['--add-dir', d]
    if session_id:
        argv += ['--resume', session_id]
    return argv


# 官方 Qoder CN 权限口径（https://docs.qoder.cn/cli/permissions）：path-scoped 写入规则只有
# Edit(...)（它同时覆盖 Edit/Write/NotebookEdit 三个工具）；`--tools` 里的 Write 是**工具可见性**，
# 与权限规则不是同一层。真实失败证据：CN 入口曾把 `Write(/references/project-closeout.md)` 当
# allow 规则下发，dont_ask 下被 CLI 拒绝，而同一文件范围的 `Edit(/SKILL.md)` 成功。
_PATH_SCOPED_WRITE = re.compile(r'^Write\((.*)\)$', re.DOTALL)


def path_scoped_write_rules(rules) -> list[str]:
    """挑出 CN 无法识别的**路径限定** Write 规则：`Write(<范围>)`，其中范围不是 `*`。
    裸 `Write` 与 `Write(*)` 是工具级规则，按官方语义原样保留，不在此拒绝。"""
    hits = []
    for rule in rules or ():
        if not isinstance(rule, str):
            continue
        m = _PATH_SCOPED_WRITE.match(rule.strip())
        if m and m.group(1) != '*':
            hits.append(rule)
    return hits


def validate_cn_permission_rules(allowed_tools, disallowed_tools) -> None:
    """CN 构造 argv 前的权限规则门：allow 与 deny **同样**检查（路径限定的 deny 若被静默接受，
    调用方会误以为已经禁写）。只拒绝、不翻译——绝不把 `Write(<范围>)` 自动改成 `Edit(<范围>)`
    或任何更宽的全工具授权，改动范围必须由调用方显式重新点单。"""
    bad = (path_scoped_write_rules(allowed_tools)
           + path_scoped_write_rules(disallowed_tools))
    if not bad:
        return
    raise ValueError(
        'Qoder CN does not accept path-scoped Write(...) permission rules: '
        + ', '.join(repr(r) for r in bad)
        + '. For the same file scope use Edit(...) — per the Qoder CN permissions doc the '
        'path-scoped write rule is Edit(...), which already covers Edit/Write/NotebookEdit; '
        'keep tool-level bare Write or Write(*) as-is, and keep Write in --tools for tool '
        'visibility. Refusing to auto-widen or auto-translate this rule.')


# BW-CN-PERMISSION-STOP-20261015T155930Z-S1：CN dont_ask 派工里禁止的 mutation 工具名。
# 原始故障：一次 CN 派工只带裸工具名 allow 规则（plan.grants edits/bash 为空），Write 新建
# 嵌套目标被 dont_ask 拒绝后，模型改用 Bash touch/ls/rm 与重定向反复试写（再次被拒），主脑
# 1 分 55 秒后人工终止。根因不能简化成"所有 CN 版本 bare 规则必然无效"（官方当前文档说路径
# 写入用 Edit(...)，同时覆盖 Edit/Write/NotebookEdit）；本门只拒绝危险形状并要求调用方显式
# 点单具体范围，不自动改写/扩大任何规则。
_CN_MUTATION_TOOL_NAMES = ('Edit', 'Write', 'NotebookEdit', 'Bash')


def cn_bare_mutation_allow_rules(allowed_tools) -> list[str]:
    """挑出 CN 派工不允许的 mutation allow 规则形状：裸工具名或整工具通配
    `Tool(*)` 的 Edit/Write/NotebookEdit/Bash（含 --tools 兼容回退生成的同名规则）。
    只看 allow 侧；deny 是收紧不是授权，原样保留。"""
    hits = []
    for rule in allowed_tools or ():
        if not isinstance(rule, str):
            continue
        core = rule.strip()
        for tool in _CN_MUTATION_TOOL_NAMES:
            if core == tool or core == tool + '(*)':
                hits.append(rule)
                break
    return hits


def validate_cn_allow_rule_shapes(allowed_tools) -> None:
    """CN 正式派工的 mutation 规则形状门（在容量 claim、建证据目录、Popen 之前）：
    拒绝裸/整工具通配的 Edit/Write/NotebookEdit/Bash allow 规则，明确要求
    `Edit(/<工作区相对具体文件>)` 与 `Bash(<登记原样命令>)`。不自动改写或扩大规则；
    只读调用（Read/Glob/Grep 裸规则、显式空 allow、deny、Write 仅在 --tools 可见性里）
    一律不受影响。静态门只证明规则形状，不声称真实权限成功。"""
    bad = cn_bare_mutation_allow_rules(allowed_tools)
    if not bad:
        return
    raise ValueError(
        'Qoder CN dispatch refuses bare or whole-tool-wildcard mutation allow rules: '
        + ', '.join(repr(r) for r in bad)
        + '. Under --permission-mode dont_ask these shapes cannot be approved mid-run; '
        'the observed real failure mode is the model retrying denied writes via Bash '
        'touch/ls/rm and redirection. Require explicit scoped rules instead: '
        'Edit(/<workspace-relative specific file>) for file writes and '
        'Bash(<registered verbatim command>) for shell commands; keep Write in --tools '
        'for tool visibility only. Read/Glob/Grep bare rules, deny rules and an '
        'explicitly empty allow list stay legal; rules are never auto-rewritten or '
        'widened here. This static gate only proves rule shape — it does not claim the '
        'real CN permission matcher will accept any rule.')


def build_cn_argv(cfg: dict, config_dir: str, model_id: str, workspace: str, tools: str,
                  session_id: str | None, *,
                  allowed_tools=None, disallowed_tools=None,
                  add_dirs=None, session_uuid=None) -> list[str]:
    """Qoder CN 原生 EXE 调用的安全参数数组（无 shell），与国际 build_argv 严格区分：
    - 首元素是原生 `cli`（非 node + qodercli.js）；
    - `--config-dir` 承载本机登录态所在配置目录（显式传入、不读默认用户目录）；
    - `--model` 下发的是 config.model_ids 映射出的**真实模型 ID**（非友好名、非 Qwen）。
    权限/工具可见性/续接口径与国际一致，permission-mode 固定 dont_ask；CN 在构造 argv
    **之前**运行两个规则门：拒绝路径限定 Write(...) allow/deny 规则
    （validate_cn_permission_rules），并拒绝裸/整工具通配 mutation allow 规则
    （validate_cn_allow_rule_shapes，BW-CN-PERMISSION-STOP-S1）。session_id 走官方
    `--resume`；新会话可经 session_uuid 传官方 `--session-id <UUID>`（主脑已核对官方
    help 支持该参数；本阶段未联网复验），供会话权限拒绝监视精确定位本会话 jsonl。"""
    perms = effective_permission_rules(tools, allowed_tools,
                                        disallowed_tools, add_dirs)
    validate_cn_permission_rules(perms['allowed_tools'], perms['disallowed_tools'])
    validate_cn_allow_rule_shapes(perms['allowed_tools'])
    argv = [cfg['cli'], '--config-dir', config_dir,
            '--cwd', workspace, '--model', model_id,
            '--tools', tools, '--permission-mode', 'dont_ask',
            '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
            '--output-format', 'json', '-p']
    for rule in perms['allowed_tools']:
        argv += ['--allowed-tools', rule]
    for rule in perms['disallowed_tools']:
        argv += ['--disallowed-tools', rule]
    for d in perms['add_dirs']:
        argv += ['--add-dir', d]
    if session_id:
        argv += ['--resume', session_id]
    elif session_uuid:
        argv += ['--session-id', session_uuid]
    return argv


def _norm_path(p: str) -> str:
    """真实路径 + 平台大小写语义归一（与 qoder_permission_watch._norm_path 同口径）。

    先解析真实路径（Windows 8.3 短名/符号链接等同指针路径归一一致），再按平台大小写语义
    规范（os.path.normcase）。绝不为了对齐测试而删除工作区/cwd 身份核验。"""
    try:
        raw = str(p).strip().strip('"')
        return os.path.normcase(os.path.realpath(raw)).rstrip('\\/')
    except Exception:
        return str(p).strip().lower()


def _extract_field(body: str, names) -> tuple[str | None, str | None]:
    """严格字段抽取：仅接受明确字段名后紧跟中/英文冒号的行。

    - 同前缀但非本字段（如“阶段编号XYZ：”）不匹配，视为该字段缺失；
    - 出现多行匹配（重复/歧义）返回错误，不做宽松的第一匹配处理；
    返回 (值, 错误)；错误为 None 表示抽取成功且无歧义（值可能为空串）。
    """
    hits = []
    for raw in body.splitlines():
        core = raw.strip()
        if core.startswith('-'):
            core = core.lstrip('-').strip()
        for name in names:
            if core.startswith(name) and core[len(name):len(name) + 1] in COLONS:
                hits.append((name, core[len(name) + 1:].strip()))
                break
    if not hits:
        return None, None
    if len(hits) > 1:
        return None, f'ambiguous duplicate field lines: {hits!r}'
    return hits[0][1], None


def analyze_report(text: str, stage: str | None, project_root: str) -> dict:
    """对报告正文做机械格式核对，仅得出 body_ok（格式合格与否）；任何一项不符即
    body_ok=false 并保留原因，不修改原文、不替执行器补写或删除前言。最终是否绑定由
    finalize_binding 综合传输层事实后决定，本函数不单独宣称完成绑定。"""
    lines = text.splitlines(keepends=True)
    lines_ns = text.splitlines()
    start_lines, end_lines = [], []
    for i, line in enumerate(lines):
        core = line[:-1] if line.endswith('\n') else line
        if core.endswith('\r'):
            core = core[:-1]
        if core == REPORT_START:
            start_lines.append(i)
        elif core == REPORT_END:
            end_lines.append(i)
    reasons = []
    markers_ok = True
    if len(start_lines) != 1 or len(end_lines) != 1:
        markers_ok = False
        reasons.append(f'boundary markers not exactly one pair '
                       f'(start={len(start_lines)}, end={len(end_lines)})')
    elif start_lines[0] >= end_lines[0]:
        markers_ok = False
        reasons.append('WORKER_REPORT_START does not precede WORKER_REPORT_END')
    if text.count(REPORT_START) != len(start_lines) \
            or text.count(REPORT_END) != len(end_lines):
        markers_ok = False
        reasons.append('marker substring appears on non-exclusive lines')
    if lines_ns and lines_ns[0].rstrip('\r') != REPORT_START:
        markers_ok = False
        reasons.append('first line is not exclusively WORKER_REPORT_START '
                       '(preface or text outside markers)')
    if lines_ns and lines_ns[-1].rstrip('\r') != REPORT_END:
        markers_ok = False
        reasons.append('last line is not exclusively WORKER_REPORT_END '
                       '(trailing text outside markers)')
    body = ''
    if markers_ok:
        body = ''.join(lines[start_lines[0]:end_lines[0] + 1])
        if body.splitlines()[0] != REPORT_START \
                or body.splitlines()[-1].rstrip('\r') != REPORT_END:
            markers_ok = False
            reasons.append('boundary markers not exclusive lines')

    stage_val, stage_err = _extract_field(body, STAGE_FIELD_NAMES)
    stage_token_match = None
    if stage_err:
        stage_token_match = False
        reasons.append(stage_err)
    if not stage:
        stage_token_match = False
        reasons.append('no expected --stage provided; binding cannot be confirmed')
    elif stage_val is None:
        stage_token_match = False
        reasons.append('report has no 阶段编号/阶段编号与执行方式 field line')
    else:
        token_re = re.compile(
            r'(?<![A-Za-z0-9._-])' + re.escape(stage) + r'(?![A-Za-z0-9._-])')
        stage_token_match = bool(token_re.search(stage_val))
        if not stage_token_match:
            reasons.append(f'阶段编号 field value does not carry stage {stage!r} '
                           f'as an exact token')

    path_val, path_err = _extract_field(body, PATH_FIELD_NAMES)
    path_match = path_val is not None and _norm_path(path_val) == _norm_path(project_root)
    if path_err:
        reasons.append(path_err)
    if path_val is None:
        reasons.append('report has no 实际项目绝对路径 field line')
    elif not path_match:
        reasons.append(f'实际项目绝对路径 {path_val!r} does not equal workspace '
                       f'{project_root!r}')

    positions = []
    for h in SECTION_HEADERS:
        pos = None
        for i, line in enumerate(body.splitlines()):
            if line.strip().startswith(h):
                pos = i
                break
        positions.append(pos)
    missing = [h for h, pos in zip(SECTION_HEADERS, positions) if pos is None]
    found = [pos for pos in positions if pos is not None]
    sections_in_order = bool(found) and found == sorted(found)
    if missing:
        reasons.append(f'missing report sections: {missing}')
    if not sections_in_order and not missing:
        reasons.append('report sections out of order')
    closing_line = any(line.strip() == pc.CLOSING_LINE
                       for line in body.splitlines())
    if not closing_line:
        reasons.append('report lacks the required closing line')

    body_ok = (markers_ok and bool(stage) and stage_token_match is True
               and path_match and not missing and sections_in_order and closing_line)
    return {'note': 'transport-layer body-format check only; body_ok is the '
                    'mechanical text gate, NOT the final binding. The final '
                    'binding is produced by finalize_binding, which additionally '
                    'requires protocol success, a non-empty session_id, exact '
                    'resume match and a matching response readback hash.',
            'body_ok': body_ok,
            'bound': body_ok,
            'reasons': reasons,
            'markers': {'ok': markers_ok,
                        'start_exclusive_lines': len(start_lines),
                        'end_exclusive_lines': len(end_lines),
                        'start_substring_count': text.count(REPORT_START),
                        'end_substring_count': text.count(REPORT_END)},
            'stage_field': stage_val,
            'stage_checked': bool(stage),
            'stage_token_match': stage_token_match,
            'path_field': path_val,
            'path_match': path_match,
            'sections_found': len(SECTION_HEADERS) - len(missing),
            'sections_missing': missing,
            'sections_in_order': sections_in_order,
            'closing_line_present': closing_line}


def finalize_binding(body_ok: bool, body_reasons, *, protocol_success: bool,
                     session_id, requested_session_id, readback_match: bool) -> dict:
    """最终绑定汇总（纯函数，可离线注入）：在正文格式之外，综合协议终态、
    非空 session_id、resume 精确一致与响应哈希回读。任一不满足即 bound=false，
    且不改动正文。"""
    reasons = list(body_reasons)
    bound = bool(body_ok) and bool(protocol_success) and bool(readback_match)
    if not protocol_success:
        reasons.append('protocol not successful (protocol_success=false)')
    if not (isinstance(session_id, str) and session_id.strip()):
        bound = False
        reasons.append('envelope lacks a non-empty session_id')
    if requested_session_id is not None and session_id != requested_session_id:
        bound = False
        reasons.append(f'resume requested session {requested_session_id!r} but '
                       f'actual session_id {session_id!r} is not an exact match')
    if not readback_match:
        bound = False
        reasons.append('response readback hash does not match in-memory response')
    return {'bound': bound,
            'body_ok': bool(body_ok),
            'protocol_success': bool(protocol_success),
            'session_id': session_id,
            'requested_session_id': requested_session_id,
            'readback_match': bool(readback_match),
            'reasons': reasons}


def _qoder_failure_facts(summary: dict) -> dict:
    """把 Qoder 终态结构映射为 failure_types 事实；只读协议/解析/拒绝/错误结构，
    不扫描报告正文，不因报告格式失败算代码失败。真实结构化错误里有明确 429 时提取
    quota_429；已知 quota/permission/parse 来源不泛化为模型差，model_execution_failure
    只用于剩余未分型的执行失败。"""
    parse_failure = bool(summary.get('parse_error'))
    perms = summary.get('permission_denials')
    permission_denied = isinstance(perms, (list, dict)) and bool(perms)
    quota_429 = ec.explicit_429(summary.get('result_errors'),
                                summary.get('result_errors_info'))
    # BW-INTERRUPTION-20261015-S2：仅当真实结构化失败错误载体里出现明确认证失败文案
    # （Custom model authentication failed.，忽略大小写与首尾空白）才识别
    # authentication_failure；仍以 protocol_success=false 的失败为前提，不扫描
    # prompt/response/正常结果文本或整份 summary。quota/permission/parse 分类
    # 保持原语义可并存，认证失败不再归入 model_execution_failure。
    auth_failure = (not summary.get('protocol_success')
                    and not parse_failure
                    and ec.explicit_authentication_failure(
                        summary.get('result_errors'),
                        summary.get('result_errors_info')))
    model_execution = (not summary.get('protocol_success')
                       and not parse_failure and not permission_denied
                       and not quota_429 and not auth_failure)
    return {'quota_429': quota_429,
            'protocol_parse_failure': parse_failure,
            'permission_rule_denied': permission_denied,
            'model_execution_failure': bool(model_execution),
            'authentication_failure': bool(auth_failure),
            'reset_hint': ec.extract_reset_hint(summary.get('result_errors'),
                                                summary.get('result_errors_info')),
            'evidence': {'stop_reason': summary.get('stop_reason'),
                         'permission_denials': perms,
                         'parse_error': summary.get('parse_error'),
                         'result_errors': summary.get('result_errors'),
                         'result_errors_info': summary.get('result_errors_info'),
                         'model_requested': summary.get('model_requested')}}


# BW-POOL-SPLIT-20261010-S3：三个 Qoder Max 主力“明确限额”最小标记的判定与落标。
# 只有真实终态的**结构化失败载体**（result_errors / errors_info）明确命中硬限额才落标；
# 绝不扫描报告正文，绝不把 permission 拒绝 / 解析失败 / 取消退出码(4294967295) / 普通报告
# 正文里的 429 / 单独 quota 标签误作限额。两个内置 Max（qoder/qodercn:Qwen3.8-Max）与 CN
# 自定义 Max（qodercn:Qwen-3.8-Max）各自独立落标、互不连坐；Flash、ZCode、国际自定义名
# 永不落标（不误挡 Flash、不动 ZCode 既有 availability 口径、不把自定义映射到内置池）。
_QUOTA_HARD_LIMIT_MARKERS = (
    'credits exhausted', 'credits have been exhausted', 'out of credits',
    'credit exhausted', 'credits depleted',
    'insufficient credits', 'insufficient balance', 'no credits left',
    'quota exhausted', 'credit usage limit',
    '积分用尽', '积分不足', '余额不足', '额度用尽', '额度不足',
)


def _qoder_max_pool_key(runtime, model):
    """只有三个 Qoder Max 主力池可承载明确限额标记：两个内置 Max（qoder/qodercn:Qwen3.8-
    Max）与 CN 自定义 Max（qodercn:Qwen-3.8-Max，短横线友好名）。其它 runtime/model（含任一
    Flash、ZCode、国际自定义名）一律 None。绝不偷偷给 Flash/ZCode 落标；自定义名只有 CN
    runtime 才成池，绝不把自定义误映射到内置池或反之。"""
    if model == 'Qwen3.8-Max':
        if runtime == RUNTIME_INTERNATIONAL:
            return 'qoder:Qwen3.8-Max'
        if runtime == RUNTIME_CN:
            return 'qodercn:Qwen3.8-Max'
        return None
    if model == 'Qwen-3.8-Max' and runtime == RUNTIME_CN:
        # CN 自定义友好名（短横线）→ CN 自定义 Max 池；国际侧无此池，绝不误映射。
        return 'qodercn:Qwen-3.8-Max'
    return None


def _error_carrier_texts(errors, errors_info):
    """只遍历结构化错误载体里的文本字段（绝不扫描报告正文/response.md）。"""
    for container in (errors, errors_info):
        if isinstance(container, list):
            for item in container:
                if isinstance(item, str):
                    yield item
                elif isinstance(item, dict):
                    for key in ('details', 'message', 'reason', 'error', 'title'):
                        val = item.get(key)
                        if isinstance(val, str):
                            yield val


def _hard_quota_limit_text(errors, errors_info):
    for text in _error_carrier_texts(errors, errors_info):
        low = text.lower()
        for marker in _QUOTA_HARD_LIMIT_MARKERS:
            if marker in low or marker in text:
                return text
    return None


def _explicit_quota_limit_hit(summary):
    """判定真实终态失败载体是否明确命中“硬限额”。优先复用既有
    execution_control.explicit_429（result_errors/errors_info 里结构化 status/code==429
    或文本开头独立 429）；必要时识别明确的 credits exhausted / out of credits / 积分用尽
    等硬限额文本。返回 (hit: bool, evidence: str|None)。只在失败终态有意义（调用方另行
    以 protocol_success 把关），绝不由 exit code / 报告正文 / permission 拒绝推断。"""
    errors = summary.get('result_errors')
    errors_info = summary.get('result_errors_info')
    if ec.explicit_429(errors, errors_info):
        return True, 'explicit_429'
    hard = _hard_quota_limit_text(errors, errors_info)
    if hard is not None:
        return True, hard
    return False, None


def _minimal_permission_stop(watch_outcome: dict) -> dict:
    """从监视结果提取最小拒绝事实（不含私人会话正文）。"""
    denial = watch_outcome.get('denial') or {}
    return {'terminated_by_watch': bool(watch_outcome.get('terminated_by_watch')),
            'observed_after_exit': bool(watch_outcome.get('observed_after_exit')),
            'exit_confirmed': bool(watch_outcome.get('exit_confirmed')),
            'kind': denial.get('kind'),
            'tool_use_id': denial.get('tool_use_id'),
            'observed_at_utc': denial.get('observed_at_utc')}


def _save_permission_stop_evidence(out: Path, watch_outcome: dict, watcher) -> str:
    """保存最小原始拒绝事件/来源/工具ID/观察时间与监视器就绪/错误/停止事实；原始会话
    文件保留不改，绝不整份拷贝私人流。"""
    payload = {'saved_at_utc': datetime.now(timezone.utc).isoformat(),
               'stop': _minimal_permission_stop(watch_outcome),
               'stop_signal': watch_outcome.get('stop_signal'),
               'stop_signal_error': watch_outcome.get('stop_signal_error'),
               'communicate_error': watch_outcome.get('communicate_error'),
               'exit_wait_error': watch_outcome.get('exit_wait_error'),
               'denial': watch_outcome.get('denial'),
               'monitor_facts': watcher.facts(),
               'note': 'minimal raw denial event only (matched tool_result block + '
                       'identifying header); the native session jsonl file itself is '
                       'kept unmodified and is never copied in full'}
    path = out / 'permission-stop.json'
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return str(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workspace', required=True)
    ap.add_argument('--prompt-file', required=True)
    ap.add_argument('--output-dir', required=True)
    ap.add_argument('--stage', required=True,
                    help='Required non-empty stage id that the report 阶段编号 field '
                         'must carry as an exact token; the call is refused if empty.')
    ap.add_argument('--model', default=DEFAULT_MODEL,
                    help=f'Qoder model to request; defaults to {DEFAULT_MODEL}. An '
                         'explicit alternate model may be passed, but the default never '
                         'falls back to a GLM model.')
    ap.add_argument('--tools', default='', help='Tool visibility list (comma separated). '
                    'Affects only what Qoder exposes; per-rule authorization is separate. '
                    'Empty disables tools visibility.')
    ap.add_argument('--allowed-tools', dest='allowed_tools', action='append', default=None,
                    help='Repeatable fine-grained allow rule (e.g. Edit(/scripts/qoder_direct.py)). '
                         'Each occurrence carries one verbatim rule; rules are never split on commas. '
                         'Providing any --allowed-tools fully replaces the legacy --tools fallback; '
                         'omitting it falls back to per-item --allowed-tools derived from --tools.')
    ap.add_argument('--disallowed-tools', dest='disallowed_tools', action='append', default=None,
                    help='Repeatable deny rule; each occurrence carries one verbatim rule.')
    ap.add_argument('--add-dir', dest='add_dirs', action='append', default=None,
                    help='Repeatable additional readable directory. Declaring a directory does '
                         'not grant edit permission; it only extends file visibility outside '
                         'the workspace root.')
    ap.add_argument('--task-scope', dest='task_scope', default=None,
                    help='JSON file with edits/bash/read_dirs. When supplied, the entry '
                         'generates the exact Qoder tools and allow rules before Popen; '
                         'manual --tools/--allowed-tools/--disallowed-tools/--add-dir '
                         'may not be mixed with it.')
    ap.add_argument('--resume-session-id', '--session-id', dest='session_id', default=None,
                    help='Resume a known Qoder session using the official --resume flag.')
    ap.add_argument('--config', default=str(DEFAULT_CONFIG),
                    help='Local entry config JSON with absolute node/qodercli paths '
                         '(international runtime=qoder).')
    ap.add_argument('--runtime', default=RUNTIME_INTERNATIONAL,
                    choices=[RUNTIME_INTERNATIONAL, RUNTIME_CN],
                    help='Executor runtime. "qoder" = existing international Node-bundle '
                         'config (unchanged). "qodercn" = local Qoder CN native EXE via a '
                         'separate CN config (cli/config_dir/model_ids).')
    ap.add_argument('--cn-config', default=str(DEFAULT_CN_CONFIG),
                    help='Qoder CN native entry config JSON (runtime=qodercn only).')
    ap.add_argument('--config-dir', dest='config_dir', default=None,
                    help='Explicit Qoder CN config dir override (runtime=qodercn only); '
                         'otherwise the CN config config_dir is used.')
    ap.add_argument('--resume-source', dest='resume_source', default=None,
                    help='Path to a prior CN request.json that verifiably established '
                         'the CN session (runtime=qodercn only). CN continuation is '
                         'refused without this verifiable CN source.')
    ap.add_argument('--dispatch-plan', dest='dispatch_plan', default=None,
                    help='Optional task-level dispatch plan JSON. New parallel dispatch '
                         'must supply it; the legacy path (no plan) stays compatible. '
                         'When present, the argv/grants/cwd built here are exactly '
                         'compared to the plan before any Popen; a rejected preflight '
                         'exits 2 with the evidence directory never created.')
    ap.add_argument('--active-tasks', dest='active_tasks', default=None,
                    help='Optional JSON file of the active-task snapshot (list of '
                         '{task_id,state,...}). When given it overrides the plan\'s '
                         'own active_tasks; otherwise the plan snapshot is used, so a '
                         'parallel plan that omits it is refused rather than treated as '
                         '"nothing in flight".')
    ap.add_argument('--task-id', dest='task_id', default=None,
                    help='Stable task identity for cross-chat capacity dedup. Defaults '
                         'to the dispatch-plan task_id when a plan is given, else --stage.')
    ap.add_argument('--dispatch-store', dest='dispatch_store', default=None,
                    help='Explicit dispatch-pool sqlite path. Defaults to the stable '
                         'shared pool (env BRAIN_WORKER_DISPATCH_STORE or '
                         '~/.brain-worker/dispatch-pool.sqlite3). Tests must pass a '
                         'temporary store.')
    ap.add_argument('--dispatch-claim', dest='dispatch_claim', default=None,
                    help='A capacity claim token pre-reserved via dispatch_pool. When '
                         'given it is validated against task/runtime/model/workspace/'
                         'prompt and consumed before Popen; any drift is refused. When '
                         'omitted the entry performs an atomic route selection and, if '
                         'this entry is not the selected combo, returns routing_required '
                         'with sent=false instead of submitting the wrong model.')
    ap.add_argument('--chat-id', dest='chat_id', default=None,
                    help='Chat/conversation identity for capacity dedup and claim scope. '
                         'When a pre-reserved claim was reserved with an explicit chat_id '
                         'in its scope, the consuming entry MUST pass the same --chat-id '
                         'or the claim is refused as drift (zero Popen). Never fabricated: '
                         'omit it when the host has no verified chat context, in which case '
                         'the auto path records chat_id=None rather than inventing one.')
    ap.add_argument('--quota-store', dest='quota_store', default=None,
                    help='Persistent quota/availability store. The Qoder entry reads the '
                         'same persistent ZCode availability state so an explicit Qoder '
                         'start is never 1:1 rerouted back to an unavailable ZCode. When '
                         'omitted it resolves through the formal default source in order: '
                         'explicit --quota-store -> env BRAIN_WORKER_QUOTA_STORE -> the '
                         'trusted default persistent file '
                         '(~/.brain-worker/quota-state.sqlite3), so the default path also '
                         'filters an unavailable ZCode. Read-only only: it never creates a '
                         'db/table, never writes the quota store, and a read error fails '
                         'closed (never claiming recovery/free/balance).')
    ap.add_argument('--quota-routes', dest='quota_routes', default=None,
                    help='Optional quota routes file for ZCode availability resolution.')
    args = ap.parse_args()
    if not args.stage or not args.stage.strip():
        ap.error('--stage is required and must be non-empty')
    # BW-QODERCN-TOKEN-MAX-DISABLE-20261028：用户明确禁用 qodercn:Qwen-3.8-Max 的新派发。
    # 无 --dispatch-claim 的正式 CN 入口请求精确落在该池时，在读取私有 CN 配置/模型映射、
    # 建 output 目录、任何配置副作用与 Popen 之前以 mode_excluded（用户要求禁用）拒绝
    # （sent=false、零占位、零证据目录）。带 --dispatch-claim 的请求不走本早门：禁用前
    # 合法遗留的精确 reserved claim 仍由下方 consume_for_entry 完整校验
    # （claim/config/model/token/workspace/权限/容量/时段），漂移照旧拒绝；其它被排除池
    # 的 consume 语义不变。绝不误伤 qodercn:Qwen3.8-Max（内置名，短横线才是禁用目标）。
    entry_pk = dp.pool_key(args.runtime, args.model)
    if args.runtime == RUNTIME_CN and not args.dispatch_claim \
            and entry_pk in dmode.USER_DISABLED_POOL_KEYS:
        mode = dmode.current_mode()
        print(json.dumps({'cn_user_disabled_pool_rejected': True, 'sent': False,
                          'exit_code': 2, 'runtime': RUNTIME_CN,
                          'model_requested': args.model,
                          'reason': 'mode_excluded', 'mode_excluded': True,
                          'dispatch_mode': mode, 'pool_key': entry_pk,
                          'reasons': [dmode.mode_excluded_reason(entry_pk, mode)]},
                         ensure_ascii=False))
        return 2
    is_cn = args.runtime == RUNTIME_CN
    cn_model_id = None
    cn_config_dir = None
    if is_cn:
        # CN 原生运行时：独立配置、独立 config_dir、友好名→真实 ID 映射；任一不满足都在
        # 读提示词、消费容量、建目录、Popen 之前拒绝（零 Popen、零证据目录）。
        try:
            cfg = load_cn_config(args.cn_config)
        except (ValueError, KeyError, FileNotFoundError, OSError,
                json.JSONDecodeError) as exc:
            print(json.dumps({'cn_entry_rejected': True, 'sent': False, 'exit_code': 2,
                              'runtime': RUNTIME_CN,
                              'reasons': [f'Qoder CN config invalid: {exc!r}']},
                             ensure_ascii=False))
            return 2
        cn_config_dir = args.config_dir or cfg['config_dir']
        cn_model_id = resolve_cn_model_id(cfg, args.model)
        if cn_model_id is None:
            print(json.dumps({'cn_model_mapping_missing': True, 'sent': False,
                              'exit_code': 2, 'runtime': RUNTIME_CN,
                              'model_requested': args.model,
                              'reasons': ['requested CN model has no real model-id '
                                          'mapping; refusing to send the friendly name '
                                          'as an ID or to auto-select a same-named/Qwen '
                                          'builtin — zero Popen']}, ensure_ascii=False))
            return 2
        if args.session_id:
            # CN 接续：至少拒绝把国际原件/session 当作 CN 续用；无来源可证 → 拒绝。绝不新建
            # 会话系统——只有 --resume-source 指向一份 runtime=qodercn 的历史 request 才放行。
            src_ok = False
            src_reason = 'no --resume-source provided to verify a CN origin'
            if args.resume_source:
                try:
                    prior = json.loads(Path(args.resume_source).read_text(encoding='utf-8'))
                    if prior.get('runtime_name') == RUNTIME_CN and prior.get('resume_verifiable'):
                        src_ok = True
                    else:
                        src_reason = (f'resume source {args.resume_source!r} is not a '
                                      f'verifiable Qoder CN original (runtime_name='
                                      f'{prior.get("runtime_name")!r})')
                except (OSError, ValueError) as exc:
                    src_reason = f'resume source unreadable: {exc!r}'
            if not src_ok:
                print(json.dumps({'cn_resume_rejected': True, 'sent': False,
                                  'exit_code': 2, 'runtime': RUNTIME_CN,
                                  'reasons': [src_reason]}, ensure_ascii=False))
                return 2
        # BW-CN-PERMISSION-STOP-S1：会话权限拒绝监视的会话身份。resume 沿用已验证原
        # session（--resume）；新调用生成一个独立 UUID 经官方 --session-id 下发并记录。
        # session id 形状非法（路径逃逸防护）一律拒绝，零 Popen、零证据目录。
        if args.session_id and not qpw.is_safe_session_id(args.session_id):
            print(json.dumps({'cn_resume_rejected': True, 'sent': False,
                              'exit_code': 2, 'runtime': RUNTIME_CN,
                              'reasons': [f'resume session id is not a safe session '
                                          f'identifier: {args.session_id!r}']},
                             ensure_ascii=False))
            return 2
        cn_watch_session_id = args.session_id or str(uuid.uuid4())
        cn_watch_source = 'resume' if args.session_id else 'generated'
    else:
        cfg = load_entry_config(args.config)
    work = Path(args.workspace).resolve(strict=True)
    prompt_path = Path(args.prompt_file).resolve(strict=True)
    prompt_bytes = prompt_path.read_bytes()
    prompt = prompt_bytes.decode('utf-8')
    if not work.is_dir() or not prompt.strip():
        ap.error('Existing workspace and non-empty prompt required')
    out = Path(args.output_dir).resolve()
    task_scope_record = None
    if args.task_scope:
        if (args.tools or args.allowed_tools is not None
                or args.disallowed_tools is not None or args.add_dirs is not None):
            print(json.dumps({'task_scope_rejected': True, 'sent': False,
                              'exit_code': 2,
                              'reasons': ['--task-scope cannot be mixed with manual '
                                          'tool or permission arguments']},
                             ensure_ascii=False))
            return 2
        try:
            scope_path = Path(args.task_scope).resolve(strict=True)
            scope_bytes = scope_path.read_bytes()
            scope_data = json.loads(scope_bytes.decode('utf-8'))
            generated = qp.build_permissions(scope_data)
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            print(json.dumps({'task_scope_rejected': True, 'sent': False,
                              'exit_code': 2, 'reasons': [str(exc)]}, ensure_ascii=False))
            return 2
        args.tools = ','.join(generated['visible_tools'])
        args.allowed_tools = list(generated['allowed_tools'])
        args.disallowed_tools = list(generated['disallowed_tools'])
        args.add_dirs = list(generated['add_dirs'])
        task_scope_record = {
            'path': str(scope_path),
            'sha256': hashlib.sha256(scope_bytes).hexdigest(),
            'generated': generated,
        }
    perms = effective_permission_rules(args.tools, args.allowed_tools,
                                        args.disallowed_tools, args.add_dirs)
    if is_cn:
        # 规则门在容量 claim、建证据目录、Popen 之前：不兼容即受控未派发（sent=false、rc=2、
        # 零 Popen、零输出目录），绝不把崩溃 traceback 留给调用方，也不放宽权限重试。
        # 新会话同时把生成的独立 UUID 经官方 --session-id 下发（供监视器精确定位本会话）。
        try:
            argv = build_cn_argv(cfg, cn_config_dir, cn_model_id, str(work), args.tools,
                                 args.session_id, allowed_tools=perms['allowed_tools'],
                                 disallowed_tools=perms['disallowed_tools'],
                                 add_dirs=perms['add_dirs'],
                                 session_uuid=(None if args.session_id
                                               else cn_watch_session_id))
        except ValueError as exc:
            print(json.dumps({'cn_permission_rule_rejected': True, 'sent': False,
                              'exit_code': 2, 'runtime': RUNTIME_CN,
                              'allowed_tools': list(perms['allowed_tools']),
                              'disallowed_tools': list(perms['disallowed_tools']),
                              'reasons': [str(exc)]}, ensure_ascii=False))
            return 2
    else:
        argv = build_argv(cfg, str(work), args.model, args.tools, args.session_id,
                          allowed_tools=perms['allowed_tools'],
                          disallowed_tools=perms['disallowed_tools'],
                          add_dirs=perms['add_dirs'])
    # Put the carrier contract in the official system-prompt channel as well as
    # the full task template. Never repair or trim the returned report. The contract
    # comes from the shared prompt-contract module so all three entries emit an
    # identical, verifiable nine-section payload before the call is made.
    # 本轮身份来自真实 runtime（国际 qoder / CN qodercn）与请求模型；不根据旧提示词
    # 文本推断执行者，也不把请求名冒充已核验后端身份。
    contract = pc.build_contract(args.stage, str(work), runtime=args.runtime,
                                 model=args.model)
    argv[argv.index('-p'):argv.index('-p')] = ['--append-system-prompt', contract]
    # prompt_sha256 是原始提示词文件字节哈希（dispatch-plan 统一口径），不做换行归一。
    prompt_sha256 = hashlib.sha256(prompt_bytes).hexdigest()

    # ---- 任务级预检（仅 --dispatch-plan 时启用）：实际 argv 建好后、Popen 与建目录前 ----
    plan_block = None
    if args.dispatch_plan:
        try:
            plan = ec.load_plan(args.dispatch_plan)
            visible = _expand_tools_to_rules(args.tools)
            active_tasks = None
            if args.active_tasks:
                active_tasks = json.loads(Path(args.active_tasks).read_text(encoding='utf-8'))
        except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
            print(json.dumps({'dispatch_plan_rejected': True, 'sent': False,
                              'exit_code': 2, 'reasons': [str(exc)]},
                             ensure_ascii=False))
            return 2
        actual = {'task_id': plan.get('task_id'), 'stage': args.stage,
                  'runtime': args.runtime, 'model': args.model, 'workspace': str(work),
                  'cwd': str(work), 'prompt_sha256': prompt_sha256,
                  'argv': argv, 'shell': False,
                  'grants': ec.grants_from_rules(perms['allowed_tools'],
                                                 perms['add_dirs'],
                                                 perms['disallowed_tools'], visible)}
        result = ec.preflight(plan, actual, active_tasks=active_tasks)
        plan_block = {'plan_path': str(Path(args.dispatch_plan).resolve()),
                      'plan_hash': result['plan_hash'], 'ok': result['ok'],
                      'reasons': result['reasons'], 'argv_sha256': result['argv_sha256'],
                      'active_task_count': result['active_task_count'],
                      'is_atomic_lock': result['is_atomic_lock']}
        if not result['ok']:
            print(json.dumps({'dispatch_plan_rejected': True, 'sent': False,
                              'exit_code': 2, 'reasons': result['reasons'],
                              'plan_hash': result['plan_hash']}, ensure_ascii=False))
            return 2
        plan_task_id = plan.get('task_id')
    else:
        plan_task_id = None

    # ---- 跨会话并发容量门（所有路径，含不传 --dispatch-plan 的兼容路径）----
    # Popen/建目录前原子消费一个容量 claim：给了 --dispatch-claim 就精确校验并 mark_running
    # （漂移即拒），否则做原子路由选择；当前入口非被选中组合 → routing_required、sent=false、
    # 退出 2、零输出目录，绝不先提交错模型、绝不浪费/重复占用分配名额。
    dispatch_store = args.dispatch_store or str(dp.default_store_path())
    quota_store = args.quota_store or os.environ.get('BRAIN_WORKER_QUOTA_STORE')
    task_id = args.task_id or plan_task_id or args.stage
    # 真实调用者身份（Z2）：在 Popen 前用本进程真实 PID + 创建时刻锚定 owner，供
    # bind_child/finish 校验——别人不能把子进程挂到不属于自己的 attempt，也不能凭一个终态
    # 参数释放本 owner 尚未启动/未知的名额。全程复用同一身份，绝不伪造。
    wrapper_pid = os.getpid()
    wrapper_created = dp.process_identity(wrapper_pid).get('created')
    # executor='qoder'：可信主脑显式指定 Qoder 入口——有空位的 Qwen3.8-Max 一律直接 claim，
    # 绝不因历史 committed 计数被 1:1 改派到 ZCode；同一持久 ZCode availability 状态只用于
    # 保证不会被绕回不可用的 ZCode。
    pool_gate = dp.consume_for_entry(
        dispatch_store, task_id=task_id, runtime=args.runtime, model=args.model,
        workspace=str(work), prompt_sha256=prompt_sha256, stage=args.stage,
        chat_id=args.chat_id, claim_token=args.dispatch_claim,
        wrapper_pid=wrapper_pid, wrapper_created=wrapper_created,
        executor=args.runtime, quota_store=quota_store, quota_routes=args.quota_routes)
    if not pool_gate['allowed']:
        print(json.dumps({'capacity_gate_rejected': True, 'sent': False, 'exit_code': 2,
                          'routing_required': bool(pool_gate.get('routing_required')),
                          'reason': pool_gate.get('reason'),
                          'selected': pool_gate.get('selected'),
                          'pool_key': pool_gate.get('pool_key'),
                          'domestic_full': pool_gate.get('domestic_full'),
                          'reasons': pool_gate['reasons']}, ensure_ascii=False))
        return 2
    claim_token = pool_gate['token']

    def _release_claim(terminal, success=None):
        """释放本 attempt 名额，带真实 owner 身份；返回 finish 结果供调用方核验，绝不在
        释放失败时谎称已释放。释放异常不掩盖真实终态（容量对账由 reconcile 兜底）。"""
        try:
            return dp.finish(dispatch_store, claim_token, terminal=terminal,
                             success=success, wrapper_pid=wrapper_pid,
                             wrapper_created=wrapper_created)
        except Exception as exc:  # noqa: BLE001
            return {'settled': False, 'released': False, 'token': claim_token,
                    'terminal': terminal, 'error': str(exc)}

    try:
        out.mkdir(parents=True, exist_ok=False)  # refuse overwriting/replaying an existing invocation
    except OSError:
        # 建目录失败发生在 claim 消费之后、Popen 之前：本 attempt 从未启动，带 owner 身份
        # 安全释放（start_failed）再退出，绝不泄漏容量、绝不误抢别人的名额。
        _release_claim('start_failed')
        raise
    # BW-CN-PERMISSION-STOP-S1：CN 会话权限拒绝监视器（纯标准库、只读会话 jsonl、零网络）。
    # 在 Popen 之前创建以冻结基线偏移：resume 的旧事件绝不重扫触发。国际路径不建监视器，
    # 默认 communicate 行为一字不变。
    watcher = None
    if is_cn:
        try:
            watcher = qpw.SessionDenialWatcher(cn_config_dir, cn_watch_session_id, str(work))
        except Exception as exc:  # noqa: BLE001 - 形状/IO 异常一律受控未启动，不放宽不重试
            rel = _release_claim('start_failed')
            print(json.dumps({'dispatch_failed': True, 'sent': False,
                              'capacity_released': bool(rel.get('released')),
                              'reasons': [f'CN permission watch init failed before '
                                          f'Popen: {exc!r}']}, ensure_ascii=False))
            return 3

    def _watch_facts_field():
        """CN 本轮监视事实（是否找到/观察到会话、精确位置、degraded、错误与读取进度）；无监视器
        （国际路径或监视器未建成）返回空字典，绝不改变国际侧输出形状。**仅留证，不冒充已观察到
        会话**——未找到会话文件时如实为 session_observed=false；正常结束没有拒绝时也必须能回读
        本轮监控状态。不新增定时器/DB/前置探针/全局健康门/授权开关。"""
        return {} if watcher is None else {'cn_watch_facts': watcher.facts()}

    # Popen 前的所有准备/序列化/落盘（载荷、契约、request.json）失败都属"已知未启动"：
    # 统一带 owner 身份 settle start_failed、sent=false、零 Popen、只释放本占位，绝不泄漏。
    try:
        # Two send channels must both be evidenced by real bytes: the task text goes to
        # stdin, the full contract goes to the official --append-system-prompt argv. Save
        # exactly what is handed to the child and read it back before recording evidence.
        stdin_payload = prompt.encode('utf-8')
        (out / 'sent-payload-stdin.bin').write_bytes(stdin_payload)
        (out / 'sent-contract-system-prompt.txt').write_bytes(contract.encode('utf-8'))
        stdin_readback = (out / 'sent-payload-stdin.bin').read_bytes()
        contract_readback = (out / 'sent-contract-system-prompt.txt').read_bytes()
        prompt_payload = pc.payload_evidence(
            raw_prompt_bytes=prompt_bytes, sent_task_text=prompt, contract=contract,
            sent_payload_bytes=stdin_payload,
            newline_caliber='raw-file-bytes-preserving-decode; task=stdin, contract=argv system-prompt')
        prompt_payload['readback_match'] = (
            stdin_readback == stdin_payload and contract_readback == contract.encode('utf-8'))
        prompt_payload['channels'] = {
            'task_stdin_sha256': pc.sha256_hex(stdin_readback),
            'system_prompt_argv_sha256': pc.sha256_hex(contract_readback),
            'contract_present_in_argv': contract in argv,
        }
        request = {'started_at': datetime.now(timezone.utc).isoformat(),
                   'workspace': str(work), 'prompt_file': str(prompt_path),
                   'prompt_sha256': prompt_sha256,
                   'prompt_payload': prompt_payload,
                   'model_requested': args.model, 'tools': args.tools,
                   'allowed_tools': list(perms['allowed_tools']),
                   'disallowed_tools': list(perms['disallowed_tools']),
                   'add_dirs': list(perms['add_dirs']),
                   'task_scope': task_scope_record,
                   'stage': args.stage, 'chat_id': args.chat_id,
                   'resume_session_id': args.session_id,
                   'executor_identity': {
                       'runtime': args.runtime,
                       'request_model': args.model,
                       'model_id': cn_model_id if is_cn else None,
                       'source': 'entry runtime + requested model; not a verified '
                                 'backend identity',
                       'older_identity_does_not_override': True,
                       'contract_sha256': hashlib.sha256(
                           contract.encode('utf-8')).hexdigest()},
                   'entry_config': str(Path(args.cn_config if is_cn else args.config).resolve()),
                   'runtime': ({'cli': cfg['cli'], 'config_dir': cn_config_dir} if is_cn
                               else {'node': cfg['node'], 'qodercli': cfg['qodercli']}),
                   'argv': argv}
        if is_cn:
            # 保留真实 runtime / model_requested(友好名) / model_id(真实 ID) 口径；旧国际
            # 请求记录不受影响。resume_verifiable 供 CN 接续来源核验。
            request['runtime_name'] = RUNTIME_CN
            request['model_id'] = cn_model_id
            request['resume_verifiable'] = True
            # 会话权限拒绝监视留证：精确 session 文件、来源（generated/resume）、基线偏移。
            # 现场布局为 projects/<workspace-key>/<sessionId>.jsonl；新会话文件可能在 Popen
            # 之后才出现，此时 projects_file=None 且 session_observed=false（绝不宣称已观察到）。
            request['cn_session_watch'] = {
                'session_id': cn_watch_session_id,
                'source': cn_watch_source,
                'projects_dir': str(Path(cn_config_dir) / 'projects'),
                'projects_file': str(watcher.path) if watcher.path is not None else None,
                'session_observed': watcher.path is not None,
                'baseline_bytes': watcher.baseline_bytes,
                'baseline_line_number': watcher.baseline_line_number,
                'poll_interval_seconds': qpw.POLL_INTERVAL_SECONDS,
                'note': 'incremental watch of exactly this one session jsonl, located by '
                        'exact uuid filename under config_dir/projects/<workspace-key>/ '
                        '(never by scanning other sessions); the file may appear after '
                        'start (new session) with baseline 0 so new events are not '
                        'swallowed; only sessionId/cwd-bound type=user tool_result '
                        'permission denials stop the child; monitor facts are recorded, '
                        'degraded watch is never claimed as protection'}
        if plan_block is not None:
            request['dispatch_plan'] = plan_block
        (out / 'request.json').write_text(json.dumps(request, ensure_ascii=False, indent=2),
                                          encoding='utf-8')
    except (OSError, TypeError, ValueError) as exc:
        rel = _release_claim('start_failed')
        print(json.dumps({'dispatch_failed': True, 'sent': False,
                          'capacity_released': bool(rel.get('released')),
                          'reasons': [f'pre-start prep/write failed before Popen: {exc!r}'],
                          **_watch_facts_field()},
                         ensure_ascii=False))
        return 3
    child = None
    try:
        with (out / 'stdout.json').open('wb') as stdout, (out / 'stderr.log').open('wb') as stderr:
            child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr)
            bind = dp.bind_child(dispatch_store, claim_token, child.pid,
                                 wrapper_pid=wrapper_pid, wrapper_created=wrapper_created)
            if not bind.get('bound'):
                # 无法把真实子进程绑到本 attempt（owner 不符/已绑/不在途）：子进程确已启动且
                # 存活未知——只标 unknown 继续占容量，绝不释放、绝不误抢、绝不杀其它任务，
                # 交 reconcile/人工核验；如实上报，不谎称成功或已释放。
                dp.mark_unknown(dispatch_store, claim_token)
                print(json.dumps({'dispatch_failed': True, 'sent': True,
                                  'terminal_state': 'unknown', 'capacity_held': True,
                                  'reasons': [f'could not bind child to claim: '
                                              f'{bind.get("reasons") or bind.get("reason")}'],
                                  **_watch_facts_field()},
                                 ensure_ascii=False))
                return 3
            (out / 'process.json').write_text(json.dumps({'pid': child.pid, 'state': 'running'}),
                                              encoding='utf-8')
            watch_outcome = None
            if watcher is not None:
                # CN：communicate 仍恰好一次携 input 送 stdin；主线程 ~0.2s 轮询会话
                # jsonl，发现真实审批拒绝即只经本 child 对象终止并等待真实退出。
                watch_outcome = qpw.communicate_with_permission_watch(
                    child, stdin_payload, watcher,
                    poll_interval=qpw.POLL_INTERVAL_SECONDS,
                    stop_grace=qpw.STOP_GRACE_SECONDS)
                if watch_outcome.get('denial') is not None:
                    # 先落最小拒绝证据（原始事实），再做终态处置；原始会话文件不动。
                    _save_permission_stop_evidence(out, watch_outcome, watcher)
                if not watch_outcome.get('exit_confirmed'):
                    # 终态无法确认：只标 unknown 继续占容量，绝不释放、绝不误抢；证据已落盘。
                    dp.mark_unknown(dispatch_store, claim_token)
                    print(json.dumps({'dispatch_failed': True, 'sent': True,
                                      'terminal_state': 'unknown', 'capacity_held': True,
                                      'permission_stop': _minimal_permission_stop(watch_outcome),
                                      **_watch_facts_field(),
                                      'reasons': ['child exit could not be confirmed '
                                                  'after the CN permission-watch stop; '
                                                  'capacity held as unknown']},
                                     ensure_ascii=False))
                    return 3
            else:
                child.communicate(input=stdin_payload)
    except KeyboardInterrupt:
        # 中断时子进程存活未知：只标 unknown 继续占容量，绝不释放、绝不误抢，交由 reconcile。
        dp.mark_unknown(dispatch_store, claim_token)
        raise
    except Exception as exc:
        if child is None:
            # Popen 本身失败（从未启动子进程）：带 owner 身份 settle start_failed，如实反映
            # 是否真释放，绝不谎称已释放。
            rel = _release_claim('start_failed')
            print(json.dumps({'dispatch_failed': True, 'sent': False,
                              'capacity_released': bool(rel.get('released')),
                              'reasons': [f'child launch failed before start: {exc}'],
                              **_watch_facts_field()},
                             ensure_ascii=False))
            return 3
        # 子进程已启动但终态无法确认（communicate 异常）：只标 unknown，保留占位，绝不释放。
        dp.mark_unknown(dispatch_store, claim_token)
        print(json.dumps({'dispatch_failed': True, 'sent': True, 'terminal_state': 'unknown',
                          'capacity_held': True,
                          'reasons': [f'subprocess terminal state could not be confirmed: {exc}'],
                          **_watch_facts_field()},
                         ensure_ascii=False))
        return 3
    # 子进程真实结束 → 先解析真实终态（原始错误/输出完整保留），判定是否明确限额；若明确
    # 限额且真实为三个 Qoder Max 主力之一，则**先落限额标记，再释放本占位**。顺序保证：占位
    # 未释放（槽仍占用）时先写标记，避免槽一释放另一个 chat 立刻向已知限额池重复提交；落标
    # 或任何后续保存失败也必须做真实终态释放（带 owner 身份、不泄漏锁、不谎称已释放）。
    # BW-CN-PERMISSION-STOP-S1：监视器观察到审批拒绝时，permission_stop 与最小
    # permission_denials 先行汇入 summary——后续 stdout 解析失败（被终止时往往是部分字节）
    # 只能追加 parse_error，绝不把权限停止覆盖成 API/模型故障。
    permission_stop = None
    if watch_outcome is not None and watch_outcome.get('denial') is not None:
        _d = watch_outcome['denial']
        permission_stop = {'terminated_by_watch': bool(watch_outcome.get('terminated_by_watch')),
                           'observed_after_exit': bool(watch_outcome.get('observed_after_exit')),
                           'kind': _d.get('kind'),
                           'tool_use_id': _d.get('tool_use_id'),
                           'source_file': _d.get('source'),
                           'line_number': _d.get('line_number'),
                           'observed_at_utc': _d.get('observed_at_utc'),
                           'exit_code': child.returncode,
                           'evidence_file': str(out / 'permission-stop.json'),
                           'partial_outputs': ('stdout.json/stderr.log saved as-is '
                                               '(possibly partial because the child was '
                                               'stopped mid-run)')}
    stopped_by_watch = bool(permission_stop and permission_stop['terminated_by_watch'])
    summary = {'exit_code': child.returncode, 'finished_at': datetime.now(timezone.utc).isoformat(),
               'model_requested': args.model, 'protocol_success': False,
               'business_verified': False, 'output_dir': str(out)}
    # BW-CN-PERMISSION-STOP-S3：CN 所有已知终态都随 summary 存本轮监视事实（是否找到/观察到
    # 会话、位置、degraded、错误与读取进度）——无拒绝的正常结束也必须能回读监控状态；无监视器
    # 的国际路径不新增字段（形状不变）。
    summary.update(_watch_facts_field())
    if permission_stop is not None:
        summary['permission_stop'] = permission_stop
    if watch_outcome is not None and watch_outcome.get('communicate_error'):
        # communicate 在子线程内如实捕获的异常（如 stdin 早关）：终态已另行经 poll/wait
        # 确认，不掩盖，也不谎称通道无异常。
        summary['cn_watch_communicate_error'] = watch_outcome['communicate_error']
    report_state = None
    stdout_bytes = None
    # BW-CN-PERMISSION-STOP-S2：CN 会话身份一致性。入口生成/接续的 session id 必须与最终
    # envelope.session_id 一致，绝不"监视 A 而接受 B 的报告"。不一致如实记录（不当作认证或
    # 额度事实），并拒绝对该报告做绑定。国际路径不设该检查，行为一字不变。
    cn_identity = None
    cn_identity_ok = True
    try:
        try:
            stdout_bytes = (out / 'stdout.json').read_bytes()
            result = json.loads(stdout_bytes.decode('utf-8'))
            summary.update(protocol_success=(child.returncode == 0 and not stopped_by_watch
                                             and result.get('type') == 'result'
                                             and result.get('subtype') == 'success' and result.get('is_error') is False
                                             and result.get('stop_reason') == 'end_turn'),
                           session_id=result.get('session_id'), stop_reason=result.get('stop_reason'),
                           total_credits=result.get('total_credits'), model_usage=result.get('modelUsage'),
                           permission_denials=result.get('permission_denials'),
                           result_errors=result.get('errors'),
                           result_errors_info=result.get('errors_info'))
            if is_cn:
                env_session_id = summary.get('session_id')
                cn_identity_ok = (env_session_id is None
                                  or str(env_session_id) == cn_watch_session_id)
                cn_identity = {
                    'watched_session_id': cn_watch_session_id,
                    'watch_source': cn_watch_source,
                    'envelope_session_id': env_session_id,
                    'match': cn_identity_ok,
                    'note': 'the CN session id generated/resumed by this entry must equal the '
                            'envelope session_id; a mismatch is recorded honestly and the '
                            'report is not bound to the watched session — it is never '
                            'treated as an auth or quota fact'}
                summary['cn_session_identity'] = cn_identity
            response = result.get('result')
            if isinstance(response, str):
                (out / 'response.md').write_bytes(response.encode('utf-8'))
                disk = (out / 'response.md').read_bytes()
                digest = hashlib.sha256(disk).hexdigest()
                summary['response_sha256'] = digest
                readback_match = digest == hashlib.sha256(response.encode('utf-8')).hexdigest()
                body = analyze_report(response, args.stage, str(work))
                binding = finalize_binding(
                    body['body_ok'], body['reasons'],
                    protocol_success=summary['protocol_success'],
                    session_id=summary.get('session_id'),
                    requested_session_id=args.session_id,
                    readback_match=readback_match)
                report_state = {**body, 'response_sha256': digest,
                                'readback_match': readback_match,
                                'protocol_success': summary['protocol_success'],
                                'session_id': summary.get('session_id'),
                                'requested_session_id': args.session_id,
                                'binding': binding,
                                'bound': binding['bound'],
                                'reasons': binding['reasons']}
            else:
                report_state = {'bound': False, 'body_ok': False,
                                'reasons': ['envelope result is not a text response'],
                                'carrier_missing': True}
        except (ValueError, OSError) as exc:
            summary['parse_error'] = str(exc)
            report_state = {'bound': False, 'body_ok': False,
                            'reasons': [f'stdout parse error: {exc}'],
                            'carrier_missing': True}
        if permission_stop is not None and not summary.get('permission_denials'):
            # envelope 未自带 permission_denials 时补最小监视拒绝（envelope 自带则原样
            # 保留）；失败分型据此得出 permission_rule_denied，不被 parse_error 独占。
            summary['permission_denials'] = [
                {'tool_use_id': permission_stop['tool_use_id'],
                 'reason': f"CN session watch observed a real {permission_stop['kind']} "
                           f"tool denial",
                 'source': permission_stop['evidence_file']}]
        if permission_stop is not None and report_state is not None:
            report_state['permission_stop'] = {
                k: permission_stop[k] for k in ('terminated_by_watch', 'observed_after_exit',
                                                'kind', 'tool_use_id', 'evidence_file')}
            if stopped_by_watch:
                # 监视器终止的 child：协议按未完成处理，报告一律不绑定（即使部分 stdout
                # 里碰巧有可解析正文）；permission_stop 事实保留，不因解析结果被改写。
                report_state['protocol_success'] = False
                report_state['bound'] = False
                report_state.setdefault('reasons', []).append(
                    'CN permission watch terminated this child; protocol incomplete and '
                    'report binding is refused')
                _binding = dict(report_state.get('binding') or {})
                _binding['bound'] = False
                _binding['protocol_success'] = False
                _binding.setdefault('reasons', []).append(
                    'CN permission watch terminated this child; report binding refused')
                report_state['binding'] = _binding
        if is_cn and not cn_identity_ok and report_state is not None:
            # 监视的会话与 envelope 报告的会话不是同一个：如实记录，拒绝把该报告绑到本会话。
            _identity_reason = ('CN session identity mismatch: the envelope session_id does '
                                'not match the session this entry watched; the report is not '
                                'bound to the watched session and is not treated as an auth '
                                'or quota fact')
            report_state['cn_session_identity'] = cn_identity
            report_state['bound'] = False
            report_state['reasons'] = list(report_state.get('reasons') or []) + [_identity_reason]
            _binding = dict(report_state.get('binding') or {})
            _binding['bound'] = False
            _binding['reasons'] = list(_binding.get('reasons') or []) + [_identity_reason]
            report_state['binding'] = _binding
        if report_state is not None:
            report_state.setdefault('protocol_success', summary['protocol_success'])
            report_state.setdefault('session_id', summary.get('session_id'))
            (out / 'report-state.json').write_text(
                json.dumps(report_state, ensure_ascii=False, indent=2), encoding='utf-8')
            summary['report_bound'] = report_state.get('bound', False)
            summary['report_state_file'] = str(out / 'report-state.json')
        summary['diagnostics'] = ec.diagnose(_qoder_failure_facts(summary))
        # 明确限额落标：只在**真实失败终态**（非协议成功）且结构化失败载体明确命中硬限额、
        # 且 runtime/model 真实为三个 Qoder Max 主力之一时落标。先落标再释放占位，杜绝另一
        # chat 在槽释放后立刻重复提交；落标异常绝不阻断真实终态释放（不泄漏锁）。原始错误与
        # 同时发生的 permission_denials 一并保留，落标不消去任何审批拒绝。
        # 权限监视终止的 attempt 绝不落 quota 标记（拒绝不是额度事实）。
        if not summary.get('protocol_success') and not stopped_by_watch:
            hit, evidence = _explicit_quota_limit_hit(summary)
            limited_pk = _qoder_max_pool_key(args.runtime, args.model) if hit else None
            if limited_pk is not None:
                ev_sha = hashlib.sha256(stdout_bytes).hexdigest() if stdout_bytes else None
                try:
                    limit_rec = dp.record_main_force_limit(
                        dispatch_store, limited_pk, task_id=task_id,
                        attempt_token=claim_token,
                        evidence_path=str(out / 'stdout.json'), evidence_sha256=ev_sha)
                except Exception as exc:  # noqa: BLE001 - 落标失败不得阻断真实终态释放
                    limit_rec = {'recorded': False, 'pool_key': limited_pk, 'error': str(exc)}
                limit_rec['evidence'] = evidence
                summary['main_force_limit'] = limit_rec
    finally:
        # 无论解析/落标成功与否，都带 owner 身份做真实终态释放（核验探针回读 child 已 dead）。
        rel = _release_claim('finished', success=(child.returncode == 0))
        summary['capacity_released'] = bool(rel.get('released'))
        summary['capacity_terminal'] = rel.get('terminal')
    (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                      encoding='utf-8')
    (out / 'process.json').write_text(json.dumps({'pid': child.pid, 'state': 'exited',
                                                  'exit_code': child.returncode}),
                                      encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary['protocol_success'] else 3


if __name__ == '__main__':
    raise SystemExit(main())
