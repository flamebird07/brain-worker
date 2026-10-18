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
- 两个 CodeBuddy 模型（deepseek-v4.1-flash / glm-5.3-flash）**各自独立 1 槽、互不占位
  互斥**（BW-WORKER-WIDGET-20261018-S3 用户最新明确），两行独立计数，物理总 14
  （`dp.DOMESTIC_TOTAL_CAPACITY`）。
- Luna（`luna:native`）为可选外援行：无上限、不计入 14、不套本机 child PID 探针。
- 界面不显示 token、argv、prompt、原始错误正文或任何秘密；`--once` 输出的 JSON
  同样只含脱敏后的状态字段。
- 空闲自动关机（BW-WIDGET-IDLE-20261018-S2）：默认不勾选、不持久化；勾选后连续
  30 分钟“有效空闲”才由 GUI 路径经 finalize 复核触发系统既有
  `shutdown.exe /s /t 0`（不加 /f、不重启）；import/--once/未勾选/关闭绝不触发。
"""
import argparse
import json
import queue
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import dispatch_pool as dp  # noqa: E402
import stage_selection as ss  # noqa: E402

# ------------------------------------------------------------------ 展示常量
# 9 个可派组合按现有主力优先级显示（BW-CUSTOM-MAX-RESUME-20261018-S1 层序），
# 另加可选 Luna 外援行；退休池（qodercn:DeepSeek-Flash、WorkBuddy 等）不显示成可派，
# 但其真实在途仍计入总占用（见 retired_active）。
ROW_DEFS = (
    (dp.CODEBUDDY_POOL_KEY, 'CodeBuddy · DeepSeek-V4.1-Flash', '主力 · 最高优先 · 无时段门'),
    (dp.CN_CUSTOM_MAX_POOL_KEY, 'Qoder CN／token plan／Qwen-3.8-Max', '主力 Max · 夜间窗'),
    (dp.CN_MAX_POOL_KEY, 'Qoder CN／Qwen3.8-Max', '主力 Max · 夜间窗'),
    (dp.QMAX_POOL_KEY, 'Qoder 国际 · Qwen3.8-Max', '主力 Max · 国际内置 · 夜间窗'),
    (dp.CLAUDE_POOL_KEY, 'Claude · deepseek-flash', '主力 · 空闲时段门'),
    (dp.ZCODE_POOL_KEY, 'ZCode · GLM-5.3', '主力 · 全天'),
    (dp.CODEBUDDY_GLM_POOL_KEY, 'CodeBuddy · GLM-5.3-Flash',
     '条件补位 · 独立 1 槽 · 需当轮 DeepSeek 限额证据'),
    (dp.QFLASH_POOL_KEY, 'Qoder 国际 · Qwen3.8-Flash', '兜底'),
    (dp.CN_FLASH_POOL_KEY, 'Qoder CN · Qwen3.8-Flash', '兜底'),
    (dp.LUNA_KEY, 'Luna · 原生外援', '可选 · 无上限 · 不计入国内总数'),
)

STATE_LABELS = {'running': '运行中', 'unknown': '待核实', 'reserved': '已预留',
                'idle': '空闲', 'window_closed': '时段外', 'read_failed': '读取失败'}
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
            for r in conn.execute('SELECT pool_key, limited, attempt_token '
                                  'FROM main_force_limits'):
                limits[r['pool_key']] = dict(r)
        except sqlite3.Error:
            limits = {}
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
        prev_limited = bool(
            lim is not None and int(lim.get('limited') or 0) == 1
            and latest_any.get(pk, {}).get('token') is not None
            and latest_any[pk]['token'] == lim.get('attempt_token'))
        rows.append({
            'pool_key': pk, 'label': label, 'note': note,
            'capacity': capacity, 'active': len(own),
            'own_active': len(own), 'state': state,
            'db_state': db_state,
            'state_label': STATE_LABELS[state], 'window_blocked': window_blocked,
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
    if row['prev_limited']:
        lines.append('上轮限额：标记仍绑定该池最新 attempt（历史标记，非实时额度）')
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
_BG = '#1b1d27'
_PANEL = '#232634'
_TITLE_BG = '#2a2e40'
_SEL_BG = '#31364b'
_FG = '#e6e8f0'
_SUB = '#9aa0b4'
_STATE_COLORS = {'running': '#59d98c', 'reserved': '#e6c266', 'unknown': '#f08a6a',
                 'idle': '#8288a0', 'window_closed': '#7aa5e8', 'read_failed': '#e8737f'}
# 主界面宽 500：必须完整放下最长行名（如「Qoder CN／token plan／Qwen-3.8-Max」）+
# 状态 + 占用，关键名称绝不截断。收起条只保留 总占用/展开/关闭 三要素（收起时隐藏
# 置顶按钮与长标题），230 宽内绝不裁剪必需信息。
_WIDTH, _HEIGHT = 500, 540
_COLLAPSED_W, _COLLAPSED_H = 230, 34


def run_gui(store_path=None, interval=2.0):
    import tkinter as tk

    font = ('Microsoft YaHei UI', 9)
    font_bold = ('Microsoft YaHei UI', 9, 'bold')
    font_title = ('Microsoft YaHei UI', 10, 'bold')

    root = tk.Tk()
    root.overrideredirect(True)
    root.configure(bg=_BG)
    root.attributes('-topmost', True)
    topmost = True
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    x = max(0, sw - _WIDTH - 24)
    y = max(0, min(24, sh - _HEIGHT - 24))
    root.geometry(f'{_WIDTH}x{_HEIGHT}+{x}+{y}')

    # --- 标题栏（拖动区）
    title_bar = tk.Frame(root, bg=_TITLE_BG)
    title_bar.pack(fill='x')
    title_lbl = tk.Label(title_bar, text='牛马看板', bg=_TITLE_BG, fg=_FG, font=font_title)
    title_lbl.pack(side='left', padx=8, pady=4)
    mini_lbl = tk.Label(title_bar, text='', bg=_TITLE_BG, fg=_SUB, font=font)
    btn_close = tk.Button(title_bar, text='×', bg=_TITLE_BG, fg=_FG, font=font_bold,
                          relief='flat', bd=0, activebackground=_TITLE_BG,
                          activeforeground='#e8737f', cursor='hand2')
    btn_close.pack(side='right', padx=(0, 6))
    btn_collapse = tk.Button(title_bar, text='—', bg=_TITLE_BG, fg=_FG, font=font_bold,
                             relief='flat', bd=0, activebackground=_TITLE_BG,
                             activeforeground=_FG, cursor='hand2')
    btn_collapse.pack(side='right')
    btn_pin = tk.Button(title_bar, text='置顶:开', bg=_TITLE_BG, fg=_SUB, font=font,
                        relief='flat', bd=0, activebackground=_TITLE_BG,
                        activeforeground=_FG, cursor='hand2')
    btn_pin.pack(side='right', padx=(0, 4))

    drag_off = {'x': 0, 'y': 0}

    def _drag_start(event):
        drag_off['x'] = event.x_root - root.winfo_x()
        drag_off['y'] = event.y_root - root.winfo_y()

    def _drag_move(event):
        nx = max(0, min(event.x_root - drag_off['x'], sw - 60))
        ny = max(0, min(event.y_root - drag_off['y'], sh - 24))
        root.geometry(f'+{nx}+{ny}')

    for w in (title_bar, title_lbl):
        w.bind('<Button-1>', _drag_start)
        w.bind('<B1-Motion>', _drag_move)

    # --- 主体
    body = tk.Frame(root, bg=_BG)
    body.pack(fill='both', expand=True)
    summary_var = tk.StringVar(value='正在读取本地只读快照…')
    summary_lbl = tk.Label(body, textvariable=summary_var, bg=_BG, fg=_SUB, font=font,
                           anchor='w', justify='left')
    summary_lbl.pack(fill='x', padx=10, pady=(6, 2))

    # --- 空闲自动关机（BW-WIDGET-IDLE-20261018-S2）：默认不勾选、不持久化，
    # 关闭/重启浮窗即停用；收起仍计时，展开可取消。
    idle_ctl = IdleShutdownController(shutdown=_system_shutdown)
    shutdown_var = tk.BooleanVar(value=False)
    idle_frame = tk.Frame(body, bg=_BG)
    idle_frame.pack(fill='x', padx=10, pady=(0, 2))
    idle_var = tk.StringVar(value='')
    idle_lbl = tk.Label(idle_frame, textvariable=idle_var, bg=_BG, fg=_SUB, font=font,
                        anchor='e')
    idle_lbl.pack(side='right')
    idle_chk = tk.Checkbutton(idle_frame, text='空闲30分钟自动关机', variable=shutdown_var,
                              bg=_BG, fg=_SUB, font=font, selectcolor=_PANEL,
                              activebackground=_BG, activeforeground=_FG,
                              anchor='w', cursor='hand2')
    idle_chk.pack(side='left')

    def _on_idle_toggle():
        idle_ctl.set_enabled(shutdown_var.get())
        _refresh_idle_text()

    idle_chk.configure(command=_on_idle_toggle)

    def _refresh_idle_text():
        st = idle_ctl.status()
        idle_var.set(idle_status_text(st))
        idle_lbl.configure(fg=_STATE_COLORS['read_failed'] if st['failed'] else _SUB)

    _refresh_idle_text()

    rows_frame = tk.Frame(body, bg=_PANEL)
    rows_frame.pack(fill='x', padx=8, pady=4)
    row_widgets = {}
    selected = {'pk': None}
    for i, (pk, label, _note) in enumerate(ROW_DEFS):
        rf = tk.Frame(rows_frame, bg=_PANEL, cursor='hand2')
        rf.pack(fill='x', padx=4, pady=1)
        name = tk.Label(rf, text=label, bg=_PANEL, fg=_FG, font=font, anchor='w')
        name.pack(side='left', padx=(4, 2))
        occ = tk.Label(rf, text='—', bg=_PANEL, fg=_SUB, font=font, anchor='e', width=7)
        occ.pack(side='right', padx=(2, 4))
        st = tk.Label(rf, text='待核', bg=_PANEL, fg=_SUB, font=font_bold, anchor='e', width=6)
        st.pack(side='right')
        row_widgets[pk] = {'frame': rf, 'name': name, 'state': st, 'occ': occ}

    detail_var = tk.StringVar(value='点击任意一行查看当前任务详情')
    detail_lbl = tk.Label(body, textvariable=detail_var, bg=_PANEL, fg=_SUB, font=font,
                          anchor='nw', justify='left', wraplength=_WIDTH - 28)
    detail_lbl.pack(fill='both', expand=True, padx=8, pady=(4, 2))

    footer_var = tk.StringVar(value=f'本地只读 · 每 {max(1, int(interval))} 秒更新 · '
                                    '数据来自跨对话调度池')
    footer_lbl = tk.Label(body, textvariable=footer_var, bg=_BG, fg='#5c6178',
                          font=('Microsoft YaHei UI', 8), anchor='w')
    footer_lbl.pack(fill='x', padx=10, pady=(0, 4))

    def _select(pk):
        selected['pk'] = pk
        for key, w in row_widgets.items():
            bg = _SEL_BG if key == pk else _PANEL
            for part in ('frame', 'name', 'state', 'occ'):
                w[part].configure(bg=bg)
        row = next((r for r in (selected.get('rows') or []) if r['pool_key'] == pk), None)
        detail_var.set(detail_text(row))

    selected['rows'] = []

    def _bind_row(pk):
        w = row_widgets[pk]
        for part in ('frame', 'name', 'state', 'occ'):
            w[part].bind('<Button-1>', lambda _e, k=pk: _select(k))

    for pk in row_widgets:
        _bind_row(pk)

    collapsed = {'on': False}

    def _toggle_collapse():
        collapsed['on'] = not collapsed['on']
        cx, cy = root.winfo_x(), root.winfo_y()
        if collapsed['on']:
            # 收起条只保留 总占用（mini_lbl）+ 展开 + 关闭：隐藏置顶按钮与长标题，
            # 保证 230 宽内三个必需元素完整可见、绝不裁剪。
            body.pack_forget()
            title_lbl.pack_forget()
            btn_pin.pack_forget()
            mini_lbl.pack(side='left', padx=(2, 6))
            root.geometry(f'{_COLLAPSED_W}x{_COLLAPSED_H}+{cx}+{cy}')
            btn_collapse.configure(text='▢')
        else:
            mini_lbl.pack_forget()
            title_lbl.pack(side='left', padx=8, pady=4)
            btn_pin.pack(side='right', padx=(0, 4))
            body.pack(fill='both', expand=True)
            root.geometry(f'{_WIDTH}x{_HEIGHT}+{cx}+{cy}')
            btn_collapse.configure(text='—')

    def _toggle_pin():
        nonlocal topmost
        topmost = not topmost
        root.attributes('-topmost', topmost)
        btn_pin.configure(text='置顶:开' if topmost else '置顶:关')

    poller = _Poller(queue.Queue(), store_path=store_path, interval=interval)

    def _on_close():
        poller.stop()
        _release_single_instance()
        root.destroy()

    btn_close.configure(command=_on_close)
    btn_collapse.configure(command=_toggle_collapse)
    btn_pin.configure(command=_toggle_pin)

    mini_base = {'full': '牛马 —', 'short': '牛马 —'}

    def _refresh_mini():
        # 收起条关机标记：有后缀时用短基文本，保证 230 宽内 展开/关闭 按钮不被挤裁。
        st = idle_ctl.status()
        if st['triggered']:
            suffix = ' 关机已发'
        elif st['failed']:
            suffix = ' 关机失败'
        elif st['enabled']:
            suffix = f" 关机{_format_countdown(st['remaining'])}"
        else:
            suffix = ''
        base = mini_base['short'] if suffix else mini_base['full']
        mini_lbl.configure(text=base + suffix)

    def _render_snapshot(snap):
        if not snap.get('ok'):
            code = snap.get('error_code', 'read_failed')
            summary_var.set(f'读取失败 · 待核（{code}） · 下轮自动重试')
            summary_lbl.configure(fg=_STATE_COLORS['read_failed'])
            for w in row_widgets.values():
                w['state'].configure(text='待核', fg=_STATE_COLORS['read_failed'])
                w['occ'].configure(text='—', fg=_SUB)
            mini_base['full'] = '牛马 读取失败·待核'
            mini_base['short'] = '牛马 待核'
            selected['rows'] = []
            detail_var.set('本地只读快照暂不可用（缺库/锁定/解析失败均如实待核，'
                           '绝不显示成空闲）。')
            return
        selected['rows'] = snap['rows']
        total = snap['domestic_total_capacity']
        active = snap['domestic_active']
        stamp = datetime.now().strftime('%H:%M:%S')
        extra = f" · 历史池在途 {snap['retired_active']}" if snap['retired_active'] else ''
        summary_var.set(f"总占用 {active}/{total} · Luna {snap['luna_active']}"
                        f"{extra} · 更新 {stamp}")
        summary_lbl.configure(fg=_SUB if snap['ok'] else _STATE_COLORS['read_failed'])
        mini_base['full'] = mini_base['short'] = f'牛马 {active}/{total}'
        for row in snap['rows']:
            w = row_widgets[row['pool_key']]
            color = _STATE_COLORS[row['state']]
            label = row['state_label']
            if row['window_blocked'] and row['state'] in _STATE_PRIORITY:
                label += '·时段外新派'
            w['state'].configure(text=label, fg=color)
            cap = '∞' if row['capacity'] is None else str(row['capacity'])
            occ = f"{row['active']}/{cap}"
            if row['prev_limited']:
                occ += ' 上轮限额'
            w['occ'].configure(text=occ)
        if selected['pk']:
            _select(selected['pk'])

    def _render(snap):
        _render_snapshot(snap)
        # 空闲计时只消费快照；真实关机动作仅在勾选到期且 finalize 同步复核
        # 一份新只读快照（无在途、签名未变）通过后由此 GUI 路径发出。
        idle_ctl.observe(snap)
        if idle_ctl.due:
            if idle_ctl.finalize(read_snapshot(store_path)) == 'failed':
                shutdown_var.set(False)  # 显著失败并解除勾选，绝不逐轮重试
        _refresh_idle_text()
        _refresh_mini()

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
