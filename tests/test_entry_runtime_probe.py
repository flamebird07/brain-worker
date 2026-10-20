"""entry_runtime_probe 离线回归（BW-UNIFIED-DESKTOP-ENTRY-20261020 R1/R1G）。

只读三态探针 + 用户 2026-10-20 授权升级的硬资格门：present / missing / unknown 三态
各自可造（含 claude 的 cli.js 缺失事故形态——加载器过而运行时不在）；同 runtime 多池
只探一次；探针任何异常内部消化为 unknown 绝不外抛；**确凿 missing 挡**（过滤原因
entry_runtime_missing、fixed 点名死通道诚实拒绝、全缺如实 no_eligible_candidate）、
**unknown 与探针崩溃绝不挡**（fail-open，selected 与全 present 基线一致）；早期拒绝的
回执不含可见性字段（纯附加 schema）；探针绝不读取 credential_source、绝不调用
claude_direct.resolve_token()。全部离线：临时目录配置 + mock 各 *_direct 模块的
DEFAULT_CONFIG / OPENCODE_EXE，绝不真实运行时、凭据、子进程或共享库。
"""
import json
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'scripts'
for p in (str(SCRIPTS), str(REPO / 'tests')):
    if p not in sys.path:
        sys.path.insert(0, p)

import claude_direct       # noqa: E402
import codebuddy_direct    # noqa: E402
import dispatch_pool as dp  # noqa: E402
import entry_runtime_probe as erp  # noqa: E402
import opencode_direct     # noqa: E402
import qoder_direct as qd  # noqa: E402
import stage_selection as ss  # noqa: E402
import zcode_direct        # noqa: E402

T_NOW = datetime(2026, 10, 20, 6, 0, 0, tzinfo=timezone.utc)  # 北京 14:00，Flash 无时段门


def _touch(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'probe-stub')
    return str(path.resolve())


class _CfgBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='bw-erp-')
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._patches = []
        for module in (claude_direct, codebuddy_direct, opencode_direct, qd, zcode_direct):
            # 全部指到不存在的临时路径：未被本用例覆写的 runtime 一律缺失态，
            # 绝不读取仓库/引擎目录里的任何真实配置。
            self._patches.append(unittest.mock.patch.object(
                module, 'DEFAULT_CONFIG', self.base / f'absent-{module.__name__}.json'))
        # qodercn 用独立的 DEFAULT_CN_CONFIG，同样隔离（安装版引擎里有真实配置）。
        self._patches.append(unittest.mock.patch.object(
            qd, 'DEFAULT_CN_CONFIG', self.base / 'absent-local-entry-cn.json'))
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _write_cfg(self, name, payload) -> Path:
        p = self.base / name
        p.write_text(json.dumps(payload), encoding='utf-8')
        return p

    def _patch_default(self, module, path, attr='DEFAULT_CONFIG'):
        patch = unittest.mock.patch.object(module, attr, path)
        patch.start()
        self.addCleanup(patch.stop)
        return patch


class CodeBuddyProbeTests(_CfgBase):
    def test_present(self):
        cfg = self._write_cfg('cb.json', {'node': _touch(self.base / 'rt/node.exe'),
                                          'cli': _touch(self.base / 'rt/cli.js')})
        self._patch_default(codebuddy_direct, cfg)
        self.assertEqual(erp.probe_runtime('codebuddy'), erp.PRESENT)

    def test_missing_pointed_file(self):
        cfg = self._write_cfg('cb.json', {'node': _touch(self.base / 'rt/node.exe'),
                                          'cli': str(self.base / 'rt/gone.js')})
        self._patch_default(codebuddy_direct, cfg)
        self.assertEqual(erp.probe_runtime('codebuddy'), erp.MISSING)

    def test_unknown_when_config_absent(self):
        # R1G 判定边界：入口配置本身不存在 = 未配置（入口还支持 --config 显式传参），
        # 绝不判 missing——否则裸仓库/CI 里硬门会清场全部候选。
        self._patch_default(codebuddy_direct, self.base / 'nope.json')
        self.assertEqual(erp.probe_runtime('codebuddy'), erp.UNKNOWN)

    def test_unknown_malformed_json(self):
        p = self.base / 'cb-bad.json'
        p.write_text('{not json', encoding='utf-8')
        self._patch_default(codebuddy_direct, p)
        self.assertEqual(erp.probe_runtime('codebuddy'), erp.UNKNOWN)


class QoderProbeTests(_CfgBase):
    def test_international_present(self):
        cfg = self._write_cfg('q.json', {'node': _touch(self.base / 'node.exe'),
                                         'qodercli': _touch(self.base / 'qodercli.js')})
        self._patch_default(qd, cfg)
        self.assertEqual(erp.probe_runtime('qoder'), erp.PRESENT)

    def test_international_missing(self):
        cfg = self._write_cfg('q.json', {'node': _touch(self.base / 'node.exe'),
                                         'qodercli': str(self.base / 'gone.js')})
        self._patch_default(qd, cfg)
        self.assertEqual(erp.probe_runtime('qoder'), erp.MISSING)

    def test_cn_present(self):
        cfg = self._write_cfg('qcn.json', {'cli': _touch(self.base / 'qcn.exe'),
                                           'config_dir': str(self.base),
                                           'model_ids': {'DeepSeek-Flash': 'ds-flash-id'}})
        self._patch_default(qd, cfg, attr='DEFAULT_CN_CONFIG')
        self.assertEqual(erp.probe_runtime('qodercn'), erp.PRESENT)

    def test_cn_missing_cli(self):
        cfg = self._write_cfg('qcn.json', {'cli': str(self.base / 'gone.exe'),
                                           'config_dir': str(self.base),
                                           'model_ids': {'X': 'id'}})
        self._patch_default(qd, cfg, attr='DEFAULT_CN_CONFIG')
        self.assertEqual(erp.probe_runtime('qodercn'), erp.MISSING)

    def test_cn_unknown_empty_model_ids(self):
        cfg = self._write_cfg('qcn.json', {'cli': _touch(self.base / 'qcn.exe'),
                                           'config_dir': str(self.base),
                                           'model_ids': {}})
        self._patch_default(qd, cfg, attr='DEFAULT_CN_CONFIG')
        self.assertEqual(erp.probe_runtime('qodercn'), erp.UNKNOWN)


class ZCodeProbeTests(_CfgBase):
    # CONFIG_FILE_KEYS 共 5 键（node/bootstrap/tsx_loader/builtin_provider_config/
    # personal_provider_config），加载器逐个要求绝对且存在。
    def test_present(self):
        cfg = self._write_cfg('z.json', {
            'node': _touch(self.base / 'node.exe'),
            'bootstrap': _touch(self.base / 'bootstrap.js'),
            'tsx_loader': _touch(self.base / 'tsx.js'),
            'builtin_provider_config': _touch(self.base / 'builtin.json'),
            'personal_provider_config': _touch(self.base / 'personal.json'),
            'runner': _touch(self.base / 'runner.mjs')})
        self._patch_default(zcode_direct, cfg)
        self.assertEqual(erp.probe_runtime('zcode'), erp.PRESENT)

    def test_missing_runner(self):
        cfg = self._write_cfg('z.json', {
            'node': _touch(self.base / 'node.exe'),
            'bootstrap': _touch(self.base / 'bootstrap.js'),
            'tsx_loader': _touch(self.base / 'tsx.js'),
            'builtin_provider_config': _touch(self.base / 'builtin.json'),
            'personal_provider_config': _touch(self.base / 'personal.json'),
            'runner': str(self.base / 'gone.mjs')})
        self._patch_default(zcode_direct, cfg)
        self.assertEqual(erp.probe_runtime('zcode'), erp.MISSING)


class ClaudeProbeTests(_CfgBase):
    def test_present(self):
        cfg = self._write_cfg('cl.json', {
            'executable': _touch(self.base / 'node.exe'),
            'launcher_args': [_touch(self.base / 'rt/cli.js')]})
        self._patch_default(claude_direct, cfg)
        self.assertEqual(erp.probe_runtime('claude'), erp.PRESENT)

    def test_incident_launcher_cli_missing(self):
        # 本轮真实事故形态：配置 JSON 完好（加载器只查 JSON 对象），
        # 但 launcher_args 里的 cli.js 整树被清 → 必须 missing，绝不能误报 present。
        cfg = self._write_cfg('cl.json', {
            'executable': _touch(self.base / 'node.exe'),
            'launcher_args': [str(self.base / 'rt/@anthropic-ai/claude-code/cli.js')]})
        self._patch_default(claude_direct, cfg)
        self.assertEqual(erp.probe_runtime('claude'), erp.MISSING)

    def test_unknown_missing_executable_key(self):
        cfg = self._write_cfg('cl.json', {'launcher_args': [_touch(self.base / 'cli.js')]})
        self._patch_default(claude_direct, cfg)
        self.assertEqual(erp.probe_runtime('claude'), erp.UNKNOWN)

    def test_unknown_malformed_launcher_args(self):
        cfg = self._write_cfg('cl.json', {'executable': _touch(self.base / 'node.exe'),
                                          'launcher_args': 'cli.js'})
        self._patch_default(claude_direct, cfg)
        self.assertEqual(erp.probe_runtime('claude'), erp.UNKNOWN)

    def test_never_touches_credentials(self):
        # 探针只读路径字段：配置里即使带着 credential_source，也绝不触发
        # resolve_token（真触发即 AssertionError 当场失败）。
        cfg = self._write_cfg('cl.json', {
            'executable': _touch(self.base / 'node.exe'),
            'launcher_args': [str(self.base / 'gone-cli.js')],
            'credential_source': {'mode': 'token_env', 'env': 'ANTHROPIC_API_KEY'}})
        self._patch_default(claude_direct, cfg)
        with unittest.mock.patch.object(
                claude_direct, 'resolve_token',
                side_effect=AssertionError('probe must never resolve credentials')):
            self.assertEqual(erp.probe_runtime('claude'), erp.MISSING)


class OpenCodeProbeTests(_CfgBase):
    def test_present_configured(self):
        cfg = self._write_cfg('oc.json', {'executable': _touch(self.base / 'opencode.exe')})
        self._patch_default(opencode_direct, cfg)
        self.assertEqual(erp.probe_runtime('opencode'), erp.PRESENT)

    def test_missing_configured(self):
        cfg = self._write_cfg('oc.json', {'executable': str(self.base / 'gone.exe')})
        self._patch_default(opencode_direct, cfg)
        self.assertEqual(erp.probe_runtime('opencode'), erp.MISSING)

    def test_unknown_non_string_executable(self):
        cfg = self._write_cfg('oc.json', {'executable': 5})
        self._patch_default(opencode_direct, cfg)
        self.assertEqual(erp.probe_runtime('opencode'), erp.UNKNOWN)

    def test_path_discovery_via_opencode_exe(self):
        # 配置缺省（{} 走发现链）时再查 OPENCODE_EXE → PATH；环境性解析成败都
        # 不是确凿 missing（R1G 判定边界：只有配置显式绝对路径缺失才 missing）。
        self._patch_default(opencode_direct, self.base / 'absent-opencode.json')
        with unittest.mock.patch.dict('os.environ', {'OPENCODE_EXE': _touch(self.base / 'bin/opencode.exe')}):
            self.assertEqual(erp.probe_runtime('opencode'), erp.PRESENT)
        with unittest.mock.patch.dict('os.environ', {'OPENCODE_EXE': str(self.base / 'bin/gone.exe')}):
            self.assertEqual(erp.probe_runtime('opencode'), erp.UNKNOWN)


class ProbeBehaviourTests(_CfgBase):
    def test_unknown_runtime_name_reports_unknown(self):
        self.assertEqual(erp.probe_runtime('future-runtime'), erp.UNKNOWN)

    def test_probe_exception_becomes_unknown(self):
        def boom():
            raise RuntimeError('probe crashed')
        with unittest.mock.patch.dict(erp.RUNTIME_PROBES, {'qoder': boom}):
            self.assertEqual(erp.probe_runtime('qoder'), erp.UNKNOWN)

    def test_same_runtime_probed_once_across_pools(self):
        calls = []

        def counting():
            calls.append(1)
            return erp.MISSING

        with unittest.mock.patch.dict(erp.RUNTIME_PROBES, {'codebuddy': counting}):
            status, missing = erp.probe_entry_runtimes(
                ['codebuddy:deepseek-v4.1-flash', 'codebuddy:glm-5.3-flash'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(status, {'codebuddy:deepseek-v4.1-flash': erp.MISSING,
                                  'codebuddy:glm-5.3-flash': erp.MISSING})
        self.assertEqual(missing, ['codebuddy:deepseek-v4.1-flash', 'codebuddy:glm-5.3-flash'])

    def test_missing_list_only_collects_missing_in_order(self):
        stub = {'qoder': lambda: erp.PRESENT, 'qodercn': lambda: erp.MISSING,
                'claude': lambda: erp.UNKNOWN}
        with unittest.mock.patch.dict(erp.RUNTIME_PROBES, stub):
            status, missing = erp.probe_entry_runtimes(
                ['qoder:Qwen3.8-Flash', 'qodercn:Qwen3.8-Flash', 'claude:deepseek-flash'])
        self.assertEqual(status, {'qoder:Qwen3.8-Flash': erp.PRESENT,
                                  'qodercn:Qwen3.8-Flash': erp.MISSING,
                                  'claude:deepseek-flash': erp.UNKNOWN})
        self.assertEqual(missing, ['qodercn:Qwen3.8-Flash'])


class StageSelectionIntegrationTests(unittest.TestCase):
    """R1G（用户 2026-10-20 授权升级）：确凿 missing 挡（entry_runtime_missing）、
    unknown 与探针崩溃绝不挡（fail-open）、fixed 点名死通道诚实拒绝、全缺如实拒绝；
    回执可见性字段与硬门同源同一次探测。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='bw-erp-ss-')
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.store = str(self.base / 'dispatch.sqlite3')
        with closing(dp.connect(self.store)) as conn:
            dp._rotation_row(conn)
            conn.execute('PRAGMA journal_mode=DELETE')
        ws = self.base / 'ws'
        ws.mkdir()
        self.workspace = str(ws)

    def _plan(self, mode='auto', fixed=None):
        auth = {'selection_mode': mode, 'quote': '探针硬门回归',
                'authorization_scope': {'task_id': 'T-ERP', 'stage': 's1'},
                'allowed_candidates': ['qoder:Qwen3.8-Flash', 'qodercn:Qwen3.8-Flash']}
        if mode == 'fixed':
            auth['user_direct_selection'] = True
            auth['runtime'], auth['model'] = fixed
        return ss.plan_stage_selection(auth, store_path=self.store, task_id='T-ERP',
                                       stage='s1', workspace=self.workspace, now=T_NOW)

    def _probe(self, statuses):
        missing = [pk for pk, s in statuses.items() if s == erp.MISSING]
        return unittest.mock.patch.object(erp, 'probe_entry_runtimes',
                                          return_value=(statuses, missing))

    def test_receipt_carries_visibility_fields(self):
        with self._probe({'qoder:Qwen3.8-Flash': erp.PRESENT,
                          'qodercn:Qwen3.8-Flash': erp.PRESENT}):
            receipt = self._plan()
        self.assertIsNone(receipt.get('refused'))
        self.assertEqual(set(receipt['entry_runtime_status']),
                         {'qoder:Qwen3.8-Flash', 'qodercn:Qwen3.8-Flash'})
        self.assertEqual(receipt['entry_runtime_missing'], [])

    def test_gate_excludes_definitive_missing(self):
        with self._probe({'qoder:Qwen3.8-Flash': erp.MISSING,
                          'qodercn:Qwen3.8-Flash': erp.PRESENT}):
            receipt = self._plan()
        self.assertIsNone(receipt.get('refused'))
        self.assertEqual(receipt['selected']['pool_key'], 'qodercn:Qwen3.8-Flash')
        excluded = {row['pool_key']: row['reason']
                    for row in receipt['excluded_candidates_with_reason']}
        self.assertEqual(excluded.get('qoder:Qwen3.8-Flash'), 'entry_runtime_missing')
        self.assertEqual(receipt['entry_runtime_missing'], ['qoder:Qwen3.8-Flash'])

    def test_unknown_and_probe_crash_never_gate(self):
        outcomes = []
        for stub in ({'qoder:Qwen3.8-Flash': erp.PRESENT,
                      'qodercn:Qwen3.8-Flash': erp.PRESENT},
                     {'qoder:Qwen3.8-Flash': erp.UNKNOWN,
                      'qodercn:Qwen3.8-Flash': erp.UNKNOWN},
                     RuntimeError('probe crashed')):
            patcher = (unittest.mock.patch.object(erp, 'probe_entry_runtimes',
                                                  side_effect=stub)
                       if isinstance(stub, Exception) else self._probe(stub))
            with patcher:
                receipt = self._plan()
            self.assertIsNone(receipt.get('refused'))
            self.assertEqual(receipt['selected']['pool_key'], 'qoder:Qwen3.8-Flash')
            outcomes.append(receipt['entry_runtime_status'])
        self.assertEqual(outcomes[1], {'qoder:Qwen3.8-Flash': erp.UNKNOWN,
                                       'qodercn:Qwen3.8-Flash': erp.UNKNOWN})
        self.assertEqual(outcomes[2], outcomes[1])  # 崩溃降级 unknown，fail-open

    def test_fixed_to_dead_runtime_refuses_honestly(self):
        with self._probe({'qoder:Qwen3.8-Flash': erp.MISSING,
                          'qodercn:Qwen3.8-Flash': erp.PRESENT}):
            receipt = self._plan(mode='fixed', fixed=('qoder', 'Qwen3.8-Flash'))
        self.assertEqual(receipt['refused'], 'fixed_candidate_unavailable')
        self.assertEqual(receipt['refused_detail'], 'entry_runtime_missing')

    def test_all_missing_refuses_no_eligible(self):
        with self._probe({'qoder:Qwen3.8-Flash': erp.MISSING,
                          'qodercn:Qwen3.8-Flash': erp.MISSING}):
            receipt = self._plan()
        self.assertEqual(receipt['refused'], 'no_eligible_candidate')
        excluded = {row['pool_key']: row['reason']
                    for row in receipt['excluded_candidates_with_reason']}
        self.assertEqual(excluded, {'qoder:Qwen3.8-Flash': 'entry_runtime_missing',
                                    'qodercn:Qwen3.8-Flash': 'entry_runtime_missing'})

    def test_early_refusal_receipt_lacks_visibility_fields(self):
        receipt = ss.plan_stage_selection(None, store_path=self.store, task_id='T-ERP',
                                          stage='s1', workspace=self.workspace, now=T_NOW)
        self.assertEqual(receipt['refused'], 'missing_authorization')
        self.assertNotIn('entry_runtime_status', receipt)
        self.assertNotIn('entry_runtime_missing', receipt)


if __name__ == '__main__':
    unittest.main()
