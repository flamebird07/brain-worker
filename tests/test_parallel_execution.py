"""四任务并行演练（真实适配器 + 临时可控 stub 原始信封，subprocess）。

覆盖需求 6：真实调用现有三个执行入口的 --dispatch-plan 路径，用可控制原始信封的
临时 stub 复现四类结果，并证明并行控制面行为（fixture 一律 newline='\n'，避免
Windows CRLF 让 Qoder/ZCode 的 read_text→encode 哈希与 read_bytes 计划绑定不符）：

- 一 CodeBuddy 结构化 429/6004（errors_info status=429, code=6004，含 reset 提示）；
- 一 ZCode events.jsonl 真实 permission_resolved/tool_call_result 形状缺 Bash 权限
  客户端（协议仍成功、summary 无拒绝记录，仍从原始 events 提取 permission_client_missing，
  正常 model_request 正文含 429/缺 client 也不误报）；
- 两 Qoder 成功完整绑定报告（九节、阶段 token、路径一致、session 非空、回读哈希一致），
  仅这两个成功任务写各自独立 workspace 的同名 output.py；失败任务零代码产出；
- 文件 barrier 证明四子进程真实重叠而非串行（max(started) <= min(finished)）；
- 失败的两个任务不阻塞其余：两 Qoder 任务照常完成并绑定；
- authorized fallback：CodeBuddy 429 先落失败 summary+hash，再以另一入口接管，证明接管
  发生时另两任务仍在途且其 prompt/request 哈希一字未改；
- BRAIN_WORKER_EXERCISE_EVIDENCE_DIR 存在时把完整 request/summary/失败证据复制过去；
- 计划拒绝：同 task_id 在途、同 workspace 写入冲突、依赖非 completed/passed（五状态，无
  verified）、缺 grant，一律 sent=False（直接 ec.preflight），并在真实入口 codebuddy 上
  验证 rc=2 零目录。

不访问网络、不读凭据、不调用真实模型；全部离线 stub 子进程。测试用 unittest，无
pytest 依赖。本文件不在本机执行过真实派工——由主脑运行验证。"""
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
import qoder_direct as qd  # noqa: E402  复用九节头做绑定报告，不改其文件
import zcode_direct as zd

QODER_ENTRY = SCRIPTS / 'qoder_direct.py'
CODEBUDDY_ENTRY = SCRIPTS / 'codebuddy_direct.py'
ZCODE_ENTRY = SCRIPTS / 'zcode_direct.py'

# 所有 stub 共享的 barrier：每个 stub 启动即写 started-<id>，等满 BARRIER_TOTAL 个
# 后才写 finished-<id>；只有四子进程真正同时在跑，max(started) 才会 <= min(finished)。
BARRIER_SNIPPET = '''
import os as _os, time as _time
from pathlib import Path as _P
def _barrier():
    _b = _P(_os.environ['BARRIER_DIR']); _t = int(_os.environ['BARRIER_TOTAL'])
    _tid = _os.environ['TASK_ID']
    (_b / ('started-' + _tid)).write_text(str(_time.time()))
    _dl = _time.time() + 40
    while _time.time() < _dl:
        if len(list(_b.glob('started-*'))) >= _t:
            break
        _time.sleep(0.03)
    (_b / ('finished-' + _tid)).write_text(str(_time.time()))
def _emit_output():
    _w = _os.environ['WORKSPACE']
    (_P(_w) / 'output.py').write_text('# task ' + _os.environ['TASK_ID'] + chr(10))
'''

QODER_STUB = BARRIER_SNIPPET + '''
import json, sys
from pathlib import Path
sys.stdin.read()
_emit_output()
_barrier()
rep = Path(_os.environ['STUB_REPORT_FILE']).read_text(encoding='utf-8')
env = {'type': 'result', 'subtype': 'success', 'is_error': False,
       'stop_reason': 'end_turn', 'session_id': 'sess-parallel-qoder',
       'modelUsage': 'stub', 'total_credits': 0, 'result': rep}
sys.stdout.write(json.dumps(env, ensure_ascii=False))
sys.stdout.flush()
'''

CODEBUDDY_STUB = BARRIER_SNIPPET + '''
import json, sys
sys.stdin.read()
_barrier()
MODEL = 'CB-PARR-429'
rate = '429 上游限流，将在 2026-10-06 14:00:00 UTC+8 重置。'
lines = [
    {'type': 'system', 'subtype': 'init', 'session_id': 'cb-1', 'model': MODEL,
     'tools': ['Read'], 'permissionMode': 'dontAsk', 'mcp_servers': []},
    {'type': 'system', 'subtype': 'status', 'session_id': 'cb-1', 'status': None},
    {'type': 'assistant', 'session_id': 'cb-1',
     'message': {'model': MODEL, 'usage': {'input_tokens': 1, 'output_tokens': 1},
                 'content': [{'type': 'text', 'text': rate}]}},
    {'type': 'result', 'subtype': 'error_during_execution', 'is_error': True,
     'session_id': 'cb-1', 'usage': {'input_tokens': 1, 'output_tokens': 1},
     'modelUsage': {MODEL: {'input_tokens': 1}}, 'errors': [rate],
     'errors_info': [{'status': 429, 'code': 6004, 'category': 'quota',
                      'details': rate}]},
]
out = sys.stdout.buffer
for l in lines:
    out.write(json.dumps(l, ensure_ascii=False).encode('utf-8') + b"\\n")
out.flush()
sys.exit(0)
'''

ZCODE_STUB = BARRIER_SNIPPET + '''
import hashlib, json, sys
from pathlib import Path
_req_path = sys.argv[sys.argv.index('--request') + 1]
req = json.loads(Path(_req_path).read_text(encoding='utf-8'))
out = Path(req['output_dir'])
_barrier()
# events.jsonl 用真实 ZCode 事件形状复现“缺 Bash 权限客户端”：permission_resolved
# deny + reason（payload 内），以及一个 tool_call_result 的 payload.isError/error；
# result.summary 里 permission_denials 仍为 0。另混入一条正常 model_request，其历史
# 正文含 429 / 缺 client 文案，验证分类器绝不扫描请求正文。
events = [
    {'type': 'turn_started', 'payload': {'turnId': 't1'}},
    {'type': 'model_request', 'payload': {'messages': [
        {'role': 'user', 'content': '上次报 429，且 No permission client configured for Bash'}]}},
    {'type': 'permission_resolved', 'payload': {
        'requestId': 'perm_1', 'toolCallId': 'call_1', 'decision': 'deny',
        'reason': 'No permission client configured for Bash'}},
    {'type': 'tool_call_result', 'payload': {
        'toolCallId': 'call_1', 'isError': True,
        'error': 'No permission client configured for Bash'}},
    {'type': 'turn_completed', 'payload': {'turnId': 't1'}},
]
out.joinpath('events.jsonl').write_text(
    "\\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\\n",
    encoding='utf-8')
resp = Path(_os.environ['STUB_REPORT_FILE']).read_bytes()
(out / 'response.md').write_bytes(resp)
env = {'carrier': 'zcode-sdk', 'stage': req['stage'], 'ok': True,
       'preflight_ok': True, 'submitted': True, 'session_id': 'sess-parallel-zcode',
       'turn_id': 't1', 'status': 'completed', 'errors': [],
       'usage': {'source': 'provider', 'input_tokens': 10, 'output_tokens': 5,
                 'total_tokens': 15, 'cache_read_included_in_input': True},
       'response_sha256': hashlib.sha256(resp).hexdigest(),
       'response_bytes': len(resp),
       'observed_tool_catalog': ['Read', 'Bash'], 'event_count': 3}
sys.stdout.write(json.dumps(env, ensure_ascii=False) + "\\n")
sys.stdout.flush()
sys.exit(0)
'''


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def build_bound_report(stage: str, workspace: str) -> str:
    lines = ['WORKER_REPORT_START',
             f'阶段编号与执行方式：{stage}；direct。',
             f'实际项目绝对路径：{workspace}',
             '']
    for head in qd.SECTION_HEADERS:
        lines += [head, '- 并行演练占位', '']
    lines += ['本阶段汇报结束；等待主脑验收。', 'WORKER_REPORT_END']
    return '\n'.join(lines) + '\n'


class ParallelRehearsalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.barrier = self.base / 'barrier'
        self.barrier.mkdir()
        self.stub_dir = self.base / 'stubs'
        self.stub_dir.mkdir()
        self._write_stubs()

    def tearDown(self):
        self._tmp.cleanup()

    def _write_stubs(self):
        (self.stub_dir / 'qoder_stub.py').write_text(QODER_STUB, encoding='utf-8')
        (self.stub_dir / 'codebuddy_stub.py').write_text(CODEBUDDY_STUB, encoding='utf-8')
        (self.stub_dir / 'zcode_stub.py').write_text(ZCODE_STUB, encoding='utf-8')

    def _mk_workspace(self, tag: str) -> Path:
        ws = (self.base / ('ws-' + tag)).resolve()
        ws.mkdir()
        return ws

    def _write(self, name: str, text: str) -> Path:
        p = self.base / name
        # newline='\n' 强制 LF：Windows 默认 write_text 会译成 CRLF，使入口 read_text→
        # encode 的哈希与 read_bytes 原字节哈希不一致，进而让 Qoder/ZCode 在预检里被误拒。
        p.write_text(text, encoding='utf-8', newline='\n')
        return p

    def _plan(self, task_id, stage, runtime, model, ws, prompt_path, visibility):
        p = self.base / f'plan-{task_id}.json'
        # 与真实入口 grants_from_rules 口径一致：qoder/codebuddy 把 --tools 逐名同时
        # 落进 bare 可见性与完整 allow 规则、以及 --tools visible_tools；zcode 只有整
        # 工具 --tools（visible_tools），绝不塞细规则。缺失即双向比对报“缺计划可见性”。
        if runtime == 'zcode':
            g = ec.grants_from_rules(list(visibility), [], zd.build_tool_disallowlist(visibility), list(visibility))
        else:
            g = ec.grants_from_rules(list(visibility), [], [], list(visibility))
        plan = {
            'task_id': task_id, 'stage': stage, 'runtime': runtime, 'model': model,
            'workspace': str(ws), 'cwd': str(ws),
            'prompt_sha256': hashlib.sha256(prompt_path.read_bytes()).hexdigest(),
            'grants': {'edits': g['edits'], 'bash': g['bash'],
                       'read_dirs': g['read_dirs']},
            'tool_visibility': g['tool_visibility'], 'visible_tools': g['visible_tools'],
            'allowed_tools': g['allowed_tools'], 'disallowed_tools': g['disallowed_tools'],
            'active_tasks': [], 'depends_on': [], 'shared_writes': [],
            'max_concurrency': 4, 'isolation': 'independent_workspace',
        }
        p.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8', newline='\n')
        return p

    def _base_env(self, task_id: str, ws: Path, report: Path = None,
                  barrier_total: int = 4) -> dict:
        env = os.environ.copy()
        for key in ('STUB_MODE', 'STUB_REPORT_FILE', 'CODEBUDDY_STUB_SPEC',
                    'CODEBUDDY_STUB_RECORD'):
            env.pop(key, None)
        env.update({'BARRIER_DIR': str(self.barrier), 'BARRIER_TOTAL': str(barrier_total),
                    'TASK_ID': task_id, 'WORKSPACE': str(ws),
                    'PYTHONIOENCODING': 'utf-8',
                    'SYSTEMROOT': os.environ.get('SYSTEMROOT', '')})
        if report is not None:
            env['STUB_REPORT_FILE'] = str(report)
        return env

    def _qoder_cmd(self, ws, prompt, out, stage, plan):
        cfg = self.base / f'qoder-cfg-{ws.name}.json'
        cfg.write_text(json.dumps({'node': sys.executable,
                                    'qodercli': str(self.stub_dir / 'qoder_stub.py')}),
                       encoding='utf-8')
        return [sys.executable, str(QODER_ENTRY), '--workspace', str(ws),
                '--prompt-file', str(prompt), '--output-dir', str(out), '--stage', stage,
                '--tools', 'Read', '--config', str(cfg), '--dispatch-plan', str(plan)]

    def _codebuddy_cmd(self, ws, prompt, out, stage, plan):
        cfg = self.base / f'cb-cfg-{ws.name}.json'
        cfg.write_text(json.dumps({'node': sys.executable,
                                   'cli': str(self.stub_dir / 'codebuddy_stub.py')}),
                       encoding='utf-8')
        return [sys.executable, str(CODEBUDDY_ENTRY), '--workspace', str(ws),
                '--prompt-file', str(prompt), '--output-dir', str(out), '--stage', stage,
                '--model', 'CB-PARR-429', '--tools', 'Read', '--config', str(cfg),
                '--dispatch-plan', str(plan)]

    def _zcode_cmd(self, ws, prompt, out, stage, plan):
        fixture = {}
        for key, name in (('bootstrap', 'boot.js'), ('tsx_loader', 'loader.mjs'),
                          ('builtin_provider_config', 'builtin.json'),
                          ('personal_provider_config', 'personal.json')):
            f = self.base / f'zc-{ws.name}-{name}'
            f.write_text('offline fixture; never executed\n', encoding='utf-8')
            fixture[key] = str(f)
        cfg = self.base / f'zcode-cfg-{ws.name}.json'
        cfg.write_text(json.dumps({
            'node': sys.executable, 'runner': str(self.stub_dir / 'zcode_stub.py'),
            'node_args': [], 'environment': {}, **fixture}), encoding='utf-8')
        return [sys.executable, str(ZCODE_ENTRY), '--workspace', str(ws),
                '--prompt-file', str(prompt), '--output-dir', str(out), '--stage', stage,
                '--tools', 'Read', '--config', str(cfg), '--dispatch-plan', str(plan)]

    def test_four_task_parallel_rehearsal(self):
        specs = []
        # 1) CodeBuddy 结构化 429/6004
        ws = self._mk_workspace('cb429')
        prompt = self._write('prompt-cb429.txt', 'CodeBuddy 并行演练任务，原样汇报。\n')
        plan = self._plan('T-CB', 'BW-PARR-CB429', 'codebuddy', 'CB-PARR-429', ws,
                          prompt, ['Read'])
        specs.append(('cb429', self._codebuddy_cmd(ws, prompt, ws / 'out',
                                                   'BW-PARR-CB429', plan),
                      self._base_env('cb429', ws), ws, 'BW-PARR-CB429'))
        # 2) ZCode 缺 Bash 权限客户端（协议仍成功）
        ws = self._mk_workspace('zcperm')
        prompt = self._write('prompt-zcperm.txt', 'ZCode 并行演练任务，原样汇报。\n')
        plan = self._plan('T-ZC', 'BW-PARR-ZCPERM', 'zcode', 'GLM-5.3-Flash', ws,
                          prompt, ['Read'])
        report = self._write('report-zcperm.txt',
                             build_bound_report('BW-PARR-ZCPERM', str(ws)))
        specs.append(('zcperm', self._zcode_cmd(ws, prompt, ws / 'out',
                                                'BW-PARR-ZCPERM', plan),
                      self._base_env('zcperm', ws, report), ws, 'BW-PARR-ZCPERM'))
        # 3 & 4) 两个 Qoder 成功完整绑定报告（独立 workspace，同名 output.py）
        for tag, stage in (('qa', 'BW-PARR-QA'), ('qb', 'BW-PARR-QB')):
            ws = self._mk_workspace(tag)
            prompt = self._write(f'prompt-{tag}.txt', f'Qoder {tag} 并行演练任务。\n')
            plan = self._plan('T-' + tag.upper(), stage, 'qoder', 'Qwen3.8-Flash', ws,
                              prompt, ['Read'])
            report = self._write(f'report-{tag}.txt',
                                 build_bound_report(stage, str(ws)))
            specs.append((tag, self._qoder_cmd(ws, prompt, ws / 'out', stage, plan),
                          self._base_env(tag, ws, report), ws, stage))

        # 并发启动四适配器子进程（真实重叠由 barrier 门控完成证明）。
        procs = {tag: subprocess.Popen(cmd, env=env, cwd=str(ws),
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for tag, cmd, env, ws, stage in specs}
        results = {}
        deadline = 180
        for tag, cmd, env, ws, stage in specs:
            proc = procs[tag]
            try:
                out, err = proc.communicate(timeout=deadline)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
                self.fail(f'timeout for task {tag} (stage {stage}) at {ws / "out"}; '
                          f'its stdout={out.decode("utf-8", "replace")[:400]!r} '
                          f'stderr={err.decode("utf-8", "replace")[:400]!r} '
                          f'summary_present={(ws / "out" / "summary.json").is_file()} '
                          f'barrier_started={len(list(self.barrier.glob("started-*")))} '
                          f'barrier_finished={len(list(self.barrier.glob("finished-*")))}')
            results[tag] = {'rc': proc.returncode, 'ws': ws, 'stage': stage,
                            'out': ws / 'out',
                            'stdout': out.decode('utf-8', 'replace'),
                            'stderr': err.decode('utf-8', 'replace')}

        # ---- barrier 证明重叠而非串行 ----
        started = sorted(float(p.read_text()) for p in self.barrier.glob('started-*'))
        finished = sorted(float(p.read_text()) for p in self.barrier.glob('finished-*'))
        self.assertEqual(len(started), 4, f'started={started}')
        self.assertEqual(len(finished), 4, f'finished={finished}')
        self.assertLessEqual(max(started), min(finished),
                             'tasks did not truly overlap (serial execution detected)')

        # ---- 路径隔离：两个 Qoder 成功任务同名 output.py 在独立 workspace 互不覆盖；
        #      失败任务（CodeBuddy 429 / ZCode 缺客户端）零代码产出，不得提前写 output ----
        qoder_outputs = set()
        for tag in ('qa', 'qb'):
            f = results[tag]['ws'] / 'output.py'
            self.assertTrue(f.is_file(), f'missing isolated output for {tag}')
            qoder_outputs.add(f.read_text(encoding='utf-8'))
        self.assertEqual(len(qoder_outputs), 2,
                         f'two Qoder outputs must stay isolated: {qoder_outputs}')
        for tag in ('cb429', 'zcperm'):
            self.assertFalse((results[tag]['ws'] / 'output.py').exists(),
                             f'failed task {tag} must produce zero code output')

        # ---- 失败任务不阻塞其余：两 Qoder 成功完成并绑定 ----
        for tag in ('qa', 'qb'):
            r = results[tag]
            self.assertEqual(r['rc'], 0, r['stderr'])
            summary = json.loads((r['out'] / 'summary.json').read_text(encoding='utf-8'))
            self.assertTrue(summary['protocol_success'], summary)
            self.assertTrue(summary['report_bound'], summary)
            self.assertEqual(summary['diagnostics']['failure_types'], [],
                             'a clean success must not be misreported as failure')

        # ---- CodeBuddy 结构化 429/6004（含 reset 提示，零产出、不绑定） ----
        rcb = results['cb429']
        self.assertEqual(rcb['rc'], 3, rcb['stderr'])
        sb = json.loads((rcb['out'] / 'summary.json').read_text(encoding='utf-8'))
        self.assertFalse(sb['protocol_success'])
        self.assertIn('quota_429', sb['diagnostics']['failure_types'])
        self.assertEqual(sb['errors_info'][0]['status'], 429)
        self.assertEqual(sb['errors_info'][0]['code'], 6004)
        self.assertIn('重置', sb['diagnostics']['reset_hint'] or '')
        self.assertFalse(sb['report_bound'])

        # ---- ZCode events 缺 Bash 客户端（真实 permission_resolved 形状，summary
        #      无拒绝记录仍被提取；正常 model_request 正文不被误报） ----
        rzc = results['zcperm']
        sz = json.loads((rzc['out'] / 'summary.json').read_text(encoding='utf-8'))
        self.assertIn('permission_client_missing', sz['diagnostics']['failure_types'])
        # 已知缺客户端来源不得泛化为模型执行失败。
        self.assertNotIn('model_execution_failure', sz['diagnostics']['failure_types'])
        raw_events = (rzc['out'] / 'events.jsonl').read_text(encoding='utf-8')
        self.assertIn('model_request', raw_events)  # 含带 429 文案的正常请求正文

        # 若主脑给出 BRAIN_WORKER_EXERCISE_EVIDENCE_DIR，把全部 request/summary/process/
        # stdout/stderr/失败证据复制过去，便于真实回读；默认 temp 即可，不提交进业务仓。
        self._persist_evidence(results, rcb)

    def _persist_evidence(self, results, rcb):
        dest = os.environ.get('BRAIN_WORKER_EXERCISE_EVIDENCE_DIR')
        target = Path(dest).resolve() if dest else (self.base / 'exercise-evidence')
        target.mkdir(parents=True, exist_ok=True)
        for tag, r in results.items():
            task_dir = target / tag
            task_dir.mkdir(parents=True, exist_ok=True)
            for fname in ('request.json', 'summary.json', 'report-state.json'):
                src = r['out'] / fname
                if src.is_file():
                    (task_dir / fname).write_bytes(src.read_bytes())
        # 失败任务的原始 summary 先落盘并记录其哈希（供 fallback 前留证）。
        fail_src = rcb['out'] / 'summary.json'
        (target / 'T-CB.failure-summary.json').write_bytes(fail_src.read_bytes())
        (target / 'T-CB.failure-sha256.txt').write_text(
            _sha(fail_src.read_bytes()) + '\n', encoding='utf-8', newline='\n')


    def _fail_summary(self, ws, tag):
        # CodeBuddy 429 失败证据先落盘 + 记哈希，供 fallback 前留证。
        src = ws / 'out' / 'summary.json'
        dest = self.base / f'failure-evidence-{tag}'
        dest.mkdir(parents=True, exist_ok=True)
        (dest / 'summary.json').write_bytes(src.read_bytes())
        sha = _sha(src.read_bytes())
        (dest / 'summary-sha256.txt').write_text(sha + '\n', encoding='utf-8',
                                                 newline='\n')
        return sha

    def test_fallback_after_429_keeps_other_tasks_unchanged(self):
        # 1) 先跑一个 CodeBuddy 429 任务并保存失败 summary + hash（fallback 前留证）。
        ws = self._mk_workspace('fb429')
        prompt = self._write('prompt-fb429.txt', 'CodeBuddy 失败演练任务。\n')
        plan = self._plan('T-FB429', 'BW-FB-CB429', 'codebuddy', 'CB-PARR-429', ws,
                          prompt, ['Read'])
        proc = subprocess.run(self._codebuddy_cmd(ws, prompt, ws / 'out',
                                                  'BW-FB-CB429', plan),
                              env=self._base_env('fb429', ws, barrier_total=1),
                              cwd=str(ws), capture_output=True, timeout=120)
        self.assertEqual(proc.returncode, 3)
        fail_sha = self._fail_summary(ws, 'fb429')
        self.assertEqual(len(fail_sha), 64)

        # 2) 起两个 Qoder 成功任务（barrier_total=3），此刻它们仍卡在 barrier 内。
        inflight = {}
        for tag, stage in (('ia', 'BW-FB-IA'), ('ib', 'BW-FB-IB')):
            wsi = self._mk_workspace(tag)
            pi = self._write(f'prompt-{tag}.txt', f'Qoder {tag} 在途任务。\n')
            pl = self._plan('T-' + tag.upper(), stage, 'qoder', 'Qwen3.8-Flash', wsi,
                            pi, ['Read'])
            outi = wsi / 'out'
            pr = subprocess.Popen(self._qoder_cmd(wsi, pi, outi, stage, pl),
                                  env=self._base_env(tag, wsi,
                                                     self._write(f'report-{tag}.txt',
                                                                 build_bound_report(
                                                                     stage, str(wsi))),
                                                     barrier_total=3),
                                  cwd=str(wsi), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
            inflight[tag] = (pr, outi)

        # 等两个在途任务写出 request.json 并卡在 barrier（started<3 说明确仍在途）。
        import time as _t
        for tag, (pr, outi) in inflight.items():
            req = outi / 'request.json'
            for _ in range(200):
                if req.is_file():
                    break
                _t.sleep(0.05)
            self.assertTrue(req.is_file(), f'{tag} request.json not written')
        self.assertLess(len(list(self.barrier.glob('started-*'))), 3,
                        'in-flight tasks should still be gated before fallback dispatch')

        # 快照两个在途任务的 prompt_sha256 + request.json 原字节哈希（fallback 前）。
        before = {}
        for tag, (pr, outi) in inflight.items():
            raw = (outi / 'request.json').read_bytes()
            before[tag] = {'raw_sha': _sha(raw),
                           'prompt_sha256': json.loads(raw.decode('utf-8'))['prompt_sha256']}

        # 3) 客户已准许的 fallback：以另一入口（Qoder ok stub）接管失败任务，用新 task_id
        #    （不与失败的 T-FB429 同 id 重发），第三个 barrier 成员放行全部在途任务。
        wsfb = self._mk_workspace('fbtake')
        pfb = self._write('prompt-fbtake.txt', 'Qoder 接管 fallback 任务。\n')
        plfb = self._plan('T-FB-TAKE', 'BW-FB-TAKE', 'qoder', 'Qwen3.8-Flash', wsfb,
                          pfb, ['Read'])
        fb_report = self._write('report-fbtake.txt',
                                build_bound_report('BW-FB-TAKE', str(wsfb)))
        fb_proc = subprocess.run(self._qoder_cmd(wsfb, pfb, wsfb / 'out',
                                                 'BW-FB-TAKE', plfb),
                                 env=self._base_env('fbtake', wsfb, fb_report,
                                                    barrier_total=3),
                                 cwd=str(wsfb), capture_output=True, timeout=120)
        self.assertEqual(fb_proc.returncode, 0,
                         fb_proc.stderr.decode('utf-8', 'replace'))
        fb_summary = json.loads((wsfb / 'out' / 'summary.json').read_text(encoding='utf-8'))
        self.assertTrue(fb_summary['report_bound'])

        # 4) join 两个在途任务；证明 fallback 期间它们的 prompt/request 哈希一字未改。
        for tag, (pr, outi) in inflight.items():
            out, err = pr.communicate(timeout=120)
            self.assertEqual(pr.returncode, 0, err.decode('utf-8', 'replace'))
            raw_after = (outi / 'request.json').read_bytes()
            self.assertEqual(_sha(raw_after), before[tag]['raw_sha'],
                             f'{tag} request.json bytes changed during fallback')
            self.assertEqual(json.loads(raw_after.decode('utf-8'))['prompt_sha256'],
                             before[tag]['prompt_sha256'],
                             f'{tag} prompt hash changed during fallback')
        # 失败任务 summary 哈希仍在 fallback 之前保存，未被覆盖。
        saved = (self.base / 'failure-evidence-fb429' / 'summary-sha256.txt').read_text()
        self.assertEqual(saved.strip(), fail_sha)

    def test_plan_rejections_sent_false(self):
        # 同 task_id 在途：锁定 prompt 不得重发/改 prompt
        active = [{'task_id': 'T1', 'state': 'executing',
                   'workspace': str(self.base / 'a'), 'prompt_sha256': 'a' * 64,
                   'writes': False, 'shared_writes': []}]
        plan = self._make_plan('T1', 'qoder', self.base / 'a', 'a' * 64)
        r = ec.preflight(plan, self._make_actual('T1', 'qoder', self.base / 'a',
                                                 'a' * 64, ['Read']),
                         active_tasks=active, max_concurrency=4)
        self.assertFalse(r['ok'])
        self.assertFalse(r['sent'])
        self.assertTrue(any('already in flight' in x for x in r['reasons']))

        # 同 workspace 写入冲突
        active = [{'task_id': 'T0', 'state': 'executing',
                   'workspace': str(self.base / 'a'), 'prompt_sha256': 'b' * 64,
                   'writes': True, 'shared_writes': []}]
        plan = self._make_plan('T1', 'qoder', self.base / 'a', 'c' * 64,
                               rules=['Read', 'Edit(/output.py)'])
        r = ec.preflight(plan, self._make_actual('T1', 'qoder', self.base / 'a',
                                                 'c' * 64, ['Read', 'Edit(/output.py)']),
                         active_tasks=active, max_concurrency=4)
        self.assertFalse(r['ok'])
        self.assertTrue(any('workspace write conflict' in x for x in r['reasons']))

        # 依赖未完成 / 非 passed 都不放行（五状态无 verified）
        plan = self._make_plan('T1', 'qoder', self.base / 'a', 'd' * 64,
                               depends_on=['T0'])
        r = ec.preflight(plan, self._make_actual('T1', 'qoder', self.base / 'a',
                                                 'd' * 64, ['Read']),
                         active_tasks=[{'task_id': 'T0', 'state': 'executing',
                                        'workspace': str(self.base / 'z'),
                                        'prompt_sha256': 'e' * 64, 'writes': False,
                                        'shared_writes': []}], max_concurrency=4)
        self.assertFalse(r['ok'])
        self.assertTrue(any('not completed' in x for x in r['reasons']), r['reasons'])
        r2 = ec.preflight(plan, self._make_actual('T1', 'qoder', self.base / 'a',
                                                  'd' * 64, ['Read']),
                          active_tasks=[{'task_id': 'T0', 'state': 'completed',
                                         'acceptance_result': 'failed',
                                         'workspace': str(self.base / 'z'),
                                         'prompt_sha256': 'e' * 64, 'writes': False,
                                         'shared_writes': []}], max_concurrency=4)
        self.assertFalse(r2['ok'])
        self.assertTrue(any('acceptance_result' in x for x in r2['reasons']),
                        r2['reasons'])

        # 缺 grant：计划要求 Edit，实际未授予
        plan = self._make_plan('T1', 'qoder', self.base / 'a', 'f' * 64,
                               rules=['Read', 'Edit(/never.py)'])
        r = ec.preflight(plan, self._make_actual('T1', 'qoder', self.base / 'a',
                                                 'f' * 64, ['Read']))
        self.assertFalse(r['ok'])
        self.assertTrue(any('missing planned grant' in x for x in r['reasons']))

    def _make_plan(self, task_id, runtime, ws, prompt_sha, *, rules=None,
                   depends_on=None):
        rules = rules or ['Read']
        if runtime == 'zcode':
            g = ec.grants_from_rules([], [], [], ['Read'])
        else:
            g = ec.grants_from_rules(rules, [], [], ['Read'])
        return {'task_id': task_id, 'stage': 'S', 'runtime': runtime,
                'model': 'm', 'workspace': str(ws), 'cwd': str(ws),
                'prompt_sha256': prompt_sha,
                'grants': {'edits': g['edits'], 'bash': g['bash'],
                           'read_dirs': g['read_dirs']},
                'tool_visibility': g['tool_visibility'],
                'visible_tools': g['visible_tools'], 'allowed_tools': g['allowed_tools'],
                'disallowed_tools': g['disallowed_tools'], 'active_tasks': [],
                'depends_on': depends_on or [],
                'shared_writes': [], 'max_concurrency': 4}

    def _make_actual(self, task_id, runtime, ws, prompt_sha, rules):
        if runtime == 'zcode':
            g = ec.grants_from_rules([], [], [], ['Read'])
        else:
            g = ec.grants_from_rules(rules, [], [], ['Read'])
        return {'task_id': task_id, 'stage': 'S', 'runtime': runtime, 'model': 'm',
                'workspace': str(ws), 'cwd': str(ws), 'prompt_sha256': prompt_sha,
                'argv': ['node', 'cli', '-p'], 'shell': False, 'grants': g}

    def test_codebuddy_missing_grant_rejects_zero_spawn(self):
        ws = self._mk_workspace('reject')
        prompt = self._write('prompt-reject.txt', 'CodeBuddy 拒绝演练任务。\n')
        # 计划要求一个入口不会实际授予的 Edit grant → 缺 grant → rc 2、零目录
        p = self.base / 'plan-reject.json'
        p.write_text(json.dumps({
            'task_id': 'T-RJ', 'stage': 'BW-PARR-REJECT', 'runtime': 'codebuddy',
            'model': 'CB-PARR-429', 'workspace': str(ws), 'cwd': str(ws),
            'prompt_sha256': hashlib.sha256(prompt.read_bytes()).hexdigest(),
            'grants': {'edits': ['Edit(/never/granted.py)'], 'bash': [], 'read_dirs': []},
            'tool_visibility': ['Read'], 'visible_tools': ['Read'],
            'allowed_tools': ['Read', 'Edit(/never/granted.py)'], 'disallowed_tools': [],
            'active_tasks': [], 'depends_on': [], 'shared_writes': [],
            'max_concurrency': 1}), encoding='utf-8', newline='\n')
        out = ws / 'out'
        cmd = self._codebuddy_cmd(ws, prompt, out, 'BW-PARR-REJECT', p)
        env = self._base_env('reject', ws)
        proc = subprocess.run(cmd, env=env, cwd=str(ws), capture_output=True,
                              timeout=120)
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(out.exists())
        self.assertIn('dispatch_plan_rejected',
                      proc.stdout.decode('utf-8', 'replace'))


if __name__ == '__main__':
    unittest.main()
