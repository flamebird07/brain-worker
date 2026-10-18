"""Current-round CodeBuddy quota-evidence verification for the GLM low-tier fallback.

BW-CODEBUDDY-GLM-FALLBACK-20261018：`codebuddy:glm-5.3-flash`（界面名 GLM-5.3-Flash）只是
一个**低层补位**，启用条件必须同时成立：

1. 授权集合包含 GLM（由 stage_selection/正式入口显式表达）；
2. **当轮** `codebuddy:deepseek-v4.1-flash` 已真实失败；
3. 当轮失败带有**明确**的 429/额度证据（本模块从该轮 `summary.json` 复验）。

本模块只做只读证据复验，不联网、不查余额、不创建/修改任何文件、不派发、不切换模型。
只复用既有分类口径（`execution_control.explicit_429` 与 `execution_control.diagnose` 产出的
`diagnostics.failure_types`），**绝不扫描成功报告正文里的 429/quota 字样**。返回结构只包含
证据路径、SHA-256 与原因码/分类名，绝不回显原始错误文本、请求头、Cookie、密钥或令牌。

默认无证据（`path` 为空/不存在）一律 fail-closed：GLM 不可用。旧轮次 summary.json、旧
persistent limit 标记、历史报告或普通网络/认证/权限/容量失败都不能满足本条件。
"""
import hashlib
import json
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import execution_control as ec  # 既有的明确 429 分类口径（不新增第二套规则）

EVIDENCE_RUNTIME = 'codebuddy'
EVIDENCE_MODEL = 'deepseek-v4.1-flash'
EVIDENCE_FAILURE_TYPE = 'quota_429'
# summary.json 顶层字段（由 codebuddy_live 原样写入）。
_REQUIRED_FIELDS = ('runtime', 'model', 'protocol_success', 'terminal_state',
                    'failure_envelope_valid')


_KNOWN_FAILURE_TYPES = frozenset(ec.FAILURE_TYPES)


def _safe_failure_types(summary):
    """只保留既有 `ec.FAILURE_TYPES` 白名单分类。

    `diagnostics.failure_types` 来自被测 JSON，形状与内容都不受本模块控制；绝不能把其中的
    任意字符串（例如合成敏感标记或注入文本）带进回执。未知分类一律丢弃（不推断、不改写）。
    """
    diagnostics = summary.get('diagnostics') if isinstance(summary, dict) else None
    raw = diagnostics.get('failure_types') if isinstance(diagnostics, dict) else None
    if not isinstance(raw, list):
        return []
    return [t for t in raw if isinstance(t, str) and t in _KNOWN_FAILURE_TYPES]


def _safe_exit_code(summary):
    """exit_code 只接受真实数字（int，排除 bool）；嵌套 dict/list/字符串一律回 None。"""
    value = summary.get('exit_code') if isinstance(summary, dict) else None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _result(path, reason, *, ok=False, sha256=None, summary=None):
    """构造脱敏结果。summary 只用于提取分类字段，绝不回显原始错误载体。

    - runtime/model 只回报已知身份（codebuddy / deepseek-v4.1-flash），其它形状回 None；
    - failure_types 只回报既有白名单分类；
    - exit_code 只回报数字；
    - 本函数不复制 summary 的原始错误文本、请求头、Cookie、密钥或令牌。
    """
    summary = summary if isinstance(summary, dict) else {}
    failure_types = _safe_failure_types(summary)
    runtime = summary.get('runtime')
    if runtime not in (EVIDENCE_RUNTIME,):
        runtime = None
    model = summary.get('model')
    if model not in (EVIDENCE_MODEL,):
        model = None
    return {'ok': bool(ok), 'allowed': bool(ok), 'reason': reason,
            'reason_code': reason,
            'path': str(path) if path is not None else None,
            'sha256': sha256,
            'runtime': runtime,
            'model': model,
            'exit_code': _safe_exit_code(summary),
            'failure_types': failure_types,
            'primary_failure_class': (EVIDENCE_FAILURE_TYPE
                                      if EVIDENCE_FAILURE_TYPE in failure_types else None),
            'source': 'current_round_summary_json',
            'raw_error_text_included': False}


def verify_quota_evidence(path):
    """只读复验当轮 summary.json；返回脱敏 dict（ok/allowed/reason/path/sha256/...）。

    通过条件（全部满足）：
    - 文件存在且是 UTF-8 JSON object；
    - runtime=codebuddy、model=deepseek-v4.1-flash（绝不接受其它 CodeBuddy 模型或旧轮汇总）；
    - protocol_success=false、terminal_state=error、failure_envelope_valid=true
      （成功正文即使含 429/quota 字样也绝不通过）；
    - diagnostics.failure_types 含 quota_429（既有分类结果），且 diagnostics.evidence 中
      真实 errors/errors_info 经 `execution_control.explicit_429` 复核为明确 429；
    - primary_failure 是非空字符串（终态主失败可定位），但不回显其原文。
    不要求 exit_code==429：真实 CLI 常以 0/非 0 其它码退出，是否明确限额只看错误结构。
    """
    if path is None or (isinstance(path, str) and not path.strip()):
        return _result(path, 'missing_evidence_path')
    try:
        evidence_path = Path(path)
    except (TypeError, ValueError):
        return _result(path, 'evidence_path_invalid')
    try:
        if not evidence_path.is_absolute():
            evidence_path = evidence_path.resolve()
        if not evidence_path.is_file():
            return _result(evidence_path, 'evidence_not_found')
        raw = evidence_path.read_bytes()
    except OSError:
        return _result(evidence_path, 'evidence_unreadable')
    sha = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode('utf-8')
        summary = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _result(evidence_path, 'evidence_invalid_json', sha256=sha)
    if not isinstance(summary, dict):
        return _result(evidence_path, 'evidence_invalid_shape', sha256=sha)
    for field in _REQUIRED_FIELDS:
        if field not in summary:
            return _result(evidence_path, 'evidence_field_missing', sha256=sha,
                           summary=summary)
    if summary.get('runtime') != EVIDENCE_RUNTIME:
        return _result(evidence_path, 'evidence_runtime_mismatch', sha256=sha,
                       summary=summary)
    if summary.get('model') != EVIDENCE_MODEL:
        return _result(evidence_path, 'evidence_model_mismatch', sha256=sha,
                       summary=summary)
    if summary.get('protocol_success') is not False:
        return _result(evidence_path, 'evidence_protocol_success', sha256=sha,
                       summary=summary)
    if summary.get('terminal_state') != 'error':
        return _result(evidence_path, 'evidence_terminal_not_error', sha256=sha,
                       summary=summary)
    if summary.get('failure_envelope_valid') is not True:
        return _result(evidence_path, 'evidence_envelope_invalid', sha256=sha,
                       summary=summary)
    diagnostics = summary.get('diagnostics')
    if not isinstance(diagnostics, dict):
        return _result(evidence_path, 'evidence_diagnostics_missing', sha256=sha,
                       summary=summary)
    failure_types = diagnostics.get('failure_types')
    if not isinstance(failure_types, list) or EVIDENCE_FAILURE_TYPE not in failure_types:
        return _result(evidence_path, 'evidence_not_explicit_quota_429', sha256=sha,
                       summary=summary)
    evidence = diagnostics.get('evidence')
    if not isinstance(evidence, dict):
        return _result(evidence_path, 'evidence_structure_missing', sha256=sha,
                       summary=summary)
    primary = evidence.get('primary_failure')
    if not isinstance(primary, str) or not primary.strip():
        return _result(evidence_path, 'evidence_primary_failure_missing', sha256=sha,
                       summary=summary)
    # 复用既有唯一分类口径：只认 error/errors_info 结构里的明确 status/code=429 或
    # 文本条目开头的独立 429/HTTP 429；绝不再扫描任何正文。
    if not ec.explicit_429(evidence.get('errors'), evidence.get('errors_info')):
        return _result(evidence_path, 'evidence_429_structure_missing', sha256=sha,
                       summary=summary)
    evidence_terminal = evidence.get('terminal_state')
    if evidence_terminal is not None and evidence_terminal != 'error':
        return _result(evidence_path, 'evidence_terminal_mismatch', sha256=sha,
                       summary=summary)
    return _result(evidence_path, 'explicit_quota_429', ok=True, sha256=sha,
                   summary=summary)


def evidence_allows_glm(path):
    """便捷布尔：仅当复验完全通过时返回 True。默认 None/空路径为 False。"""
    return bool(verify_quota_evidence(path).get('ok'))
