"""BW-CLAUDE-PATH-20261018-S2 离线回归：首次真实权限拒绝立即停止。

只证离线生命周期：stdout 逐行监控结构化拒绝载体，命中后立即 terminate 本 run 的
Popen、停止后续动作、保留原始 stdout/stderr/触发 tool id；成功 Read 正文引用拒绝词
与普通可恢复工具错误绝不误杀；stderr 并发 drain 不死锁；退出未知保留 unknown；
传输失败不误判权限。真实 CLI 停止/退出由 root 正反验证。
"""
import hashlib
import json
import sys
import tempfile
import threading
import time
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
SECRET = 'sk-STOP-SECRET-DO-NOT-LEAK'


def _report_body(stage, workspace):
    lines = [pc.REPORT_START,
             f'阶段编号与执行方式：{stage}；direct。',
             f'实际项目绝对路径：{workspace}',
             '汇报时间与执行环境：offline stub']
    for header in pc.SECTION_HEADERS:
        lines.append(f'{header}\n- 字段：值')
    lines.append(pc.CLOSING_LINE)
    lines.append(pc.REPORT_END)
    return '\n'.join(lines)


class _OutStream:
    def __init__(self, text, owner):
        self._lines = text.splitlines(keepends=True)
        self._index = 0
        self.owner = owner
        self.stopped = False
        self.closed = False

    def readline(self):
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


class StopFakePopen:
    def __init__(self, stdout, stderr='', terminate_rc=-15, unknown_exit=False,
                 sink=None, pid=4242):
        self._stdout = stdout
        self._stderr = stderr
        self._terminate_rc = terminate_rc
        self._unknown_exit = unknown_exit
        self.sink = sink if sink is not None else []
        self.pid = pid
        self.returncode = None
        self.terminated = False
        self.stdin = self.stdout = self.stderr = None

    def __call__(self, argv, *, cwd=None, stdin=None, stdout=None, stderr=None,
                 env=None, text=None):
        self.sink.append(self)
        self.stdin = _InStream()
        self.stdout = _OutStream(self._stdout, self)
        self.stderr = _OutStream(self._stderr, self)
        return self

    def terminate(self):
        self.terminated = True
        self.stdout.stop()
        self.stderr.stop()
        if not self._unknown_exit:
            self.returncode = self._terminate_rc

    def wait(self, timeout=None):
        if self.returncode is None and self._unknown_exit:
            return None
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def communicate(self, input=None):
        self.returncode = 0
        return self._stdout, self._stderr


class _NeverEofStream(_OutStream):
    """Yields the configured lines, then blocks (no EOF) until explicitly stopped."""

    def __init__(self, text, owner):
        super().__init__(text, owner)
        self._release = threading.Event()

    def readline(self):
        if self.stopped:
            return ''
        if self._index >= len(self._lines):
            self._release.wait()  # no EOF: proves the reader must not block the main thread
            return ''
        line = self._lines[self._index]
        self._index += 1
        return line


class HangingStopFakePopen(StopFakePopen):
    """Denial then no EOF; terminate() fails and the process never reaches a known exit."""

    def __init__(self, stdout, stderr='', pid=4243):
        super().__init__(stdout, stderr=stderr, unknown_exit=True, pid=pid)

    def __call__(self, argv, *, cwd=None, stdin=None, stdout=None, stderr=None,
                 env=None, text=None):
        self.sink.append(self)
        self.stdin = _InStream()
        self.stdout = _NeverEofStream(self._stdout, self)
        self.stderr = _NeverEofStream(self._stderr, self)
        return self

    def terminate(self):
        raise RuntimeError('terminate failed in test double')

    def wait(self, timeout=None):
        if timeout:
            time.sleep(timeout)  # simulate a bounded wait timing out; returncode stays None
        return None


class _NoStopOutStream(_OutStream):
    """Data already emitted/buffered before terminate: stop() must not discard it."""

    def stop(self):
        pass


class BufferedStopFakePopen(StopFakePopen):
    """Preloaded follow-up lines are already-emitted original output; they must be preserved."""

    def __call__(self, argv, *, cwd=None, stdin=None, stdout=None, stderr=None,
                 env=None, text=None):
        self.sink.append(self)
        self.stdin = _InStream()
        self.stdout = _NoStopOutStream(self._stdout, self)
        self.stderr = _NoStopOutStream(self._stderr, self)
        return self


class _GatedProbeStream(_OutStream):
    """After the denial, a probe is emitted only if terminate never arrives.

    terminate() releases the gate and returns EOF, so the configured probe must never
    appear in raw stdout. This proves the stop ends real follow-up instead of filtering
    evidence that was already received.
    """

    def __init__(self, text, owner, probe_line):
        super().__init__(text, owner)
        self._probe = probe_line
        self._release = threading.Event()
        self._probe_sent = False

    def readline(self):
        if self.stopped:
            return ''
        if self._index < len(self._lines):
            line = self._lines[self._index]
            self._index += 1
            return line
        if self._probe_sent:
            return ''
        self._release.wait(2.0)
        if self.stopped:
            return ''
        self._probe_sent = True
        return self._probe

    def stop(self):
        self.stopped = True
        self._release.set()


class GatedProbeFakePopen(StopFakePopen):
    def __init__(self, stdout, probe_line, stderr='', pid=4244):
        super().__init__(stdout, stderr=stderr, pid=pid)
        self._probe_line = probe_line

    def __call__(self, argv, *, cwd=None, stdin=None, stdout=None, stderr=None,
                 env=None, text=None):
        self.sink.append(self)
        self.stdin = _InStream()
        self.stdout = _GatedProbeStream(self._stdout, self, self._probe_line)
        self.stderr = _OutStream(self._stderr, self)
        return self


def _jsonl(events):
    return '\n'.join(json.dumps(event) for event in events)


class PermissionStopTests(unittest.TestCase):
    STAGE = 'BW-CLAUDE-PATH-20261018-S2'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.store = self.base / 'dispatch.sqlite3'
        self.ws = self.base / 'ws'
        self.ws.mkdir()
        self.prompt = self.base / 'prompt.txt'
        self.prompt.write_text('do the task', encoding='utf-8')
        patch = mock.patch.object(dp, 'process_identity',
                                  return_value={'state': 'alive', 'created': 'c1'})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def _cfg(self):
        return {'executable': str(self.base / 'claude.exe'), 'model': 'deepseek-flash',
                'permission_mode': 'dontAsk',
                'tools': ['Read'], 'allowed_tools': ['Read'], 'setting_sources': [],
                'credential_source': {'mode': 'token_env', 'env': 'ANTHROPIC_API_KEY'}}

    def _run(self, fake):
        return cd.run(self._cfg(), workspace=str(self.ws), prompt_file=self.prompt,
                      out_dir=self.base / 'out', stage=self.STAGE, task_id='t1',
                      prompt_sha256=hashlib.sha256(self.prompt.read_bytes()).hexdigest(),
                      now=OFFPEAK, popen_factory=fake,
                      environ={'ANTHROPIC_API_KEY': SECRET},
                      dispatch_store=self.store, bind_child_pid=True,
                      child_prober=lambda pid: {'state': 'dead', 'created': 'c1'},
                      required_tools=['Read'])

    def _read_tool_result(self, content, is_error=False, tool_id='r1'):
        return {'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': tool_id, 'is_error': is_error,
             'content': content}]}}

    def _result_report(self, body=None):
        return {'type': 'result', 'subtype': 'success', 'is_error': False,
                'session_id': 's1',
                'result': body or _report_body(self.STAGE, str(self.ws))}

    def test_first_tool_result_denial_stops_and_skips_later_probe(self):
        denial = self._read_tool_result(
            'Permission to use Edit has been denied.', is_error=True, tool_id='deny1')
        later_probe = {'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'name': 'Edit', 'id': 'probe2',
             'input': {'file_path': './other.py'}}]}}
        stream = _jsonl([denial, later_probe, self._result_report()])
        # 预加载的后续行是**已发出**的原始数据：不能用 raw 不含它来证明零执行；
        # 应核对 terminate 在首拒绝触发，且原始输出原样保存（不裁剪证据）。
        fake = BufferedStopFakePopen(stream)
        out = self._run(fake)
        self.assertTrue(out['stopped'], out)
        self.assertTrue(out['permission_stop'])
        self.assertTrue(fake.terminated)
        self.assertEqual(out['permission_stop']['tool_use_id'], 'deny1')
        self.assertEqual(out['permission_stop']['source'], 'tool_result')
        self.assertFalse(out['facts']['execution_success'])
        self.assertFalse(out['facts']['protocol_success'])
        self.assertEqual(out['facts']['permission_denials'] + out['facts']['approval_denials'], 1)
        self.assertTrue(out['facts']['permission_stop_requested'])
        self.assertTrue(out['facts']['terminated_by_worker'])
        # 已发出的后续事件仍在原始流中：竞态如实可见，证据不被裁剪。
        self.assertTrue(out['facts']['observed_followup_tool_use'])
        raw = Path(out['evidence']['raw_stdout_path']).read_text(encoding='utf-8')
        self.assertIn('Permission to use Edit has been denied.', raw)
        self.assertIn('probe2', raw)
        self.assertTrue(fake.stdin.data)
        self.assertIn('do the task', ''.join(fake.stdin.data))
        self.assertTrue(fake.stdin.closed)
        self.assertEqual(len(fake.sink), 1)
        self.assertEqual(out['capacity_release'].get('released'), True)
        self.assertEqual(
            dp.status(self.store, now=OFFPEAK)['pools']['claude:deepseek-flash']['active'], 0)
        for path in Path(out['evidence']['events_jsonl']).parent.glob('*'):
            if path.is_file():
                self.assertNotIn(SECRET, path.read_text(encoding='utf-8'), path.name)

    def test_structured_permission_denials_in_result_stops(self):
        result = {'type': 'result', 'subtype': 'success', 'is_error': False,
                  'session_id': 's1', 'permission_denials': [
                      {'tool_name': 'Edit', 'tool_use_id': 'deny9'}]}
        later = {'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'name': 'Edit', 'id': 'probe9'}]}}
        fake = BufferedStopFakePopen(_jsonl([result, later]))
        out = self._run(fake)
        self.assertTrue(out['stopped'])
        self.assertTrue(fake.terminated)
        self.assertEqual(out['permission_stop']['source'], 'permission_denials')
        self.assertEqual(out['permission_stop']['tool_use_id'], 'deny9')
        raw = Path(out['evidence']['raw_stdout_path']).read_text(encoding='utf-8')
        self.assertIn('probe9', raw)  # 已发出的后续原始事件必须保留

    def test_terminate_prevents_real_followup_after_denial(self):
        # 可控 fake：若 terminate 未到达，2 秒后就会发出 probe；到达则 EOF。
        # raw 不含 probe 是“停止真实后续”的结果，而不是过滤已收证据。
        denial = self._read_tool_result(
            'Permission to use Edit has been denied.', is_error=True, tool_id='deny1')
        fake = GatedProbeFakePopen(
            _jsonl([denial]), json.dumps({'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'name': 'Edit', 'id': 'probe-gated'}]}}) + '\n')
        out = self._run(fake)
        self.assertTrue(out['stopped'], out)
        self.assertTrue(fake.terminated)
        raw = Path(out['evidence']['raw_stdout_path']).read_text(encoding='utf-8')
        self.assertIn('Permission to use Edit has been denied.', raw)
        self.assertNotIn('probe-gated', raw)
        self.assertFalse(out['facts']['observed_followup_tool_use'])
        self.assertFalse(out['facts']['raw_stdout_partial'])

    def test_raw_stdout_preserves_success_followup_after_denial(self):
        # 队列已收到拒绝与其后的成功 result 原始行：统一保存，不因停止而裁剪。
        denial = self._read_tool_result(
            'Permission to use Edit has been denied.', is_error=True, tool_id='deny1')
        fake = BufferedStopFakePopen(_jsonl([denial, self._result_report()]))
        out = self._run(fake)
        self.assertTrue(out['stopped'])
        raw = Path(out['evidence']['raw_stdout_path']).read_text(encoding='utf-8')
        self.assertIn('Permission to use Edit has been denied.', raw)
        self.assertIn(pc.REPORT_END, raw)
        self.assertFalse(out['facts']['raw_stdout_partial'])

    def test_raw_stdout_preserves_error_followup_after_denial(self):
        # 错误原流同样不得裁剪：拒绝之后已到达的错误事件仍写入 raw。
        denial = self._read_tool_result(
            'Permission to use Edit has been denied.', is_error=True, tool_id='deny1')
        error = {'type': 'result', 'subtype': 'error', 'is_error': True,
                 'session_id': 's1', 'result': 'API Error: ECONNRESET (followup-error)'}
        fake = BufferedStopFakePopen(_jsonl([denial, error]))
        out = self._run(fake)
        self.assertTrue(out['stopped'])
        raw = Path(out['evidence']['raw_stdout_path']).read_text(encoding='utf-8')
        self.assertIn('Permission to use Edit has been denied.', raw)
        self.assertIn('followup-error', raw)
        self.assertFalse(out['facts']['raw_stdout_partial'])

    def test_successful_read_body_with_denial_words_is_not_stopped(self):
        stream = _jsonl([
            {'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'name': 'Read', 'id': 'r1'}]}},
            self._read_tool_result(
                '# docs: Permission to use Edit has been denied; quoted text only',
                is_error=False, tool_id='r1'),
            self._result_report()])
        fake = StopFakePopen(stream)
        out = self._run(fake)
        self.assertFalse(out['stopped'], out)
        self.assertIsNone(out['permission_stop'])
        self.assertFalse(fake.terminated)
        self.assertTrue(out['sent'], out)
        self.assertTrue(out['facts']['execution_success'], out['facts'])
        self.assertTrue(out['passed'], out)

    def test_ordinary_recoverable_tool_error_is_not_stopped(self):
        stream = _jsonl([
            {'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'name': 'Read', 'id': 'r1'}]}},
            self._read_tool_result('File not found: ./missing.py', is_error=True,
                                   tool_id='r1'),
            {'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'name': 'Read', 'id': 'r2'}]}},
            self._read_tool_result('ok', is_error=False, tool_id='r2'),
            self._result_report()])
        fake = StopFakePopen(stream)
        out = self._run(fake)
        self.assertFalse(out['stopped'], out)
        self.assertFalse(fake.terminated)
        self.assertTrue(out['facts']['ok_tool_receipts'] >= 1, out['facts'])

    def test_stderr_is_drained_continuously(self):
        stderr = '\n'.join(f'line {i}' for i in range(5000)) + '\n'
        stream = _jsonl([
            {'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'name': 'Read', 'id': 'r1'}]}},
            self._read_tool_result('ok', tool_id='r1'),
            self._result_report()])
        fake = StopFakePopen(stream, stderr=stderr)
        out = self._run(fake)
        self.assertTrue(out['sent'], out)
        raw_err = Path(out['evidence']['raw_stderr_path']).read_text(encoding='utf-8')
        self.assertIn('line 4999', raw_err)

    def test_unknown_exit_after_stop_keeps_placeholder(self):
        fake = StopFakePopen(
            _jsonl([self._read_tool_result('Permission to use Edit has been denied.',
                                           is_error=True, tool_id='deny1')]),
            unknown_exit=True)
        out = self._run(fake)
        self.assertTrue(out['launch_unknown'], out)
        self.assertEqual(out['permission_stop']['tool_use_id'], 'deny1')
        self.assertNotIn('capacity_release', out)
        self.assertEqual(
            dp.status(self.store, now=OFFPEAK)['pools']['claude:deepseek-flash']['active'], 1)

    def test_denial_without_eof_and_terminate_failure_returns_unknown(self):
        # 拒绝后没有 EOF、terminate 失败、进程仍活：主线程必须有界返回 unknown，
        # 不阻塞读下一行、不假释放，并把已收集 raw stdout/permission_stop/进程元数据落盘。
        denial = self._read_tool_result(
            'Permission to use Edit has been denied.', is_error=True, tool_id='deny1')
        fake = HangingStopFakePopen(_jsonl([denial]))
        with mock.patch.object(cd, '_TERMINAL_WAIT_SECONDS', 0.05), \
             mock.patch.object(cd, '_DRAIN_JOIN_SECONDS', 0.05):
            started = time.monotonic()
            out = self._run(fake)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0, 'denial stop must not block on a next line')
        self.assertTrue(out['launch_unknown'], out)
        self.assertEqual(out['permission_stop']['tool_use_id'], 'deny1')
        self.assertNotIn('capacity_release', out)
        self.assertFalse(fake.terminated)
        self.assertEqual(
            dp.status(self.store, now=OFFPEAK)['pools']['claude:deepseek-flash']['active'], 1)
        evidence = out['evidence']
        raw = Path(evidence['raw_stdout_path']).read_text(encoding='utf-8')
        self.assertIn('Permission to use Edit has been denied.', raw)
        self.assertNotIn('probe', raw)
        unknown = json.loads(Path(evidence['unknown_json']).read_text(encoding='utf-8'))
        self.assertEqual(unknown['state'], 'unknown')
        self.assertFalse(unknown['success_binding_generated'])
        self.assertIsNone(unknown['exit_code'])  # 如实 None，不伪造已退出
        self.assertTrue(unknown['raw_stdout_partial'])  # reader 未到 EOF：快照标 partial
        self.assertTrue(unknown['process']['stop_requested'])
        self.assertFalse(unknown['process']['terminated_by_worker'])
        self.assertEqual(unknown['permission_stop']['tool_use_id'], 'deny1')
        for path in Path(evidence['unknown_json']).parent.glob('*'):
            if path.is_file():
                self.assertNotIn(SECRET, path.read_text(encoding='utf-8'), path.name)

    def test_transport_failure_is_not_permission_stop(self):
        stream = _jsonl([
            {'type': 'result', 'subtype': 'success', 'is_error': True, 'session_id': 's1',
             'result': 'API Error: Unable to connect to API (ECONNRESET)'}])
        fake = StopFakePopen(stream)
        out = self._run(fake)
        self.assertFalse(out['stopped'], out)
        self.assertFalse(fake.terminated)
        self.assertEqual(out['facts']['failure_kind'], 'transport_error')
        self.assertFalse(out['facts']['permission_stop'])
        self.assertEqual(out['capacity_release'].get('released'), True)


if __name__ == '__main__':
    unittest.main()
