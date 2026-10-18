"""BW-CLAUDE-PATH-20261018-S2 离线回归：确定性精确 ./ 写授权 profile。

只证配置一致性（不是 OS 沙箱）：workspace + 显式文件清单 → Edit(./rel)/Write(./rel)；
拒绝 C:/ 绝对规则、通配、.. 逃逸、盘符不一致、目录、范围外兄弟文件、裸 Edit/Write；
新文件与最近存在祖先 resolve、大小写/正反斜杠转换、符号链接越界（平台可用时）。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import claude_direct as cd              # noqa: E402
import claude_permissions as cp         # noqa: E402


def _cfg(allowed, tools=('Read', 'Edit', 'Write'), disallowed=None):
    cfg = {'tools': list(tools), 'allowed_tools': list(allowed)}
    if disallowed is not None:
        cfg['disallowed_tools'] = list(disallowed)
    return cfg


class CanonicalFileTests(unittest.TestCase):
    WS = 'C:/ws/project'

    def test_forward_and_backslash_relative_are_equivalent(self):
        self.assertEqual(cp.canonical_relative_file(self.WS, 'scripts/a.py'),
                         'scripts/a.py')
        self.assertEqual(cp.canonical_relative_file(self.WS, 'scripts\\a.py'),
                         'scripts/a.py')
        self.assertEqual(cp.canonical_relative_file(self.WS, './scripts/a.py'),
                         'scripts/a.py')

    def test_absolute_same_drive_inside_workspace_is_relativized(self):
        self.assertEqual(
            cp.canonical_relative_file(self.WS, 'C:/ws/project/scripts/a.py'),
            'scripts/a.py')

    def test_drive_mismatch_is_rejected(self):
        with self.assertRaises(cp.PermissionProfileError):
            cp.canonical_relative_file(self.WS, 'D:/ws/project/scripts/a.py')

    def test_traversal_escape_is_rejected(self):
        for bad in ('../outside.py', 'scripts/../../outside.py',
                    'C:/ws/outside.py', '/etc/passwd'):
            with self.assertRaises(cp.PermissionProfileError, msg=bad):
                cp.canonical_relative_file(self.WS, bad)

    def test_wildcards_and_directories_are_rejected(self):
        for bad in ('scripts/*.py', 'scripts/?.py', 'scripts/[ab].py'):
            with self.assertRaises(cp.PermissionProfileError, msg=bad):
                cp.canonical_relative_file(self.WS, bad)
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(cp.PermissionProfileError):
                cp.canonical_relative_file(d, '.')

    def test_existing_directory_target_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / 'sub'
            target.mkdir()
            with self.assertRaises(cp.PermissionProfileError):
                cp.canonical_relative_file(d, 'sub')

    def test_new_file_under_existing_workspace_is_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                cp.canonical_relative_file(d, 'new/dir/file.py'),
                'new/dir/file.py')

    def test_symlink_escape_is_rejected_when_supported(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / 'ws'
            ws.mkdir()
            outside = Path(d) / 'outside'
            outside.mkdir()
            link = ws / 'link'
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest('symlink creation not available on this host')
            with self.assertRaises(cp.PermissionProfileError):
                cp.canonical_relative_file(str(ws), 'link/file.py')

    def test_build_rules_for_both_write_tools(self):
        rules = cp.build_write_rules(self.WS, ['scripts/b.py', 'scripts/a.py'],
                                     tools=('Read', 'Edit', 'Write'))
        self.assertEqual(rules, [
            'Edit(./scripts/b.py)', 'Edit(./scripts/a.py)',
            'Write(./scripts/b.py)', 'Write(./scripts/a.py)'])


class WriteProfilePreflightTests(unittest.TestCase):
    WS = 'C:/ws/project'

    def test_exact_edit_and_write_scope_passes(self):
        cfg = _cfg(['Read', 'Edit(./a.py)', 'Write(./a.py)'])
        result = cd.preflight_required_permissions(
            cfg, ['Read', 'Edit', 'Write'], write_files=['a.py'], workspace=self.WS)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['expected_write_rules'],
                         ['Edit(./a.py)', 'Write(./a.py)'])

    def test_windows_case_insensitive_rule_matches(self):
        cfg = _cfg(['Read', 'Edit(./Scripts/File.py)'])
        result = cd.preflight_required_permissions(
            cfg, ['Read', 'Edit'], write_files=['scripts/file.py'], workspace=self.WS)
        self.assertTrue(result['ok'], result)

    def test_posix_case_sensitive_rule_does_not_match(self):
        # 纯词法 POSIX workspace（非宿主 tempdir）：Windows 宿主的 tempdir 会被正确识别为
        # Windows profile（大小写不敏感），不能用来验证 POSIX 语义。该正/负例跨平台确定。
        cfg = _cfg(['Read', 'Edit(./Scripts/File.py)'])
        result = cd.preflight_required_permissions(
            cfg, ['Read', 'Edit'], write_files=['scripts/file.py'],
            workspace='/ws/project')
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['missing_write_rules'], ['Edit(./scripts/file.py)'])
        self.assertEqual(result['extra_write_rules'], ['Edit(./Scripts/File.py)'])

    def test_new_file_and_absolute_mapping_remain_positive(self):
        # 保留既有正例：Windows 绝对路径→./ 映射与尚不存在的新目录文件。
        self.assertEqual(
            cp.canonical_relative_file(self.WS, 'C:/ws/project/new/dir/file.py'),
            'new/dir/file.py')
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(cp.canonical_relative_file(d, 'new/dir/file.py'),
                             'new/dir/file.py')

    def test_trailing_separator_is_rejected(self):
        # `newdir/` 不得被剥成新文件 `newdir`。
        for bad in ('newdir/', 'newdir\\', 'scripts/dir/'):
            with self.assertRaises(cp.PermissionProfileError, msg=bad):
                cp.canonical_relative_file(self.WS, bad)

    def test_windows_alias_segments_are_rejected(self):
        # 冒号 ADS、保留设备名、尾空格/点都是 Windows 歧义/非法别名，必须拒绝。
        for bad in ('a.txt:stream', 'NUL', 'nul.txt', 'CON', 'CON.txt',
                    'a.txt.', 'a.txt ', 'scripts/NUL.txt', 'aux.log'):
            with self.assertRaises(cp.PermissionProfileError, msg=bad):
                cp.canonical_relative_file(self.WS, bad)

    def test_absolute_windows_alias_segment_is_rejected(self):
        with self.assertRaises(cp.PermissionProfileError):
            cp.canonical_relative_file(
                self.WS, 'C:/ws/project/logs/file.py:stream')

    def test_unverifiable_resolution_is_rejected_not_best_effort(self):
        # 真实 host 解析抛 PermissionError 时不得 best-effort 返回合法路径。
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(Path, 'resolve',
                                   side_effect=PermissionError('denied')):
                with self.assertRaises(cp.PermissionProfileError):
                    cp.canonical_relative_file(d, 'new/dir/file.py')

    def test_unrelated_sibling_grant_is_an_extra_scope(self):
        cfg = _cfg(['Read', 'Edit(./a.py)', 'Edit(./b.py)'])
        result = cd.preflight_required_permissions(
            cfg, ['Read', 'Edit'], write_files=['a.py'], workspace=self.WS)
        self.assertFalse(result['ok'])
        self.assertEqual(result['extra_write_rules'], ['Edit(./b.py)'])
        self.assertEqual(result['missing_write_rules'], [])

    def test_rule_with_parent_traversal_is_rejected(self):
        cfg = _cfg(['Read', 'Edit(./sub/../a.py)'])
        result = cd.preflight_required_permissions(
            cfg, ['Read', 'Edit'], write_files=['a.py'], workspace=self.WS)
        self.assertFalse(result['ok'])
        self.assertIn('Edit(./sub/../a.py)', result['invalid_rules'])

    def test_backslash_rule_is_normalized(self):
        cfg = _cfg(['Read', 'Edit(.\\scripts\\a.py)'])
        result = cd.preflight_required_permissions(
            cfg, ['Read', 'Edit'], write_files=['scripts/a.py'], workspace=self.WS)
        self.assertTrue(result['ok'], result)

    def test_undeclared_write_tool_rule_is_rejected(self):
        # 未声明 Write，但 cfg 带有 Write(./outside.txt) → 范围外授权必须拒绝。
        cfg = _cfg(['Read', 'Edit(./a.py)', 'Write(./outside.txt)'])
        result = cd.preflight_required_permissions(
            cfg, ['Read', 'Edit'], write_files=['a.py'], workspace=self.WS)
        self.assertFalse(result['ok'], result)
        self.assertIn('Write(./outside.txt)', result['extra_write_rules'])

    def test_write_requirement_without_read_is_rejected(self):
        # 任何写操作必须同时显式声明 Read；只声明 Edit 不得被视为读写任务成立。
        cfg = _cfg(['Edit(./a.py)'])
        result = cd.preflight_required_permissions(
            cfg, ['Edit'], write_files=['a.py'], workspace=self.WS)
        self.assertFalse(result['ok'], result)
        self.assertTrue(any('Read requirement' in r for r in result['reasons']), result)

    def test_write_files_without_write_requirement_is_rejected(self):
        cfg = _cfg(['Read', 'Edit(./a.py)'])
        result = cd.preflight_required_permissions(
            cfg, ['Read'], write_files=['a.py'], workspace=self.WS)
        self.assertFalse(result['ok'])
        self.assertTrue(any('no Edit/Write' in r for r in result['reasons']), result)

    def test_read_only_task_with_write_grants_is_rejected(self):
        cfg = _cfg(['Read', 'Edit(./a.py)'])
        result = cd.preflight_required_permissions(cfg, ['Read'])
        self.assertFalse(result['ok'])
        self.assertTrue(any('read-only' in r for r in result['reasons']), result)

    def test_write_requirement_without_scope_is_rejected(self):
        cfg = _cfg(['Read', 'Edit(./a.py)'])
        result = cd.preflight_required_permissions(
            cfg, ['Read', 'Edit'], write_files=[], workspace=self.WS)
        self.assertFalse(result['ok'])
        self.assertTrue(any('explicit write_files' in r for r in result['reasons']), result)

    def test_absolute_rule_never_matches_exact_scope(self):
        cfg = _cfg(['Read', 'Edit(C:/ws/project/a.py)'])
        result = cd.preflight_required_permissions(
            cfg, ['Read', 'Edit'], write_files=['a.py'], workspace=self.WS)
        self.assertFalse(result['ok'])
        self.assertIn('Edit(C:/ws/project/a.py)', result['invalid_rules'])
        self.assertEqual(result['missing_write_rules'], ['Edit(./a.py)'])


if __name__ == '__main__':
    unittest.main()
