"""BW-CLAUDE-DEEPSEEK-S3 离线回归：claude_direct 入口（零真实模型/零网络/零 DPAPI）。

用注入的假 Popen + token_env 环境 + 显式临时 dispatch store 驱动整条生命周期，断言：
argv 无 shell/无 token/无 bypass/无 --bare、--tools/--setting-sources 始终传、allowedTools 只出现
一次、env 钉死 deepseek-flash 且清除继承凭据/代理、outdir 必须不存在、结果分型（成功/429/
is_error 载体/零工具工程证据）、时段门拒派、跨高峰释放、launch_unknown 保持、token 绝不进入任何
证据文件。真实 Claude 调用与真实凭据由 root 本机执行，本文件只证明离线设计正确。
"""
import json
import sys
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import claude_direct as cd      # noqa: E402
import dispatch_pool as dp      # noqa: E402
import execution_control as ec  # noqa: E402
import prompt_contract as pc    # noqa: E402

OFFPEAK = datetime(2026, 10, 8, 15, 0, 0, tzinfo=timezone.utc)  # 北京 23:00 周四 offhours
PEAK = datetime(2026, 10, 12, 1, 0, 0, tzinfo=timezone.utc)     # 北京 09:00 周一 高峰
SECRET = 'sk-SECRET-TOKEN-DO-NOT-LEAK'


def _report_body(stage, workspace):
    """构造满足 qoder_direct.analyze_report 的九节正文（首末标记独占行、字段/标题/结语齐全）。"""
    lines = [pc.REPORT_START,
             f'阶段编号与执行方式：{stage}；direct。',
             f'实际项目绝对路径：{workspace}',
             '汇报时间与执行环境：offline stub']
    for h in pc.SECTION_HEADERS:
        lines.append(f'{h}\n- 字段：值')
    lines.append(pc.CLOSING_LINE)
    lines.append(pc.REPORT_END)
    return '\n'.join(lines)


class _OutStream:
    """Deterministic stdout/stderr line source for offline lifecycle tests."""

    def __init__(self, text, owner):
        self._lines = text.splitlines(keepends=True)
        self._index = 0
        self.owner = owner
        self.stopped = False
        self.closed = False

    def readline(self):
        if self.owner._comm_delay:
            time.sleep(self.owner._comm_delay)
            self.owner._comm_delay = 0.0
        if self.owner._raise_comm:
            raise self.owner._raise_comm
        if self.stopped or self._index >= len(self._lines):
            return ''
        line = self._lines[self._index]
        self._index += 1
        return line

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


class _InStream:
    def __init__(self):
        self.data = []
        self.closed = False

    def write(self, value):
        if self.closed:
            raise ValueError('stdin closed')
        self.data.append(value)
        return len(value)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakePopen:
    def __init__(self, stdout, stderr='', returncode=0, pid=4242, raise_comm=None,
                 raise_init=False, sink=None, comm_delay=0.0, terminate_rc=-15):
        self._stdout, self._stderr = stdout, stderr
        self._rc, self.pid = returncode, pid
        self._raise_comm, self._raise_init = raise_comm, raise_init
        self._comm_delay = comm_delay
        self._terminate_rc = terminate_rc
        self.argv = self.env = None
        self.sink = sink
        self.returncode = None
        self.terminated = False
        self.stdin = self.stdout = self.stderr = None

    def __call__(self, argv, *, cwd=None, stdin=None, stdout=None, stderr=None,
                 env=None, text=None):
        if self.sink is not None:
            self.sink.append(self)
        self.argv, self.env = list(argv), env
        if self._raise_init:
            raise OSError('boom before handle')
        self.stdin = _InStream()
        self.stdout = _OutStream(self._stdout, self)
        self.stderr = _OutStream(self._stderr, self)
        return self

    def terminate(self):
        self.terminated = True
        if self.stdout is not None:
            self.stdout.stop()
        if self.stderr is not None:
            self.stderr.stop()
        self.returncode = self._terminate_rc

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = self._rc
        return self.returncode

    def communicate(self, input=None):
        if self._comm_delay:
            # 极短等待仅确保真实时钟推进（毫秒级，不是长 sleep）；不改变生命周期语义。
            time.sleep(self._comm_delay)
        if self._raise_comm:
            raise self._raise_comm
        self.returncode = self._rc
        return self._stdout, self._stderr


def _jsonl(events):
    return '\n'.join(json.dumps(e) for e in events)


class PureFunctionTests(unittest.TestCase):
    def cfg(self, **over):
        base = {'executable': 'C:/abs/claude.exe', 'model': 'deepseek-flash',
                'tools': ['Read', 'Edit'], 'allowed_tools': ['Edit(/abs/x.py)'],
                'max_turns': 8, 'setting_sources': [],
                'credential_source': {'mode': 'token_env', 'env': 'ANTHROPIC_API_KEY'}}
        base.update(over)
        return base

    def test_argv_shape_no_token_no_bypass_no_bare(self):
        argv = cd.build_argv(self.cfg(), 'C:/ws', 'deepseek-flash', 'CONTRACT')
        self.assertEqual(argv[0], 'C:/abs/claude.exe')
        for frag in ('-p', '--output-format', 'stream-json', '--verbose'):
            self.assertIn(frag, argv)
        self.assertIn('dontAsk', argv)
        self.assertIn('--append-system-prompt', argv)
        self.assertIn('CONTRACT', argv)
        self.assertEqual(argv[argv.index('--max-turns') + 1], '8')
        # --tools / --setting-sources 始终传（空 setting_sources 也传空值）
        self.assertIn('--tools', argv)
        self.assertIn('--setting-sources', argv)
        self.assertEqual(argv[argv.index('--setting-sources') + 1], '')
        self.assertIn('--strict-mcp-config', argv)
        self.assertIn('--disable-slash-commands', argv)
        # allowedTools 只出现一次，后跟全部规则
        self.assertEqual(argv.count('--allowedTools'), 1)
        self.assertEqual(argv.count('--disallowedTools'), 1)
        self.assertNotIn('--bare', argv)
        joined = ' '.join(argv).lower()
        self.assertNotIn('bypass', joined)
        self.assertNotIn('yolo', joined)
        self.assertNotIn(SECRET, joined)

    def test_argv_rejects_bypass_permission_mode(self):
        with self.assertRaises(ValueError):
            cd.build_argv(self.cfg(permission_mode='bypassPermissions'), 'C:/ws',
                          'deepseek-flash', '')

    def test_argv_launcher_args_node_cli(self):
        cfg = self.cfg(executable='C:/node/node.exe',
                       launcher_args=['C:/abs/claude-code/cli.js'])
        argv = cd.build_argv(cfg, 'C:/ws', 'deepseek-flash', '')
        self.assertEqual(argv[0], 'C:/node/node.exe')
        self.assertEqual(argv[1], 'C:/abs/claude-code/cli.js')
        self.assertIn('-p', argv)

    def test_env_pins_all_models_and_uses_api_key(self):
        env = cd.build_subprocess_env(self.cfg(), SECRET,
                                      environ={'ANTHROPIC_AUTH_TOKEN': 'stale',
                                               'CLAUDE_CODE_SIMPLE': '1',
                                               'HTTP_PROXY': 'http://p'})
        self.assertEqual(env['ANTHROPIC_BASE_URL'], cd.BASE_URL)
        for var in cd._PINNED_MODEL_ENVS:
            self.assertEqual(env[var], 'deepseek-flash')
        self.assertEqual(env['ANTHROPIC_API_KEY'], SECRET)
        # 继承的敏感/代理变量被清除
        self.assertNotIn('ANTHROPIC_AUTH_TOKEN', env)
        self.assertNotIn('CLAUDE_CODE_SIMPLE', env)
        self.assertNotIn('HTTP_PROXY', env)
        self.assertEqual(env['CLAUDE_CODE_MAX_RETRIES'], '0')
        self.assertEqual(env['API_TIMEOUT_MS'], '60000')

    def test_resolve_token_env_and_missing(self):
        self.assertEqual(cd.resolve_token(self.cfg(), {'ANTHROPIC_API_KEY': SECRET}), SECRET)
        with self.assertRaises(KeyError):
            cd.resolve_token(self.cfg(), {})
        with self.assertRaises(ValueError):
            cd.resolve_token(self.cfg(credential_source={'mode': 'nonsense'}), {})

    def test_load_config_allows_default_local_name(self):
        # 受信任宿主默认加载 claude-entry.local.json：入口不按文件名拒绝
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / cd.PROTECTED_LOCAL_NAME
            p.write_text('{"executable": "x"}', encoding='utf-8')
            cfg = cd.load_entry_config(str(p))
            self.assertEqual(cfg['executable'], 'x')

    def test_summarize_stream_counts_tools(self):
        events = [
            {'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'name': 'Edit', 'id': 't1'},
                {'type': 'text', 'text': 'hello'}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't1'}]}},
            {'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': 's9',
             'result': 'BODY'}]
        s = cd.summarize_stream(_jsonl(events).splitlines())
        self.assertEqual(s['tool_use_count'], 1)
        self.assertEqual(s['tool_result_count'], 1)
        self.assertEqual(s['subtype'], 'success')
        self.assertEqual(s['session_id'], 's9')
        self.assertEqual(s['result_event']['result'], 'BODY')
        self.assertFalse(s['parse_error'])

    def test_summarize_stream_marks_malformed(self):
        s = cd.summarize_stream(['not json', '{"type":"result","subtype":"success"}'])
        self.assertTrue(s['parse_error'])
        self.assertEqual(s['malformed_lines'], 1)

    def test_classify_zero_tool_engineering_not_success(self):
        s = {'subtype': 'success', 'is_error': False, 'result_event': {},
             'tool_use_count': 0, 'tool_result_count': 0, 'session_id': 'x'}
        f = cd.classify_result(s, 0, '', is_engineering_task=True)
        self.assertTrue(f['protocol_success'])
        self.assertFalse(f['execution_success'])
        self.assertTrue(f['zero_tool_engineering'])

    def test_classify_429(self):
        s = {'subtype': 'error_during_execution', 'is_error': True,
             'result_event': {'result_errors': [{'status': 429}], 'errors_info': None},
             'tool_use_count': 1, 'tool_result_count': 0, 'session_id': 'x'}
        f = cd.classify_result(s, 1, '', is_engineering_task=False)
        self.assertTrue(f['quota_429'])
        self.assertFalse(f['protocol_success'])

    def test_classify_is_error_true_with_success_subtype(self):
        # is_error=true 即便 subtype=success 也不算协议成功（真实错误载体）
        s = {'subtype': 'success', 'is_error': True,
             'result_event': {'subtype': 'success', 'is_error': True, 'result': 'boom'},
             'tool_use_count': 2, 'tool_result_count': 2, 'session_id': 'x'}
        f = cd.classify_result(s, 0, '', is_engineering_task=False)
        self.assertFalse(f['protocol_success'])
        self.assertFalse(f['execution_success'])

    def test_classify_is_error_result_string_extracted_as_carrier(self):
        # 真实 API 错误在 is_error=true 时就在 result 字符串里（即便 subtype=success）：
        # 载体必须提取它；开头独立 429 → 明确限额（不是正文中间数字）。
        s = {'subtype': 'success', 'is_error': True,
             'result_event': {'subtype': 'success', 'is_error': True,
                              'result': '429 Too Many Requests: upstream rate limited'},
             'tool_use_count': 0, 'tool_result_count': 0, 'session_id': 'x'}
        f = cd.classify_result(s, 0, '', is_engineering_task=False)
        self.assertFalse(f['protocol_success'])
        self.assertTrue(f['quota_429'])

    def test_classify_ordinary_body_429_never_quota(self):
        # 成功报告正文里的 429 字样绝不扫成限额（is_error=false 时 result 不进错误载体）。
        s = {'subtype': 'success', 'is_error': False,
             'result_event': {'subtype': 'success', 'is_error': False,
                              'result': '共处理 429 条记录，全部成功'},
             'tool_use_count': 1, 'tool_result_count': 1, 'session_id': 'x'}
        f = cd.classify_result(s, 0, '', is_engineering_task=False)
        self.assertFalse(f['quota_429'])
        self.assertTrue(f['protocol_success'])

    def test_classify_cancellation_exit_is_not_429(self):
        # 取消退出码 4294967295/-1 不是 429：不触发限额、绝不落限额标记。
        s = {'subtype': 'error_during_execution', 'is_error': True,
             'result_event': {'subtype': 'error_during_execution', 'is_error': True,
                              'result': '4294967295 cancelled by user'},
             'session_id': 'x'}
        f = cd.classify_result(s, -1, '', is_engineering_task=False)
        self.assertFalse(f['quota_429'])
        self.assertFalse(f['protocol_success'])

    def test_classify_parse_error_never_protocol_success(self):
        s = {'subtype': 'success', 'is_error': False, 'result_event': {},
             'parse_error': True, 'malformed_lines': 3, 'session_id': 'x'}
        f = cd.classify_result(s, 0, '', is_engineering_task=False)
        self.assertFalse(f['protocol_success'])

    def test_classify_missing_receipts_engineering_not_success(self):
        # 缺回执：有 tool_use 但 0 条**成功** tool_result 回执 → 工程任务绝不冒执行成功。
        s = {'subtype': 'success', 'is_error': False,
             'result_event': {'subtype': 'success', 'is_error': False, 'result': 'ok'},
             'tool_use_count': 2, 'tool_result_count': 0,
             'ok_receipt_ids': [], 'error_receipt_ids': [],
             'missing_receipt_ids': ['u1', 'u2'], 'orphan_result_ids': [],
             'permission_denials': [], 'session_id': 'x'}
        f = cd.classify_result(s, 0, '', is_engineering_task=True)
        self.assertTrue(f['protocol_success'])
        self.assertFalse(f['execution_success'])
        self.assertEqual(f['missing_tool_receipts'], 2)

    def test_classify_all_denied_not_success(self):
        s = {'subtype': 'success', 'is_error': False,
             'result_event': {'subtype': 'success', 'is_error': False,
                              'permission_denials': [{'tool_use_id': 'u1'},
                                                     {'tool_use_id': 'u2'}]},
             'tool_use_count': 2, 'tool_result_count': 0,
             'ok_receipt_ids': [], 'error_receipt_ids': [],
             'missing_receipt_ids': ['u1', 'u2'], 'orphan_result_ids': [],
             'permission_denials': [{'tool_use_id': 'u1'}, {'tool_use_id': 'u2'}],
             'session_id': 'x'}
        f = cd.classify_result(s, 0, '', is_engineering_task=True)
        self.assertFalse(f['execution_success'])
        self.assertEqual(f['permission_denials'], 2)

    def test_summarize_stream_pairs_receipts(self):
        # tool_use.id ↔ tool_result.tool_use_id 精确配对；错误回执单独记（普通可恢复
        # 错误只作诊断，不泛化成权限故障），成功工程至少有 1 条成功回执。
        events = [
            {'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'name': 'Read', 'id': 'u1'},
                {'type': 'tool_use', 'name': 'Edit', 'id': 'u2'}]}},
            {'type': 'user', 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': 'u1'},
                {'type': 'tool_result', 'tool_use_id': 'u2',
                 'is_error': True, 'content': 'file changed'}]}},
            {'type': 'result', 'subtype': 'success', 'is_error': False,
             'session_id': 's1', 'result': 'done'}]
        s = cd.summarize_stream(_jsonl(events).splitlines())
        self.assertEqual(s['ok_receipt_ids'], ['u1'])
        self.assertEqual(s['error_receipt_ids'], ['u2'])
        self.assertEqual(s['missing_receipt_ids'], [])
        f = cd.classify_result(s, 0, '', is_engineering_task=True)
        self.assertTrue(f['execution_success'])
        self.assertEqual(f['error_tool_receipts'], 1)

    def test_redact_scrubs_token(self):
        self.assertNotIn(SECRET, cd._redact({'k': f'x {SECRET} y'}, SECRET))

    # ---------------------------------------------------------- S5 反例补充
    def test_build_actual_grants_equal_grants_from_rules(self):
        # _build_actual 的 grants 必须与 ec.grants_from_rules 权威结果**精确相等**
        # （旧实现错填 edits=[]、tool_visibility=全部可见工具）。
        cfg = self.cfg(tools=['Read', 'Edit'],
                       allowed_tools=['Read', 'Edit(/out/output.txt)'],
                       disallowed_tools=['Bash'], add_dirs=[])
        argv = cd.build_argv(cfg, 'C:/ws', 'deepseek-flash', 'C')
        actual = cd._build_actual(cfg, task_id='t', stage='S', model='deepseek-flash',
                                  workspace='C:/ws', prompt_sha256='a' * 64, argv=argv,
                                  task_text='x')
        expected = ec.grants_from_rules(['Read', 'Edit(/out/output.txt)'], add_dirs=[],
                                        disallowed_rules=['Bash'],
                                        tools_items=['Read', 'Edit'])
        self.assertEqual(actual['grants'], expected)

    def test_real_plan_passes_and_drift_rejected(self):
        # 由真实 grants 构造的计划必须通过预检；任一字段漂移仍被拒。
        cfg = self.cfg(tools=['Read', 'Edit'],
                       allowed_tools=['Read', 'Edit(/out/output.txt)'],
                       disallowed_tools=[], add_dirs=[])
        argv = cd.build_argv(cfg, 'C:/ws', 'deepseek-flash', 'C')
        actual = cd._build_actual(cfg, task_id='t1', stage='S5', model='deepseek-flash',
                                  workspace='C:/ws', prompt_sha256='a' * 64, argv=argv,
                                  task_text='task')
        plan = {'task_id': 't1', 'stage': 'S5', 'runtime': 'claude',
                'model': 'deepseek-flash', 'workspace': 'C:/ws', 'cwd': 'C:/ws',
                'prompt_sha256': 'a' * 64, 'grants': dict(actual['grants'])}
        # 真实计划须补齐 grants 派生的顶层清单字段（权威来源即 actual['grants']）；
        # 缺任一项会被预检正确拒绝。
        for k in ec.PLAN_LIST_KEYS:
            plan[k] = actual['grants'][k]
        pf = ec.preflight(plan, actual)
        self.assertTrue(pf.get('ok'), pf)
        drift = dict(plan, model='claude-opus-4-6')
        pf2 = ec.preflight(drift, actual)
        self.assertFalse(pf2.get('ok'), pf2)

    def test_classify_one_success_one_missing_result_rejected(self):
        # 一成功回执 + 一缺结果 → 工程任务绝不冒执行成功。
        s = {'subtype': 'success', 'is_error': False,
             'result_event': {'subtype': 'success', 'is_error': False, 'result': 'ok'},
             'tool_use_count': 2, 'tool_result_count': 1,
             'ok_receipt_ids': ['u1'], 'error_receipt_ids': [],
             'missing_receipt_ids': ['u2'], 'orphan_result_ids': [],
             'permission_denials': [], 'session_id': 'x'}
        f = cd.classify_result(s, 0, '', is_engineering_task=True)
        self.assertTrue(f['protocol_success'])
        self.assertFalse(f['execution_success'])
        self.assertFalse(f['execution_evidence_ok'])
        self.assertEqual(f['missing_tool_receipts'], 1)

    def test_classify_structured_denial_with_ok_receipt_rejected(self):
        # permission_denials 非空（is_error=false）即便有成功回执也必须拒。
        s = {'subtype': 'success', 'is_error': False,
             'result_event': {'subtype': 'success', 'is_error': False,
                              'permission_denials': [{'tool_use_id': 'u2'}]},
             'tool_use_count': 2, 'tool_result_count': 1,
             'ok_receipt_ids': ['u1'], 'error_receipt_ids': [],
             'missing_receipt_ids': [], 'orphan_result_ids': [],
             'permission_denials': [{'tool_use_id': 'u2'}], 'session_id': 'x'}
        f = cd.classify_result(s, 0, '', is_engineering_task=True)
        self.assertTrue(f['protocol_success'])
        self.assertFalse(f['execution_success'])

    def test_classify_denial_marker_is_error_false_rejected(self):
        # 权限拒绝可能 is_error=false：由 approval_denial_ids 识别，必须拒。
        s = {'subtype': 'success', 'is_error': False,
             'result_event': {'subtype': 'success', 'is_error': False, 'result': 'ok'},
             'tool_use_count': 1, 'tool_result_count': 1,
             'ok_receipt_ids': ['u1'], 'error_receipt_ids': [],
             'missing_receipt_ids': [], 'orphan_result_ids': [],
             'approval_denial_ids': ['u1'], 'permission_denials': [], 'session_id': 'x'}
        f = cd.classify_result(s, 0, '', is_engineering_task=True)
        self.assertFalse(f['execution_success'])

    def test_summarize_success_read_source_denial_text_not_denial(self):
        # 成功 Read 的源码正文里出现审批拒绝字样 → 绝不误判为拒绝。
        events = [
            {'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'name': 'Read', 'id': 'r1'}]}},
            {'type': 'user', 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': 'r1',
                 'content': '# permission to use Edit denied; tests reference this string'}]}},
            {'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': 's',
             'result': 'ok'}]
        s = cd.summarize_stream(_jsonl(events).splitlines())
        self.assertEqual(s['approval_denial_ids'], [])
        self.assertFalse(s['approval_denial_in_result'])
        f = cd.classify_result(s, 0, '', is_engineering_task=True)
        self.assertTrue(f['execution_success'])

    def test_summarize_error_receipt_approval_denial_detected(self):
        # 错误回执里**开头**的审批拒绝被识别（is_error=true 回执）。
        events = [
            {'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'name': 'Edit', 'id': 'u1'}]}},
            {'type': 'user', 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': 'u1', 'is_error': True,
                 'content': 'Permission to use Edit has been denied.'}]}},
            {'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': 's',
             'result': 'ok'}]
        s = cd.summarize_stream(_jsonl(events).splitlines())
        self.assertEqual(s['approval_denial_ids'], ['u1'])
        f = cd.classify_result(s, 0, '', is_engineering_task=False)
        self.assertFalse(f['execution_success'])

    def test_summarize_duplicate_and_orphan_rejected(self):
        # 重复 tool_use.id + 孤儿 result → 冲突终态，绝不执行成功。
        events = [
            {'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'name': 'Read', 'id': 'u1'},
                {'type': 'tool_use', 'name': 'Read', 'id': 'u1'}]}},
            {'type': 'user', 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': 'u1'},
                {'type': 'tool_result', 'tool_use_id': 'orphan1'}]}},
            {'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': 's',
             'result': 'ok'}]
        s = cd.summarize_stream(_jsonl(events).splitlines())
        self.assertEqual(s['duplicate_use_ids'], ['u1'])
        self.assertIn('orphan1', s['orphan_result_ids'])
        f = cd.classify_result(s, 0, '', is_engineering_task=True)
        self.assertFalse(f['execution_success'])


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.store = self.base / 'dispatch.sqlite3'
        self.ws = self.base / 'ws'
        self.ws.mkdir()
        self.prompt = self.base / 'prompt.txt'
        self.prompt.write_text('do the task', encoding='utf-8')
        self.out = self.base / 'out'
        self._p = mock.patch.object(dp, 'process_identity',
                                    return_value={'state': 'alive', 'created': 'c1'})
        self._p.start()
        self.addCleanup(self._p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.sink = []

    def cfg(self):
        return {'executable': str(self.base / 'claude.exe'), 'model': 'deepseek-flash',
                'tools': ['Read'], 'allowed_tools': ['Read'], 'setting_sources': [],
                'credential_source': {'mode': 'token_env', 'env': 'ANTHROPIC_API_KEY'}}

    def sha(self):
        import hashlib
        return hashlib.sha256(self.prompt.read_bytes()).hexdigest()

    def run_with(self, fake, *, now=OFFPEAK, is_eng=True, task='t1', out=None,
                 bind_child_pid=False):
        # 离线 mock 显式注入 bind_child_pid=False（仅离线，绝不作为 CLI 权限开关）；
        # 真实绑定路径由专门的反例测试用注入的 bind_child 覆盖。
        return cd.run(self.cfg(), workspace=str(self.ws), prompt_file=self.prompt,
                      out_dir=out or self.out, stage='BW-CLAUDE-DEEPSEEK-S3', task_id=task,
                      prompt_sha256=self.sha(), now=now, popen_factory=fake,
                      environ={'ANTHROPIC_API_KEY': SECRET}, dispatch_store=self.store,
                      child_prober=lambda pid: {'state': 'dead', 'created': 'c1'},
                      is_engineering_task=is_eng, bind_child_pid=bind_child_pid,
                      required_tools=['Read'] if is_eng else None)

    def success_stream(self):
        body = _report_body('BW-CLAUDE-DEEPSEEK-S3', str(self.ws))
        return _jsonl([
            {'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'name': 'Read', 'id': 't1'}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't1'}]}},
            {'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': 's1',
             'result': body}])

    def test_stub_success_releases_and_no_secret_leak(self):
        fake = FakePopen(self.success_stream(), pid=4242, sink=self.sink)
        # 成功 fixture 走真实绑定路径：bind_child_pid=True，配合 setUp 的
        # process_identity(alive/created=c1) 与 child_prober(dead/created=c1)
        # 模拟绑定成立与真实终态释放（不 mock passed/finish）。
        out = self.run_with(fake, bind_child_pid=True)
        self.assertTrue(out['sent'], out)
        self.assertTrue(out['facts']['execution_success'], out['facts'])
        self.assertTrue(out['binding']['bound'], out['binding'])
        self.assertTrue(out['child_verified'], out)
        self.assertTrue(out['passed'])
        self.assertTrue(out['capacity_release'].get('released'), out['capacity_release'])
        # 真实终态释放 → claude 槽空闲
        self.assertEqual(dp.status(self.store, now=OFFPEAK)['pools']['claude:deepseek-flash']['active'], 0)
        # token 只进 env，绝不进 argv
        self.assertNotIn(SECRET, ' '.join(fake.argv))
        self.assertEqual(fake.env['ANTHROPIC_API_KEY'], SECRET)
        # 任何证据文件都不含 token
        for f in Path(out['evidence']['events_jsonl']).parent.glob('*'):
            if f.is_file():
                self.assertNotIn(SECRET, f.read_text(encoding='utf-8'), f.name)

    def test_output_dir_must_not_exist(self):
        self.out.mkdir()
        fake = FakePopen(self.success_stream(), sink=self.sink)
        out = self.run_with(fake)
        self.assertFalse(out['sent'])
        self.assertEqual(out['reason'], 'output_dir_exists')
        self.assertEqual(self.sink, [])  # 零 Popen

    def test_peak_blocks_before_popen(self):
        fake = FakePopen(self.success_stream(), sink=self.sink)
        out = self.run_with(fake, now=PEAK)
        self.assertTrue(out['dispatch_failed'], out)
        self.assertFalse(out['sent'])
        self.assertEqual(self.sink, [])  # 零 Popen
        self.assertEqual(dp.status(self.store, now=PEAK)['pools']['claude:deepseek-flash']['active'], 0)

    def test_zero_tool_engineering_not_bound_success(self):
        body = _report_body('BW-CLAUDE-DEEPSEEK-S3', str(self.ws))
        stream = _jsonl([{'type': 'result', 'subtype': 'success', 'is_error': False,
                          'session_id': 's1', 'result': body}])  # 无 tool_use
        out = self.run_with(FakePopen(stream, sink=self.sink))
        self.assertTrue(out['facts']['zero_tool_engineering'])
        self.assertFalse(out['facts']['execution_success'])

    def test_format_failure_binding_false(self):
        stream = _jsonl([{'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'name': 'Read', 'id': 't1'}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't1'}]}},
            {'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': 's1',
             'result': 'not a structured report'}])
        out = self.run_with(FakePopen(stream, sink=self.sink))
        self.assertFalse(out['binding']['bound'])
        self.assertTrue(out['sent'])

    def test_launch_error_kept_unknown(self):
        # 已启动但 communicate 抛错 → 保留 launch_unknown，绝不假释放
        fake = FakePopen('', raise_comm=RuntimeError('interrupted'), sink=self.sink)
        out = self.run_with(fake)
        self.assertFalse(out['sent'])
        self.assertTrue(out['launch_unknown'])
        self.assertEqual(dp.status(self.store, now=OFFPEAK)['pools']['claude:deepseek-flash']['active'], 1)

    def test_no_popen_handle_is_not_sent(self):
        fake = FakePopen('', raise_init=True, sink=self.sink)
        out = self.run_with(fake)
        self.assertFalse(out['sent'])
        self.assertFalse(out['dispatched'])

    def test_dispatch_plan_rejected_before_popen(self):
        self.out  # keep
        plan = {'task_id': 't1', 'stage': 'BW-CLAUDE-DEEPSEEK-S3', 'runtime': 'claude',
                'model': 'deepseek-flash', 'workspace': str(self.ws), 'cwd': str(self.ws),
                'prompt_sha256': 'a' * 64, 'grants': {'edits': [], 'bash': [], 'read_dirs': []},
                'tool_visibility': ['Read'], 'visible_tools': ['Read'],
                'allowed_tools': ['Read'], 'disallowed_tools': []}
        fake = FakePopen(self.success_stream(), sink=self.sink)
        out = cd.run(self.cfg(), workspace=str(self.ws), prompt_file=self.prompt,
                     out_dir=self.out, stage='BW-CLAUDE-DEEPSEEK-S3', task_id='t1',
                     prompt_sha256=self.sha(), now=OFFPEAK, popen_factory=fake,
                     environ={'ANTHROPIC_API_KEY': SECRET}, dispatch_store=self.store,
                     required_tools=['Read'], plan=plan)  # plan grants mismatch → reject
        self.assertFalse(out['sent'])
        self.assertFalse(out['dispatched'])
        self.assertEqual(self.sink, [])

    def test_bind_failure_required_keeps_unknown_not_passed(self):
        # 生产默认 bind_child_pid=True 且绑定未明确成功 → 不写入子进程、不假释放、不 passed。
        with mock.patch.object(dp, 'bind_child', return_value={'bound': False}):
            out = cd.run(self.cfg(), workspace=str(self.ws), prompt_file=self.prompt,
                         out_dir=self.out, stage='BW-CLAUDE-DEEPSEEK-S3', task_id='t1',
                         prompt_sha256=self.sha(), now=OFFPEAK,
                         popen_factory=FakePopen(self.success_stream(), sink=self.sink),
                         environ={'ANTHROPIC_API_KEY': SECRET}, dispatch_store=self.store,
                         child_prober=lambda pid: {'state': 'dead', 'created': 'c1'},
                         required_tools=['Read'], bind_child_pid=True)
        self.assertFalse(out['sent'])
        self.assertTrue(out.get('launch_unknown'))
        self.assertFalse(out.get('passed', False))
        self.assertFalse(out['child_verified'])
        self.assertEqual(
            dp.status(self.store, now=OFFPEAK)['pools']['claude:deepseek-flash']['active'], 1)

    def test_release_refused_not_passed(self):
        # 终态释放未明确 released=true → 绝不 passed。
        with mock.patch.object(dp, 'finish', return_value={'released': False}):
            out = self.run_with(FakePopen(self.success_stream(), sink=self.sink))
        self.assertTrue(out['sent'])
        self.assertFalse(out['passed'])
        self.assertEqual(out['capacity_release'].get('released'), False)

    def test_secret_in_report_redacted(self):
        # 报告正文里的 token 必须脱敏；其余原文逐字保留，任何证据文件都不含 secret。
        body = _report_body('BW-CLAUDE-DEEPSEEK-S3', str(self.ws)) + f'\nnote: {SECRET}'
        stream = _jsonl([
            {'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'name': 'Read', 'id': 't1'}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't1'}]}},
            {'type': 'result', 'subtype': 'success', 'is_error': False, 'session_id': 's1',
             'result': body}])
        out = self.run_with(FakePopen(stream, sink=self.sink))
        rep = Path(out['evidence']['report_md']).read_text(encoding='utf-8')
        self.assertNotIn(SECRET, rep)
        self.assertIn('[REDACTED_TOKEN]', rep)
        # 只脱敏 token，报告其余原文逐字保留（首末标记仍在）。
        self.assertIn(pc.REPORT_START, rep)
        self.assertIn(pc.REPORT_END, rep)
        self.assertTrue(out['evidence'].get('report_redacted'))
        for f in Path(out['evidence']['report_md']).parent.glob('*'):
            if f.is_file():
                self.assertNotIn(SECRET, f.read_text(encoding='utf-8'), f.name)

    def test_injected_clock_keeps_offline_determinism(self):
        # 显式注入 now：开始/结束/终态释放都用注入值，离线用例保持确定性。
        fake = FakePopen(self.success_stream(), sink=self.sink)
        out = self.run_with(fake, now=OFFPEAK, bind_child_pid=True)
        self.assertTrue(out['sent'], out)
        self.assertEqual(out['started_at_utc'], OFFPEAK.isoformat())
        self.assertEqual(out['finished_at_utc'], OFFPEAK.isoformat())
        req = json.loads(Path(out['evidence']['request_path']).read_text(encoding='utf-8'))
        self.assertEqual(req['started_at_utc'], OFFPEAK.isoformat())
        self.assertEqual(req['finished_at_utc'], OFFPEAK.isoformat())

    def test_injected_offset_clock_is_saved_as_same_instant_utc(self):
        # 显式注入 +08 aware datetime：started_at_utc/finished_at_utc 存档必须是同一
        # 时刻的 UTC（+00:00），不是原偏移字符串；不伪造修复现场旧证据。
        beijing = datetime(2026, 10, 18, 13, 9, 48, tzinfo=timezone(timedelta(hours=8)))
        expected = beijing.astimezone(timezone.utc).isoformat()
        fake = FakePopen(self.success_stream(), sink=self.sink)
        out = self.run_with(fake, now=beijing)
        self.assertTrue(out['sent'], out)
        self.assertEqual(out['started_at_utc'], expected)
        self.assertEqual(out['finished_at_utc'], expected)
        self.assertTrue(out['started_at_utc'].endswith('+00:00'), out['started_at_utc'])
        self.assertEqual(datetime.fromisoformat(out['started_at_utc']), beijing)
        req = json.loads(Path(out['evidence']['request_path']).read_text(encoding='utf-8'))
        self.assertEqual(req['started_at_utc'], expected)
        self.assertEqual(req['finished_at_utc'], expected)

    def test_real_clock_start_and_finish_are_distinct(self):
        # 默认（未注入 now）：开始取 Popen 前真实时点，结束取 communicate 返回后的新真实
        # 时点；不再把派工开始的同一 now 写成结束。dp.utcnow 仅用于时段门，保持离线可控。
        fake = FakePopen(self.success_stream(), sink=self.sink, comm_delay=0.02)
        with mock.patch.object(dp, 'utcnow', return_value=OFFPEAK):
            out = self.run_with(fake, now=None, bind_child_pid=True)
        self.assertTrue(out['sent'], out)
        self.assertTrue(out['passed'], out)
        started = datetime.fromisoformat(out['started_at_utc'])
        finished = datetime.fromisoformat(out['finished_at_utc'])
        self.assertLessEqual(started, finished)
        self.assertNotEqual(started, finished)  # 真实时钟推进
        self.assertNotEqual(started, OFFPEAK)
        self.assertNotEqual(finished, OFFPEAK)
        req = json.loads(Path(out['evidence']['request_path']).read_text(encoding='utf-8'))
        self.assertEqual(req['started_at_utc'], out['started_at_utc'])
        self.assertEqual(req['finished_at_utc'], out['finished_at_utc'])
        # 终态释放使用真实结束时钟，而不是开始时钟。
        with closing(dp.connect(self.store)) as conn:
            row = conn.execute('SELECT ended_at_utc FROM attempts WHERE token=?',
                               (out['token'],)).fetchone()
        self.assertEqual(datetime.fromisoformat(row['ended_at_utc']), finished)

    def test_real_clock_unknown_keeps_placeholder(self):
        # 默认真实时钟下 communicate 异常（真实退出未知）→ 保留 unknown 占位，绝不假释放。
        fake = FakePopen('', raise_comm=RuntimeError('interrupted'), sink=self.sink)
        with mock.patch.object(dp, 'utcnow', return_value=OFFPEAK):
            out = self.run_with(fake, now=None, bind_child_pid=True)
        self.assertTrue(out.get('launch_unknown'), out)
        self.assertFalse(out['sent'])
        self.assertEqual(
            dp.status(self.store, now=OFFPEAK)['pools']['claude:deepseek-flash']['active'], 1)

    def test_sent_contract_carries_current_identity(self):
        # 实际发送的契约（落盘原件）携带本轮 runtime 与请求模型；不是旧提示词身份。
        out = self.run_with(FakePopen(self.success_stream(), sink=self.sink))
        contract = Path(out['evidence']['sent_contract_path']).read_text(encoding='utf-8')
        self.assertIn('runtime=claude；请求模型=deepseek-flash', contract)
        self.assertIn('不覆盖本轮身份', contract)
        req = json.loads(Path(out['evidence']['request_path']).read_text(encoding='utf-8'))
        self.assertEqual(req['executor_identity']['runtime'], 'claude')
        self.assertEqual(req['executor_identity']['request_model'], 'deepseek-flash')
        import hashlib
        self.assertEqual(req['contract_sha256'],
                         hashlib.sha256(contract.encode('utf-8')).hexdigest())

    def test_cli_non_pinned_model_rejected_zero_popen(self):
        # CLI --model 非 deepseek-flash → 明确拒绝（0 Popen），不因 cfg 而静默忽略。
        with mock.patch.object(cd.subprocess, 'Popen',
                               side_effect=AssertionError('Popen must not run')) as m:
            rc = cd.main(['--workspace', str(self.ws), '--prompt-file', str(self.prompt),
                          '--output-dir', str(self.base / 'rej-out'), '--stage', 'S5',
                          '--task-id', 't1', '--model', 'claude-opus-4-6',
                          '--config', str(self.base / 'missing.json')])
        self.assertEqual(rc, 2)
        m.assert_not_called()


if __name__ == '__main__':
    unittest.main()
