"""entry runtime 实存探针（BW-UNIFIED-DESKTOP-ENTRY-20261020 R1）。

根因回顾：stage_selection 裁决候选时从不校验执行器配置的运行时是否真实存在，把死
通道当合格候选选中，白烧一轮才在派发预检炸出 FileNotFoundError；round_failed_candidates
被 structured_limit_window_expired_no_inflight 清空后同一死执行器又按优先级 0 被重新选中。

本模块**只做只读可见性，不做资格门**：`_zcode_availability_blocked()` 已被用户的
BW-SIMPLE-PRIORITY-RETRY-S4 政策刻意中和为恒返回 None（绝不因本函数而永久或到期后挡
新轮真实尝试）；把 runtime 缺失做成硬资格门会与该现行用户决定直接冲突，升级需用户单独
拍板。因此探针结果只进回执两个附加字段，绝不影响 selected / selected_reason / 过滤。

三态口径：
- ``present``：该 runtime 的入口配置可加载，且其声明的运行时文件逐个存在；
- ``missing``：入口配置**存在**、但其指向的运行时文件**确定不存在**（加载器
  FileNotFoundError 或本探针的路径实存校验不过）——这正是本轮真实事故的形态，也是
  **唯一可作硬资格门依据**的状态；
- ``unknown``：无法判定——**入口配置文件本身不存在**（该引擎副本未配置该执行器；
  正式入口还支持 --config 显式传参，无缺省配置绝不等于死通道）、配置结构坏、非
  FileNotFoundError 的加载器异常、或依赖环境的解析失败（PATH/OPENCODE_EXE 发现、
  相对可执行名）。**unknown 绝不能被读成「正常」，也绝不能作为硬门依据。**

（R1G 修订，2026-10-20：最初版本把「配置文件不存在」也归 missing，导致无私有配置的
裸仓库/CI 环境里硬门把全部候选挡成 no_eligible_candidate——60 项规划器测试实爆。
环境相关性是判定边界：只有配置在、文件不在才是物理事实。）

复用各入口自己的加载器（load_entry_config / load_zcode_config / load_cn_config /
resolve_executable），不另写一套解析，规则天然与真实派发预检同源。特别注意 claude：
其 ``load_entry_config()`` 只校验 JSON 对象存在，真实运行时是 ``build_argv()`` 用的
``executable`` 与 ``launcher_args`` 里的绝对路径，必须额外逐个校验存在，否则探针会误报
present（本轮 Claude 事故：cli.js 整树被清而配置 JSON 完好）。

安全边界：纯只读——只调用加载器与路径 stat/which，零子进程、零写入；绝不读取或回显
``credential_source`` / token，绝不调用 ``claude_direct.resolve_token()``。任何异常都
在内部消化为 unknown，绝不让探针把规划器搞崩。同一次调用内按 runtime 缓存（两个
codebuddy 池共用同一 runtime 与配置，只探一次）。本文件导入即无副作用。
"""
from __future__ import annotations

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


def _configured_load_status(config_path, load):
    """R1G 判定边界：入口配置文件本身不存在 = 该引擎副本未配置该执行器 →
    (UNKNOWN, None)，绝不判 missing（正式入口支持 --config 显式传参，无缺省配置
    不是物理死通道；否则裸仓库/CI 会被硬门清场）。配置存在才交给加载器判定。"""
    if not Path(config_path).is_file():
        return UNKNOWN, None
    return _load_status(load)


def _probe_codebuddy() -> str:
    # load_entry_config 已要求 node、cli 均为绝对且存在的文件，加载成功即 present。
    status, _ = _configured_load_status(codebuddy_direct.DEFAULT_CONFIG,
                                        codebuddy_direct.load_entry_config)
    return status


def _probe_qoder() -> str:
    # 同上：node、qodercli 绝对且存在。
    status, _ = _configured_load_status(qd.DEFAULT_CONFIG, qd.load_entry_config)
    return status


def _probe_qodercn() -> str:
    # cli 存在文件、config_dir 存在目录、model_ids 非空——加载器已全部强制；
    # model_ids 结构坏抛 ValueError，如实归 unknown 而非 missing。
    status, _ = _configured_load_status(qd.DEFAULT_CN_CONFIG, qd.load_cn_config)
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
    cfg_path = Path(opencode_direct.DEFAULT_CONFIG)
    cfg = {}
    if cfg_path.is_file():
        status, loaded = _load_status(opencode_direct.load_entry_config)
        if status is not PRESENT:
            return status
        cfg = loaded or {}
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
        return MISSING if absolute_configured else UNKNOWN
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
