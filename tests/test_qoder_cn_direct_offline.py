#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BW-QODER-CN-20261010-A2 Qoder CN 原生入口离线测试（纯标准库 unittest，零真实 CLI/网络）。

覆盖任务书明确要求的、**且只有这些**新行为：
1. 老国际 argv 不变：build_argv 仍产出已验证的官方参数形状。
2. CN 原生 + 自定义 model ID 映射：build_cn_argv 首元素为原生 cli、含 --config-dir、
   --model 下发真实 model_id（不是友好名、不是 Qwen）。
3. 缺映射 → 0 Popen：CN 请求的友好名没有真实 ID 映射时 main() 早退（返回码 2、
   subprocess.Popen 从未被调用、输出目录从未创建）。
4. CN 接续无来源可证 → 拒绝：无 --resume-source 或非 qodercn 原件时拒绝续用（0 Popen）。
5. BW-CANDIDATE-SCOPE-S2 主力明确限额落标：由正式 qd.main() 走真实容量入口→失败终态→
   落标→终态释放的整条链验收（详见下方 S2 用例段落说明；不复刻生产分支、不 mock 被测件）。

绝不读写真实/共享池；全程临时目录 + 假 Popen。真实 CN 任务未实际运行，成本/后端
身份未知（见报告）。
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
_SCRIPTS = HERE.parent / 'scripts'
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from dispatch_mode_support import user_disabled_pools  # noqa: E402


def _load_qd():
    spec = importlib.util.spec_from_file_location(
        'qoder_direct_cn_under_test', _SCRIPTS / 'qoder_direct.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


qd = _load_qd()

# 已验证的国际官方参数形状（与 test_qoder_direct_offline 的 round-01 一致）。
_INTL_NODE = '/abs/node'
_INTL_CLI = '/abs/qodercli.js'
INTL_ARGV = [_INTL_NODE, _INTL_CLI, '--cwd', '/abs/ws', '--model', 'Qwen3.8-Flash',
             '--tools', 'Read', '--permission-mode', 'dont_ask', '--strict-mcp-config',
             '--mcp-config', '{"mcpServers":{}}', '--output-format', 'json', '-p',
             '--allowed-tools', 'Read']


class _NoPopen(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='bw-cn-direct-')
        self.tmp = Path(self._tmp.name)
        self.calls = []
        self._real_popen = qd.subprocess.Popen
        qd.subprocess.Popen = self._fake_popen
        # 隔离：绝不触碰真实/共享池或额度库。
        self._saved_env = {k: os.environ.get(k) for k in
                           ('BRAIN_WORKER_DISPATCH_STORE', 'BRAIN_WORKER_QUOTA_STORE')}
        os.environ['BRAIN_WORKER_DISPATCH_STORE'] = str(self.tmp / 'pool.sqlite3')
        os.environ['BRAIN_WORKER_QUOTA_STORE'] = str(self.tmp / 'quota.sqlite3')

    def tearDown(self):
        qd.subprocess.Popen = self._real_popen
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def _fake_popen(self, *a, **k):
        self.calls.append(a)
        raise AssertionError('subprocess.Popen must not be called in a refusal path')

    def write_cn_config(self, model_ids):
        cli = self.tmp / 'qodercn-native.exe'
        cli.write_text('placeholder-stub')  # 只要求存在，绝不代表真跑
        cfg_dir = self.tmp / 'cfgdir'
        cfg_dir.mkdir(exist_ok=True)
        cfg = self.tmp / 'local-entry-cn.json'
        cfg.write_text(json.dumps({'cli': str(cli), 'config_dir': str(cfg_dir),
                                   'model_ids': model_ids}), encoding='utf-8')
        return cfg

    def make_prompt(self):
        ws = self.tmp / 'proj'
        ws.mkdir(exist_ok=True)
        pf = self.tmp / 'prompt.txt'
        pf.write_text('CN 离线链路测试提示词', encoding='utf-8')
        return ws, pf


class InternationalArgvTests(unittest.TestCase):
    def test_international_build_argv_shape_unchanged(self):
        built = qd.build_argv({'node': _INTL_NODE, 'qodercli': _INTL_CLI},
                              '/abs/ws', 'Qwen3.8-Flash', 'Read', None)
        self.assertEqual(built, INTL_ARGV, built)

    def test_cn_argv_differs_from_international_uses_real_id(self):
        cfg = {'cli': '/abs/qodercn.exe', 'config_dir': '/abs/cfg',
               'model_ids': {'DeepSeek-Flash': 'deepseek-flash-v3-real-id'}}
        argv = qd.build_cn_argv(cfg, '/abs/cfg', 'deepseek-flash-v3-real-id',
                                '/abs/ws', 'Read', None)
        self.assertEqual(argv[0], '/abs/qodercn.exe')
        self.assertIn('--config-dir', argv)
        self.assertEqual(argv[argv.index('--config-dir') + 1], '/abs/cfg')
        # --model 下发真实 ID，不是友好名 'DeepSeek-Flash'。
        self.assertEqual(argv[argv.index('--model') + 1], 'deepseek-flash-v3-real-id')
        self.assertNotIn('DeepSeek-Flash', argv)
        self.assertNotIn(_INTL_NODE, argv)  # 与国际 node 形状不同


class ResolveModelIdTests(_NoPopen):
    def test_resolve_cn_model_id_maps_and_missing(self):
        cfg = {'cli': 'x', 'config_dir': 'y',
               'model_ids': {'DeepSeek-Flash': 'real-id-123',
                             'Qwen-3.8-Max': 'synthetic-cn-max-real-id'}}
        # 自定义 Token Plan：必须走 model_ids 映射到真实服务端 ID（保留客户端自定义）。
        self.assertEqual(qd.resolve_cn_model_id(cfg, 'DeepSeek-Flash'), 'real-id-123')
        # CN 内置官方 Qwen 白名单：字面串直传即合法成功路径（不查 model_ids、不猜 UUID、
        # 不把内置名当自定义）。这是 S2/S3 的 CN 主力/兜底组合。
        self.assertEqual(qd.resolve_cn_model_id(cfg, 'Qwen3.8-Max'), 'Qwen3.8-Max')
        self.assertEqual(qd.resolve_cn_model_id(cfg, 'Qwen3.8-Flash'), 'Qwen3.8-Flash')
        # BW-MAX-WINDOW-20261010-S2：自定义友好名 Qwen-3.8-Max（短横线）**不是**内置白名单，
        # 必须经私有 model_ids 映射到合成真实 ID；绝不因名字像 Qwen 就当内置直传。
        self.assertNotIn('Qwen-3.8-Max', qd.CN_BUILTIN_MODELS)
        self.assertEqual(qd.resolve_cn_model_id(cfg, 'Qwen-3.8-Max'),
                         'synthetic-cn-max-real-id')
        # 缺映射时该自定义名一律 None（零 Popen），绝不回落到内置同名 Qwen3.8-Max 或猜 UUID。
        self.assertIsNone(qd.resolve_cn_model_id(
            {'cli': 'x', 'config_dir': 'y', 'model_ids': {}}, 'Qwen-3.8-Max'))
        # 未映射的非内置名仍拒绝（None → 调用方零 Popen），绝不猜同名内建/Qwen。
        self.assertIsNone(qd.resolve_cn_model_id(cfg, 'Bogus-Not-Mapped'))
        self.assertIsNone(qd.resolve_cn_model_id(cfg, 'GLM-5.3'))


class CnMainRefusalTests(_NoPopen):
    def _run_main(self, argv):
        old = sys.argv
        sys.argv = ['qoder_direct.py', *argv]
        try:
            return qd.main()
        finally:
            sys.argv = old

    def test_missing_mapping_zero_popen(self):
        cfg = self.write_cn_config({'DeepSeek-Flash': 'real-id-123'})
        ws, pf = self.make_prompt()
        out_dir = self.tmp / 'out-missing'
        rc = self._run_main([
            '--runtime', 'qodercn', '--cn-config', str(cfg),
            '--workspace', str(ws), '--prompt-file', str(pf),
            '--output-dir', str(out_dir), '--stage', 'BW-CN-TEST',
            '--model', 'Bogus-Not-Mapped'])
        self.assertEqual(rc, 2, rc)
        self.assertEqual(self.calls, [])  # 0 Popen
        self.assertFalse(out_dir.exists())  # 零证据目录

    def test_builtin_default_model_not_refused_at_mapping(self):
        # CN 内置白名单：缺省 --model 是 Qwen3.8-Max，属 CN 内置官方组合，映射门**不得**再
        # 以“无 model_ids 映射”拒绝（旧口径已改）。用记录型假 Popen 证明它通过映射门、消费了
        # CN claim、真正到达 Popen，且 --model 下发的是内置字面串（非友好名映射、非猜 UUID）。
        # 绝不真跑 CLI：假 Popen 立即抛哨兵，main 捕获后安全释放本占位（start_failed）。
        # BW-MAX-WINDOW-20261010-S2：内置 Max 只在主力时段放行；本例只验映射门，故把时钟
        # 固定在时段内（离线测试夹具唯一允许的时间控制），使容量门照常 claim 到到达 Popen。
        from datetime import datetime, timezone
        real_utcnow = qd.dp.utcnow
        qd.dp.utcnow = lambda: datetime(2026, 10, 8, 15, 0, 0, tzinfo=timezone.utc)
        try:
            cfg = self.write_cn_config({'DeepSeek-Flash': 'real-id-123'})
            ws, pf = self.make_prompt()
            out_dir = self.tmp / 'out-builtin-default'
            captured = {}

            def recording_popen(argv, *a, **k):
                captured['argv'] = argv
                raise RuntimeError('sentinel: reached Popen (mapping gate passed)')

            qd.subprocess.Popen = recording_popen
            rc = self._run_main([
                '--runtime', 'qodercn', '--cn-config', str(cfg),
                '--workspace', str(ws), '--prompt-file', str(pf),
                '--output-dir', str(out_dir), '--stage', 'BW-CN-TEST'])
        finally:
            qd.dp.utcnow = real_utcnow
        # 不是映射门拒绝（rc != 2）；确实到达 Popen；--model 为内置 Qwen3.8-Max 字面串。
        self.assertNotEqual(rc, 2, rc)
        self.assertIn('argv', captured)
        argv = captured['argv']
        self.assertEqual(qd.DEFAULT_MODEL, 'Qwen3.8-Max')
        self.assertEqual(argv[argv.index('--model') + 1], qd.DEFAULT_MODEL)

    def test_builtin_default_model_daytime_capacity_gate_refuses_zero_popen(self):
        # BW-MAX-WINDOW-20261010-S2：时段外（北京白天）原生 CN 入口缺省内置 Max 必须被容量门
        # 如实拒绝（rc=2、main_force_window_closed、0 Popen），绝不静默换成 Flash 或自定义。
        from datetime import datetime, timezone
        real_utcnow = qd.dp.utcnow
        qd.dp.utcnow = lambda: datetime(2026, 10, 8, 4, 0, 0, tzinfo=timezone.utc)  # 北京 12:00
        try:
            cfg = self.write_cn_config({'DeepSeek-Flash': 'real-id-123'})
            ws, pf = self.make_prompt()
            out_dir = self.tmp / 'out-builtin-daytime'
            rc = self._run_main([
                '--runtime', 'qodercn', '--cn-config', str(cfg),
                '--workspace', str(ws), '--prompt-file', str(pf),
                '--output-dir', str(out_dir), '--stage', 'BW-CN-TEST'])
        finally:
            qd.dp.utcnow = real_utcnow
        self.assertEqual(rc, 2, rc)
        self.assertEqual(self.calls, [])  # 0 Popen
        self.assertFalse(out_dir.exists())  # 零证据目录

    def test_cn_resume_without_verifiable_source_rejected(self):
        cfg = self.write_cn_config({'DeepSeek-Flash': 'real-id-123'})
        ws, pf = self.make_prompt()
        out_dir = self.tmp / 'out-resume'
        rc = self._run_main([
            '--runtime', 'qodercn', '--cn-config', str(cfg),
            '--workspace', str(ws), '--prompt-file', str(pf),
            '--output-dir', str(out_dir), '--stage', 'BW-CN-TEST',
            '--model', 'DeepSeek-Flash', '--resume-session-id', 'sess-intl-1'])
        self.assertEqual(rc, 2, rc)
        self.assertEqual(self.calls, [])
        self.assertFalse(out_dir.exists())

    def test_cn_resume_with_international_source_rejected(self):
        cfg = self.write_cn_config({'DeepSeek-Flash': 'real-id-123'})
        ws, pf = self.make_prompt()
        # 伪造一份国际 runtime 的 request.json 作为 resume-source → 仍拒绝（非 qodercn）。
        bad_src = self.tmp / 'intl-request.json'
        bad_src.write_text(json.dumps({'runtime': {'node': 'x', 'qodercli': 'y'}}),
                           encoding='utf-8')
        out_dir = self.tmp / 'out-resume2'
        rc = self._run_main([
            '--runtime', 'qodercn', '--cn-config', str(cfg),
            '--workspace', str(ws), '--prompt-file', str(pf),
            '--output-dir', str(out_dir), '--stage', 'BW-CN-TEST',
            '--model', 'DeepSeek-Flash', '--resume-session-id', 'sess-intl-1',
            '--resume-source', str(bad_src)])
        self.assertEqual(rc, 2, rc)
        self.assertEqual(self.calls, [])
        self.assertFalse(out_dir.exists())


# ============================================================================
# BW-CN-WRITE-20261012T135751Z：CN 逐文件新建权限规则不兼容的离线反例
# ----------------------------------------------------------------------------
# 原始失败（真实，保留不改写）：CN 入口把 allow 规则 `Write(/references/project-closeout.md)`
# 原样下发，permission-mode=dont_ask 下被 CLI 拒绝；同一轮里 `Edit(/SKILL.md)` 成功。
# 官方口径（docs.qoder.cn/cli/permissions）：path-scoped 写入规则是 Edit(...)，覆盖
# Edit/Write/NotebookEdit；`--tools` 里的 Write 只是工具可见性，与权限规则不同层。
# 本组用例只验证最小修复：CN 在构造 argv 前拒绝路径限定 Write(...) 的 allow **和** deny，
# 工具级裸 Write / Write(*) 与 Edit(...) 逐文件规则、Write 可见性一律原样到达 argv，
# 国际 build_argv 一字不变；拒绝必须发生在容量 claim 与 Popen 之前（0 Popen、零证据目录）。
# ============================================================================
_CN_BAD_WRITE_RULES = ('Write(/references/project-closeout.md)', 'Write(/probe-created.txt)',
                       'Write(C:/work/new.md)', 'Write(**)', 'Write()')


class CnPathScopedWriteRuleTests(unittest.TestCase):
    def test_detector_flags_only_path_scoped_write(self):
        """只标记路径限定 Write(...)；裸 Write、Write(*) 与 Edit(...)/Bash(...)/Read 不动。"""
        self.assertEqual(qd.path_scoped_write_rules(_CN_BAD_WRITE_RULES),
                         list(_CN_BAD_WRITE_RULES))
        for rule in ('Write', 'Write(*)', 'Edit(/SKILL.md)', 'Edit(**)', 'Bash(pytest x)',
                     'Read', 'NotebookEdit(/a.ipynb)', 'WriteX(/a.md)', 'write(/a.md)'):
            with self.subTest(rule=rule):
                self.assertEqual(qd.path_scoped_write_rules([rule]), [])
        self.assertEqual(qd.path_scoped_write_rules(None), [])

    def test_validate_rejects_allow_and_deny_symmetrically(self):
        """allow 与 deny 同检：路径限定 deny 被静默接受会让调用方误以为已禁写。"""
        for rule in _CN_BAD_WRITE_RULES:
            for side in ('allowed_tools', 'disallowed_tools'):
                with self.subTest(rule=rule, side=side):
                    kwargs = {side: [rule]}
                    with self.assertRaises(ValueError) as ctx:
                        qd.validate_cn_permission_rules(kwargs.get('allowed_tools'),
                                                        kwargs.get('disallowed_tools'))
                    msg = str(ctx.exception)
                    self.assertIn(rule, msg)
                    self.assertIn('Edit(...)', msg)
        # 工具级与逐文件 Edit 一律放行，且不改写调用方给的规则内容。
        qd.validate_cn_permission_rules(['Write', 'Write(*)', 'Edit(/SKILL.md)'],
                                         ['Write', 'Bash(npm test)'])

    def test_message_never_widens_or_translates_authorization(self):
        """拒绝文案不得出现"已自动改成 Edit"之类的隐式提权；只要求调用方自己改用同范围 Edit。"""
        with self.assertRaises(ValueError) as ctx:
            qd.validate_cn_permission_rules(['Write(/references/project-closeout.md)'], None)
        msg = str(ctx.exception)
        self.assertIn('does not accept path-scoped Write(...)', msg)
        self.assertIn('Refusing to auto-widen or auto-translate', msg)
        self.assertNotIn('Edit(/references/project-closeout.md)', msg)  # 绝不代填替换规则


class CnArgvRuleGateTests(unittest.TestCase):
    CFG = {'cli': '/abs/qodercn.exe', 'config_dir': '/abs/cfg',
           'model_ids': {'DeepSeek-Flash': 'real-id-123'}}

    def _build(self, **kw):
        # 默认 --tools 只带只读工具：裸 Write 回退会先触发 BW-CN-PERMISSION-STOP-S1 的
        # mutation 门，掩盖本组用例各自要测的门。
        sid = kw.pop('session_id', None)
        return qd.build_cn_argv(self.CFG, '/abs/cfg', 'real-id-123', '/abs/ws',
                               kw.pop('tools', 'Read'), sid, **kw)

    @staticmethod
    def _rules(argv):
        return [argv[i + 1] for i, a in enumerate(argv) if a == '--allowed-tools']

    def _denies(self, argv):
        return [argv[i + 1] for i, a in enumerate(argv) if a == '--disallowed-tools']

    def test_cn_argv_refused_before_any_argv_is_built(self):
        for rule in _CN_BAD_WRITE_RULES:
            with self.subTest(rule=rule):
                self.assertRaises(ValueError, self._build, allowed_tools=[rule])
                self.assertRaises(ValueError, self._build, disallowed_tools=[rule])

    def test_tool_level_write_and_edit_rules_pass_through_verbatim(self):
        """BW-CN-PERMISSION-STOP-S1 起裸/整工具通配 mutation allow 规则被 CN 门拒绝（原始
        故障：裸规则下 Write 被 dont_ask 拒绝后模型改用 Bash touch/重定向反复试写）。
        逐文件 Edit(...) 规则 + Write 仅作 --tools 可见性原样放行：不拆逗号、不改前缀、
        不追加默认。"""
        for bad in ('Write', 'Write(*)', 'Edit', 'Edit(*)', 'Bash', 'Bash(*)',
                    'NotebookEdit', 'NotebookEdit(*)'):
            with self.subTest(rule=bad):
                with self.assertRaises(ValueError) as ctx:
                    self._build(tools='Read,Write',
                                allowed_tools=['Edit(/scripts/qoder_direct.py)', bad])
                msg = str(ctx.exception)
                self.assertIn(repr(bad), msg)
                # 明确要求具体形状：Edit(/工作区相对具体文件) 与 Bash(登记原样命令)。
                self.assertIn('Edit(/<workspace-relative specific file>)', msg)
                self.assertIn('Bash(<registered verbatim command>)', msg)
                # 静态门只证明规则形状，不声称真实权限成功。
                self.assertIn('does not claim', msg)
        argv = self._build(tools='Read,Write',
                           allowed_tools=['Edit(/scripts/qoder_direct.py)'],
                           disallowed_tools=['Write'])
        self.assertEqual(argv[:1], ['/abs/qodercn.exe'])
        self.assertEqual(self._rules(argv), ['Edit(/scripts/qoder_direct.py)'])
        # deny 是收紧不是授权：裸 Write deny 原样保留，不受 mutation 门影响。
        self.assertEqual(self._denies(argv), ['Write'])
        self.assertEqual(argv[argv.index('--tools') + 1], 'Read,Write')
        self.assertEqual(argv[argv.index('--permission-mode') + 1], 'dont_ask')

    def test_legacy_tools_fallback_bare_write_is_not_a_path_rule(self):
        """未显式给 allowed_tools 时按 --tools 逗号回退成裸工具名：回退生成的裸 mutation
        规则（Write/Edit/Bash）同样被拒（含 tools-only 兼容回退）；只读回退照常放行。"""
        with self.assertRaises(ValueError) as ctx:
            self._build(tools='Read,Write')
        self.assertIn('Write', str(ctx.exception))
        with self.assertRaises(ValueError):
            self._build(tools='Read,Glob,Grep,Edit,Write,Bash')
        argv = self._build(tools='Read,Glob,Grep')
        self.assertEqual(self._rules(argv), ['Read', 'Glob', 'Grep'])

    def test_cn_argv_new_session_uuid_and_resume_flags(self):
        """BW-CN-PERMISSION-STOP-S1：新会话可传 session_uuid → 官方 --session-id；resume
        仍走 --resume；两者都不传时 argv 无会话 flag。"""
        argv = self._build(tools='Read', allowed_tools=['Read'],
                           session_uuid='0f0e0d0c-1111-2222-3333-444455556666')
        self.assertEqual(argv[-2:], ['--session-id', '0f0e0d0c-1111-2222-3333-444455556666'])
        argv = self._build(tools='Read', allowed_tools=['Read'],
                           session_id='0f0e0d0c-1111-2222-3333-444455556666')
        self.assertEqual(argv[-2:], ['--resume', '0f0e0d0c-1111-2222-3333-444455556666'])
        argv = self._build(tools='Read', allowed_tools=['Read'])
        self.assertNotIn('--session-id', argv)
        self.assertNotIn('--resume', argv)

    def test_international_build_argv_unchanged_for_path_scoped_write(self):
        """国际路径不受本门影响（本轮明确禁止改国际行为）。"""
        built = qd.build_argv({'node': _INTL_NODE, 'qodercli': _INTL_CLI},
                              '/abs/ws', 'Qwen3.8-Flash', 'Read,Write', None,
                              allowed_tools=['Write(/references/project-closeout.md)'],
                              disallowed_tools=['Write(/x.md)'])
        self.assertEqual([built[i + 1] for i, a in enumerate(built)
                          if a == '--allowed-tools'],
                         ['Write(/references/project-closeout.md)'])
        self.assertEqual([built[i + 1] for i, a in enumerate(built) if a == '--disallowed-tools'],
                         ['Write(/x.md)'])


class CnMainWriteRuleRefusalTests(_NoPopen):
    def _run_main(self, argv):
        old = sys.argv
        sys.argv = ['qoder_direct.py', *argv]
        printed = io.StringIO()
        try:
            with redirect_stdout(printed):
                rc = qd.main()
        finally:
            sys.argv = old
        return rc, json.loads(printed.getvalue())

    def _case(self, tag, extra):
        cfg = self.write_cn_config({'DeepSeek-Flash': 'real-id-123'})
        ws, pf = self.make_prompt()
        out_dir = self.tmp / ('out-' + tag)
        # BW-CN-PERMISSION-STOP-S1 起 --tools 回退含裸 Write 会先触发 mutation 门；本组
        # 用例专测路径限定 Write(...) 规则拒绝，故 --tools 只带只读工具（裸 Read 合法）。
        argv = ['--runtime', 'qodercn', '--cn-config', str(cfg),
                '--workspace', str(ws), '--prompt-file', str(pf),
                '--output-dir', str(out_dir), '--stage', 'BW-CN-WRITE-RULE-' + tag,
                '--model', 'Qwen3.8-Flash', '--dispatch-store',
                str(self.tmp / 'pool.sqlite3'), '--tools', 'Read', *extra]
        return argv, out_dir

    def test_main_refuses_path_scoped_write_rule_zero_popen(self):
        """正式 main 受控未派发：rc=2、sent=false、0 Popen、零证据目录、不消费容量。"""
        for label, extra in (('allow', ['--allowed-tools',
                                         'Write(/references/project-closeout.md)']),
                             ('deny', ['--disallowed-tools',
                                       'Write(/references/project-closeout.md)'])):
            with self.subTest(label=label):
                argv, out_dir = self._case('main-' + label, extra)
                rc, printed = self._run_main(argv)
                self.assertEqual(rc, 2, printed)
                self.assertTrue(printed['cn_permission_rule_rejected'], printed)
                self.assertFalse(printed['sent'], printed)
                self.assertEqual(printed['runtime'], qd.RUNTIME_CN)
                self.assertIn('Write(/references/project-closeout.md)',
                              ' '.join(printed['reasons']))
                self.assertEqual(self.calls, [], 'refusal must happen before Popen')
                self.assertFalse(out_dir.exists())
                with closing(qd.dp.connect(str(self.tmp / 'pool.sqlite3'))) as conn:
                    self.assertEqual(conn.execute('SELECT COUNT(*) c FROM attempts'
                                                  ).fetchone()['c'], 0,
                                     'refusal must not consume a capacity claim')

    def test_main_accepts_edit_rule_with_write_visibility(self):
        """正向对照：同一 CN 入口在 Edit(...) 逐文件规则 + Write 仅作 --tools 可见性下照常
        走到 Popen 前逻辑，绝不被新门误伤（假 Popen 抛哨兵，证明 argv 已带该规则原样到达
        外部进程入口）。BW-CN-PERMISSION-STOP-S1 起裸 `Write` allow 规则本身被门拒绝，
        故正向用例不再携带裸 Write 规则（Write 只保留在可见性里）。"""
        cfg = self.write_cn_config({'DeepSeek-Flash': 'real-id-123'})
        ws, pf = self.make_prompt()
        out_dir = self.tmp / 'out-edit-rule'
        captured = {}

        def recording_popen(argv, *a, **k):
            captured['argv'] = argv
            raise RuntimeError('sentinel: reached Popen (rule gate passed)')

        qd.subprocess.Popen = recording_popen
        old = sys.argv
        sys.argv = ['qoder_direct.py', '--runtime', 'qodercn', '--cn-config', str(cfg),
                    '--workspace', str(ws), '--prompt-file', str(pf),
                    '--output-dir', str(out_dir), '--stage', 'BW-CN-WRITE-RULE-OK',
                    '--model', 'Qwen3.8-Flash', '--tools', 'Read,Write',
                    '--allowed-tools', 'Edit(/scripts/qoder_direct.py)',
                    '--dispatch-store', str(self.tmp / 'pool.sqlite3')]
        try:
            with redirect_stdout(io.StringIO()):
                rc = qd.main()
        finally:
            sys.argv = old
        self.assertIn('argv', captured, f'rule gate must not block valid Edit rules (rc={rc})')
        argv = captured['argv']
        rules = [argv[i + 1] for i, a in enumerate(argv) if a == '--allowed-tools']
        self.assertEqual(rules, ['Edit(/scripts/qoder_direct.py)'])
        self.assertEqual(argv[argv.index('--tools') + 1], 'Read,Write')
        # 新会话：官方 --session-id 携带入口生成的 UUID（供会话权限拒绝监视精确定位）。
        self.assertIn('--session-id', argv)
        sid = argv[argv.index('--session-id') + 1]
        request = json.loads((out_dir / 'request.json').read_text(encoding='utf-8'))
        self.assertEqual(request['cn_session_watch']['session_id'], sid)
        self.assertEqual(request['cn_session_watch']['source'], 'generated')


# ============================================================================
# BW-POOL-SPLIT-20261010-S5 需求 2 回归：qoder_direct 明确限额文本识别
# ============================================================================
class ExplicitQuotaLimitHitTests(unittest.TestCase):
    """_explicit_quota_limit_hit 只扫结构化错误载体（result_errors / errors_info），
    绝不扫报告正文、绝不把 permission / 认证 / 成功 / 取消 exit / 单独 quota 误作限额。
    覆盖真实失败文案 “You've reached your credit usage limit.”。"""

    def _hit(self, errors=None, errors_info=None):
        summary = {'result_errors': errors, 'result_errors_info': errors_info,
                   'protocol_success': False}
        return qd._explicit_quota_limit_hit(summary)

    def test_recognizes_credit_usage_limit_real_text(self):
        """需求 2 必识别文案：errors 列表直接含 “You've reached your credit usage limit.”。"""
        hit, ev = self._hit(errors=["You've reached your credit usage limit."])
        self.assertTrue(hit, (hit, ev))
        self.assertEqual(ev, "You've reached your credit usage limit.")

    def test_recognizes_credit_usage_limit_in_errors_info(self):
        hit, ev = self._hit(errors_info=[{'status': 402, 'code': 'quota',
                                          'details': "You've reached your credit "
                                          'usage limit. Please upgrade.'}])
        self.assertTrue(hit, (hit, ev))
        self.assertIn('credit usage limit', ev.lower())

    def test_recognizes_case_insensitive(self):
        hit, _ = self._hit(errors=["YOU'VE REACHED YOUR CREDIT USAGE LIMIT."])
        self.assertTrue(hit, hit)

    def test_recognizes_credits_exhausted_variants(self):
        for text in ('Credits exhausted for this account.',
                     'You are out of credits.',
                     'insufficient balance — top up required',
                     '额度用尽，请稍后重试',
                     '积分不足'):
            with self.subTest(text=text):
                hit, _ = self._hit(errors=[text])
                self.assertTrue(hit, (text, hit))

    def test_recognizes_personal_credits_have_been_exhausted_real_terminal(self):
        """实证缺口：真实 CN 内置 Max 终态文案 “Your personal Credits have been
        exhausted. Please check your plan.”（“have been” 插在中间，不与旧的连续
        'credits exhausted' marker 匹配）。必须准确识别为硬限额。"""
        msg = ('Your personal Credits have been exhausted. Please check your plan.')
        hit, ev = self._hit(errors=[msg])
        self.assertTrue(hit, (hit, ev))
        self.assertEqual(ev, msg)

    def test_personal_credits_have_been_exhausted_case_insensitive(self):
        for msg in ('your personal credits have been exhausted. please check your plan.',
                    'YOUR PERSONAL CREDITS HAVE BEEN EXHAUSTED.',
                    'Your Personal Credits Have Been Exhausted'):
            with self.subTest(msg=msg):
                hit, _ = self._hit(errors=[msg])
                self.assertTrue(hit, (msg, hit))

    def test_personal_credits_have_been_exhausted_in_errors_info(self):
        """同一文案落在 errors_info 结构化载体（details/message）也须识别；这仍只来自
        失败终态错误载体，绝不扫报告正文。"""
        hit, ev = self._hit(errors_info=[{
            'details': 'Your personal Credits have been exhausted. Please check your plan.'}])
        self.assertTrue(hit, (hit, ev))
        self.assertIn('credits have been exhausted', str(ev).lower())

    def test_does_not_classify_bare_quota_word(self):
        """单独 “quota” 文案不能当硬限额；无 429、无 markers → hit=False。"""
        hit, ev = self._hit(errors=['quota status: normal'])
        self.assertFalse(hit, (hit, ev))
        self.assertIsNone(ev)

    def test_does_not_scan_report_body(self):
        """载体全空、正文里出现 429 或 credit usage limit → hit=False（分类器不看正文）。"""
        summary = {'result_errors': None, 'result_errors_info': None,
                   'response_text': "upstream said 429 and You've reached your "
                                    'credit usage limit. (report body)',
                   'protocol_success': False}
        self.assertFalse(qd._explicit_quota_limit_hit(summary)[0])

    def test_does_not_classify_permission_denial(self):
        hit, ev = self._hit(errors=['No permission client configured for Bash'],
                            errors_info=[{'category': 'permission',
                                          'details': 'user denied tool Edit'}])
        self.assertFalse(hit, (hit, ev))

    def test_does_not_classify_auth_error(self):
        hit, ev = self._hit(errors=['invalid api key', 'unauthorized: 401'],
                            errors_info=[{'status': 401, 'code': 'auth',
                                          'details': 'token expired'}])
        self.assertFalse(hit, (hit, ev))

    def test_does_not_classify_success_or_cancellation(self):
        """成功/取消（exit 4294967295）绝不落标：载体为空则 hit=False，
        protocol_success=True 也仍由调用方另行把关（此处只测分类器本身）。"""
        self.assertFalse(self._hit(errors=[], errors_info=[])[0])
        # 模拟取消：errors 里出现 exit code 文案但没有 markers
        hit, _ = self._hit(errors=['process exited with code 4294967295 (cancelled)'])
        self.assertFalse(hit)

    def test_marker_table_excludes_ambiguous_tokens(self):
        """硬限额 markers 只保留明确文案；不含裸 'quota' 或 '429'，避免误伤。"""
        joined = '|'.join(qd._QUOTA_HARD_LIMIT_MARKERS)
        self.assertNotIn('quota|', joined + '|')  # 无裸 quota 项
        for marker in qd._QUOTA_HARD_LIMIT_MARKERS:
            self.assertNotIn('429', marker)
            self.assertNotEqual(marker.strip(), 'quota')
        # 明确包含需求 2 的关键片段
        self.assertIn('credit usage limit', qd._QUOTA_HARD_LIMIT_MARKERS)

    def test_qoder_max_pool_key_for_max_pools_only(self):
        """只有三个 Qoder Max 主力池（两内置 + CN 自定义）可承载限额标记；非 Max 模型 /
        未知 runtime / 国际自定义名一律 None，绝不偷偷给 Flash/ZCode 落标、也不把自定义
        误映射到内置池。"""
        self.assertEqual(qd._qoder_max_pool_key(qd.RUNTIME_INTERNATIONAL, 'Qwen3.8-Max'),
                         'qoder:Qwen3.8-Max')
        self.assertEqual(qd._qoder_max_pool_key(qd.RUNTIME_CN, 'Qwen3.8-Max'),
                         'qodercn:Qwen3.8-Max')
        # CN 自定义友好名（短横线）→ CN 自定义 Max 池，独立于内置池。
        self.assertEqual(qd._qoder_max_pool_key(qd.RUNTIME_CN, 'Qwen-3.8-Max'),
                         'qodercn:Qwen-3.8-Max')
        # 国际侧无自定义 Qwen-3.8-Max 池：绝不误映射为内置国际 Max。
        self.assertIsNone(qd._qoder_max_pool_key(qd.RUNTIME_INTERNATIONAL, 'Qwen-3.8-Max'))
        for model in ('Qwen3.8-Flash', 'GLM-5.3', 'DeepSeek-Flash', 'some-custom'):
            for rt in (qd.RUNTIME_INTERNATIONAL, qd.RUNTIME_CN, 'zcode', 'codebuddy'):
                with self.subTest(rt=rt, model=model):
                    self.assertIsNone(qd._qoder_max_pool_key(rt, model))


# ============================================================================
# BW-CANDIDATE-SCOPE-S2 需求 2 正式入口离线验收：本组用例**逐例调用正式 qd.main()**，
# 让它真实走完 解析 argv → 容量入口 claim → Popen 子进程 → 失败终态解析 →
# _explicit_quota_limit_hit → _qoder_max_pool_key → dp.record_main_force_limit →
# summary.main_force_limit → 终态释放（dp.finish）的整条生产链。测试端绝不复刻
# "命中→池 key→落标"的判断分支，也不 mock 被测识别器 / record_main_force_limit /
# 容量守卫；只 mock 外部 Popen（合成子进程）与可注入的 PID/时钟：
# - Popen：合成 child 真实把终态 envelope 写进 main 打开的 stdout 文件句柄，并回报
#   预设退出码；容量入口真实 bind_child 绑到这个合成 PID，communicate 结束即
#   模拟真实进程死亡，使真实终态探针能够放行释放；
# - PID：仅合成 child 的存活身份是注入的，wrapper 身份仍取本进程真实 PID/创建时刻；
# - 时钟：dp.utcnow 固定到 UTC 15:00（北京 23:00，主力时段内），避免主力时段门影响
#   落标用例；生产入口不新增 --now 开关。
# 全程临时 SQLite、临时合成 CN/国际配置与 CLI 存在性占位，绝无真实 CLI / 网络 /
# 凭据 / 模型调用 / 共享池读写。断言一律回读临时库与证据目录里的真实产物。
# ============================================================================
_CN_LIMIT_TEXT = 'Your personal Credits have been exhausted. Please check your plan.'


class _SyntheticChild:
    """合成子进程：只承担"外部进程"这一件事——把终态 envelope 原样写进 stdout 句柄，
    再按预设退出码退出。绝不参与限额判定、落标或释放逻辑。
    BW-CN-PERMISSION-STOP-S1：CN 正式 main 现经 qoder_permission_watch 与子进程交互
    （子线程 communicate + 主线程轮询 + 可能 terminate/kill），假 Popen 需补齐真实
    Popen 对象的 poll/wait/terminate/kill 表面（任务书允许的必要 fixture 调整；
    生产行为不在测试里绕开监视）。"""

    def __init__(self, pid, stdout_handle, payload, returncode, on_exit):
        self.pid = pid
        self.returncode = None
        self._stdout = stdout_handle
        self._payload = payload
        self._exit_code = returncode
        self._on_exit = on_exit
        self.terminated = False

    def communicate(self, input=None):
        if self._stdout is not None:
            self._stdout.write(self._payload)
            self._stdout.flush()
        self.returncode = self._exit_code
        self.terminated = True
        self._on_exit()  # 真实进程结束：让终态探针如实回报 dead
        return (None, None)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        # 只模拟"经本对象发出的停止信号落地"：进程立刻退出、退出码与真实 OS 终止一致非零。
        if self.returncode is None:
            self.returncode = 1
        self.terminated = True
        self._on_exit()

    def kill(self):
        self.terminate()


class MainForceLimitRealEntryTests(_NoPopen):
    CHILD_PID = 424242  # 仅代表"本入口启动过一个子进程"的可注入 PID，绝不存在于本机

    def setUp(self):
        super().setUp()
        self.store = str(self.tmp / 'pool.sqlite3')
        self.quota_store = str(self.tmp / 'quota-isolated-absent.sqlite3')
        self.night = datetime(2026, 10, 8, 15, 0, 0, tzinfo=timezone.utc)  # 北京 23:00
        self._real_utcnow = qd.dp.utcnow
        qd.dp.utcnow = lambda: self.night
        self.addCleanup(setattr, qd.dp, 'utcnow', self._real_utcnow)
        self._real_identity = qd.dp.process_identity
        qd.dp.process_identity = self._identity
        self.addCleanup(setattr, qd.dp, 'process_identity', self._real_identity)
        self._child_alive = False
        self._pending = (b'', 0)
        self.children = []

    def _identity(self, pid, platform=None):
        """只有合成 child 的存活身份是注入的；wrapper 身份仍走本进程真实查询。"""
        try:
            if int(pid) == self.CHILD_PID:
                return {'pid': pid,
                        'state': 'alive' if self._child_alive else 'dead',
                        'created': 'synthetic-child-created' if self._child_alive else None}
        except (TypeError, ValueError):
            pass
        return self._real_identity(pid, platform)

    def _fake_popen(self, argv, *args, **kwargs):
        self.calls.append(argv)
        payload, exit_code = self._pending
        child = _SyntheticChild(self.CHILD_PID, kwargs.get('stdout'), payload,
                                exit_code, self._child_died)
        self.children.append(child)
        self._child_alive = True  # 子进程已诞生：真实 bind_child 会绑到这个 PID
        return child

    def _child_died(self):
        self._child_alive = False

    # ---------- 各例隔离的运行夹具：独立 tag → 独立 workspace/配置/证据目录 ----------
    def _cn_case(self, tag, model, model_ids):
        cli = self.tmp / ('cn-cli-' + tag + '.exe')
        cli.write_text('placeholder-stub')  # 只要求存在，绝不代表真跑
        cfg_dir = self.tmp / ('cfg-' + tag)
        cfg_dir.mkdir(exist_ok=True)
        cfg = self.tmp / ('local-entry-cn-' + tag + '.json')
        cfg.write_text(json.dumps({'cli': str(cli), 'config_dir': str(cfg_dir),
                                   'model_ids': model_ids}), encoding='utf-8')
        argv, out = self._common_case(tag, ['--runtime', qd.RUNTIME_CN,
                                           '--cn-config', str(cfg)])
        argv += ['--model', model]
        return argv, out

    def _intl_case(self, tag, model):
        node = self.tmp / ('intl-node-' + tag)
        node.write_text('placeholder-stub')
        cli = self.tmp / ('intl-qodercli-' + tag + '.js')
        cli.write_text('placeholder-stub')
        cfg = self.tmp / ('local-entry-' + tag + '.json')
        cfg.write_text(json.dumps({'node': str(node), 'qodercli': str(cli)}),
                       encoding='utf-8')
        argv, out = self._common_case(tag, ['--runtime', qd.RUNTIME_INTERNATIONAL,
                                            '--config', str(cfg)])
        argv += ['--model', model]
        return argv, out

    def _common_case(self, tag, head):
        ws = self.tmp / ('ws-' + tag)
        ws.mkdir(exist_ok=True)
        pf = self.tmp / ('prompt-' + tag + '.txt')
        pf.write_text('CN 主力限额真实入口离线用例。任务ID：' + tag, encoding='utf-8')
        out = self.tmp / ('out-' + tag)
        argv = [head[0], head[1], head[2], head[3],
                 '--workspace', str(ws), '--prompt-file', str(pf),
                 '--output-dir', str(out), '--stage', 'BW-CN-LIMIT-' + tag,
                 '--task-id', 'T-' + tag, '--dispatch-store', self.store,
                 '--quota-store', self.quota_store]
        return argv, out

    def _run_main(self, argv, envelope, exit_code):
        """唯一入口：正式 qd.main()。测试只提供外部事实（argv + 子进程终态字节）。"""
        self._pending = (json.dumps(envelope, ensure_ascii=False).encode('utf-8'),
                         exit_code)
        before = len(self.calls)
        old = sys.argv
        sys.argv = ['qoder_direct.py', *argv]
        printed = io.StringIO()
        try:
            with redirect_stdout(printed):
                rc = qd.main()
        finally:
            sys.argv = old
        # 真实到达过一次外部子进程入口——不是复刻分支，也不是 0 Popen 的早退路径。
        self.assertEqual(len(self.calls) - before, 1,
                         f'main must reach Popen exactly once; calls={self.calls}')
        self.assertEqual(len(self.children), len(self.calls))
        self.assertTrue(self.children[-1].terminated, 'synthetic child must truly exit')
        return rc, json.loads(printed.getvalue())

    @staticmethod
    def _failure_envelope(errors, *, errors_info=None, permission_denials=None):
        env = {'type': 'result', 'subtype': 'error_during_execution', 'is_error': True,
               'stop_reason': 'end_turn', 'session_id': 'sess-cn-limit-case',
               'total_credits': 0, 'modelUsage': {}, 'errors': errors}
        if errors_info is not None:
            env['errors_info'] = errors_info
        if permission_denials is not None:
            env['permission_denials'] = permission_denials
        return env

    @staticmethod
    def _success_envelope(result_text):
        return {'type': 'result', 'subtype': 'success', 'is_error': False,
                'stop_reason': 'end_turn', 'session_id': 'sess-cn-limit-ok',
                'total_credits': 0, 'modelUsage': {}, 'errors': [],
                'errors_info': [], 'permission_denials': [], 'result': result_text}

    def _summary(self, out):
        return json.loads((out / 'summary.json').read_text(encoding='utf-8'))

    def _attempt_by_task(self, task_id):
        with closing(qd.dp.connect(self.store)) as conn:
            row = conn.execute('SELECT * FROM attempts WHERE task_id=?',
                               (task_id,)).fetchone()
            return dict(row) if row is not None else None

    def _limited_pools(self):
        return sorted(pk for pk, row in qd.dp.main_force_limits(self.store).items()
                      if int(row['limited']) == 1)

    # ---------- 1. CN 内置 Max 失败终态：只落它自己，且经真实 claim/bind/record/release ----------
    def test_main_records_builtin_cn_max_only_through_real_chain(self):
        argv, out = self._cn_case('builtin-max', 'Qwen3.8-Max',
                                  {'DeepSeek-Flash': 'real-id-123'})
        rc, printed = self._run_main(
            argv, self._failure_envelope([_CN_LIMIT_TEXT],
                                         permission_denials=[{'tool': 'Edit',
                                                              'reason': 'user denied tool Edit'}]),
            exit_code=1)
        self.assertEqual(rc, 3, rc)
        summary = self._summary(out)
        self.assertEqual(summary, printed)
        self.assertFalse(summary['protocol_success'])
        # 原错误必须原样保留，落标不得消去任何审批拒绝。
        self.assertEqual(summary['result_errors'], [_CN_LIMIT_TEXT])
        self.assertEqual(summary['permission_denials'],
                         [{'tool': 'Edit', 'reason': 'user denied tool Edit'}])
        stdout_bytes = (out / 'stdout.json').read_bytes()
        self.assertIn(_CN_LIMIT_TEXT.encode('utf-8'), stdout_bytes)
        self.assertIn(_CN_LIMIT_TEXT.encode('utf-8'),
                      (out / 'summary.json').read_bytes())

        rec = summary['main_force_limit']
        self.assertTrue(rec['recorded'], rec)
        self.assertEqual(rec['pool_key'], 'qodercn:Qwen3.8-Max')
        self.assertEqual(rec['evidence'], _CN_LIMIT_TEXT)

        limits = qd.dp.main_force_limits(self.store)
        self.assertEqual(self._limited_pools(), ['qodercn:Qwen3.8-Max'])
        row = limits['qodercn:Qwen3.8-Max']
        # 绑定必须指向真实存在的 attempt / task / 证据文件与哈希，绝不凭空造标记。
        self.assertEqual(row['task_id'], 'T-builtin-max')
        attempt = self._attempt_by_task('T-builtin-max')
        self.assertIsNotNone(attempt)
        self.assertEqual(row['attempt_token'], attempt['token'])
        self.assertEqual(attempt['pool_key'], 'qodercn:Qwen3.8-Max')
        self.assertEqual(attempt['runtime'], qd.RUNTIME_CN)
        self.assertEqual(attempt['model'], 'Qwen3.8-Max')
        # 生产侧保存的是 .resolve() 后的长路径，而 tempfile 在 Windows 上可能给出
        # 8.3 短名别名（ADMINI~1），二者是同一真实文件；故两边同 resolve 后仍严格相等，
        # 并用 samefile 复核同一 inode/句柄，不放宽为结尾匹配。
        evidence_path = Path(row['evidence_path'])
        real_path = out / 'stdout.json'
        self.assertEqual(evidence_path.resolve(), real_path.resolve())
        self.assertTrue(os.path.samefile(str(evidence_path), str(real_path)))
        self.assertEqual(row['evidence_sha256'],
                         hashlib.sha256(stdout_bytes).hexdigest())
        self.assertEqual(rec['evidence_sha256'], row['evidence_sha256'])

        # 真实终态释放：入口自己结算自己那份占位，绝不泄漏、绝不误抢别人。
        self.assertEqual(attempt['state'], 'finished')
        self.assertEqual(attempt['terminal'], 'finished')
        self.assertEqual(attempt['child_pid'], self.CHILD_PID)
        self.assertEqual(attempt['wrapper_pid'], os.getpid())
        self.assertTrue(summary['capacity_released'], summary)
        self.assertEqual(summary['capacity_terminal'], 'finished')
        self.assertEqual(
            qd.dp.status(self.store, now=self.night)['pools']['qodercn:Qwen3.8-Max']
            ['active'], 0)

        # 不连坐：CN 自定义 Max / 国际内置 Max 一律未被波及。
        self.assertFalse(qd.dp.main_force_limited(self.store, 'qodercn:Qwen-3.8-Max'))
        self.assertFalse(qd.dp.main_force_limited(self.store, 'qoder:Qwen3.8-Max'))
        self.assertNotIn('qodercn:Qwen3.8-Flash', self._limited_pools())
        self.assertNotIn('qoder:Qwen3.8-Flash', self._limited_pools())

    # ---------- 2. CN 自定义 Max 失败：只落自己；内置 Max 随后仍能真实 claim 到 Popen ----------
    def test_main_records_custom_cn_max_without_touching_builtin(self):
        # 自定义友好名 Qwen-3.8-Max 与内置 Qwen3.8-Max 的真实 config 映射口径不同：
        # 前者必须经 model_ids 映射成服务端 ID 才能下发，本例据此走独立池落标。
        # BW-QODERCN-TOKEN-MAX-DISABLE-20261028：目标池现行被用户禁用，无 claim 的正式
        # CN 入口在读 CN 配置/模型映射之前即 mode_excluded 拒绝；「自定义 Max 失败只落
        # 自己标」的落标机理属禁用前历史口径，这里临时用空禁用集复现该历史场景
        # （真实禁用期间该池不可能再产生这类新失败）。
        with user_disabled_pools(frozenset()):
            argv, out = self._cn_case('custom-max', 'Qwen-3.8-Max',
                                      {'Qwen-3.8-Max': 'synthetic-cn-custom-max-real-id'})
            rc, printed = self._run_main(
                argv, self._failure_envelope([_CN_LIMIT_TEXT]), exit_code=1)
            self.assertEqual(rc, 3, rc)
            self.assertEqual(self._limited_pools(), ['qodercn:Qwen-3.8-Max'])
            summary = self._summary(out)
            self.assertEqual(summary['main_force_limit']['pool_key'],
                             'qodercn:Qwen-3.8-Max')
            attempt = self._attempt_by_task('T-custom-max')
            self.assertEqual(attempt['pool_key'], 'qodercn:Qwen-3.8-Max')
            self.assertEqual(attempt['state'], 'finished')
            self.assertFalse(qd.dp.main_force_limited(self.store, 'qodercn:Qwen3.8-Max'))

        # 反证"不波及内置"：同一库、同一时段下，内置 Max 随后仍被真实容量门放行到 Popen，
        # 且它这次的非限额终态绝不新增任何标记（自定义标记也不得扩大到内置）。
        before_limited = list(self._limited_pools())
        argv2, out2 = self._cn_case('builtin-after-custom', 'Qwen3.8-Max',
                                    {'DeepSeek-Flash': 'real-id-123'})
        rc2, _printed2 = self._run_main(
            argv2, self._failure_envelope(['No permission client configured for Bash'],
                                          permission_denials=[{'tool': 'Bash',
                                                                'reason': 'denied'}]),
            exit_code=1)
        self.assertEqual(rc2, 3, rc2)
        self.assertTrue((out2 / 'stdout.json').exists(),
                        '内置 Max 必须真的跑到子进程并留下终态证据')
        self.assertEqual(len(self.calls), 2, self.calls)
        self.assertEqual(self._limited_pools(), before_limited)
        self.assertNotIn('main_force_limit', self._summary(out2))

    # ---------- 3. 国际内置 Max 失败：只落国际自己那一个池 ----------
    def test_main_records_international_max_only(self):
        argv, out = self._intl_case('intl-max', 'Qwen3.8-Max')
        rc, _printed = self._run_main(argv, self._failure_envelope([_CN_LIMIT_TEXT]),
                                      exit_code=1)
        self.assertEqual(rc, 3, rc)
        self.assertEqual(self._limited_pools(), ['qoder:Qwen3.8-Max'])
        self.assertFalse(qd.dp.main_force_limited(self.store, 'qodercn:Qwen3.8-Max'))
        self.assertFalse(qd.dp.main_force_limited(self.store, 'qodercn:Qwen-3.8-Max'))
        attempt = self._attempt_by_task('T-intl-max')
        self.assertEqual(attempt['pool_key'], 'qoder:Qwen3.8-Max')
        self.assertEqual(attempt['runtime'], qd.RUNTIME_INTERNATIONAL)
        self.assertEqual(attempt['state'], 'finished')
        self.assertTrue(self._summary(out)['capacity_released'])

    # ---------- 4. 成功正文含该句但 errors=[]：绝不因正文/未绑定改成硬限额 ----------
    def test_main_success_body_mentioning_limit_text_is_never_recorded(self):
        body = ('9. 建议下一步：继续。\n\n' + _CN_LIMIT_TEXT +
                '\n（以上为正文里出现的一句话，不是错误载体）')
        argv, out = self._cn_case('success-text', 'Qwen3.8-Max',
                                  {'DeepSeek-Flash': 'real-id-123'})
        rc, summary = self._run_main(argv, self._success_envelope(body), exit_code=0)
        self.assertEqual(rc, 0, rc)
        self.assertTrue(summary['protocol_success'])
        self.assertEqual(summary['result_errors'], [])
        # 报告未绑定也绝不能被升级成硬限额。
        self.assertFalse(summary['report_bound'], summary)
        self.assertNotIn('main_force_limit', summary)
        self.assertEqual(self._limited_pools(), [])
        self.assertIn(_CN_LIMIT_TEXT.encode('utf-8'), (out / 'response.md').read_bytes())
        self.assertEqual(self._attempt_by_task('T-success-text')['state'], 'finished')

    # ---------- 5. permission / auth / 取消终态（无限额文本）一律不落标 ----------
    def test_main_non_quota_terminals_never_record(self):
        cases = (
            ('permission', ['No permission client configured for Bash'],
             [{'tool': 'Edit', 'reason': 'user denied tool Edit'}], None, 1),
            ('auth', ['invalid api key', 'unauthorized: 401'], None,
             [{'status': 401, 'code': 'auth', 'details': 'token expired'}], 1),
            ('cancelled', ['process exited with code 4294967295 (cancelled)'], None,
             None, 4294967295))
        for tag, errors, denials, errors_info, exit_code in cases:
            with self.subTest(tag=tag):
                argv, out = self._cn_case('terminal-' + tag, 'Qwen3.8-Max',
                                          {'DeepSeek-Flash': 'real-id-123'})
                rc, _printed = self._run_main(
                    argv, self._failure_envelope(errors, permission_denials=denials,
                                                 errors_info=errors_info),
                    exit_code=exit_code)
                self.assertEqual(rc, 3, rc)
                summary = self._summary(out)
                self.assertNotIn('main_force_limit', summary)
                self.assertEqual(summary['result_errors'], errors)  # 原错误仍保存
                self.assertEqual(self._limited_pools(), [])
                self.assertTrue(summary['capacity_released'], summary)

    # ---------- 6. Flash 终态即使带真实限额文本也永不落标（池 key 恒 None） ----------
    def test_main_flash_quota_text_is_never_recorded(self):
        argv, out = self._cn_case('flash', 'Qwen3.8-Flash',
                                  {'DeepSeek-Flash': 'real-id-123'})
        rc, summary = self._run_main(argv, self._failure_envelope([_CN_LIMIT_TEXT]),
                                     exit_code=1)
        self.assertEqual(rc, 3, rc)
        self.assertNotIn('main_force_limit', summary)
        self.assertEqual(self._limited_pools(), [])
        self.assertEqual(summary['result_errors'], [_CN_LIMIT_TEXT])
        self.assertTrue(qd.dp.main_force_limited(self.store, 'qodercn:Qwen3.8-Max')
                        is False)
        self.assertEqual(self._attempt_by_task('T-flash')['pool_key'],
                         'qodercn:Qwen3.8-Flash')


if __name__ == '__main__':
    unittest.main(verbosity=2)
