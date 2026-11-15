"""BW-MAC-COMPAT-20261114-S2：ZCode app-server 传输的离线协议 stub。

同一台机器上的合成脚本，完全离线（零网络、零官方运行时、零模型调用）：
按 STUB_MODE 场景剧本扮演 app-server 的 stdio 协议（无 `jsonrpc` 键的行分隔
JSON），用于固化 zcode_appserver_direct.py 的传输行为契约——happy path、
权限请求 deny 应答、runtimePreferences 应答、未知服务器请求 error 应答、
setModel 拒绝、turnEnded 前挂起（超时）、提交后崩溃（未知退出）。

剧本凭据只落 STUB_EVIDENCE_FILE（本地临时文件），绝不写仓库、绝不碰真实库。
happy path 的响应按 prompt_contract 九节骨架如实构造（与官方契约同源常量），
使 report 绑定链（analyze_report/finalize_binding）在离线端到端被真实覆盖。
"""
import json
import os
import sys
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import prompt_contract as pc  # noqa: E402 九节骨架常量与官方契约同源


def stub_report(stage, workspace):
    """按官方九节骨架构造的最小合格报告（字段值如实标注 stub）。"""
    return '\n'.join([
        pc.REPORT_START,
        f'阶段编号与执行方式：{stage}；zcode-appserver stub 传输',
        f'实际项目绝对路径：{workspace}',
        f'汇报时间与执行环境：stub {time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}',
        '',
        pc.SECTION_HEADERS[0], '- 接手时已验证的文件/版本/运行状态：stub',
        '- 本阶段获得的授权范围：stub plan 只读', '- 本阶段禁止事项：无',
        '- 交接来源（如更换 Agent）：无', '',
        pc.SECTION_HEADERS[1], '- 实际执行的动作：stub 无真实动作',
        '- 变更文件及范围：无', '- 未修改但检查过的关键文件或状态：无', '',
        pc.SECTION_HEADERS[2], '1. 事实：stub 协议闭环成立',
        '   证据位置/命令/回读：tests/stub_zcode_appserver.py',
        '   验证环境：离线 stub',
        '2. 事实：stub 未调用真实模型',
        '   证据位置/命令/回读：events.jsonl 原件',
        '   验证环境：离线 stub', '',
        pc.SECTION_HEADERS[3], '- 推断及依据：无', '- 仍未知的内容：真实模型行为', '',
        pc.SECTION_HEADERS[4], '- 工作目录：' + workspace,
        '- 原样命令：stub 无', '- 实际退出码：0', '- 关键结果：协议闭环',
        '- 未运行或未完成的验证及原因：无真实业务', '',
        pc.SECTION_HEADERS[5], '- 未完成项：无', '- 失败项：无',
        '- 缺失证据：无', '- 剩余风险：stub 不证明真实模型行为', '',
        pc.SECTION_HEADERS[6], '- 实际副作用：仅本地临时证据文件',
        '- 越界动作：无',
        '- 敏感信息处理：仅写脱敏摘要，不写 Cookie、密钥、令牌或账务原文。', '',
        pc.SECTION_HEADERS[7],
        '- 状态（只能选一项）：阶段完成',
        '- 状态依据：stub 协议闭环',
        '- 是否满足本阶段验收标准：是', '',
        pc.SECTION_HEADERS[8], '- 建议：无', '',
        pc.CLOSING_LINE,
        pc.REPORT_END,
    ])


def log_event(obj):
    path = os.environ.get('STUB_EVIDENCE_FILE')
    if not path:
        return
    with open(path, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + '\n')


def recv_line():
    line = sys.stdin.readline()
    if not line:
        return None
    try:
        return json.loads(line)
    except ValueError:
        return {'_raw': line[:200]}


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + '\n')
    sys.stdout.flush()


def reply_result(rid, result):
    send({'id': rid, 'result': result})


def reply_error(rid, message, code=-32000):
    send({'id': rid, 'error': {'code': code, 'message': message}})


def run_ok(mode='ok'):
    stage = os.environ.get('STUB_STAGE', 'stub-stage')
    ws = os.environ.get('STUB_WORKSPACE', '/tmp')
    saw = {'capabilities': False, 'create': False, 'setMode': None,
           'setModel': None, 'send_content': None, 'permission_reply': None,
           'runtime_prefs_reply': None, 'unknown_request_reply': None,
           'close': False, 'stop': False}
    while True:
        msg = recv_line()
        if msg is None:
            break
        # 每条收到的消息即时落证：预检等入口提前 kill 的场景也要留痕。
        log_event({'recv': msg})
        method = msg.get('method')
        rid = msg.get('id')
        params = msg.get('params') or {}
        # 服务器→客户端请求（client 回来的应答没有 method）。
        if method == 'runtime/capabilities':
            saw['capabilities'] = True
            reply_result(rid, {'sessionLifecycleWorkspace': True,
                               'historyFirst': True})
            continue
        if method == 'session/create':
            saw['create'] = True
            saw['workspace'] = params.get('workspace')
            # 与真实运行时同构：create 结果的 projection.sessionId 是字面量
            # 'unknown'，真实 id 经 session/lifecycle/changed 通知给出；setMode
            # 前必须到达 state=ready（BW-MAC-COMPAT-20261114-S2 实测契约）。
            reply_result(rid, {'projection': {'sessionId': 'unknown',
                                              'mode': 'build', 'status': 'idle'},
                               'protocol': {'name': 'ZCode Protocol',
                                            'version': 1}})
            send({'method': 'session/lifecycle/changed',
                  'params': {'sessionId': 'sess_stub_1', 'state': 'starting',
                             'generation': 1, 'revision': 1}})
            send({'method': 'session/lifecycle/changed',
                  'params': {'sessionId': 'sess_stub_1', 'state': 'ready',
                             'generation': 1, 'revision': 2}})
            continue
        if method == 'session/setMode':
            saw['setMode'] = params.get('mode')
            reply_result(rid, {})
            continue
        if method == 'session/setModel':
            saw['setModel'] = params.get('model')
            if mode == 'model_refuse':
                reply_error(rid, 'stub: model selection unavailable', -32001)
                send({'method': 'bye', 'params': {}})
                break
            reply_result(rid, {})
            continue
        if method == 'session/send':
            saw['send_content'] = params.get('content')
            reply_result(rid, {'turnId': 'turn_stub_1'})
            if mode == 'crash_after_send':
                log_event({'saw': saw})
                os._exit(3)
            if mode == 'hang':
                # 提交后永不出 turnEnded：客户端超时路径必须杀进程并按超时结算。
                deadline = time.time() + 120
                while time.time() < deadline and recv_line() is not None:
                    pass
                log_event({'saw': saw})
                os._exit(0)
            # 权限请求与运行时偏好探测场景在 turn 结束前发出。
            if mode == 'permission_request':
                send({'id': 'server-1', 'method': 'interaction/requestPermission',
                      'params': {'sessionId': 'sess_stub_1', 'toolName': 'Bash',
                                 'toolCallId': 'tc1', 'requestId': 'req1',
                                 'riskLevel': 'medium', 'input': {'command': 'ls'},
                                 'reason': 'stub wants bash',
                                 'options': [
                                     {'optionId': 'opt-allow',
                                      'label': 'Allow once',
                                      'kind': 'allowOnce'},
                                     {'optionId': 'opt-deny',
                                      'label': 'Deny', 'kind': 'deny'}]}})
            if mode == 'runtime_prefs':
                send({'id': 'server-2',
                      'method': 'session/requestRuntimePreferences',
                      'params': {'sessionId': 'sess_stub_1',
                                 'scope': 'runtime-materialization'}})
            if mode == 'unknown_request':
                send({'id': 'server-3', 'method': 'some/futureMethod',
                      'params': {'sessionId': 'sess_stub_1'}})
            send({'method': 'turnEnded', 'params': {'turnId': 'turn_stub_1'}})
            continue
        if method == 'session/messages':
            reply_result(rid, {'messages': [
                {'role': 'user', 'content': params.get('sessionId')},
                {'role': 'assistant',
                 'content': [{'type': 'text', 'text': stub_report(stage, ws)}]},
            ]})
            continue
        if method == 'session/stop':
            saw['stop'] = True
            reply_result(rid, {})
            continue
        if method == 'session/close':
            saw['close'] = True
            reply_result(rid, {})
            break
        # 客户端对服务器请求的应答（id 形如 server-N，result 或 error）。
        if 'result' in msg or 'error' in msg:
            if msg.get('id') == 'server-1':
                saw['permission_reply'] = msg
            if msg.get('id') == 'server-2':
                saw['runtime_prefs_reply'] = msg
            if msg.get('id') == 'server-3':
                saw['unknown_request_reply'] = msg
            continue
    log_event({'saw': saw})
    return 0


if __name__ == '__main__':
    sys.exit(run_ok(os.environ.get('STUB_MODE', 'ok')))
