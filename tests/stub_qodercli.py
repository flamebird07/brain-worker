"""Offline stub standing in for the official Qoder CLI entry (test only).

argv shape matches the official invocation shape used by qoder_direct.py;
prints exactly one JSON envelope to stdout. 隔离测试用，不访问网络、不调用模型。

环境变量：
- STUB_MODE: ok（默认）| bad_stop | empty | not_json
- STUB_REPORT_FILE: UTF-8 文件，其内容作为 envelope.result
"""
import json
import os
import sys
from pathlib import Path

prompt = sys.stdin.read()
mode = os.environ.get('STUB_MODE', 'ok')
report_file = os.environ.get('STUB_REPORT_FILE', '')

if mode == 'not_json':
    sys.stdout.write('not-json-output')
    sys.exit(0)

envelope = {'type': 'result', 'subtype': 'success', 'is_error': False,
            'stop_reason': 'end_turn', 'session_id': 'sess_stub_direct',
            'modelUsage': 'stub-usage', 'total_credits': 0}
if mode == 'bad_stop':
    envelope['stop_reason'] = 'max_tokens'
if mode != 'empty':
    envelope['result'] = Path(report_file).read_text(encoding='utf-8')
sys.stdout.write(json.dumps(envelope, ensure_ascii=False))
