"""PROMO-VERIFY-A1 2026-11-09 缺陷修复离线回归（零网络、零真实 CLI/凭据/共享库）。

覆盖五组修复：
- A 组：effective_permission_rules 对空 --tools 可见性 + 非空授权规则的矛盾检测（Python 层），
  以及正式入口 CLI 端到端受控拒绝（rc=2、sent=false、零输出目录、零 Popen）；纯 argv 构造器
  build_argv 保持忠实构造（既有离线回归钉死的形状）。
- B 组：_qoder_failure_facts 的 tools_channel_disabled 事实与 ec.diagnose 映射（假成功空转）。
- C 组：qoder_permissions._validate_bash 首 token 最小形状校验（拒绝自然语言描述，参数位置
  中文路径与成对双引号首 token 照常放行）。
- D 组：stage_selection authorization 可选 task_profile 字段（只回显与提示，不排除候选）。

复用 tests/stub_qodercli.py 既有离线基建与 tests/test_stage_selection.py 的临时 SQLite 夹具。
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SCRIPTS = REPO / 'scripts'
for p in (str(SCRIPTS), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import execution_control as ec  # noqa: E402
import qoder_permissions as qp  # noqa: E402
import test_stage_selection as tss  # noqa: E402  复用临时 SQLite/mock 夹具


def _load_qd():
    spec = importlib.util.spec_from_file_location(
        'qoder_direct_promo_verify_under_test', SCRIPTS / 'qoder_direct.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


qd = _load_qd()


class ToolsVisibilityContradictionTests(unittest.TestCase):
    """A 组：空 --tools 可见性与非空授权规则矛盾。"""

    def test_python_empty_tools_with_rules_raises(self):
        for rules in (['Read'], ['Read', 'Bash(echo hi)']):
            with self.subTest(rules=rules):
                with self.assertRaises(ValueError) as ctx:
                    qd.effective_permission_rules(tools='', allowed_tools=rules)
                msg = str(ctx.exception)
                self.assertIn('Empty disables tools visibility', msg)
                self.assertIn('allowed_tools', msg)
                self.assertIn('--task-scope', msg)
                self.assertIn(str(len(rules)), msg)

    def test_python_empty_tools_explicit_empty_allowed_ok(self):
        r = qd.effective_permission_rules(tools='', allowed_tools=[])
        self.assertEqual(r['allowed_tools'], [])

    def test_python_empty_tools_fallback_ok(self):
        r = qd.effective_permission_rules(tools='', allowed_tools=None)
        self.assertEqual(r['allowed_tools'], [])

    def test_python_nonempty_tools_with_rules_ok(self):
        r = qd.effective_permission_rules(tools='Read', allowed_tools=['Read'])
        self.assertEqual(r['allowed_tools'], ['Read'])

    def test_python_none_tools_with_rules_not_contradiction(self):
        # tools=None 表示未显式给可见性参数，非“显式空字符串”，不触发矛盾检测。
        r = qd.effective_permission_rules(tools=None, allowed_tools=['Read'])
        self.assertEqual(r['allowed_tools'], ['Read'])

    def test_build_argv_stays_faithful_constructor(self):
        # 纯 argv 构造器形状由既有离线回归钉死：矛盾防御在 main() 入口生效，构造器忠实构造。
        cfg = {'node': '/abs/node', 'qodercli': '/abs/qodercli.js'}
        argv = qd.build_argv(cfg, '/abs/ws', 'Qwen3.8-Flash', '', None,
                             allowed_tools=['Edit', 'Write', 'Bash'])
        self.assertIn('Edit', argv)
        self.assertEqual(argv[argv.index('--tools') + 1], '')

    def test_cli_missing_tools_with_allowed_rules_rejected(self):
        with tempfile.TemporaryDirectory(prefix='bw-promo-verify-a1-') as tmp:
            base = Path(tmp)
            ws = base / 'ws'
            ws.mkdir()
            pf = base / 'prompt.txt'
            pf.write_text('PROMO-VERIFY-A1 矛盾参数离线用例', encoding='utf-8')
            cfg = base / 'entry.json'
            cfg.write_text(json.dumps({'node': sys.executable,
                                       'qodercli': str(HERE / 'stub_qodercli.py')}),
                           encoding='utf-8')
            out = base / 'out-never-created'
            store = base / 'pool.sqlite3'
            env = os.environ.copy()
            env['BRAIN_WORKER_DISPATCH_STORE'] = str(store)
            cmd = [sys.executable, str(SCRIPTS / 'qoder_direct.py'),
                   '--workspace', str(ws), '--prompt-file', str(pf),
                   '--output-dir', str(out), '--config', str(cfg),
                   '--stage', 'BW-PROMO-VERIFY-FIX', '--model', 'Qwen3.8-Flash',
                   '--dispatch-store', str(store),
                   '--allowed-tools', 'Read', '--allowed-tools', 'Bash(echo hi)']
            r = subprocess.run(cmd, capture_output=True, env=env, timeout=120)
            self.assertEqual(r.returncode, 2,
                             f'rc={r.returncode}, stdout={r.stdout!r}, stderr={r.stderr!r}')
            printed = json.loads(r.stdout.decode('utf-8'))
            self.assertTrue(printed['permission_args_rejected'], printed)
            self.assertFalse(printed['sent'], printed)
            self.assertEqual(printed['exit_code'], 2, printed)
            self.assertTrue(printed['reasons'], printed)
            self.assertIn('Empty disables tools visibility', ' '.join(printed['reasons']))
            self.assertEqual(printed['tools'], '', printed)
            self.assertEqual(printed['allowed_tools'], ['Read', 'Bash(echo hi)'], printed)
            # 拒绝发生在容量 claim 与建目录之前：零输出目录、零 Popen、零容量占位库。
            self.assertFalse(out.exists())
            self.assertFalse(store.exists())


class ToolsChannelDisabledFactTests(unittest.TestCase):
    """B 组：工具通道禁用空转的假成功分型。"""

    def test_fact_true_on_false_success_spin(self):
        facts = qd._qoder_failure_facts(
            {'protocol_success': True, 'num_turns': 1,
             'model_emitted_toolcall_text': True})
        self.assertIs(facts['tools_channel_disabled'], True)
        self.assertEqual(facts['evidence']['num_turns'], 1)

    def test_fact_false_when_turns_above_two(self):
        facts = qd._qoder_failure_facts(
            {'protocol_success': True, 'num_turns': 3,
             'model_emitted_toolcall_text': True})
        self.assertIs(facts['tools_channel_disabled'], False)

    def test_fact_false_without_toolcall_marker(self):
        for summary in ({'protocol_success': True, 'num_turns': 1,
                         'model_emitted_toolcall_text': False},
                        {'protocol_success': True, 'num_turns': 1}):
            with self.subTest(summary=summary):
                facts = qd._qoder_failure_facts(summary)
                self.assertIs(facts['tools_channel_disabled'], False)

    def test_fact_false_on_protocol_failure(self):
        facts = qd._qoder_failure_facts(
            {'protocol_success': False, 'num_turns': 1,
             'model_emitted_toolcall_text': True})
        self.assertIs(facts['tools_channel_disabled'], False)

    def test_fact_false_when_num_turns_not_int(self):
        facts = qd._qoder_failure_facts(
            {'protocol_success': True, 'num_turns': '1',
             'model_emitted_toolcall_text': True})
        self.assertIs(facts['tools_channel_disabled'], False)

    def test_failure_type_registered_and_diagnosed(self):
        self.assertIn('tools_channel_disabled', ec.FAILURE_TYPES)
        facts = qd._qoder_failure_facts(
            {'protocol_success': True, 'num_turns': 1,
             'model_emitted_toolcall_text': True})
        diag = ec.diagnose(facts)
        self.assertIn('tools_channel_disabled', diag['failure_types'])
        self.assertIn('tools_channel_disabled',
                      ec.diagnose({'tools_channel_disabled': True})['failure_types'])

    def test_existing_failure_types_still_coexist(self):
        # 既有失败终态分型不被新类型挤掉：解析失败仍映射 protocol_parse_failure。
        facts = qd._qoder_failure_facts(
            {'protocol_success': False, 'parse_error': 'bad json', 'num_turns': 1})
        diag = ec.diagnose(facts)
        self.assertIn('protocol_parse_failure', diag['failure_types'])
        self.assertNotIn('tools_channel_disabled', diag['failure_types'])


class BashFirstTokenShapeTests(unittest.TestCase):
    """C 组：scope.bash 首 token 最小形状校验。"""

    def test_natural_language_description_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            qp.build_permissions({'bash': ['只读查看进程与端口（tasklist/netstat）']})
        msg = str(ctx.exception)
        self.assertIn('executable name', msg)
        self.assertIn('verbatim', msg)

    def test_ascii_parens_first_token_rejected(self):
        with self.assertRaises(ValueError):
            qp.build_permissions({'bash': ['查看进程(tasklist)']})

    def test_pure_chinese_first_token_rejected(self):
        with self.assertRaises(ValueError):
            qp.build_permissions({'bash': ['查看进程']})

    def test_legal_ascii_exe_with_chinese_arg_path(self):
        cmd = 'type "C:/用户/文件.txt"'
        data = qp.build_permissions({'bash': [cmd]})
        self.assertIn(f'Bash({cmd})', data['allowed_tools'])
        self.assertIn('Bash', data['visible_tools'])

    def test_legal_quoted_ascii_first_token(self):
        cmd = '"node" --version'
        data = qp.build_permissions({'bash': [cmd]})
        self.assertIn(f'Bash({cmd})', data['allowed_tools'])

    def test_legal_python_command_with_parens_in_arg_position(self):
        cmd = 'python -c "print(1)"'
        data = qp.build_permissions({'bash': [cmd]})
        self.assertIn(f'Bash({cmd})', data['allowed_tools'])

    def test_legal_unittest_command(self):
        cmd = 'python -m unittest tests.test_x'
        data = qp.build_permissions({'bash': [cmd]})
        self.assertIn(f'Bash({cmd})', data['allowed_tools'])


class TaskProfileTests(tss._Base):
    """D 组：authorization 可选 task_profile（只回显与提示，preview 不 claim）。"""

    def setUp(self):
        super().setUp()
        self.build_store()
        self.allowed = ['qoder:Qwen3.8-Flash', 'qodercn:Qwen3.8-Flash']

    def test_exploratory_with_qodercn_echoes_note(self):
        r = self.plan(self.auth('auto', 'T', 's2', self.allowed,
                                task_profile='exploratory'))
        self.assertEqual(r['task_profile'], 'exploratory', r)
        self.assertIn('task_profile_note', r)
        self.assertIn('qodercn', r['task_profile_note'])
        self.assertIn('PROMO-VERIFY-A1', r['task_profile_note'])

    def test_absent_field_adds_no_new_keys(self):
        r = self.plan(self.auth('auto', 'T', 's2', self.allowed))
        self.assertNotIn('task_profile', r)
        self.assertNotIn('task_profile_note', r)

    def test_non_string_refused(self):
        r = self.plan(self.auth('auto', 'T', 's2', self.allowed, task_profile=123))
        self.assertEqual(r['refused'], 'invalid_task_profile', r)

    def test_exploratory_without_qodercn_no_note(self):
        r = self.plan(self.auth('auto', 'T', 's2', ['qoder:Qwen3.8-Flash'],
                                task_profile='exploratory'))
        self.assertEqual(r['task_profile'], 'exploratory', r)
        self.assertNotIn('task_profile_note', r)

    def test_non_exploratory_string_echoed_without_note(self):
        r = self.plan(self.auth('auto', 'T', 's2', self.allowed,
                                task_profile='fixed_file'))
        self.assertEqual(r['task_profile'], 'fixed_file', r)
        self.assertNotIn('task_profile_note', r)


if __name__ == '__main__':
    unittest.main()