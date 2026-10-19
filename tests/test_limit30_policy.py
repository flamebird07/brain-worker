"""BW-LIMIT30-20261019-S4 限额 30 分钟政策离线回归（零真实模型/零网络/零 Popen）。

只用临时 SQLite 与合成错误载体，绝不触碰真实共享池/凭据/生产库。聚焦上一轮集中缺口：
- 分类正确性：真实结构化 429 / 真实 402 Insufficient Balance 分别落 quota_429 与
  hard_balance_402（402 绝不冒充 429）；S4 缺口 1 回归——"API Error: 401 Insufficient
  Balance" 是认证 401，绝不得被识别为 402 硬余额；正文 429/402、取消码 4294967295、
  网络 ECONNRESET 不误报；
- 1800 秒只读窗口：1799 秒拒绝新派发 / 恰好 1800 秒按原优先级重新可选；到期不删原
  标记/证据、不声称额度恢复、无需手动重置（S4 缺口 4 的终态不泄漏锁核验）；
- S4 缺口 2 回归：main_force_limits 携带**非法时间**（历史损坏数据）时
  `_quota_limit_window_active` 绝不 ValueError 崩溃；未知证据原样保留，不伪造恢复
  （不谎称窗口有效，也不删标记）；
- 跨连接/跨 workspace 共享同一临时库：另一"进程"新派发同样在窗口内被拒；
- AUTO / 显式入口 / 预留 consume 三口径一致拒绝，且 consume 拒绝只释放本 token
  自己的 reserved（start_failed），不抢占、不泄漏；
- CodeBuddy GLM 与 DeepSeek 额度独立：窗口政策不波及两 CodeBuddy 池；无当轮
  DeepSeek 证据 GLM 仍可 reserve/consume，两槽互不占用；
- 浮窗展示纯函数：限额窗口内空闲显示"上轮限额"、真实运行中只显示"运行中"（运行
  优先于历史限额）、到期撤下且不声称恢复；旧标记时间不借杂项 updated_at 延长。

所有用例都用合成数据，不真实关机、不联网、不读写真实 ~/.brain-worker。
"""
import hashlib
import json
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(REPO / 'tests') not in sys.path:
    sys.path.insert(0, str(REPO / 'tests'))

import claude_direct as cd  # noqa: E402
import dispatch_pool as dp  # noqa: E402
import worker_status_widget as wsw  # noqa: E402

ZC = 'zcode:GLM-5.3'
CL = 'claude:deepseek-flash'
CB = 'codebuddy:deepseek-v4.1-flash'
GLM = 'codebuddy:glm-5.3-flash'
T0 = datetime(2026, 10, 19, 4, 0, 0, tzinfo=timezone.utc)
SHA64 = 'p' + '0' * 63


def _summary(result_event, *, subtype='success'):
    """与 test_claude_transport 同形的最小合成 summary（不读真实文件）。"""
    return {'subtype': subtype, 'is_error': bool(result_event.get('is_error')),
            'result_event': result_event, 'tool_use_count': 0,
            'tool_result_count': 0, 'ok_receipt_ids': [], 'error_receipt_ids': [],
            'missing_receipt_ids': [], 'orphan_result_ids': [], 'duplicate_use_ids': [],
            'duplicate_result_ids': [], 'permission_denials': [],
            'session_id': 'sess-synthetic', 'malformed_lines': 0, 'parse_error': False}


class _PoolBase(unittest.TestCase):
    """独立临时 dispatch 库；每个 task 用独立真实 workspace 目录，不放松单写守卫。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='bw-limit30-')
        self.base = Path(self._tmp.name)
        self.store = str(self.base / 'dispatch.sqlite3')
        self.assertNotEqual(self.store, str(dp.default_store_path()))
        self.addCleanup(self._tmp.cleanup)
        with closing(dp.connect(self.store)) as conn:
            conn.execute('PRAGMA journal_mode=DELETE')

    def ws(self, task):
        d = self.base / 'ws' / str(task)
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def insert_attempt(self, token, task, pk, state='reserved', *, ended=None):
        runtime, model = pk.split(':', 1)
        with closing(dp.connect(self.store)) as conn:
            conn.execute('INSERT INTO attempts(token, task_id, runtime, model, '
                         'pool_key, state, reserved_at_utc, ended_at_utc, workspace) '
                         'VALUES(?,?,?,?,?,?,?,?,?)',
                         (token, task, runtime, model, pk, state, dp._iso(T0),
                          dp._iso(ended) if ended else None, self.ws(task)))

    def evidence(self, name):
        p = self.base / name
        p.write_text(json.dumps({'errors': [{'status': 429,
                                             'message': 'upstream rate limit'}]}),
                     encoding='utf-8')
        return str(p), hashlib.sha256(p.read_bytes()).hexdigest()

    def mark(self, pk, task, token, *, now=T0):
        path, sha = self.evidence(f'{task}-limit.json')
        rec = dp.record_main_force_limit(self.store, pk, task_id=task,
                                         attempt_token=token, evidence_path=path,
                                         evidence_sha256=sha, now=now)
        self.assertTrue(rec['recorded'], rec)
        return path, sha

    def claim(self, task, runtime, model, executor, now):
        return dp.reserve(self.store, task_id=task, runtime=runtime, model=model,
                          workspace=self.ws(task), prompt_sha256=SHA64, stage='s',
                          executor=executor, now=now, _preclaim=False)

    def state_of(self, token):
        with closing(dp.connect(self.store)) as conn:
            r = conn.execute('SELECT state FROM attempts WHERE token=?',
                             (token,)).fetchone()
            return r['state'] if r is not None else None


# ------------------------------------------------------------------ 分类正确性
class TestQuotaClassification(unittest.TestCase):
    """真实 429/402 分别正确落标；401 余额文案、正文、取消码、网络错误不误报。"""

    def test_structured_429_is_quota_not_402(self):
        ev = {'subtype': 'error_during_execution', 'is_error': True,
              'result': 'API Error', 'session_id': 's',
              'errors': [{'status': 429, 'message': 'upstream rate limit'}]}
        f = cd.classify_result(_summary(ev), 1, '', is_engineering_task=False)
        self.assertTrue(f['quota_429'], f)
        self.assertFalse(f['hard_balance_402'], f)
        self.assertTrue(f['quota_limited'], f)

    def test_structured_402_balance_is_hard_quota_never_masqueraded_as_429(self):
        ev = {'subtype': 'error_during_execution', 'is_error': True,
              'result': 'API Error: 402 Insufficient Balance', 'session_id': 's',
              'errors': [{'status': 402, 'message': 'Insufficient Balance'}]}
        f = cd.classify_result(_summary(ev), 1, '', is_engineering_task=False)
        self.assertTrue(f['hard_balance_402'], f)
        self.assertTrue(f['quota_limited'], f)
        # 绝不把 402 标成 HTTP 429（正确硬余额分类）。
        self.assertFalse(f['quota_429'], f)

    def test_leading_string_402_balance_counts_as_hard_quota(self):
        ev = {'subtype': 'error_during_execution', 'is_error': True,
              'result': 'API Error: 402 Insufficient Balance', 'session_id': 's'}
        f = cd.classify_result(_summary(ev), 1, '', is_engineering_task=False)
        self.assertTrue(f['hard_balance_402'], f)
        self.assertFalse(f['quota_429'], f)

    def test_api_error_401_insufficient_balance_is_never_402(self):
        # S4 缺口 1 回归：401 Insufficient Balance 文案属认证失败，绝不能被认成
        # 402 硬余额（旧实现用 401|402 通用前缀正则误命中；修复后仅开头独立 402 才算）。
        for carrier in ('API Error: 401 Insufficient Balance',
                        'API Error: 401 insufficient balance',
                        '401 余额不足'):
            with self.subTest(carrier=carrier):
                self.assertFalse(cd._explicit_hard_balance_402([carrier], None, {}),
                                 carrier)
                ev = {'subtype': 'error_during_execution', 'is_error': True,
                      'result': carrier, 'session_id': 's'}
                f = cd.classify_result(_summary(ev), 1, '',
                                       is_engineering_task=False)
                self.assertFalse(f['hard_balance_402'], f)
                self.assertFalse(f['quota_429'], f)
                self.assertFalse(f['quota_limited'], f)

    def test_structured_401_with_balance_message_is_not_hard_balance(self):
        # 结构化 status=401 即使 message 带余额文本，也不是 402 硬余额。
        ev = {'subtype': 'error_during_execution', 'is_error': True,
              'result': 'API Error', 'session_id': 's',
              'errors': [{'status': 401, 'message': 'Insufficient Balance'}]}
        self.assertFalse(cd._explicit_hard_balance_402(
            ev['errors'], None, ev))
        f = cd.classify_result(_summary(ev), 1, '', is_engineering_task=False)
        self.assertFalse(f['hard_balance_402'], f)
        self.assertFalse(f['quota_limited'], f)

    def test_ordinary_body_text_and_cancel_and_network_never_trigger(self):
        # 成功正文里的 429/402/余额字样不进错误载体。
        ev = {'subtype': 'success', 'is_error': False, 'session_id': 's',
              'result': '报告中说明 429 与 402 Insufficient Balance 的成因，任务全部完成。'}
        f = cd.classify_result(_summary(ev), 0, '', is_engineering_task=False)
        self.assertFalse(f['quota_429'], f)
        self.assertFalse(f['hard_balance_402'], f)
        self.assertFalse(f['quota_limited'], f)
        # 取消退出码 4294967295 本身不是 429/402。
        ev2 = {'subtype': 'error_during_execution', 'is_error': True,
               'result': 'cancelled by user', 'session_id': 's'}
        f2 = cd.classify_result(_summary(ev2), 4294967295, '',
                                is_engineering_task=False)
        self.assertFalse(f2['quota_limited'], f2)
        # 网络 ECONNRESET 只落传输诊断，不是限额（与既有 transport 用例同形：
        # subtype=success 但 is_error=true 的真实错误载体）。
        ev3 = {'subtype': 'success', 'is_error': True,
               'result': 'API Error: Unable to connect to API (ECONNRESET)',
               'session_id': 's'}
        f3 = cd.classify_result(_summary(ev3), 1, '', is_engineering_task=False)
        self.assertFalse(f3['quota_limited'], f3)
        self.assertEqual(f3['failure_kind'], 'transport_error')


# ------------------------------------------------------------------ 1800 秒窗口
class TestQuotaLimitWindow(_PoolBase):
    """1799 拒 / 1800 放行；到期不删证据、不声称恢复、不泄漏锁、跨连接一致。"""

    def test_1799_rejects_and_1800_allows_per_pool(self):
        for pk, runtime, model, tag in ((ZC, 'zcode', 'GLM-5.3', 'zc'),
                                        (CL, 'claude', 'deepseek-flash', 'cl')):
            with self.subTest(pool=pk):
                # task/文件/目录名一律不带 ':'（Windows 路径安全）。
                tok = f'bound-{tag}'
                self.insert_attempt(tok, f'b-{tag}', pk, 'running', ended=T0)
                self.mark(pk, f'b-{tag}', tok)
                denied = self.claim(f'new-{tag}', runtime, model, runtime,
                                    T0 + timedelta(seconds=1799))
                self.assertFalse(denied.get('allowed'), denied)
                self.assertEqual(denied['reason'], 'main_force_limited', denied)
                allowed = self.claim(f'exp-{tag}', runtime, model, runtime,
                                     T0 + timedelta(seconds=1800))
                self.assertTrue(allowed.get('allowed'), allowed)
                self.assertEqual(allowed['pool_key'], pk)
                # 到期只是**只读**放行：原标记 limited=1、绑定证据、错误原件都还在。
                snaps = dp.status(self.store, now=T0 + timedelta(seconds=1801))
                self.assertTrue(snaps['pools'][pk]['limited'], snaps['pools'][pk])
                lim = snaps['main_force_limits'][pk]
                self.assertEqual(int(lim['limited']), 1)
                self.assertTrue(Path(lim['evidence_path']).is_file())
                self.assertEqual(lim['evidence_sha256'],
                                 hashlib.sha256(
                                     Path(lim['evidence_path']).read_bytes()).hexdigest())

    def test_two_pools_independent(self):
        tok = 'bound-z'
        self.insert_attempt(tok, 'b-z', ZC, 'running', ended=T0)
        self.mark(ZC, 'b-z', tok)
        # ZCode 限额绝不连带 Claude：1799 秒时 Claude 正常可选。
        ok = self.claim('c-free', 'claude', 'deepseek-flash', 'claude',
                        T0 + timedelta(seconds=1799))
        self.assertTrue(ok.get('allowed'), ok)

    def test_other_process_other_workspace_same_store_also_refused(self):
        # 已在运行的另一进程（独立连接）绑定的真实失败，跨连接/跨 workspace 同样拒新派。
        tok = 'proc2-bound'
        self.insert_attempt(tok, 'proc2-task', ZC, 'running', ended=T0)
        self.mark(ZC, 'proc2-task', tok)
        r = self.claim('proc3-task', 'zcode', 'GLM-5.3', 'zcode',
                       T0 + timedelta(seconds=60))
        self.assertFalse(r.get('allowed'), r)
        self.assertEqual(r['reason'], 'main_force_limited', r)

    def test_in_flight_placeholder_never_preempted(self):
        # 窗口拒绝只作用于**新派发**：绑定失败的原 attempt 保持 running 不动。
        tok = 'busy-bound'
        self.insert_attempt(tok, 'busy-task', ZC, 'running', ended=None)
        self.mark(ZC, 'busy-task', tok, now=T0)
        r = self.claim('later', 'zcode', 'GLM-5.3', 'zcode',
                       T0 + timedelta(seconds=60))
        self.assertFalse(r.get('allowed'), r)
        self.assertEqual(self.state_of(tok), 'running')

    def test_consume_releases_only_own_reserved_and_lock_not_leaked(self):
        # 预留 consume 在窗口内一致拒绝；本 token 的 reserved 真实释放为 start_failed
        # （终态释放不泄漏锁——后续新事务立即可成功），其它在途不受影响。
        bound = 'lock-bound'
        self.insert_attempt(bound, 'lock-b', ZC, 'running', ended=T0)
        self.mark(ZC, 'lock-b', bound)
        mine = self.claim('lock-mine', 'zcode', 'GLM-5.3', 'zcode',
                          T0 - timedelta(seconds=3600))
        self.assertTrue(mine.get('allowed'), mine)
        out = dp.consume_for_entry(self.store, task_id='lock-mine', runtime='zcode',
                                   model='GLM-5.3', workspace=self.ws('lock-mine'),
                                   prompt_sha256=SHA64, stage='s',
                                   claim_token=mine['token'],
                                   now=T0 + timedelta(seconds=10))
        self.assertFalse(out.get('allowed'), out)
        self.assertEqual(out['reason'], 'main_force_limited', out)
        self.assertTrue(out.get('capacity_released'), out)
        self.assertEqual(self.state_of(mine['token']), 'start_failed')
        self.assertEqual(self.state_of(bound), 'running')
        # 不泄漏锁：紧随其后的普通预留（Claude 池）在同一库上立即成功。
        nxt = self.claim('lock-next', 'claude', 'deepseek-flash', 'claude',
                        T0 + timedelta(seconds=11))
        self.assertTrue(nxt.get('allowed'), nxt)

    def test_auto_explicit_consume_consistent(self):
        # 三口径一致：窗口内 AUTO 不选中 ZCode、显式入口拒绝、预留 consume 拒绝。
        bound = 'tri-bound'
        self.insert_attempt(bound, 'tri-b', ZC, 'running', ended=T0)
        self.mark(ZC, 'tri-b', bound)
        # 占满比 ZCode 更高的层（CodeBuddy deepseek 1 + GLM 1 + 三 Max 4 + Claude 2），
        # 使 AUTO 在无窗口时会落到 ZCode；有窗口必须跳过。
        for i, (pk, task) in enumerate(((CB, 'cb'), (GLM, 'gl'), (CL, 'cl'),
                                        (CL, 'cl2'))):
            self.insert_attempt(f'fill{i}', task, pk, 'running', ended=T0)
        with closing(dp.connect(self.store)) as conn:
            self.assertFalse(dp._quota_limit_window_active(conn, CL, T0 + timedelta(seconds=60)))
        now = T0 + timedelta(seconds=60)
        r = dp.select_and_claim(self.store, task_id='tri-auto', runtime='zcode',
                                model='GLM-5.3', workspace=self.ws('tri-auto'),
                                prompt_sha256=SHA64, now=now, executor='auto',
                                _preclaim=False)
        chosen = (r.get('selected') or {}).get('pool_key') or r.get('pool_key')
        self.assertNotEqual(chosen, ZC, r)
        exp = self.claim('tri-exp', 'zcode', 'GLM-5.3', 'zcode', now)
        self.assertFalse(exp.get('allowed'), exp)
        self.assertEqual(exp['reason'], 'main_force_limited', exp)

    def test_illegal_time_never_crashes_and_evidence_kept(self):
        # S4 缺口 2 回归：历史损坏时间串不得 ValueError 崩溃；未知证据保留、不伪造恢复。
        tok = 'bad-bound'
        self.insert_attempt(tok, 'bad-b', ZC, 'running', ended=None)
        self.mark(ZC, 'bad-b', tok)
        for bad in ('not-a-time', '2026-10-19T04:00', '2026-13-45T99:99:99'):
            with self.subTest(ts=bad), closing(dp.connect(self.store)) as conn:
                # connect 是 autocommit（isolation_level=None），直接 UPDATE 即落盘。
                conn.execute('UPDATE main_force_limits SET recorded_at_utc=? '
                             'WHERE pool_key=?', (bad, ZC))
                # 不抛 ValueError，按"不伪造有效窗口"返回 False。
                self.assertFalse(dp._quota_limit_window_active(conn, ZC, T0 + timedelta(seconds=60)))
        # 标记与错误原件都保留（不伪造恢复、不删证据），杂项 updated_at 不被借用延长。
        snaps = dp.status(self.store, now=T0 + timedelta(seconds=120))
        self.assertEqual(int(snaps['main_force_limits'][ZC]['limited']), 1)
        self.assertTrue(Path(snaps['main_force_limits'][ZC]['evidence_path']).is_file())

    def test_future_time_and_naive_time_never_fabricate_block(self):
        tok = 'fut-bound'
        self.insert_attempt(tok, 'fut-b', ZC, 'running', ended=None)
        self.mark(ZC, 'fut-b', tok)
        with closing(dp.connect(self.store)) as conn:
            conn.execute('UPDATE main_force_limits SET recorded_at_utc=? WHERE pool_key=?',
                         (dp._iso(T0 + timedelta(hours=2)), ZC))
            self.assertFalse(dp._quota_limit_window_active(conn, ZC, T0))
            conn.execute('UPDATE main_force_limits SET recorded_at_utc=? WHERE pool_key=?',
                         ('2026-10-19T04:00:00', ZC))  # 无时区
            self.assertFalse(dp._quota_limit_window_active(conn, ZC, T0 + timedelta(seconds=60)))

    def test_bound_ended_at_overrides_marker_recorded_at(self):
        # 新一次真实失败从该失败时刻计：优先取绑定 attempt 的 ended_at_utc。
        tok = 'ref-bound'
        self.insert_attempt(tok, 'ref-b', ZC, 'running', ended=T0)
        self.mark(ZC, 'ref-b', tok, now=T0 - timedelta(hours=1))
        with closing(dp.connect(self.store)) as conn:
            # 距 ended_at 1799 秒仍拒（不因 recorded_at 更早而提前放行）。
            self.assertTrue(dp._quota_limit_window_active(
                conn, ZC, T0 + timedelta(seconds=1799)))
            # 距 ended_at 恰好 1800 秒放行（不因 recorded_at 刷新而延长）。
            self.assertFalse(dp._quota_limit_window_active(
                conn, ZC, T0 + timedelta(seconds=1800)))


# ------------------------------------------------------------------ CodeBuddy 独立
class TestCodeBuddyIndependentOfWindow(_PoolBase):
    """两 CodeBuddy 池不在 1800 秒窗口池内；GLM 无任何 DeepSeek 证据可 reserve/consume，
    两模型槽各自独立、优先级不动。"""

    def test_glm_reserve_and_consume_without_any_evidence(self):
        r = dp.reserve(self.store, task_id='glm1', runtime='codebuddy',
                       model='glm-5.3-flash', workspace=self.ws('glm1'),
                       prompt_sha256=SHA64, stage='s', now=T0,
                       executor='codebuddy', codebuddy_glm_allowed=False,
                       _preclaim=False)
        self.assertTrue(r.get('allowed'), r)
        c = dp.consume_for_entry(self.store, task_id='glm1', runtime='codebuddy',
                                 model='glm-5.3-flash', workspace=self.ws('glm1'),
                                 prompt_sha256=SHA64, stage='s', claim_token=r['token'],
                                 now=T0, executor='codebuddy',
                                 codebuddy_glm_allowed=False)
        self.assertTrue(c.get('allowed'), c)
        # 独立槽：DeepSeek 同时在途不挡 GLM 第 2 用例的入口，反之亦然。
        d = dp.reserve(self.store, task_id='cb1', runtime='codebuddy',
                       model='deepseek-v4.1-flash', workspace=self.ws('cb1'),
                       prompt_sha256=SHA64, stage='s', now=T0,
                       executor='codebuddy', _preclaim=False)
        self.assertTrue(d.get('allowed'), d)
        # 同模型第 2 个仍 capacity_full（各 1 独立槽，不减容量）。
        again = dp.reserve(self.store, task_id='glm2', runtime='codebuddy',
                           model='glm-5.3-flash', workspace=self.ws('glm2'),
                           prompt_sha256=SHA64, stage='s', now=T0,
                           executor='codebuddy', codebuddy_glm_allowed=True,
                           _preclaim=False)
        self.assertFalse(again.get('allowed'), again)
        self.assertEqual(again['reason'], 'capacity_full', again)

    def test_window_pools_include_codebuddy_deepseek_only(self):
        self.assertIn(CB, dp.QUOTA_LIMIT_WINDOW_POOLS)
        self.assertNotIn(GLM, dp.QUOTA_LIMIT_WINDOW_POOLS)
        self.assertEqual(dp.QUOTA_LIMIT_WINDOW_POOLS, (ZC, CL, CB))
        self.assertEqual(dp.QUOTA_LIMIT_WINDOW_SECONDS, 1800)


# ------------------------------------------------------------------ 浮窗展示
class TestWidgetDisplayRules(unittest.TestCase):
    """纯视图函数：运行优先于历史限额；窗口内空闲显示限额；到期消失且不声称恢复。"""

    def _lim(self, ended):
        return {'limited': 1, 'attempt_token': 't1', 'recorded_at_utc': ended}

    def test_window_idle_shows_limit_then_expires(self):
        lim = self._lim(dp._iso(T0))
        latest_any = {ZC: {'token': 't1'}}
        self.assertTrue(wsw._prev_limited_for(ZC, lim, latest_any, dp._iso(T0),
                                              T0 + timedelta(seconds=1799)))
        self.assertFalse(wsw._prev_limited_for(ZC, lim, latest_any, dp._iso(T0),
                                               T0 + timedelta(seconds=1800)))
        row = {'prev_limited': True, 'attempts': []}
        self.assertTrue(wsw.row_shows_limit(row))
        row_expired = {'prev_limited': False, 'attempts': []}
        self.assertFalse(wsw.row_shows_limit(row_expired))

    def test_true_running_hides_limit(self):
        running_row = {'prev_limited': True,
                       'attempts': [{'display_state': 'running'}]}
        self.assertFalse(wsw.row_shows_limit(running_row))
        self.assertEqual(wsw._main_state_display('running', '运行中', True, True),
                         '运行中')
        # 未知+真运行混池：待核优先，但不冒充全绿、也不附带限额文字。
        self.assertEqual(wsw._main_state_display('unknown', '待核实', True, True),
                         '待核实')

    def test_idle_within_window_displays_prev_limit(self):
        self.assertEqual(wsw._main_state_display('idle', '空闲', True, False),
                         '上轮限额')
        self.assertEqual(wsw._main_state_display('idle', '空闲', False, False), '空闲')

    def test_illegal_display_time_never_fabricates_limit(self):
        for bad in ('garbage', '2026-10-19T04:00', dp._iso(T0 + timedelta(hours=5))):
            with self.subTest(ts=bad):
                self.assertFalse(wsw._quota_window_limited(
                    self._lim(bad), bad, T0 + timedelta(seconds=60)))


if __name__ == '__main__':
    unittest.main()
