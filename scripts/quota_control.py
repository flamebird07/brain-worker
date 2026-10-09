"""quota_control — brain-worker 持久额度冷却与路由门禁（标准库 sqlite3）。

BW-QUOTA-20261008-S1 首版 + S2 修复（详细口径见随包 references/quota-routing.md）：
- Windows 进程存活查询改为只读 ctypes 探针，绝不 os.kill（S1 缺陷 A）；
- 通道/额度组并发占位：同一 quota_group 同时只允许一个派发在途（S1 缺陷 B）；
- 占位生命周期绑定 attempt/nonce：终态识别→冷却落库→占位释放原子完成，
  无“429 已发生但冷却未落库”的二次派发空窗；终态未知保留占位（S1 缺陷 C）；
- recovery probe 程序化强制有界、无副作用（禁写工具/resume/大提示词/plan，
  显式时限），非 429 失败/取消/超时结算 probe 占位但不写 healthy（S1 缺陷 D）；
- record_success 带 epoch/attempt 核验：迟到成功不能清除更新的限流（S1 缺陷 E）；
- 结构化 Retry-After 真正进入分类，并与 reset 窗口取最严格服务端下限（缺陷 F）；
- 未匹配通道保守考虑同 runtime 已知受限组，路径别名/复制 config 不能绕过（G）；
- release-lock 人工路径绑定原 owner pid + 显式查证确认，绝不释放活执行器；
  CLI 参数顺序修正（--store 在子命令之前或之内均可）（S1 缺陷 H）。

持久状态库（sqlite3，默认 `~/.brain-worker/quota-state.sqlite3`，环境变量
BRAIN_WORKER_QUOTA_STORE 覆盖；测试必须传显式临时 store，绝不写真实状态）：
- cooldowns：按 quota_group 记录冷却截止（UTC ISO）与状态
  cooling / recovery_unverified。冷却只被更晚的事件延长（单调）；epoch 单调递增，
  成功清除必须匹配派发时观察到的 epoch；到期只转为 recovery_unverified，
  不自动判恢复。probe_active/probe_attempt 绑定在飞的单次核验。
- workspace_locks：同一真实工作区（realpath）单一写入执行器。
- channel_locks：同一 quota_group（受限通道）同时只有一个派发/probe 在途；
  独立组不同工作区可并行。两张占位表都在同一 BEGIN IMMEDIATE 事务内与冷却检查
  原子完成；活/未知 owner 不按时间过期抢占。

额度分组（quota-routes.json，受信任本机配置，worker 的任意 plan 不能自报组）：
真实通道以运行时入口/CLI/provider 等套餐不敏感标识映射到 quota_group，关系来源必须
user_confirmed；未匹配到路由的通道归入共享组 unknown-shared，且若同 runtime 的任何
已知组仍在冷却，未匹配通道保守阻断（不能以新 unknown 组跳过已知冷却）。不读凭据。

429 分类（复用 execution_control.explicit_429 / extract_reset_hint，不另造口径）：
- 明确 reset 窗口与结构化 Retry-After 同时存在时取**更晚**（最严格服务端下限），
  两者原文/时刻都保留；
- 无窗口、非 quota 类临时 429 → 有上限指数退避 + jitter；
- quota 类（errors_info.category=quota）但无任何窗口 → 保守长冷却
  QUOTA_NO_WINDOW_HOURS，不猜恢复、不重派完整任务；
- 非 429（权限错误等）/正文偶然 429 数字一律不产生冷却。

退出语义：所有门禁拒绝都返回 {'allowed': False, 'sent': False, reasons, ...}，
调用方在 Popen/建目录之前退出 2、零输出目录。
"""
import argparse
import json
import os
import random
import re
import sqlite3
import sys
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
import execution_control as ec  # 只读复用 explicit_429 / extract_reset_hint 口径

DEFAULT_ROUTES_PATH = _SCRIPTS_DIR / 'quota-routes.json'
# 未匹配路由的通道共享的保守组：未知关系不可默认独立。
UNKNOWN_SHARED_GROUP = 'unknown-shared'
# 临时 429 的有上限指数退避（秒）：base * 2^attempts，上限 CAP，jitter 0-25%。
TEMP_BACKOFF_BASE_SECONDS = 30
TEMP_BACKOFF_CAP_SECONDS = 1800
TEMP_BACKOFF_JITTER = 0.25
# quota 类 429 但完全没有窗口：保守长冷却，不猜恢复。
QUOTA_NO_WINDOW_HOURS = 24
# recovery probe 的程序化边界（缺陷 D）：禁写工具、禁 resume、提示词有界、
# 显式时限；probe 不是完整任务的补票通道。
PROBE_FORBIDDEN_TOOLS = ('Write', 'Edit', 'Bash')
PROBE_PROMPT_MAX_BYTES = 2048
PROBE_TIMEOUT_MIN_SECONDS = 5
PROBE_TIMEOUT_MAX_SECONDS = 600
PROBE_DEFAULT_TIMEOUT_SECONDS = 120
# 带时区服务器 reset 时间，如 "2026-10-08 18:14:13 UTC+8"。无时区后缀不猜。
_RESET_DT_RE = re.compile(
    r'(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2}):(\d{2})'
    r'\s*(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?\b')
QUOTA_CATEGORIES = ('quota',)
KNOWN_KINDS = ('quota_reset', 'retry_after', 'quota_no_window', 'temporary_backoff')
# 结构化 Retry-After 字段：只认错误结构 dict 字段（含嵌套 headers），不扫正文。
_RETRY_AFTER_ITEM_KEYS = ('retry_after', 'Retry-After', 'retryAfter')
_RETRY_AFTER_HEADER_KEYS = ('Retry-After', 'retry-after')

# BW-ZCODE-MANUAL-QUOTA-20261008-S1 用户政策：ZCode 额度由用户手动管理/重置，取消
# 自动额度冷却。固定策略按真实 runtime 判定，只有 'zcode' 生效；CodeBuddy 及其它
# runtime 的额度冷却规则完全不变，也不提供可被任务书随意关闭的开关。
# 注意：这里取消的只是"额度冷却门禁"——gate_dispatch/settle_attempt/import_terminal
# 对 ZCode 不再据 cooldowns 表放行或写冷却，也绝不因 ZCode 触碰同组（如 unknown-shared）
# 里 CodeBuddy 的冷却；但工作区单写入与通道单在途的并发占位对 ZCode 仍然保留（跨 CB/Z
# 同一真实工作区仍串行、同组跨工作区仍单在途、活/未知 owner 不被抢占）。低层 API
# （record_quota_event / _record_cooldown_tx / classify_quota_failure）不受影响，继续
# 兼容 CodeBuddy 旧规则。
AUTO_QUOTA_COOLDOWN_DISABLED_RUNTIMES = ('zcode',)

# BW-AVAILABILITY-20261009-B2：ZCode 独立持久 availability/anti-repeat 状态（绝不复活
# 旧 cooldowns 表）。真实 BigModel/Zhipu Coding Plan 渠道的 1308/1310 是硬额度事实；
# 同一数字来自其它 provider 不泛化。窄的硬上限消息语义（服务器自己的文本）优先于
# wrapper 派生的 rate_limit 标签——category=rate_limit 是旧 wrapper 只认识 1308 时
# 派生的字段，不是服务器权威口径。
ZCODE_HARD_QUOTA_PROVIDER_CODES = (1308, 1310)
_TRUSTED_HARD_QUOTA_PROVIDER_RE = re.compile(r'bigmodel|zhipu', re.IGNORECASE)
# BW-AVAILABILITY-20261009-B4 缺陷 2：硬上限消息语义只认服务器自己的周期性/套餐额度耗尽
# 文本（使用上限 / 用量上限 / 额度用尽/耗尽/达到 / quota|usage|plan 明确耗尽），绝不把普通
# 限流/频率上限（rate limit / 每分钟请求上限 / too many requests / limit reached|exceeded）
# 升格为硬 hold——现场事故里 wrapper 派生的 category=rate_limit 只是旧字段，不是服务器权威。
_HARD_LIMIT_MESSAGE_RE = re.compile(
    r'(使用上限|用量上限|额度已?(用尽|耗尽|达到)|配额已?(用尽|耗尽|达到)'
    r'|周期额度上限|quota (?:has been )?(?:exhausted|used up|drained)'
    r'|(?:usage|plan) quota limit)', re.IGNORECASE)
ZCODE_AVAILABILITY_STATES = ('backoff', 'hard_hold', 'recovery_unverified', 'healthy')
# 探测失败/超时/取消后的再探测退避（秒）：base * 2^probe_failures，上限 CAP。
ZCODE_PROBE_RETRY_BASE_SECONDS = 60
ZCODE_PROBE_RETRY_CAP_SECONDS = 3600
# ZCode availability 分类 kind（与旧 cooldowns 的 KNOWN_KINDS 完全分离）。
ZCODE_AVAILABILITY_KINDS = ('hard_hold', 'hard_reset_window', 'backoff_until_window',
                            'temporary_backoff')


def _auto_quota_cooldown_disabled(runtime) -> bool:
    """真实 runtime 是否取消自动额度冷却（仅 ZCode）。固定策略，不可配置。"""
    return runtime in AUTO_QUOTA_COOLDOWN_DISABLED_RUNTIMES


def is_trusted_hard_quota_provider(provider) -> bool:
    """1308/1310 硬额度码只限真实 BigModel/Zhipu 渠道；其它 provider 不泛化。"""
    return bool(provider) and bool(
        _TRUSTED_HARD_QUOTA_PROVIDER_RE.search(str(provider)))


def hard_limit_message(text) -> bool:
    """窄的硬上限消息语义（服务器文本），优先于 rate_limit 标签。普通限流文本
    （too many requests / rate limited / 频率限制）绝不命中。"""
    return bool(text) and isinstance(text, str) and bool(
        _HARD_LIMIT_MESSAGE_RE.search(text))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cooldowns(
  quota_group TEXT PRIMARY KEY,
  state TEXT NOT NULL CHECK(state IN ('cooling','recovery_unverified')),
  cooldown_until_utc TEXT NOT NULL,
  reason TEXT,
  source TEXT,
  recorded_at_utc TEXT,
  backoff_attempts INTEGER NOT NULL DEFAULT 0,
  probe_active INTEGER NOT NULL DEFAULT 0,
  last_probe_at_utc TEXT,
  epoch INTEGER NOT NULL DEFAULT 0,
  probe_attempt TEXT);
CREATE TABLE IF NOT EXISTS workspace_locks(
  workspace TEXT PRIMARY KEY,
  quota_group TEXT NOT NULL,
  runtime TEXT NOT NULL,
  pid INTEGER NOT NULL,
  host TEXT,
  purpose TEXT,
  acquired_at_utc TEXT NOT NULL,
  attempt TEXT NOT NULL DEFAULT '',
  note TEXT);
CREATE TABLE IF NOT EXISTS channel_locks(
  quota_group TEXT PRIMARY KEY,
  runtime TEXT NOT NULL,
  pid INTEGER NOT NULL,
  host TEXT,
  purpose TEXT,
  workspace TEXT NOT NULL,
  acquired_at_utc TEXT NOT NULL,
  attempt TEXT NOT NULL DEFAULT '',
  note TEXT);
CREATE TABLE IF NOT EXISTS zcode_availability(
  channel_key TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  quota_group TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('backoff','hard_hold','recovery_unverified','healthy')),
  epoch INTEGER NOT NULL DEFAULT 0,
  kind TEXT,
  blocked_until_utc TEXT,
  probe_active INTEGER NOT NULL DEFAULT 0,
  probe_attempt TEXT,
  probe_failures INTEGER NOT NULL DEFAULT 0,
  probe_blocked_until_utc TEXT,
  backoff_attempts INTEGER NOT NULL DEFAULT 0,
  last_provider_code INTEGER,
  last_reason TEXT,
  reset_hint_utc TEXT,
  evidence_path TEXT,
  evidence_sha256 TEXT,
  manual_reset_at_utc TEXT,
  manual_reset_evidence TEXT,
  manual_reset_epoch INTEGER,
  recovery_eligibility_consumed INTEGER NOT NULL DEFAULT 0,
  recorded_at_utc TEXT,
  updated_at_utc TEXT);
"""

# 旧库（S1 结构）迁移：CREATE TABLE IF NOT EXISTS 不会给已有表补列。
_MIGRATIONS = (
    ('cooldowns', 'epoch',
     'ALTER TABLE cooldowns ADD COLUMN epoch INTEGER NOT NULL DEFAULT 0'),
    ('cooldowns', 'probe_attempt',
     'ALTER TABLE cooldowns ADD COLUMN probe_attempt TEXT'),
    ('workspace_locks', 'attempt',
     'ALTER TABLE workspace_locks ADD COLUMN attempt TEXT NOT NULL DEFAULT \'\''),
    # BW-AVAILABILITY-20261009-B4 缺陷 3：一次 recovery 资格持久消费 + 人工重置绑定
    # 失败代际（防旧事件重放给新失败重造资格）。
    ('zcode_availability', 'manual_reset_epoch',
     'ALTER TABLE zcode_availability ADD COLUMN manual_reset_epoch INTEGER'),
    ('zcode_availability', 'recovery_eligibility_consumed',
     'ALTER TABLE zcode_availability ADD COLUMN recovery_eligibility_consumed '
     'INTEGER NOT NULL DEFAULT 0'),
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def default_store_path() -> Path:
    override = os.environ.get('BRAIN_WORKER_QUOTA_STORE')
    if override:
        return Path(override)
    return Path.home() / '.brain-worker' / 'quota-state.sqlite3'


def default_routes_path() -> Path:
    override = os.environ.get('BRAIN_WORKER_QUOTA_ROUTES')
    return Path(override) if override else DEFAULT_ROUTES_PATH


def _connect(store_path) -> sqlite3.Connection:
    path = Path(store_path)
    if str(path) != ':memory:':
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(_SCHEMA)
    for table, column, ddl in _MIGRATIONS:
        cols = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
        if column not in cols:
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass  # 并发迁移竞态：另一连接已补列
    return conn


def _open_readonly(store_path) -> sqlite3.Connection:
    """BW-AVAILABILITY-20261009-B4 缺陷 5：真正只读的取数连接——绝不 mkdir、绝不
    DDL/迁移、绝不开 WAL、绝不建表。缺库/缺表时 SELECT 直接抛错（由调用方 fail-closed
    当作"无记录"，而不是悄悄 healthy），且绝不落任何文件。:memory: 无法以 ro URI 打开，
    故只读路径拒绝内存库（调用方本就要求真实持久文件才走只读诊断）。"""
    path = Path(store_path)
    if str(path) == ':memory:':
        raise sqlite3.OperationalError('read-only availability lookup needs a real '
                                       'persistent file, not an in-memory db')
    if not path.is_file():
        # 绝不 mkdir/建库：缺库直接返回"空"（调用方不产生任何副作用）。
        raise FileNotFoundError(str(path))
    uri = path.as_uri() + '?mode=ro'
    conn = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _table_present(conn, name) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


# ---------------------------------------------------------------- 路由解析
def load_routes(path=None) -> dict:
    """读取受信任本机路由配置；缺失/损坏一律视为“无已知独立关系”。"""
    p = Path(path) if path else default_routes_path()
    if not p.is_file():
        return {'routes': [], 'path': str(p), 'exists': False}
    routes = json.loads(p.read_text(encoding='utf-8'))
    if not isinstance(routes, dict) or not isinstance(routes.get('routes'), list):
        raise ValueError(f'quota routes file must be a JSON object with a routes list: {p}')
    for item in routes['routes']:
        if not isinstance(item, dict) or not isinstance(item.get('match'), dict) \
                or not item.get('quota_group') or not item.get('independence'):
            raise ValueError(f'route entries need match/quota_group/independence: {item!r}')
        if item['independence'] not in ('user_confirmed', 'shared_declared'):
            raise ValueError(f"route independence must be 'user_confirmed' or "
                             f"'shared_declared': {item['independence']!r}")
    routes['path'] = str(p)
    routes['exists'] = True
    return routes


def resolve_group(routes, runtime: str, identity: dict) -> dict:
    """真实通道标识 → quota_group。只有受信任路由配置能命名组；未匹配一律进入
    共享保守组 unknown-shared（未知关系不可默认独立），绝不因 plan 自报而放行。
    match 的每个键都必须与 identity 精确相等；推荐绑定跨配置别名稳定的运行时事实
    （CodeBuddy 的 entry_cli、ZCode 的 provider），entry_config 别名不改变通道。"""
    for item in routes.get('routes', []):
        if item.get('runtime') != runtime:
            continue
        match = item['match']
        if all(identity.get(k) == v for k, v in match.items()):
            return {'quota_group': item['quota_group'], 'basis': item['independence'],
                    'matched': True,
                    'source': 'trusted local quota-routes config'}
    return {'quota_group': UNKNOWN_SHARED_GROUP, 'basis': 'unmatched_conservative_shared',
            'matched': False,
            'source': 'no trusted route matched; unknown relationship is NOT treated as '
                      'independent'}


def _runtime_route_groups(routes, runtime: str) -> set:
    return {item['quota_group'] for item in routes.get('routes', [])
            if item.get('runtime') == runtime and item.get('quota_group')}


def _independent_confirmed_groups(routes) -> set:
    """来自受信任路由里 independence=user_confirmed 的已知组集合：这些组彼此不连带
    （可交替接续）。shared_declared 或未知组不在此列，仍进保守共享互相阻断。routes
    缺失/更名时集合为空 → 全部保守连带，防止改配置绕过历史冷却（S4 缺陷 1）。"""
    return {item['quota_group'] for item in (routes or {}).get('routes', [])
            if item.get('independence') == 'user_confirmed' and item.get('quota_group')}


# ---------------------------------------------------------------- 429 分类
def parse_reset_datetime(texts) -> datetime | None:
    """从错误结构文本提取带时区的服务器 reset 时间并换算 UTC。只认带明确时区
    （UTC+8 / +08:00 等）的形式；无时区或非法日期（如 13 月 45 日）返回 None，
    不猜、不用固定值替代。

    BW-AVAILABILITY-20261009-B2：多个 reset 时**跳过非法项、取最晚的合法值**（最严格
    服务端下限），绝不因首个非法/无时区项就丢弃后续合法值，也绝不返回早于其它合法值的
    首个匹配。全非法才返回 None。"""
    latest = None
    for text in texts or []:
        if not isinstance(text, str):
            continue
        for m in _RESET_DT_RE.finditer(text):
            year, month, day, hour, minute, second, sign, tzh, tzm = m.groups()
            try:
                offset = timedelta(hours=int(tzh), minutes=int(tzm or 0))
                if sign == '-':
                    offset = -offset
                dt = datetime(int(year), int(month), int(day), int(hour), int(minute),
                              int(second), tzinfo=timezone(offset))
            except ValueError:
                continue  # 非法日期（如 13 月 45 日）：跳过，继续看后续候选
            dt_utc = dt.astimezone(timezone.utc)
            if latest is None or dt_utc > latest:
                latest = dt_utc
    return latest


def parse_retry_after(value, now=None) -> datetime | None:
    """结构化 Retry-After：整数秒或 HTTP-date（RFC 7231）。来源只能是调用方从
    结构化错误/headers 提取的字段值，本模块绝不扫正文。"""
    if value is None:
        return None
    now = now or utcnow()
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value >= 0:
        return now + timedelta(seconds=float(value))
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return now + timedelta(seconds=int(text))
        try:
            dt = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


def extract_retry_after(errors, errors_info, now=None):
    """只从结构化错误字段提取 Retry-After：errors/errors_info 的 dict 条目自身的
    retry_after / Retry-After / retryAfter 键，或其嵌套 headers dict 的
    Retry-After / retry-after 键。绝不扫描 message/details 正文。

    不能遇到首个 invalid 值就返回而丢掉后续有效值（S4 缺陷 10）：收集**所有**结构化
    候选，逐个用 parse_retry_after 解析，返回解析后**最严格（最晚下限）**的那个原值；
    全无效才返回 None。classify_quota_failure 再与 reset 窗口取更晚者。"""
    now = now or utcnow()
    candidates = []
    for container in (errors, errors_info):
        if not isinstance(container, list):
            continue
        for item in container:
            if not isinstance(item, dict):
                continue
            for key in _RETRY_AFTER_ITEM_KEYS:
                if key in item:
                    candidates.append(item.get(key))
            headers = item.get('headers')
            if isinstance(headers, dict):
                for key in _RETRY_AFTER_HEADER_KEYS:
                    if key in headers:
                        candidates.append(headers.get(key))
    best_raw = None
    best_dt = None
    for value in candidates:
        if not isinstance(value, (int, float, str)) or isinstance(value, bool):
            continue
        parsed = parse_retry_after(value, now=now)
        if parsed is None:
            continue  # 无法解析的字符串：跳过，继续看后续候选
        if best_dt is None or parsed > best_dt:
            best_dt = parsed
            best_raw = value
    return best_raw


def _failure_texts(errors, errors_info) -> list:
    pool = []
    for container in (errors, errors_info):
        if isinstance(container, list):
            for item in container:
                if isinstance(item, str):
                    pool.append(item)
                elif isinstance(item, dict):
                    for key in ('details', 'message', 'reason'):
                        val = item.get(key)
                        if isinstance(val, str):
                            pool.append(val)
    return pool


def _has_quota_category(errors_info) -> bool:
    """BW-AVAILABILITY-20261009-B2 同条目绑定修正：category=quota 只有与 429
    status/code 出现在**同一条真实错误条目**里才算数——另一条不带 429 的 quota 条目
    绝不能污染普通 429（否则普通限流会被误升格成 24h 无窗口冷却）。"""
    if isinstance(errors_info, list):
        for item in errors_info:
            if not isinstance(item, dict) or item.get('category') not in QUOTA_CATEGORIES:
                continue
            if item.get('status') == 429 or item.get('response_status') == 429:
                return True
            for key in ('code', 'provider_code'):
                value = item.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value == 429:
                    return True
                if isinstance(value, str) and value.strip() == '429':
                    return True
    return False


def classify_quota_failure(errors, errors_info, retry_after=None, now=None) -> dict:
    """复用 ec.explicit_429 口径分类。返回是否额度事件、类别、候选冷却截止与
    原因；非 429 一律 not quota 事件（不产生冷却）。reset 窗口与 Retry-After
    同时存在时取更晚者（最严格服务端等待下限），两者时刻都保留在结果里。"""
    now = now or utcnow()
    base = {'is_quota_429': False, 'kind': None, 'cooldown_until_utc': None,
            'reason': None, 'reset_hint': ec.extract_reset_hint(errors, errors_info)}
    if not ec.explicit_429(errors, errors_info):
        base['reason'] = 'no explicit 429 status/code in error structures'
        return base
    base['is_quota_429'] = True
    texts = _failure_texts(errors, errors_info)
    reset_dt = parse_reset_datetime(texts)
    ra_dt = parse_retry_after(retry_after, now=now)
    candidates = [dt for dt in (reset_dt, ra_dt) if dt is not None]
    if candidates:
        until_dt = max(candidates)
        base['cooldown_until_utc'] = _iso(until_dt)
        base['reset_window_utc'] = _iso(reset_dt) if reset_dt is not None else None
        base['retry_after_until_utc'] = _iso(ra_dt) if ra_dt is not None else None
        base['retry_after_raw'] = retry_after if ra_dt is not None else None
        if reset_dt is not None and (ra_dt is None or reset_dt >= ra_dt):
            base['kind'] = 'quota_reset'
            base['reason'] = ('explicit 429 with a tz-aware server reset window; '
                              'cooldown until the parsed UTC instant')
        else:
            base['kind'] = 'retry_after'
            base['reason'] = ('explicit 429 with a structured Retry-After (seconds or '
                              'HTTP-date); when both a reset window and Retry-After '
                              'exist the later (strictest) server floor wins and is '
                              'never capped by the backoff cap')
        if reset_dt is not None and ra_dt is not None and ra_dt > reset_dt:
            base['reason'] += ('; Retry-After is later than the reset window, so the '
                               'strictest floor applies')
        return base
    if _has_quota_category(errors_info):
        base['kind'] = 'quota_no_window'
        base['cooldown_until_utc'] = _iso(now + timedelta(hours=QUOTA_NO_WINDOW_HOURS))
        base['reason'] = ('explicit 429 with quota category but no reset window and no '
                          f'Retry-After; conservative {QUOTA_NO_WINDOW_HOURS}h block, '
                          'no full-task re-dispatch, no guessed recovery')
        return base
    base['kind'] = 'temporary_backoff'
    base['reason'] = ('explicit temporary 429 without any window; capped exponential '
                      'backoff with jitter (scheduler-level, single layer)')
    return base


# ------------------------------------------------- ZCode 独立 availability 状态
# BW-AVAILABILITY-20261009-B2：全新的 ZCode 可用性/防重复状态，与旧 cooldowns 表完全
# 分离（旧冷却不复活、ZCode 失败也绝不写 cooldowns）。channel_key 绑定真实 provider +
# 受信任 quota_group：配置别名不能拆分同一真实通道；routes 缺失/更名不能借新别名绕过
# （保守匹配：同 provider 或同 group 的任一未清行都参与判定）。
def zcode_channel_key(provider, quota_group) -> str:
    return f'{provider}|{quota_group}'


def _as_429_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip('-').isdigit():
        return int(value.strip())
    return None


def classify_zcode_availability(errors, errors_info, *, provider, retry_after=None,
                                now=None) -> dict:
    """ZCode availability 事件分类（与旧 classify_quota_failure 口径分离）。

    - 只认**同一条真实错误条目内绑定**的 status/provider/code/message 429 事实：
      另一条不带 429 的 quota 条目不能污染普通 429；报告正文里的 429 数字、取消退出码
      都不进入本分类（调用方只在结构化失败信封上调用，且入口对取消码另有守卫）。
    - 硬额度只限真实 BigModel/Zhipu 渠道的 1308/1310，或窄的硬上限消息语义（服务器
      自己的文本，优先于 wrapper 派生的 rate_limit 标签）；同一数字来自其它 provider
      不泛化。
    - reset/Retry-After 取最晚的合法下限（跳过非法/无时区项，绝不猜）。
    - 结果只记录状态与原因，绝不宣称真实额度/账务/免费。"""
    now = now or utcnow()
    base = {'is_trusted_429': False, 'hard_quota': False, 'kind': None,
            'provider_code': None, 'blocked_until_utc': None, 'reason': None,
            'reset_window_utc': None, 'retry_after_until_utc': None,
            'retry_after_raw': None, 'provider': provider, 'bound_entry': None}
    entries = []
    for container in (errors, errors_info):
        if isinstance(container, list):
            for item in container:
                if isinstance(item, (dict, str)):
                    entries.append(item)
    chosen = None
    windows = []
    for item in entries:
        if isinstance(item, dict):
            is429 = ec.explicit_429(None, [item])
        else:
            is429 = ec.explicit_429([item], None)
        if not is429:
            continue
        if isinstance(item, dict):
            texts = [item.get(k) for k in ('message', 'details', 'reason')
                     if isinstance(item.get(k), str)]
            entry_provider = item.get('provider') or provider
            pcode = (_as_429_int(item.get('provider_code'))
                     or _as_429_int(item.get('code')))
            ra_raw = extract_retry_after(None, [item], now=now)
        else:
            texts = [item]
            entry_provider = provider
            pcode = None
            ra_raw = extract_retry_after([item], None, now=now)
        # BW-AVAILABILITY-20261009-B4 缺陷 2：硬额度只认**核验过的请求 provider**（受信任
        # BigModel/Zhipu 或显式可信别名）在同一条 429 载体里携带 1308/1310；条目里自报
        # provider 冒充受信任但核验请求来自其它渠道，一律不泛化为硬额度。
        hard_by_code = (pcode in ZCODE_HARD_QUOTA_PROVIDER_CODES
                        and is_trusted_hard_quota_provider(provider))
        hard_by_msg = any(hard_limit_message(t) for t in texts)
        hard = bool(hard_by_code or hard_by_msg)
        reset_dt = parse_reset_datetime(texts)
        ra_dt = parse_retry_after(ra_raw, now=now) if ra_raw is not None else None
        for dt in (reset_dt, ra_dt):
            if dt is not None:
                windows.append(dt)
        cand = {'hard': hard, 'hard_by_code': hard_by_code, 'hard_by_msg': hard_by_msg,
                'provider_code': pcode, 'entry_provider': entry_provider,
                'reset_dt': reset_dt, 'ra_dt': ra_dt, 'ra_raw': ra_raw,
                'category': item.get('category') if isinstance(item, dict) else None,
                'status': (item.get('status', item.get('response_status'))
                           if isinstance(item, dict) else None)}
        # 硬额度条目优先作为事实来源；同类取首个。
        if chosen is None or (hard and not chosen['hard']):
            chosen = cand
    if chosen is None:
        base['reason'] = ('no explicit 429 status/code bound inside a single real error '
                          'entry; report-body 429 digits, separate non-429 quota entries '
                          'and cancel exit codes never trigger availability recording')
        return base
    base['is_trusted_429'] = True
    base['hard_quota'] = chosen['hard']
    base['provider_code'] = chosen['provider_code']
    base['bound_entry'] = {'status': chosen['status'], 'provider': chosen['entry_provider'],
                           'provider_code': chosen['provider_code'],
                           'category': chosen['category'],
                           'note': 'status/provider/code/message were bound in the SAME '
                                   'real error entry'}
    until = max(windows) if windows else None
    if until is not None:
        base['blocked_until_utc'] = _iso(until)
        base['reset_window_utc'] = _iso(chosen['reset_dt']) \
            if chosen['reset_dt'] is not None else None
        base['retry_after_until_utc'] = _iso(chosen['ra_dt']) \
            if chosen['ra_dt'] is not None else None
        base['retry_after_raw'] = chosen['ra_raw'] if chosen['ra_dt'] is not None else None
    if chosen['hard']:
        if until is None:
            base['kind'] = 'hard_hold'
            base['reason'] = ('hard quota limit (trusted provider code '
                              f'{chosen["provider_code"]} or hard-limit message semantics '
                              'which override the wrapper-derived rate_limit label) with '
                              'NO trusted tz-aware window: hold awaiting an explicit user '
                              'reset or new recovery evidence; no guessed 24h, no fixed '
                              'short cycle, no claim about actual quota/billing')
        else:
            base['kind'] = 'hard_reset_window'
            base['reason'] = ('hard quota limit with a trusted server window; hold until '
                              'the latest valid floor, then a single bounded probe must '
                              'verify recovery (never auto-declared healthy)')
    else:
        if until is None:
            base['kind'] = 'temporary_backoff'
            base['reason'] = ('ordinary temporary 429 without any window; bounded '
                              'exponential backoff with jitter (never a hard hold)')
        else:
            base['kind'] = 'backoff_until_window'
            base['reason'] = ('ordinary temporary 429 with a trusted server window; '
                              'bounded until the latest valid floor')
    return base


def _availability_row_effective(conn, row, now):
    """把行推进到当前有效状态（事务内）：hard_hold 到期 → recovery_unverified（恢复点
    到了也只"可核验"，不自动 healthy）。返回 (state, blocked_until_dt)。"""
    state = row['state']
    until = (datetime.fromisoformat(row['blocked_until_utc'])
             if row['blocked_until_utc'] else None)
    if state == 'hard_hold' and until is not None and until <= now:
        conn.execute('UPDATE zcode_availability SET state=?, updated_at_utc=? '
                     'WHERE channel_key=? AND state=?',
                     ('recovery_unverified', _iso(now), row['channel_key'], 'hard_hold'))
        state = 'recovery_unverified'
    return state, until


def zcode_blocking_rows(store_path, now=None) -> list:
    """当前对完整派工构成阻断的 ZCode availability 行（供 dispatch_pool 资格过滤）。
    healthy 与已到期的 backoff 不阻断；recovery_unverified 阻断完整派工（只放行单次
    有界 probe，由 gate 判定）。只读，不改状态。BW-AVAILABILITY-20261009-B4 缺陷 5：
    走真正只读连接，绝不建库/建表/迁移；缺库或缺表视为"无任何 availability 记录"
    （返回空），其它读取错误直接抛出，由调用方 fail-closed（绝不因读失败当 healthy）。"""
    now = now or utcnow()
    out = []
    try:
        conn = _open_readonly(store_path)
    except FileNotFoundError:
        return []
    try:
        with closing(conn):
            if not _table_present(conn, 'zcode_availability'):
                return []
            for row in conn.execute('SELECT * FROM zcode_availability').fetchall():
                state = row['state']
                until = (datetime.fromisoformat(row['blocked_until_utc'])
                         if row['blocked_until_utc'] else None)
                if state == 'healthy':
                    continue
                if state == 'backoff' and until is not None and until <= now:
                    continue
                out.append({'channel_key': row['channel_key'], 'provider': row['provider'],
                            'quota_group': row['quota_group'], 'state': state,
                            'epoch': row['epoch'],
                            'blocked_until_utc': row['blocked_until_utc'],
                            'last_reason': row['last_reason'],
                            'last_provider_code': row['last_provider_code']})
    except sqlite3.Error:
        # 只读连接损坏/锁定：绝不静默当无阻断，向上抛给调用方 fail-closed。
        raise
    return out


def zcode_availability_gate(store_path, *, provider, routes_path=None, identity=None,
                            purpose='dispatch', now=None) -> dict:
    """ZCode 入口在 Popen 前的权威 availability 原子门（与旧 cooldowns 门完全独立）：
    - hard_hold（无窗口=无限期，有窗口=到恢复点）→ 拒绝完整派工；恢复点到了转
      recovery_unverified，只放行**单次有界无副作用 probe**（CAS 单赢家）；
    - backoff 未到期 → 拒绝；到期即可正常派工；
    - probe 失败/超时/取消后 probe_blocked_until 之内不能立即再 probe；
    - 保守跨别名匹配：同 provider 或同 group 的任一未清行都阻断（配置别名/routes 更名
      不能拆分或绕过）；
    - 只记录状态/原因，绝不宣称真实额度/账务/免费。"""
    if purpose not in ('dispatch', 'probe'):
        raise ValueError("purpose must be 'dispatch' or 'probe'")
    now = now or utcnow()
    routes = load_routes(routes_path)
    ident = dict(identity or {})
    ident.setdefault('provider', provider)
    resolution = resolve_group(routes, 'zcode', ident)
    group = resolution['quota_group']
    key = zcode_channel_key(provider, group)
    attempt_id = uuid.uuid4().hex
    with closing(_connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            rows = conn.execute('SELECT * FROM zcode_availability').fetchall()
            matching = [r for r in rows
                        if r['provider'] == provider or r['quota_group'] == group]
            reasons = []
            block_until = []
            probe_candidate = None
            for row in matching:
                state, until = _availability_row_effective(conn, row, now)
                if state == 'healthy':
                    continue
                if state == 'backoff':
                    if until is not None and until <= now:
                        continue  # 有界退避到期：可正常派工
                    reasons.append(f'zcode channel {row["channel_key"]} is in bounded '
                                   f'backoff until {row["blocked_until_utc"]} (ordinary '
                                   f'temporary 429; reason={row["last_reason"]})')
                    block_until.append(row['blocked_until_utc'])
                    continue
                if state == 'hard_hold':
                    reasons.append(
                        f'zcode channel {row["channel_key"]} is under a HARD quota hold '
                        f'(provider_code={row["last_provider_code"]}, '
                        f'blocked_until={row["blocked_until_utc"] or "indefinite"}): '
                        f'awaiting an explicit user reset or new recovery evidence via '
                        f'the narrow manual reset entry; no guessed recovery, no auto '
                        f'retry (reason={row["last_reason"]})')
                    if row['blocked_until_utc']:
                        block_until.append(row['blocked_until_utc'])
                    continue
                # recovery_unverified
                probe_blocked = row['probe_blocked_until_utc']
                if probe_blocked and datetime.fromisoformat(probe_blocked) > now:
                    reasons.append(f'zcode channel {row["channel_key"]} recovery probe '
                                   f'failed/timed out/cancelled; cannot immediately '
                                   f're-probe until {probe_blocked}')
                    block_until.append(probe_blocked)
                    continue
                # BW-AVAILABILITY-20261009-B4 缺陷 3：唯一一次 recovery 资格已被真实失败/
                # 超时/取消消耗（consumed=1）时，退避窗口到期也绝不再凭空发定时 probe——
                # 必须由新的 manual reset（推进 epoch、重置 consumed）重新取得资格。
                if row['recovery_eligibility_consumed'] and not row['probe_active']:
                    reasons.append(
                        f'zcode channel {row["channel_key"]} already consumed its single '
                        f'recovery eligibility on a failed probe; a bounded timed re-probe '
                        f'without NEW recovery evidence is never granted — reset again '
                        f'with fresh evidence to re-arm one probe')
                    continue
                if purpose == 'dispatch':
                    reasons.append(f'zcode channel {row["channel_key"]} recovery is '
                                   f'unverified; a single bounded no-side-effect probe '
                                   f'must pass first (manual reset makes it verifiable, '
                                   f'NOT healthy); full dispatch refused')
                    continue
                if row['probe_active']:
                    reasons.append(f'zcode channel {row["channel_key"]} already has a '
                                   f'recovery probe in flight; only one bounded probe '
                                   f'at a time')
                    continue
                if probe_candidate is None or row['channel_key'] == key:
                    probe_candidate = row
            if reasons:
                conn.execute('ROLLBACK')
                return {'allowed': False, 'sent': False, 'provider': provider,
                        'quota_group': group, 'channel_key': key,
                        'resolution': resolution, 'purpose': purpose,
                        'state': 'blocked', 'reasons': reasons,
                        'blocked_until_utc': max(block_until) if block_until else None}
            granted_epoch = 0
            probe_granted = False
            state_now = None
            # BW-AVAILABILITY-20261009-B4 缺陷 1：attempt 必须携带并结算 CAS 实际授予的
            # 那一行的真实 row key（同 provider 配置别名/组更名时 probe 可能在旧组行上授予，
            # 却按新组 key 返回→结算找不到行→旧行 probe_active 永远 true）。这里回传
            # 授予行的 channel_key/quota_group/provider，供入口按精确行 CAS 结算。
            granted_channel_key = key
            granted_group = group
            granted_provider = provider
            if probe_candidate is not None:
                conn.execute('UPDATE zcode_availability SET probe_active=1, '
                             'probe_attempt=?, updated_at_utc=? WHERE channel_key=? '
                             "AND state='recovery_unverified' AND probe_active=0",
                             (attempt_id, _iso(now), probe_candidate['channel_key']))
                probe_granted = True
                granted_epoch = probe_candidate['epoch']
                state_now = 'recovery_unverified'
                granted_channel_key = probe_candidate['channel_key']
                granted_group = probe_candidate['quota_group']
                granted_provider = probe_candidate['provider']
            else:
                exact = conn.execute('SELECT * FROM zcode_availability WHERE '
                                     'channel_key=?', (key,)).fetchone()
                if exact is not None:
                    granted_epoch = exact['epoch']
                    state_now = exact['state']
                else:
                    # 无行的普通/新渠道：给一个明确的初始代际 0（绝非 None），使任何
                    # 迟到/正常成功都必须匹配代际，不能凭空清除后来的失败代际。
                    granted_epoch = 0
                    state_now = 'available'
            conn.execute('COMMIT')
        except Exception:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise
    return {'allowed': True, 'sent': False, 'provider': provider, 'quota_group': group,
            'channel_key': granted_channel_key, 'granted_channel_key': granted_channel_key,
            'granted_quota_group': granted_group, 'granted_provider': granted_provider,
            'resolution': resolution, 'purpose': purpose,
            'attempt_id': attempt_id, 'availability_epoch': granted_epoch,
            'probe_granted': probe_granted, 'state': state_now or 'available',
            'reasons': [],
            'note': 'independent ZCode availability state; makes no claim about actual '
                    'quota, billing, or free availability'}


def zcode_probe_ticket_valid(store_path, *, provider, channel_key, attempt_id,
                             epoch=None, now=None) -> bool:
    """BW-AVAILABILITY-20261009-B4 缺陷 4：容量门核验一张**真正绑定**的 recovery probe
    票据（绝不信任可自报的 `probe=true` 布尔）。只读查库，行必须：
    - channel_key 精确匹配（CAS 授予的那一行）；
    - state='recovery_unverified'；
    - probe_active=1 且 probe_attempt==attempt_id（本次授予的唯一在飞 probe）；
    - 给了 epoch 就必须等于行代际（防旧票据对后来的失败代际冒充有效）。
    任一不符 → False（正常派工照旧被拒）。缺库/缺表/读取错误一律 False（fail-closed）。"""
    if not attempt_id or not channel_key:
        return False
    now = now or utcnow()
    try:
        conn = _open_readonly(store_path)
    except (FileNotFoundError, sqlite3.OperationalError):
        return False
    try:
        with closing(conn):
            if not _table_present(conn, 'zcode_availability'):
                return False
            row = conn.execute('SELECT * FROM zcode_availability WHERE channel_key=?',
                               (channel_key,)).fetchone()
    except sqlite3.Error:
        return False
    if row is None:
        return False
    if row['state'] != 'recovery_unverified' or not row['probe_active']:
        return False
    if row['probe_attempt'] != attempt_id:
        return False
    if epoch is not None and row['epoch'] != epoch:
        return False
    return True


def record_zcode_unavailability(store_path, *, provider, quota_group, classification,
                                attempt_id=None, source=None, evidence_path=None,
                                evidence_sha256=None, now=None) -> dict:
    """把一次**受信任 429** 终态落进独立 availability 状态（ZCode 入口在错误证据确认
    后、接续冻结/终态结算之前调用——即使随后冻结失败或终态未知，不可用事实也已保存）。
    - epoch 单调递增；迟到成功靠 epoch+attempt CAS，绝不能清除更新的失败 epoch；
    - 硬额度无窗口 → hard_hold 无限期（等显式用户重置/新恢复证据，不猜 24h）；
    - 普通临时 429 → 有界指数退避；
    - 单调下限：已有更晚的 blocked_until 不被更早事件缩短，无限期 hold 不被有限窗口替换；
    - 若本次正是失败 probe 的结算（attempt 匹配）→ probe 占位结算 + 失败退避窗口，
      失败/超时/取消后不能立即再 probe。"""
    now = now or utcnow()
    if not classification.get('is_trusted_429'):
        return {'recorded': False,
                'reasons': ['classification is not a trusted 429 availability event']}
    kind = classification.get('kind')
    if kind not in ZCODE_AVAILABILITY_KINDS:
        return {'recorded': False, 'reasons': [f'unknown availability kind {kind!r}']}
    key = zcode_channel_key(provider, quota_group)
    new_until = (datetime.fromisoformat(classification['blocked_until_utc'])
                 if classification.get('blocked_until_utc') else None)
    new_state = 'hard_hold' if kind in ('hard_hold', 'hard_reset_window') else 'backoff'
    with closing(_connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute('SELECT * FROM zcode_availability WHERE channel_key=?',
                               (key,)).fetchone()
            epoch = (row['epoch'] + 1) if row is not None else 1
            backoff_attempts = (row['backoff_attempts'] if row is not None else 0)
            probe_failures = (row['probe_failures'] if row is not None else 0)
            probe_blocked = (row['probe_blocked_until_utc'] if row is not None else None)
            kept_later = False
            if kind == 'temporary_backoff':
                delay = min(TEMP_BACKOFF_BASE_SECONDS * (2 ** backoff_attempts),
                            TEMP_BACKOFF_CAP_SECONDS)
                delay = delay * (1 + random.uniform(0, TEMP_BACKOFF_JITTER))
                candidate_until = now + timedelta(seconds=delay)
                backoff_attempts += 1
            elif kind == 'hard_hold':
                candidate_until = None
                backoff_attempts = 0
            else:
                candidate_until = new_until
                backoff_attempts = 0 if new_state == 'hard_hold' else backoff_attempts
            state = new_state
            if row is not None:
                existing_until = (datetime.fromisoformat(row['blocked_until_utc'])
                                  if row['blocked_until_utc'] else None)
                if row['state'] == 'hard_hold' and existing_until is None:
                    # 已有无限期硬 hold：任何有限窗口都不缩短它，除非新事件也是无限期。
                    state = 'hard_hold'
                    candidate_until = None
                    kept_later = True
                elif candidate_until is None:
                    state = 'hard_hold'
                elif existing_until is not None and existing_until > candidate_until:
                    candidate_until = existing_until
                    kept_later = True
                    if row['state'] == 'hard_hold':
                        state = 'hard_hold'
            probe_settled = False
            if attempt_id is not None and row is not None \
                    and row['probe_attempt'] == attempt_id:
                probe_settled = True
                probe_failures += 1
                retry_delay = min(ZCODE_PROBE_RETRY_BASE_SECONDS * (2 ** probe_failures),
                                  ZCODE_PROBE_RETRY_CAP_SECONDS)
                probe_blocked = _iso(now + timedelta(seconds=retry_delay))
            # BW-AVAILABILITY-20261009-B5：这次落库若正是在飞 recovery probe 的真实失败
            # 结算（attempt 匹配），就原子消费该代际唯一一次核验资格——清 probe_active 同时
            # 置 consumed=1。否则 settle 之后（probe_attempt 已清、settle 不再认 owner）到
            # probe_blocked 到期时，同一恢复依据会被 gate 再次放行，等于用固定退避反复试硬额度。
            consume_elig = 1 if probe_settled else 0
            conn.execute(
                'INSERT INTO zcode_availability(channel_key, provider, quota_group, '
                'state, epoch, kind, blocked_until_utc, probe_active, probe_attempt, '
                'probe_failures, probe_blocked_until_utc, backoff_attempts, '
                'last_provider_code, last_reason, reset_hint_utc, evidence_path, '
                'evidence_sha256, recorded_at_utc, updated_at_utc, '
                'recovery_eligibility_consumed) '
                'VALUES(?,?,?,?,?,?,?,0,NULL,?,?,?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(channel_key) DO UPDATE SET state=excluded.state, '
                'epoch=excluded.epoch, kind=excluded.kind, '
                'blocked_until_utc=excluded.blocked_until_utc, probe_active=0, '
                'probe_attempt=NULL, probe_failures=excluded.probe_failures, '
                'probe_blocked_until_utc=COALESCE(excluded.probe_blocked_until_utc, '
                'zcode_availability.probe_blocked_until_utc), '
                'backoff_attempts=excluded.backoff_attempts, '
                'last_provider_code=excluded.last_provider_code, '
                'last_reason=excluded.last_reason, reset_hint_utc=excluded.reset_hint_utc, '
                'evidence_path=excluded.evidence_path, '
                'evidence_sha256=excluded.evidence_sha256, '
                'recovery_eligibility_consumed=MAX(zcode_availability.'
                'recovery_eligibility_consumed, excluded.recovery_eligibility_consumed), '
                'updated_at_utc=excluded.updated_at_utc',
                (key, provider, quota_group, state, epoch, kind,
                 _iso(candidate_until) if candidate_until is not None else None,
                 probe_failures, probe_blocked, backoff_attempts,
                 classification.get('provider_code'), 
                 (classification.get('reason') or '') + (f' | source: {source}'
                  if source else ''),
                 classification.get('blocked_until_utc'), evidence_path,
                 evidence_sha256, _iso(now), _iso(now), consume_elig))
            conn.execute('COMMIT')
        except Exception:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise
    return {'recorded': True, 'channel_key': key, 'provider': provider,
            'quota_group': quota_group, 'state': state, 'kind': kind, 'epoch': epoch,
            'hard_quota': bool(classification.get('hard_quota')),
            'provider_code': classification.get('provider_code'),
            'blocked_until_utc': _iso(candidate_until) if candidate_until else None,
            'monotonic_kept_later': kept_later, 'probe_settled': probe_settled,
            'note': 'independent ZCode availability/anti-repeat state; the legacy '
                    'cooldowns table is never touched (no revived cooldowns); records '
                    'state/reason only, never claims actual quota/billing/free'}


def settle_zcode_attempt(store_path, *, provider, quota_group=None, attempt_id, epoch,
                         success, executed=True, channel_key=None, now=None) -> dict:
    """ZCode attempt 终态对 availability 状态的结算（epoch+attempt CAS）：
    - success=True 且 epoch 匹配（probe 还需 attempt 匹配）→ 转 healthy（核验通过才
      恢复；manual reset 只到 recovery_unverified，"可核验"≠healthy）；
    - 迟到成功（epoch 已被更新失败推进）→ 绝不清除新失败 epoch，只结算自己的 probe 占位；
    - success=False → 绝不写 healthy；executed=True 的真实 probe 失败/超时/取消结算
      占位并设再探测退避窗口；executed=False（Popen 前被拒，probe 从未执行）只归还
      probe 资格，不算失败。
    BW-AVAILABILITY-20261009-B4 缺陷 1：优先结算 attempt 携带的**精确 CAS 授予行 key**
    （别名/组更名时授予行与重算 key 可能不同）；epoch 一律按代际比较（None 归一为初始代
    际 0），绝不因 epoch 为 None 就跳过比较、让迟到/普通成功清除更新失败。"""
    now = now or utcnow()
    key = channel_key or zcode_channel_key(provider, quota_group)
    attempt_epoch = 0 if epoch is None else epoch
    with closing(_connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute('SELECT * FROM zcode_availability WHERE channel_key=?',
                               (key,)).fetchone()
            if row is None:
                conn.execute('COMMIT')
                return {'settled': False, 'cleared': False,
                        'reasons': ['no availability state recorded for this channel']}
            owns_probe = row['probe_attempt'] is not None \
                and row['probe_attempt'] == attempt_id
            if success:
                if attempt_epoch != row['epoch']:
                    if owns_probe:
                        conn.execute('UPDATE zcode_availability SET probe_active=0, '
                                     'probe_attempt=NULL, updated_at_utc=? '
                                     'WHERE channel_key=?', (_iso(now), key))
                    conn.execute('COMMIT')
                    return {'settled': True, 'cleared': False, 'epoch': row['epoch'],
                            'reasons': [
                                f'stale success: availability epoch is {row["epoch"]} but '
                                f'this attempt observed {attempt_epoch}; a late success must '
                                'not clear a newer failure epoch (two legal parallel tasks on '
                                'the same provider keep the newer failure)']}
                if row['probe_active'] and not owns_probe:
                    conn.execute('COMMIT')
                    return {'settled': True, 'cleared': False, 'epoch': row['epoch'],
                            'reasons': ['a different recovery probe is in flight; this '
                                        'success does not match it and clears nothing']}
                conn.execute('UPDATE zcode_availability SET state=?, '
                             'blocked_until_utc=NULL, probe_active=0, probe_attempt=NULL, '
                             'probe_blocked_until_utc=NULL, backoff_attempts=0, '
                             'recovery_eligibility_consumed=0, '
                             'updated_at_utc=? WHERE channel_key=?',
                             ('healthy', _iso(now), key))
                conn.execute('COMMIT')
                return {'settled': True, 'cleared': True, 'state': 'healthy',
                        'epoch': row['epoch'],
                        'note': 'probe/dispatch success verified against epoch+attempt; '
                                'provider-side quota state remains provider-managed and '
                                'is not claimed'}
            if owns_probe:
                if executed:
                    probe_failures = row['probe_failures'] + 1
                    retry_delay = min(
                        ZCODE_PROBE_RETRY_BASE_SECONDS * (2 ** probe_failures),
                        ZCODE_PROBE_RETRY_CAP_SECONDS)
                    # BW-AVAILABILITY-20261009-B4 缺陷 3：一次 recovery 资格真实失败/超时/
                    # 取消即消耗（consumed=1），此后即便退避窗口到期也不再凭空重发定时 probe，
                    # 必须由**新的**人工恢复证据（manual reset，推进 epoch 并重置 consumed）
                    # 才能重新取得唯一一次资格——绝不无新证据地反复 probe。
                    conn.execute('UPDATE zcode_availability SET probe_active=0, '
                                 'probe_attempt=NULL, probe_failures=?, '
                                 'probe_blocked_until_utc=?, '
                                 'recovery_eligibility_consumed=1, updated_at_utc=? '
                                 'WHERE channel_key=?',
                                 (probe_failures,
                                  _iso(now + timedelta(seconds=retry_delay)),
                                  _iso(now), key))
                else:
                    # pre-start refusal：probe 从未执行，只归还本次未执行的资格，不计失败、
                    # 不消耗 recovery 资格（executed=False）。
                    conn.execute('UPDATE zcode_availability SET probe_active=0, '
                                 'probe_attempt=NULL, updated_at_utc=? WHERE channel_key=?',
                                 (_iso(now), key))
            conn.execute('COMMIT')
            return {'settled': True, 'cleared': False, 'state': row['state'],
                    'reasons': ['failure/timeout/cancel never writes healthy; probe '
                                'placeholder settled' + ('' if executed else
                                ' (pre-start refusal; probe eligibility restored)')]}
        except Exception:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise


def zcode_manual_reset(store_path, *, provider, quota_group=None, routes_path=None,
                       evidence_ref=None, epoch=None, now=None) -> dict:
    """窄的幂等人工重置入口（绑定 epoch + 证据引用）：把 hard_hold/backoff 转为
    recovery_unverified——"可核验"≠healthy：仍必须由单次有界无副作用 probe 成功核验。
    - evidence_ref 必填（用户重置/新恢复证据的引用路径）；
    - 给了 epoch 就必须匹配当前行（防止把更新的失败 epoch 重置掉）；
    - probe 在飞时拒绝；同 epoch+同证据重复调用幂等；
    - 重置推进 epoch，使重置前在飞的迟到成功无法清除重置后状态；
    - 绝不宣称真实额度/账务/免费。"""
    now = now or utcnow()
    if not evidence_ref or not str(evidence_ref).strip():
        return {'reset': False,
                'reasons': ['evidence_ref is required: the manual reset must bind the '
                            'explicit user reset / new recovery evidence reference']}
    group = quota_group
    if group is None:
        routes = load_routes(routes_path)
        group = resolve_group(routes, 'zcode', {'provider': provider})['quota_group']
    key = zcode_channel_key(provider, group)
    with closing(_connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute('SELECT * FROM zcode_availability WHERE channel_key=?',
                               (key,)).fetchone()
            if row is None:
                conn.execute('ROLLBACK')
                return {'reset': False, 'channel_key': key,
                        'reasons': ['no availability state recorded for this channel; '
                                    'nothing to reset']}
            # BW-AVAILABILITY-20261009-B5：重复/重放判定必须先于通用 epoch 比较——同一绑定
            # 证据重放它自己创建的那一代 reset 必须**稳定幂等**返回，绝不因把 epoch 参数
            # 传成“重置前的旧代际”（如首次 reset(epoch=1,E) 已把行推进到 2）而在下面 epoch
            # 不匹配处被误判为 stale、既不返回新授权也不推进 epoch。
            prior_bound = (row['manual_reset_evidence'] == str(evidence_ref)
                           and row['manual_reset_epoch'] is not None)
            # 旧事件重放：该证据当初绑定的是更老的失败代际，行已被更新失败推进 → 绝不能
            # 给新失败重造资格或把新失败重置掉（即便省略 epoch 参数）。
            if prior_bound and row['epoch'] > row['manual_reset_epoch']:
                conn.execute('ROLLBACK')
                return {'reset': False, 'channel_key': key, 'epoch': row['epoch'],
                        'reasons': [f'stale reset replay: this evidence was bound to '
                                    f'failure generation {row["manual_reset_epoch"]} but '
                                    f'channel is now at epoch {row["epoch"]} (a newer '
                                    'failure); an old reset event can never re-arm '
                                    'eligibility for or wipe out a later failure']}
            # 同证据 + 行代际正是该证据当初创建的那一代，且仍处 recovery_unverified 或已
            # 由合法 probe 成功核验为 healthy → 幂等确认（BW-AVAILABILITY-20261009-B6）：
            # 返回**实际**状态，绝不推进 epoch、绝不重开核验资格。B4/B5 此处只认
            # recovery_unverified，导致 healthy 代际的同证据重放误落入下面的新 reset（推进
            # epoch、把已核验通道重开成 recovery_unverified），故一并覆盖 healthy。
            if prior_bound and row['epoch'] == row['manual_reset_epoch'] \
                    and row['state'] in ('recovery_unverified', 'healthy'):
                conn.execute('COMMIT')
                return {'reset': True, 'idempotent': True, 'channel_key': key,
                        'state': row['state'], 'epoch': row['epoch'],
                        'note': ('idempotent repeat of the same bound reset; already '
                                 'verified healthy — epoch not advanced, eligibility not '
                                 're-armed'
                                 if row['state'] == 'healthy' else
                                 'idempotent repeat of the same bound reset; still '
                                 'probe-eligible only, NOT healthy')}
            # 通用 epoch 守卫（非重放路径）：显式给了 epoch 就必须匹配当前行，防止把更新的
            # 失败代际重置掉。
            if epoch is not None and row['epoch'] != epoch:
                conn.execute('ROLLBACK')
                return {'reset': False, 'channel_key': key, 'epoch': row['epoch'],
                        'reasons': [f'stale reset: current epoch is {row["epoch"]} but '
                                    f'the request bound {epoch}; refusing to reset a '
                                    'newer failure state']}
            if row['probe_active']:
                conn.execute('ROLLBACK')
                return {'reset': False, 'channel_key': key,
                        'reasons': ['a recovery probe is in flight; settle it first']}
            new_epoch = row['epoch'] + 1
            conn.execute('UPDATE zcode_availability SET state=?, blocked_until_utc=NULL, '
                         'probe_blocked_until_utc=NULL, epoch=?, manual_reset_at_utc=?, '
                         'manual_reset_evidence=?, manual_reset_epoch=?, '
                         'recovery_eligibility_consumed=0, updated_at_utc=? '
                         'WHERE channel_key=?',
                         ('recovery_unverified', new_epoch, _iso(now),
                          str(evidence_ref), new_epoch, _iso(now), key))
            conn.execute('COMMIT')
        except Exception:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise
    return {'reset': True, 'idempotent': False, 'channel_key': key,
            'state': 'recovery_unverified', 'epoch': new_epoch,
            'note': 'manual reset makes the channel probe-verifiable, NOT healthy; a '
                    'single bounded no-side-effect probe must succeed before normal '
                    'dispatch; no claim about actual quota/billing/free'}


def zcode_availability_status(store_path, now=None) -> dict:
    """只读诊断（BW-AVAILABILITY-20261009-B4 缺陷 5）：绝不建库/建表/迁移，缺库或缺表
    返回空 dict（无任何 availability 记录），其它读取错误抛出交由调用方处理。"""
    now = now or utcnow()
    try:
        conn = _open_readonly(store_path)
    except FileNotFoundError:
        return {}
    try:
        with closing(conn):
            if not _table_present(conn, 'zcode_availability'):
                return {}
            rows = conn.execute('SELECT * FROM zcode_availability').fetchall()
    except sqlite3.Error:
        raise
    channels = {}
    for row in rows:
        until = (datetime.fromisoformat(row['blocked_until_utc'])
                 if row['blocked_until_utc'] else None)
        channels[row['channel_key']] = {
            'provider': row['provider'], 'quota_group': row['quota_group'],
            'state': row['state'], 'epoch': row['epoch'], 'kind': row['kind'],
            'blocked_until_utc': row['blocked_until_utc'],
            'blocked_expired': bool(until is not None and until <= now),
            'probe_active': bool(row['probe_active']),
            'probe_failures': row['probe_failures'],
            'probe_blocked_until_utc': row['probe_blocked_until_utc'],
            'recovery_eligibility_consumed': bool(row['recovery_eligibility_consumed']),
            'last_provider_code': row['last_provider_code'],
            'last_reason': row['last_reason'],
            'evidence_path': row['evidence_path'],
            'evidence_sha256': row['evidence_sha256'],
            'manual_reset_at_utc': row['manual_reset_at_utc'],
            'manual_reset_evidence': row['manual_reset_evidence'],
            'manual_reset_epoch': row['manual_reset_epoch'],
            'recorded_at_utc': row['recorded_at_utc']}
    return channels


# ---------------------------------------------------------------- 进程存活（只读）
def _windows_pid_state(pid) -> str:
    """Windows 只读存活查询：ctypes OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)
    + GetExitCodeProcess + CloseHandle。**绝不使用 os.kill**——Windows 上 os.kill
    除 CTRL 事件外走 TerminateProcess（Python 官方文档 os.kill），对任意 pid 是
    真实终止风险。返回 'alive' / 'dead' / 'unknown'；查询失败一律 'unknown'，
    由调用方保守按存活处理；ERROR_INVALID_PARAMETER(87) 表示 pid 不存在 → 'dead'。
    本函数可被测试替换（模块级符号），离线用例不依赖真实 Windows 句柄。"""
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.restype = ctypes.c_void_p
        open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
    except (OSError, AttributeError, ImportError):
        return 'unknown'
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_INVALID_PARAMETER = 87
    handle = open_process(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return 'dead' if ctypes.get_last_error() == ERROR_INVALID_PARAMETER else 'unknown'
    try:
        code = wintypes.DWORD(0)
        if not get_exit_code(handle, ctypes.byref(code)):
            return 'unknown'
        return 'alive' if code.value == STILL_ACTIVE else 'dead'
    finally:
        close_handle(handle)


def _pid_alive(pid, platform=None) -> bool:
    """只读、无副作用的存活查询，返回 True=存活或未知（保守），False=确认不存在。
    Windows 分支绝不调用 os.kill；POSIX 分支用 os.kill(pid, 0)（信号 0 不投递，
    只做存在性探测）。platform 参数仅供离线测试注入，不猜宿主。

    默认宿主判定用 sys.platform（Windows 为 'win32'），而非 os.name：os.name 在
    Windows 上是 'nt'，不以 'win' 开头，会让默认路径错误地落入 os.kill 分支——
    而 Windows 的 os.kill 除 CTRL 事件外走 TerminateProcess，对任意 pid 是真实终止
    风险（S3-safety 缺陷）。同时把 'nt' 也识别为 Windows，双保险。"""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    p = platform if platform is not None else sys.platform
    low = str(p).lower()
    if low.startswith('win') or low == 'nt':
        return _windows_pid_state(pid) != 'dead'
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _norm_workspace(workspace) -> str:
    return os.path.normcase(os.path.realpath(str(workspace)))


def _release_hint(store_path, workspace) -> str:
    return ('verify the previous executor\'s terminal state (its summary.json/process.json '
            'or process table), then run: '
            f'python {Path(__file__).name} --store {store_path} release-lock '
            f'--workspace "{workspace}" --owner-pid <recorded pid> '
            '--confirm-owner-terminal')


def _verify_pool_claim(claim, workspace, runtime) -> bool:
    """核验一个来自 dispatch_pool 的**真实容量 claim**（不是一个可信布尔旗标）：
    只有当 runtime 为 ZCode、池容量确实 >= 2、且该 token 在池 store 里此刻仍是在途
    （reserved/running/unknown）并绑定同一真实工作区时，才允许把“同 provider 通道单在途”
    放宽为“由已核验的并发容量池治理”，从而支持两个不同工作区同 provider 并行。缺 token、
    缺 store、非多槽池、token 已终态或工作区不符一律返回 False（维持旧的通道单在途语义）。
    绝不删通道行、绝不禁用额度门、绝不凭不可信旗标伪造 claim。"""
    if runtime != 'zcode' or not isinstance(claim, dict):
        return False
    cap = claim.get('capacity')
    if not isinstance(cap, int) or cap < 2:
        return False
    token = claim.get('token')
    if not token:
        return False
    try:
        import dispatch_pool as dp
    except Exception:
        return False
    try:
        store = claim.get('store') or str(dp.default_store_path())
        check = dp.validate_claim(store, token, runtime=runtime, workspace=workspace)
    except Exception:
        return False
    return bool(check.get('ok'))


# ---------------------------------------------------------------- 状态读写
def get_status(store_path, quota_group=None, now=None) -> dict:
    now = now or utcnow()
    with closing(_connect(store_path)) as conn:
        rows = conn.execute('SELECT * FROM cooldowns').fetchall()
        locks = conn.execute('SELECT * FROM workspace_locks').fetchall()
        channels = conn.execute('SELECT * FROM channel_locks').fetchall()
    cooldowns = {}
    for row in rows:
        until = datetime.fromisoformat(row['cooldown_until_utc'])
        if quota_group and row['quota_group'] != quota_group:
            continue
        cooldowns[row['quota_group']] = {
            'state': row['state'], 'cooldown_until_utc': row['cooldown_until_utc'],
            'reason': row['reason'], 'source': row['source'],
            'recorded_at_utc': row['recorded_at_utc'],
            'backoff_attempts': row['backoff_attempts'],
            'probe_active': bool(row['probe_active']),
            'epoch': row['epoch'],
            'cooldown_expired': until <= now}
    return {'store': str(store_path), 'now_utc': _iso(now), 'cooldowns': cooldowns,
            'workspace_locks': [dict(lock) for lock in locks],
            'channel_locks': [dict(lock) for lock in channels],
            'zcode_availability': zcode_availability_status(store_path, now=now)}


def _effective_state(conn, group, now) -> sqlite3.Row | None:
    row = conn.execute('SELECT * FROM cooldowns WHERE quota_group=?',
                       (group,)).fetchone()
    if row is None:
        return None
    until = datetime.fromisoformat(row['cooldown_until_utc'])
    if row['state'] == 'cooling' and until <= now:
        conn.execute('UPDATE cooldowns SET state=?, recorded_at_utc=? '
                     'WHERE quota_group=? AND state=?',
                     ('recovery_unverified', _iso(now), group, 'cooling'))
        row = conn.execute('SELECT * FROM cooldowns WHERE quota_group=?',
                           (group,)).fetchone()
    return row


def gate_dispatch(store_path, *, runtime, identity, workspace, purpose='dispatch',
                  routes_path=None, pid=None, host=None, now=None,
                  verified_pool_claim=None) -> dict:
    """Popen 前的唯一持久门禁：额度冷却 + 同 runtime 已知组保守检查 + 工作区单写入
    + 通道（quota_group）单在途，检查+占位在同一 BEGIN IMMEDIATE 事务内原子完成。
    拒绝必带 sent=false 与 UTC 截止；放行即已持有该工作区与该通道的占位，返回
    attempt_id/cooldown_epoch 供 settle_attempt 绑定生命周期。活/未知 owner 的占位
    不按时间过期抢占。"""
    if purpose not in ('dispatch', 'probe'):
        raise ValueError("purpose must be 'dispatch' or 'probe'")
    now = now or utcnow()
    routes = load_routes(routes_path)
    resolution = resolve_group(routes, runtime, identity or {})
    group = resolution['quota_group']
    ws = _norm_workspace(workspace)
    # ZCode + 已核验的多槽容量 claim → 通道并发由 dispatch_pool 治理，放宽“同组通道单在途”
    # 为“同 provider 两不同工作区可并行”，但工作区单写入与活/未知占位保护完全不变。
    relax_channel = _verify_pool_claim(verified_pool_claim, workspace, runtime)
    hint = _release_hint(store_path, workspace)
    attempt_id = uuid.uuid4().hex
    with closing(_connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            # BW-ZCODE-MANUAL-QUOTA-20261008-S1：ZCode 取消自动额度冷却——按真实 runtime
            # 固定策略跳过下面所有额度冷却/到期/probe 判定（row 保持 None），但**仍然执行**
            # 下方的工作区单写入 + 通道单在途并发占位（活/未知 owner 不抢占）。缺 routes、
            # 落到 unknown-shared、或共享组的历史冷却都不再挡 ZCode；CodeBuddy 及其它
            # runtime 的额度冷却门禁完全不变。
            skip_quota = _auto_quota_cooldown_disabled(runtime)
            row = None
            if not skip_quota:
                # 双向保守共享检查（S4 缺陷 1）：直接扫 store 里所有未清冷却行（含已到期的
                # recovery_unverified 与未到期的 cooling——行被清除才算恢复），不看当前 routes
                # 文件是否还列出该组。这样 routes 缺失/更名/改组、配置别名、跨 runtime 未知
                # 都无法抹掉历史冷却或借 unknown-shared 绕过。豁免只有一条：两个由用户明确确认
                # 独立的已知组（basis=user_confirmed）互不连带，可交替接续；它们仍会被
                # unknown-shared 或其它非独立组的冷却保守阻断，也会被自己组的冷却阻断。
                independent_confirmed = _independent_confirmed_groups(routes)
                own_independent = (resolution['matched']
                                   and group in independent_confirmed)
                rows = conn.execute(
                    'SELECT quota_group, cooldown_until_utc, state, probe_active '
                    'FROM cooldowns').fetchall()
                blocking = [r for r in rows if r['quota_group'] != group
                            and not (own_independent
                                     and r['quota_group'] in independent_confirmed)]
                # S5 缺陷 4：完整工程派发仍走保守跨组阻断；但对**已到期的 recovery_unverified**
                # 组，必须保留“单次有界 no-side-effect probe”恢复路径，否则两条互相保守阻断的
                # 冷却（如已到期 CB 组 + unknown-shared）会永久死锁、双方都进不去 probe。这里
                # 只对 purpose=='probe' 网开一面：跳过“仅因其它未清冷却行”的阻断，但仍禁止与
                # **仍在主动冷却(until>now) 或有在途 probe(probe_active)** 的相关组并发——相关组
                # 之间串行，绝不同时消耗同一份额度；恢复成功只按被核验身份/epoch 清本组，绝不
                # 宣称所有未知组都恢复，也绝不因此清空其它行。
                if purpose == 'probe':
                    hard_block = [r for r in blocking
                                  if datetime.fromisoformat(r['cooldown_until_utc']) > now
                                  or r['probe_active']]
                    if hard_block:
                        conn.execute('ROLLBACK')
                        return {'allowed': False, 'sent': False, 'quota_group': group,
                                'resolution': resolution, 'workspace': ws,
                                'reasons': [
                                    f'probe serialized against related groups '
                                    f'{sorted(r["quota_group"] for r in hard_block)}: at least '
                                    f'one still has an active (not yet expired) cooldown or an '
                                    f'in-flight probe; related (non user_confirmed-independent) '
                                    f'groups must not probe or dispatch concurrently on shared '
                                    f'quota; wait for that cooldown to expire or its probe to '
                                    f'settle before probing this group'],
                                    'cooldown_until_utc': max(
                                        r['cooldown_until_utc'] for r in hard_block)}
                    blocking = []
                if blocking:
                    conn.execute('ROLLBACK')
                    return {'allowed': False, 'sent': False, 'quota_group': group,
                            'resolution': resolution, 'workspace': ws,
                            'reasons': [
                                f'conservative shared-cooldown block: quota group(s) '
                                f'{sorted(r["quota_group"] for r in blocking)} still have an '
                                f'un-cleared cooldown in the store (cooling or '
                                f'recovery_unverified) and this channel ('
                                f'{"matched independent" if own_independent else "unmatched/non-independent"} '
                                f'group {group!r}) may share that quota; only the two '
                                f'user_confirmed independent groups skip each other. A '
                                f'switched, renamed or missing routes file cannot erase a '
                                f'stored cooldown, and unknown relationships are never '
                                f'defaulted to independent'],
                                'cooldown_until_utc': max(
                                    r['cooldown_until_utc'] for r in blocking)}
                row = _effective_state(conn, group, now)
                if row is not None:
                    until = datetime.fromisoformat(row['cooldown_until_utc'])
                    if until > now:
                        conn.execute('ROLLBACK')
                        return {'allowed': False, 'sent': False, 'quota_group': group,
                                'resolution': resolution, 'workspace': ws,
                                'reasons': [f"quota cooldown active until "
                                            f"{row['cooldown_until_utc']} (UTC); "
                                            f"state={row['state']}, reason={row['reason']}"],
                                'cooldown_until_utc': row['cooldown_until_utc']}
                    # 到期：不再 cooling，但恢复未经核验。
                    if purpose == 'dispatch':
                        conn.execute('ROLLBACK')
                        return {'allowed': False, 'sent': False, 'quota_group': group,
                                'resolution': resolution, 'workspace': ws,
                                'reasons': ['cooldown expired but recovery is unverified; '
                                            'a single bounded no-side-effect probe must '
                                            'pass first; full re-dispatch refused'],
                                'cooldown_until_utc': row['cooldown_until_utc']}
                    if row['probe_active']:
                        conn.execute('ROLLBACK')
                        return {'allowed': False, 'sent': False, 'quota_group': group,
                                'resolution': resolution, 'workspace': ws,
                                'reasons': ['a recovery probe is already in flight; only '
                                            'one bounded probe at a time'],
                                'cooldown_until_utc': row['cooldown_until_utc']}
                    conn.execute('UPDATE cooldowns SET probe_active=1, probe_attempt=?, '
                                 'last_probe_at_utc=? WHERE quota_group=?',
                                 (attempt_id, _iso(now), group))
                elif purpose == 'probe':
                    # probe 只用于核验一个已到期的冷却；没有冷却记录就没有可核验对象。
                    conn.execute('ROLLBACK')
                    return {'allowed': False, 'sent': False, 'quota_group': group,
                            'resolution': resolution, 'workspace': ws,
                            'reasons': ['recovery probe requires an existing expired '
                                        'cooldown (recovery_unverified) to verify; none is '
                                        'recorded for this group'],
                            'cooldown_until_utc': None}
            lock = conn.execute('SELECT * FROM workspace_locks WHERE workspace=?',
                                (ws,)).fetchone()
            if lock is not None:
                conn.execute('ROLLBACK')
                if _pid_alive(lock['pid']):
                    reasons = [f"workspace already has a live writer (runtime="
                               f"{lock['runtime']}, pid={lock['pid']}, purpose="
                               f"{lock['purpose']}, acquired {lock['acquired_at_utc']}); "
                               f"single-writer per real workspace; no preemption on "
                               f"lease age"]
                else:
                    reasons = [f"workspace lock held by a process that is no longer "
                               f"alive (pid={lock['pid']}); terminal state unknown — "
                               f"conservatively blocked, lock is NOT auto-deleted; "
                               f"{hint}"]
                return {'allowed': False, 'sent': False, 'quota_group': group,
                        'resolution': resolution, 'workspace': ws,
                        'reasons': reasons, 'cooldown_until_utc':
                            (row['cooldown_until_utc'] if row is not None else None)}
            channel = conn.execute('SELECT * FROM channel_locks WHERE quota_group=?',
                                   (group,)).fetchone()
            if channel is not None and not relax_channel:
                conn.execute('ROLLBACK')
                if _pid_alive(channel['pid']):
                    reasons = [f"quota channel {group} already has a live dispatch "
                               f"(runtime={channel['runtime']}, pid={channel['pid']}, "
                               f"workspace={channel['workspace']}, acquired "
                               f"{channel['acquired_at_utc']}); single in-flight "
                               f"dispatch per restricted channel; no preemption on "
                               f"lease age"]
                else:
                    reasons = [f"quota channel {group} lock held by a process that is "
                               f"no longer alive (pid={channel['pid']}); terminal state "
                               f"unknown — conservatively blocked, lock is NOT "
                               f"auto-deleted; {hint}"]
                return {'allowed': False, 'sent': False, 'quota_group': group,
                        'resolution': resolution, 'workspace': ws,
                        'reasons': reasons, 'cooldown_until_utc':
                            (row['cooldown_until_utc'] if row is not None else None)}
            owner_pid = pid if pid is not None else os.getpid()
            conn.execute('INSERT INTO workspace_locks(workspace, quota_group, runtime, '
                         'pid, host, purpose, acquired_at_utc, attempt) '
                         'VALUES(?,?,?,?,?,?,?,?)',
                         (ws, group, runtime, owner_pid,
                          host or os.environ.get('COMPUTERNAME'), purpose, _iso(now),
                          attempt_id))
            if not relax_channel:
                # 常规路径：同组通道单在途占位。已核验多槽容量 claim 的 ZCode 由 dispatch_pool
                # 治理并发，不占用（也不受限于）单行通道锁，从而支持同 provider 两工作区并行。
                conn.execute('INSERT INTO channel_locks(quota_group, runtime, pid, host, '
                             'purpose, workspace, acquired_at_utc, attempt) '
                             'VALUES(?,?,?,?,?,?,?,?)',
                             (group, runtime, owner_pid,
                              host or os.environ.get('COMPUTERNAME'), purpose, ws,
                              _iso(now), attempt_id))
            conn.execute('COMMIT')
        except Exception:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise
    return {'allowed': True, 'sent': False, 'quota_group': group,
            'resolution': resolution, 'workspace': ws, 'purpose': purpose,
            'attempt_id': attempt_id, 'pid': pid if pid is not None else os.getpid(),
            'cooldown_epoch': (row['epoch'] if row is not None else None),
            'cooldown_until_utc': (row['cooldown_until_utc'] if row is not None
                                   else None), 'acquired_at_utc': _iso(now),
            'auto_cooldown_disabled': skip_quota,
            'channel_relaxed_by_verified_claim': relax_channel,
            'note': ('workspace + channel placeholders acquired atomically; settle via '
                     'settle_attempt after the terminal state is confirmed'
                     + ('; auto quota cooldown DISABLED for this runtime (manual quota '
                        'management): no cooldown was consulted, required, extended or '
                        'cleared, but concurrency placeholders are retained'
                        if skip_quota else ''))}


# ---------------------------------------------------------------- 冷却写入/清除核心
def _record_cooldown_tx(conn, quota_group, classification, source, now) -> dict:
    """事务内落库（gate/settle/import 共用）：单调保留更晚冷却、epoch 递增、
    清 probe 占位。返回与 record_quota_event 相同的摘要。"""
    row = conn.execute('SELECT * FROM cooldowns WHERE quota_group=?',
                       (quota_group,)).fetchone()
    attempts = (row['backoff_attempts'] + 1) if row is not None else 0
    kind = classification.get('kind')
    if kind == 'quota_reset':
        candidate = datetime.fromisoformat(classification['cooldown_until_utc'])
        attempts = 0
    elif kind == 'retry_after':
        candidate = datetime.fromisoformat(classification['cooldown_until_utc'])
    elif kind == 'quota_no_window':
        candidate = datetime.fromisoformat(classification['cooldown_until_utc'])
        attempts = 0
    elif kind == 'temporary_backoff':
        delay = min(TEMP_BACKOFF_BASE_SECONDS * (2 ** attempts),
                    TEMP_BACKOFF_CAP_SECONDS)
        delay = delay * (1 + random.uniform(0, TEMP_BACKOFF_JITTER))
        candidate = now + timedelta(seconds=delay)
    else:
        raise ValueError(f'unknown kind {kind!r}')
    kept_later = False
    if row is not None:
        existing_until = datetime.fromisoformat(row['cooldown_until_utc'])
        if existing_until > candidate and row['state'] == 'cooling':
            candidate = existing_until  # 早事件不缩短已有更晚冷却
            kept_later = True
    epoch = (row['epoch'] + 1) if row is not None else 1
    conn.execute('INSERT INTO cooldowns(quota_group, state, cooldown_until_utc, '
                 'reason, source, recorded_at_utc, backoff_attempts, probe_active, '
                 'epoch, probe_attempt) VALUES(?,?,?,?,?,?,?,0,?,NULL) '
                 'ON CONFLICT(quota_group) DO UPDATE SET '
                 'state=excluded.state, cooldown_until_utc=excluded.'
                 'cooldown_until_utc, reason=excluded.reason, source=excluded.'
                 'source, recorded_at_utc=excluded.recorded_at_utc, '
                 'backoff_attempts=excluded.backoff_attempts, probe_active=0, '
                 'epoch=excluded.epoch, probe_attempt=NULL',
                 (quota_group, 'cooling', _iso(candidate),
                  classification.get('reason'), source, _iso(now), attempts, epoch))
    return {'recorded': True, 'quota_group': quota_group, 'kind': kind,
            'cooldown_until_utc': _iso(candidate), 'epoch': epoch,
            'monotonic_kept_later': kept_later}


def _clear_cooldown_tx(conn, quota_group, attempt, now) -> dict:
    """事务内带 epoch/attempt 核验的成功清除（缺陷 E）：冷却必须自本次 attempt
    开始后未被更新，probe 成功还必须匹配当前在飞 probe 的 attempt；迟到成功一律
    拒绝，不清除更新的限流。"""
    row = conn.execute('SELECT * FROM cooldowns WHERE quota_group=?',
                       (quota_group,)).fetchone()
    if row is None:
        return {'cleared': False, 'reasons': ['no cooldown recorded for this group']}
    observed = attempt.get('cooldown_epoch') if isinstance(attempt, dict) else None
    if observed != row['epoch']:
        return {'cleared': False, 'reasons': [
            f'stale success: cooldown epoch is {row["epoch"]} but this attempt '
            f'observed {observed}; a late success must not clear a newer cooldown']}
    if isinstance(attempt, dict) and attempt.get('purpose') == 'probe' \
            and row['probe_attempt'] != attempt.get('attempt_id'):
        return {'cleared': False, 'reasons': [
            'probe success does not match the currently active probe attempt; '
            'not clearing']}
    conn.execute('DELETE FROM cooldowns WHERE quota_group=?', (quota_group,))
    return {'cleared': True, 'quota_group': quota_group, 'epoch_cleared': row['epoch'],
            'cleared_at_utc': _iso(now),
            'note': 'observed terminal success for this channel; provider-side quota '
                    'state remains provider-managed and is not claimed verified'}


def settle_attempt(store_path, attempt: dict, *, terminal: str, success=False,
                   classification=None, source=None, now=None) -> dict:
    """attempt 生命周期结算（缺陷 C）：真实终态识别 → 冷却写入/清除 → 工作区+通道
    占位释放，在同一 BEGIN IMMEDIATE 事务内原子完成，杜绝“429 已发生但冷却未落库
    就释放占位”的二次派发空窗。

    terminal：
    - 'confirmed_exit'：子进程已退出（returncode 已知）。success=True 走带核验的
      清除；classification 为 429 分类则落冷却；非 429 失败/取消/超时不写冷却、
      不写 healthy，只结算占位。probe 的 probe_active 一并结算。
    - 'start_failed'：Popen/建目录前失败，子进程从未启动 → 安全释放占位（probe
      占位一并结算，通道不锁死）。
    - 'unknown'：communicate/wait 异常，终态未确认 → 保留工作区+通道+probe 占位
      （fail-closed），由人工查证释放路径处理。
    attempt 是 gate_dispatch 的返回值（含 attempt_id/workspace/quota_group/
    purpose/cooldown_epoch）。"""
    if terminal not in ('confirmed_exit', 'start_failed', 'unknown'):
        raise ValueError("terminal must be 'confirmed_exit', 'start_failed' or 'unknown'")
    if not isinstance(attempt, dict) or not attempt.get('attempt_id'):
        raise ValueError('attempt must be the dict returned by gate_dispatch')
    now = now or utcnow()
    group = attempt['quota_group']
    ws = attempt['workspace']
    aid = attempt['attempt_id']
    with closing(_connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            wslock = conn.execute('SELECT * FROM workspace_locks WHERE workspace=?',
                                  (ws,)).fetchone()
            chlock = conn.execute('SELECT * FROM channel_locks WHERE quota_group=?',
                                  (group,)).fetchone()
            owns_ws = wslock is not None and wslock['attempt'] == aid
            owns_ch = chlock is not None and chlock['attempt'] == aid
            if not owns_ws and not owns_ch:
                conn.execute('ROLLBACK')
                return {'settled': False, 'released': False, 'terminal_state': terminal,
                        'quota_outcome': None,
                        'reasons': ['attempt owns no current workspace/channel '
                                    'placeholder for this workspace/group']}
            # BW-ZCODE-MANUAL-QUOTA-20261008-S1：按**已核验占位的真实 runtime** 判定是否
            # 取消自动额度冷却——只有本 attempt 自己持有的 workspace/channel 占位携带的
            # runtime 才可信（不凭任务书自报）。ZCode：确认终态/start_failed 只释放本次
            # 占位，绝不新增/延长/清除任何冷却，也绝不触碰同组（如 unknown-shared）里
            # CodeBuddy 记录的冷却；unknown 与所有 runtime 一样保守保留占位。CodeBuddy
            # 及其它 runtime 的额度冷却规则完全不变。
            holder = wslock if owns_ws else chlock
            actual_runtime = holder['runtime']
            skip_quota = _auto_quota_cooldown_disabled(actual_runtime)
            if terminal == 'unknown':
                conn.execute('ROLLBACK')
                return {'settled': False, 'released': False,
                        'terminal_state': 'unknown', 'quota_outcome': None,
                        'auto_cooldown_disabled': skip_quota,
                        'reasons': ['subprocess terminal state could not be confirmed; '
                                    'workspace/channel/probe placeholders retained '
                                    'conservatively (fail-closed); use the manual '
                                    'verified release path after checking the executor']}
            outcome = None
            if skip_quota:
                # ZCode：不写/不清/不延长冷却，也绝不动 cooldowns 表；429 分类只在返回里
                # 如实报告，绝不伪造 success/attempt/epoch。
                if terminal == 'confirmed_exit' and classification is not None \
                        and classification.get('is_quota_429'):
                    outcome = {'recorded': False, 'cleared': False,
                               'auto_cooldown_disabled': True, 'runtime': actual_runtime,
                               'reasons': [
                                   f'auto quota cooldown disabled for runtime '
                                   f'{actual_runtime!r} (manual quota management); the '
                                   f'captured 429 (kind={classification.get("kind")!r}) '
                                   f'is reported but NOT written, and no existing '
                                   f'cooldown is added, extended, cleared or probed']}
                else:
                    outcome = {'recorded': False, 'cleared': False,
                               'auto_cooldown_disabled': True, 'runtime': actual_runtime,
                               'reasons': [
                                   f'auto quota cooldown disabled for runtime '
                                   f'{actual_runtime!r}: placeholder released only; no '
                                   f'cooldown written, cleared, extended or probed']}
            elif terminal == 'confirmed_exit':
                if success:
                    outcome = _clear_cooldown_tx(conn, group, attempt, now)
                elif classification is not None and classification.get('is_quota_429'):
                    if classification.get('kind') in KNOWN_KINDS:
                        outcome = _record_cooldown_tx(conn, group, classification,
                                                      source or 'settle_attempt', now)
                    else:
                        outcome = {'recorded': False, 'reasons': [
                            f'unknown kind {classification.get("kind")!r}']}
                # 非 429 失败/取消/超时：不写冷却，也绝不写 healthy。
            # start_failed 与 confirmed_exit 都释放本次占位；本 attempt 的 probe 残留
            # （在飞占位）一并结算，失败不写 healthy、不永久锁死通道。
            # ZCode（skip_quota）绝不写 cooldowns 表——连本组 probe 残留也不 UPDATE，
            # 以免误清同组（unknown-shared）里 CodeBuddy 的冷却行。
            if not skip_quota:
                conn.execute('UPDATE cooldowns SET probe_active=0, probe_attempt=NULL '
                             'WHERE quota_group=? AND probe_attempt=?', (group, aid))
            conn.execute('DELETE FROM workspace_locks WHERE workspace=? AND attempt=?',
                         (ws, aid))
            conn.execute('DELETE FROM channel_locks WHERE quota_group=? AND attempt=?',
                         (group, aid))
            conn.execute('COMMIT')
        except Exception:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise
    return {'settled': True, 'released': True, 'terminal_state': terminal,
            'quota_outcome': outcome, 'auto_cooldown_disabled': skip_quota,
            'purpose': attempt.get('purpose'), 'quota_group': group}


def release_lock(store_path, workspace, pid=None, attempt=None, now=None) -> dict:
    """终态后释放工作区+通道占位（进程内路径，绑定本 attempt 或本 pid）。pid 不匹配
    拒绝（不能释放别人的占位）。人工释放死/未知 owner 用 release_lock_terminal。"""
    ws = _norm_workspace(workspace)
    now = now or utcnow()
    with closing(_connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            lock = conn.execute('SELECT * FROM workspace_locks WHERE workspace=?',
                                (ws,)).fetchone()
            if lock is None:
                conn.execute('ROLLBACK')
                return {'released': False, 'reasons': ['no lock held for this workspace']}
            if pid is not None and lock['pid'] != pid:
                conn.execute('ROLLBACK')
                return {'released': False,
                        'reasons': [f"lock pid {lock['pid']} != caller pid {pid}; refusing"]}
            if attempt is not None and lock['attempt'] != attempt:
                conn.execute('ROLLBACK')
                return {'released': False,
                        'reasons': [f"lock attempt {lock['attempt']!r} != caller attempt "
                                    f"{attempt!r}; refusing"]}
            group = lock['quota_group']
            aid = lock['attempt']
            conn.execute('DELETE FROM workspace_locks WHERE workspace=?', (ws,))
            if aid:
                conn.execute('DELETE FROM channel_locks WHERE quota_group=? AND '
                             'attempt=?', (group, aid))
                conn.execute('UPDATE cooldowns SET probe_active=0, probe_attempt=NULL '
                             'WHERE quota_group=? AND probe_attempt=?', (group, aid))
            conn.execute('COMMIT')
        except Exception:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise
    return {'released': True, 'workspace': ws, 'released_at_utc': _iso(now)}


def release_lock_terminal(store_path, workspace, *, owner_pid=None,
                          confirm_owner_terminal=False, now=None) -> dict:
    """死/未知 owner 的人工释放路径（缺陷 H）：必须显式 confirm_owner_terminal
    （操作者已查证原执行器终态：summary/process 证据或进程表）；记录的 owner pid
    仍存活则一律拒绝——绝不释放活执行器。连同该 attempt 的通道占位与 probe 残留
    一起正确结算。"""
    ws = _norm_workspace(workspace)
    now = now or utcnow()
    with closing(_connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            lock = conn.execute('SELECT * FROM workspace_locks WHERE workspace=?',
                                (ws,)).fetchone()
            channel = None
            if lock is not None:
                channel = conn.execute('SELECT * FROM channel_locks WHERE quota_group=?',
                                       (lock['quota_group'],)).fetchone()
            if lock is None and channel is None:
                conn.execute('ROLLBACK')
                return {'released': False,
                        'reasons': ['no workspace or channel lock held for this '
                                    'workspace']}
            holder = lock if lock is not None else channel
            if not confirm_owner_terminal:
                conn.execute('ROLLBACK')
                return {'released': False,
                        'reasons': ['manual release requires explicit confirmation that '
                                    "the owner's terminal state was verified "
                                    '(--confirm-owner-terminal); refusing blindly']}
            if owner_pid is not None and holder['pid'] != owner_pid:
                conn.execute('ROLLBACK')
                return {'released': False,
                        'reasons': [f"recorded owner pid {holder['pid']} != "
                                    f"--owner-pid {owner_pid}; refusing"]}
            if _pid_alive(holder['pid']):
                conn.execute('ROLLBACK')
                return {'released': False,
                        'reasons': [f"owner pid {holder['pid']} appears alive; a live "
                                    f"executor is never released; verify the pid and "
                                    f"its terminal state first"]}
            group = holder['quota_group']
            aid = holder['attempt']
            if lock is not None:
                conn.execute('DELETE FROM workspace_locks WHERE workspace=?', (ws,))
            if channel is not None:
                conn.execute('DELETE FROM channel_locks WHERE quota_group=?', (group,))
            elif aid:
                conn.execute('DELETE FROM channel_locks WHERE quota_group=? AND '
                             'attempt=?', (group, aid))
            if aid:
                conn.execute('UPDATE cooldowns SET probe_active=0, probe_attempt=NULL '
                             'WHERE quota_group=? AND probe_attempt=?', (group, aid))
            conn.execute('COMMIT')
        except Exception:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise
    return {'released': True, 'workspace': ws, 'quota_group': group,
            'settled_attempt': aid, 'released_at_utc': _iso(now),
            'note': 'manual release after verified terminal state; probe residue '
                    'settled; cooldown itself is NOT cleared by a manual release'}


def record_quota_event(store_path, quota_group, classification, *, source,
                       now=None) -> dict:
    """把 classify_quota_failure 的结果落库（独立入口/import 用；入口结算优先走
    settle_attempt）。单调性：已有更晚冷却不被更早事件缩短；epoch 递增。"""
    now = now or utcnow()
    if not classification.get('is_quota_429'):
        return {'recorded': False, 'reasons': ['classification is not a quota event']}
    if classification.get('kind') not in KNOWN_KINDS:
        return {'recorded': False,
                'reasons': [f'unknown kind {classification.get("kind")!r}']}
    with closing(_connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            result = _record_cooldown_tx(conn, quota_group, classification, source, now)
            conn.execute('COMMIT')
        except Exception:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise
    return result


def record_success(store_path, quota_group, *, purpose='dispatch', attempt=None,
                   cooldown_epoch=None, now=None) -> dict:
    """真实终态成功清除冷却（缺陷 E 后的保守口径）：必须绑定本次 attempt（gate_dispatch
    返回值，携带 cooldown_epoch；probe 还携带 attempt_id），且冷却自 attempt 开始后
    未被更新才允许清除；迟到成功不得覆盖新限流。无绑定的裸清除一律拒绝。"""
    now = now or utcnow()
    if attempt is None:
        attempt = {'purpose': purpose, 'cooldown_epoch': cooldown_epoch}
    elif cooldown_epoch is not None:
        attempt = dict(attempt)
        attempt.setdefault('cooldown_epoch', cooldown_epoch)
    with closing(_connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute('SELECT * FROM cooldowns WHERE quota_group=?',
                               (quota_group,)).fetchone()
            if row is None:
                conn.execute('COMMIT')
                return {'cleared': False,
                        'reasons': ['no cooldown recorded for this group']}
            result = _clear_cooldown_tx(conn, quota_group, attempt, now)
            if result.get('cleared'):
                result['purpose'] = purpose
            conn.execute('COMMIT')
        except Exception:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise
    return result


def import_terminal(store_path, terminal_path, *, runtime, identity, routes_path=None,
                    now=None) -> dict:
    """离线导入终态 JSON（例如 tests/fixtures/quota-terminal.synthetic.json 之类的
    随包合成样本或调用方自备的脱敏文件）：
    只读 errors/errors_info 结构，按路由映射组后记录冷却，不改写原件。

    BW-ZCODE-MANUAL-QUOTA-20261008-S1：runtime='zcode' 取消自动额度冷却——仍完整提取并
    返回 429 分类/原因（供审计），但**不写 cooldown**，也绝不新增/延长/清除/探测任何
    冷却；返回里显式声明自动冷却已禁用。CodeBuddy 及其它 runtime 的导入规则不变。"""
    terminal = json.loads(Path(terminal_path).read_text(encoding='utf-8'))
    errors = terminal.get('errors')
    errors_info = terminal.get('errors_info')
    retry_after = extract_retry_after(errors, errors_info)
    classification = classify_quota_failure(errors, errors_info,
                                            retry_after=retry_after, now=now)
    routes = load_routes(routes_path)
    resolution = resolve_group(routes, runtime, identity or {})
    if _auto_quota_cooldown_disabled(runtime):
        return {'classification': classification, 'resolution': resolution,
                'recorded': None, 'auto_cooldown_disabled': True,
                'note': ('auto quota cooldown disabled for this runtime (ZCode manual '
                         'quota management): the 429 classification/reason is extracted '
                         'and returned for audit but NOT written, so no cooldown is '
                         'added, extended, cleared or probed; CodeBuddy and other '
                         'runtimes keep the original import rules')}
    recorded = None
    if classification['is_quota_429']:
        recorded = record_quota_event(store_path, resolution['quota_group'],
                                      classification,
                                      source=f'offline terminal import: {terminal_path}',
                                      now=now)
    return {'classification': classification, 'resolution': resolution,
            'recorded': recorded}


# ---------------------------------------------------------------- probe 边界
def validate_probe_dispatch(*, probe, tools_items, resume_session_id, prompt_bytes,
                            dispatch_plan, timeout_seconds=None) -> list:
    """recovery probe 的程序化边界（缺陷 D）：只读工具、禁 resume、调度提供的有界
    小提示词、禁携带完整任务 plan、显式时限。probe 不是重跑工程上下文的通道；
    返回问题列表（空=通过）。入口把实际边界记入 request.quota_probe_bounds。"""
    problems = []
    if not probe:
        return problems
    if dispatch_plan:
        problems.append('recovery probe cannot carry --dispatch-plan; a probe is a '
                        'bounded verification, never a full task dispatch')
    clash = [t for t in (tools_items or []) if t in PROBE_FORBIDDEN_TOOLS]
    if clash:
        problems.append(f'recovery probe must not grant write/execute tools '
                        f'{clash}; probe is read-only and side-effect free '
                        f'(forbidden: {list(PROBE_FORBIDDEN_TOOLS)})')
    if resume_session_id:
        problems.append('recovery probe must not resume an old session '
                        f'(--resume-session-id {resume_session_id!r}); a probe is a '
                        'single fresh bounded execution')
    size = len(prompt_bytes) if isinstance(prompt_bytes, (bytes, bytearray)) else None
    if size is None:
        problems.append('recovery probe requires the scheduler-supplied minimal prompt '
                        'bytes for a bounded-size check')
    elif size > PROBE_PROMPT_MAX_BYTES:
        problems.append(f'recovery probe prompt is {size} bytes, exceeding the '
                        f'{PROBE_PROMPT_MAX_BYTES}-byte bound; supply the minimal '
                        'verification prompt, never the full long task or engineering '
                        'context')
    if timeout_seconds is not None:
        if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) \
                or not PROBE_TIMEOUT_MIN_SECONDS <= timeout_seconds <= \
                PROBE_TIMEOUT_MAX_SECONDS:
            problems.append(f'--quota-probe-timeout-seconds must be an integer in '
                            f'[{PROBE_TIMEOUT_MIN_SECONDS}, '
                            f'{PROBE_TIMEOUT_MAX_SECONDS}]')
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='persistent quota cooldown & routing gate')
    ap.add_argument('--store', default=str(default_store_path()))
    ap.add_argument('--routes', default=str(default_routes_path()))
    sub = ap.add_subparsers(dest='command', required=True)
    st = sub.add_parser('status')
    st.add_argument('--store', default=argparse.SUPPRESS)
    st.add_argument('--group', default=None)
    imp = sub.add_parser('import')
    imp.add_argument('--store', default=argparse.SUPPRESS)
    imp.add_argument('--terminal', required=True)
    imp.add_argument('--runtime', required=True)
    imp.add_argument('--identity', required=True,
                     help='JSON object of channel identity (e.g. resolved runtime '
                          'entry/cli paths or provider)')
    rl = sub.add_parser('release-lock')
    rl.add_argument('--store', default=argparse.SUPPRESS)
    rl.add_argument('--workspace', required=True)
    rl.add_argument('--owner-pid', dest='owner_pid', type=int, default=None,
                    help='pid recorded on the lock being released')
    rl.add_argument('--confirm-owner-terminal', dest='confirm_owner_terminal',
                    action='store_true',
                    help='Operator asserts the recorded owner pid has reached a '
                         'verified terminal state (checked against summary/process '
                         'evidence); a pid that still appears alive is never released')
    zs = sub.add_parser('zcode-status')
    zs.add_argument('--store', default=argparse.SUPPRESS)
    zs.add_argument('--provider', default=None,
                    help='Optional filter: only show the availability row of channels '
                         'whose provider matches this string')
    zr = sub.add_parser('zcode-reset')
    zr.add_argument('--store', default=argparse.SUPPRESS)
    zr.add_argument('--provider', required=True)
    zr.add_argument('--quota-group', dest='quota_group', default=None,
                    help='Explicit quota group; resolved from --routes when omitted')
    zr.add_argument('--evidence-ref', dest='evidence_ref', required=True,
                    help='Reference to the explicit user reset / new recovery evidence '
                         'binding this reset (required, never fabricated)')
    zr.add_argument('--epoch', type=int, default=None,
                    help='Optional epoch the reset is bound to; a stale epoch is refused')
    args = ap.parse_args(argv)
    if args.command == 'status':
        print(json.dumps(get_status(args.store, args.group), ensure_ascii=False, indent=2))
        return 0
    if args.command == 'zcode-status':
        channels = zcode_availability_status(args.store)
        if args.provider:
            channels = {k: v for k, v in channels.items()
                        if v.get('provider') == args.provider}
        print(json.dumps({'store': str(args.store), 'zcode_availability': channels},
                         ensure_ascii=False, indent=2))
        return 0
    if args.command == 'zcode-reset':
        result = zcode_manual_reset(args.store, provider=args.provider,
                                    quota_group=args.quota_group,
                                    routes_path=args.routes,
                                    evidence_ref=args.evidence_ref,
                                    epoch=args.epoch)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['reset'] else 2
    if args.command == 'import':
        identity = json.loads(args.identity)
        result = import_terminal(args.store, args.terminal, runtime=args.runtime,
                                 identity=identity, routes_path=args.routes)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        # ZCode 自动冷却禁用视为正常导入（0）；CodeBuddy 等仍以冷却是否成功落库判定。
        return 0 if result.get('auto_cooldown_disabled') \
            or not result['classification']['is_quota_429'] \
            or (result['recorded'] or {}).get('recorded') else 2
    if args.command == 'release-lock':
        if not args.confirm_owner_terminal:
            print(json.dumps({
                'released': False,
                'reasons': ['manual release requires --confirm-owner-terminal after '
                            'verifying the recorded owner pid really finished (check '
                            'its summary.json/process.json); a live executor is never '
                            'released']}, ensure_ascii=False, indent=2))
            return 2
        result = release_lock_terminal(args.store, args.workspace,
                                       owner_pid=args.owner_pid,
                                       confirm_owner_terminal=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['released'] else 2
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
