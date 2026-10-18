"""BW-PERMISSION-CONFIG-20261018-S1 离线回归：显式 required_tools 启动前核对。

只证离线契约：主脑显式声明的工具需求对照真实 --tools/--allowedTools/--disallowedTools
逐项核对，拒绝发生在凭据读取/容量 consume/Popen 之前（零模型、零占位、不读 token）；
未声明 required_tools 保持旧调用兼容。路径限定 Edit(<path>) 规则是正例；显式 deny
优先于 allow。绝不从自然语言猜写意图、不自动加授权，也不是 OS 沙箱。
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import claude_direct as cd      # noqa: E402
import dispatch_pool as dp      # noqa: E402
import prompt_contract as pc    # noqa: E402

OFFPEAK = datetime(2026, 10, 8, 15, 0, 0, tzinfo=timezone.utc)
SECRET = 'sk-PREFLIGHT-SECRET-DO-NOT-LEAK'


def _cfg(**over):
    base = {'executable': 'C:/abs/claude.exe', 'model': 'deepseek-flash',
            'tools': ['Read'], 'allowed_tools': ['Read'], 'setting_sources': [],
            'credential_source': {'mode': 'token_env', 'env': 'ANTHROPIC_API_KEY'}}
    base.update(over)
    return base


def _report_body(stage, workspace):
    lines = [pc.REPORT_START,
             f'阶段编号与执行方式：{stage}；direct。',
             f'实际项目绝对路径：{workspace}',
             '汇报时间与执行环境：offline stub']
    for h in pc.SECTION_HEADERS:
        lines.append(f'{h}\n- 字段：值')
    lines.append(pc.CLOSING_LINE)
    lines.append(pc.REPORT_END)
    return '\n'.join(lines)


class FakePopen:
    def __init__(self, stdout, sink):
        self._stdout, self.sink = stdout, sink
        self.argv = self.env = None

    def __call__(self, argv, *, cwd=None, stdin=None, stdout=None, stderr=None,
                 env=None, text=None):
        self.argv, self.env = list(argv), env
        self.sink.append(self)
        return self

    def communicate(self, input=None):
        self.returncode = 0
        return self._stdout, ''


class PurePreflightTests(unittest.TestCase):
    def test_path_limited_edit_rule_is_a_positive(self):
        result = cd.preflight_required_permissions(
            _cfg(tools=['Read', 'Edit'],
                 allowed_tools=['Read', 'Edit(/abs/workspace/out.txt)']),
            ['Edit'])
        self.assertTrue(result['ok'], result)

    def test_visible_but_not_allowed_is_rejected(self):
        result = cd.preflight_required_permissions(
            _cfg(tools=['Read', 'Edit'], allowed_tools=['Read']), ['Edit'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['missing_allowed'], ['Edit'])
        self.assertEqual(result['missing_visible'], [])

    def test_not_visible_is_rejected(self):
        result = cd.preflight_required_permissions(
            _cfg(tools=['Read'], allowed_tools=['Read', 'Edit(/x)']), ['Edit'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['missing_visible'], ['Edit'])

    def test_explicit_deny_beats_allow(self):
        result = cd.preflight_required_permissions(
            _cfg(tools=['Read', 'Edit'],
                 allowed_tools=['Read', 'Edit(/x)'], disallowed_tools=['Edit']),
            ['Edit'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['denied'], ['Edit'])
        self.assertTrue(any('explicitly denied' in r for r in result['reasons']))

    def test_deny_with_path_rule_also_denies_base_tool(self):
        result = cd.preflight_required_permissions(
            _cfg(tools=['Edit'], allowed_tools=['Edit'],
                 disallowed_tools=['Edit(/x)']), ['Edit'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['denied'], ['Edit'])

    def test_read_only_requirement_is_not_hurt(self):
        result = cd.preflight_required_permissions(
            _cfg(tools=['Read', 'Grep', 'Glob'],
                 allowed_tools=['Read', 'Grep', 'Glob']), ['Read'])
        self.assertTrue(result['ok'], result)

    def test_unsupported_complex_rule_is_not_treated_as_grant(self):
        result = cd.preflight_required_permissions(
            _cfg(tools=['Edit'], allowed_tools=['Edit*']), ['Edit'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['missing_allowed'], ['Edit'])

    # ---------------- BW-PERMISSION-CONFIG-S2：畸形规则精确负例 ----------------

    def test_extra_close_paren_allow_rule_is_not_a_grant(self):
        malformed = 'Edit(/file.py))'
        self.assertIsNone(cd._rule_base_tool(malformed))
        result = cd.preflight_required_permissions(
            _cfg(tools=['Edit'], allowed_tools=[malformed]), ['Edit'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['missing_allowed'], ['Edit'])
        self.assertEqual(result['denied'], [])

    def test_empty_tool_name_and_empty_arg_rules_are_not_grants(self):
        for malformed in ('(/file.py)', 'Edit()', 'Edit( )', 'Edit((/x))', 'Edit(/x'):
            self.assertIsNone(cd._rule_base_tool(malformed), malformed)
        result = cd.preflight_required_permissions(
            _cfg(tools=['Edit'], allowed_tools=['(/file.py)']), ['Edit'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['missing_allowed'], ['Edit'])

    def test_malformed_deny_rule_for_required_tool_is_not_ignored(self):
        # 无法解释的拒绝规则不得静默忽略后让宽 allow 通过：保守拒绝并出声。
        malformed = 'Edit(/file.py))'
        result = cd.preflight_required_permissions(
            _cfg(tools=['Edit'], allowed_tools=['Edit'],
                 disallowed_tools=[malformed]), ['Edit'])
        self.assertFalse(result['ok'])
        self.assertIn(malformed, result['invalid_rules'])
        self.assertTrue(any('malformed' in r for r in result['reasons']))

    def test_legal_path_rule_remains_positive(self):
        self.assertEqual(cd._rule_base_tool('Edit(/abs/out.txt)'), 'Edit')
        result = cd.preflight_required_permissions(
            _cfg(tools=['Edit'], allowed_tools=['Edit(/abs/out.txt)']), ['Edit'])
        self.assertTrue(result['ok'], result)

    def test_unrelated_legal_bash_parentheses_do_not_break_read(self):
        # Bash 命令参数里的括号不是 shell 语法解析目标；未声明 Bash 需求时不影响 Read。
        cfg = _cfg(tools=['Read', 'Bash'],
                   allowed_tools=['Read', 'Bash(git log --format="%h (parents)")'])
        result = cd.preflight_required_permissions(cfg, ['Read'])
        self.assertTrue(result['ok'], result)

    def test_required_bash_with_nested_parens_is_conservatively_rejected(self):
        # 声明 Bash 需求时承认边界：不解析命令，无法可靠解释的规则保守不算授权。
        result = cd.preflight_required_permissions(
            _cfg(tools=['Bash'], allowed_tools=['Bash(echo (x))']), ['Bash'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['missing_allowed'], ['Bash'])

    def test_undeclared_requirement_is_skipped_for_compatibility(self):
        result = cd.preflight_required_permissions(_cfg(), None)
        self.assertTrue(result['ok'])
        self.assertTrue(result['skipped'])

    def test_blank_declaration_is_an_error(self):
        with self.assertRaises(ValueError):
            cd.normalize_required_tools('')
        with self.assertRaises(ValueError):
            cd.normalize_required_tools(['Read', ''])

    def test_normalize_accepts_csv_and_dedupes(self):
        self.assertEqual(cd.normalize_required_tools('Read, Edit,Read'),
                         ['Read', 'Edit'])


class LifecyclePreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.store = self.base / 'dispatch.sqlite3'
        self.ws = self.base / 'ws'
        self.ws.mkdir()
        self.prompt = self.base / 'prompt.txt'
        self.prompt.write_text('do the task', encoding='utf-8')
        self.out = self.base / 'out'
        self.sink = []
        p = mock.patch.object(dp, 'process_identity',
                              return_value={'state': 'alive', 'created': 'c1'})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def _run(self, fake, required_tools, *, cfg=None, environ=None):
        import hashlib
        return cd.run(
            cfg or _cfg(), workspace=str(self.ws), prompt_file=self.prompt,
            out_dir=self.out, stage='BW-PERMISSION-CONFIG-S1', task_id='t1',
            prompt_sha256=hashlib.sha256(self.prompt.read_bytes()).hexdigest(),
            now=OFFPEAK, popen_factory=fake,
            environ={'ANTHROPIC_API_KEY': SECRET} if environ is None else environ,
            dispatch_store=self.store, bind_child_pid=False,
            required_tools=required_tools)

    def _success_stream(self, tool='Read'):
        body = _report_body('BW-PERMISSION-CONFIG-S1', str(self.ws))
        events = [
            {'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'name': tool, 'id': 't1'}]}},
            {'type': 'user', 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': 't1'}]}},
            {'type': 'result', 'subtype': 'success', 'is_error': False,
             'session_id': 's1', 'result': body}]
        return '\n'.join(json.dumps(e) for e in events)

    def test_declared_write_without_grant_zero_popen_zero_token_zero_claim(self):
        fake = FakePopen(self._success_stream(), self.sink)
        claim = mock.Mock(side_effect=AssertionError('capacity must not be consumed'))
        with mock.patch.object(cd, 'resolve_token',
                               side_effect=AssertionError('token must not be read')), \
             mock.patch.object(dp, 'consume_for_entry', claim):
            out = self._run(fake, ['Edit'])
        self.assertFalse(out['sent'])
        self.assertFalse(out['dispatched'])
        self.assertEqual(out['reason'], 'required_tools_not_granted')
        self.assertIn('Edit', out['permission_preflight']['missing_allowed'])
        self.assertEqual(self.sink, [])  # 零 Popen
        claim.assert_not_called()
        self.assertEqual(
            dp.status(self.store, now=OFFPEAK)['pools']['claude:deepseek-flash']['active'], 0)
        self.assertNotIn(SECRET, json.dumps(out))

    def test_explicit_deny_zero_popen(self):
        fake = FakePopen(self._success_stream('Edit'), self.sink)
        cfg = _cfg(tools=['Read', 'Edit'],
                   allowed_tools=['Read', 'Edit(/out/x.txt)'],
                   disallowed_tools=['Edit'])
        with mock.patch.object(cd, 'resolve_token',
                               side_effect=AssertionError('token must not be read')):
            out = self._run(fake, ['Edit'], cfg=cfg)
        self.assertEqual(out['reason'], 'required_tools_not_granted')
        self.assertEqual(out['permission_preflight']['denied'], ['Edit'])
        self.assertEqual(self.sink, [])

    def test_read_only_requirement_reaches_popen(self):
        fake = FakePopen(self._success_stream('Read'), self.sink)
        out = self._run(fake, ['Read'])
        self.assertTrue(out['sent'], out)
        self.assertEqual(len(self.sink), 1)
        self.assertNotIn(SECRET, ' '.join(fake.argv))

    def test_path_limited_edit_requirement_reaches_popen(self):
        fake = FakePopen(self._success_stream('Edit'), self.sink)
        cfg = _cfg(tools=['Read', 'Edit'],
                   allowed_tools=['Read', 'Edit(/abs/workspace/out.txt)'])
        out = self._run(fake, ['Edit'], cfg=cfg)
        self.assertTrue(out['sent'], out)
        self.assertEqual(len(self.sink), 1)

    def test_undeclared_requirement_keeps_old_calls_working(self):
        fake = FakePopen(self._success_stream('Read'), self.sink)
        out = self._run(fake, None)
        self.assertTrue(out['sent'], out)
        self.assertEqual(len(self.sink), 1)

    def test_malformed_allow_rule_zero_popen_zero_token(self):
        fake = FakePopen(self._success_stream('Edit'), self.sink)
        cfg = _cfg(tools=['Edit'], allowed_tools=['Edit(/file.py))'])
        with mock.patch.object(cd, 'resolve_token',
                               side_effect=AssertionError('token must not be read')):
            out = self._run(fake, ['Edit'], cfg=cfg)
        self.assertEqual(out['reason'], 'required_tools_not_granted')
        self.assertEqual(out['permission_preflight']['missing_allowed'], ['Edit'])
        self.assertEqual(self.sink, [])

    def test_malformed_deny_rule_zero_popen_zero_token(self):
        fake = FakePopen(self._success_stream('Edit'), self.sink)
        cfg = _cfg(tools=['Edit'], allowed_tools=['Edit'],
                   disallowed_tools=['Edit(/file.py))'])
        with mock.patch.object(cd, 'resolve_token',
                               side_effect=AssertionError('token must not be read')):
            out = self._run(fake, ['Edit'], cfg=cfg)
        self.assertEqual(out['reason'], 'required_tools_not_granted')
        self.assertIn('Edit(/file.py))', out['permission_preflight']['invalid_rules'])
        self.assertEqual(self.sink, [])

    # ---------------- BW-PERMISSION-CONFIG-S2：request/return 最小回执 ----------------

    def test_required_tools_enter_request_and_return_evidence(self):
        fake = FakePopen(self._success_stream('Read'), self.sink)
        out = self._run(fake, ['Read'])
        self.assertTrue(out['sent'], out)
        self.assertEqual(out['required_tools'], ['Read'])
        self.assertEqual(out['permission_preflight']['required_tools'], ['Read'])
        self.assertFalse(out['permission_preflight']['skipped'])
        self.assertTrue(out['permission_preflight']['ok'])
        req = json.loads(Path(out['evidence']['request_path']).read_text(encoding='utf-8'))
        self.assertEqual(req['required_tools'], ['Read'])
        self.assertEqual(req['permission_preflight']['required_tools'], ['Read'])
        self.assertFalse(req['permission_preflight']['skipped'])
        self.assertNotIn(SECRET, json.dumps(req))

    def test_undeclared_required_tools_is_explicitly_skipped_in_evidence(self):
        fake = FakePopen(self._success_stream('Read'), self.sink)
        out = self._run(fake, None)
        self.assertTrue(out['sent'], out)
        self.assertIsNone(out['required_tools'])
        self.assertTrue(out['permission_preflight']['skipped'])
        self.assertIsNone(out['permission_preflight']['required_tools'])
        req = json.loads(Path(out['evidence']['request_path']).read_text(encoding='utf-8'))
        self.assertIsNone(req['required_tools'])
        self.assertTrue(req['permission_preflight']['skipped'])
        self.assertIsNone(req['permission_preflight']['required_tools'])


class CliDeclarationTests(unittest.TestCase):
    def test_blank_required_tools_rejected_before_config_load(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cd.main(['--workspace', 'C:/ws', '--prompt-file', 'C:/ws/p.txt',
                          '--output-dir', 'C:/ws/out', '--stage', 'S1',
                          '--task-id', 't1', '--required-tools', ''])
        self.assertEqual(rc, 2)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload['reason'], 'required_tools_invalid')
        self.assertFalse(payload['sent'])

    def test_required_tools_passed_to_run(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            prompt = base / 'prompt.txt'
            prompt.write_text('task', encoding='utf-8')
            cfg = base / 'entry.json'
            cfg.write_text(json.dumps(_cfg()), encoding='utf-8')
            captured = {}

            def fake_run(cfg_, **kwargs):
                captured.update(kwargs)
                return {'sent': False, 'dispatched': False,
                        'reason': 'test_stub'}

            buf = io.StringIO()
            with mock.patch.object(cd, 'run', side_effect=fake_run), \
                 contextlib.redirect_stdout(buf):
                rc = cd.main(['--workspace', str(base), '--prompt-file', str(prompt),
                              '--output-dir', str(base / 'out'), '--stage', 'S1',
                              '--task-id', 't1', '--config', str(cfg),
                              '--required-tools', 'Read,Edit'])
        self.assertEqual(rc, 2)
        self.assertEqual(captured['required_tools'], ['Read', 'Edit'])


if __name__ == '__main__':
    unittest.main()
