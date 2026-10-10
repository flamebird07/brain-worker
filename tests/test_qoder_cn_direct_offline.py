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
               'model_ids': {'DeepSeek-Flash': 'real-id-123'}}
        self.assertEqual(qd.resolve_cn_model_id(cfg, 'DeepSeek-Flash'), 'real-id-123')
        self.assertIsNone(qd.resolve_cn_model_id(cfg, 'Qwen3.8-Max'))


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

    def test_unmapped_default_model_zero_popen(self):
        # 忘传 --model 时缺省是国际 Qwen3.8-Max，CN 配置未映射该名 → 拒绝、0 Popen。
        cfg = self.write_cn_config({'DeepSeek-Flash': 'real-id-123'})
        ws, pf = self.make_prompt()
        out_dir = self.tmp / 'out-default'
        rc = self._run_main([
            '--runtime', 'qodercn', '--cn-config', str(cfg),
            '--workspace', str(ws), '--prompt-file', str(pf),
            '--output-dir', str(out_dir), '--stage', 'BW-CN-TEST'])
        self.assertEqual(rc, 2, rc)
        self.assertEqual(self.calls, [])
        self.assertFalse(out_dir.exists())

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


if __name__ == '__main__':
    unittest.main(verbosity=2)
