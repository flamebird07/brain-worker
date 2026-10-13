#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BW-ZCODE-FILES-S1 逐文件读写边界契约（FileSystemPort 执行边界的 Python 侧绑定）。

本模块只做**派工前的静态校验与规范化冻结**，不接触 OS 沙箱、不引入服务/DB、不改动官方
SDK。冻结后的规范化契约连同 canonical SHA 写进 request.json；JS 侧（scripts/zcode_file_scope.mjs
+ scripts/zcode_sdk_runner.mjs）用同一棵公开 SDK 树的 createNodeFileSystemAdapter 装饰出一个
受控 FileSystemPort，读只放行登记文件、写只放行精确 writable_files，注入 createZCodeApp 的公开
fileSystemPort。任何缺公开 factory/契约/绑定不一致一律 fail-closed，零模型提交，绝不回退默认宽
文件端口。canonical SHA 口径与 scripts/zcode_permission_broker.mjs 的 canonicalize 完全一致，
两语言共用同一 JSON 规范化（递归排序键、无空格、非 ASCII 原样）。

契约输入 JSON（最小，不多余状态/开关）：
  {
    "workspace": "<绝对工作区路径>",
    "readable_files": ["<工作区相对或绝对路径>", ...],   // 允许明确的外部只读参考
    "writable_files":  ["<工作区相对路径>", ...]           // 必须落在工作区真实目录内
  }

规范化冻结结构（写入 request 的对象，SHA 覆盖它）：
  {
    "workspace": "<normcase(realpath) 工作区>",
    "readable_files": [{"real": "<normcase(realpath)>", "sha256": "<64hex>"}, ...],  // 升序去重
    "writable_files":  [{"real": "...", "sha256": "<64hex 或 null(新文件)"}]           // 升序去重
  }
writable 默认可读：JS 侧读集合 = readable ∪ writable；写集合 = writable。
"""
from __future__ import annotations

import hashlib
import json
import os
import stat

FILE_SCOPE_TOP_KEYS = ('workspace', 'readable_files', 'writable_files')
FILE_SCOPE_ITEM_KEYS = ('real', 'sha256')


def canonical_json(obj) -> str:
    """与 broker.canonicalize / JS JSON.stringify 对齐：递归排序键、无空格、非 ASCII 原样。"""
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def canonical_sha(contract) -> str:
    return hashlib.sha256(canonical_json(contract).encode('utf-8')).hexdigest()


def _normcase(p: str) -> str:
    # Windows 折叠大小写并把分隔符统一为反斜杠（os.path.normcase 语义）；POSIX 保留大小写。
    return os.path.normcase(os.path.normpath(p))


def _path_key(p: str) -> str:
    return _normcase(p)


def _within(root_key: str, key: str) -> bool:
    return key == root_key or key.startswith(root_key + os.sep)


def _resolve_abs(workspace_real: str, declared: str) -> str:
    if os.path.isabs(declared):
        return os.path.normpath(declared)
    return os.path.normpath(os.path.join(workspace_real, declared))


def _is_alias(target_lex: str) -> bool:
    """末段或某祖先组件是否为 symlink/junction/reparse：normpath 与 realpath 折叠 `.`/`..`
    但不解析链接；realpath 解析后不相等即存在别名。目标缺失时 realpath 保留缺失尾段，
    仍能检测已有祖先组件的别名。"""
    try:
        real = os.path.realpath(target_lex)
    except OSError:
        return True
    return _normcase(real) != _normcase(target_lex)


def _nlink_gt_one(path: str) -> bool:
    try:
        return os.stat(path).st_nlink > 1
    except OSError:
        return False


def _normalize_entry(kind, declared, workspace_real, root_key, reasons):
    """把一个声明路径规范化为 {real, sha256}；任一越界/别名/类型/缺失问题写入 reasons。"""
    if not isinstance(declared, str) or not declared or declared != declared.strip():
        reasons.append(f'{kind}: entries must be non-empty untrimmed path strings, '
                       f'got {declared!r}')
        return None
    target_lex = _resolve_abs(workspace_real, declared)
    lex_key = _normcase(target_lex)
    real_path = os.path.realpath(target_lex)
    real_key = _normcase(real_path)

    # 写边界：可写文件必须真实落在工作区目录内；读允许明确外部只读参考，不做 in-workspace 强制。
    if kind == 'writable' and not _within(root_key, real_key):
        reasons.append(f'writable path resolves outside the workspace: {declared!r}')
        return None
    # 别名/reparse 保守拒绝：登记与执行都必须用精确真实路径，不通过链接指向绑定（两侧同拒）。
    if _is_alias(target_lex):
        reasons.append(f'path traverses a symlink/junction/reparse alias: {declared!r}')
        return None

    if os.path.islink(target_lex):
        reasons.append(f'path itself is a symlink: {declared!r}')
        return None

    exists = os.path.lexists(target_lex)
    if not exists:
        if kind == 'readable':
            reasons.append(f'readable file does not exist (nothing to read): {declared!r}')
            return None
        # 新写文件：父目录必须已存在、是真实目录、别名安全、仍在工作区内；拒绝隐式建目录。
        parent = os.path.dirname(target_lex)
        parent_key = _normcase(parent)
        if not _within(root_key, parent_key):
            reasons.append(f'new file parent resolves outside the workspace: {declared!r}')
            return None
        if _is_alias(parent):
            reasons.append(f'new file parent traverses an alias: {declared!r}')
            return None
        if not os.path.isdir(parent):
            reasons.append(f'new file parent directory missing (implicit directory creation '
                           f'is refused): {declared!r}')
            return None
        return {'real': _normcase(os.path.realpath(parent) + os.sep + os.path.basename(target_lex)),
                'sha256': None}

    # 已存在：必须是真实普通文件，不是目录/多硬链接。
    try:
        st = os.stat(real_path)
    except OSError as exc:
        reasons.append(f'cannot stat resolved path: {declared!r}: {exc}')
        return None
    if stat.S_ISDIR(st.st_mode):
        reasons.append(f'path is a directory, not a file: {declared!r}')
        return None
    if not stat.S_ISREG(st.st_mode):
        reasons.append(f'path is not a regular file: {declared!r}')
        return None
    if _nlink_gt_one(real_path):
        reasons.append(f'path has multiple hard links (nlink>1); refusing to bind a shared '
                       f'inode as an exclusive target: {declared!r}')
        return None
    digest = hashlib.sha256(_read_bytes(real_path)).hexdigest()
    return {'real': real_key, 'sha256': digest}


def _read_bytes(path: str) -> bytes:
    with open(path, 'rb') as handle:
        return handle.read()


def normalize_file_scope(contract, workspace) -> tuple:
    """派工前规范化并冻结；返回 (normalized, reasons)。reasons 非空即拒绝，normalized=None。"""
    reasons: list = []
    if not isinstance(contract, dict):
        return None, ['file scope contract must be a JSON object']
    if set(contract) != set(FILE_SCOPE_TOP_KEYS):
        return None, [f'file scope contract top-level keys must be exactly '
                      f'{sorted(FILE_SCOPE_TOP_KEYS)}, got {sorted(contract)}']
    workspace_str = contract['workspace']
    if not isinstance(workspace_str, str) or not workspace_str:
        return None, ['workspace must be a non-empty absolute path string']
    ws_real = os.path.realpath(str(workspace))
    root_key = _normcase(ws_real)
    ws_key = _normcase(os.path.realpath(workspace_str))
    # 契约声明的 workspace 必须与派工工作区解析到同一真实目录（不信任调用方任意改写）。
    if ws_key != root_key:
        return None, [f'contract workspace {workspace_str!r} does not resolve to the dispatch '
                      f'workspace real path ({ws_real!r})']
    out = {}
    for kind, key in (('readable', 'readable_files'), ('writable', 'writable_files')):
        raw = contract[key]
        if not isinstance(raw, list):
            reasons.append(f'{key} must be a list of path strings')
            out[key] = []
            continue
        items = []
        local_reasons = []
        for declared in raw:
            norm = _normalize_entry(kind, declared, ws_real, root_key, local_reasons)
            if norm is not None:
                items.append(norm)
        if local_reasons:
            reasons.extend(local_reasons)
            out[key] = []
            continue
        # 去重 + 升序（按 real），保证规范化 JSON 稳定、SHA 可跨语言重算。
        dedup = {}
        for item in items:
            dedup[item['real']] = item
        out[key] = [dedup[r] for r in sorted(dedup)]
    if reasons:
        return None, reasons
    normalized = {'workspace': root_key,
                  'readable_files': out['readable_files'],
                  'writable_files': out['writable_files']}
    return normalized, []


def plan_edit_coverage_reasons(plan_edits, normalized, actual_workspace) -> list:
    """校验 plan.grants.edits 是否被契约 writable_files 精确覆盖（execution_control 复用同源口径）。

    只接受 `Edit(<工作区相对/绝对单文件>)` 精确形式；通配符、目录、`*`/`?`/`[ ]`、多段或
    无法解析为单文件的表达一律拒绝。每个 edit 的规范化 real 必须命中某个 writable real，且
    writable 集合与 edits 集合双向相等（调用方不能删/加 grants 绕过）。
    """
    reasons = []
    ws_real = os.path.realpath(str(actual_workspace))
    root_key = _normcase(ws_real)
    writable_keys = {e['real'] for e in (normalized.get('writable_files') or [])}
    edit_keys = set()
    for rule in plan_edits:
        if not (isinstance(rule, str) and rule.startswith('Edit(') and rule.endswith(')')):
            reasons.append(f'unsupported Edit form (only Edit(<single file>) is accepted): '
                           f'{rule!r}')
            continue
        inner = rule[len('Edit('):-1]
        if not inner or inner != inner.strip():
            reasons.append(f'Edit target must be a non-empty untrimmed path: {rule!r}')
            continue
        if any(ch in inner for ch in ('*', '?', '[', ']')):
            reasons.append(f'Edit target contains a wildcard (refused): {rule!r}')
            continue
        lex = _resolve_abs(ws_real, inner)
        key = _normcase(os.path.realpath(lex))
        if not _within(root_key, key):
            reasons.append(f'Edit target resolves outside the workspace: {rule!r}')
            continue
        edit_keys.add(key)
    if reasons:
        return reasons
    if edit_keys != writable_keys:
        only_edits = sorted(edit_keys - writable_keys)
        only_files = sorted(writable_keys - edit_keys)
        reasons.append('file-scope writable set does not exactly cover plan edits '
                       f'(uncovered edits={only_edits}, extra writable not in plan={only_files})')
    return reasons
