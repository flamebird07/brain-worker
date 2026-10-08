"""Offline deterministic tests for scripts/quota_control.py（及入口接入）。

无网络、无真实模型、无真实 CodeBuddy/ZCode CLI；全部使用显式临时 sqlite store
与临时路由配置，绝不写真实 ~/.brain-worker 状态。回放素材是本仓库内的合成离线
样本 tests/fixtures/quota-terminal.synthetic.json：仅保留 429/status6004/quota
结构与 2026-10-08 18:14:13 UTC+8 reset 窗口用于分类断言，session/request 标识
为清楚的 synthetic 占位，不代表任何真实会话、请求或 worker 原始报告。
"""
import hashlib
import io
import json
import os
import contextlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / 'scripts'
HARNESS = REPO / 'tests' / 'offline_codebuddy_harness.py'
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / 'tests'))
import quota_control as qc  # noqa: E402
import continuation_contract as cc  # noqa: E402
import execution_control as ec  # noqa: E402
import zcode_direct as zd  # noqa: E402
import codebuddy_direct as cbd  # noqa: E402
# 退休的 CodeBuddy 传输链已完整搬出生产模块，只存在于测试专用 harness；PrepLeak 的
# 入口生命周期回归直接调 harness.replay_dispatch（进程内、合成 noop stub，绝不 Popen）。
import offline_codebuddy_harness as cbh  # noqa: E402

TERMINAL = REPO / 'tests' / 'fixtures' / 'quota-terminal.synthetic.json'
# 合成离线样本保留的历史 reset 窗口：2026-10-08 18:14:13 UTC+8 == 2026-10-08T10:14:13Z。
EXPECTED_RESET_UTC = datetime(2026, 10, 8, 10, 14, 13, tzinfo=timezone.utc)

# CB 直连回放统一走仓库唯一可信固定合成 stub tests/offline_codebuddy_stub.py：
# 行为只由 CODEBUDDY_STUB_SPEC/RECORD 数据参数化，可信字节/哈希由仓库定义，env 无法
# 声明或放宽白名单（见 offline_codebuddy_harness._trusted_stub_hashes）。



def cb_init(session_id='S-1', model='GLM-CB-1'):
    return {'type': 'system', 'subtype': 'init', 'session_id': session_id,
            'model': model, 'tools': ['Read'], 'permissionMode': 'dontAsk',
            'mcp_servers': []}


def cb_result_success(text, session_id='S-1', model='GLM-CB-1'):
    return {'type': 'result', 'subtype': 'success', 'is_error': False,
            'result': text, 'session_id': session_id,
            'usage': {'input_tokens': 1, 'output_tokens': 2},
            'modelUsage': {model: {'input_tokens': 1}}}


def cb_result_429(session_id='S-1', model='GLM-CB-1'):
    return {'type': 'result', 'subtype': 'error_during_execution', 'is_error': True,
            'errors': ['429 您的使用量已超出频率限制，将在 2026-10-08 18:14:13 UTC+8 '
                       '重置，您也可以切换其他模型继续使用。'],
            'errors_info': [{'status': 429, 'code': 6004, 'category': 'quota',
                             'details': '429 您的使用量已超出频率限制，将在 '
                                        '2026-10-08 18:14:13 UTC+8 重置。'}],
            'session_id': session_id,
            'usage': {'input_tokens': 1, 'output_tokens': 2},
            'modelUsage': {model: {'input_tokens': 1}}}


def nine_report(stage, project):
    lines = ['WORKER_REPORT_START',
             f'阶段编号与执行方式：{stage}；direct。',
             f'实际项目绝对路径：{project}',
             '汇报时间与执行环境：', '']
    heads = ['一、当前基线与授权', '二、实际执行范围', '三、已验证事实',
             '四、推断（必须与事实分开）', '五、测试与验证',
             '六、未完成项与剩余风险', '七、实际副作用与越界检查',
             '八、本阶段状态', '九、建议下一步（只提出建议，不执行）']
    for head in heads:
        lines += [head, '- 离线占位', '']
    lines += ['本阶段汇报结束；等待主脑验收。', 'WORKER_REPORT_END', '']
    return '\n'.join(lines)


def make_registered_test_evidence(ws, input_files, name='reg-offline',
                                 stdout=b'ok\n', stderr=b''):
    """在工作区内合成一份 execution_control.run_registered_test 口径的 evidence.json
    及其 sibling stdout.log/stderr.log，返回 (evidence_rel, registered_inputs)。
    指纹用 ec.compute_fingerprint 复算，inputs 用绝对路径（跨 CWD 稳定）。"""
    argv = [sys.executable, '-c', 'pass']
    cwd_norm = os.path.normcase(os.path.realpath(str(ws)))
    registered_inputs = [str(Path(p).resolve()) for p in input_files]
    inputs_map = {}
    for p in registered_inputs:
        data = Path(p).read_bytes()
        inputs_map[os.path.normcase(os.path.realpath(p))] = hashlib.sha256(data).hexdigest()
    env_sha = hashlib.sha256(b'offline-env').hexdigest()
    runtime_version = 'offline-stub-runtime'
    fp = ec.compute_fingerprint(argv, cwd_norm, registered_inputs,
                                {'env_sha256': env_sha, 'runtime_version': runtime_version})
    ev_dir = ws / 'reg-test'
    ev_dir.mkdir(parents=True, exist_ok=True)
    prior = {'name': name, 'argv': [str(a) for a in argv], 'cwd': cwd_norm,
             'exit_code': 0, 'inputs_unchanged': True, 'inputs': inputs_map,
             'env_sha256': env_sha, 'runtime_version': runtime_version,
             'fingerprint': fp,
             'stdout_sha256': hashlib.sha256(stdout).hexdigest(),
             'stderr_sha256': hashlib.sha256(stderr).hexdigest()}
    (ev_dir / 'evidence.json').write_text(json.dumps(prior), encoding='utf-8')
    (ev_dir / 'stdout.log').write_bytes(stdout)
    (ev_dir / 'stderr.log').write_bytes(stderr)
    return 'reg-test/evidence.json', registered_inputs


class ClassifyTests(unittest.TestCase):
    """分类：区分 quota reset / Retry-After / 临时 429 / 无窗口 quota / 非 429。"""

    def test_replay_redacted_terminal_reset(self):
        terminal = json.loads(TERMINAL.read_text(encoding='utf-8'))
        cls = qc.classify_quota_failure(terminal['errors'], terminal['errors_info'])
        self.assertTrue(cls['is_quota_429'])
        self.assertEqual(cls['kind'], 'quota_reset')
        self.assertEqual(datetime.fromisoformat(cls['cooldown_until_utc']),
                         EXPECTED_RESET_UTC)

    def test_corrupt_reset_time_not_guessed(self):
        self.assertIsNone(qc.parse_reset_datetime(
            ['将在 2026-13-45 99:99:99 UTC+8 重置']))
        self.assertIsNone(qc.parse_reset_datetime(['将在 2026-10-08 18:14:13 重置']))
        # 损坏/无时区时间 + quota 类别 → 保守无窗口，不用固定 5 分钟替代。
        errors_info = [{'status': 429, 'category': 'quota',
                        'details': '429 将在 2026-13-45 99:99:99 UTC+8 重置'}]
        cls = qc.classify_quota_failure(['429 限流'], errors_info)
        self.assertEqual(cls['kind'], 'quota_no_window')

    def test_retry_after_seconds_and_http_date(self):
        now = datetime(2026, 10, 8, 6, 0, 0, tzinfo=timezone.utc)
        cls = qc.classify_quota_failure(['429 too many requests'],
                                        [{'status': 429}], retry_after=120, now=now)
        self.assertEqual(cls['kind'], 'retry_after')
        self.assertEqual(datetime.fromisoformat(cls['cooldown_until_utc']),
                         now + timedelta(seconds=120))
        http_date = 'Wed, 08 Oct 2026 07:00:00 GMT'
        cls2 = qc.classify_quota_failure(['429 too many requests'],
                                         [{'status': 429}], retry_after=http_date,
                                         now=now)
        self.assertEqual(cls2['kind'], 'retry_after')
        self.assertEqual(datetime.fromisoformat(cls2['cooldown_until_utc']),
                         datetime(2026, 10, 8, 7, 0, tzinfo=timezone.utc))

    def test_retry_after_floor_not_capped(self):
        # Retry-After 下限不被指数退避 cap 截断：10000s > cap(1800s) 仍生效。
        now = datetime(2026, 10, 8, 6, 0, 0, tzinfo=timezone.utc)
        cls = qc.classify_quota_failure(['429 slow down'], [{'status': 429}],
                                        retry_after=10000, now=now)
        self.assertEqual(datetime.fromisoformat(cls['cooldown_until_utc']),
                         now + timedelta(seconds=10000))

    def test_temporary_429_backoff_capped(self):
        now = datetime(2026, 10, 8, 6, 0, 0, tzinfo=timezone.utc)
        cls = qc.classify_quota_failure(['429 temporary'], [{'status': 429}], now=now)
        self.assertEqual(cls['kind'], 'temporary_backoff')
        # 反复落库后延迟封顶在 cap*(1+jitter) 内。
        store = Path(tempfile.mkdtemp()) / 's.sqlite3'
        for _ in range(8):
            qc.record_quota_event(store, 'g-temp', cls, source='t', now=now)
        status = qc.get_status(store, 'g-temp')
        until = datetime.fromisoformat(status['cooldowns']['g-temp']['cooldown_until_utc'])
        self.assertLessEqual(until, now + timedelta(
            seconds=qc.TEMP_BACKOFF_CAP_SECONDS * (1 + qc.TEMP_BACKOFF_JITTER)))

    def test_non_429_and_incidental_text_not_quota(self):
        # 不相关权限错误、仅 category=quota、正文偶然 429 数字都不产生冷却。
        cls = qc.classify_quota_failure(
            ['Permission to use Bash has been denied'],
            [{'status': 403, 'category': 'permission'}])
        self.assertFalse(cls['is_quota_429'])
        cls2 = qc.classify_quota_failure(['正文提到 429 与 6004 编号'],
                                         [{'code': 6004, 'category': 'quota'}])
        self.assertFalse(cls2['is_quota_429'])

    def test_extract_retry_after_skips_invalid_keeps_later_valid(self):
        # S4 缺陷 10：首个结构化 Retry-After 是无意义字符串，不得因此丢掉后续 7200。
        now = datetime(2026, 10, 8, 6, 0, 0, tzinfo=timezone.utc)
        errors_info = [{'status': 429, 'retry_after': 'not-a-number'},
                       {'status': 429, 'retry_after': 7200}]
        raw = qc.extract_retry_after([], errors_info, now=now)
        self.assertEqual(raw, 7200)

    def test_extract_retry_after_takes_strictest_valid(self):
        # S4 缺陷 10：多个有效不同值取最严格（最晚下限）。
        now = datetime(2026, 10, 8, 6, 0, 0, tzinfo=timezone.utc)
        errors_info = [{'status': 429, 'headers': {'Retry-After': '60'}},
                       {'status': 429, 'retry_after': 7200}]
        raw = qc.extract_retry_after([], errors_info, now=now)
        self.assertEqual(raw, 7200)
        cls = qc.classify_quota_failure(['429'], errors_info,
                                        retry_after=raw, now=now)
        self.assertEqual(datetime.fromisoformat(cls['cooldown_until_utc']),
                         now + timedelta(seconds=7200))

    def test_zcode_provider_business_stderr_frame_extract(self):
        # S4 缺陷 11：未改的 SDK runner 只在 envelope.errors 放字符串，真实 status/code
        # 在本次子进程自己写出的 stderr 帧里；zcode_direct 必须安全有限白名单提取并据此
        # 自动持久冷却，绝不回显 headers/正文偶然数字。
        frame = ("ProviderBusinessError: [1308][已达到 5 小时的使用上限。"
                 "您的限额将在 2026-10-08 15:17:30 重置。]\n"
                 "  code: '1308',\n  responseStatus: 429,\n"
                 "  set-cookie: SECRET-HEADER-VALUE\n").encode('utf-8')
        entry = zd._provider_business_error_from_stderr(frame, 'acct-z')
        self.assertIsNotNone(entry)
        self.assertEqual(entry['status'], 429)
        self.assertEqual(entry['code'], '1308')
        self.assertEqual(entry['provider'], 'acct-z')
        self.assertIn('unverified', entry['reset_timezone'])
        blob = json.dumps(entry, ensure_ascii=False)
        self.assertNotIn('set-cookie', blob)
        self.assertNotIn('SECRET-HEADER-VALUE', blob)
        # reset 无时区 → 保守 quota_no_window(24h)，不猜 UTC+8。
        cls = qc.classify_quota_failure(
            ['some string error'], [entry],
            now=datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc))
        self.assertTrue(cls['is_quota_429'])
        self.assertEqual(cls['kind'], 'quota_no_window')

    def test_zcode_no_invented_429_from_arbitrary_stderr(self):
        # S4 缺陷 11：正文里偶然出现的 429/1308 数字不构成 ProviderBusinessError 帧，
        # 没有 responseStatus 结构字段就不算额度冷却。
        text = ('normal runner log mentioning code 429 and id 1308 in prose\n').encode('utf-8')
        self.assertIsNone(zd._provider_business_error_from_stderr(text, 'acct-z'))


class GatePersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = self.tmp / 'store.sqlite3'
        self.ws = self.tmp / 'ws'
        self.ws.mkdir()
        self.routes = self.tmp / 'routes.json'
        self.routes.write_text(json.dumps({'routes': [
            {'runtime': 'codebuddy', 'match': {'entry_config': '/cb/entry.json'},
             'quota_group': 'g-cb', 'independence': 'user_confirmed'},
            {'runtime': 'zcode', 'match': {'provider': 'acct-z'},
             'quota_group': 'g-zc', 'independence': 'user_confirmed'},
        ]}), encoding='utf-8')
        self.cb_id = {'entry_config': '/cb/entry.json'}
        self.zc_id = {'provider': 'acct-z'}

    def tearDown(self):
        self._tmp.cleanup()

    def test_start_failed_settles_this_attempt_placeholders(self):
        # S4 缺陷 2：gate 之后、Popen 之前失败（建目录/freeze/写 request）→
        # settle_attempt('start_failed') 只释放本 attempt 的工作区+通道占位，
        # 绝不泄漏、也绝不误删他人占位；不写冷却、不写 healthy。
        gate = qc.gate_dispatch(self.store, runtime='codebuddy', identity=self.cb_id,
                                workspace=self.ws, routes_path=self.routes,
                                now=datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc))
        self.assertTrue(gate['allowed'])
        status = qc.get_status(self.store)
        self.assertEqual(len(status['workspace_locks']), 1)
        self.assertEqual(len(status['channel_locks']), 1)
        settle = qc.settle_attempt(self.store, gate, terminal='start_failed',
                                   now=datetime(2026, 10, 8, 6, 0, 30,
                                                tzinfo=timezone.utc))
        self.assertTrue(settle['settled'])
        self.assertTrue(settle['released'])
        self.assertIsNone(settle['quota_outcome'])  # 不写冷却、不写 healthy
        after = qc.get_status(self.store)
        self.assertEqual(after['workspace_locks'], [])
        self.assertEqual(after['channel_locks'], [])

    def test_independent_groups_share_one_workspace_single_writer(self):
        # S4 缺陷 4：两个用户确认独立的组仍共享同一工作区写入权。g-cb 持有工作区
        # 占位时，针对同一 workspace 的 g-zc 派发被拒（无空窗，不看组独立性）；
        # 只有 g-cb 终态结算释放后，g-zc 才可进入，杜绝并发双写/空目录覆盖。
        t0 = datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc)
        gate_cb = qc.gate_dispatch(self.store, runtime='codebuddy', identity=self.cb_id,
                                   workspace=self.ws, routes_path=self.routes, now=t0)
        self.assertTrue(gate_cb['allowed'])
        self.assertEqual(gate_cb['quota_group'], 'g-cb')
        # 同一工作区、另一独立组的第二个执行器被工作区单写者门禁拒绝。
        gate_zc = qc.gate_dispatch(self.store, runtime='zcode', identity=self.zc_id,
                                   workspace=self.ws, routes_path=self.routes,
                                   now=t0 + timedelta(seconds=1))
        self.assertFalse(gate_zc['allowed'])
        self.assertEqual(gate_zc['quota_group'], 'g-zc')
        self.assertTrue(any('workspace already has a live writer' in r
                            for r in gate_zc['reasons']))
        # 未结算前占位仍在，不存在空窗。
        self.assertEqual(len(qc.get_status(self.store)['workspace_locks']), 1)
        # g-cb 以 confirmed_exit 结算后才释放；此时 g-zc 才可进入同一工作区。
        qc.settle_attempt(self.store, gate_cb, terminal='confirmed_exit', success=True,
                          now=t0 + timedelta(seconds=2))
        gate_zc2 = qc.gate_dispatch(self.store, runtime='zcode', identity=self.zc_id,
                                    workspace=self.ws, routes_path=self.routes,
                                    now=t0 + timedelta(seconds=3))
        self.assertTrue(gate_zc2['allowed'])

    def test_import_then_gate_rejects_across_processes_and_dirs(self):
        result = qc.import_terminal(self.store, TERMINAL, runtime='codebuddy',
                                    identity=self.cb_id, routes_path=self.routes,
                                    now=datetime(2026, 10, 8, 5, 34, tzinfo=timezone.utc))
        self.assertTrue(result['classification']['is_quota_429'])
        gate = qc.gate_dispatch(self.store, runtime='codebuddy', identity=self.cb_id,
                                workspace=self.ws, routes_path=self.routes,
                                now=datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc))
        self.assertFalse(gate['allowed'])
        self.assertFalse(gate['sent'])
        self.assertEqual(datetime.fromisoformat(gate['cooldown_until_utc']),
                         EXPECTED_RESET_UTC)
        # 新进程（新连接/新输出目录/新工作区）读取同一持久 store 仍拒绝。
        gate2 = qc.gate_dispatch(self.store, runtime='codebuddy', identity=self.cb_id,
                                 workspace=self.tmp / 'ws2', routes_path=self.routes,
                                 now=datetime(2026, 10, 8, 6, 1, tzinfo=timezone.utc))
        self.assertFalse(gate2['allowed'])
        self.assertEqual(gate2['quota_group'], 'g-cb')

    def test_expiry_probe_single_and_verify(self):
        qc.import_terminal(self.store, TERMINAL, runtime='codebuddy',
                           identity=self.cb_id, routes_path=self.routes,
                           now=datetime(2026, 10, 8, 5, 34, tzinfo=timezone.utc))
        after = datetime(2026, 10, 8, 10, 15, tzinfo=timezone.utc)
        gate = qc.gate_dispatch(self.store, runtime='codebuddy', identity=self.cb_id,
                                workspace=self.ws, routes_path=self.routes, now=after)
        self.assertFalse(gate['allowed'])  # recovery_unverified：不能直接判恢复
        self.assertIn('unverified', ' '.join(gate['reasons']))
        # 同组同时最多一个在飞 probe：第二个被阻断。
        probe1 = qc.gate_dispatch(self.store, runtime='codebuddy', identity=self.cb_id,
                                  workspace=self.ws, purpose='probe',
                                  routes_path=self.routes, now=after)
        self.assertTrue(probe1['allowed'])
        probe2 = qc.gate_dispatch(self.store, runtime='codebuddy', identity=self.cb_id,
                                  workspace=self.ws, purpose='probe',
                                  routes_path=self.routes, now=after)
        self.assertFalse(probe2['allowed'])
        self.assertIn('already in flight', ' '.join(probe2['reasons']))
        # probe 失败（新的带时区 429）→ settle_attempt 续冷却、释放本次占位。
        failed = qc.classify_quota_failure(
            ['429 您的使用量已超出频率限制，将在 2026-10-08 20:00:00 UTC+8 重置'],
            [{'status': 429, 'category': 'quota'}], now=after)
        qc.settle_attempt(self.store, probe1, terminal='confirmed_exit',
                          success=False, classification=failed, source='probe failed')
        status = qc.get_status(self.store, 'g-cb')
        self.assertEqual(status['cooldowns']['g-cb']['state'], 'cooling')
        # 再次到期后 probe 成功才清冷却（终态原子，非裸 release）。
        much_later = datetime(2026, 10, 8, 12, 1, tzinfo=timezone.utc)
        gate3 = qc.gate_dispatch(self.store, runtime='codebuddy', identity=self.cb_id,
                                 workspace=self.ws, routes_path=self.routes,
                                 now=much_later)
        self.assertFalse(gate3['allowed'])
        probe3 = qc.gate_dispatch(self.store, runtime='codebuddy', identity=self.cb_id,
                                  workspace=self.ws, purpose='probe',
                                  routes_path=self.routes, now=much_later)
        self.assertTrue(probe3['allowed'])
        settled = qc.settle_attempt(self.store, probe3, terminal='confirmed_exit',
                                    success=True)
        self.assertTrue(settled['quota_outcome']['cleared'])
        gate4 = qc.gate_dispatch(self.store, runtime='codebuddy', identity=self.cb_id,
                                 workspace=self.ws, routes_path=self.routes,
                                 now=much_later)
        self.assertTrue(gate4['allowed'])
        qc.settle_attempt(self.store, gate4, terminal='confirmed_exit', success=True)

    def test_late_success_cannot_clear_newer_cooldown(self):
        # 缺陷 E：旧 attempt 观察到的 epoch 不得清除更新的冷却。
        now = datetime(2026, 10, 8, 5, 34, tzinfo=timezone.utc)
        qc.import_terminal(self.store, TERMINAL, runtime='codebuddy',
                           identity=self.cb_id, routes_path=self.routes, now=now)
        stale_attempt = qc.gate_dispatch(self.store, runtime='codebuddy',
                                         identity=self.cb_id, workspace=self.ws,
                                         purpose='probe', routes_path=self.routes,
                                         now=datetime(2026, 10, 8, 10, 15, tzinfo=timezone.utc))
        self.assertTrue(stale_attempt['allowed'])
        # 期间另一通道事件写入新的冷却（epoch 递增）。
        new = qc.classify_quota_failure(
            ['429 将在 2026-10-09 18:00:00 UTC+8 重置'], [{'status': 429}], now=now)
        qc.record_quota_event(self.store, 'g-cb', new, source='newer event', now=now)
        # 用旧 attempt 报成功：不得清除新冷却。
        settled = qc.settle_attempt(self.store, stale_attempt, terminal='confirmed_exit',
                                    success=True)
        self.assertNotEqual((settled.get('quota_outcome') or {}).get('cleared'), True)
        status = qc.get_status(self.store, 'g-cb')
        self.assertEqual(status['cooldowns']['g-cb']['state'], 'cooling')

    def test_unknown_terminal_retains_placeholders(self):
        # 缺陷 C：终态无法确认 → 保留工作区/通道占位，保守阻断、不默认删锁。
        now = datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc)
        attempt = qc.gate_dispatch(self.store, runtime='codebuddy',
                                   identity=self.cb_id, workspace=self.ws,
                                   routes_path=self.routes, now=now)
        self.assertTrue(attempt['allowed'])
        settled = qc.settle_attempt(self.store, attempt, terminal='unknown')
        self.assertFalse(settled['released'])
        self.assertEqual(settled['terminal_state'], 'unknown')
        status = qc.get_status(self.store)
        self.assertEqual(len(status['workspace_locks']), 1)
        self.assertEqual(len(status['channel_locks']), 1)

    def test_unmatched_channel_cannot_bypass_known_cooldown(self):
        # 缺陷 G：配置别名/复制/未知新组不得借 unknown-shared 绕过同 runtime 已知冷却。
        now = datetime(2026, 10, 8, 5, 34, tzinfo=timezone.utc)
        qc.import_terminal(self.store, TERMINAL, runtime='codebuddy',
                           identity=self.cb_id, routes_path=self.routes, now=now)
        # 一个未匹配路由的 codebuddy 通道（别名 config）→ unknown-shared。
        alias_id = {'entry_cli': 'C:/copied/codebuddy-alias.json'}
        gate = qc.gate_dispatch(self.store, runtime='codebuddy', identity=alias_id,
                                workspace=self.tmp / 'ws-alias', routes_path=self.routes,
                                now=datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc))
        self.assertFalse(gate['allowed'])
        self.assertEqual(gate['quota_group'], qc.UNKNOWN_SHARED_GROUP)
        self.assertTrue(any('conservative shared-cooldown block' in r
                            and 'skip each other' in r for r in gate['reasons']))

    def _cooldown_until_tomorrow(self, now):
        return qc.classify_quota_failure(
            ['429 将在 2026-10-09 18:00:00 UTC+8 重置'], [{'status': 429}], now=now)

    def test_unknown_cooldown_blocks_known_dispatch(self):
        # S4 缺陷 1 unknown→known 双向保守：unknown-shared 有未清冷却时，匹配已知组
        # 的 dispatch 也应被保守阻断（关系未知不可默认独立）。
        now = datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc)
        qc.record_quota_event(self.store, qc.UNKNOWN_SHARED_GROUP,
                              self._cooldown_until_tomorrow(now), source='unknown hit',
                              now=now)
        gate = qc.gate_dispatch(self.store, runtime='codebuddy', identity=self.cb_id,
                                workspace=self.ws, routes_path=self.routes, now=now)
        self.assertFalse(gate['allowed'])
        self.assertEqual(gate['quota_group'], 'g-cb')
        self.assertTrue(any(qc.UNKNOWN_SHARED_GROUP in r for r in gate['reasons']))

    def test_routes_missing_cannot_erase_stored_cooldown(self):
        # S4 缺陷 1：routes 缺失/更名不得让 store 里既有已知冷却被绕过。用不存在的
        # routes 文件分发已配置的 cb 通道 → 落 unknown-shared，但 g-cb 未清冷却仍阻断。
        now = datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc)
        qc.record_quota_event(self.store, 'g-cb', self._cooldown_until_tomorrow(now),
                              source='stored', now=now)
        missing = self.tmp / 'no-such-routes.json'
        gate = qc.gate_dispatch(self.store, runtime='codebuddy', identity=self.cb_id,
                                workspace=self.ws, routes_path=missing, now=now)
        self.assertFalse(gate['allowed'])
        self.assertTrue(any('missing routes file cannot erase' in r
                            or 'switched, renamed or missing routes file' in r
                            for r in gate['reasons']))

    def test_expired_recovery_unverified_still_blocks_unknown(self):
        # S4 缺陷 1：已到期但 recovery_unverified 的已知冷却仍未被清除，未知通道不得绕过。
        qc.import_terminal(self.store, TERMINAL, runtime='codebuddy',
                           identity=self.cb_id, routes_path=self.routes,
                           now=datetime(2026, 10, 8, 5, 34, tzinfo=timezone.utc))
        alias_id = {'entry_cli': 'C:/copied/alias.json'}
        gate = qc.gate_dispatch(self.store, runtime='codebuddy', identity=alias_id,
                                workspace=self.tmp / 'ws-alias', routes_path=self.routes,
                                now=datetime(2026, 10, 8, 11, 0, tzinfo=timezone.utc))
        self.assertFalse(gate['allowed'])
        self.assertEqual(gate['quota_group'], qc.UNKNOWN_SHARED_GROUP)

    def test_expired_two_group_deadlock_has_bounded_probe_recovery(self):
        # S5 缺陷 4：已到期 CB 冷却 + 已到期 unknown-shared 冷却互相保守阻断——完整
        # dispatch 仍禁止，但到期后必须提供单次有界 no-side-effect probe 恢复路径，否则
        # 双方永久死锁。恢复成功只清被核验的本组，绝不宣称未知组都恢复，也不清其它行；
        # 相关组之间串行，绝不同时消耗同一份额度。
        rec_now = datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc)
        qc.record_quota_event(self.store, 'g-cb', self._cooldown_until_tomorrow(rec_now),
                              source='cb cooling', now=rec_now)
        qc.record_quota_event(self.store, qc.UNKNOWN_SHARED_GROUP,
                              self._cooldown_until_tomorrow(rec_now),
                              source='unknown cooling', now=rec_now)
        expired = datetime(2026, 10, 10, 6, 0, tzinfo=timezone.utc)
        # 完整派发仍被跨组保守阻断。
        disp = qc.gate_dispatch(self.store, runtime='codebuddy', identity=self.cb_id,
                                workspace=self.ws, routes_path=self.routes, now=expired)
        self.assertFalse(disp['allowed'])
        self.assertTrue(any('conservative shared-cooldown block' in r
                            for r in disp['reasons']))
        # 到期后的有界 probe 可进入（这是死锁解锁路径，不靠直接清行）。
        probe1 = qc.gate_dispatch(self.store, runtime='codebuddy', identity=self.cb_id,
                                  workspace=self.ws, purpose='probe',
                                  routes_path=self.routes, now=expired)
        self.assertTrue(probe1['allowed'])
        # 同组并发第二个 probe 被在途阻断。
        probe2 = qc.gate_dispatch(self.store, runtime='codebuddy', identity=self.cb_id,
                                  workspace=self.ws, purpose='probe',
                                  routes_path=self.routes, now=expired)
        self.assertFalse(probe2['allowed'])
        # 相关未知组在本组 probe 在途时也必须串行，不得同时探测。
        alias_id = {'entry_cli': 'C:/copied/alias.json'}
        rel = qc.gate_dispatch(self.store, runtime='codebuddy', identity=alias_id,
                               workspace=self.tmp / 'ws-alias', purpose='probe',
                               routes_path=self.routes, now=expired)
        self.assertFalse(rel['allowed'])
        self.assertTrue(any('serialized against related groups' in r
                            for r in rel['reasons']))
        # probe 成功：只清被核验的本组 g-cb，绝不宣称未知组都恢复，未知行仍在。
        settled = qc.settle_attempt(self.store, probe1, terminal='confirmed_exit',
                                    success=True)
        self.assertTrue(settled['quota_outcome']['cleared'])
        status = qc.get_status(self.store)
        self.assertNotIn('g-cb', status['cooldowns'])
        self.assertIn(qc.UNKNOWN_SHARED_GROUP, status['cooldowns'])

    def test_independent_confirmed_groups_do_not_chain(self):
        # S4 缺陷 1 唯一豁免：两个 user_confirmed 独立组互不连带，可交替接续（不同工作区）。
        now = datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc)
        qc.record_quota_event(self.store, 'g-cb', self._cooldown_until_tomorrow(now),
                              source='cb cooling', now=now)
        gate = qc.gate_dispatch(self.store, runtime='zcode', identity=self.zc_id,
                                workspace=self.tmp / 'ws-zc', routes_path=self.routes,
                                now=now)
        self.assertTrue(gate['allowed'])
        self.assertEqual(gate['quota_group'], 'g-zc')

    def test_monotonic_later_cooldown_not_shortened(self):
        now = datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc)
        late = qc.classify_quota_failure(
            ['429 将在 2026-10-09 18:00:00 UTC+8 重置'], [{'status': 429}], now=now)
        qc.record_quota_event(self.store, 'g-cb', late, source='late', now=now)
        early = qc.classify_quota_failure(
            ['429 将在 2026-10-08 12:00:00 UTC+8 重置'], [{'status': 429}], now=now)
        rec = qc.record_quota_event(self.store, 'g-cb', early, source='early', now=now)
        self.assertTrue(rec['monotonic_kept_later'])
        status = qc.get_status(self.store, 'g-cb')
        self.assertEqual(datetime.fromisoformat(
            status['cooldowns']['g-cb']['cooldown_until_utc']),
            datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc))

    def test_shared_group_cross_client_blocked(self):
        # 新政策（BW-ZCODE-MANUAL-QUOTA-20261008-S1）：共享路由的历史额度冷却不再挡
        # ZCode 派工（ZCode 手动额度、取消自动冷却），但同组通道单在途锁仍生效——同组
        # 跨工作区仍单在途、活任务不被抢占。CodeBuddy 侧共享冷却规则不变；ZCode 也不
        # 会清除/覆盖 CB 记录的同组冷却。
        shared = self.tmp / 'shared-routes.json'
        shared.write_text(json.dumps({'routes': [
            {'runtime': 'codebuddy', 'match': {'entry_config': '/cb/entry.json'},
             'quota_group': 'g-glm', 'independence': 'shared_declared'},
            {'runtime': 'zcode', 'match': {'provider': 'acct-z'},
             'quota_group': 'g-glm', 'independence': 'shared_declared'},
        ]}), encoding='utf-8')
        import_now = datetime(2026, 10, 8, 5, 34, tzinfo=timezone.utc)
        qc.import_terminal(self.store, TERMINAL, runtime='codebuddy',
                           identity=self.cb_id, routes_path=shared, now=import_now)
        t = datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc)
        # 历史 g-glm 冷却不再挡 ZCode：ZCode 派工放行，并如实标记自动冷却禁用。
        gate = qc.gate_dispatch(self.store, runtime='zcode', identity=self.zc_id,
                                workspace=self.ws, routes_path=shared, now=t)
        self.assertTrue(gate['allowed'])
        self.assertEqual(gate['quota_group'], 'g-glm')
        self.assertTrue(gate['auto_cooldown_disabled'])
        # ZCode 不新增/清除冷却：CB 记录的 g-glm 冷却行原样保留。
        self.assertIn('g-glm', qc.get_status(self.store)['cooldowns'])
        # 通道单在途锁仍生效：ZCode 已持 g-glm 在途占位时，同组到另一工作区仍被挡住。
        gate2 = qc.gate_dispatch(self.store, runtime='zcode', identity=self.zc_id,
                                 workspace=self.tmp / 'ws-zc2', routes_path=shared, now=t)
        self.assertFalse(gate2['allowed'])
        self.assertTrue(any('single in-flight' in r for r in gate2['reasons']))
        # start_failed 释放本次占位但不碰冷却：g-glm 冷却行仍在。
        qc.settle_attempt(self.store, gate, terminal='start_failed', now=t)
        self.assertIn('g-glm', qc.get_status(self.store)['cooldowns'])

    def test_user_confirmed_independent_group_continues(self):
        now = datetime(2026, 10, 8, 5, 34, tzinfo=timezone.utc)
        qc.import_terminal(self.store, TERMINAL, runtime='codebuddy',
                           identity=self.cb_id, routes_path=self.routes, now=now)
        gate = qc.gate_dispatch(self.store, runtime='zcode', identity=self.zc_id,
                                workspace=self.ws, routes_path=self.routes,
                                now=datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc))
        self.assertTrue(gate['allowed'])  # user_confirmed 独立组可接续
        self.assertEqual(gate['quota_group'], 'g-zc')
        qc.release_lock(self.store, str(self.ws))

    def test_unknown_relationship_not_independent(self):
        # 新政策：无路由配置时两通道都落 unknown-shared。ZCode 取消自动额度冷却——历史
        # unknown-shared 冷却不再挡 ZCode，但通道锁仍生效；CodeBuddy 侧规则不变（未知
        # 关系不默认独立，冷却仍阻断 CB），且 ZCode 派工不会清除该共享冷却。
        absent = self.tmp / 'absent-routes.json'
        now = datetime(2026, 10, 8, 5, 34, tzinfo=timezone.utc)
        qc.import_terminal(self.store, TERMINAL, runtime='codebuddy',
                           identity=self.cb_id, routes_path=absent, now=now)
        t = datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc)
        gate = qc.gate_dispatch(self.store, runtime='zcode', identity=self.zc_id,
                                workspace=self.ws, routes_path=absent, now=t)
        self.assertTrue(gate['allowed'])            # ZCode 不再受 unknown-shared 冷却
        self.assertEqual(gate['quota_group'], qc.UNKNOWN_SHARED_GROUP)
        self.assertTrue(gate['auto_cooldown_disabled'])
        # CodeBuddy 通道（未匹配→unknown-shared）仍被本组 unknown-shared 冷却阻断。
        cb = qc.gate_dispatch(self.store, runtime='codebuddy', identity=self.cb_id,
                              workspace=self.tmp / 'ws-cb', routes_path=absent, now=t)
        self.assertFalse(cb['allowed'])
        # ZCode 派工不清除 unknown-shared 冷却。
        self.assertIn(qc.UNKNOWN_SHARED_GROUP, qc.get_status(self.store)['cooldowns'])
        # 通道单在途锁仍生效：ZCode 已占 unknown-shared，同组另一工作区被通道锁挡住。
        zc2 = qc.gate_dispatch(self.store, runtime='zcode', identity=self.zc_id,
                               workspace=self.tmp / 'ws-zc2', routes_path=absent, now=t)
        self.assertFalse(zc2['allowed'])
        self.assertTrue(any('single in-flight' in r for r in zc2['reasons']))
        qc.settle_attempt(self.store, gate, terminal='start_failed', now=t)
        self.assertIn(qc.UNKNOWN_SHARED_GROUP, qc.get_status(self.store)['cooldowns'])

    def test_import_terminal_zcode_disables_auto_cooldown(self):
        # 新政策：ZCode 离线 import 仍提取 429 分类/原因，但明确返回自动冷却禁用、绝不写
        # cooldown；对照 CodeBuddy 同形 import 仍记录冷却（低层 API 旧规则不变）。
        now = datetime(2026, 10, 8, 5, 34, tzinfo=timezone.utc)
        res = qc.import_terminal(self.store, TERMINAL, runtime='zcode',
                                 identity=self.zc_id, routes_path=self.routes, now=now)
        self.assertTrue(res['classification']['is_quota_429'])
        self.assertEqual(res['classification']['kind'], 'quota_reset')
        self.assertIsNone(res['recorded'])
        self.assertTrue(res['auto_cooldown_disabled'])
        # ZCode 不新增 cooldown。
        self.assertNotIn('g-zc', qc.get_status(self.store)['cooldowns'])
        # 对照：CodeBuddy 同形 import 仍记录 g-cb 冷却。
        res_cb = qc.import_terminal(self.store, TERMINAL, runtime='codebuddy',
                                    identity=self.cb_id, routes_path=self.routes, now=now)
        self.assertTrue(res_cb['recorded']['recorded'])
        self.assertIn('g-cb', qc.get_status(self.store)['cooldowns'])

    def test_workspace_single_writer_and_stale_lock(self):
        now = datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc)
        first = qc.gate_dispatch(self.store, runtime='codebuddy',
                                 identity=self.cb_id, workspace=self.ws,
                                 routes_path=self.routes, now=now)
        self.assertTrue(first['allowed'])
        second = qc.gate_dispatch(self.store, runtime='zcode', identity=self.zc_id,
                                  workspace=self.ws, routes_path=self.routes, now=now)
        self.assertFalse(second['allowed'])  # 同一真实工作区单一写入执行器
        self.assertTrue(any('live writer' in r for r in second['reasons']))
        # 独立工作区不受影响。
        other = self.tmp / 'ws-other'
        other.mkdir()
        third = qc.gate_dispatch(self.store, runtime='zcode', identity=self.zc_id,
                                 workspace=other, routes_path=self.routes, now=now)
        self.assertTrue(third['allowed'])
        qc.release_lock(self.store, str(other))
        qc.release_lock(self.store, str(self.ws))
        # 占位已释放后可再次占位。
        fourth = qc.gate_dispatch(self.store, runtime='codebuddy',
                                  identity=self.cb_id, workspace=self.ws,
                                  routes_path=self.routes, now=now)
        self.assertTrue(fourth['allowed'])
        qc.release_lock(self.store, str(self.ws))
        # 进程已死的占位：终态未知，保守阻断且不默认删锁；给出显式释放方式。
        conn = sqlite3.connect(self.store)
        conn.execute('INSERT INTO workspace_locks(workspace, quota_group, runtime, pid,'
                     ' host, purpose, acquired_at_utc) VALUES(?,?,?,?,?,?,?)',
                     (str(qc._norm_workspace(self.ws)), 'g-cb', 'codebuddy', 999999999,
                      'h', 'dispatch', '2026-10-08T06:00:00+00:00'))
        conn.commit()
        conn.close()
        stale = qc.gate_dispatch(self.store, runtime='codebuddy',
                                 identity=self.cb_id, workspace=self.ws,
                                 routes_path=self.routes, now=now)
        self.assertFalse(stale['allowed'])
        self.assertTrue(any('NOT auto-deleted' in r or 'not auto-deleted' in r.lower()
                            or 'terminal state unknown' in r
                            for r in stale['reasons']))
        wrong = qc.release_lock(self.store, str(self.ws), pid=os.getpid())
        self.assertFalse(wrong['released'])  # pid 不匹配不能释放别人占位
        right = qc.release_lock(self.store, str(self.ws), pid=999999999)
        self.assertTrue(right['released'])


class ContinuationTests(unittest.TestCase):
    def test_partial_outputs_kept_and_test_inputs_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            ws = tmp / 'proj'
            (ws / 'src').mkdir(parents=True)
            f = ws / 'src' / 'a.js'
            f.write_text('original\n', encoding='utf-8', newline='\n')
            # 登记测试证据：input = src/a.js（绝对路径），此刻哈希与内容一致。
            ev_rel, reg_inputs = make_registered_test_evidence(ws, [str(f)])
            frozen = cc.freeze(['src/a.js'], ws)
            # 中断后的部分写入留存：executor 直接改文件（不回滚）。
            f.write_text('original\npartial-edit\n', encoding='utf-8', newline='\n')
            evaluation = cc.evaluate(frozen, ws)
            self.assertTrue(evaluation['files']['src/a.js']['changed'])
            self.assertFalse(evaluation['files']['src/a.js']['missing_now'])
            self.assertTrue(evaluation['any_changed'])
            # 绑定登记测试证据：内部指纹/日志仍自洽 → 历史已验证 valid=True；
            # 当前输入已被业务改动 → stale=True，旧绿测不可继承，但不抹掉原验证。
            binding = cc.bind_registered_test(ev_rel, str(ws), reg_inputs)
            stale = cc.test_inputs_stale(binding)
            self.assertTrue(binding['valid'], binding.get('reason'))
            self.assertTrue(stale['stale'])
            self.assertIn(os.path.normcase(os.path.realpath(str(f))),
                          stale['mismatched_inputs'] + stale.get('missing_inputs', []))
            handoff = cc.build_handoff(frozen, evaluation, binding, stale,
                                       ['继续从 partial-edit 修补'],
                                       original_error_ref='tests/fixtures/quota-terminal.synthetic.json')
            self.assertFalse(handoff['final_worker_report_present'])  # 不补写报告
            self.assertTrue(handoff['test_inputs_stale']['stale'])
            # 未声明清单时如实标未提供，不做全项目扫描。
            undeclared = cc.freeze([], ws)
            self.assertFalse(undeclared['files_declared'])

    def test_valid_registered_test_inheritable_until_input_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            ws = tmp / 'proj'
            ws.mkdir()
            f = ws / 'lib.py'
            f.write_text('print(1)\n', encoding='utf-8', newline='\n')
            ev_rel, reg_inputs = make_registered_test_evidence(ws, [str(f)])
            # 输入未变：证据内部指纹/日志 SHA/当前输入全部匹配 → valid、非 stale。
            binding = cc.bind_registered_test(ev_rel, str(ws), reg_inputs)
            self.assertTrue(binding['valid'], binding.get('reason'))
            self.assertFalse(cc.test_inputs_stale(binding)['stale'])
            # 篡改日志后回读哈希不符 → invalid（旧绿测不可继承）。
            (ws / 'reg-test' / 'stdout.log').write_bytes(b'tampered\n')
            tampered = cc.bind_registered_test(ev_rel, str(ws), reg_inputs)
            self.assertFalse(tampered['valid'])
            self.assertTrue(cc.test_inputs_stale(tampered)['stale'])

    def test_freeze_rejects_out_of_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / 'proj'
            ws.mkdir()
            with self.assertRaises(ValueError):
                cc.freeze(['../outside.js'], ws)

    def test_windows_pid_probe_never_calls_os_kill(self):
        # 缺陷 A 静态确认在 Windows 分支绝不落到 os.kill（Windows 上 os.kill 走
        # TerminateProcess）。用一个会抛异常的哨兵替换 os.kill，Windows 存活查询
        # 必须完全避开它。POSIX 分支才允许 os.kill(pid, 0)。
        real_kill = os.kill
        calls = []

        def sentinel(pid, sig):
            calls.append((pid, sig))
            raise AssertionError('os.kill must not be called on Windows branch')

        os.kill = sentinel
        try:
            state = qc._windows_pid_state(999999999)  # 只读句柄查询，返回 alive/dead/unknown
            self.assertIn(state, ('alive', 'dead', 'unknown'))
            self.assertEqual(calls, [])
            # _pid_alive 走 Windows 分支（platform 注入）时同样不触碰 os.kill。
            qc._pid_alive(999999999, platform='win32')
            self.assertEqual(calls, [])
        finally:
            os.kill = real_kill

    def test_wrapper_quota_1308_empty_events_is_conservative_no_window(self):
        # 需求 1：ZCode 包装层 HTTP 429/provider_code 1308、events 为空，只在已知
        # 失败结构按白名单提取 status/code/message/provider，不猜 UTC+8、不自动重派。
        envelope = {'errors': [{'response_status': 429, 'provider_code': '1308',
                                'provider': 'account:bigmodel-individual-coding-plan',
                                'message': '[1308][已达到 5 小时的使用上限。您的限额将在 '
                                           '2026-10-08 15:17:30 重置。]',
                                'reset_timezone': 'unverified: message did not contain timezone',
                                'source_stderr_sha256': '51febe98' + '0' * 56,
                                'headers': {'x-secret': 'never-emit'}}],
                    'errors_info': None}
        wrapper = zd._wrapper_quota_error(envelope)
        self.assertEqual(wrapper['status'], 429)
        self.assertEqual(wrapper['code'], '1308')
        self.assertNotIn('headers', wrapper)  # 白名单外字段（headers）绝不落入
        now = datetime(2026, 10, 8, 7, 0, tzinfo=timezone.utc)
        cls = qc.classify_quota_failure(
            [envelope['errors'][0]['message']], [wrapper], retry_after=None, now=now)
        self.assertTrue(cls['is_quota_429'])
        # reset 无时区 → 不猜 UTC+8，保守无窗口 24h 阻断。
        self.assertEqual(cls['kind'], 'quota_no_window')
        self.assertEqual(datetime.fromisoformat(cls['cooldown_until_utc']),
                         now + timedelta(hours=qc.QUOTA_NO_WINDOW_HOURS))

    def test_wrapper_429_never_invented_from_normal_report_text(self):
        # 正常报告正文里偶然出现 429/1308 不产生额度信号：非已知失败结构一律 None。
        envelope = {'errors': ['WORKER_REPORT 正文里提到 429 与 1308，但不是错误结构'],
                    'errors_info': None}
        self.assertIsNone(zd._wrapper_quota_error(envelope))

    def test_build_handoff_nonempty_files_and_receipts_no_nameerror(self):
        # S4 缺陷 3：非空 files + 写回执路径不得 NameError；只成功且身份核验的回执才算关联。
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            ws = tmp / 'proj'
            (ws / 'src').mkdir(parents=True)
            f = ws / 'src' / 'a.py'
            f.write_text('v1\n', encoding='utf-8', newline='\n')
            frozen = cc.freeze(['src/a.py'], ws, copies_dir=tmp / 'copies')
            f.write_text('v2\n', encoding='utf-8', newline='\n')
            ev = cc.evaluate(frozen, ws, copies_dir=tmp / 'copies')
            receipts = [
                {'tool': 'Write', 'tool_use_id': 't1', 'file_path': str(f),
                 'successful': True, 'identity_verified': True, 'source': 'events.jsonl'},
                {'tool': 'Edit', 'tool_use_id': 't2', 'file_path': str(f),
                 'successful': False, 'identity_verified': True, 'source': 'events.jsonl'},
                {'tool': 'Write', 'tool_use_id': 't3', 'file_path': str(f),
                 'successful': True, 'identity_verified': False, 'source': 'events.jsonl'}]
            handoff = cc.build_handoff(frozen, ev, None, None, [],
                                       write_receipts=receipts)
            rec = handoff['files']['src/a.py']
            self.assertEqual(len(rec['write_receipts']), 3)  # 全部尝试保留以示审计
            self.assertEqual(len(rec['successful_write_receipts']), 1)  # 仅成功+核验
            self.assertTrue(rec['write_receipt_linked'])

    def test_extract_write_receipts_requires_session_and_turn_match(self):
        # S4 缺陷 9 + S5 缺陷 5：身份核验必须 session 与 turn **两者齐全**且严格匹配。
        # 缺任一（缺 turn / 只有旧 turn / 缺 session）一律 unverified，绝不以其中一项
        # 冒充当前 session/turn 成功。scheduled 与 result 都要落在同一 session+turn 上。
        events = [
            {'sessionId': 'S1', 'turnId': 'T1', 'type': 'tool_call_scheduled', 'payload': {
                'toolCallId': 'c1', 'toolName': 'Write', 'input': {'file_path': '/x'}}},
            {'sessionId': 'S1', 'turnId': 'T1', 'type': 'tool_call_result', 'payload': {
                'toolCallId': 'c1', 'result': {'success': True}}},
            # 旧 turn 的同 session 写入：不属于当前 turn。
            {'sessionId': 'S1', 'turnId': 'T-old', 'type': 'tool_call_scheduled', 'payload': {
                'toolCallId': 'c2', 'toolName': 'Write', 'input': {'file_path': '/y'}}},
            {'sessionId': 'S1', 'turnId': 'T-old', 'type': 'tool_call_result', 'payload': {
                'toolCallId': 'c2', 'result': {'success': True}}},
            # 只有 session、缺 turnId 的写入。
            {'sessionId': 'S1', 'type': 'tool_call_scheduled', 'payload': {
                'toolCallId': 'c3', 'toolName': 'Write', 'input': {'file_path': '/z'}}},
            {'sessionId': 'S1', 'type': 'tool_call_result', 'payload': {
                'toolCallId': 'c3', 'result': {'success': True}}}]
        # 两者齐全：只 c1（session+turn 均匹配）算当前成功关联。
        r = cc.extract_write_receipts(events, session_id='S1', turn_id='T1')
        self.assertEqual({x['tool_use_id'] for x in r}, {'c1'})
        self.assertTrue(all(x['identity_verified'] is True for x in r))
        # 缺 turn（仅 session）→ 两者不齐 → 一律 unverified。
        r_noturn = cc.extract_write_receipts(events, session_id='S1')
        self.assertTrue(r_noturn)
        self.assertTrue(all(x['identity_verified'] is False for x in r_noturn))
        # 旧 turn：caller 给当前 turn，c2 的 turn 不符 → 不在当前成功关联集合。
        self.assertNotIn('c2', {x['tool_use_id'] for x in r})
        # 未给任何身份 → unverified。
        r_bare = cc.extract_write_receipts(events)
        self.assertTrue(all(x['identity_verified'] is False for x in r_bare))

    def test_bind_registered_test_rejects_extra_external_input(self):
        # S4 缺陷 5：追加一个 workspace 外、合法 SHA、保留旧 fingerprint 的 input → 拒绝；
        # 且绝不读取外部 bytes（invalid binding 下 stale 直接返回，不外读）。
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            ws = tmp / 'proj'
            ws.mkdir()
            f = ws / 'a.py'
            f.write_text('print(1)\n', encoding='utf-8', newline='\n')
            ev_rel, reg = make_registered_test_evidence(ws, [str(f)])
            evp = ws / 'reg-test' / 'evidence.json'
            prior = json.loads(evp.read_text(encoding='utf-8'))
            outside = tmp / 'outside-secret.py'
            outside.write_text('secret\n', encoding='utf-8')
            prior['inputs'][os.path.normcase(os.path.realpath(str(outside)))] = 'a' * 64
            evp.write_text(json.dumps(prior), encoding='utf-8')
            binding = cc.bind_registered_test(ev_rel, str(ws), reg)
            self.assertFalse(binding['valid'])
            self.assertTrue(cc.test_inputs_stale(binding)['stale'])

    def test_bind_registered_test_rejects_missing_input(self):
        # S4 缺陷 5：registered_inputs 少给一个（缺失）→ 精确集合不符 → 拒绝。
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            ws = tmp / 'proj'
            ws.mkdir()
            f1 = ws / 'a.py'
            f2 = ws / 'b.py'
            f1.write_text('1\n', encoding='utf-8', newline='\n')
            f2.write_text('2\n', encoding='utf-8', newline='\n')
            ev_rel, reg = make_registered_test_evidence(ws, [str(f1), str(f2)])
            binding = cc.bind_registered_test(ev_rel, str(ws), [reg[0]])  # 漏 reg[1]
            self.assertFalse(binding['valid'])

    def test_check_prev_handoff_drift_resolves_relative_to_workspace(self):
        # S4 缺陷 7：prev_handoff 是相对 workspace 的引用，必须按 workspace 解析而非启动 cwd。
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            ws = tmp / 'proj'
            ws.mkdir()
            (ws / 'a.py').write_text('same\n', encoding='utf-8', newline='\n')
            prev = {'handoff_kind': 'continuation-contract',
                    'workspace_realpath': os.path.realpath(str(ws)),
                    'files': {'a.py': {'current_sha256': cc._sha(b'same\n')}}}
            (ws / 'prev.json').write_text(json.dumps(prev), encoding='utf-8')
            spec = {'files': ['a.py'], 'prev_handoff': 'prev.json'}
            cwd = os.getcwd()
            try:
                os.chdir(tmp)  # 起始 cwd != workspace
                reasons = cc.check_prev_handoff_drift(spec, str(ws))
            finally:
                os.chdir(cwd)
            self.assertEqual(reasons, [])

    def test_check_prev_handoff_drift_refuses_physical_target_even_same_sha(self):
        # S5 缺陷 7：上一手冻结的物理目标与当前 realpath 不一致（A→B 同内容软链/改指向），
        # 即便内容 SHA 仍相等也必须拒绝接续，绝不透过新链接把写入落到 B。
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            ws = tmp / 'proj'
            (ws / 'src').mkdir(parents=True)
            f = ws / 'src' / 'a.py'
            data = b'same-content\n'
            f.write_bytes(data)
            sha = hashlib.sha256(data).hexdigest()
            good = json.dumps({'workspace_realpath': os.path.realpath(str(ws)),
                               'files': {'src/a.py': {'current_sha256': sha,
                                       'physical_target': os.path.realpath(str(f))}}})
            # 物理漂移：current_sha256 相同，但 physical_target 指向另一路径。
            drift = json.dumps({'workspace_realpath': os.path.realpath(str(ws)),
                                 'files': {'src/a.py': {'current_sha256': sha,
                                         'physical_target': os.path.realpath(
                                             str(ws)) + os.sep + 'OTHER' + os.sep + 'a.py'}}})
            (ws / 'prev.json').write_text(good, encoding='utf-8')
            self.assertEqual(cc.check_prev_handoff_drift(
                {'prev_handoff': 'prev.json'}, str(ws)), [])
            (ws / 'prev.json').write_text(drift, encoding='utf-8')
            reasons = cc.check_prev_handoff_drift({'prev_handoff': 'prev.json'}, str(ws))
            self.assertTrue(any('physical target drift' in r for r in reasons))

    def test_build_handoff_records_physical_target_and_binds_real_error_carrier(self):
        # S5 缺陷 7：handoff 保存上一手物理目标；original_error_ref 只绑真实错误载体
        # （存在且 SHA 核验），空/缺失的 report-state 不得冒充 original_error_ref。
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            ws = tmp / 'proj'
            (ws / 'src').mkdir(parents=True)
            f = ws / 'src' / 'a.py'
            f.write_text('v1\n', encoding='utf-8')
            frozen = cc.freeze(['src/a.py'], ws, copies_dir=tmp / 'copies')
            carrier = tmp / 'stdout.jsonl'
            carrier.write_bytes(b'{"is_error":true}\n')
            empty_state = tmp / 'report-state.json'
            empty_state.write_bytes(b'{}')
            ev = cc.evaluate(frozen, ws, copies_dir=tmp / 'copies')
            handoff = cc.build_handoff(frozen, ev, None, None, [],
                                       original_error_ref=str(carrier))
            self.assertEqual(os.path.normcase(handoff['files']['src/a.py']['physical_target']),
                             os.path.normcase(os.path.realpath(str(f))))
            self.assertTrue(handoff['original_error_ref']['verified'])
            # 指向不存在文件（模拟空 report-state 冒充）→ 核验失败，绝不宣称已绑原始错误。
            missing = cc.build_handoff(frozen, ev, None, None, [],
                                       original_error_ref=str(tmp / 'nope.jsonl'))
            self.assertFalse(missing['original_error_ref']['verified'])

    def test_evaluate_terminal_copy_respects_growth_bound(self):
        # S4 缺陷 8：终态副本复用同一越界边界；文件增长越界 → 拒绝复制，diff 不假交付。
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            ws = tmp / 'proj'
            ws.mkdir()
            f = ws / 'a.txt'
            f.write_text('small\n', encoding='utf-8')
            frozen = cc.freeze(['a.txt'], ws, copies_dir=tmp / 'c')
            f.write_bytes(b'x' * (cc.MAX_COPY_BYTES_PER_FILE + 1))
            ev = cc.evaluate(frozen, ws, copies_dir=tmp / 'c')
            rec = ev['files']['a.txt']
            self.assertNotIn('terminal_copy', rec)
            self.assertIn('terminal_copy_refused', rec)
            self.assertTrue(rec['changed'])
            self.assertEqual(cc.diff_declared(frozen, ev)['a.txt']['diff_status'],
                             'copies_not_kept')

    def test_diff_declared_rejects_tampered_copy(self):
        # S4 缺陷 8：diff 前先回读副本 SHA；副本被篡改不得仍报 diff_status=ok。
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            ws = tmp / 'proj'
            ws.mkdir()
            f = ws / 'a.txt'
            f.write_text('v1\n', encoding='utf-8', newline='\n')
            frozen = cc.freeze(['a.txt'], ws, copies_dir=tmp / 'c')
            f.write_text('v2\n', encoding='utf-8', newline='\n')
            ev = cc.evaluate(frozen, ws, copies_dir=tmp / 'c')
            self.assertEqual(cc.diff_declared(frozen, ev)['a.txt']['diff_status'], 'ok')
            Path(ev['files']['a.txt']['terminal_copy']).write_bytes(b'tampered\n')
            self.assertEqual(cc.diff_declared(frozen, ev)['a.txt']['diff_status'],
                             'copy_tampered')


class EntryIntegrationTests(unittest.TestCase):
    """codebuddy_direct 真实入口：门禁在 Popen 前生效（含不传 --dispatch-plan）。"""

    STAGE = 'BW-QUOTA-20261008-S1'
    MODEL = 'GLM-CB-1'

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.workspace = self.tmp / 'ws'
        self.workspace.mkdir()
        self.store = self.tmp / 'store' / 'state.sqlite3'
        self.routes = self.tmp / 'routes-absent.json'
        self.prompt = self.tmp / 'prompt.md'
        self.prompt.write_text('请原样汇报。', encoding='utf-8')
        # 迁移到仓库唯一可信固定合成 stub：harness 按仓库字节核验 config.cli，可信哈希
        # 由仓库定义（env 无法声明/放宽）。行为只由 CODEBUDDY_STUB_SPEC/RECORD 数据参数化。
        stub = REPO / 'tests' / 'offline_codebuddy_stub.py'
        self.config = self.tmp / 'cb-entry.json'
        self.config.write_text(json.dumps({'node': sys.executable,
                                           'cli': str(stub)}), encoding='utf-8')

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, out, events, *extra):
        spec = self.tmp / 'spec.json'
        spec.write_text(json.dumps({'stdout': [json.dumps(e, ensure_ascii=False)
                                               for e in events]}), encoding='utf-8')
        record = self.tmp / 'record.json'
        env = {**os.environ.copy(),
               'CODEBUDDY_STUB_SPEC': str(spec), 'CODEBUDDY_STUB_RECORD': str(record),
               'CB_OFFLINE_SYNTHETIC_STUB': '1',
               'BRAIN_WORKER_QUOTA_STORE': str(self.store),
               'BRAIN_WORKER_QUOTA_ROUTES': str(self.routes),
               'PYTHONIOENCODING': 'utf-8'}
        # CB 直连已退休：生产 codebuddy_direct.main 固定拒绝新直连（人工转交），额度门禁/
        # 占位释放的入口回归改由测试专用离线 harness 调用 dispatch_core 在合成 stub 下回放。
        proc = subprocess.run(
            [sys.executable, str(REPO / 'tests' / 'offline_codebuddy_harness.py'),
             '--workspace', str(self.workspace), '--prompt-file', str(self.prompt),
             '--output-dir', str(out), '--stage', self.STAGE,
             '--model', self.MODEL, '--config', str(self.config), *extra],
            capture_output=True, cwd=str(self.tmp), env=env, timeout=120)
        summary = None
        if (Path(out) / 'summary.json').is_file():
            summary = json.loads((Path(out) / 'summary.json').read_text(encoding='utf-8'))
        return {'proc': proc, 'rc': proc.returncode, 'summary': summary,
                'out': Path(out), 'stdout_text': proc.stdout.decode('utf-8', 'replace'),
                'record': json.loads(record.read_text(encoding='utf-8'))
                if record.is_file() else None}

    def _cooldown_from_terminal(self):
        qc.import_terminal(self.store, TERMINAL, runtime='codebuddy',
                           identity={'entry_config': str(self.config.resolve())},
                           routes_path=self.routes,
                           now=datetime(2026, 10, 8, 5, 34, tzinfo=timezone.utc))

    def test_cooldown_blocks_dispatch_without_plan(self):
        self._cooldown_from_terminal()
        events = [cb_init(), cb_result_success('x')]
        res = self._run(self.tmp / 'out-blocked', events)  # 不传 --dispatch-plan
        self.assertEqual(res['rc'], 2)
        self.assertFalse(res['out'].exists())  # 零输出目录、零模型额度
        self.assertIn('"sent": false', res['stdout_text'])
        self.assertIn('2026-10-08T10:14:13', res['stdout_text'])
        self.assertIsNone(res['record'])  # stub 未被启动

    def test_exit0_is_error_true_records_cooldown(self):
        # CLI exit 0 + result.is_error=true 的 429 信封：交付失败并落冷却。
        events = [cb_init(), cb_result_429()]
        res = self._run(self.tmp / 'out-429', events)
        self.assertEqual(res['rc'], 3)
        self.assertFalse(res['summary']['protocol_success'])
        self.assertFalse(res['summary']['report_bound'])
        self.assertTrue(res['summary']['quota_outcome']['recorded'])
        request = json.loads((res['out'] / 'request.json').read_text(encoding='utf-8'))
        self.assertEqual(request['cli_retry_policy']['CODEBUDDY_MAX_RETRIES'], '2')
        self.assertEqual(request['cli_retry_policy']['CODEBUDDY_RETRY_WATCHDOG'], '0')
        self.assertEqual(res['record']['max_retries_env'], '2')
        self.assertEqual(res['record']['watchdog_env'], '0')
        status = qc.get_status(self.store, qc.UNKNOWN_SHARED_GROUP)
        self.assertEqual(status['cooldowns'][qc.UNKNOWN_SHARED_GROUP]['state'],
                         'cooling')
        self.assertEqual(datetime.fromisoformat(
            status['cooldowns'][qc.UNKNOWN_SHARED_GROUP]['cooldown_until_utc']),
            EXPECTED_RESET_UTC)
        # 冷却中再次派发（换输出目录也不行）被门禁拒绝。
        res2 = self._run(self.tmp / 'out-429-again', events)
        self.assertEqual(res2['rc'], 2)
        self.assertIn('"sent": false', res2['stdout_text'])

    def test_success_clears_and_lock_released(self):
        report = nine_report(self.STAGE, str(self.workspace.resolve()))
        events = [cb_init(),
                  {'type': 'assistant', 'session_id': 'S-1',
                   'message': {'model': self.MODEL,
                               'usage': {'input_tokens': 1, 'output_tokens': 2},
                               'content': [{'type': 'text', 'text': report}]}},
                  cb_result_success(report)]
        out = self.tmp / 'out-ok'
        res = self._run(out, events)
        self.assertEqual(res['rc'], 0, res['stdout_text'])
        self.assertTrue(res['summary']['report_bound'])
        # 工作区占位已随终态释放；无冷却组无副作用。
        status = qc.get_status(self.store)
        self.assertEqual(status['workspace_locks'], [])
        # 再次派发不被自身残留占位阻断（未匹配路由 → unknown-shared，无冷却）。
        res2 = self._run(self.tmp / 'out-ok2', events)
        self.assertEqual(res2['rc'], 0, res2['stdout_text'])

    def test_probe_bounds_refused_before_dispatch(self):
        # 缺陷 D：recovery probe 授予写工具 → 程序化边界在建目录/Popen 前拒绝、零目录。
        events = [cb_init(), cb_result_success('x')]
        res = self._run(self.tmp / 'out-probe', events,
                        '--quota-recovery-probe', '--tools', 'Write')
        self.assertEqual(res['rc'], 2)
        self.assertFalse(res['out'].exists())
        self.assertIn('recovery probe bounds violated', res['stdout_text'])

    def test_continuation_drift_refused_before_dispatch(self):
        # 缺陷 I：上一手 handoff 声明文件已漂移 → 接续拒绝、零目录、不静默覆盖。
        ws = self.workspace
        (ws / 'tracked.py').write_text('baseline\n', encoding='utf-8', newline='\n')
        frozen = cc.freeze(['tracked.py'], str(ws))
        ev = cc.evaluate(frozen, str(ws))
        handoff = cc.build_handoff(frozen, ev, None, None, ['todo'])
        (ws / 'tracked.py').write_text('silently changed after handoff\n',
                                       encoding='utf-8', newline='\n')
        spec = {'files': ['tracked.py'], 'prev_handoff': 'continuation-prior.json'}
        (ws / 'continuation-prior.json').write_text(json.dumps(handoff), encoding='utf-8')
        (ws / 'cont-contract.json').write_text(json.dumps(spec), encoding='utf-8')
        events = [cb_init(), cb_result_success('x')]
        res = self._run(self.tmp / 'out-drift', events,
                        '--continuation-contract', str(ws / 'cont-contract.json'))
        self.assertEqual(res['rc'], 2)
        self.assertFalse(res['out'].exists())
        self.assertIn('drift', res['stdout_text'])

    def test_start_failed_releases_placeholder_no_leak(self):
        # S4 缺陷 2：输出目录冲突在建目录/Popen 前被拒绝；未启动 stub、零额度、
        # 不泄漏工作区/通道占位，且不越界删除用户预建目录。
        out = self.tmp / 'out-collide'
        out.mkdir()  # 预建同名输出目录
        events = [cb_init(), cb_result_success('x')]
        res = self._run(out, events)
        self.assertEqual(res['rc'], 2)
        self.assertIn('output directory already exists', res['stdout_text'])
        self.assertIsNone(res['record'])  # stub 从未启动 → 零模型额度
        status = qc.get_status(self.store)
        self.assertEqual(status['workspace_locks'], [])
        self.assertEqual(status['channel_locks'], [])
        self.assertTrue(out.exists())  # 用户已有目录未被删除


class PrepLeakTests(unittest.TestCase):
    """S5 缺陷 1：gate→Popen 之间的准备失败必须在统一 known-not-started 生命周期里
    settle 本 attempt 的 start_failed——零 Popen、sent=false、释放本占位不泄漏。分别注入
    mkdir 普通 OSError（PermissionError）与 payload 写 PermissionError 验证锁清理。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.ws = self.tmp / 'ws'
        self.ws.mkdir()
        self.store = self.tmp / 'store' / 'state.sqlite3'
        self.store.parent.mkdir(parents=True, exist_ok=True)
        self.routes = self.tmp / 'routes-absent.json'
        self.prompt = self.tmp / 'prompt.md'
        self.prompt.write_text('请原样汇报。', encoding='utf-8')
        self.out = self.tmp / 'out'
        # CodeBuddy 入口配置：node 用当前解释器，cli 指向仓库唯一可信固定合成 stub。
        # replay_dispatch 现在也跑同一套隔离/可信字节门禁，故 cli 必须通过校验才能抵达
        # 被注入的 mkdir/payload 失败点（绝不 Popen，注入发生在 Popen 之前）。
        self.cb_stub = REPO / 'tests' / 'offline_codebuddy_stub.py'
        self.cb_config = self.tmp / 'cb.json'
        self.cb_config.write_text(json.dumps({'node': sys.executable,
                                              'cli': str(self.cb_stub)}), encoding='utf-8')
        # ZCode 入口配置：CONFIG_FILE_KEYS 必须都是存在的绝对文件。
        self.z_files = {}
        for key in ('node', 'bootstrap', 'tsx_loader',
                    'builtin_provider_config', 'personal_provider_config'):
            p = self.tmp / (key.replace('_', '-') + '.bin')
            p.write_text('runtime-stub\n', encoding='utf-8')
            self.z_files[key] = str(p)
        self.z_config = self.tmp / 'z.json'
        self.z_config.write_text(json.dumps(self.z_files), encoding='utf-8')

    def tearDown(self):
        self._tmp.cleanup()

    def _cb_argv(self):
        return ['codebuddy_direct.py', '--workspace', str(self.ws),
                '--prompt-file', str(self.prompt), '--output-dir', str(self.out),
                '--stage', 'BW-QUOTA-20261008-S5', '--model', 'GLM-CB-1',
                '--config', str(self.cb_config), '--quota-store', str(self.store),
                '--quota-routes', str(self.routes)]

    def _cb_pipeline(self):
        """退休政策：生产 cbd.main 现固定拒绝新直连，只走人工转交。额度门禁/占位释放
        的入口回归改由**已搬出生产模块**的传输链（harness.replay_dispatch，历史解析/
        结算/终态留证）离线回放；生产入口本身在别处（manual_relay 测试）单独证明拒绝。
        本回归进程内直调 replay_dispatch（仓库可信合成 stub，mkdir/payload 先失败，绝不
        Popen），sys.argv 已由 _run 打桩。"""
        return cbh.replay_dispatch(cbd.build_parser().parse_args())

    def _zd_argv(self):
        return ['zcode_direct.py', '--workspace', str(self.ws),
                '--prompt-file', str(self.prompt), '--output-dir', str(self.out),
                '--stage', 'BW-QUOTA-20261008-S5', '--provider', zd.DEFAULT_PROVIDER,
                '--config', str(self.z_config), '--quota-store', str(self.store),
                '--quota-routes', str(self.routes),
                '--dispatch-store', str(self.tmp / 'dispatch-pool.sqlite3')]

    def _assert_cleaned(self, stdout_text):
        self.assertIn('"sent": false', stdout_text)
        status = qc.get_status(self.store)
        self.assertEqual(status['workspace_locks'], [])
        self.assertEqual(status['channel_locks'], [])
        # 不泄漏、不越界：out 目录可能已由 mkdir 创建，也可能没有；两种都不残留锁。

    def _run(self, argv, main, patchers):
        buf = io.StringIO()
        with mock.patch.object(sys, 'argv', argv), \
                mock.patch.dict(os.environ, {'PYTHONIOENCODING': 'utf-8',
                                             'CB_OFFLINE_SYNTHETIC_STUB': '1'}):
            with contextlib.ExitStack() as stack:
                for p in patchers:
                    stack.enter_context(p)
                with contextlib.redirect_stdout(buf):
                    rc = main()
        return rc, buf.getvalue()

    def _out_only_mkdir(self, exc):
        """只在输出目录 mkdir 处抛 exc；store/connect 等其它 mkdir 走真实实现。
        autospec 使 side_effect 收到绑定的 self，才能按路径判定。入口可能对 output_dir
        resolve()，故原始与解析后的路径都视为目标。"""
        real = Path.mkdir
        targets = {str(self.out), str(self.out.resolve())}

        def side_effect(self_p, *a, **k):
            if str(self_p) in targets:
                raise exc
            return real(self_p, *a, **k)

        return side_effect

    def test_codebuddy_mkdir_generic_oserror_releases(self):
        # mkdir 普通 OSError（PermissionError，非 FileExistsError）→ start_failed 释放。
        patchers = [mock.patch.object(Path, 'mkdir', autospec=True,
                                      side_effect=self._out_only_mkdir(
                                          PermissionError('no perm')))]
        rc, out_text = self._run(self._cb_argv(), self._cb_pipeline, patchers)
        self.assertEqual(rc, 3, out_text)
        self.assertIn('before start', out_text)
        self._assert_cleaned(out_text)

    def test_codebuddy_payload_permissionerror_releases(self):
        # 载荷构造/写失败（PermissionError）→ 统一 start_failed 释放，不泄漏、零 Popen。
        patchers = [mock.patch.object(cbd.pc, 'compose_task_payload',
                                      side_effect=PermissionError('disk'))]
        rc, out_text = self._run(self._cb_argv(), self._cb_pipeline, patchers)
        self.assertEqual(rc, 3, out_text)
        self._assert_cleaned(out_text)
        self.assertFalse(self.out.joinpath('request.json').exists())  # 未写请求、未派发

    def test_zcode_mkdir_generic_oserror_releases(self):
        patchers = [mock.patch.object(Path, 'mkdir', autospec=True,
                                      side_effect=self._out_only_mkdir(
                                          PermissionError('no perm')))]
        rc, out_text = self._run(self._zd_argv(), zd.main, patchers)
        self.assertEqual(rc, 3, out_text)
        self._assert_cleaned(out_text)

    def test_zcode_payload_permissionerror_releases(self):
        patchers = [mock.patch.object(zd.pc, 'compose_task_payload',
                                      side_effect=PermissionError('disk'))]
        rc, out_text = self._run(self._zd_argv(), zd.main, patchers)
        self.assertEqual(rc, 3, out_text)
        self._assert_cleaned(out_text)
        self.assertFalse(self.out.joinpath('request.json').exists())


class ZCodeEntryReplayTests(unittest.TestCase):
    """S6 缺陷 2 + BW-ZCODE-MANUAL-QUOTA-20261008-S1：完整入口离线回放（非手工 helper）。
    复用 PrepLeakTests 的临时 stub 配置/工作区/routes-absent（→ unknown-shared），mock
    subprocess.Popen（零 OS 子进程/网络/模型）按入口真实写出的 stdout.json / stderr.log
    文件句柄注入 S2 同形 string-only errors 信封 + 随包白名单
    stderr_error_frame。新政策：ZCode 取消自动额度冷却，入口对 429/1308 只失败退出、
    如实记录原始数字码 1308 与错误载体，不再落本组冷却、第二次独立新 output 仍可派工，
    一次 main 恰好一次 mock 进程（不自动重试）；original_error_ref 直指本次 stdout 失败
    信封、SHA 对原始字节核验，空/仅 warning 的 stderr 绝不冒充原错误，缺报告如实保留。"""

    REAL_STDERR_FRAME = ('ProviderBusinessError: [1308][已达到 5 小时的使用上限。'
                         '您的限额将在 2026-10-08 15:17:30 重置。]\n'
                         "code: 'PROVIDER_BUSINESS_ERROR',\n"
                         "code: '1308',\n"
                         'responseStatus: 429,\n').encode('utf-8')

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.ws = self.tmp / 'ws'
        self.ws.mkdir()
        self.store = self.tmp / 'store' / 'state.sqlite3'
        self.store.parent.mkdir(parents=True, exist_ok=True)
        self.routes = self.tmp / 'routes-absent.json'  # 不存在 → 通道落 unknown-shared
        self.prompt = self.tmp / 'prompt.md'
        self.prompt.write_text('请原样汇报。', encoding='utf-8')
        self.z_files = {}
        for key in ('node', 'bootstrap', 'tsx_loader',
                    'builtin_provider_config', 'personal_provider_config'):
            p = self.tmp / (key.replace('_', '-') + '.bin')
            p.write_text('runtime-stub\n', encoding='utf-8')
            self.z_files[key] = str(p)
        self.z_config = self.tmp / 'z.json'
        self.z_config.write_text(json.dumps(self.z_files), encoding='utf-8')

    def tearDown(self):
        self._tmp.cleanup()

    def _argv(self, out):
        # 本回归只验证额度冷却语义，不验证并发轮换：每次回放用**各自独立**的临时容量池
        # （按 out 名派生），避免 1:1 轮换把第二次 zcode 派工路由到 qoder 而干扰断言。
        # 额度 store 仍共享（self.store），冷却/结算语义不变。
        out = Path(out)
        return ['zcode_direct.py', '--workspace', str(self.ws),
                '--prompt-file', str(self.prompt), '--output-dir', str(out),
                '--stage', 'BW-QUOTA-20261008-S6', '--provider', zd.DEFAULT_PROVIDER,
                '--config', str(self.z_config), '--quota-store', str(self.store),
                '--quota-routes', str(self.routes),
                '--dispatch-store', str(out.parent / f'dispatch-{out.name}.sqlite3')]

    class _Child:
        def __init__(self, rc):
            self.returncode = rc
            self.pid = 424242

        def wait(self):
            return self.returncode

        def communicate(self, *a, **k):
            return (b'', b'')

        def kill(self):
            pass

    def _run(self, out, stdout_bytes, stderr_bytes, rc, counter):
        def _popen(argv, *a, **k):
            counter['n'] += 1
            k['stdout'].write(stdout_bytes)
            k['stderr'].write(stderr_bytes)
            return self._Child(rc)

        buf = io.StringIO()
        with mock.patch.object(sys, 'argv', self._argv(out)), \
                mock.patch.dict(os.environ, {'PYTHONIOENCODING': 'utf-8'}), \
                mock.patch.object(zd.subprocess, 'Popen', side_effect=_popen):
            with contextlib.redirect_stdout(buf):
                code = zd.main()
        summary = json.loads((Path(out) / 'summary.json').read_text(encoding='utf-8'))
        return code, summary, buf.getvalue()

    def test_entry_reports_1308_without_auto_cooldown_allows_next_dispatch(self):
        # 新政策（BW-ZCODE-MANUAL-QUOTA-20261008-S1）：ZCode 手动额度、取消自动冷却。
        # 完整入口回放 429/1308 → 失败退出、原始数字码 1308、错误载体 SHA 仍真，但**不落
        # 本组冷却**；随后独立新 output 仍可派工（不被冷却挡），一次 main 恰好一次 mock
        # 进程、不自动重试。
        counter = {'n': 0}
        env = {'carrier': 'zcode-sdk', 'ok': False, 'preflight_ok': True,
               'errors': ['ProviderBusinessError: [1308][已达到 5 小时的使用上限。'
                          '您的限额将在 2026-10-08 15:17:30 重置。]'],
               'errors_info': None, 'event_count': 0}
        stdout_bytes = json.dumps(env, ensure_ascii=False).encode('utf-8')
        out1 = self.tmp / 'out-1308'
        code, summary, _ = self._run(out1, stdout_bytes, self.REAL_STDERR_FRAME, 5, counter)
        self.assertEqual(code, 3, summary)
        self.assertFalse(summary['protocol_success'])
        self.assertEqual(counter['n'], 1)  # 恰好一次 Popen（不自动重试）
        # 入口明确标识 ZCode 自动额度冷却禁用，且失败但不谎称额度/恢复/免费。
        self.assertTrue(summary['quota_auto_cooldown_disabled'])
        self.assertFalse(summary['free_quota_verified'])
        # 取消自动冷却：入口对 ZCode 不再落本组冷却（unknown-shared 不出现）。
        self.assertNotIn(qc.UNKNOWN_SHARED_GROUP, qc.get_status(self.store)['cooldowns'])
        outcome = summary['quota_outcome']
        self.assertTrue(outcome['auto_cooldown_disabled'])
        self.assertFalse(outcome.get('recorded'))
        # 原始数字码 1308（内层），外层 wrapper 码区分保留；无时区 reset→unverified 不猜。
        ref = summary['quota_wrapper_reference']
        self.assertEqual(ref['provider_code'], 1308)
        self.assertEqual(ref['wrapper_code'], 'PROVIDER_BUSINESS_ERROR')
        self.assertTrue(str(ref['reset_timezone']).startswith('unverified'))
        # 原始错误引用直指本次 stdout 失败信封，SHA 对原始字节核验，绝不指向 report-state。
        oer = summary['original_error_reference']
        self.assertEqual(os.path.realpath(oer['path']),
                         os.path.realpath(str(out1 / 'stdout.json')))
        self.assertEqual(oer['sha256'], hashlib.sha256(stdout_bytes).hexdigest())
        self.assertNotIn('report-state', oer['path'])
        # SDK ProviderBusinessError 的本次 stderr 原 ref/SHA 一并保留。
        self.assertEqual(os.path.realpath(oer['sdk_stderr_frame']['path']),
                         os.path.realpath(str(out1 / 'stderr.log')))
        self.assertEqual(oer['sdk_stderr_frame']['sha256'],
                         hashlib.sha256(self.REAL_STDERR_FRAME).hexdigest())
        # 缺原始报告如实保留：无 response.md → 不宣称 bound，不补写报告。
        self.assertFalse(summary['report_bound'])
        self.assertFalse((out1 / 'response.md').exists())
        # 第二次独立新 output 不再被冷却挡住：正常派工、mock 进程发生、仍不落冷却。
        out2 = self.tmp / 'out-1308-again'
        code2, summary2, _ = self._run(out2, stdout_bytes, self.REAL_STDERR_FRAME,
                                       5, counter)
        self.assertEqual(counter['n'], 2)  # 第二次确实派工（一次 main 仍只一次 Popen）
        self.assertEqual(code2, 3)         # 本次运行因模型 429 失败，非门禁 sent=false
        self.assertTrue(summary2['quota_auto_cooldown_disabled'])
        self.assertNotIn(qc.UNKNOWN_SHARED_GROUP, qc.get_status(self.store)['cooldowns'])

    def test_structured_stdout_429_empty_stderr_binds_stdout_envelope(self):
        # 缺陷 2 对照：结构化 429 只在 stdout.json、stderr 为空 → 原错误 ref 直接指本次
        # stdout 失败信封且 SHA 一致（绝不因 stderr 空漏记，也绝不绑空/缺失 stderr）。
        counter = {'n': 0}
        env = {'carrier': 'zcode-sdk', 'ok': False, 'preflight_ok': True,
               'errors': ['quota exceeded'],
               'errors_info': [{'response_status': 429,
                                'provider': 'account:bigmodel-individual-coding-plan',
                                'provider_code': 429, 'message': '[429] too many requests'}],
               'event_count': 0}
        stdout_bytes = json.dumps(env, ensure_ascii=False).encode('utf-8')
        out = self.tmp / 'out-structured'
        code, summary, _ = self._run(out, stdout_bytes, b'', 5, counter)
        self.assertEqual(code, 3)
        oer = summary['original_error_reference']
        self.assertEqual(os.path.realpath(oer['path']),
                         os.path.realpath(str(out / 'stdout.json')))
        self.assertEqual(oer['sha256'], hashlib.sha256(stdout_bytes).hexdigest())
        self.assertNotIn('sdk_stderr_frame', oer)  # 空 stderr 无真实帧 → 不冒充

    def test_warning_only_stderr_does_not_masquerade_as_original_error(self):
        # 缺陷 2 对照：stderr 仅 warning（无白名单错误帧）不得冒充原错误；本次 stdout
        # 失败信封才是原错误载体。
        counter = {'n': 0}
        env = {'carrier': 'zcode-sdk', 'ok': False, 'preflight_ok': True,
               'errors': ['ProviderBusinessError: [1308][已达到 5 小时的使用上限]'],
               'errors_info': None, 'event_count': 0}
        stdout_bytes = json.dumps(env, ensure_ascii=False).encode('utf-8')
        out = self.tmp / 'out-warning'
        code, summary, _ = self._run(out, stdout_bytes, b'WARNING: noisy but not an error\n',
                                     5, counter)
        self.assertEqual(code, 3)
        oer = summary['original_error_reference']
        self.assertEqual(os.path.realpath(oer['path']),  # 绝不指 warning stderr
                         os.path.realpath(str(out / 'stdout.json')))
        self.assertEqual(oer['sha256'], hashlib.sha256(stdout_bytes).hexdigest())
        self.assertNotIn('sdk_stderr_frame', oer)


class CBReceiptIdentityTests(unittest.TestCase):
    """S5 缺陷 6：CB 回执只认事件自带身份——同 ID、同 session、result 非 is_error、
    且不在 tool_failures flags 里才算成功+核验；缺身份只标 unverified，绝不拿全局
    session 补造，也不把缺字段的真实流当作“无执行”。"""

    def _use(self, id_, sid, line, path='/x/a.py', name='Write'):
        return {'name': name, 'id': id_, 'file_path': path, 'session': sid, 'line': line}

    def _res(self, id_, sid, line, is_error=False):
        return {'tool_use_id': id_, 'session': sid, 'line': line, 'is_error': is_error}

    def test_full_success_same_session_verified(self):
        parsed = {'tool_use_events': [self._use('t1', 'S1', 3)],
                  'tool_results': [self._res('t1', 'S1', 8)],
                  'tool_failures': [], 'session_id': 'S1'}
        r = cbd._cb_write_receipts(parsed)
        self.assertEqual(len(r), 1)
        self.assertTrue(r[0]['successful'])
        self.assertTrue(r[0]['identity_verified'])
        self.assertEqual((r[0]['use_line'], r[0]['result_line']), (3, 8))

    def test_old_old_pair_vs_current_session_not_verified(self):
        # S6 缺陷 1：use 与 result session 彼此相同但都等于 OLD，而本流当前 session 为
        # CURRENT → 三者不齐 → 绝不 identity_verified（旧会话复用不得冒充本次成功）。
        parsed = {'tool_use_events': [self._use('t1', 'OLD', 3)],
                  'tool_results': [self._res('t1', 'OLD', 8)],
                  'tool_failures': [], 'session_id': 'CURRENT'}
        r = cbd._cb_write_receipts(parsed)
        self.assertEqual(len(r), 1)
        self.assertTrue(r[0]['successful'])          # 真实成功仍在
        self.assertFalse(r[0]['identity_verified'])   # 但身份不属本次 CURRENT 会话

    def test_use_current_but_result_old_not_verified(self):
        # use=CURRENT、result=OLD：三者不齐（≠全等）→ unverified；且绝不拿全局补造。
        parsed = {'tool_use_events': [self._use('t1', 'CURRENT', 3)],
                  'tool_results': [self._res('t1', 'OLD', 8)],
                  'tool_failures': [], 'session_id': 'CURRENT'}
        r = cbd._cb_write_receipts(parsed)
        self.assertFalse(r[0]['identity_verified'])

    def test_failure_flag_excludes_success(self):
        # 同 ID 出现在 tool_failures.flags：即便 result 非 is_error 也不算成功。
        parsed = {'tool_use_events': [self._use('t1', 'S1', 3)],
                  'tool_results': [self._res('t1', 'S1', 8, is_error=False)],
                  'tool_failures': [{'tool_use_id': 't1'}]}
        r = cbd._cb_write_receipts(parsed)
        self.assertEqual(len(r), 1)
        self.assertFalse(r[0]['successful'])  # 真实流有执行尝试，只是失败——不是“无执行”

    def test_cross_session_not_identity_verified(self):
        # use 与 result 分属不同 session → 不得核验；也不得拿全局 session 补造。
        parsed = {'tool_use_events': [self._use('t1', 'S1', 3)],
                  'tool_results': [self._res('t1', 'S2', 8)],
                  'tool_failures': [], 'session_id': 'GLOBAL'}
        r = cbd._cb_write_receipts(parsed)
        self.assertEqual(len(r), 1)
        self.assertFalse(r[0]['identity_verified'])

    def test_missing_result_identity_stays_unverified_not_dropped(self):
        # result 缺 session（身份不明）→ 仍保留该执行尝试，只把回执身份标 unverified；
        # 绝不因缺字段就判定“无执行”而丢弃。
        parsed = {'tool_use_events': [self._use('t1', 'S1', 3)],
                  'tool_results': [{'tool_use_id': 't1', 'is_error': False, 'line': 8}],
                  'tool_failures': []}
        r = cbd._cb_write_receipts(parsed)
        self.assertEqual(len(r), 1)
        self.assertFalse(r[0]['identity_verified'])

    def test_empty_tool_use_id_is_not_fabricated(self):
        parsed = {'tool_use_events': [self._use('', 'S1', 3)],
                  'tool_results': [], 'tool_failures': []}
        r = cbd._cb_write_receipts(parsed)
        self.assertEqual(r, [])  # 空 ID 无法关联，不伪造回执


class StderrFrameRealOrderTests(unittest.TestCase):
    """S5 缺陷 2/3：真实白名单帧保留原始行序——外层 wrapper_code 在前、内层数字
    provider_code 1308 在后；普通 429（非 1308、无耗尽）不得误归 quota→24h。"""

    REAL_FRAME = ('ProviderBusinessError: [1308][已达到 5 小时的使用上限。'
                  '您的限额将在 2026-10-08 15:17:30 重置。]\n'
                  "code: 'PROVIDER_BUSINESS_ERROR',\n"
                  "code: '1308',\n"
                  'responseStatus: 429,\n').encode('utf-8')

    def test_inner_numeric_code_not_outer_wrapper(self):
        entry = zd._provider_business_error_from_stderr(self.REAL_FRAME, 'acct-z')
        self.assertIsNotNone(entry)
        self.assertEqual(entry['status'], 429)
        # 内层真实数字码（不是外层 PROVIDER_BUSINESS_ERROR）：
        self.assertEqual(entry['code'], '1308')
        self.assertEqual(entry['provider_code'], 1308)      # 数值保留，非只 str
        self.assertEqual(entry['wrapper_code'], 'PROVIDER_BUSINESS_ERROR')
        self.assertEqual(entry['category'], 'quota')        # 1308 = 实际额度事实
        self.assertNotIn('PROVIDER_BUSINESS_ERROR', entry['code'])

    def test_ordinary_429_frame_is_not_quota_no_window(self):
        # 无耗尽、无窗口的普通 429（非 1308）不得标 quota，应走 temporary_backoff。
        frame = ('ProviderBusinessError: rate limited\n'
                 "code: 'PROVIDER_BUSINESS_ERROR',\n"
                 "code: '429',\n"
                 'responseStatus: 429,\n').encode('utf-8')
        entry = zd._provider_business_error_from_stderr(frame, 'acct-z')
        self.assertIsNotNone(entry)
        self.assertEqual(entry['category'], 'rate_limit')
        self.assertEqual(entry['provider_code'], 429)
        cls = qc.classify_quota_failure(['429 rate limited'], [entry], now=now_2026())
        self.assertTrue(cls['is_quota_429'])
        self.assertEqual(cls['kind'], 'temporary_backoff')

    def test_structured_envelope_provider_code_numeric_preserved(self):
        envelope = {'errors': [{'response_status': 429, 'provider_code': 1308,
                                'provider': 'account:bigmodel-individual-coding-plan',
                                'message': '[1308][已达到 5 小时的使用上限。]'}],
                    'errors_info': None}
        w = zd._wrapper_quota_error(envelope)
        self.assertEqual(w['provider_code'], 1308)          # 数值保留
        self.assertEqual(w['category'], 'quota')
        # 普通 429、非 1308、无 quota 标记 → rate_limit（不误归 24h）。
        env2 = {'errors': [{'response_status': 429, 'provider_code': 429,
                            'message': 'too many requests'}]}
        w2 = zd._wrapper_quota_error(env2)
        self.assertEqual(w2['category'], 'rate_limit')

    def test_real_1308_frame_auto_records_cooldown_with_code(self):
        # S5 缺陷 2：入口对 stderr 用的就是本解析函数；把真实帧喂进 classify→record，
        # 必须自动落冷却并按内层数字码 1308（非外层 wrapper）归 quota；无时区→保守 24h，
        # 绝不因偶然文本猜 UTC+8。冷却来源保留数字 provider_code。
        entry = zd._provider_business_error_from_stderr(self.REAL_FRAME, 'acct-z')
        self.assertIsNotNone(entry)
        msg = entry['message']
        now = now_2026()
        cls = qc.classify_quota_failure([msg], [entry], now=now)
        self.assertTrue(cls['is_quota_429'])
        self.assertEqual(cls['kind'], 'quota_no_window')  # 无时区不猜 → 保守无窗口
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / 'state.sqlite3'
            rec = qc.record_quota_event(store, 'g-zc', cls,
                                        source=f'zcode provider_code {entry["provider_code"]}',
                                        now=now)
            self.assertTrue(rec['recorded'])
            status = qc.get_status(store, 'g-zc')
            cd = status['cooldowns']['g-zc']
            self.assertEqual(cd['state'], 'cooling')
            self.assertEqual(datetime.fromisoformat(cd['cooldown_until_utc']),
                             now + timedelta(hours=qc.QUOTA_NO_WINDOW_HOURS))
            self.assertIn('1308', cd['source'])            # 数字码进入可审计来源


def now_2026():
    return datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc)


if __name__ == '__main__':
    unittest.main()
