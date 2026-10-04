#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BW-QODER-DIRECT-02 直连脚本离线测试（不调用真实 Qoder CLI、不访问网络）。

覆盖：
- 入口配置：加载成功 / 缺文件 / 缺键 / 路径不存在
- build_argv：公开隔离夹具核对已验证的官方参数形状；--resume 附加
- analyze_report：正向绑定 + 反例（缺节/错节顺序/错阶段 token/错路径/内联标记/缺尾标记）
- stub 执行器全链路：输出文件布局、退出码 0/3、输出目录拒绝覆盖、
  协议成功但报告未绑定（协议成功不等于业务验收）

用法：python test_qoder_direct_offline.py [--script <qoder_direct.py 路径>]
（--script 用于安装后对正式安装目录的脚本重跑等价断言）
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CAND = HERE.parent
RUN_DIR = HERE / ('offline-run-' + time.strftime('%Y%m%d-%H%M%S'))
SECTIONS = ('一、当前基线与授权', '二、实际执行范围', '三、已验证事实',
            '四、推断（必须与事实分开）', '五、测试与验证',
            '六、未完成项与剩余风险', '七、实际副作用与越界检查',
            '八、本阶段状态', '九、建议下一步（只提出建议，不执行）')
CLOSING = '本阶段汇报结束；等待主脑验收。'
RESULTS = []


def check(name, passed, detail=''):
    RESULTS.append({'name': name, 'pass': bool(passed), 'detail': str(detail)[:300]})
    print(('PASS ' if passed else 'FAIL ') + name + ('' if passed else f'  >>> {str(detail)[:400]}'))


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location('qoder_direct_under_test', path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def build_report(stage: str, project_root: str, *, drop_section=None,
                 close_line=True) -> str:
    lines = ['WORKER_REPORT_START',
             f'阶段编号与执行方式：{stage}。执行方式：离线测试。',
             f'实际项目绝对路径：{project_root}',
             '汇报时间与执行环境：离线测试环境', '']
    for i, h in enumerate(SECTIONS):
        if drop_section == i:
            continue
        lines += [h, '- 内容占位', '']
    if close_line:
        lines.append(CLOSING)
    lines.append('WORKER_REPORT_END')
    return chr(10).join(lines) + chr(10)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--script', default=str(CAND / 'scripts' / 'qoder_direct.py'))
    args = ap.parse_args()
    script_path = Path(args.script).resolve(strict=True)
    RUN_DIR.mkdir(parents=True, exist_ok=False)
    print(f'== Qoder direct 候选离线测试 run {RUN_DIR.name} ==')
    print(f'under test: {script_path}')
    qd = load_module(script_path)

    # ---------- 入口配置 ----------
    entry_file = RUN_DIR / 'entry.json'
    entry_file.write_text(json.dumps({'node': sys.executable, 'qodercli': str(HERE / 'stub_qodercli.py')}), encoding='utf-8')
    cfg = qd.load_entry_config(entry_file)
    check('entry config loads with existing isolated fixture files',
          Path(cfg['node']).is_file() and Path(cfg['qodercli']).is_file(), cfg)
    try:
        qd.load_entry_config(HERE / 'no-such-config.json')
        check('entry config missing file raises', False, 'no exception')
    except FileNotFoundError:
        check('entry config missing file raises', True)
    bad = RUN_DIR / 'bad-config.json'
    bad.write_text(json.dumps({'node': cfg['node']}), encoding='utf-8')
    try:
        qd.load_entry_config(bad)
        check('entry config missing key raises', False, 'no exception')
    except KeyError:
        check('entry config missing key raises', True)
    bad2 = RUN_DIR / 'bad-path-config.json'
    bad2.write_text(json.dumps({'node': cfg['node'],
                                'qodercli': str(RUN_DIR / 'nope.js')}),
                    encoding='utf-8')
    try:
        qd.load_entry_config(bad2)
        check('entry config nonexistent qodercli raises', False, 'no exception')
    except FileNotFoundError:
        check('entry config nonexistent qodercli raises', True)

    # ---------- build_argv 与 round-01 已验证真实 argv 等价 ----------
    # Public portable fixture retains the verified official argument shape.
    round01 = {'workspace': str(RUN_DIR), 'model_requested': 'Qwen3.8-Flash', 'tools': 'Read', 'resume_session_id': None,
               'argv': [cfg['node'], cfg['qodercli'], '--cwd', str(RUN_DIR), '--model', 'Qwen3.8-Flash', '--tools', 'Read',
                        '--permission-mode', 'dont_ask', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                        '--output-format', 'json', '-p', '--allowed-tools', 'Read']}
    argv01 = round01['argv']
    built = qd.build_argv({'node': argv01[0], 'qodercli': argv01[1]},
                          round01['workspace'], round01['model_requested'],
                          round01['tools'], round01['resume_session_id'])
    check('build_argv matches official argument-shape fixture',
          built == argv01, f'built={built}')
    built_cfg = qd.build_argv(qd.load_entry_config(entry_file),
                              round01['workspace'], round01['model_requested'],
                              round01['tools'], None)
    check('build_argv from isolated entry config equals fixture argv',
          built_cfg == argv01, f'built={built_cfg}')
    with_resume = qd.build_argv({'node': argv01[0], 'qodercli': argv01[1]},
                                round01['workspace'], round01['model_requested'],
                                '', 'sess-abc')
    check('build_argv resume appends official --resume flag',
          with_resume[:len(with_resume) - 2] == qd.build_argv(
              {'node': argv01[0], 'qodercli': argv01[1]},
              round01['workspace'], round01['model_requested'], '', None)
          and with_resume[-2:] == ['--resume', 'sess-abc'], with_resume[-4:])

    # ---------- analyze_report ----------
    stage_id = 'BW-QODER-DIRECT-02-OFFTEST'
    work = RUN_DIR / 'proj'
    work.mkdir(parents=True)
    report = build_report(stage_id, str(work))
    st = qd.analyze_report(report, stage_id, str(work))
    check('analyze_report full valid report binds', st['bound'] is True,
          json.dumps({k: st[k] for k in ('bound', 'reasons', 'sections_found')},
                     ensure_ascii=False))
    st = qd.analyze_report(build_report(stage_id, str(work), drop_section=3),
                           stage_id, str(work))
    check('analyze_report missing section refuses',
          st['bound'] is False and st['sections_missing'], st['sections_missing'])
    st = qd.analyze_report(build_report('OTHER-STAGE', str(work)), stage_id, str(work))
    check('analyze_report wrong stage token refuses',
          st['bound'] is False and st['stage_token_match'] is False, st['reasons'])
    st = qd.analyze_report(build_report(stage_id, str(RUN_DIR / 'elsewhere')),
                           stage_id, str(work))
    check('analyze_report wrong project path refuses',
          st['bound'] is False and st['path_match'] is False, st['reasons'])
    inline = build_report(stage_id, str(work)).replace('一、当前基线与授权',
                                                       '前缀 WORKER_REPORT_START 一、当前基线与授权')
    st = qd.analyze_report(inline, stage_id, str(work))
    check('analyze_report inline marker refuses',
          st['bound'] is False and not st['markers']['ok'], st['reasons'])
    truncated = build_report(stage_id, str(work)).rsplit('WORKER_REPORT_END', 1)[0]
    st = qd.analyze_report(truncated, stage_id, str(work))
    check('analyze_report missing end marker refuses',
          st['bound'] is False and not st['markers']['ok'], st['reasons'])


    # ---------- stub 执行器全链路 ----------
    stub_cfg_path = RUN_DIR / 'stub-entry.json'
    stub_cfg_path.write_text(json.dumps({
        'node': sys.executable, 'qodercli': str(HERE / 'stub_qodercli.py')},
        ensure_ascii=False, indent=1), encoding='utf-8')
    prompt_path = RUN_DIR / 'prompt.txt'
    prompt_path.write_text('离线链路测试提示词。任务ID：OFFTEST', encoding='utf-8')
    report_path = RUN_DIR / 'stub-report.txt'
    report_path.write_text(report, encoding='utf-8')

    def run_transport(mode, out_dir, stage=stage_id):
        env = os.environ.copy()
        env['STUB_MODE'] = mode
        env['STUB_REPORT_FILE'] = str(report_path)
        cmd = [sys.executable, str(script_path),
               '--workspace', str(work), '--prompt-file', str(prompt_path),
               '--output-dir', str(out_dir), '--config', str(stub_cfg_path)]
        if stage:
            cmd += ['--stage', stage]
        return subprocess.run(cmd, capture_output=True, env=env, timeout=120)

    out_ok = RUN_DIR / 'e2e-ok'
    r = run_transport('ok', out_ok)
    summary = json.loads((out_ok / 'summary.json').read_text(encoding='utf-8'))
    rs = json.loads((out_ok / 'report-state.json').read_text(encoding='utf-8'))
    check('e2e-ok exit 0 and protocol success',
          r.returncode == 0 and summary['protocol_success'] is True,
          f'rc={r.returncode}, summary={summary}')

    check('e2e-ok output layout complete',
          all((out_ok / f).exists() for f in
              ('request.json', 'process.json', 'stdout.json', 'stderr.log',
               'response.md', 'summary.json', 'report-state.json')),
          sorted(p.name for p in out_ok.iterdir()))
    resp_bytes = (out_ok / 'response.md').read_bytes()
    check('e2e-ok response.md verbatim byte-level carrier (no newline translation)',
          resp_bytes == report.encode('utf-8')
          and hashlib.sha256(resp_bytes).hexdigest() == summary['response_sha256'],
          f"sha={summary.get('response_sha256')}")
    check('e2e-ok report-state bound with all evidence',
          rs['bound'] is True and rs['readback_match'] is True
          and rs['protocol_success'] is True and rs['session_id'] == 'sess_stub_direct'
          and rs['sections_found'] == 9 and rs['closing_line_present'] is True,
          json.dumps(rs, ensure_ascii=False)[:400])
    req = json.loads((out_ok / 'request.json').read_text(encoding='utf-8'))
    check('e2e-ok request.json records stage/runtime/argv/prompt hash',
          req['stage'] == stage_id and req['runtime']['qodercli'].endswith('stub_qodercli.py')
          and req_runtime_argv(req) and len(req['prompt_sha256']) == 64,
          json.dumps({k: req[k] for k in ('stage', 'runtime')}, ensure_ascii=False))

    out_bad = RUN_DIR / 'e2e-badstop'
    r = run_transport('bad_stop', out_bad)
    summary = json.loads((out_bad / 'summary.json').read_text(encoding='utf-8'))
    check('e2e-badstop exit 3 and protocol failure',
          r.returncode == 3 and summary['protocol_success'] is False,
          f'rc={r.returncode}, stop_reason={summary.get("stop_reason")}')

    out_case3 = RUN_DIR / 'e2e-case3'
    r = run_transport('empty', out_case3)
    summary = json.loads((out_case3 / 'summary.json').read_text(encoding='utf-8'))
    rs = json.loads((out_case3 / 'report-state.json').read_text(encoding='utf-8'))
    glob_hit = list(out_case3.glob('response.md'))
    facts = [r.returncode == 0,
             summary['protocol_success'] is True,
             summary['report_bound'] is False,
             rs['bound'] is False,
             rs.get('carrier_missing') is True,
             len(glob_hit) == 0]
    check('e2e-case3 envelope has no result field: transport keeps evidence, marks report unbound',
          all(facts), json.dumps({'rc': r.returncode, 'facts': facts}, ensure_ascii=False))

    out_case4 = RUN_DIR / 'e2e-case4'
    r = run_transport('not_json', out_case4)
    summary = json.loads((out_case4 / 'summary.json').read_text(encoding='utf-8'))
    facts = [r.returncode == 3,
             summary['protocol_success'] is False,
             'parse_error' in summary,
             (out_case4 / 'stdout.json').exists()]
    check('e2e-case4 stdout is not official JSON: exit 3, evidence kept',
          all(facts), json.dumps({'rc': r.returncode, 'facts': facts}, ensure_ascii=False))

    before = {p.name: p.read_bytes() for p in out_ok.iterdir()}
    r = run_transport('ok', out_ok)
    after = {p.name: p.read_bytes() for p in out_ok.iterdir()}
    facts = [r.returncode != 0, before == after]
    check('e2e output dir replay refused: existing evidence unchanged',
          all(facts), json.dumps({'rc': r.returncode, 'facts': facts}, ensure_ascii=False))

    # ---------- 汇总 ----------
    failed = [x for x in RESULTS if x['pass'] is False]
    print(f'== 候选离线测试汇总：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 ==')
    for x in failed:
        print(f"FAILED {x['name']} :: {x['detail']}")
    (RUN_DIR / 'offline-summary.json').write_text(json.dumps(
        {'run_dir': str(RUN_DIR), 'script_under_test': str(script_path),
         'results': RESULTS}, ensure_ascii=False, indent=1), encoding='utf-8')
    return 0 if failed == [] else 1


def req_runtime_argv(req: dict) -> bool:
    """request.json 的 argv 应为 [node, qodercli, ...官方旗标]，与 build_argv 形状一致。"""
    argv = req['argv']
    return (argv[0] == req['runtime']['node']
            and argv[1] == req['runtime']['qodercli']
            and '--permission-mode' in argv and 'dont_ask' in argv
            and '--output-format' in argv and 'json' in argv
            and argv[-1] == '-p')


if __name__ == '__main__':
    raise SystemExit(main())
