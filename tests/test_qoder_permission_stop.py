#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BW-CN-PERMISSION-STOP-20261015T155930Z-S1 CN 权限派工门与会话拒绝监视离线测试。

覆盖（纯标准库 unittest，零真实 CLI/网络/凭据；每用例独立临时目录与临时 SQLite）：
1. 静态规则门：tools-only 兼容回退或显式裸/整工具通配 Edit/Write/NotebookEdit/Bash
   allow 规则 → 正式 main 受控未派发（rc=2、sent=false、0 Popen、0 attempt、零证据目录）；
   有效 `Edit(/nested/created.txt)` + Write 仅作 --tools 可见性照常到达 Popen；国际
   build_argv/main 行为一字不变（无 --session-id、无 cn_session_watch、裸 Write 不拒）。
2. 会话监视绑定：只认 sessionId 精确一致 + cwd 真实路径归一一致（Windows 8.3 短名与长名
   同一工作区）+ type=user 的真实 message.content tool_result；**事件顶层或块内**的结构化
   toolDenialKind=permission-rule、或 tool_result 直接结果固定文案（现场真实 dont_ask 文案 +
   既有 codebuddy 文案）命中；固定文案**只在结果起始处**（允许 `Error:` 前缀）直接构成拒绝时
   命中，输出中间引用同一句（如 "echoed diagnostic: Permission to use Write has been denied …"）
   绝不误停；is_error 真/假不掩盖；普通工具错误/402/429/取消不是审批拒绝；
   用户/助手叙述不扫描；resume 旧事件不触发（启动前冻结 EOF 基线）；会话文件可能在 Popen 后
   才出现（从 0 读、不吞新增事件）；重复候选/IO 异常明确降级不任意挑一个；分段 JSON 行缓冲；
   坏行只记事实不断监视。`line_number` 是文件绝对行号，跳过历史只计位不冒充已解析。
3. 终止语义：假 child 走正式 main——拒绝后经本 child 对象 terminate、真实退出确认后释放
   本 attempt；protocol_success=false、report_bound=false、permission_stop/工具ID/真实
   exit 保留、不落 quota 标记；自然退出后才发现拒绝 → observed_after_exit 分类、不伪称
   已终止；终态无法确认 → mark_unknown 保留占位、证据先落盘。**CN 所有已知终态都随 summary
   存本轮 `cn_watch_facts`**（是否找到/观察到会话、位置、degraded、错误、读取进度）——无拒绝
   的正常结束也必须能回读监控状态，国际路径不新增字段。
4. 本地无网络合成子进程（真实 OS 进程）：把原生会话事件写进指定 native session 文件、
   延迟写 sentinel——观察到拒绝即终止、后续 sentinel 未执行；普通工具错误照常继续到自然
   退出。不是只测正则：监视循环、terminate、等待退出、容量释放全部真实发生。

既有 CN 入口测试（test_qoder_cn_direct_offline.py）的假 Popen 已补 poll/wait/terminate/
kill 表面（任务书允许的必要 fixture 调整），生产监视路径不在测试里绕开。
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
_SCRIPTS = HERE.parent / 'scripts'
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import qoder_permission_watch as qpw  # noqa: E402 与 qd 共享同一模块实例


def _load_qd():
    spec = importlib.util.spec_from_file_location(
        'qoder_direct_perm_stop_under_test', _SCRIPTS / 'qoder_direct.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


qd = _load_qd()

_SID = '11111111-2222-3333-4444-555555555555'
# 现场真实 CN dont_ask 拒绝文案（本次事件原文，字段层级与原件一致）。
_REAL_DENIED_TEXT = ('Error: This action needs approval, and the "Don\'t ask" permission '
                     'mode does not prompt. Switch modes to approve it.')
# 既有 codebuddy-direct 档案记录的同系 CLI 固定文案（仍保留支持）。
_LEGACY_DENIED_TEXT = 'Permission to use Write has been denied under dont_ask permission mode.'
# 现场布局：config_dir/projects/<workspace-key>/<sessionId>.jsonl（不是 projects/<sid>.jsonl）。
_WORKSPACE_KEY = 'synthetic-workspace-key'


def _session_file(config_dir, session_id, workspace_key=_WORKSPACE_KEY):
    return qpw.session_jsonl_path(config_dir, session_id, workspace_key)


def _event(blocks, *, session_id=_SID, cwd='/abs/ws', etype='user', content=None, **root):
    if content is None:
        content = blocks
    obj = {'type': etype, 'sessionId': session_id, 'cwd': cwd,
           'message': {'role': 'user' if etype == 'user' else 'assistant',
                       'content': content}}
    obj.update(root)
    return obj


def _denial_event(*, kind='permission-rule', level='root', session_id=_SID, cwd='/abs/ws',
                  is_error=True, text=_REAL_DENIED_TEXT):
    """现场形状：`toolDenialKind` 在**事件顶层**，tool_result 直接结果文案为真实 dont_ask
    文案且 `is_error=true`。level='block'/'nested' 仅用于兼容层级回归。"""
    block = {'type': 'tool_result', 'tool_use_id': 'toolu_stop01',
             'content': text, 'is_error': is_error}
    root = {}
    if kind == 'permission-rule':
        if level == 'root':
            root['toolDenialKind'] = 'permission-rule'
        elif level == 'block':
            block['toolDenialKind'] = 'permission-rule'
        elif level == 'nested':
            block['content'] = {'text': text, 'toolDenialKind': 'permission-rule'}
    return _event([block], session_id=session_id, cwd=cwd, **root)


def _tool_result_event(text, *, is_error=True, session_id=_SID, cwd='/abs/ws'):
    return _event([{'type': 'tool_result', 'tool_use_id': 'toolu_err01',
                    'content': text, 'is_error': is_error}],
                  session_id=session_id, cwd=cwd)


# ---------------------------------------------------------------------------
# 1. 静态规则门（纯函数 + 正式 main 的 0 Popen 拒绝）
# ---------------------------------------------------------------------------
class CnBareMutationRuleGateTests(unittest.TestCase):
    CFG = {'cli': '/abs/qodercn.exe', 'config_dir': '/abs/cfg',
           'model_ids': {'DeepSeek-Flash': 'real-id-123'}}

    def _build(self, **kw):
        sid = kw.pop('session_id', None)
        return qd.build_cn_argv(self.CFG, '/abs/cfg', 'real-id-123', '/abs/ws',
                               kw.pop('tools', 'Read'), sid, **kw)

    @staticmethod
    def _rules(argv):
        return [argv[i + 1] for i, a in enumerate(argv) if a == '--allowed-tools']

    def test_detector_flags_bare_and_wildcard_mutation_only(self):
        flagged = ['Edit', 'Write', 'NotebookEdit', 'Bash',
                   'Edit(*)', 'Write(*)', 'NotebookEdit(*)', 'Bash(*)']
        self.assertEqual(qd.cn_bare_mutation_allow_rules(flagged), flagged)
        for rule in ('Read', 'Glob', 'Grep', 'WebFetch', 'Read(*)',
                     'Edit(/nested/created.txt)', 'Edit(/SKILL.md)',
                     'Bash(python -m unittest discover -s tests)', 'Bash(npm run test:*)',
                     'write', 'edit'):
            with self.subTest(rule=rule):
                self.assertEqual(qd.cn_bare_mutation_allow_rules([rule]), [])
        self.assertEqual(qd.cn_bare_mutation_allow_rules(None), [])
        self.assertEqual(qd.cn_bare_mutation_allow_rules([42, None]), [])  # 非串交上层严格校验

    def test_build_cn_argv_refuses_each_bare_mutation_rule(self):
        for bad in ('Edit', 'Write', 'NotebookEdit', 'Bash', 'Write(*)', 'Bash(*)'):
            with self.subTest(rule=bad):
                with self.assertRaises(ValueError) as ctx:
                    self._build(tools='Read,Write', allowed_tools=['Read', bad])
                msg = str(ctx.exception)
                self.assertIn(repr(bad), msg)
                self.assertIn('Edit(/<workspace-relative specific file>)', msg)
                self.assertIn('Bash(<registered verbatim command>)', msg)
                self.assertIn('does not claim', msg)  # 静态门不声称真实权限成功

    def test_deny_side_and_readonly_and_empty_allow_stay_legal(self):
        """deny 是收紧不是授权：裸 Write deny 原样保留；只读裸规则、显式空 allow、
        Write 仅在 --tools 可见性都不受门影响。"""
        argv = self._build(tools='Read,Write',
                           allowed_tools=['Read', 'Glob',
                                          'Edit(/nested/created.txt)',
                                          'Bash(python -m unittest discover -s tests)'],
                           disallowed_tools=['Write', 'Edit(/secret.txt)'])
        self.assertEqual(self._rules(argv),
                         ['Read', 'Glob', 'Edit(/nested/created.txt)',
                          'Bash(python -m unittest discover -s tests)'])
        denies = [argv[i + 1] for i, a in enumerate(argv) if a == '--disallowed-tools']
        self.assertEqual(denies, ['Write', 'Edit(/secret.txt)'])
        empty = self._build(tools='Read,Write', allowed_tools=[])
        self.assertEqual(self._rules(empty), [])
        self.assertEqual(empty[empty.index('--tools') + 1], 'Read,Write')
        readonly_fallback = self._build(tools='Read,Glob,Grep')
        self.assertEqual(self._rules(readonly_fallback), ['Read', 'Glob', 'Grep'])

    def test_tools_only_fallback_bare_mutation_refused(self):
        with self.assertRaises(ValueError) as ctx:
            self._build(tools='Read,Glob,Grep,Edit,Write,Bash')
        self.assertIn('Write', str(ctx.exception))
        self.assertIn('Edit', str(ctx.exception))
        self.assertIn('Bash', str(ctx.exception))


class _CnMainBase(unittest.TestCase):
    """正式 main 的 CN 用例基座：临时目录/池/额度库隔离 + 假 Popen 注入。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='bw-perm-stop-')
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._saved_env = {k: os.environ.get(k) for k in
                           ('BRAIN_WORKER_DISPATCH_STORE', 'BRAIN_WORKER_QUOTA_STORE')}
        for k, v in self._saved_env.items():
            if v is None:
                self.addCleanup(os.environ.pop, k, None)
            else:
                self.addCleanup(os.environ.__setitem__, k, v)
        os.environ['BRAIN_WORKER_DISPATCH_STORE'] = str(self.tmp / 'pool.sqlite3')
        os.environ['BRAIN_WORKER_QUOTA_STORE'] = str(self.tmp / 'quota-absent.sqlite3')
        self._real_popen = qd.subprocess.Popen
        self.calls = []
        qd.subprocess.Popen = self._popen
        self.addCleanup(setattr, qd.subprocess, 'Popen', self._real_popen)
        self.store = str(self.tmp / 'pool.sqlite3')

    def _popen(self, argv, *a, **k):
        self.calls.append(argv)
        raise AssertionError('subprocess.Popen must not be called in a refusal path')

    def write_cn_config(self, tag='cn'):
        cli = self.tmp / (tag + '-cli.exe')
        cli.write_text('placeholder-stub', encoding='utf-8')
        cfg_dir = self.tmp / (tag + '-cfgdir')
        (cfg_dir / 'projects').mkdir(parents=True, exist_ok=True)
        cfg = self.tmp / (tag + '-entry.json')
        cfg.write_text(json.dumps({'cli': str(cli), 'config_dir': str(cfg_dir),
                                   'model_ids': {'DeepSeek-Flash': 'real-id-123'}}),
                       encoding='utf-8')
        return cfg, cfg_dir

    def make_case(self, tag):
        ws = self.tmp / ('ws-' + tag)
        ws.mkdir(exist_ok=True)
        pf = self.tmp / ('prompt-' + tag + '.txt')
        pf.write_text('BW-CN-PERMISSION-STOP 离线用例 ' + tag, encoding='utf-8')
        out = self.tmp / ('out-' + tag)
        return ws, pf, out

    def base_argv(self, tag, cfg, ws, pf, out, extra=()):
        return ['--runtime', 'qodercn', '--cn-config', str(cfg),
                '--workspace', str(ws), '--prompt-file', str(pf),
                '--output-dir', str(out), '--stage', 'BW-CN-PERM-STOP-' + tag,
                '--model', 'Qwen3.8-Flash', '--task-id', 'T-' + tag,
                '--dispatch-store', self.store, *extra]

    def run_main(self, argv):
        old = sys.argv
        sys.argv = ['qoder_direct.py', *argv]
        printed = io.StringIO()
        try:
            with redirect_stdout(printed):
                rc = qd.main()
        finally:
            sys.argv = old
        text = printed.getvalue()
        return rc, (json.loads(text) if text.strip() else None)

    def attempts_count(self):
        with closing(qd.dp.connect(self.store)) as conn:
            return conn.execute('SELECT COUNT(*) c FROM attempts').fetchone()['c']

    def attempt_by_task(self, task_id):
        with closing(qd.dp.connect(self.store)) as conn:
            row = conn.execute('SELECT * FROM attempts WHERE task_id=?',
                               (task_id,)).fetchone()
            return dict(row) if row is not None else None


class CnMainBareRuleRefusalTests(_CnMainBase):
    def test_tools_only_bare_mutation_refused_zero_popen(self):
        cfg, _ = self.write_cn_config()
        ws, pf, out = self.make_case('tools-only')
        rc, printed = self.run_main(self.base_argv(
            'tools-only', cfg, ws, pf, out, ['--tools', 'Read,Glob,Grep,Edit,Write,Bash']))
        self.assertEqual(rc, 2, printed)
        self.assertTrue(printed['cn_permission_rule_rejected'], printed)
        self.assertFalse(printed['sent'], printed)
        self.assertEqual(printed['runtime'], qd.RUNTIME_CN)
        joined = ' '.join(printed['reasons'])
        self.assertIn('Edit(/<workspace-relative specific file>)', joined)
        self.assertIn('Bash(<registered verbatim command>)', joined)
        self.assertEqual(self.calls, [], 'refusal must happen before Popen')
        self.assertFalse(out.exists(), 'refusal must not create the evidence dir')
        self.assertEqual(self.attempts_count(), 0,
                         'refusal must not consume a capacity claim')

    def test_explicit_bare_bash_rule_refused_zero_popen(self):
        cfg, _ = self.write_cn_config()
        ws, pf, out = self.make_case('bare-bash')
        rc, printed = self.run_main(self.base_argv(
            'bare-bash', cfg, ws, pf, out, ['--tools', 'Read',
                                            '--allowed-tools', 'Read',
                                            '--allowed-tools', 'Bash']))
        self.assertEqual(rc, 2, printed)
        self.assertTrue(printed['cn_permission_rule_rejected'], printed)
        self.assertIn(repr('Bash'), ' '.join(printed['reasons']))
        self.assertEqual(self.calls, [])
        self.assertFalse(out.exists())
        self.assertEqual(self.attempts_count(), 0)

    def test_valid_edit_rule_with_write_visibility_reaches_popen(self):
        cfg, _ = self.write_cn_config()
        ws, pf, out = self.make_case('edit-ok')
        captured = {}

        def recording_popen(argv, *a, **k):
            captured['argv'] = argv
            raise RuntimeError('sentinel: reached Popen (mutation gate passed)')

        qd.subprocess.Popen = recording_popen
        rc, _printed = self.run_main(self.base_argv(
            'edit-ok', cfg, ws, pf, out,
            ['--tools', 'Read,Write', '--allowed-tools', 'Edit(/nested/created.txt)']))
        self.assertIn('argv', captured, f'gate must not block scoped Edit rules (rc={rc})')
        argv = captured['argv']
        rules = [argv[i + 1] for i, a in enumerate(argv) if a == '--allowed-tools']
        self.assertEqual(rules, ['Edit(/nested/created.txt)'])
        self.assertEqual(argv[argv.index('--tools') + 1], 'Read,Write')
        self.assertIn('--session-id', argv)
        sid = argv[argv.index('--session-id') + 1]
        self.assertTrue(qpw.is_safe_session_id(sid))
        request = json.loads((out / 'request.json').read_text(encoding='utf-8'))
        self.assertEqual(request['cn_session_watch']['session_id'], sid)
        self.assertEqual(request['cn_session_watch']['source'], 'generated')


class InternationalUnchangedTests(unittest.TestCase):
    def test_international_argv_keeps_bare_rules_and_never_session_id(self):
        cfg = {'node': '/abs/node', 'qodercli': '/abs/qodercli.js'}
        argv = qd.build_argv(cfg, '/abs/ws', 'Qwen3.8-Flash', 'Read,Write', None,
                             allowed_tools=['Read', 'Write'])
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv)
                          if a == '--allowed-tools'], ['Read', 'Write'])
        self.assertNotIn('--session-id', argv)
        # 国际侧不做 CN mutation 门：裸 Write/Edit/Bash allow 规则照旧构造。
        argv2 = qd.build_argv(cfg, '/abs/ws', 'Qwen3.8-Flash', '', None,
                              allowed_tools=['Edit', 'Write', 'Bash'])
        self.assertIn('Edit', argv2)

    def test_international_main_has_no_session_watch(self):
        tmp = tempfile.TemporaryDirectory(prefix='bw-perm-stop-intl-')
        self.addCleanup(tmp.cleanup)
        tmp = Path(tmp.name)
        saved = {k: os.environ.get(k) for k in
                 ('BRAIN_WORKER_DISPATCH_STORE', 'BRAIN_WORKER_QUOTA_STORE')}
        os.environ['BRAIN_WORKER_DISPATCH_STORE'] = str(tmp / 'pool.sqlite3')
        os.environ['BRAIN_WORKER_QUOTA_STORE'] = str(tmp / 'quota-absent.sqlite3')
        for k, v in saved.items():
            if v is None:
                self.addCleanup(os.environ.pop, k, None)
            else:
                self.addCleanup(os.environ.__setitem__, k, v)
        node = tmp / 'node'
        node.write_text('stub', encoding='utf-8')
        cli = tmp / 'qodercli.js'
        cli.write_text('stub', encoding='utf-8')
        cfg = tmp / 'entry.json'
        cfg.write_text(json.dumps({'node': str(node), 'qodercli': str(cli)}),
                       encoding='utf-8')
        ws = tmp / 'ws'
        ws.mkdir()
        pf = tmp / 'prompt.txt'
        pf.write_text('BW-CN-PERMISSION-STOP 国际不变用例', encoding='utf-8')
        out = tmp / 'out-intl'
        captured = {}

        class _IntlChild:
            def __init__(self):
                self.pid = 616161
                self.returncode = 0

            def communicate(self, input=None):
                return (None, None)

        real_identity = qd.dp.process_identity

        def _identity(pid, platform=None):
            try:
                if int(pid) == 616161:
                    return {'pid': pid, 'state': 'dead', 'created': None}
            except (TypeError, ValueError):
                pass
            return real_identity(pid, platform)

        def recording_popen(argv, *a, **k):
            captured['argv'] = argv
            return _IntlChild()

        real_popen = qd.subprocess.Popen
        qd.dp.process_identity = _identity
        self.addCleanup(setattr, qd.dp, 'process_identity', real_identity)
        qd.subprocess.Popen = recording_popen
        try:
            old = sys.argv
            sys.argv = ['qoder_direct.py', '--runtime', 'qoder', '--config', str(cfg),
                        '--workspace', str(ws), '--prompt-file', str(pf),
                        '--output-dir', str(out), '--stage', 'BW-CN-PERM-STOP-INTL',
                        '--model', 'Qwen3.8-Flash', '--task-id', 'T-intl',
                        '--dispatch-store', str(tmp / 'pool.sqlite3'),
                        '--tools', 'Read,Write', '--allowed-tools', 'Read',
                        '--allowed-tools', 'Write']
            printed = io.StringIO()
            try:
                with redirect_stdout(printed):
                    rc = qd.main()
            finally:
                sys.argv = old
        finally:
            qd.subprocess.Popen = real_popen
        self.assertEqual(rc, 3, printed.getvalue())  # 空 stdout → 解析失败，但协议路径走完
        self.assertNotIn('--session-id', captured['argv'])
        request = json.loads((out / 'request.json').read_text(encoding='utf-8'))
        self.assertNotIn('cn_session_watch', request)
        self.assertFalse((out / 'permission-stop.json').exists())
        summary = json.loads((out / 'summary.json').read_text(encoding='utf-8'))
        self.assertNotIn('permission_stop', summary)


# ---------------------------------------------------------------------------
# 2. 会话监视绑定（真实文件增量读取；非只测正则）
# ---------------------------------------------------------------------------
class SessionWatcherUnitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='bw-perm-stop-watch-')
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.config_dir = self.tmp / 'cfgdir'
        self.workspace_key = _WORKSPACE_KEY
        # 现场布局：projects/<workspace-key>/（目录先存在，会话文件按需出现）。
        (self.config_dir / 'projects' / self.workspace_key).mkdir(parents=True)
        self.ws = self.tmp / 'ws'
        self.ws.mkdir()
        self.file = _session_file(self.config_dir, _SID, self.workspace_key)

    def append_raw(self, text: str):
        with self.file.open('a', encoding='utf-8') as fh:
            fh.write(text)

    def append_event(self, obj):
        self.append_raw(json.dumps(obj, ensure_ascii=False) + '\n')

    def watcher(self):
        return qpw.SessionDenialWatcher(self.config_dir, _SID, str(self.ws))

    def test_session_file_layout_is_workspace_keyed(self):
        """现场路径是 projects/<workspace-key>/<sessionId>.jsonl；只按精确 UUID 文件名定位，
        同名文件不在 projects 根下，别的 session 文件也不被当成本会话。"""
        self.assertEqual(self.file,
                         self.config_dir / 'projects' / self.workspace_key / (_SID + '.jsonl'))
        (self.config_dir / 'projects' / 'other-ws-key').mkdir()
        other = self.config_dir / 'projects' / 'other-ws-key' / ('other' + '.jsonl')
        other.write_text('{"type":"user"}\n', encoding='utf-8')
        self.append_raw('{"type":"user"}\n')  # 本会话的历史行（不会触发：无顶层标记）
        found, error = qpw.find_session_jsonl(self.config_dir, _SID)
        self.assertIsNone(error)
        self.assertEqual(found, self.file)
        # projects 根下没有裸 <sid>.jsonl：不是现场布局，绝不当作候选。
        self.assertFalse((self.config_dir / 'projects' / (_SID + '.jsonl')).exists())

    def test_file_appearing_after_watcher_start_is_read_from_zero(self):
        """新会话：Popen 后才出现文件；基线必须是 0，绝不吞掉新增事件。"""
        w = self.watcher()
        self.assertFalse(w.facts()['session_observed'])
        self.assertIsNone(w.path)
        self.append_event(_denial_event(cwd=str(self.ws)))
        rec = w.poll()
        self.assertIsNotNone(rec)
        self.assertEqual(rec['line_number'], 1)
        facts = w.facts()
        self.assertTrue(facts['session_observed'])
        self.assertEqual(facts['baseline_bytes'], 0)
        self.assertEqual(facts['lines_seen'], 1)

    def test_duplicate_candidates_degrade_without_arbitrary_pick(self):
        """重复候选明确降级：绝不任意挑一个，也不宣称已观察到会话。"""
        duplicate = self.config_dir / 'projects' / 'second-ws-key' / (_SID + '.jsonl')
        duplicate.parent.mkdir()
        duplicate.write_text('{"type":"user"}\n', encoding='utf-8')
        self.append_event(_denial_event(cwd=str(self.ws)))
        found, error = qpw.find_session_jsonl(self.config_dir, _SID)
        self.assertIsNone(found)
        self.assertIn('ambiguous', error)
        w = self.watcher()
        self.assertIsNone(w.path)
        self.assertIsNone(w.poll())
        facts = w.facts()
        self.assertTrue(facts['degraded'])
        self.assertFalse(facts['session_observed'])
        self.assertFalse(any(e['fact'] == 'stop' for e in facts['events']))

    def test_structured_kind_detected_is_error_false_idempotent(self):
        w = self.watcher()
        self.append_event(_denial_event(kind='permission-rule', is_error=False,
                                        cwd=str(self.ws)))
        first = w.poll()
        self.assertIsNotNone(first)
        self.assertEqual(first['kind'], 'permission-rule')
        self.assertEqual(first['tool_use_id'], 'toolu_stop01')
        self.assertEqual(first['is_error'], False)
        self.assertEqual(first['source'], str(self.file))
        self.assertEqual(w.poll(), first)  # 幂等
        facts = w.facts()
        self.assertEqual(facts['lines_seen'], 1)
        self.assertFalse(facts['degraded'])
        self.assertTrue(any(e['fact'] == 'stop' for e in facts['events']))

    def test_real_root_level_kind_and_real_dont_ask_text(self):
        """本次真实事件形状：toolDenialKind 在事件顶层、is_error=true、真实 dont_ask 文案。"""
        w = self.watcher()
        self.append_event(_denial_event(kind='permission-rule', level='root',
                                        is_error=True, cwd=str(self.ws)))
        rec = w.poll()
        self.assertIsNotNone(rec)
        self.assertEqual(rec['kind'], 'permission-rule')
        self.assertEqual(rec['toolDenialKind'], 'permission-rule')
        self.assertEqual(rec['is_error'], True)
        # 最小原始事件保留顶层拒绝标记，原件不改。
        self.assertEqual(rec['raw_event']['toolDenialKind'], 'permission-rule')
        self.assertEqual(rec['raw_event']['tool_result']['tool_use_id'], 'toolu_stop01')

    def test_block_and_nested_kind_levels_still_supported(self):
        for level in ('block', 'nested'):
            with self.subTest(level=level):
                w = self.watcher()
                self.append_event(_denial_event(level=level, cwd=str(self.ws)))
                rec = w.poll()
                self.assertIsNotNone(rec)
                self.assertEqual(rec['kind'], 'permission-rule')

    def test_real_dont_ask_fixed_text_detected_without_structured_kind(self):
        w = self.watcher()
        self.append_event(_denial_event(kind='text-only', is_error=True,
                                        cwd=str(self.ws)))
        rec = w.poll()
        self.assertEqual(rec['kind'], 'fixed-text')
        self.assertIn('needs approval', rec['text_excerpt'])
        self.assertIn("Don't ask", rec['text_excerpt'])
        self.assertIsNone(rec['toolDenialKind'])

    def test_legacy_permission_to_use_text_still_supported(self):
        w = self.watcher()
        self.append_event(_denial_event(kind='text-only', is_error=False,
                                        cwd=str(self.ws), text=_LEGACY_DENIED_TEXT))
        rec = w.poll()
        self.assertEqual(rec['kind'], 'fixed-text')
        self.assertIn('Permission to use Write', rec['text_excerpt'])

    def test_quoted_fixed_text_mid_stdout_is_not_a_denial(self):
        """本机独立反例：is_error=false 的正常 tool_result 输出里**引用**了固定拒绝句，但该句
        不在结果起始处、不构成这条结果自身的拒绝——绝不误停（起始锚定，不是宽松 substring）。"""
        w = self.watcher()
        self.append_event(_tool_result_event(
            'echoed diagnostic: Permission to use Write has been denied under '
            'dont_ask permission mode. this is test stdout',
            is_error=False, cwd=str(self.ws)))
        self.assertIsNone(w.poll())
        self.assertFalse(any(e['fact'] == 'stop' for e in w.facts()['events']))

    def test_real_dont_ask_verbatim_text_at_start_is_a_denial(self):
        """现场真实 dont_ask 完整原句（带 `Error:` 前缀、位于工具结果起始处）仍必须识别；
        is_error=false 也不掩盖真实拒绝。"""
        w = self.watcher()
        self.append_event(_tool_result_event(_REAL_DENIED_TEXT, is_error=False,
                                             cwd=str(self.ws)))
        rec = w.poll()
        self.assertIsNotNone(rec)
        self.assertEqual(rec['kind'], 'fixed-text')
        self.assertIn('needs approval', rec['text_excerpt'])
        self.assertIn("Don't ask", rec['text_excerpt'])

    def test_quoted_legacy_text_with_leading_noise_is_not_a_denial(self):
        """同一 `Permission to use … has been denied` 句被前置噪声引用时不命中，只有位于起始处
        才命中——证明判定不靠 substring。"""
        w = self.watcher()
        self.append_event(_tool_result_event(
            'note> ' + _LEGACY_DENIED_TEXT, is_error=False, cwd=str(self.ws)))
        self.assertIsNone(w.poll())
        self.assertFalse(any(e['fact'] == 'stop' for e in w.facts()['events']))

    def test_fixed_text_case_insensitive(self):
        w = self.watcher()
        self.append_event(_denial_event(
            kind='text-only', is_error=False, cwd=str(self.ws),
            text='permission to use bash HAS BEEN denied for this session.'))
        self.assertIsNotNone(w.poll())

    def test_real_dont_ask_text_case_insensitive(self):
        w = self.watcher()
        self.append_event(_denial_event(
            kind='text-only', cwd=str(self.ws),
            text='THIS ACTION NEEDS APPROVAL, AND THE "Don\'t ask" MODE DOES NOT PROMPT.'))
        self.assertIsNotNone(w.poll())

    def test_ordinary_errors_and_quota_cancel_texts_are_not_denials(self):
        for text in ('File does not exist: /missing.txt',
                     'command not found: pytest (exit code 1)',
                     'HTTP 429 too many requests',
                     '402 payment required: insufficient balance',
                     'process exited with code 4294967295 (cancelled)',
                     '<tool_use_error>timeout after 30s</tool_use_error>'):
            with self.subTest(text=text):
                w = self.watcher()  # 先建监视器（新会话次序），再让"子进程"写事件
                self.append_event(_tool_result_event(text, is_error=True,
                                                     cwd=str(self.ws)))
                self.assertIsNone(w.poll())
                self.assertFalse(any(e['fact'] == 'stop'
                                     for e in w.facts()['events']))

    def test_short_and_long_workspace_paths_are_the_same_workspace(self):
        """同一工作区的真实路径归一：带 `..`/尾分隔符的等价写法必须一致（Windows 短名由
        8.3 用例单独覆盖）；身份核验不被删除。"""
        sub = self.ws / 'sub'
        sub.mkdir()
        for alt in (str(sub) + os.sep + '..', str(self.ws) + os.sep + '.',
                    str(self.ws) + os.sep):
            with self.subTest(alt=alt):
                self.assertEqual(qpw._norm_path(alt),
                                 qpw._norm_path(str(self.ws)))
        self.assertNotEqual(qpw._norm_path(str(self.ws)),
                            qpw._norm_path(str(self.tmp / 'other-ws')))

    @unittest.skipUnless(os.name == 'nt', 'Windows 8.3 short-name semantics')
    def test_windows_8_3_short_name_is_the_same_workspace(self):
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        n = ctypes.windll.kernel32.GetShortPathNameW(str(self.ws), buf, 260)
        if not n:
            self.skipTest('8.3 short path unavailable on this volume')
        short = buf.value
        self.assertEqual(qpw._norm_path(short), qpw._norm_path(str(self.ws)))

    def test_session_and_cwd_binding(self):
        w = self.watcher()
        self.append_event(_denial_event(session_id='99999999-9999-9999-9999-999999999999',
                                        cwd=str(self.ws)))
        self.append_event(_denial_event(cwd='/some/other/workspace'))
        self.assertIsNone(w.poll())
        self.append_event(_denial_event(cwd=str(self.ws)))
        rec = w.poll()
        self.assertEqual(rec['line_number'], 3)

    def test_narratives_and_non_tool_result_not_scanned(self):
        denial_block = _denial_event(cwd=str(self.ws))['message']['content'][0]
        events = [
            _event([{'type': 'text',
                     'text': 'earlier the tool said ' + _REAL_DENIED_TEXT + ' (quote)'}],
                   cwd=str(self.ws)),                              # 用户叙述文本块
            _event(None, etype='assistant', cwd=str(self.ws),
                   content=[denial_block]),                         # 助手侧（type 门）
            _event(None, cwd=str(self.ws), content=_REAL_DENIED_TEXT),   # content 是纯字符串
            _event([{'type': 'tool_use', 'id': 'x', 'name': 'Write',
                     'input': {'note': _REAL_DENIED_TEXT}}], cwd=str(self.ws)),  # 工具调用块
        ]
        w = self.watcher()
        for ev in events:
            self.append_event(ev)
        self.assertIsNone(w.poll())
        facts = w.facts()
        self.assertEqual(facts['lines_seen'], 4)
        self.assertFalse(any(e['fact'] == 'stop' for e in facts['events']))

    def test_resume_baseline_skips_old_events(self):
        self.append_event(_denial_event(cwd=str(self.ws)))
        w = self.watcher()  # 基线 = 现存大小：旧事件不触发
        self.assertEqual(w.baseline_bytes, self.file.stat().st_size)
        self.assertEqual(w.facts()['baseline_line_number'], 1)
        self.assertEqual(w.facts()['history_lines_skipped'], 1)  # 跳过历史不冒充已解析
        self.assertEqual(w.facts()['lines_seen'], 0)
        self.assertIsNone(w.poll())
        self.append_event(_denial_event(cwd=str(self.ws)))
        rec = w.poll()
        self.assertEqual(rec['line_number'], 2)  # 绝对原文件行号
        self.assertEqual(w.facts()['lines_seen'], 1)  # 新增已读行计数

    def test_segmented_json_line_buffered_until_newline(self):
        line = json.dumps(_denial_event(cwd=str(self.ws)), ensure_ascii=False)
        half = line[:len(line) // 2]
        w = self.watcher()  # 先建监视器：半行字节被读进缓冲，不解析、不崩溃
        self.append_raw(half)
        self.assertIsNone(w.poll())
        self.append_raw(line[len(line) // 2:] + '\n')
        rec = w.poll()
        self.assertEqual(rec['kind'], 'permission-rule')
        self.assertEqual(rec['line_number'], 1)

    def test_invalid_json_line_recorded_not_fatal(self):
        w = self.watcher()
        self.append_raw('{not valid json\n')
        self.assertIsNone(w.poll())
        facts = w.facts()
        self.assertEqual(facts['parse_errors'], 1)
        self.assertTrue(facts['degraded'])  # 读错如实记录，不冒充保护成功
        self.append_event(_denial_event(cwd=str(self.ws)))
        rec = w.poll()
        self.assertEqual(rec['line_number'], 2)

    def test_file_shrink_rebaselines_and_records_fact(self):
        self.append_event(_denial_event(cwd=str(self.ws)))
        w = self.watcher()
        with self.file.open('w', encoding='utf-8') as fh:
            fh.write('{"type":"user"}\n')  # 模拟轮转/重写变小
        self.assertIsNone(w.poll())
        facts = w.facts()
        self.assertTrue(any('shrank' in e['detail'] for e in facts['events']))
        self.assertTrue(facts['degraded'])
        self.append_event(_denial_event(cwd=str(self.ws)))
        rec = w.poll()
        self.assertEqual(rec['line_number'], 2)  # 重新基线后仍是绝对原文件行号

    def test_safe_session_id_and_workspace_key_guard(self):
        self.assertTrue(qpw.is_safe_session_id(_SID))
        for bad in ('../../etc/passwd', 'a/b', '', None, '.hidden', 'x' * 200):
            with self.subTest(bad=bad):
                self.assertFalse(qpw.is_safe_session_id(bad))
        with self.assertRaises(ValueError):
            qpw.session_jsonl_path(self.config_dir, '../escape', self.workspace_key)
        with self.assertRaises(ValueError):
            qpw.session_jsonl_path(self.config_dir, _SID, '../escape')
        with self.assertRaises(ValueError):
            qpw.session_jsonl_path(self.config_dir, _SID, 'a/b')
        # 只定位这一个精确会话文件，路径固定在 projects/<workspace-key>/<sid>.jsonl。
        self.assertEqual(qpw.session_jsonl_path(self.config_dir, _SID, self.workspace_key),
                         self.config_dir / 'projects' / self.workspace_key / (_SID + '.jsonl'))


# ---------------------------------------------------------------------------
# 3+4. 正式 main 的监视/终止/未知终态 + 真实合成子进程
# ---------------------------------------------------------------------------
class _WatchFakeChild:
    """可控合成 child：在 communicate 里把事件写进真实 native session jsonl，再按配置
    持有/退出。只承担"外部进程"职责，不参与判定。"""

    def __init__(self, pid, argv, stdout_handle, config_dir, *, events=(),
                 envelope=None, hold=False, release_on_terminate=True,
                 events_after_exit=False, on_exit=None):
        self.pid = pid
        self.returncode = None
        self.terminated = False
        self._argv = list(argv)
        self._stdout = stdout_handle
        self._config_dir = config_dir
        self._events = list(events)
        self._envelope = envelope
        self._hold = hold
        self._release = release_on_terminate
        self._events_after_exit = events_after_exit
        self._stopped = threading.Event()
        self._on_exit = on_exit

    def _session_id(self):
        for flag in ('--session-id', '--resume'):
            if flag in self._argv:
                return self._argv[self._argv.index(flag) + 1]
        return _SID

    def _write_events(self):
        # 现场布局：config_dir/projects/<workspace-key>/<sessionId>.jsonl（不是 projects/<sid>.jsonl）。
        sid = self._session_id()
        target = qpw.session_jsonl_path(self._config_dir, sid, _WORKSPACE_KEY)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('a', encoding='utf-8') as fh:
            for obj in self._events:
                # 真实 CLI 会在事件上盖章本会话 ID：这里同样以 argv 实际会话为准。
                stamped = {**obj, 'sessionId': sid}
                fh.write(json.dumps(stamped, ensure_ascii=False) + '\n')

    def _finish(self, code):
        if self.returncode is None:
            self.returncode = code
        if self._stdout is not None and self._envelope is not None:
            # 真实 CLI 会用本会话真实 ID 盖章 envelope：这里同样以 argv 实际会话为准，令
            # envelope.session_id 与生成/接续的真实 UUID 一致（身份核验同口径）。
            stamped = {**self._envelope, 'session_id': self._session_id()}
            self._stdout.write(json.dumps(stamped, ensure_ascii=False)
                               .encode('utf-8'))
            self._stdout.flush()
        if self._on_exit is not None:
            self._on_exit()
        return (None, None)

    def communicate(self, input=None):
        if self._events_after_exit:
            self._finish(0)
            self._write_events()
            return (None, None)
        self._write_events()
        if self._hold:
            self._stopped.wait(timeout=5)
        self._finish(0)
        return (None, None)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.terminated = True
        if not self._release:
            return  # 模拟终止信号后终态仍不可确认（poll 仍 None）
        self._stopped.set()
        self._finish(1)

    def kill(self):
        self.terminate()


class CnMainWatchTests(_CnMainBase):
    CHILD_PID = 515151

    def setUp(self):
        super().setUp()
        self._real_identity = qd.dp.process_identity
        qd.dp.process_identity = self._identity
        self.addCleanup(setattr, qd.dp, 'process_identity', self._real_identity)
        self._child_alive = False

    def _identity(self, pid, platform=None):
        try:
            if int(pid) == self.CHILD_PID:
                return {'pid': pid,
                        'state': 'alive' if self._child_alive else 'dead',
                        'created': 'synthetic-watch-child' if self._child_alive else None}
        except (TypeError, ValueError):
            pass
        return self._real_identity(pid, platform)

    def _install_child(self, cfg_dir, *, events=(), envelope=None, hold=False,
                       release=True, events_after_exit=False):
        holder = {}

        def popen(argv, *a, **k):
            self.calls.append(argv)
            child = _WatchFakeChild(self.CHILD_PID, argv, k.get('stdout'), str(cfg_dir),
                                    events=events, envelope=envelope, hold=hold,
                                    release_on_terminate=release,
                                    events_after_exit=events_after_exit,
                                    on_exit=self._child_died)
            holder['child'] = child
            self._child_alive = True
            return child

        qd.subprocess.Popen = popen
        return holder

    def _child_died(self):
        self._child_alive = False

    def _success_envelope(self, result='synthetic natural-exit text'):
        return {'type': 'result', 'subtype': 'success', 'is_error': False,
                'stop_reason': 'end_turn', 'session_id': _SID, 'result': result}

    def test_watch_terminates_child_on_denial_and_refuses_binding(self):
        cfg, cfg_dir = self.write_cn_config()
        ws, pf, out = self.make_case('stop')
        holder = self._install_child(
            cfg_dir, events=[_denial_event(cwd=str(ws))], hold=True)
        rc, printed = self.run_main(self.base_argv(
            'stop', cfg, ws, pf, out, ['--tools', 'Read,Write',
                                       '--allowed-tools', 'Edit(/nested/created.txt)']))
        self.assertEqual(rc, 3, printed)
        self.assertTrue(printed['permission_stop']['terminated_by_watch'], printed)
        self.assertFalse(printed['protocol_success'], printed)
        self.assertFalse(printed['report_bound'], printed)
        self.assertIn('parse_error', printed)  # 部分 stdout 未解析成 API 故障掩盖拒绝
        self.assertEqual(printed['permission_stop']['tool_use_id'], 'toolu_stop01')
        # 同一真实路径：Windows Administrator 与 8.3 短名 ADMINI~1 字面不同，
        # 统一解析为真实路径后比较（不改写断言、不 mock 生产路径）。
        self.assertEqual(Path(printed['permission_denials'][0]['source']).resolve(),
                         Path(out / 'permission-stop.json').resolve())
        self.assertNotIn('main_force_limit', printed)  # 绝不落 quota 标记
        child = holder['child']
        self.assertTrue(child.terminated)  # 只经本 child 对象发出的停止
        self.assertEqual(child.returncode, 1)
        self.assertEqual(printed['exit_code'], 1)
        stop = json.loads((out / 'permission-stop.json').read_text(encoding='utf-8'))
        self.assertTrue(stop['stop']['terminated_by_watch'])
        self.assertEqual(stop['denial']['raw_event']['tool_result']['tool_use_id'],
                         'toolu_stop01')
        self.assertTrue(any(e['fact'] == 'stop' for e in stop['monitor_facts']['events']))
        report_state = json.loads((out / 'report-state.json').read_text(encoding='utf-8'))
        self.assertFalse(report_state['bound'])
        self.assertTrue(any('permission watch terminated' in r
                            for r in report_state['reasons']))
        attempt = self.attempt_by_task('T-stop')
        self.assertEqual(attempt['state'], 'finished')  # 实际已退出才释放本 attempt
        self.assertEqual(attempt['child_pid'], self.CHILD_PID)
        self.assertTrue(printed['capacity_released'], printed)
        # 原始会话文件保留（监视只增量读取，绝不改写）。
        request = json.loads((out / 'request.json').read_text(encoding='utf-8'))
        session_file = qpw.session_jsonl_path(
            cfg_dir, request['cn_session_watch']['session_id'], _WORKSPACE_KEY)
        first_line = json.loads(session_file.read_text(encoding='utf-8').splitlines()[0])
        self.assertEqual(first_line['message']['content'][0]['tool_use_id'], 'toolu_stop01')

    def test_natural_exit_denial_classified_not_falsely_terminated(self):
        cfg, cfg_dir = self.write_cn_config()
        ws, pf, out = self.make_case('natural')
        self._install_child(cfg_dir, events=[_denial_event(cwd=str(ws))],
                            envelope=self._success_envelope(), events_after_exit=True)
        rc, printed = self.run_main(self.base_argv(
            'natural', cfg, ws, pf, out, ['--tools', 'Read,Write',
                                          '--allowed-tools',
                                          'Edit(/nested/created.txt)']))
        self.assertEqual(rc, 0, printed)  # 自然成功终态不被强行改写
        stop = printed['permission_stop']
        self.assertTrue(stop['observed_after_exit'], printed)
        self.assertFalse(stop['terminated_by_watch'], printed)
        self.assertTrue(printed['protocol_success'], printed)
        self.assertTrue((out / 'permission-stop.json').exists())
        attempt = self.attempt_by_task('T-natural')
        self.assertEqual(attempt['state'], 'finished')
        self.assertTrue(printed['capacity_released'], printed)

    def test_unknown_terminal_marks_unknown_and_keeps_capacity(self):
        cfg, cfg_dir = self.write_cn_config()
        ws, pf, out = self.make_case('unknown')
        holder = self._install_child(cfg_dir, events=[_denial_event(cwd=str(ws))],
                                     hold=True, release=False)
        real_grace = qpw.STOP_GRACE_SECONDS
        qpw.STOP_GRACE_SECONDS = 0.15  # 仅缩短测试等待；生产行为不变
        try:
            rc, printed = self.run_main(self.base_argv(
                'unknown', cfg, ws, pf, out,
                ['--tools', 'Read,Write',
                 '--allowed-tools', 'Edit(/nested/created.txt)']))
        finally:
            qpw.STOP_GRACE_SECONDS = real_grace
        self.assertEqual(rc, 3, printed)
        self.assertEqual(printed['terminal_state'], 'unknown', printed)
        self.assertTrue(printed['capacity_held'], printed)
        self.assertFalse(printed['permission_stop']['exit_confirmed'], printed)
        self.assertTrue(holder['child'].terminated)  # 停止信号确实经本对象发出过
        self.assertTrue((out / 'permission-stop.json').exists(),
                        'evidence must be saved before marking unknown')
        attempt = self.attempt_by_task('T-unknown')
        self.assertEqual(attempt['state'], 'unknown')  # 保留占位，绝不释放/抢占
        self.assertFalse(printed.get('capacity_released', False), printed)

    def test_normal_tool_error_continues_without_stop(self):
        cfg, cfg_dir = self.write_cn_config()
        ws, pf, out = self.make_case('normal-err')
        self._install_child(cfg_dir,
                            events=[_tool_result_event(
                                'command not found: pytest (exit code 1)',
                                is_error=True, cwd=str(ws))],
                            envelope=self._success_envelope())
        rc, printed = self.run_main(self.base_argv(
            'normal-err', cfg, ws, pf, out, ['--tools', 'Read,Write',
                                             '--allowed-tools',
                                             'Edit(/nested/created.txt)']))
        self.assertEqual(rc, 0, printed)
        self.assertNotIn('permission_stop', printed)
        self.assertFalse((out / 'permission-stop.json').exists())
        self.assertTrue(printed['protocol_success'], printed)
        # 无拒绝的正常结束也必须能回读本轮监控状态（会话是否被找到/观察、读取进度）。
        facts = printed['cn_watch_facts']
        self.assertTrue(facts['session_observed'], printed)
        self.assertFalse(facts['degraded'], printed)
        self.assertGreaterEqual(facts['lines_seen'], 1, printed)
        self.assertEqual(self.attempt_by_task('T-normal-err')['state'], 'finished')

    def test_main_resume_watches_verified_session_and_skips_old_events(self):
        cfg, cfg_dir = self.write_cn_config()
        ws, pf, out = self.make_case('resume')
        # 旧会话文件已有一笔历史拒绝事件：resume 基线必须跳过、绝不触发停止。
        old_session = qpw.session_jsonl_path(cfg_dir, _SID, _WORKSPACE_KEY)
        old_session.parent.mkdir(parents=True, exist_ok=True)
        old_session.write_text(
            json.dumps(_denial_event(cwd=str(ws)), ensure_ascii=False) + '\n',
            encoding='utf-8')
        src = self.tmp / 'prior-cn-request.json'
        src.write_text(json.dumps({'runtime_name': 'qodercn',
                                   'resume_verifiable': True}), encoding='utf-8')
        self._install_child(cfg_dir, events=[],
                            envelope=self._success_envelope())
        rc, printed = self.run_main(self.base_argv(
            'resume', cfg, ws, pf, out,
            ['--resume-session-id', _SID, '--resume-source', str(src)]))
        self.assertEqual(rc, 0, printed)
        self.assertNotIn('permission_stop', printed)
        request = json.loads((out / 'request.json').read_text(encoding='utf-8'))
        self.assertEqual(request['cn_session_watch']['source'], 'resume')
        self.assertEqual(request['cn_session_watch']['session_id'], _SID)
        self.assertGreater(request['cn_session_watch']['baseline_bytes'], 0)
        argv = self.calls[0]
        self.assertEqual(argv[argv.index('--resume') + 1], _SID)
        self.assertNotIn('--session-id', argv)


_FAKE_CN_CLI = r'''
import argparse, json, os, sys, time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument('--config-dir', dest='config_dir', required=True)
ap.add_argument('--session-id', dest='session_id', default=None)
ap.add_argument('--resume', dest='resume', default=None)
ap.add_argument('--cwd', dest='cwd', required=True)
args, _rest = ap.parse_known_args()
sid = args.session_id or args.resume
spec = json.loads(os.environ.get('BRAIN_FAKE_CN_SPEC', '{"mode":"deny-kind"}'))
try:
    sys.stdin.buffer.read()
except Exception:
    pass
projects = Path(args.config_dir) / 'projects'
# 现场布局：projects/<workspace-key>/<sessionId>.jsonl（不是 projects/<sid>.jsonl）。
session_file = projects / spec.get('workspace_key', 'synthetic-workspace-key') / (sid + '.jsonl')
session_file.parent.mkdir(parents=True, exist_ok=True)


def emit(block):
    session_file.open('a', encoding='utf-8').write(json.dumps(
        {'type': 'user', 'sessionId': sid, 'cwd': args.cwd,
         'message': {'role': 'user', 'content': [block]}}, ensure_ascii=False) + '\n')


mode = spec.get('mode')
if mode == 'deny-kind':
    emit({'type': 'tool_result', 'tool_use_id': 'toolu_real01', 'is_error': False,
          'content': 'Write blocked', 'toolDenialKind': 'permission-rule'})
elif mode == 'deny-text':
    emit({'type': 'tool_result', 'tool_use_id': 'toolu_real02', 'is_error': False,
          'content': 'Permission to use Write has been denied under dont_ask.'})
else:
    emit({'type': 'tool_result', 'tool_use_id': 'toolu_real03', 'is_error': True,
          'content': 'command not found: pytest (exit code 1)'})
time.sleep(float(spec.get('hold', 10 if mode.startswith('deny') else 0.3)))
Path(spec['sentinel']).write_text('late', encoding='utf-8')
print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                  'stop_reason': 'end_turn', 'session_id': sid,
                  'result': 'BW-CN-PERMISSION-STOP synthetic ok'}))
'''


class RealSubprocessStopTests(_CnMainBase):
    """本地无网络合成子进程（真实 OS 进程）写指定 native session、延迟写 sentinel：
    证明观察到拒绝 → 真实终止 → 后续 sentinel 未执行；普通错误照常继续到自然退出。"""

    def _run_real(self, tag, mode, hold=None):
        cfg, cfg_dir = self.write_cn_config(tag)
        ws, pf, out = self.make_case(tag)
        script = self.tmp / (tag + '-fake-cn.py')
        script.write_text(_FAKE_CN_CLI, encoding='utf-8')
        sentinel = self.tmp / (tag + '-sentinel.txt')
        spec = {'mode': mode, 'sentinel': str(sentinel), 'workspace_key': _WORKSPACE_KEY}
        if hold is not None:
            spec['hold'] = hold
        real_popen = self._real_popen
        holder = {}

        def script_popen(argv, *a, **k):
            env = dict(os.environ)
            env['BRAIN_FAKE_CN_SPEC'] = json.dumps(spec)
            child = real_popen([sys.executable, str(script)] + list(argv[1:]),
                               *a, env=env, **k)
            holder['child'] = child
            self.calls.append(argv)
            return child

        qd.subprocess.Popen = script_popen
        rc, printed = self.run_main(self.base_argv(
            tag, cfg, ws, pf, out, ['--tools', 'Read,Write',
                                    '--allowed-tools', 'Edit(/nested/created.txt)']))
        return rc, printed, out, sentinel, holder.get('child'), cfg_dir

    def test_real_denial_terminates_before_sentinel(self):
        rc, printed, out, sentinel, child, cfg_dir = self._run_real(
            'real-stop', 'deny-kind')
        self.assertEqual(rc, 3, printed)
        self.assertTrue(printed['permission_stop']['terminated_by_watch'], printed)
        self.assertNotEqual(printed['exit_code'], 0)
        self.assertEqual(printed['permission_stop']['tool_use_id'], 'toolu_real01')
        self.assertFalse(sentinel.exists(),
                         'post-denial sentinel must NOT execute after termination')
        stop = json.loads((out / 'permission-stop.json').read_text(encoding='utf-8'))
        self.assertEqual(stop['denial']['kind'], 'permission-rule')
        self.assertGreaterEqual(stop['monitor_facts']['lines_seen'], 1)
        attempt = self.attempt_by_task('T-real-stop')
        self.assertEqual(attempt['state'], 'finished')
        self.assertEqual(attempt['child_pid'], child.pid)  # 终止的是自己启动的 child
        self.assertTrue(printed['capacity_released'], printed)
        session_file = qpw.session_jsonl_path(
            cfg_dir, json.loads((out / 'request.json').read_text(encoding='utf-8'))
            ['cn_session_watch']['session_id'], _WORKSPACE_KEY)
        self.assertTrue(session_file.exists())  # 原始会话文件保留不改

    def test_real_fixed_text_denial_terminates(self):
        rc, printed, out, sentinel, child, _cfg_dir = self._run_real(
            'real-stop-text', 'deny-text')
        self.assertEqual(rc, 3, printed)
        self.assertTrue(printed['permission_stop']['terminated_by_watch'], printed)
        self.assertEqual(printed['permission_stop']['kind'], 'fixed-text')
        self.assertFalse(sentinel.exists())

    def test_real_normal_error_continues_to_natural_exit(self):
        rc, printed, out, sentinel, child, _cfg_dir = self._run_real(
            'real-normal', 'normal')
        self.assertEqual(rc, 0, printed)
        self.assertNotIn('permission_stop', printed)
        self.assertTrue(sentinel.exists(), 'normal tool error must not stop the child')
        self.assertEqual(child.returncode, 0)
        # 真实合成子进程的正常结束同样要能回读本轮监控状态（观察就绪、无降级）。
        facts = printed['cn_watch_facts']
        self.assertTrue(facts['session_observed'], printed)
        self.assertFalse(facts['degraded'], printed)
        self.assertGreaterEqual(facts['lines_seen'], 1, printed)
        self.assertEqual(self.attempt_by_task('T-real-normal')['state'], 'finished')


if __name__ == '__main__':
    unittest.main(verbosity=2)
