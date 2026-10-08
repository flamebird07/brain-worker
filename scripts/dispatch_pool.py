"""dispatch_pool — brain-worker 跨会话并发容量池与路由（标准库 sqlite3）。

用户 2026-10-08 最终分配决策的可执行落地：所有会话共享同一持久池，绝不各自为政。

三个国内池 + 一个救援池（容量口径见 references/global-dispatch.md）：
- 主力 1:1：`zcode:GLM-5.3` 与 `qoder:Qwen3.8-Max` 各最多 2 个真实在途执行器；
- 溢出：`qoder:Qwen3.8-Flash` 最多 2 个，只有主力两池都满才允许；
- 国内合计 6；
- Luna（`luna:native`）只做救援、无数量上限，只有六个国内名额全满、真的问过用户、
  且 300 秒无回复后才可由宿主原生调用；本模块只持久化票据与竞争裁决，绝不谎称已经
  问过或已经派生 Luna（真实提问与原生工具由宿主负责）。

2026-10-08 修订二（BW-GLOBAL-POOL-20261008-Z2）在 Z1 之上的关键修正：
- 主力分配一律先按持久 committed 计数选择主力（跨聊天 1:1），平票才优先请求组合；
  本池有空位也不绕过轮转直接 claim（Z 任务结束后再请求 Z 会 routing 到 Max）。
- 两主力满时请求 Z/Max 一律 routing_required 到 Flash 入口，绝不替模型偷换组合落库；
  attempts.pool_key 恒等于 runtime+':'+model，任何入口不再虚记名额。
- select/reserve 成功国内 claim 的同一事务内取消同 task 旧 pending 待援票；用户已回复
  external_agent/cancel 的票据阻断该 task 的国内启动。
- claim_due：先核票据真实性与用户回复，再看国内空位（30 秒内释放也立即 domestic_
  reclaim，不必等到期）；只有六仍满才判 300 秒 deadline/明确 reply luna。国内回收与
  Luna 两个分支都写回票据原 scope 的真实绑定字段（stage/chat/prompt_sha256/workspace）
  并返回完整原 scope；传入 scope 时两个分支都必须与原 scope 一致。
- bind_child 校验当前真实调用者（wrapper）身份；finish 对无 child 的 Popen-bind 窗口/
  unknown attempt 只有 owner 身份一致才接受 start_failed/cancelled，unknown 无 child
  绝不能凭终态参数释放；reserved 未消费的取消仍合法。

2026-10-08 修订三（BW-GLOBAL-POOL-20261008-Z3）工程补齐：
- _insert_claim wrapper 分支 SQL 15 列对齐 15 个 VALUES（修复生产 auto-consume 直接
  OperationalError）；select/reserve 的防重顺序改为先判 native-on-Luna 再判普通在途
  （task_already_on_luna 理由准确，语义不变仍拒绝）；reconcile 对无 wrapper 无 child 的
  在途行（含 running）一律明确 held_unknown。
- process_identity：Linux 在 /proc 可读时返回稳定创建身份（boot_id + /proc/PID/stat
  starttime），Windows 沿用 GetProcessTimes；失败一律 unknown/无身份。
- adopt-legacy（CLI 同名）：受信任宿主专用的最小上线交接——只接纳宿主提供的既有原
  request.json 与邻近 process.json，核验真实活 PID 与创建身份后把老 worker 如实计入；
  仅 GLM-5.3 / Qwen3.8-Max / Flash，超上限如实计入并阻止新派；无 fake-alive 开关。

2026-10-08 修订四（BW-GLOBAL-POOL-20261008-Z4）实质修正：
- _local_pid_state Windows fallback：OpenProcess 句柄成功不等于存活（父进程仍持
  句柄时已退出子进程也能打开）；正确声明 argtypes/restype，用 GetExitCodeProcess
  且 exit code == STILL_ACTIVE 才算 alive，非 STILL_ACTIVE 才 dead，探测失败
  （含 ERROR_ACCESS_DENIED）一律 unknown，绝不臆断 dead。qc 存在/缺席同语义。
- 同真实 workspace 单写入守卫：select_and_claim / reserve / consume（显式 token）
  / claim_due（国内回收与 Luna 两分支）/ adopt-legacy 全部在同一 BEGIN IMMEDIATE
  内核对 normcase(realpath(workspace)) 是否已有别的 task 的在途写入（reserved/
  running/unknown），冲突拒绝且 sent=false；同 task 原消费/续claim 除外。symlink/
  大小写/斜杠别名经 realpath+normcase 归一后同样拒绝。
- adopt-legacy 兼容真实旧原件：Qoder 旧 request（model_requested + runtime∈
  {node,qodercli}，无顶层 task_id/model）与 Zcode 旧 request（carrier='zcode-sdk'
  + selection.providerId/modelId）；task_id 由顶层 → dispatch_plan.task_id → 原
  stage 可靠推导。旧 process.json 原件只有 pid/state（Z 或有 started_at_utc）、
  没有 created：允许受信任宿主传入当前核验的 child_created 独立身份留证（created_
  source 记入 adopt_evidence），生产仍用真实 process_identity 验证 alive+birth
  完全一致；无 fake-alive 开关，绝不改写旧原件。
- adopt-legacy 把确认的实际 worker PID+birth 绑定到 child_pid/child_created（不再
  放 wrapper_pid 冒充未启动 wrapper）：旧 worker 真实退出后 reconcile 立即
  reconciled_exit 释放容量（结束即空名额）。同 PID+birth 即使不同 task 入参也绝不
  双计（pid_already_adopted，源 task 与原 request 精确绑定）；超额如实计入并阻新
  派，不杀/不丢旧 worker。

2026-10-08 修订五（BW-GLOBAL-POOL-20261008-Z6）native 终态收口实质补修：
- _native_receipt_evidence：sha256 必须是严格 64 位 ASCII 十六进制（统一小写落库）；
  content 与 sha256 同时给出必须核对一致；content 为 string 时按 UTF-8 原文字节
  哈希（绝不加 JSON 引号），对象按明确 canonical JSON 口径。回执元信息仍只是受
  信任宿主边界内的可追溯证据，绝不宣称独立服务器验证。
- settle_native：全部身份/回执校验（票据 token 绑定、国内 attempt 禁入、
  launch_unknown、agent_id、scope、receipt）先于幂等分支——错误 agent/scope/
  缺回执/不同终态的重复请求不再被伪称幂等成功；合法重复（同终态+同原回执哈希）
  幂等返回且不覆盖旧证据、不碰新 attempt。
- settle_native：terminal 与 success 不得矛盾（finished=成功、native_failed=失败）；
  显式 success 只在与 terminal 一致时接受。

2026-10-08 修订（BW-GLOBAL-POOL-20261008-Z1）统一的关键口径：
- 所有公开生产路径（reserve / select-and-claim / entry-auto consume）执行同一套
  主力优先/1:1/溢出/Luna 条件；不存在任何“测试播种”后门参数或环境变量。
- consume 对显式 claim token 是同一事务内的 CAS（reserved→running）：重复/并发消费
  只有一个 allowed，绝不允许一槽双 Popen；token 必须与原 task/stage/chat/promptSHA/
  真实 workspace/runtime/model 完全一致，任一漂移即拒。
- auto（entry-auto）路径与显式 token 路径同样在 Popen 前记录 wrapper PID 与真实
  创建身份；bind_child 单次绑定当前 owner，绝不覆盖已绑定的活 child。
- finish 增加真实终态核验：绑定过的 child 仍活（可注入 prober 回读）必须拒绝；只有
  未绑定 child（确认未启动/取消）或 child 已真实退出才释放本 attempt；重复释放幂等。
- reconcile 先判已绑定 child 的终态，再看 wrapper：wrapper 活但 child 已死 → 释放；
  真实 Windows 上死 PID 的 created=None 属正常，不要求死人可回读创建时刻才释放；
  child 存活未知/出生漂移（PID 复用）保守 unknown 不抢占。每次新 claim 前做一次
  安全的 pre-claim 对账，只对有真实 PID 记录且已验证死亡的 attempt 释放。
- ask_record 必须带非空真实询问回执标识（ask_message_id）与完整原 scope（task/stage/
  chat/workspace/promptSHA），且六个国内名额确实全满才记录；等待固定 300 秒，任何
  更短的 deadline_seconds 直接拒绝（更长的也按固定 300 落库）。
- claim_due 在同一事务内重查三池并原子裁决：国内已恢复空位 → 原子预留国内 claim 并
  取消同 task 待援票据（不再出现“只 cancel 不预留”的丢槽竞态）；六仍满、到期、无人
  回复（或用户明确 reply luna）→ claim Luna（不同 task 无全局数量 cap）。票据/重启/
  重复 callback/回复全部幂等，不重置 deadline，不覆盖已 claimed/launch_unknown；
  同 task native 启动后不能再回到国内启动。原生工具由宿主调用，Python 绝不冒称已 spawn。

其它既有口径保持：
- 容量并发 ≠ 额度冷却：本模块只管“同时几个真实执行器在跑”，额度门禁仍在 quota_control。
- 原子性：路由选择/claim/任务防重全部在同一 `BEGIN IMMEDIATE` 事务里完成，跨进程/跨
  会话/跨 cwd 一致；默认 store 为 `~/.brain-worker/dispatch-pool.sqlite3`，环境变量
  `BRAIN_WORKER_DISPATCH_STORE` 覆盖，测试必须传显式临时 store。
- 子进程真实结束立即释放容量名额（不等报告绑定/业务验收）；后续报告解析失败仍释放；
  已启动但存活未知不得假定 start_failed 释放。PID 复用必须绑定创建时刻。
- 容量只看本池在途 attempt 的真实终态与 PID 存活，不依赖 execution_control 快照。

CLI（宿主/主脑可真实操作，全部输出 JSON；不提供任何伪造终态的 flag）：
  status            查看各池在途/容量/空闲、国内合计、Luna 票据
  reserve           为确切 (runtime, model) 预留（统一策略：Flash 需主力全满；Luna 拒绝）
  select-and-claim  无 claim 时的原子路由选择；当前入口非被选中组合 → routing_required
  validate          校验一个 claim token 是否与 task/stage/chat/runtime/model/workspace/prompt 精确一致
  bind-child        记录真实子进程 PID 与创建时刻身份（单次，不覆盖）
  finish            终态释放容量（finished/start_failed/cancelled；带真实终态核验）
  reconcile         按 PID 存活/创建身份对账，安全回收已死 attempt，不误抢活/未知
  ask-record        记录一次真实的“问用户（外部 agent 或 Luna）”票据，固定 300 秒
  reply             记录用户回复选择（不再把等待超时当默认无限授权；reply luna = 已授权接续）
  claim-due         到期裁决：国内空位原子回收 / 六满且无人回应才 claim Luna
  mark-launch-unknown  原生调用非幂等窗口标记（只在 claimed 票据上，绝不自动重启）
  record-agent-id   记录宿主真实原生调用后的 agentID
  settle-native     受信任宿主原生终态结算（绑定 claimed 票据/agent_id/scope +
                     原始原生回执 source_tool/receipt_ref/sha256；单事务释放）
  cancel-pending    国内名额释放时原子取消同 task 待援票据
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
# 只读复用 quota_control 的只读 PID 存活探针（Windows 走 ctypes，绝不 os.kill）。
# 隔离修订目录没有 quota_control 时退化为本地只读探针，行为口径不变。
try:
    import quota_control as qc  # noqa: E402
except ImportError:  # isolated revision tree / standalone tests
    qc = None

# ------------------------------------------------------------------ 池定义
MAIN_FORCE_POOLS = (('zcode', 'GLM-5.3'), ('qoder', 'Qwen3.8-Max'))
MAIN_FORCE_CAPACITY = 2
OVERFLOW_POOLS = (('qoder', 'Qwen3.8-Flash'),)
OVERFLOW_CAPACITY = 2
LUNA_RUNTIME = 'luna'
LUNA_MODEL = 'native'
LUNA_ASK_TIMEOUT_SECONDS = 300
DOMESTIC_TOTAL_CAPACITY = (MAIN_FORCE_CAPACITY * len(MAIN_FORCE_POOLS)
                           + OVERFLOW_CAPACITY * len(OVERFLOW_POOLS))

# pool_key -> capacity（None 表示无上限，仅 Luna）。
CAPACITY = {
    'zcode:GLM-5.3': MAIN_FORCE_CAPACITY,
    'qoder:Qwen3.8-Max': MAIN_FORCE_CAPACITY,
    'qoder:Qwen3.8-Flash': OVERFLOW_CAPACITY,
    'luna:native': None,
}
MAIN_FORCE_KEYS = tuple(f'{r}:{m}' for r, m in MAIN_FORCE_POOLS)
OVERFLOW_KEYS = tuple(f'{r}:{m}' for r, m in OVERFLOW_POOLS)
LUNA_KEY = f'{LUNA_RUNTIME}:{LUNA_MODEL}'

# attempt 状态机：在途（占容量）与已释放（不占容量）。
ACTIVE_STATES = ('reserved', 'running', 'unknown')
RELEASED_STATES = ('finished', 'start_failed', 'cancelled', 'reconciled_exit')

_SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts(
  token TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  stage TEXT,
  chat_id TEXT,
  prompt_sha256 TEXT,
  workspace TEXT,
  runtime TEXT NOT NULL,
  model TEXT NOT NULL,
  pool_key TEXT NOT NULL,
  state TEXT NOT NULL,
  wrapper_pid INTEGER,
  wrapper_created TEXT,
  child_pid INTEGER,
  child_created TEXT,
  reserved_at_utc TEXT,
  started_at_utc TEXT,
  ended_at_utc TEXT,
  terminal TEXT,
  success INTEGER,
  origin TEXT,
  adopt_evidence TEXT);
CREATE TABLE IF NOT EXISTS rotation(
  pool_group TEXT PRIMARY KEY,
  committed_zcode INTEGER NOT NULL DEFAULT 0,
  committed_qoder INTEGER NOT NULL DEFAULT 0,
  updated_at_utc TEXT);
CREATE TABLE IF NOT EXISTS luna_tickets(
  task_id TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  scope TEXT,
  ask_message_id TEXT,
  asked_at_utc TEXT,
  deadline_utc TEXT,
  reply_choice TEXT,
  reply_note TEXT,
  replied_at_utc TEXT,
  claimed_token TEXT,
  launch_state TEXT,
  agent_id TEXT,
  updated_at_utc TEXT);
"""


# ------------------------------------------------------------------ 基础工具
def pool_key(runtime, model) -> str:
    return f'{runtime}:{model}'


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt) -> str:
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    return dt.astimezone(timezone.utc).isoformat()


def _parse(ts):
    if not ts:
        return None
    return datetime.fromisoformat(ts)


def default_store_path() -> Path:
    override = os.environ.get('BRAIN_WORKER_DISPATCH_STORE')
    if override:
        return Path(override)
    return Path.home() / '.brain-worker' / 'dispatch-pool.sqlite3'


# 首次并发初始化：多个 OS 进程同时 spawn 打开同一个（可能全新的）SQLite 库时，
# 切换到 WAL / 建表需要短暂排他锁，`PRAGMA journal_mode=WAL` 在并发下可能直接抛
# “database is locked”。生产初始化必须正确支持这种并发：对 busy/locked 做有界等待 +
# 重试，并在每次失败时可靠关闭那条尚未初始化完成的连接，绝不泄漏句柄。只吞
# busy/locked，其它 OperationalError 原样抛出（不掩盖真实缺陷，也不预建测试库）。
_CONNECT_BUSY_TIMEOUT_MS = 30000
_CONNECT_INIT_MAX_RETRY = 50
_CONNECT_INIT_BACKOFF_SECONDS = 0.02


def _is_busy_locked(exc) -> bool:
    msg = str(exc).lower()
    return 'database is locked' in msg or 'database is busy' in msg or 'database table is locked' in msg


def _init_connection(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=_CONNECT_BUSY_TIMEOUT_MS / 1000.0,
                           isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f'PRAGMA busy_timeout={_CONNECT_BUSY_TIMEOUT_MS}')
        conn.execute('PRAGMA journal_mode=WAL')
        conn.executescript(_SCHEMA)
        _migrate(conn)
    except BaseException:
        # 初始化中途失败：可靠关闭这条连接（含 busy/locked 与其它异常），不泄漏句柄。
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - close 失败也不能掩盖原异常
            pass
        raise
    return conn


def connect(store_path=None) -> sqlite3.Connection:
    path = Path(store_path) if store_path else default_store_path()
    if str(path) != ':memory:':
        path.parent.mkdir(parents=True, exist_ok=True)
    attempt = 0
    while True:
        try:
            return _init_connection(path)
        except sqlite3.OperationalError as exc:
            # 只对首次并发初始化的 busy/locked 做有界等待 + 重试；其它错误立即抛出。
            if not _is_busy_locked(exc) or attempt >= _CONNECT_INIT_MAX_RETRY:
                raise
            attempt += 1
            time.sleep(_CONNECT_INIT_BACKOFF_SECONDS * attempt)


def _migrate(conn):
    """老库升级：luna_tickets 补 agent_id 列、attempts 补 adopt_evidence 列（无破坏性）。"""
    cols = {r['name'] for r in conn.execute('PRAGMA table_info(luna_tickets)')}
    if cols and 'agent_id' not in cols:
        try:
            conn.execute('ALTER TABLE luna_tickets ADD COLUMN agent_id TEXT')
        except sqlite3.OperationalError:
            pass
    acols = {r['name'] for r in conn.execute('PRAGMA table_info(attempts)')}
    if acols and 'adopt_evidence' not in acols:
        try:
            conn.execute('ALTER TABLE attempts ADD COLUMN adopt_evidence TEXT')
        except sqlite3.OperationalError:
            pass


def capacity_for(pk) -> int | None:
    """已知池容量；Luna 无上限（None）；未列入分配决策的组合容量为 0（拒绝）。"""
    return CAPACITY.get(pk, 0)


def is_known_pool(pk) -> bool:
    return pk in CAPACITY


# ------------------------------------------------------------------ PID 身份
def _windows_process_created(pid):
    """Windows 只读获取进程创建时刻（FILETIME，64 位整数字符串），用于 PID 复用绑定。
    查询失败一律返回 None（调用方保守处理），绝不 os.kill、绝不终止任何进程。
    真实 Windows 上已退出的 PID 查不到创建时刻（created=None）属正常现象。"""
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

        class FILETIME(ctypes.Structure):
            _fields_ = [('lo', wintypes.DWORD), ('hi', wintypes.DWORD)]

        open_process = kernel32.OpenProcess
        open_process.restype = ctypes.c_void_p
        open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        handle = open_process(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None
        try:
            get_times = kernel32.GetProcessTimes
            get_times.argtypes = [ctypes.c_void_p, ctypes.POINTER(FILETIME),
                                  ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
                                  ctypes.POINTER(FILETIME)]
            creation, exit_t, kernel_t, user_t = FILETIME(), FILETIME(), FILETIME(), FILETIME()
            if not get_times(ctypes.c_void_p(handle), ctypes.byref(creation),
                             ctypes.byref(exit_t), ctypes.byref(kernel_t),
                             ctypes.byref(user_t)):
                return None
            return str((creation.hi << 32) | creation.lo)
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    except (OSError, AttributeError, ImportError, ValueError):
        return None


def _linux_process_created(pid):
    """Linux 只读创建身份：/proc/PID/stat 的 starttime（第 22 字段）+ boot_id，
    组成 'boot_id:starttime' 稳定区分 PID 复用。/proc 不可读/字段异常一律 None
    （调用方按 unknown 处理，绝不臆造身份）。"""
    try:
        stat = Path(f'/proc/{int(pid)}/stat').read_text(encoding='utf-8')
        # comm 可能含空格/括号：以最后一个 ')' 切分，其后第 20 个字段即第 22 列 starttime。
        fields = stat[stat.rindex(')') + 2:].split()
        starttime = fields[19]
        boot = ''
        try:
            boot = Path('/proc/sys/kernel/random/boot_id').read_text(
                encoding='utf-8').strip()
        except OSError:
            pass
        return f'{boot}:{starttime}' if boot else str(starttime)
    except (OSError, ValueError, IndexError):
        return None


def _local_pid_state(pid, platform=None):
    """quota_control 缺席时的本地只读存活探测（Windows GetExitCodeProcess / POSIX 信号 0）。
    Z4：Windows 上父进程仍持 process 句柄时，OpenProcess 对已退出的子进程同样能成功，
    单凭句柄会把死进程判成 alive；必须声明 argtypes/restype 后用 GetExitCodeProcess，
    exit code == STILL_ACTIVE(259) 才算 alive，非 STILL_ACTIVE 才是 dead；探测失败
    （含 ERROR_ACCESS_DENIED）一律 unknown，绝不臆断 dead。句柄在 finally 中关闭。"""
    plat = str(platform or sys.platform).lower()
    if plat.startswith('win') or plat == 'nt':
        try:
            import ctypes
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 0x103
            ERROR_INVALID_PARAMETER = 87
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            open_process = kernel32.OpenProcess
            open_process.restype = wintypes.HANDLE
            open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            get_exit_code = kernel32.GetExitCodeProcess
            get_exit_code.restype = wintypes.BOOL
            get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            close_handle = kernel32.CloseHandle
            close_handle.restype = wintypes.BOOL
            close_handle.argtypes = [wintypes.HANDLE]
            handle = open_process(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                # 87 = 该 PID 不存在（真 dead）；拒绝访问等其它错误只能 unknown。
                if ctypes.get_last_error() == ERROR_INVALID_PARAMETER:
                    return 'dead'
                return 'unknown'
            try:
                code = wintypes.DWORD()
                if not get_exit_code(handle, ctypes.byref(code)):
                    return 'unknown'
                return 'alive' if code.value == STILL_ACTIVE else 'dead'
            finally:
                close_handle(handle)
        except (OSError, AttributeError, ImportError, ValueError):
            return 'unknown'
    try:
        os.kill(int(pid), 0)
        return 'alive'
    except ProcessLookupError:
        return 'dead'
    except (PermissionError, OSError):
        return 'unknown'


def process_identity(pid, platform=None):
    """只读进程身份：{pid, state: alive/dead/unknown, created}. Z3：created 在 Windows
    为 GetProcessTimes 的 FILETIME；在 /proc 可读的 Linux 为 boot_id+starttime。死进程或
    读取失败一律 None（没有身份，绝不臆造）。绝不带副作用（不终止、不信号投递，除 POSIX
    信号 0）。生命周期对账据此区分明确 dead 与 PID 复用（同 PID 活但 birth 不符 ≠ 原
    child 已死）。"""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return {'pid': pid, 'state': 'dead', 'created': None}
    plat = str(platform or sys.platform).lower()
    if qc is not None:
        state = qc._windows_pid_state(pid) if (
            plat.startswith('win') or plat == 'nt') else (
            'alive' if qc._pid_alive(pid, platform) else 'dead')
    else:
        state = _local_pid_state(pid, platform)
    if plat.startswith('win') or plat == 'nt':
        created = _windows_process_created(pid)
    else:
        created = _linux_process_created(pid)
    return {'pid': pid, 'state': state, 'created': created}


def _created_matches(recorded, observed) -> bool:
    """PID 复用防护：任一侧创建时刻未知则不能确认同一进程（保守视为不匹配→不抢占）。"""
    if not recorded or not observed:
        return False
    return str(recorded) == str(observed)


# ------------------------------------------------------------------ 计数/查询
def _count_active(conn, pk) -> int:
    row = conn.execute(
        'SELECT COUNT(*) AS n FROM attempts WHERE pool_key=? AND state IN (%s)'
        % ','.join('?' * len(ACTIVE_STATES)), (pk, *ACTIVE_STATES)).fetchone()
    return int(row['n']) if row else 0


def _active_task(conn, task_id):
    return conn.execute(
        'SELECT * FROM attempts WHERE task_id=? AND state IN (%s) LIMIT 1'
        % ','.join('?' * len(ACTIVE_STATES)), (task_id, *ACTIVE_STATES)).fetchone()


def _norm_workspace(workspace):
    """真实 workspace 归一：normcase(realpath())，symlink/大小写/斜杠别名同目录归一。"""
    if not workspace:
        return workspace
    return os.path.normcase(os.path.realpath(str(workspace)))


def _workspace_conflict(conn, workspace, *, own_task_id=None, own_token=None):
    """Z4 同真实 workspace 单写入守卫：同一事务内核对 normcase(realpath(workspace))
    是否已有别的 task 的在途 attempt（reserved/running/unknown；包含 Luna 行——原生
    Luna 也在写同一真实目录）。同 task（own_task_id）或同 token（own_token）的原
    consume/续claim 除外。返回冲突行或 None；不做容量回收、不动业务占位。"""
    if not workspace:
        return None
    wn = _norm_workspace(workspace)
    for row in conn.execute(
            'SELECT * FROM attempts WHERE workspace=? AND state IN (%s)'
            % ','.join('?' * len(ACTIVE_STATES)), (wn, *ACTIVE_STATES)):
        if own_token is not None and row['token'] == own_token:
            continue
        if own_task_id is not None and row['task_id'] == own_task_id:
            continue
        return row
    return None


def _workspace_busy_reject(task_id, pk, row):
    return _reject('workspace_in_flight', task_id, pk,
                   [f'workspace {row["workspace"]!r} already has an in-flight attempt '
                    f'from a different task (token={row["token"]}, '
                    f'task={row["task_id"]!r}, state={row["state"]}); one real '
                    'workspace never takes two concurrent writers — no capacity is '
                    'reclaimed and no business placeholder is deleted'])


def _domestic_active(conn) -> int:
    keys = MAIN_FORCE_KEYS + OVERFLOW_KEYS
    return sum(_count_active(conn, k) for k in keys)


def _rotation_row(conn):
    row = conn.execute('SELECT * FROM rotation WHERE pool_group=?', ('main',)).fetchone()
    if row is None:
        conn.execute('INSERT INTO rotation(pool_group, committed_zcode, committed_qoder) '
                     "VALUES('main',0,0)")
        row = conn.execute('SELECT * FROM rotation WHERE pool_group=?', ('main',)).fetchone()
    return row


def _bump_rotation(conn, pk, now):
    row = _rotation_row(conn)
    if pk == 'zcode:GLM-5.3':
        conn.execute('UPDATE rotation SET committed_zcode=committed_zcode+1, '
                     'updated_at_utc=? WHERE pool_group=?', (_iso(now), 'main'))
    elif pk == 'qoder:Qwen3.8-Max':
        conn.execute('UPDATE rotation SET committed_qoder=committed_qoder+1, '
                     'updated_at_utc=? WHERE pool_group=?', (_iso(now), 'main'))


def _luna_native_started(conn, task_id) -> bool:
    """同 task 的 Luna native 已启动（票据 claimed 或 launch_unknown）→ 不得再国内启动。"""
    row = conn.execute('SELECT state, launch_state FROM luna_tickets WHERE task_id=?',
                       (task_id,)).fetchone()
    if row is None:
        return False
    return row['state'] == 'claimed' or row['launch_state'] == 'launch_unknown'


def _ticket_answered_external(conn, task_id):
    """同 task 待援票据已被用户明确回复 external_agent/cancel → 不得再偷偷国内启动。"""
    row = conn.execute('SELECT state, reply_choice FROM luna_tickets WHERE task_id=?',
                       (task_id,)).fetchone()
    if row is None:
        return None
    if row['state'] == 'replied' and row['reply_choice'] in ('external_agent', 'cancel'):
        return row['reply_choice']
    return None


def _cancel_pending_ticket(conn, task_id, now, reason='domestic_slot_freed'):
    """同一事务内取消同 task 的 pending 待援票据（国内已拿到名额，绝不双派）。"""
    cur = conn.execute(
        "UPDATE luna_tickets SET state='cancelled', reply_note=?, updated_at_utc=? "
        "WHERE task_id=? AND state='pending'", (reason, _iso(now), task_id))
    return cur.rowcount


# ------------------------------------------------------------------ status
def status(store_path=None, now=None) -> dict:
    now = now or utcnow()
    with closing(connect(store_path)) as conn:
        pools = {}
        for pk, cap in CAPACITY.items():
            active = _count_active(conn, pk)
            pools[pk] = {'capacity': cap, 'active': active,
                         'free': (None if cap is None else max(0, cap - active))}
        rot = _rotation_row(conn)
        domestic_active = _domestic_active(conn)
        tickets = [dict(r) for r in conn.execute('SELECT * FROM luna_tickets').fetchall()]
    return {'store': str(store_path or default_store_path()), 'now_utc': _iso(now),
            'pools': pools,
            'rotation': {'committed_zcode': rot['committed_zcode'],
                         'committed_qoder': rot['committed_qoder']},
            'domestic': {'capacity': DOMESTIC_TOTAL_CAPACITY,
                         'active': domestic_active,
                         'free': DOMESTIC_TOTAL_CAPACITY - domestic_active,
                         'full': domestic_active >= DOMESTIC_TOTAL_CAPACITY},
            'luna_tickets': tickets}


# ------------------------------------------------------------------ 统一国内策略
def _select_main_force(conn, requested_pk, now):
    """主力 1:1 目标选择：committed 少者优先；平票时优先请求的组合（best-effort，
    非严格均衡，见 references/global-dispatch.md）。返回 (target_pk, reason)。"""
    rot = _rotation_row(conn)
    committed = {'zcode:GLM-5.3': rot['committed_zcode'],
                 'qoder:Qwen3.8-Max': rot['committed_qoder']}
    order = list(MAIN_FORCE_KEYS)
    if requested_pk in committed:
        order.sort(key=lambda k: (committed[k], 0 if k == requested_pk else 1))
    else:
        order.sort(key=lambda k: committed[k])
    for pk in order:
        cap = capacity_for(pk)
        if cap is not None and _count_active(conn, pk) < cap:
            return pk, 'main_force_selected'
    return None, 'main_force_full'


def _domestic_policy(conn, requested_pk, now):
    """reserve 与 select-and-claim 共用的统一国内裁决（语义集中，无测试后门）：
    返回 ('claim', pk) / ('routing', target_pk) / ('full', None)。
    - Luna 永不在此自动放行（只能经 claim-due 票据裁决）；
    - Flash：任一主力仍有空位 → routing 到该主力（主力有空不用 Flash）；
    - 主力（Z2 修订）：一律先按持久 committed 计数选主力（1:1），平票才优先请求组合，
      本池有空也不绕过轮转；选中组合≠请求组合 → routing；两主力都满 → Flash 有空也只
      能 routing 到 Flash（绝不替模型偷换组合落库），Flash 也满才 full。"""
    if requested_pk == LUNA_KEY:
        return 'reject_luna', None
    if requested_pk in MAIN_FORCE_KEYS:
        # 修订 Z2：任意新主力分配一律先按持久 committed 计数选主力（跨聊天 1:1），
        # 平票才优先请求的组合；本池有空也不再绕过轮转直接 claim。
        target, _ = _select_main_force(conn, requested_pk, now)
        if target is not None:
            if target == requested_pk:
                return 'claim', requested_pk
            return 'routing', target
        ovf = OVERFLOW_KEYS[0]
        if capacity_for(ovf) is not None and _count_active(conn, ovf) < capacity_for(ovf):
            # 修订 Z2：两主力满时请求 Z/Max 只能 routing 到 Flash 入口，绝不替模型
            # 偷换组合落库（attempts.pool_key 必须等于 runtime+':'+model）。
            return 'routing', ovf
        return 'full', None
    if requested_pk in OVERFLOW_KEYS:
        target, _ = _select_main_force(conn, requested_pk, now)
        if target is not None:
            return 'routing', target
        cap = capacity_for(requested_pk)
        if cap is not None and _count_active(conn, requested_pk) < cap:
            return 'claim', requested_pk
        return 'full', None
    return 'unknown_pool', None


def _preclaim_reconcile(conn, now):
    """每次新 claim 前的安全对账：只处理有真实 PID 记录的国内 attempt，且只在
    已绑定 child 经只读探针确认已死亡时释放（自动复用已结束容量）。任何探测失败、
    无 PID 记录、存活/未知一律不动（交给显式 reconcile 保守处理）。"""
    rows = conn.execute(
        'SELECT * FROM attempts WHERE state IN (%s) AND pool_key != ? '
        'AND child_pid IS NOT NULL' % ','.join('?' * len(ACTIVE_STATES)),
        (*ACTIVE_STATES, LUNA_KEY)).fetchall()
    released = []
    for row in rows:
        try:
            ident = process_identity(int(row['child_pid']))
        except Exception:
            continue
        if ident.get('state') == 'dead':
            conn.execute("UPDATE attempts SET state='reconciled_exit', "
                         "terminal='reconciled_exit', ended_at_utc=? WHERE token=? "
                         "AND state IN (%s)" % ','.join('?' * len(ACTIVE_STATES)),
                         (_iso(now), row['token'], *ACTIVE_STATES))
            released.append(row['token'])
    return released


def _insert_claim(conn, *, task_id, stage, chat_id, prompt_sha256, workspace,
                  runtime, model, pk, token, now, origin,
                  wrapper_pid=None, wrapper_created=None) -> str:
    """插入一个 claim。给了 wrapper_pid（entry 在 Popen 前的真实调用者身份）则直接
    running 并记录 wrapper 创建身份；否则 reserved（等待 consume CAS）。"""
    tok = token or uuid.uuid4().hex
    if wrapper_pid is not None and wrapper_created is None:
        wrapper_created = process_identity(int(wrapper_pid)).get('created')
    if wrapper_pid is not None:
        conn.execute(
            'INSERT INTO attempts(token, task_id, stage, chat_id, prompt_sha256, workspace, '
            'runtime, model, pool_key, state, wrapper_pid, wrapper_created, '
            'started_at_utc, reserved_at_utc, origin) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (tok, task_id, stage, chat_id, prompt_sha256,
             _norm_workspace(workspace),
             runtime, model, pk, 'running', int(wrapper_pid), wrapper_created,
             _iso(now), _iso(now), origin))
    else:
        conn.execute(
            'INSERT INTO attempts(token, task_id, stage, chat_id, prompt_sha256, workspace, '
            'runtime, model, pool_key, state, reserved_at_utc, origin) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (tok, task_id, stage, chat_id, prompt_sha256,
             _norm_workspace(workspace),
             runtime, model, pk, 'reserved', _iso(now), origin))
    return tok


# ------------------------------------------------------------------ 路由选择
def select_and_claim(store_path=None, *, task_id, runtime, model, workspace,
                     prompt_sha256, stage=None, chat_id=None, token=None,
                     now=None, origin='select-and-claim',
                     wrapper_pid=None, wrapper_created=None,
                     _preclaim=True) -> dict:
    """无显式 claim 时的原子路由选择（与 reserve 共用 _domestic_policy，语义集中）：
    - 请求 Luna → 拒绝（Luna 只经 claim-due 竞争裁决，绝不自动派生）；
    - 请求溢出 Flash：主力仍有空位 → routing_required 到该主力；
    - 请求主力：按 committed 轮换；当前入口非目标 → routing_required（不提交错模型）；
      两主力都满 → 溢出 Flash（有空位则直接 claim）否则六名额全满拒绝。
    防重：同 task 已有在途 attempt，或同 task Luna native 已启动 → 一律拒绝。
    auto consume 场景传 wrapper_pid（默认由 consume_for_entry 填 os.getpid()），
    Popen 前即记录 wrapper 真实创建身份。"""
    now = now or utcnow()
    pk = pool_key(runtime, model)
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            if _preclaim:
                _preclaim_reconcile(conn, now)
            # Z3：先判 native 已启动再判普通在途——同 task 的 Luna attempt 本身就是在途
            # 行，先给准确的 task_already_on_luna（仍一律拒绝，绝不因换理由放松）。
            if _luna_native_started(conn, task_id):
                conn.execute('ROLLBACK')
                return _reject('task_already_on_luna', task_id, pk,
                               ['a native Luna launch for this task was already claimed '
                                '(or is launch_unknown); the task can never fall back to '
                                'a domestic start afterwards'])
            dup = _active_task(conn, task_id)
            if dup is not None:
                conn.execute('ROLLBACK')
                return _reject('duplicate_task_in_flight', task_id, pk,
                               [f'task {task_id!r} already has an in-flight attempt '
                                f'(token={dup["token"]}, pool={dup["pool_key"]}, '
                                f'state={dup["state"]}); a task can never double-start'])
            answered = _ticket_answered_external(conn, task_id)
            if answered is not None:
                conn.execute('ROLLBACK')
                return _reject('task_answered_external', task_id, pk,
                               [f'the user explicitly replied {answered!r} to the ask '
                                'ticket; a domestic start for this task is not allowed'])
            # Z4：真实 workspace 单写入守卫先于路由/容量裁决——别的 task 已在同目录
            # 在途写入则直接拒绝（sent=false），不得改到 Luna/Flash 绕过。
            busy = _workspace_conflict(conn, workspace, own_task_id=task_id)
            if busy is not None:
                conn.execute('ROLLBACK')
                return _workspace_busy_reject(task_id, pk, busy)
            verdict, target = _domestic_policy(conn, pk, now)
            domestic_full = _domestic_active(conn) >= DOMESTIC_TOTAL_CAPACITY
            if verdict == 'unknown_pool':
                conn.execute('ROLLBACK')
                return _reject('unknown_pool', task_id, pk,
                               [f'runtime/model {runtime!r}/{model!r} is not part of the '
                                f'sanctioned allocation (main force zcode:GLM-5.3 / '
                                f'qoder:Qwen3.8-Max, overflow qoder:Qwen3.8-Flash, '
                                f'rescue luna:native); refusing to dispatch an '
                                f'unsanctioned combo'])
            if verdict == 'reject_luna':
                conn.execute('ROLLBACK')
                return _reject('luna_requires_ticket', task_id, pk,
                               ['Luna is rescue-only and is never auto-selected here; it '
                                'may only be claimed via claim-due after a real ask ticket '
                                'expired with all six domestic slots full and no reply'])
            if verdict == 'routing':
                tr, tm = target.split(':', 1)
                why = ('main force still has a free slot; overflow Qwen3.8-Flash must '
                       f'not be used while {target} has capacity'
                       if pk in OVERFLOW_KEYS else
                       (f'both main pools are full; continue via the overflow entry '
                        f'{target} instead of submitting {pk} under a Flash pool_key'
                        if target in OVERFLOW_KEYS else
                        f'1:1 rotation selected {target} for this task; the current entry '
                        f'({pk}) is not the selected combo — continue via the selected '
                        'entry instead of submitting the wrong model'))
                conn.execute('ROLLBACK')
                return _routing(task_id, pk, target, [why], domestic_full=domestic_full)
            if verdict == 'full':
                conn.execute('ROLLBACK')
                return _reject('capacity_full', task_id, pk,
                               ['all six domestic slots are occupied; ask the user '
                                '(external agent or Luna) and record an ask ticket'],
                               domestic_full=True)
            claim_pk = target
            tok = _insert_claim(conn, task_id=task_id, stage=stage, chat_id=chat_id,
                                prompt_sha256=prompt_sha256, workspace=workspace,
                                runtime=runtime, model=model, pk=claim_pk,
                                token=token, now=now, origin=origin,
                                wrapper_pid=wrapper_pid,
                                wrapper_created=wrapper_created)
            _bump_rotation(conn, claim_pk, now)
            # Z2：同 task 国内救回时同一事务取消旧 pending 待援票，防止之后被 claim_due
            # 再次 Luna（双派）。
            _cancel_pending_ticket(conn, task_id, now)
            conn.execute('COMMIT')
            return {'allowed': True, 'claimed': True, 'token': tok, 'pool_key': claim_pk,
                    'routing_required': False, 'reasons': [], 'task_id': task_id,
                    'selected': {'runtime': runtime, 'model': model},
                    'domestic_active': _domestic_active(conn)}
        except Exception:
            conn.execute('ROLLBACK')
            raise


def reserve(store_path=None, *, task_id, runtime, model, workspace, prompt_sha256,
            stage=None, chat_id=None, token=None, now=None, origin='reserve',
            _preclaim=True) -> dict:
    """为确切 (runtime, model) 预留一个 claim。修订后与 select-and-claim 共用统一国内
    策略（_domestic_policy）：空库/主力有空时 reserve Flash 一律 routing_required 到
    主力（不保留任何绕过主力顺序的口子）；reserve Luna 一律拒绝（只能经 ask 票据 +
    claim-due）。主脑/测试据此拿到确定 token，交给对应入口 --dispatch-claim 精确消费。"""
    now = now or utcnow()
    pk = pool_key(runtime, model)
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            if _preclaim:
                _preclaim_reconcile(conn, now)
            # Z3：与 select_and_claim 相同顺序——native 已启动的准确理由优先于普通在途。
            if _luna_native_started(conn, task_id):
                conn.execute('ROLLBACK')
                return _reject('task_already_on_luna', task_id, pk,
                               ['a native Luna launch for this task was already claimed '
                                '(or is launch_unknown); no domestic start afterwards'])
            dup = _active_task(conn, task_id)
            if dup is not None:
                conn.execute('ROLLBACK')
                return _reject('duplicate_task_in_flight', task_id, pk,
                               [f'task {task_id!r} already has an in-flight attempt '
                                f'(token={dup["token"]}, state={dup["state"]})'])
            answered = _ticket_answered_external(conn, task_id)
            if answered is not None:
                conn.execute('ROLLBACK')
                return _reject('task_answered_external', task_id, pk,
                               [f'the user explicitly replied {answered!r} to the ask '
                                'ticket; a domestic start for this task is not allowed'])
            # Z4：真实 workspace 单写入守卫先于路由/容量裁决（与 select_and_claim 同）。
            busy = _workspace_conflict(conn, workspace, own_task_id=task_id)
            if busy is not None:
                conn.execute('ROLLBACK')
                return _workspace_busy_reject(task_id, pk, busy)
            verdict, target = _domestic_policy(conn, pk, now)
            if verdict == 'unknown_pool':
                conn.execute('ROLLBACK')
                return _reject('unknown_pool', task_id, pk,
                               [f'{runtime!r}/{model!r} is not a sanctioned pool'])
            if verdict == 'reject_luna':
                conn.execute('ROLLBACK')
                return _reject('luna_requires_ticket', task_id, pk,
                               ['Luna is rescue-only; reserve never hands out a Luna '
                                'slot — only claim-due after a real, expired, unreplied '
                                'ask ticket with all six domestic slots full'])
            if verdict == 'routing':
                conn.execute('ROLLBACK')
                return _routing(task_id, pk, target,
                                [f'unified domestic policy: {pk} may not be reserved '
                                 f'while {target} still has capacity; reserve {target} '
                                 'instead'],
                                domestic_full=_domestic_active(conn) >= DOMESTIC_TOTAL_CAPACITY)
            if verdict == 'full':
                conn.execute('ROLLBACK')
                return _reject('capacity_full', task_id, pk,
                               [f'{pk} and every other domestic pool is at capacity; '
                                'all six domestic slots are occupied'],
                               domestic_full=True)
            tok = _insert_claim(conn, task_id=task_id, stage=stage, chat_id=chat_id,
                                prompt_sha256=prompt_sha256, workspace=workspace,
                                runtime=runtime, model=model, pk=pk, token=token,
                                now=now, origin=origin)
            if pk in MAIN_FORCE_KEYS:
                _bump_rotation(conn, pk, now)
            _cancel_pending_ticket(conn, task_id, now)
            conn.execute('COMMIT')
            return {'allowed': True, 'reserved': True, 'token': tok, 'pool_key': pk,
                    'routing_required': False, 'reasons': [], 'task_id': task_id,
                    'selected': {'runtime': runtime, 'model': model}}
        except Exception:
            conn.execute('ROLLBACK')
            raise


def _reject(reason, task_id, pk, reasons, domestic_full=False):
    return {'allowed': False, 'sent': False, 'claimed': False, 'token': None,
            'routing_required': reason == 'routing_required',
            'reason': reason, 'pool_key': pk, 'task_id': task_id,
            'reasons': reasons, 'domestic_full': domestic_full}


def _routing(task_id, requested_pk, target_pk, reasons, domestic_full=False):
    tr, tm = target_pk.split(':', 1)
    return {'allowed': False, 'sent': False, 'claimed': False, 'token': None,
            'routing_required': True, 'reason': 'routing_required',
            'pool_key': requested_pk, 'task_id': task_id, 'reasons': reasons,
            'selected': {'pool_key': target_pk, 'runtime': tr, 'model': tm},
            'domestic_full': domestic_full}


# ------------------------------------------------------------------ claim 消费
def _validate_attempt_row(row, *, task_id=None, runtime=None, model=None, workspace=None,
                          prompt_sha256=None, stage=None, chat_id=None):
    """对已取出的 attempt 行做精确一致性校验（供只读 validate 与事务内 CAS 复用）。
    显式 claim token 必须与原 task/stage/chat/promptSHA/真实 workspace/runtime/model
    全部一致；任一漂移即拒。"""
    reasons = []
    if task_id is not None and row['task_id'] != task_id:
        reasons.append(f'task_id drift: claim={row["task_id"]!r} entry={task_id!r}')
    if runtime is not None and row['runtime'] != runtime:
        reasons.append(f'runtime drift: claim={row["runtime"]!r} entry={runtime!r}')
    if model is not None and row['model'] != model:
        reasons.append(f'model drift: claim={row["model"]!r} entry={model!r}')
    if stage is not None and row['stage'] != stage:
        reasons.append(f'stage drift: claim={row["stage"]!r} entry={stage!r}')
    if chat_id is not None and row['chat_id'] != chat_id:
        reasons.append(f'chat_id drift: claim={row["chat_id"]!r} entry={chat_id!r}')
    if workspace is not None:
        wn = os.path.normcase(os.path.realpath(str(workspace)))
        if row['workspace'] != wn:
            reasons.append(f'workspace drift: claim={row["workspace"]!r} entry={wn!r}')
    if prompt_sha256 is not None and row['prompt_sha256'] != prompt_sha256:
        reasons.append('prompt_sha256 drift: claim does not match the entry prompt bytes')
    if row['state'] not in ACTIVE_STATES:
        reasons.append(f'claim already terminal (state={row["state"]}); cannot consume')
    return {'ok': not reasons, 'reasons': reasons, 'drift': bool(reasons)}


def validate_claim(store_path, token, *, task_id=None, runtime=None, model=None,
                   workspace=None, prompt_sha256=None, stage=None, chat_id=None) -> dict:
    """校验 claim token 与 task/stage/chat/runtime/model/workspace/prompt 精确一致；
    任一漂移即拒。只读，不改状态。"""
    with closing(connect(store_path)) as conn:
        row = conn.execute('SELECT * FROM attempts WHERE token=?', (token,)).fetchone()
    if row is None:
        return {'ok': False, 'reasons': [f'no such claim token: {token}'], 'drift': True,
                'attempt': None}
    attempt = dict(row)
    check = _validate_attempt_row(row, task_id=task_id, runtime=runtime, model=model,
                                  workspace=workspace, prompt_sha256=prompt_sha256,
                                  stage=stage, chat_id=chat_id)
    return {'ok': check['ok'], 'reasons': check['reasons'], 'drift': check['drift'],
            'attempt': attempt}


def consume_for_entry(store_path=None, *, task_id, runtime, model, workspace,
                      prompt_sha256, stage=None, chat_id=None, claim_token=None,
                      wrapper_pid=None, wrapper_created=None, now=None) -> dict:
    """入口在 Popen 前的唯一容量门：
    - 给了 --dispatch-claim → 同一 `BEGIN IMMEDIATE` 事务内先精确校验（含 stage/chat）
      再 CAS reserved→running；重复/并发消费只有一个 allowed（一槽绝不双 Popen）；
    - 没给 → select_and_claim 原子路由；非被选中组合 → routing_required（sent=false）。
    auto 路径同样在 Popen 前记录 wrapper PID 与真实创建身份（缺省为当前进程）。
    放行即已持有该池一个在途名额，返回 token 供 finish/bind_child 绑定生命周期。"""
    now = now or utcnow()
    if claim_token:
        with closing(connect(store_path)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                row = conn.execute('SELECT * FROM attempts WHERE token=?',
                                   (claim_token,)).fetchone()
                if row is None:
                    conn.execute('ROLLBACK')
                    return {'allowed': False, 'sent': False, 'claimed': False,
                            'token': None, 'routing_required': False,
                            'reason': 'claim_invalid', 'pool_key': pool_key(runtime, model),
                            'task_id': task_id,
                            'reasons': [f'no such claim token: {claim_token}'],
                            'drift': True}
                check = _validate_attempt_row(row, task_id=task_id, runtime=runtime,
                                               model=model, workspace=workspace,
                                               prompt_sha256=prompt_sha256,
                                               stage=stage, chat_id=chat_id)
                if not check['ok']:
                    conn.execute('ROLLBACK')
                    return {'allowed': False, 'sent': False, 'claimed': False,
                            'token': None, 'routing_required': False,
                            'reason': 'claim_invalid', 'pool_key': pool_key(runtime, model),
                            'task_id': task_id, 'reasons': check['reasons'],
                            'drift': check['drift']}
                # Z4：同 task 原消费除外；别的 task 已在同真实 workspace 在途写入则拒。
                busy = _workspace_conflict(conn, workspace, own_token=claim_token)
                if busy is not None:
                    conn.execute('ROLLBACK')
                    return _workspace_busy_reject(task_id, pool_key(runtime, model),
                                                  busy)
                if wrapper_pid is None:
                    wrapper_pid = os.getpid()
                if wrapper_created is None:
                    wrapper_created = process_identity(int(wrapper_pid)).get('created')
                cur = conn.execute(
                    "UPDATE attempts SET state='running', started_at_utc=?, "
                    'wrapper_pid=COALESCE(?, wrapper_pid), '
                    'wrapper_created=COALESCE(?, wrapper_created) '
                    "WHERE token=? AND state='reserved'",
                    (_iso(now), int(wrapper_pid), wrapper_created, claim_token))
                if cur.rowcount != 1:
                    conn.execute('ROLLBACK')
                    return {'allowed': False, 'sent': False, 'claimed': False,
                            'token': None, 'routing_required': False,
                            'reason': 'consume_conflict',
                            'pool_key': row['pool_key'], 'task_id': task_id,
                            'reasons': ['this claim was already consumed (state != '
                                        'reserved) by a competing entry; only one '
                                        'consume may win — never double-Popen one slot']}
                conn.execute('COMMIT')
                return {'allowed': True, 'claimed': True, 'token': claim_token,
                        'pool_key': row['pool_key'], 'routing_required': False,
                        'reasons': [], 'task_id': task_id,
                        'selected': {'runtime': runtime, 'model': model}}
            except Exception:
                conn.execute('ROLLBACK')
                raise
    if wrapper_pid is None:
        wrapper_pid = os.getpid()
    return select_and_claim(store_path, task_id=task_id, runtime=runtime, model=model,
                            workspace=workspace, prompt_sha256=prompt_sha256,
                            stage=stage, chat_id=chat_id, now=now, origin='entry-auto',
                            wrapper_pid=wrapper_pid, wrapper_created=wrapper_created)


def mark_running(store_path, token, *, wrapper_pid=None, wrapper_created=None, now=None):
    """CAS reserved→running（同 consume 语义的单入口版本）；已消费/已终态时 settled=False，
    绝不二次放行。"""
    now = now or utcnow()
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            if wrapper_pid is not None and wrapper_created is None:
                wrapper_created = process_identity(int(wrapper_pid)).get('created')
            cur = conn.execute(
                "UPDATE attempts SET state='running', started_at_utc=?, "
                'wrapper_pid=COALESCE(?, wrapper_pid), '
                'wrapper_created=COALESCE(?, wrapper_created) '
                "WHERE token=? AND state='reserved'",
                (_iso(now), wrapper_pid, wrapper_created, token))
            changed = cur.rowcount
            conn.execute('COMMIT')
            return {'settled': bool(changed), 'token': token,
                    'reason': None if changed else 'not_reserved'}
        except Exception:
            conn.execute('ROLLBACK')
            raise


def bind_child(store_path, token, child_pid, *, child_created=None, wrapper_pid=None,
               wrapper_created=None, now=None) -> dict:
    """单次绑定当前 owner 的真实 child PID+创建身份：只在尚无 child 且 attempt 在途时
    绑定；已绑定（尤其还活着）绝不覆盖。Z2 修订：attempt 已记录 wrapper 身份时，只有
    当前真实调用者身份（wrapper_pid/created）与记录一致才允许绑定——别人不能把子进程
    挂到不属于自己的 attempt 上。"""
    now = now or utcnow()
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            if child_created is None:
                child_created = process_identity(int(child_pid)).get('created')
            row = conn.execute('SELECT child_pid, state, wrapper_pid, wrapper_created '
                               'FROM attempts WHERE token=?', (token,)).fetchone()
            if row is not None and row['wrapper_pid'] is not None:
                if wrapper_pid is None or int(row['wrapper_pid']) != int(wrapper_pid):
                    conn.execute('ROLLBACK')
                    return {'bound': False, 'token': token,
                            'reason': 'owner_mismatch',
                            'reasons': ['attempt has a recorded wrapper (owner) pid '
                                        f'{row["wrapper_pid"]}; only that real owner may '
                                        'bind a child to this attempt']}
                if (row['wrapper_created'] and wrapper_created is not None
                        and str(wrapper_created) != str(row['wrapper_created'])):
                    conn.execute('ROLLBACK')
                    return {'bound': False, 'token': token,
                            'reason': 'owner_mismatch',
                            'reasons': ['caller wrapper creation identity does not match '
                                        'the recorded owner (possible pid reuse)']}
            cur = conn.execute(
                'UPDATE attempts SET child_pid=?, child_created=? WHERE token=? '
                "AND child_pid IS NULL AND state IN (%s)"
                % ','.join('?' * len(ACTIVE_STATES)),
                (child_pid, child_created, token, *ACTIVE_STATES))
            changed = cur.rowcount
            conn.execute('COMMIT')
            if not changed:
                row = conn.execute('SELECT child_pid, child_created FROM attempts '
                                   'WHERE token=?', (token,)).fetchone()
                existing = (dict(row) if row is not None else None)
                return {'bound': False, 'token': token,
                        'existing_child': existing,
                        'reasons': ['child already bound (single-shot; never overwrite '
                                    'an existing live child) or attempt not in flight']}
            return {'bound': True, 'token': token, 'child_pid': child_pid,
                    'child_created': child_created}
        except Exception:
            conn.execute('ROLLBACK')
            raise


def finish(store_path, token, *, terminal='finished', success=None, now=None,
           prober=None, wrapper_pid=None, wrapper_created=None) -> dict:
    """终态释放容量名额（带真实终态核验，绝不能凭一个 CLI 参数假造）：
    - 已绑定 child：只接受探针回读 child 已真实退出（dead）才释放；child 仍活 → 拒绝；
      child 存活未知 → 拒绝（先 reconcile/mark_unknown）；已启动未知的绝不假定
      start_failed 释放。死 PID 的 created=None 属正常，不额外要求创建时刻。
    - 从未绑定 child（Z2 修订）：reserved 且无 wrapper（未消费的合法取消/预留）可
      start_failed/cancelled 释放；已进入 Popen-bind 窗口（有 wrapper 或 state=unknown）
      的无 child attempt，只有当前真实 owner 身份（wrapper_pid/created 与记录一致，
      即 owner 亲证 Popen 真失败/主动取消）才可 start_failed/cancelled，unknown 无
      child 绝不能凭一个终态参数释放；finished 无 child 证据 → 拒绝。
    - 子进程真实结束即释放（后续报告解析失败也仍释放）；重复释放幂等，不影响新 attempt。
    prober/wrapper_* 参数仅供测试与真实 owner 注入；CLI 不提供任何伪造终态的 flag。"""
    now = now or utcnow()
    if terminal not in RELEASED_STATES:
        raise ValueError(f'terminal must be one of {RELEASED_STATES}')
    probe = prober or (lambda pid: process_identity(int(pid)))
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute('SELECT * FROM attempts WHERE token=?', (token,)).fetchone()
            if row is None:
                conn.execute('ROLLBACK')
                return {'settled': False, 'released': False, 'token': token,
                        'terminal': terminal,
                        'reasons': [f'no such attempt token: {token}']}
            if row['state'] not in ACTIVE_STATES:
                conn.execute('ROLLBACK')
                return {'settled': False, 'released': False, 'token': token,
                        'terminal': row['terminal'], 'idempotent': True,
                        'reasons': [f'attempt already terminal '
                                    f'(state={row["state"]}, terminal={row["terminal"]}); '
                                    'repeat release changes nothing and never affects a '
                                    'new attempt']}
            if row['pool_key'] == LUNA_KEY:
                # 原生 Luna attempt 没有 OS child，国内 finish 的 child/owner 证据链
                # 对它无意义且可能被用来绕过回执要求；唯一终态出口是 settle-native。
                conn.execute('ROLLBACK')
                return {'settled': False, 'released': False, 'token': token,
                        'terminal': terminal,
                        'reasons': ['native Luna attempts have no OS child; the only '
                                    'terminal exit is settle-native with a trusted-'
                                    'host native receipt (raw worker text is never a '
                                    'terminal)']}
            if row['child_pid'] is not None:
                ident = probe(int(row['child_pid'])) or {'state': 'unknown', 'created': None}
                if ident.get('state') == 'alive':
                    conn.execute('ROLLBACK')
                    return {'settled': False, 'released': False, 'token': token,
                            'terminal': terminal, 'reason': 'child_alive',
                            'reasons': [f'bound child pid {row["child_pid"]} is still '
                                        'alive; direct finish of a live child is refused '
                                        '— wait for real exit or reconcile']}
                if ident.get('state') != 'dead':
                    conn.execute("UPDATE attempts SET state='unknown', "
                                 "terminal='unknown' WHERE token=?", (token,))
                    conn.execute('COMMIT')
                    return {'settled': False, 'released': False, 'token': token,
                            'terminal': 'unknown', 'reason': 'child_state_unknown',
                            'reasons': ['child liveness could not be verified; held as '
                                        'unknown (never assumed start_failed)']}
            elif terminal == 'finished':
                conn.execute('ROLLBACK')
                return {'settled': False, 'released': False, 'token': token,
                        'terminal': terminal, 'reason': 'no_child_terminal_unverified',
                        'reasons': ['terminal=finished requires a bound child verified '
                                    'exited; use start_failed/cancelled only when the '
                                    'process truly never started / was cancelled']}
            elif row['wrapper_pid'] is not None or row['state'] == 'unknown':
                # Z2：Popen-bind 窗口/unknown 的无 child attempt，只有当前真实 owner
                # （wrapper 身份一致，亲证 Popen 真失败或主动取消）才可释放。
                if (row['wrapper_pid'] is None or wrapper_pid is None
                        or int(wrapper_pid) != int(row['wrapper_pid'])):
                    conn.execute('ROLLBACK')
                    return {'settled': False, 'released': False, 'token': token,
                            'terminal': terminal, 'reason': 'owner_unverified',
                            'reasons': ['attempt is in a Popen-bind window (wrapper '
                                        f'pid {row["wrapper_pid"]}, state={row["state"]}) '
                                        'with no child; only the recorded real owner '
                                        'confirming its own Popen failure/cancel may '
                                        'release it — an unknown attempt is never '
                                        'released on a terminal argument alone']}
                if (row['wrapper_created'] and wrapper_created is not None
                        and str(wrapper_created) != str(row['wrapper_created'])):
                    conn.execute('ROLLBACK')
                    return {'settled': False, 'released': False, 'token': token,
                            'terminal': terminal, 'reason': 'owner_unverified',
                            'reasons': ['caller wrapper creation identity does not '
                                        'match the recorded owner (possible pid reuse)']}
            cur = conn.execute(
                'UPDATE attempts SET state=?, ended_at_utc=?, terminal=?, '
                'success=? WHERE token=? AND state IN (%s)'
                % ','.join('?' * len(ACTIVE_STATES)),
                (terminal, _iso(now), terminal,
                 (None if success is None else int(bool(success))), token, *ACTIVE_STATES))
            changed = cur.rowcount
            conn.execute('COMMIT')
            return {'settled': bool(changed), 'released': bool(changed),
                    'token': token, 'terminal': terminal}
        except Exception:
            conn.execute('ROLLBACK')
            raise


def mark_unknown(store_path, token, *, now=None) -> dict:
    """子进程可能仍活/存活未知：只标 unknown，继续占容量，绝不释放、绝不被抢占。"""
    now = now or utcnow()
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            cur = conn.execute(
                "UPDATE attempts SET state='unknown', terminal='unknown', ended_at_utc=? "
                "WHERE token=? AND state IN ('reserved','running')",
                (_iso(now), token))
            changed = cur.rowcount
            conn.execute('COMMIT')
            return {'settled': bool(changed), 'released': False, 'token': token,
                    'terminal': 'unknown', 'capacity_held': True}
        except Exception:
            conn.execute('ROLLBACK')
            raise


def reconcile(store_path=None, *, now=None, prober=None, platform=None) -> dict:
    """按 PID 存活/创建身份对在途 attempt 对账（修订：先判已绑定 child 的终态，再看
    wrapper；Luna native attempt 无 PID，跳过）：
    - 有 child：child 死（真实 Windows 死 PID created=None 属正常，不要求创建时刻）
      → reconciled_exit 释放（无论 wrapper 死活）；child 活且出生匹配 → 保持（reserved
      提升为 running）；child 活但出生未知/漂移（PID 复用嫌疑）或存活未知 → unknown
      保守不抢占；
    - 无 child：wrapper 活 → 保持（可能处于 Popen/bind 窗口，reserved 提升 running）；
      wrapper 死/存活未知 → 缺真实“未启动”证据，置 unknown 保留，绝不凭租期放行。
    缺证据一律保守 unknown，绝不按占位时间新旧抢占。prober 仅供测试注入合成探针。"""
    now = now or utcnow()
    prober = prober or (lambda pid: process_identity(pid, platform))
    released, held, promoted = [], [], []
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            rows = conn.execute(
                'SELECT * FROM attempts WHERE state IN (%s)'
                % ','.join('?' * len(ACTIVE_STATES)), ACTIVE_STATES).fetchall()
            for row in rows:
                tok = row['token']
                if row['pool_key'] == LUNA_KEY:
                    continue
                if row['child_pid'] is not None:
                    cident = prober(int(row['child_pid'])) or {'state': 'unknown',
                                                               'created': None}
                    if cident.get('state') == 'dead':
                        conn.execute("UPDATE attempts SET state='reconciled_exit', "
                                     "terminal='reconciled_exit', ended_at_utc=? "
                                     "WHERE token=?", (_iso(now), tok))
                        released.append(tok)
                        continue
                    if cident.get('state') == 'alive' and _created_matches(
                            row['child_created'], cident.get('created')):
                        if row['state'] == 'reserved':
                            conn.execute("UPDATE attempts SET state='running' "
                                         "WHERE token=?", (tok,))
                            promoted.append(tok)
                        continue
                    conn.execute("UPDATE attempts SET state='unknown', terminal='unknown' "
                                 "WHERE token=?", (tok,))
                    held.append({'token': tok, 'why': 'child alive with unknown/drifting '
                                                      'creation identity (possible pid '
                                                      'reuse) or liveness unknown'})
                    continue
                wp = row['wrapper_pid']
                if wp is None:
                    # 从未消费的合法预留（state=reserved、无 wrapper、无 child）：这是等待
                    # consume 的 reserved→running CAS、或显式 cancel 的干净名额。reconcile
                    # 绝不把它翻成 unknown——否则会破坏 consume 的同事务 CAS 竞争，并令合法
                    # cancel 误报 owner_unverified 无法释放（不做 lease 抢占，保持 reserved，
                    # 仍占容量，由持有 token 的入口 consume 或 cancel 单赢家结算）。
                    if row['state'] == 'reserved':
                        continue
                    # Z3：无 wrapper 也无 child 的 running/unknown 行没有任何可核验身份，
                    # 一律明确 held_unknown，绝不保持 running 也不释放。
                    conn.execute("UPDATE attempts SET state='unknown', terminal='unknown' "
                                 "WHERE token=?", (tok,))
                    held.append({'token': tok, 'why': 'no wrapper pid recorded'})
                    continue
                wident = prober(int(wp)) or {'state': 'unknown', 'created': None}
                if wident.get('state') == 'alive':
                    if row['state'] == 'reserved':
                        conn.execute("UPDATE attempts SET state='running' WHERE token=?",
                                     (tok,))
                        promoted.append(tok)
                    continue
                conn.execute("UPDATE attempts SET state='unknown', terminal='unknown' "
                             "WHERE token=?", (tok,))
                held.append({'token': tok, 'why': 'wrapper dead or unknown with no bound '
                                                  'child; no real never-started evidence, '
                                                  'held conservatively'})
            conn.execute('COMMIT')
            return {'released': released, 'held': held, 'promoted': promoted}
        except Exception:
            conn.execute('ROLLBACK')
            raise


# ------------------------------------------------------------------ Luna 票据
_SCOPE_KEYS = ('task_id', 'stage', 'chat_id', 'workspace', 'prompt_sha256')


def _normalize_scope(scope):
    """原 scope 绑定核验：接受 dict 或 JSON 字符串，必须含 task/stage/chat/workspace/
    promptSHA 五个键，task_id/workspace/prompt_sha256 非空。返回 (ok, dict, reasons)。"""
    if scope is None:
        return False, None, ['scope is required: the ticket must bind the original '
                             'task/stage/chat/workspace/prompt_sha256 being escalated']
    if isinstance(scope, str):
        try:
            parsed = json.loads(scope)
        except ValueError:
            return False, None, ['scope must be a JSON object or dict, not a bare string']
    elif isinstance(scope, dict):
        parsed = dict(scope)
    else:
        return False, None, [f'unsupported scope type: {type(scope).__name__}']
    if not isinstance(parsed, dict):
        return False, None, ['scope must be a JSON object binding the original ask facts']
    missing = [k for k in _SCOPE_KEYS if k not in parsed]
    if missing:
        return False, None, [f'scope missing keys: {missing}']
    for k in ('task_id', 'workspace', 'prompt_sha256'):
        if not parsed.get(k):
            return False, None, [f'scope.{k} must be non-empty']
    return True, parsed, []


def ask_record(store_path=None, *, task_id, scope=None, ask_message_id=None,
               now=None, deadline_seconds=LUNA_ASK_TIMEOUT_SECONDS) -> dict:
    """记录一次真实的“问用户（外部 agent 或 Luna）”票据并开始 300 秒计时。
    修订后的硬校验：
    - ask_message_id 必须非空（真实询问回执标识；本函数绝不假装已经问过）；
    - scope 必须完整绑定原 task/stage/chat/workspace/promptSHA，且 scope.task_id 与
      task_id 一致；
    - 六个国内名额必须确实全满（未满时优先国内，不该升级询问）；
    - 等待固定 LUNA_ASK_TIMEOUT_SECONDS=300 秒：更短的 deadline_seconds 直接拒绝，
      更长也按固定 300 落库（任务不能任意缩短/自定义）。
    同一 task 只能有一个 pending/claimed 票据；重启不重置 deadline（持久化）。
    now 参数仅供确定性函数测试注入；宿主生产路径（CLI）始终使用真实 UTC。"""
    now = now or utcnow()
    ok, parsed_scope, scope_reasons = _normalize_scope(scope)
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute('SELECT * FROM luna_tickets WHERE task_id=?',
                               (task_id,)).fetchone()
            if row is not None and row['state'] in ('pending', 'claimed'):
                conn.execute('ROLLBACK')
                return {'recorded': False, 'task_id': task_id, 'state': row['state'],
                        'deadline_utc': row['deadline_utc'],
                        'reasons': ['an ask ticket is already pending/claimed for this '
                                    'task; it is never reset by a restart']}
            reasons = []
            if not ask_message_id or not str(ask_message_id).strip():
                reasons.append('ask_message_id is required: a real ask receipt '
                               'identifier (message id) from the actual question')
            if not ok:
                reasons.extend(scope_reasons)
            elif parsed_scope['task_id'] != task_id:
                reasons.append(f"scope.task_id {parsed_scope['task_id']!r} does not "
                               f'match ticket task_id {task_id!r}')
            if deadline_seconds is not None and deadline_seconds < LUNA_ASK_TIMEOUT_SECONDS:
                reasons.append(f'deadline_seconds={deadline_seconds} is below the fixed '
                               f'{LUNA_ASK_TIMEOUT_SECONDS}s wait; a task may never '
                               'shorten the user-response window')
            if _domestic_active(conn) < DOMESTIC_TOTAL_CAPACITY:
                reasons.append('domestic slots are not full; prioritize domestic '
                               'capacity instead of escalating to an ask')
            if reasons:
                conn.execute('ROLLBACK')
                return {'recorded': False, 'task_id': task_id, 'reasons': reasons}
            asked = _iso(now)
            deadline = _iso(now + timedelta(seconds=LUNA_ASK_TIMEOUT_SECONDS))
            scope_json = json.dumps(parsed_scope, ensure_ascii=False, sort_keys=True)
            conn.execute(
                'INSERT INTO luna_tickets(task_id, state, scope, ask_message_id, '
                'asked_at_utc, deadline_utc, agent_id, updated_at_utc) '
                'VALUES(?,?,?,?,?,?,?,?) '
                'ON CONFLICT(task_id) DO UPDATE SET state=?, scope=?, ask_message_id=?, '
                'asked_at_utc=?, deadline_utc=?, reply_choice=NULL, reply_note=NULL, '
                'replied_at_utc=NULL, claimed_token=NULL, launch_state=NULL, agent_id=NULL, '
                'updated_at_utc=?',
                (task_id, 'pending', scope_json, str(ask_message_id), asked, deadline,
                 None, asked,
                 'pending', scope_json, str(ask_message_id), asked, deadline, asked))
            conn.execute('COMMIT')
            return {'recorded': True, 'task_id': task_id, 'state': 'pending',
                    'asked_at_utc': asked, 'deadline_utc': deadline,
                    'deadline_seconds': LUNA_ASK_TIMEOUT_SECONDS}
        except Exception:
            conn.execute('ROLLBACK')
            raise


def reply(store_path=None, *, task_id, choice, note=None, now=None) -> dict:
    """记录用户回复选择（幂等：首条回复生效，之后重复 callback 不覆盖、不重置任何
    计时）。一旦回复，claim-due 的超时自动裁决停止；reply luna 是明确的已授权接续
    路径（claim-due 可立即裁决 Luna）；external_agent/cancel 停止自动 Luna。"""
    now = now or utcnow()
    if choice not in ('luna', 'external_agent', 'domestic', 'cancel'):
        raise ValueError("choice must be one of luna/external_agent/domestic/cancel")
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute('SELECT * FROM luna_tickets WHERE task_id=?',
                               (task_id,)).fetchone()
            if row is None:
                conn.execute('ROLLBACK')
                return {'recorded': False, 'task_id': task_id,
                        'reasons': ['no ask ticket exists for this task']}
            if row['state'] == 'claimed':
                conn.execute('ROLLBACK')
                return {'recorded': False, 'task_id': task_id, 'state': 'claimed',
                        'reasons': ['ticket already claimed; reply recorded too late']}
            if row['state'] == 'replied':
                conn.execute('ROLLBACK')
                return {'recorded': False, 'task_id': task_id, 'state': 'replied',
                        'reply_choice': row['reply_choice'], 'idempotent': True,
                        'reasons': ['reply already recorded; repeat callbacks never '
                                    'overwrite the first reply or reset anything']}
            if row['state'] == 'cancelled':
                conn.execute('ROLLBACK')
                return {'recorded': False, 'task_id': task_id, 'state': 'cancelled',
                        'reasons': ['ticket was already cancelled']}
            conn.execute('UPDATE luna_tickets SET state=?, reply_choice=?, reply_note=?, '
                         'replied_at_utc=?, updated_at_utc=? WHERE task_id=?',
                         ('replied', choice, note, _iso(now), _iso(now), task_id))
            conn.execute('COMMIT')
            return {'recorded': True, 'task_id': task_id, 'state': 'replied',
                    'choice': choice}
        except Exception:
            conn.execute('ROLLBACK')
            raise


def claim_due(store_path=None, *, task_id, now=None, scope=None,
              _preclaim=True) -> dict:
    """到期裁决（同一 `BEGIN IMMEDIATE` 事务内的竞争，修订后无丢槽竞态）：
    - 票据 pending 且已过 deadline 且未回复（或用户已明确 reply luna）时：
      * 国内已恢复空位 → 在同一事务内按真实主力策略原子预留一个国内 claim、
        取消同 task 待援票据，返回 mode=domestic_reclaim + 真实选中组合 + token
        （绝不“只 cancel 不预留”，槽不再丢失）；scope 必须与票据原 scope 一致；
      * 六仍满 → 创建一个 luna attempt（不同 task 无全局数量 cap）并把票据置
        claimed（launch_state=host_must_call_native）。实际原生工具由宿主调用，
        Python 绝不冒称已 spawn；启动后崩溃未写出 agentID → mark_launch_unknown，
        绝不自动重复启动。
    - 已 claimed / launch_unknown → 幂等拒绝（带 agent_id/launch_state），不重启；
    - 已回复（非 luna）→ 超时不是默认无限授权，拒绝；未到期 → not_due。"""
    now = now or utcnow()
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            if _preclaim:
                _preclaim_reconcile(conn, now)
            row = conn.execute('SELECT * FROM luna_tickets WHERE task_id=?',
                               (task_id,)).fetchone()
            if row is None:
                conn.execute('ROLLBACK')
                return {'claimed': False, 'task_id': task_id,
                        'reasons': ['no ask ticket; Luna was never really requested']}
            if row['state'] == 'claimed' or row['launch_state'] == 'launch_unknown':
                conn.execute('ROLLBACK')
                return {'claimed': False, 'task_id': task_id, 'state': row['state'],
                        'already_claimed': True,
                        'claimed_token': row['claimed_token'],
                        'launch_state': row['launch_state'],
                        'agent_id': row['agent_id'],
                        'reasons': ['ticket already claimed or launch_unknown; the '
                                    'native call is non-idempotent — no auto-repeat, '
                                    'no restart']}
            if row['state'] == 'cancelled':
                conn.execute('ROLLBACK')
                return {'claimed': False, 'task_id': task_id, 'state': 'cancelled',
                        'reasons': ['ticket was cancelled (a domestic slot freed or the '
                                    'user chose otherwise)']}
            if row['state'] == 'settled':
                conn.execute('ROLLBACK')
                return {'claimed': False, 'task_id': task_id, 'state': 'settled',
                        'reasons': ['ticket was already settled natively (terminal '
                                    'receipt recorded); it is never re-escalated or '
                                    're-dispatched']}
            if row['state'] == 'replied':
                if row['reply_choice'] not in ('luna', 'domestic'):
                    conn.execute('ROLLBACK')
                    return {'claimed': False, 'task_id': task_id, 'state': 'replied',
                            'reply_choice': row['reply_choice'],
                            'reasons': ['user replied '
                                        f'{row["reply_choice"]!r}; wait-timeout is not a '
                                        'default infinite authorization and the explicit '
                                        'reply stops auto-Luna']}
            # Z2：先做票据原 scope 解析与（若传入）一致性核验——两个分支（国内回收与
            # Luna）都必须与原 scope 完全一致，任一字段漂移即拒。
            ticket_scope = row['scope']
            parsed_ticket_scope = None
            if ticket_scope:
                ok_ts, parsed_ticket_scope, _ = _normalize_scope(ticket_scope)
                if not ok_ts:
                    parsed_ticket_scope = None
            if scope is not None:
                ok, parsed, scope_reasons = _normalize_scope(scope)
                if not ok:
                    conn.execute('ROLLBACK')
                    return {'claimed': False, 'task_id': task_id, 'state': row['state'],
                            'reasons': scope_reasons}
                if json.dumps(parsed, ensure_ascii=False, sort_keys=True) != ticket_scope:
                    conn.execute('ROLLBACK')
                    return {'claimed': False, 'task_id': task_id, 'state': row['state'],
                            'reasons': ['scope does not match the original ticket scope '
                                        '(checked for both domestic reclaim and Luna)']}
            # Z2 修订顺序：真实票且未被 claimed/launch_unknown、用户未选外部/cancel 之后，
            # 先看国内是否已恢复空位（不必等到期：30 秒内释放也立即国内回收）；只有六
            # 仍满才判断 300 秒 deadline 或用户明确 reply luna。
            reply_luna = row['state'] == 'replied' and row['reply_choice'] == 'luna'
            if not reply_luna and _domestic_active(conn) < DOMESTIC_TOTAL_CAPACITY:
                target, _ = _select_main_force(conn, None, now)
                if target is None:
                    ovf = OVERFLOW_KEYS[0]
                    if (_count_active(conn, ovf) < capacity_for(ovf)):
                        target = ovf
                if target is None:
                    conn.execute('ROLLBACK')
                    return {'claimed': False, 'task_id': task_id, 'state': row['state'],
                            'reasons': ['no domestic slot could be atomically reserved '
                                        'despite a free count; retry']}
                dup = _active_task(conn, task_id)
                if dup is not None:
                    conn.execute("UPDATE luna_tickets SET state='cancelled', "
                                 'updated_at_utc=? WHERE task_id=?',
                                 (_iso(now), task_id))
                    conn.execute('COMMIT')
                    return {'claimed': False, 'task_id': task_id, 'state': 'cancelled',
                            'reasons': [f'task {task_id!r} already has an in-flight '
                                        'domestic attempt; aid ticket cancelled']}
                tr, tm = target.split(':', 1)
                # Z4：国内回收同样过真实 workspace 单写入守卫（别的 task 在途则拒，
                # 票据保持 pending 等下次裁决，绝不改派 Luna/Flash 绕过）。
                busy_ws = _workspace_conflict(
                    conn, parsed_ticket_scope.get('workspace')
                    if parsed_ticket_scope else None, own_task_id=task_id)
                if busy_ws is not None:
                    conn.execute('ROLLBACK')
                    return {'claimed': False, 'task_id': task_id,
                            'state': row['state'],
                            'reasons': [f'workspace {busy_ws["workspace"]!r} already '
                                        f'has an in-flight attempt from a different '
                                        f'task (token={busy_ws["token"]}, '
                                        f'task={busy_ws["task_id"]!r}); the domestic '
                                        'reclaim was refused and the ticket stays '
                                        'pending — never rerouted around the guard']}
                # Z2：国内回收必须写回票据原 scope 的真实绑定字段（stage/chat/
                # prompt_sha256/workspace），返回的 token 才能被原入口按原 scope 消费。
                tok = _insert_claim(conn, task_id=task_id,
                                    stage=parsed_ticket_scope.get('stage')
                                    if parsed_ticket_scope else None,
                                    chat_id=parsed_ticket_scope.get('chat_id')
                                    if parsed_ticket_scope else None,
                                    prompt_sha256=parsed_ticket_scope.get('prompt_sha256')
                                    if parsed_ticket_scope else None,
                                    workspace=_norm_workspace(
                                        parsed_ticket_scope.get('workspace'))
                                    if parsed_ticket_scope else None,
                                    runtime=tr, model=tm, pk=target, token=None, now=now,
                                    origin='claim-due-domestic-reclaim')
                if target in MAIN_FORCE_KEYS:
                    _bump_rotation(conn, target, now)
                conn.execute("UPDATE luna_tickets SET state='cancelled', "
                             "reply_note='domestic_slot_freed', updated_at_utc=? "
                             "WHERE task_id=?", (_iso(now), task_id))
                conn.execute('COMMIT')
                return {'claimed': True, 'mode': 'domestic_reclaim', 'task_id': task_id,
                        'token': tok, 'pool_key': target,
                        'selected': {'pool_key': target, 'runtime': tr, 'model': tm},
                        'scope': parsed_ticket_scope, 'launch_state': None,
                        'reasons': ['a domestic slot is free again; atomically reserved '
                                    'for this task (bound to the original ticket scope) '
                                    'and cancelled the pending aid (no double-dispatch, '
                                    'no lost slot)']}
            # 六仍满：未到期（不重置 deadline）一律拒绝；到期未回复或明确 reply luna
            # 才允许 Luna。
            if row['state'] == 'pending':
                deadline = _parse(row['deadline_utc'])
                if deadline is not None and now < deadline:
                    conn.execute('ROLLBACK')
                    return {'claimed': False, 'task_id': task_id, 'state': 'pending',
                            'not_due': True, 'deadline_utc': row['deadline_utc'],
                            'reasons': ['all six domestic slots are still full and the '
                                        'ask deadline has not elapsed yet']}
            dup = _active_task(conn, task_id)
            if dup is not None:
                conn.execute('ROLLBACK')
                return {'claimed': False, 'task_id': task_id,
                        'reasons': [f'task {task_id!r} already has an in-flight attempt']}
            tok = uuid.uuid4().hex
            # Z4：Luna 分支同样过真实 workspace 单写入守卫（原生 Luna 写同一真实目录，
            # 与国内执行器互斥）。
            busy_ws = _workspace_conflict(
                conn, parsed_ticket_scope.get('workspace')
                if parsed_ticket_scope else None, own_task_id=task_id)
            if busy_ws is not None:
                conn.execute('ROLLBACK')
                return {'claimed': False, 'task_id': task_id, 'state': row['state'],
                        'reasons': [f'workspace {busy_ws["workspace"]!r} already has '
                                    f'an in-flight attempt from a different task '
                                    f'(token={busy_ws["token"]}, '
                                    f'task={busy_ws["task_id"]!r}); Luna was refused '
                                    'for the same one-writer-per-workspace rule']}
            # Z2：Luna attempt 同样写回票据原 scope，保持记录完整可追溯。
            conn.execute(
                'INSERT INTO attempts(token, task_id, stage, chat_id, prompt_sha256, '
                'workspace, runtime, model, pool_key, state, reserved_at_utc, origin) '
                "VALUES(?,?,?,?,?,?,?,?,?, 'running', ?, 'luna-claim-due')",
                (tok, task_id,
                 parsed_ticket_scope.get('stage') if parsed_ticket_scope else None,
                 parsed_ticket_scope.get('chat_id') if parsed_ticket_scope else None,
                 parsed_ticket_scope.get('prompt_sha256') if parsed_ticket_scope else None,
                 _norm_workspace(parsed_ticket_scope.get('workspace'))
                 if parsed_ticket_scope else None,
                 LUNA_RUNTIME, LUNA_MODEL, LUNA_KEY, _iso(now)))
            conn.execute("UPDATE luna_tickets SET state='claimed', claimed_token=?, "
                         "launch_state='host_must_call_native', updated_at_utc=? "
                         "WHERE task_id=?", (tok, _iso(now), task_id))
            conn.execute('COMMIT')
            return {'claimed': True, 'mode': 'luna', 'task_id': task_id, 'token': tok,
                    'pool_key': LUNA_KEY, 'launch_state': 'host_must_call_native',
                    'scope': parsed_ticket_scope,
                    'reasons': ['all six domestic slots full and the ask deadline elapsed '
                                'with no reply (or the user explicitly replied luna); '
                                'the host may now call native Luna. If the native tool '
                                'started but crashed before writing an agentID, record '
                                'launch_unknown and do NOT auto-repeat the start.']}
        except Exception:
            conn.execute('ROLLBACK')
            raise


def mark_launch_unknown(store_path=None, *, task_id, now=None) -> dict:
    """原生 Luna 调用非幂等窗口：工具已启动但在写出 agentID 前崩溃 → launch_unknown，
    待人工核验，绝不自动重复启动。只对已 claimed 票据生效，不改变 claimed 终态。"""
    now = now or utcnow()
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute('SELECT state, launch_state FROM luna_tickets '
                               'WHERE task_id=?', (task_id,)).fetchone()
            if row is None or row['state'] != 'claimed':
                conn.execute('ROLLBACK')
                return {'recorded': False, 'task_id': task_id,
                        'reasons': ['launch_unknown only applies to a claimed ticket '
                                    '(native already started once)']}
            conn.execute("UPDATE luna_tickets SET launch_state='launch_unknown', "
                         'updated_at_utc=? WHERE task_id=?', (_iso(now), task_id))
            conn.execute('COMMIT')
            return {'recorded': True, 'task_id': task_id,
                    'launch_state': 'launch_unknown'}
        except Exception:
            conn.execute('ROLLBACK')
            raise


def record_agent_id(store_path=None, *, task_id, agent_id, now=None) -> dict:
    """记录宿主真实原生调用后的 agentID（真实回执；本模块绝不自己生成或冒称）。"""
    now = now or utcnow()
    if not agent_id or not str(agent_id).strip():
        return {'recorded': False, 'task_id': task_id,
                'reasons': ['agent_id must be a non-empty real native-agent identifier']}
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute('SELECT state FROM luna_tickets WHERE task_id=?',
                               (task_id,)).fetchone()
            if row is None or row['state'] != 'claimed':
                conn.execute('ROLLBACK')
                return {'recorded': False, 'task_id': task_id,
                        'reasons': ['agent_id can only be recorded on a claimed ticket']}
            conn.execute('UPDATE luna_tickets SET agent_id=?, updated_at_utc=? '
                         'WHERE task_id=?', (str(agent_id), _iso(now), task_id))
            conn.execute('COMMIT')
            return {'recorded': True, 'task_id': task_id, 'agent_id': str(agent_id)}
        except Exception:
            conn.execute('ROLLBACK')
            raise


NATIVE_TERMINAL_STATES = ('finished', 'native_failed')


_RECEIPT_SHA256 = re.compile(r'\A[0-9a-fA-F]{64}\Z')


def _receipt_content_bytes(content):
    """Z6-B 回执原文哈希口径：content 为 string 时按 UTF-8 原文字节哈希（绝不加
    JSON 引号）；对象按明确 canonical JSON（sort_keys + ensure_ascii=False +
    紧凑分隔符）哈希。"""
    if isinstance(content, str):
        return content.encode('utf-8')
    return json.dumps(content, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def _native_receipt_evidence(receipt):
    """校验宿主提供的原生终态回执，返回可追溯元信息 (evidence, reasons)。
    回执必须是 dict/JSON 对象，含：
    - source_tool：受信任宿主用哪个原生工具独立回读确认终态；
    - receipt_ref：原始原生回执的可检索引用（消息/运行标识等）；
    - sha256：原始回执哈希（Z6：严格 64 位 ASCII 十六进制，统一小写落库）；
      或 content：原始回执原文（本函数只计算并保存哈希，绝不解析/采信其内容
      本身）。两者同时给出必须核对一致；content 为 string 时按 UTF-8 原文字节
      哈希（不加 JSON 引号），对象按 canonical JSON。仅 terminal 字符串、缺
      source_tool/receipt_ref/哈希、非 64 位十六进制 sha 都视为没有真实宿主回执，
      拒绝。元信息只是受信任宿主边界内的可追溯证据，不是独立服务器验证。"""
    if receipt is None:
        return None, ['receipt is required: a bare terminal string is not a native '
                      'terminal proof; the trusted host must independently re-read the '
                      'native tool and pass the original receipt reference + hash']
    if isinstance(receipt, str):
        try:
            parsed = json.loads(receipt)
        except ValueError:
            return None, ['receipt must be a JSON object with source_tool/receipt_ref '
                          '(+ sha256 or content); a bare non-JSON string is not a '
                          'native receipt']
    elif isinstance(receipt, dict):
        parsed = dict(receipt)
    else:
        return None, [f'unsupported receipt type: {type(receipt).__name__}']
    if not isinstance(parsed, dict):
        return None, ['receipt must be a JSON object binding the native tool receipt']
    source_tool = parsed.get('source_tool')
    receipt_ref = parsed.get('receipt_ref')
    digest = parsed.get('sha256')
    content = parsed.get('content')
    if not source_tool or not str(source_tool).strip():
        return None, ['receipt.source_tool is required: which native tool the trusted '
                      'host re-read to independently confirm the terminal state']
    if not receipt_ref or not str(receipt_ref).strip():
        return None, ['receipt.receipt_ref is required: a retrievable reference to the '
                      'original native terminal receipt']
    # Z6-B：sha256 必须是严格 64 位 ASCII 十六进制（大小写规范：统一按小写落库）。
    if digest is not None:
        digest = str(digest)
        if not _RECEIPT_SHA256.match(digest):
            return None, ['receipt.sha256 must be exactly 64 ASCII hexadecimal '
                          f'characters (canonical lowercase on storage); got {digest!r}']
        digest = digest.lower()
    if content is None and digest is None:
        return None, ['receipt must carry sha256 of the original native receipt, '
                      'or the receipt content itself (its hash is computed and '
                      'stored; the raw text alone without any reference is never '
                      'accepted)']
    if content is not None:
        # Z6-B：content 与 sha256 同时给出必须核对一致；string 原文按 UTF-8 原字节
        # （不加 JSON 引号），对象按 canonical JSON 口径。
        computed = hashlib.sha256(_receipt_content_bytes(content)).hexdigest()
        if digest is None:
            digest = computed
        elif digest != computed:
            return None, ['receipt.sha256 does not match the sha256 of receipt.content '
                          '(string content is hashed as its raw UTF-8 bytes without '
                          'JSON quotes; object content uses canonical JSON); when both '
                          'are given they must agree']
    return {'source_tool': str(source_tool), 'receipt_ref': str(receipt_ref),
            'receipt_sha256': digest}, []


def settle_native(store_path=None, *, task_id, token, agent_id, terminal,
                  success=None, scope=None, receipt=None, now=None) -> dict:
    """原生 Luna 终态结算（最小受信任宿主原生回执入口；单事务）。

    信任边界：本机没有 Luna 的 OS 子进程，原生平台终态无法由本脚本独立探测。
    只有受信任宿主先用原生工具独立回读、确认 agent 真实终态之后，才可携带原始
    回执元信息调用本入口结算；原始 worker 文本/仅 terminal 字符串绝不构成终态。

    硬校验（任一不满足 → 拒绝且不释放，attempt/workspace 继续占用）：
    - 票据必须处于 claimed 且 claimed_token 与传入 token 一致（token 漂移拒绝）；
    - launch_state=launch_unknown → 拒绝（待人工核验，也绝不自动重派）；
    - 票据必须已登记真实 agent_id，且与传入 agent_id 一致（缺/错 agentID 拒绝）；
    - 传入 scope 若给出，必须与票据原 scope 完全一致（scope 漂移拒绝）；
    - receipt 必须是含 source_tool/receipt_ref + sha256(或 content) 的真实回执
      元信息（哈希与引用入库可追溯；仅 terminal 字符串拒绝）；
    - terminal 只接受明确终态 finished（成功）/native_failed（失败），运行中/
      unknown 等非终态拒绝；正常完成绝不伪标 cancelled/start_failed。

    单事务内：attempt 置终态（成功/失败分开，回执元信息存 adopt_evidence）、
    票据置 settled/launch_state=native_terminal_recorded，原 task/workspace 随
    attempt 退出 ACTIVE 而释放（Luna 本不占国内 6 个名额，国内活进程不受影响）。
    重复同终态结算幂等且不影响新 attempt；不加 force、没有任何全放行布尔。"""
    now = now or utcnow()
    if terminal not in NATIVE_TERMINAL_STATES:
        raise ValueError(f'terminal must be one of {NATIVE_TERMINAL_STATES}; running/'
                         'unknown are explicitly not native terminal states')
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute('SELECT * FROM luna_tickets WHERE task_id=?',
                               (task_id,)).fetchone()
            if row is None:
                conn.execute('ROLLBACK')
                return {'settled': False, 'released': False, 'task_id': task_id,
                        'reasons': ['no such luna ticket; settle_native only applies '
                                    'to a real claimed Luna escalation']}
            attempt = None
            if token:
                attempt = conn.execute('SELECT * FROM attempts WHERE token=?',
                                       (token,)).fetchone()
            if attempt is None:
                conn.execute('ROLLBACK')
                return {'settled': False, 'released': False, 'task_id': task_id,
                        'reasons': [f'no attempt matches token {token!r}; settle_native '
                                    'must bind the exact claimed ticket token']}
            # Z6-C：token 必须绑定本票据（claimed 或已 settled 的幂等重放同此口径）。
            if row['claimed_token'] != token:
                conn.execute('ROLLBACK')
                return {'settled': False, 'released': False, 'task_id': task_id,
                        'token': token, 'state': row['state'],
                        'reasons': ['ticket/token drift: the ticket is not bound '
                                    f'to this exact token (state={row["state"]}, '
                                    f'claimed_token={row["claimed_token"]!r}); refused '
                                    'and nothing released']}
            # Z6-C：国内 attempt 绝不能经 native 入口结算。
            if attempt['pool_key'] != LUNA_KEY:
                conn.execute('ROLLBACK')
                return {'settled': False, 'released': False, 'task_id': task_id,
                        'token': token,
                        'reasons': ['settle_native only settles native Luna attempts; '
                                    f'a domestic attempt (pool={attempt["pool_key"]}) '
                                    'must leave through the domestic finish/reconcile '
                                    'path — the native entry never settles it']}
            # Z6-C：全部身份/回执校验先于幂等返回——错误 agent/scope/缺回执/不同终态
            # 的重复请求绝不能伪称幂等成功。
            if row['launch_state'] == 'launch_unknown':
                conn.execute('ROLLBACK')
                return {'settled': False, 'released': False, 'task_id': task_id,
                        'token': token, 'launch_state': 'launch_unknown',
                        'reasons': ['launch_unknown must stay protected: the native '
                                    'launch was never confirmed; it is never settled '
                                    'here and never auto-re-dispatched (manual '
                                    'verification required)']}
            if not row['agent_id']:
                conn.execute('ROLLBACK')
                return {'settled': False, 'released': False, 'task_id': task_id,
                        'token': token,
                        'reasons': ['ticket has no recorded real agent_id; a native '
                                    'terminal can only be settled for a registered '
                                    'real agent']}
            if not agent_id or str(agent_id) != str(row['agent_id']):
                conn.execute('ROLLBACK')
                return {'settled': False, 'released': False, 'task_id': task_id,
                        'token': token,
                        'reasons': [f'agent_id mismatch: ticket holds '
                                    f'{row["agent_id"]!r}; the settle receipt must '
                                    'bind the same real native agent']}
            if scope is not None:
                ok, parsed, scope_reasons = _normalize_scope(scope)
                if not ok:
                    conn.execute('ROLLBACK')
                    return {'settled': False, 'released': False, 'task_id': task_id,
                            'token': token, 'reasons': scope_reasons}
                if json.dumps(parsed, ensure_ascii=False, sort_keys=True) != row['scope']:
                    conn.execute('ROLLBACK')
                    return {'settled': False, 'released': False, 'task_id': task_id,
                            'token': token,
                            'reasons': ['scope does not match the original ticket '
                                        'scope; drift is refused and nothing is '
                                        'released']}
            evidence, ev_reasons = _native_receipt_evidence(receipt)
            if evidence is None:
                conn.execute('ROLLBACK')
                return {'settled': False, 'released': False, 'task_id': task_id,
                        'token': token, 'reasons': ev_reasons}
            if attempt['state'] not in ACTIVE_STATES:
                # 票据已结算后的重复请求：只有同终态 + 同原回执哈希的合法幂等重放
                # 返回 settled=true（不覆盖旧证据、不碰任何新 attempt）；其余全拒。
                stored_digest = None
                if attempt['adopt_evidence']:
                    try:
                        stored_digest = json.loads(attempt['adopt_evidence']).get(
                            'native_terminal_receipt', {}).get('receipt_sha256')
                    except ValueError:
                        stored_digest = None
                same_terminal = attempt['terminal'] == terminal
                same_receipt = (stored_digest is not None
                                and stored_digest == evidence['receipt_sha256'])
                conn.execute('ROLLBACK')
                if same_terminal and same_receipt:
                    return {'settled': True, 'released': False, 'task_id': task_id,
                            'token': token, 'terminal': attempt['terminal'],
                            'idempotent': True, 'receipt_sha256': stored_digest,
                            'reasons': ['attempt already terminal '
                                        f'(state={attempt["state"]}, '
                                        f'terminal={attempt["terminal"]}) with the '
                                        'same original receipt; the legal repeat '
                                        'changes nothing, never overwrites the first '
                                        'evidence and never affects a new attempt']}
                return {'settled': False, 'released': False, 'task_id': task_id,
                        'token': token, 'terminal': attempt['terminal'],
                        'reasons': ['attempt already terminal '
                                    f'(state={attempt["state"]}, '
                                    f'terminal={attempt["terminal"]}); a repeat '
                                    'settle with a different terminal or a different '
                                    'receipt is refused and never overwrites the '
                                    'recorded evidence']}
            if row['state'] != 'claimed':
                conn.execute('ROLLBACK')
                return {'settled': False, 'released': False, 'task_id': task_id,
                        'token': token, 'state': row['state'],
                        'reasons': [f'ticket state is {row["state"]!r}, not claimed; '
                                    'refused and nothing released']}
            if success is None:
                success = (terminal == 'finished')
            elif bool(success) != (terminal == 'finished'):
                # Z6-D：terminal 与 success 不得矛盾——--success true 不能把
                # native_failed 记成成功，--success false 也不能把 finished 记失败。
                conn.execute('ROLLBACK')
                return {'settled': False, 'released': False, 'task_id': task_id,
                        'token': token, 'terminal': terminal,
                        'reasons': [f'success={bool(success)!r} contradicts '
                                    f'terminal={terminal!r}; finished means success '
                                    'and native_failed means failure — the flag is '
                                    'only accepted when it agrees with the terminal']}
            conn.execute(
                'UPDATE attempts SET state=?, ended_at_utc=?, terminal=?, success=?, '
                'adopt_evidence=? WHERE token=? AND state IN (%s)'
                % ','.join('?' * len(ACTIVE_STATES)),
                (terminal, _iso(now), terminal, int(bool(success)),
                 json.dumps({'native_terminal_receipt': evidence},
                            ensure_ascii=False, sort_keys=True),
                 token, *ACTIVE_STATES))
            conn.execute("UPDATE luna_tickets SET state='settled', "
                         "launch_state='native_terminal_recorded', updated_at_utc=? "
                         'WHERE task_id=?', (_iso(now), task_id))
            conn.execute('COMMIT')
            return {'settled': True, 'released': True, 'task_id': task_id,
                    'token': token, 'terminal': terminal,
                    'success': bool(success),
                    'receipt_sha256': evidence['receipt_sha256'],
                    'reasons': ['trusted host re-read the native tool and provided the '
                                'original terminal receipt (source_tool/receipt_ref/'
                                'sha256 recorded); attempt and ticket settled in one '
                                'transaction, original task/workspace released '
                                '(domestic slots were never held by Luna)']}
        except Exception:
            conn.execute('ROLLBACK')
            raise


def cancel_pending(store_path=None, *, task_id, now=None, reason='domestic_slot_freed') -> dict:
    """国内名额释放时，原子取消同 task 的 pending 救援票据（优先国内，不双派）。"""
    now = now or utcnow()
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            cur = conn.execute(
                "UPDATE luna_tickets SET state='cancelled', reply_note=?, updated_at_utc=? "
                "WHERE task_id=? AND state='pending'", (reason, _iso(now), task_id))
            changed = cur.rowcount
            conn.execute('COMMIT')
            return {'cancelled': bool(changed), 'task_id': task_id, 'reason': reason}
        except Exception:
            conn.execute('ROLLBACK')
            raise


# ------------------------------------------------------------------ adopt-legacy
# Z3：新旧版本上线交接。只接纳“受信任宿主”提供的既有 Qoder/Zcode 原请求与邻近
# process.json，绝不自动扫描用户目录；宿主负责确认该 PID 与该 receipt 的真实命令/
# 工作区对应（可信操作前提，见 handoff/revision3-report.md）。仅允许 GLM-5.3 与
# Qwen3.8-Max/Flash；CB/WB 等其它组合一律拒绝且不触摸其 quota/并发锁。
ADOPTABLE_POOL_KEYS = ('zcode:GLM-5.3', 'qoder:Qwen3.8-Max', 'qoder:Qwen3.8-Flash')


def _normalize_legacy_request(request):
    """Z4/Z5：把宿主已核对的原 request.json 归一为本池绑定字段，兼容真实新旧 schema：
    - 当前 schema：顶层 task_id/runtime/model；
    - Qoder 真实旧原件：无顶层 task_id/model；runtime 是对象 {node, qodercli}（不是
      字符串 'node'/'qodercli'），模型在 model_requested（池 runtime 统一为 qoder，
      model 取 model_requested）；
    - Zcode 真实旧原件：carrier='zcode-sdk' + selection.{providerId, modelId}。runtime
      必须归为 'zcode'（来自 carrier），providerId 是账号/供应商标识
      （account:bigmodel-individual-coding-plan），绝不能当作 runtime；model 取 modelId。
    task_id 可靠推导顺序：顶层 task_id → dispatch_plan.task_id → 原阶段 stage。
    只读解析，绝不改写任何旧原件；识别不了返回 (None, reasons)。"""
    if not isinstance(request, dict):
        return None, ['request must be a JSON object']
    if request.get('task_id') and request.get('runtime') and request.get('model'):
        return ({'task_id': str(request['task_id']),
                 'runtime': str(request['runtime']), 'model': str(request['model']),
                 'stage': request.get('stage'), 'chat_id': request.get('chat_id')}, [])
    selection = request.get('selection')
    if isinstance(selection, str):
        try:
            selection = json.loads(selection)
        except ValueError:
            selection = None
    if not isinstance(selection, dict):
        selection = {}
    plan = request.get('dispatch_plan')
    if isinstance(plan, str):
        try:
            plan = json.loads(plan)
        except ValueError:
            plan = None
    if not isinstance(plan, dict):
        plan = {}
    rt = request.get('runtime')
    # Qoder 真实旧原件 runtime 是 {node, qodercli} 对象；也兼容历史上的字符串形式。
    qoder_runtime = (
        (isinstance(rt, dict) and ('node' in rt or 'qodercli' in rt))
        or (isinstance(rt, str) and rt in ('node', 'qodercli'))
    )
    if request.get('carrier') == 'zcode-sdk' and selection.get('modelId'):
        # runtime 恒为 zcode（来自 carrier），providerId 只是账号/供应商标识，不是 runtime。
        runtime, model = 'zcode', str(selection['modelId'])
    elif request.get('model_requested') and qoder_runtime:
        runtime, model = 'qoder', str(request['model_requested'])
    else:
        return None, ['unrecognized request schema: expected current {task_id, runtime, '
                      'model}, legacy Qoder {model_requested, runtime {node,qodercli}} or '
                      "legacy Zcode {carrier='zcode-sdk', selection.modelId}"]
    task_id = request.get('task_id') or plan.get('task_id') or request.get('stage')
    if not task_id:
        return None, ['cannot derive task_id: need request task_id, dispatch_plan.'
                      'task_id or the original stage']
    return ({'task_id': str(task_id), 'runtime': runtime, 'model': model,
             'stage': request.get('stage'), 'chat_id': request.get('chat_id')}, [])


def _file_sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def _adopt_reject(reason, task_id, pk, reasons):
    return {'adopted': False, 'token': None, 'task_id': task_id, 'pool_key': pk,
            'reason': reason, 'reasons': reasons}


def adopt_legacy(store_path=None, *, task_id, runtime, model, request_path,
                 process_path, prober=None, now=None, child_created=None) -> dict:
    """把一个仍活着的旧版 worker 如实计入本池（Z3 上线交接 + Z4 实质修正）：
    - 输入只认宿主提供的既有原 request.json 与邻近 process.json 文件路径，只读解析、
      绝不改写旧原件。request 兼容真实新旧 schema（见 _normalize_legacy_request）：
      当前顶层 task_id/runtime/model；Qoder 旧件 model_requested + runtime∈{node,
      qodercli}；Zcode 旧件 carrier='zcode-sdk' + selection.providerId/modelId。
      task 由顶层 → dispatch_plan.task_id → 原 stage 可靠推导；调用参数 task/
      runtime/model 必须与推导结果完全一致，原 prompt SHA/workspace/stage 照绑；
    - process.json 原件只有 {pid, state}（Z 或有 started_at_utc）、没有 created 属
      正常：此时允许受信任宿主传入其当前核验的 child_created 独立身份留证
      （created_source='host_vouched_child_created' 记入 adopt_evidence；宿主必须
      核验真实 argv/工作区与该 PID 对应）。生产仍用真实只读探针验证：PID 现在
      必须 alive 且观测创建身份与留证身份完全一致；已退出/unknown/漂移一律拒绝；
      无任何 fake-alive 开关（prober 仅供离线测试注入，CLI 不暴露）；
    - 仅 ADOPTABLE_POOL_KEYS（GLM-5.3 / Qwen3.8-Max / Flash）；其它组合（CB/WB 等）
      拒绝且不触摸其 quota/并发锁；
    - Z4-D：确认的实际 worker PID+birth 绑定到 child_pid/child_created（绝不冒充
      未启动 wrapper），旧 worker 真实退出后 reconcile 即 reconciled_exit 释放容量
      （结束即空名额）；
    - 源文件内容哈希留证（attempts.adopt_evidence，仅哈希，绝不落 argv/凭据）；
    - 幂等/防双计：同 PID+创建身份的 adopted attempt 已在途 → 同 task 幂等返回、
      不同 task 入参拒绝（pid_already_adopted，源 task 与原 request 精确绑定）；
      同 task 有其它在途 attempt → duplicate_task_in_flight 拒绝；
    - Z4-B：同真实 workspace 已有别的 task 在途写入 → workspace_in_flight 拒绝；
    - 超上限也如实计入（老 worker 真实占着名额），随后新派发会因容量检查被阻止；
      绝不为符合上限丢弃/杀掉老 worker，也不清任何旧 quota/工作区锁。"""
    now = now or utcnow()
    probe = prober or (lambda pid: process_identity(int(pid)))
    rp, pp = Path(request_path), Path(process_path)
    try:
        request = json.loads(rp.read_text(encoding='utf-8'))
        request_sha = _file_sha256(rp)
    except (OSError, ValueError) as exc:
        return _adopt_reject('request_unreadable', task_id, None,
                             [f'cannot read/parse the original request file: '
                              f'{type(exc).__name__}'])
    try:
        proc = json.loads(pp.read_text(encoding='utf-8'))
        process_sha = _file_sha256(pp)
    except (OSError, ValueError) as exc:
        return _adopt_reject('process_receipt_unreadable', task_id, None,
                             [f'cannot read/parse the adjacent process receipt: '
                              f'{type(exc).__name__}'])
    if not isinstance(request, dict) or not isinstance(proc, dict):
        return _adopt_reject('receipt_not_object', task_id, None,
                             ['request.json / process.json must be JSON objects'])
    pid = proc.get('pid')
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return _adopt_reject('process_identity_missing', task_id, None,
                             ['process.json must carry a real positive integer pid'])
    receipt_created = proc.get('created')
    if receipt_created and str(receipt_created).strip():
        recorded_created, created_source = str(receipt_created), 'process_receipt'
    elif child_created and str(child_created).strip():
        # Z4-C：真实旧 process.json 原件没有 created——受信任宿主传入当前核验的
        # child_created 独立身份留证；下面仍用真实探针做 alive+birth 完全一致验证。
        recorded_created = str(child_created)
        created_source = 'host_vouched_child_created'
    else:
        return _adopt_reject('process_identity_missing', task_id, None,
                             ['process.json has no creation identity (real legacy '
                              'receipts only carry pid/state); the trusted host must '
                              'pass its currently-verified child_created — a worker is '
                              'never adopted on a bare pid'])
    ident = probe(pid) or {'state': 'unknown', 'created': None}
    if ident.get('state') == 'dead':
        return _adopt_reject('legacy_process_exited', task_id, None,
                             [f'pid {pid} has already exited; an exited legacy worker '
                              'is not adopted (nothing was touched)'])
    if ident.get('state') != 'alive':
        return _adopt_reject('legacy_alive_unknown', task_id, None,
                             [f'pid {pid} liveness could not be verified '
                              f'(state={ident.get("state")!r}); pending verification, '
                              'refused — never adopted on an unverifiable pid'])
    observed = ident.get('created')
    if not observed or str(observed) != str(recorded_created):
        return _adopt_reject('legacy_identity_drift', task_id, None,
                             [f'observed creation identity for pid {pid} does not match '
                              f'the vouched identity ({created_source}); possible pid '
                              'reuse or forged receipt; refused'])
    normalized, norm_reasons = _normalize_legacy_request(request)
    if normalized is None:
        return _adopt_reject('request_binding_incomplete', task_id, None, norm_reasons)
    if not request.get('workspace') or not request.get('prompt_sha256'):
        return _adopt_reject('request_binding_incomplete', task_id, None,
                             ['original request is missing workspace/prompt_sha256; '
                              'the attempt must be bound to the verified original '
                              'request facts'])
    if normalized['task_id'] != str(task_id):
        return _adopt_reject('request_task_mismatch', task_id, None,
                             [f"derived request task_id {normalized['task_id']!r} != "
                              f'caller task_id {task_id!r}'])
    if normalized['runtime'] != str(runtime) or normalized['model'] != str(model):
        return _adopt_reject('request_combo_mismatch', task_id,
                             pool_key(runtime, model),
                             ['caller runtime/model must match the verified original '
                              'request exactly; the attempt is bound to the original '
                              'combination, not to what the caller claims'])
    pk = pool_key(normalized['runtime'], normalized['model'])
    if pk not in ADOPTABLE_POOL_KEYS:
        # CB/WB 等未授权组合：拒绝且不触摸其 quota/并发锁。
        return _adopt_reject('adopt_pool_forbidden', task_id, pk,
                             [f'{pk} is not adoptable (only '
                              f'{"/".join(ADOPTABLE_POOL_KEYS)}); nothing was touched '
                              '— no quota or concurrency locks of that combo were '
                              'modified'])
    workspace = _norm_workspace(request['workspace'])
    evidence = json.dumps({'request_sha256': request_sha,
                           'process_sha256': process_sha,
                           'vouched_pid': int(pid),
                           'created_source': created_source}, sort_keys=True)
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            # Z4-D：同 PID+创建身份绝不双计——即使换 task 入参也拒绝，源 task 与原
            # request 的精确绑定优先。
            same_pid = conn.execute(
                "SELECT * FROM attempts WHERE origin='adopt-legacy' AND child_pid=? "
                'AND child_created=? AND state IN (%s) LIMIT 1'
                % ','.join('?' * len(ACTIVE_STATES)),
                (int(pid), str(recorded_created), *ACTIVE_STATES)).fetchone()
            if same_pid is not None:
                if same_pid['task_id'] == task_id:
                    conn.execute('ROLLBACK')
                    return {'adopted': True, 'idempotent': True,
                            'token': same_pid['token'], 'task_id': task_id,
                            'pool_key': same_pid['pool_key'],
                            'reason': 'already_adopted',
                            'reasons': ['the exact same pid+creation identity was '
                                        'already adopted for this task; not double '
                                        'counted']}
                conn.execute('ROLLBACK')
                return _adopt_reject(
                    'pid_already_adopted', task_id, pk,
                    [f'pid {pid} with this creation identity is already adopted by '
                     f'task {same_pid["task_id"]!r} (token={same_pid["token"]}); the '
                     'same live worker is never double counted even under different '
                     'task inputs — the source task and its original request stay '
                     'exactly bound'])
            dup = _active_task(conn, task_id)
            if dup is not None:
                conn.execute('ROLLBACK')
                return _adopt_reject('duplicate_task_in_flight', task_id, pk,
                                     [f'task {task_id!r} already has an in-flight '
                                      f'attempt (token={dup["token"]}, '
                                      f'state={dup["state"]})'])
            # Z4-B：同真实 workspace 已有别的 task 在途写入 → 拒绝（不做容量回收、
            # 不动业务占位）。
            busy = _workspace_conflict(conn, workspace, own_task_id=task_id)
            if busy is not None:
                conn.execute('ROLLBACK')
                return _adopt_reject('workspace_in_flight', task_id, pk,
                                     [f'workspace {busy["workspace"]!r} already has '
                                      f'an in-flight attempt from a different task '
                                      f'(token={busy["token"]}, '
                                      f'task={busy["task_id"]!r}); one real workspace '
                                      'never takes two concurrent writers'])
            tok = uuid.uuid4().hex
            # Z4-D：worker PID+birth 绑定 child_pid/child_created（真实执行器，结束即
            # reconciled_exit 释放），绝不放 wrapper_pid 冒充未启动 wrapper。
            conn.execute(
                'INSERT INTO attempts(token, task_id, stage, chat_id, prompt_sha256, '
                'workspace, runtime, model, pool_key, state, child_pid, child_created, '
                'reserved_at_utc, started_at_utc, origin, adopt_evidence) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (tok, task_id, normalized.get('stage'), normalized.get('chat_id'),
                 request.get('prompt_sha256'), workspace, normalized['runtime'],
                 normalized['model'], pk, 'running', int(pid), str(recorded_created),
                 _iso(now), _iso(now), 'adopt-legacy', evidence))
            if pk in MAIN_FORCE_KEYS:
                # 主力池如实计入持久轮转计数（1:1），老活 worker 不绕过也不被丢弃。
                _bump_rotation(conn, pk, now)
            conn.execute('COMMIT')
            return {'adopted': True, 'idempotent': False, 'token': tok,
                    'task_id': task_id, 'pool_key': pk, 'reason': None, 'reasons': [],
                    'evidence': {'request_sha256': request_sha,
                                 'process_sha256': process_sha,
                                 'created_source': created_source}}
        except Exception:
            conn.execute('ROLLBACK')
            raise


# ------------------------------------------------------------------ CLI
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description='brain-worker cross-chat dispatch capacity pool')
    ap.add_argument('--store', default=None,
                    help='dispatch pool sqlite path (default: env '
                         'BRAIN_WORKER_DISPATCH_STORE or ~/.brain-worker/dispatch-pool.sqlite3)')
    sub = ap.add_subparsers(dest='command', required=True)

    sub.add_parser('status', help='show pool capacity/active/free and Luna tickets')

    p = sub.add_parser('reserve', help='reserve an exact (runtime, model) claim')
    _add_claim_args(p)

    p = sub.add_parser('select-and-claim', help='atomic 1:1 route selection + claim')
    _add_claim_args(p)

    p = sub.add_parser('validate', help='validate a claim token against entry facts')
    p.add_argument('--token', required=True)
    p.add_argument('--task-id', dest='task_id', default=None)
    p.add_argument('--runtime', default=None)
    p.add_argument('--model', default=None)
    p.add_argument('--workspace', default=None)
    p.add_argument('--prompt-sha256', dest='prompt_sha256', default=None)
    p.add_argument('--stage', default=None)
    p.add_argument('--chat-id', dest='chat_id', default=None)

    p = sub.add_parser('bind-child', help='record the real child pid + creation identity')
    p.add_argument('--token', required=True)
    p.add_argument('--child-pid', dest='child_pid', type=int, required=True)
    p.add_argument('--wrapper-pid', dest='wrapper_pid', type=int, default=None,
                   help='current real caller (owner) pid; required when the attempt '
                        'already records a wrapper identity')

    p = sub.add_parser('finish', help='terminal-release a claim (real terminal verified)')
    p.add_argument('--token', required=True)
    p.add_argument('--terminal', default='finished',
                   choices=list(RELEASED_STATES))
    p.add_argument('--success', default=None, choices=['true', 'false'])
    p.add_argument('--wrapper-pid', dest='wrapper_pid', type=int, default=None,
                   help='current real caller (owner) pid; required to release a '
                        'no-child Popen-bind-window/unknown attempt via '
                        'start_failed/cancelled')
    p.add_argument('--wrapper-created', dest='wrapper_created', default=None,
                   help='creation identity of the caller pid (pid-reuse binding)')

    sub.add_parser('reconcile', help='reconcile in-flight attempts by pid liveness/identity')

    p = sub.add_parser('ask-record', help='record a real ask-the-user ticket (fixed 300s)')
    p.add_argument('--task-id', dest='task_id', required=True)
    p.add_argument('--scope', default=None,
                   help='JSON object binding task_id/stage/chat_id/workspace/prompt_sha256')
    p.add_argument('--ask-message-id', dest='ask_message_id', default=None)

    p = sub.add_parser('reply', help='record the user reply choice')
    p.add_argument('--task-id', dest='task_id', required=True)
    p.add_argument('--choice', required=True,
                   choices=['luna', 'external_agent', 'domestic', 'cancel'])
    p.add_argument('--note', default=None)

    p = sub.add_parser('claim-due', help='atomic due adjudication: domestic reclaim or Luna')
    p.add_argument('--task-id', dest='task_id', required=True)
    p.add_argument('--scope', default=None,
                   help='JSON object matching the original ticket scope (for reclaim)')

    p = sub.add_parser('mark-launch-unknown', help='record a non-idempotent launch_unknown')
    p.add_argument('--task-id', dest='task_id', required=True)

    p = sub.add_parser('record-agent-id', help='record the real native agent id')
    p.add_argument('--task-id', dest='task_id', required=True)
    p.add_argument('--agent-id', dest='agent_id', required=True)

    p = sub.add_parser('settle-native',
                       help='settle a native Luna terminal (trusted-host native '
                            'receipt only; binds claimed ticket + agent_id + scope)')
    p.add_argument('--task-id', dest='task_id', required=True)
    p.add_argument('--token', required=True,
                   help='the exact claimed_token returned by claim-due')
    p.add_argument('--agent-id', dest='agent_id', required=True,
                   help='the real agent_id recorded via record-agent-id')
    p.add_argument('--terminal', required=True,
                   choices=list(NATIVE_TERMINAL_STATES),
                   help='finished (success) / native_failed (failure); running/unknown '
                        'are refused — there is deliberately no force/allow-all flag')
    p.add_argument('--success', default=None, choices=['true', 'false'])
    p.add_argument('--scope', default=None,
                   help='JSON object matching the original ticket scope (drift refused)')
    p.add_argument('--receipt', default=None,
                   help='JSON object with source_tool + receipt_ref + sha256 (or '
                        'content) of the original native terminal receipt the trusted '
                        'host re-read; a bare terminal string is refused')

    p = sub.add_parser('cancel-pending', help='atomically cancel a pending aid ticket')
    p.add_argument('--task-id', dest='task_id', required=True)
    p.add_argument('--reason', default='domestic_slot_freed')

    p = sub.add_parser(
        'adopt-legacy',
        help='adopt a live legacy worker vouched by the trusted host (verified '
             'original request.json + adjacent process.json; real liveness and '
             'creation identity are checked — there is deliberately NO fake-alive '
             'flag)')
    p.add_argument('--task-id', dest='task_id', required=True)
    p.add_argument('--runtime', required=True)
    p.add_argument('--model', required=True)
    p.add_argument('--request-json', dest='request_json', required=True,
                   help='path to the original request.json provided by the host')
    p.add_argument('--process-json', dest='process_json', required=True,
                   help='path to the adjacent process.json {pid, created} receipt')
    p.add_argument('--child-created', dest='child_created', default=None,
                   help='trusted-host vouched creation identity for the pid, only '
                        'used when the real legacy process.json original carries no '
                        'created field; liveness + identity are still verified with '
                        'the real probe (no fake-alive)')
    return ap


def _add_claim_args(p):
    p.add_argument('--task-id', dest='task_id', required=True)
    p.add_argument('--runtime', required=True)
    p.add_argument('--model', required=True)
    p.add_argument('--workspace', required=True)
    p.add_argument('--prompt-sha256', dest='prompt_sha256', required=True)
    p.add_argument('--stage', default=None)
    p.add_argument('--chat-id', dest='chat_id', default=None)
    p.add_argument('--token', default=None)


def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    store = args.store
    now = utcnow()
    if args.command == 'status':
        out = status(store, now=now)
    elif args.command == 'reserve':
        out = reserve(store, task_id=args.task_id, runtime=args.runtime, model=args.model,
                      workspace=args.workspace, prompt_sha256=args.prompt_sha256,
                      stage=args.stage, chat_id=args.chat_id, token=args.token, now=now)
    elif args.command == 'select-and-claim':
        out = select_and_claim(store, task_id=args.task_id, runtime=args.runtime,
                               model=args.model, workspace=args.workspace,
                               prompt_sha256=args.prompt_sha256, stage=args.stage,
                               chat_id=args.chat_id, token=args.token, now=now)
    elif args.command == 'validate':
        out = validate_claim(store, args.token, task_id=args.task_id, runtime=args.runtime,
                             model=args.model, workspace=args.workspace,
                             prompt_sha256=args.prompt_sha256, stage=args.stage,
                             chat_id=args.chat_id)
    elif args.command == 'bind-child':
        out = bind_child(store, args.token, args.child_pid, wrapper_pid=args.wrapper_pid,
                         now=now)
    elif args.command == 'finish':
        success = None if args.success is None else (args.success == 'true')
        out = finish(store, args.token, terminal=args.terminal, success=success,
                     now=now, wrapper_pid=args.wrapper_pid,
                     wrapper_created=args.wrapper_created)
    elif args.command == 'reconcile':
        out = reconcile(store, now=now)
    elif args.command == 'ask-record':
        out = ask_record(store, task_id=args.task_id, scope=args.scope,
                         ask_message_id=args.ask_message_id, now=now)
    elif args.command == 'reply':
        out = reply(store, task_id=args.task_id, choice=args.choice, note=args.note, now=now)
    elif args.command == 'claim-due':
        out = claim_due(store, task_id=args.task_id, now=now, scope=args.scope)
    elif args.command == 'mark-launch-unknown':
        out = mark_launch_unknown(store, task_id=args.task_id, now=now)
    elif args.command == 'record-agent-id':
        out = record_agent_id(store, task_id=args.task_id, agent_id=args.agent_id, now=now)
    elif args.command == 'settle-native':
        success = None if args.success is None else (args.success == 'true')
        out = settle_native(store, task_id=args.task_id, token=args.token,
                            agent_id=args.agent_id, terminal=args.terminal,
                            success=success, scope=args.scope,
                            receipt=args.receipt, now=now)
    elif args.command == 'cancel-pending':
        out = cancel_pending(store, task_id=args.task_id, reason=args.reason, now=now)
    elif args.command == 'adopt-legacy':
        out = adopt_legacy(store, task_id=args.task_id, runtime=args.runtime,
                           model=args.model, request_path=args.request_json,
                           process_path=args.process_json, now=now,
                           child_created=args.child_created)
    else:  # pragma: no cover - argparse guards
        out = {'error': f'unknown command {args.command}'}
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    if isinstance(out, dict) and (out.get('allowed') is False
                                  or out.get('ok') is False
                                  or out.get('claimed') is False
                                  or out.get('recorded') is False):
        # 拒绝/未 claim 也返回 0，让主脑读取 JSON 决策；仅内部错误抛异常。
        return 0
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
