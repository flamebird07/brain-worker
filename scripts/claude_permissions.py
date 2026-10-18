"""Exact write-permission profile for the Claude Code entry (BW-CLAUDE-PATH-20261018-S2).

This module is a **deterministic configuration consistency check**, not an OS sandbox and
not a general glob/path policy engine. Given an explicit workspace and an explicit list of
files, it derives exactly:

    Edit(./<workspace-relative/posix/file>)
    Write(./<workspace-relative/posix/file>)

and validates that the entry config grants exactly those rules for the write tools the task
explicitly declares. It never rewrites the config and never widens a grant silently.

Rejected by construction:
  * wildcards (`* ? [ ]`) and non-file/dir ambiguity;
  * absolute rules (`C:/...`, `\\server\...`, `/...`) in an exact write profile;
  * `..` traversal and aliases that resolve outside the workspace;
  * drive mismatches (workspace on C:, target on D:);
  * directory targets and empty/trailing-separator paths;
  * bare `Edit` / `Write` grants for a precise profile.

Existing and new files are checked through their nearest existing ancestor with
``Path.resolve`` so a symlinked directory cannot be used to escape the workspace (on the
host where the workspace actually exists). Windows separators/drive letters and POSIX paths
are normalized by pure string logic, so conversion is offline testable on either host.

The real Claude CLI still needs a real positive/negative check by the trusted host: this
module only proves that the emitted rules match the declared file list.
"""
import os
import re
from pathlib import Path

_TOOL_ORDER = ('Edit', 'Write')
_WILDCARD = '*?[]'
_DRIVE_RE = re.compile(r'^([A-Za-z]):')
_WIN_BAD_CHARS = frozenset('<>:"|?*')
_WIN_RESERVED_NAMES = frozenset(
    {'CON', 'PRN', 'AUX', 'NUL'}
    | {f'COM{i}' for i in range(1, 10)}
    | {f'LPT{i}' for i in range(1, 10)})


class PermissionProfileError(ValueError):
    """Raised when a file list or rule cannot be represented as an exact ./ rule."""


def _is_windows_abs(path: str) -> bool:
    return bool(re.match(r'^[A-Za-z]:[\\/]', path)) or path.startswith('\\\\')


def _windows_workspace(workspace: str) -> bool:
    return _is_windows_abs(workspace.replace('/', '\\'))


def _workspace_parts(workspace: str) -> tuple[list, str]:
    """Return (normalized parts, style) where style is 'win' or 'posix'."""
    ws = str(workspace)
    if not ws or ws != ws.strip():
        raise PermissionProfileError('workspace must be a non-empty, non-padded path')
    if '\x00' in ws:
        raise PermissionProfileError('workspace contains NUL')
    norm = ws.replace('\\', '/')
    if norm.startswith('//'):
        raise PermissionProfileError('UNC/device workspace paths are not supported')
    if _windows_workspace(ws):
        m = _DRIVE_RE.match(norm)
        if not m or not norm[2:3] == '/':
            raise PermissionProfileError(f'workspace is not an absolute drive path: {ws!r}')
        parts = [m.group(1).upper() + ':'] + _collapse([p for p in norm[2:].split('/')
                                                        if p not in ('', '.')], win=True)
        return parts, 'win'
    if not norm.startswith('/'):
        raise PermissionProfileError(f'workspace must be absolute: {ws!r}')
    return _collapse([p for p in norm.split('/') if p not in ('', '.')], win=False), 'posix'


def _collapse(parts, *, win: bool):
    out = []
    floor = 1 if win else 0
    for part in parts:
        if part in ('', '.'):
            continue
        if part == '..':
            if len(out) <= floor:
                raise PermissionProfileError('path traversal escapes the workspace root')
            out.pop()
            continue
        if any(ch in part for ch in _WILDCARD):
            raise PermissionProfileError(f'wildcards are not allowed: {part!r}')
        if '\x00' in part:
            raise PermissionProfileError('path contains NUL')
        out.append(part)
    return out


def _same(left: str, right: str, win: bool) -> bool:
    return left.casefold() == right.casefold() if win else left == right


def _windows_segment_error(part: str):
    """Reject a Windows path segment that is illegal or ambiguous (ADS/device/trailing dot).

    Returns a short reason or None. Applied only to the canonical relative target segments;
    drive letters are handled separately by the caller.
    """
    if not part:
        return 'empty segment'
    if any(ch in _WIN_BAD_CHARS or ord(ch) < 32 for ch in part):
        return 'illegal Windows character (colon is an ADS alias)'
    if part[-1] in (' ', '.'):
        return 'trailing space/dot is ambiguous on Windows'
    if part.split('.', 1)[0].upper() in _WIN_RESERVED_NAMES:
        return 'reserved Windows device name'
    return None


def _resolve_nearest(path: Path) -> Path:
    """Resolve symlinks in the nearest existing ancestor, preserving not-yet-existing names."""
    current = path
    suffix = []
    while not current.exists():
        if current.parent == current:
            break
        suffix.append(current.name)
        current = current.parent
    resolved = current.resolve()
    for name in reversed(suffix):
        resolved = resolved / name
    return resolved


def canonical_relative_file(workspace, write_file) -> str:
    """Normalize one explicit write target to a canonical forward-slash relative file path.

    Accepts relative paths with either separator, and absolute paths only when they are
    inside the same-drive workspace. The returned path has no leading ``./`` and uses
    forward slashes; it is suitable for ``Edit(./{rel})`` / ``Write(./{rel})``.
    """
    if not isinstance(write_file, str):
        raise PermissionProfileError(
            f'write file must be a string, got {type(write_file).__name__}')
    if not write_file or write_file != write_file.strip():
        raise PermissionProfileError('write file must be non-empty without surrounding whitespace')
    if '\x00' in write_file:
        raise PermissionProfileError('write file contains NUL')
    if any(ch in write_file for ch in _WILDCARD):
        raise PermissionProfileError(f'wildcards are not allowed in write file: {write_file!r}')

    ws_parts, style = _workspace_parts(workspace)
    win = style == 'win'
    item = write_file.replace('\\', '/')
    if item.startswith('//'):
        raise PermissionProfileError('UNC/device write paths are not supported')
    # 尾分隔符/重复分隔符意味着输入本身不是明确的“文件”目标：绝不把 `newdir/`
    # 静默剥成新文件 `newdir`。
    if item.endswith('/') or '//' in item:
        raise PermissionProfileError(
            f'write file has a trailing/ambiguous separator: {write_file!r}')

    drive = _DRIVE_RE.match(item)
    if drive:
        if not win:
            raise PermissionProfileError(
                f'drive-qualified path {write_file!r} does not match a POSIX workspace')
        if not re.match(r'^[A-Za-z]:/', item):
            raise PermissionProfileError(
                f'drive-relative path is ambiguous: {write_file!r}')
        expected_drive = ws_parts[0].rstrip(':')
        if drive.group(1).upper() != expected_drive.upper():
            raise PermissionProfileError(
                f'drive mismatch: workspace drive {expected_drive!r}, '
                f'file drive {drive.group(1)!r}')
        full_parts = _collapse([drive.group(1).upper() + ':']
                               + [p for p in item[2:].split('/') if p not in ('', '.')],
                               win=True)
    elif item.startswith('/'):
        if win:
            raise PermissionProfileError(
                f'absolute POSIX path {write_file!r} is ambiguous in a Windows profile')
        full_parts = _collapse([p for p in item.split('/') if p not in ('', '.')], win=False)
    else:
        full_parts = _collapse(ws_parts + [p for p in item.split('/')
                                           if p not in ('', '.')], win=win)

    if len(full_parts) <= len(ws_parts):
        raise PermissionProfileError(f'write target is not a file inside the workspace: {write_file!r}')
    if not all(_same(a, b, win) for a, b in zip(full_parts, ws_parts)):
        raise PermissionProfileError(f'write target escapes the workspace: {write_file!r}')

    rel_parts = full_parts[len(ws_parts):]
    rel = '/'.join(rel_parts)
    if not rel or rel.endswith('/') or rel in ('.', '..'):
        raise PermissionProfileError(f'write target is not a file: {write_file!r}')
    if win:
        for part in rel_parts:
            problem = _windows_segment_error(part)
            if problem:
                raise PermissionProfileError(
                    f'write file segment {part!r} is not a legal unambiguous Windows '
                    f'name ({problem}): {write_file!r}')

    # Filesystem check: directory targets rejected; nearest existing ancestor resolved so
    # symlink aliases cannot point outside the workspace. Only runs when the workspace is
    # absolute on this host (pure lexical Windows tests on POSIX skip this step).
    try:
        ws_path = Path(workspace)
        if ws_path.is_absolute():
            lexical = ws_path
            for part in rel_parts:
                lexical = lexical / part
            if lexical.is_dir():
                raise PermissionProfileError(f'write target is a directory: {write_file!r}')
            resolved_ws = ws_path.resolve()
            resolved = _resolve_nearest(lexical)
            try:
                common = os.path.commonpath([os.path.normcase(str(resolved_ws)),
                                             os.path.normcase(str(resolved))])
            except ValueError:
                raise PermissionProfileError(
                    f'write target resolves outside the workspace: {write_file!r}')
            if os.path.normcase(common) != os.path.normcase(str(resolved_ws)):
                raise PermissionProfileError(
                    f'write target resolves outside the workspace: {write_file!r}')
    except PermissionProfileError:
        raise
    except OSError as exc:
        # 无法在本机解析真实路径边界时绝不 best-effort 放行：诚实拒绝，而不是宣称 ok。
        raise PermissionProfileError(
            f'cannot verify write target on this host ({type(exc).__name__}): '
            f'{write_file!r}') from exc
    return rel


def canonical_write_files(workspace, write_files) -> list[str]:
    """Normalize/dedupe an explicit file list, preserving order."""
    if write_files is None:
        return []
    if isinstance(write_files, str):
        raise PermissionProfileError('write_files must be a list of file paths, not a string')
    if not isinstance(write_files, (list, tuple)):
        raise PermissionProfileError(
            f'write_files must be a list, got {type(write_files).__name__}')
    result = []
    for item in write_files:
        rel = canonical_relative_file(workspace, item)
        if rel not in result:
            result.append(rel)
    return result


def build_write_rules(workspace, write_files, tools=_TOOL_ORDER) -> list[str]:
    """Generate the exact ``Edit(./...)`` / ``Write(./...)`` rules for the file list."""
    requested = [t for t in _TOOL_ORDER if t in (tools or ())]
    rels = canonical_write_files(workspace, write_files)
    return [f'{tool}(./{rel})' for tool in requested for rel in rels]


def _rule_list(value, field):
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise PermissionProfileError(f'{field} must be a list of strings')
    out = []
    for item in value:
        if not isinstance(item, str) or not item or item != item.strip():
            raise PermissionProfileError(f'{field} entry is not a non-empty string: {item!r}')
        out.append(item)
    return out


def _write_tool_of(rule):
    """Return 'Edit'/'Write' when the rule is a write grant/deny (even malformed), else None."""
    if not isinstance(rule, str) or not rule or rule != rule.strip():
        return None
    if rule in _TOOL_ORDER:
        return rule
    head = rule.split('(', 1)[0]
    if head in _TOOL_ORDER:
        return head
    for tool in _TOOL_ORDER:
        if head.startswith(tool) and len(head) > len(tool):
            nxt = head[len(tool)]
            if not (nxt.isalnum() or nxt in '_-'):
                return tool
    return None


def _parse_exact_rule(rule, tool, win):
    """Parse ``Tool(./relative)``; return the canonical relative path or raise."""
    if rule == tool:
        raise PermissionProfileError(f'bare {tool} grant is not an exact file scope')
    if not rule.startswith(tool + '(') or not rule.endswith(')'):
        raise PermissionProfileError(f'rule {rule!r} is not exactly {tool}(./file)')
    arg = rule[len(tool) + 1:-1]
    if not arg or arg != arg.strip():
        raise PermissionProfileError(f'empty or padded argument in {rule!r}')
    if '\x00' in arg:
        raise PermissionProfileError(f'NUL in {rule!r}')
    if '(' in arg or ')' in arg:
        raise PermissionProfileError(f'unbalanced/extra parentheses in {rule!r}')
    if any(ch in arg for ch in _WILDCARD):
        raise PermissionProfileError(f'wildcard in {rule!r}')
    norm = arg.replace('\\', '/')
    if re.match(r'^[A-Za-z]:', norm) or norm.startswith('/'):
        raise PermissionProfileError(
            f'absolute rule {rule!r} is not accepted in a precise ./ profile')
    if not norm.startswith('./'):
        raise PermissionProfileError(f'rule {rule!r} must start with ./')
    rel = norm[2:]
    if not rel or rel.endswith('/'):
        raise PermissionProfileError(f'rule {rule!r} does not name a file')
    parts = rel.split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise PermissionProfileError(
            f'rule {rule!r} is not a canonical ./ path (traversal/alias refused)')
    return '/'.join(parts)


def _key(rel, win):
    return rel.casefold() if win else rel


def validate_write_profile(cfg, required_tools, write_files, workspace) -> dict:
    """Validate cfg grants against the explicit write file list.

    The caller must separately check tool visibility. This function returns only
    tool/rule names and reasons; it never returns credentials or environment values.
    """
    required = list(required_tools or [])
    declared = [t for t in _TOOL_ORDER if t in required]
    reasons, invalid, missing, extra = [], [], [], []
    denied = []

    try:
        rels = canonical_write_files(workspace, write_files)
    except PermissionProfileError as exc:
        return {'ok': False, 'write_files': [], 'expected_write_rules': [],
                'missing_write_rules': [], 'extra_write_rules': [],
                'denied_write_tools': [], 'invalid_write_rules': [],
                'reasons': [f'write file scope invalid: {exc}']}

    win = _workspace_parts(workspace)[1] == 'win'

    # 任何写操作都必须同时显式声明 Read、写工具需求与精确 write_files；
    # 禁止用“只声明 Edit/Write 但未声明 Read”的方式把读需求绕过去。
    if declared and 'Read' not in required:
        reasons.append(f'required write tool(s) {declared!r} also require an explicit '
                       'Read requirement; write tasks are never read-implicit')
    if declared and not rels:
        reasons.append(f'required write tool(s) {declared!r} need an explicit write_files '
                       'scope; refusing to infer target paths')
    if rels and not declared:
        reasons.append('write_files were declared but required_tools contains no Edit/Write; '
                       'refusing to grant writes without an explicit task requirement')

    allowed = _rule_list(cfg.get('allowed_tools'), 'allowed_tools')
    disallowed = _rule_list(cfg.get('disallowed_tools'), 'disallowed_tools')

    actual = {tool: [] for tool in _TOOL_ORDER}
    for rule in allowed:
        tool = _write_tool_of(rule)
        if tool is None:
            continue
        try:
            rel = _parse_exact_rule(rule, tool, win)
        except PermissionProfileError as exc:
            if rule not in invalid:
                invalid.append(rule)
            reasons.append(f'--allowedTools rule {rule!r} is not an exact ./ file rule: {exc}')
            continue
        if rel not in actual[tool]:
            actual[tool].append(rel)

    if not declared:
        if any(actual[t] for t in _TOOL_ORDER):
            reasons.append('--allowedTools grants Edit/Write but the task has no explicit '
                           'write requirement/scope; zero-call refusal')
        if invalid:
            reasons.append('--allowedTools contains write rules that cannot be represented '
                           'as an exact ./ profile')
        return {'ok': not reasons, 'write_files': rels, 'expected_write_rules': [],
                'missing_write_rules': [], 'extra_write_rules': [],
                'denied_write_tools': [], 'invalid_write_rules': invalid,
                'reasons': reasons}

    expected_keys = {tool: [_key(rel, win) for rel in rels] for tool in _TOOL_ORDER}
    expected_rules = [f'{tool}(./{rel})' for tool in declared for rel in rels]
    # 未声明的写工具不得携带任何授权（写权限超出任务需求）。
    for tool in _TOOL_ORDER:
        if tool in declared:
            continue
        for rel in actual[tool]:
            rule = f'{tool}(./{rel})'
            extra.append(rule)
            reasons.append(f'--allowedTools grants undeclared write tool rule {rule!r}; '
                           'declare the tool or remove the grant')
    for tool in declared:
        missing_rels = [rel for rel in rels
                        if _key(rel, win) not in {_key(x, win) for x in actual[tool]}]
        extra_rels = [rel for rel in actual[tool]
                      if _key(rel, win) not in set(expected_keys[tool])]
        for rel in missing_rels:
            missing.append(f'{tool}(./{rel})')
            reasons.append(f'required write rule missing from --allowedTools: {tool}(./{rel})')
        for rel in extra_rels:
            extra.append(f'{tool}(./{rel})')
            reasons.append(f'--allowedTools grants a write rule outside the explicit scope: '
                           f'{tool}(./{rel})')

    for rule in disallowed:
        tool = _write_tool_of(rule)
        if tool is None or tool not in declared:
            continue
        if tool not in denied:
            denied.append(tool)
        reasons.append(f'required write tool {tool!r} is explicitly denied by '
                       '--disallowedTools (deny wins)')
        try:
            _parse_exact_rule(rule, tool, win)
        except PermissionProfileError:
            if rule not in invalid:
                invalid.append(rule)

    return {'ok': not reasons, 'write_files': rels,
            'expected_write_rules': expected_rules,
            'missing_write_rules': missing, 'extra_write_rules': extra,
            'denied_write_tools': denied, 'invalid_write_rules': invalid,
            'reasons': reasons}


def validate_read_only_profile(cfg) -> dict:
    """A read-only task must not carry Edit/Write grants without an explicit scope."""
    reasons, invalid = [], []
    allowed = _rule_list(cfg.get('allowed_tools'), 'allowed_tools')
    granted = []
    for rule in allowed:
        tool = _write_tool_of(rule)
        if tool is None:
            continue
        granted.append(rule)
        try:
            _parse_exact_rule(rule, tool, False)
        except PermissionProfileError:
            if rule not in invalid:
                invalid.append(rule)
    if granted:
        reasons.append('--allowedTools grants Edit/Write but the task declares a read-only '
                       'requirement; refusing without an explicit write scope')
    return {'ok': not reasons, 'write_files': [], 'expected_write_rules': [],
            'missing_write_rules': [], 'extra_write_rules': [],
            'denied_write_tools': [], 'invalid_write_rules': invalid,
            'reasons': reasons}
