"""dispatch_mode — 牛马派发的**手动三档**共享策略（strong | medium | weak，标准库）。

BW-MANUAL-THREE-MODE-20261023（用户明确指令，当前权威）：
- 档位是**共享调度策略**，不是某个会话的私有偏好：浮窗手动切换后原子持久化，所有新的
  reserve / select-and-claim / consume / claim-due / stage_selection / 浮窗都读同一份；
- 切换**只影响之后的新裁决**：在途 attempt（reserved/running/unknown）继续按原状态自然
  结束，本模块绝不追杀、绝不假释放、绝不改派；
- 缺文件 / 畸形 / 读失败一律**安全回落 weak**，并把真实来源与读取错误如实回报（
  `source` / `error`），绝不把错误冒充成用户选过的 strong/medium。

三档新派池数 strong/medium/weak=5/4/10，槽数=6/5/13；物理登记仍为 16。
strong：Sonus(1) > ZCode(2) > Claude(3) > 两内置 Max(4)，全部无时段门。
medium：两内置 Max(1) > Claude(2) > ZCode(3)；weak：CN 内置 Max(1) > ZCode(2) >
Claude(3) > 国际 Max(4) > CodeBuddy DeepSeek(5) > 两 OpenCode(6) > CodeBuddy GLM(7) >
两 Flash(8)。medium/weak 保留 Max 夜间门与 Claude 高峰门。
2026-10-28 用户要求禁用 qodercn:Qwen-3.8-Max 新派；登记、历史和精确遗留 claim 保留。
历史「CN 自定义 Max 恢复/三路 Max」的新派口径已被本次禁用覆盖。

BW-SONUS-REENABLE-20261028（用户明确指令，当前权威）：**恢复启用** `qoder:Sonus`
的新派发——`SONUS_TEMP_DISABLED = False`，完整回到 BW-STRONG-SONUS-20261024 三档常态
口径（strong 专属、层=1 全档第一、独立 1 槽、无时段门；medium/weak 显式 Sonus 一律
mode_excluded）。

BW-SONUS-TEMP-DISABLE-20261026（历史，已被 BW-SONUS-REENABLE-20261028 恢复覆盖，不再
生效）：曾按用户明确指令**暂时禁用** `qoder:Sonus` 的新派发。`SONUS_TEMP_DISABLED = True`
期间，Sonus 从**所有档位**（含 strong）的可新派集合中排除：AUTO 不选它；显式 reserve /
select-and-claim / consume / claim-due / 正式 qoder 入口一律 mode_excluded（sent=false、
零新占位），拒绝文案明确写「用户要求的临时禁用」——绝不伪装成额度、认证、时段或永久
退役。禁用期间 Sonus 的容量登记、模型字面量、优先级表历史值、宿主递归排除、物理总容量
16 与历史 attempt 全部保留；把 `SONUS_TEMP_DISABLED` 改回 False 即恢复原三档口径（本
次恢复即如此操作）。禁用期间遗留的 reserved/running/unknown Sonus 不追杀、不假释放。

BW-STRONG-SONUS-20261024（当前权威口径，2026-10-28 起重新生效）：`qoder:Sonus` 是国际 Qoder CLI
`--list-models` 只读核验过的精确字面模型名（绝不改写/猜 ID），新增为 strong 专属的
独立 1 槽正常池——strong 新派 Sonus 全档第一；medium/weak 显式 Sonus 一律 mode_excluded
（sent=false、零新占位、绝不静默换模型）；Sonus 无时段门、不纳入任何 Max 限额/夜间门；
已知正常池（绝非 legacy/未知池），已存在的在途 Sonus 切档不追杀、不假释放。

本模块只做**策略事实**（档位、优先级表、可派集合、时段门）与状态文件读写：不起进程、
不联网、不读凭据、不写任何调度库。dispatch_pool 是唯一消费方，浮窗只读展示。
"""
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

MODES = ('strong', 'medium', 'weak')
DEFAULT_MODE = 'weak'
MODE_ENV_VAR = 'BRAIN_WORKER_DISPATCH_MODE_FILE'
MODE_LABELS = {'strong': '强', 'medium': '中', 'weak': '弱'}
MODE_NOTES = {
    'strong': '强档 · Sonus／ZCode／Claude／两内置 Max · 全部无时段门',
    'medium': '中档 · 仅 ZCode／Claude／两内置 Max · Max 夜间门 + Claude 高峰门',
    'weak': '弱档 · 10 个可新派池 · Max 夜间门 + Claude 高峰门',
}
# 每档可新派范围的纯文字描述（回执/浮窗文案用，绝不承载数字层号/容量，防漂移）。
MODE_SCOPE_TEXT = {
    'strong': 'Sonus／ZCode／Claude／两内置 Qwen3.8-Max（5 池）',
    'medium': 'ZCode／Claude／两内置 Qwen3.8-Max（4 池）',
    'weak': '10 个可新派池（不含 Sonus 与 CN token plan Max）',
}

# ------------------------------------------------------------------ 池名（权威字面量）
# 与 dispatch_pool 的 CAPACITY 键一一对应；本模块刻意不 import dispatch_pool，避免循环
# 依赖（dispatch_pool 反过来 import 本模块）。键名漂移由 dispatch_pool 的启动自检与测试
# `test_dispatch_mode.py` 共同兜住。
ZCODE_POOL_KEY = 'zcode:GLM-5.3'
CLAUDE_POOL_KEY = 'claude:deepseek-flash'
QMAX_INTL_POOL_KEY = 'qoder:Qwen3.8-Max'
QMAX_CN_POOL_KEY = 'qodercn:Qwen3.8-Max'
QMAX_CN_CUSTOM_POOL_KEY = 'qodercn:Qwen-3.8-Max'
USER_DISABLED_POOL_KEYS = frozenset((QMAX_CN_CUSTOM_POOL_KEY,))
CODEBUDDY_DEEPSEEK_POOL_KEY = 'codebuddy:deepseek-v4.1-flash'
CODEBUDDY_GLM_POOL_KEY = 'codebuddy:glm-5.3-flash'
OPENCODE_DEEPSEEK_POOL_KEY = 'opencode:deepseek-v4.1-flash-free'
OPENCODE_GLM_POOL_KEY = 'opencode:glm-5.3-flash-free'
QFLASH_INTL_POOL_KEY = 'qoder:Qwen3.8-Flash'
QFLASH_CN_POOL_KEY = 'qodercn:Qwen3.8-Flash'
# BW-STRONG-SONUS-20261024：国际 Qoder / Sonus 的精确池字面量（runtime qoder + CLI
# --list-models 核验字面名 `Sonus`）。strong 专属独立 1 槽；绝不能归入 MAX_POOL_KEYS
# （不套任何夜间门）、不进 weak 全候选、也不是 legacy/未知池。
SONUS_POOL_KEY = 'qoder:Sonus'
# BW-SONUS-TEMP-DISABLE-20261026 唯一权威开关（BW-SONUS-REENABLE-20261028 起 False）：
# True = 所有档位的可新派集合剔除 Sonus（复用 mode_excluded 硬门，文案明确为用户要求
# 的临时禁用，相关特殊分支只在 True 时生效）；False = 完整恢复 BW-STRONG-SONUS-20261024
# 三档口径（strong 专属、层=1、独立 1 槽、无时段门）。除本开关的读取外，Sonus 的一切
# 登记/字面量/层号历史/容量/宿主递归排除保持原样。
SONUS_TEMP_DISABLED = False

# 三个 Qwen3.8-Max（CN 自定义 + CN 内置 + 国际内置）。strong 全不设门；medium/weak 全部门。
# Sonus 不在其中：它不是 Qwen3.8-Max，任何 Max 夜间门/限额标记都不得连坐到 Sonus。
MAX_POOL_KEYS = (QMAX_CN_CUSTOM_POOL_KEY, QMAX_CN_POOL_KEY, QMAX_INTL_POOL_KEY)
# strong/medium 共用的五池**硬**可派集合（strong 在此之上再加 Sonus）。
NARROW_POOL_KEYS = (ZCODE_POOL_KEY, CLAUDE_POOL_KEY,
                    QMAX_INTL_POOL_KEY, QMAX_CN_POOL_KEY, QMAX_CN_CUSTOM_POOL_KEY)
# BW-STRONG-SONUS-20261024：strong 的完整可派集合 = Sonus 第一 + 原五池。
STRONG_POOL_KEYS = (SONUS_POOL_KEY,) + NARROW_POOL_KEYS
# strong 专属、medium/weak 排除的池（当前只有 Sonus）。供一致性自检与浮窗「本档不参与」
# 判定使用；这些池必须在 dispatch_pool.CAPACITY 里真实登记（tests/test_dispatch_mode.py
# 断言），否则就是键名漂移。
STRONG_ONLY_POOL_KEYS = (SONUS_POOL_KEY,)
# weak 的完整可派集合（与浮窗 11 行一致；Sonus 不属于 weak 全候选）。
ALL_POOL_KEYS = (QMAX_CN_CUSTOM_POOL_KEY, QMAX_CN_POOL_KEY, ZCODE_POOL_KEY,
                 CLAUDE_POOL_KEY, QMAX_INTL_POOL_KEY, CODEBUDDY_DEEPSEEK_POOL_KEY,
                 OPENCODE_DEEPSEEK_POOL_KEY, OPENCODE_GLM_POOL_KEY,
                 CODEBUDDY_GLM_POOL_KEY, QFLASH_INTL_POOL_KEY, QFLASH_CN_POOL_KEY)
# 档位策略管辖的全部已知池（weak 全表 + strong 专属池）：浮窗据此判断一行是否属于
# 「本档不参与」语义；集合外的行（如已禁用的 Luna）不属于任何档位，绝不冒充排除标记。
KNOWN_POLICY_POOL_KEYS = ALL_POOL_KEYS + STRONG_ONLY_POOL_KEYS

# ------------------------------------------------------------------ 优先级表
# 数字越小优先级越高；层内规则（真实在途少者优先、CN Max 自定义子优先、平票 stable
# tie）仍由 dispatch_pool._select_main_force 统一实现，本模块只给层号。
STRONG_PRIORITY = {
    SONUS_POOL_KEY: 1,
    ZCODE_POOL_KEY: 2,
    CLAUDE_POOL_KEY: 3,
    QMAX_INTL_POOL_KEY: 4,
    QMAX_CN_POOL_KEY: 4,
    QMAX_CN_CUSTOM_POOL_KEY: 4,
}
MEDIUM_PRIORITY = {
    QMAX_CN_CUSTOM_POOL_KEY: 1,
    QMAX_CN_POOL_KEY: 1,
    QMAX_INTL_POOL_KEY: 1,
    CLAUDE_POOL_KEY: 2,
    ZCODE_POOL_KEY: 3,
}
# weak = 上一阶段已验收的当前整表（BW-CLAUDE-ALLDAY-QMAX-AFTER-20261023 层序）。
# dispatch_pool.POOL_PRIORITY 仍是本表的兼容别名（同一份数值），不另存一份漂移副本。
WEAK_PRIORITY = {
    QMAX_CN_CUSTOM_POOL_KEY: 1,
    QMAX_CN_POOL_KEY: 1,
    ZCODE_POOL_KEY: 2,
    CLAUDE_POOL_KEY: 3,
    QMAX_INTL_POOL_KEY: 4,
    CODEBUDDY_DEEPSEEK_POOL_KEY: 5,
    OPENCODE_DEEPSEEK_POOL_KEY: 6,
    OPENCODE_GLM_POOL_KEY: 6,
    CODEBUDDY_GLM_POOL_KEY: 7,
    QFLASH_INTL_POOL_KEY: 8,
    QFLASH_CN_POOL_KEY: 8,
}
PRIORITY_BY_MODE = {'strong': STRONG_PRIORITY, 'medium': MEDIUM_PRIORITY,
                    'weak': WEAK_PRIORITY}

# ------------------------------------------------------------------ 时段门（北京时间）
BEIJING_UTC_OFFSET = timedelta(hours=8)
NIGHT_WINDOW_START_HOUR = 22  # 22:00 含
NIGHT_WINDOW_END_HOUR = 8     # 08:00 不含


def beijing_hour(now) -> int:
    """注入的 now 先归一到 UTC，再按固定 +08:00 折算北京时间小时（0..23）。
    naive 按 UTC 理解，绝不读取宿主本地时区。"""
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        base = now.replace(tzinfo=timezone.utc)
    else:
        base = now.astimezone(timezone.utc)
    return (base + BEIJING_UTC_OFFSET).hour


def night_window_open(now) -> bool:
    """北京时间 22:00（含）–次日 08:00（不含）→ True。"""
    h = beijing_hour(now)
    return h >= NIGHT_WINDOW_START_HOUR or h < NIGHT_WINDOW_END_HOUR


def _claude_peak_open(now) -> bool:
    """Claude 既有工作日高峰门（medium/weak 恢复；strong 不调用）：复用
    deepseek_offpeak 的纯时间判定（工作日 09:00–12:00 / 14:00–18:00 关闭，周末/法定
    假日/离峰放行）。依赖加载/运行异常一律 **fail-closed**（当作窗口关闭），
    绝不 fail-open 把故障伪装成可派时段（BW-MANUAL-THREE-MODE-CLOSEOUT-20261023）。"""
    try:
        import deepseek_offpeak as offpeak
    except ImportError:
        return False
    try:
        return bool(offpeak.can_dispatch(now))
    except Exception:  # noqa: BLE001 - 判定失败按窗口关闭处理（fail-closed）
        return False


def normalize_mode(mode) -> str:
    """把任意输入归一成合法档位；未知/空一律回落 DEFAULT_MODE（weak）。"""
    if isinstance(mode, str):
        value = mode.strip().lower()
        if value in MODES:
            return value
    return DEFAULT_MODE


# ------------------------------------------------------------------ 状态文件
def default_mode_path() -> Path:
    override = os.environ.get(MODE_ENV_VAR)
    if override:
        return Path(override)
    return Path.home() / '.brain-worker' / 'dispatch-mode.json'


def resolve_path(path=None) -> Path:
    return Path(path) if path else default_mode_path()


def load_mode(path=None) -> dict:
    """只读加载当前档位。永不抛异常、永不建文件。

    返回 {mode, source, path, error, raw_mode, default_mode}：
    - source='file'：读到合法档位；
    - source='default'：文件不存在 → 回落 weak；
    - source='error_fallback'：读失败/畸形/非法值 → 回落 weak，error 为固定短码
      （unreadable / malformed_json / not_an_object / invalid_mode），**绝不回显文件
      正文**，也绝不把错误冒充成 strong。"""
    p = resolve_path(path)
    record = {'mode': DEFAULT_MODE, 'source': 'default', 'path': str(p),
              'error': None, 'raw_mode': None, 'default_mode': DEFAULT_MODE}
    try:
        raw = p.read_text(encoding='utf-8')
    except FileNotFoundError:
        return record
    except OSError:
        record.update(source='error_fallback', error='unreadable')
        return record
    except UnicodeDecodeError:
        record.update(source='error_fallback', error='malformed_json')
        return record
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        record.update(source='error_fallback', error='malformed_json')
        return record
    if not isinstance(data, dict):
        record.update(source='error_fallback', error='not_an_object')
        return record
    raw_mode = data.get('mode')
    record['raw_mode'] = raw_mode if isinstance(raw_mode, str) else None
    if not isinstance(raw_mode, str) or raw_mode.strip().lower() not in MODES:
        record.update(source='error_fallback', error='invalid_mode')
        return record
    record.update(mode=raw_mode.strip().lower(), source='file')
    return record


def save_mode(mode, path=None) -> dict:
    """原子保存档位（同目录临时文件 + os.replace，绝不半写）。

    非法档位直接 ValueError（绝不静默写成 weak 冒充用户选择）。返回与 load_mode 同形的
    记录，source='file'。"""
    value = mode.strip().lower() if isinstance(mode, str) else mode
    if value not in MODES:
        raise ValueError(f'dispatch mode must be one of {MODES}, got {mode!r}')
    p = resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {'mode': value,
               'updated_at_utc': datetime.now(timezone.utc).isoformat()}
    fd, tmp_name = tempfile.mkstemp(prefix='.dispatch-mode-', suffix='.tmp',
                                    dir=str(p.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, ensure_ascii=False)
            fh.write('\n')
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, p)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return {'mode': value, 'source': 'file', 'path': str(p), 'error': None,
            'raw_mode': value, 'default_mode': DEFAULT_MODE}


def current_mode(path=None) -> str:
    """每次调用都重新读文件（多进程切换后下一次裁决立即可见，绝不在 import 时缓存）。"""
    return load_mode(path)['mode']


# ------------------------------------------------------------------ 策略查询
def allowed_pools_for_mode(mode=None) -> tuple:
    """允许新派的集合；用户禁用只过滤新派，不删除物理登记。"""
    m = normalize_mode(mode) if mode is not None else current_mode()
    if m == 'strong':
        pools = STRONG_POOL_KEYS
    elif m == 'medium':
        pools = NARROW_POOL_KEYS
    else:
        pools = ALL_POOL_KEYS
    if SONUS_TEMP_DISABLED:
        pools = tuple(pk for pk in pools if pk != SONUS_POOL_KEY)
    return tuple(pk for pk in pools if pk not in USER_DISABLED_POOL_KEYS)


def priority_map_for_mode(mode=None) -> dict:
    """该档位的层号表（副本，调用方可安全持有）。"""
    m = normalize_mode(mode) if mode is not None else current_mode()
    return dict(PRIORITY_BY_MODE[m])


def priority_for_pool(pk, mode=None):
    return priority_map_for_mode(mode).get(pk)


def pool_allowed(pk, mode=None) -> bool:
    return pk in allowed_pools_for_mode(mode)


def window_gated_pools_for_mode(mode=None) -> tuple:
    """该档位受时段门约束的池（strong 为空；medium/weak 为三个 Max + Claude）。"""
    m = normalize_mode(mode) if mode is not None else current_mode()
    if m == 'strong':
        return ()
    return tuple(MAX_POOL_KEYS) + (CLAUDE_POOL_KEY,)


def window_blocks_for_mode(pk, now, mode=None) -> bool:
    """该池在该档位、该时刻是否被时段门挡下（只影响**新派发**）。

    - strong：六个可派池全部无门 → 恒 False（Sonus 无任何门，Claude 不套高峰门，
      三个 Max 不套夜间门）；
    - medium/weak：三个 Max 受北京 22:00–08:00 夜间门；Claude 受既有工作日高峰门；
      其它池（Sonus / ZCode / CodeBuddy / OpenCode / 两 Flash）保持无时段门——Sonus 在
      medium/weak 是 mode_excluded 排除，时段门永不先于档位硬策略生效。"""
    m = normalize_mode(mode) if mode is not None else current_mode()
    if m == 'strong':
        return False
    if pk in MAX_POOL_KEYS:
        return not night_window_open(now)
    if pk == CLAUDE_POOL_KEY:
        return not _claude_peak_open(now)
    return False


def window_closed_reason(pk, now=None, mode=None) -> str:
    """人类可读的时段门原因（只用于回执/浮窗，不含任何秘密）。"""
    m = normalize_mode(mode) if mode is not None else current_mode()
    if pk in MAX_POOL_KEYS:
        return (f'{pk!r} is a Qwen3.8-Max main force that the {m} dispatch mode only allows '
                'to be newly dispatched during the Beijing-time window 22:00 (inclusive) '
                'to 08:00 (exclusive); it is outside that window now, so a new dispatch is '
                'honestly refused and never silently swapped to another model')
    if pk == CLAUDE_POOL_KEY:
        return (f'{pk!r} keeps its existing weekday peak gate in the {m} dispatch mode '
                '(Beijing time 09:00-12:00 and 14:00-18:00 on working days are closed; '
                'weekends, statutory holidays and off-peak hours stay open); a new '
                'dispatch is refused rather than silently swapped to another model')
    return f'{pk!r} is blocked by its dispatch time window in the {m} dispatch mode'


def mode_excluded_reason(pk, mode=None) -> str:
    m = normalize_mode(mode) if mode is not None else current_mode()
    if pk in USER_DISABLED_POOL_KEYS:
        return (f'{pk!r} 用户要求禁用：Qoder CN／token plan／Qwen-3.8-Max '
                '在所有档位禁止新派发（mode_excluded，sent=false、零新占位、不改派）；'
                '不是额度、认证、时段、容量问题或永久退役。容量登记和历史保留，'
                '遗留在途不追杀、不假释放；禁用前合法取得且仍 reserved 的精确 claim '
                '可沿既有校验和生命周期启动、结束。')
    if pk == SONUS_POOL_KEY and SONUS_TEMP_DISABLED:
        # BW-SONUS-TEMP-DISABLE-20261026：用户明确要求的**临时禁用**——如实写明缘由，
        # 绝不伪装成额度、认证、时段或永久退役；回执仍是 mode_excluded（sent=false、
        # 零新占位、绝不静默换模型），在途 Sonus 不追杀、不假释放。恢复 = 把开关改回
        # False（容量/字面量/层号历史/物理总容量 16 从未删除）。
        return (f'{pk!r} is temporarily disabled from new dispatch in ALL modes by an '
                'explicit user instruction (a reversible temporary disable, not a '
                'quota/auth/time-window issue and not a permanent retirement); the '
                'request is refused as mode_excluded with sent=false and zero new '
                'placeholders, never silently swapped to another model. In-flight '
                'attempts of this pool are untouched and keep running to their natural '
                'end; its capacity registration and history are fully preserved for '
                're-enable')
    return (f'{pk!r} is not part of the {m} dispatch mode: this mode only allows new '
            f'dispatch to {MODE_SCOPE_TEXT[m]}, so the request is '
            'refused as mode_excluded with sent=false and zero new placeholders. In-flight '
            'attempts of this pool are untouched and keep running to their natural end')
