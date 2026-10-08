"""Offline stub standing in for scripts/zcode_sdk_runner.mjs（仅供测试，不访问网络、不调用模型）。

argv 形状与真实 runner 一致：`python stub_zcode_runner.py --request <request.json>`。
stdout 只打印一个 JSON 信封（与真实 runner 字段同名），证据写进 request.output_dir。

关键证明：stub 在 output_dir 落 `stub-receipts.jsonl`，逐行记录**收到的完整请求 JSON**
与**实际 submit 次数**。测试据此断言预检拒绝/预检只读时提交次数为 0，而不是只 grep 文本。

环境变量 STUB_MODE：
- ok（默认）：预检通过并提交，response.md 按 STUB_REPORT_FILE 原样字节落盘，退出 0
- resume_match：同上，但 session_id 回显请求的 resume_session_id
- model_mismatch / resume_mismatch / disabled_model / absent_model：预检拒绝，零提交，退出 5
- protocol_failure：提交了但 turn 状态非 completed，退出 3
- missing_session：提交且 ok，但信封 session_id 为空
- missing_response：提交且 ok，但不写 response.md
- empty_stdout / not_json_stdout / crash：无有效信封，退出 0/1/1
"""
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

argv = sys.argv[1:]
if argv[:1] != ['--request'] or len(argv) != 2:
    sys.stderr.write(f'stub_zcode_runner: unexpected argv {argv!r}\n')
    sys.exit(2)
request_path = Path(argv[1])
request = json.loads(request_path.read_text(encoding='utf-8'))
out_dir = Path(request['output_dir'])
mode = os.environ.get('STUB_MODE', 'ok')
stage = request['stage']
selection = request['selection']
report_file = os.environ.get('STUB_REPORT_FILE', '')

envelope = {
    'carrier': 'zcode-sdk', 'stage': stage,
    'provider_requested': selection['providerId'],
    'model_requested': selection['modelId'],
    'reasoning_requested': selection['options']['reasoningLevel'],
    'mode': request['mode'], 'allowed_tools': sorted(request['allowed_tools']),
    'preflight_only': bool(request['preflight_only']),
    'preflight_ok': False, 'submitted': False, 'ok': False,
    'session_id': None, 'turn_id': None, 'status': None, 'model_label': None,
    'selection_before_submit': None, 'selection_after_submit': None,
    'usage': None, 'free_quota_verified': False, 'event_count': 0,
    'response_sha256': None, 'response_bytes': 0,
    'observed_tool_catalog': ['Read', 'Write', 'Edit', 'Bash', 'Glob', 'Grep', 'js',
                              'Task', 'Agent', 'WebFetch', 'WebSearch', 'Skill'],
    'tool_disallowlist_effective': None, 'serialization_failed': False,
    'errors': [], 'limitations': ['offline stub; no model call was made'],
}
preflight = {'stage': stage, 'selection': selection, 'phase': 'stub', 'ok': False}
receipts = out_dir / 'stub-receipts.jsonl'


def record(payload):
    payload = dict(payload, at_utc=datetime.now(timezone.utc).isoformat())
    with receipts.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + '\n')


def refuse(reason, code=5):
    preflight['refused'] = reason
    preflight['ok'] = False
    envelope['limitations'].append(
        'no prompt submitted: preflight refused, no fallback to another model')
    envelope['errors'].append(reason)
    finish(code)


def write_evidence():
    (out_dir / 'preflight.json').write_text(
        json.dumps(preflight, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')


def finish(code):
    write_evidence()
    record({'event': 'exit', 'mode': mode, 'exit_code': code,
            'submitted': envelope['submitted'], 'submit_count': submit_count,
            'preflight_ok': envelope['preflight_ok']})
    sys.stdout.write(json.dumps(envelope, ensure_ascii=False) + '\n')
    sys.exit(code)


# 收到的请求先落证据，再决定行为；这样即使随后拒绝也留有完整凭证。
submit_count = 0
if not out_dir.is_dir():
    sys.stderr.write(f'stub_zcode_runner: output dir missing {out_dir}\n')
    sys.exit(2)
record({'event': 'request', 'mode': mode, 'request': request})

if mode in ('crash',):
    write_evidence()
    raise RuntimeError('stub simulated runner crash')
if mode == 'not_json_stdout':
    sys.stdout.write('this is not a json envelope')
    sys.exit(1)
if mode == 'empty_stdout':
    sys.exit(0)

# 预检只读：绝不建会话、绝不提交，与真实 runner 一致。
if request['preflight_only']:
    preflight.update(phase='registry', submit_eligible=True, disabled_reason_verified=False)
    envelope['preflight_ok'] = True
    envelope['ok'] = True
    envelope['limitations'].append('preflight-only: App not created, no session, no prompt submitted')
    finish(0)

if mode == 'model_mismatch':
    refuse('selection mismatch after setModel: {"providerId":"account:other","modelId":"GLM-5.3"}')
if mode == 'absent_model':
    refuse('target provider/model is absent from App.listModels()')
if mode == 'disabled_model':
    refuse('target model is not selectable: provider disabled for this account')
if mode == 'resume_mismatch':
    envelope['session_id'] = 'sess-actual-other'
    requested = request['resume_session_id']
    preflight['resume_mismatch'] = {'requested': requested, 'actual': 'sess-actual-other'}
    refuse(f'resume session mismatch: requested {requested}, actual sess-actual-other')

preflight.update(phase='app', ok=True, session_id='sess_stub_zcode')
envelope['preflight_ok'] = True
envelope['selection_before_submit'] = selection
if mode == 'resume_match':
    envelope['session_id'] = request['resume_session_id']
    preflight['session_id'] = request['resume_session_id']
elif mode == 'missing_session':
    envelope['session_id'] = None
    preflight['session_id'] = 'sess_stub_zcode'
else:
    envelope['session_id'] = 'sess_stub_zcode'
envelope['observed_tool_catalog'].append('CronCreate')
allowed = set(request['allowed_tools'])
envelope['tool_disallowlist_effective'] = sorted(
    set(request['tool_disallowlist_base'])
    | {name for name in envelope['observed_tool_catalog'] if name not in allowed})

# 真正的“提交”动作：只有走到这里才算提交一次，receipts 逐条计数。
submit_count += 1
record({'event': 'submit', 'mode': mode, 'submit_count': submit_count,
        'prompt_sha256': request['prompt_sha256'],
        'tool_disallowlist_effective': envelope['tool_disallowlist_effective']})
envelope['submitted'] = True
envelope['turn_id'] = 'turn_stub_1'
envelope['model_label'] = f"{selection['providerId']}/{selection['modelId']}"
envelope['selection_after_submit'] = selection
envelope['usage'] = {'source': 'provider', 'model_request_count': 1, 'input_tokens': 10,
                     'output_tokens': 5, 'total_tokens': 15, 'cache_read_tokens': 2,
                     'cache_write_tokens': 0, 'reasoning_tokens': 0,
                     'cache_read_included_in_input': True}
envelope['event_count'] = 2
# 可选 STUB_EVENTS_FILE：用真实形状的工具事件（scheduled/result，带
# sessionId/turnId）替换默认两条 turn 事件，供执行证据门禁做入口级端到端测试。
events_spec = os.environ.get('STUB_EVENTS_FILE')
if events_spec:
    specs = json.loads(Path(events_spec).read_text(encoding='utf-8'))
    lines = []
    for ev in specs:
        ev = dict(ev)
        ev.setdefault('sessionId', envelope['session_id'])
        ev.setdefault('turnId', envelope['turn_id'])
        lines.append(json.dumps(ev, ensure_ascii=False))
    (out_dir / 'events.jsonl').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    envelope['event_count'] = len(lines)
else:
    (out_dir / 'events.jsonl').write_text(
        json.dumps({'type': 'turn_started', 'turnId': 'turn_stub_1'}) + '\n'
        + json.dumps({'type': 'turn_completed', 'turnId': 'turn_stub_1'}) + '\n',
        encoding='utf-8')
# 可选 STUB_WRITE_FILE/STUB_WRITE_TEXT：模拟执行器对工作区文件的真实写入效果。
write_file = os.environ.get('STUB_WRITE_FILE')
if write_file:
    Path(write_file).write_text(os.environ.get('STUB_WRITE_TEXT', ''), encoding='utf-8')
(out_dir / 'result.json').write_text(json.dumps({
    'sessionId': envelope['session_id'], 'turnId': envelope['turn_id'],
    'usage': envelope['usage'], 'projection': {'status': 'completed'},
    'events_count': envelope['event_count']}, ensure_ascii=False, indent=1) + '\n',
    encoding='utf-8')

if mode == 'missing_response':
    envelope['status'] = 'completed'
    envelope['ok'] = True
    finish(0)

if not report_file:
    refuse('STUB_REPORT_FILE is required for a submitting stub mode')
response = Path(report_file).read_bytes()
(out_dir / 'response.md').write_bytes(response)
envelope['response_bytes'] = len(response)
envelope['response_sha256'] = hashlib.sha256(response).hexdigest()

if mode == 'protocol_failure':
    envelope['status'] = 'error_payment_required'
    envelope['ok'] = False
    envelope['errors'].append('turn status error_payment_required')
    finish(3)

envelope['status'] = 'completed'
envelope['ok'] = True
finish(0)
