"""prompt_contract 共享九节契约与三入口实际发送载荷取证（离线，真实子进程/SDK receipts）。

覆盖需求：
- 单一来源：prompt_contract.SECTION_HEADERS 与 qoder_direct.SECTION_HEADERS 逐字相同，
  且本模块不反向导入任何入口（无循环导入）；
- build_contract 返回的契约实际进入发送载荷：英文指令 + 阶段/路径具体值 + 九标题 +
  首末标记 + 结语，全部可在留证字节里核对；
- 三入口发送的实际内容精确等于留证：Qoder 任务走 stdin、契约走 --append-system-prompt
  argv，两通道都按字节回读；CodeBuddy 契约拼接任务从 stdin；ZCode request.prompt 原样
  交给 SDK submitPrompt，离线 receipts 记录消费值；
- CRLF 输入：原始 filehash 与发送 hash 可不同，plan 使用原始 filehash 通过并真实派发；
  使用错误 hash 的 plan 一律 rc 2 零派发、零目录；
- 完整九节/字段/结语到达真实载荷；格式错误报告仍 bound=false 且原文字节不改；
- 纯预检（ZCode --preflight-only）不提交、不附带契约。

不访问网络、不读凭据、不调用真实模型。本文件不在本机执行过真实派工——由主脑运行验证。
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPTS = ROOT / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import prompt_contract as pc  # noqa: E402
import qoder_direct as qd  # noqa: E402
import zcode_direct as zd  # noqa: E402
import execution_control as ec  # noqa: E402

QODER_ENTRY = SCRIPTS / 'qoder_direct.py'
CODEBUDDY_ENTRY = SCRIPTS / 'codebuddy_direct.py'
ZCODE_ENTRY = SCRIPTS / 'zcode_direct.py'
ZCODE_STUB = HERE / 'stub_zcode_runner.py'

# 最小本地测试载体（嵌入本文件，不新增业务 stub）：记录收到的 stdin 原字节后回放信封。
QODER_STUB = '''\
import json, os, sys
from pathlib import Path
data = sys.stdin.buffer.read()
sf = os.environ.get('PC_STDIN_FILE')
if sf:
    Path(sf).write_bytes(data)
rep = Path(os.environ['PC_REPORT_FILE']).read_bytes().decode('utf-8')
env = {"type": "result", "subtype": "success", "is_error": False,
       "stop_reason": "end_turn", "session_id": "sess-pc-qoder", "modelUsage": "stub",
       "total_credits": 0, "result": rep}
sys.stdout.write(json.dumps(env, ensure_ascii=False))
'''

CODEBUDDY_STUB = '''\
import json, os, sys
from pathlib import Path
data = sys.stdin.buffer.read()
sf = os.environ.get('PC_STDIN_FILE')
if sf:
    Path(sf).write_bytes(data)
rep = Path(os.environ['PC_REPORT_FILE']).read_bytes().decode('utf-8')
model = os.environ['PC_MODEL']
lines = [
    {"type": "system", "subtype": "init", "session_id": "pc-cb", "model": model,
     "tools": ["Read"], "permissionMode": "dontAsk", "mcp_servers": []},
    {"type": "system", "subtype": "status", "session_id": "pc-cb", "status": None},
    {"type": "assistant", "session_id": "pc-cb",
     "message": {"model": model, "usage": {"input_tokens": 1, "output_tokens": 1},
                 "content": [{"type": "text", "text": rep}]}},
    {"type": "result", "subtype": "success", "is_error": False, "session_id": "pc-cb",
     "result": rep, "usage": {"input_tokens": 1, "output_tokens": 1},
     "modelUsage": {model: {"input_tokens": 1}}},
]
out = sys.stdout.buffer
for l in lines:
    out.write(json.dumps(l, ensure_ascii=False).encode('utf-8') + b"\\n")
out.flush()
'''

MARKER_ASCII = 'ASCII-MARKER-7f3a'
MARKER_UNICODE = '中文与非-ASCII-标记-ǚ'


def bound_report(stage, ws):
    lines = ['WORKER_REPORT_START',
             f'阶段编号与执行方式：{stage}；direct。',
             f'实际项目绝对路径：{ws}',
             '汇报时间与执行环境：离线测试', '']
    for head in pc.SECTION_HEADERS:
        lines += [head, '- 契约载荷测试占位', '']
    lines += [pc.CLOSING_LINE, 'WORKER_REPORT_END']
    return '\n'.join(lines) + '\n'


def read_json(path):
    return json.loads(Path(path).read_bytes().decode('utf-8'))


class PromptContractPureTests(unittest.TestCase):
    def test_section_headers_single_source(self):
        self.assertEqual(pc.SECTION_HEADERS, qd.SECTION_HEADERS)

    def test_missing_required_closing_line_refuses_binding(self):
        report = bound_report('CLOSING-01', '/isolated/example').replace(pc.CLOSING_LINE, '')
        body = qd.analyze_report(report, 'CLOSING-01', '/isolated/example')
        self.assertFalse(body['body_ok'])
        self.assertIn('report lacks the required closing line', body['reasons'])

    def test_build_contract_carries_every_required_element(self):
        stage = 'BW-PC-CONTRACT-01'
        ws = 'C:\\work\\proj'
        contract = pc.build_contract(stage, ws)
        self.assertTrue(contract.startswith(
            'Final response must contain only the complete nine-section report'))
        self.assertIn(pc.REPORT_START, contract)
        self.assertIn(pc.REPORT_END, contract)
        self.assertIn(pc.CLOSING_LINE, contract)
        self.assertIn(f'阶段编号与执行方式：{stage}', contract)
        self.assertIn(ws, contract)
        for head in pc.SECTION_HEADERS:
            self.assertIn(head, contract)

    def test_compose_task_payload_preserves_task_verbatim(self):
        contract = 'C'
        task = '任务原文\r\nsecond'
        composed = pc.compose_task_payload(contract, task)
        self.assertEqual(composed, contract + '\n\n' + task)
        self.assertTrue(composed.endswith(task))

    def test_payload_evidence_distinguishes_raw_from_sent(self):
        raw = 'a\r\nb\r\n'.encode('utf-8')
        sent_task = 'a\nb\n'
        ev = pc.payload_evidence(raw_prompt_bytes=raw, sent_task_text=sent_task,
                                 contract='CONTRACT', sent_payload_bytes=('C\n\n' + sent_task).encode('utf-8'),
                                 newline_caliber='test')
        self.assertEqual(ev['prompt_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(ev['task_text_utf8_sha256'], hashlib.sha256(sent_task.encode('utf-8')).hexdigest())
        self.assertNotEqual(ev['prompt_sha256'], ev['task_text_utf8_sha256'])
        self.assertTrue(ev['newline_conversion']['raw_file_has_crlf'])
        self.assertFalse(ev['newline_conversion']['raw_equals_sent_task_utf8'])
        self.assertIsNone(pc.payload_evidence(raw_prompt_bytes=raw, sent_task_text=sent_task,
                                               contract=None, sent_payload_bytes=raw,
                                               newline_caliber='t')['contract_sha256'])


class _SubprocessBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.ws = self.base / 'ws'
        self.ws.mkdir()
        self.stage = 'BW-PC-DISPATCH-01'
        self.model_qoder = 'Qwen3.8-Flash'
        self.model_cb = 'PC-CB-1'
        self.report_file = self.base / 'report.txt'
        self.report_file.write_text(bound_report(self.stage, str(self.ws)),
                                    encoding='utf-8', newline='\n')
        self.stub_dir = self.base / 'stubs'
        self.stub_dir.mkdir()
        (self.stub_dir / 'qoder_stub.py').write_text(QODER_STUB, encoding='utf-8')
        (self.stub_dir / 'cb_stub.py').write_text(CODEBUDDY_STUB, encoding='utf-8')

    def tearDown(self):
        self._tmp.cleanup()

    def _env(self, **extra):
        env = os.environ.copy()
        env.update({'PYTHONIOENCODING': 'utf-8',
                    'SYSTEMROOT': os.environ.get('SYSTEMROOT', ''),
                    'PC_REPORT_FILE': str(self.report_file),
                    'STUB_REPORT_FILE': str(self.report_file)})
        env.update(extra)
        return env

    def _plan_for(self, runtime, prompt_path, *, model):
        if runtime == 'zcode':
            g = ec.grants_from_rules(['Read'], [], zd.build_tool_disallowlist(['Read']), ['Read'])
        else:
            g = ec.grants_from_rules(['Read'], [], [], ['Read'])
        plan = {'task_id': 'T-PC', 'stage': self.stage, 'runtime': runtime, 'model': model,
                'workspace': str(self.ws), 'cwd': str(self.ws),
                'prompt_sha256': hashlib.sha256(prompt_path.read_bytes()).hexdigest(),
                'grants': {'edits': g['edits'], 'bash': g['bash'], 'read_dirs': g['read_dirs']},
                'tool_visibility': g['tool_visibility'], 'visible_tools': g['visible_tools'],
                'allowed_tools': g['allowed_tools'], 'disallowed_tools': g['disallowed_tools'],
                'active_tasks': [], 'depends_on': [], 'shared_writes': [],
                'max_concurrency': 1, 'isolation': 'independent_workspace'}
        p = self.base / f'plan-{runtime}.json'
        p.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8', newline='\n')
        return p

    def _assert_headers_in(self, text):
        for head in pc.SECTION_HEADERS:
            self.assertIn(head, text)


class QoderSendEvidenceTests(_SubprocessBase):
    def test_qoder_crlf_raw_plan_and_received_bytes(self):
        prompt = self.base / 'crlf.txt'
        raw = f'Qoder 原字节 {MARKER_UNICODE}\r\n尾部保留\r\n'.encode('utf-8')
        prompt.write_bytes(raw)
        plan = self._plan_for('qoder', prompt, model=self.model_qoder)
        proc, received = self._run(prompt, self.ws / 'crlf-ok', plan)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode('utf-8', 'replace'))
        self.assertEqual(received.read_bytes(), raw)
        self.assertEqual(prompt.read_bytes(), raw)
        evidence = read_json(self.ws / 'crlf-ok/request.json')['prompt_payload']
        self.assertTrue(evidence['newline_conversion']['raw_equals_sent_task_utf8'])
        wrong = read_json(plan)
        wrong['prompt_sha256'] = hashlib.sha256(raw.replace(b'\r\n', b'\n')).hexdigest()
        plan.write_text(json.dumps(wrong), encoding='utf-8')
        received.unlink()
        proc, received = self._run(prompt, self.ws / 'crlf-rejected', plan)
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(received.exists())
        self.assertFalse((self.ws / 'crlf-rejected').exists())

    def _run(self, prompt_path, out, plan):
        cfg = self.base / 'qoder-cfg.json'
        cfg.write_text(json.dumps({'node': sys.executable,
                                   'qodercli': str(self.stub_dir / 'qoder_stub.py')}),
                       encoding='utf-8')
        stdin_file = self.base / 'qoder-recv-stdin.bin'
        cmd = [sys.executable, str(QODER_ENTRY), '--workspace', str(self.ws),
               '--prompt-file', str(prompt_path), '--output-dir', str(out),
               '--stage', self.stage, '--model', self.model_qoder, '--tools', 'Read',
               '--config', str(cfg), '--dispatch-plan', str(plan)]
        proc = subprocess.run(cmd, env=self._env(PC_STDIN_FILE=str(stdin_file)),
                              cwd=str(self.ws), capture_output=True, timeout=120)
        return proc, stdin_file

    def test_qoder_two_channel_evidence_and_nine_sections(self):
        prompt = self.base / 'prompt.txt'
        prompt.write_text(f'Qoder 载荷任务 {MARKER_ASCII} {MARKER_UNICODE}\n',
                          encoding='utf-8', newline='\n')
        out = self.ws / 'out'
        proc, stdin_file = self._run(prompt, out, self._plan_for('qoder', prompt, model=self.model_qoder))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode('utf-8', 'replace'))
        request = read_json(out / 'request.json')
        # prompt_sha256 是原始文件字节哈希，不是 JSON 文件哈希也不是载荷哈希。
        self.assertEqual(request['prompt_sha256'],
                         hashlib.sha256(prompt.read_bytes()).hexdigest())
        pp = request['prompt_payload']
        # stdin 通道：保存字节、回读、子进程真实收到，三者一致。
        received = stdin_file.read_bytes()
        self.assertEqual(pp['task_text_utf8_sha256'], hashlib.sha256(received).hexdigest())
        self.assertEqual(pp['channels']['task_stdin_sha256'], hashlib.sha256(received).hexdigest())
        self.assertEqual((out / 'sent-payload-stdin.bin').read_bytes(), received)
        self.assertTrue(MARKER_ASCII.encode('utf-8') in received)
        self.assertTrue(MARKER_UNICODE.encode('utf-8') in received)
        self.assertFalse(pp['newline_conversion']['raw_file_has_crlf'])
        # argv 通道：--append-system-prompt 后的 token 原样落盘，九节契约在其中。
        argv = request['argv']
        idx = argv.index('--append-system-prompt')
        contract_tok = argv[idx + 1]
        self.assertEqual(pp['contract_sha256'],
                         hashlib.sha256(contract_tok.encode('utf-8')).hexdigest())
        self.assertEqual((out / 'sent-contract-system-prompt.txt').read_bytes(),
                         contract_tok.encode('utf-8'))
        self.assertTrue(pp['channels']['contract_present_in_argv'])
        self.assertTrue(pp['readback_match'])
        self._assert_headers_in(contract_tok)
        self.assertIn(self.stage, contract_tok)
        self.assertIn(str(self.ws), contract_tok)
        self.assertIn(pc.CLOSING_LINE, contract_tok)
        # 报告绑定仍走原分析器，成功载荷 bound 为真。
        state = read_json(out / 'report-state.json')
        self.assertTrue(state['bound'])


class CodeBuddySendEvidenceTests(_SubprocessBase):
    def test_codebuddy_crlf_raw_plan_and_received_bytes(self):
        prompt = self.base / 'cb-crlf.txt'
        raw = f'CodeBuddy 原字节 {MARKER_UNICODE}\r\n尾部保留\r\n'.encode('utf-8')
        prompt.write_bytes(raw)
        plan = self._plan_for('codebuddy', prompt, model=self.model_cb)
        cfg = self.base / 'cb-crlf-cfg.json'
        cfg.write_text(json.dumps({'node': sys.executable, 'cli': str(self.stub_dir / 'cb_stub.py')}), encoding='utf-8')
        received = self.base / 'cb-crlf-receipt.bin'
        def run(out):
            return subprocess.run([sys.executable, str(CODEBUDDY_ENTRY), '--workspace', str(self.ws),
                '--prompt-file', str(prompt), '--output-dir', str(out), '--stage', self.stage,
                '--model', self.model_cb, '--tools', 'Read', '--config', str(cfg), '--dispatch-plan', str(plan)],
                env=self._env(PC_STDIN_FILE=str(received), PC_MODEL=self.model_cb), capture_output=True, timeout=30)
        out = self.ws / 'cb-crlf-ok'
        proc = run(out)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode('utf-8', 'replace'))
        self.assertEqual(received.read_bytes(), pc.compose_task_payload(pc.build_contract(self.stage, str(self.ws)), raw.decode('utf-8')).encode('utf-8'))
        self.assertEqual((out / 'sent-payload-stdin.bin').read_bytes(), received.read_bytes())
        self.assertEqual(prompt.read_bytes(), raw)
        wrong = read_json(plan)
        wrong['prompt_sha256'] = hashlib.sha256(raw.replace(b'\r\n', b'\n')).hexdigest()
        plan.write_text(json.dumps(wrong), encoding='utf-8')
        received.unlink()
        proc = run(self.ws / 'cb-crlf-rejected')
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(received.exists())
        self.assertFalse((self.ws / 'cb-crlf-rejected').exists())

    def test_codebuddy_contract_and_task_reach_single_stdin(self):
        prompt = self.base / 'prompt.txt'
        prompt.write_text(f'CodeBuddy 载荷任务 {MARKER_ASCII}\n', encoding='utf-8', newline='\n')
        out = self.ws / 'out'
        cfg = self.base / 'cb-cfg.json'
        cfg.write_text(json.dumps({'node': sys.executable,
                                   'cli': str(self.stub_dir / 'cb_stub.py')}), encoding='utf-8')
        stdin_file = self.base / 'cb-recv-stdin.bin'
        cmd = [sys.executable, str(CODEBUDDY_ENTRY), '--workspace', str(self.ws),
               '--prompt-file', str(prompt), '--output-dir', str(out),
               '--stage', self.stage, '--model', self.model_cb, '--tools', 'Read',
               '--config', str(cfg), '--dispatch-plan',
               str(self._plan_for('codebuddy', prompt, model=self.model_cb))]
        proc = subprocess.run(cmd, env=self._env(PC_STDIN_FILE=str(stdin_file),
                                                 PC_MODEL=self.model_cb),
                              cwd=str(self.ws), capture_output=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode('utf-8', 'replace'))
        request = read_json(out / 'request.json')
        self.assertEqual(request['prompt_sha256'],
                         hashlib.sha256(prompt.read_bytes()).hexdigest())
        pp = request['prompt_payload']
        received = stdin_file.read_bytes()
        # 单一 stdin 通道同时携带契约与逐字任务，且等于留证字节。
        self.assertEqual(pp['sent_payload_sha256'], hashlib.sha256(received).hexdigest())
        self.assertEqual((out / 'sent-payload-stdin.bin').read_bytes(), received)
        self.assertTrue(pp['readback_match'])
        text = received.decode('utf-8')
        self._assert_headers_in(text)
        self.assertIn(pc.REPORT_START, text)
        self.assertIn(pc.CLOSING_LINE, text)
        self.assertTrue(text.endswith(f'CodeBuddy 载荷任务 {MARKER_ASCII}\n'))
        state = read_json(out / 'report-state.json')
        self.assertTrue(state['bound'])

    def test_codebuddy_malformed_report_unbound_original_unchanged(self):
        prompt = self.base / 'prompt-malformed.txt'
        prompt.write_text('CodeBuddy 载荷任务（格式错误报告）\n', encoding='utf-8', newline='\n')
        bad = self.base / 'bad-report.txt'
        bad.write_text('好的，下面是我的报告：\nWORKER_REPORT_START\n缺少九节\n',
                       encoding='utf-8', newline='\n')
        out = self.ws / 'out-bad'
        cfg = self.base / 'cb-cfg-bad.json'
        cfg.write_text(json.dumps({'node': sys.executable,
                                   'cli': str(self.stub_dir / 'cb_stub.py')}), encoding='utf-8')
        cmd = [sys.executable, str(CODEBUDDY_ENTRY), '--workspace', str(self.ws),
               '--prompt-file', str(prompt), '--output-dir', str(out),
               '--stage', self.stage, '--model', self.model_cb, '--tools', 'Read',
               '--config', str(cfg)]
        proc = subprocess.run(cmd, env=self._env(PC_MODEL=self.model_cb,
                                                 PC_REPORT_FILE=str(bad)),
                              cwd=str(self.ws), capture_output=True, timeout=120)
        self.assertEqual(proc.returncode, 3)
        state = read_json(out / 'report-state.json')
        self.assertFalse(state['bound'])
        self.assertFalse(state['body_ok'])
        # 原文不裁剪、不改写：response.md 与模型返回逐字节一致。
        self.assertEqual((out / 'response.md').read_bytes(), bad.read_bytes())


class ZcodeSendEvidenceTests(_SubprocessBase):
    def _cfg(self):
        fixture = {}
        for key, name in (('bootstrap', 'boot.js'), ('tsx_loader', 'loader.mjs'),
                          ('builtin_provider_config', 'builtin.json'),
                          ('personal_provider_config', 'personal.json')):
            f = self.base / f'zc-{name}'
            f.write_text('offline fixture; never executed\n', encoding='utf-8')
            fixture[key] = str(f)
        cfg = self.base / 'zcode-cfg.json'
        cfg.write_text(json.dumps({'node': sys.executable, 'runner': str(ZCODE_STUB),
                                   'node_args': [], 'environment': {}, **fixture}),
                       encoding='utf-8')
        return cfg

    def _run(self, prompt, out, extra=(), env_mode='ok', plan=None):
        cfg = self._cfg()
        cmd = [sys.executable, str(ZCODE_ENTRY), '--workspace', str(self.ws),
               '--prompt-file', str(prompt), '--output-dir', str(out),
               '--stage', self.stage, '--tools', 'Read', '--config', str(cfg), *extra]
        if plan is not None:
            cmd += ['--dispatch-plan', str(plan)]
        env = self._env(STUB_MODE=env_mode)
        proc = subprocess.run(cmd, env=env, cwd=str(self.ws), capture_output=True, timeout=120)
        return proc

    def _receipts(self, out):
        recs = []
        path = out / 'stub-receipts.jsonl'
        if path.is_file():
            for line in path.read_text(encoding='utf-8').splitlines():
                if line.strip():
                    recs.append(json.loads(line))
        return recs

    def test_zcode_contract_reaches_request_prompt_and_receipt(self):
        prompt = self.base / 'prompt.txt'
        prompt.write_text(f'ZCode 载荷任务 {MARKER_ASCII} {MARKER_UNICODE}\n',
                          encoding='utf-8', newline='\n')
        out = self.ws / 'out'
        proc = self._run(prompt, out, plan=self._plan_for('zcode', prompt, model='GLM-5.3-Flash'))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode('utf-8', 'replace'))
        request = read_json(out / 'request.json')
        self.assertEqual(request['prompt_sha256'],
                         hashlib.sha256(prompt.read_bytes()).hexdigest())
        pp = request['prompt_payload']
        # receipts 记录 runner 真实收到的完整 request，证明 prompt/prompt_sha256 被消费。
        recv = next(r['request'] for r in self._receipts(out) if r.get('event') == 'request')
        self.assertEqual(recv['prompt'], request['prompt'])
        self.assertEqual(recv['prompt_sha256'], request['prompt_sha256'])
        self.assertEqual(pp['sent_payload_sha256'],
                         hashlib.sha256(recv['prompt'].encode('utf-8')).hexdigest())
        self.assertEqual((out / 'sent-task-payload.bin').read_bytes(),
                         recv['prompt'].encode('utf-8'))
        self.assertTrue(pp['readback_match'])
        self._assert_headers_in(recv['prompt'])
        submit = [r for r in self._receipts(out) if r.get('event') == 'submit']
        self.assertEqual(len(submit), 1)
        self.assertEqual(submit[0]['prompt_sha256'], request['prompt_sha256'])

    def test_zcode_preflight_only_submits_nothing_and_omits_contract(self):
        prompt = self.base / 'prompt-pf.txt'
        prompt.write_text(f'ZCode 预检任务 {MARKER_ASCII}\n', encoding='utf-8', newline='\n')
        out = self.ws / 'out-pf'
        proc = self._run(prompt, out, extra=['--preflight-only'])
        self.assertEqual(proc.returncode, 0, proc.stderr.decode('utf-8', 'replace'))
        request = read_json(out / 'request.json')
        recv = next(r['request'] for r in self._receipts(out) if r.get('event') == 'request')
        self.assertEqual(recv['prompt'], f'ZCode 预检任务 {MARKER_ASCII}\n')
        self.assertNotIn(pc.REPORT_START, recv['prompt'])
        self.assertIsNone(request['prompt_payload']['contract_sha256'])
        self.assertEqual(request['prompt_payload']['channels']['submit_channel'], 'none')
        self.assertEqual(request['prompt_payload']['channels']['payload_role'], 'planned_request_prompt')
        self.assertEqual([r for r in self._receipts(out) if r.get('event') == 'submit'], [])

    def test_zcode_crlf_raw_plan_passes_but_wrong_plan_zero_dispatch(self):
        prompt = self.base / 'prompt-crlf.txt'
        prompt.write_bytes(f'ZCode CRLF 任务一\r\n二 {MARKER_ASCII}\r\n'.encode('utf-8'))
        raw_hash = hashlib.sha256(prompt.read_bytes()).hexdigest()
        normalized_hash = hashlib.sha256(prompt.read_bytes().decode('utf-8').replace('\r\n', '\n').encode('utf-8')).hexdigest()
        self.assertNotEqual(raw_hash, normalized_hash, 'fixture must actually exercise CRLF')

        # 正确 plan 使用原始文件字节哈希 → 真实派发，且发送 hash 与原始 filehash 不同。
        ok_plan = self._plan_for('zcode', prompt, model='GLM-5.3-Flash')
        self.assertEqual(json.loads(ok_plan.read_text(encoding='utf-8'))['prompt_sha256'], raw_hash)
        out = self.ws / 'out-crlf'
        proc = self._run(prompt, out, plan=ok_plan)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode('utf-8', 'replace'))
        pp = read_json(out / 'request.json')['prompt_payload']
        self.assertEqual(pp['prompt_sha256'], raw_hash)
        self.assertTrue(pp['newline_conversion']['raw_file_has_crlf'])
        self.assertNotEqual(pp['prompt_sha256'], pp['task_text_utf8_sha256'])
        self.assertIn('CRLF/CR-to-LF', pp['newline_conversion']['caliber'])

        # 错误 plan（发送文本哈希）→ 预检在 Popen 之前拒绝：rc 2、零目录、零提交。
        bad_plan = self.base / 'plan-badhash.json'
        plan_data = json.loads(ok_plan.read_text(encoding='utf-8'))
        plan_data['prompt_sha256'] = normalized_hash
        bad_plan.write_text(json.dumps(plan_data, ensure_ascii=False), encoding='utf-8', newline='\n')
        bad_out = self.ws / 'out-bad'
        proc = self._run(prompt, bad_out, plan=bad_plan)
        self.assertEqual(proc.returncode, 2, proc.stdout.decode('utf-8', 'replace'))
        self.assertFalse(bad_out.exists())
        self.assertEqual(self._receipts(bad_out), [])


if __name__ == '__main__':
    unittest.main()
