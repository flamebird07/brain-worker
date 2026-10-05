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
               mcp_servers=None):
    event = {'type': 'system', 'subtype': 'init', 'session_id': session_id,
             'model': model, 'tools': ['Read'],
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


if __name__ == '__main__':
    unittest.main()
