#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BW-QODER-DIRECT-02 round-02 targeted offline tests for the binding gate.

Covers the reproduced gaps the main brain flagged (offline only: no network,
no real model call). Kept separate from the proven 22-item suite so that suite
stays as an untouched regression baseline. Positive first-run and positive
resume must still bind; negative cases (protocol=false, missing session, resume
mismatch, readback mismatch, text outside markers, missing/fake-prefix/ambiguous
stage field) must refuse. --stage is required non-empty at call time.

Run: python test_qoder_direct_binding_offline.py [--script <qoder_direct.py>]
"""
from __future__ import annotations
import argparse, hashlib, importlib.util, json, os, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CAND = HERE.parent
RUN_DIR = HERE / ('binding-run-' + time.strftime('%Y%m%d-%H%M%S'))
FW = '：'  # full-width colon (U+FF1A) used as the field separator
SECTIONS = ('一、当前基线与授权', '二、实际执行范围', '三、已验证事实',
            '四、推断（必须与事实分开）', '五、测试与验证',
            '六、未完成项与剩余风险', '七、实际副作用与越界检查',
            '八、本阶段状态', '九、建议下一步（只提出建议，不执行）')
CLOSING = '本阶段汇报结束；等待主脑验收。'
RESULTS = []


def check(name, passed, detail=''):
    RESULTS.append({'name': name, 'pass': bool(passed), 'detail': str(detail)[:300]})
    print(('PASS ' if passed else 'FAIL ') + name + ('' if passed else f'  >>> {str(detail)[:400]}'))


def load_module(path):
    spec = importlib.util.spec_from_file_location('qd_bind', path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def report(stage, root, *, stage_field=None, path_field=None, close=True):
    sf = stage_field if stage_field is not None else f'阶段编号与执行方式{FW}{stage}。执行方式：离线。'
    pf = path_field if path_field is not None else f'实际项目绝对路径{FW}{root}'
    lines = ['WORKER_REPORT_START', sf, pf, '汇报时间与执行环境：离线' + '']
    for h in SECTIONS:
        lines += [h, '- 占位', '']
    if close:
        lines.append(CLOSING)
    lines.append('WORKER_REPORT_END')
    return '\n'.join(lines) + '\n'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--script', default=str(CAND / 'scripts' / 'qoder_direct.py'))
    args = ap.parse_args()
    script_path = Path(args.script).resolve(strict=True)
    RUN_DIR.mkdir(parents=True, exist_ok=False)
    print(f'== Qoder direct binding gate tests run {RUN_DIR.name} ==')
    print(f'under test: {script_path}')
    qd = load_module(script_path)
    stage = 'BW-QODER-DIRECT-02-BIND'
    work = RUN_DIR / 'proj'
    work.mkdir(parents=True)
    root = str(work)
    good = report(stage, root)

    st = qd.analyze_report(good, stage, root)
    check('positive first body binds (body_ok & bound True)',
          st['body_ok'] is True and st['bound'] is True and st['reasons'] == [],
          st['reasons'])
    pre = '全部工作与核验完成。以下为最终报告原文。\n' + good
    st = qd.analyze_report(pre, stage, root)
    check('preface outside markers refused',
          st['body_ok'] is False and any('first line' in r for r in st['reasons']),
          st['reasons'])
    st = qd.analyze_report(good + '额外后记\n', stage, root)
    check('trailing text after END refused',
          st['body_ok'] is False and any('last line' in r for r in st['reasons']),
          st['reasons'])
    st = qd.analyze_report(report(stage, root, stage_field=''), stage, root)
    check('missing stage field refused',
          st['body_ok'] is False and any('no 阶段编号' in r for r in st['reasons']),
          st['reasons'])
    st = qd.analyze_report(good, None, root)
    check('no expected --stage refuses (cannot confirm)',
          st['body_ok'] is False and st['stage_token_match'] is False, st['reasons'])
    st = qd.analyze_report(report(stage, root, stage_field=f'阶段编号误写{FW}{stage}'),
                           stage, root)
    check('fake same-prefix field 阶段编号... refused',
          st['body_ok'] is False and st['stage_field'] is None, st['reasons'])
    dup = good.replace(f'阶段编号与执行方式{FW}{stage}。执行方式：离线。',
                       f'阶段编号与执行方式{FW}{stage}。执行方式：离线。\n阶段编号{FW}{stage}')
    st = qd.analyze_report(dup, stage, root)
    check('duplicate ambiguous stage field refused',
          st['body_ok'] is False and any('ambiguous' in r for r in st['reasons']),
          st['reasons'])

    # finalize_binding pure aggregation (readback injected here, no file tampering)
    fb = qd.finalize_binding(True, [], protocol_success=True, session_id='sess',
                             requested_session_id=None, readback_match=True)
    check('finalize good first-run binds', fb['bound'] is True, fb)
    fb = qd.finalize_binding(True, [], protocol_success=False, session_id='sess',
                             requested_session_id=None, readback_match=True)
    check('finalize protocol=false refuses despite valid body',
          fb['bound'] is False and any('protocol' in r for r in fb['reasons']), fb['reasons'])
    fb = qd.finalize_binding(True, [], protocol_success=True, session_id=None,
                             requested_session_id=None, readback_match=True)
    check('finalize missing session refuses',
          fb['bound'] is False and any('non-empty session_id' in r for r in fb['reasons']), fb['reasons'])
    fb = qd.finalize_binding(True, [], protocol_success=True, session_id='returned',
                             requested_session_id='expected', readback_match=True)
    check('finalize resume mismatch refuses',
          fb['bound'] is False and any('not an exact match' in r for r in fb['reasons']), fb['reasons'])
    fb = qd.finalize_binding(True, [], protocol_success=True, session_id='expected',
                             requested_session_id='expected', readback_match=True)
    check('finalize positive resume binds', fb['bound'] is True, fb)
    fb = qd.finalize_binding(True, [], protocol_success=True, session_id='sess',
                             requested_session_id=None, readback_match=False)
    check('finalize readback mismatch refuses',
          fb['bound'] is False and any('readback' in r for r in fb['reasons']), fb['reasons'])

    # ---------- end-to-end via env-driven offline stub ----------
    stub = RUN_DIR / 'stub_env.py'
    stub.write_text(
        'import json,os,sys\nfrom pathlib import Path\n'
        'sys.stdin.buffer.read()\n'
        'mode=os.environ.get("STUB_MODE","ok")\n'
        'rep=Path(os.environ["STUB_REPORT_FILE"]).read_text(encoding="utf-8")\n'
        'env={"type":"result","subtype":"success","is_error":False,'
        '"stop_reason":"end_turn","session_id":"sess_stub_direct","modelUsage":"stub","total_credits":0,"result":rep}\n'
        'if mode=="bad_stop": env["stop_reason"]="max_tokens"\n'
        'if mode=="no_session": env.pop("session_id",None)\n'
        'sys.stdout.write(json.dumps(env,ensure_ascii=False))\n', encoding='utf-8')
    cfgp = RUN_DIR / 'stub-entry.json'
    cfgp.write_text(json.dumps({'node': sys.executable, 'qodercli': str(stub)}), encoding='utf-8')
    promptp = RUN_DIR / 'prompt.txt'
    promptp.write_text('绑定门禁离线测试。任务ID：BIND', encoding='utf-8')
    repfp = RUN_DIR / 'report.txt'
    repfp.write_text(good, encoding='utf-8')

    def run(mode, out, extra=None):
        env = os.environ.copy()
        env['STUB_MODE'] = mode
        env['STUB_REPORT_FILE'] = str(repfp)
        cmd = [sys.executable, str(script_path), '--workspace', root,
               '--prompt-file', str(promptp), '--output-dir', str(out),
               '--config', str(cfgp), '--stage', stage]
        if extra:
            cmd += extra
        return subprocess.run(cmd, capture_output=True, env=env, timeout=120)

    def state(out):
        return json.loads((out / 'report-state.json').read_text(encoding='utf-8')), \
               json.loads((out / 'summary.json').read_text(encoding='utf-8'))

    o = RUN_DIR / 'e-ok-first'; run('ok', o); rs, sm = state(o)
    check('e2e ok first-run final bound True (protocol+session+readback+body)',
          rs['bound'] is True and rs['body_ok'] is True and rs['protocol_success'] is True
          and rs['readback_match'] is True and sm['report_bound'] is True,
          rs['reasons'])

    o = RUN_DIR / 'e-resume-good'; run('ok', o, ['--resume-session-id', 'sess_stub_direct']); rs, sm = state(o)
    check('e2e positive resume still binds',
          rs['bound'] is True and rs['requested_session_id'] == 'sess_stub_direct', rs['reasons'])

    o = RUN_DIR / 'e-resume-bad'; run('ok', o, ['--resume-session-id', 'other-session']); rs, sm = state(o)
    check('e2e wrong resumed session refuses binding',
          rs['bound'] is False and any('not an exact match' in r for r in rs['reasons'])
          and (o / 'response.md').exists(), rs['reasons'])

    o = RUN_DIR / 'e-no-session'; run('no_session', o); rs, sm = state(o)
    check('e2e missing session refuses binding, keeps response',
          rs['bound'] is False and rs['protocol_success'] is True
          and any('non-empty session_id' in r for r in rs['reasons'])
          and (o / 'response.md').exists(), rs['reasons'])

    o = RUN_DIR / 'e-badstop'; rb = run('bad_stop', o); rs, sm = state(o)
    check('e2e protocol false but valid body: body_ok True yet final bound False',
          rs['body_ok'] is True and rs['bound'] is False and rs['protocol_success'] is False
          and rb.returncode == 3 and sm['report_bound'] is False
          and any('protocol' in r for r in rs['reasons'])
          and (o / 'response.md').exists(), rs['reasons'])

    # ---------- --stage required ----------
    o = RUN_DIR / 'e-nostage'
    env = os.environ.copy(); env['STUB_MODE'] = 'ok'; env['STUB_REPORT_FILE'] = str(repfp)
    r = subprocess.run([sys.executable, str(script_path), '--workspace', root,
                        '--prompt-file', str(promptp), '--output-dir', str(o),
                        '--config', str(cfgp)], capture_output=True, env=env, timeout=60)
    check('--stage required: refusal before invocation, no output dir created',
          r.returncode != 0 and not o.exists(), f'rc={r.returncode}, stderr={r.stderr.decode("utf-8","replace")[:200]}')

    failed = [x for x in RESULTS if x['pass'] is False]
    print(f'== 绑定门禁离线测试汇总：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 ==')
    for x in failed:
        print(f'FAILED {x["name"]} :: {x["detail"]}')
    (RUN_DIR / 'binding-summary.json').write_text(json.dumps(
        {'run_dir': str(RUN_DIR), 'script_under_test': str(script_path),
         'results': RESULTS}, ensure_ascii=False, indent=1), encoding='utf-8')
    return 0 if failed == [] else 1


if __name__ == '__main__':
    raise SystemExit(main())
