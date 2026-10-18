#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Qoder 精确权限 helper 的离线回归（纯函数 + 小 CLI，零网络、零模型、零派工）。

覆盖：
- 中文/空格/Windows 反斜杠的相对路径规范化与 Edit(/路径) 规则；
- 始终 Read/Glob/Grep；有 edits 才可见 Edit+Write，allow 只出现 Edit(...)；
  不生成 Write(...)、裸 Edit、裸 Write；
- Bash 原样保留（含括号、逗号），不追加 cd/echo/包装；
- read_dirs 原样传递；缺省三列表为空；
- 路径逃逸/绝对/盘符/通配/破坏规则/控制字符与畸形 schema/未知键/错误类型全部拒绝；
- 同一输出喂 grants_from_rules 与 preflight，精确 plan/actual 通过；计划丢 edits
  或 actual 缺 allow 被既有 preflight 拒绝；
- 输入 scope 不被修改；CLI 只读临时 scope 文件并打印 JSON，不创建其它文件。
"""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPTS = ROOT / 'scripts'
sys.path.insert(0, str(SCRIPTS))

import execution_control as ec  # noqa: E402
import qoder_permissions as qp  # noqa: E402


def make_plan(output, edits=None):
    return {
        'task_id': 'T-QODER-PERMS-OFFLINE',
        'stage': 'BW-QODER-PERMISSIONS-20261018-S2',
        'runtime': 'qoder',
        'model': 'Qwen3.8-Max',
        'workspace': 'C:/synthetic/ws',
        'cwd': 'C:/synthetic/ws',
        'prompt_sha256': 'a' * 64,
        'grants': {
            'edits': list(output['grants']['edits'] if edits is None else edits),
            'bash': list(output['grants']['bash']),
            'read_dirs': list(output['grants']['read_dirs']),
        },
        'tool_visibility': list(output['grants']['tool_visibility']),
        'visible_tools': list(output['visible_tools']),
        'allowed_tools': list(output['allowed_tools']),
        'disallowed_tools': list(output['disallowed_tools']),
    }


def make_actual(output, plan):
    return {
        'task_id': plan['task_id'],
        'stage': plan['stage'],
        'runtime': plan['runtime'],
        'model': plan['model'],
        'workspace': plan['workspace'],
        'cwd': plan['cwd'],
        'prompt_sha256': plan['prompt_sha256'],
        'argv': ['node', 'qodercli', '-p'],
        'shell': False,
        'grants': ec.grants_from_rules(
            output['allowed_tools'], output['add_dirs'],
            output['disallowed_tools'], output['visible_tools']),
    }


class PathAndRuleShapeTests(unittest.TestCase):

    def test_chinese_space_and_windows_separator_normalized(self):
        out = qp.build_permissions(
            {'edits': ['scripts\\子目录\\我的 文件.py']})
        self.assertEqual(out['grants']['edits'],
                         ['Edit(/scripts/子目录/我的 文件.py)'])
        self.assertIn('Edit', out['visible_tools'])
        self.assertIn('Write', out['visible_tools'])

    def test_no_write_path_rule_and_no_bare_mutation_allow(self):
        out = qp.build_permissions({'edits': ['a.py', 'tests/test_b.py']})
        self.assertNotIn('Edit', out['allowed_tools'])
        self.assertNotIn('Write', out['allowed_tools'])
        self.assertFalse(any(r.startswith('Write(') for r in out['allowed_tools']))
        self.assertEqual(out['grants']['edits'],
                         ['Edit(/a.py)', 'Edit(/tests/test_b.py)'])

    def test_empty_scope_grants_only_read_visibility(self):
        out = qp.build_permissions({})
        self.assertEqual(out['visible_tools'], ['Read', 'Glob', 'Grep'])
        self.assertEqual(out['allowed_tools'], ['Read', 'Glob', 'Grep'])
        self.assertEqual(out['disallowed_tools'], [])
        self.assertEqual(out['add_dirs'], [])
        self.assertEqual(out['grants']['edits'], [])
        self.assertEqual(out['grants']['bash'], [])
        self.assertEqual(out['grants']['read_dirs'], [])
        self.assertEqual(out['grants']['tool_visibility'], ['Read', 'Glob', 'Grep'])

    def test_missing_lists_are_empty(self):
        out = qp.build_permissions({'edits': ['x.py']})
        self.assertEqual(out['grants']['bash'], [])
        self.assertEqual(out['grants']['read_dirs'], [])

    def test_bash_is_verbatim_with_parentheses_and_comma(self):
        command = 'node --test "tests/a b.test.js" && echo "(done), 逗号"'
        out = qp.build_permissions({'bash': [command]})
        self.assertEqual(out['grants']['bash'], [f'Bash({command})'])
        self.assertEqual(out['visible_tools'], ['Read', 'Glob', 'Grep', 'Bash'])
        self.assertNotIn('cd ', out['grants']['bash'][0])

    def test_bash_is_not_wrapped_or_rewritten(self):
        command = 'python -m unittest discover -s tests -p test_*.py'
        out = qp.build_permissions({'bash': [command]})
        rule = out['grants']['bash'][0]
        self.assertEqual(rule, f'Bash({command})')
        self.assertNotIn('/c ', rule)
        self.assertNotIn('sh -c', rule)

    def test_read_dirs_passed_through(self):
        dirs = ['C:\\参考 资料', 'D:/external/notes']
        out = qp.build_permissions({'read_dirs': dirs})
        self.assertEqual(out['add_dirs'], dirs)
        self.assertEqual(out['grants']['read_dirs'], dirs)

    def test_input_scope_is_not_mutated(self):
        scope = {'edits': ['scripts\\我的 文件.py'],
                 'bash': ['python -m unittest tests.test_x'],
                 'read_dirs': ['C:\\refs']}
        before = copy.deepcopy(scope)
        qp.build_permissions(scope)
        self.assertEqual(scope, before)


class RejectionTests(unittest.TestCase):

    def test_rejects_non_relative_escaping_and_wildcard_paths(self):
        bad = [
            '', '   ', ' /abs.py', '/abs.py', 'C:\\tmp\\a.py', 'C:/tmp/a.py',
            '\\\\server\\share\\a.py', '../a.py', 'a/../../b.py', 'a/./b.py',
            './a.py', 'a//b.py', 'a/', 'scripts/*.py', 'scripts/?.py',
            'scripts/(x).py', 'scripts/a.py\n', 'scripts/\x00a.py',
            ' scripts/a.py', 'scripts/a.py ',
        ]
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    qp.build_permissions({'edits': [value]})

    def test_rejects_malformed_schema_and_unknown_keys(self):
        cases = [
            [], 'x', None, {'edits': 'a.py'}, {'bash': 'echo x'},
            {'read_dirs': 'C:/refs'}, {'edits': [1]}, {'bash': [None]},
            {'read_dirs': [True]}, {'unknown': []},
            {'edits': ['a.py'], 'extra': []},
        ]
        for scope in cases:
            with self.subTest(scope=scope):
                with self.assertRaises(ValueError):
                    qp.build_permissions(scope)

    def test_rejects_empty_or_multiline_bash(self):
        for command in ['', '   ', 'a\nb', 'a\rb', 'a\x00b']:
            with self.subTest(command=command):
                with self.assertRaises(ValueError):
                    qp.build_permissions({'bash': [command]})

    def test_rejects_bad_read_dirs(self):
        for value in ['', '   ', 1, 'a\nb']:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    qp.build_permissions({'read_dirs': [value]})


class PreflightIntegrationTests(unittest.TestCase):

    def _scope(self):
        # 客户报告里的 17 文件 case 缩成 2 个合成文件，证明 grants 非空且可精确比对。
        return {'edits': ['scripts\\模块\\我的 文件.py', 'tests/test_x.py'],
                'bash': ['python -m unittest tests.test_x'],
                'read_dirs': []}

    def test_exact_plan_and_actual_pass_preflight(self):
        out = qp.build_permissions(self._scope())
        plan = make_plan(out)
        actual = make_actual(out, plan)
        self.assertGreaterEqual(len(plan['grants']['edits']), 2)
        self.assertGreaterEqual(len(plan['grants']['bash']), 1)
        result = ec.preflight(plan, actual)
        self.assertTrue(result['ok'], result['reasons'])
        self.assertFalse(result['sent'])

    def test_plan_missing_edits_is_rejected(self):
        out = qp.build_permissions(self._scope())
        plan = make_plan(out, edits=[])
        actual = make_actual(out, plan)
        result = ec.preflight(plan, actual)
        self.assertFalse(result['ok'])
        self.assertTrue(any('grants.edits' in r and 'unplanned' in r
                            for r in result['reasons']), result['reasons'])

    def test_actual_missing_allow_is_rejected(self):
        out = qp.build_permissions(self._scope())
        plan = make_plan(out)
        actual = make_actual(out, plan)
        # 实际 argv 侧少了精确 allow：只按 Read/Glob/Grep 构造，模拟丢了 Edit/Bash 规则。
        actual['grants'] = ec.grants_from_rules(
            ['Read', 'Glob', 'Grep'], [], [], ['Read', 'Glob', 'Grep'])
        actual['visible_tools'] = ['Read', 'Glob', 'Grep']
        actual['allowed_tools'] = ['Read', 'Glob', 'Grep']
        result = ec.preflight(plan, actual)
        self.assertFalse(result['ok'])
        self.assertTrue(any('missing planned' in r for r in result['reasons']),
                        result['reasons'])


class CliTests(unittest.TestCase):

    def test_cli_reads_scope_and_prints_json_without_extra_files(self):
        scope = {'edits': ['scripts\\我的 文件.py'],
                 'bash': ['python -m unittest tests.test_x'], 'read_dirs': []}
        with tempfile.TemporaryDirectory() as tmp:
            scope_path = Path(tmp) / 'scope.json'
            scope_path.write_text(json.dumps(scope, ensure_ascii=False),
                                  encoding='utf-8')
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / 'qoder_permissions.py'),
                 '--scope', str(scope_path)],
                capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout), qp.build_permissions(scope))
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ['scope.json'])

    def test_cli_rejects_invalid_scope_with_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            scope_path = Path(tmp) / 'scope.json'
            scope_path.write_text(json.dumps({'edits': ['../escape.py']}),
                                  encoding='utf-8')
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / 'qoder_permissions.py'),
                 '--scope', str(scope_path)],
                capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(proc.returncode, 2)
            payload = json.loads(proc.stdout)
            self.assertFalse(payload['ok'])
            self.assertIn('canonical relative path', payload['error'])


if __name__ == '__main__':
    unittest.main()
