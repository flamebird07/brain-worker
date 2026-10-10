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

绝不读写真实/共享池；全程临时目录 + 假 Popen。真实 CN 任务未实际运行，成本/后端
身份未知（见报告）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
_SCRIPTS = HERE.parent / 'scripts'
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


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


if __name__ == '__main__':
    unittest.main(verbosity=2)
