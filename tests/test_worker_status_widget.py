"""worker_status_widget 离线验收：只读快照语义 + 独立槽/占位/时段/限额/失败诚实性。

全部用例使用临时 SQLite（dp.connect 建 schema 后切 DELETE journal）、合成 rows、
注入时钟与 PID 探针；不启动真 Tk、不连真实 ~/.brain-worker 池、不调真实模型、
不探测真实进程。绝不放宽真实库读取语义：失败路径断言的是生产 read_snapshot 的
真实拒绝/待核行为。
BW-WORKER-WIDGET-20261018-S3：两 CodeBuddy 模型各自独立 1 槽（总 14）；展示层主行
状态绝不只信 DB running——死 PID/复用/探测未知/未绑定的 running 主行必须显著待核
（待核优先，不被同池真 running 掩盖），只有匹配活 PID 才显示运行中；reserved 无子
进程仍已预留；Luna 原生不套本机探针。
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
        self.assertEqual(order[:7], [
            dp.CODEBUDDY_POOL_KEY, dp.CN_CUSTOM_MAX_POOL_KEY, dp.CN_MAX_POOL_KEY,
            dp.QMAX_POOL_KEY, dp.CLAUDE_POOL_KEY, dp.ZCODE_POOL_KEY,
            dp.CODEBUDDY_GLM_POOL_KEY])
        self.assertEqual(set(order[7:9]), {dp.QFLASH_POOL_KEY, dp.CN_FLASH_POOL_KEY})
        self.assertEqual(order[-1], dp.LUNA_KEY)
        # 退休池绝不显示成可派行。
        self.assertNotIn('qodercn:DeepSeek-Flash', order)
        self.assertFalse(any(pk.startswith('workbuddy') for pk in order))
        # BW-WORKER-WIDGET-20261018-S3 当前权威：两 CodeBuddy 模型各独立 1 槽 → 物理总 14。
        self.assertEqual(dp.DOMESTIC_TOTAL_CAPACITY, 14)
        self.assertEqual((sum(dp.MAIN_FORCE_CAPACITY.values())
                          + sum(dp.OVERFLOW_CAPACITY.values())), 14)
        self.assertEqual(dp.MAIN_FORCE_CAPACITY[dp.CODEBUDDY_GLM_POOL_KEY], 1)
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
        with redirect_stdout(buf):
            code = wsw.main(['--store', self.store, '--once'])
        self.assertEqual(code, 0)
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
        self.assertEqual(snap['domestic_total_capacity'], 14)
        self.assertEqual(len(snap['rows']), 10)
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
        self.assertEqual(snap['domestic_total_capacity'], 14)
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
        self.assertEqual(qm['state_label'], '时段外')
        cl = self._row(snap, dp.CLAUDE_POOL_KEY)
        self.assertEqual(cl['state'], 'window_closed')
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
        self.assertEqual(snap['domestic_total_capacity'], 14)

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


if __name__ == '__main__':
    unittest.main(verbosity=2)
