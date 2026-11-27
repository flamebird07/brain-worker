"""BW-MAC-COMPAT-20261114-S2：zcode_appserver_direct 传输离线回归。

全部走 tests/stub_zcode_appserver.py 合成协议 stub：零网络、零官方运行时、
零模型调用、零真实库写入（dispatch/quota store 都是每用例临时 SQLite）。
固化契约：门序列（availability→容量→额度→Popen）、stdio 协议闭环、
权限 fail-closed（默认 deny、--allow-tools 显式放行）、超时 ≠ 429、
真实退出结算与 unknown 保留、证据契约（request/preflight/events/result/
response/summary + SHA）。
"""
import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'scripts'
for p in (str(SCRIPTS), str(REPO / 'tests')):
    if p not in sys.path:
        sys.path.insert(0, p)

import dispatch_pool as dp  # noqa: E402

ENTRY = SCRIPTS / 'zcode_appserver_direct.py'
STUB = REPO / 'tests' / 'stub_zcode_appserver.py'
STAGE = 'mac-transport-s1'


class AppServerTransportTests(unittest.TestCase):
    """每用例独立临时工作区/输出/库；子进程执行真实入口与真实 stub。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='bw-appserver-'))
        self.ws = self.tmp / 'ws'
        self.ws.mkdir()
        self.prompt = self.tmp / 'prompt.txt'
        self.prompt.write_text('只做只读检查，报告 stage 标记。\n', encoding='utf-8')
        self.dispatch = self.tmp / 'dispatch.sqlite3'
        self.quota = self.tmp / 'quota.sqlite3'
        self.evidence = self.tmp / 'stub-evidence.jsonl'
        self.cfg = self.tmp / 'appserver-entry.json'
        self._write_config(mode=os.environ.get('BW_STUB_DEFAULT_MODE', 'ok'))

    def _write_config(self, mode, extra_env=None):
        env = {'STUB_MODE': mode, 'STUB_STAGE': STAGE,
               'STUB_WORKSPACE': str(self.ws),
               'STUB_EVIDENCE_FILE': str(self.evidence)}
        env.update(extra_env or {})
        self.cfg.write_text(json.dumps({
            'command': [sys.executable, str(STUB)],
            'environment': env,
            '_notes': ['offline stub config; generated per-test'],
        }, ensure_ascii=False, indent=2), encoding='utf-8')

    def _out(self, name='out'):
        return self.tmp / name

    def _run(self, *extra, timeout=180):
        # BW-WEAK-RESTRUCTURE-20261116：弱档 ZCode 只认 GLM-5.3-Flash（GLM-5.3 在弱档
        # mode_excluded）——传输测试统一用弱档可派模型；默认档位文件不存在 → weak。
        extra = ('--model', 'GLM-5.3-Flash', *extra)
        env = {**os.environ.copy(), 'PYTHONUTF8': '1'}
        cmd = [sys.executable, str(ENTRY),
               '--workspace', str(self.ws),
               '--prompt-file', str(self.prompt),
               '--output-dir', str(self._out()),
               '--stage', STAGE,
               '--config', str(self.cfg),
               '--dispatch-store', str(self.dispatch),
               '--quota-store', str(self.quota),
               *extra]
        return subprocess.run(cmd, capture_output=True, env=env, timeout=timeout)

    def _stub_rows(self):
        if not self.evidence.exists():
            return []
        return [json.loads(l) for l in
                self.evidence.read_text(encoding='utf-8').splitlines() if l.strip()]

    def _stub_saw(self):
        for row in reversed(self._stub_rows()):
            if 'saw' in row:
                return row['saw']
        return {}

    def _stub_recv_methods(self):
        return [row['recv'].get('method') for row in self._stub_rows()
                if 'recv' in row and isinstance(row.get('recv'), dict)]

    def _attempts(self, task_id=None):
        # BW-WEAK-TIER-ENFORCE-20261122：弱档队列占位（pre-weak-*）与被测任务行共存，
        # 断言一律按 task_id 过滤（None=不过滤）。
        with contextlib.closing(sqlite3.connect(str(self.dispatch))) as conn:
            conn.row_factory = sqlite3.Row
            try:
                if task_id is None:
                    return [dict(r) for r in conn.execute(
                        'SELECT * FROM attempts').fetchall()]
                return [dict(r) for r in conn.execute(
                    'SELECT * FROM attempts WHERE task_id=?', (task_id,)).fetchall()]
            except sqlite3.OperationalError:
                return []  # 从未触库（如预检路径）：无表即无行。

    def _quota_locks(self):
        """额度库的 workspace/channel 占位行（预检泄漏负例的核对面）。"""
        with contextlib.closing(sqlite3.connect(str(self.quota))) as conn:
            conn.row_factory = sqlite3.Row
            rows = []
            for table in ('workspace_locks', 'channel_locks'):
                try:
                    rows.extend(dict(r) for r in conn.execute(
                        f'SELECT * FROM {table}').fetchall())
                except sqlite3.OperationalError:
                    pass
            return rows

    # ---------------- happy path ----------------
    def test_happy_path_full_loop_and_binding(self):
        self._occupy_higher_weak_slots()
        proc = self._run()
        self.assertEqual(proc.returncode, 0,
                         proc.stdout.decode('utf-8', 'replace')
                         + proc.stderr.decode('utf-8', 'replace'))
        out = self._out()
        summary = json.loads((out / 'summary.json').read_text(encoding='utf-8'))
        self.assertTrue(summary['protocol_success'], summary)
        self.assertTrue(summary['submitted'])
        self.assertTrue(summary['turn_ended'])
        self.assertIsNone(summary['timed_out'] or None)
        self.assertEqual(summary['session_id'], 'sess_stub_1')
        self.assertEqual(summary['turn_id'], 'turn_stub_1')
        self.assertEqual(summary['permission_denied'], 0)
        # 证据契约完整且 response 哈希可回读。
        self.assertTrue((out / 'request.json').is_file())
        self.assertTrue((out / 'preflight.json').is_file())
        self.assertTrue((out / 'events.jsonl').is_file())
        self.assertTrue((out / 'response.md').is_file())
        self.assertTrue((out / 'messages.json').is_file())
        disk = (out / 'response.md').read_bytes()
        self.assertEqual(summary['response_sha256'],
                         __import__('hashlib').sha256(disk).hexdigest())
        # 九节绑定链离线端到端成立。
        self.assertTrue(summary['report_state']['body_ok'],
                        summary['report_state'])
        self.assertTrue(summary['report_state']['binding']['bound'])
        # stub 视角：模式映射、模型显式绑定、契约前置。
        saw = self._stub_saw()
        self.assertEqual(saw['setMode'], 'plan')
        # BW-WEAK-RESTRUCTURE-20261116：弱档 ZCode 专用 GLM-5.3-Flash——stub 视角的
        # 显式 setModel 绑定应为该模型。
        self.assertEqual(saw['setModel']['modelId'], 'GLM-5.3-Flash')
        self.assertEqual(saw['setModel']['providerId'],
                         'account:bigmodel-individual-coding-plan')
        self.assertEqual(saw['setModel']['options']['reasoningLevel'], 'low')
        self.assertIn(STAGE, saw['send_content'])
        self.assertIn('WORKER_REPORT_START', saw['send_content'])
        self.assertTrue(saw['close'])
        # 容量已以真实终态释放。
        rows = self._attempts(task_id='mac-transport-s1')
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]['state'], 'finished')
        # 协议原件有双向记录。
        events = [json.loads(l) for l in
                  (out / 'events.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertTrue(any(e['dir'] == 'send' for e in events))
        self.assertTrue(any(e['dir'] == 'recv' for e in events))

    def test_edit_mode_maps_to_edit_session_mode(self):
        self._occupy_higher_weak_slots()
        self._write_config('ok')
        proc = self._run('--mode', 'edit')
        self.assertEqual(proc.returncode, 0, proc.stdout.decode('utf-8'))
        self.assertEqual(self._stub_saw()['setMode'], 'edit')

    # ---------------- 权限 fail-closed ----------------
    def test_permission_request_denied_by_default_and_recorded(self):
        self._occupy_higher_weak_slots()
        self._write_config('permission_request')
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout.decode('utf-8'))
        summary = json.loads((self._out() / 'summary.json')
                             .read_text(encoding='utf-8'))
        self.assertEqual(summary['permission_denied'], 1)
        self.assertEqual(summary['permission_auto_allowed'], 0)
        reply = self._stub_saw()['permission_reply']
        self.assertEqual(reply['result']['optionId'], 'opt-deny')
        self.assertEqual(reply['result']['kind'], 'deny')
        # 拒绝事件进了 request.json 的 permission_policy 与 summary 的 evidence。
        req = json.loads((self._out() / 'request.json').read_text(encoding='utf-8'))
        self.assertEqual(req['permission_policy']['default'].startswith('deny'), True)
        self.assertEqual(len(summary['permission_events']), 1)

    def test_allow_tools_grants_allowonce_only_for_listed_tool(self):
        self._occupy_higher_weak_slots()
        self._write_config('permission_request')
        proc = self._run('--allow-tools', 'Bash')
        self.assertEqual(proc.returncode, 0, proc.stdout.decode('utf-8'))
        summary = json.loads((self._out() / 'summary.json')
                             .read_text(encoding='utf-8'))
        self.assertEqual(summary['permission_auto_allowed'], 1)
        self.assertEqual(summary['permission_denied'], 0)
        reply = self._stub_saw()['permission_reply']
        self.assertEqual(reply['result']['optionId'], 'opt-allow')
        self.assertEqual(reply['result']['kind'], 'allowOnce')

    def test_runtime_preferences_reply_carries_failclosed_defaults(self):
        self._occupy_higher_weak_slots()
        self._write_config('runtime_prefs')
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout.decode('utf-8'))
        reply = self._stub_saw()['runtime_prefs_reply']
        self.assertEqual(reply['result']['modelContextBudgetStrategy'],
                         'preflight-v1')
        self.assertFalse(reply['result']['nativeSearchEnhancementsEnabled'])
        self.assertEqual(reply['result']['toolSearchMode'], 'off')

    def test_unknown_server_request_gets_error_reply(self):
        self._occupy_higher_weak_slots()
        self._write_config('unknown_request')
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout.decode('utf-8'))
        summary = json.loads((self._out() / 'summary.json')
                             .read_text(encoding='utf-8'))
        self.assertEqual(summary['unsupported_server_requests'], 1)
        reply = self._stub_saw()['unknown_request_reply']
        self.assertIn('error', reply)

    # ---------------- 派发前拒绝（零目录、零占位） ----------------
    def test_tools_flag_refused_on_this_transport(self):
        proc = self._run('--tools', 'Read')
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(self._out().exists())
        payload = json.loads(proc.stdout.decode('utf-8'))
        self.assertFalse(payload['sent'])
        self.assertTrue(any('not supported' in r for r in payload['reasons']))

    def test_missing_config_refused_with_zero_dir(self):
        proc = self._run('--config', str(self.tmp / 'nope.json'))
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(self._out().exists())

    def _occupy_higher_weak_slots(self):
        # BW-WEAK-TIER-ENFORCE-20261122：弱档纯优先级队列——先占满层1-3（两 OpenCode/
        # CodeBuddy GLM/Claude×2 共 5 槽），zcode:GLM-5.3-Flash 才成为队首可被本传输
        # 真实 claim；队列判断只看层序与空位。
        combos = [('opencode', dp.OPENCODE_DEEPSEEK_MODEL),
                  ('codebuddy', dp.CODEBUDDY_GLM_MODEL),
                  ('claude', 'deepseek-flash'),
                  ('claude', 'deepseek-flash')]
        for i, (rt, m) in enumerate(combos):
            r = dp.consume_for_entry(
                str(self.dispatch), task_id=f'pre-weak-{i}', runtime=rt, model=m,
                workspace=str(self.tmp / f'pre-weak-{i}'),
                prompt_sha256='1' * 64, stage='pre-weak', executor=rt,
                quota_store=str(self.quota))
            assert r.get('allowed'), (rt, m, r)

    def _readonly_state_dir(self):
        """共享状态隔离目录：只读（模拟受限会话对 ~/.brain-worker 的权限形态）。"""
        ro = self.tmp / 'ro-state'
        ro.mkdir()
        os.chmod(ro, 0o555)
        if os.getuid() == 0 or os.access(ro / 'w', os.W_OK):
            try:
                (ro / 'w').touch()
                os.chmod(ro, 0o755)
                self.skipTest('宿主可写只读目录（root/ACL），无法模拟受限会话')
            except OSError:
                pass
        self.addCleanup(os.chmod, ro, 0o755)
        return ro

    def test_preflight_zero_shared_state_writes_survives_unwritable_store(self):
        # BW-WEAK-RESTRUCTURE-20261116-R2：--preflight-only 零共享状态写入——共享状态
        # 目录只读时预检仍成功（capabilities 只读往返），绝不建库/迁移/WAL。
        ro = self._readonly_state_dir()
        proc = self._run('--preflight-only',
                         '--quota-store', str(ro / 'quota-state.sqlite3'),
                         '--dispatch-store', str(ro / 'dispatch-pool.sqlite3'))
        self.assertEqual(proc.returncode, 0, proc.stdout.decode('utf-8', 'replace')
                         + proc.stderr.decode('utf-8', 'replace'))
        self.assertFalse((ro / 'quota-state.sqlite3').exists(),
                         'preflight 绝不创建共享额度库')
        self.assertFalse((ro / 'dispatch-pool.sqlite3').exists(),
                         'preflight 绝不创建共享调度库')
        summary = json.loads((self._out() / 'summary.json').read_text(encoding='utf-8'))
        self.assertTrue(summary['protocol_success'])
        self.assertFalse(summary['submitted'])
        self.assertEqual(json.loads((self._out() / 'preflight.json')
                                    .read_text(encoding='utf-8'))['ok'], True)

    def test_unwritable_shared_state_structured_refusal_not_traceback(self):
        # BW-WEAK-RESTRUCTURE-20261116-R2：真实派发遇共享状态库打不开/写不进 → 结构化
        # 拒绝（sent=false、shared_state_error、阶段/路径/原始异常），退出码 2；
        # 绝不输出裸 traceback，绝不误分类为认证/额度/模型错误，零证据目录。
        ro = self._readonly_state_dir()
        proc = self._run('--quota-store', str(ro / 'quota-state.sqlite3'),
                         '--dispatch-store', str(ro / 'dispatch-pool.sqlite3'))
        self.assertEqual(proc.returncode, 2)
        payload = json.loads(proc.stdout.decode('utf-8', 'replace'))
        self.assertTrue(payload['refused_before_dispatch'])
        self.assertFalse(payload['sent'])
        self.assertTrue(payload['shared_state_error'])
        self.assertEqual(payload['error_class'], 'shared_state_unwritable')
        self.assertIn(payload['phase'],
                      ('availability_gate', 'capacity_claim', 'quota_gate'))
        joined = json.dumps(payload, ensure_ascii=False)
        self.assertIn('NOT an auth/quota/time-window/model error', joined)
        self.assertNotIn('Traceback', proc.stderr.decode('utf-8', 'replace'))
        self.assertFalse(self._out().exists(), '拒绝路径零证据目录')

    def test_capacity_full_refused_with_zero_dir_and_no_placeholder(self):
        # BW-WEAK-RESTRUCTURE-20261116：弱档 ZCode 只认 GLM-5.3-Flash——先占满
        # zcode:GLM-5.3-Flash（容量 1）：用另一个 task 的真实 running attempt。
        self._occupy_higher_weak_slots()
        blocker = dp.consume_for_entry(
            str(self.dispatch), task_id='blocker', runtime='zcode',
            model='GLM-5.3-Flash', workspace=str(self.tmp / 'other'),
            prompt_sha256='0' * 64, stage='blocker', executor='zcode',
            quota_store=str(self.quota))
        self.assertTrue(blocker['allowed'], blocker)
        proc = self._run('--task-id', 'real-task')
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(self._out().exists())
        payload = json.loads(proc.stdout.decode('utf-8'))
        self.assertTrue(payload.get('capacity_gate_rejected'))
        self.assertFalse(payload['sent'])
        # blocker 的占位仍在（绝不误释放别人）。
        states = [r['state'] for r in self._attempts(task_id='blocker')]
        self.assertEqual(states, ['running'], states)

    # ---------------- Popen 后拒绝 / 真实失败 ----------------
    def test_setmodel_refusal_settles_start_failed_and_releases(self):
        self._occupy_higher_weak_slots()
        self._write_config('model_refuse')
        proc = self._run()
        self.assertEqual(proc.returncode, 3)
        payload = json.loads(proc.stdout.decode('utf-8'))
        self.assertFalse(payload['sent'])
        self.assertTrue(payload['capacity_released'], payload)
        rows = self._attempts(task_id='mac-transport-s1')
        self.assertEqual([r['state'] for r in rows], ['start_failed'], rows)
        self.assertFalse(self._stub_saw().get('send_content'))

    def test_timeout_kills_child_settles_real_exit(self):
        self._occupy_higher_weak_slots()
        self._write_config('hang')
        proc = self._run('--timeout', '3', timeout=60)
        self.assertEqual(proc.returncode, 3)
        summary = json.loads((self._out() / 'summary.json')
                             .read_text(encoding='utf-8'))
        self.assertTrue(summary['timed_out'])
        self.assertTrue(summary['submitted'])
        self.assertFalse(summary['protocol_success'])
        self.assertTrue(summary['capacity_released'])
        rows = self._attempts(task_id='mac-transport-s1')
        self.assertEqual([r['state'] for r in rows], ['finished'], rows)
        self.assertNotIn('429', json.dumps(summary))

    def test_crash_after_send_is_real_failure_and_releases(self):
        self._occupy_higher_weak_slots()
        self._write_config('crash_after_send')
        proc = self._run()
        self.assertEqual(proc.returncode, 3)
        summary = json.loads((self._out() / 'summary.json')
                             .read_text(encoding='utf-8'))
        self.assertTrue(summary['submitted'])
        self.assertFalse(summary['turn_ended'])
        self.assertFalse(summary['protocol_success'])
        self.assertTrue(summary['capacity_released'])
        self.assertEqual(summary['report_state'].get('carrier_missing'), True)

    # ---------------- 预检 ----------------
    def test_preflight_only_creates_no_session_and_no_capacity(self):
        proc = self._run('--preflight-only')
        self.assertEqual(proc.returncode, 0, proc.stdout.decode('utf-8'))
        out = self._out()
        summary = json.loads((out / 'summary.json').read_text(encoding='utf-8'))
        self.assertTrue(summary['protocol_success'])
        self.assertFalse(summary['submitted'])
        preflight = json.loads((out / 'preflight.json').read_text(encoding='utf-8'))
        self.assertTrue(preflight['ok'])
        self.assertNotIn('session/create', self._stub_recv_methods())
        self.assertNotIn('session/send', self._stub_recv_methods())
        self.assertEqual(self._attempts(), [])
        # 预检拿过额度门占位，必须同进程结算：绝不带锁退出（真实泄漏负例）。
        self.assertEqual(self._quota_locks(), [], '预检不得遗留 workspace/channel 锁')

    def test_windows_host_refused(self):
        import zcode_appserver_direct as zax
        with unittest.mock.patch.object(zax.sys, 'platform', 'win32'):
            code = zax.main(['--workspace', str(self.ws),
                             '--prompt-file', str(self.prompt),
                             '--output-dir', str(self._out('w')),
                             '--stage', STAGE])
        self.assertEqual(code, 2)
        self.assertFalse(self._out('w').exists())


if __name__ == '__main__':
    unittest.main()
