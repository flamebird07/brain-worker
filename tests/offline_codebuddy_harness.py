"""测试专用离线回放 harness：把已退休的 CodeBuddy 直连传输链**完整搬出生产模块**，
只在本文件内以 `replay_dispatch(args)` 存在，且仅在**隔离合成 stub** 下重放，用于历史
429 / 限流 / 冷却 / 解析 / 终态留证的回归。

这不是生产入口，也不给生产留任何可执行传输旁路：
- 生产 `scripts/codebuddy_direct.py` 已删除 `dispatch_core` 与 `import subprocess`，
  只保留纯解析/终态诊断/历史证据函数（parse_stream、_cb_failure_facts、build_argv、
  load_entry_config、effective_permission_rules 等）。生产 CLI `main()` 在读配置/提示词、
  建输出、quota gate、Popen 之前无条件硬停，返回 manual_relay_only、sent=false、非成功
  退出码；没有任何命令行参数、环境变量或生产配置能把 main() 重新接回真实派发，也不能
  从生产模块直接 Popen 真实 CLI（模块内已无 subprocess 传输）。
- 旧真实传输（Popen 子进程 + 额度结算 + 终态留证）现在**只**存在于本测试文件，绝不在
  生产可达路径上；本文件不新增任何生产开关。

回放前置校验（全部满足才 replay，否则非零退出拒绝，绝不落到真实运行时/账号）：
  (1) 隔离标记环境变量 `CB_OFFLINE_SYNTHETIC_STUB=1`；
  (2) `--config` 不是生产默认入口配置（禁止默认用户配置）；
  (3) `config.node` 的 realpath 必须等于当前测试解释器 `sys.executable` 的 realpath
      —— 禁止真实 node / 任意 stub 目录里的真实 CLI（真实 CodeBuddy CLI 是 JS，无法在
      本解释器下作为脚本执行）；
  (4) `config.cli` 必须是仓库可信的固定合成 stub：UTF-8 脚本、首行恰为固定标记
      `STUB_MARKER`，且其磁盘字节 sha256 必须等于**仓库内固定文件**
      `tests/offline_codebuddy_stub.py` 当前字节的 sha256。信任锚是仓库里的这个文件，
      **环境变量绝不可能定义或放宽该白名单**：即便某个任意脚本自带固定标记、且调用方
      用环境变量自报一个与自身字节自洽的哈希，只要它不等于仓库可信 stub 的字节，一律
      拒绝、零 Popen。`run(argv)` 与 `replay_dispatch(args)` 两个传输入口都跑同一套校验。"""
import hashlib
import importlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import codebuddy_direct as cb  # noqa: E402  纯函数（解析/诊断/证据），生产模块无传输
import execution_control as ec  # noqa: E402
import prompt_contract as pc  # noqa: E402
import quota_control as qc  # noqa: E402
import continuation_contract as cc  # noqa: E402
from qoder_direct import analyze_report, finalize_binding  # noqa: E402

ISOLATION_FLAG = 'CB_OFFLINE_SYNTHETIC_STUB'
# 仓库可信固定合成 stub 的标记首行；仓库内固定 stub 文件必须以此行开头。
STUB_MARKER = '# OFFLINE-SYNTHETIC-CODEBUDDY-STUB v1'
# 信任锚：仓库内唯一的固定合成 stub 文件。可信字节/哈希由**仓库**定义（即本文件当前
# 磁盘字节的 sha256），绝不来自环境变量或调用方自报——env 无法定义/放宽该白名单。
TRUSTED_STUB_PATH = Path(__file__).resolve().parent / 'offline_codebuddy_stub.py'
TESTS_DIR = Path(__file__).resolve().parent


def _legacy_fixture_hashes():
    """仓库内既有、但**不迁移到 canonical stub** 的固定合成 stub 的字节哈希。

    下列测试各自内嵌一个 CODEBUDDY_STUB 常量，经 `Path.write_text(..., encoding='utf-8')`
    （未传 newline → 按 os.linesep 翻译换行）写出后交给本 harness 回放：
      - tests/test_prompt_contract.py：不在本轮编辑白名单，无法迁移；
      - tests/test_parallel_execution.py：其 CB stub 与共享 barrier/固定 429 信封耦合，
        迁移风险高于收益。
    为保留它们原始的载荷/字节/并行回归覆盖，这里**从仓库源码**派生其落盘后的确切字节
    哈希，信任锚仍固定在仓库（env 绝不可能定义或放宽该集合）。"""
    legacy_sources = (('test_prompt_contract', 'CODEBUDDY_STUB'),
                      ('test_parallel_execution', 'CODEBUDDY_STUB'))
    hashes = set()
    if str(TESTS_DIR) not in sys.path:
        sys.path.insert(0, str(TESTS_DIR))
    for mod_name, attr in legacy_sources:
        try:
            mod = importlib.import_module(mod_name)
            source = getattr(mod, attr)
        except Exception:  # noqa: BLE001  派生失败只缩小信任集，绝不放宽
            continue
        # 复刻 write_text(newline=None) 的换行翻译：'\n' → os.linesep，得到落盘确切字节。
        body = source.replace('\n', os.linesep).encode('utf-8')
        hashes.add(hashlib.sha256(body).hexdigest())
    return hashes


def _trusted_stub_hashes():
    """仓库可信 stub 的 sha256 集合（信任锚固定在仓库内，绝不来自环境变量）：
    (1) 仓库唯一固定合成 stub 文件 offline_codebuddy_stub.py 的真实磁盘字节；
    (2) 仓库内既有、本轮不可编辑的固定 legacy fixture（见 _legacy_fixture_hashes）。"""
    hashes = set(_legacy_fixture_hashes())
    try:
        hashes.add(hashlib.sha256(TRUSTED_STUB_PATH.read_bytes()).hexdigest())
    except OSError:
        pass
    return hashes


def _refuse(reason):
    print(json.dumps({'offline_harness_refused': True, 'synthetic_stub_isolation': False,
                      'reason': reason, 'exit_code': 2}, ensure_ascii=False))
    return 2


def _validate_replay_target(args):
    """返回 None 表示可安全回放；否则返回拒绝原因字符串。绝不读取生产默认配置内容，
    只做路径比较；node 必须是本解释器；cli 必须是仓库可信固定 stub（标记首行 + 字节
    sha256 等于仓库内 tests/offline_codebuddy_stub.py 的真实字节）。可信哈希只来自仓库
    文件，环境变量 CB_OFFLINE_STUB_SHA256 一律被忽略，无法定义或放宽白名单。"""
    try:
        cfg_path = Path(args.config).resolve()
    except OSError:
        return 'entry config path unresolvable in harness'
    try:
        default_cfg = Path(cb.DEFAULT_CONFIG).resolve()
    except OSError:
        default_cfg = None
    if default_cfg is not None and cfg_path == default_cfg:
        return ('refusing the production default entry config; the offline harness only '
                'replays an explicit test-supplied synthetic-stub config, never the '
                'default user config')
    try:
        cfg = cb.load_entry_config(args.config)
    except Exception as exc:  # noqa: BLE001  harness 只回放，任何配置错误都拒绝
        return f'entry config unreadable in harness: {exc}'
    node = cfg.get('node')
    cli = cfg.get('cli')
    if not node or os.path.realpath(str(node)) != os.path.realpath(sys.executable):
        return ('config node must be the current test interpreter (sys.executable); a '
                'real node/CodeBuddy runtime is forbidden in offline replay')
    if not cli:
        return 'config cli missing; nothing to replay'
    try:
        cli_path = Path(cli).resolve()
    except OSError:
        return 'config cli path unresolvable in harness'
    if not cli_path.is_file():
        return 'config cli is not an existing file'
    try:
        raw = cli_path.read_bytes()
        text = raw.decode('utf-8')
    except (OSError, UnicodeDecodeError):
        return 'config cli is not a UTF-8 synthetic stub script'
    lines = text.splitlines()
    if not lines or lines[0].strip() != STUB_MARKER:
        return ('config cli is not a repo-trusted synthetic stub (first line must be '
                f'exactly {STUB_MARKER!r}); a real CodeBuddy CLI is forbidden')
    # 信任锚固定在仓库：cli 字节必须与仓库可信固定 stub 集合中的某一个逐字节相同。
    # 自带标记 + 自报哈希自洽的任意脚本仍会被拒（哈希不在仓库可信集合内）。
    trusted = _trusted_stub_hashes()
    if not trusted:
        return ('repo-trusted synthetic stub is unavailable; refusing to replay without '
                'the fixed trusted fixture')
    actual = hashlib.sha256(raw).hexdigest()
    if actual not in trusted:
        return ('synthetic stub content hash mismatch: on-disk cli bytes do not equal '
                'any repo-trusted fixed stub (tests/offline_codebuddy_stub.py or a '
                'repo-blessed legacy fixture); an arbitrary script is refused even if '
                'it carries the marker and a self-consistent env-declared hash')
    return None


def _assert_isolated_replay(args):
    """run 与 replay_dispatch 共用的同一套前置门禁：隔离标记 + 目标校验。返回拒绝原因
    字符串（None 表示可回放）。任何传输入口都不得绕过。"""
    if os.environ.get(ISOLATION_FLAG) != '1':
        return (f'set {ISOLATION_FLAG}=1 to run the isolated synthetic-stub replay; '
                f'this harness never dispatches a real CodeBuddy runtime or account call')
    return _validate_replay_target(args)


def run(argv):
    try:
        args = cb.build_parser().parse_args(argv)
    except SystemExit as exc:
        # 参数解析失败：仍属拒绝，非成功退出，不落到流水线。
        return int(exc.code) if isinstance(exc.code, int) else 2
    reason = _assert_isolated_replay(args)
    if reason is not None:
        return _refuse(reason)
    # 只重放退休前的流水线；合成 stub 已确认，绝不接真实运行时。
    return replay_dispatch(args)


def replay_dispatch(args):
    """历史派发流水线（原 codebuddy_direct.dispatch_core，已完整搬出生产模块）：仅供
    本离线 harness 在合成 stub 下回放，保留原始解析/诊断/额度结算/终态与失败留证。
    生产 CLI 入口 main() 已在读配置/提示词、建输出、quota gate、Popen 之前硬停，绝不
    调用本函数；本函数不是任何命令行/参数/环境可重新启用的生产入口，只做纯离线证据回放。
    本函数自身再跑一遍与 run() 完全相同的隔离/目标门禁，任何直接调用（绕过 run）也拒绝。"""
    reason = _assert_isolated_replay(args)
    if reason is not None:
        return _refuse(reason)
    # ---- preflight：全部校验通过前不创建任何输出目录（失败退出码 2，零创建）----
    try:
        if not args.stage or not args.stage.strip():
            raise ValueError('--stage is required and must be non-empty')
        if not args.model or not args.model.strip():
            raise ValueError('--model is required and must be non-empty (no fallback)')
        cfg = cb.load_entry_config(args.config)
        work = Path(args.workspace).resolve(strict=True)
        if not work.is_dir():
            raise ValueError(f'workspace is not an existing directory: {args.workspace}')
        prompt_path = Path(args.prompt_file).resolve(strict=True)
        prompt_bytes = prompt_path.read_bytes()
        try:
            prompt = prompt_bytes.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise ValueError(f'prompt file is not valid UTF-8: {prompt_path}: {exc}')
        if not prompt.strip():
            raise ValueError(f'prompt file is empty: {prompt_path}')
        tools_items = cb.parse_tools_arg(args.tools)
        perms = cb.effective_permission_rules(tools_items, args.allowed_tools,
                                              args.disallowed_tools)
        out = Path(args.output_dir).resolve()
        if out.exists():
            raise ValueError(f'output directory already exists (refusing to '
                             f'overwrite/replay): {out}')
        # Windows scoped Read/Edit/Write 文件规则的派工前校验：无效形态在建目录与
        # Popen 之前退出 2、零创建、零模型额度；allow 与 deny 两侧同等校验。原始
        # 允许/拒绝列表随后仍逐字传给 CLI（不静默转换），非 Windows 维持原样行为。
        scoped_problems = cb.validate_windows_scoped_file_rules(
            perms['allowed_tools'], perms['disallowed_tools'], str(work))
        if scoped_problems:
            raise ValueError('invalid Windows scoped file grant: '
                             + ' | '.join(scoped_problems))
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({'preflight_error': str(exc), 'exit_code': 2},
                         ensure_ascii=False))
        return 2
    argv = cb.build_argv(cfg, str(work), args.model, tools_items, args.session_id,
                         allowed_tools=args.allowed_tools,
                         disallowed_tools=args.disallowed_tools)

    # ---- 任务级预检（仅 --dispatch-plan 时启用）：argv 建好后、Popen/建目录前 ----
    plan_block = None
    if args.dispatch_plan:
        try:
            plan = ec.load_plan(args.dispatch_plan)
            active_tasks = None
            if args.active_tasks:
                active_tasks = json.loads(Path(args.active_tasks).read_text(encoding='utf-8'))
        except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
            print(json.dumps({'dispatch_plan_rejected': True, 'sent': False,
                              'exit_code': 2, 'reasons': [str(exc)]}, ensure_ascii=False))
            return 2
        actual = {'task_id': plan.get('task_id'), 'stage': args.stage,
                  'runtime': 'codebuddy', 'model': args.model, 'workspace': str(work),
                  'cwd': str(work),
                  'prompt_sha256': hashlib.sha256(prompt_bytes).hexdigest(),
                  'argv': argv, 'shell': False,
                  'grants': ec.grants_from_rules(perms['allowed_tools'], [],
                                                 perms['disallowed_tools'],
                                                 tools_items)}
        result = ec.preflight(plan, actual, active_tasks=active_tasks)
        plan_block = {'plan_path': str(Path(args.dispatch_plan).resolve()),
                      'plan_hash': result['plan_hash'], 'ok': result['ok'],
                      'reasons': result['reasons'], 'argv_sha256': result['argv_sha256'],
                      'active_task_count': result['active_task_count'],
                      'is_atomic_lock': result['is_atomic_lock']}
        if not result['ok']:
            print(json.dumps({'dispatch_plan_rejected': True, 'sent': False,
                              'exit_code': 2, 'reasons': result['reasons'],
                              'plan_hash': result['plan_hash']}, ensure_ascii=False))
            return 2

    # ---- 持久额度冷却门禁（所有路径，含不传 --dispatch-plan 的兼容路径）----
    # Popen/建目录前原子检查+占位；拒绝必带 sent=false、原因与 UTC 截止，退出 2、
    # 零输出目录。quota_group 只来自受信任本机路由配置，plan 不能自报组绕过。
    # 通道身份用解析后的真实运行时入口（cli/node 的 realpath）而非 entry config
    # 路径（缺陷 G）：复制/改名同一入口配置不改变通道，配置别名无法绕开已知冷却。
    quota_store = args.quota_store or str(qc.default_store_path())
    channel_identity = {'entry_cli': str(Path(cfg['cli']).resolve()),
                        'node': str(Path(cfg['node']).resolve())}
    probe_problems = qc.validate_probe_dispatch(
        probe=args.quota_probe, tools_items=tools_items,
        resume_session_id=args.session_id, prompt_bytes=prompt_bytes,
        dispatch_plan=args.dispatch_plan,
        timeout_seconds=args.quota_probe_timeout if args.quota_probe else None)
    if probe_problems:
        print(json.dumps({'preflight_error': 'recovery probe bounds violated: '
                          + ' | '.join(probe_problems), 'exit_code': 2},
                         ensure_ascii=False))
        return 2
    continuation_spec = None
    if args.continuation_contract:
        try:
            continuation_spec = cc.load_continuation_contract(
                args.continuation_contract, str(work))
        except ValueError as exc:
            print(json.dumps({'preflight_error': f'continuation contract invalid: '
                              f'{exc}', 'exit_code': 2}, ensure_ascii=False))
            return 2
        drift = cc.check_prev_handoff_drift(continuation_spec, str(work))
        if drift:
            print(json.dumps({'continuation_drift_refused': True, 'sent': False,
                              'exit_code': 2, 'reasons': drift}, ensure_ascii=False))
            return 2
    quota_gate = qc.gate_dispatch(
        quota_store, runtime='codebuddy',
        identity=channel_identity,
        workspace=str(work),
        purpose='probe' if args.quota_probe else 'dispatch',
        routes_path=args.quota_routes)
    if not quota_gate['allowed']:
        print(json.dumps({'quota_gate_rejected': True, 'sent': False,
                          'exit_code': 2, 'reasons': quota_gate['reasons'],
                          'quota_group': quota_gate.get('quota_group'),
                          'cooldown_until_utc': quota_gate.get('cooldown_until_utc')},
                         ensure_ascii=False))
        return 2

    # gate 之后、Popen 之前的准备阶段失败都属"已知未启动"（S4 缺陷 2）：安全结算本
    # attempt 的 start_failed（只释放本次占位，绝不释放别人/活执行器），sent=false、零 Popen。
    def _abort_before_start(payload, code):
        payload = dict(payload)
        payload['quota_settlement'] = qc.settle_attempt(
            quota_store, quota_gate, terminal='start_failed')
        payload.setdefault('sent', False)
        print(json.dumps(payload, ensure_ascii=False))
        return code

    try:
        out.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        return _abort_before_start(
            {'quota_gate_rejected': False, 'refused_before_dispatch': True,
             'exit_code': 2, 'reasons': [f'output dir already exists: {out}']}, 2)
    except OSError as exc:
        # 普通 OSError/PermissionError/NotADirectoryError 等（非"已存在"）也是"已知未启动"：
        # 统一走 start_failed 结算本 attempt 占位，零 Popen、sent=false，不泄漏、不释放别人的。
        return _abort_before_start(
            {'quota_gate_rejected': False, 'refused_before_dispatch': True,
             'exit_code': 3,
             'reasons': [f'output dir could not be created before start: {exc!r}']}, 3)
    # 接续契约 baseline：Popen 前对有界声明文件保存 baseline 字节副本+SHA 并随
    # request 留证（缺陷 I/J）；终态确认后再 evaluate 生成 continuation.json。
    continuation_copies = out / 'continuation-copies'
    continuation_baseline = None
    if continuation_spec is not None:
        try:
            continuation_baseline = cc.freeze(continuation_spec.get('files') or [],
                                             str(work), copies_dir=continuation_copies)
        except (ValueError, OSError) as exc:
            return _abort_before_start(
                {'dispatch_failed': True, 'exit_code': 3,
                 'reasons': [f'continuation baseline freeze failed before start: {exc}']},
                3)
    # The shared contract is prepended to the verbatim task and the whole payload is
    # what actually goes to the child stdin; save those bytes and read them back so the
    # recorded hashes describe the real send, not a re-serialization of a JSON file.
    try:
        contract = pc.build_contract(args.stage, str(work))
        stdin_payload = pc.compose_task_payload(contract, prompt).encode('utf-8')
        (out / 'sent-payload-stdin.bin').write_bytes(stdin_payload)
        stdin_readback = (out / 'sent-payload-stdin.bin').read_bytes()
    except Exception as exc:
        # 契约构造/载荷落盘/回读失败（PermissionError/OSError/其他）仍属"已知未启动"：
        # 统一 settle start_failed、零 Popen、sent=false、只释放本 attempt 占位。
        return _abort_before_start(
            {'dispatch_failed': True, 'refused_before_dispatch': True, 'exit_code': 3,
             'reasons': [f'sent payload contract/write failed before start: {exc!r}']}, 3)
    prompt_sha256 = pc.sha256_hex(prompt_bytes)
    prompt_payload = pc.payload_evidence(
        raw_prompt_bytes=prompt_bytes, sent_task_text=prompt, contract=contract,
        sent_payload_bytes=stdin_payload,
        newline_caliber='raw-file-bytes-preserving-decode; task=stdin, contract prepended to task')
    prompt_payload['readback_match'] = stdin_readback == stdin_payload
    prompt_payload['channels'] = {'task_stdin_sha256': pc.sha256_hex(stdin_readback)}
    request = {'started_at': datetime.now(timezone.utc).isoformat(),
               'workspace': str(work), 'prompt_file': str(prompt_path),
               'prompt_sha256': prompt_sha256,
               'prompt_payload': prompt_payload,
               'model_requested': args.model, 'tools': args.tools,
               'tools_items': tools_items,
               'allowed_tools': list(perms['allowed_tools']),
               'disallowed_tools': list(perms['disallowed_tools']),
               'stage': args.stage, 'resume_session_id': args.session_id,
               'entry_config': str(Path(args.config).resolve()),
               'runtime': {'node': cfg['node'], 'cli': cfg['cli']},
               'argv': argv,
               'quota_gate': quota_gate,
               # 子进程显式有界 CLI 重试（只改本次子进程 env，不动用户全局）：
               # MAX_RETRIES=2 生成前有界指数+jitter 且尊重 retry-after；
               # WATCHDOG=0 关闭无限重试。调度层只记 CLI 最终终态，不叠加自动重跑。
               'cli_retry_policy': {'CODEBUDDY_MAX_RETRIES': '2',
                                    'CODEBUDDY_RETRY_WATCHDOG': '0',
                                    'source': 'explicit per-child env; user global '
                                              'environment untouched'}}
    if args.quota_probe:
        # probe 实际执行边界留证（缺陷 D）：记录程序化强制的边界值，不是 purpose 标记。
        request['quota_probe_bounds'] = {
            'purpose': 'recovery_probe',
            'forbidden_tools': list(qc.PROBE_FORBIDDEN_TOOLS),
            'granted_tools': list(tools_items),
            'resume_session_id': None,
            'dispatch_plan': None,
            'prompt_max_bytes': qc.PROBE_PROMPT_MAX_BYTES,
            'prompt_bytes_sent': len(stdin_payload),
            'timeout_seconds': args.quota_probe_timeout,
            'single_execution': True}
    if continuation_spec is not None:
        # 派发前有界声明文件 baseline 随 request 留证（缺陷 I）。
        request['continuation'] = {
            'contract_path': str(Path(args.continuation_contract).resolve()),
            'baseline': continuation_baseline,
            'prev_handoff': continuation_spec.get('prev_handoff'),
            'test_evidence': continuation_spec.get('test_evidence'),
            'original_report_ref': continuation_spec.get('original_report_ref'),
            'original_error_ref': continuation_spec.get('original_error_ref')}
    if plan_block is not None:
        request['dispatch_plan'] = plan_block
    try:
        (out / 'request.json').write_bytes(json.dumps(request, ensure_ascii=False,
                                                      indent=2).encode('utf-8'))
    except (OSError, TypeError, ValueError) as exc:
        return _abort_before_start(
            {'dispatch_failed': True, 'exit_code': 3,
             'reasons': [f'request.json serialization failed: {exc}']}, 3)
    env = os.environ.copy()
    env['DISABLE_AUTOUPDATER'] = '1'
    env['CODEBUDDY_MAX_RETRIES'] = '2'
    env['CODEBUDDY_RETRY_WATCHDOG'] = '0'
    # attempt 生命周期（缺陷 C）：占位释放绝不再先于终态识别/冷却落库。
    # - 启动前失败（从未 Popen）→ settle start_failed，安全释放占位；
    # - 终态无法确认（communicate 异常且无法回收）→ settle unknown，保留占位并退出；
    # - 已确认退出 → 解析终态 → 冷却写入/清除 + 占位释放，同一事务原子完成。
    child = None
    timed_out = False
    try:
        with (out / 'stdout.jsonl').open('wb') as stdout, \
                (out / 'stderr.log').open('wb') as stderr:
            child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=stdout,
                                     stderr=stderr, cwd=str(work), env=env)
            (out / 'process.json').write_bytes(json.dumps(
                {'pid': child.pid, 'state': 'running'}, ensure_ascii=False).encode('utf-8'))
            try:
                child.communicate(input=stdin_payload,
                                  timeout=args.quota_probe_timeout
                                  if args.quota_probe else None)
            except subprocess.TimeoutExpired:
                # 有界 recovery probe 超时：kill 后回收，按非 429 失败结算，绝不写 healthy。
                timed_out = True
                child.kill()
                child.communicate()
    except Exception as exc:
        if child is None:
            settlement = qc.settle_attempt(quota_store, quota_gate,
                                          terminal='start_failed')
            print(json.dumps({'dispatch_failed': True, 'sent': False,
                              'reasons': [f'child launch failed before start: {exc}'],
                              'quota_settlement': settlement}, ensure_ascii=False))
            return 3
        settlement = qc.settle_attempt(quota_store, quota_gate, terminal='unknown')
        print(json.dumps({'dispatch_failed': True, 'sent': True,
                          'terminal_state': 'unknown',
                          'reasons': [f'subprocess terminal state could not be '
                                      f'confirmed: {exc}',
                                      'workspace/channel/probe placeholders retained '
                                      '(fail-closed); verify the executor, then use '
                                      'the manual release path'],
                          'quota_settlement': settlement}, ensure_ascii=False))
        return 3
    exit_code = child.returncode
    parsed = cb.parse_stream((out / 'stdout.jsonl').read_bytes(),
                             args.model, tools_items)
    protocol_success = bool(parsed['protocol_success'] and exit_code == 0)
    if exit_code != 0:
        parsed['parse_errors'].append(f'process exited non-zero: {exit_code}')
    summary = {'protocol_success': protocol_success,
               'report_bound': False,
               'session_id': parsed['session_id'],
               'model_requested': args.model,
               'observed_models': parsed['observed_models'],
               'observed_tool_calls': parsed['observed_tool_calls'],
               'usage': parsed['usage'],
               'model_usage': parsed['model_usage'],
               'permission_denials': parsed['permission_denials'],
               'parse_errors': parsed['parse_errors'],
               'exit_code': exit_code,
               'terminal_reason': parsed['terminal_reason'],
               'terminal_state': parsed.get('terminal_state'),
               'parse_success': parsed.get('parse_success'),
               'failure_envelope': parsed.get('failure_envelope'),
               'failure_envelope_valid': parsed.get('failure_envelope_valid'),
               'primary_failure': parsed.get('primary_failure'),
               'result_errors': parsed.get('errors'),
               'errors_info': parsed.get('errors_info'),
               'reset_hint': parsed.get('reset_hint'),
               'failure_stage': parsed.get('failure_stage'),
               'recoverability': parsed.get('recoverability'),
               'tool_failures': parsed.get('tool_failures'),
               'tool_failure_stats': cb._tool_failure_stats(parsed.get('tool_failures')),
               'reinit_events': parsed.get('reinit_events'),
               'business_verified': False,
               'free_quota_verified': False,
               'model_backend_identity_verified': False,
               'usage_billing_basis': 'unknown; raw usage saved without interpretation',
               'finished_at': datetime.now(timezone.utc).isoformat(),
               'output_dir': str(out)}
    failure_facts = cb._cb_failure_facts(parsed, exit_code)
    summary['diagnostics'] = ec.diagnose(failure_facts)
    # 终态结算（缺陷 C/E/F）：明确 429（结构化 errors/errors_info，含从错误结构
    # 提取的 Retry-After）→ 同一事务写冷却并释放占位；真实成功 → 带 epoch/attempt
    # 核验的清除；非 429 失败/取消/probe 超时 → 只结算占位，既不写冷却也绝不写
    # healthy。CLI exit 0 但 result.is_error=true 的 429 信封同样触发冷却。
    quota_outcome = None
    classification = None
    if not protocol_success and failure_facts['quota_429'] and not timed_out:
        raw_errors = parsed.get('errors')
        raw_errors_info = parsed.get('errors_info')
        classification = qc.classify_quota_failure(
            raw_errors, raw_errors_info,
            retry_after=qc.extract_retry_after(raw_errors, raw_errors_info))
    # 保全优先于结算（S4 缺陷 4/6）：仍在持有工作区占位时，先把当前原始报告/失败信封落盘，
    # 再冻结终态源副本+回读、生成接续包，最后才做原子额度结算+释放，消除释放与保全之间的
    # 错冻版本空窗。当前报告/错误引用只从 out 固定路径派生（response.md/report-state.json
    # 本次是否真实存在如实反映），历史 spec 引用另列，绝不冒充当前。缺报告保留缺失不补写。
    if parsed['response_text'] is not None:
        (out / 'response.md').write_bytes(parsed['response_text'].encode('utf-8'))
        disk = (out / 'response.md').read_bytes()
        digest = hashlib.sha256(disk).hexdigest()
        summary['response_sha256'] = digest
        readback_match = digest == hashlib.sha256(
            parsed['response_text'].encode('utf-8')).hexdigest()
        body = analyze_report(parsed['response_text'], args.stage, str(work))
        binding = finalize_binding(
            body['body_ok'], body['reasons'],
            protocol_success=protocol_success,
            session_id=parsed['session_id'],
            requested_session_id=args.session_id,
            readback_match=readback_match)
        report_state = {**body, 'response_sha256': digest,
                        'readback_match': readback_match,
                        'protocol_success': protocol_success,
                        'session_id': parsed['session_id'],
                        'requested_session_id': args.session_id,
                        'binding': binding,
                        'bound': binding['bound'],
                        'reasons': binding['reasons']}
    else:
        report_state = {'bound': False, 'body_ok': False,
                        'protocol_success': protocol_success,
                        'parse_success': parsed.get('parse_success'),
                        'failure_envelope_valid': parsed.get('failure_envelope_valid'),
                        'reasons': ['no parseable text result in stream',
                                    *parsed['parse_errors']],
                        'carrier_missing': True}
        if parsed.get('failure_envelope'):
            report_state['terminal_state'] = parsed.get('terminal_state')
            report_state['primary_failure'] = parsed.get('primary_failure')
            report_state['reset_hint'] = parsed.get('reset_hint')
            report_state['failure_stage'] = parsed.get('failure_stage')
            report_state['recoverability'] = parsed.get('recoverability')
            report_state['reasons'].insert(
                0, f'failure envelope: no final report carrier; delivery failed '
                   f'(protocol_success stays false)')
        if parsed.get('carried_result') is not None:
            # 失败信封携带的 result 文本原样落盘待验，不参与绑定（不生成 response.md）。
            (out / 'failure-result.pending.txt').write_bytes(
                parsed['carried_result'].encode('utf-8'))
            report_state['carried_result_pending'] = str(out / 'failure-result.pending.txt')
            report_state['carried_result_bound'] = False
    (out / 'report-state.json').write_bytes(json.dumps(report_state, ensure_ascii=False,
                                                       indent=2).encode('utf-8'))
    summary['report_bound'] = bool(report_state.get('bound', False))
    summary['report_state_file'] = str(out / 'report-state.json')
    current_report = out / 'response.md'
    # 原始错误必须绑定真实失败载体：CodeBuddy 的失败信封在**当前 stdout.jsonl**里（原始
    # 流字节 + SHA 可追溯），report-state.json 只是独立格式诊断，不能冒充 original_error_ref。
    # 成功态没有失败载体则如实留 None；历史 spec 引用另列，绝不指向空报告状态。
    error_carrier = out / 'stdout.jsonl'
    current_error = error_carrier if (not protocol_success
                                      and error_carrier.is_file()) else None
    if continuation_baseline is not None:
        def _resolve_ws_ref(rel):
            if rel is None:
                return None
            return str(Path(os.path.join(str(work), str(rel))))
        try:
            evaluation = cc.evaluate(continuation_baseline, str(work),
                                     copies_dir=continuation_copies)
            test_binding = None
            test_stale = None
            tev = (continuation_spec or {}).get('test_evidence')
            if tev:
                test_binding = cc.bind_registered_test(
                    tev['evidence_path'], str(work), tev['registered_inputs'])
                test_stale = cc.test_inputs_stale(test_binding)
            handoff = cc.build_handoff(
                continuation_baseline, evaluation, test_binding, test_stale,
                (continuation_spec or {}).get('todos'),
                original_report_ref=str(current_report)
                if current_report.is_file() else None,
                original_error_ref=str(current_error)
                if current_error is not None and current_error.is_file() else None,
                write_receipts=cb._cb_write_receipts(parsed),
                terminal_state='probe_timeout' if timed_out else 'confirmed_exit')
            handoff['historical_refs'] = {
                'original_report_ref': _resolve_ws_ref(
                    (continuation_spec or {}).get('original_report_ref')),
                'original_error_ref': _resolve_ws_ref(
                    (continuation_spec or {}).get('original_error_ref')),
                'note': 'spec-carried refs are prior-handoff history only; the bound '
                        'current report/error above derive from this run\'s out/ path '
                        'and are never impersonated by older refs'}
            handoff['declared_diffs'] = cc.diff_declared(continuation_baseline, evaluation)
            handoff_bytes = json.dumps(handoff, ensure_ascii=False, indent=2).encode('utf-8')
            (out / 'continuation.json').write_bytes(handoff_bytes)
            summary['continuation'] = {
                'file': str(out / 'continuation.json'),
                'sha256': hashlib.sha256(handoff_bytes).hexdigest(),
                'workspace_realpath': handoff['workspace_realpath'],
                'final_worker_report_present': handoff['final_worker_report_present'],
                'any_changed': handoff['any_changed'],
                'test_inputs_stale': (handoff.get('test_inputs_stale') or {}).get('stale')}
        except Exception as exc:
            # 保全失败：保留真实错误 + 在途占位（settle unknown，不释放），不先释放再假装保全。
            summary['continuation_persist_failed'] = str(exc)
            settlement = qc.settle_attempt(quota_store, quota_gate, terminal='unknown')
            summary['quota_settlement'] = {
                'settled': settlement.get('settled'), 'released': settlement.get('released'),
                'terminal_state': settlement.get('terminal_state'),
                'purpose': quota_gate.get('purpose')}
            summary['placeholders_retained'] = True
            (out / 'summary.json').write_bytes(json.dumps(summary, ensure_ascii=False,
                                                          indent=2).encode('utf-8'))
            (out / 'process.json').write_bytes(json.dumps(
                {'pid': child.pid, 'state': 'exited', 'exit_code': exit_code},
                ensure_ascii=False).encode('utf-8'))
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 3
    else:
        summary['continuation_status'] = 'unverified_no_contract'
    # 额度终态结算（缺陷 C/E/F）：确认终态 → 同一事务原子写冷却/清除 + 释放占位。非 429
    # 失败/取消/probe 超时 → 只结算占位，既不写冷却也绝不写 healthy。迟到成功不得覆盖新冷却。
    settlement = qc.settle_attempt(
        quota_store, quota_gate, terminal='confirmed_exit',
        success=bool(protocol_success), classification=classification,
        source='codebuddy_direct terminal envelope (exit '
               f'{exit_code}, protocol_success={protocol_success}, '
               f'timed_out={timed_out})')
    quota_outcome = settlement.get('quota_outcome')
    summary['quota_settlement'] = {'settled': settlement.get('settled'),
                                   'released': settlement.get('released'),
                                   'terminal_state': settlement.get('terminal_state'),
                                   'purpose': quota_gate.get('purpose')}
    if timed_out:
        summary['quota_probe_timed_out'] = True
    summary['quota_outcome'] = quota_outcome
    if plan_block is not None:
        summary['dispatch_plan'] = plan_block
    (out / 'summary.json').write_bytes(json.dumps(summary, ensure_ascii=False,
                                                  indent=2).encode('utf-8'))
    (out / 'process.json').write_bytes(json.dumps(
        {'pid': child.pid, 'state': 'exited', 'exit_code': exit_code},
        ensure_ascii=False).encode('utf-8'))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if (protocol_success and summary['report_bound']) else 3


if __name__ == '__main__':
    raise SystemExit(run(sys.argv[1:]))
