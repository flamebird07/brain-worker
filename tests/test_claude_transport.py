"""BW-CLAUDE-TRANSPORT-20261018-S1 离线回归：传输故障诊断（零真实模型/零网络）。

只使用临时目录/临时 sqlite 与注入的 FakePopen（复用 test_claude_direct 的 fixture），
不启动真实进程、不读凭据、不联网。断言：独立网络码只从 terminal result 错误载体识别，
不扫描成功正文/assistant/stderr；区分零工具与部分成功工具；429/401/402/权限/业务测试
失败不串型；persisted facts 可回读、原错误脱敏保留、已有终态释放与 unknown 语义不变。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import claude_direct as cd      # noqa: E402
import dispatch_pool as dp      # noqa: E402

try:  # 源码树运行（python -m unittest tests.test_*）与 discover 运行两种导入路径。
    from tests.test_claude_direct import FakePopen, _jsonl, SECRET, OFFPEAK
except ImportError:  # discover -s tests 把 tests 目录作为顶层
    from test_claude_direct import FakePopen, _jsonl, SECRET, OFFPEAK


def _summary(result_event, *, ok=(), session='sess-1', subtype='success',
             tool_use_count=None):
    return {'subtype': subtype, 'is_error': bool(result_event.get('is_error')),
            'result_event': result_event,
            'tool_use_count': len(ok) if tool_use_count is None else tool_use_count,
            'tool_result_count': len(ok),
            'ok_receipt_ids': list(ok), 'error_receipt_ids': [],
            'missing_receipt_ids': [], 'orphan_result_ids': [],
            'permission_denials': [], 'session_id': session,
            'malformed_lines': 0, 'parse_error': False}


class TransportDiagnosisTests(unittest.TestCase):
    def test_success_subtype_is_error_econnreset_zero_tools(self):
        carrier = 'API Error: Unable to connect to API (ECONNRESET)'
        ev = {'subtype': 'success', 'is_error': True, 'result': carrier,
              'session_id': 'sess-1'}
        f = cd.classify_result(_summary(ev), 1, '', is_engineering_task=False)
        self.assertFalse(f['protocol_success'])
        self.assertFalse(f['execution_success'])
        self.assertFalse(f['quota_429'])
        self.assertEqual(f['failure_kind'], 'transport_error')
        te = f['transport_error']
        self.assertEqual(te['code'], 'ECONNRESET')
        self.assertEqual(te['source'], 'result')
        self.assertEqual(te['completed_tool_receipts'], 0)
        self.assertEqual(te['session_id'], 'sess-1')
        self.assertIn('ECONNRESET', te['message'])
        # 原 error carrier 不被改写，session/tool 身份可追溯。
        self.assertEqual(ev['result'], carrier)
        self.assertEqual(f['zero_tool_engineering'], False)

    def test_two_successful_tools_then_transport_failure(self):
        carrier = 'API Error: Unable to connect to API (ECONNRESET)'
        ev = {'subtype': 'success', 'is_error': True, 'result': carrier,
              'session_id': 'sess-2'}
        f = cd.classify_result(_summary(ev, ok=('u1', 'u2'), tool_use_count=2,
                                        session='sess-2'),
                               1, '', is_engineering_task=True)
        self.assertFalse(f['protocol_success'])
        self.assertFalse(f['execution_success'])
        self.assertEqual(f['ok_tool_receipts'], 2)
        self.assertEqual(f['failure_kind'], 'transport_error')
        te = f['transport_error']
        self.assertEqual(te['completed_tool_receipts'], 2)
        self.assertEqual(te['completed_tool_receipt_ids'], ['u1', 'u2'])
        self.assertEqual(te['session_id'], 'sess-2')

    def test_success_report_containing_econnreset_not_transport(self):
        ev = {'subtype': 'success', 'is_error': False, 'session_id': 's',
              'result': '本报告说明 ECONNRESET 的复现方式；共处理 429 条记录，全部成功。'}
        f = cd.classify_result(_summary(ev, ok=('u1',)), 0, '',
                               is_engineering_task=True)
        self.assertTrue(f['protocol_success'])
        self.assertTrue(f['execution_success'])
        self.assertFalse(f['quota_429'])
        self.assertIsNone(f['failure_kind'])
        self.assertIsNone(f['transport_error'])

    def test_assistant_and_stderr_econnreset_not_scanned(self):
        s = _summary({'subtype': 'success', 'is_error': False, 'result': 'ok'},
                     ok=('u1',))
        s['assistant_text'] = 'ECONNRESET appears in an assistant paragraph'
        f = cd.classify_result(s, 0, 'ECONNRESET on stderr only',
                               is_engineering_task=True)
        self.assertIsNone(f['failure_kind'])
        self.assertIsNone(f['transport_error'])

    def test_quota_429_not_overridden_by_transport(self):
        ev = {'subtype': 'error_during_execution', 'is_error': True,
              'result': '429 Too Many Requests (ECONNRESET)', 'session_id': 's'}
        f = cd.classify_result(_summary(ev), 1, '', is_engineering_task=False)
        self.assertTrue(f['quota_429'])
        self.assertIsNone(f['failure_kind'])
        self.assertIsNone(f['transport_error'])

    def test_auth_and_payment_errors_not_transport(self):
        for text in ('API Error: 401 Unauthorized',
                     'API Error: 402 Payment Required'):
            ev = {'subtype': 'error_during_execution', 'is_error': True,
                  'result': text, 'session_id': 's'}
            f = cd.classify_result(_summary(ev), 1, '', is_engineering_task=False)
            self.assertFalse(f['quota_429'], text)
            self.assertIsNone(f['failure_kind'], text)
            self.assertIsNone(f['transport_error'], text)

    def test_http_auth_payment_error_with_embedded_transport_code_not_transport(self):
        # 反例 1：错误文本夹带 ECONNRESET，但结构化状态明确是 401 认证错误；
        # 认证/支付优先，绝不改判 transport。
        ev = {'subtype': 'success', 'is_error': True,
              'result': 'API Error: 401 Unauthorized (previous transport ECONNRESET)',
              'errors_info': [{'status': 401, 'category': 'auth'}],
              'session_id': 's'}
        f = cd.classify_result(_summary(ev), 1, '', is_engineering_task=False)
        self.assertFalse(f['quota_429'])
        self.assertIsNone(f['failure_kind'])
        self.assertIsNone(f['transport_error'])
        ev2 = {'subtype': 'success', 'is_error': True,
               'result': 'API Error: 402 Payment Required (ECONNRESET)',
               'errors_info': [{'status_code': 402, 'category': 'payment'}],
               'session_id': 's'}
        f2 = cd.classify_result(_summary(ev2), 1, '', is_engineering_task=False)
        self.assertIsNone(f2['failure_kind'])
        self.assertIsNone(f2['transport_error'])

    def test_structured_errors_info_headers_not_copied(self):
        # 反例 2：结构化载体只读白名单 code/明确 message 字段；headers 绝不进
        # transport.message（不序列化整个 dict）。
        marker = 'Bearer synthetic-sensitive-marker'
        ev = {'subtype': 'error_during_execution', 'is_error': True,
              'errors_info': {'code': 'ECONNRESET',
                              'headers': {'Authorization': marker}},
              'session_id': 's'}
        f = cd.classify_result(_summary(ev), 1, '', is_engineering_task=True)
        te = f['transport_error']
        self.assertEqual(f['failure_kind'], 'transport_error')
        self.assertEqual(te['code'], 'ECONNRESET')
        self.assertEqual(te['source'], 'errors_info')
        self.assertEqual(te['message'], 'ECONNRESET')
        facts_text = json.dumps(f, ensure_ascii=False)
        self.assertNotIn(marker, facts_text)
        self.assertNotIn('synthetic-sensitive-marker', facts_text)
        self.assertNotIn('Authorization', json.dumps(te, ensure_ascii=False))
        self.assertNotIn('headers', json.dumps(te, ensure_ascii=False))

    def test_business_test_failure_mentioning_network_code_not_transport(self):
        # 反例 3：终态业务测试报告引用 ECONNRESET ≠ CLI 传输失败。
        text = ('FAILED test_network.py: AssertionError expected ECONNRESET '
                'in mocked response')
        ev = {'subtype': 'error_during_execution', 'is_error': True,
              'result': text, 'session_id': 's'}
        f = cd.classify_result(_summary(ev), 1, '', is_engineering_task=True)
        self.assertIsNone(f['failure_kind'])
        self.assertIsNone(f['transport_error'])

    def test_permission_denial_not_transport(self):
        s = cd.summarize_stream(_jsonl([
            {'type': 'result', 'subtype': 'success', 'is_error': True,
             'session_id': 's',
             'result': 'Permission to use Edit has been denied.'}]).splitlines())
        f = cd.classify_result(s, 1, '', is_engineering_task=True)
        self.assertGreaterEqual(f['approval_denials'], 1)
        self.assertIsNone(f['failure_kind'])
        self.assertIsNone(f['transport_error'])

    def test_business_test_failure_not_transport(self):
        s = cd.summarize_stream(_jsonl([
            {'type': 'result', 'subtype': 'error_during_execution', 'is_error': True,
             'session_id': 's',
             'result': 'FAILED tests/test_socket.py::test_x - AssertionError: 2 != 3'}]
        ).splitlines())
        f = cd.classify_result(s, 1, '', is_engineering_task=True)
        self.assertIsNone(f['failure_kind'])
        self.assertIsNone(f['transport_error'])

    def test_carrier_without_network_code_not_guessed(self):
        for text in ('API Error: Unable to connect to API',
                     'handler ECONNRESET_HANDLER and xECONNRESET are identifiers'):
            ev = {'subtype': 'success', 'is_error': True, 'result': text,
                  'session_id': 's'}
            f = cd.classify_result(_summary(ev), 1, '', is_engineering_task=False)
            self.assertIsNone(f['failure_kind'], text)
            self.assertIsNone(f['transport_error'], text)

    def test_structured_errors_info_code_detected(self):
        ev = {'subtype': 'error_during_execution', 'is_error': True,
              'result': 'API Error: Unable to connect to API',
              'errors_info': {'code': 'ETIMEDOUT', 'message': 'request timed out'},
              'session_id': 's'}
        f = cd.classify_result(_summary(ev), 1, '', is_engineering_task=False)
        te = f['transport_error']
        self.assertEqual(f['failure_kind'], 'transport_error')
        self.assertEqual(te['code'], 'ETIMEDOUT')
        self.assertEqual(te['source'], 'errors_info')
        self.assertIn('request timed out', te['message'])

    def test_structured_result_errors_code_detected(self):
        ev = {'subtype': 'error_during_execution', 'is_error': True,
              'result': 'API Error: Unable to connect to API',
              'result_errors': [{'code': 'ECONNREFUSED',
                                 'message': 'connect ECONNREFUSED 127.0.0.1:443'}],
              'session_id': 's'}
        f = cd.classify_result(_summary(ev), 1, '', is_engineering_task=False)
        te = f['transport_error']
        self.assertEqual(te['code'], 'ECONNREFUSED')
        self.assertEqual(te['source'], 'result_errors')
        self.assertIn('127.0.0.1:443', te['message'])

    def test_cli_version_from_system_init_only(self):
        s = cd.summarize_stream(_jsonl([
            {'type': 'system', 'subtype': 'init', 'claude_code_version': '2.1.76',
             'session_id': 's'},
            {'type': 'result', 'subtype': 'success', 'is_error': False,
             'session_id': 's', 'result': 'ok'}]).splitlines())
        self.assertEqual(s['cli_version'], '2.1.76')
        f = cd.classify_result(s, 0, '', is_engineering_task=False)
        self.assertEqual(f['cli_version'], '2.1.76')
        # 无 init 字段 ⇒ None，绝不另起进程猜版本。
        s2 = cd.summarize_stream(_jsonl([
            {'type': 'result', 'subtype': 'success', 'is_error': False,
             'session_id': 's', 'result': 'ok'}]).splitlines())
        self.assertIsNone(s2['cli_version'])


def _stream(result_text, *, tools=(), session='sess-t', version='2.1.76'):
    events = [{'type': 'system', 'subtype': 'init',
               'claude_code_version': version, 'session_id': session}]
    if tools:
        events.append({'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'name': name, 'id': tid}
            for tid, name in tools]}})
        events.append({'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': tid}
            for tid, _ in tools]}})
    events.append({'type': 'result', 'subtype': 'success', 'is_error': True,
                   'result': result_text, 'session_id': session})
    return _jsonl(events)


class TransportLifecycleTests(unittest.TestCase):
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

    def cfg(self):
        return {'executable': str(self.base / 'claude.exe'), 'model': 'deepseek-flash',
                'tools': ['Read'], 'allowed_tools': ['Read'], 'setting_sources': [],
                'credential_source': {'mode': 'token_env', 'env': 'ANTHROPIC_API_KEY'}}

    def sha(self):
        import hashlib
        return hashlib.sha256(self.prompt.read_bytes()).hexdigest()

    def run_stream(self, stream):
        return cd.run(self.cfg(), workspace=str(self.ws), prompt_file=self.prompt,
                      out_dir=self.out, stage='BW-CLAUDE-TRANSPORT-20261018-S1',
                      task_id='t1', prompt_sha256=self.sha(), now=OFFPEAK,
                      popen_factory=FakePopen(stream),
                      environ={'ANTHROPIC_API_KEY': SECRET}, dispatch_store=self.store,
                      child_prober=lambda pid: {'state': 'dead', 'created': 'c1'},
                      is_engineering_task=True, bind_child_pid=True,
                      required_tools=['Read'])

    def _active(self):
        return dp.status(self.store, now=OFFPEAK)['pools']['claude:deepseek-flash']['active']

    def test_zero_tool_transport_persisted_and_released(self):
        carrier = 'API Error: Unable to connect to API (ECONNRESET)'
        out = self.run_stream(_stream(carrier))
        self.assertTrue(out['sent'], out)
        facts = out['facts']
        self.assertEqual(facts['failure_kind'], 'transport_error')
        self.assertEqual(facts['transport_error']['code'], 'ECONNRESET')
        self.assertEqual(facts['transport_error']['completed_tool_receipts'], 0)
        self.assertEqual(facts['transport_error']['session_id'], 'sess-t')
        self.assertFalse(facts['protocol_success'])
        self.assertFalse(facts['execution_success'])
        self.assertFalse(out['passed'])
        self.assertTrue(out['capacity_release'].get('released'), out['capacity_release'])
        self.assertEqual(self._active(), 0)
        # persisted facts 可回读
        res = json.loads(Path(out['evidence']['result_json']).read_text(encoding='utf-8'))
        self.assertEqual(res['facts']['failure_kind'], 'transport_error')
        self.assertEqual(res['facts']['transport_error']['code'], 'ECONNRESET')
        summary = json.loads(Path(out['evidence']['summary_json']).read_text(
            encoding='utf-8'))
        self.assertIn('evidence_sha256', summary)
        # request 中的 CLI 版本来自 system/init 原件字段
        req = json.loads(Path(out['evidence']['request_path']).read_text(encoding='utf-8'))
        self.assertEqual(req['cli_version'], '2.1.76')
        # events 保留原 error carrier 与 session，不裁剪、不改写
        events = [json.loads(line) for line in Path(
            out['evidence']['events_jsonl']).read_text(encoding='utf-8').splitlines()
            if line.strip()]
        result_events = [e for e in events if e.get('type') == 'result']
        self.assertEqual(result_events[0]['result'], carrier)
        self.assertEqual(result_events[0]['session_id'], 'sess-t')

    def test_two_tool_receipts_then_transport_readback(self):
        carrier = 'API Error: Unable to connect to API (ECONNRESET)'
        out = self.run_stream(_stream(carrier, tools=(('u1', 'Read'), ('u2', 'Read'))))
        self.assertTrue(out['sent'], out)
        facts = out['facts']
        self.assertEqual(facts['ok_tool_receipts'], 2)
        self.assertEqual(facts['failure_kind'], 'transport_error')
        self.assertEqual(facts['transport_error']['completed_tool_receipts'], 2)
        self.assertEqual(facts['transport_error']['completed_tool_receipt_ids'],
                         ['u1', 'u2'])
        self.assertFalse(out['passed'])
        self.assertEqual(self._active(), 0)
        res = json.loads(Path(out['evidence']['result_json']).read_text(encoding='utf-8'))
        self.assertEqual(res['facts']['transport_error']['completed_tool_receipts'], 2)

    def test_http_401_with_embedded_transport_persisted_not_transport(self):
        # 反例 1 的持久化回读：认证错误优先，落盘 facts 也不得标 transport；
        # 真实退出仍走既有终态释放。
        carrier = 'API Error: 401 Unauthorized (previous transport ECONNRESET)'
        stream = _jsonl([
            {'type': 'system', 'subtype': 'init',
             'claude_code_version': '2.1.76', 'session_id': 'sess-auth'},
            {'type': 'result', 'subtype': 'success', 'is_error': True,
             'result': carrier,
             'errors_info': [{'status': 401, 'category': 'auth'}],
             'session_id': 'sess-auth'}])
        out = self.run_stream(stream)
        self.assertTrue(out['sent'], out)
        self.assertIsNone(out['facts']['failure_kind'])
        self.assertIsNone(out['facts']['transport_error'])
        self.assertFalse(out['passed'])
        self.assertEqual(self._active(), 0)
        res = json.loads(Path(out['evidence']['result_json']).read_text(encoding='utf-8'))
        self.assertIsNone(res['facts']['failure_kind'])
        self.assertIsNone(res['facts']['transport_error'])
        events = [json.loads(line) for line in Path(
            out['evidence']['events_jsonl']).read_text(encoding='utf-8').splitlines()
            if line.strip()]
        result_events = [e for e in events if e.get('type') == 'result']
        self.assertEqual(result_events[0]['result'], carrier)
        self.assertEqual(result_events[0]['session_id'], 'sess-auth')

    def test_structured_headers_not_persisted_into_transport_facts(self):
        # 反例 2 的持久化回读：原事件保留，但 transport facts 只含 code，绝不复制 headers。
        marker = 'Bearer synthetic-sensitive-marker'
        stream = _jsonl([
            {'type': 'system', 'subtype': 'init',
             'claude_code_version': '2.1.76', 'session_id': 'sess-h'},
            {'type': 'result', 'subtype': 'error_during_execution', 'is_error': True,
             'errors_info': {'code': 'ECONNRESET',
                             'headers': {'Authorization': marker}},
             'session_id': 'sess-h'}])
        out = self.run_stream(stream)
        self.assertTrue(out['sent'], out)
        self.assertEqual(out['facts']['failure_kind'], 'transport_error')
        self.assertEqual(out['facts']['transport_error']['code'], 'ECONNRESET')
        self.assertEqual(out['facts']['transport_error']['message'], 'ECONNRESET')
        self.assertEqual(self._active(), 0)
        res = json.loads(Path(out['evidence']['result_json']).read_text(encoding='utf-8'))
        self.assertNotIn(marker, json.dumps(res['facts'], ensure_ascii=False))
        self.assertEqual(res['facts']['transport_error']['message'], 'ECONNRESET')

    def test_original_error_redacted_but_preserved(self):
        carrier = ('API Error: Unable to connect to API (ECONNRESET) '
                   f'token={SECRET}')
        out = self.run_stream(_stream(carrier))
        self.assertTrue(out['sent'], out)
        for f in Path(out['evidence']['events_jsonl']).parent.glob('*'):
            if f.is_file():
                self.assertNotIn(SECRET, f.read_text(encoding='utf-8'), f.name)
        events_text = Path(out['evidence']['events_jsonl']).read_text(encoding='utf-8')
        self.assertIn('ECONNRESET', events_text)
        self.assertIn('API Error', events_text)
        self.assertIn('[REDACTED_TOKEN]', events_text)
        res = json.loads(Path(out['evidence']['result_json']).read_text(encoding='utf-8'))
        msg = res['facts']['transport_error']['message']
        self.assertIn('ECONNRESET', msg)
        self.assertIn('[REDACTED_TOKEN]', msg)
        self.assertNotIn(SECRET, msg)

    def test_existing_unknown_placeholder_not_released(self):
        # 已有 unknown 生命周期保持不变：communicate 异常 ⇒ 保留占位，绝不假释放。
        fake = FakePopen('', raise_comm=RuntimeError('interrupted'))
        out = cd.run(self.cfg(), workspace=str(self.ws), prompt_file=self.prompt,
                     out_dir=self.out, stage='BW-CLAUDE-TRANSPORT-20261018-S1',
                     task_id='t1', prompt_sha256=self.sha(), now=OFFPEAK,
                     popen_factory=fake,
                     environ={'ANTHROPIC_API_KEY': SECRET}, dispatch_store=self.store,
                     child_prober=lambda pid: {'state': 'dead', 'created': 'c1'},
                     is_engineering_task=True, bind_child_pid=False,
                     required_tools=['Read'])
        self.assertTrue(out.get('launch_unknown'), out)
        self.assertFalse(out['sent'])
        self.assertEqual(self._active(), 1)


if __name__ == '__main__':
    unittest.main()
