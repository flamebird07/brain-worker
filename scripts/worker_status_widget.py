"""桌面浮窗：只读展示跨对话牛马（dispatch pool）工作状态。

BW-WORKER-WIDGET-20261018-S2 边界（必须遵守）：
- 本模块 import 时**不创建 Tk、不连接任何数据库、不扫描任何路径**；tkinter 只在
  `run_gui()` 内部惰性导入，`--once` 路径完全不加载 GUI。
- 数据只来自既有共享 SQLite 调度池（`dispatch_pool.default_store_path()` /
  `BRAIN_WORKER_DISPATCH_STORE` / 显式 `--store`），经 `stage_selection._open_store_ro`
  的 `mode=ro` 短事务只读快照：绝不建库/建表/迁移/WAL、绝不 reconcile、绝不调用会
  初始化数据库的 `dp.status`、绝不 kill/finish 任何 worker。
- 占用计数以 attempts 表的 `reserved/running/unknown`（`dp.ACTIVE_STATES`）为权威：
  子 PID 死亡、创建身份不明或 PID 复用**都不释放占位**；但展示层主行状态绝不只信
  DB `running`——没有匹配活 PID 的 running 在主行显著标「待核」（待核优先，不被同池
  另一个真 running 掩盖），只有创建身份匹配的活 PID 才显示「运行中」。
- 两个 CodeBuddy 模型（deepseek-v4.1-flash / glm-5.3-flash）与两个 OpenCode 免费模型
  （deepseek-v4.1-flash-free / glm-5.3-flash-free）**各自独立 1 槽、互不占位互斥**，
  各行独立计数，物理总 16（`dp.DOMESTIC_TOTAL_CAPACITY`）。
- Luna（`luna:native`）为可选外援行：无上限、不计入 16、不套本机 child PID 探针。
- 界面不显示 token、argv、prompt、原始错误正文或任何秘密；`--once` 输出的 JSON
  同样只含脱敏后的状态字段。
- 空闲自动关机（BW-WIDGET-IDLE-20261018-S2）：默认不勾选、不持久化；勾选后连续
  30 分钟“有效空闲”才由 GUI 路径经 finalize 复核触发系统既有
  `shutdown.exe /s /t 0`（不加 /f、不重启）；import/--once/未勾选/关闭绝不触发。
- 桌面交互（BW-WIDGET-HOVER-20261019-S1/S2）：默认精简只列 `display_state=running`
  的行；hover 整窗展开全部 12 行，移出边界约 200ms 收回；精简/展开同宽 560、动态高度
  并夹紧屏幕（贴底时整体上移、完整内容优先）；无手动收起/展开按钮；hover 只切视图，
  绝不勾选/停用/重置空闲关机 controller。真实运行中的行（含未知+运行混池）不展示历史
  限额，`prev_limited` 原始证据与 30 分钟计时保持不变。
"""
import argparse
import json
import queue
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import dispatch_pool as dp  # noqa: E402
import stage_selection as ss  # noqa: E402
try:
    import deepseek_offpeak as offpeak  # noqa: E402
except Exception:  # noqa: BLE001 - 缺模块时 Claude 时段外退回纯「时段外」标签
    offpeak = None

# ------------------------------------------------------------------ 展示常量
# 11 个可派组合按现有主力优先级显示（BW-OPENCODE-20261019 层序），
# 另加可选 Luna 外援行；退休池（qodercn:DeepSeek-Flash、WorkBuddy 等）不显示成可派，
# 但其真实在途仍计入总占用（见 retired_active）。
ROW_DEFS = (
    (dp.CODEBUDDY_POOL_KEY, 'CodeBuddy／DeepSeek-V4.1-Flash', '主力 · 最高优先 · 无时段门'),
    (dp.CN_CUSTOM_MAX_POOL_KEY, 'Qoder CN／token plan／Qwen-3.8-Max', '主力 Max · 夜间窗'),
    (dp.CN_MAX_POOL_KEY, 'Qoder CN／Qwen3.8-Max', '主力 Max · 夜间窗'),
    (dp.QMAX_POOL_KEY, 'Qoder 国际／Qwen3.8-Max', '主力 Max · 国际内置 · 夜间窗'),
    (dp.CLAUDE_POOL_KEY, 'Claude Code／deepseek-flash', '主力 · 空闲时段门'),
    (dp.ZCODE_POOL_KEY, 'ZCode／GLM-5.3', '主力 · 全天'),
    (dp.OPENCODE_DEEPSEEK_POOL_KEY, 'OpenCode／deepseek-v4.1-flash-free',
     '主力 · 独立 1 槽 · 无时段门'),
    (dp.OPENCODE_GLM_POOL_KEY, 'OpenCode／glm-5.3-flash-free',
     '主力 · 独立 1 槽 · 无时段门'),
    (dp.CODEBUDDY_GLM_POOL_KEY, 'CodeBuddy／GLM-5.3-Flash',
     '独立 1 槽 · 与 DeepSeek 额度独立 · 无证据前置门'),
    (dp.QFLASH_POOL_KEY, 'Qoder 国际／Qwen3.8-Flash', '兜底'),
    (dp.CN_FLASH_POOL_KEY, 'Qoder CN／Qwen3.8-Flash', '兜底'),
    (dp.LUNA_KEY, 'Luna／原生外援', '可选 · 无上限 · 不计入国内总数'),
)

STATE_LABELS = {'running': '运行中', 'unknown': '待核实', 'reserved': '已预留',
                'idle': '空闲', 'window_closed': '时段外', 'read_failed': '读取失败'}

# 时段外展示（仅显示层，不改时段门/容量/派工/关机行为）：
# - 三个 Qoder Max：固定夜间窗口 22:00（含）–次日 08:00（不含），不写"北京时间"。
# - Claude（deepseek-flash）：显示下一个连续可派区间（复用 deepseek_offpeak 纯函数，
#   UTC+8 工作日/周末/法定假日），周末/假日连续放行绝不硬截到"次日 09:00"；跨日/跨月
#   用"次日 HH:MM"或"MM-DD HH:MM"标注日期消歧；无网络日历。
MAX_WINDOW_CLOSED_LABEL = '时段外（22:00–次日08:00）'
_MAX_WINDOW_POOLS = tuple(getattr(dp, 'WINDOW_GATED_MAIN_FORCES', ()))


def _bj_minute_of(dt_naive) -> int:
    return dt_naive.hour * 60 + dt_naive.minute


def _claude_blocked_probe(dt_naive) -> bool:
    """naive 北京时间该分钟是否"关闭新派"（复用 offpeak 纯函数，绝不再次 to_beijing，
    避免对已是北京时间的入参二次 +8）。与 classify_dispatch 判定顺序一致：
    周末/假日放行；已知工作日仅高峰关闭；未知年度仅高峰保守关闭。"""
    if offpeak.is_weekend(dt_naive):
        return False
    if offpeak.year_known(dt_naive):
        if offpeak.is_holiday(dt_naive):
            return False
        return offpeak.in_peak_minutes(dt_naive)
    return offpeak.in_peak_minutes(dt_naive)


def claude_next_open_window(bj_now):
    """给定 naive 北京时间，若当前不可派，返回下一个连续可派区间的 (start, end)
    （均为 naive datetime，end 不含）。可派或 offpeak 不可用时返回 None。
    纯函数、无网络；周末/法定假日整天放行，故放行区间可连续跨过整个假期/周末，
    绝不硬截成"次日 09:00"。"""
    if offpeak is None or bj_now is None:
        return None
    if not _claude_blocked_probe(bj_now):
        return None
    midnight = bj_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = None
    for lo, hi in offpeak.PEAK_INTERVALS:
        if lo <= _bj_minute_of(bj_now) < hi:
            start = midnight + timedelta(minutes=hi)
            break
    if start is None:
        return None
    end = start
    cap = 15 * 24 * 60
    opened = False
    for _ in range(cap):
        if _claude_blocked_probe(end):
            opened = True
            break
        end = end + timedelta(minutes=1)
    if not opened:
        return None
    return (start, end)


def _fmt_claude_interval(start, end):
    """把 (start,end) naive 北京时间格式化成"下次…"片段。同日只写 HH:MM；目标日正好
    是次日写"次日HH:MM"；更远的跨日/跨月写"MM-DD HH:MM"消歧（周末/假日连续放行）。"""
    s = f'{start:%H:%M}'
    day_delta = (end.date() - start.date()).days
    if day_delta <= 0:
        e = f'{end:%H:%M}'
    elif day_delta == 1:
        e = f'次日{end:%H:%M}'
    else:
        e = f'{end:%m-%d %H:%M}'
    return f'下次{s}–{e}'


def window_closed_display_label(pk, now):
    """时段外行的展示标签（仅显示）。三 Qoder Max → 固定夜间窗口；Claude → 下一个
    连续可派区间；offpeak 缺失/无法解析时退回纯 STATE_LABELS['window_closed']。"""
    if pk in _MAX_WINDOW_POOLS:
        return MAX_WINDOW_CLOSED_LABEL
    if pk == getattr(dp, 'CLAUDE_POOL_KEY', None) and offpeak is not None:
        try:
            bj = offpeak.to_beijing(now if now is not None else dp.utcnow())
            win = claude_next_open_window(bj)
            if win is not None:
                return f'时段外（{_fmt_claude_interval(*win)}）'
        except Exception:  # noqa: BLE001 - 展示层永不冒进，退回纯标签
            pass
    return STATE_LABELS['window_closed']

RESULT_LABELS = {'passed': '上轮调用通过', 'failed': '上轮调用未通过',
                 'unknown': '上轮调用待核', 'none': '暂无调用记录'}
PID_CHECK_LABELS = {
    'no_binding': '未绑定子进程',
    'native': '云端原生 · 不探本机进程',
    'alive_matched': '本机进程存活 · 创建身份匹配',
    'alive_unmatched': 'PID 存活但创建身份不明/复用 · 待核',
    'dead': '子进程已退出 · 占位仍待对账（不自动释放）',
    'unknown': '进程状态未知 · 待核',
}
# 在途状态集合（成员判断用）：有任何在途就绝不显示“时段外/空闲”（时段门只影响新派
# 资格，不能把在途遮盖）。
_STATE_PRIORITY = ('running', 'unknown', 'reserved')
# 展示层主行状态优先序（BW-WORKER-WIDGET-20261018-S3 root 审查修正）：待核优先——
# 同池只要有一个 PID 探针未确认的在途（死 PID/复用/探测未知/running 却无匹配活 PID），
# 主行必须显著「待核实」，绝不被同池另一个真 running 掩盖成绿灯。
_DISPLAY_PRIORITY = ('unknown', 'running', 'reserved')
# BW-WIDGET-QUOTA-20261019-S5：主行“上轮限额”展示标签（纯展示，绝不写回 DB/容量/idle 判定）。
PREV_LIMIT_LABEL = '上轮限额'
# BW-WIDGET-QUOTA30-20261019-S3：CodeBuddy／deepseek-v4.1-flash 精确池真实限额标记的展示
# 期限——从绑定 attempt 的 ended_at_utc（终态未落盘回退标记 recorded_at_utc）起 0<=age<1800 秒
# 显示；恰好 1800 秒及更久只撤下历史提示，绝不声称额度恢复。纯展示窗口，不改 DB/调度/重试/冷却。
QUOTA_LIMIT_WINDOW_SECONDS = 1800
# BW-LIMIT30-20261019-S3：浮窗 30 分钟窗口覆盖 CodeBuddy/deepseek、ZCode/GLM-5.3 与
# Claude/deepseek-flash 三个精确池；其它池（Qoder Max 等）保持原有 token 规则。
WINDOW_LIMIT_POOLS = (dp.CODEBUDDY_POOL_KEY, dp.ZCODE_POOL_KEY, dp.CLAUDE_POOL_KEY)


def _within_recent_window(ref_text, now, window_seconds):
    """半小时展示窗口判定：ref 必须为带时区、非未来的过去时间，且 0<=age<window。
    缺失/无法解析/无时区/未来时间一律返回 False（不伪造有效窗口）；恰好 window 及更久 False。
    只读时间戳，绝不用 import 时刻或 now 顶替原始失败时间。"""
    ref = _parse_iso(ref_text)
    if ref is None:
        return False
    if ref.tzinfo is None:
        return False
    age = (now - ref).total_seconds()
    return 0 <= age < window_seconds


def _quota_window_limited(lim, bound_ended, now, window_seconds=QUOTA_LIMIT_WINDOW_SECONDS):
    """CodeBuddy deepseek 精确池专用：真实限额标记按绑定 attempt 的真实终止时间是否仍落在
    半小时内展示，**不再用最新 attempt_token 相等**让新预留/新失败把半小时内真实限额提前隐藏。
    重复限额由现有入口更新同一标记与其绑定 attempt（更晚的 ended_at_utc），自然重新计时。"""
    if not lim or int(lim.get('limited') or 0) != 1:
        return False
    ref_text = bound_ended or lim.get('recorded_at_utc')
    return _within_recent_window(ref_text, now, window_seconds)


def _prev_limited_for(pk, lim, latest_any, bound_ended, now):
    """按池选择限额展示规则：CodeBuddy/deepseek、ZCode/GLM-5.3 与 Claude/deepseek-flash
    使用半小时真实窗口（BW-LIMIT30-20261019-S3 扩展）；其余池保持原有“标记仍绑定该池最新
    attempt”的既有规则，不改动其它模型现有期限。"""
    if pk in WINDOW_LIMIT_POOLS:
        return _quota_window_limited(lim, bound_ended, now)
    return bool(
        lim is not None and int(lim.get('limited') or 0) == 1
        and latest_any.get(pk, {}).get('token') is not None
        and latest_any[pk]['token'] == lim.get('attempt_token'))


def _main_state_display(state, state_label, prev_limited, has_true_running=False):
    """纯格式函数：无在途（state 不在 _STATE_PRIORITY）且限额展示窗口命中时，主状态醒目标为
    “上轮限额”。只要该行含真实 display_state=running 的 attempt，就只显示“运行中”，
    该行任何位置都不再出现历史限额（用户 2026-10-19 明确纠正：运行中就不该显示限额）。
    未知+真运行的混合池仍保持待核优先（返回待核实，不冒充全绿），只是不再附带限额文字。
    只返回展示标签，不改变 prev_limited/原始证据/30 分钟计时/占用计数/空闲判定原语义。"""
    if state == 'running' or has_true_running:
        return state_label
    if prev_limited:
        if state in _STATE_PRIORITY:
            return f'{state_label}·限额'
        return PREV_LIMIT_LABEL
    return state_label


def _display_state(db_state, pid_check):
    """主行/逐条展示用的可信状态：DB `running` 只有在探针确认「活 PID + 创建身份匹配」
    时才显示运行中，否则一律降级为待核（unknown）；`reserved` 未绑子进程仍如实显示
    已预留；Luna 原生（native）不套本机探针，DB 状态直接展示。绝不据此改 DB 或释放占位。"""
    if pid_check == 'native':
        return db_state
    if db_state == 'running':
        return 'running' if pid_check == 'alive_matched' else 'unknown'
    return db_state


def _failure(store, now, code, detail):
    return {'ok': False, 'store': str(store), 'now_utc': dp._iso(now),
            'error_code': code, 'error': detail,
            'domestic_total_capacity': dp.DOMESTIC_TOTAL_CAPACITY,
            'domestic_active': None, 'luna_active': None, 'rows': []}


def _parse_iso(text):
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text))
    except ValueError:
        return None


def _elapsed_seconds(start_text, now):
    start = _parse_iso(start_text)
    if start is None:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=now.tzinfo)
    return max(0, int((now - start).total_seconds()))


def _pid_check(row, probe):
    """只读核对 child_pid+child_created（复用 dp 权威探针与 _created_matches）。
    任何不明/复用/探测失败都如实待核，绝不据此释放占位、绝不 kill/reconcile。"""
    pid = row.get('child_pid')
    if pid is None:
        return 'no_binding'
    try:
        ident = probe(pid) or {}
    except Exception:
        return 'unknown'
    state = str(ident.get('state', 'unknown'))
    if state == 'dead':
        return 'dead'
    if state == 'alive':
        return ('alive_matched'
                if dp._created_matches(row.get('child_created'), ident.get('created'))
                else 'alive_unmatched')
    return 'unknown'


def _annotate(row, now):
    ref = row.get('started_at_utc') or row.get('reserved_at_utc')
    pid_check = row.pop('_pid_check', 'no_binding')
    db_state = row.get('state')
    disp = _display_state(db_state, pid_check)
    out = {'task_id': row.get('task_id'), 'stage': row.get('stage'),
           'state': db_state,
           'display_state': disp,
           'display_state_label': STATE_LABELS[disp],
           'reserved_at_utc': row.get('reserved_at_utc'),
           'started_at_utc': row.get('started_at_utc'),
           'child_pid': row.get('child_pid'),
           'pid_check': pid_check,
           'elapsed_seconds': (_elapsed_seconds(ref, now)
                               if db_state in ('running', 'reserved', 'unknown')
                               else None)}
    return out


# ------------------------------------------------------------------ 只读快照
def read_snapshot(store_path=None, *, now=None, pid_probe=None):
    """跨对话调度池的一份只读快照（纯函数式：时钟与 PID 探针可注入，测试友好）。

    返回 dict：ok=True 时含 rows（按 ROW_DEFS 顺序）与总占用；任何缺库/缺表/锁定/
    解析失败都返回 ok=False + error_code，**绝不回落成空表冒充空闲**，也绝不建库、
    建表、建 WAL/-shm sidecar 或写任何字节。
    """
    now = now or dp.utcnow()
    probe = pid_probe or dp.process_identity
    path = store_path if store_path else dp.default_store_path()
    try:
        conn = ss._open_store_ro(path)
    except ss.Refused as exc:
        return _failure(path, now, exc.code, exc.detail or str(exc))
    except sqlite3.Error as exc:
        return _failure(path, now, 'read_failed', str(exc))
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='attempts'").fetchone() is None:
            return _failure(path, now, 'schema_missing',
                            'no attempts schema (read-only refusal, never migrated)')
        placeholders = ','.join('?' * len(dp.ACTIVE_STATES))
        active = [dict(r) for r in conn.execute(
            'SELECT task_id, stage, pool_key, state, child_pid, child_created, '
            'reserved_at_utc, started_at_utc FROM attempts '
            f'WHERE state IN ({placeholders})', dp.ACTIVE_STATES).fetchall()]
        # 每个池“最新一条已落终态”的 attempt：只读真实 success/terminal/ended_at_utc。
        last_terminal = {r['pool_key']: dict(r) for r in conn.execute(
            'SELECT pool_key, state, terminal, success, ended_at_utc FROM attempts a '
            'WHERE a.terminal IS NOT NULL AND rowid=(SELECT MAX(rowid) FROM attempts b '
            "WHERE b.pool_key=a.pool_key AND b.terminal IS NOT NULL)")}
        # 每个池“最新一条”attempt（任意状态）：仅用于主力限额标记是否仍绑定最新对应 attempt。
        latest_any = {r['pool_key']: dict(r) for r in conn.execute(
            'SELECT pool_key, token FROM attempts a '
            'WHERE rowid=(SELECT MAX(rowid) FROM attempts b WHERE b.pool_key=a.pool_key)')}
        limits = {}
        try:
            for r in conn.execute('SELECT pool_key, limited, attempt_token, '
                                  'recorded_at_utc FROM main_force_limits'):
                limits[r['pool_key']] = dict(r)
        except sqlite3.Error:
            limits = {}
        # BW-WIDGET-QUOTA30-20261019-S3 / BW-LIMIT30-20261019-S3：三个半小时窗口池的展示
        # 需要其绑定 attempt 的真实 ended_at_utc（只读，绝不改写限额写入端或 attempt）。
        bound_ended = {}
        for pk_l, lim_l in limits.items():
            if pk_l not in WINDOW_LIMIT_POOLS:
                continue
            if int(lim_l.get('limited') or 0) != 1 or not lim_l.get('attempt_token'):
                continue
            try:
                br = conn.execute('SELECT ended_at_utc FROM attempts WHERE token=?',
                                  (lim_l['attempt_token'],)).fetchone()
            except sqlite3.Error:
                br = None
            if br is not None:
                bound_ended[pk_l] = dict(br).get('ended_at_utc')
        # 空闲关机用的最小活动签名（同一只读事务内）：仅 rowid/计数/最近时间戳，
        # 绝无 token/prompt/错误正文。两个刷新间隔内开始又结束的短调用会改变
        # count/max_rowid/max_ended，计时器据此重置，不能只看 active==0。
        act = conn.execute(
            'SELECT MAX(rowid) AS max_rowid, COUNT(*) AS cnt, '
            'MAX(reserved_at_utc) AS max_reserved, '
            'MAX(started_at_utc) AS max_started, '
            'MAX(ended_at_utc) AS max_ended FROM attempts').fetchone()
        activity = {'max_rowid': act['max_rowid'] or 0, 'count': act['cnt'],
                    'max_reserved': act['max_reserved'],
                    'max_started': act['max_started'],
                    'max_ended': act['max_ended']}
    except sqlite3.Error as exc:
        return _failure(path, now, 'read_failed', str(exc))
    finally:
        conn.close()

    by_pool = {}
    for row in active:
        by_pool.setdefault(row['pool_key'], []).append(row)
    domestic_active = sum(len(v) for k, v in by_pool.items() if k != dp.LUNA_KEY)
    luna_active = len(by_pool.get(dp.LUNA_KEY, []))
    shown_keys = {pk for pk, _, _ in ROW_DEFS}
    retired_active = sum(len(v) for k, v in by_pool.items()
                         if k != dp.LUNA_KEY and k not in shown_keys)

    def _annotated(row):
        pk = row['pool_key']
        check = 'native' if pk == dp.LUNA_KEY else _pid_check(row, probe)
        out = dict(row)
        out['_pid_check'] = check
        return _annotate(out, now)

    rows = []
    for pk, label, note in ROW_DEFS:
        own = by_pool.get(pk, [])
        # BW-WORKER-WIDGET-20261018-S3：两个 CodeBuddy 模型各自独立 1 槽，容量/占用
        # 只按自身 pool_key 计数，绝不合并、绝不跨模型互斥。
        capacity = None if pk == dp.LUNA_KEY else dp.CAPACITY.get(pk, 0)
        attempts = [_annotated(r) for r in own]
        # 主行状态 = 展示可信状态（PID 探针未确认的 running 降级待核），待核优先，
        # 绝不被同池另一个真 running 掩盖；DB 原始状态另行如实回显，占位计数不变。
        disp_states = {a['display_state'] for a in attempts}
        db_states = {a['state'] for a in attempts}
        state = next((s for s in _DISPLAY_PRIORITY if s in disp_states), None)
        db_state = next((s for s in _STATE_PRIORITY if s in db_states), None)
        window_blocked = bool(dp._max_window_blocks(pk, now))
        if state is None:
            state = 'window_closed' if window_blocked else 'idle'
        if db_state is None:
            db_state = state
        lt = last_terminal.get(pk)
        if lt is None:
            last_result = 'none'
        elif lt.get('success') == 1:
            last_result = 'passed'
        elif lt.get('success') == 0:
            last_result = 'failed'
        else:
            # success 缺失/NULL = 未知；绝不把 finished 一概当绿灯。
            last_result = 'unknown'
        lim = limits.get(pk)
        prev_limited = _prev_limited_for(pk, lim, latest_any,
                                          bound_ended.get(pk), now)
        has_true_running = any(a['display_state'] == 'running' for a in attempts)
        # 时段外行的展示标签富化（仅显示层）：三 Qoder Max 固定夜间窗口 / Claude 下一个
        # 连续可派区间；其它状态沿用 STATE_LABELS。
        state_label = (window_closed_display_label(pk, now)
                       if state == 'window_closed' else STATE_LABELS[state])
        rows.append({
            'pool_key': pk, 'label': label, 'note': note,
            'capacity': capacity, 'active': len(own),
            'own_active': len(own), 'state': state,
            'db_state': db_state,
            'state_label': state_label, 'window_blocked': window_blocked,
            'main_state_label': _main_state_display(
                state, state_label, prev_limited, has_true_running),
            'last_result': last_result, 'last_result_label': RESULT_LABELS[last_result],
            'prev_limited': prev_limited,
            'attempts': attempts,
        })
    return {'ok': True, 'store': str(path), 'now_utc': dp._iso(now),
            'domestic_total_capacity': dp.DOMESTIC_TOTAL_CAPACITY,
            'domestic_active': domestic_active, 'luna_active': luna_active,
            'retired_active': retired_active, 'activity': activity, 'rows': rows}


def snapshot_to_json_str(snap):
    """--once 的安全 JSON：字段全部来自 read_snapshot，天然不含 token/prompt/argv/秘密。"""
    return json.dumps(snap, ensure_ascii=False, sort_keys=True, default=str)


def format_duration(seconds):
    if seconds is None:
        return '—'
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f'{h}时{m:02d}分'
    if m:
        return f'{m}分{s:02d}秒'
    return f'{s}秒'


def _format_countdown(seconds):
    seconds = max(0, int(seconds or 0))
    m, s = divmod(seconds, 60)
    return f'{m:02d}:{s:02d}'


# ------------------------------------------------------------------ 空闲自动关机
IDLE_SHUTDOWN_SECONDS = 1800
_MAX_SNAPSHOT_AGE_SECONDS = 60.0
_ACTIVITY_KEYS = ('max_rowid', 'count', 'max_reserved', 'max_started', 'max_ended')


def _system_shutdown():
    """真实关机动作：只允许 GUI 路径在用户勾选且连续 30 分钟有效空闲、finalize
    复核通过后调用；使用系统既有 shutdown.exe /s /t 0——不加 /f、不重启、不执行
    任何业务动作、不动别人安排的关机。测试一律注入 mock，绝不真跑。"""
    subprocess.run(['shutdown.exe', '/s', '/t', '0'], check=True)


class IdleShutdownController:
    """纯控制器：monotonic 时钟、关机回调、UTC 当前时间均可注入，自身不碰 GUI/DB。

    语义（BW-WIDGET-IDLE-20261018-S2）：
    - 默认停用、不持久化；set_enabled(True) 从当刻开始计时，False 立即取消。
    - 只有“有效空闲”计时：快照 ok、字段齐全、未过期、无任何在途（国内/Luna/
      退休池的 reserved/running/unknown 都算在途，含 PID 身份待核），且活动签名
      与上次观察一致；签名变化=两个刷新间隔内有过新调用（含开始又结束的短任务），
      立即重新计满 30 分钟。
    - 读取失败/过期/字段缺失 → 清零暂停；恢复后必须重新完整 30 分钟，绝不用旧
      绿灯关机。计时用 monotonic，系统时钟跳变不会提前触发。
    - 到期（due）不直接关机：调用方必须同步取得一份新只读快照交 finalize() 复核
      （无在途且签名未变）才触发；成功后至多一次；失败置 failed 并自动停用
      （解除勾选），绝不每个刷新周期重试。
    """

    def __init__(self, *, idle_seconds=IDLE_SHUTDOWN_SECONDS, clock=time.monotonic,
                 shutdown=None, utcnow=None,
                 max_snapshot_age=_MAX_SNAPSHOT_AGE_SECONDS):
        self._idle = float(idle_seconds)
        self._clock = clock
        self._shutdown = shutdown if shutdown is not None else _system_shutdown
        self._utcnow = utcnow or dp.utcnow
        self._max_age = float(max_snapshot_age)
        self.enabled = False
        self.due = False
        self.triggered = False
        self.failed = False
        self._armed_at = None
        self._last_activity = None
        self._pause_reason = None

    # -- 用户勾选/取消
    def set_enabled(self, on):
        """勾选=从当刻重新开始计时；取消=立即取消计划。triggered 后绝不再武装
        （成功至多一次）；failed 后重新勾选视为用户新一轮授权，清除失败标记。"""
        self.enabled = bool(on) and not self.triggered
        self.failed = False
        self.due = False
        self._armed_at = None
        self._last_activity = None
        self._pause_reason = None

    # -- 快照分类
    def _snapshot_stale(self, snap):
        stamp = _parse_iso(snap.get('now_utc'))
        if stamp is None:
            return True
        now = self._utcnow()
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=now.tzinfo)
        return abs((now - stamp).total_seconds()) > self._max_age

    def _classify(self, snap):
        if not isinstance(snap, dict) or not snap.get('ok'):
            return 'read_failed', None
        try:
            counts = (int(snap['domestic_active']), int(snap['luna_active']),
                      int(snap['retired_active']))
        except (KeyError, TypeError, ValueError):
            return 'fields_missing', None
        activity = snap.get('activity')
        if (not isinstance(activity, dict)
                or any(k not in activity for k in _ACTIVITY_KEYS)):
            return 'fields_missing', None
        if self._snapshot_stale(snap):
            return 'stale', activity
        if any(c > 0 for c in counts):
            return 'busy', activity
        return 'idle', activity

    # -- 每个刷新周期喂一份最新快照
    def observe(self, snap):
        if not self.enabled or self.triggered or self.failed:
            return
        self.due = False
        verdict, activity = self._classify(snap)
        if verdict != 'idle':
            # 在途/读取失败/过期/字段缺失：清零暂停，绝不用旧绿灯继续倒计时。
            self._armed_at = None
            self._pause_reason = verdict
            if verdict == 'busy':
                self._last_activity = activity
            else:
                self._last_activity = None
            return
        self._pause_reason = None
        if activity != self._last_activity:
            # 首份有效快照或签名变化（含两刷新间隔内开始又结束的短调用）→ 重新计时。
            self._last_activity = activity
            self._armed_at = self._clock()
            return
        if self._armed_at is None:
            self._armed_at = self._clock()
        if self._clock() - self._armed_at >= self._idle:
            self.due = True

    def remaining(self):
        """剩余秒数（int≥0）；未启用/已触发/已失败返回 None，暂停期间返回整段时长。"""
        if not self.enabled or self.triggered or self.failed:
            return None
        if self._armed_at is None:
            return int(self._idle)
        return max(0, int(self._idle - (self._clock() - self._armed_at)))

    # -- 到期后的最终复核 + 唯一触发点
    def finalize(self, final_snap):
        """触发前同步复核一份新只读快照：必须 ok、字段齐、未过期、无在途且活动
        签名与到期时刻一致。返回 'shutdown'（指令已发出，至多一次）/
        'failed'（系统动作失败，已停用且不重试）/ 'aborted'（复核未过，重新计时）。"""
        if not self.enabled or not self.due or self.triggered or self.failed:
            return 'aborted'
        verdict, activity = self._classify(final_snap)
        if verdict != 'idle' or activity != self._last_activity:
            self.due = False
            self._armed_at = None
            self._pause_reason = verdict if verdict != 'idle' else 'signature_changed'
            if activity is not None:
                self._last_activity = activity
            return 'aborted'
        try:
            self._shutdown()
        except Exception as exc:
            self.failed = True
            self.enabled = False
            self.due = False
            self._armed_at = None
            self._pause_reason = 'shutdown_failed'
            self.error_text = f'自动关机执行失败（{type(exc).__name__}）· 已停用 · 不自动重试'
            return 'failed'
        self.triggered = True
        self.enabled = False
        self.due = False
        return 'shutdown'

    def status(self):
        return {'enabled': self.enabled, 'due': self.due,
                'triggered': self.triggered, 'failed': self.failed,
                'remaining': self.remaining(), 'pause_reason': self._pause_reason}


_IDLE_PAUSE_TEXT = {
    'busy': '有任务在途 · 结束后重新计30分',
    'read_failed': '读取失败 · 恢复后重新计30分',
    'stale': '快照过期 · 重新计时',
    'fields_missing': '快照字段缺失 · 重新计时',
    'signature_changed': '检测到新调用 · 重新计30分',
    'shutdown_failed': '',
}


def idle_status_text(st):
    """勾选区的简短普通话状态文案（不含错误正文/秘密）。"""
    if st.get('triggered'):
        return '关机指令已发出'
    if st.get('failed'):
        return '自动关机执行失败 · 已停用 · 不自动重试'
    if not st.get('enabled'):
        return '未开启 · 勾选后连续空闲30分钟自动关机'
    if st.get('due'):
        return '已满30分钟 · 正在最终复核'
    reason = st.get('pause_reason')
    if reason:
        return _IDLE_PAUSE_TEXT.get(reason, '暂停计时 · 恢复后重新计30分')
    return f"空闲中 · 剩余 {_format_countdown(st.get('remaining'))} 关机"


def detail_text(row):
    """选中行的底部详情文本：完整任务 ID/阶段/最近时间/进程核对，不含长路径与秘密。"""
    if not row:
        return '点击任意一行查看当前任务详情'
    lines = [f"{row['label']}（{row['pool_key']}） · {row['note']}"]
    cap = '∞' if row['capacity'] is None else str(row['capacity'])
    lines.append(f"占用 {row['active']}/{cap}"
                 + f" · 状态：{row['state_label']}"
                 + ('（时段外仅限制新派，不遮盖在途）'
                    if row['window_blocked'] and row['state'] in _STATE_PRIORITY else '')
                 + ('（新派受时段门限制）'
                    if row['window_blocked'] and row['state'] not in _STATE_PRIORITY else ''))
    if not row['attempts']:
        lines.append('当前无在途任务')
    for a in row['attempts']:
        ref = a.get('started_at_utc') or a.get('reserved_at_utc') or '—'
        state_label = a.get('display_state_label') or STATE_LABELS.get(a['state'], a['state'])
        if a.get('display_state') != a.get('state'):
            state_label += f"（DB:{STATE_LABELS.get(a['state'], a['state'])}）"
        lines.append(f"· {state_label} 任务 {a['task_id']} 阶段 {a['stage'] or '—'} "
                     f"起 {ref} 已 {format_duration(a['elapsed_seconds'])} "
                     f"[{PID_CHECK_LABELS.get(a['pid_check'], a['pid_check'])}]")
    lines.append(f"最近结果：{row['last_result_label']}"
                 + ('（success 缺失=未知，不当通过）' if row['last_result'] == 'unknown' else ''))
    # 真实运行中绝不展示历史限额（prev_limited 原始字段仍保留，只过滤展示）。
    if row['prev_limited'] and not row_has_true_running(row):
        if row['pool_key'] == dp.CODEBUDDY_POOL_KEY:
            lines.append('上轮限额：近 30 分钟内的真实限额/429 错误记录（按绑定 attempt 的真实终止时间计时），'
                         '仅代表历史期限展示窗口、不代表实时余额、不猜恢复时间、不阻止下一轮最高优先重试；'
                         '超过 30 分钟仅撤下本提示，绝不声称额度已恢复')
        elif row['pool_key'] in WINDOW_LIMIT_POOLS:
            lines.append('上轮限额：近 30 分钟内的真实限额/402 余额不足/429 错误记录（按绑定 attempt 的真实终止时间计时），'
                         '仅代表历史期限、不代表实时余额、不猜恢复时间；30 分钟内新派发跳过该池，'
                         '满 30 分钟后按原优先级恢复可选（原错误证据保留，无需手动重置）；'
                         '超过 30 分钟仅撤下本提示，绝不声称额度已恢复')
        else:
            lines.append('上轮限额：该标记仍绑定此池最新 attempt 的历史限额记录，'
                         '仅代表历史期限、不代表实时余额、不猜恢复时间、不新增冷却，也不阻止下一轮最高优先重试；'
                         '最新 attempt 变化后即不再显示，非实时额度')
    return '\n'.join(lines)


# ------------------------------------------------------------------ 单实例
_MUTEX_NAME = 'BW-WorkerStatusWidget-SingleInstance'
_mutex_handle = None


def _acquire_single_instance():
    """Windows 命名 mutex 单实例；非 Windows 或创建失败时诚实降级放行（不加第二道闸）。
    ctypes 必须显式声明 64 位指针宽度的返回/参数类型，否则 HANDLE 会被默认 c_int
    截断（句柄值失真、CloseHandle 关错或泄漏）。失败如实返回，绝不伪称已验证。"""
    global _mutex_handle
    if not sys.platform.startswith('win'):
        return True
    try:
        import ctypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        kernel32.CloseHandle.restype = ctypes.c_bool
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
        if not handle:
            return True
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        _mutex_handle = handle
        return True
    except (OSError, AttributeError):
        return True


def _release_single_instance():
    global _mutex_handle
    if _mutex_handle is not None and sys.platform.startswith('win'):
        try:
            import ctypes
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel32.CloseHandle.restype = ctypes.c_bool
            kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
            kernel32.CloseHandle(_mutex_handle)
        except (OSError, AttributeError):
            pass
    _mutex_handle = None


# ------------------------------------------------------------------ 后台轮询
class _Poller(threading.Thread):
    """后台单线程顺序轮询：读取→入队→等待，天然不重叠；Tk 只在主线程消费队列。"""

    def __init__(self, out_queue, store_path=None, interval=2.0, pid_probe=None):
        super().__init__(daemon=True)
        self._q = out_queue
        self._store = store_path
        self._interval = max(1.0, float(interval))
        self._probe = pid_probe
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                snap = read_snapshot(self._store, pid_probe=self._probe)
            except Exception:
                # 任何未预期异常都如实降级为读取失败，绝不保留旧绿灯冒充当前。
                snap = _failure(self._store or '', dp.utcnow(), 'read_failed',
                                'unexpected poller error')
            self._q.put(snap)
            self._stop.wait(self._interval)

    def stop(self):
        self._stop.set()


# ------------------------------------------------------------------ Tk 浮窗
# 现代深色小卡片：墨蓝底、分层行卡、柔和边框；绿=真运行、橙=限额、红=待核、灰=空闲。
_BG = '#0f1724'
_CARD = '#1b2738'
_CARD_SEL = '#22324a'
_BORDER = '#2a3a50'
_BORDER_SEL = '#3b82f6'
_FG = '#e8eef7'
_TITLE = '#f1f5f9'
_SUB = '#93a3b8'
_DIM = '#64748b'
_GREEN = '#5fd39a'
_RED = '#f07f7f'
_STATE_COLORS = {'running': _GREEN, 'reserved': '#9aa8c0', 'unknown': _RED,
                 'idle': _DIM, 'window_closed': '#7f9bc4', 'read_failed': _RED}
# 限额专属橙：只覆盖展示层文字/徽标，不改 row['state']/容量计数/空闲判定原语义。
_QUOTA_LIMIT_COLOR = '#f5a524'
_BADGE_LIMIT_BG = '#2b2417'
_BADGE_RUN_BG = '#173126'
_BADGE_RUN_FG = _GREEN
_BADGE_WINDOW_BG = '#17263b'
_BADGE_WINDOW_FG = '#7f9bc4'
# 精简/展开同宽 560：完整放下最长名称 + 组合状态/徽标 + 占用数，关闭按钮不追鼠标。
_WIDTH = 560
_MIN_H = 96
_HOVER_COLLAPSE_MS = 200


def row_has_true_running(row):
    """纯视图判定：只认 attempt 的 display_state=running（已由 PID 身份核对），
    绝不用 DB state=running 冒充。混池中只要有一条真运行即返回 True。"""
    return any((a or {}).get('display_state') == 'running'
               for a in (row.get('attempts') or []))


def view_rows(snap, expanded=False):
    """精简模式只列有真运行 attempt 的行；展开模式保留全部 ROW_DEFS 行。
    读取失败绝不返回可冒充空闲的空表语义（调用方另行显示待核）。"""
    if not isinstance(snap, dict) or not snap.get('ok'):
        return []
    rows = list(snap.get('rows') or [])
    if expanded:
        return rows
    return [r for r in rows if row_has_true_running(r)]


def row_shows_limit(row):
    """纯视图判定：该行是否展示历史限额。真实运行中（含未知+运行的混合池）一律不展示，
    避免“运行中·限额”误导；prev_limited 原始证据与 30 分钟计时保持原样。"""
    return bool(row.get('prev_limited')) and not row_has_true_running(row)


def view_summary(snap):
    """精简摘要的纯计数：真实总占用/容量、Luna、运行行、待核、已预留、近期限额、
    退休池在途。隐藏非运行行不丢失这些安全提示；只读快照字段，不改任何状态。
    限额计数同样排除真实运行中的行（运行中不显示限额）。"""
    if not isinstance(snap, dict) or not snap.get('ok'):
        return {'ok': False, 'total': None, 'capacity': None, 'luna': None,
                'retired': 0, 'running': 0, 'pending': 0, 'reserved': 0,
                'limited': 0}
    rows = list(snap.get('rows') or [])
    return {
        'ok': True,
        'total': snap.get('domestic_active'),
        'capacity': snap.get('domestic_total_capacity'),
        'luna': snap.get('luna_active'),
        'retired': int(snap.get('retired_active') or 0),
        'running': sum(1 for r in rows if row_has_true_running(r)),
        'pending': sum(1 for r in rows if r.get('state') == 'unknown'),
        'reserved': sum(1 for r in rows if r.get('state') == 'reserved'),
        'limited': sum(1 for r in rows if row_shows_limit(r)),
    }


def compact_state_text(row):
    """精简行主文字：有在途时用真实状态（待核优先），绝不被限额文字遮盖；
    无在途但限额窗口命中时显示“上轮限额”。组合信息由徽标另行补齐，不截断名称/占用。"""
    state = row.get('state')
    if state in _STATE_PRIORITY:
        return row.get('state_label') or STATE_LABELS.get(state, state)
    return (row.get('main_state_label') or row.get('state_label')
            or STATE_LABELS.get(state, state))


def row_badges(row):
    """行内小徽标：含运行（混池不冒充全绿）、时段外（在途也如实标注）、限额（橙色）。
    真实运行中的行不出限额徽标；只返回展示文案，不改 state/占用/空闲判定。"""
    badges = []
    if row.get('state') != 'running' and row_has_true_running(row):
        badges.append(('run', '含运行'))
    if row.get('window_blocked') and not str(compact_state_text(row)).startswith('时段外'):
        badges.append(('window', '时段外'))
    if row_shows_limit(row):
        badges.append(('limit', '限额'))
    return badges


def state_color(row):
    """状态色：真运行绿、待核红、限额无在途橙、空闲/时段外淡化。"""
    if compact_state_text(row) == PREV_LIMIT_LABEL:
        return _QUOTA_LIMIT_COLOR
    return _STATE_COLORS.get(row.get('state'), _SUB)


def run_gui(store_path=None, interval=2.0):
    import tkinter as tk

    font = ('Microsoft YaHei UI', 9)
    font_bold = ('Microsoft YaHei UI', 9, 'bold')
    font_title = ('Microsoft YaHei UI', 11, 'bold')
    font_small = ('Microsoft YaHei UI', 8)
    font_badge = ('Microsoft YaHei UI', 8)

    root = tk.Tk()
    root.overrideredirect(True)
    root.configure(bg=_BG)
    root.attributes('-topmost', True)
    topmost = True
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    width = min(_WIDTH, max(320, sw - 16))
    start_x = max(0, sw - width - 24)
    start_y = max(0, min(24, sh - _MIN_H - 24))
    root.geometry(f'{width}x{_MIN_H}+{start_x}+{start_y}')

    shell = tk.Frame(root, bg=_BG, padx=12, pady=10)
    shell.pack(fill='both', expand=True)

    # --- 标题栏：标题/圆点/只读标签为拖动区；置顶与关闭在精简/展开两种模式都保留。
    title_bar = tk.Frame(shell, bg=_BG, cursor='fleur')
    title_bar.pack(fill='x')
    dot = tk.Label(title_bar, text='●', bg=_BG, fg=_DIM, font=font_small, cursor='fleur')
    dot.pack(side='left', padx=(0, 6))
    title_lbl = tk.Label(title_bar, text='牛马看板', bg=_BG, fg=_TITLE,
                         font=font_title, cursor='fleur')
    title_lbl.pack(side='left')
    readonly_lbl = tk.Label(title_bar, text='只读', bg=_BG, fg=_DIM,
                            font=font_small, cursor='fleur')
    readonly_lbl.pack(side='left', padx=(8, 0), pady=(3, 0))
    btn_close = tk.Button(title_bar, text='×', bg=_BG, fg=_SUB,
                          font=('Microsoft YaHei UI', 12, 'bold'), relief='flat', bd=0,
                          activebackground=_BG, activeforeground=_RED, cursor='hand2',
                          padx=4, pady=0)
    btn_close.pack(side='right')
    btn_pin = tk.Button(title_bar, text='置顶:开', bg=_BG, fg=_DIM, font=font_small,
                        relief='flat', bd=0, activebackground=_BG, activeforeground=_FG,
                        cursor='hand2', padx=2)
    btn_pin.pack(side='right', padx=(0, 4))

    tk.Frame(shell, bg=_BORDER, height=1).pack(fill='x', pady=(8, 6))

    # --- 摘要：总占用/16 与运行数常驻；待核/预留/限额/Luna/其他在途只在非零时出现，
    # 顺序固定，动态切换不留多余分隔点，零项不再占位。
    summary_frame = tk.Frame(shell, bg=_BG)
    summary_frame.pack(fill='x')
    summary_state = {'sig': None}
    summary_widgets = []

    def _render_summary(items):
        sig = tuple(items)
        if summary_state['sig'] == sig:
            return
        summary_state['sig'] = sig
        for widget in summary_widgets:
            widget.destroy()
        summary_widgets.clear()
        for i, (_key, text, fg) in enumerate(items):
            if i:
                sep = tk.Label(summary_frame, text='·', bg=_BG, fg=_DIM,
                               font=font_small)
                sep.pack(side='left', padx=5)
                _bind_hover(sep)
                summary_widgets.append(sep)
            lbl = tk.Label(summary_frame, text=text, bg=_BG, fg=fg, font=font_small)
            lbl.pack(side='left')
            _bind_hover(lbl)
            summary_widgets.append(lbl)

    rows_frame = tk.Frame(shell, bg=_BG)
    rows_frame.pack(fill='x', pady=(8, 0))

    empty_card = tk.Frame(shell, bg=_CARD, highlightbackground=_BORDER,
                          highlightthickness=1)
    empty_lbl = tk.Label(empty_card, text='正在读取本地只读快照…', bg=_CARD, fg=_SUB,
                         font=font, anchor='w', justify='left')
    empty_lbl.pack(fill='x', padx=12, pady=10)

    compact_idle_var = tk.StringVar(value='')
    compact_idle_lbl = tk.Label(shell, textvariable=compact_idle_var, bg=_BG, fg=_SUB,
                                font=font_small, anchor='e')

    # --- 空闲自动关机（BW-WIDGET-IDLE-20261018-S2）：默认不勾选、不持久化；
    # hover 只切视图，绝不勾选/停用/重置 controller；精简仍显示已开启的计时。
    idle_ctl = IdleShutdownController(shutdown=_system_shutdown)
    shutdown_var = tk.BooleanVar(value=False)
    idle_frame = tk.Frame(shell, bg=_BG)
    idle_chk = tk.Checkbutton(idle_frame, text='空闲30分钟自动关机', variable=shutdown_var,
                              bg=_BG, fg=_SUB, font=font_small, selectcolor=_CARD,
                              activebackground=_BG, activeforeground=_FG,
                              anchor='w', cursor='hand2', highlightthickness=0, bd=0)
    idle_chk.pack(side='left')
    idle_var = tk.StringVar(value='')
    idle_lbl = tk.Label(idle_frame, textvariable=idle_var, bg=_BG, fg=_SUB,
                        font=font_small, anchor='e')
    idle_lbl.pack(side='right')

    detail_frame = tk.Frame(shell, bg=_CARD, highlightbackground=_BORDER,
                            highlightthickness=1)
    # 未选中行时只占 2 行高，选中后 6 行并可滚轮，避免大块空白。
    detail_txt = tk.Text(detail_frame, height=2, wrap='word', bg=_CARD, fg=_SUB,
                         font=font_small, relief='flat', bd=0, highlightthickness=0,
                         padx=10, pady=8, insertbackground=_FG, cursor='arrow',
                         spacing1=1, spacing3=1)
    detail_txt.pack(fill='both', expand=True)
    detail_txt.configure(state='disabled')

    footer_var = tk.StringVar(value='')
    footer_lbl = tk.Label(shell, textvariable=footer_var, bg=_BG, fg=_DIM,
                          font=font_small, anchor='w')

    # --- 12 行卡片（ROW_DEFS 顺序/名称原样，完整不截断；左色条+行卡层次）。
    row_widgets = {}
    for pk, label, _note in ROW_DEFS:
        card = tk.Frame(rows_frame, bg=_CARD, highlightbackground=_BORDER,
                        highlightthickness=1, cursor='hand2')
        accent = tk.Frame(card, bg=_DIM, width=3, cursor='hand2')
        accent.pack(side='left', fill='y')
        inner = tk.Frame(card, bg=_CARD, cursor='hand2')
        inner.pack(side='left', fill='both', expand=True, padx=10, pady=6)
        inner.columnconfigure(0, weight=1)
        inner.columnconfigure(2, minsize=80)
        inner.columnconfigure(3, minsize=42)
        name = tk.Label(inner, text=label, bg=_CARD, fg=_FG, font=font, anchor='w',
                        cursor='hand2')
        name.grid(row=0, column=0, sticky='w')
        badges = tk.Frame(inner, bg=_CARD, cursor='hand2')
        badges.grid(row=0, column=1, sticky='e', padx=(6, 6))
        state = tk.Label(inner, text='', bg=_CARD, fg=_SUB, font=font_bold, anchor='e',
                         wraplength=160, justify='right', cursor='hand2')
        state.grid(row=0, column=2, sticky='e')
        occ = tk.Label(inner, text='', bg=_CARD, fg=_SUB, font=font, anchor='e',
                       cursor='hand2')
        occ.grid(row=0, column=3, sticky='e', padx=(6, 0))
        badge_widgets = {}
        for key, fg, bg in (('run', _BADGE_RUN_FG, _BADGE_RUN_BG),
                            ('window', _BADGE_WINDOW_FG, _BADGE_WINDOW_BG),
                            ('limit', _QUOTA_LIMIT_COLOR, _BADGE_LIMIT_BG)):
            badge_widgets[key] = tk.Label(badges, text='', bg=bg, fg=fg,
                                          font=font_badge, padx=5, pady=1,
                                          cursor='hand2')
        row_widgets[pk] = {'card': card, 'accent': accent, 'inner': inner,
                           'name': name, 'state': state, 'occ': occ,
                           'badges': badges, 'badge_widgets': badge_widgets,
                           'badge_sig': ()}

    selected = {'pk': None, 'rows': []}
    last_snap = {'snap': None}
    view_state = {'visible': ()}
    layout_state = {'sig': None}
    pre_expand = {'x': None, 'y': None}
    hover = {'expanded': False, 'after': None, 'dragging': False}
    drag = {'active': False, 'moved': False, 'dx': 0, 'dy': 0, 'pk': None,
            'start_x': 0, 'start_y': 0}

    def _set_detail(text):
        if detail_txt.get('1.0', 'end-1c') == text:
            return
        detail_txt.configure(state='normal')
        detail_txt.delete('1.0', 'end')
        detail_txt.insert('1.0', text)
        detail_txt.configure(state='disabled')

    def _set_rows(pks):
        visible = tuple(pks)
        if visible == view_state['visible']:
            return
        for w in row_widgets.values():
            w['card'].pack_forget()
        for pk in visible:
            row_widgets[pk]['card'].pack(fill='x', pady=2)
        view_state['visible'] = visible

    def _apply_mode():
        mode = 'expanded' if hover['expanded'] else 'compact'
        has_rows = bool(view_state['visible'])
        idle_compact = bool(compact_idle_var.get()) and mode == 'compact'
        sig = (mode, has_rows, idle_compact)
        if layout_state['sig'] == sig:
            return
        layout_state['sig'] = sig
        for widget in (summary_frame, rows_frame, empty_card, compact_idle_lbl,
                       idle_frame, detail_frame, footer_lbl):
            widget.pack_forget()
        summary_frame.pack(fill='x')
        rows_frame.pack(fill='x', pady=(8, 0))
        if not has_rows:
            empty_card.pack(fill='x', pady=(8, 0))
        if mode == 'expanded':
            idle_frame.pack(fill='x', pady=(8, 2))
            detail_frame.pack(fill='x', pady=(6, 0))
            footer_lbl.pack(fill='x', pady=(6, 0))
        elif idle_compact:
            compact_idle_lbl.pack(fill='x', pady=(6, 0))

    def _place(height, cx=None, cy=None):
        """完整内容优先：高度取 shell 请求高度，位置只做屏幕内夹紧。
        展开时若底部空间不足，窗口整体上移（标题 y 可变），绝不为固定标题裁掉行。"""
        height = max(_MIN_H, min(int(height), max(_MIN_H, sh - 16)))
        cx = root.winfo_x() if cx is None else int(cx)
        cy = root.winfo_y() if cy is None else int(cy)
        cx = max(0, min(cx, max(0, sw - width)))
        cy = max(0, min(cy, max(0, sh - height)))
        root.geometry(f'{width}x{height}+{cx}+{cy}')

    def _fit_window(cx=None, cy=None):
        if hover['dragging']:
            return
        root.update_idletasks()
        _place(shell.winfo_reqheight(), cx, cy)

    def _set_expanded(on):
        on = bool(on)
        if hover['expanded'] == on:
            return
        if on:
            pre_expand['x'] = root.winfo_x()
            pre_expand['y'] = root.winfo_y()
        hover['expanded'] = on
        snap = last_snap['snap']
        if snap is None:
            pks = []
        elif snap.get('ok'):
            pks = [r['pool_key'] for r in view_rows(snap, expanded=on)]
        else:
            pks = list(row_widgets) if on else []
        _set_rows(pks)
        _apply_mode()
        if not on and pre_expand['x'] is not None:
            _fit_window(pre_expand['x'], pre_expand['y'])
        else:
            _fit_window()

    def _pointer_inside():
        px, py = root.winfo_pointerxy()
        x0, y0 = root.winfo_rootx(), root.winfo_rooty()
        return (x0 <= px < x0 + root.winfo_width()
                and y0 <= py < y0 + root.winfo_height())

    def _cancel_collapse():
        if hover['after'] is not None:
            try:
                root.after_cancel(hover['after'])
            except Exception:
                pass
            hover['after'] = None

    def _on_enter(_event=None):
        if hover['dragging']:
            return
        _cancel_collapse()
        if not hover['expanded']:
            _set_expanded(True)

    def _on_leave(_event=None):
        if hover['dragging']:
            return
        _cancel_collapse()
        hover['after'] = root.after(_HOVER_COLLAPSE_MS, _maybe_collapse)

    def _maybe_collapse():
        hover['after'] = None
        if hover['dragging']:
            return
        if _pointer_inside():
            return
        if hover['expanded']:
            _set_expanded(False)

    def _drag_start(event, pk=None):
        drag.update(active=True, moved=False,
                    dx=event.x_root - root.winfo_x(),
                    dy=event.y_root - root.winfo_y(),
                    start_x=event.x_root, start_y=event.y_root, pk=pk)
        hover['dragging'] = True
        _cancel_collapse()

    def _drag_move(event):
        if not drag['active']:
            return
        if not drag['moved']:
            if (abs(event.x_root - drag['start_x']) > 3
                    or abs(event.y_root - drag['start_y']) > 3):
                drag['moved'] = True
        if not drag['moved']:
            return
        nx = event.x_root - drag['dx']
        ny = event.y_root - drag['dy']
        nx = max(0, min(nx, max(0, sw - root.winfo_width())))
        ny = max(0, min(ny, max(0, sh - root.winfo_height())))
        root.geometry(f'+{nx}+{ny}')

    def _drag_end(_event=None):
        if not drag['active']:
            return
        was_click = not drag['moved']
        pk = drag['pk']
        drag.update(active=False, moved=False, pk=None)
        hover['dragging'] = False
        if hover['expanded']:
            pre_expand['x'] = root.winfo_x()
            pre_expand['y'] = root.winfo_y()
        if was_click and pk:
            _select(pk)
        _fit_window()
        if _pointer_inside():
            if not hover['expanded']:
                _set_expanded(True)
        else:
            _on_leave()

    def _select(pk):
        selected['pk'] = pk
        for key, w in row_widgets.items():
            bg = _CARD_SEL if key == pk else _CARD
            border = _BORDER_SEL if key == pk else _BORDER
            for part in ('card', 'inner', 'name', 'state', 'occ', 'badges'):
                w[part].configure(bg=bg)
            w['card'].configure(highlightbackground=border)
        row = next((r for r in selected['rows'] if r['pool_key'] == pk), None)
        detail_txt.configure(height=6 if row is not None else 2)
        if row is not None:
            _set_detail(detail_text(row))
        elif last_snap['snap'] is not None and not last_snap['snap'].get('ok'):
            _set_detail('本地只读快照暂不可用（缺库/锁定/解析失败均如实待核，'
                        '绝不显示成空闲）。')
        else:
            _set_detail('悬停展开全部模型 · 点击行查看任务详情')

    def _refresh_idle_text():
        st = idle_ctl.status()
        idle_var.set(idle_status_text(st))
        idle_lbl.configure(fg=_RED if st['failed'] else _SUB)
        if st['triggered']:
            compact = '关机指令已发出'
        elif st['failed']:
            compact = '自动关机失败 · 已停用'
        elif st['enabled']:
            if st['due']:
                compact = '空闲关机 · 复核中'
            elif st['pause_reason']:
                compact = '空闲关机 · 计时暂停'
            else:
                compact = f"空闲关机 {_format_countdown(st['remaining'])}"
        else:
            compact = ''
        compact_idle_var.set(compact)
        compact_idle_lbl.configure(fg=_RED if st['failed'] else _SUB)

    def _on_idle_toggle():
        idle_ctl.set_enabled(shutdown_var.get())
        _refresh_idle_text()
        _apply_mode()
        _fit_window()

    idle_chk.configure(command=_on_idle_toggle)

    def _on_detail_wheel(event):
        detail_txt.yview_scroll(-1 if event.delta > 0 else 1, 'units')
        return 'break'

    detail_txt.bind('<MouseWheel>', _on_detail_wheel)

    def _render_snapshot(snap):
        last_snap['snap'] = snap
        ok = bool(snap and snap.get('ok'))
        if not ok:
            code = (snap or {}).get('error_code', 'read_failed')
            _render_summary([('error', f'读取失败 · 待核（{code}） · 下轮自动重试', _RED)])
            dot.configure(fg=_RED)
            for w in row_widgets.values():
                w['name'].configure(fg=_SUB)
                w['state'].configure(text='待核', fg=_RED)
                w['occ'].configure(text='—', fg=_DIM)
                w['accent'].configure(bg=_RED)
                for part in ('card', 'inner', 'name', 'state', 'occ', 'badges'):
                    w[part].configure(bg=_CARD)
                w['card'].configure(highlightbackground=_BORDER)
                for badge in w['badge_widgets'].values():
                    badge.pack_forget()
                w['badge_sig'] = ()
            selected['pk'] = None
            selected['rows'] = []
            detail_txt.configure(height=2)
            _set_detail('本地只读快照暂不可用（缺库/锁定/解析失败均如实待核，'
                        '绝不显示成空闲）。')
            empty_lbl.configure(text='本地只读快照不可用 · 待核', fg=_RED)
            _set_rows(list(row_widgets) if hover['expanded'] else [])
            footer_var.set('本地只读 · 读取失败 · 下轮自动重试')
            return

        counts = view_summary(snap)
        total, cap = counts['total'], counts['capacity']
        summary_items = [('total', f'总占用 {total}/{cap}', _FG),
                         ('run', f"运行 {counts['running']}",
                          _GREEN if counts['running'] else _DIM)]
        # 零项不占位：待核/预留/限额/Luna/其他在途只在非零时按固定顺序出现。
        if counts['pending']:
            summary_items.append(('pending', f"待核 {counts['pending']}", _RED))
        if counts['reserved']:
            summary_items.append(('reserved', f"预留 {counts['reserved']}", _SUB))
        if counts['limited']:
            summary_items.append(('limit', f"限额 {counts['limited']}",
                                  _QUOTA_LIMIT_COLOR))
        if counts['luna']:
            summary_items.append(('luna', f"Luna {counts['luna']}", _SUB))
        if counts['retired']:
            summary_items.append(('retired', f"其他在途 {counts['retired']}", _SUB))
        _render_summary(summary_items)
        if counts['pending']:
            dot.configure(fg=_RED)
        elif counts['limited']:
            dot.configure(fg=_QUOTA_LIMIT_COLOR)
        elif counts['running']:
            dot.configure(fg=_GREEN)
        else:
            dot.configure(fg=_DIM)

        selected['rows'] = snap['rows']
        for row in snap['rows']:
            w = row_widgets[row['pool_key']]
            color = state_color(row)
            name_fg = _FG if row['state'] in _STATE_PRIORITY else _SUB
            occ_fg = _FG if row['active'] else _DIM
            w['name'].configure(fg=name_fg)
            w['state'].configure(text=compact_state_text(row), fg=color)
            cap_text = '∞' if row['capacity'] is None else str(row['capacity'])
            w['occ'].configure(text=f"{row['active']}/{cap_text}", fg=occ_fg)
            w['accent'].configure(
                bg=color if (row['state'] != 'idle' or row_shows_limit(row)) else _BORDER)
            badge_map = dict(row_badges(row))
            badge_sig = tuple(k for k in ('run', 'window', 'limit') if k in badge_map)
            if w['badge_sig'] != badge_sig:
                for badge in w['badge_widgets'].values():
                    badge.pack_forget()
                for key in badge_sig:
                    badge = w['badge_widgets'][key]
                    badge.configure(text=badge_map[key])
                    badge.pack(side='left', padx=(0, 4))
                w['badge_sig'] = badge_sig
            else:
                for key in badge_sig:
                    w['badge_widgets'][key].configure(text=badge_map[key])

        _set_rows([r['pool_key'] for r in view_rows(snap, expanded=hover['expanded'])])
        if counts['running']:
            empty_lbl.configure(text='', fg=_DIM)
        else:
            extra = []
            if counts['pending']:
                extra.append(f"待核 {counts['pending']}")
            if counts['reserved']:
                extra.append(f"已预留 {counts['reserved']}")
            if counts['retired']:
                extra.append(f"其他在途 {counts['retired']}")
            suffix = (' · ' + ' · '.join(extra)) if extra else ''
            empty_lbl.configure(text=f'当前没有运行中的牛马{suffix} · 悬停展开全部',
                                fg=_SUB)
        if selected['pk']:
            _select(selected['pk'])
        else:
            detail_txt.configure(height=2)
            _set_detail('悬停展开全部模型 · 点击行查看任务详情')
        footer_var.set(f"本地只读 · 每 {max(1, int(interval))} 秒更新 · "
                       f"更新 {datetime.now().strftime('%H:%M:%S')} · 详情可滚轮")

    def _render(snap):
        _render_snapshot(snap)
        # 空闲计时只消费快照；真实关机动作仅在勾选到期且 finalize 同步复核
        # 一份新只读快照（无在途、签名未变）通过后由此 GUI 路径发出。
        idle_ctl.observe(snap)
        if idle_ctl.due:
            if idle_ctl.finalize(read_snapshot(store_path)) == 'failed':
                shutdown_var.set(False)  # 显著失败并解除勾选，绝不逐轮重试
        _refresh_idle_text()
        _apply_mode()
        _fit_window()

    poller = _Poller(queue.Queue(), store_path=store_path, interval=interval)

    def _on_close():
        _cancel_collapse()
        poller.stop()
        _release_single_instance()
        root.destroy()

    def _toggle_pin():
        nonlocal topmost
        topmost = not topmost
        root.attributes('-topmost', topmost)
        btn_pin.configure(text='置顶:开' if topmost else '置顶:关',
                          fg=_SUB if topmost else _DIM)

    btn_close.configure(command=_on_close)
    btn_pin.configure(command=_toggle_pin)

    # --- 拖动：标题栏与整张行卡都可拖；按下到松开之间不触发收缩。
    for widget in (title_bar, title_lbl, dot, readonly_lbl):
        widget.bind('<Button-1>', _drag_start)
        widget.bind('<B1-Motion>', _drag_move)
        widget.bind('<ButtonRelease-1>', _drag_end)

    for pk, w in row_widgets.items():
        for part in ('card', 'accent', 'inner', 'name', 'state', 'occ', 'badges'):
            widget = w[part]
            widget.bind('<Button-1>', lambda e, k=pk: _drag_start(e, k))
            widget.bind('<B1-Motion>', _drag_move)
            widget.bind('<ButtonRelease-1>', _drag_end)
        for badge in w['badge_widgets'].values():
            badge.bind('<Button-1>', lambda e, k=pk: _drag_start(e, k))
            badge.bind('<B1-Motion>', _drag_move)
            badge.bind('<ButtonRelease-1>', _drag_end)

    # --- 整窗/所有子控件统一 Enter/Leave；200ms 后按真实指针边界复核，避免子控件间闪烁。
    def _bind_hover(widget):
        widget.bind('<Enter>', _on_enter, add='+')
        widget.bind('<Leave>', _on_leave, add='+')
        for child in widget.winfo_children():
            _bind_hover(child)

    _bind_hover(root)

    _render_summary([('loading', '正在读取本地只读快照…', _SUB)])
    footer_var.set(f'本地只读 · 每 {max(1, int(interval))} 秒更新')
    _set_detail('悬停展开全部模型 · 点击行查看任务详情')
    _refresh_idle_text()
    _set_rows([])
    _apply_mode()
    _fit_window(start_x, start_y)

    q = poller._q

    def _drain():
        latest = None
        try:
            while True:
                latest = q.get_nowait()
        except queue.Empty:
            pass
        if latest is not None:
            _render(latest)
        root.after(150, _drain)

    poller.start()
    root.after(80, _drain)
    root.mainloop()



# ------------------------------------------------------------------ CLI
def main(argv=None):
    ap = argparse.ArgumentParser(
        description='牛马状态桌面浮窗（本地只读，跨对话调度池；不派工、不改库、不查余额）')
    ap.add_argument('--store', default=None,
                    help='显式 dispatch pool SQLite 路径（缺省走 dispatch_pool 默认/'
                         'BRAIN_WORKER_DISPATCH_STORE 环境覆盖；绝不建库）')
    ap.add_argument('--interval', type=float, default=2.0,
                    help='刷新间隔秒（默认 2，最小 1）')
    ap.add_argument('--once', action='store_true',
                    help='只做一次只读快照并输出安全 JSON（不启动 GUI）；ok=false 时退出码 2')
    args = ap.parse_args(argv)
    if args.once:
        snap = read_snapshot(args.store)
        print(snapshot_to_json_str(snap))
        return 0 if snap.get('ok') else 2
    if not _acquire_single_instance():
        print('浮窗已在运行（单实例），本次不重复打开。', file=sys.stderr)
        return 0
    try:
        run_gui(args.store, args.interval)
    finally:
        _release_single_instance()
    return 0


if __name__ == '__main__':
    sys.exit(main())
