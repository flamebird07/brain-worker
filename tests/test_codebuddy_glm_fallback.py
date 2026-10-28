"""BW-CODEBUDDY-GLM-FALLBACK-20261018 离线回归：CodeBuddy GLM-5.3-Flash 低层补位。

全部使用临时 SQLite / 合成的当轮 summary.json / 进程内 FakePopen，零真实网络、账号、
子进程或真实额度文件。覆盖：
- 当轮明确 429/额度证据复验（真实 envelope、CLI exit 0 但真实错误、取消码 4294967295、
  成功正文 429、普通/网络/auth/权限失败均按既有分类）；
- 回执脱敏：任意合成敏感标记不得经 `diagnostics.failure_types`、runtime/model/exit_code
  形状回显，只保留既有白名单分类与已知身份/数字；
- 层序：BW-LIMIT30-20261019-S3 起 GLM 与 DeepSeek 额度独立，无证据也参与层序；Max/
  Claude/ZCode 仍优先，GLM 在 Flash 前；授权集合缺 GLM 不扩权；GLM 失败后落 Flash；
- `_select_main_force` 缺省 eligible=None 也包含 GLM（自身满位时仍不得选中），显式
  eligible 才限制范围；
- 独立名额（BW-WORKER-WIDGET-20261018-S3 用户最新明确，覆盖旧共享 1 槽口径）：
  DeepSeek 与 GLM 各独立 1 槽、互不占位互斥；同模型第 2 个在途仍拒绝；并发同模型只有
  一个赢家、两模型可同时各赢一个；finish 只释放自己的槽；物理总 15（BW-ZCODE-RESTORE-
  1-20261022 恢复 ZCode 1 槽；BW-OPENCODE-20261019 起含两 OpenCode 免费模型各 1 槽）
  不双计、status 按各自独立容量口径；
- planner 与正式 reserve/consume 裁决一致（均不要求 DeepSeek 限额证据）；正式 GLM 无
  proof 仍可进入配置读取并 Popen；带 proof 的 CLI argv/model/pool/evidence 正确，不硬记
  DeepSeek；
- 其它旧 CodeBuddy 模型与 WorkBuddy 仍退休。
"""
import io
import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / 'scripts'
for p in (str(SCRIPTS), str(REPO / 'tests')):
    if p not in sys.path:
        sys.path.insert(0, p)

import codebuddy_direct as cb  # noqa: E402
import codebuddy_fallback as cbf  # noqa: E402
import codebuddy_live as cl  # noqa: E402
import dispatch_pool as dp  # noqa: E402
import execution_control as ec  # noqa: E402
import prompt_contract as pc  # noqa: E402
import stage_selection as ss  # noqa: E402
import stub_codebuddy_live as stub  # noqa: E402

CB = 'codebuddy:deepseek-v4.1-flash'
GLM = 'codebuddy:glm-5.3-flash'
CB_MODEL = 'deepseek-v4.1-flash'
GLM_MODEL = 'glm-5.3-flash'
QMAX = 'qoder:Qwen3.8-Max'
CN_MAX = 'qodercn:Qwen3.8-Max'
CN_CUSTOM_MAX = 'qodercn:Qwen-3.8-Max'
CLAUDE = 'claude:deepseek-flash'
ZCODE = 'zcode:GLM-5.3'
QFLASH = 'qoder:Qwen3.8-Flash'
CN_FLASH = 'qodercn:Qwen3.8-Flash'
MAXES = (QMAX, CN_MAX, CN_CUSTOM_MAX)
# BW-OPENCODE-20261019：两 OpenCode 免费模型 tier4 高于条件 GLM tier5；GLM 兜底测试必须
# 把它们也算进"更高优先层"并一并占满，否则 AUTO 会落到仍空的 OpenCode 而非 GLM。
# 这只是如实反映现有层序（不改 GLM 独立调度规则），非降低排序。
HIGHER_THAN_GLM = (CB, QMAX, CN_MAX, CN_CUSTOM_MAX, CLAUDE, ZCODE,
                   dp.OPENCODE_DEEPSEEK_POOL_KEY, dp.OPENCODE_GLM_POOL_KEY)
ALL_CANDIDATES = list(dp.MAIN_FORCE_KEYS) + list(dp.OVERFLOW_KEYS)
SHA = 'p' + '0' * 63
SECRET = 'SYNTHETIC_SECRET_SHOULD_NOT_BE_ECHOED'

T_NIGHT = datetime(2026, 10, 8, 15, 0, 0, tzinfo=timezone.utc)  # 北京 23:00


def _summary(*, runtime='codebuddy', model=CB_MODEL, protocol_success=False,
             terminal_state='error', failure_envelope_valid=True,
             failure_types=('quota_429',), errors=None, exit_code=0,
             primary_failure='explicit 429 upstream'):
    """构造当轮 summary.json 的**合成**形状；不读取任何真实额度文件。"""
    if errors is None:
        errors = [{'status': 429, 'message': '429 upstream rate limit'}]
    return {
        'runtime': runtime,
        'model': model,
        'protocol_success': protocol_success,
        'terminal_state': terminal_state,
        'failure_envelope_valid': failure_envelope_valid,
        'exit_code': exit_code,
        'diagnostics': {
            'failure_types': list(failure_types),
            'evidence': {
                'primary_failure': primary_failure,
                'errors': errors,
                'errors_info': [],
                'terminal_state': terminal_state,
            },
        },
    }


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='bw-cb-glm-')
        self.base = Path(self._tmp.name)
        self.store = str(self.base / 'dispatch.sqlite3')
        self.assertNotEqual(self.store, str(dp.default_store_path()))
        patches = [
            mock.patch.object(dp, '_zcode_availability_blocked', lambda *a, **k: None),
            mock.patch.object(dp, 'process_identity',
                              return_value={'state': 'alive', 'created': 'c1'}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        with closing(dp.connect(self.store)) as conn:
            dp._rotation_row(conn)
            conn.execute('PRAGMA journal_mode=DELETE')

    def _normalize(self):
        with closing(dp.connect(self.store)) as conn:
            conn.execute('PRAGMA journal_mode=DELETE')

    def ws(self, tid):
        d = self.base / 'ws' / str(tid)
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def seed(self, pk, n=1, state='reserved', task_prefix=None):
        runtime, model = pk.split(':', 1)
        with closing(dp.connect(self.store)) as conn:
            for i in range(n):
                tid = f'{task_prefix or "seed"}-{pk.replace(":", "-")}-{i}'
                conn.execute(
                    'INSERT INTO attempts(token, task_id, runtime, model, pool_key, '
                    'state, child_pid, reserved_at_utc, workspace) '
                    'VALUES(?,?,?,?,?,?,?,?,?)',
                    (f'seed-{tid}', tid, runtime, model, pk, state, None,
                     dp._iso(T_NIGHT), str(self.base / tid)))
            conn.execute('PRAGMA journal_mode=DELETE')

    def evidence(self, name='summary.json', **kw):
        path = self.base / name
        path.write_text(json.dumps(_summary(**kw), ensure_ascii=False),
                        encoding='utf-8')
        return str(path)


class TestQuotaEvidenceSanitized(_Base):
    def test_cli_exit_zero_real_error_envelope_passes(self):
        # CLI 常以 0 退出；是否明确限额只看错误结构，不要求 exit_code==429。
        out = cbf.verify_quota_evidence(self.evidence(exit_code=0))
        self.assertTrue(out['ok'], out)
        self.assertEqual(out['reason'], 'explicit_quota_429')
        self.assertEqual(out['runtime'], 'codebuddy')
        self.assertEqual(out['model'], CB_MODEL)
        self.assertEqual(out['exit_code'], 0)
        self.assertEqual(out['failure_types'], ['quota_429'])

    def test_arbitrary_failure_type_string_not_echoed(self):
        path = self.evidence(failure_types=['quota_429', SECRET])
        out = cbf.verify_quota_evidence(path)
        self.assertTrue(out['ok'], out)  # 白名单内的明确 429 仍有效
        self.assertNotIn(SECRET, json.dumps(out, ensure_ascii=False))
        self.assertEqual(out['failure_types'], ['quota_429'])

    def test_unknown_runtime_model_and_nested_exit_code_not_echoed(self):
        payload = _summary(runtime={'leak': SECRET}, model=['x', SECRET],
                           exit_code={'message': SECRET}, failure_types=[SECRET])
        path = self.base / 'bad-shape.json'
        path.write_text(json.dumps(payload), encoding='utf-8')
        out = cbf.verify_quota_evidence(str(path))
        self.assertFalse(out['ok'], out)
        self.assertIsNone(out['runtime'])
        self.assertIsNone(out['model'])
        self.assertIsNone(out['exit_code'])
        self.assertEqual(out['failure_types'], [])
        self.assertNotIn(SECRET, json.dumps(out, ensure_ascii=False))

    def test_raw_error_and_header_text_not_echoed(self):
        errors = [{'status': 429, 'message': SECRET,
                   'headers': {'Authorization': SECRET, 'Cookie': SECRET}}]
        out = cbf.verify_quota_evidence(self.evidence(errors=errors))
        self.assertTrue(out['ok'], out)
        self.assertNotIn(SECRET, json.dumps(out, ensure_ascii=False))
        self.assertFalse(out['raw_error_text_included'])

    def test_cancel_exit_code_is_not_quota_proof(self):
        out = cbf.verify_quota_evidence(
            self.evidence(errors=[], exit_code=4294967295))
        self.assertFalse(out['ok'], out)
        self.assertEqual(out['reason'], 'evidence_429_structure_missing')
        self.assertEqual(out['exit_code'], 4294967295)

    def test_success_body_429_never_passes(self):
        out = cbf.verify_quota_evidence(self.evidence(
            protocol_success=True, terminal_state='success',
            failure_envelope_valid=False, failure_types=[],
            errors=[{'message': 'report body mentions 429 quota'}]))
        self.assertFalse(out['ok'], out)
        self.assertEqual(out['reason'], 'evidence_protocol_success')
        self.assertNotIn('report body mentions', json.dumps(out, ensure_ascii=False))

    def test_ordinary_network_auth_permission_failures_never_pass(self):
        cases = {
            'ordinary': (['model_execution_failure'],
                         [{'status': 500, 'message': 'ordinary failure'}]),
            'network': (['model_execution_failure'],
                        [{'message': 'network connection reset'}]),
            'auth': (['authentication_failure'],
                     ['Custom model authentication failed. Please check API Key.']),
            'permission': (['permission_rule_denied'],
                           [{'message': 'permission denied by rule'}]),
        }
        for name, (types, errors) in cases.items():
            out = cbf.verify_quota_evidence(
                self.evidence(name=f'{name}.json', failure_types=types, errors=errors))
            self.assertFalse(out['ok'], f'{name}: {out}')
            self.assertNotEqual(out['reason'], 'explicit_quota_429')
            self.assertNotIn('permission denied by rule',
                             json.dumps(out, ensure_ascii=False))

    def test_missing_or_unreadable_evidence_fails_closed(self):
        self.assertFalse(cbf.verify_quota_evidence(None)['ok'])
        self.assertFalse(cbf.verify_quota_evidence('')['ok'])
        missing = cbf.verify_quota_evidence(str(self.base / 'no-such.json'))
        self.assertEqual(missing['reason'], 'evidence_not_found')
        self.assertFalse(missing['ok'])


class TestSelectMainForceDefault(_Base):
    def test_default_eligible_none_can_include_glm(self):
        # BW-LIMIT30-20261019-S3：GLM 与 DeepSeek 额度独立，缺省候选含 GLM。
        # 所有更高层容量为 0、GLM 自身仍有空位 → 缺省 eligible 落到 GLM（层=4）。
        def _cap(pk):
            return 1 if pk == GLM else 0

        with mock.patch.object(dp, 'capacity_for', side_effect=_cap):
            with closing(dp.connect(self.store)) as conn:
                target, reason = dp._select_main_force(conn, GLM, T_NIGHT)
        self.assertEqual(target, GLM)
        self.assertEqual(reason, 'main_force_selected')

    def test_default_eligible_none_skips_full_glm(self):
        # 安全边界不退化：GLM 自己满位时仍不得被缺省选中。
        def _cap(pk):
            return 1 if pk == GLM else 0

        self.seed(GLM, 1)
        with mock.patch.object(dp, 'capacity_for', side_effect=_cap):
            with closing(dp.connect(self.store)) as conn:
                target, reason = dp._select_main_force(conn, GLM, T_NIGHT)
        self.assertIsNone(target)
        self.assertEqual(reason, 'main_force_full')

    def test_explicit_eligible_containing_glm_sorts_it_last(self):
        self.seed(ZCODE, 2)
        self.seed(QMAX, 1)
        self.seed(CN_MAX, 1)
        self.seed(CN_CUSTOM_MAX, 2)
        self.seed(CLAUDE, 2)
        with closing(dp.connect(self.store)) as conn:
            target, reason = dp._select_main_force(
                conn, GLM, T_NIGHT, eligible={GLM})
        self.assertEqual(target, GLM)
        self.assertEqual(reason, 'main_force_selected')

    def test_explicit_eligible_without_glm_never_returns_it(self):
        with closing(dp.connect(self.store)) as conn:
            target, _ = dp._select_main_force(
                conn, GLM, T_NIGHT, eligible={CB})
        self.assertEqual(target, CB)


class TestPlannerGlmCondition(_Base):
    def auth(self, allowed=None, **kw):
        a = {'selection_mode': 'auto', 'quote': '用户真实原话',
             'authorization_scope': {'task_id': 'T', 'stage': 's2'},
             'allowed_candidates': list(allowed if allowed is not None
                                        else ALL_CANDIDATES)}
        a.update(kw)
        return a

    def plan(self, auth, **kw):
        evidence = kw.pop('codebuddy_quota_evidence', None)
        if evidence is not None:
            auth = dict(auth, codebuddy_quota_evidence=evidence)
        return ss.plan_stage_selection(
            auth, store_path=self.store, task_id='T', stage='s2',
            workspace=self.ws('T'), now=T_NIGHT, **kw)

    def test_max_still_before_glm_when_evidence_valid(self):
        receipt = self.plan(self.auth(),
                            round_failed_candidates=[CB],
                            codebuddy_quota_evidence=self.evidence())
        self.assertTrue(receipt['codebuddy_glm_allowed'], receipt)
        self.assertIn(receipt['selected']['pool_key'], MAXES, receipt)
        self.assertNotEqual(receipt['selected']['pool_key'], GLM)

    def test_glm_after_zcode_and_before_flash(self):
        receipt = self.plan(self.auth(),
                            round_failed_candidates=list(HIGHER_THAN_GLM),
                            codebuddy_quota_evidence=self.evidence())
        self.assertTrue(receipt['codebuddy_glm_allowed'], receipt)
        self.assertEqual(receipt['selected']['pool_key'], GLM, receipt)
        self.assertEqual(receipt['selected_reason'], 'main_force_selected')
        for flash in (QFLASH, CN_FLASH):
            self.assertEqual(receipt['candidate_filters'][flash], 'eligible')

    def test_authorization_without_glm_not_expanded(self):
        allowed = [k for k in ALL_CANDIDATES if k != GLM]
        receipt = self.plan(self.auth(allowed),
                            round_failed_candidates=list(HIGHER_THAN_GLM),
                            codebuddy_quota_evidence=self.evidence())
        self.assertNotIn(GLM, receipt['allowed_candidates'])
        self.assertFalse(receipt['codebuddy_glm_allowed'], receipt)
        self.assertFalse(receipt['codebuddy_glm_condition']['authorized'])
        self.assertNotEqual(receipt['selected']['pool_key'], GLM)
        self.assertEqual(receipt['selected_reason'], 'flash_fallback_selected')

    def test_new_round_without_evidence_still_enables_glm(self):
        # BW-LIMIT30-20261019-S3：撤销“当轮 DeepSeek 真实限额证据”前置门。
        receipt = self.plan(self.auth(),
                            round_failed_candidates=list(HIGHER_THAN_GLM))
        self.assertTrue(receipt['codebuddy_glm_allowed'], receipt)
        self.assertEqual(receipt['candidate_filters'][GLM], 'eligible')
        self.assertEqual(receipt['selected']['pool_key'], GLM, receipt)
        self.assertEqual(receipt['selected_reason'], 'main_force_selected')
        self.assertFalse(receipt['codebuddy_glm_condition']['quota_evidence_required'])
        self.assertTrue(receipt['codebuddy_glm_condition']['independent_from_deepseek'])

    def test_ordinary_failed_evidence_is_legacy_diagnostic_only(self):
        # 普通失败证据只作兼容诊断，既不放行也不阻断（GLM 本就独立）。
        receipt = self.plan(
            self.auth(), round_failed_candidates=list(HIGHER_THAN_GLM),
            codebuddy_quota_evidence=self.evidence(
                name='ordinary.json', failure_types=['model_execution_failure'],
                errors=[{'status': 500, 'message': 'ordinary failure'}]))
        self.assertTrue(receipt['codebuddy_glm_allowed'], receipt)
        self.assertEqual(receipt['candidate_filters'][GLM], 'eligible')
        self.assertEqual(receipt['selected']['pool_key'], GLM, receipt)
        self.assertFalse(receipt['codebuddy_quota_evidence']['required'])

    def test_glm_failure_falls_to_flash(self):
        receipt = self.plan(
            self.auth(),
            round_failed_candidates=list(HIGHER_THAN_GLM) + [GLM],
            codebuddy_quota_evidence=self.evidence())
        self.assertTrue(receipt['codebuddy_glm_allowed'], receipt)
        self.assertEqual(receipt['candidate_filters'][GLM],
                         'already_tried_this_round')
        self.assertIn(receipt['selected']['pool_key'], (QFLASH, CN_FLASH))
        self.assertEqual(receipt['selected_reason'], 'flash_fallback_selected')

    def test_old_db_limit_mark_without_timestamp_does_not_block_glm(self):
        # 旧库只有裸 limited 标记、无可用时间参照 → 1800 秒窗口不成立，不得误挡。
        with closing(dp.connect(self.store)) as conn:
            conn.execute('INSERT INTO main_force_limits(pool_key, limited) '
                         'VALUES(?,1)', (GLM,))
            conn.execute('PRAGMA journal_mode=DELETE')
        receipt = self.plan(self.auth(),
                            round_failed_candidates=list(HIGHER_THAN_GLM))
        self.assertTrue(receipt['codebuddy_glm_allowed'], receipt)
        self.assertEqual(receipt['candidate_filters'][GLM], 'eligible')
        self.assertEqual(receipt['selected']['pool_key'], GLM, receipt)


class TestFormalGateConsistency(_Base):
    def test_planner_selection_matches_formal_claim_and_consume(self):
        # BW-LIMIT30-20261019-S3：planner 与正式入口都不再要求 DeepSeek 限额证据，
        # 两处裁决一致；GLM 仍只占自己的独立 1 槽，绝不占用 DeepSeek 名额。
        receipt = ss.plan_stage_selection(
            {'selection_mode': 'auto', 'quote': '用户真实原话',
             'authorization_scope': {'task_id': 'T', 'stage': 's'},
             'allowed_candidates': list(ALL_CANDIDATES),
             'round_failed_candidates': list(HIGHER_THAN_GLM)},
            store_path=self.store, task_id='T', stage='s',
            workspace=self.ws('T'), now=T_NIGHT)
        self.assertEqual(receipt['selected']['pool_key'], GLM, receipt)
        self.assertTrue(receipt['codebuddy_glm_allowed'], receipt)
        # 正式 reserve：无证据、无条件布尔 → 与 planner 一致允许。
        claimed = dp.reserve(self.store, task_id='f1', runtime='codebuddy',
                             model=GLM_MODEL, workspace=self.ws('f1'),
                             prompt_sha256=SHA, stage='s', now=T_NIGHT,
                             executor='codebuddy', codebuddy_glm_allowed=False,
                             _preclaim=False)
        self.assertTrue(claimed.get('allowed'), claimed)
        consumed = dp.consume_for_entry(
            self.store, task_id='f1', runtime='codebuddy', model=GLM_MODEL,
            workspace=self.ws('f1'), prompt_sha256=SHA, stage='s',
            claim_token=claimed['token'], now=T_NIGHT, executor='codebuddy',
            codebuddy_glm_allowed=False)
        self.assertTrue(consumed.get('allowed'), consumed)
        with closing(dp.connect(self.store)) as conn:
            self.assertEqual(dp._count_active(conn, GLM), 1)
            self.assertEqual(dp._count_active(conn, CB), 0)
        # 终态释放后，同槽可再次成功消费（GLM 只占自己的独立 1 槽）。
        # 终态释放：该 attempt 已被 consume_for_entry 消费为 running 并记录了真实 owner
        # （wrapper_pid=os.getpid()、wrapper_created=mock 身份 'c1'）。BW-LIMIT30-20261019-S4
        # 缺口 3：无 child 的 running attempt 位于 Popen-bind 窗口，必须由**合法 owner 亲证**
        # （wrapper 身份一致）才可 cancelled 释放——传 wrapper_pid=None 会正确落到
        # owner_unverified（生产 release 语义不放宽）。这里注入与记录一致的 owner 身份，
        # 绑定真实终态释放，而不是放松生产门。
        rel = dp.finish(self.store, claimed['token'], terminal='cancelled',
                        now=T_NIGHT, wrapper_pid=os.getpid(), wrapper_created='c1')
        self.assertTrue(rel.get('released') or rel.get('settled'), rel)
        claimed2 = dp.reserve(self.store, task_id='f3', runtime='codebuddy',
                              model=GLM_MODEL, workspace=self.ws('f3'),
                              prompt_sha256=SHA, stage='s', now=T_NIGHT,
                              executor='codebuddy', codebuddy_glm_allowed=True,
                              _preclaim=False)
        self.assertTrue(claimed2.get('allowed'), claimed2)
        consumed2 = dp.consume_for_entry(
            self.store, task_id='f3', runtime='codebuddy', model=GLM_MODEL,
            workspace=self.ws('f3'), prompt_sha256=SHA, stage='s',
            claim_token=claimed2['token'], now=T_NIGHT, executor='codebuddy',
            codebuddy_glm_allowed=True)
        self.assertTrue(consumed2.get('allowed'), consumed2)
        with closing(dp.connect(self.store)) as conn:
            self.assertEqual(dp._count_active(conn, GLM), 1)
            self.assertEqual(dp._count_active(conn, CB), 0)


class TestIndependentModelSlots(_Base):
    """BW-WORKER-WIDGET-20261018-S3：两 CodeBuddy 模型各独立 1 槽、互不占位互斥。"""

    def setUp_store(self):
        """清空 attempts，保持每个状态用例独立。"""
        with closing(dp.connect(self.store)) as conn:
            conn.execute('DELETE FROM attempts')
            conn.execute('PRAGMA journal_mode=DELETE')

    def test_own_active_blocks_only_itself(self):
        for state in ('reserved', 'running', 'unknown'):
            for occupancy in (CB, GLM):
                self.setUp_store()
                self.seed(occupancy, 1, state=state)
                with closing(dp.connect(self.store)) as conn:
                    other = GLM if occupancy == CB else CB
                    self.assertFalse(dp._has_own_capacity(conn, occupancy),
                                     f'{state}/{occupancy}')
                    self.assertTrue(dp._has_own_capacity(conn, other),
                                    f'{state}/{occupancy} must not block {other}')
                    self.assertEqual(dp._count_active(conn, occupancy), 1)
                    self.assertEqual(dp._count_active(conn, other), 0)

    def test_cross_model_active_does_not_block_reserve(self):
        # GLM 在途不再挡 DeepSeek 自身名额；同模型第 2 个仍拒绝。
        claimed = dp.reserve(self.store, task_id='g', runtime='codebuddy',
                             model=GLM_MODEL, workspace=self.ws('g'),
                             prompt_sha256=SHA, stage='s', now=T_NIGHT,
                             executor='codebuddy', codebuddy_glm_allowed=True,
                             _preclaim=False)
        self.assertTrue(claimed.get('allowed'), claimed)
        cb_ok = dp.reserve(self.store, task_id='d', runtime='codebuddy',
                           model=CB_MODEL, workspace=self.ws('d'),
                           prompt_sha256=SHA, stage='s', now=T_NIGHT,
                           executor='codebuddy', _preclaim=False)
        self.assertTrue(cb_ok.get('allowed'), cb_ok)
        glm_again = dp.reserve(self.store, task_id='g2', runtime='codebuddy',
                               model=GLM_MODEL, workspace=self.ws('g2'),
                               prompt_sha256=SHA, stage='s', now=T_NIGHT,
                               executor='codebuddy', codebuddy_glm_allowed=True,
                               _preclaim=False)
        self.assertFalse(glm_again.get('allowed'), glm_again)
        self.assertEqual(glm_again['reason'], 'capacity_full', glm_again)

    def test_finish_releases_only_own_slot(self):
        glm = dp.reserve(self.store, task_id='g', runtime='codebuddy',
                         model=GLM_MODEL, workspace=self.ws('g'),
                         prompt_sha256=SHA, stage='s', now=T_NIGHT,
                         executor='codebuddy', codebuddy_glm_allowed=True,
                         _preclaim=False)
        self.assertTrue(glm.get('allowed'), glm)
        cb = dp.reserve(self.store, task_id='d', runtime='codebuddy',
                        model=CB_MODEL, workspace=self.ws('d'),
                        prompt_sha256=SHA, stage='s', now=T_NIGHT,
                        executor='codebuddy', _preclaim=False)
        self.assertTrue(cb.get('allowed'), cb)
        rel = dp.finish(self.store, glm['token'], terminal='cancelled',
                        now=T_NIGHT, wrapper_pid=None)
        self.assertTrue(rel.get('released') or rel.get('settled'), rel)
        with closing(dp.connect(self.store)) as conn:
            # GLM 结束只释放自己：DeepSeek 在途不受影响。
            self.assertEqual(dp._count_active(conn, GLM), 0)
            self.assertEqual(dp._count_active(conn, CB), 1)
        glm_again = dp.reserve(self.store, task_id='g3', runtime='codebuddy',
                               model=GLM_MODEL, workspace=self.ws('g3'),
                               prompt_sha256=SHA, stage='s', now=T_NIGHT,
                               executor='codebuddy', codebuddy_glm_allowed=True,
                               _preclaim=False)
        self.assertTrue(glm_again.get('allowed'), glm_again)

    def test_concurrent_same_model_has_exactly_one_winner(self):
        def _claim(tid):
            return dp.reserve(self.store, task_id=tid, runtime='codebuddy',
                              model=GLM_MODEL, workspace=self.ws(tid),
                              prompt_sha256=SHA, stage='s', now=T_NIGHT,
                              executor='codebuddy', codebuddy_glm_allowed=True,
                              _preclaim=False)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(_claim, ('race-a', 'race-b')))
        allowed = [r for r in results if r.get('allowed')]
        denied = [r for r in results if not r.get('allowed')]
        self.assertEqual(len(allowed), 1, results)
        self.assertEqual(len(denied), 1, results)
        self.assertEqual(denied[0]['reason'], 'capacity_full', denied[0])
        with closing(dp.connect(self.store)) as conn:
            self.assertEqual(dp._count_active(conn, GLM), 1)

    def test_concurrent_two_models_can_both_claim(self):
        def _claim(args):
            tid, model = args
            return dp.reserve(self.store, task_id=tid, runtime='codebuddy',
                              model=model, workspace=self.ws(tid),
                              prompt_sha256=SHA, stage='s', now=T_NIGHT,
                              executor='codebuddy', codebuddy_glm_allowed=True,
                              _preclaim=False)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(_claim, (('x-ds', CB_MODEL),
                                             ('x-glm', GLM_MODEL))))
        self.assertTrue(all(r.get('allowed') for r in results), results)
        with closing(dp.connect(self.store)) as conn:
            self.assertEqual(dp._count_active(conn, CB), 1)
            self.assertEqual(dp._count_active(conn, GLM), 1)

    def test_total_physical_capacity_16_and_status_independent_counts(self):
        # BW-STRONG-SONUS-20261024：物理总 16（原 15 + strong 专属 Sonus 1 槽）；
        # 两 OpenCode 免费模型各独立 1 槽、独立计数。弱档不能新派 Sonus，这里按真实
        # 形态 seed 一条 strong 遗留在途以满足 16/16。
        self.assertEqual(dp.DOMESTIC_TOTAL_CAPACITY, 16)
        self.assertEqual(dp.CAPACITY[GLM], 1)
        self.assertEqual(dp.MAIN_FORCE_CAPACITY[GLM], 1)
        self.seed(ZCODE, 1)
        self.seed(CB, 1)
        self.seed(GLM, 1)
        self.seed(QMAX, 1)
        self.seed(CN_MAX, 1)
        self.seed(CN_CUSTOM_MAX, 2)
        self.seed(CLAUDE, 2)
        self.seed(dp.OPENCODE_DEEPSEEK_POOL_KEY, 1)
        self.seed(dp.OPENCODE_GLM_POOL_KEY, 1)
        self.seed(QFLASH, 2)
        self.seed(CN_FLASH, 2)
        self.seed(dp.SONUS_POOL_KEY, 1)
        st = dp.status(self.store, now=T_NIGHT)
        self.assertEqual(st['domestic']['capacity'], 16)
        self.assertEqual(st['domestic']['active'], 16)
        self.assertEqual(st['pools'][CB]['active'], 1)
        self.assertEqual(st['pools'][CB]['free'], 0)
        self.assertEqual(st['pools'][GLM]['active'], 1)
        self.assertEqual(st['pools'][GLM]['free'], 0)
        self.assertNotIn('shared_active', st['pools'][GLM])
        self.assertNotIn('shared_group', st['pools'][GLM])
        self.assertNotIn('shared_capacity', st['pools'][CB])
        self.assertEqual(sum(p['active'] for p in st['pools'].values()), 16)
        for model in (GLM_MODEL, CB_MODEL):
            out = dp.reserve(self.store, task_id=f'full-{model}',
                             runtime='codebuddy',
                             model=model, workspace=self.ws(f'full-{model}'),
                             prompt_sha256=SHA, stage='s', now=T_NIGHT,
                             executor='codebuddy', codebuddy_glm_allowed=True,
                             _preclaim=False)
            self.assertFalse(out.get('allowed'), out)
            self.assertEqual(out['reason'], 'capacity_full', out)


def _valid_report(stage, workspace):
    body = [pc.REPORT_START,
            f'阶段编号与执行方式：{stage}；direct。',
            f'实际项目绝对路径：{workspace}',
            '汇报时间与执行环境：offline-synthetic']
    for header in pc.SECTION_HEADERS:
        body.extend(['', header, '- 合成小节内容'])
    body.extend(['', pc.CLOSING_LINE, pc.REPORT_END])
    return '\n'.join(body)


def _stream(report_text, model):
    init = {'type': 'system', 'subtype': 'init', 'session_id': 'S1',
            'model': model, 'tools': ['Read'], 'mcp_servers': [],
            'permissionMode': 'dontAsk'}
    lines = [json.dumps(init, ensure_ascii=False)]
    lines.append(json.dumps(
        {'type': 'assistant', 'session_id': 'S1',
         'message': {'model': model, 'usage': {'input_tokens': 1, 'output_tokens': 2},
                     'content': [{'type': 'text', 'text': report_text}]}},
        ensure_ascii=False))
    lines.append(json.dumps(
        {'type': 'result', 'subtype': 'success', 'is_error': False,
         'session_id': 'S1', 'usage': {'input_tokens': 1, 'output_tokens': 2},
         'modelUsage': {}, 'result': report_text, 'errors': []},
        ensure_ascii=False))
    return ('\n'.join(lines) + '\n').encode('utf-8')


class TestFormalLiveGlm(_Base):
    def setUp(self):
        super().setUp()
        self.work = self.base / 'live-ws'
        self.work.mkdir(parents=True, exist_ok=True)
        self.stage = 'BW-CB-GLM-LIVE'
        self.report = _valid_report(self.stage, str(self.work))
        self.cfg = {'node': 'node', 'cli': 'cli.js'}

    def _prompt(self):
        p = self.base / 'prompt.txt'
        p.write_text('do a bounded read-only probe', encoding='utf-8')
        return p

    def test_missing_proof_reaches_config_load_without_quota_refusal(self):
        # BW-LIMIT30-20261019-S3：缺证据不再在读取配置前拒绝；入口直接进入配置读取。
        # 配置不存在 → FileNotFoundError，且 stdout 没有任何 quota 拒绝 JSON、outdir 未创建、
        # 零占位（证明旧前置门已撤销，且仍不产生副作用）。
        argv = ['--workspace', str(self.work),
                '--prompt-file', str(self.base / 'missing-prompt.txt'),
                '--output-dir', str(self.base / 'never-created'),
                '--stage', self.stage, '--task-id', 't', '--model', GLM_MODEL,
                '--config', str(self.base / 'no-such-config.json')]
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(FileNotFoundError):
                cl.main(argv)
        self.assertEqual(buf.getvalue(), '')
        self.assertFalse((self.base / 'never-created').exists())
        with closing(dp.connect(self.store)) as conn:
            self.assertEqual(dp._count_active(conn, GLM), 0)
            self.assertEqual(dp._count_active(conn, CB), 0)

    def test_run_missing_proof_dispatches_glm(self):
        # 无 DeepSeek 证据 → 正式入口照常认领并 Popen；旧参数仅作兼容诊断。
        rec = {}
        stream = _stream(self.report, GLM_MODEL)
        factory = stub.make_popen_factory(stdout=stream, returncode=0, record=rec)
        out = cl.run(self.cfg, workspace=str(self.work),
                     prompt_file=self._prompt(), out_dir=self.base / 'run-nope',
                     stage=self.stage, task_id='r', model=GLM_MODEL,
                     tools_items=['Read'], prompt_sha256=cl._sha(
                         (self.base / 'prompt.txt').read_bytes()),
                     dispatch_store=self.store, popen_factory=factory,
                     child_prober=lambda pid: {'state': 'dead', 'created': None},
                     codebuddy_quota_evidence=None, now=T_NIGHT)
        self.assertTrue(out.get('dispatched'), out)
        self.assertTrue(out.get('passed'), out)
        self.assertEqual(out['pool_key'], GLM)
        self.assertTrue(out['codebuddy_glm_allowed'])
        self.assertIsNone(out['codebuddy_quota_evidence'])
        self.assertIn('argv', rec)  # 真实到达 Popen
        self.assertEqual(rec['argv'][rec['argv'].index('--model') + 1], GLM_MODEL)
        with closing(dp.connect(self.store)) as conn:
            self.assertEqual(dp._count_active(conn, CB), 0)

    def test_with_proof_uses_glm_model_argv_pool_and_evidence(self):
        evidence = self.evidence()
        rec = {}
        stream = _stream(self.report, GLM_MODEL)
        factory = stub.make_popen_factory(stdout=stream, returncode=0, record=rec)
        out = cl.run(self.cfg, workspace=str(self.work),
                     prompt_file=self._prompt(), out_dir=self.base / 'glm-out',
                     stage=self.stage, task_id='ok', model=GLM_MODEL,
                     tools_items=['Read'], prompt_sha256=cl._sha(
                         (self.base / 'prompt.txt').read_bytes()),
                     dispatch_store=self.store, popen_factory=factory,
                     child_prober=lambda pid: {'state': 'dead', 'created': None},
                     codebuddy_quota_evidence=evidence, now=T_NIGHT)
        self.assertTrue(out.get('dispatched'), out)
        self.assertTrue(out.get('passed'), out)
        self.assertEqual(out['pool_key'], GLM)
        self.assertTrue(out['codebuddy_glm_allowed'])
        self.assertTrue(out['codebuddy_quota_evidence']['ok'])
        self.assertEqual(out['codebuddy_quota_evidence']['source'],
                         'current_round_summary_json')
        argv = rec['argv']
        self.assertEqual(argv[argv.index('--model') + 1], GLM_MODEL)
        self.assertNotIn(CB_MODEL, argv)
        req = json.loads((self.base / 'glm-out' / 'request.json').read_text(
            encoding='utf-8'))
        self.assertEqual(req['model'], GLM_MODEL)
        self.assertEqual(req['pool_key'], GLM)
        self.assertEqual(req['executor_identity']['request_model'], GLM_MODEL)
        payload = (self.base / 'glm-out' / 'sent-payload-stdin.bin').read_text(
            encoding='utf-8')
        self.assertIn(f'runtime=codebuddy；请求模型={GLM_MODEL}', payload)
        self.assertNotIn(CB_MODEL, payload)
        with closing(dp.connect(self.store)) as conn:
            self.assertEqual(dp._count_active(conn, CB), 0)

    def test_old_codebuddy_and_workbuddy_remain_retired(self):
        def plan(runtime, model):
            return {'task_id': 'T', 'stage': 's', 'runtime': runtime, 'model': model,
                    'workspace': '/tmp/x', 'cwd': '/tmp/x', 'prompt_sha256': SHA,
                    'grants': {'edits': [], 'bash': [], 'read_dirs': []},
                    'max_concurrency': 1, 'active_tasks': []}

        def actual(runtime, model):
            return {'task_id': 'T', 'stage': 's', 'runtime': runtime, 'model': model,
                    'workspace': '/tmp/x', 'cwd': '/tmp/x', 'prompt_sha256': SHA,
                    'shell': False, 'argv': ['node', 'cli.js'],
                    'grants': {'edits': [], 'bash': [], 'read_dirs': [],
                               'tool_visibility': [], 'allowed_tools': [],
                               'disallowed_tools': [], 'visible_tools': []}}

        for runtime, model in (('codebuddy', 'glm-4'),
                               ('codebuddy', 'glm-5.3'),
                               ('workbuddy', 'anything')):
            pf = ec.preflight(plan(runtime, model), actual(runtime, model))
            self.assertTrue(pf.get('manual_relay_only'), (runtime, model, pf))
            self.assertFalse(pf['ok'])


if __name__ == '__main__':
    unittest.main()
