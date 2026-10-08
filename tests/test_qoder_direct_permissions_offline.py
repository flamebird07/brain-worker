#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BW-QODER-PERMISSIONS-01 权限参数离线回归：验证 --allowed-tools 可重复单值语义。

覆盖范围（与 build_argv 保持原 5 位置参数兼容）：
- --tools 逗号列表兼容回退逐项生成 --allowed-tools；空 tools 不授予权限；
- 显式 allowed_tools（包括空列表）完全替代回退，不追加 Write/Edit/Bash 等默认；
- 规则原样保留：不按逗号拆分（Bash 里可以有逗号），不去空白，无 shell 拼接；
- --disallowed-tools/--add-dir 可重复，各自作为独立单值参数出现在 argv；
- 严格校验非字符串项、空/仅空白字符串项、非列表输入 → 拒绝，绝不静默过滤；
- --tools 只影响可见性；permission-mode 固定 dont_ask，不使用 bypass_permissions；
- CLI 层重复 --allowed-tools/--disallowed-tools/--add-dir 到达实际 stub 子进程
  的 request.json argv（真实传输层证据，非纯函数断言）。

历史真实测试只证明单 Read；本文件不声称多工具真实调用已通过，只覆盖参数构造
与 CLI→子进程 argv 落盘。真实细粒度权限由主脑另作隔离验证。

用法：python tests/test_qoder_direct_permissions_offline.py [--script <path>]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CAND = HERE.parent
RUN_DIR = HERE / ('perms-run-' + time.strftime('%Y%m%d-%H%M%S'))
RESULTS = []


def check(name, passed, detail=''):
    RESULTS.append({'name': name, 'pass': bool(passed), 'detail': str(detail)[:300]})
    print(('PASS ' if passed else 'FAIL ') + name
          + ('' if passed else f'  >>> {str(detail)[:400]}'))


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location('qd_perms', path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def expect_raises(fn, exc=ValueError, label=''):
    try:
        fn()
    except exc as err:
        return True, str(err)
    except Exception as err:  # noqa: BLE001
        return False, f'{type(err).__name__}: {err}'
    return False, f'no exception for {label}'


def find_all(argv, flag):
    """Return the list of values emitted for a repeatable flag in argv order."""
    out = []
    for i, tok in enumerate(argv):
        if tok == flag:
            if i + 1 >= len(argv) or argv[i + 1].startswith('--'):
                raise AssertionError(f'{flag} missing value in argv')
            out.append(argv[i + 1])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--script', default=str(CAND / 'scripts' / 'qoder_direct.py'))
    args = ap.parse_args()
    script_path = Path(args.script).resolve(strict=True)
    RUN_DIR.mkdir(parents=True, exist_ok=False)
    print(f'== Qoder direct 权限参数离线测试 run {RUN_DIR.name} ==')
    print(f'under test: {script_path}')
    qd = load_module(script_path)
    cfg = {'node': 'NODE', 'qodercli': 'QODERCLI'}

    def build(**kw):
        kw.setdefault('workspace', 'WS')
        kw.setdefault('model', 'M')
        kw.setdefault('tools', '')
        kw.setdefault('session_id', None)
        ws = kw.pop('workspace')
        model = kw.pop('model')
        tools = kw.pop('tools')
        sid = kw.pop('session_id')
        return qd.build_argv(cfg, ws, model, tools, sid, **kw)

    # ---------- 兼容回退：--tools 逗号列表逐项 → --allowed-tools ----------
    a = build(tools='Read,Write,Edit,Bash')
    check('fallback splits --tools into one --allowed-tools per item',
          find_all(a, '--allowed-tools') == ['Read', 'Write', 'Edit', 'Bash'],
          a[-8:])
    check('fallback keeps single-item --tools as one --allowed-tools (round01 shape)',
          build(tools='Read')[-2:] == ['--allowed-tools', 'Read']
          and find_all(build(tools='Read'), '--allowed-tools') == ['Read'],
          build(tools='Read'))
    z = build(tools='')
    check('empty --tools grants zero --allowed-tools',
          '--allowed-tools' not in z and z[-1] == '-p', z)

    # --tools 只影响可见性：--tools 参数本身仍在 argv
    vis = build(tools='Read,Grep')
    check('--tools preserved as visibility argument even with permission rules',
          vis[vis.index('--tools') + 1] == 'Read,Grep'
          and find_all(vis, '--allowed-tools') == ['Read', 'Grep'], vis)

    # ---------- 显式 allowed_tools 完全替代回退 ----------
    e = build(tools='Read,Write',
              allowed_tools=['Edit(/scripts/qoder_direct.py)', 'Read'])
    check('explicit allowed_tools replaces fallback (no silent defaults)',
          find_all(e, '--allowed-tools') == ['Edit(/scripts/qoder_direct.py)', 'Read'],
          e)
    ee = build(tools='Read,Write,Edit,Bash', allowed_tools=[])
    check('explicit empty allowed_tools yields zero allow rules (no fallback)',
          '--allowed-tools' not in ee and ee[-1] == '-p', ee)
    ed = build(allowed_tools=['Edit(/scripts/qoder_direct.py)'])
    check('no --tools still works when explicit rules given',
          find_all(ed, '--allowed-tools') == ['Edit(/scripts/qoder_direct.py)']
          and '--tools' in ed and ed[ed.index('--tools') + 1] == '', ed)

    # ---------- 规则原样保留：不按逗号拆分，不去空白 ----------
    comma_rule = 'Bash(git commit -m "hi, world")'
    c = build(allowed_tools=[comma_rule])
    check('rule containing comma is preserved verbatim as one --allowed-tools value',
          find_all(c, '--allowed-tools') == [comma_rule], c)
    space_rule = 'Bash(echo hello world)'
    sp = build(allowed_tools=[space_rule])
    check('rule containing spaces preserved verbatim',
          find_all(sp, '--allowed-tools') == [space_rule], sp)
    both_rule = 'Bash(python -c "print(1, 2)")'
    b = build(allowed_tools=[both_rule])
    check('rule containing both commas and spaces preserved verbatim',
          find_all(b, '--allowed-tools') == [both_rule], b)

    # ---------- 重复的 allow / deny / add-dir ----------
    rep = build(allowed_tools=['Read', 'Grep', 'Edit(/a.py)'],
                disallowed_tools=['WebFetch', 'Bash(rm -rf:*)'],
                add_dirs=['C:/other/root', 'D:/extra'])
    check('repeated --allowed-tools emits one flag per rule',
          find_all(rep, '--allowed-tools') == ['Read', 'Grep', 'Edit(/a.py)'], rep)
    check('repeated --disallowed-tools emits one flag per rule',
          find_all(rep, '--disallowed-tools') == ['WebFetch', 'Bash(rm -rf:*)'], rep)
    check('repeated --add-dir emits one flag per directory',
          find_all(rep, '--add-dir') == ['C:/other/root', 'D:/extra'], rep)

    # ---------- 严格校验非法输入 ----------
    ok, why = expect_raises(
        lambda: build(allowed_tools=['Read', 42]),
        label='non-string item in allowed_tools')
    check('non-string item in allowed_tools rejected', ok, why)
    ok, why = expect_raises(
        lambda: build(allowed_tools=['Read', None]),
        label='None item')
    check('None item in allowed_tools rejected', ok, why)
    ok, why = expect_raises(
        lambda: build(allowed_tools=['Read', '   ']),
        label='whitespace-only rule')
    check('whitespace-only rule in allowed_tools rejected (never silently filtered)',
          ok, why)
    ok, why = expect_raises(
        lambda: build(allowed_tools=['Read', '']),
        label='empty string rule')
    check('empty string rule in allowed_tools rejected', ok, why)
    ok, why = expect_raises(
        lambda: build(disallowed_tools=['WebFetch', 3.14]),
        label='non-string disallow')
    check('non-string item in disallowed_tools rejected', ok, why)
    ok, why = expect_raises(
        lambda: build(add_dirs=['path', ' ']),
        label='whitespace-only dir')
    check('whitespace-only add-dir rejected', ok, why)
    ok, why = expect_raises(
        lambda: build(allowed_tools='Read'),
        label='bare string instead of list')
    check('allowed_tools as bare string (not list) rejected', ok, why)
    ok, why = expect_raises(
        lambda: build(disallowed_tools='WebFetch'),
        label='bare string disallowed_tools')
    check('disallowed_tools as bare string rejected', ok, why)
    ok, why = expect_raises(
        lambda: build(add_dirs='one/dir'),
        label='bare string add_dirs')
    check('add_dirs as bare string rejected', ok, why)

    # ---------- 兼容回退：--tools 严格校验 ----------
    ok, why = expect_raises(
        lambda: build(tools='Read,,Write'),
        label='legacy tools with empty item')
    check('legacy --tools empty item rejected (never silently filtered)', ok, why)
    ok, why = expect_raises(
        lambda: build(tools='Read, Write'),
        label='legacy tools with surrounding space')
    check('legacy --tools whitespace around item rejected', ok, why)
    ok, why = expect_raises(
        lambda: build(tools='Read,'),
        label='legacy tools trailing comma')
    check('legacy --tools trailing comma rejected', ok, why)

    # ---------- permission-mode 与 bypass ----------
    pm = build(tools='Read', allowed_tools=['Edit(/x.py)'],
               disallowed_tools=['WebFetch'], add_dirs=['C:/tmp'], session_id='s')
    check('permission-mode stays dont_ask; never bypass_permissions',
          pm[pm.index('--permission-mode') + 1] == 'dont_ask'
          and 'bypass_permissions' not in pm and 'bypassPermissions' not in pm, pm)
    check('resume appended after permission rules and dirs',
          pm[-2:] == ['--resume', 's']
          and pm.index('--allowed-tools') < pm.index('--disallowed-tools')
          < pm.index('--add-dir') < pm.index('--resume'), pm)

    # ---------- effective_permission_rules 纯函数一致性 ----------
    r1 = qd.effective_permission_rules(tools='Read,Write')
    check('effective_permission_rules applies fallback when allowed_tools=None',
          r1 == {'allowed_tools': ['Read', 'Write'],
                 'disallowed_tools': [], 'add_dirs': []}, r1)
    r2 = qd.effective_permission_rules(tools='Read,Write', allowed_tools=[])
    check('effective_permission_rules honors explicit empty list (no fallback)',
          r2['allowed_tools'] == [], r2)
    r3 = qd.effective_permission_rules(tools=None, allowed_tools=None)
    check('effective_permission_rules with tools=None grants zero',
          r3 == {'allowed_tools': [], 'disallowed_tools': [], 'add_dirs': []}, r3)

    # ---------- CLI→stub 子进程：实际 argv 落到 request.json ----------
    stub_cfg = RUN_DIR / 'entry.json'
    stub_cfg.write_text(json.dumps(
        {'node': sys.executable, 'qodercli': str(HERE / 'stub_qodercli.py')}),
        encoding='utf-8')
    work = RUN_DIR / 'proj'
    work.mkdir(parents=True)
    promptp = RUN_DIR / 'prompt.txt'
    promptp.write_text('权限参数离线回归。', encoding='utf-8')
    reportp = RUN_DIR / 'stub-report.txt'
    reportp.write_text(
        'WORKER_REPORT_START\n阶段编号与执行方式：BW-QODER-PERMISSIONS-01-OFFLINE\n'
        f'实际项目绝对路径：{work}\n汇报时间与执行环境：离线\n\n'
        '一、当前基线与授权\n- 略\n\n二、实际执行范围\n- 略\n\n三、已验证事实\n- 略\n\n'
        '四、推断（必须与事实分开）\n- 略\n\n五、测试与验证\n- 略\n\n'
        '六、未完成项与剩余风险\n- 略\n\n七、实际副作用与越界检查\n- 略\n\n'
        '八、本阶段状态\n- 略\n\n九、建议下一步（只提出建议，不执行）\n- 略\n\n'
        '本阶段汇报结束；等待主脑验收。\nWORKER_REPORT_END\n', encoding='utf-8')

    def run_cli(out_dir, extra):
        env = os.environ.copy()
        env['STUB_MODE'] = 'ok'
        env['STUB_REPORT_FILE'] = str(reportp)
        # 并发容量池隔离：每个 out 用各自全新的临时 store，绝不读写真实 ~/.brain-worker 池。
        env['BRAIN_WORKER_DISPATCH_STORE'] = str(
            RUN_DIR / ('dispatch-' + Path(out_dir).name + '.sqlite3'))
        cmd = [sys.executable, str(script_path),
               '--workspace', str(work), '--prompt-file', str(promptp),
               '--output-dir', str(out_dir), '--config', str(stub_cfg),
               '--stage', 'BW-QODER-PERMISSIONS-01-OFFLINE'] + extra
        return subprocess.run(cmd, capture_output=True, env=env, timeout=120)

    # 场景 A：默认零工具（不传 --tools、不传 --allowed-tools）
    outA = RUN_DIR / 'e-default-zero'
    rA = run_cli(outA, [])
    reqA = json.loads((outA / 'request.json').read_text(encoding='utf-8'))
    check('CLI default zero: no --allowed-tools in argv, no --tools value',
          rA.returncode == 0 and '--allowed-tools' not in reqA['argv']
          and reqA['argv'][reqA['argv'].index('--tools') + 1] == ''
          and reqA['allowed_tools'] == [],
          f'rc={rA.returncode}, argv={reqA["argv"]}, allowed_tools={reqA.get("allowed_tools")}')

    # 场景 B：仅 --tools Read，触发兼容回退（单工具，与历史真实调用一致形状）
    outB = RUN_DIR / 'e-tools-only'
    rB = run_cli(outB, ['--tools', 'Read'])
    reqB = json.loads((outB / 'request.json').read_text(encoding='utf-8'))
    check('CLI --tools Read emits one --allowed-tools Read (compat fallback)',
          rB.returncode == 0
          and find_all(reqB['argv'], '--allowed-tools') == ['Read']
          and reqB['allowed_tools'] == ['Read']
          and reqB['tools'] == 'Read',
          f'rc={rB.returncode}, argv={reqB["argv"]}')

    # 场景 C：CLI 重复 --allowed-tools/--disallowed-tools/--add-dir，规则含逗号空格原样到达
    outC = RUN_DIR / 'e-cli-repeat'
    rule_with_comma = 'Bash(git commit -m "hi, there")'
    rC = run_cli(outC, ['--tools', 'Read',
                        '--allowed-tools', 'Edit(/scripts/qoder_direct.py)',
                        '--allowed-tools', rule_with_comma,
                        '--disallowed-tools', 'WebFetch',
                        '--disallowed-tools', 'Bash(rm -rf:*)',
                        '--add-dir', str(work / 'outside'),
                        '--add-dir', str(RUN_DIR)])
    reqC = json.loads((outC / 'request.json').read_text(encoding='utf-8'))
    contracts = find_all(reqC['argv'], '--append-system-prompt')
    check('CLI carries stage and workspace report contract in system channel',
          len(contracts) == 1 and reqC['stage'] in contracts[0]
          and str(work) in contracts[0], contracts)
    check('CLI repeated --allowed-tools reaches stub argv verbatim (comma rule preserved)',
          rC.returncode == 0
          and find_all(reqC['argv'], '--allowed-tools')
          == ['Edit(/scripts/qoder_direct.py)', rule_with_comma],
          f'rc={rC.returncode}, argv={reqC["argv"]}')
    check('CLI repeated --disallowed-tools reaches stub argv verbatim',
          find_all(reqC['argv'], '--disallowed-tools')
          == ['WebFetch', 'Bash(rm -rf:*)'], reqC['argv'])
    check('CLI repeated --add-dir reaches stub argv',
          find_all(reqC['argv'], '--add-dir')
          == [str(work / 'outside'), str(RUN_DIR)], reqC['argv'])
    check('CLI explicit --allowed-tools replaces --tools fallback (no Read leak)',
          'Read' not in find_all(reqC['argv'], '--allowed-tools'),
          reqC['argv'])
    check('request.json records effective rules and dirs for audit',
          reqC['allowed_tools'] == ['Edit(/scripts/qoder_direct.py)', rule_with_comma]
          and reqC['disallowed_tools'] == ['WebFetch', 'Bash(rm -rf:*)']
          and reqC['add_dirs'] == [str(work / 'outside'), str(RUN_DIR)]
          and reqC['tools'] == 'Read',
          {k: reqC.get(k) for k in ('allowed_tools', 'disallowed_tools', 'add_dirs', 'tools')})

    # 场景 D：CLI 传空 --allowed-tools 应被拒绝（不允许静默过滤成零）
    outD = RUN_DIR / 'e-cli-empty-rule'
    rD = run_cli(outD, ['--allowed-tools', ''])
    check('CLI empty --allowed-tools rejected before subprocess (no silent filter)',
          rD.returncode != 0 and not (outD / 'request.json').exists()
          and (b'non-empty' in rD.stderr or b'whitespace' in rD.stderr),
          f'rc={rD.returncode}, stderr={rD.stderr.decode("utf-8","replace")[:220]}')

    # 场景 E：CLI 空白 --allowed-tools 应被拒绝
    outE = RUN_DIR / 'e-cli-ws-rule'
    rE = run_cli(outE, ['--allowed-tools', '   '])
    check('CLI whitespace-only --allowed-tools rejected',
          rE.returncode != 0 and not (outE / 'request.json').exists(),
          f'rc={rE.returncode}, stderr={rE.stderr.decode("utf-8","replace")[:220]}')

    failed = [x for x in RESULTS if x['pass'] is False]
    print(f'== 权限参数离线测试汇总：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 ==')
    for x in failed:
        print(f"FAILED {x['name']} :: {x['detail']}")
    (RUN_DIR / 'perms-summary.json').write_text(json.dumps(
        {'run_dir': str(RUN_DIR), 'script_under_test': str(script_path),
         'results': RESULTS}, ensure_ascii=False, indent=1), encoding='utf-8')
    return 0 if failed == [] else 1


if __name__ == '__main__':
    raise SystemExit(main())
