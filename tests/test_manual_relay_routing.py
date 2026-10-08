"""人工转交退休策略的运行级回归（非文案匹配）：

1) 生产 CLI `scripts/codebuddy_direct.py` 的 main() 在读配置/提示词、建输出、quota
   gate、Popen 之前对**任何**新直连固定拒绝：无 plan、带 plan、resume、probe、配置
   存在/缺失一律返回 sent=false、status=manual_relay_only、非成功退出码，且零输出目录、
   零 sqlite 额度副作用、零子进程。
2) 控制面 `execution_control.preflight` 对 CodeBuddy/WorkBuddy 计划（同时检查 plan 与
   actual 的真实 runtime）明确拒绝，保留 sent=false；即便计划其余字段完全匹配也拒。
   Qoder/ZCode 的正常派发不受影响（matching plan → ok=True）。

全部离线：只用 sys.executable 跑本地脚本与内存中的 dict，不触网、不调真实模型/账号、
不写真实状态目录。"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import execution_control as ec  # noqa: E402

CODEBUDDY_ENTRY = SCRIPTS / 'codebuddy_direct.py'


def _grants(rules=('Read',)):
    return ec.grants_from_rules(list(rules), [], [], list(rules))


def _plan(runtime, ws, prompt_sha):
    g = _grants()
    return {'task_id': 'T-MR', 'stage': 'BW-MANUAL-RELAY', 'runtime': runtime,
            'model': 'm', 'workspace': str(ws), 'cwd': str(ws),
            'prompt_sha256': prompt_sha,
            'grants': {'edits': g['edits'], 'bash': g['bash'], 'read_dirs': g['read_dirs']},
            'tool_visibility': g['tool_visibility'], 'visible_tools': g['visible_tools'],
            'allowed_tools': g['allowed_tools'], 'disallowed_tools': g['disallowed_tools'],
            'active_tasks': [], 'depends_on': [], 'shared_writes': [],
            'max_concurrency': 1, 'isolation': 'independent_workspace'}


def _actual(runtime, ws, prompt_sha):
    g = _grants()
    return {'task_id': 'T-MR', 'stage': 'BW-MANUAL-RELAY', 'runtime': runtime,
            'model': 'm', 'workspace': str(ws), 'cwd': str(ws),
            'prompt_sha256': prompt_sha, 'argv': ['node', 'cli', '-p'], 'shell': False,
            'grants': g}


class ProductionCliRefusal(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.ws = self.tmp / 'ws'
        self.ws.mkdir()
        self.quota_store = self.tmp / 'store' / 'state.sqlite3'

    def tearDown(self):
        self._tmp.cleanup()

    def _assert_refused(self, extra_args, cfg_path):
        out = self.tmp / ('out-' + hashlib.sha1(
            repr(extra_args).encode('utf-8')).hexdigest()[:12])
        prompt = self.tmp / 'no-such-prompt.txt'  # 故意不存在：证明未读提示词
        cmd = [sys.executable, str(CODEBUDDY_ENTRY), '--workspace', str(self.ws),
               '--prompt-file', str(prompt), '--output-dir', str(out),
               '--stage', 'BW-MANUAL-RELAY', '--model', 'CB-M',
               '--config', str(cfg_path),
               '--quota-store', str(self.quota_store), *extra_args]
        proc = subprocess.run(cmd, capture_output=True, cwd=str(self.tmp),
                              env={**os.environ, 'PYTHONIOENCODING': 'utf-8'}, timeout=60)
        text = proc.stdout.decode('utf-8', 'replace')
        self.assertEqual(proc.returncode, 2, text + proc.stderr.decode('utf-8', 'replace'))
        payload = json.loads(text)
        self.assertTrue(payload['manual_relay_only'])
        self.assertEqual(payload['status'], 'manual_relay_only')
        self.assertFalse(payload['sent'])
        # 零副作用：未建输出目录、未创建额度 sqlite、未落到流水线。
        self.assertFalse(out.exists())
        self.assertFalse(self.quota_store.exists())
        return payload

    def test_refuses_no_plan_and_missing_config(self):
        # 配置缺失也不能绕过：仍走 manual_relay，而非 preflight_error（证明未读配置）。
        self._assert_refused([], self.tmp / 'no-such-config.json')

    def test_refuses_with_existing_config(self):
        cfg = self.tmp / 'cfg.json'
        cfg.write_text(json.dumps({'node': sys.executable,
                                   'cli': str(self.tmp / 'cb-real-runtime-must-not-run.exe')}),
                       encoding='utf-8')
        self._assert_refused([], cfg)

    def test_refuses_with_dispatch_plan(self):
        prompt = self.tmp / 'plan-prompt.txt'
        prompt.write_text('任务\n', encoding='utf-8', newline='\n')
        plan = self.tmp / 'plan.json'
        plan.write_text(json.dumps(_plan('codebuddy', self.ws,
                                         hashlib.sha256(prompt.read_bytes()).hexdigest())),
                        encoding='utf-8', newline='\n')
        cfg = self.tmp / 'cfg2.json'
        cfg.write_text(json.dumps({'node': sys.executable,
                                   'cli': str(self.tmp / 'cb-stub-never-run.py')}), encoding='utf-8')
        self._assert_refused(['--dispatch-plan', str(plan)], cfg)

    def test_refuses_resume(self):
        self._assert_refused(['--resume-session-id', 'S-existing'],
                             self.tmp / 'no-such-config.json')

    def test_refuses_recovery_probe(self):
        self._assert_refused(['--quota-recovery-probe', '--tools', 'Read'],
                             self.tmp / 'no-such-config.json')


class ControlPlaneRefusal(unittest.TestCase):
    SHA = 'a' * 64

    def test_codebuddy_matching_plan_still_refused(self):
        r = ec.preflight(_plan('codebuddy', 'C:\\ws', self.SHA),
                         _actual('codebuddy', 'C:\\ws', self.SHA))
        self.assertFalse(r['ok'])
        self.assertFalse(r['sent'])
        self.assertTrue(r['manual_relay_only'])
        self.assertTrue(any('retired' in x.lower() and 'manual relay' in x.lower()
                            for x in r['reasons']), r['reasons'])

    def test_workbuddy_plan_refused(self):
        # workbuddy 不在 CAPABILITY：短路拒绝仍带 sent=false/manual_relay_only，不抛异常。
        r = ec.preflight(_plan('workbuddy', 'C:\\ws', self.SHA),
                         _actual('workbuddy', 'C:\\ws', self.SHA))
        self.assertFalse(r['ok'])
        self.assertFalse(r['sent'])
        self.assertTrue(r['manual_relay_only'])

    def test_actual_runtime_drift_to_retired_refused(self):
        # plan 为 qoder 但 actual 真实 runtime 漂到 codebuddy：也按退休拒绝。
        r = ec.preflight(_plan('qoder', 'C:\\ws', self.SHA),
                         _actual('codebuddy', 'C:\\ws', self.SHA))
        self.assertFalse(r['ok'])
        self.assertTrue(r['manual_relay_only'])

    def test_qoder_plan_still_dispatchable(self):
        r = ec.preflight(_plan('qoder', 'C:\\ws', self.SHA),
                         _actual('qoder', 'C:\\ws', self.SHA))
        self.assertTrue(r['ok'], r['reasons'])
        self.assertFalse(r.get('manual_relay_only', False))

    def test_zcode_plan_still_dispatchable(self):
        g = ec.grants_from_rules([], [], [], ['Read'])
        plan = _plan('zcode', 'C:\\ws', self.SHA)
        plan['grants'] = {'edits': g['edits'], 'bash': g['bash'], 'read_dirs': g['read_dirs']}
        plan['tool_visibility'] = g['tool_visibility']
        plan['visible_tools'] = g['visible_tools']
        plan['allowed_tools'] = g['allowed_tools']
        plan['disallowed_tools'] = g['disallowed_tools']
        actual = _actual('zcode', 'C:\\ws', self.SHA)
        actual['grants'] = g
        r = ec.preflight(plan, actual)
        self.assertTrue(r['ok'], r['reasons'])
        self.assertFalse(r.get('manual_relay_only', False))


if __name__ == '__main__':
    unittest.main()
