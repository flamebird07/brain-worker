"""Offline tests for scripts/codebuddy_direct.py.

真实子进程stub：config.node=sys.executable、config.cli=临时 stub 脚本，stub 模拟
node <cli> 的 argv/stdin/stdout 形状，不需要安装 Node 或 CodeBuddy CLI，无网络。
测试真实呼叫入口脚本（subprocess 调 codebuddy_direct.py），不只测文案。
所有读写强制 UTF-8 或字节，避免 Windows 默认 GBK 破坏中文；tempdir 各自隔离，
不依赖主脑私有路径。未在本机执行过真实 CodeBuddy——由主脑后续真实派工验证。
"""
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENTRY = REPO / 'scripts' / 'codebuddy_direct.py'

STUB_SOURCE = '''\
import base64, hashlib, json, os, sys

record_path = os.environ["CODEBUDDY_STUB_RECORD"]
with open(os.environ["CODEBUDDY_STUB_SPEC"], "rb") as f:
    spec = json.loads(f.read().decode("utf-8"))
stdin_bytes = sys.stdin.buffer.read()
record = {
    "argv": sys.argv,
    "cwd": os.getcwd(),
    "stdin_sha256": hashlib.sha256(stdin_bytes).hexdigest(),
    "stdin_len": len(stdin_bytes),
    "stdin_has_crlf": b"\\r\\n" in stdin_bytes,
    "env_disable_autoupdater": os.environ.get("DISABLE_AUTOUPDATER"),
}
with open(record_path, "wb") as f:
    f.write(json.dumps(record, ensure_ascii=False).encode("utf-8"))
out = sys.stdout.buffer
for b64 in spec.get("stdout_raw_b64", []):
    out.write(base64.b64decode(b64))
    out.write(b"\\n")
for line in spec.get("stdout", []):
    out.write(line.encode("utf-8"))
    out.write(b"\\n")
out.flush()
sys.stderr.buffer.write(spec.get("stderr", "").encode("utf-8"))
sys.stderr.buffer.flush()
sys.exit(spec.get("exit", 0))
'''


def read_json(path: Path):
    return json.loads(Path(path).read_bytes().decode('utf-8'))


def init_event(session_id='S-1', model='GLM-CB-1', omit=None,
               mcp_servers=None, permission_mode='dontAsk'):
    event = {'type': 'system', 'subtype': 'init', 'session_id': session_id,
             'model': model, 'tools': ['Read'], 'permissionMode': permission_mode,
             'mcp_servers': [] if mcp_servers is None else mcp_servers}
    for key in omit or []:
        event.pop(key, None)
    return event


def status_event(session_id='S-1'):
    return {'type': 'system', 'subtype': 'status', 'status': None,
            'session_id': session_id}


def assistant_event(model='GLM-CB-1', text='hello', omit=None, tool_uses=None,
                    session_id='S-1'):
    content = []
    if text is not None:
        content.append({'type': 'text', 'text': text})
    for tu in tool_uses or []:
        content.append({'type': 'tool_use', 'id': tu['id'], 'name': tu['name'],
                        'input': {}})
    message = {'model': model,
               'usage': {'input_tokens': 1, 'output_tokens': 2},
               'content': content}
    for key in omit or []:
        message.pop(key, None)
    return {'type': 'assistant', 'session_id': session_id, 'message': message}


def tool_result_event(tool_use_id='c1', text='ok', is_error=False, session_id='S-1'):
    return {'type': 'user', 'session_id': session_id,
            'message': {'role': 'user',
                        'content': [{'type': 'tool_result',
                                     'tool_use_id': tool_use_id,
                                     'content': [{'type': 'text', 'text': text}],
                                     'is_error': is_error}]}}


def result_event(session_id='S-1', text='report', subtype='success',
                 is_error=False, extra=None, omit=None):
    event = {'type': 'result', 'subtype': subtype, 'is_error': is_error,
             'result': text, 'session_id': session_id,
             'usage': {'input_tokens': 1, 'output_tokens': 2},
             'modelUsage': {'GLM-CB-1': {'input_tokens': 1}}}
    for key in omit or []:
        event.pop(key, None)
    event.update(extra or {})
    return event


def report_text(stage, project_path):
    return '\r\n'.join([
        'WORKER_REPORT_START',
        f'阶段编号与执行方式：{stage}；direct。',
        f'实际项目绝对路径：{project_path}',
        '汇报时间与执行环境：',
        '',
        '一、当前基线与授权',
        '- 接手时已验证的文件/版本/运行状态：',
        '- 本阶段获得的授权范围：',
        '- 本阶段禁止事项：',
        '- 交接来源（如更换 Agent）：',
        '',
        '二、实际执行范围',
        '- 实际执行的动作：',
        '- 变更文件及范围：',
        '- 未修改但检查过的关键文件或状态：',
        '',
        '三、已验证事实',
        '1. 事实：',
        '   证据位置/命令/回读：',
        '   验证环境：',
        '2. 事实：',
        '   证据位置/命令/回读：',
        '   验证环境：',
        '',
        '四、推断（必须与事实分开）',
        '- 推断及依据：',
        '- 仍未知的内容：',
        '',
        '五、测试与验证',
        '- 工作目录：',
        '- 原样命令：',
        '- 实际退出码：',
        '- 关键结果：',
        '- 未运行或未完成的验证及原因：',
        '',
        '六、未完成项与剩余风险',
        '- 未完成项：',
        '- 失败项：',
        '- 缺失证据：',
        '- 剩余风险：',
        '',
        '七、实际副作用与越界检查',
        '- 实际副作用：',
        '- 越界动作：',
        '- 敏感信息处理：仅写脱敏摘要，不写 Cookie、密钥、令牌或账务原文。',
        '',
        '八、本阶段状态',
        '- 状态（只能选一项）：阶段完成 / 部分完成 / 待补验证 / 阻塞 / 异常',
        '- 状态依据：',
        '- 是否满足本阶段验收标准：是 / 否 / 证据不足',
        '',
        '九、建议下一步（只提出建议，不执行）',
        '- 建议：',
        '',
        '本阶段汇报结束；等待主脑验收。',
        'WORKER_REPORT_END',
    ]) + '\r\n'


class CodeBuddyDirectTests(unittest.TestCase):
    STAGE = 'BW-CODEBUDDY-IMPLEMENT-01'
    MODEL = 'GLM-CB-1'

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.workspace = (self.tmp / 'ws').resolve()
        self.workspace.mkdir()
        self.record_path = self.tmp / 'stub-record.json'
        self.prompt = self.tmp / 'prompt.md'
        self.prompt.write_bytes('请原样汇报。\r\n第二行。'.encode('utf-8'))
        stub_path = self.tmp / 'codebuddy-stub.py'
        stub_path.write_bytes(STUB_SOURCE.encode('utf-8'))
        self.cli = stub_path
        config_path = self.tmp / 'codebuddy-entry.json'
        config_path.write_bytes(json.dumps({'node': sys.executable,
                                            'cli': str(self.cli)}).encode('utf-8'))
        self.config = config_path

    def tearDown(self):
        self._tmp.cleanup()

    def write_spec(self, events, *, exit_code=0, stderr='', raw_b64=None):
        spec_path = self.tmp / 'stub-spec.json'
        payload = {
            'stdout': [json.dumps(e, ensure_ascii=False) for e in events],
            'stderr': stderr, 'exit': exit_code}
        if raw_b64:
            payload['stdout_raw_b64'] = raw_b64
        spec_path.write_bytes(json.dumps(payload).encode('utf-8'))
        return spec_path

    def run_entry(self, out_dir, spec_path, *extra_args, prompt=None):
        if prompt is not None:
            self.prompt.write_bytes(prompt)
        out_dir = Path(out_dir)
        proc = subprocess.run(
            [sys.executable, str(ENTRY),
             '--workspace', str(self.workspace),
             '--prompt-file', str(self.prompt),
             '--output-dir', str(out_dir),
             '--stage', self.STAGE,
             '--model', self.MODEL,
             '--config', str(self.config),
             *extra_args],
            capture_output=True, cwd=str(self.tmp),
            env={'CODEBUDDY_STUB_RECORD': str(self.record_path),
                 'CODEBUDDY_STUB_SPEC': str(spec_path),
                 'SYSTEMROOT': os.environ.get('SYSTEMROOT', ''),
                 'PYTHONIOENCODING': 'utf-8'})
        result = {'proc': proc, 'out': out_dir, 'rc': proc.returncode}
        if (out_dir / 'summary.json').is_file():
            result['summary'] = read_json(out_dir / 'summary.json')
        if self.record_path.is_file():
            result['record'] = read_json(self.record_path)
        return result

    def success_events(self, *, model=None, session_id='S-1', text=None,
                       init_extra=None, result_extra=None):
        model = model or self.MODEL
        text = text if text is not None else report_text(self.STAGE, str(self.workspace))
        init = init_event(session_id=session_id, model=model)
        init.update(init_extra or {})
        return [init, status_event(session_id=session_id),
                assistant_event(model=model, text=text, session_id=session_id),
                result_event(session_id=session_id, text=text, extra=result_extra)]

    # ---- 成功路径：真实 status + assistant + result，CRLF stdin，argv 规则 ----
    def test_success_binds_with_crlf_stdin_and_argv_rules(self):
        spec = self.write_spec(self.success_events())
        out = self.tmp / 'out-success'
        res = self.run_entry(out, spec,
                             '--tools', 'Read,Grep',
                             '--allowed-tools', 'Read',
                             '--allowed-tools', 'Edit(/scripts/x.py)')
        self.assertEqual(res['rc'], 0, res['proc'].stderr.decode('utf-8', 'replace'))
        self.assertTrue(res['summary']['protocol_success'], res['summary']['parse_errors'])
        self.assertTrue(res['summary']['report_bound'])
        self.assertEqual(res['summary']['session_id'], 'S-1')
        rec = res['record']
        prompt_bytes = self.prompt.read_bytes()
        self.assertTrue(b'\r\n' in prompt_bytes)
        self.assertTrue(rec['stdin_has_crlf'])
        self.assertEqual(rec['cwd'], str(self.workspace.resolve()))
        self.assertEqual(rec['env_disable_autoupdater'], '1')
        argv = rec['argv']
        # stub 以 `python <cli> ...` 启动，解释器（node 角色）不出现在子进程
        # sys.argv 中，argv[0] 即 cli 路径；入口记录的完整 argv 首项为 node。
        self.assertEqual(argv[0], str(self.cli))
        self.assertEqual(
            argv[1:],
            ['-p', '--verbose', '--output-format', 'stream-json',
             '--model', self.MODEL, '--permission-mode', 'dontAsk',
             '--tools', 'Read,Grep',
             '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
             '--setting-sources', '',
             '--settings', '{"disableAllHooks":true}',
             '--agent', 'cli', '--no-session-persistence',
             '--allowedTools', 'Read', 'Edit(/scripts/x.py)'])
        request = read_json(out / 'request.json')
        # request.json 记录含 node 前导的完整 argv；子进程 argv 去掉 node 后应一致
        self.assertEqual(request['argv'][0], sys.executable)
        self.assertEqual(request['argv'][1:], argv)
        self.assertEqual(request['prompt_sha256'],
                         hashlib.sha256(prompt_bytes).hexdigest())
        self.assertNotIn('prompt_text', request)  # 原文只经 stdin 传输，不重复落盘
        self.assertEqual(request['stage'], self.STAGE)
        self.assertEqual(request['model_requested'], self.MODEL)
        process = read_json(out / 'process.json')
        self.assertEqual(process['exit_code'], 0)
        self.assertTrue((out / 'stdout.jsonl').is_file())
        self.assertTrue((out / 'stderr.log').is_file())
        response = (out / 'response.md').read_bytes()
        self.assertTrue(response.startswith(b'WORKER_REPORT_START'))
        self.assertTrue(response.endswith(b'WORKER_REPORT_END\r\n'))
        report_state = read_json(out / 'report-state.json')
        self.assertTrue(report_state['bound'])
        self.assertEqual(report_state['markers']['start_substring_count'], 1)
        self.assertEqual(report_state['markers']['end_substring_count'], 1)

    # ---- --tools 永远恰好一次、单一 comma 值 ----
    def test_empty_tools_still_emits_single_empty_comma_value(self):
        spec = self.write_spec(self.success_events())
        out = self.tmp / 'out-emptytools'
        res = self.run_entry(out, spec)  # 不传 --tools，默认 ''
        self.assertEqual(res['rc'], 0, res['proc'].stderr.decode('utf-8', 'replace'))
        argv = res['record']['argv']
        tools_positions = [i for i, a in enumerate(argv) if a == '--tools']
        self.assertEqual(len(tools_positions), 1)
        i = tools_positions[0]
        self.assertEqual(argv[i + 1], '')               # 空也必须有一个 '' 值
        self.assertEqual(argv[i + 2], '--strict-mcp-config')  # 不是逐项 arg
        self.assertNotIn('--allowedTools', argv)        # 空 tools 回退不追加规则

    def test_tools_is_single_comma_value_not_repeated(self):
        spec = self.write_spec(self.success_events())
        out = self.tmp / 'out-toolscomma'
        res = self.run_entry(out, spec, '--tools', 'Read,Grep')
        self.assertEqual(res['rc'], 0, res['proc'].stderr.decode('utf-8', 'replace'))
        argv = res['record']['argv']
        i = argv.index('--tools')
        self.assertEqual(argv[i + 1], 'Read,Grep')      # 一个 comma 字符串
        self.assertEqual(argv[i + 2], '--strict-mcp-config')  # 其后不接工具名

    def test_tools_fallback_when_no_explicit_allow(self):
        spec = self.write_spec(self.success_events())
        out = self.tmp / 'out-fallback'
        res = self.run_entry(out, spec, '--tools', 'Read,Glob')
        self.assertEqual(res['rc'], 0, res['proc'].stderr.decode('utf-8', 'replace'))
        argv = res['record']['argv']
        i = argv.index('--allowedTools')
        self.assertEqual(argv[i + 1:][:2], ['Read', 'Glob'])

    def test_explicit_allow_replaces_tools_fallback(self):
        spec = self.write_spec(self.success_events())
        out = self.tmp / 'out-explicit'
        res = self.run_entry(out, spec, '--tools', 'Read,Glob',
                             '--allowed-tools', 'Bash(python -V)')
        self.assertEqual(res['rc'], 0, res['proc'].stderr.decode('utf-8', 'replace'))
        argv = res['record']['argv']
        i = argv.index('--allowedTools')
        # 显式 allow 完全替代回退：allowed-tools 后只有这一条规则（到下一 flag 前）
        rules = []
        for token in argv[i + 1:]:
            if token.startswith('--'):
                break
            rules.append(token)
        self.assertEqual(rules, ['Bash(python -V)'])
        # --tools 可见性参数仍保留单一 comma 值
        self.assertEqual(argv[argv.index('--tools') + 1], 'Read,Glob')

    def test_disallowed_rules_passed_variadic(self):
        spec = self.write_spec(self.success_events())
        out = self.tmp / 'out-disallowed'
        res = self.run_entry(out, spec, '--tools', 'Read',
                             '--disallowed-tools', 'Bash(rm -rf /)',
                             '--disallowed-tools', 'Edit(/secret.txt)')
        self.assertEqual(res['rc'], 0, res['proc'].stderr.decode('utf-8', 'replace'))
        argv = res['record']['argv']
        i = argv.index('--disallowedTools')
        rules = []
        for token in argv[i + 1:]:
            if token.startswith('--'):
                break
            rules.append(token)
        self.assertEqual(rules, ['Bash(rm -rf /)', 'Edit(/secret.txt)'])

    def test_resume_flag_passed(self):
        spec = self.write_spec(self.success_events())
        out = self.tmp / 'out-resume'
        res = self.run_entry(out, spec, '--resume-session-id', 'S-1')
        self.assertEqual(res['rc'], 0, res['proc'].stderr.decode('utf-8', 'replace'))
        self.assertIn('--resume', res['record']['argv'])

    # ---- 真实 user/tool_result 通过（is_error=false，无拒绝文案）----
    def test_real_tool_result_user_event_passes(self):
        rep = report_text(self.STAGE, str(self.workspace))
        events = [init_event(), status_event(),
                  assistant_event(model=self.MODEL, text=None,
                                  tool_uses=[{'id': 'c1', 'name': 'Read'}]),
                  tool_result_event(tool_use_id='c1', text='file contents',
                                    is_error=False),
                  assistant_event(model=self.MODEL, text=rep),
                  result_event(text=rep)]
        out = self.tmp / 'out-toolresult'
        res = self.run_entry(out, self.write_spec(events), '--tools', 'Read')
        self.assertEqual(res['rc'], 0, res['proc'].stderr.decode('utf-8', 'replace'))
        self.assertTrue(res['summary']['protocol_success'], res['summary']['parse_errors'])
        self.assertIn('Read', res['summary']['observed_tool_calls'])

    # ---- 权限拒绝在 tool_result 文本、is_error=false、result.permission_denials=[] ----
    def test_permission_denial_in_tool_result_text_fail_closed(self):
        rep = report_text(self.STAGE, str(self.workspace))
        denied = ('Error: Permission to use Bash has been denied because this tool '
                  'requires approval but permission prompts are not available in '
                  'non-interactive mode.')
        events = [init_event(), status_event(),
                  assistant_event(model=self.MODEL, text=None,
                                  tool_uses=[{'id': 'cb', 'name': 'Bash'}]),
                  tool_result_event(tool_use_id='cb', text=denied, is_error=False),
                  assistant_event(model=self.MODEL, text=rep),
                  result_event(text=rep, extra={'permission_denials': []})]
        out = self.tmp / 'out-denialtext'
        res = self.run_entry(out, self.write_spec(events), '--tools', 'Read,Bash')
        self.assertEqual(res['rc'], 3)
        self.assertFalse(res['summary']['protocol_success'])
        self.assertFalse(res['summary']['report_bound'])
        denials = res['summary']['permission_denials']
        self.assertTrue(denials)
        self.assertTrue(any(d.get('source') == 'user.tool_result.content.text'
                            for d in denials))
        self.assertEqual(denials[0]['tool_use_id'], 'cb')
        # 空数组 result.permission_denials 不被当作无拒绝
        self.assertTrue(any('denied' in str(d).lower() for d in denials))

    def test_quoted_denial_in_report_text_does_not_trigger(self):
        # 模型报告正文里普通引用 "Permission ... denied" 字样不应被误判为运行时拒绝
        # （拒绝只在 user.tool_result 中扫描）
        rep = report_text(self.STAGE, str(self.workspace))
        events = [init_event(), status_event(),
                  assistant_event(model=self.MODEL, text=rep),
                  result_event(text=rep)]
        out = self.tmp / 'out-quoteonly'
        res = self.run_entry(out, self.write_spec(events))
        self.assertEqual(res['rc'], 0, res['proc'].stderr.decode('utf-8', 'replace'))
        self.assertEqual(res['summary']['permission_denials'], [])

    # ---- 实际调用工具超出白名单 ----
    def test_tool_use_outside_whitelist_rejects(self):
        rep = report_text(self.STAGE, str(self.workspace))
        events = [init_event(), status_event(),
                  assistant_event(model=self.MODEL, text=None,
                                  tool_uses=[{'id': 'cw', 'name': 'WebSearch'}]),
                  assistant_event(model=self.MODEL, text=rep),
                  result_event(text=rep)]
        out = self.tmp / 'out-overreach'
        res = self.run_entry(out, self.write_spec(events), '--tools', 'Read')
        self.assertEqual(res['rc'], 3)
        self.assertFalse(res['summary']['protocol_success'])
        self.assertIn('WebSearch', res['summary']['observed_tool_calls'])
        self.assertTrue(any('WebSearch' in e for e in res['summary']['parse_errors']))

    # ---- preflight：退出码 2，输出目录零创建，零派工 ----
    def test_preflight_bad_tools_rejected_zero_spawn(self):
        spec = self.write_spec(self.success_events())
        out = self.tmp / 'out-badtool'
        res = self.run_entry(out, spec, '--tools', 'Read,WebSearch')
        self.assertEqual(res['rc'], 2)
        self.assertFalse(out.exists())
        self.assertFalse(self.record_path.is_file())
        self.assertNotIn('summary', res)

    def test_preflight_duplicate_tools_rejected(self):
        spec = self.write_spec(self.success_events())
        out = self.tmp / 'out-duptool'
        res = self.run_entry(out, spec, '--tools', 'Read,Read')
        self.assertEqual(res['rc'], 2)
        self.assertFalse(out.exists())

    def test_preflight_existing_output_dir_rejected_zero_spawn(self):
        out = self.tmp / 'out-exists'
        out.mkdir()
        spec = self.write_spec(self.success_events())
        res = self.run_entry(out, spec)
        self.assertEqual(res['rc'], 2)
        self.assertEqual(list(out.iterdir()), [])
        self.assertFalse(self.record_path.is_file())

    def test_preflight_missing_workspace_rejected(self):
        spec = self.write_spec(self.success_events())
        out = self.tmp / 'out-nows'
        proc = subprocess.run(
            [sys.executable, str(ENTRY), '--workspace', str(self.tmp / 'nope'),
             '--prompt-file', str(self.prompt), '--output-dir', str(out),
             '--stage', self.STAGE, '--model', self.MODEL,
             '--config', str(self.config)],
            capture_output=True, cwd=str(self.tmp),
            env={'CODEBUDDY_STUB_RECORD': str(self.record_path),
                 'CODEBUDDY_STUB_SPEC': str(spec),
                 'SYSTEMROOT': os.environ.get('SYSTEMROOT', ''),
                 'PYTHONIOENCODING': 'utf-8'})
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(out.exists())

    def test_preflight_empty_stage_rejected(self):
        spec = self.write_spec(self.success_events())
        out = self.tmp / 'out-nostage'
        proc = subprocess.run(
            [sys.executable, str(ENTRY), '--workspace', str(self.workspace),
             '--prompt-file', str(self.prompt), '--output-dir', str(out),
             '--stage', '  ', '--model', self.MODEL,
             '--config', str(self.config)],
            capture_output=True, cwd=str(self.tmp),
            env={'CODEBUDDY_STUB_RECORD': str(self.record_path),
                 'CODEBUDDY_STUB_SPEC': str(spec),
                 'SYSTEMROOT': os.environ.get('SYSTEMROOT', ''),
                 'PYTHONIOENCODING': 'utf-8'})
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(out.exists())

    # ---- 模型身份 ----
    def test_init_model_mismatch_rejects(self):
        events = self.success_events(model='OTHER-MODEL')
        res = self.run_entry(self.tmp / 'out-initmodel', self.write_spec(events))
        self.assertEqual(res['rc'], 3)
        self.assertFalse(res['summary']['protocol_success'])
        self.assertFalse(res['summary']['report_bound'])
        self.assertEqual(res['summary']['observed_models'][0], 'OTHER-MODEL')

    def test_assistant_model_mismatch_rejects(self):
        rep = report_text(self.STAGE, str(self.workspace))
        events = [init_event(), status_event(),
                  assistant_event(model='OTHER-MODEL', text=rep),
                  result_event(text=rep)]
        res = self.run_entry(self.tmp / 'out-asstmodel', self.write_spec(events))
        self.assertEqual(res['rc'], 3)
        self.assertIn('OTHER-MODEL', res['summary']['observed_models'])
        self.assertFalse(res['summary']['protocol_success'])

    def test_missing_model_evidence_rejects(self):
        rep = report_text(self.STAGE, str(self.workspace))
        events = [init_event(), status_event(),
                  assistant_event(text=rep, omit=['model']),
                  result_event(text=rep)]
        res = self.run_entry(self.tmp / 'out-nomodel', self.write_spec(events))
        self.assertEqual(res['rc'], 3)
        self.assertTrue(res['summary']['parse_errors'])

    # ---- 会话漂移 ----
    def test_session_drift_in_events_rejects(self):
        rep = report_text(self.STAGE, str(self.workspace))
        events = [init_event(session_id='S-1'),
                  {'type': 'system', 'subtype': 'status', 'session_id': 'S-DRIFT'},
                  assistant_event(text=rep, session_id='S-1'),
                  result_event(session_id='S-1', text=rep)]
        res = self.run_entry(self.tmp / 'out-drift', self.write_spec(events))
        self.assertEqual(res['rc'], 3)
        self.assertFalse(res['summary']['protocol_success'])
        self.assertTrue(any('session_id drift' in e
                            for e in res['summary']['parse_errors']))

    # ---- MCP 严格空 ----
    def test_nonempty_mcp_servers_rejects(self):
        events = self.success_events(init_extra={'mcp_servers': [{'name': 'x'}]})
        res = self.run_entry(self.tmp / 'out-mcp', self.write_spec(events))
        self.assertEqual(res['rc'], 3)
        self.assertFalse(res['summary']['protocol_success'])
        self.assertTrue(any('mcp_servers' in e for e in res['summary']['parse_errors']))

    # ---- 无 assistant / 空 assistant text ----
    def test_no_assistant_rejects(self):
        rep = report_text(self.STAGE, str(self.workspace))
        events = [init_event(), status_event(), result_event(text=rep)]
        res = self.run_entry(self.tmp / 'out-noasst', self.write_spec(events))
        self.assertEqual(res['rc'], 3)
        self.assertFalse(res['summary']['protocol_success'])

    def test_assistant_empty_text_rejects(self):
        rep = report_text(self.STAGE, str(self.workspace))
        events = [init_event(), status_event(),
                  assistant_event(text='   '), result_event(text=rep)]
        res = self.run_entry(self.tmp / 'out-emptyasst', self.write_spec(events))
        self.assertEqual(res['rc'], 3)
        self.assertTrue(any('non-empty text' in e for e in res['summary']['parse_errors']))

    # ---- 最终 result 与最后 assistant text 不一致 ----
    def test_result_mismatch_with_last_assistant_rejects(self):
        rep = report_text(self.STAGE, str(self.workspace))
        events = [init_event(), status_event(),
                  assistant_event(text=rep), result_event(text=rep + 'T')]
        res = self.run_entry(self.tmp / 'out-mismatch', self.write_spec(events))
        self.assertEqual(res['rc'], 3)
        self.assertFalse(res['summary']['protocol_success'])
        self.assertTrue(any('exactly equal' in e for e in res['summary']['parse_errors']))

    # ---- 终态与流校验 ----
    def test_result_permission_denials_array_fail_closed(self):
        rep = report_text(self.STAGE, str(self.workspace))
        events = [init_event(), status_event(), assistant_event(text=rep),
                  result_event(text=rep,
                               extra={'permission_denials': [{'tool': 'Bash'}]})]
        res = self.run_entry(self.tmp / 'out-denied', self.write_spec(events))
        self.assertEqual(res['rc'], 3)
        self.assertTrue(res['summary']['permission_denials'])
        self.assertFalse(res['summary']['protocol_success'])
        self.assertFalse(res['summary']['report_bound'])
        self.assertTrue((res['out'] / 'stdout.jsonl').is_file())

    def test_cancelled_terminal_reason_rejects(self):
        events = self.success_events(result_extra={'terminal_reason': 'cancelled'})
        res = self.run_entry(self.tmp / 'out-cancelled', self.write_spec(events))
        self.assertEqual(res['rc'], 3)
        self.assertFalse(res['summary']['protocol_success'])

    def test_nonzero_exit_rejects(self):
        spec = self.write_spec(self.success_events(), exit_code=1, stderr='boom')
        out = self.tmp / 'out-nonzero2'
        res = self.run_entry(out, spec)
        self.assertEqual(res['rc'], 3)
        self.assertEqual(res['summary']['exit_code'], 1)
        self.assertFalse(res['summary']['protocol_success'])
        self.assertIn('non-zero', ' '.join(res['summary']['parse_errors']))

    def test_missing_result_session_rejects(self):
        rep = report_text(self.STAGE, str(self.workspace))
        events = [init_event(), status_event(), assistant_event(text=rep),
                  result_event(omit=['session_id'], text=rep)]
        res = self.run_entry(self.tmp / 'out-nosess', self.write_spec(events))
        self.assertEqual(res['rc'], 3)
        self.assertFalse(res['summary']['report_bound'])

    def test_init_missing_session_rejects(self):
        rep = report_text(self.STAGE, str(self.workspace))
        events = [init_event(omit=['session_id']), status_event(),
                  assistant_event(text=rep), result_event(text=rep)]
        res = self.run_entry(self.tmp / 'out-noinitsess', self.write_spec(events))
        self.assertEqual(res['rc'], 3)

    def test_duplicate_result_rejects(self):
        events = self.success_events()
        rep = report_text(self.STAGE, str(self.workspace))
        events.append(result_event(text=rep))
        res = self.run_entry(self.tmp / 'out-dupresult', self.write_spec(events))
        self.assertEqual(res['rc'], 3)
        self.assertFalse(res['summary']['protocol_success'])

    def test_trailing_non_json_rejects(self):
        spec_path = self.tmp / 'stub-spec-trailing.json'
        lines = [json.dumps(e, ensure_ascii=False) for e in self.success_events()]
        lines.append('NOT JSON AT ALL')
        spec_path.write_bytes(json.dumps({'stdout': lines, 'stderr': '',
                                          'exit': 0}).encode('utf-8'))
        res = self.run_entry(self.tmp / 'out-trailing', spec_path)
        self.assertEqual(res['rc'], 3)
        self.assertTrue(any('non-JSON' in e for e in res['summary']['parse_errors']))

    def test_missing_result_event_rejects(self):
        events = [init_event(), status_event(),
                  assistant_event(text=report_text(self.STAGE, str(self.workspace)))]
        res = self.run_entry(self.tmp / 'out-noresult', self.write_spec(events))
        self.assertEqual(res['rc'], 3)

    def test_invalid_utf8_bytes_rejected_no_replace(self):
        bad = b'{"type":"system","subtype":"init","session_id":"\xff\xfe","' \
              b'model":"x","tools":[],"mcp_servers":[]}'
        spec_path = self.tmp / 'stub-spec-badutf8.json'
        spec_path.write_bytes(json.dumps({
            'stdout': [], 'stdout_raw_b64': [base64.b64encode(bad).decode('ascii')],
            'stderr': '', 'exit': 0}).encode('utf-8'))
        out = self.tmp / 'out-badutf8'
        res = self.run_entry(out, spec_path)
        self.assertEqual(res['rc'], 3)
        self.assertFalse(res['summary']['protocol_success'])
        self.assertTrue(any('valid UTF-8' in e for e in res['summary']['parse_errors']))
        # 原件按字节保留，未被 replace 破坏
        self.assertEqual((out / 'stdout.jsonl').read_bytes(), bad + b'\n')

    def test_unknown_event_type_rejects(self):
        rep = report_text(self.STAGE, str(self.workspace))
        events = [init_event(), {'type': 'weird', 'foo': 1},
                  assistant_event(text=rep), result_event(text=rep)]
        res = self.run_entry(self.tmp / 'out-unknown', self.write_spec(events))
        self.assertEqual(res['rc'], 3)
        self.assertTrue(any('unknown event type' in e
                            for e in res['summary']['parse_errors']))

    # ---- 报告绑定 ----
    def test_report_preface_rejects_and_originals_kept(self):
        text = '前言说明\n' + report_text(self.STAGE, str(self.workspace))
        # 最终 result 与最后 assistant 需一致，两者都用带前言文本
        events = [init_event(), status_event(),
                  assistant_event(text=text), result_event(text=text)]
        res = self.run_entry(self.tmp / 'out-preface', self.write_spec(events))
        self.assertEqual(res['rc'], 3)
        self.assertFalse(res['summary']['report_bound'])
        self.assertEqual((res['out'] / 'response.md').read_bytes(),
                         text.encode('utf-8'))

    def test_report_wrong_stage_rejects(self):
        rep = report_text('WRONG-STAGE', str(self.workspace))
        events = [init_event(), status_event(),
                  assistant_event(text=rep), result_event(text=rep)]
        res = self.run_entry(self.tmp / 'out-stage', self.write_spec(events))
        self.assertEqual(res['rc'], 3)
        self.assertFalse(res['summary']['report_bound'])

    def test_report_wrong_path_rejects(self):
        rep = report_text(self.STAGE, 'C:\\somewhere\\else')
        events = [init_event(), status_event(),
                  assistant_event(text=rep), result_event(text=rep)]
        res = self.run_entry(self.tmp / 'out-path', self.write_spec(events))
        self.assertEqual(res['rc'], 3)
        self.assertFalse(res['summary']['report_bound'])
        self.assertEqual((res['out'] / 'response.md').read_bytes(),
                         rep.encode('utf-8'))

    def test_resume_session_mismatch_rejects_binding(self):
        events = self.success_events(session_id='S-ACTUAL')
        res = self.run_entry(self.tmp / 'out-resume-mismatch',
                             self.write_spec(events),
                             '--resume-session-id', 'S-REQUESTED')
        self.assertEqual(res['rc'], 3)
        self.assertFalse(res['summary']['report_bound'])

    def test_empty_result_text_rejects(self):
        rep = report_text(self.STAGE, str(self.workspace))
        events = [init_event(), status_event(), assistant_event(text=rep),
                  result_event(text='   ')]
        res = self.run_entry(self.tmp / 'out-emptytext', self.write_spec(events))
        self.assertEqual(res['rc'], 3)

    # ---- 计费口径 ----
    def test_billing_flags_false_and_raw_usage_saved(self):
        res = self.run_entry(self.tmp / 'out-billing',
                             self.write_spec(self.success_events()))
        summary = res['summary']
        self.assertFalse(summary['business_verified'])
        self.assertFalse(summary['free_quota_verified'])
        self.assertFalse(summary['model_backend_identity_verified'])
        self.assertEqual(summary['usage'], {'input_tokens': 1, 'output_tokens': 2})
        self.assertEqual(summary['model_usage'],
                         {'GLM-CB-1': {'input_tokens': 1}})

    # ---- 失败信封：429 / error_during_execution / is_error=true，无 result 文本 ----
    # 精简合成流复现原始失败形状（不复制真实业务流），真实子进程回归。
    def test_failure_envelope_429_parses_but_still_fails(self):
        rate_msg = ('429 您的使用量已超出频率限制，将在 2026-10-06 13:55:40 UTC+8 '
                    '重置，您也可以切换其他模型继续使用。')
        events = [init_event(), status_event(),
                  assistant_event(model=self.MODEL, text=rate_msg),
                  result_event(subtype='error_during_execution', is_error=True,
                               omit=['result'],
                               extra={'errors': [rate_msg],
                                      'errors_info': [{'status': 429, 'code': 6004,
                                                       'category': 'quota',
                                                       'details': rate_msg}]})]
        out = self.tmp / 'out-429'
        res = self.run_entry(out, self.write_spec(events))
        summary = res['summary']
        self.assertEqual(res['rc'], 3)
        self.assertFalse(summary['protocol_success'])
        self.assertFalse(summary['report_bound'])
        self.assertTrue(summary['failure_envelope'])
        self.assertEqual(summary['terminal_state'], 'error')
        self.assertEqual(summary['failure_stage'], 'result_terminal')
        self.assertIn('429', summary['primary_failure'])
        self.assertIn('重置', summary['reset_hint'])
        self.assertIn('rate_limited', summary['recoverability'])
        self.assertEqual(summary['result_errors'], [rate_msg])
        # 原始 errors_info 保留，不被当成“缺 result 字段”的结构损坏
        self.assertEqual(summary['errors_info'][0]['category'], 'quota')
        joined = ' '.join(summary['parse_errors'])
        self.assertNotIn('result event missing fields', joined)
        # CLI 退出码 0 也不能伪成功
        self.assertEqual(summary['exit_code'], 0)
        self.assertTrue((out / 'stdout.jsonl').is_file())
        self.assertFalse((out / 'response.md').exists())
        report_state = read_json(out / 'report-state.json')
        self.assertTrue(report_state['carrier_missing'])
        self.assertIn('429', report_state['primary_failure'])

    # ---- 成功 result 缺 result 字段仍拒绝（未过度放宽）----
    def test_success_result_missing_result_field_rejects(self):
        events = [init_event(), status_event(),
                  assistant_event(text=report_text(self.STAGE, str(self.workspace))),
                  result_event(omit=['result'])]
        res = self.run_entry(self.tmp / 'out-succnoresult', self.write_spec(events))
        self.assertEqual(res['rc'], 3)
        self.assertTrue(any('result event missing fields' in e
                            for e in res['summary']['parse_errors']))

    # ---- 身份稳定的重复 init：同会话重初始化被接受 ----
    def test_identity_stable_duplicate_init_accepted(self):
        rep = report_text(self.STAGE, str(self.workspace))
        events = [init_event(), status_event(),
                  assistant_event(text=rep), init_event(),
                  result_event(text=rep)]
        out = self.tmp / 'out-reinit-ok'
        res = self.run_entry(out, self.write_spec(events))
        self.assertEqual(res['rc'], 0, res['proc'].stderr.decode('utf-8', 'replace'))
        self.assertTrue(res['summary']['protocol_success'], res['summary']['parse_errors'])
        self.assertTrue(res['summary']['report_bound'])
        reinit = res['summary']['reinit_events']
        self.assertTrue(reinit)
        self.assertTrue(all(r['identical'] for r in reinit))
        self.assertEqual(reinit[0]['changed_fields'], [])

    # ---- 重复 init 模型漂移：拒绝 ----
    def test_duplicate_init_model_drift_rejects(self):
        rep = report_text(self.STAGE, str(self.workspace))
        drift = init_event(model='OTHER-MODEL')
        events = [init_event(), status_event(),
                  assistant_event(text=rep), drift, result_event(text=rep)]
        res = self.run_entry(self.tmp / 'out-reinit-drift', self.write_spec(events))
        self.assertEqual(res['rc'], 3)
        self.assertFalse(res['summary']['protocol_success'])
        self.assertTrue(any('identity drift' in e
                            for e in res['summary']['parse_errors']))
        drift_record = res['summary']['reinit_events'][0]
        self.assertFalse(drift_record['identical'])
        self.assertIn('model', drift_record['changed_fields'])

    # ---- 重复 init 缺身份字段（permissionMode）：拒绝 ----
    def test_duplicate_init_missing_identity_field_rejects(self):
        rep = report_text(self.STAGE, str(self.workspace))
        missing_perm = init_event()
        missing_perm.pop('permissionMode')
        events = [init_event(), status_event(),
                  assistant_event(text=rep), missing_perm, result_event(text=rep)]
        res = self.run_entry(self.tmp / 'out-reinit-missing', self.write_spec(events))
        self.assertEqual(res['rc'], 3)
        self.assertTrue(any('missing identity fields' in e
                            for e in res['summary']['parse_errors']))
        self.assertIn('permissionMode',
                      res['summary']['reinit_events'][0]['missing_identity_fields'])

    # ---- 普通工具失败仅诊断，不推翻后续合法修复成功 ----
    def test_ordinary_tool_failure_is_diagnostic_and_later_success_binds(self):
        rep = report_text(self.STAGE, str(self.workspace))
        events = [init_event(), status_event(),
                  assistant_event(model=self.MODEL, text=None,
                                  tool_uses=[{'id': 'c1', 'name': 'Read'}]),
                  tool_result_event(
                      tool_use_id='c1', is_error=True,
                      text='<tool_use_error>Error: File does not exist: '
                           'notes/missing.md</tool_use_error>'),
                  assistant_event(model=self.MODEL, text=rep),
                  result_event(text=rep)]
        out = self.tmp / 'out-toolfail'
        res = self.run_entry(out, self.write_spec(events), '--tools', 'Read')
        self.assertEqual(res['rc'], 0, res['proc'].stderr.decode('utf-8', 'replace'))
        self.assertTrue(res['summary']['protocol_success'], res['summary']['parse_errors'])
        self.assertTrue(res['summary']['report_bound'])
        failures = res['summary']['tool_failures']
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]['tool_use_id'], 'c1')
        self.assertEqual(failures[0]['line'], 4)
        self.assertIn('file_not_found', failures[0]['flags'])
        self.assertIn('tool_use_error', failures[0]['flags'])
        self.assertIn('is_error_true', failures[0]['flags'])
        stats = res['summary']['tool_failure_stats']
        self.assertEqual(stats['total'], 1)
        self.assertEqual(stats['by_flag']['file_not_found'], 1)
        # 定位失败没有被记成协议错误
        self.assertEqual(res['summary']['parse_errors'], [])

    # ---- is_error=false 里藏权限拒绝：仍 fail-closed，且不当作普通工具失败 ----
    def test_hidden_permission_denial_not_counted_as_tool_failure(self):
        rep = report_text(self.STAGE, str(self.workspace))
        denial = 'Error: Permission to use Bash has been denied (prompts unavailable).'
        events = [init_event(), status_event(),
                  assistant_event(model=self.MODEL, text=None,
                                  tool_uses=[{'id': 'cb', 'name': 'Bash'}]),
                  tool_result_event(tool_use_id='cb', text=denial, is_error=False),
                  assistant_event(model=self.MODEL, text=rep),
                  result_event(text=rep)]
        res = self.run_entry(self.tmp / 'out-hidden-denial',
                             self.write_spec(events), '--tools', 'Read,Bash')
        self.assertEqual(res['rc'], 3)
        self.assertFalse(res['summary']['protocol_success'])
        # 权限拒绝单独记录，不落入普通 tool_failures
        self.assertEqual(res['summary']['tool_failures'], [])
        self.assertTrue(res['summary']['permission_denials'])

    # ================= 第二轮补修：集中缺口 =====================

    def _failure_events(self, *, errors, errors_info=None, subtype='error_during_execution',
                        is_error=True, omit_result=True, text=None):
        extra = {'errors': errors}
        if errors_info is not None:
            extra['errors_info'] = errors_info
        if text is not None:
            extra['result'] = text
        evts = [init_event(), status_event(),
                result_event(subtype=subtype, is_error=is_error,
                             omit=['result'] if (omit_result and text is None) else [],
                             extra=extra)]
        return evts

    # ---- 1. 全字段身份漂移：仅 __timestamp 不同被接受 ----
    def test_duplicate_init_only_timestamp_difference_accepted(self):
        rep = report_text(self.STAGE, str(self.workspace))
        first = init_event()
        first['cwd'] = 'C:\\ws'
        first['apiKeySource'] = 'copilot.example'
        first['__timestamp'] = 'T-1'
        second = dict(first)
        second['__timestamp'] = 'T-2'
        events = [first, status_event(), assistant_event(text=rep), second,
                  result_event(text=rep)]
        res = self.run_entry(self.tmp / 'out-reinit-ts', self.write_spec(events))
        self.assertEqual(res['rc'], 0, res['proc'].stderr.decode('utf-8', 'replace'))
        self.assertTrue(res['summary']['protocol_success'], res['summary']['parse_errors'])
        reinit = res['summary']['reinit_events'][0]
        self.assertTrue(reinit['identical'])
        self.assertEqual(reinit['changed_fields'], [])
        self.assertEqual(reinit['added_fields'], [])
        self.assertEqual(reinit['removed_fields'], [])

    # ---- 1. 全字段身份漂移：cwd 值变化被拒绝（不再只比固定五键）----
    def test_duplicate_init_cwd_drift_rejected(self):
        rep = report_text(self.STAGE, str(self.workspace))
        first = init_event()
        first['cwd'] = 'C:\\ws-a'
        second = init_event()
        second['cwd'] = 'C:\\ws-b'
        events = [first, status_event(), assistant_event(text=rep), second,
                  result_event(text=rep)]
        res = self.run_entry(self.tmp / 'out-reinit-cwd', self.write_spec(events))
        self.assertEqual(res['rc'], 3)
        self.assertFalse(res['summary']['protocol_success'])
        reinit = res['summary']['reinit_events'][0]
        self.assertFalse(reinit['identical'])
        self.assertIn('cwd', reinit['changed_fields'])

    # ---- 1. apiKeySource 漂移被拒绝 ----
    def test_duplicate_init_api_key_source_drift_rejected(self):
        rep = report_text(self.STAGE, str(self.workspace))
        first = init_event()
        first['apiKeySource'] = 'copilot.example'
        second = init_event()
        second['apiKeySource'] = 'other.example'
        events = [first, status_event(), assistant_event(text=rep), second,
                  result_event(text=rep)]
        res = self.run_entry(self.tmp / 'out-reinit-key', self.write_spec(events))
        self.assertEqual(res['rc'], 3)
        self.assertIn('apiKeySource', res['summary']['reinit_events'][0]['changed_fields'])

    # ---- 1. 重复 init 新增未知字段被拒绝 ----
    def test_duplicate_init_added_unknown_field_rejected(self):
        rep = report_text(self.STAGE, str(self.workspace))
        first = init_event()
        second = init_event()
        second['brandNewSecurityField'] = {'mode': 'bypass'}
        events = [first, status_event(), assistant_event(text=rep), second,
                  result_event(text=rep)]
        res = self.run_entry(self.tmp / 'out-reinit-add', self.write_spec(events))
        self.assertEqual(res['rc'], 3)
        self.assertFalse(res['summary']['protocol_success'])
        reinit = res['summary']['reinit_events'][0]
        self.assertFalse(reinit['identical'])
        self.assertIn('brandNewSecurityField', reinit['added_fields'])

    # ---- 1. 重复 init 删除既有字段（agent）被拒绝 ----
    def test_duplicate_init_removed_field_rejected(self):
        rep = report_text(self.STAGE, str(self.workspace))
        first = init_event()
        first['agent'] = 'cli'
        second = init_event()  # 无 agent 键
        events = [first, status_event(), assistant_event(text=rep), second,
                  result_event(text=rep)]
        res = self.run_entry(self.tmp / 'out-reinit-remove', self.write_spec(events))
        self.assertEqual(res['rc'], 3)
        self.assertFalse(res['summary']['protocol_success'])
        reinit = res['summary']['reinit_events'][0]
        self.assertFalse(reinit['identical'])
        self.assertIn('agent', reinit['removed_fields'])

    # ---- 1. 每个 init 仍校验必备类型：tools 非 list[str] 拒绝 ----
    def test_first_init_bad_tools_type_rejected(self):
        rep = report_text(self.STAGE, str(self.workspace))
        bad = init_event()
        bad['tools'] = 'Read'  # 不是 list
        events = [bad, status_event(), assistant_event(text=rep), result_event(text=rep)]
        res = self.run_entry(self.tmp / 'out-init-tools', self.write_spec(events))
        self.assertEqual(res['rc'], 3)
        self.assertFalse(res['summary']['protocol_success'])

    # ---- 5. 协议事件带空 session_id 一律拒绝（不因 sid 空跳过核对）----
    def test_empty_session_id_field_rejected(self):
        rep = report_text(self.STAGE, str(self.workspace))
        events = [init_event(session_id='S-1'),
                  {'type': 'system', 'subtype': 'status', 'session_id': '   '},
                  assistant_event(text=rep, session_id='S-1'),
                  result_event(session_id='S-1', text=rep)]
        res = self.run_entry(self.tmp / 'out-empty-sid', self.write_spec(events))
        self.assertEqual(res['rc'], 3)
        self.assertFalse(res['summary']['protocol_success'])
        self.assertTrue(res['summary']['parse_errors'])

    # ---- 3. is_error=true 但 content 缺失/空：仍记为失败，excerpt 可为 None ----
    def test_empty_content_is_error_true_still_recorded(self):
        rep = report_text(self.STAGE, str(self.workspace))
        block = {'type': 'tool_result', 'tool_use_id': 'c9', 'is_error': True}
        events = [init_event(), status_event(),
                  assistant_event(model=self.MODEL, text=None,
                                  tool_uses=[{'id': 'c9', 'name': 'Read'}]),
                  {'type': 'user', 'session_id': 'S-1',
                   'message': {'role': 'user', 'content': [block]}},
                  assistant_event(model=self.MODEL, text=rep),
                  result_event(text=rep)]
        out = self.tmp / 'out-emptycontent-err'
        res = self.run_entry(out, self.write_spec(events), '--tools', 'Read')
        self.assertEqual(res['rc'], 0, res['proc'].stderr.decode('utf-8', 'replace'))
        # 普通工具失败仍不推翻后续合法修复成功
        self.assertTrue(res['summary']['protocol_success'], res['summary']['parse_errors'])
        failures = res['summary']['tool_failures']
        self.assertEqual(len(failures), 1)
        self.assertIsNone(failures[0]['excerpt'])
        self.assertEqual(failures[0]['tool_use_id'], 'c9')
        self.assertIn('is_error_true', failures[0]['flags'])
        self.assertEqual(res['summary']['tool_failure_stats']['by_flag']['is_error_true'], 1)

    # ---- 4. 成功信封 errors 非空不得绿灯 ----
    def test_success_envelope_with_errors_not_greenlit(self):
        rep = report_text(self.STAGE, str(self.workspace))
        events = [init_event(), status_event(), assistant_event(text=rep),
                  result_event(text=rep, extra={'errors': ['latent error']})]
        res = self.run_entry(self.tmp / 'out-succerrors', self.write_spec(events))
        self.assertEqual(res['rc'], 3)
        self.assertFalse(res['summary']['protocol_success'])
        self.assertFalse(res['summary']['report_bound'])

    # ---- 4. 缺/空 errors 不得声明合法已解析失败 ----
    def test_error_terminal_missing_errors_not_valid_envelope(self):
        for omit_extra in ({}, {'errors': []}):
            with self.subTest(errors=omit_extra.get('errors')):
                events = [init_event(), status_event(),
                          result_event(subtype='error_during_execution', is_error=True,
                                       omit=['result'], extra=dict(omit_extra))]
                res = self.run_entry(
                    self.tmp / f'out-noerr-{list(omit_extra.keys())}',
                    self.write_spec(events))
                self.assertEqual(res['rc'], 3)
                self.assertFalse(res['summary']['protocol_success'])
                self.assertFalse(res['summary']['failure_envelope'])
                self.assertFalse(res['summary']['failure_envelope_valid'])
                self.assertFalse(res['summary']['parse_success'])

    # ---- 4. 矛盾 subtype/is_error 不算合法已解析失败 ----
    def test_error_terminal_contradictory_subtype_is_error_invalid(self):
        events = [init_event(), status_event(),
                  result_event(subtype='error_during_execution', is_error=False,
                               omit=['result'], extra={'errors': ['boom']})]
        res = self.run_entry(self.tmp / 'out-contradict', self.write_spec(events))
        self.assertEqual(res['rc'], 3)
        self.assertFalse(res['summary']['protocol_success'])
        self.assertFalse(res['summary']['failure_envelope_valid'])
        self.assertFalse(res['summary']['parse_success'])

    # ---- 4/2. 合法 429 失败信封：parse_success True 而 protocol_success False ----
    def test_valid_429_envelope_parse_success_but_not_protocol(self):
        rate_msg = ('上游返回 429，将在 2026-10-06 13:55:40 UTC+8 重置。')
        events = self._failure_events(errors=[rate_msg],
                                      errors_info=[{'status': 429, 'code': 6004,
                                                   'category': 'quota',
                                                   'details': rate_msg}])
        res = self.run_entry(self.tmp / 'out-429-valid', self.write_spec(events))
        self.assertEqual(res['rc'], 3)
        self.assertTrue(res['summary']['parse_success'])
        self.assertTrue(res['summary']['failure_envelope_valid'])
        self.assertTrue(res['summary']['failure_envelope'])
        self.assertFalse(res['summary']['protocol_success'])
        self.assertEqual(res['summary']['terminal_state'], 'error')
        self.assertTrue(res['summary']['reset_hint'])
        self.assertTrue(res['summary']['recoverability'].startswith('rate_limited'))

    # ---- 2. 明确 429 但无 reset：窗口未知，recoverability 不宣称 time-bound ----
    def test_429_without_reset_window_unknown(self):
        events = self._failure_events(errors=['usage exceeded'],
                                      errors_info=[{'status': 429, 'details': 'usage exceeded'}])
        res = self.run_entry(self.tmp / 'out-429-noreset', self.write_spec(events))
        self.assertTrue(res['summary']['failure_envelope_valid'])
        self.assertFalse(res['summary']['protocol_success'])
        self.assertIsNone(res['summary']['reset_hint'])
        self.assertTrue(res['summary']['recoverability'].startswith('rate_limited'))
        self.assertNotIn('time-bound', res['summary']['recoverability'])

    # ---- 2. 仅 quota 无 429：不足以断言 rate_limit，标 unknown ----
    def test_quota_without_429_is_unknown_recoverability(self):
        events = self._failure_events(errors=['quota plan limit reached'],
                                      errors_info=[{'category': 'quota', 'status': 200,
                                                   'details': 'quota plan limit reached'}])
        res = self.run_entry(self.tmp / 'out-quota-only', self.write_spec(events))
        self.assertTrue(res['summary']['failure_envelope_valid'])
        self.assertFalse(res['summary']['protocol_success'])
        self.assertTrue(res['summary']['recoverability'].startswith('unknown'))

    # ---- 2. 正文出现 429 数字/id 但无明确 status/code 429：不推断限流 ----
    def test_429_digit_in_body_does_not_imply_rate_limit(self):
        events = self._failure_events(
            errors=['request id 01a10f429... exceeded something (429 chars)'],
            errors_info=[{'category': 'other', 'details': 'generic failure'}])
        res = self.run_entry(self.tmp / 'out-429-body', self.write_spec(events))
        self.assertTrue(res['summary']['failure_envelope_valid'])
        self.assertTrue(res['summary']['recoverability'].startswith('unknown'))

    # ---- 2. 后续 errors 含 reset 提示也不能丢失 ----
    def test_reset_hint_scanned_across_all_errors(self):
        events = self._failure_events(errors=['first failure without window',
                                              '将在 2026-10-06 20:00 UTC+8 重置'],
                                      errors_info=[{'status': 429,
                                                   'details': 'first failure without window'}])
        res = self.run_entry(self.tmp / 'out-reset-later', self.write_spec(events))
        self.assertTrue(res['summary']['reset_hint'])
        self.assertIn('重置', res['summary']['reset_hint'])
        self.assertTrue(res['summary']['recoverability'].startswith('rate_limited'))

    # ---- 4. 失败信封携带 result 文本：原样落盘待验、不绑定，无 response.md ----
    def test_failure_envelope_carried_result_preserved_unbound(self):
        carried = 'RAW RESULT TEXT \u4e2d\u6587  (pending verification, must not trim)  '
        events = self._failure_events(errors=['error_during_execution happened'],
                                      text=carried)
        out = self.tmp / 'out-carried'
        res = self.run_entry(out, self.write_spec(events))
        self.assertEqual(res['rc'], 3)
        self.assertTrue(res['summary']['failure_envelope_valid'])
        self.assertFalse(res['summary']['protocol_success'])
        self.assertFalse(res['summary']['report_bound'])
        self.assertFalse((out / 'response.md').exists())
        pending = out / 'failure-result.pending.txt'
        self.assertTrue(pending.is_file())
        # 原样保留，不 trim、不删前言
        self.assertEqual(pending.read_bytes(), carried.encode('utf-8'))
        report_state = read_json(out / 'report-state.json')
        self.assertTrue(report_state['carrier_missing'])
        self.assertFalse(report_state['bound'])

    # ================= 第三轮定点补修 =====================

    # ---- 6. 成功终态 errors 存在但非 list（string/dict/null）拒绝，原始值不 trim 保留 ----
    def test_success_errors_non_list_rejected_and_preserved(self):
        rep = report_text(self.STAGE, str(self.workspace))
        cases = {
            'string': '  latent error text  ',   # 含前后空格，须原样不 trim
            'dict': {'code': 'E', 'message': 'boom'},
            'null': None,
        }
        for label, value in cases.items():
            with self.subTest(errors=label):
                events = [init_event(), status_event(), assistant_event(text=rep),
                          result_event(text=rep, extra={'errors': value})]
                res = self.run_entry(self.tmp / f'out-succerr-{label}',
                                     self.write_spec(events))
                self.assertEqual(res['rc'], 3)
                self.assertFalse(res['summary']['protocol_success'])
                self.assertFalse(res['summary']['report_bound'])
                self.assertTrue(any('not a list' in e
                                    for e in res['summary']['parse_errors']))
                # 原始 errors 保留进 result_errors（不 trim、不改写）
                self.assertEqual(res['summary']['result_errors'], value)

    # ---- 6. 成功终态缺 errors 字段与空 list 兼容，正常绑定 ----
    def test_success_errors_missing_or_empty_list_ok(self):
        for label, extra in (('missing', None), ('empty', {'errors': []})):
            with self.subTest(errors=label):
                events = self.success_events(result_extra=extra)
                res = self.run_entry(self.tmp / f'out-succerr-ok-{label}',
                                     self.write_spec(events))
                self.assertEqual(res['rc'], 0,
                                 res['proc'].stderr.decode('utf-8', 'replace'))
                self.assertTrue(res['summary']['protocol_success'],
                                res['summary']['parse_errors'])
                self.assertTrue(res['summary']['report_bound'])
                if label == 'empty':
                    self.assertEqual(res['summary']['result_errors'], [])

    # ---- 7. errors-only 开头独立 429：判限流、无 errors_info、无 reset 保持窗口未知 ----
    def test_leading_429_errors_only_rate_limited_reset_unknown(self):
        events = self._failure_events(errors=['429 frequency limit reached'])
        res = self.run_entry(self.tmp / 'out-lead429', self.write_spec(events))
        s = res['summary']
        self.assertEqual(res['rc'], 3)
        self.assertTrue(s['failure_envelope_valid'])
        self.assertFalse(s['protocol_success'])
        self.assertFalse(s['report_bound'])
        self.assertTrue(s['recoverability'].startswith('rate_limited'))
        # 无 reset：不宣称已提供窗口，明确标 unknown
        self.assertIsNone(s['reset_hint'])
        self.assertIn('no reset window', s['recoverability'])
        self.assertIn('unknown', s['recoverability'])
        self.assertNotIn('supplied an explicit reset window', s['recoverability'])
        self.assertEqual(s['result_errors'], ['429 frequency limit reached'])
        self.assertIsNone(s['errors_info'])

    # ---- 7. HTTP 429 开头可识别为限流 ----
    def test_http_429_leading_recognized(self):
        events = self._failure_events(errors=['HTTP 429 Too Many Requests'])
        res = self.run_entry(self.tmp / 'out-http429', self.write_spec(events))
        s = res['summary']
        self.assertTrue(s['failure_envelope_valid'])
        self.assertTrue(s['recoverability'].startswith('rate_limited'))

    # ---- 7. 普通正文中间出现 429 不误判为限流 ----
    def test_plain_body_mid_429_not_rate_limited(self):
        events = self._failure_events(
            errors=['processed 429 records before generic failure'])
        res = self.run_entry(self.tmp / 'out-mid429', self.write_spec(events))
        s = res['summary']
        self.assertTrue(s['failure_envelope_valid'])
        self.assertTrue(s['recoverability'].startswith('unknown'))
        self.assertNotIn('rate_limited', s['recoverability'])


if __name__ == '__main__':
    unittest.main()
