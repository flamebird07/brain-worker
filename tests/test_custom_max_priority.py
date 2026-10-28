"""CN token plan Max 用户禁用的离线回归；临时 SQLite、假入口、零真实模型。"""
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / 'scripts'
for p in (str(SCRIPTS), str(REPO / 'tests')):
    if p not in sys.path:
        sys.path.insert(0, p)

import dispatch_mode as dm  # noqa: E402
import dispatch_pool as dp  # noqa: E402
from dispatch_mode_support import dispatch_mode  # noqa: E402
import stage_selection as ss  # noqa: E402

CODEBUDDY = 'codebuddy:deepseek-v4.1-flash'
QMAX = 'qoder:Qwen3.8-Max'
CN_MAX = 'qodercn:Qwen3.8-Max'
CN_CUSTOM_MAX = 'qodercn:Qwen-3.8-Max'
CLAUDE = 'claude:deepseek-flash'
ZCODE = 'zcode:GLM-5.3'  # BW-ZCODE-RESTORE-1-20261022：恢复可派主力（容量 1、层=2）
OPENCODE_DS = 'opencode:deepseek-v4.1-flash-free'
QFLASH = 'qoder:Qwen3.8-Flash'
CN_FLASH = 'qodercn:Qwen3.8-Flash'
MAXES = (QMAX, CN_MAX, CN_CUSTOM_MAX)

# 北京 23:00 周四：三 Max 夜间窗内，claude 非高峰可派
NIGHT = datetime(2026, 10, 8, 15, 0, 0, tzinfo=timezone.utc)
# 北京 10:00 周四：Max 夜间窗外；claude 工作日高峰门关；ZCode 全天
DAY = datetime(2026, 10, 8, 2, 0, 0, tzinfo=timezone.utc)

ALL_MAINS = list(dp.MAIN_FORCE_KEYS)
ALL_FLASH = list(dp.OVERFLOW_KEYS)
SHA = 'p' + '0' * 63


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='bw-custom-max-')
        self.base = Path(self._tmp.name)
        self.store = str(self.base / 'dispatch.sqlite3')
        self.assertNotEqual(self.store, str(dp.default_store_path()))
        self._seed_n = 0
        z = mock.patch.object(dp, '_zcode_availability_blocked',
                              lambda *a, **k: None)
        pid = mock.patch.object(dp, 'process_identity',
                                return_value={'state': 'alive', 'created': 'c1'})
        z.start()
        pid.start()
        self.addCleanup(z.stop)
        self.addCleanup(pid.stop)
        self.addCleanup(self._tmp.cleanup)
        self.build_store()

    def build_store(self):
        with closing(dp.connect(self.store)) as conn:
            dp._rotation_row(conn)
            conn.execute('PRAGMA journal_mode=DELETE')

    def ws(self, tid):
        d = self.base / 'ws' / str(tid)
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def seed(self, pk, n=1, state='reserved'):
        runtime, model = pk.split(':', 1)
        with closing(dp.connect(self.store)) as conn:
            for _ in range(n):
                self._seed_n += 1
                tok = f'tok-{self._seed_n}'
                conn.execute(
                    'INSERT INTO attempts(token, task_id, runtime, model, pool_key, '
                    'state, child_pid, reserved_at_utc) VALUES(?,?,?,?,?,?,?,?)',
                    (tok, f'task-{tok}', runtime, model, pk, state, None,
                     dp._iso(NIGHT)))
            conn.execute('PRAGMA journal_mode=DELETE')

    def fill_codebuddy(self):
        """占住 CodeBuddy 层=3 的唯一名额，使后续断言聚焦 Max 层内部次序。"""
        self.seed(CODEBUDDY, 1)

    def select(self, requested, now, zcode_blocked=False):
        with closing(dp.connect(self.store)) as conn:
            elig = dp._eligible_main_forces(conn, zcode_blocked, now)
            return dp._select_main_force(conn, requested, now, eligible=elig)

    def reserve(self, tid, runtime, model, executor='auto', now=NIGHT, ws=None):
        return dp.reserve(self.store, task_id=tid, runtime=runtime, model=model,
                          workspace=ws or self.ws(tid), prompt_sha256=SHA, stage='s',
                          now=now, executor=executor, _preclaim=False)

    def claim_auto(self, tid, runtime='qoder', model='Qwen3.8-Flash', now=NIGHT):
        # BW-ZCODE-RETIRE-20261020：原默认请求组合 zcode:GLM-5.3 已退役（会先撞
        # unknown_pool），改用现役国际 Flash 请求组合验证 AUTO 层序。
        return dp.select_and_claim(self.store, task_id=tid, runtime=runtime, model=model,
                                   workspace=self.ws(tid), prompt_sha256=SHA, stage='s',
                                   now=now, executor='auto', _preclaim=False)

    def auth(self, allowed, task='T', stage='s2'):
        return {'selection_mode': 'auto', 'quote': '用户真实原话',
                'authorization_scope': {'task_id': task, 'stage': stage},
                'allowed_candidates': allowed}

    def plan(self, auth, task='T', stage='s2', now=NIGHT):
        return ss.plan_stage_selection(
            auth, previous=None, store_path=self.store, task_id=task, stage=stage,
            workspace=self.ws(task), now=now)


class CustomMaxPriorityTests(_Base):
    def test_disabled_custom_skipped_builtin_precedes_codebuddy(self):
        target, reason = self.select(ZCODE, NIGHT)
        self.assertEqual(reason, 'main_force_selected')
        self.assertEqual(target, CN_MAX)
        self.assertLess(dp.POOL_PRIORITY[CN_CUSTOM_MAX],
                        dp.POOL_PRIORITY[CODEBUDDY])

    def test_disabled_custom_with_legacy_inflight_still_skipped(self):
        self.fill_codebuddy()
        self.seed(CN_CUSTOM_MAX, 1)
        target, _ = self.select(QMAX, NIGHT)
        self.assertEqual(target, CN_MAX)

    def test_custom_full_falls_to_builtins(self):
        self.fill_codebuddy()
        self.seed(CN_CUSTOM_MAX, 2)
        target, _ = self.select(ZCODE, NIGHT)
        self.assertIn(target, (QMAX, CN_MAX))

    def test_all_max_full_falls_to_zcode_then_claude(self):
        # 全部 Max 满后先落恢复可派的 ZCode（层=2）；ZCode 唯一槽占满才轮到 claude。
        self.fill_codebuddy()
        self.seed(CN_CUSTOM_MAX, 2)
        self.seed(QMAX, 1)
        self.seed(CN_MAX, 1)
        target, _ = self.select(ZCODE, NIGHT)
        self.assertEqual(target, ZCODE)
        self.seed(ZCODE, 1)
        target, _ = self.select(ZCODE, NIGHT)
        self.assertEqual(target, CLAUDE)

    def test_day_all_max_excluded_in_weak_mode(self):
        # BW-MANUAL-THREE-MODE-20261023：weak（默认）档白天三个 Max **全部**被恢复后的夜间门
        # 剔除（DAY = 北京 10:00 工作日，claude 高峰门关闭）。层序 ZCode(2) > claude(3) >
        # 两 OpenCode(6)，绝不改派任何 Max。
        self.fill_codebuddy()
        with closing(dp.connect(self.store)) as conn:
            elig = dp._eligible_main_forces(conn, False, DAY)
        for pk in (CN_MAX, CN_CUSTOM_MAX, QMAX):
            self.assertNotIn(pk, elig)
        self.assertIn(ZCODE, elig)
        self.assertNotIn(CLAUDE, elig)  # 工作日 10:00 属高峰 → claude 恢复高峰门
        self.assertEqual(self.select(ZCODE, DAY)[0], ZCODE)
        self.seed(ZCODE, 1)
        self.assertIn(self.select(ZCODE, DAY)[0], dp.OPENCODE_POOL_KEYS)

    def test_day_max_all_day_in_strong_mode(self):
        # strong 档：六个可派池全部无时段门——白天三个 Max 与 claude 都在候选，层序
        # Sonus(1) > ZCode(2) > claude(3) > 三个 Max(4)。BW-SONUS-REENABLE-20261028：
        # Sonus 恢复启用、在 strong 候选且 AUTO 首选；占住其独立 1 槽后原目标（强档
        # 白天无时段门 + 层序）断言强度不变：ZCode → claude → 国际 Max → CN 自定义 Max。
        with dispatch_mode('strong'):
            self.fill_codebuddy()
            with closing(dp.connect(self.store)) as conn:
                elig = dp._eligible_main_forces(conn, False, DAY)
            self.assertNotIn(CN_CUSTOM_MAX, elig)
            for pk in (CN_MAX, QMAX, ZCODE, CLAUDE):
                self.assertIn(pk, elig)
            self.assertIn(dp.SONUS_POOL_KEY, elig)
            self.assertEqual(self.select(dp.SONUS_POOL_KEY, DAY)[0],
                             dp.SONUS_POOL_KEY)
            self.seed(dp.SONUS_POOL_KEY, 1)
            self.assertEqual(self.select(ZCODE, DAY)[0], ZCODE)
            self.seed(ZCODE, 1)
            self.assertEqual(self.select(ZCODE, DAY)[0], CLAUDE)
            self.seed(CLAUDE, 2)
            self.assertEqual(self.select(ZCODE, DAY)[0], QMAX)
            self.seed(QMAX, 1)
            self.assertEqual(self.select(ZCODE, DAY)[0], CN_MAX)

    def test_explicit_builtin_selection_not_rewritten(self):
        r = self.reserve('qm', 'qoder', 'Qwen3.8-Max', executor='qoder')
        self.assertTrue(r.get('allowed'), r)
        self.assertEqual(r['pool_key'], QMAX)
        r = self.reserve('cm', 'qodercn', 'Qwen3.8-Max', executor='qodercn')
        self.assertTrue(r.get('allowed'), r)
        self.assertEqual(r['pool_key'], CN_MAX)


class SameOrderAndGuardsTests(_Base):
    def test_stage_preview_and_atomic_entry_same_order(self):
        self.fill_codebuddy()
        auth = self.auth(ALL_MAINS + ALL_FLASH)
        r = self.plan(auth)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected']['pool_key'], CN_MAX, r)
        self.assertEqual(r['candidate_filters'][CN_CUSTOM_MAX], 'mode_excluded')
        out = self.claim_auto('t1')
        self.assertEqual(out['selected']['pool_key'], CN_MAX, out)

    def test_capacity_16_current_authority(self):
        # BW-STRONG-SONUS-20261024：物理总量 16（原 15 + strong 专属 Sonus 1 槽）；
        # weak 可新派容量仍为 15（Sonus 弱档不可新派）。
        self.assertEqual(dp.DOMESTIC_TOTAL_CAPACITY, 16)
        self.assertEqual(dp.capacity_for(CN_CUSTOM_MAX), 2)
        self.assertEqual(dp.capacity_for(ZCODE), 1)
        self.assertEqual(dp.capacity_for(dp.SONUS_POOL_KEY), 1)
        self.assertEqual(sum(dp.MAIN_FORCE_CAPACITY.values())
                         + sum(dp.OVERFLOW_CAPACITY.values()), 16)
        self.assertEqual(sum(dp.capacity_for(k) for k in dm.ALL_POOL_KEYS), 15)

    def test_workspace_single_write_guard_unchanged(self):
        # 同 workspace 在途互斥语义不变（恢复后的 zcode executor 与 codebuddy executor
        # 同受单写守卫约束；本例用 codebuddy 占位验证）。
        shared = self.ws('shared')
        r1 = self.reserve('w1', 'codebuddy', 'deepseek-v4.1-flash',
                          executor='codebuddy', ws=shared)
        self.assertTrue(r1.get('allowed'), r1)
        r2 = self.reserve('w2', 'codebuddy', 'deepseek-v4.1-flash',
                          executor='codebuddy', ws=shared)
        self.assertFalse(r2.get('allowed'), r2)
        self.assertEqual(r2['reason'], 'workspace_in_flight')

    def test_unknown_inflight_still_holds_capacity(self):
        self.fill_codebuddy()
        self.seed(CN_CUSTOM_MAX, 2, state='unknown')
        target, _ = self.select(ZCODE, NIGHT)
        self.assertNotEqual(target, CN_CUSTOM_MAX)
        self.assertIn(target, (QMAX, CN_MAX))


class UserDisabledCustomMaxTests(_Base):
    def legacy_claim(self, task):
        with mock.patch.object(dm, 'USER_DISABLED_POOL_KEYS', frozenset()):
            claim = self.reserve(task, 'qodercn', 'Qwen-3.8-Max', executor='qodercn')
        self.assertTrue(claim['allowed'], claim)
        return claim['token']

    def consume(self, task, token, **changes):
        args = dict(task_id=task, runtime='qodercn', model='Qwen-3.8-Max',
                    workspace=self.ws(task), prompt_sha256=SHA, stage='s',
                    claim_token=token, wrapper_pid=4321, wrapper_created='c1', now=NIGHT)
        args.update(changes)
        return dp.consume_for_entry(self.store, **args)

    def test_exact_disabled_registration_and_mode_counts(self):
        self.assertEqual(dm.USER_DISABLED_POOL_KEYS, {CN_CUSTOM_MAX})
        self.assertNotIn(CN_CUSTOM_MAX, dp.LEGACY_RETIRED_POOL_KEYS)
        self.assertEqual(dp.capacity_for(CN_CUSTOM_MAX), 2)
        self.assertEqual(dp.DOMESTIC_TOTAL_CAPACITY, 16)
        import install_desktop_skill as installer
        self.assertIn(CN_CUSTOM_MAX, installer.HOST_EXCLUDED['qoder'])
        for mode, count, slots in (('strong', 5, 6), ('medium', 4, 5), ('weak', 10, 13)):
            pools = dm.allowed_pools_for_mode(mode)
            self.assertEqual(len(pools), count)
            self.assertEqual(sum(dp.capacity_for(pk) for pk in pools), slots)
            self.assertNotIn(CN_CUSTOM_MAX, pools)
            self.assertIn(CN_MAX, pools)
            self.assertIn('用户要求禁用', dm.mode_excluded_reason(CN_CUSTOM_MAX, mode))

    def test_new_dispatch_refused_in_all_modes_day_and_night(self):
        for mode in dm.MODES:
            with dispatch_mode(mode):
                for now in (DAY, NIGHT):
                    for operation in (dp.reserve, dp.select_and_claim, dp.consume_for_entry):
                        for executor in ('auto', 'qodercn'):
                            out = operation(self.store, task_id='new', runtime='qodercn',
                                            model='Qwen-3.8-Max', workspace=self.ws('new'),
                                            prompt_sha256=SHA, executor=executor, now=now)
                            self.assertFalse(out['allowed'], out)
                            self.assertFalse(out['sent'], out)
                            self.assertEqual(out['reason'], 'mode_excluded', out)
                            self.assertIn('用户要求禁用', out['reasons'][0])
                            self.assertFalse(out.get('routing_required'), out)
                            self.assertIsNone(out.get('token'))
        with closing(dp.connect(self.store)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 0)

    def test_auto_cannot_select_disabled_even_as_last_free_main(self):
        for pk in dp.MAIN_FORCE_KEYS:
            if pk != CN_CUSTOM_MAX:
                self.seed(pk, dp.capacity_for(pk))
        for mode in dm.MODES:
            with dispatch_mode(mode):
                target, _ = self.select(CN_CUSTOM_MAX, NIGHT)
                self.assertIsNone(target)
                plan = self.plan(self.auth([CN_CUSTOM_MAX, CN_MAX]))
                self.assertEqual(plan['candidate_filters'][CN_CUSTOM_MAX], 'mode_excluded')
                self.assertIsNone(plan['selected'])

    def test_legacy_exact_claim_consumes_once_and_finishes_in_all_modes(self):
        for mode in dm.MODES:
            task = 'legacy-' + mode
            token = self.legacy_claim(task)
            with dispatch_mode(mode):
                out = self.consume(task, token)
                self.assertTrue(out['allowed'], out)
                duplicate = self.consume(task, token)
                self.assertFalse(duplicate['allowed'], duplicate)
                self.assertFalse(duplicate.get('capacity_released'))
                bound = dp.bind_child(self.store, token, child_pid=9876,
                                      child_created='child', wrapper_pid=4321,
                                      wrapper_created='c1', now=NIGHT)
                self.assertTrue(bound['bound'], bound)
                with mock.patch.object(dp, 'process_identity',
                                       return_value={'state': 'dead', 'created': None}):
                    ended = dp.finish(self.store, token, terminal='finished', success=True,
                                      wrapper_pid=4321, wrapper_created='c1', now=NIGHT)
                self.assertTrue(ended['released'], ended)
            self.assertEqual(dp.status(self.store, now=NIGHT)['pools'][CN_CUSTOM_MAX]['active'], 0)

    def test_legacy_claim_drift_and_workspace_guard_remain(self):
        token = self.legacy_claim('old')
        for drift in ({'task_id': 'wrong'}, {'runtime': 'qoder'},
                      {'model': 'Qwen3.8-Max'}, {'workspace': self.ws('wrong')},
                      {'prompt_sha256': 'wrong'}, {'stage': 'wrong'}, {'chat_id': 'wrong'}):
            out = self.consume('old', token, **drift)
            self.assertEqual(out['reason'], 'claim_invalid', out)
        with closing(dp.connect(self.store)) as conn:
            dp._insert_claim(conn, task_id='other', stage='s', chat_id=None,
                             runtime='qodercn', model='Qwen3.8-Max', pk=CN_MAX,
                             workspace=self.ws('old'), prompt_sha256=SHA,
                             token=None, now=NIGHT, origin='offline-legacy-fixture')
        out = self.consume('old', token)
        self.assertEqual(out['reason'], 'workspace_in_flight', out)
        with closing(dp.connect(self.store)) as conn:
            self.assertEqual(conn.execute('SELECT state FROM attempts WHERE token=?',
                                          (token,)).fetchone()[0], 'reserved')

    def test_running_unknown_and_reserved_never_mutated_by_new_refusal(self):
        for state in dp.ACTIVE_STATES:
            self.seed(CN_CUSTOM_MAX, state=state)
        with closing(dp.connect(self.store)) as conn:
            before = [tuple(row) for row in conn.execute('SELECT * FROM attempts')]
        for mode in dm.MODES:
            with dispatch_mode(mode):
                out = self.reserve('new', 'qodercn', 'Qwen-3.8-Max', executor='qodercn')
                self.assertEqual(out['reason'], 'mode_excluded')
        with closing(dp.connect(self.store)) as conn:
            self.assertEqual([tuple(row) for row in conn.execute('SELECT * FROM attempts')],
                             before)

    def test_claim_due_exact_disabled_ticket_stays_pending(self):
        import json
        scope = {'task_id': 'ticket', 'stage': 's', 'chat_id': 'c',
                 'workspace': self.ws('ticket'), 'prompt_sha256': SHA,
                 'executor': 'qodercn', 'model': 'Qwen-3.8-Max',
                 'allowed_candidates': [CN_CUSTOM_MAX]}
        with closing(dp.connect(self.store)) as conn:
            conn.execute('INSERT INTO luna_tickets(task_id, scope, state, asked_at_utc, '
                         'deadline_utc, updated_at_utc) VALUES(?,?,?,?,?,?)',
                         ('ticket', json.dumps(scope, sort_keys=True), 'pending',
                          dp._iso(NIGHT), dp._iso(NIGHT), dp._iso(NIGHT)))
        for mode in dm.MODES:
            with dispatch_mode(mode), mock.patch.object(dp, 'LUNA_DISABLED', False):
                out = dp.claim_due(self.store, task_id='ticket', now=NIGHT, _preclaim=False)
                with mock.patch.object(dp, '_domestic_active', return_value=16):
                    full = dp.claim_due(self.store, task_id='ticket', now=NIGHT, _preclaim=False)
            for result in (out, full):
                self.assertFalse(result['claimed'], result)
                self.assertEqual(result['reason'], 'mode_excluded', result)
                self.assertIn('用户要求禁用', ' '.join(result['reasons']))
        with closing(dp.connect(self.store)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT state FROM luna_tickets').fetchone()[0],
                             'pending')

    def test_claim_due_default_path_disabled_beats_luna_gate(self):
        # BW-QODERCN-TOKEN-MAX-DISABLE-20261028 S2：生产默认 LUNA_DISABLED=True 时，
        # claim_due 的「用户要求禁用」必须先于 Luna 总门——scope 唯一指向目标池的票据
        # 收到 mode_excluded（用户要求禁用），绝不被 luna_disabled 遮蔽；指向其它池
        # （如 CN 内置 Max）的票据保持原 luna_disabled 总门，不误伤。
        import json
        def _ticket(task_id, model):
            scope = {'task_id': task_id, 'stage': 's', 'chat_id': 'c',
                     'workspace': self.ws(task_id), 'prompt_sha256': SHA,
                     'executor': 'qodercn', 'model': model}
            with closing(dp.connect(self.store)) as conn:
                conn.execute('INSERT INTO luna_tickets(task_id, scope, state, '
                             'asked_at_utc, deadline_utc, updated_at_utc) '
                             'VALUES(?,?,?,?,?,?)',
                             (task_id, json.dumps(scope, sort_keys=True), 'pending',
                              dp._iso(NIGHT), dp._iso(NIGHT), dp._iso(NIGHT)))
        _ticket('ticket-default', 'Qwen-3.8-Max')
        _ticket('ticket-builtin', 'Qwen3.8-Max')
        # 显式钉住生产默认 True（其它测试模块可能全局改写 dp.LUNA_DISABLED，这里不依赖
        # 全局状态，只验证 True 时的默认路径顺序）。
        with mock.patch.object(dp, 'LUNA_DISABLED', True):
            out = dp.claim_due(self.store, task_id='ticket-default', now=NIGHT,
                               _preclaim=False)
            self.assertFalse(out['claimed'], out)
            self.assertEqual(out['reason'], 'mode_excluded', out)
            self.assertIn('用户要求禁用', ' '.join(out['reasons']))
            other = dp.claim_due(self.store, task_id='ticket-builtin', now=NIGHT,
                                 _preclaim=False)
            self.assertFalse(other['claimed'], other)
            self.assertEqual(other['reason'], 'luna_disabled', other)

    def test_formal_cn_entry_disabled_gate_precedes_cn_config_read(self):
        # S2 生产缺口 2：无 --dispatch-claim 的正式 CN 入口请求精确目标时，必须在读取
        # 私有 CN 配置/模型映射之前拒绝——用不存在的 CN 配置路径反证顺序：若先读配置
        # 会打印 cn_entry_rejected，本用例要求打印用户禁用拒绝（mode_excluded）。内置
        # 名 Qwen3.8-Max 同路径必须仍是配置错误（不误伤）。
        import io
        import json
        import qoder_direct as qd
        from contextlib import redirect_stdout
        prompt = self.base / 'prompt2.txt'
        prompt.write_text('offline refusal ordering', encoding='utf-8')
        missing_cfg = str(self.base / 'no-such-cn-config.json')
        def _run(model):
            output = self.base / ('output-order-' + model)
            argv = ['qoder_direct.py', '--runtime', 'qodercn', '--model', model,
                    '--workspace', self.ws('entry2'), '--prompt-file', str(prompt),
                    '--stage', 's', '--output-dir', str(output),
                    '--dispatch-store', self.store, '--cn-config', missing_cfg,
                    '--tools', 'Read']
            stdout = io.StringIO()
            with mock.patch.object(sys, 'argv', argv),                     mock.patch.object(qd.subprocess, 'Popen') as popen,                     redirect_stdout(stdout):
                code = qd.main()
            return code, json.loads(stdout.getvalue()), popen, output
        code, receipt, popen, output = _run('Qwen-3.8-Max')
        self.assertEqual(code, 2)
        self.assertTrue(receipt['cn_user_disabled_pool_rejected'], receipt)
        self.assertEqual(receipt['reason'], 'mode_excluded')
        self.assertEqual(receipt['pool_key'], 'qodercn:Qwen-3.8-Max')
        self.assertIn('用户要求禁用', ' '.join(receipt['reasons']))
        popen.assert_not_called()
        self.assertFalse(output.exists())
        code2, receipt2, _popen2, output2 = _run('Qwen3.8-Max')
        self.assertEqual(code2, 2)
        self.assertTrue(receipt2['cn_entry_rejected'], receipt2)
        self.assertFalse(output2.exists())

    def test_formal_cn_entry_refuses_without_popen(self):
        import io
        import json
        import qoder_direct as qd
        from contextlib import redirect_stdout
        prompt = self.base / 'prompt.txt'
        prompt.write_text('offline refusal', encoding='utf-8')
        for mode in dm.MODES:
            output = self.base / ('output-' + mode)
            argv = ['qoder_direct.py', '--runtime', 'qodercn', '--model', 'Qwen-3.8-Max',
                    '--workspace', self.ws('entry'), '--prompt-file', str(prompt),
                    '--stage', 's', '--output-dir', str(output),
                    '--dispatch-store', self.store, '--tools', 'Read']
            stdout = io.StringIO()
            with dispatch_mode(mode), mock.patch.object(sys, 'argv', argv), \
                    mock.patch.object(qd, 'load_cn_config', return_value={
                        'cli': 'offline-cli', 'config_dir': str(self.base),
                        'model_ids': {'Qwen-3.8-Max': 'offline-model-id'}}), \
                    mock.patch.object(qd.subprocess, 'Popen') as popen, redirect_stdout(stdout):
                code = qd.main()
            self.assertEqual(code, 2)
            receipt = json.loads(stdout.getvalue())
            self.assertFalse(receipt['sent'])
            self.assertEqual(receipt['reason'], 'mode_excluded')
            self.assertIn('用户要求禁用', ' '.join(receipt['reasons']))
            popen.assert_not_called()
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
