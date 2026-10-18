"""BW-CLAUDE-SIMPLE-20261018-S1 离线回归：Claude 默认 CLI bypass。

只证离线契约：默认 permission_mode=bypassPermissions 且真实 argv 带
`--dangerously-skip-permissions`（不是只带 `--allow-dangerously-skip-permissions`）；
普通工程入口不要求 required_tools/write_files、不跑精确文件授权预检，但回执如实记录
mode/enforcement/exact_file_authorization=false；工具可见性（六项常用工具含 Bash）仍
进入 argv/回执；显式 dontAsk 保留旧严格 profile；模型/endpoint/未知模式仍拒绝；工程成功
仍要求至少一条成功 tool 回执，不放宽报告绑定。不联网、不读凭据、不启动真实模型。
"""
import hashlib
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
SECRET = 'sk-BYPASS-SECRET-DO-NOT-LEAK'
STAGE = 'BW-CLAUDE-SIMPLE-20261018-S1'
TOOLS6 = ['Read', 'Glob', 'Grep', 'Edit', 'Write', 'Bash']


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


def _cfg(**over):
    """默认不写 permission_mode —— 用于证默认就是 bypass。"""
    base = {'executable': 'C:/abs/claude.exe', 'model': 'deepseek-flash',
            'tools': list(TOOLS6), 'allowed_tools': [], 'setting_sources': [],
            'credential_source': {'mode': 'token_env', 'env': 'ANTHROPIC_API_KEY'}}
    base.update(over)
    return base


class _OutStream:
    def __init__(self, text):
        self._lines = text.splitlines(keepends=True)
        self._index = 0
        self.stopped = False

    def readline(self):
        if self.stopped or self._index >= len(self._lines):
            return ''
        line = self._lines[self._index]
        self._index += 1
        return line

    def stop(self):
        self.stopped = True

    def close(self):
        pass


class _InStream:
    def __init__(self):
        self.data = []

    def write(self, value):
        self.data.append(value)
        return len(value)

    def flush(self):
        pass

    def close(self):
        pass


class FakePopen:
    def __init__(self, stdout, sink):
        self._stdout, self.sink = stdout, sink
        self.argv = self.env = None
        self.returncode = None
        self.terminated = False
        self.stdin = self.stdout = self.stderr = None

    def __call__(self, argv, *, cwd=None, stdin=None, stdout=None, stderr=None,
                 env=None, text=None):
        self.argv, self.env = list(argv), env
        self.sink.append(self)
        self.stdin = _InStream()
        self.stdout = _OutStream(self._stdout)
        self.stderr = _OutStream('')
        return self

    def terminate(self):
        self.terminated = True
        self.stdout.stop()
        self.stderr.stop()
        self.returncode = -15

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def communicate(self, input=None):
        self.returncode = 0
        return self._stdout, ''


class BypassArgvTests(unittest.TestCase):
    def test_default_mode_is_bypass_with_real_flag(self):
        argv = cd.build_argv(_cfg(), 'C:/ws', 'deepseek-flash', 'CONTRACT')
        self.assertEqual(argv[argv.index('--permission-mode') + 1], 'bypassPermissions')
        self.assertIn('--dangerously-skip-permissions', argv)
        # 只“允许”bypass 的标志不等于启用，入口绝不使用它冒充。
        self.assertNotIn('--allow-dangerously-skip-permissions', argv)
        self.assertNotIn('--bare', argv)
        self.assertEqual(argv.count('--allowedTools'), 1)
        self.assertEqual(argv.count('--disallowedTools'), 1)
        self.assertNotIn(SECRET, ' '.join(argv))

    def test_explicit_bypass_matches_default(self):
        default = cd.build_argv(_cfg(), 'C:/ws', 'deepseek-flash', 'C')
        explicit = cd.build_argv(_cfg(permission_mode='bypassPermissions'), 'C:/ws',
                                 'deepseek-flash', 'C')
        self.assertEqual(default, explicit)

    def test_six_default_tools_visible_including_bash(self):
        argv = cd.build_argv(_cfg(), 'C:/ws', 'deepseek-flash', '')
        tools = argv[argv.index('--tools') + 1].split(',')
        self.assertEqual(tools, TOOLS6)
        self.assertIn('Bash', tools)
        # 不默认附逐文件 allowed 规则（示例配置 allowed_tools 为空）。
        self.assertEqual(argv[argv.index('--allowedTools') + 1], '--disallowedTools')

    def test_other_model_and_endpoint_still_rejected(self):
        with self.assertRaises(ValueError):
            cd.build_argv(_cfg(), 'C:/ws', 'claude-opus-4-6', '')
        with self.assertRaises(ValueError):
            cd.build_argv(_cfg(base_url='https://example.invalid'), 'C:/ws',
                          'deepseek-flash', '')

    def test_unsupported_permission_mode_rejected(self):
        with self.assertRaises(ValueError):
            cd.build_argv(_cfg(permission_mode='acceptEdits'), 'C:/ws',
                          'deepseek-flash', '')


class BypassLifecycleTests(unittest.TestCase):
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

    def _sha(self):
        return hashlib.sha256(self.prompt.read_bytes()).hexdigest()

    def _run(self, fake, *, cfg=None, required_tools=None, write_files=None,
             is_eng=True):
        return cd.run(cfg or _cfg(), workspace=str(self.ws), prompt_file=self.prompt,
                      out_dir=self.out, stage=STAGE, task_id='t1',
                      prompt_sha256=self._sha(), now=OFFPEAK, popen_factory=fake,
                      environ={'ANTHROPIC_API_KEY': SECRET},
                      dispatch_store=self.store, bind_child_pid=False,
                      child_prober=lambda pid: {'state': 'dead', 'created': 'c1'},
                      is_engineering_task=is_eng,
                      required_tools=required_tools, write_files=write_files)

    def _success_stream(self, tool='Read'):
        body = _report_body(STAGE, str(self.ws))
        events = [
            {'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'name': tool, 'id': 't1'}]}},
            {'type': 'user', 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': 't1'}]}},
            {'type': 'result', 'subtype': 'success', 'is_error': False,
             'session_id': 's1', 'result': body}]
        return '\n'.join(json.dumps(e) for e in events)

    def test_engineering_without_requirements_reaches_popen_and_records_bypass(self):
        fake = FakePopen(self._success_stream(), self.sink)
        out = self._run(fake, required_tools=None, write_files=None, is_eng=True)
        self.assertTrue(out['sent'], out)
        self.assertEqual(len(self.sink), 1)
        self.assertIn('--dangerously-skip-permissions', fake.argv)
        self.assertEqual(out['permission_mode'], 'bypassPermissions')
        self.assertEqual(out['permission_enforcement'], 'cli_bypass')
        self.assertFalse(out['exact_file_authorization'])
        self.assertTrue(out['permission_preflight']['skipped'])
        self.assertFalse(out['permission_preflight']['exact_file_authorization'])
        self.assertIsNone(out['permission_preflight']['required_tools'])
        self.assertEqual(out['permission_preflight']['tools'], TOOLS6)
        # 有成功 tool 回执的正常假工程才 execution_success。
        self.assertTrue(out['facts']['execution_success'], out['facts'])
        req = json.loads(Path(out['evidence']['request_path']).read_text(encoding='utf-8'))
        self.assertEqual(req['permission_mode'], 'bypassPermissions')
        self.assertEqual(req['permission_enforcement'], 'cli_bypass')
        self.assertFalse(req['exact_file_authorization'])
        self.assertFalse(req['permission_preflight']['exact_file_authorization'])
        self.assertNotIn(SECRET, json.dumps(req))

    def test_bypass_does_not_loosen_execution_success(self):
        body = _report_body(STAGE, str(self.ws))
        stream = '\n'.join(json.dumps(e) for e in [
            {'type': 'result', 'subtype': 'success', 'is_error': False,
             'session_id': 's1', 'result': body}])  # 零工具
        out = self._run(FakePopen(stream, self.sink))
        self.assertTrue(out['sent'], out)
        self.assertTrue(out['facts']['zero_tool_engineering'])
        self.assertFalse(out['facts']['execution_success'])

    def test_explicit_dontask_keeps_required_tools_gate(self):
        fake = FakePopen(self._success_stream(), self.sink)
        with mock.patch.object(cd, 'resolve_token',
                               side_effect=AssertionError('token must not be read')):
            out = self._run(fake, cfg=_cfg(permission_mode='dontAsk'))
        self.assertFalse(out['sent'])
        self.assertEqual(out['reason'], 'required_tools_missing')
        self.assertEqual(self.sink, [])  # 零 Popen、零凭据

    def test_explicit_dontask_keeps_exact_file_preflight(self):
        cfg = _cfg(permission_mode='dontAsk', tools=['Read', 'Edit'],
                   allowed_tools=['Read', 'Edit(C:/abs/out.txt)'])
        fake = FakePopen(self._success_stream('Edit'), self.sink)
        with mock.patch.object(cd, 'resolve_token',
                               side_effect=AssertionError('token must not be read')):
            out = self._run(fake, cfg=cfg, required_tools=['Read', 'Edit'],
                            write_files=['out.txt'])
        self.assertFalse(out['sent'])
        self.assertEqual(out['reason'], 'required_tools_not_granted')
        self.assertIn('Edit(./out.txt)', out['permission_preflight']['missing_write_rules'])
        self.assertEqual(out['permission_preflight']['mode'], 'dontAsk')
        self.assertEqual(self.sink, [])  # C:/ 规则仍被精确 profile 拒绝

    def test_dontask_with_grants_reaches_popen(self):
        cfg = _cfg(permission_mode='dontAsk', tools=['Read', 'Edit'],
                   allowed_tools=['Read', 'Edit(./out.txt)'])
        fake = FakePopen(self._success_stream('Edit'), self.sink)
        out = self._run(fake, cfg=cfg, required_tools=['Read', 'Edit'],
                        write_files=['out.txt'])
        self.assertTrue(out['sent'], out)
        self.assertNotIn('--dangerously-skip-permissions', fake.argv)
        self.assertEqual(out['permission_enforcement'], 'dontask_exact_preflight')
        self.assertTrue(out['exact_file_authorization'])


if __name__ == '__main__':
    unittest.main()
