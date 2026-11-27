"""Read-only discovery of default worker entry configuration and runtime files.

present means the configured or discovered runtime is available locally.
missing means a configuration points to a missing runtime file.
unconfigured means default configuration/discovery supplied no runtime.
unknown means runtime availability could not be determined. Explicit launch
configs may differ from the defaults inspected here. This probe never resolves
credentials, starts an executor, or writes state.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import claude_direct      # noqa: E402  仅用 load_entry_config；绝不触 resolve_token
import codebuddy_direct   # noqa: E402
import dispatch_pool as dp  # noqa: E402
import opencode_direct    # noqa: E402
import qoder_direct as qd  # noqa: E402
import zcode_direct       # noqa: E402

PRESENT = 'present'
MISSING = 'missing'
UNKNOWN = 'unknown'
UNCONFIGURED = 'unconfigured'


def _load_status(load):
    """调用某入口自己的加载器并归类。成功返回 (PRESENT, cfg)；FileNotFoundError
    返回 (MISSING, None)——配置指向的运行时文件确定不存在；其它任何异常
    返回 (UNKNOWN, None)——结构坏/无法判定，绝不读作正常。"""
    try:
        cfg = load()
    except FileNotFoundError:
        return MISSING, None
    except Exception:
        return UNKNOWN, None
    return PRESENT, cfg


def _configured_load_status(config_path, load, fallback=None):
    """R1G 判定边界：入口配置文件本身不存在 = 该引擎副本未配置该执行器 →
    (UNCONFIGURED, None)，绝不判 missing（正式入口支持 --config 显式传参，无缺省配置
    不是物理死通道；否则裸仓库/CI 会被硬门清场）。配置存在才交给加载器判定。"""
    if not Path(config_path).is_file() and (fallback is None or not fallback().is_file()):
        return UNCONFIGURED, None
    return _load_status(load)


def _probe_codebuddy() -> str:
    # load_entry_config 已要求 node、cli 均为绝对且存在的文件，加载成功即 present。
    status, _ = _configured_load_status(codebuddy_direct.DEFAULT_CONFIG,
                                        codebuddy_direct.load_entry_config)
    return status


def _probe_qoder() -> str:
    # 同上：node、qodercli 绝对且存在。
    status, _ = _configured_load_status(qd.DEFAULT_CONFIG, qd.load_entry_config,
                                        qd.installed_engine_config)
    return status


def _probe_qodercn() -> str:
    # cli 存在文件、config_dir 存在目录、model_ids 非空——加载器已全部强制；
    # model_ids 结构坏抛 ValueError，如实归 unknown 而非 missing。
    status, _ = _configured_load_status(qd.DEFAULT_CN_CONFIG, qd.load_cn_config,
                                        qd.installed_engine_cn_config)
    return status


def _probe_zcode() -> str:
    # CONFIG_FILE_KEYS（node/bootstrap/tsx_loader/builtin/personal）绝对且存在，
    # runner 若给出也由加载器校验；environment/node_args 结构坏是 ValueError → unknown。
    status, _ = _configured_load_status(zcode_direct.DEFAULT_CONFIG,
                                        zcode_direct.load_zcode_config)
    return status


def _probe_claude() -> str:
    status, cfg = _configured_load_status(claude_direct.DEFAULT_CONFIG,
                                          claude_direct.load_entry_config)
    if status is not PRESENT:
        return status
    # 该加载器只校验 JSON 对象存在；真实运行时是 build_argv 的 executable +
    # launcher_args 里的绝对路径（本探针绝不读 credential_source / 凭据字段）。
    exe = cfg.get('executable')
    if not isinstance(exe, str) or not exe.strip():
        return UNKNOWN  # 无运行时路径可判（build_argv 也会拒绝，但非「文件缺失」）
    launcher = cfg.get('launcher_args')
    if launcher is None:
        launcher = []
    if not isinstance(launcher, (list, tuple)) or any(
            not isinstance(a, str) or not a for a in launcher):
        return UNKNOWN  # 与 build_argv 同口径的结构坏，非文件缺失
    exe_path = Path(exe.strip())
    if exe_path.is_absolute():
        paths = [exe_path]
    else:
        # 相对名走 PATH：环境性解析失败不作确凿 missing（R1G 判定边界）。
        if shutil.which(exe.strip()) is None:
            return UNKNOWN
        paths = []
    # launcher_args 里的绝对路径逐个实存校验（如官方 Node CLI 的 cli.js）；
    # 相对项可能是 flag，无法按路径判定，跳过而不是猜。
    paths.extend(Path(a) for a in launcher if Path(a).is_absolute())
    for p in paths:
        if not p.is_file():
            return MISSING
    return PRESENT


def _probe_opencode() -> str:
    configured = (Path(opencode_direct.DEFAULT_CONFIG).is_file()
                  or opencode_direct.installed_engine_entry_config().is_file())
    status, cfg = _load_status(opencode_direct.load_entry_config)
    if status != PRESENT:
        return status
    # 无配置是合法形态（{} 走 PATH 发现）。判定链：cfg.executable/exe →
    # OPENCODE_EXE → PATH。只有「配置显式给出绝对路径而该文件不存在」才是确凿
    # missing；无配置/相对名/PATH、OPENCODE_EXE 的环境性成败一律不作 missing。
    raw = cfg.get('executable', cfg.get('exe'))
    if raw is not None and (not isinstance(raw, str) or not raw.strip()):
        return UNKNOWN
    absolute_configured = (isinstance(raw, str) and Path(raw.strip()).is_absolute())
    try:
        opencode_direct.resolve_executable(cfg)
    except ValueError:
        if absolute_configured:
            return MISSING
        return UNKNOWN if configured or os.environ.get('OPENCODE_EXE') else UNCONFIGURED
    except Exception:
        return UNKNOWN
    return PRESENT


RUNTIME_PROBES = {
    dp.CODEBUDDY_RUNTIME: _probe_codebuddy,
    qd.RUNTIME_INTERNATIONAL: _probe_qoder,
    qd.RUNTIME_CN: _probe_qodercn,
    # dispatch_pool 自身亦以字面量 'zcode' 引用该 runtime（见 argparse choices）。
    'zcode': _probe_zcode,
    claude_direct.RUNTIME: _probe_claude,
    dp.OPENCODE_RUNTIME: _probe_opencode,
}


def probe_runtime(runtime: str, _cache: dict | None = None) -> str:
    """判定单个 runtime 的 entry 实存三态。未知 runtime 名（未来新增、尚未登记
    判定规则）如实返回 unknown，绝不猜。任何异常内部消化，绝不抛出。"""
    cache = {} if _cache is None else _cache
    if runtime in cache:
        return cache[runtime]
    probe = RUNTIME_PROBES.get(runtime)
    if probe is None:
        status = UNKNOWN
    else:
        try:
            status = probe()
        except Exception:
            status = UNKNOWN
    cache[runtime] = status
    return status


def probe_entry_runtimes(pool_keys) -> tuple[dict, list]:
    """对池键集合逐个判定 entry runtime 实存，返回 ({pk: status}, missing pk 列表)。

    只读、零副作用；同 runtime 只探一次（两个 codebuddy 池共用同一 runtime），
    missing 列表保持入参顺序。纯可见性：调用方只把它放进回执，绝不据此过滤候选。"""
    cache: dict = {}
    status_map = {}
    for pk in pool_keys:
        runtime = str(pk).split(':', 1)[0]
        status_map[pk] = probe_runtime(runtime, _cache=cache)
    missing = [pk for pk, s in status_map.items() if s == MISSING]
    return status_map, missing


def opencode_tool_profile():
    """Report only effective tool names and whether Bash is enabled."""
    try:
        config = opencode_direct.build_opencode_config(opencode_direct.load_entry_config())
    except (ValueError, OSError):
        return None
    tools = sorted(name for name, rule in config['permission'].items() if rule == 'allow')
    return {'allowed_tools': tools, 'command_execution': 'bash' in tools}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Inspect configured worker runtimes without launching them')
    parser.add_argument('runtimes', nargs='*', help='executor names; default: all configured executor types')
    parser.add_argument('--json', action='store_true', help='emit JSON for scripts')
    args = parser.parse_args(argv)
    names = args.runtimes or list(RUNTIME_PROBES)
    results = {name: probe_runtime(name) for name in names}
    if args.json:
        print(json.dumps(results, sort_keys=True))
    else:
        for name, status in results.items():
            print(f'{name}: {status}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
