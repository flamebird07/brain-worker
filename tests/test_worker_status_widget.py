"""worker_status_widget 离线验收：只读快照语义 + 独立槽/占位/时段/限额/失败诚实性。

全部用例使用临时 SQLite（dp.connect 建 schema 后切 DELETE journal）、合成 rows、
注入时钟与 PID 探针；不启动真 Tk、不连真实 ~/.brain-worker 池、不调真实模型、
不探测真实进程。绝不放宽真实库读取语义：失败路径断言的是生产 read_snapshot 的
真实拒绝/待核行为。
BW-WORKER-WIDGET-20261018-S3：两 CodeBuddy 模型各自独立 1 槽；BW-OPENCODE-20261019-S3：
两个 OpenCode 免费模型（deepseek-v4.1-flash-free / glm-5.3-flash-free）各独立 1 槽、
主行排两行（总 16、展示 12 行）；展示层主行
状态绝不只信 DB running——死 PID/复用/探测未知/未绑定的 running 主行必须显著待核
（待核优先，不被同池真 running 掩盖），只有匹配活 PID 才显示运行中；reserved 无子
进程仍已预留；Luna 原生不套本机探针。
BW-WIDGET-IDLE-20261018-S2：空闲自动关机——纯控制器注入 monotonic 时钟与 mock
关机回调；真实 shutdown.exe 命令仅以 mock subprocess 验证参数，绝不真跑；覆盖默认
关闭/1799-1800 边界/取消/在途与 Luna/退休占位/短调用漏采样/读取错误与陈旧快照/
恢复重新 30 分/最终复核突然来任务/只触发一次/系统动作失败不循环。
"""
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'scripts'
for p in (str(SCRIPTS), str(REPO / 'tests')):
    if p not in sys.path:
        sys.path.insert(0, p)

import dispatch_pool as dp  # noqa: E402
import worker_status_widget as wsw  # noqa: E402

# 北京 2026-10-08(周四) 23:00：三 Qoder Max 夜间窗开、claude 空闲时段放行。
T0 = datetime(2026, 10, 8, 15, 0, 0, tzinfo=timezone.utc)
# 北京 2026-10-08(周四) 10:00：Max 夜间窗关、claude 工作日高峰门关。
PEAK = datetime(2026, 10, 8, 2, 0, 0, tzinfo=timezone.utc)

_COLUMNS = ('token, task_id, stage, runtime, model, pool_key, state, child_pid, '
            'child_created, reserved_at_utc, started_at_utc, ended_at_utc, terminal, '
            'success')


def _iso(dt):
    return dp._iso(dt)


def _probe(mapping):
    """注入式 PID 探针：mapping 外的 PID 一律按 dead/无创建身份处理。"""
    def probe(pid):
        return dict(mapping.get(int(pid), {'pid': pid, 'state': 'dead', 'created': None}))
    return probe


def _forbidding_probe():
    def probe(pid):
        raise AssertionError(f'pid_probe must not be called (luna native), got {pid}')
    return probe


class _WidgetBase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='bw-widget-')
        self.base = Path(self._tmp.name)
        self.store = str(self.base / 'dispatch.sqlite3')
        self.assertNotEqual(str(self.store), str(dp.default_store_path()))
        self.saved_env = os.environ.pop('BRAIN_WORKER_DISPATCH_STORE', None)
        self.addCleanup(self._tmp.cleanup)

    def tearDown(self):
        if self.saved_env is not None:
            os.environ['BRAIN_WORKER_DISPATCH_STORE'] = self.saved_env

    def _conn(self):
        conn = dp.connect(self.store)
        conn.execute('PRAGMA journal_mode=DELETE')
        return conn

    def _insert(self, conn, token, pk, state, *, task_id=None, stage='S1',
                child_pid=None, child_created=None, reserved=None, started=None,
                ended=None, terminal=None, success=None):
        runtime, model = pk.split(':', 1)
        conn.execute(
            f'INSERT INTO attempts({_COLUMNS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (token, task_id or f'T-{token}', stage, runtime, model, pk, state,
             child_pid, child_created, _iso(reserved), _iso(started), _iso(ended),
             terminal, success))

    def _snap(self, now=T0, probe=None, store=None):
        return wsw.read_snapshot(store if store is not None else self.store,
                                 now=now, pid_probe=probe or _probe({}))

    def _row(self, snap, pk):
        return next(r for r in snap['rows'] if r['pool_key'] == pk)

    def _sidecars(self):
        return [p.name for p in self.base.iterdir()
                if p.name.endswith(('-wal', '-shm', '-journal'))]


class TestModuleDiscipline(_WidgetBase):

    def test_import_does_not_start_gui_or_touch_tkinter(self):
        # 本测试文件自身绝不 import tkinter；widget 模块惰性导入，import 后不得出现。
        self.assertNotIn('tkinter', sys.modules)
        self.assertTrue(callable(wsw.run_gui))
        self.assertTrue(callable(wsw.read_snapshot))

    def test_row_defs_match_authoritative_priority_and_totals(self):
        order = [pk for pk, _, _ in wsw.ROW_DEFS]
        # BW-OPENCODE-20261019-S3：两个 OpenCode 免费模型各独立 1 槽，排在 ZCode 之后、
        # 条件补位 CodeBuddy GLM 之前（AUTO 层序 tier 4 < 条件 GLM tier 5）。
        self.assertEqual(order[:9], [
            dp.CODEBUDDY_POOL_KEY, dp.CN_CUSTOM_MAX_POOL_KEY, dp.CN_MAX_POOL_KEY,
            dp.QMAX_POOL_KEY, dp.CLAUDE_POOL_KEY, dp.ZCODE_POOL_KEY,
            dp.OPENCODE_DEEPSEEK_POOL_KEY, dp.OPENCODE_GLM_POOL_KEY,
            dp.CODEBUDDY_GLM_POOL_KEY])
        self.assertEqual(set(order[9:11]), {dp.QFLASH_POOL_KEY, dp.CN_FLASH_POOL_KEY})
        self.assertEqual(order[-1], dp.LUNA_KEY)
        # 退休池绝不显示成可派行。
        self.assertNotIn('qodercn:DeepSeek-Flash', order)
        self.assertFalse(any(pk.startswith('workbuddy') for pk in order))
        # BW-WORKER-WIDGET-20261018-S3 + BW-OPENCODE-20261019-S3 当前权威：两 CodeBuddy
        # 模型与两 OpenCode 免费模型各独立 1 槽 → 物理总 16。
        self.assertEqual(dp.DOMESTIC_TOTAL_CAPACITY, 16)
        self.assertEqual((sum(dp.MAIN_FORCE_CAPACITY.values())
                          + sum(dp.OVERFLOW_CAPACITY.values())), 16)
        self.assertEqual(dp.MAIN_FORCE_CAPACITY[dp.CODEBUDDY_GLM_POOL_KEY], 1)
        self.assertEqual(dp.MAIN_FORCE_CAPACITY[dp.OPENCODE_DEEPSEEK_POOL_KEY], 1)
        self.assertEqual(dp.MAIN_FORCE_CAPACITY[dp.OPENCODE_GLM_POOL_KEY], 1)
        # 用户明确要求的完整 CN 名称，绝不再写「CN 内置/CN 自定义」。
        labels = dict((pk, label) for pk, label, _ in wsw.ROW_DEFS)
        self.assertEqual(labels[dp.CN_MAX_POOL_KEY], 'Qoder CN／Qwen3.8-Max')
        self.assertEqual(labels[dp.CN_CUSTOM_MAX_POOL_KEY],
                         'Qoder CN／token plan／Qwen-3.8-Max')
        for pk, _label, note in wsw.ROW_DEFS:
            self.assertNotIn('CN 内置', note)
            self.assertNotIn('CN 自定义', note)
            self.assertNotIn('共用', note)

    def test_once_cli_outputs_safe_json(self):
        with closing(self._conn()) as conn:
            self._insert(conn, 'z1', dp.ZCODE_POOL_KEY, 'running',
                         started=T0 - timedelta(seconds=30))
        buf = io.StringIO()
        with mock.patch.object(wsw.subprocess, 'run') as m_run, redirect_stdout(buf):
            code = wsw.main(['--store', self.store, '--once'])
        self.assertEqual(code, 0)
        m_run.assert_not_called()  # --once 绝不触发任何系统动作
        text = buf.getvalue()
        snap = json.loads(text)
        self.assertTrue(snap['ok'])
        for banned in ('prompt_sha256', '"token"', 'workspace', 'argv',
                       'attempt_token', 'child_created'):
            self.assertNotIn(banned, text)

    def test_once_cli_exit_2_on_read_failure(self):
        missing = str(self.base / 'missing.sqlite3')
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = wsw.main(['--store', missing, '--once'])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(buf.getvalue())['ok'])


class TestSnapshotStates(_WidgetBase):

    def test_empty_pool_shows_idle_not_error(self):
        with closing(self._conn()):
            pass
        snap = self._snap()
        self.assertTrue(snap['ok'])
        self.assertEqual(snap['domestic_active'], 0)
        self.assertEqual(snap['domestic_total_capacity'], 16)
        self.assertEqual(len(snap['rows']), 12)
        for row in snap['rows']:
            self.assertEqual(row['state'], 'idle', row['pool_key'])
            self.assertEqual(row['state_label'], '空闲')

    def test_running_state_elapsed_and_total(self):
        # 真活：DB running + 探针确认活 PID 且创建身份匹配 → 主行才显示运行中。
        probe = _probe({4321: {'pid': 4321, 'state': 'alive', 'created': '111'},
                        4322: {'pid': 4322, 'state': 'alive', 'created': '222'}})
        with closing(self._conn()) as conn:
            self._insert(conn, 'z1', dp.ZCODE_POOL_KEY, 'running', child_pid=4321,
                         child_created='111', started=T0 - timedelta(seconds=90))
            self._insert(conn, 'f1', dp.QFLASH_POOL_KEY, 'running', child_pid=4322,
                         child_created='222', started=T0 - timedelta(seconds=5))
        snap = self._snap(probe=probe)
        z = self._row(snap, dp.ZCODE_POOL_KEY)
        self.assertEqual(z['state'], 'running')
        self.assertEqual(z['state_label'], '运行中')
        self.assertEqual((z['active'], z['capacity']), (1, 2))
        self.assertEqual(z['attempts'][0]['elapsed_seconds'], 90)
        self.assertEqual(snap['domestic_active'], 2)

    def test_reserved_is_distinct_from_running(self):
        with closing(self._conn()) as conn:
            self._insert(conn, 'f1', dp.CN_FLASH_POOL_KEY, 'reserved',
                         reserved=T0 - timedelta(seconds=20))
        snap = self._snap()
        f = self._row(snap, dp.CN_FLASH_POOL_KEY)
        # reserved 未绑子进程仍如实显示已预留（不套活 PID 要求）。
        self.assertEqual(f['state'], 'reserved')
        self.assertEqual(f['state_label'], '已预留')
        self.assertEqual(f['attempts'][0]['pid_check'], 'no_binding')
        self.assertEqual(f['attempts'][0]['elapsed_seconds'], 20)

    def test_running_dominates_reserved_in_same_pool(self):
        probe = _probe({4321: {'pid': 4321, 'state': 'alive', 'created': '111'}})
        with closing(self._conn()) as conn:
            self._insert(conn, 'z1', dp.ZCODE_POOL_KEY, 'running', child_pid=4321,
                         child_created='111', started=T0)
            self._insert(conn, 'z2', dp.ZCODE_POOL_KEY, 'reserved', reserved=T0)
        snap = self._snap(probe=probe)
        z = self._row(snap, dp.ZCODE_POOL_KEY)
        self.assertEqual(z['state'], 'running')
        self.assertEqual(z['active'], 2)
        self.assertEqual({a['state'] for a in z['attempts']}, {'running', 'reserved'})

    def test_codebuddy_two_models_count_independently(self):
        probe = _probe({4321: {'pid': 4321, 'state': 'alive', 'created': '111'}})
        with closing(self._conn()) as conn:
            self._insert(conn, 'cb1', dp.CODEBUDDY_POOL_KEY, 'running',
                         child_pid=4321, child_created='111', started=T0)
        snap = self._snap(probe=probe)
        ds = self._row(snap, dp.CODEBUDDY_POOL_KEY)
        glm = self._row(snap, dp.CODEBUDDY_GLM_POOL_KEY)
        # BW-WORKER-WIDGET-20261018-S3：两模型各自独立 1 槽，绝不合并/互斥计数。
        self.assertEqual((ds['active'], ds['capacity']), (1, 1))
        self.assertEqual((glm['active'], glm['capacity']), (0, 1))
        self.assertEqual(glm['own_active'], 0)
        self.assertEqual(glm['state'], 'idle')
        self.assertEqual(snap['domestic_active'], 1)
        self.assertEqual(snap['domestic_total_capacity'], 16)
        # 两模型可同时各 1 在途 → 总占用 2，绝不按共享槽压成 1。
        with closing(self._conn()) as conn:
            self._insert(conn, 'g1', dp.CODEBUDDY_GLM_POOL_KEY, 'reserved',
                         reserved=T0)
        snap = self._snap(probe=probe)
        self.assertEqual((self._row(snap, dp.CODEBUDDY_GLM_POOL_KEY)['active'],
                          self._row(snap, dp.CODEBUDDY_GLM_POOL_KEY)['state']),
                         (1, 'reserved'))
        self.assertEqual(snap['domestic_active'], 2)

    def test_dead_reused_unbound_running_show_pending_on_main_row(self):
        with closing(self._conn()) as conn:
            # unknown 即使 PID 存活也待核，占位保留。
            self._insert(conn, 'q1', dp.QMAX_POOL_KEY, 'unknown', child_pid=4321,
                         child_created='111', started=T0 - timedelta(seconds=60))
            # running 但子进程已死：占位仍按权威计数，主行必须显著待核（绝不绿灯）。
            self._insert(conn, 'c1', dp.CLAUDE_POOL_KEY, 'running', child_pid=5150,
                         child_created='222', started=T0 - timedelta(seconds=60))
            # running 且 PID 活但创建身份不匹配（PID 复用嫌疑）：主行待核。
            self._insert(conn, 'z1', dp.ZCODE_POOL_KEY, 'running', child_pid=6001,
                         child_created='333', started=T0 - timedelta(seconds=60))
            # running 却未绑定任何子进程：没有匹配活 PID → 主行待核。
            self._insert(conn, 'f1', dp.QFLASH_POOL_KEY, 'running',
                         started=T0 - timedelta(seconds=60))
        probe = _probe({4321: {'pid': 4321, 'state': 'alive', 'created': '111'},
                        6001: {'pid': 6001, 'state': 'alive', 'created': '999'}})
        snap = self._snap(probe=probe)
        q = self._row(snap, dp.QMAX_POOL_KEY)
        self.assertEqual(q['state_label'], '待核实')
        self.assertEqual(q['active'], 1)
        self.assertEqual(q['attempts'][0]['pid_check'], 'alive_matched')
        c = self._row(snap, dp.CLAUDE_POOL_KEY)
        self.assertEqual(c['state'], 'unknown')       # 主行待核，绝不绿灯
        self.assertEqual(c['state_label'], '待核实')
        self.assertEqual(c['active'], 1)              # 占位计数不变（不释放）
        self.assertEqual(c['db_state'], 'running')    # DB 原状态如实回显
        self.assertEqual(c['attempts'][0]['state'], 'running')
        self.assertEqual(c['attempts'][0]['display_state'], 'unknown')
        self.assertEqual(c['attempts'][0]['pid_check'], 'dead')
        z = self._row(snap, dp.ZCODE_POOL_KEY)
        self.assertEqual(z['state'], 'unknown')
        self.assertEqual(z['attempts'][0]['pid_check'], 'alive_unmatched')
        f = self._row(snap, dp.QFLASH_POOL_KEY)
        self.assertEqual(f['state'], 'unknown')
        self.assertEqual(f['attempts'][0]['pid_check'], 'no_binding')
        self.assertEqual(snap['domestic_active'], 4)

    def test_unknown_probe_never_fakes_running(self):
        # 探针异常/状态未知 → 主行待核，绝不伪绿灯。
        def flaky(pid):
            raise OSError('probe unavailable')

        with closing(self._conn()) as conn:
            self._insert(conn, 'c1', dp.CLAUDE_POOL_KEY, 'running', child_pid=9,
                         child_created='x', started=T0)
        snap = self._snap(probe=flaky)
        c = self._row(snap, dp.CLAUDE_POOL_KEY)
        self.assertEqual(c['state'], 'unknown')
        self.assertEqual(c['attempts'][0]['pid_check'], 'unknown')

    def test_pending_not_masked_by_real_running_in_same_pool(self):
        # 混合池：一个真活 running + 一个死 PID running → 主行必须待核（未知优先，
        # 绝不被同池另一个 running 掩盖），DB 占位计数不变（2/2）。
        probe = _probe({4321: {'pid': 4321, 'state': 'alive', 'created': '111'}})
        with closing(self._conn()) as conn:
            self._insert(conn, 'z1', dp.ZCODE_POOL_KEY, 'running', child_pid=4321,
                         child_created='111', started=T0)
            self._insert(conn, 'z2', dp.ZCODE_POOL_KEY, 'running', child_pid=5150,
                         child_created='222', started=T0)
        snap = self._snap(probe=probe)
        z = self._row(snap, dp.ZCODE_POOL_KEY)
        self.assertEqual(z['state'], 'unknown')
        self.assertEqual(z['state_label'], '待核实')
        self.assertEqual(z['active'], 2)
        self.assertEqual({a['display_state'] for a in z['attempts']},
                         {'running', 'unknown'})

    def test_window_closed_only_masks_idle_rows_never_in_flight(self):
        probe = _probe({4321: {'pid': 4321, 'state': 'alive', 'created': '111'}})
        with closing(self._conn()) as conn:
            self._insert(conn, 'cn1', dp.CN_MAX_POOL_KEY, 'running', child_pid=4321,
                         child_created='111', started=PEAK - timedelta(seconds=600))
        snap = self._snap(now=PEAK, probe=probe)
        cn = self._row(snap, dp.CN_MAX_POOL_KEY)
        # 跨时段在途：运行中优先于时段门，绝不遮盖。
        self.assertEqual(cn['state'], 'running')
        self.assertTrue(cn['window_blocked'])
        qm = self._row(snap, dp.QMAX_POOL_KEY)
        self.assertEqual(qm['state'], 'window_closed')
        # 三 Qoder Max：固定夜间窗口富标签（仅显示层）。
        self.assertEqual(qm['state_label'], wsw.MAX_WINDOW_CLOSED_LABEL)
        self.assertEqual(qm['state_label'], '时段外（22:00–次日08:00）')
        cl = self._row(snap, dp.CLAUDE_POOL_KEY)
        self.assertEqual(cl['state'], 'window_closed')
        # Claude 10:00(北京周四)高峰：下一连续放行区间 12:00–14:00。
        self.assertEqual(cl['state_label'], '时段外（下次12:00–14:00）')
        # CodeBuddy 主力无时段门。
        self.assertFalse(self._row(snap, dp.CODEBUDDY_POOL_KEY)['window_blocked'])

    def test_luna_optional_row_not_counted_in_domestic_total(self):
        with closing(self._conn()) as conn:
            self._insert(conn, 'l1', dp.LUNA_KEY, 'running', child_pid=777,
                         child_created='777', started=T0 - timedelta(seconds=30))
        snap = self._snap(probe=_forbidding_probe())  # Luna 原生绝不套本机 PID 探测
        luna = self._row(snap, dp.LUNA_KEY)
        self.assertEqual(luna['capacity'], None)
        self.assertEqual(luna['active'], 1)
        # Luna 原生不套本机探针：DB running 直接如实显示运行中。
        self.assertEqual(luna['state'], 'running')
        self.assertEqual(luna['attempts'][0]['pid_check'], 'native')
        self.assertEqual(snap['luna_active'], 1)
        self.assertEqual(snap['domestic_active'], 0)
        self.assertEqual(snap['domestic_total_capacity'], 16)

    def test_retired_pools_not_shown_but_real_in_flight_counted(self):
        with closing(self._conn()) as conn:
            self._insert(conn, 'd1', 'qodercn:DeepSeek-Flash', 'running', started=T0)
            self._insert(conn, 'w1', 'workbuddy:gpt-x', 'unknown', reserved=T0)
        snap = self._snap()
        shown = {r['pool_key'] for r in snap['rows']}
        self.assertNotIn('qodercn:DeepSeek-Flash', shown)
        self.assertNotIn('workbuddy:gpt-x', shown)
        self.assertEqual(snap['retired_active'], 2)
        self.assertEqual(snap['domestic_active'], 2)


class TestLastResultAndLimits(_WidgetBase):

    def test_success_missing_or_false_never_fakes_passed(self):
        with closing(self._conn()) as conn:
            # finished 但 success 缺失 = 未知，不一概绿灯。
            self._insert(conn, 'z1', dp.ZCODE_POOL_KEY, 'finished',
                         started=T0, ended=T0, terminal='finished', success=None)
            self._insert(conn, 'f1', dp.QFLASH_POOL_KEY, 'finished',
                         started=T0, ended=T0, terminal='finished', success=0)
            self._insert(conn, 'c1', dp.CLAUDE_POOL_KEY, 'finished',
                         started=T0, ended=T0, terminal='finished', success=1)
        snap = self._snap()
        self.assertEqual(self._row(snap, dp.ZCODE_POOL_KEY)['last_result'], 'unknown')
        self.assertEqual(self._row(snap, dp.ZCODE_POOL_KEY)['last_result_label'],
                         '上轮调用待核')
        self.assertEqual(self._row(snap, dp.QFLASH_POOL_KEY)['last_result'], 'failed')
        self.assertEqual(self._row(snap, dp.CLAUDE_POOL_KEY)['last_result'], 'passed')
        self.assertEqual(self._row(snap, dp.CN_FLASH_POOL_KEY)['last_result'], 'none')

    def test_prev_limit_only_when_bound_to_latest_attempt(self):
        with closing(self._conn()) as conn:
            conn.execute('INSERT INTO main_force_limits(pool_key, limited, task_id, '
                         'attempt_token, recorded_at_utc) VALUES(?,?,?,?,?)',
                         (dp.QMAX_POOL_KEY, 1, 'T-old', 'q-old', _iso(T0)))
            self._insert(conn, 'q-old', dp.QMAX_POOL_KEY, 'finished', started=T0,
                         ended=T0, terminal='finished', success=0)
        # 场景 A：标记仍绑定该池最新 attempt → 显示“上轮限额”。
        snap = self._snap()
        self.assertTrue(self._row(snap, dp.QMAX_POOL_KEY)['prev_limited'])
        # 场景 B：出现更新的成功 attempt → 旧历史标记绝不当当前不可用。
        with closing(self._conn()) as conn:
            self._insert(conn, 'q-new', dp.QMAX_POOL_KEY, 'finished',
                         started=T0 + timedelta(minutes=5),
                         ended=T0 + timedelta(minutes=9),
                         terminal='finished', success=1)
        snap = self._snap()
        qm = self._row(snap, dp.QMAX_POOL_KEY)
        self.assertFalse(qm['prev_limited'])
        self.assertEqual(qm['last_result'], 'passed')
        # 场景 C：标记已解除（limited=0）→ 不显示。
        with closing(self._conn()) as conn:
            conn.execute('UPDATE main_force_limits SET limited=0, released_at_utc=? '
                         'WHERE pool_key=?', (_iso(T0), dp.QMAX_POOL_KEY))
        self.assertFalse(self._row(self._snap(), dp.QMAX_POOL_KEY)['prev_limited'])

    def test_codebuddy_prev_limit_shows_main_state_when_idle_and_isolated(self):
        """BW-WIDGET-QUOTA30-20261019-S3 / BW-WIDGET-HOVER-20261019-S2：CodeBuddy 精确池
        deepseek 真实限额在**无在途**时主状态醒目显示“上轮限额”（非空闲绿/非空闲灰）；
        真实运行中则只显示“运行中”，行/徽标/详情/摘要都不再出现历史限额（用户明确纠正：
        运行中就不该显示限额）。两 CodeBuddy 模型独立落标——DeepSeek 限额不污染 GLM；
        占用计数与空闲判定原语义不变，prev_limited 原始证据保留。"""
        with closing(self._conn()) as conn:
            # 只有 deepseek 池落标并绑定其失败 attempt（ended_at=T0）。
            conn.execute('INSERT INTO main_force_limits(pool_key, limited, task_id, '
                         'attempt_token, recorded_at_utc) VALUES(?,?,?,?,?)',
                         (dp.CODEBUDDY_POOL_KEY, 1, 'T-cb', 'cb-old', _iso(T0)))
            self._insert(conn, 'cb-old', dp.CODEBUDDY_POOL_KEY, 'finished',
                         started=T0, ended=T0, terminal='finished', success=0)
        snap = self._snap()
        cb = self._row(snap, dp.CODEBUDDY_POOL_KEY)
        self.assertTrue(cb['prev_limited'])
        self.assertEqual(cb['state'], 'idle')          # DB/容量/idle 判定原语义不变
        self.assertEqual(cb['main_state_label'], '上轮限额')
        # GLM 未落标 → 不受 DeepSeek 限额连带。
        glm = self._row(snap, dp.CODEBUDDY_GLM_POOL_KEY)
        self.assertFalse(glm['prev_limited'])
        self.assertEqual(glm['main_state_label'], '空闲')
        # 在途优先：deepseek 有真实 running 时主状态只显示“运行中”，历史限额不再出现
        # （用户 2026-10-19 明确纠正：运行中就不该显示限额），原始 prev_limited 保留。
        with closing(self._conn()) as conn:
            self._insert(conn, 'cb-run', dp.CODEBUDDY_POOL_KEY, 'running',
                         started=T0, child_pid=4321,
                         child_created='2026-10-08T15:00:00+00:00')
        snap2 = self._snap(probe=_probe({4321: {'pid': 4321, 'state': 'alive',
                                                'created': '2026-10-08T15:00:00+00:00'}}))
        cb2 = self._row(snap2, dp.CODEBUDDY_POOL_KEY)
        self.assertEqual(cb2['state'], 'running')
        self.assertEqual(cb2['main_state_label'], '运行中')
        self.assertTrue(cb2['prev_limited'])  # 原始证据不变，只过滤展示
        self.assertNotIn('limit', [k for k, _ in wsw.row_badges(cb2)])
        self.assertNotIn('限额', wsw.detail_text(cb2))
        # 后续新成功 attempt 顶掉旧标记 token → 半小时内真实限额提示仍保留，
        # **不再用最新 attempt_token 相等把它提前隐藏**（保留 30 分钟）。
        with closing(self._conn()) as conn:
            conn.execute('UPDATE attempts SET state=?, terminal=?, success=?, ended_at_utc=? '
                         'WHERE token=?', ('finished', 'finished', 1, _iso(T0), 'cb-run'))
        snap3 = self._snap()
        cb3 = self._row(snap3, dp.CODEBUDDY_POOL_KEY)
        self.assertTrue(cb3['prev_limited'])
        self.assertEqual(cb3['main_state_label'], '上轮限额')


class TestQuota30Window(_WidgetBase):
    """BW-WIDGET-QUOTA30-20261019-S3：仅精确 codebuddy:deepseek-v4.1-flash 池的真实限额
    标记按绑定 attempt 的 ended_at_utc（终态未落盘回退标记 recorded_at_utc）起 0<=age<1800
    显示；恰好 1800 及更久只撤下历史提示，不声称恢复。非该 DeepSeek 模型保持原 token 规则。
    不用最新 attempt_token 相等让新预留/失败提前隐藏；重复限额重计时；缺失/无时区/未来时间
    不伪造有效窗口；原始时间取自失败落盘时间，非 import 时刻。"""

    def _mark(self, conn, *, ended, recorded=None, token='cb-old'):
        conn.execute('INSERT INTO main_force_limits(pool_key, limited, task_id, '
                     'attempt_token, recorded_at_utc) VALUES(?,?,?,?,?)',
                     (dp.CODEBUDDY_POOL_KEY, 1, 'T-cb', token,
                      _iso(recorded if recorded is not None else ended)))
        self._insert(conn, token, dp.CODEBUDDY_POOL_KEY, 'finished',
                     started=ended, ended=ended, terminal='finished', success=0)

    def test_boundary_1799_shows_1800_hides(self):
        with closing(self._conn()) as conn:
            self._mark(conn, ended=T0)
        # 原始失败时刻非 import 时间：以注入 now 相对 ended_at_utc 计算年龄。
        self.assertTrue(self._row(
            self._snap(now=T0 + timedelta(seconds=1799)),
            dp.CODEBUDDY_POOL_KEY)['prev_limited'])
        self.assertFalse(self._row(
            self._snap(now=T0 + timedelta(seconds=1800)),
            dp.CODEBUDDY_POOL_KEY)['prev_limited'])
        self.assertFalse(self._row(
            self._snap(now=T0 + timedelta(seconds=1801)),
            dp.CODEBUDDY_POOL_KEY)['prev_limited'])

    def test_new_reservation_or_failure_does_not_hide_early(self):
        with closing(self._conn()) as conn:
            self._mark(conn, ended=T0)
        # 半小时内插入新预留/新失败（latest attempt token 变化）→ 真实限额绝不提前隐藏。
        with closing(self._conn()) as conn:
            self._insert(conn, 'cb-new', dp.CODEBUDDY_POOL_KEY, 'reserved',
                         reserved=T0 + timedelta(seconds=10))
        snap = self._snap(now=T0 + timedelta(seconds=60))
        cb = self._row(snap, dp.CODEBUDDY_POOL_KEY)
        self.assertTrue(cb['prev_limited'])
        self.assertEqual(cb['main_state_label'], '已预留·限额')
        # 新失败落终态但标记仍绑定旧 attempt（未更新）→ 旧真实限额窗口继续。
        with closing(self._conn()) as conn:
            conn.execute('UPDATE attempts SET state=?, terminal=?, success=? '
                         'WHERE token=?', ('finished', 'finished', 0, 'cb-new'))
        self.assertTrue(self._row(
            self._snap(now=T0 + timedelta(seconds=60)),
            dp.CODEBUDDY_POOL_KEY)['prev_limited'])

    def test_repeat_limit_retimes_from_new_attempt(self):
        # 再次实际限额：现有入口更新同一标记与新绑定 attempt（更晚 ended_at）→ 自然重新计时。
        with closing(self._conn()) as conn:
            self._mark(conn, ended=T0, token='cb-old')
            self._insert(conn, 'cb-old2', dp.CODEBUDDY_POOL_KEY, 'finished',
                         started=T0 + timedelta(seconds=1700),
                         ended=T0 + timedelta(seconds=1700),
                         terminal='finished', success=0)
            conn.execute('UPDATE main_force_limits SET attempt_token=?, recorded_at_utc=? '
                         'WHERE pool_key=?',
                         ('cb-old2', _iso(T0 + timedelta(seconds=1700)),
                          dp.CODEBUDDY_POOL_KEY))
        # 距首次失败已 1799 秒本应过期，但距新绑定 attempt 仅 99 秒 → 仍显示（重计时）。
        snap = self._snap(now=T0 + timedelta(seconds=1799))
        self.assertTrue(self._row(snap, dp.CODEBUDDY_POOL_KEY)['prev_limited'])

    def test_fallback_to_recorded_when_terminal_not_persisted(self):
        with closing(self._conn()) as conn:
            conn.execute('INSERT INTO main_force_limits(pool_key, limited, task_id, '
                         'attempt_token, recorded_at_utc) VALUES(?,?,?,?,?)',
                         (dp.CODEBUDDY_POOL_KEY, 1, 'T-cb', 'cb-x', _iso(T0)))
            # 绑定 attempt 终态尚未落盘（ended_at_utc 为 NULL）。
            self._insert(conn, 'cb-x', dp.CODEBUDDY_POOL_KEY, 'running', started=T0)
        self.assertTrue(self._row(
            self._snap(now=T0 + timedelta(seconds=1700)),
            dp.CODEBUDDY_POOL_KEY)['prev_limited'])
        self.assertFalse(self._row(
            self._snap(now=T0 + timedelta(seconds=1800)),
            dp.CODEBUDDY_POOL_KEY)['prev_limited'])

    def test_bad_missing_or_future_time_never_fakes_window(self):
        naive = T0.replace(tzinfo=None)
        future = T0 + timedelta(seconds=100)
        # (说明, 绑定 attempt 的 ended_at_utc 落盘值, 标记 recorded_at_utc 落盘值)
        cases = (
            ('naive', str(naive), str(naive)),        # 无时区
            ('future', _iso(future), _iso(future)),   # 未来
            ('missing', None, None),                  # 缺失
            ('garbage', 'not-a-time', 'not-a-time'),  # 无法解析
        )
        for label, ended_val, rec_val in cases:
            with self.subTest(kind=label):
                with closing(self._conn()) as conn:
                    conn.execute('DELETE FROM main_force_limits WHERE pool_key=?',
                                 (dp.CODEBUDDY_POOL_KEY,))
                    conn.execute('DELETE FROM attempts WHERE token=?', ('cb-bad',))
                    conn.execute('INSERT INTO main_force_limits(pool_key, limited, '
                                 'task_id, attempt_token, recorded_at_utc) VALUES(?,?,?,?,?)',
                                 (dp.CODEBUDDY_POOL_KEY, 1, 'T-cb', 'cb-bad', rec_val))
                    self._insert(conn, 'cb-bad', dp.CODEBUDDY_POOL_KEY, 'finished',
                                 started=T0, ended=None, terminal='finished', success=0)
                    if ended_val is not None:
                        conn.execute('UPDATE attempts SET ended_at_utc=? WHERE token=?',
                                     (ended_val, 'cb-bad'))
                # 现在落在 T0：naive/未来/缺失/坏值都绝不伪造有效窗口。
                self.assertFalse(self._row(
                    self._snap(now=T0), dp.CODEBUDDY_POOL_KEY)['prev_limited'])

    def test_other_model_keeps_original_token_rule(self):
        # 非 deepseek 池（qoder 国际 Max）保持原有“绑定最新 attempt token”规则与既有期限。
        with closing(self._conn()) as conn:
            conn.execute('INSERT INTO main_force_limits(pool_key, limited, task_id, '
                         'attempt_token, recorded_at_utc) VALUES(?,?,?,?,?)',
                         (dp.QMAX_POOL_KEY, 1, 'T-q', 'q-old', _iso(T0)))
            self._insert(conn, 'q-old', dp.QMAX_POOL_KEY, 'finished', started=T0,
                         ended=T0, terminal='finished', success=0)
        self.assertTrue(self._row(self._snap(now=T0 + timedelta(seconds=60)),
                                  dp.QMAX_POOL_KEY)['prev_limited'])
        # 更新的成功 attempt 顶掉 token → 原规则下立即隐藏（不受半小时窗口延长）。
        with closing(self._conn()) as conn:
            self._insert(conn, 'q-new', dp.QMAX_POOL_KEY, 'finished',
                         started=T0 + timedelta(seconds=30),
                         ended=T0 + timedelta(seconds=40),
                         terminal='finished', success=1)
        self.assertFalse(self._row(self._snap(now=T0 + timedelta(seconds=60)),
                                   dp.QMAX_POOL_KEY)['prev_limited'])

    def test_in_flight_and_limit_both_visible(self):
        # 有在途（unknown 待核）时：真实待核状态与半小时限额同时可辨，不被限额遮盖。
        with closing(self._conn()) as conn:
            self._mark(conn, ended=T0)
            self._insert(conn, 'cb-run', dp.CODEBUDDY_POOL_KEY, 'unknown',
                         started=T0, child_pid=9, child_created='x')
        snap = self._snap(now=T0 + timedelta(seconds=120),
                          probe=_probe({}))
        cb = self._row(snap, dp.CODEBUDDY_POOL_KEY)
        self.assertEqual(cb['state'], 'unknown')
        self.assertTrue(cb['prev_limited'])
        self.assertTrue(cb['main_state_label'].startswith('待核实'))
        self.assertIn('限额', cb['main_state_label'])
        self.assertEqual(cb['active'], 1)  # 占位计数不因限额展示改变

    def test_detail_text_declares_recent_record_not_balance(self):
        with closing(self._conn()) as conn:
            self._mark(conn, ended=T0)
        row = self._row(self._snap(now=T0 + timedelta(seconds=30)),
                        dp.CODEBUDDY_POOL_KEY)
        text = wsw.detail_text(row)
        self.assertIn('30', text)
        self.assertIn('不代表实时余额', text)

    def test_running_hides_limit_then_reappears_after_end_within_window(self):
        """BW-WIDGET-HOVER-20261019-S2：真实 running attempt 在途时，主状态/徽标/详情/
        摘要限额计数全部不显示历史限额；结束后未满 30 分钟重新如实展示；恰好 1800 秒
        只撤下提示、绝不宣称额度恢复。prev_limited 原始证据与计时不变。"""
        created = '2026-10-08T15:00:00+00:00'
        with closing(self._conn()) as conn:
            self._mark(conn, ended=T0)
            self._insert(conn, 'cb-run', dp.CODEBUDDY_POOL_KEY, 'running',
                         started=T0, child_pid=4321, child_created=created)
            # 另一模型运行中：DeepSeek 的限额标记绝不串到 GLM 行。
            self._insert(conn, 'glm-run', dp.CODEBUDDY_GLM_POOL_KEY, 'running',
                         started=T0, child_pid=8765, child_created=created)
        probe = _probe({4321: {'pid': 4321, 'state': 'alive', 'created': created},
                        8765: {'pid': 8765, 'state': 'alive', 'created': created}})
        snap = self._snap(now=T0 + timedelta(seconds=60), probe=probe)
        cb = self._row(snap, dp.CODEBUDDY_POOL_KEY)
        self.assertEqual(cb['state'], 'running')
        self.assertTrue(cb['prev_limited'])          # 原始证据保留
        self.assertEqual(cb['main_state_label'], '运行中')
        self.assertEqual(wsw.compact_state_text(cb), '运行中')
        self.assertNotIn(('limit', '限额'), wsw.row_badges(cb))
        self.assertNotIn('限额', wsw.detail_text(cb))
        self.assertEqual(wsw.view_summary(snap)['limited'], 0)
        glm = self._row(snap, dp.CODEBUDDY_GLM_POOL_KEY)
        self.assertFalse(glm['prev_limited'])
        self.assertEqual(glm['main_state_label'], '运行中')
        self.assertNotIn(('limit', '限额'), wsw.row_badges(glm))
        # 只针对历史限额状态段断言：GLM 的静态模型说明（“需当轮 DeepSeek 限额证据”）不是限额状态。
        self.assertNotIn('上轮限额：', wsw.detail_text(glm))
        # 运行结束后（距失败 150 秒 < 1800）历史限额重新展示，不声称余额恢复。
        with closing(self._conn()) as conn:
            conn.execute('UPDATE attempts SET state=?, terminal=?, success=?, '
                         'ended_at_utc=? WHERE token=?',
                         ('finished', 'finished', 1,
                          _iso(T0 + timedelta(seconds=120)), 'cb-run'))
        snap2 = self._snap(now=T0 + timedelta(seconds=150))
        cb2 = self._row(snap2, dp.CODEBUDDY_POOL_KEY)
        self.assertEqual(cb2['state'], 'idle')
        self.assertTrue(cb2['prev_limited'])
        self.assertEqual(cb2['main_state_label'], '上轮限额')
        self.assertIn(('limit', '限额'), wsw.row_badges(cb2))
        self.assertIn('不代表实时余额', wsw.detail_text(cb2))
        self.assertEqual(wsw.view_summary(snap2)['limited'], 1)
        # 恰好 1800 秒：撤下历史提示，不宣称恢复。
        snap3 = self._snap(now=T0 + timedelta(seconds=1800))
        cb3 = self._row(snap3, dp.CODEBUDDY_POOL_KEY)
        self.assertFalse(cb3['prev_limited'])
        self.assertEqual(cb3['main_state_label'], '空闲')
        self.assertNotIn(('limit', '限额'), wsw.row_badges(cb3))


class TestWindowLimitPools(_WidgetBase):
    """BW-LIMIT30-20261019-S3：zcode:GLM-5.3 与 claude:deepseek-flash 与 CodeBuddy
    deepseek 同口径——按绑定 attempt 的真实 ended_at_utc 计 0<=age<1800 显示；空闲显示
    “上轮限额”，真实运行中只显示“运行中”，恰好 1800 秒撤下提示且不声称恢复。"""

    def _mark(self, pk, *, ended=T0, token=None):
        token = token or f'{pk}-old'
        with closing(self._conn()) as conn:
            conn.execute('INSERT INTO main_force_limits(pool_key, limited, task_id, '
                         'attempt_token, recorded_at_utc) VALUES(?,?,?,?,?)',
                         (pk, 1, 'T-w', token, _iso(ended)))
            self._insert(conn, token, pk, 'finished', started=ended, ended=ended,
                         terminal='finished', success=0)
        return token

    def test_zcode_and_claude_boundary_1799_shows_1800_hides(self):
        for pk in (dp.ZCODE_POOL_KEY, dp.CLAUDE_POOL_KEY):
            with self.subTest(pool=pk):
                self._mark(pk)
                row = self._row(self._snap(now=T0 + timedelta(seconds=1799)), pk)
                self.assertTrue(row['prev_limited'])
                self.assertEqual(row['main_state_label'], '上轮限额')
                self.assertIn(('limit', '限额'), wsw.row_badges(row))
                self.assertFalse(self._row(
                    self._snap(now=T0 + timedelta(seconds=1800)), pk)['prev_limited'])

    def test_running_hides_limit_for_new_pools(self):
        created = '2026-10-08T15:00:00+00:00'
        for i, pk in enumerate((dp.ZCODE_POOL_KEY, dp.CLAUDE_POOL_KEY)):
            with self.subTest(pool=pk):
                pid = 5000 + i
                self._mark(pk)
                with closing(self._conn()) as conn:
                    self._insert(conn, f'{pk}-run', pk, 'running', started=T0,
                                 child_pid=pid, child_created=created)
                probe = _probe({pid: {'pid': pid, 'state': 'alive', 'created': created}})
                row = self._row(self._snap(now=T0 + timedelta(seconds=60),
                                           probe=probe), pk)
                self.assertEqual(row['state'], 'running')
                self.assertTrue(row['prev_limited'])      # 原始证据保留
                self.assertEqual(row['main_state_label'], '运行中')
                self.assertNotIn(('limit', '限额'), wsw.row_badges(row))
                self.assertNotIn('上轮限额：', wsw.detail_text(row))

    def test_expired_window_does_not_claim_recovery(self):
        self._mark(dp.ZCODE_POOL_KEY)
        row = self._row(self._snap(now=T0 + timedelta(seconds=1801)),
                        dp.ZCODE_POOL_KEY)
        self.assertFalse(row['prev_limited'])
        self.assertEqual(row['main_state_label'], '空闲')
        text = wsw.detail_text(row)
        self.assertNotIn('上轮限额：', text)
        self.assertNotIn('额度已恢复', text)


class TestFailureHonesty(_WidgetBase):

    def test_missing_store_refused_and_never_created(self):
        missing = self.base / 'missing.sqlite3'
        snap = self._snap(store=str(missing))
        self.assertFalse(snap['ok'])
        self.assertEqual(snap['error_code'], 'store_refused')
        self.assertEqual(snap['rows'], [])
        self.assertFalse(missing.exists())
        self.assertEqual(self._sidecars(), [])

    def test_wal_without_shm_refused_no_sidecar_created(self):
        with closing(self._conn()) as conn:
            self._insert(conn, 'z1', dp.ZCODE_POOL_KEY, 'running', started=T0)
        before = Path(self.store).read_bytes()
        (self.base / (Path(self.store).name + '-wal')).write_bytes(b'')
        snap = self._snap()
        self.assertFalse(snap['ok'])
        self.assertEqual(snap['error_code'], 'store_snapshot_unavailable')
        # 绝不为读状态创建 -shm sidecar，库字节不变。
        self.assertNotIn((Path(self.store).name + '-shm'), os.listdir(self.base))
        self.assertEqual(Path(self.store).read_bytes(), before)

    def test_corrupt_store_reports_read_failure_not_idle(self):
        broken = self.base / 'broken.sqlite3'
        broken.write_bytes(b'this is definitely not a sqlite database' * 4)
        snap = self._snap(store=str(broken))
        self.assertFalse(snap['ok'])
        self.assertEqual(snap['error_code'], 'read_failed')
        self.assertEqual(snap['rows'], [])

    def test_locked_store_surfaces_as_read_failure(self):
        with closing(self._conn()) as conn:
            self._insert(conn, 'z1', dp.ZCODE_POOL_KEY, 'running', started=T0)
        with mock.patch.object(wsw.ss, '_open_store_ro',
                               side_effect=sqlite3.OperationalError('database is locked')):
            snap = self._snap()
        self.assertFalse(snap['ok'])
        self.assertEqual(snap['error_code'], 'read_failed')
        self.assertEqual(snap['rows'], [])  # 绝不回落空表冒充空闲

    def test_schema_missing_store_refused(self):
        empty_db = self.base / 'empty.sqlite3'
        with closing(sqlite3.connect(str(empty_db))) as conn:
            conn.execute('CREATE TABLE unrelated(x INTEGER)')
        snap = self._snap(store=str(empty_db))
        self.assertFalse(snap['ok'])
        self.assertEqual(snap['error_code'], 'schema_missing')


class TestReadOnlyInvariance(_WidgetBase):

    def test_snapshot_never_mutates_store_bytes_or_rows(self):
        with closing(self._conn()) as conn:
            self._insert(conn, 'z1', dp.ZCODE_POOL_KEY, 'running', started=T0)
            self._insert(conn, 'cb1', dp.CODEBUDDY_POOL_KEY, 'reserved', reserved=T0)
            self._insert(conn, 'l1', dp.LUNA_KEY, 'unknown', reserved=T0)
        before = Path(self.store).read_bytes()
        ro = sqlite3.connect(Path(self.store).as_uri() + '?mode=ro', uri=True)
        with closing(ro):
            count_before = ro.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]
        for _ in range(2):
            snap = self._snap(probe=_probe({}))
            self.assertTrue(snap['ok'])
        after = Path(self.store).read_bytes()
        ro2 = sqlite3.connect(Path(self.store).as_uri() + '?mode=ro', uri=True)
        with closing(ro2):
            count_after = ro2.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(count_before, count_after)
        self.assertEqual(self._sidecars(), [])


class TestGuiHelpers(_WidgetBase):

    def test_detail_text_contains_full_task_id_and_no_secrets(self):
        with closing(self._conn()) as conn:
            self._insert(conn, 'z1', dp.ZCODE_POOL_KEY, 'running',
                         task_id='BW-FULL-TASK-ID-20261018-0001', stage='S3',
                         child_pid=4321, child_created='111',
                         started=T0 - timedelta(seconds=75))
        snap = self._snap(probe=_probe({4321: {'pid': 4321, 'state': 'alive',
                                              'created': '111'}}))
        text = wsw.detail_text(self._row(snap, dp.ZCODE_POOL_KEY))
        self.assertIn('BW-FULL-TASK-ID-20261018-0001', text)
        self.assertIn('S3', text)
        self.assertIn('1分15秒', text)
        self.assertIn('创建身份匹配', text)
        self.assertNotIn('prompt', text.lower())

    def test_format_duration(self):
        self.assertEqual(wsw.format_duration(None), '—')
        self.assertEqual(wsw.format_duration(59), '59秒')
        self.assertEqual(wsw.format_duration(90), '1分30秒')
        self.assertEqual(wsw.format_duration(3720), '1时02分')

    def test_detail_text_window_only_for_the_three_window_pools(self):
        """BW-LIMIT30-20261019-S3：只有 zcode:GLM-5.3 / claude:deepseek-flash /
        codebuddy:deepseek-v4.1-flash 三个精确池详情说“近 30 分钟/超过 30 分钟撤下”；
        其它池保留“标记绑定最新 attempt、历史非实时额度”说明，绝不冒称 30 分钟。"""
        # 非窗口池（Qoder 国际 Max）按原有 token 规则落标且仍绑定最新 attempt → prev_limited。
        with closing(self._conn()) as conn:
            conn.execute('INSERT INTO main_force_limits(pool_key, limited, task_id, '
                         'attempt_token, recorded_at_utc) VALUES(?,?,?,?,?)',
                         (dp.QMAX_POOL_KEY, 1, 'T-q', 'q-old', _iso(T0)))
            self._insert(conn, 'q-old', dp.QMAX_POOL_KEY, 'finished', started=T0,
                         ended=T0, terminal='finished', success=0)
        qrow = self._row(self._snap(), dp.QMAX_POOL_KEY)
        self.assertTrue(qrow['prev_limited'])
        qtext = wsw.detail_text(qrow)
        self.assertIn('上轮限额', qtext)
        self.assertNotIn('30 分钟', qtext)      # 其它池绝不冒称半小时窗口
        self.assertNotIn('30分钟', qtext)
        self.assertIn('最新 attempt', qtext)    # 保留原“绑定最新 attempt、历史非实时额度”语义
        # 三个精确窗口池：ZCode / Claude / CodeBuddy deepseek 均保留 30 分钟说明。
        for pk in (dp.CODEBUDDY_POOL_KEY, dp.ZCODE_POOL_KEY, dp.CLAUDE_POOL_KEY):
            with self.subTest(pool=pk):
                with closing(self._conn()) as conn:
                    conn.execute('DELETE FROM main_force_limits WHERE pool_key=?', (pk,))
                    conn.execute('INSERT INTO main_force_limits(pool_key, limited, '
                                 'task_id, attempt_token, recorded_at_utc) '
                                 'VALUES(?,?,?,?,?)',
                                 (pk, 1, 'T-w', f'w-{pk}', _iso(T0)))
                    self._insert(conn, f'w-{pk}', pk, 'finished',
                                 started=T0, ended=T0, terminal='finished', success=0)
                row = self._row(self._snap(now=T0 + timedelta(seconds=30)), pk)
                self.assertTrue(row['prev_limited'], row)
                text = wsw.detail_text(row)
                self.assertIn('30 分钟', text)
                self.assertIn('不代表实时余额', text)


class TestCompactViewHelpers(unittest.TestCase):
    """BW-WIDGET-HOVER-20261019-S1：精简/展开纯视图筛选与摘要。只认 attempt 的
    display_state=running；混池待核优先但保留“含运行”；零运行/读失败不冒充空闲；
    展开保留全部行；摘要不丢总占用/Luna/待核/限额。不启动 Tk。"""

    @staticmethod
    def _row(pk, state, display_states, *, active=1, capacity=1,
             prev_limited=False, window_blocked=False):
        return {
            'pool_key': pk, 'label': pk, 'note': '',
            'capacity': capacity, 'active': active, 'own_active': active,
            'state': state, 'db_state': state,
            'state_label': wsw.STATE_LABELS[state],
            'window_blocked': window_blocked,
            'main_state_label': wsw._main_state_display(
                state, wsw.STATE_LABELS[state], prev_limited,
                'running' in display_states),
            'last_result': 'none', 'last_result_label': '暂无调用记录',
            'prev_limited': prev_limited,
            'attempts': [
                {'task_id': f'T-{i}', 'stage': 'S1', 'state': d,
                 'display_state': d,
                 'display_state_label': wsw.STATE_LABELS.get(d, d),
                 'reserved_at_utc': None, 'started_at_utc': None,
                 'child_pid': None, 'pid_check': 'alive_matched',
                 'elapsed_seconds': 1}
                for i, d in enumerate(display_states)
            ],
        }

    @staticmethod
    def _snap(rows, **over):
        snap = {'ok': True, 'domestic_total_capacity': 16, 'domestic_active': 3,
                'luna_active': 1, 'retired_active': 0, 'rows': rows}
        snap.update(over)
        return snap

    def test_compact_only_true_running_and_mixed_pending_keeps_pending_priority(self):
        rows = [
            self._row(dp.ZCODE_POOL_KEY, 'running', ['running']),
            self._row(dp.CLAUDE_POOL_KEY, 'unknown', ['running', 'unknown']),
            self._row(dp.QMAX_POOL_KEY, 'unknown', ['unknown']),
            self._row(dp.CN_MAX_POOL_KEY, 'reserved', ['reserved']),
            self._row(dp.CODEBUDDY_POOL_KEY, 'idle', [], active=0),
        ]
        snap = self._snap(rows)
        compact = wsw.view_rows(snap, expanded=False)
        self.assertEqual([r['pool_key'] for r in compact],
                         [dp.ZCODE_POOL_KEY, dp.CLAUDE_POOL_KEY])
        mixed = compact[1]
        self.assertEqual(wsw.compact_state_text(mixed), '待核实')
        self.assertEqual(wsw.state_color(mixed), wsw._STATE_COLORS['unknown'])
        self.assertIn(('run', '含运行'), wsw.row_badges(mixed))
        self.assertNotIn(dp.QMAX_POOL_KEY, [r['pool_key'] for r in compact])
        self.assertNotIn(dp.CN_MAX_POOL_KEY, [r['pool_key'] for r in compact])
        self.assertEqual([r['pool_key'] for r in wsw.view_rows(snap, expanded=True)],
                         [r['pool_key'] for r in rows])

    def test_db_running_without_matched_live_pid_is_not_compact_running(self):
        row = self._row(dp.ZCODE_POOL_KEY, 'running', ['unknown'])
        self.assertFalse(wsw.row_has_true_running(row))
        self.assertEqual(wsw.view_rows(self._snap([row])), [])

    def test_zero_running_and_read_failure_keep_risk_counts_without_fake_running(self):
        rows = [
            self._row(dp.QMAX_POOL_KEY, 'unknown', ['unknown']),
            self._row(dp.CN_MAX_POOL_KEY, 'reserved', ['reserved']),
            self._row(dp.CODEBUDDY_POOL_KEY, 'idle', [], active=0,
                      prev_limited=True),
        ]
        snap = self._snap(rows, domestic_active=2, luna_active=1,
                          retired_active=1)
        self.assertEqual(wsw.view_rows(snap), [])
        counts = wsw.view_summary(snap)
        self.assertEqual((counts['total'], counts['capacity'], counts['luna'],
                          counts['retired']), (2, 16, 1, 1))
        self.assertEqual((counts['running'], counts['pending'],
                          counts['reserved'], counts['limited']), (0, 1, 1, 1))
        failed = {'ok': False, 'error_code': 'read_failed', 'rows': []}
        self.assertEqual(wsw.view_rows(failed), [])
        self.assertEqual(wsw.view_rows(failed, expanded=True), [])
        self.assertFalse(wsw.view_summary(failed)['ok'])

    def test_summary_counts_mixed_pool_as_running_and_pending(self):
        rows = [
            self._row(dp.CLAUDE_POOL_KEY, 'unknown', ['running', 'unknown']),
            self._row(dp.ZCODE_POOL_KEY, 'running', ['running']),
            self._row(dp.QMAX_POOL_KEY, 'idle', [], active=0, prev_limited=True),
        ]
        counts = wsw.view_summary(self._snap(rows, domestic_active=2,
                                             luna_active=0))
        self.assertEqual((counts['running'], counts['pending'],
                          counts['limited']), (2, 1, 1))

    def test_compact_state_text_quota_only_when_no_in_flight(self):
        idle_lim = self._row(dp.CODEBUDDY_POOL_KEY, 'idle', [], active=0,
                             prev_limited=True)
        self.assertEqual(wsw.compact_state_text(idle_lim), wsw.PREV_LIMIT_LABEL)
        self.assertEqual(wsw.state_color(idle_lim), wsw._QUOTA_LIMIT_COLOR)
        run_lim = self._row(dp.ZCODE_POOL_KEY, 'running', ['running'],
                            prev_limited=True)
        self.assertEqual(run_lim['main_state_label'], '运行中')
        self.assertEqual(wsw.compact_state_text(run_lim), '运行中')
        self.assertNotIn(('limit', '限额'), wsw.row_badges(run_lim))
        self.assertEqual(wsw.state_color(run_lim), wsw._STATE_COLORS['running'])
        closed = self._row(dp.QMAX_POOL_KEY, 'window_closed', [], active=0,
                           window_blocked=True)
        self.assertEqual(wsw.compact_state_text(closed), '时段外')
        self.assertNotIn(('window', '时段外'), wsw.row_badges(closed))

    def test_running_suppresses_limit_but_mixed_pending_keeps_pending_priority(self):
        """用户 2026-10-19 纠正：运行中不显示限额。真运行行与未知+真运行的混合池
        都不出现限额徽标/组合文字/摘要计数；混合池仍是待核实（不冒充全绿）。"""
        running = self._row(dp.ZCODE_POOL_KEY, 'running', ['running'],
                            prev_limited=True)
        mixed = self._row(dp.CLAUDE_POOL_KEY, 'unknown', ['running', 'unknown'],
                          prev_limited=True)
        idle = self._row(dp.CODEBUDDY_POOL_KEY, 'idle', [], active=0,
                         prev_limited=True)
        snap = self._snap([running, mixed, idle], domestic_active=2)
        self.assertEqual(wsw.compact_state_text(mixed), '待核实')
        self.assertEqual(mixed['main_state_label'], '待核实')
        self.assertNotIn(('limit', '限额'), wsw.row_badges(mixed))
        self.assertNotIn(('limit', '限额'), wsw.row_badges(running))
        self.assertIn(('run', '含运行'), wsw.row_badges(mixed))
        self.assertEqual(wsw.state_color(mixed), wsw._STATE_COLORS['unknown'])
        self.assertFalse(wsw.row_shows_limit(running))
        self.assertFalse(wsw.row_shows_limit(mixed))
        self.assertTrue(wsw.row_shows_limit(idle))
        # 摘要限额计数只算真正在展示限额的行（空闲的历史限额）。
        self.assertEqual(wsw.view_summary(snap)['limited'], 1)
        self.assertEqual(wsw.view_summary(snap)['running'], 2)
        self.assertEqual(wsw.view_summary(snap)['pending'], 1)

    def test_expanded_keeps_all_rows_even_with_zero_running(self):
        rows = [
            self._row(dp.QMAX_POOL_KEY, 'unknown', ['unknown']),
            self._row(dp.CODEBUDDY_POOL_KEY, 'idle', [], active=0),
        ]
        snap = self._snap(rows)
        self.assertEqual(wsw.view_rows(snap), [])
        self.assertEqual([r['pool_key'] for r in wsw.view_rows(snap, expanded=True)],
                         [dp.QMAX_POOL_KEY, dp.CODEBUDDY_POOL_KEY])


class _FakeAfterRoot:
    def __init__(self):
        self.cancelled = []

    def after_cancel(self, after_id):
        self.cancelled.append(after_id)


class TestHoverCollapsePolling(unittest.TestCase):
    """BW-HOVER-REPAIR-20261019-S3：复用既有 150ms drain 循环核对真实指针边界，
    不新增线程/独立定时器。纯决策函数 + 假 root，不启动 Tk：漏 Leave 也回缩、
    重复刷新不重置 200ms 计时、重新移入取消、拖动不收缩、关闭清理待办。"""

    def test_missed_leave_still_schedules_one_collapse(self):
        # 进入展开后指针移到窗外且未收到 Leave：下一 tick 必须调度一次延迟回缩。
        self.assertEqual(wsw.hover_poll_action(True, False, False, False), 'schedule')

    def test_repeated_refresh_does_not_reset_collapse_timer(self):
        # 已有一个待办时，后续每 150ms 的刷新只等待，绝不重新 after(200) 导致永不触发。
        for _ in range(10):
            self.assertEqual(wsw.hover_poll_action(True, False, False, True), 'wait')

    def test_reenter_cancels_pending_collapse(self):
        self.assertEqual(wsw.hover_poll_action(True, False, True, True), 'cancel')
        self.assertEqual(wsw.hover_poll_action(True, False, True, False), 'none')

    def test_dragging_never_collapses_even_outside(self):
        self.assertEqual(wsw.hover_poll_action(True, True, False, False), 'none')
        self.assertEqual(wsw.hover_poll_action(True, True, False, True), 'none')
        self.assertFalse(wsw.hover_should_collapse(True, True, False))

    def test_collapse_due_only_when_expanded_outside_and_not_dragging(self):
        self.assertTrue(wsw.hover_should_collapse(True, False, False))
        self.assertFalse(wsw.hover_should_collapse(True, False, True))
        self.assertFalse(wsw.hover_should_collapse(False, False, False))

    def test_close_clears_pending_after(self):
        fake = _FakeAfterRoot()
        hover = {'after': 'collapse-job-1'}
        wsw.cancel_hover_after(fake, hover)
        self.assertEqual(fake.cancelled, ['collapse-job-1'])
        self.assertIsNone(hover['after'])
        # 无待办时关闭绝不多调 after_cancel。
        wsw.cancel_hover_after(fake, hover)
        self.assertEqual(fake.cancelled, ['collapse-job-1'])


# ---------------------------------------------------------------- 空闲自动关机
class _FakeClock:
    def __init__(self, start=100000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += float(seconds)


def _activity(rowid=5, count=5, ended='2026-10-08T14:30:00+00:00'):
    return {'max_rowid': rowid, 'count': count,
            'max_reserved': '2026-10-08T14:00:00+00:00',
            'max_started': '2026-10-08T14:01:00+00:00',
            'max_ended': ended}


def _snap(activity=None, now=None, **over):
    s = {'ok': True, 'store': 'mem', 'now_utc': _iso(now or T0),
         'domestic_active': 0, 'luna_active': 0, 'retired_active': 0,
         'rows': [],
         'activity': _activity() if activity is None else activity}
    s.update(over)
    return s


class TestActivitySignature(_WidgetBase):

    def test_signature_changes_on_short_task_between_polls(self):
        with closing(self._conn()) as conn:
            self._insert(conn, 'z1', dp.ZCODE_POOL_KEY, 'finished',
                         reserved=T0 - timedelta(minutes=5),
                         started=T0 - timedelta(minutes=4),
                         ended=T0 - timedelta(minutes=3),
                         terminal='finished', success=1)
        snap1 = self._snap()
        act1 = snap1['activity']
        self.assertTrue(act1['max_rowid'] >= 1)
        self.assertEqual(act1['count'], 1)
        # 两个刷新间隔内开始又结束的短调用：active 仍为 0，但签名必须变化。
        with closing(self._conn()) as conn:
            self._insert(conn, 'z2', dp.ZCODE_POOL_KEY, 'finished',
                         reserved=T0 - timedelta(seconds=90),
                         started=T0 - timedelta(seconds=80),
                         ended=T0 - timedelta(seconds=70),
                         terminal='finished', success=1)
        snap2 = self._snap()
        self.assertEqual(snap2['domestic_active'], 0)
        self.assertNotEqual(snap2['activity'], act1)
        self.assertEqual(snap2['activity']['count'], 2)

    def test_signature_has_no_secrets_and_failure_has_none(self):
        with closing(self._conn()) as conn:
            self._insert(conn, 'SECRET-TOKEN-XYZ', dp.ZCODE_POOL_KEY, 'running',
                         started=T0)
        snap = self._snap()
        text = json.dumps(snap['activity'], ensure_ascii=False)
        self.assertNotIn('SECRET-TOKEN-XYZ', text)
        self.assertNotIn('token', text)
        broken = self.base / 'broken.sqlite3'
        broken.write_bytes(b'not a database at all' * 4)
        fail = self._snap(store=str(broken))
        self.assertFalse(fail['ok'])
        self.assertNotIn('activity', fail)  # 失败快照绝不携带可当绿灯的签名


class TestIdleShutdownController(unittest.TestCase):

    def setUp(self):
        self.clock = _FakeClock()
        self.calls = []
        self.ctl = self._ctl()

    def _ctl(self, shutdown=None):
        def _rec():
            self.calls.append(self.clock())
        return wsw.IdleShutdownController(
            clock=self.clock, shutdown=_rec if shutdown is None else shutdown,
            utcnow=lambda: T0)

    def _arm(self, ctl=None):
        ctl = ctl or self.ctl
        ctl.set_enabled(True)
        ctl.observe(_snap())
        return ctl

    def test_default_disabled_never_triggers(self):
        self.assertFalse(self.ctl.enabled)
        for _ in range(120):  # 远超 30 分钟的空闲快照
            self.clock.advance(120)
            self.ctl.observe(_snap())
        self.assertFalse(self.ctl.due)
        self.assertIsNone(self.ctl.remaining())
        self.assertEqual(self.ctl.finalize(_snap()), 'aborted')
        self.assertEqual(self.calls, [])
        self.assertNotIn('tkinter', sys.modules)  # 未启动 GUI

    def test_boundary_1799_no_trigger_1800_triggers_once(self):
        self._arm()
        self.assertEqual(self.ctl.remaining(), 1800)
        self.clock.advance(1799)
        self.ctl.observe(_snap())
        self.assertFalse(self.ctl.due)
        self.assertEqual(self.ctl.remaining(), 1)
        self.clock.advance(1)
        self.ctl.observe(_snap())
        self.assertTrue(self.ctl.due)
        self.assertEqual(self.ctl.remaining(), 0)
        self.assertEqual(self.ctl.finalize(_snap()), 'shutdown')
        self.assertEqual(len(self.calls), 1)  # 成功至多一次
        self.assertTrue(self.ctl.triggered)
        # 触发后即使重新勾选/继续喂空闲快照也绝不再触发。
        self.ctl.set_enabled(True)
        self.assertFalse(self.ctl.enabled)
        self.clock.advance(7200)
        self.ctl.observe(_snap())
        self.assertEqual(self.ctl.finalize(_snap()), 'aborted')
        self.assertEqual(len(self.calls), 1)

    def test_uncheck_cancels_immediately(self):
        self._arm()
        self.clock.advance(1700)
        self.ctl.observe(_snap())
        self.ctl.set_enabled(False)
        self.clock.advance(3600)
        self.ctl.observe(_snap())
        self.assertFalse(self.ctl.due)
        self.assertEqual(self.ctl.finalize(_snap()), 'aborted')
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.ctl.remaining())

    def test_in_flight_luna_retired_all_block_and_restart_full_30min(self):
        for busy in ({'domestic_active': 1},   # reserved/running/unknown 均计入在途
                     {'luna_active': 1}, {'retired_active': 1}):
            with self.subTest(busy=busy):
                ctl = self._arm()
                self.clock.advance(1500)
                ctl.observe(_snap(**busy))
                self.assertFalse(ctl.due)
                self.assertEqual(ctl.status()['pause_reason'], 'busy')
                self.clock.advance(100)
                ctl.observe(_snap())  # 活任务结束 → 重新计满 30 分钟
                self.clock.advance(1799)
                ctl.observe(_snap())
                self.assertFalse(ctl.due)
                self.clock.advance(1)
                ctl.observe(_snap())
                self.assertTrue(ctl.due)

    def test_short_task_missed_between_polls_resets_timer(self):
        ctl = self._arm()
        self.clock.advance(100)
        # 短任务在两个刷新间隔内开始又结束：active==0 但活动签名已变 → 重置。
        ctl.observe(_snap(activity=_activity(rowid=6, count=6,
                                             ended='2026-10-08T14:40:00+00:00')))
        self.assertFalse(ctl.due)
        self.clock.advance(1700)  # 距勾选已 1800 秒，但距重置只有 1700
        ctl.observe(_snap(activity=_activity(rowid=6, count=6,
                                             ended='2026-10-08T14:40:00+00:00')))
        self.assertFalse(ctl.due)
        self.clock.advance(100)
        ctl.observe(_snap(activity=_activity(rowid=6, count=6,
                                             ended='2026-10-08T14:40:00+00:00')))
        self.assertTrue(ctl.due)

    def test_read_error_stale_and_missing_fields_reset_and_never_use_old_green(self):
        ctl = self._arm()
        self.clock.advance(1500)
        ctl.observe({'ok': False, 'error_code': 'read_failed', 'rows': [],
                     'domestic_active': None, 'luna_active': None})
        self.assertFalse(ctl.due)
        self.assertEqual(ctl.status()['pause_reason'], 'read_failed')
        # 恢复后必须重新完整 30 分钟，绝不接着旧计时关机。
        self.clock.advance(400)   # 距勾选已 1900 秒
        ctl.observe(_snap())
        self.assertFalse(ctl.due)
        self.clock.advance(1799)
        ctl.observe(_snap())
        self.assertFalse(ctl.due)
        self.clock.advance(1)
        ctl.observe(_snap())
        self.assertTrue(ctl.due)
        # 陈旧快照（now_utc 远超允许时差）→ 暂停重新计时。
        ctl2 = self._arm(self._ctl())
        self.clock.advance(600)
        ctl2.observe(_snap(now=T0 - timedelta(seconds=121)))
        self.assertFalse(ctl2.due)
        self.assertEqual(ctl2.status()['pause_reason'], 'stale')
        # 字段缺失（无活动签名/无占用计数）→ 绝不当空闲。
        ctl3 = self._arm(self._ctl())
        no_act = _snap()
        del no_act['activity']
        ctl3.observe(no_act)
        self.assertEqual(ctl3.status()['pause_reason'], 'fields_missing')
        no_cnt = _snap()
        del no_cnt['retired_active']
        ctl3.observe(no_cnt)
        self.assertFalse(ctl3.due)
        self.assertEqual(self.calls, [])

    def test_wall_clock_jump_does_not_trigger_early(self):
        # 计时只认注入的 monotonic 时钟；系统时钟跳变（快照与 utcnow 同步前跳）
        # 不得提前触发。
        jumped = [T0]
        ctl = wsw.IdleShutdownController(clock=self.clock,
                                         shutdown=lambda: self.calls.append(1),
                                         utcnow=lambda: jumped[0])
        ctl.set_enabled(True)
        ctl.observe(_snap(now=jumped[0]))
        jumped[0] = T0 + timedelta(hours=6)  # 系统时钟前跳 6 小时
        self.clock.advance(60)               # monotonic 只过了 60 秒
        ctl.observe(_snap(now=jumped[0]))
        self.assertFalse(ctl.due)
        self.assertEqual(self.calls, [])

    def test_finalize_recheck_sees_new_task_and_aborts(self):
        ctl = self._arm()
        self.clock.advance(1800)
        ctl.observe(_snap())
        self.assertTrue(ctl.due)
        # 最后复核时突然来任务 → 放弃触发并重新计时。
        self.assertEqual(ctl.finalize(_snap(domestic_active=1)), 'aborted')
        self.assertEqual(self.calls, [])
        self.assertFalse(ctl.due)
        ctl.observe(_snap(domestic_active=1))
        self.clock.advance(10)
        ctl.observe(_snap())
        self.clock.advance(1799)
        ctl.observe(_snap())
        self.assertFalse(ctl.due)
        self.clock.advance(1)
        ctl.observe(_snap())
        self.assertTrue(ctl.due)
        self.assertEqual(ctl.finalize(_snap()), 'shutdown')
        self.assertEqual(len(self.calls), 1)

    def test_finalize_recheck_signature_changed_aborts(self):
        ctl = self._arm()
        self.clock.advance(1800)
        ctl.observe(_snap())
        self.assertTrue(ctl.due)
        changed = _snap(activity=_activity(rowid=9, count=9,
                                           ended='2026-10-08T15:10:00+00:00'))
        self.assertEqual(ctl.finalize(changed), 'aborted')
        self.assertEqual(self.calls, [])
        self.assertEqual(ctl.status()['pause_reason'], 'signature_changed')

    def test_finalize_recheck_read_failure_aborts(self):
        ctl = self._arm()
        self.clock.advance(1800)
        ctl.observe(_snap())
        self.assertTrue(ctl.due)
        self.assertEqual(ctl.finalize({'ok': False, 'error_code': 'read_failed'}),
                         'aborted')
        self.assertEqual(self.calls, [])
        self.assertFalse(ctl.due)

    def test_shutdown_failure_shows_and_disables_without_retry_loop(self):
        attempts = []

        def boom():
            attempts.append(1)
            raise OSError('shutdown refused')

        ctl = wsw.IdleShutdownController(clock=self.clock, shutdown=boom,
                                         utcnow=lambda: T0)
        ctl.set_enabled(True)
        ctl.observe(_snap())
        self.clock.advance(1800)
        ctl.observe(_snap())
        self.assertTrue(ctl.due)
        self.assertEqual(ctl.finalize(_snap()), 'failed')
        self.assertTrue(ctl.failed)
        self.assertFalse(ctl.enabled)  # 失败即解除勾选
        st = ctl.status()
        self.assertTrue(st['failed'])
        self.assertIn('失败', wsw.idle_status_text(st))
        # 之后每 2 秒继续喂空闲快照也绝不重试。
        for _ in range(10):
            self.clock.advance(2)
            ctl.observe(_snap())
            self.assertEqual(ctl.finalize(_snap()), 'aborted')
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.calls, [])

    def test_status_text_states_are_short_and_no_secrets(self):
        ctl = self._arm()
        self.assertIn('剩余 30:00', wsw.idle_status_text(ctl.status()))
        self.clock.advance(60)
        ctl.observe(_snap())
        self.assertIn('剩余 29:00', wsw.idle_status_text(ctl.status()))
        for text in (wsw.idle_status_text({'enabled': False, 'triggered': False,
                                           'failed': False, 'due': False,
                                           'remaining': None,
                                           'pause_reason': None}),
                     wsw.idle_status_text({'enabled': False, 'triggered': True,
                                           'failed': False, 'due': False,
                                           'remaining': None,
                                           'pause_reason': None})):
            self.assertLessEqual(len(text), 24)
            self.assertNotIn('token', text.lower())


# ------------------------------------------- 时段外展示标签（BW-OPENCODE-20261019-S4）
class TestWindowClosedLabels(unittest.TestCase):
    """仅显示层的时段外富标签；不改时段门/容量/派工/关机。纯函数注入 naive 北京时间；
    不启动 Tk、不联网、不读日历。Claude 复用 deepseek_offpeak 纯函数计算下一个连续
    放行区间，跨日/跨周末/跨法定假日都不硬截成"次日09:00"。"""

    def test_max_pools_fixed_window_label_independent_of_now(self):
        for pk in dp.WINDOW_GATED_MAIN_FORCES:
            for now in (datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc),
                        datetime(2026, 10, 8, 0, 0, tzinfo=timezone.utc)):
                self.assertEqual(wsw.window_closed_display_label(pk, now),
                                 '时段外（22:00–次日08:00）')
        self.assertEqual(wsw.MAX_WINDOW_CLOSED_LABEL, '时段外（22:00–次日08:00）')

    def test_max_window_boundary_22_open_08_closed(self):
        # 北京 22:00(=UTC 14:00, 10-07) 含 → 窗口开，不挡；北京 08:00(=UTC 00:00, 10-08)
        # 不含 → 窗口关，挡下。
        self.assertFalse(dp._max_window_blocks(dp.QMAX_POOL_KEY,
                                               datetime(2026, 10, 7, 14, 0,
                                                        tzinfo=timezone.utc)))
        self.assertTrue(dp._max_window_blocks(dp.QMAX_POOL_KEY,
                                              datetime(2026, 10, 8, 0, 0,
                                                       tzinfo=timezone.utc)))

    def test_claude_next_open_window_boundaries_cross_day_weekend_holiday(self):
        # 周四 10:00 高峰(09-12) → 下一放行 12:00–14:00 同日。
        self.assertEqual(wsw.claude_next_open_window(datetime(2026, 10, 8, 10, 0)),
                         (datetime(2026, 10, 8, 12, 0), datetime(2026, 10, 8, 14, 0)))
        # 周四 15:00 高峰(14-18) → 下一放行 18:00–次日(周五)09:00。
        self.assertEqual(wsw.claude_next_open_window(datetime(2026, 10, 8, 15, 0)),
                         (datetime(2026, 10, 8, 18, 0), datetime(2026, 10, 9, 9, 0)))
        # 周五 15:00 → 跨整个周末到周一 09:00（绝不硬截成"次日09:00"）。
        self.assertEqual(wsw.claude_next_open_window(datetime(2026, 10, 9, 15, 0)),
                         (datetime(2026, 10, 9, 18, 0), datetime(2026, 10, 12, 9, 0)))
        # 9-30(周三)15:00 → 跨国庆假期 10-01..07 到 10-08(周四)09:00。
        self.assertEqual(wsw.claude_next_open_window(datetime(2026, 9, 30, 15, 0)),
                         (datetime(2026, 9, 30, 18, 0), datetime(2026, 10, 8, 9, 0)))
        # 未知年度 2027-01-07(周四)10:00 → 高峰保守关闭，下一放行 12:00–14:00。
        self.assertEqual(wsw.claude_next_open_window(datetime(2027, 1, 7, 10, 0)),
                         (datetime(2027, 1, 7, 12, 0), datetime(2027, 1, 7, 14, 0)))
        # 可派时刻（周末/非高峰）返回 None。
        self.assertIsNone(wsw.claude_next_open_window(datetime(2026, 10, 8, 13, 0)))
        self.assertIsNone(wsw.claude_next_open_window(datetime(2026, 10, 10, 10, 0)))

    def test_claude_display_label_via_aware_utc_now(self):
        # UTC 02:00 = 北京周四 10:00 → 富标签 12:00–14:00。
        self.assertEqual(
            wsw.window_closed_display_label(
                dp.CLAUDE_POOL_KEY, datetime(2026, 10, 8, 2, 0, tzinfo=timezone.utc)),
            '时段外（下次12:00–14:00）')
        # UTC 07:00 = 北京周四 15:00 → 跨日到次日 09:00。
        self.assertEqual(
            wsw.window_closed_display_label(
                dp.CLAUDE_POOL_KEY, datetime(2026, 10, 8, 7, 0, tzinfo=timezone.utc)),
            '时段外（下次18:00–次日09:00）')
        # 北京周五 15:00 → 跨周末到 10-12，写明日期，不截次日。
        self.assertEqual(
            wsw.window_closed_display_label(
                dp.CLAUDE_POOL_KEY, datetime(2026, 10, 9, 7, 0, tzinfo=timezone.utc)),
            '时段外（下次18:00–10-12 09:00）')

    def test_display_only_no_state_gate_mutation(self):
        # 标签富化绝不改变时段门判定本身（同一 now 下 blocked 结果与原语义一致）。
        now = datetime(2026, 10, 8, 2, 0, tzinfo=timezone.utc)  # 北京周四 10:00
        self.assertTrue(dp._max_window_blocks(dp.CLAUDE_POOL_KEY, now))
        self.assertTrue(dp._max_window_blocks(dp.QMAX_POOL_KEY, now))
        self.assertFalse(dp._max_window_blocks(dp.CODEBUDDY_POOL_KEY, now))


class TestRealShutdownCommandMocked(unittest.TestCase):

    def test_system_shutdown_uses_existing_exe_without_force_or_reboot(self):
        with mock.patch.object(wsw.subprocess, 'run') as m_run:
            wsw._system_shutdown()
        m_run.assert_called_once()
        args, kwargs = m_run.call_args
        cmd = args[0]
        self.assertEqual(cmd, ['shutdown.exe', '/s', '/t', '0'])
        self.assertNotIn('/f', cmd)   # 不强杀
        self.assertNotIn('/r', cmd)   # 不重启
        self.assertNotIn('/a', cmd)   # 不动别人安排的关机
        self.assertFalse(kwargs.get('shell'))
        self.assertTrue(kwargs.get('check'))


# ------------------------------------- 真实 Tk 布局高度回归（BW-HOVER-REPAIR-20261019-S6）
class TestRealTkLayoutHeight(unittest.TestCase):
    """用户现场确认：鼠标移开后模型行已隐藏，但外框尺寸没恢复。Root 真实 Tk 复现：
    展开 height=710；行隐藏后 height 仍 643/requested=643——旧测试只断言行数为 0 所以
    误通过。本类用**真实 Tk** 驱动生产 `apply_layout` 的 pack 决策，同时断言可见模型行数
    与窗口 shell 的请求高度：紧凑无行时 rows_frame 必须退出布局、移出后高度恢复到紧凑
    内容需要的高度（不是只撤下底部区块）；一条运行仍显示该行并按真实行数缩高；重新移入
    展开仍显示全部；动态刷新（增/减运行行）高度跟随。无显示环境（无法创建 Tk）整体跳过，
    由 Root 在有显示环境运行真实 Tk 与完整测试。"""

    CARD_H = 30       # 单张行卡固定高，可确定算出 shell 请求高度
    SUMMARY_H = 15
    EMPTY_H = 40

    def setUp(self):
        try:
            import tkinter as tk
        except Exception:  # noqa: BLE001 - 无 tkinter 直接跳过，绝不影响离线断言
            self.skipTest('tkinter unavailable')
        try:
            self.root = tk.Tk()
        except Exception:  # noqa: BLE001 - 无显示环境（headless）诚实跳过，不伪报通过
            self.skipTest('no display for real Tk regression')
        self.tk = tk
        self.root.withdraw()  # 不闪现窗口，仅用于几何测量
        self.addCleanup(self._teardown)

        self.shell = tk.Frame(self.root)
        self.shell.pack()
        self.frames = {
            'summary': tk.Frame(self.shell, height=self.SUMMARY_H),
            'rows': tk.Frame(self.shell),
            'empty': tk.Frame(self.shell, height=self.EMPTY_H),
            'compact_idle': tk.Frame(self.shell, height=12),
            'idle': tk.Frame(self.shell, height=20),
            'detail': tk.Frame(self.shell, height=50),
            'footer': tk.Frame(self.shell, height=12),
        }
        self.cards = [tk.Frame(self.frames['rows'], height=self.CARD_H)
                      for _ in range(12)]

    def _teardown(self):
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    def _set_visible(self, count):
        """镜像生产 _set_rows：先 forget 全部卡，只 pack 前 count 张（fill='x', pady=2）。"""
        for c in self.cards:
            c.pack_forget()
        for c in self.cards[:count]:
            c.pack(fill='x', pady=2)
        return tuple(range(count))

    def _apply(self, expanded, visible, idle_compact=False):
        wsw.apply_layout(self.frames, expanded=expanded,
                         has_rows=bool(visible), idle_compact=idle_compact)
        self.root.update_idletasks()
        return self.frames['rows'].winfo_reqheight(), self.shell.winfo_reqheight()

    def _packed_cards(self):
        # The test root is intentionally withdrawn, so winfo_ismapped() is
        # always false for descendants.  The pack manager state is the layout
        # contract that apply_layout() controls and remains observable here.
        return sum(1 for c in self.cards if c.winfo_manager() == 'pack')

    def test_compact_zero_rows_drops_rows_frame_from_layout(self):
        vis = self._set_visible(0)
        self._apply(False, vis)
        # 无可见行：rows_frame 绝不参与布局，empty_card 顶上，窗口收到紧凑内容高度。
        self.assertEqual(self.frames['rows'].winfo_manager(), '')
        self.assertEqual(self.frames['empty'].winfo_manager(), 'pack')
        self.assertEqual(self._packed_cards(), 0)

    def test_compact_one_running_shows_row_and_shrinks_by_real_count(self):
        vis = self._set_visible(1)
        _, one_h = self._apply(False, vis)
        self.assertEqual(self.frames['rows'].winfo_manager(), 'pack')
        self.assertEqual(self.frames['empty'].winfo_manager(), '')
        self.assertEqual(self._packed_cards(), 1)

        vis0 = self._set_visible(0)
        _, zero_h = self._apply(False, vis0)
        # 一条运行行与零运行占位使用不同内容高度；关键契约是 rows_frame
        # 只在有真实行时参与布局。零运行占位本身可能比单张行卡更高。
        self.assertNotEqual(one_h, zero_h)

    def test_height_recovers_after_expand_then_collapse(self):
        # 关键回归：先量紧凑零行高度，再展开成 12 行，再收回零行——高度必须回到初始值，
        # 而非像旧缺陷那样停在展开时的 643/710 附近。
        _, compact0 = self._apply(False, self._set_visible(0))
        _, expanded = self._apply(True, self._set_visible(12))
        self.assertEqual(self.frames['rows'].winfo_manager(), 'pack')
        self.assertEqual(self.frames['empty'].winfo_manager(), '')
        self.assertEqual(self._packed_cards(), 12)
        self.assertGreater(expanded, compact0)
        _, compact1 = self._apply(False, self._set_visible(0))
        self.assertEqual(self.frames['rows'].winfo_manager(), '')
        self.assertEqual(self._packed_cards(), 0)
        # 收回后精确恢复到展开前的紧凑高度（旧代码 rows_frame 常驻会残留展开高度）。
        self.assertEqual(compact1, compact0)
        self.assertLess(compact1, expanded)

    def test_dynamic_refresh_adds_and_removes_running_rows(self):
        # 动态刷新：0 → 1 → 0 → 2，可见行数与 rows_frame 布局参与状态、高度同步跟随。
        _, h0 = self._apply(False, self._set_visible(0))
        self.assertEqual(self._packed_cards(), 0)
        self.assertEqual(self.frames['rows'].winfo_manager(), '')

        _, h1 = self._apply(False, self._set_visible(1))
        self.assertEqual(self._packed_cards(), 1)
        self.assertEqual(self.frames['rows'].winfo_manager(), 'pack')
        self.assertNotEqual(h1, h0)

        _, h0b = self._apply(False, self._set_visible(0))
        self.assertEqual(self._packed_cards(), 0)
        self.assertEqual(self.frames['rows'].winfo_manager(), '')
        self.assertEqual(h0b, h0)  # 运行行消失后高度回到零运行值

        _, h2 = self._apply(False, self._set_visible(2))
        self.assertEqual(self._packed_cards(), 2)
        self.assertGreater(h2, h1)

    def test_reenter_expand_shows_all_again(self):
        # 移出收回 → 重新移入展开：恢复全部 12 行且高度回到展开量级。
        _, compact0 = self._apply(False, self._set_visible(0))
        _, h_exp = self._apply(True, self._set_visible(12))
        _, compact_recover = self._apply(False, self._set_visible(0))
        self.assertEqual(compact_recover, compact0)
        _, reexpand = self._apply(True, self._set_visible(12))
        self.assertEqual(self._packed_cards(), 12)
        self.assertEqual(self.frames['rows'].winfo_manager(), 'pack')
        self.assertEqual(reexpand, h_exp)

    def test_idle_compact_keeps_countdown_without_rows_frame(self):
        vis = self._set_visible(0)
        _, idle_h = self._apply(False, vis, idle_compact=True)
        self.assertEqual(self.frames['rows'].winfo_manager(), '')
        self.assertEqual(self.frames['compact_idle'].winfo_manager(), 'pack')
        self.assertEqual(self.frames['empty'].winfo_manager(), 'pack')


if __name__ == '__main__':
    unittest.main(verbosity=2)
