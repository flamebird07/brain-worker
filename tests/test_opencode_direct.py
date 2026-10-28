"""BW-OPENCODE-20261019-S3 离线回归：opencode_direct 正式直连入口。

零真实 CLI / 零网络 / 零模型：注入假 Popen + 独立临时 dispatch store 驱动整条原子
生命周期（consume_for_entry → mark_running → bind_child → communicate → finish），
断言：两个精确免费模型硬门（其它 model 在任何副作用前拒绝、0 Popen）、有界墙钟、
outdir 必须不存在、prompt/config/argv 预检先行、argv 与 env 隔离形状
（OPENCODE_CONFIG_CONTENT share=disabled、* deny/read+edit allow/
external_directory deny、mcp {}、plugin []、私有目录、禁用更新/LSP/Claude 兼容）、
stdin 携带完整九节契约（含本轮身份）+ 任务原文、JSON 流语义（末 step_finish=stop
才协议成功、tool-calls 中停是失败、工具 error/权限拒绝/缺失回执单独记录、顶层 error
结构化分类 quota/auth/permission/transport、正常 read 正文里的 429/ECONNRESET 不误报、
零工具回执不算工程成功）、真实终态释放本槽 vs 绑定失败/communicate 崩溃保留 unknown
不假释放、business_verified 恒 false、session_id 缺失不绑定。
BW-ZCODE-RESTORE-OPENCODE-REPAIR-20261022：缺省配置发现顺序（仓库相邻真实配置 →
已安装引擎回退 → OPENCODE_EXE/PATH；显式 --config 缺失仍拒绝）fail-closed。
真实 OpenCode 调用与免费额度由 root 本机执行验证；本文件只证明离线设计正确。
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import dispatch_pool as dp            # noqa: E402
import execution_control as ec        # noqa: E402
import opencode_direct as od          # noqa: E402
import prompt_contract as pc          # noqa: E402

STAGE = 'BW-TEST-S1'
DEEPSEEK = dp.OPENCODE_DEEPSEEK_MODEL
GLM = dp.OPENCODE_GLM_MODEL
DPOOL = dp.OPENCODE_DEEPSEEK_POOL_KEY
GPOOL = dp.OPENCODE_GLM_POOL_KEY
# 北京时间 2026-10-19 12:00（周一白天）：三个 Qoder Max 时段门此时关闭；OpenCode 无
# 时段门，派工在本时刻照常成功——时段无关性由本夹具时间自证。
NOW = datetime(2026, 10, 19, 4, 0, 0, tzinfo=timezone.utc)


def _report_body(stage=STAGE, workspace=None):
    lines = [pc.REPORT_START,
             f'阶段编号与执行方式：{stage}；direct。',
             f'实际项目绝对路径：{workspace}',
             '汇报时间与执行环境：offline stub']
    for h in pc.SECTION_HEADERS:
        lines.append(f'{h}\n- 字段：值')
    lines.append(pc.CLOSING_LINE)
    lines.append(pc.REPORT_END)
    return '\n'.join(lines)


def _jsonl(events):
    return ('\n'.join(json.dumps(e) for e in events)).encode('utf-8')


SID = 'sess-offline-1'


def ev_step_start():
    return {'type': 'step_start', 'sessionID': SID,
            'part': {'type': 'step-start', 'sessionID': SID}}


def ev_step_finish(reason='stop'):
    return {'type': 'step_finish', 'sessionID': SID,
            'part': {'type': 'step-finish', 'reason': reason,
                     'tokens': {'input': 1, 'output': 2}, 'cost': 0,
                     'sessionID': SID}}


def ev_text(text, sid=SID):
    return {'type': 'text', 'sessionID': sid,
            'part': {'type': 'text', 'text': text, 'sessionID': sid}}


def ev_tool(call='c1', tool='read', status='completed', error=None,
            input=None, output='ok', sid=SID):
    state = {'status': status, 'input': input if input is not None
             else {'filePath': 'a.txt'}, 'output': output}
    if error is not None:
        state['error'] = error
    return {'type': 'tool_use', 'sessionID': sid,
            'part': {'type': 'tool', 'tool': tool, 'callID': call,
                     'sessionID': sid, 'state': state}}


def ev_top_error(name='APIError', message='boom'):
    return {'type': 'error', 'sessionID': SID,
            'error': {'name': name, 'data': {'message': message}}}


def strip_sid(node):
    if isinstance(node, dict):
        return {k: strip_sid(v) for k, v in node.items() if k != 'sessionID'}
    if isinstance(node, list):
        return [strip_sid(v) for v in node]
    return node


def success_stream(body=None, workspace=None):
    return _jsonl([ev_step_start(),
                   ev_tool(),
                   ev_step_finish('tool-calls'),
                   ev_step_start(),
                   ev_text(body if body is not None
                           else _report_body(workspace=workspace)),
                   ev_step_finish('stop')])


class FakePopen:
    """假 Popen：记录 argv/cwd/env 与 stdin 写入；可注入首次 communicate 超时、
    communicate 崩溃或构造期崩溃（Popen 未起）。"""

    def __init__(self, stdout=b'', stderr=b'', returncode=0, pid=4242,
                 timeout=False, raise_comm=None, raise_init=False, sink=None):
        self._stdout, self._stderr = stdout, stderr
        self._rc, self.pid = returncode, pid
        self._timeout = timeout
        self._raise_comm = raise_comm
        self._raise_init = raise_init
        self.sink = sink
        self.argv = self.cwd = self.env = None
        self.returncode = None
        self.stdin_data = None
        self.killed = False

    def __call__(self, argv, *, cwd=None, stdin=None, stdout=None, stderr=None,
                 env=None):
        if self.sink is not None:
            self.sink.append(self)
        self.argv, self.cwd, self.env = list(argv), str(cwd), env
        if self._raise_init:
            raise OSError('boom before handle')
        return self

    def communicate(self, input=None, timeout=None):
        if self._timeout:
            self._timeout = False
            raise subprocess.TimeoutExpired(self.argv[0], timeout or 1)
        if self._raise_comm:
            raise self._raise_comm
        if input is not None:
            self.stdin_data = input
        if self.returncode is None:
            self.returncode = self._rc
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True
        self.returncode = -9


class PureFunctionTests(unittest.TestCase):
    def test_json_progress_count_ignores_noise_and_counts_valid_events(self):
        raw = (b'not-json\n'
               b'{"type":"step_start"}\n'
               b'{"message":"no type"}\n'
               b'{"type":"text","part":{"text":"ok"}}\n')
        self.assertEqual(od._json_progress_count(raw), 2)

    def test_no_progress_watch_stops_real_silent_child(self):
        proc = subprocess.Popen(
            [sys.executable, '-c', 'import time; time.sleep(5)'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        started = __import__('time').monotonic()
        try:
            with self.assertRaises(od.NoProgressExpired):
                od._communicate_with_no_progress_watch(
                    proc, b'', total_seconds=2, idle_seconds=0.15)
        finally:
            proc.kill()
            proc.communicate(timeout=2)
        self.assertLess(__import__('time').monotonic() - started, 1.5)

    def test_build_argv_shape_and_model_hard_gate(self):
        argv = od.build_argv('C:/tools/opencode.exe', DEEPSEEK)
        self.assertEqual(argv, ['C:/tools/opencode.exe', 'run', '--pure', '--model',
                                f'opencode/{DEEPSEEK}', '--format', 'json'])
        self.assertEqual(od.build_argv('x', GLM)[4], f'opencode/{GLM}')
        for bad in (dp.CODEBUDDY_MODEL, dp.CODEBUDDY_GLM_MODEL,
                    'deepseek-v4.1-flash-free ', 'opencode/go', 'gpt-5', ''):
            with self.assertRaises(ValueError, msg=repr(bad)):
                od.build_argv('x', bad)

    def test_default_timeout_is_bounded(self):
        self.assertGreater(od.DEFAULT_TIMEOUT_SECONDS, 0)
        self.assertLessEqual(od.DEFAULT_TIMEOUT_SECONDS, 3600)
        self.assertEqual(od.DEFAULT_NO_PROGRESS_TIMEOUT_SECONDS, 300)

    def test_example_config_is_loadable_and_minimal(self):
        cfg = od.load_entry_config(str(od.EXAMPLE_CONFIG))
        self.assertIsInstance(cfg, dict)
        self.assertEqual(cfg.get('executable'), 'opencode')
        self.assertEqual(cfg.get('allowed_tools'), [])
        content = od.build_opencode_config(cfg)
        self.assertEqual(content['share'], 'disabled')
        self.assertEqual(content['mcp'], {})
        self.assertEqual(content['plugin'], [])

    def test_load_entry_config_validation(self):
        # 显式路径不存在 → 抛错；未显式指定且默认文件缺失 → 空配置（PATH 发现）。
        missing = SCRIPTS / 'no-such-opencode-entry.json'
        self.assertFalse(missing.is_file())
        with self.assertRaises(ValueError):
            od.load_entry_config(str(missing))
        d = Path(tempfile.mkdtemp())
        p = d / 'entry.json'
        p.write_bytes(json.dumps({'allowed_tools': ['Agent', 'task']}).encode('utf-8'))
        with self.assertRaises(ValueError):
            od.load_entry_config(str(p))
        p.write_bytes(json.dumps({'allowed_tools': [' bash ']}).encode('utf-8'))
        with self.assertRaises(ValueError):
            od.load_entry_config(str(p))
        p.write_bytes(b'[')
        with self.assertRaises(ValueError):
            od.load_entry_config(str(p))
        p.write_bytes(json.dumps(['not', 'an', 'object']).encode('utf-8'))
        with self.assertRaises(ValueError):
            od.load_entry_config(str(p))

    def test_resolve_executable_paths(self):
        with self.assertRaises(ValueError):
            od.resolve_executable({'executable': 'C:/definitely/not/here/opencode.exe'})
        with self.assertRaises(ValueError):
            od.resolve_executable({'executable': 'opencode-not-on-this-path-xyz'},
                                  environ={'PATH': ''})
        self.assertEqual(od.resolve_executable({'executable': sys.executable}),
                         str(Path(sys.executable)))

    def test_permission_profile_default_and_forced_deny(self):
        content = od.build_opencode_config({})
        self.assertEqual(content['permission'],
                         {'*': 'deny', 'read': 'allow', 'edit': 'allow',
                          'external_directory': 'deny'})
        # 显式追加已授权工具可以，但 external_directory 永远钉死 deny，
        # agent/task/subagent 一律拒绝。
        content2 = od.build_opencode_config({'allowed_tools': ['bash', 'webfetch']})
        self.assertEqual(content2['permission']['bash'], 'allow')
        self.assertEqual(content2['permission']['webfetch'], 'allow')
        content3 = od.build_opencode_config({'allowed_tools': ['external_directory']})
        self.assertEqual(content3['permission']['external_directory'], 'deny')
        for bad in ('agent', 'Task', 'subagent'):
            with self.assertRaises(ValueError, msg=bad):
                od.build_opencode_config({'allowed_tools': [bad]})

    def test_private_dirs_created_once_under_outdir(self):
        d = Path(tempfile.mkdtemp())
        dirs = od.build_private_dirs(d)
        self.assertEqual(set(dirs),
                         {'OPENCODE_CONFIG_DIR', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME'})
        for v in dirs.values():
            p = Path(v)
            self.assertTrue(p.is_dir() and not any(p.iterdir()))
            self.assertTrue(str(p).startswith(str(d)))
        # 绝不复用既有目录（第二次同基址直接拒绝）。
        with self.assertRaises(FileExistsError):
            od.build_private_dirs(d)

    def test_subprocess_env_isolation_no_credentials(self):
        env = od.build_subprocess_env({}, {'XDG_CONFIG_HOME': 'C:/run/.xdg'},
                                      environ={'PATH': 'C:/x', 'SOME': 'v'})
        content = json.loads(env['OPENCODE_CONFIG_CONTENT'])
        self.assertEqual(content['permission']['external_directory'], 'deny')
        self.assertEqual(env['XDG_CONFIG_HOME'], 'C:/run/.xdg')
        self.assertEqual(env['OPENCODE_DISABLE_AUTOUPDATE'], '1')
        self.assertEqual(env['OPENCODE_DISABLE_CLAUDE_CODE'], '1')
        self.assertEqual(env['OPENCODE_DISABLE_LSP_DOWNLOAD'], '1')
        self.assertEqual(env['SOME'], 'v')

    def test_build_subprocess_env_never_inherits_host_global_dirs(self):
        # 私有目录未提供 → 绝不继承宿主全局 XDG/CONFIG 目录（真实隔离，不只是覆盖）。
        env = od.build_subprocess_env(
            {}, {'XDG_CONFIG_HOME': 'C:/run/.xdg'},
            environ={'PATH': 'C:/x', 'OPENCODE_CONFIG_DIR': 'C:/Users/.opencode',
                     'XDG_DATA_HOME': 'C:/Users/.local/share'})
        self.assertEqual(env['XDG_CONFIG_HOME'], 'C:/run/.xdg')
        self.assertNotIn('OPENCODE_CONFIG_DIR', env)
        self.assertNotIn('XDG_DATA_HOME', env)

    def test_wildcard_and_unknown_tools_rejected(self):
        # `*` 通配、未知工具名、以及 agent/task/subagent 的大小写变体一律拒绝。
        for bad in ('*', 'notARealTool', 'Bashx', 'BashExtra'):
            with self.assertRaises(ValueError, msg=bad):
                od.build_opencode_config({'allowed_tools': [bad]})
        for bad in ('*', 'Agent', 'TASK', 'SubAgent'):
            d = Path(tempfile.mkdtemp())
            p = d / 'entry.json'
            p.write_bytes(json.dumps({'allowed_tools': [bad]}).encode('utf-8'))
            with self.assertRaises(ValueError, msg=bad):
                od.load_entry_config(str(p))

    def test_build_actual_reflects_real_tools_not_empty_grants(self):
        # 整工具 profile 的 actual grants 如实反映 read/edit(+显式 bash)，绝不空 grants
        # 冒充"无权限"；仍是整工具开关，不带逐文件 Edit 硬授权。
        cfg = {'allowed_tools': ['bash']}
        actual = od._build_actual(cfg=cfg, task_id='t', stage=STAGE, model=DEEPSEEK,
                                  workspace='/ws', prompt_sha256='a' * 64,
                                  argv=od.build_argv('x', DEEPSEEK))
        g = actual['grants']
        self.assertNotIn('Edit(/ws/f1)', g['edits'])
        self.assertEqual(g['edits'], [])
        joined = ' '.join(g.get('tool_visibility', []) + g.get('allowed_tools', []))
        for tok in ('read', 'edit', 'bash'):
            self.assertIn(tok, joined.lower(), g)
        # 逐文件硬 Edit 的 plan 仍被正式 preflight 拒绝（opencode 无逐文件表达力）。
        plan = {'task_id': 't', 'stage': STAGE, 'runtime': 'opencode', 'model': DEEPSEEK,
                'workspace': '/ws', 'cwd': '/ws', 'prompt_sha256': 'a' * 64,
                'grants': {'edits': ['Edit(/ws/f1)'], 'bash': [], 'read_dirs': []}}
        pf = ec.preflight(plan, actual, active_tasks=[])
        self.assertFalse(pf.get('ok'), pf)

    # ---- JSON 流解析 ----
    def test_status_code_only_classified_without_message_keyword(self):
        # 官方 data.statusCode 数值单独命中 quota/auth 分类，不依赖正文字样。
        q = od.parse_events(_jsonl([{'type': 'error', 'sessionID': SID, 'error': {
            'name': 'APIError', 'data': {'message': 'Too Many Requests',
                                         'statusCode': 429}}}]))
        self.assertTrue(q['failure_classification']['quota'], q['failure_classification'])
        self.assertFalse(q['protocol_success'])
        a = od.parse_events(_jsonl([{'type': 'error', 'sessionID': SID, 'error': {
            'name': 'APIError', 'data': {'statusCode': 401}}}]))
        self.assertTrue(a['failure_classification']['auth'], a['failure_classification'])

    def test_headers_never_scanned_for_error_classification(self):
        # headers 里的 401/429 字样绝不参与分类（只读结构化错误正文，不读响应头）。
        parsed = od.parse_events(_jsonl([{'type': 'error', 'sessionID': SID, 'error': {
            'name': 'APIError', 'data': {'message': 'request failed',
                                         'headers': {'x-retry': '401 429'}}}}]))
        self.assertFalse(parsed['failure_classification']['auth'],
                         parsed['failure_classification'])
        self.assertFalse(parsed['failure_classification']['quota'],
                         parsed['failure_classification'])

    def test_parse_last_stop_required_tool_calls_midstop_fails(self):
        parsed = od.parse_events(_jsonl([ev_step_start(), ev_step_finish('tool-calls')]))
        self.assertFalse(parsed['protocol_success'])
        self.assertEqual(parsed['last_step_reason'], 'tool-calls')
        parsed2 = od.parse_events(_jsonl([ev_step_finish('tool-calls'),
                                          ev_step_start(), ev_step_finish('stop')]))
        self.assertTrue(parsed2['protocol_success'])

    def test_parse_completed_read_receipt_evidence_no_body_scan(self):
        parsed = od.parse_events(_jsonl([
            ev_tool(output='429 ECONNRESET permission denied quota exceeded: '
                          'literal text inside a normal read output'),
            ev_step_finish('stop')]))
        self.assertEqual(len(parsed['read_receipts']), 1)
        self.assertTrue(parsed['execution_evidence_ok'])
        fc = parsed['failure_classification']
        # 正常 read 正文不做任何关键词扫描：绝不误报。
        self.assertFalse(fc['quota'])
        self.assertFalse(fc['transport'])
        self.assertFalse(fc['permission'])
        self.assertFalse(fc['auth'])
        self.assertTrue(parsed['protocol_success'])

    def test_parse_tool_error_permission_class_only_on_structured(self):
        parsed = od.parse_events(_jsonl([ev_tool(status='error', output=None,
                                                 error='Permission to edit denied'),
                                         ev_step_finish('stop')]))
        self.assertEqual(len(parsed['permission_denials']), 1)
        self.assertFalse(parsed['execution_evidence_ok'])
        self.assertTrue(parsed['failure_classification']['permission'])

    def test_parse_top_error_quota_classification_breaks_protocol(self):
        parsed = od.parse_events(_jsonl([ev_step_start(),
                                         ev_top_error(message='429 Too Many Requests: '
                                                              'insufficient balance')]))
        self.assertFalse(parsed['protocol_success'])
        self.assertEqual(len(parsed['top_level_errors']), 1)
        fc = parsed['failure_classification']
        self.assertTrue(fc['quota'])
        self.assertFalse(fc['model_execution_failure'])

    def test_parse_auth_failure_classification(self):
        parsed = od.parse_events(_jsonl([ev_top_error(
            name='UnknownError',
            message='Custom model authentication failed (401 unauthorized)')]))
        self.assertTrue(parsed['failure_classification']['auth'])
        self.assertFalse(parsed['protocol_success'])

    def test_parse_malformed_line_marks_parse_failure(self):
        parsed = od.parse_events(b'not-json-line\n'
                                 + _jsonl([ev_step_finish('stop')]))
        self.assertEqual(len(parsed['parse_errors']), 1)
        self.assertFalse(parsed['protocol_success'])
        self.assertTrue(parsed['failure_classification']['protocol_parse_failure'])

    def test_parse_missing_tool_receipt_blocks_evidence(self):
        parsed = od.parse_events(_jsonl([ev_tool(status='running'),
                                         ev_step_finish('stop')]))
        self.assertEqual(len(parsed['missing_tool_receipts']), 1)
        self.assertFalse(parsed['execution_evidence_ok'])


class ConfigDiscoveryTests(unittest.TestCase):
    """BW-ZCODE-RESTORE-OPENCODE-REPAIR-20261022：缺省配置发现顺序（fail-closed）。

    仓库副本只有 example、无 OPENCODE_EXE 且 PATH 无 opencode 时，回退本用户已安装
    brain-worker 引擎的 scripts/opencode-entry.json（复用 install_desktop_skill 引擎根
    约定，Path.home() 推导，跨用户、无当前用户名/机器路径硬编码）；显式 --config 缺失
    仍拒绝、绝不回退；全部缺失时如实拒绝。全部离线：引擎回退路径用注入替身，不读真实
    ~/.ccsc 私有配置，不输出其内容。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.repo_cfg = self.base / 'repo-scripts' / 'opencode-entry.json'
        self.repo_cfg.parent.mkdir(parents=True)
        self.engine_cfg = self.base / 'engine-scripts' / 'opencode-entry.json'
        self.engine_cfg.parent.mkdir(parents=True)
        self.engine_calls = []

    def _write_cfg(self, path, exe):
        path.write_bytes(json.dumps({'executable': exe, 'allowed_tools': []})
                         .encode('utf-8'))

    def _patch_discovery(self, *, engine_exists):
        if engine_exists:
            self._write_cfg(self.engine_cfg, str(sys.executable))

        def fake_engine_cfg():
            self.engine_calls.append('engine')
            return self.engine_cfg

        for patcher in (mock.patch.object(od, 'DEFAULT_CONFIG', self.repo_cfg),
                        mock.patch.object(od, 'installed_engine_entry_config',
                                          fake_engine_cfg)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_repo_adjacent_config_wins_and_engine_not_consulted(self):
        # 默认仓库相邻真实配置存在 → 优先；引擎回退路径函数绝不被调用。
        self._write_cfg(self.repo_cfg, str(sys.executable))
        self._patch_discovery(engine_exists=True)
        cfg = od.load_entry_config()
        self.assertEqual(cfg.get('executable'), str(sys.executable))
        self.assertEqual(self.engine_calls, [])

    def test_engine_fallback_used_only_when_default_missing(self):
        # 默认配置缺失 → 只此时才查安装引擎配置；可执行文件存在时解析成功，
        # 不再抛 OpenCode executable not found。
        self._patch_discovery(engine_exists=True)
        cfg = od.load_entry_config()
        self.assertEqual(self.engine_calls, ['engine'])
        self.assertEqual(cfg.get('executable'), str(sys.executable))
        self.assertEqual(od.resolve_executable(cfg, environ={'PATH': ''}),
                         str(Path(sys.executable)))

    def test_explicit_missing_config_never_falls_back(self):
        self._patch_discovery(engine_exists=True)
        with self.assertRaises(ValueError):
            od.load_entry_config(str(self.base / 'missing-entry.json'))
        self.assertEqual(self.engine_calls, [])

    def test_invalid_engine_fallback_refused_not_silently_skipped(self):
        # 回退候选存在但非法 → 抛错，绝不静默降级成空配置冒充 PATH 发现。
        self._patch_discovery(engine_exists=False)
        self.engine_cfg.write_bytes(b'{not-json')
        with self.assertRaises(ValueError):
            od.load_entry_config()

    def test_all_missing_still_refuses_honestly(self):
        self._patch_discovery(engine_exists=False)
        cfg = od.load_entry_config()
        self.assertEqual(cfg, {})
        with self.assertRaisesRegex(ValueError, 'not found'):
            od.resolve_executable(cfg, environ={'PATH': ''})

    def test_opencode_exe_compatibility_retained(self):
        # 无任何配置命中时空配置，executable 仍可来自既有 OPENCODE_EXE 环境变量。
        self._patch_discovery(engine_exists=False)
        cfg = od.load_entry_config()
        self.assertEqual(od.resolve_executable(
            cfg, environ={'PATH': '', 'OPENCODE_EXE': sys.executable}),
            str(Path(sys.executable)))

    def test_engine_config_path_follows_home_no_user_hardcode(self):
        # 回退路径完全由 Path.home() 推导（跨用户默认）；opencode_direct 源码不含
        # 当前用户名或 .ccsc 字面量——引擎根字面量唯一来源是共享的引擎根约定。
        env = {k: v for k, v in os.environ.items() if k != 'BRAIN_WORKER_HOME'}
        fake_home = self.base / 'some-other-user-home'
        with mock.patch.dict(od.os.environ, env, clear=True), \
                mock.patch.object(od.Path, 'home', return_value=fake_home):
            derived = od.installed_engine_entry_config()
        self.assertEqual(derived, fake_home / '.ccsc' / 'codex-mimo' / 'skills'
                         / 'brain-worker' / 'scripts' / 'opencode-entry.json')
        src = Path(od.__file__).read_text(encoding='utf-8')
        self.assertNotIn('Administrator', src)
        self.assertNotIn('.ccsc', src)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.store = str(self.base / 'dispatch.sqlite3')
        self.assertNotEqual(self.store, str(dp.default_store_path()))
        self.ws = self.base / 'ws'
        self.ws.mkdir()
        self.prompt = self.base / 'prompt.txt'
        self.prompt.write_text('执行本轮任务原文', encoding='utf-8')
        self.out = self.base / 'out'
        self.sink = []
        self._p = mock.patch.object(dp, 'process_identity',
                                    return_value={'state': 'alive', 'created': 'c1'})
        self._p.start()
        self.addCleanup(self._p.stop)
        self.addCleanup(self.tmp.cleanup)

    def cfg(self):
        return {'executable': sys.executable, 'allowed_tools': []}

    def sha(self):
        return hashlib.sha256(self.prompt.read_bytes()).hexdigest()

    def claim(self, task='t1', model=DEEPSEEK, ws=None):
        r = dp.reserve(self.store, task_id=task, runtime='opencode', model=model,
                       workspace=str(ws or self.ws), prompt_sha256=self.sha(),
                       stage=STAGE, executor='opencode', now=NOW, _preclaim=False)
        self.assertTrue(r.get('allowed'), r)
        return r['token']

    def active(self, pk):
        return dp.status(self.store, now=NOW)['pools'][pk]['active']

    def run_entry(self, fake, *, task='t1', model=DEEPSEEK, claim_token='AUTO',
                  cfg=None, **over):
        token = claim_token
        if token == 'AUTO':
            token = self.claim(task=task, model=model)
        kwargs = dict(workspace=str(self.ws), prompt_file=self.prompt,
                      out_dir=self.out, stage=STAGE, task_id=task, model=model,
                      prompt_sha256=self.sha(), claim_token=token, now=NOW,
                      popen_factory=fake, environ={'PATH': 'C:/x'},
                      dispatch_store=self.store,
                      child_prober=lambda pid: {'state': 'dead', 'created': 'c1'})
        kwargs.update(over)
        return od.run(cfg if cfg is not None else self.cfg(), **kwargs)

    # ---- 成功全链路 ----
    def test_success_full_lifecycle_releases_own_slot(self):
        body = _report_body(workspace=str(self.ws))
        stream = success_stream(body=body, workspace=str(self.ws))
        fake = FakePopen(stream, sink=self.sink)
        out = self.run_entry(fake)
        self.assertTrue(out['sent'], out)
        self.assertTrue(out['dispatched'] and out['started'])
        self.assertTrue(out['child_verified'])
        self.assertTrue(out['protocol_success'], out)
        self.assertTrue(out['report_bound'], out)
        self.assertTrue(out['execution_evidence_ok'])
        self.assertTrue(out['passed'], out)
        self.assertFalse(out['business_verified'])
        self.assertFalse(out['model_backend_identity_verified'])
        self.assertEqual(out['requested_model'], DEEPSEEK)
        self.assertEqual(out['pool_key'], DPOOL)
        self.assertEqual(out['exit_code'], 0)
        self.assertTrue(out['capacity_release'].get('released'), out['capacity_release'])
        self.assertEqual(self.active(DPOOL), 0)
        # argv / cwd / 单次 CLI 调用
        self.assertEqual(fake.argv, [sys.executable, 'run', '--pure', '--model',
                                     f'opencode/{DEEPSEEK}', '--format', 'json'])
        self.assertEqual(fake.cwd, str(self.ws))
        self.assertEqual(len(self.sink), 1)
        # stdin：完整九节契约（含本轮身份）+ 任务原文，不裁剪
        sent = fake.stdin_data
        self.assertIn(pc.REPORT_START.encode(), sent)
        self.assertIn(pc.REPORT_END.encode(), sent)
        self.assertIn('阶段编号与执行方式'.encode(), sent)
        self.assertIn(f'runtime=opencode；请求模型={DEEPSEEK}'.encode(), sent)
        self.assertIn('执行本轮任务原文'.encode(), sent)
        # env 隔离
        content = json.loads(fake.env['OPENCODE_CONFIG_CONTENT'])
        self.assertEqual(content['share'], 'disabled')
        self.assertEqual(content['permission'],
                         {'*': 'deny', 'read': 'allow', 'edit': 'allow',
                          'external_directory': 'deny'})
        self.assertEqual(content['mcp'], {})
        self.assertEqual(content['plugin'], [])
        for key in ('OPENCODE_CONFIG_DIR', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME'):
            self.assertIn(str(self.out), fake.env[key])
            self.assertTrue(Path(fake.env[key]).is_dir(), key)
        self.assertEqual(fake.env['OPENCODE_DISABLE_AUTOUPDATE'], '1')
        self.assertEqual(fake.env['OPENCODE_DISABLE_CLAUDE_CODE'], '1')
        self.assertEqual(fake.env['OPENCODE_DISABLE_LSP_DOWNLOAD'], '1')
        # 证据文件齐全且逐字保存
        ev = out['evidence']
        for key in ('stdout_jsonl', 'stderr_log', 'events_jsonl', 'sent_payload_stdin',
                    'response_md', 'report_state', 'summary_json', 'request_json',
                    'process_json'):
            self.assertTrue(Path(ev[key]).is_file(), key)
        self.assertEqual(Path(ev['stdout_jsonl']).read_bytes(), stream)
        self.assertEqual(Path(ev['sent_payload_stdin']).read_bytes(), sent)
        self.assertEqual(Path(ev['response_md']).read_text(encoding='utf-8'), body)
        summary = json.loads(Path(ev['summary_json']).read_bytes().decode('utf-8'))
        self.assertFalse(summary['business_verified'])
        self.assertFalse(summary['free_quota_verified'])
        self.assertFalse(summary['model_backend_identity_verified'])
        self.assertEqual(summary['requested_model'], DEEPSEEK)
        self.assertEqual(summary['identity_source'],
                         'entry runtime + requested model; not a verified '
                         'backend/account identity')
        self.assertFalse(summary['timed_out'])
        self.assertTrue(summary['protocol_success'])
        self.assertTrue(summary['report_bound'])
        self.assertEqual(summary['execution_evidence']['scope'],
                         'tool receipts only; not business acceptance and not '
                         'independent file-system truth')
        request = json.loads(Path(ev['request_json']).read_bytes().decode('utf-8'))
        self.assertFalse(request['shell'])
        self.assertTrue(request['stdin_payload'])
        self.assertTrue(request['no_paid_fallback'])
        self.assertTrue(request['one_cli_invocation_per_run'])
        self.assertEqual(request['prompt_sha256'], self.sha())
        self.assertEqual(request['sent_payload_sha256'],
                         hashlib.sha256(sent).hexdigest())
        self.assertEqual(request['timeout_seconds'], od.DEFAULT_TIMEOUT_SECONDS)

    def test_glm_model_claims_only_its_own_pool(self):
        tok = self.claim(model=GLM)
        fake = FakePopen(success_stream(workspace=str(self.ws)), sink=self.sink)
        out = self.run_entry(fake, model=GLM, claim_token=tok)
        self.assertEqual(out['pool_key'], GPOOL)
        self.assertTrue(out['passed'], out)
        self.assertIn(f'opencode/{GLM}', fake.argv)
        self.assertEqual(self.active(GPOOL), 0)
        self.assertEqual(self.active(DPOOL), 0)

    def test_auto_claim_without_token_scoped_to_opencode_no_window_gate(self):
        # 北京时间白天（Max 时段门关闭处）：executor='opencode' 的自动认领照常落到
        # 本免费池——无时段门；结束后真实释放。
        fake = FakePopen(success_stream(workspace=str(self.ws)), sink=self.sink)
        out = self.run_entry(fake, claim_token=None)
        self.assertTrue(out['sent'], out)
        self.assertEqual(out['pool_key'], DPOOL)
        self.assertTrue(out['capacity_release'].get('released'), out['capacity_release'])
        self.assertEqual(self.active(DPOOL), 0)

    # ---- 前置拒绝：零副作用、零 Popen ----
    def test_non_free_model_refused_before_any_side_effect(self):
        for bad in (dp.CODEBUDDY_MODEL, 'gpt-5', 'opencode/go'):
            with self.subTest(bad=bad):
                fake = FakePopen(b'', sink=self.sink)
                out = self.run_entry(fake, model=bad, claim_token=None)
                self.assertTrue(out['dispatch_failed'])
                self.assertEqual(out['reason'], 'cli_model_rejected')
                self.assertFalse(out['sent'])
                self.assertEqual(self.sink, [])
                self.assertFalse(self.out.exists())
                self.assertEqual(self.active(DPOOL), 0)

    def test_timeout_bounds_refused_zero_side_effect(self):
        for bad in (0, -5):
            fake = FakePopen(b'', sink=self.sink)
            out = self.run_entry(fake, timeout_seconds=bad, claim_token=None)
            self.assertEqual(out['reason'], 'timeout_out_of_bounds')
            self.assertEqual(self.sink, [])
            self.assertFalse(self.out.exists())

    def test_existing_output_dir_refused_zero_popen(self):
        self.out.mkdir()
        fake = FakePopen(b'', sink=self.sink)
        out = self.run_entry(fake, claim_token=None)
        self.assertEqual(out['reason'], 'output_dir_exists')
        self.assertEqual(self.sink, [])
        self.assertEqual(self.active(DPOOL), 0)

    def test_prompt_sha_mismatch_precheck_before_claim(self):
        fake = FakePopen(b'', sink=self.sink)
        out = self.run_entry(fake, prompt_sha256='f' * 64, claim_token=None)
        self.assertEqual(out['reason'], 'precheck_failed')
        self.assertEqual(self.sink, [])
        with closing(dp.connect(self.store)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) n FROM attempts').fetchone()['n'], 0)

    def test_forbidden_tool_config_refused_before_claim(self):
        fake = FakePopen(b'', sink=self.sink)
        out = self.run_entry(fake, claim_token=None,
                             cfg={'executable': sys.executable,
                                  'allowed_tools': ['agent']})
        self.assertEqual(out['reason'], 'precheck_failed')
        self.assertEqual(self.sink, [])
        with closing(dp.connect(self.store)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) n FROM attempts').fetchone()['n'], 0)

    def test_dispatch_plan_rejected_zero_popen(self):
        plan = {'task_id': 't1', 'stage': STAGE, 'runtime': 'opencode',
                'model': DEEPSEEK, 'workspace': str(self.ws), 'cwd': str(self.ws),
                'prompt_sha256': 'a' * 64,
                'grants': {'edits': [], 'bash': [], 'read_dirs': []}}
        fake = FakePopen(b'', sink=self.sink)
        out = self.run_entry(fake, plan=plan, claim_token=None)
        self.assertTrue(out['dispatch_failed'])
        self.assertIn(out['reason'], ('dispatch_plan_rejected', 'dispatch_plan_invalid'))
        self.assertEqual(self.sink, [])
        with closing(dp.connect(self.store)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) n FROM attempts').fetchone()['n'], 0)

    def test_pool_full_refused_zero_popen(self):
        # 两个 OpenCode 槽各被别的 task 真实占满（不同 workspace）→ 本 task 拒绝、0 Popen。
        ws2, ws3 = self.base / 'ws2', self.base / 'ws3'
        ws2.mkdir(); ws3.mkdir()
        for t, m, w in (('o1', DEEPSEEK, ws2), ('o2', GLM, ws3)):
            r = dp.reserve(self.store, task_id=t, runtime='opencode', model=m,
                           workspace=str(w), prompt_sha256='p', stage='s',
                           executor='opencode', now=NOW, _preclaim=False)
            self.assertTrue(r.get('allowed'), r)
        fake = FakePopen(b'', sink=self.sink)
        out = self.run_entry(fake, claim_token=None)
        self.assertTrue(out['dispatch_failed'])
        self.assertFalse(out.get('sent'))
        self.assertEqual(self.sink, [])
        self.assertEqual(self.active(DPOOL), 1)
        self.assertEqual(self.active(GPOOL), 1)

    def test_same_workspace_single_writer_guard(self):
        # 别的 task 已在本 workspace 在途 → 即使我有合法 claim token，也在 Popen 前拒绝。
        tok = self.claim()
        norm_ws = os.path.normcase(os.path.realpath(str(self.ws)))
        with closing(dp.connect(self.store)) as conn:
            conn.execute('INSERT INTO attempts(token, task_id, runtime, model, pool_key, '
                         "state, workspace, reserved_at_utc) VALUES(?,?,?,?,?,?,?,?)",
                         ('other-holder', 'other-task', 'codebuddy',
                          dp.CODEBUDDY_MODEL, dp.CODEBUDDY_POOL_KEY, 'running',
                          norm_ws, dp._iso(NOW)))
        fake = FakePopen(b'', sink=self.sink)
        out = self.run_entry(fake, claim_token=tok)
        self.assertTrue(out['dispatch_failed'])
        self.assertFalse(out.get('sent'))
        self.assertEqual(self.sink, [])

    # ---- 启动后生命周期 ----
    def test_bind_failure_keeps_unknown_never_writes_stdin(self):
        fake = FakePopen(success_stream(workspace=str(self.ws)), sink=self.sink)
        with mock.patch.object(dp, 'bind_child', return_value={'bound': False}):
            out = self.run_entry(fake)
        self.assertTrue(out.get('launch_unknown'), out)
        self.assertFalse(out['sent'])
        self.assertFalse(out['child_verified'])
        self.assertIsNone(fake.stdin_data)  # 绑定未成功绝不写 stdin
        self.assertEqual(self.active(DPOOL), 1)  # unknown 继续占容量

    def test_communicate_crash_after_start_keeps_unknown(self):
        fake = FakePopen(b'', raise_comm=RuntimeError('interrupted'), sink=self.sink)
        out = self.run_entry(fake)
        self.assertTrue(out.get('launch_unknown'), out)
        self.assertFalse(out['sent'])
        self.assertEqual(self.active(DPOOL), 1)

    def test_popen_init_failure_releases_start_failed(self):
        fake = FakePopen(b'', raise_init=True, sink=self.sink)
        out = self.run_entry(fake)
        self.assertFalse(out['started'])
        self.assertFalse(out['dispatched'])
        self.assertTrue(out['capacity_release'].get('released'), out['capacity_release'])
        self.assertEqual(self.active(DPOOL), 0)

    def test_timeout_kills_then_confirms_exit_and_releases(self):
        fake = FakePopen(b'', timeout=True, sink=self.sink)
        out = self.run_entry(fake, timeout_seconds=1)
        self.assertTrue(fake.killed)
        self.assertTrue(out['timed_out'])
        self.assertFalse(out['protocol_success'])
        self.assertFalse(out['passed'])
        self.assertEqual(out['exit_code'], -9)
        self.assertTrue(out['capacity_release'].get('released'), out['capacity_release'])
        self.assertEqual(self.active(DPOOL), 0)
        summary = json.loads(Path(out['evidence']['summary_json']).read_bytes()
                             .decode('utf-8'))
        self.assertTrue(summary['timed_out'])

    def test_abnormal_exit_releases_with_nonzero_code(self):
        fake = FakePopen(success_stream(workspace=str(self.ws)), returncode=1,
                         sink=self.sink)
        out = self.run_entry(fake)
        self.assertEqual(out['exit_code'], 1)
        self.assertFalse(out['protocol_success'])
        self.assertFalse(out['passed'])
        self.assertTrue(out['capacity_release'].get('released'))
        self.assertEqual(self.active(DPOOL), 0)

    # ---- JSON 流语义在真实链路上的表现 ----
    def test_mid_stop_tool_calls_last_is_failure_but_releases(self):
        stream = _jsonl([ev_step_start(), ev_tool(), ev_step_finish('tool-calls')])
        out = self.run_entry(FakePopen(stream, sink=self.sink))
        self.assertFalse(out['protocol_success'])
        self.assertFalse(out['passed'])
        self.assertTrue(out['capacity_release'].get('released'))

    def test_missing_session_id_never_binds(self):
        events = strip_sid([ev_step_start(), ev_tool(), ev_step_finish('tool-calls'),
                            ev_step_start(),
                            ev_text(_report_body(workspace=str(self.ws))),
                            ev_step_finish('stop')])
        out = self.run_entry(FakePopen(_jsonl(events), sink=self.sink))
        self.assertFalse(out['report_bound'])
        self.assertFalse(out['passed'])
        self.assertTrue(out['protocol_success'])  # 协议层可成功，绑定独立失败

    def test_zero_tool_run_binds_report_but_not_engineering_success(self):
        stream = _jsonl([ev_step_start(),
                         ev_text(_report_body(workspace=str(self.ws))),
                         ev_step_finish('stop')])
        out = self.run_entry(FakePopen(stream, sink=self.sink))
        self.assertTrue(out['report_bound'])
        self.assertFalse(out['execution_evidence_ok'])
        self.assertFalse(out['passed'])

    def test_tool_error_receipt_blocks_engineering_success(self):
        stream = _jsonl([ev_tool(status='error', output=None,
                                 error='tool execution failed'),
                         ev_text(_report_body(workspace=str(self.ws))),
                         ev_step_finish('stop')])
        out = self.run_entry(FakePopen(stream, sink=self.sink))
        self.assertFalse(out['execution_evidence_ok'])
        self.assertFalse(out['passed'])

    def test_permission_denied_receipt_classified(self):
        stream = _jsonl([ev_tool(status='error', output=None,
                                 error='Permission to use Edit has been denied'),
                         ev_text(_report_body(workspace=str(self.ws))),
                         ev_step_finish('stop')])
        out = self.run_entry(FakePopen(stream, sink=self.sink))
        self.assertFalse(out['passed'])
        self.assertTrue(out['failure_classification']['permission'])

    def test_top_level_error_429_classified_quota_not_model_blame(self):
        stream = _jsonl([ev_step_start(),
                         ev_top_error(message='429 rate limit reached'),
                         ev_step_finish('stop')])
        out = self.run_entry(FakePopen(stream, sink=self.sink))
        self.assertFalse(out['protocol_success'])
        self.assertFalse(out['passed'])
        fc = out['failure_classification']
        self.assertTrue(fc['quota'])
        self.assertFalse(fc['model_execution_failure'])
        self.assertTrue(out['capacity_release'].get('released'))

    def test_transport_error_classified_from_structured_event(self):
        stream = _jsonl([ev_top_error(name='ProviderError',
                                      message='ECONNRESET while streaming')])
        out = self.run_entry(FakePopen(stream, sink=self.sink))
        self.assertFalse(out['protocol_success'])
        fc = out['failure_classification']
        self.assertTrue(fc['transport'])
        self.assertFalse(fc['model_execution_failure'])

    def test_malformed_line_is_parse_failure(self):
        stream = b'garbage-not-json\n' + _jsonl([ev_step_finish('stop')])
        out = self.run_entry(FakePopen(stream, sink=self.sink))
        self.assertFalse(out['protocol_success'])
        self.assertFalse(out['passed'])
        self.assertTrue(out['capacity_release'].get('released'))

    def test_non_nine_section_response_not_bound_no_fake_report(self):
        stream = _jsonl([ev_tool(), ev_text('plain prose, no contract'),
                         ev_step_finish('stop')])
        out = self.run_entry(FakePopen(stream, sink=self.sink))
        self.assertFalse(out['report_bound'])
        self.assertFalse(out['passed'])
        # 绝不生成假九节：response.md 保存的就是原始文本
        self.assertEqual(Path(out['evidence']['response_md']).read_text(encoding='utf-8'),
                         'plain prose, no contract')

    def test_final_report_is_last_text_part_intermediates_preserved(self):
        body = _report_body(workspace=str(self.ws))
        stream = _jsonl([ev_tool(), ev_text('中间思考文字'),
                         ev_step_finish('tool-calls'),
                         ev_text(body), ev_step_finish('stop')])
        out = self.run_entry(FakePopen(stream, sink=self.sink))
        self.assertTrue(out['report_bound'], out)
        summary = json.loads(Path(out['evidence']['summary_json']).read_bytes()
                             .decode('utf-8'))
        self.assertEqual(summary['intermediate_text_parts'], ['中间思考文字'])


class MainCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.prompt = self.base / 'p.txt'
        self.prompt.write_text('t', encoding='utf-8')
        self.ws = self.base / 'ws'
        self.ws.mkdir()

    def _args(self, model=DEEPSEEK, extra=()):
        return ['--workspace', str(self.ws), '--prompt-file', str(self.prompt),
                '--output-dir', str(self.base / 'out'), '--stage', STAGE,
                '--task-id', 't1', '--model', model, *extra]

    def test_bad_model_exit2_zero_popen(self):
        with mock.patch.object(od.subprocess, 'Popen',
                               side_effect=AssertionError('Popen must not run')) as m:
            rc = od.main(self._args(model=dp.CODEBUDDY_MODEL))
        self.assertEqual(rc, 2)
        m.assert_not_called()
        self.assertFalse((self.base / 'out').exists())

    def test_bad_timeout_exit2_zero_popen(self):
        with mock.patch.object(od.subprocess, 'Popen',
                               side_effect=AssertionError('Popen must not run')) as m:
            rc = od.main(self._args(extra=['--timeout-seconds', '0']))
        self.assertEqual(rc, 2)
        m.assert_not_called()

    def test_invalid_config_exit2_zero_popen(self):
        with mock.patch.object(od.subprocess, 'Popen',
                               side_effect=AssertionError('Popen must not run')) as m:
            rc = od.main(self._args(extra=['--config',
                                           str(self.base / 'missing-entry.json')]))
        self.assertEqual(rc, 2)
        m.assert_not_called()

    def test_refused_dispatch_maps_exit2(self):
        # 未派发（前置拒绝）→ 主脑语义 exit 2、0 Popen；outdir 不建立。
        with mock.patch.object(od.subprocess, 'Popen',
                               side_effect=AssertionError('Popen must not run')) as m:
            rc = od.main(self._args(model='glm-5.3-flash', extra=[
                '--dispatch-store', str(self.base / 'unused-store.sqlite3')]))
        self.assertEqual(rc, 2)
        m.assert_not_called()
        self.assertFalse((self.base / 'unused-store.sqlite3').exists())


if __name__ == '__main__':
    unittest.main()
