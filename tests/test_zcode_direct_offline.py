#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BW-ZCODE-INSTALL-01 ZCode 直连离线测试：不登录、不访问网络、不调用任何模型。

执行载体是**真实子进程** stub（tests/stub_zcode_runner.py，镜像官方 runner 的
argv 与信封形状）。stub 把收到的完整请求 JSON 与真实 submit 次数写进
`<output_dir>/stub-receipts.jsonl`，因此“零提交”“工具排除”等结论来自子进程留存的
凭证，而不是文本 grep。

覆盖：
- 派工前拒绝：配置缺项/路径不存在、空 stage、非法 mode（含 yolo）、未知/空白/重复 tools、
  plan 模式要求 Write/Edit/Bash、输出目录已存在（重放拒绝，已有证据字节不变）
- 请求构造：格式指令前置且原任务逐字保留、prompt_sha256、环境值不落证据、
  整工具默认全禁 + 显式名单放行、argv 无 shell 拼接
- 预检拒绝：模型不存在/被禁用/选择漂移/resume 不匹配 → receipts submit 次数为 0
- preflight-only：零提交、无 response.md、不宣称可发送
- 三态分离：协议成功+正文合格+回读一致才 bound；协议失败或正文带前言或缺 session
  或缺 response 一律不绑定，且 business_verified 恒为 false
- 信封缺失/非 JSON/崩溃：exit 3 并保留 stdout.json / stderr.log 证据

用法：python tests/test_zcode_direct_offline.py
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RUN_DIR = HERE / ('zcode-run-' + time.strftime('%Y%m%d-%H%M%S'))
STAGE = 'BW-ZCODE-INSTALL-01-OFFTEST'
SECONDS_STAGE = 'BW-ZCODE-INSTALL-01'
SECTIONS = ('一、当前基线与授权', '二、实际执行范围', '三、已验证事实',
            '四、推断（必须与事实分开）', '五、测试与验证',
            '六、未完成项与剩余风险', '七、实际副作用与越界检查',
            '八、本阶段状态', '九、建议下一步（只提出建议，不执行）')
CLOSING = '本阶段汇报结束；等待主脑验收。'
SECRET = 'sh-SECRET-DO-NOT-PERSIST'
LAYOUT = ('request.json', 'process.json', 'stdout.json', 'stderr.log', 'preflight.json',
          'response.md', 'result.json', 'events.jsonl', 'summary.json',
          'report-state.json', 'stub-receipts.jsonl')
RESULTS = []


def check(name, passed, detail=''):
    RESULTS.append({'name': name, 'pass': bool(passed), 'detail': str(detail)[:400]})
    print(('PASS ' if passed else 'FAIL ') + name
          + ('' if passed else f'  >>> {str(detail)[:400]}'))


def build_report(stage: str, project_root: str, *, preface=None, drop_section=None,
                 stage_field='阶段编号与执行方式') -> str:
    lines = []
    if preface:
        lines.append(preface)
    lines += ['WORKER_REPORT_START',
              f'{stage_field}：{stage}；direct。',
              f'实际项目绝对路径：{project_root}',
              '']
    for i, head in enumerate(SECTIONS):
        if drop_section == i:
            continue
        lines += [head, '- 离线测试占位内容', '']
    lines += [CLOSING, 'WORKER_REPORT_END']
    return '\n'.join(lines) + '\n'


def main() -> int:
    script = ROOT / 'scripts' / 'zcode_direct.py'
    stub = HERE / 'stub_zcode_runner.py'
    runner = ROOT / 'scripts' / 'zcode_sdk_runner.mjs'
    RUN_DIR.mkdir(parents=True, exist_ok=False)
    print(f'== ZCode direct 离线测试 run {RUN_DIR.name} ==')
    print(f'under test: {script}')

    work = RUN_DIR / 'proj'
    work.mkdir()
    # 官方运行时文件在离线测试里用占位文件代替：只验证路径校验，绝不执行官方代码。
    fixture = {}
    for key, name in (('node', None), ('bootstrap', 'bootstrap-dist-index.js'),
                      ('tsx_loader', 'tsx-loader.mjs'),
                      ('builtin_provider_config', 'builtin-provider.json'),
                      ('personal_provider_config', 'personal-provider.json')):
        if name is None:
            fixture[key] = str(Path(sys.executable).resolve())
        else:
            path = RUN_DIR / name
            path.write_text('offline fixture; never executed by the python suite\n',
                            encoding='utf-8')
            fixture[key] = str(path)
    cfg_path = RUN_DIR / 'zcode-entry.json'

    def write_cfg(mutate=None, **overrides):
        cfg = {'node': fixture['node'], 'bootstrap': fixture['bootstrap'],
               'tsx_loader': fixture['tsx_loader'],
               'builtin_provider_config': fixture['builtin_provider_config'],
               'personal_provider_config': fixture['personal_provider_config'],
               'runner': str(stub), 'node_args': [],
               'environment': {'HTTPS_PROXY': SECRET}}
        cfg.update(overrides)
        if mutate:
            mutate(cfg)
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding='utf-8')
        return cfg_path

    write_cfg()
    prompt_path = RUN_DIR / 'prompt.txt'
    ORIGINAL = '离线链路测试任务。要求逐字保留本行，不得改写。\n'
    prompt_path.write_text(ORIGINAL, encoding='utf-8')
    good_report = RUN_DIR / 'report-good.txt'
    good_report.write_text(build_report(STAGE, str(work)), encoding='utf-8')
    preface_report = RUN_DIR / 'report-preface.txt'
    preface_report.write_text(
        build_report(STAGE, str(work), preface='好的，下面是我的报告：'), encoding='utf-8')

    counter = {'n': 0}

    def run_transport(out_dir, *, mode='ok', report=good_report, stage=STAGE,
                      config=None, extra=(), events_file=None,
                      write_file=None, write_text=None):
        counter['n'] += 1
        env = os.environ.copy()
        # 额度门禁隔离：显式临时 store/routes，绝不写真实 ~/.brain-worker 状态，
        # 也不默认禁用门禁；未匹配路由时进保守共享组，门禁路径仍被真实执行。
        env['BRAIN_WORKER_QUOTA_STORE'] = str(RUN_DIR / 'quota-store' / 'state.sqlite3')
        env['BRAIN_WORKER_QUOTA_ROUTES'] = str(RUN_DIR / 'quota-routes-absent.json')
        # 跨会话并发容量池隔离：每次回放用各自全新的临时 store（按调用序号唯一），绝不
        # 读写真实 ~/.brain-worker 池；空池下主力 GLM-5.3 直接放行，不受轮换历史影响。
        env['BRAIN_WORKER_DISPATCH_STORE'] = str(
            RUN_DIR / ('dispatch-%d.sqlite3' % counter['n']))
        env['STUB_MODE'] = mode
        env.pop('STUB_REPORT_FILE', None)
        env.pop('STUB_EVENTS_FILE', None)
        env.pop('STUB_WRITE_FILE', None)
        env.pop('STUB_WRITE_TEXT', None)
        if report is not None:
            env['STUB_REPORT_FILE'] = str(report)
        if events_file is not None:
            env['STUB_EVENTS_FILE'] = str(events_file)
        if write_file is not None:
            env['STUB_WRITE_FILE'] = str(write_file)
            env['STUB_WRITE_TEXT'] = write_text or ''
        cmd = [sys.executable, str(script), '--workspace', str(work),
               '--prompt-file', str(prompt_path), '--output-dir', str(out_dir),
               '--config', str(config or cfg_path), '--stage', stage, *extra]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                              env=env, timeout=120)
        summary = None
        sfile = Path(out_dir) / 'summary.json'
        if sfile.is_file():
            summary = json.loads(sfile.read_text(encoding='utf-8'))
        rfile = Path(out_dir) / 'report-state.json'
        state = json.loads(rfile.read_text(encoding='utf-8')) if rfile.is_file() else None
        return proc, summary, state

    def submit_count(out_dir) -> int:
        receipts = Path(out_dir) / 'stub-receipts.jsonl'
        if not receipts.is_file():
            return -1
        n = 0
        for line in receipts.read_text(encoding='utf-8').splitlines():
            if not line.strip():
                continue
            if json.loads(line).get('event') == 'submit':
                n += 1
        return n

    def last_receipt_request(out_dir):
        receipts = Path(out_dir) / 'stub-receipts.jsonl'
        for line in reversed(receipts.read_text(encoding='utf-8').splitlines()):
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get('event') == 'request':
                return rec['request']
        return None

    # ---------- 派工前机械校验（不建证据目录） ----------
    bad_cfg = RUN_DIR / 'missing-keys.json'
    bad_cfg.write_text(json.dumps({'node': fixture['node']}), encoding='utf-8')
    proc, summary, _ = run_transport(RUN_DIR / 't-config-incomplete', config=bad_cfg)
    check('incomplete entry config refused before dispatch',
          proc.returncode == 2 and not (RUN_DIR / 't-config-incomplete').exists()
          and 'missing key' in proc.stdout, f'rc={proc.returncode} out={proc.stdout[:200]}')

    gone_cfg = RUN_DIR / 'gone-paths.json'
    gone_cfg.write_text(json.dumps({
        'node': fixture['node'], 'bootstrap': str(RUN_DIR / 'no-bootstrap.js'),
        'tsx_loader': fixture['tsx_loader'],
        'builtin_provider_config': fixture['builtin_provider_config'],
        'personal_provider_config': fixture['personal_provider_config'],
        'runner': str(stub), 'node_args': []}), encoding='utf-8')
    proc, _, _ = run_transport(RUN_DIR / 't-config-paths', config=gone_cfg)
    check('entry config path that does not exist is refused',
          proc.returncode == 2 and not (RUN_DIR / 't-config-paths').exists()
          and 'must be an existing absolute file' in proc.stdout,
          f'rc={proc.returncode} out={proc.stdout[:200]}')

    proc, _, _ = run_transport(RUN_DIR / 't-empty-stage', stage='   ')
    check('blank --stage refused before dispatch, no evidence dir',
          proc.returncode == 2 and not (RUN_DIR / 't-empty-stage').exists()
          and '--stage is required' in proc.stdout, f'rc={proc.returncode}')

    proc, _, _ = run_transport(RUN_DIR / 't-bad-mode', extra=['--mode', 'yolo'])
    check('yolo mode is never offered (argparse rejects it)',
          proc.returncode == 2 and not (RUN_DIR / 't-bad-mode').exists()
          and 'invalid choice' in proc.stderr, f'rc={proc.returncode} err={proc.stderr[-200:]}')

    proc, _, _ = run_transport(RUN_DIR / 't-bad-tools', extra=['--tools', 'Read,Task'])
    check('unknown tool grant (Task) refused before dispatch',
          proc.returncode == 2 and not (RUN_DIR / 't-bad-tools').exists()
          and 'non-permitted tool' in proc.stdout, proc.stdout[:200])

    proc, _, _ = run_transport(RUN_DIR / 't-blank-tools', extra=['--tools', 'Read, ,Grep'])
    check('blank/duplicate tool items refused, not silently dropped',
          proc.returncode == 2 and not (RUN_DIR / 't-blank-tools').exists()
          and 'empty/whitespace item' in proc.stdout, proc.stdout[:200])

    proc, _, _ = run_transport(RUN_DIR / 't-plan-write',
                               extra=['--mode', 'plan', '--tools', 'Write'])
    check('plan mode cannot grant Write/Edit/Bash',
          proc.returncode == 2 and not (RUN_DIR / 't-plan-write').exists()
          and 'plan is read-only' in proc.stdout, proc.stdout[:200])

    # ---------- 请求构造：格式指令前置、原任务逐字、环境值不落证据 ----------
    out_ok = RUN_DIR / 't-ok'
    proc, summary, state = run_transport(out_ok)
    req = json.loads((out_ok / 'request.json').read_text(encoding='utf-8'))
    facts = [proc.returncode == 0, summary['protocol_success'] is True,
             summary['business_verified'] is False, summary['free_quota_verified'] is False,
             req['prompt'].startswith('Final response must contain only the complete nine-section report'),
             req['prompt'].endswith('\n\n' + ORIGINAL),
             req['prompt_sha256'] == hashlib.sha256(prompt_path.read_bytes()).hexdigest(),
             req['prompt_payload']['sent_payload_sha256'] == hashlib.sha256(
                 req['prompt'].encode('utf-8')).hexdigest(),
             req['prompt_payload']['readback_match'] is True,
             req['prompt_payload']['contract_sha256'] is not None,
             SECONDS_STAGE in req['prompt'], str(work) in req['prompt'],
             submit_count(out_ok) == 1]
    check('dispatch sends the format contract plus the verbatim task, one real submit',
          all(facts), json.dumps({'rc': proc.returncode, 'facts': facts}, ensure_ascii=False))
    check('request.json carries the resolved selection and no credential values',
          req['selection'] == {'providerId': 'account:bigmodel-individual-coding-plan',
                               'modelId': 'GLM-5.3-Flash',
                               'options': {'reasoningLevel': 'low'}}
          and req['environment_keys'] == ['HTTPS_PROXY'] and 'environment' not in req,
          json.dumps({k: req.get(k) for k in ('selection', 'environment_keys')},
                     ensure_ascii=False))
    leaked = [str(p) for p in sorted(out_ok.iterdir())
              if p.is_file() and SECRET in p.read_text(encoding='utf-8', errors='ignore')]
    check('entry config environment values stay out of every evidence file',
          leaked == [], leaked)
    check('argv is an argument array with no shell concatenation',
          req['argv'] == [fixture['node'], str(stub), '--request',
                          str(out_ok / 'request.json')], req['argv'])

    # 默认全禁 + 显式放行（整工具开关）
    disallow = set(req['tool_disallowlist_base'])
    always_off = ('js', 'Task', 'Agent', 'WebFetch', 'WebSearch',
                  'Write', 'Edit', 'Bash', 'Skill', 'CronCreate')
    granted_by_default = ('Read', 'Glob', 'Grep')
    check('default grant disables the whole verified tool catalog incl. node_repl(js)',
          all(n in disallow for n in always_off)
          and all(n in disallow for n in granted_by_default),
          json.dumps([n for n in always_off + granted_by_default if n not in disallow]))
    eff = set(summary['tool_disallowlist_effective'])
    check('live catalog additions are also excluded (no silent tool leak)',
          {'Read', 'CronCreate'} <= eff and 'Write' in eff and 'js' in eff,
          json.dumps(sorted(eff)[:6], ensure_ascii=False))
    check('protocol success still keeps business acceptance false',
          summary['protocol_success'] is True
          and summary['report_bound'] is True and state['bound'] is True
          and state['sections_found'] == 9 and state['readback_match'] is True,
          json.dumps({k: state.get(k) for k in ('bound', 'reasons', 'sections_found')},
                     ensure_ascii=False))
    check('evidence layout complete for a successful dispatch',
          all((out_ok / f).is_file() for f in LAYOUT),
          sorted(p.name for p in out_ok.iterdir()))
    check('response.md kept as verbatim original bytes with hash readback',
          (out_ok / 'response.md').read_bytes() == good_report.read_bytes()
          and summary['response_sha256']
          == hashlib.sha256(good_report.read_bytes()).hexdigest(),
          summary.get('response_sha256'))
    usage = summary['usage'] or {}
    check('usage comes from the runner result and never re-adds cache read',
          usage.get('input_tokens') == 10 and usage.get('total_tokens') == 15
          and usage.get('cache_read_included_in_input') is True
          and summary['free_quota_verified'] is False, json.dumps(usage, ensure_ascii=False))

    # ---------- 重放拒绝：已有证据字节不变 ----------
    before = {p.name: p.read_bytes() for p in out_ok.iterdir()}
    proc, _, _ = run_transport(out_ok)
    after = {p.name: p.read_bytes() for p in out_ok.iterdir()}
    check('reusing an output dir is refused and existing evidence is untouched',
          proc.returncode == 2 and before == after and 'already exists' in proc.stdout,
          f'rc={proc.returncode} delta={[k for k in before if before[k] != after.get(k)]}')

    # ---------- 预检拒绝：零提交证明 ----------
    for mode, expect in (('model_mismatch', 'selection mismatch after setModel'),
                         ('absent_model', 'absent from App.listModels'),
                         ('disabled_model', 'not selectable')):
        tag = RUN_DIR / f't-{mode}'
        proc, summary, state = run_transport(tag, mode=mode)
        facts = [proc.returncode == 3, summary['protocol_success'] is False,
                 summary['preflight_ok'] is False, summary['submitted'] is False,
                 submit_count(tag) == 0, not (tag / 'response.md').exists(),
                 (tag / 'preflight.json').is_file(), state['bound'] is False,
                 expect in json.dumps(summary['runner_errors'], ensure_ascii=False)]
        check(f'{mode}: zero submissions, failing preflight kept, no success claim',
              all(facts), json.dumps({'rc': proc.returncode, 'facts': facts,
                                      'errors': summary['runner_errors']},
                                     ensure_ascii=False))

    # ---------- resume：精确匹配才绑定 ----------
    tag = RUN_DIR / 't-resume-match'
    proc, summary, state = run_transport(tag, mode='resume_match',
                                         extra=['--resume-session-id', 'sess-zcode-42'])
    check('resume with exact session id binds',
          proc.returncode == 0 and summary['session_id'] == 'sess-zcode-42'
          and state['binding']['requested_session_id'] == 'sess-zcode-42'
          and state['bound'] is True, json.dumps(summary.get('session_id')))

    tag = RUN_DIR / 't-resume-mismatch'
    proc, summary, state = run_transport(tag, mode='resume_mismatch',
                                         extra=['--resume-session-id', 'sess-zcode-42'])
    check('resume mismatch refuses submission (zero submits, evidence kept)',
          proc.returncode == 3 and summary['submitted'] is False
          and submit_count(tag) == 0 and not (tag / 'response.md').exists()
          and state['bound'] is False, json.dumps(summary.get('runner_errors')))

    tag = RUN_DIR / 't-resume-drift'
    proc, summary, state = run_transport(tag, extra=['--resume-session-id', 'sess-zcode-42'])
    check('submitted turn whose session id differs is never bound',
          proc.returncode == 0 and summary['protocol_success'] is True
          and summary['report_bound'] is False and state['body_ok'] is True
          and any('exact match' in r for r in state['reasons']),
          json.dumps(state['reasons'], ensure_ascii=False)[:300])

    # ---------- preflight-only：只查可选性，不建会话不提交 ----------
    tag = RUN_DIR / 't-preflight-only'
    proc, summary, state = run_transport(tag, mode='ok', extra=['--preflight-only'])
    facts = [proc.returncode == 0, summary['preflight_only'] is True,
             summary['submitted'] is False, submit_count(tag) == 0,
             not (tag / 'response.md').exists(), state['bound'] is False,
             'preflight-only' in json.dumps(state['reasons'], ensure_ascii=False)]
    check('preflight-only checks selectability only and submits nothing',
          all(facts), json.dumps({'rc': proc.returncode, 'facts': facts},
                                 ensure_ascii=False))
    req = last_receipt_request(tag)
    check('preflight-only request omits the report contract from the task text',
          req is not None and req['preflight_only'] is True
          and req['prompt'] == ORIGINAL, (req or {}).get('prompt', '')[:120])

    # ---------- 协议失败 / 正文不合格：三态分离 ----------
    tag = RUN_DIR / 't-protocol-failure'
    proc, summary, state = run_transport(tag, mode='protocol_failure')
    check('protocol failure with a perfectly formatted report stays unbound',
          proc.returncode == 3 and summary['protocol_success'] is False
          and state['body_ok'] is True and state['bound'] is False
          and state['binding']['protocol_success'] is False
          and any('protocol not successful' in r for r in state['reasons']),
          json.dumps(state['reasons'], ensure_ascii=False)[:300])

    tag = RUN_DIR / 't-preface'
    proc, summary, state = run_transport(tag, report=preface_report)
    disk = (tag / 'response.md').read_bytes()
    check('prefaced report is preserved byte-for-byte and refused binding, never rewritten',
          proc.returncode == 0 and summary['protocol_success'] is True
          and disk == preface_report.read_bytes() and state['body_ok'] is False
          and state['bound'] is False
          and any('preface' in r for r in state['reasons']),
          json.dumps(state['reasons'], ensure_ascii=False)[:300])

    tag = RUN_DIR / 't-missing-session'
    proc, summary, state = run_transport(tag, mode='missing_session')
    check('success envelope without a session id is not bound',
          proc.returncode == 0 and summary['session_id'] is None
          and state['bound'] is False
          and any('session_id' in r for r in state['reasons']),
          json.dumps(state['reasons'], ensure_ascii=False)[:300])

    tag = RUN_DIR / 't-missing-response'
    proc, summary, state = run_transport(tag, mode='missing_response')
    check('success envelope without a response file leaves report unbound',
          proc.returncode == 0 and state['bound'] is False
          and state.get('carrier_missing') is True and not (tag / 'response.md').exists(),
          json.dumps(state, ensure_ascii=False)[:300])

    tag = RUN_DIR / 't-no-envelope'
    proc, summary, state = run_transport(tag, mode='empty_stdout')
    check('empty runner stdout fails with evidence retained',
          proc.returncode == 3 and summary['protocol_success'] is False
          and 'parse_error' in summary and (tag / 'stdout.json').is_file(),
          f'rc={proc.returncode}')

    tag = RUN_DIR / 't-bad-envelope'
    proc, summary, state = run_transport(tag, mode='not_json_stdout')
    check('non-JSON runner stdout fails, no binding, no success claim',
          proc.returncode == 3 and summary['protocol_success'] is False
          and summary['report_bound'] is False, f'rc={proc.returncode}')

    tag = RUN_DIR / 't-crash'
    proc, summary, state = run_transport(tag, mode='crash')
    check('abnormal runner exit keeps stderr evidence and fails',
          proc.returncode == 3 and 'runner stdout is empty' in summary.get('parse_error', '')
          and 'stub simulated runner crash' in (tag / 'stderr.log').read_text(encoding='utf-8'),
          f'rc={proc.returncode}')

    # ---------- edit 授权模式：显式名单只放行该工具 ----------
    tag = RUN_DIR / 't-edit-tools'
    proc, summary, state = run_transport(tag, extra=['--mode', 'edit',
                                                     '--tools', 'Read,Grep,Write'])
    req = json.loads((tag / 'request.json').read_text(encoding='utf-8'))
    eff = set(summary['tool_disallowlist_effective'])
    check('edit mode grants only the listed tools and still disables the rest',
          proc.returncode == 0 and req['allowed_tools'] == ['Read', 'Grep', 'Write']
          and {'Edit', 'Bash', 'js', 'Task'} <= eff and 'Read' not in eff
          and state['bound'] is True, json.dumps(sorted(eff)[:8], ensure_ascii=False))

    # ---------- 执行证据契约：派工前拒绝与缺省未验 ----------
    check('no execution contract leaves execution_evidence_ok explicitly null',
          summary['execution_evidence_ok'] is None
          and summary['execution_evidence_status'] == 'unverified_no_contract'
          and (out_ok / 'execution-evidence.json').is_file(),
          json.dumps({'ok': summary.get('execution_evidence_ok'),
                      'status': summary.get('execution_evidence_status')},
                     ensure_ascii=False))
    bad_contract = RUN_DIR / 'bad-contract.json'
    bad_contract.write_text(json.dumps(
        {'task_type': 'engineering', 'required_reads': ['../outside.py'],
         'expected_artifacts': []}, ensure_ascii=False), encoding='utf-8')
    proc, _, _ = run_transport(RUN_DIR / 't-contract-escape',
                               extra=['--execution-contract', str(bad_contract)])
    check('contract path escaping the workspace is refused before dispatch',
          proc.returncode == 2 and not (RUN_DIR / 't-contract-escape').exists()
          and 'escapes workspace' in proc.stdout, proc.stdout[:200])

    # ---------- 入口级端到端：显式契约 × stub 工具事件 × baseline ----------
    (work / 'task.md').write_text('original task file\n', encoding='utf-8')
    zero_tool_contract = RUN_DIR / 'zero-tool-contract.json'
    zero_tool_contract.write_text(json.dumps(
        {'task_type': 'engineering', 'required_reads': ['task.md'],
         'required_modified_files': [], 'allow_no_changes': True},
        ensure_ascii=False), encoding='utf-8')
    tag = RUN_DIR / 't-contract-zero-tools'
    proc, summary, state = run_transport(tag, extra=['--execution-contract',
                                                      str(zero_tool_contract)])
    check('explicit contract with zero tool events fails evidence but keeps protocol/bound',
          proc.returncode == 3 and summary['protocol_success'] is True
          and summary['report_bound'] is True and state['bound'] is True
          and summary['execution_evidence_ok'] is False
          and summary['execution_evidence_status'] == 'no_required_execution',
          json.dumps({'rc': proc.returncode,
                      'status': summary.get('execution_evidence_status')},
                     ensure_ascii=False))

    good_contract = RUN_DIR / 'good-contract.json'
    good_contract.write_text(json.dumps(
        {'task_type': 'engineering', 'required_reads': ['task.md'],
         'required_modified_files': ['task.md']}, ensure_ascii=False),
        encoding='utf-8')
    events_path = RUN_DIR / 'tool-events.json'
    task_abs = str(work / 'task.md')
    events_path.write_text(json.dumps([
        {'type': 'tool_call_scheduled',
         'payload': {'toolCallId': 'a', 'toolName': 'Read',
                     'input': {'file_path': task_abs}}},
        {'type': 'tool_call_result',
         'payload': {'toolCallId': 'a', 'result': {'success': True,
                                                   'content': 'read ok'}}},
        {'type': 'tool_call_scheduled',
         'payload': {'toolCallId': 'b', 'toolName': 'Edit',
                     'input': {'file_path': task_abs}}},
        {'type': 'tool_call_result',
         'payload': {'toolCallId': 'b', 'result': {'success': True,
                                                   'content': 'edited'}}},
    ], ensure_ascii=False), encoding='utf-8')
    tag = RUN_DIR / 't-contract-verified'
    original_sha = hashlib.sha256((work / 'task.md').read_bytes()).hexdigest()
    proc, summary, state = run_transport(
        tag, extra=['--execution-contract', str(good_contract)],
        events_file=events_path, write_file=work / 'task.md',
        write_text='changed by stub executor\n')
    req = json.loads((tag / 'request.json').read_text(encoding='utf-8'))
    baseline = req.get('execution_contract_baseline') or {}
    ev_file = json.loads((tag / 'execution-evidence.json').read_text(encoding='utf-8'))
    check('valid read+modify contract with dispatch baseline verifies and exits 0',
          proc.returncode == 0 and summary['execution_evidence_ok'] is True
          and summary['execution_evidence_status'] == 'verified'
          and req['execution_contract_sha256'] is not None
          and any(v.get('sha256') == original_sha for v in baseline.values())
          and ev_file.get('write_evidence'), json.dumps(
              {'rc': proc.returncode, 'status': summary.get('execution_evidence_status'),
               'baseline_keys': sorted(baseline)}, ensure_ascii=False))

    # 契约文件在执行后被改写：request 里的快照与哈希不受影响（入口从不重读契约文件）
    good_contract.write_text(json.dumps(
        {'task_type': 'reasoning'}, ensure_ascii=False), encoding='utf-8')
    check('post-run rewrite of the contract file cannot change the recorded snapshot',
          req['execution_contract']['task_type'] == 'engineering'
          and req['execution_contract']['required_reads'] == ['task.md'],
          json.dumps(req.get('execution_contract'), ensure_ascii=False))

    tag = RUN_DIR / 't-contract-preflight'
    proc, summary, state = run_transport(tag, extra=['--preflight-only',
                                                      '--execution-contract',
                                                      str(good_contract)])
    check('preflight-only with a contract never claims real execution success',
          proc.returncode == 3 and summary['submitted'] is False
          and summary['execution_evidence_status'] == 'unverified_preflight_only',
          json.dumps({'rc': proc.returncode,
                      'status': summary.get('execution_evidence_status')},
                     ensure_ascii=False))

    # ---------- 纯函数直连检查 ----------
    sys.path.insert(0, str(ROOT / 'scripts'))
    import zcode_direct as zd  # noqa: E402
    check('validate_selection rejects yolo and blank provider/model',
          any('yolo' in r for r in zd.validate_selection(stage='S', provider='p', model='m',
                                                         reasoning='low', mode='yolo', tools=[]))
          and zd.validate_selection(stage='', provider=' ', model='', reasoning='mid',
                                    mode='plan', tools=[]) != []
          and zd.validate_selection(stage='S', provider='p', model='m', reasoning='low',
                                    mode='plan', tools=[]) == [],
          zd.validate_selection(stage='', provider=' ', model='', reasoning='mid',
                                mode='plan', tools=[]))
    check('parse_tools accepts only exact permitted names and rejects duplicates',
          zd.parse_tools('') == [] and zd.parse_tools('Read,Grep') == ['Read', 'Grep'],
          'ok')
    try:
        zd.parse_tools('Read, Read')
        check('duplicate tool item raises', False, 'no exception')
    except ValueError:
        check('duplicate tool item raises', True)
    probe_runner = Path('/x/runner.mjs')
    probe_request = Path('/x/request.json')
    built = zd.build_node_argv({'node': '/x/node', 'tsx_loader': sys.executable},
                               probe_runner, probe_request)
    check('build_node_argv imports the tsx loader by fileURL when node_args is absent',
          built == ['/x/node', '--import', Path(sys.executable).as_uri(),
                    str(probe_runner), '--request', str(probe_request)], built)
    check('official runner file exists for the real dispatch path',
          runner.is_file() and 'startProcessProviderRegistryRuntime' in
          runner.read_text(encoding='utf-8'), str(runner))

    failed = [x for x in RESULTS if x['pass'] is False]
    print(f'== ZCode direct 离线测试汇总：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 ==')
    for x in failed:
        print(f"FAILED {x['name']} :: {x['detail']}")
    (RUN_DIR / 'zcode-offline-summary.json').write_text(json.dumps(
        {'run_dir': str(RUN_DIR), 'script_under_test': str(script),
         'results': RESULTS}, ensure_ascii=False, indent=1), encoding='utf-8')
    return 0 if not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
