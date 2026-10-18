"""qoder_permissions — 从显式 scope 生成 Qoder 精确权限参数数据（标准库，纯函数）。

输入是一个明确的 scope JSON，只有三个键（缺省视为空列表）：

- ``edits``: 工作区根相对的具体文件列表（``scripts/foo.py`` / ``scripts\\foo.py``）；
- ``bash``: 完整、原样的单条命令字符串列表；
- ``read_dirs``: 已经授权的只读目录列表（原样传递）。

输出是可直接交给主脑构造 Qoder plan/argv 的数据：

- ``visible_tools``: 始终含 Read/Glob/Grep；有 edits 才加 Edit+Write；有 bash 才加 Bash；
- ``allowed_tools``: Read/Glob/Grep 裸规则 + 每个文件一条 ``Edit(/路径)`` + 每条命令一条
  ``Bash(原样命令)``；绝不生成 ``Write(路径)``、裸 Edit 或裸 Write；
- ``disallowed_tools``: 空列表（本 helper 只生成只读/写入授权，不新造 deny）；
- ``add_dirs``: 原样传递的只读目录；
- ``grants``: 调用现有 ``execution_control.grants_from_rules`` 得到的可复用集合。

本模块只做字符串校验与组合：不启动进程、不执行任何命令、不派工、不写 DB/配置，也不
修改调用方传入的 plan/scope。scope 只是准备输入，不自动构成用户授权；主脑核对授权后，
必须显式用本输出构造 Qoder 计划与实际 argv，并由既有 preflight 复核。
"""
import argparse
import json
import re
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import execution_control as ec  # noqa: E402 复用既有 grants_from_rules，不复制其逻辑

SCOPE_KEYS = ('edits', 'bash', 'read_dirs')
BASE_VISIBLE = ('Read', 'Glob', 'Grep')
_DRIVE_RE = re.compile(r'^[A-Za-z]:')


def _require_list(value, field) -> list:
    if not isinstance(value, list):
        raise ValueError(f'scope.{field} must be a list of strings')
    return value


def normalize_edit_path(value) -> str:
    """校验并规范化一个工作区根相对的具体文件路径。

    允许常规中文与内部空格；统一反斜杠为正斜杠。拒绝空/非字符串、外部绝对路径、
    盘符路径、``..``/``.``/空段逃逸或非规范形式、整类通配符，以及会破坏规则语法的
    换行、控制字符和括号。不做任何“猜测修正”。
    """
    if not isinstance(value, str):
        raise ValueError('edit path must be a string')
    if value == '' or value.isspace():
        raise ValueError('edit path must be a non-empty string')
    if value != value.strip():
        raise ValueError(f'edit path must not have surrounding whitespace: {value!r}')
    if any(ord(ch) < 0x20 or ch == '\x7f' for ch in value):
        raise ValueError(f'edit path must not contain control characters: {value!r}')
    if '(' in value or ')' in value:
        raise ValueError(f'edit path must not contain parentheses (breaks rule syntax): '
                         f'{value!r}')
    if '*' in value or '?' in value:
        raise ValueError(f'edit path must name one concrete file, not a wildcard: {value!r}')

    path = value.replace('\\', '/')
    if path.startswith('/'):
        raise ValueError(f'edit path must be workspace-root-relative, not absolute: '
                         f'{value!r}')
    if _DRIVE_RE.match(path):
        raise ValueError(f'edit path must not contain a drive letter: {value!r}')
    parts = path.split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise ValueError(f'edit path must be a canonical relative path without '
                         f'empty/./.. segments: {value!r}')
    return path


def _validate_bash(command, index) -> str:
    """逐字保留命令；只拒绝空/仅空白、换行/控制字符这类无法作为单条规则承载的输入。
    不解析 shell、不追加任何包装、不猜测参数完整性。"""
    if not isinstance(command, str):
        raise ValueError(f'scope.bash[{index}] must be a string')
    if command == '' or command.isspace():
        raise ValueError(f'scope.bash[{index}] must be a non-empty command')
    if any(ord(ch) < 0x20 or ch == '\x7f' for ch in command):
        raise ValueError(f'scope.bash[{index}] must be one line without control '
                         f'characters: {command!r}')
    return command


def _validate_read_dir(dirname, index) -> str:
    if not isinstance(dirname, str):
        raise ValueError(f'scope.read_dirs[{index}] must be a string')
    if dirname == '' or dirname.isspace():
        raise ValueError(f'scope.read_dirs[{index}] must be non-empty')
    if any(ord(ch) < 0x20 or ch == '\x7f' for ch in dirname):
        raise ValueError(f'scope.read_dirs[{index}] must not contain control '
                         f'characters: {dirname!r}')
    return dirname


def build_permissions(scope) -> dict:
    """从 scope JSON 对象生成 Qoder 参数数据；非法输入抛 ValueError。"""
    if not isinstance(scope, dict):
        raise ValueError('scope must be a JSON object')
    unknown = sorted(set(scope) - set(SCOPE_KEYS))
    if unknown:
        raise ValueError(f'scope has unknown keys: {unknown}; allowed: '
                         f'{list(SCOPE_KEYS)}')

    edits_in = _require_list(scope.get('edits', []), 'edits')
    bash_in = _require_list(scope.get('bash', []), 'bash')
    read_dirs_in = _require_list(scope.get('read_dirs', []), 'read_dirs')

    edits = [f'Edit(/{normalize_edit_path(item)})' for item in edits_in]
    bash = [f'Bash({_validate_bash(item, i)})' for i, item in enumerate(bash_in)]
    read_dirs = [_validate_read_dir(item, i) for i, item in enumerate(read_dirs_in)]

    visible = list(BASE_VISIBLE)
    if edits:
        visible += ['Edit', 'Write']
    if bash:
        visible += ['Bash']
    disallowed = []
    allowed = list(BASE_VISIBLE) + edits + bash

    grants = ec.grants_from_rules(allowed, read_dirs, disallowed, visible)
    return {'visible_tools': visible, 'allowed_tools': allowed,
            'disallowed_tools': disallowed, 'add_dirs': read_dirs,
            'grants': grants}


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description='generate Qoder permission data from an explicit scope JSON')
    ap.add_argument('--scope', required=True,
                    help='path to the scope JSON (edits/bash/read_dirs)')
    return ap


def main(argv=None) -> int:
    args = build_argparser().parse_args(argv)
    try:
        scope = json.loads(Path(args.scope).read_text(encoding='utf-8'))
        data = build_permissions(scope)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
