"""stage_selection 只读规划入口的离线回归（BW-STAGE-SELECTION-S1/S2）。

只用临时 SQLite（每用例独立 store，经既有 `dispatch_pool.connect` 建库与
`_rotation_row` 建 rotation 行）、mock 掉 ZCode availability 只读回核
（`dp._zcode_availability_blocked`），绝不真实模型 / 真实共享库 / 凭据 / 余额。
断言规划入口只读（读写前后文件哈希不变）、缺库/缺 schema/不可读快照不创建、冲突整项
拒绝、退出码 0/2。S2 补：真实旧 scope 到期重选、同阶段按指定 Flash、父授权交集约束、
跨 task/无验收/在途不重选、缺 parent、CN 不跨区、WAL 活 writer 可见、上下文缺项。
BW-TASK-PERMISSION-S1 补：派工规划前的任务权限需求（可选 permission_grants，复用
execution_control grants 字典结构与 CAPABILITY 能力表）过滤——精确 Edit/Bash/read_dirs
需求下不支持的授权内候选标 permission_incompatible（与 not_authorized/zcode_unavailable
可辨）、fixed 精确要求诚实拒绝、无需求维持旧兼容行为、临时降级/父授权重选应用当前需求、
畸形输入拒绝、CLI 最小新输入。BW-PERMISSION-SCOPE-S1 补：task_scope（普通文件/测试
范围清单）与硬合同 permission_grants 分清——只进回执不驱动过滤、hard grants 完整生效、
guards 不被绕过、畸形 scope 拒绝、permission_incompatibility_details 冲突明细。
"""
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'scripts'
for p in (str(SCRIPTS), str(REPO / 'tests')):
    if p not in sys.path:
        sys.path.insert(0, p)

import dispatch_pool as dp  # noqa: E402
import execution_control as ec  # noqa: E402
import stage_selection as ss  # noqa: E402
from dispatch_mode_support import dispatch_mode  # noqa: E402

T_NIGHT = datetime(2026, 10, 8, 15, 0, 0, tzinfo=timezone.utc)  # 北京 23:00（主力窗口内）
T_DAY = datetime(2026, 10, 8, 2, 0, 0, tzinfo=timezone.utc)     # 北京 10:00（工作日高峰）

ALL_MAINS = list(dp.MAIN_FORCE_KEYS)
ALL_FLASH = list(dp.OVERFLOW_KEYS)
# BW-CODEBUDDY-MAIN-S3 + BW-CB-REORDER-20261020：本文件大量用例验证的是不含 CodeBuddy 的
# 层序（三 Qoder Max > claude）。BW-ZCODE-RETIRE-20261020：ZCode 已全局退役、不再属于
# MAIN_FORCE_KEYS，本列表自动不含它。这些用例把授权候选明确收窄为”不含 CodeBuddy”的
# 集合，从而原样验证既有层序而不放宽任何断言；CodeBuddy（层=3，claude 之后、两 OpenCode
# 之前）由独立 tests/test_codebuddy_reactivation.py 验证（含自动/显式入口与容量），
# 新 AUTO 选择顺序另见本文件 ReorderTierTests。
LEGACY_MAINS = [k for k in ALL_MAINS if k not in (dp.CODEBUDDY_POOL_KEY, dp.CODEBUDDY_GLM_POOL_KEY)]
CN_MAINS = [k for k in ALL_MAINS if k.startswith('qodercn:')]
CN_FLASH = 'qodercn:Qwen3.8-Flash'


def _patch_zcode(available=True):
    return unittest.mock.patch.object(
        dp, '_zcode_availability_blocked',
        lambda *a, **k: None if available else {'blocked': True, 'reasons': ['mock']})


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='bw-stage-sel-')
        self.base = Path(self._tmp.name)
        self.store = str(self.base / 'dispatch.sqlite3')
        self.assertNotEqual(self.store, str(dp.default_store_path()))
        self.saved_env = os.environ.pop('BRAIN_WORKER_DISPATCH_STORE', None)
        self._zpatch = _patch_zcode(True)
        self._zpatch.start()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._zpatch.stop)

    def tearDown(self):
        if self.saved_env is not None:
            os.environ['BRAIN_WORKER_DISPATCH_STORE'] = self.saved_env

    def _ws(self, name):
        d = self.base / 'ws' / str(name)
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def _normalize(self):
        """把临时库从 WAL 归到 delete 模式，使只读 mode=ro 打开无需 -shm（测试确定性）。"""
        with closing(dp.connect(self.store)) as conn:
            conn.execute('PRAGMA journal_mode=DELETE')

    def build_store(self):
        with closing(dp.connect(self.store)) as conn:
            dp._rotation_row(conn)
            conn.execute('PRAGMA journal_mode=DELETE')
        return self.store

    def reserve(self, task, runtime, model, ws=None, executor='auto'):
        r = dp.reserve(self.store, task_id=task, runtime=runtime, model=model,
                       workspace=ws or self._ws(task), prompt_sha256='p',
                       executor=executor, now=T_NIGHT, _preclaim=False)
        self.assertTrue(r.get('reserved') or r.get('allowed'), r)
        self._normalize()
        return r['token']

    def set_limit(self, pk):
        with closing(dp.connect(self.store)) as conn:
            conn.execute('INSERT INTO main_force_limits(pool_key, limited) VALUES(?,1)',
                         (pk,))
            conn.execute('PRAGMA journal_mode=DELETE')

    def auth(self, mode, task, stage, allowed, **kw):
        a = {'selection_mode': mode, 'quote': kw.pop('quote', '用户真实原话'),
             'authorization_scope': kw.pop('scope', {'task_id': task, 'stage': stage}),
             'allowed_candidates': allowed}
        if mode in ('fixed', 'executor'):
            a['user_direct_selection'] = kw.pop('user_direct_selection', True)
        a.update(kw)
        return a

    def plan(self, auth, *, previous=None, task='T', stage='s2', ws=None, now=T_NIGHT,
             permission_grants=None, task_scope=None):
        return ss.plan_stage_selection(
            auth, previous=previous, store_path=self.store, task_id=task, stage=stage,
            workspace=ws or self._ws(task), now=now, permission_grants=permission_grants,
            task_scope=task_scope)

    def _digest(self):
        h = hashlib.sha256()
        for suffix in ('', '-wal', '-shm'):
            p = Path(self.store + suffix)
            if p.exists():
                h.update(p.read_bytes())
        return h.hexdigest()


class AuthorizationTests(_Base):
    def test_fixed_and_executor_require_explicit_user_choice(self):
        self.build_store()
        fixed = self.auth('fixed', 'T', 's2', [CN_FLASH], runtime='qodercn',
                          model='Qwen3.8-Flash')
        fixed.pop('user_direct_selection')
        r = self.plan(fixed)
        self.assertEqual(r['refused'], 'explicit_user_choice_required', r)
        executor = self.auth('executor', 'T', 's2', ALL_MAINS + ALL_FLASH,
                             executor='opencode', user_direct_selection=False)
        r = self.plan(executor)
        self.assertEqual(r['refused'], 'explicit_user_choice_required', r)

    def test_auto_does_not_need_explicit_user_choice(self):
        self.build_store()
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH))
        self.assertIsNone(r['refused'], r)
        self.assertFalse(r['user_direct_selection'])

    def test_receipt_explains_priority_eligible_excluded_and_next(self):
        self.build_store()
        allowed = ALL_MAINS + ALL_FLASH
        with closing(dp.connect(self.store)) as conn:
            conn.execute(
                'INSERT INTO main_force_limits(pool_key, limited, recorded_at_utc) '
                'VALUES(?,1,?)', (dp.CODEBUDDY_POOL_KEY, dp._iso(T_DAY)))
            conn.execute('PRAGMA journal_mode=DELETE')
        # BW-MANUAL-THREE-MODE-20261023：回执的 selected_priority / eligible / 排除原因
        # 必须跟随**当前档**。弱档白天：三个 Max 夜间窗与 claude 工作日高峰门都关，
        # ZCode（层=2）是最高可用层；CodeBuddy 因真实限额排除，下一候选是层=6 的 OpenCode。
        with dispatch_mode('weak'):
            r = self.plan(self.auth('auto', 'T', 's2', allowed), now=T_DAY)
            self.assertIsNone(r['refused'], r)
            self.assertEqual(r['dispatch_mode'], 'weak', r)
            self.assertEqual(r['selected_priority'], 2, r)
            self.assertEqual(r['selected']['pool_key'], dp.ZCODE_POOL_KEY, r)
            eligible = {x['pool_key']: x for x in r['eligible_candidates']}
            self.assertEqual(eligible[dp.ZCODE_POOL_KEY]['priority'], 2)
            self.assertEqual(eligible[dp.ZCODE_POOL_KEY]['capacity'], 1)
            self.assertEqual(eligible[dp.OPENCODE_DEEPSEEK_POOL_KEY]['priority'], 6)
            for pk in (dp.CLAUDE_POOL_KEY, dp.QMAX_POOL_KEY, dp.CN_MAX_POOL_KEY,
                       dp.CN_CUSTOM_MAX_POOL_KEY):
                self.assertNotIn(pk, eligible, pk)
            excluded = {x['pool_key']: x for x in r['excluded_candidates_with_reason']}
            self.assertEqual(excluded[dp.CODEBUDDY_POOL_KEY]['reason'],
                             'main_force_limited')
            for pk in (dp.CLAUDE_POOL_KEY, dp.QMAX_POOL_KEY, dp.CN_MAX_POOL_KEY,
                       dp.CN_CUSTOM_MAX_POOL_KEY):
                self.assertEqual(excluded[pk]['reason'],
                                 'mode_excluded' if pk == dp.CN_CUSTOM_MAX_POOL_KEY
                                 else 'window_closed', pk)
            # 选中层=2 之上只剩两个层=1 的 CN Max；claude(3)/国际 Max(4) 更低层不算
            # higher_priority。
            self.assertEqual([x['pool_key'] for x in r['higher_priority_exclusions']],
                             [dp.CN_CUSTOM_MAX_POOL_KEY, dp.CN_MAX_POOL_KEY])
            self.assertEqual(r['next_candidate']['priority'], 6)
            self.assertIn(r['next_candidate']['pool_key'], dp.OPENCODE_POOL_KEYS)
        # 强档白天：可派池全部无时段门，常态层序 Sonus(1) → ZCode(2) → claude(3) →
        # 三 Max(4)；回执层号与排除原因随档位切换，绝不沿用弱档表。CodeBuddy 不在强档
        # 硬集合内，即便带真实限额标记也先报 mode_excluded（档位排除先于限额原因，
        # 绝不冒充 main_force_limited）。BW-SONUS-REENABLE-20261029-R3：Sonus 恢复
        # 常态新派——strong 规划首选 Sonus(1)，下一候选 ZCode(2)。
        with dispatch_mode('strong'):
            r = self.plan(self.auth('auto', 'T', 's2', allowed), now=T_DAY)
            self.assertEqual(r['dispatch_mode'], 'strong', r)
            self.assertEqual(r['selected_priority'], 1, r)
            self.assertEqual(r['selected']['pool_key'], dp.SONUS_POOL_KEY, r)
            eligible = {x['pool_key']: x for x in r['eligible_candidates']}
            self.assertEqual(eligible[dp.SONUS_POOL_KEY]['priority'], 1)
            self.assertEqual(eligible[dp.ZCODE_POOL_KEY]['priority'], 2)
            self.assertEqual(eligible[dp.CLAUDE_POOL_KEY]['priority'], 3)
            self.assertEqual(eligible[dp.CLAUDE_POOL_KEY]['capacity'], 2)
            self.assertEqual(eligible[dp.QMAX_POOL_KEY]['priority'], 4)
            self.assertEqual(eligible[dp.CN_MAX_POOL_KEY]['priority'], 4)
            excluded = {x['pool_key']: x for x in r['excluded_candidates_with_reason']}
            self.assertEqual(excluded[dp.CODEBUDDY_POOL_KEY]['reason'],
                             'mode_excluded')
            self.assertEqual(excluded[dp.OPENCODE_DEEPSEEK_POOL_KEY]['reason'],
                             'mode_excluded')
            self.assertEqual(r['next_candidate']['priority'], 2)
            self.assertEqual(r['next_candidate']['pool_key'], dp.ZCODE_POOL_KEY)

    def test_expired_structured_limit_opens_new_round_and_restores_priority(self):
        self.build_store()
        failed_at = T_NIGHT
        now = failed_at + timedelta(seconds=dp.QUOTA_LIMIT_WINDOW_SECONDS)
        with closing(dp.connect(self.store)) as conn:
            conn.execute(
                'INSERT INTO main_force_limits(pool_key, limited, recorded_at_utc) '
                'VALUES(?,1,?)', (dp.CLAUDE_POOL_KEY, dp._iso(failed_at)))
            conn.execute('PRAGMA journal_mode=DELETE')
        # BW-ZCODE-RETIRE-20261020：原第二候选 zcode 已退役，改用同场景的 QMAX 之外的
        # 空授权集不改变本例语义——只保留 claude，验证到期后 claude 重新可选与新轮开启。
        # BW-ROUND-TRANSITION-FIX：轮转必须由**结构化限额事件**且与过期窗口同一事件触发，
        # 本例改用携带分类/时间/事件引用的结构化入参（failed_at_utc 与限额参照一致）。
        auth = self.auth('auto', 'T', 's2', [dp.CLAUDE_POOL_KEY],
                         round_failed_candidates=[
                             {'pool_key': dp.CLAUDE_POOL_KEY,
                              'failure_class': 'quota_429',
                              'failed_at_utc': dp._iso(failed_at),
                              'event_ref': 'tk-claude-429'}],
                         dispatch_round={'id': 7,
                                         'started_at_utc': dp._iso(failed_at)})
        r = self.plan(auth, now=now)
        self.assertIsNone(r['refused'], r)
        self.assertEqual(r['dispatch_round']['id'], 8)
        self.assertEqual(r['effective_round_failed_candidates'], [])
        self.assertEqual(r['round_transition']['reason'],
                         'structured_limit_window_expired_no_inflight')
        # 回执附加字段：触发轮转事件的标识/分类/时间（纯附加，供 UI 解释）。
        trig = r['round_transition']['trigger_events']
        self.assertEqual(len(trig), 1, trig)
        self.assertEqual(trig[0]['pool_key'], dp.CLAUDE_POOL_KEY)
        self.assertEqual(trig[0]['failure_class'], 'quota_429')
        self.assertEqual(trig[0]['failed_at_utc'], dp._iso(failed_at))
        self.assertEqual(trig[0]['event_ref'], 'tk-claude-429')
        self.assertEqual(trig[0]['limit_expires_at_utc'], dp._iso(now))
        self.assertEqual(r['selected']['pool_key'], dp.CLAUDE_POOL_KEY, r)

    def test_task_scope_generates_first_qoder_permission_plan(self):
        self.build_store()
        scope = {'edits': ['app/main.py', 'tests/test_main.py'],
                 'bash': ['python -m unittest tests.test_main'],
                 'read_dirs': []}
        r = self.plan(self.auth('auto', 'T', 's2', [dp.QMAX_POOL_KEY]),
                      now=T_NIGHT, task_scope=scope)
        self.assertIsNone(r['refused'], r)
        plan = r['qoder_permission_plan']
        self.assertEqual(plan['visible_tools'],
                         ['Read', 'Glob', 'Grep', 'Edit', 'Write', 'Bash'])
        self.assertIn('Edit(/app/main.py)', plan['allowed_tools'])
        self.assertIn('Bash(python -m unittest tests.test_main)',
                      plan['allowed_tools'])

    def test_recent_limits_and_day_windows_select_opencode_before_flash(self):
        self.build_store()
        with closing(dp.connect(self.store)) as conn:
            # BW-ZCODE-RETIRE-20261020：zcode 已退役、不再是可过滤候选，本例只保留
            # CodeBuddy 的真实限额标记验证（白天层序 CodeBuddy 受限 → OpenCode 先于 Flash）。
            for pk in (dp.CODEBUDDY_POOL_KEY,):
                conn.execute(
                    'INSERT INTO main_force_limits(pool_key, limited, recorded_at_utc) '
                    'VALUES(?,1,?)', (pk, dp._iso(T_DAY)))
            conn.execute('PRAGMA journal_mode=DELETE')
        # BW-QODER-INTL-MAX-ALLDAY-20261023：国际 Max 白天可派，先占满其唯一名额，
        # 层=2 的 ZCode 才是最高可用层（两个 CN Max 仍被夜间门挡下）。
        self.reserve('im', 'qoder', 'Qwen3.8-Max', executor='qoder')
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH), now=T_DAY)
        self.assertIsNone(r['refused'], r)
        self.assertEqual(r['candidate_filters'][dp.CODEBUDDY_POOL_KEY],
                         'main_force_limited')
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'eligible', r)
        self.assertEqual(r['selected']['pool_key'], dp.ZCODE_POOL_KEY, r)
        self.assertNotIn(r['selected']['pool_key'], dp.OVERFLOW_KEYS)

    def test_fixed_cn_flash_kept_even_with_free_main(self):
        self.build_store()
        auth = self.auth('fixed', 'T', 's2', [CN_FLASH], runtime='qodercn',
                         model='Qwen3.8-Flash')
        r = self.plan(auth)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected']['pool_key'], CN_FLASH)

    def test_executor_cn_never_crosses_region(self):
        self.build_store()
        auth = self.auth('executor', 'T', 's2', ALL_MAINS + ALL_FLASH, executor='qodercn')
        r = self.plan(auth)
        self.assertEqual(r['selected_reason'], 'executor_region_main', r)
        self.assertTrue(r['selected']['pool_key'].startswith('qodercn:'), r)
        # 别区/别执行器绝不显示为 eligible，如实 not_authorized（含恢复枚举的 ZCode）。
        self.assertEqual(r['candidate_filters'][dp.OPENCODE_DEEPSEEK_POOL_KEY],
                         'not_authorized')
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'not_authorized')
        self.assertEqual(r['candidate_filters']['qoder:Qwen3.8-Max'], 'not_authorized')

    def test_executor_falls_to_own_flash_never_cross(self):
        self.build_store()
        # 白天国际 Max 名额占满（BW-QODER-INTL-MAX-ALLDAY-20261023 起它全天可派）→
        # executor='qoder' 落到同区 Flash；绝不落到 CN。
        self.reserve('im', 'qoder', 'Qwen3.8-Max', executor='qoder')
        auth = self.auth('executor', 'T', 's2', ALL_MAINS + ALL_FLASH, executor='qoder')
        r = self.plan(auth, now=T_DAY)
        self.assertEqual(r['selected_reason'], 'executor_region_flash', r)
        self.assertEqual(r['selected']['pool_key'], 'qoder:Qwen3.8-Flash')
        self.assertEqual(r['candidate_filters'][CN_FLASH], 'not_authorized')

    def test_unknown_pool_matrix_rejects_luna_and_retired(self):
        self.build_store()
        for bad in ('luna:native', 'qodercn:DeepSeek-Flash', 'qoder:GLM-5.3'):
            r = self.plan(self.auth('auto', 'T', 's2', [bad]))
            self.assertEqual(r['refused'], 'unauthorized_candidate', bad)

    def test_no_quote_and_empty_allowed_rejected(self):
        self.build_store()
        r = self.plan(self.auth('auto', 'T', 's2', [dp.ZCODE_POOL_KEY], quote='   '))
        self.assertEqual(r['refused'], 'no_source_quote')
        r = self.plan(self.auth('auto', 'T', 's2', []))
        self.assertEqual(r['refused'], 'empty_allowed_candidates')

    def test_executor_missing_or_illegal_field_refused(self):
        self.build_store()
        r = self.plan(self.auth('executor', 'T', 's2', ALL_MAINS + ALL_FLASH))
        self.assertEqual(r['refused'], 'invalid_executor', r)
        r = self.plan(self.auth('executor', 'T', 's2', ALL_MAINS + ALL_FLASH, executor=123))
        self.assertEqual(r['refused'], 'invalid_executor', r)

    # ---- BW-OPENCODE-20261019-S3：executor='opencode' 只在本执行器两独立槽内选择 ----
    def test_executor_opencode_selects_own_pool_no_window_gate(self):
        self.build_store()
        # 工作日高峰（T_DAY）：三 Max/claude 时段门外，OpenCode 无时段门仍如实可选，
        # 且绝不跨执行器——别区候选一律 not_authorized。
        auth = self.auth('executor', 'T', 's2', ALL_MAINS + ALL_FLASH,
                         executor='opencode')
        r = self.plan(auth, now=T_DAY)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected_reason'], 'executor_region_main', r)
        self.assertIn(r['selected']['pool_key'], dp.OPENCODE_POOL_KEYS, r)
        self.assertEqual(r['candidate_filters']['qoder:Qwen3.8-Max'], 'not_authorized')
        # claude 与恢复枚举的 zcode 都如实不跨执行器（not_authorized）。
        self.assertEqual(r['candidate_filters'][dp.CLAUDE_POOL_KEY], 'not_authorized')
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'not_authorized')

    def test_executor_opencode_full_refuses_never_crosses(self):
        self.build_store()
        # 两 OpenCode 独立槽都真实占满后，executor='opencode' 规划如实拒绝，绝不借
        # 别的执行器/别的模型（包括同区 Flash）顶替。
        self.reserve('o1', 'opencode', dp.OPENCODE_DEEPSEEK_MODEL, executor='opencode')
        self.reserve('o2', 'opencode', dp.OPENCODE_GLM_MODEL, executor='opencode')
        auth = self.auth('executor', 'T2', 's2', ALL_MAINS + ALL_FLASH,
                         executor='opencode')
        r = self.plan(auth, task='T2')
        self.assertEqual(r['selected'], None, r)
        self.assertEqual(r['refused'], 'no_eligible_candidate', r)


class RoundTransitionIdentityTests(_Base):
    """BW-ROUND-TRANSITION-FIX 回归：旧过期结构化限额窗口记录绝不再冒充当前轮真实失败
    清空失败列表并触发 structured_limit_window_expired_no_inflight 轮转。只有“当前轮
    失败本身是结构化限额事件、且过期窗口参照时刻正是该同一事件”才允许滚轮；纯字符串
    旧形态与非限额分类（网络/连接重置/权限/协议）一律保留到本轮结束。覆盖组合：
    旧限额过期/限额未过期 × 纯字符串/结构化入参 × 无上一轮回执/携带回执。"""

    def _seed_limit(self, pk, recorded_at):
        with closing(dp.connect(self.store)) as conn:
            conn.execute(
                'INSERT INTO main_force_limits(pool_key, limited, recorded_at_utc) '
                'VALUES(?,1,?)', (pk, dp._iso(recorded_at)))
            conn.execute('PRAGMA journal_mode=DELETE')

    def _event(self, pk, failed_at, failure_class='network_failure', event_ref=None):
        return {'pool_key': pk, 'failure_class': failure_class,
                'failed_at_utc': dp._iso(failed_at),
                'event_ref': event_ref or f'tk-{pk.split(":")[0]}'}

    def test_old_expired_limit_never_clears_plain_string_without_receipt(self):
        """实测缺陷场景 1（stage-selection-r1-round2.json）：codebuddy 真实失败后按纯
        pool_key 字符串入参、全新调用未携带上一轮回执（round_started=None），同 pool
        存在 6 小时前的旧过期限额行——绝不清空失败列表、绝不轮转、绝不重选该 pool，
        下一候选必须是 claude:deepseek-flash。"""
        self.build_store()
        old_ref = T_NIGHT - timedelta(hours=6)  # 早已过期的历史限额窗口
        self._seed_limit(dp.CODEBUDDY_POOL_KEY, old_ref)
        allowed = [dp.CODEBUDDY_POOL_KEY, dp.CLAUDE_POOL_KEY]
        auth = self.auth('auto', 'T', 's2', allowed,
                         round_failed_candidates=[dp.CODEBUDDY_POOL_KEY])
        r = self.plan(auth, now=T_NIGHT)
        self.assertIsNone(r['refused'], r)
        self.assertIsNone(r['round_transition'], r)
        self.assertEqual(r['dispatch_round']['id'], 1, r)
        self.assertEqual(r['effective_round_failed_candidates'],
                         [dp.CODEBUDDY_POOL_KEY], r)
        self.assertEqual(r['candidate_filters'][dp.CODEBUDDY_POOL_KEY],
                         'already_tried_this_round', r)
        self.assertEqual(r['selected']['pool_key'], dp.CLAUDE_POOL_KEY, r)

    def test_old_expired_limit_never_clears_structured_network_failure_with_receipt(self):
        """实测缺陷场景 2（login-log-repair TLS 502）：当前轮失败是结构化**网络**事件、
        携带上一轮回执，旧限额行（早于本轮开始）过期——网络失败保留到本轮结束，绝不
        被限额轮转逻辑清除，下一候选必须是 claude:deepseek-flash；回执含当前轮失败
        事件（标识/分类/时间）。"""
        self.build_store()
        old_ref = T_NIGHT - timedelta(hours=6)
        self._seed_limit(dp.CODEBUDDY_POOL_KEY, old_ref)
        round_started = T_NIGHT - timedelta(minutes=10)
        failed_at = T_NIGHT - timedelta(minutes=5)
        allowed = [dp.CODEBUDDY_POOL_KEY, dp.CLAUDE_POOL_KEY]
        auth = self.auth('auto', 'T', 's2', allowed,
                         round_failed_candidates=[
                             self._event(dp.CODEBUDDY_POOL_KEY, failed_at)],
                         dispatch_round={'id': 3,
                                         'started_at_utc': dp._iso(round_started)})
        r = self.plan(auth, now=T_NIGHT)
        self.assertIsNone(r['refused'], r)
        self.assertIsNone(r['round_transition'], r)
        self.assertEqual(r['dispatch_round']['id'], 3, r)
        self.assertEqual(r['effective_round_failed_candidates'],
                         [dp.CODEBUDDY_POOL_KEY], r)
        self.assertEqual(r['candidate_filters'][dp.CODEBUDDY_POOL_KEY],
                         'already_tried_this_round', r)
        self.assertEqual(r['selected']['pool_key'], dp.CLAUDE_POOL_KEY, r)
        self.assertEqual(r['round_failed_events'][0]['failure_class'],
                         'network_failure', r)
        self.assertEqual(r['round_failed_events'][0]['failed_at_utc'],
                         dp._iso(failed_at), r)

    def test_active_limit_window_never_rotates_round(self):
        """限额**未过期**（窗口仍 active）：即便当前轮失败是结构化 quota_429 同一事件，
        也绝不触发轮转；轮号/失败列表原样，该 pool 本轮不可重选。"""
        self.build_store()
        ref = T_NIGHT - timedelta(seconds=60)  # 60 秒前，1800 秒窗口内仍 active
        self._seed_limit(dp.CODEBUDDY_POOL_KEY, ref)
        allowed = [dp.CODEBUDDY_POOL_KEY, dp.CLAUDE_POOL_KEY]
        auth = self.auth('auto', 'T', 's2', allowed,
                         round_failed_candidates=[
                             self._event(dp.CODEBUDDY_POOL_KEY, ref,
                                         failure_class='quota_429')],
                         dispatch_round={'id': 2, 'started_at_utc': dp._iso(ref)})
        r = self.plan(auth, now=T_NIGHT)
        self.assertIsNone(r['refused'], r)
        self.assertIsNone(r['round_transition'], r)
        self.assertEqual(r['dispatch_round']['id'], 2, r)
        self.assertEqual(r['effective_round_failed_candidates'],
                         [dp.CODEBUDDY_POOL_KEY], r)
        self.assertEqual(r['candidate_filters'][dp.CODEBUDDY_POOL_KEY],
                         'main_force_limited', r)
        self.assertEqual(r['selected']['pool_key'], dp.CLAUDE_POOL_KEY, r)

    def test_same_event_expired_limit_rotates_and_keeps_other_failures(self):
        """正例（携带回执）：当前轮失败本身是结构化 quota_429、过期窗口正是同一事件、
        参照时刻不早于本轮开始——允许轮转；只清该同一限额事件，同轮的结构化网络失败
        与纯字符串失败一律保留（绝不清空整表）。"""
        self.build_store()
        ref = T_NIGHT - timedelta(seconds=dp.QUOTA_LIMIT_WINDOW_SECONDS)  # 恰满 1800 秒
        self._seed_limit(dp.CODEBUDDY_POOL_KEY, ref)
        round_started = ref - timedelta(minutes=5)
        net_failed_at = ref - timedelta(minutes=1)
        allowed = [dp.CODEBUDDY_POOL_KEY, dp.QMAX_POOL_KEY, dp.CLAUDE_POOL_KEY]
        auth = self.auth('auto', 'T', 's2', allowed,
                         round_failed_candidates=[
                             self._event(dp.CODEBUDDY_POOL_KEY, ref,
                                         failure_class='quota_429'),
                             self._event(dp.QMAX_POOL_KEY, net_failed_at),
                             dp.CLAUDE_POOL_KEY],
                         dispatch_round={'id': 4,
                                         'started_at_utc': dp._iso(round_started)})
        r = self.plan(auth, now=T_NIGHT)
        self.assertIsNone(r['refused'], r)
        self.assertEqual(r['round_transition']['from_round'], 4, r)
        self.assertEqual(r['round_transition']['to_round'], 5, r)
        self.assertEqual(r['round_transition']['reason'],
                         'structured_limit_window_expired_no_inflight')
        self.assertEqual(sorted(r['effective_round_failed_candidates']),
                         sorted([dp.QMAX_POOL_KEY, dp.CLAUDE_POOL_KEY]), r)
        trig = r['round_transition']['trigger_events']
        self.assertEqual(len(trig), 1, trig)
        self.assertEqual(trig[0]['pool_key'], dp.CODEBUDDY_POOL_KEY)
        self.assertEqual(trig[0]['failure_class'], 'quota_429')
        self.assertEqual(trig[0]['failed_at_utc'], dp._iso(ref))
        self.assertEqual(trig[0]['limit_reference_utc'], dp._iso(ref))
        # 到期后 codebuddy 恢复原优先级层（层=3，BW-CB-REORDER-20261020；同轮 QMAX/claude
        # 仍失败保留被剔，真实尝试仍由正式入口复核），其余失败保留。
        self.assertEqual(r['selected']['pool_key'], dp.CODEBUDDY_POOL_KEY, r)
        self.assertEqual(r['candidate_filters'][dp.QMAX_POOL_KEY],
                         'already_tried_this_round', r)
        self.assertEqual(r['candidate_filters'][dp.CLAUDE_POOL_KEY],
                         'already_tried_this_round', r)

    def test_expired_limit_without_receipt_same_event_rotates(self):
        """无上一轮回执（round_started=None）+ 结构化 quota_429 同一事件已到期：允许
        轮转（1→2）；这与旧缺陷的区别是身份绑定成立，而非任何旧窗口都能清表。
        BW-CB-REORDER-20261020：授权集合收窄为仅 CodeBuddy（claude 层=2 现先于
        CodeBuddy 层=3），本例只验证轮转清空失败集合后 CodeBuddy 恢复可选。"""
        self.build_store()
        ref = T_NIGHT - timedelta(seconds=dp.QUOTA_LIMIT_WINDOW_SECONDS)
        self._seed_limit(dp.CODEBUDDY_POOL_KEY, ref)
        allowed = [dp.CODEBUDDY_POOL_KEY]
        auth = self.auth('auto', 'T', 's2', allowed,
                         round_failed_candidates=[
                             self._event(dp.CODEBUDDY_POOL_KEY, ref,
                                         failure_class='quota_429')])
        r = self.plan(auth, now=T_NIGHT)
        self.assertIsNone(r['refused'], r)
        self.assertEqual(r['dispatch_round']['id'], 2, r)
        self.assertEqual(r['round_transition']['reason'],
                         'structured_limit_window_expired_no_inflight', r)
        self.assertEqual(r['effective_round_failed_candidates'], [], r)
        self.assertEqual(r['selected']['pool_key'], dp.CODEBUDDY_POOL_KEY, r)

    def test_expired_limit_event_time_mismatch_never_rotates(self):
        """身份绑定：结构化 quota_429 事件时间与其 pool 上过期窗口的参照时刻**不是同一
        事件**（当前失败晚于旧窗口）——旧窗口绝不清除当前失败、绝不轮转。"""
        self.build_store()
        old_ref = T_NIGHT - timedelta(hours=6)
        self._seed_limit(dp.CODEBUDDY_POOL_KEY, old_ref)
        failed_at = T_NIGHT - timedelta(minutes=5)  # 当前轮真实失败，晚于旧窗口参照
        round_started = T_NIGHT - timedelta(minutes=10)
        allowed = [dp.CODEBUDDY_POOL_KEY, dp.CLAUDE_POOL_KEY]
        auth = self.auth('auto', 'T', 's2', allowed,
                         round_failed_candidates=[
                             self._event(dp.CODEBUDDY_POOL_KEY, failed_at,
                                         failure_class='quota_429')],
                         dispatch_round={'id': 9,
                                         'started_at_utc': dp._iso(round_started)})
        r = self.plan(auth, now=T_NIGHT)
        self.assertIsNone(r['refused'], r)
        self.assertIsNone(r['round_transition'], r)
        self.assertEqual(r['dispatch_round']['id'], 9, r)
        self.assertEqual(r['effective_round_failed_candidates'],
                         [dp.CODEBUDDY_POOL_KEY], r)
        self.assertEqual(r['candidate_filters'][dp.CODEBUDDY_POOL_KEY],
                         'already_tried_this_round', r)
        self.assertEqual(r['selected']['pool_key'], dp.CLAUDE_POOL_KEY, r)

    def test_limit_record_older_than_round_start_never_clears(self):
        """即便分类是结构化限额，但限额窗口参照时刻**早于当前轮开始时间**（旧轮遗留
        记录）：不得清除当前轮失败、不得轮转。"""
        self.build_store()
        old_ref = T_NIGHT - timedelta(hours=2)  # 过期，但早于本轮开始
        self._seed_limit(dp.CODEBUDDY_POOL_KEY, old_ref)
        round_started = T_NIGHT - timedelta(minutes=30)
        allowed = [dp.CODEBUDDY_POOL_KEY, dp.CLAUDE_POOL_KEY]
        auth = self.auth('auto', 'T', 's2', allowed,
                         round_failed_candidates=[
                             self._event(dp.CODEBUDDY_POOL_KEY, old_ref,
                                         failure_class='quota_429')],
                         dispatch_round={'id': 5,
                                         'started_at_utc': dp._iso(round_started)})
        r = self.plan(auth, now=T_NIGHT)
        self.assertIsNone(r['refused'], r)
        self.assertIsNone(r['round_transition'], r)
        self.assertEqual(r['dispatch_round']['id'], 5, r)
        self.assertEqual(r['effective_round_failed_candidates'],
                         [dp.CODEBUDDY_POOL_KEY], r)
        self.assertEqual(r['selected']['pool_key'], dp.CLAUDE_POOL_KEY, r)


class ReorderTierTests(_Base):
    """BW-CLAUDE-ALLDAY-QMAX-AFTER-20261023（用户明确指令）：现行 AUTO 选择顺序为
    两个 CN Max(1) > ZCode(2，恢复可派容量 1) > claude(3，全天) > 国际 Qoder Max(4，全天) >
    CodeBuddy deepseek(5) > 两 OpenCode 免费模型(6) > CodeBuddy GLM(7) > 两区 Flash(8)。"""

    def test_pool_priority_authoritative_numbers(self):
        # 整表权威层序：任何漂移直接失败；ZCode 以层=2 回表。
        self.assertEqual(dp.POOL_PRIORITY[dp.CODEBUDDY_POOL_KEY], 5)
        self.assertEqual(dp.POOL_PRIORITY, {
            dp.CN_CUSTOM_MAX_POOL_KEY: 1, dp.CN_MAX_POOL_KEY: 1,
            dp.ZCODE_POOL_KEY: 2,
            dp.CLAUDE_POOL_KEY: 3,
            dp.QMAX_POOL_KEY: 4,
            dp.CODEBUDDY_POOL_KEY: 5,
            dp.OPENCODE_DEEPSEEK_POOL_KEY: 6, dp.OPENCODE_GLM_POOL_KEY: 6,
            dp.CODEBUDDY_GLM_POOL_KEY: 7,
            dp.QFLASH_POOL_KEY: 8, dp.CN_FLASH_POOL_KEY: 8})

    def test_auto_night_max_before_codebuddy(self):
        # 夜间空库：两个 CN Max（层=1）先于 CodeBuddy（层=5），即便授权集合同时含两者。
        self.build_store()
        allowed = [dp.CODEBUDDY_POOL_KEY, dp.QMAX_POOL_KEY]
        r = self.plan(self.auth('auto', 'T', 's2', allowed), now=T_NIGHT)
        self.assertIsNone(r['refused'], r)
        # 授权集合内 QMAX 层=4 高于 CodeBuddy 层=5。
        self.assertEqual(r['selected']['pool_key'], dp.QMAX_POOL_KEY, r)
        self.assertEqual(r['selected_priority'], 4, r)

    def test_auto_order_claude_then_codebuddy_then_opencode(self):
        # 同一授权集合内按本轮失败逐层下移：claude(3) → CodeBuddy deepseek(5) →
        # OpenCode deepseek-free(6)，selected_priority 与 POOL_PRIORITY 完全一致
        # （授权集合不含 ZCode，ZCode 层=2 不参与本集合的选择）。
        self.build_store()
        allowed = [dp.CODEBUDDY_POOL_KEY, dp.CLAUDE_POOL_KEY,
                   dp.OPENCODE_DEEPSEEK_POOL_KEY]
        r = self.plan(self.auth('auto', 'T', 's2', allowed), now=T_NIGHT)
        self.assertIsNone(r['refused'], r)
        self.assertEqual(r['selected']['pool_key'], dp.CLAUDE_POOL_KEY, r)
        self.assertEqual(r['selected_priority'], 3, r)
        r2 = self.plan(self.auth('auto', 'T', 's2', allowed,
                                 round_failed_candidates=[dp.CLAUDE_POOL_KEY]),
                       now=T_NIGHT)
        self.assertEqual(r2['selected']['pool_key'], dp.CODEBUDDY_POOL_KEY, r2)
        self.assertEqual(r2['selected_priority'], 5, r2)
        r3 = self.plan(self.auth('auto', 'T', 's2', allowed,
                                 round_failed_candidates=[dp.CLAUDE_POOL_KEY,
                                                          dp.CODEBUDDY_POOL_KEY]),
                       now=T_NIGHT)
        self.assertEqual(r3['selected']['pool_key'],
                         dp.OPENCODE_DEEPSEEK_POOL_KEY, r3)
        self.assertEqual(r3['selected_priority'], 6, r3)


class WindowAndLimitTests(_Base):
    def test_day_peak_gates_close_max_and_claude_select_zcode_then_opencode(self):
        # BW-MANUAL-THREE-MODE-CLOSEOUT-20261023：weak 档白天（工作日高峰）三个 Max 的
        # 北京 22:00–08:00 夜间门与 claude 的工作日高峰门都关；ZCode（层=2）无门，是白天
        # 最高可用层。旧「国际 Max/claude 白天可派」断言属 BW-QODER-INTL-MAX-ALLDAY /
        # BW-CLAUDE-ALLDAY 旧口径，已按三档最终口径废止。
        self.build_store()
        r = self.plan(self.auth('auto', 'T', 's2', LEGACY_MAINS + ALL_FLASH), now=T_DAY)
        self.assertEqual(r['selected']['pool_key'], dp.ZCODE_POOL_KEY, r)
        self.assertEqual(r['candidate_filters']['qoder:Qwen3.8-Max'], 'window_closed')
        self.assertEqual(r['candidate_filters'][dp.CN_MAX_POOL_KEY], 'window_closed')
        self.assertEqual(r['candidate_filters'][dp.CN_CUSTOM_MAX_POOL_KEY],
                         'mode_excluded')
        self.assertEqual(r['candidate_filters'][dp.CLAUDE_POOL_KEY], 'window_closed')
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'eligible')
        # ZCode 占满后（层=2 无空位）白天按层序落到无门的 OpenCode（层=6）；被门关住的
        # claude（层=3）与国际 Max（层=4）绝不因层序更高被放进白天。
        self.reserve('z0', 'zcode', 'GLM-5.3', executor='zcode')
        r2 = self.plan(self.auth('auto', 'T2', 's2', LEGACY_MAINS + ALL_FLASH),
                       task='T2', now=T_DAY)
        self.assertEqual(r2['selected']['pool_key'], dp.OPENCODE_DEEPSEEK_POOL_KEY, r2)
        self.assertEqual(r2['candidate_filters'][dp.CLAUDE_POOL_KEY], 'window_closed', r2)

    def test_night_admits_gated_max_and_keeps_fairness(self):
        self.build_store()
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH), now=T_NIGHT)
        self.assertEqual(r['candidate_filters']['qoder:Qwen3.8-Max'], 'eligible')
        self.assertEqual(r['candidate_filters'][dp.CLAUDE_POOL_KEY], 'eligible')
        # BW-ZCODE-RESTORE-1-20261022：ZCode 恢复为逐候选枚举的现役候选（夜间亦然）。
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'eligible')

    def test_explicit_limit_no_longer_blocks_new_round(self):
        """BW-SIMPLE-PRIORITY-RETRY-S3 新用户政策：历史明确限额不再挡一个正常新轮次的
        真实尝试——QMAX 带限额标记在本轮仍 eligible，绝不因历史标记永久/到期后剔除；
        真实 429/失败才落诊断、成功/失败 CAS 结算不变。"""
        self.build_store()
        self.set_limit('qoder:Qwen3.8-Max')
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH), now=T_NIGHT)
        self.assertEqual(r['candidate_filters']['qoder:Qwen3.8-Max'], 'eligible')
        self.assertNotEqual(r['selected']['pool_key'], None)

    def test_capacity_full_filter_and_fallback(self):
        self.build_store()
        # BW-ZCODE-RETIRE-20261020：原用 ZCode×2 制造 capacity_full；退役后改用
        # claude（capacity=2，夜间窗口开）验证同一"满→如实 capacity_full、回退更高优先层"。
        self.reserve('a', 'claude', 'deepseek-flash', executor='claude')
        self.reserve('b', 'claude', 'deepseek-flash', executor='claude')
        r = self.plan(self.auth('auto', 'T', 's2', LEGACY_MAINS + ALL_FLASH), now=T_NIGHT)
        self.assertEqual(r['candidate_filters'][dp.CLAUDE_POOL_KEY], 'capacity_full')
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'eligible')
        # 满层回退：最高优先层（三 Max）有空位即被选中，绝不下探 Flash。
        self.assertIn(r['selected']['pool_key'],
                      [dp.QMAX_POOL_KEY, dp.CN_MAX_POOL_KEY,
                       dp.CN_CUSTOM_MAX_POOL_KEY], r)


class NightAutoCustomMaxRegression(_Base):
    """BW-CANDIDATE-SCOPE-S1 需求 3：独立夜间 AUTO 候选回归。绝不查询额度、不扩权、
    不把某 runtime:model 失败泛化成整个 executor，也不把 not_authorized 说成用户撤权。"""

    def test_night_auto_no_longer_drops_builtins_with_history_limit(self):
        """BW-SIMPLE-PRIORITY-RETRY-S3：历史明确限额不再挡新轮——内置 CN/国际 Max 即便带
        限额标记，本轮仍 eligible，绝不因历史标记把它们剔除；按既有 Max>Claude>Z 优先级
        选择最高优先层的空位池。allowed 全集与逐候选原因如实保留，不缩权、不假装撤权。"""
        self.build_store()
        self.set_limit('qoder:Qwen3.8-Max')
        self.set_limit('qodercn:Qwen3.8-Max')
        allowed = LEGACY_MAINS + ALL_FLASH
        r = self.plan(self.auth('auto', 'T', 's2', allowed), now=T_NIGHT)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(sorted(r['allowed_candidates']), sorted(allowed))  # 不缩权
        self.assertEqual(r['candidate_filters']['qoder:Qwen3.8-Max'], 'eligible')
        self.assertEqual(r['candidate_filters']['qodercn:Qwen3.8-Max'], 'eligible')
        self.assertEqual(r['candidate_filters'][dp.CN_CUSTOM_MAX_POOL_KEY], 'mode_excluded')
        self.assertNotIn('explicitly_limited', set(r['candidate_filters'].values()))
        # 最高优先层（三 Max）有空位即被选中，绝不下探 Claude/Flash/Z
        self.assertIn(r['selected']['pool_key'], ['qoder:Qwen3.8-Max',
                                                   'qodercn:Qwen3.8-Max',
                                                   dp.CN_CUSTOM_MAX_POOL_KEY], r)
        self.assertNotEqual(r['selected']['pool_key'], dp.CLAUDE_POOL_KEY)
        self.assertNotIn(r['selected']['pool_key'], ALL_FLASH)

    def test_auto_narrow_cn_scope_stays_cn_others_not_authorized(self):
        """合法用户把 AUTO 范围收窄到只 CN：选择仍在 CN 内，其余区/池如实 not_authorized
        ——这只表示不在本轮输入里，绝不伪造"用户撤权"，也绝不偷偷扩权到 qoder/claude。"""
        self.build_store()
        cn_scope = CN_MAINS + [CN_FLASH]
        r = self.plan(self.auth('auto', 'T', 's2', cn_scope), now=T_NIGHT)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(sorted(r['allowed_candidates']), sorted(cn_scope))
        self.assertTrue(r['selected']['pool_key'].startswith('qodercn:'), r)
        self.assertEqual(r['candidate_filters']['qoder:Qwen3.8-Max'], 'not_authorized')
        self.assertEqual(r['candidate_filters'][dp.CLAUDE_POOL_KEY], 'not_authorized')
        # 别区 OpenCode 与 ZCode 都如实 not_authorized（不在本轮输入里≠用户撤权）。
        self.assertEqual(r['candidate_filters'][dp.OPENCODE_DEEPSEEK_POOL_KEY],
                         'not_authorized')
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'not_authorized')

    def test_only_flash_input_marks_mains_not_authorized_without_fake_revocation(self):
        """错误输入只给 Flash 时，主力的 not_authorized 只反映未列入输入（不是撤权）；
        选择仍是该 Flash，合法窄范围不被偷偷扩到主力。"""
        self.build_store()
        r = self.plan(self.auth('auto', 'T', 's2', [CN_FLASH]), now=T_NIGHT)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected']['pool_key'], CN_FLASH, r)
        self.assertEqual(r['candidate_filters']['qoder:Qwen3.8-Max'], 'not_authorized')
        self.assertEqual(r['candidate_filters'][dp.CN_CUSTOM_MAX_POOL_KEY], 'not_authorized')


class PreviousAndConflictTests(_Base):
    def test_previous_in_flight_and_not_reviewed_block(self):
        self.build_store()
        auth = self.auth('auto', 'T', 's2', ALL_MAINS)
        for st in ('reserved', 'running', 'unknown', 'executing', 'awaiting_acceptance'):
            r = self.plan(auth, previous={'state': st, 'acceptance_reviewed': True})
            self.assertEqual(r['refused'], 'previous_in_flight', st)
        r = self.plan(auth, previous={'state': 'finished', 'acceptance_reviewed': False})
        self.assertEqual(r['refused'], 'previous_awaiting_acceptance')

    def test_task_in_flight_blocks_new_decision(self):
        self.build_store()
        # BW-ZCODE-RETIRE-20261020：在途守卫用例改用现役组合（qoder Max），语义不变。
        self.reserve('T', 'qoder', 'Qwen3.8-Max', executor='qoder')
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH))
        self.assertEqual(r['refused'], 'task_in_flight')

    def test_workspace_conflict_rejects_whole_item(self):
        self.build_store()
        shared = self._ws('shared')
        self.reserve('a', 'qoder', 'Qwen3.8-Max', ws=shared, executor='qoder')
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH), ws=shared)
        self.assertEqual(r['refused'], 'workspace_conflict')

    def test_scope_drift_rejected(self):
        self.build_store()
        # BW-ZCODE-RETIRE-20261020：授权候选换成现役池，scope 漂移仍优先如实拒绝。
        auth = self.auth('auto', 'T', 's2', [dp.QMAX_POOL_KEY],
                         scope={'task_id': 'OTHER', 'stage': 's2'})
        r = self.plan(auth)
        self.assertEqual(r['refused'], 'scope_conflict')


class TemporaryFallbackTests(_Base):
    def test_expired_temp_scope_reselects_via_parent(self):
        # 独立反例：temp 原 quote/scope 与 fallback_scope 都是 T/s1，previous=T/s1 已验收，
        # 调用 stage=s2 必须先识别旧 scope 到期（不是先用 s2 验证旧 s1 scope）。
        self.build_store()
        parent = self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH)
        auth = self.auth('temporary_fallback', 'T', 's1', ALL_MAINS + ALL_FLASH,
                         fallback_scope={'task_id': 'T', 'stage': 's1'},
                         runtime='qodercn', model='Qwen3.8-Flash',
                         parent_authorization=parent)
        prev = {'task_id': 'T', 'state': 'finished', 'acceptance_reviewed': True,
                'stage': 's1', 'runtime': 'qodercn', 'model': 'Qwen3.8-Flash'}
        r = self.plan(auth, previous=prev, stage='s2')
        self.assertEqual(r['refused'], None, r)
        self.assertTrue(r['reselection_due'], r)
        self.assertEqual(r['fallback_scope'], {'task_id': 'T', 'stage': 's1'})  # 旧值不改写
        self.assertEqual(r['selection_mode'], 'auto')                # 有效父授权
        self.assertEqual(r['source_selection_mode'], 'temporary_fallback')
        self.assertEqual(r['source_authorization_scope'],
                         {'task_id': 'T', 'stage': 's1'})
        self.assertIn(r['selected']['pool_key'], ALL_MAINS, r)
        self.assertEqual(sorted(r['allowed_candidates']), sorted(ALL_MAINS + ALL_FLASH))

    def test_new_stage_without_parent_refused(self):
        self.build_store()
        auth = self.auth('temporary_fallback', 'T', 's1', [CN_FLASH],
                         fallback_scope={'task_id': 'T', 'stage': 's1'},
                         runtime='qodercn', model='Qwen3.8-Flash')
        prev = {'task_id': 'T', 'state': 'finished', 'acceptance_reviewed': True,
                'stage': 's1'}
        r = self.plan(auth, previous=prev, stage='s2')
        self.assertEqual(r['refused'], 'parent_authorization_required')

    def test_expired_temp_cross_task_previous_no_reselect(self):
        self.build_store()
        parent = self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH)
        auth = self.auth('temporary_fallback', 'T', 's1', ALL_MAINS + ALL_FLASH,
                         fallback_scope={'task_id': 'T', 'stage': 's1'},
                         runtime='qodercn', model='Qwen3.8-Flash',
                         parent_authorization=parent)
        prev = {'task_id': 'OTHER', 'state': 'finished', 'acceptance_reviewed': True,
                'stage': 's1'}
        r = self.plan(auth, previous=prev, stage='s2')
        self.assertEqual(r['refused'], 'fallback_scope_mismatch', r)

    def test_expired_temp_no_acceptance_and_in_flight_no_reselect(self):
        self.build_store()
        parent = self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH)
        auth = self.auth('temporary_fallback', 'T', 's1', ALL_MAINS + ALL_FLASH,
                         fallback_scope={'task_id': 'T', 'stage': 's1'},
                         runtime='qodercn', model='Qwen3.8-Flash',
                         parent_authorization=parent)
        prev = {'task_id': 'T', 'state': 'finished', 'acceptance_reviewed': False,
                'stage': 's1'}
        r = self.plan(auth, previous=prev, stage='s2')
        self.assertEqual(r['refused'], 'previous_awaiting_acceptance', r)
        prev = {'task_id': 'T', 'state': 'running', 'acceptance_reviewed': True,
                'stage': 's1'}
        r = self.plan(auth, previous=prev, stage='s2')
        self.assertEqual(r['refused'], 'previous_in_flight', r)

    def test_expired_temp_parent_candidates_intersect_base(self):
        self.build_store()
        # 父授权只允许国际 Max，base 只授权 CN Flash → 交集空 → 拒绝（不扩权）
        parent = self.auth('auto', 'T', 's2', ['qoder:Qwen3.8-Max'])
        auth = self.auth('temporary_fallback', 'T', 's1', [CN_FLASH],
                         fallback_scope={'task_id': 'T', 'stage': 's1'},
                         runtime='qodercn', model='Qwen3.8-Flash',
                         parent_authorization=parent)
        prev = {'task_id': 'T', 'state': 'finished', 'acceptance_reviewed': True,
                'stage': 's1'}
        r = self.plan(auth, previous=prev, stage='s2')
        self.assertEqual(r['refused'], 'parent_authorization_required', r)
        self.assertEqual(r['refused_detail'], 'empty intersection with base')

    def test_expired_temp_parent_intersection_drops_extra(self):
        self.build_store()
        parent = self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH)  # 允许全部
        auth = self.auth('temporary_fallback', 'T', 's1', [CN_FLASH],  # base 只授权 CN Flash
                         fallback_scope={'task_id': 'T', 'stage': 's1'},
                         runtime='qodercn', model='Qwen3.8-Flash',
                         parent_authorization=parent)
        prev = {'task_id': 'T', 'state': 'finished', 'acceptance_reviewed': True,
                'stage': 's1'}
        r = self.plan(auth, previous=prev, stage='s2')
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['allowed_candidates'], [CN_FLASH])   # 交集不扩权
        self.assertEqual(r['selected']['pool_key'], CN_FLASH, r)

    def test_same_stage_fallback_uses_designated_flash(self):
        # 同阶段：即使 base 授权集合里有可用主力，也按当阶段明确 runtime+model 的 Flash
        # 临时降级（不是 AUTO 关系选择、也不是永久 fixed）。
        self.build_store()
        auth = self.auth('temporary_fallback', 'T', 's1', ALL_MAINS + ALL_FLASH,
                         fallback_scope={'task_id': 'T', 'stage': 's1'},
                         runtime='qodercn', model='Qwen3.8-Flash')
        prev = {'task_id': 'T', 'state': 'finished', 'acceptance_reviewed': True, 'stage': 's1',
                'runtime': 'qodercn', 'model': 'Qwen3.8-Flash'}
        r = self.plan(auth, previous=prev, stage='s1')
        self.assertFalse(r['reselection_due'])
        self.assertEqual(r['selection_mode'], 'temporary_fallback')
        self.assertEqual(r['allowed_candidates'], ALL_MAINS + ALL_FLASH)
        self.assertEqual(r['selected']['pool_key'], CN_FLASH, r)
        self.assertTrue(r['session_reuse_allowed'])

    def test_same_stage_temp_non_flash_combo_refused(self):
        self.build_store()
        auth = self.auth('temporary_fallback', 'T', 's1', ALL_MAINS + ALL_FLASH,
                         fallback_scope={'task_id': 'T', 'stage': 's1'},
                         runtime='zcode', model='GLM-5.3')
        prev = {'state': 'finished', 'acceptance_reviewed': True, 'stage': 's1'}
        r = self.plan(auth, previous=prev, stage='s1')
        self.assertEqual(r['refused'], 'temporary_not_flash', r)

    def test_temp_fallback_scope_missing_task_id_refused(self):
        # 缺 task_id 不得猜授权：即使 stage 相同也如实拒绝，不凭 previous/集合蒙混。
        self.build_store()
        auth = self.auth('temporary_fallback', 'T', 's1', ALL_MAINS + ALL_FLASH,
                         fallback_scope={'stage': 's1'},
                         runtime='qodercn', model='Qwen3.8-Flash')
        prev = {'task_id': 'T', 'state': 'finished', 'acceptance_reviewed': True, 'stage': 's1'}
        r = self.plan(auth, previous=prev, stage='s1')
        self.assertEqual(r['refused'], 'fallback_scope_mismatch', r)

    def test_session_reuse_requires_same_task_and_combo(self):
        # 复用仅限 previous 显式属本 task 且组合相同、已验收终态；OTHER/missing task 一律 false。
        self.build_store()
        auth = self.auth('fixed', 'T', 's2', [CN_FLASH], runtime='qodercn',
                         model='Qwen3.8-Flash')
        base = {'state': 'finished', 'acceptance_reviewed': True, 'stage': 's1',
                'runtime': 'qodercn', 'model': 'Qwen3.8-Flash'}
        r = self.plan(auth, previous=dict(base, task_id='T'))
        self.assertEqual(r['refused'], None, r)
        self.assertTrue(r['session_reuse_allowed'], r)
        r = self.plan(auth, previous=dict(base, task_id='OTHER'))
        self.assertEqual(r['refused'], None, r)
        self.assertFalse(r['session_reuse_allowed'], r)
        r = self.plan(auth, previous=dict(base))  # 缺 task_id → 不复用
        self.assertEqual(r['refused'], None, r)
        self.assertFalse(r['session_reuse_allowed'], r)

    def test_fallback_scope_mismatch_refused(self):
        self.build_store()
        auth = self.auth('temporary_fallback', 'T', 's9', [CN_FLASH],
                         fallback_scope={'task_id': 'T', 'stage': 's9'})
        r = self.plan(auth)
        self.assertEqual(r['refused'], 'fallback_scope_mismatch')

    def test_auto_not_constrained_by_previous_model(self):
        self.build_store()
        prev = {'state': 'finished', 'acceptance_reviewed': True, 'stage': 's1',
                'runtime': 'qodercn', 'model': 'Qwen3.8-Flash'}
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH), previous=prev)
        # 上一轮是 CN Flash，但 auto 仍按授权集合选主力，绝不从旧模型猜长期约束。
        self.assertIn(r['selected']['pool_key'], ALL_MAINS, r)


class ContextAndReadOnlyTests(_Base):
    def test_missing_context_refused(self):
        self.build_store()
        auth = self.auth('auto', 'T', 's2', [dp.ZCODE_POOL_KEY])
        ws = self._ws('T')
        for kw in ({'task_id': None, 'stage': 's2', 'workspace': ws},
                   {'task_id': 'T', 'stage': None, 'workspace': ws},
                   {'task_id': 'T', 'stage': 's2', 'workspace': None}):
            r = ss.plan_stage_selection(auth, store_path=self.store, now=T_NIGHT, **kw)
            self.assertEqual(r['refused'], 'missing_context', kw)

    def test_plan_does_not_modify_database(self):
        self.build_store()
        before = self._digest()
        self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH))
        self.assertEqual(before, self._digest())

    def test_wal_writer_latest_active_must_be_read(self):
        # 临时 WAL 库 + 打开的 writer 保留最新 active；规划只读打开必须读到它。
        # BW-ZCODE-RETIRE-20261020：在途占位改用现役 qoder Max 组合，WAL 可见性语义不变。
        writer = dp.connect(self.store)
        try:
            dp._rotation_row(writer)
            r = dp.reserve(self.store, task_id='Q', runtime='qoder', model='Qwen3.8-Max',
                           workspace=self._ws('Q'), prompt_sha256='p', executor='qoder',
                           now=T_NIGHT, _preclaim=False)
            self.assertTrue(r.get('reserved') or r.get('allowed'), r)
            self.assertTrue(Path(self.store + '-wal').exists())
            self.assertTrue(Path(self.store + '-shm').exists())
            plan = self.plan(self.auth('auto', 'Q', 's2', ALL_MAINS + ALL_FLASH), task='Q')
            self.assertEqual(plan['refused'], 'task_in_flight', plan)
            self.assertTrue(Path(self.store + '-shm').exists())  # 不制造/不删除 sidecar
        finally:
            writer.close()

    def test_wal_without_shm_refused_without_creating_sidecar(self):
        p = self.base / 'walonly.sqlite3'
        with closing(dp.connect(str(p))) as conn:
            dp._rotation_row(conn)
        shm = Path(str(p) + '-shm')
        if shm.exists():
            shm.unlink()
        Path(str(p) + '-wal').write_bytes(b'')
        # BW-ZCODE-RETIRE-20261020：授权候选换成现役池，让用例真正走到快照读取路径。
        r = ss.plan_stage_selection(
            self.auth('auto', 'T', 's2', [dp.QMAX_POOL_KEY]), store_path=str(p),
            task_id='T', stage='s2', workspace=self._ws('T'), now=T_NIGHT)
        self.assertEqual(r['refused'], 'store_snapshot_unavailable', r)
        self.assertFalse(shm.exists())

    def test_missing_schema_refused_and_unchanged(self):
        p = self.base / 'noschema.sqlite3'
        conn = sqlite3.connect(str(p))
        conn.execute('CREATE TABLE other(x)')
        conn.commit()
        conn.close()
        ws = self._ws('T')
        before = sorted(fp.name for fp in self.base.iterdir())
        # BW-ZCODE-RETIRE-20261020：现役候选，确保拒绝原因如实是 store 缺 schema。
        r = ss.plan_stage_selection(
            self.auth('auto', 'T', 's2', [dp.QMAX_POOL_KEY]), store_path=str(p),
            task_id='T', stage='s2', workspace=ws, now=T_NIGHT)
        self.assertEqual(r['refused'], 'store_refused', r)
        self.assertEqual(before, sorted(fp.name for fp in self.base.iterdir()))

    def test_open_store_ro_closes_connection_on_error(self):
        # connect 已建但 PRAGMA/BEGIN 失败：必须先 close 再抛 Refused，不泄漏只读连接。
        self.build_store()
        fake = unittest.mock.MagicMock()
        fake.execute.side_effect = sqlite3.OperationalError('boom')
        with unittest.mock.patch.object(ss.sqlite3, 'connect', return_value=fake):
            with self.assertRaises(ss.Refused) as ctx:
                ss._open_store_ro(self.store)
        self.assertEqual(ctx.exception.code, 'store_snapshot_unavailable')
        self.assertTrue(fake.close.called)

    def test_missing_store_refused_and_not_created(self):
        missing = self.base / 'nope.sqlite3'
        # BW-ZCODE-RETIRE-20261020：现役候选，拒绝原因如实是缺库，不被授权校验抢跑。
        r = ss.plan_stage_selection(
            self.auth('auto', 'T', 's2', [dp.QMAX_POOL_KEY]), store_path=str(missing),
            task_id='T', stage='s2', workspace=self._ws('T'), now=T_NIGHT)
        self.assertEqual(r['refused'], 'store_refused')
        self.assertFalse(missing.exists())

    def test_cli_exit_codes(self):
        self.build_store()
        # BW-ZCODE-RETIRE-20261020：CLI 用真实时钟，原来借 ZCode 无时段门保证恒可选；
        # 退役后改用同样无时段门的现役 OpenCode DeepSeek 槽，退出码语义不变。
        auth = self.auth('auto', 'T', 's2', [dp.OPENCODE_DEEPSEEK_POOL_KEY])
        ws = self._ws('T')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ss.main(['--store', self.store, '--task-id', 'T', '--stage', 's2',
                            '--workspace', ws,
                            '--authorization-json', json.dumps(auth)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buf.getvalue())['selected']['pool_key'],
                         dp.OPENCODE_DEEPSEEK_POOL_KEY)
        buf = io.StringIO()
        missing = str(self.base / 'gone.sqlite3')
        with contextlib.redirect_stdout(buf):
            code = ss.main(['--store', missing, '--task-id', 'T', '--stage', 's2',
                            '--workspace', ws,
                            '--authorization-json', json.dumps(auth)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(buf.getvalue())['refused'], 'store_refused')


class PermissionGrantsTests(_Base):
    """BW-TASK-PERMISSION-S1：派工计划生成之前的任务权限需求 × 运行时能力匹配。
    复用 execution_control.CAPABILITY（fine_grained/read_dirs），授权与权限需求分开；
    能力不支持的授权内候选标 permission_incompatible，绝不写成 not_authorized/quota，
    也绝不修改 CAPABILITY 把 ZCode 伪装成 fine_grained=True。"""

    FINE = {'edits': ['Edit(C:/abs/file.py)'], 'bash': ['Bash(pytest -q)'],
            'read_dirs': []}

    def _assert_capability_table_untouched(self):
        self.assertEqual(ec.CAPABILITY['zcode'],
                         {'fine_grained': False, 'read_dirs': False})
        self.assertEqual(ec.CAPABILITY['qoder']['fine_grained'], True)

    def test_day_fine_grants_skip_incompatible_falls_to_flash(self):
        """白天（工作日高峰）精确 Edit/Bash 需求：能力不兼容的候选（OpenCode/ZCode）如实
        permission_incompatible；三个 Max 与 claude 被 weak 档时段门关住
        （BW-MANUAL-THREE-MODE-CLOSEOUT-20261023 恢复既有门），能力兼容且无门的最高层是
        两个 Flash（层=8）→ flash_fallback 选中。BW-ZCODE-RESTORE-1-20261022：ZCode
        fine_grained=False，精确需求下如实 permission_incompatible、绝不启动 Z 计划。"""
        self.build_store()
        r = self.plan(self.auth('auto', 'T', 's2', LEGACY_MAINS + ALL_FLASH),
                      now=T_DAY, permission_grants=self.FINE)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['permission_requirements'],
                         {'fine_grained': True, 'read_dirs': False})
        self.assertEqual(r['selected']['pool_key'], dp.QFLASH_POOL_KEY, r)
        self.assertEqual(r['selected_reason'], 'flash_fallback_selected', r)
        self.assertEqual(r['selected_priority'], 8, r)
        # 精确需求下无逐文件能力的现役候选如实 permission_incompatible。
        self.assertEqual(r['candidate_filters'][dp.OPENCODE_DEEPSEEK_POOL_KEY],
                         'permission_incompatible')
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY],
                         'permission_incompatible')
        self.assertEqual(r['candidate_filters'][dp.CLAUDE_POOL_KEY], 'window_closed')
        self.assertEqual(r['candidate_filters']['qodercn:Qwen3.8-Flash'], 'eligible')
        self.assertEqual(r['candidate_filters'][dp.QFLASH_POOL_KEY], 'eligible')
        self.assertNotEqual(r['selected']['runtime'], 'zcode')
        self._assert_capability_table_untouched()

    def test_day_same_window_without_exact_rules_z_still_eligible(self):
        """同一白天窗口、不要求精确规则（未传 grants）：BW-ZCODE-RESTORE-1-20261022
        后 ZCode 恢复为层=2 现役候选并被直接选中（无需求时不做能力过滤）。
        BW-QODER-INTL-MAX-ALLDAY-20261023：国际 Max 白天也可派且层=1，故先占满其唯一
        名额后 ZCode 才是最高可用层。"""
        self.build_store()
        self.reserve('im', 'qoder', 'Qwen3.8-Max', executor='qoder')
        r = self.plan(self.auth('auto', 'T', 's2', LEGACY_MAINS + ALL_FLASH), now=T_DAY)
        self.assertEqual(r['selected']['pool_key'], dp.ZCODE_POOL_KEY, r)
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'eligible')
        self.assertEqual(r['permission_requirements'],
                         {'fine_grained': False, 'read_dirs': False})

    def test_fixed_z_with_exact_requirement_refused_not_swapped(self):
        """fixed 明确 ZCode 且本轮要求精确 Edit+Bash：ZCode 无逐文件能力，本轮唯一
        授权候选如实 permission_incompatible——诚实拒绝（fixed_candidate_unavailable），
        绝不偷偷换执行器。"""
        self.build_store()
        auth = self.auth('fixed', 'T', 's2', [dp.ZCODE_POOL_KEY],
                         runtime='zcode', model='GLM-5.3')
        r = self.plan(auth, permission_grants=self.FINE)
        self.assertEqual(r['refused'], 'fixed_candidate_unavailable', r)
        self.assertEqual(r['refused_detail'], 'permission_incompatible', r)
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY],
                         'permission_incompatible', r)
        self.assertIsNone(r['selected'])

    def test_read_dirs_requirement_is_independent_capability_filter(self):
        """外部 read_dirs 需求走独立能力过滤：只剔除无 read_dirs 能力的候选，与
        fine_grained 需求分别可辨（CodeBuddy 两模型/ZCode/两 OpenCode 都无 read_dirs，
        如实 permission_incompatible）。BW-MANUAL-THREE-MODE-CLOSEOUT-20261023：weak 档
        白天（工作日高峰）三个 Max 与 claude 被时段门关住，有 read_dirs 能力且无门的
        最高层是两个 Flash（层=8）——能力过滤与时段门各自如实标注、互不冒充。"""
        self.build_store()
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH), now=T_DAY,
                      permission_grants={'edits': [], 'bash': [],
                                         'read_dirs': ['D:/refs']})
        self.assertEqual(r['permission_requirements'],
                         {'fine_grained': False, 'read_dirs': True})
        self.assertEqual(r['candidate_filters'][dp.CODEBUDDY_POOL_KEY],
                         'permission_incompatible')
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY],
                         'permission_incompatible')
        self.assertEqual(r['candidate_filters'][dp.OPENCODE_DEEPSEEK_POOL_KEY],
                         'permission_incompatible')
        self.assertEqual(r['candidate_filters'][dp.CLAUDE_POOL_KEY], 'window_closed')
        # 有 read_dirs 能力（qoder/qodercn）且未被时段门挡住的 Flash 被选中。
        self.assertEqual(r['selected']['pool_key'], dp.QFLASH_POOL_KEY, r)
        self.assertEqual(r['selected_reason'], 'flash_fallback_selected', r)
        self._assert_capability_table_untouched()

    def test_missing_qoder_authorization_never_expands_via_permission_filter(self):
        """能力过滤只作用于授权集合内候选：缺 Qoder 授权时精确需求绝不把候选扩到
        Qoder，非授权池仍如实 not_authorized，需求兼容也不构成授权。
        BW-ZCODE-RESTORE-1-20261022：未授权的 ZCode 即便能力不兼容也只标
        not_authorized（过滤不扩权）。"""
        self.build_store()
        auth = self.auth('auto', 'T', 's2', [CN_FLASH, dp.OPENCODE_DEEPSEEK_POOL_KEY])
        r = self.plan(auth, permission_grants=self.FINE)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected']['pool_key'], CN_FLASH, r)
        self.assertEqual(r['candidate_filters']['qoder:Qwen3.8-Flash'], 'not_authorized')
        self.assertEqual(r['candidate_filters'][dp.OPENCODE_DEEPSEEK_POOL_KEY],
                         'permission_incompatible')
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'not_authorized')

    def test_permission_incompatible_and_availability_now_orthogonal(self):
        """BW-SIMPLE-PRIORITY-RETRY-S3：历史 availability 阻断不再作为普通新轮的过滤
        原因——只作 receipt 只读诊断（zcode_blocked），绝不假装 healthy 或清除 hold；
        只有真正的权限能力冲突才记 permission_incompatible。BW-ZCODE-RESTORE-1-20261022：
        ZCode 恢复逐候选枚举：混合 Edit+Bash 下如实 permission_incompatible；无精确需求时
        仅为 eligible 候选，availability 阻断只回显只读诊断。"""
        self.build_store()
        with _patch_zcode(False):
            # 混合 Edit+Bash：无逐文件能力的现役候选（OpenCode/ZCode）如实不兼容。
            r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH),
                          permission_grants=self.FINE)
            self.assertEqual(r['zcode_blocked'], True)
            self.assertEqual(r['candidate_filters'][dp.OPENCODE_DEEPSEEK_POOL_KEY],
                             'permission_incompatible', r)
            self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY],
                             'permission_incompatible', r)
        with _patch_zcode(False):
            # 无精确权限需求：旧的 availability 阻断只回显诊断，不产生任何过滤原因。
            r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH))
            self.assertEqual(r['zcode_blocked'], True)
            self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'eligible', r)
            self.assertNotIn('zcode_unavailable', set(r['candidate_filters'].values()))
            self.assertNotEqual(r['selected'], None, r)

    def test_workspace_inflight_guards_and_readonly_hash_kept_with_grants(self):
        """权限过滤不替代既有守卫：同任务在途/工作区冲突照旧整项拒绝；
        带 grants 的规划仍只读，读写前后快照哈希不变。
        BW-ZCODE-RETIRE-20261020：在途占位改用现役 qoder Max 组合，守卫语义不变。"""
        self.build_store()
        shared = self._ws('shared')
        self.reserve('a', 'qoder', 'Qwen3.8-Max', ws=shared, executor='qoder')
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH),
                      ws=shared, permission_grants=self.FINE)
        self.assertEqual(r['refused'], 'workspace_conflict')
        self.reserve('T', 'qodercn', 'Qwen3.8-Flash', executor='qodercn')
        before = self._digest()
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH),
                      task='T', permission_grants=self.FINE)
        self.assertEqual(r['refused'], 'task_in_flight')
        self.assertEqual(before, self._digest())

    def test_malformed_permission_grants_refused_and_cli_exit_two(self):
        """严格拒绝畸形 grants：非对象/未知键/非列表/空或带空白项 →
        invalid_permission_grants；CLI 最小新输入 --permission-grants-json 畸形
        JSON 或拒绝结果退出码 2。"""
        self.build_store()
        bad = ([1, 2], {'edits': 'not-a-list'}, {'edits': ['']},
               {'edits': [' x ']}, {'edits': [], 'tool_visibility': ['Read']},
               {'editz': []})
        for b in bad:
            r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH),
                          permission_grants=b)
            self.assertEqual(r['refused'], 'invalid_permission_grants', b)
        ws = self._ws('T')
        # 畸形 JSON 字符串输入（与 --authorization-json 同口径）：非零 SystemExit，
        # 绝不宽容；合法 JSON 内的畸形 grants 结构则走拒收回执、退出码 2。
        with self.assertRaises(SystemExit) as ctx:
            with contextlib.redirect_stdout(io.StringIO()):
                ss.main(['--store', self.store, '--task-id', 'T', '--stage', 's2',
                         '--workspace', ws,
                         '--authorization-json',
                         json.dumps(self.auth('auto', 'T', 's2', ALL_MAINS)),
                         '--permission-grants-json', '{not json'])
        self.assertIn('invalid JSON', str(ctx.exception))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ss.main(['--store', self.store, '--task-id', 'T', '--stage', 's2',
                            '--workspace', ws,
                            '--authorization-json',
                            json.dumps(self.auth('auto', 'T', 's2', ALL_MAINS)),
                            '--permission-grants-json', '{"edits": [" x "]}'])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(buf.getvalue())['refused'],
                         'invalid_permission_grants')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ss.main(['--store', self.store, '--task-id', 'T', '--stage', 's2',
                            '--workspace', ws,
                            '--authorization-json',
                            json.dumps(self.auth('fixed', 'T', 's2',
                                                 [dp.ZCODE_POOL_KEY],
                                                 runtime='zcode', model='GLM-5.3')),
                            '--permission-grants-json', json.dumps(self.FINE)])
        self.assertEqual(code, 2)
        # BW-ZCODE-RESTORE-1-20261022：ZCode 恢复后 fixed 点名可通过授权校验，
        # 但 FINE 授权与 ZCode 受控文件画像不兼容 → fixed_candidate_unavailable，仍退出码 2。
        self.assertEqual(json.loads(buf.getvalue())['refused'],
                         'fixed_candidate_unavailable')

    def test_temp_fallback_same_stage_and_parent_reselect_apply_current_need(self):
        """临时降级不丢失当前阶段权限需求：同阶段指定 Flash 与旧 scope 到期经父授权
        重选，都按**本轮** grants 应用能力过滤，绝不从旧 session 继承需求、也不因临时/
        父授权绕过过滤。BW-ZCODE-RESTORE-1-20261022：ZCode 恢复枚举，逐候选冲突由
        OpenCode 与 ZCode 共同如实承担（均无逐文件能力）。BW-MANUAL-THREE-MODE-
        CLOSEOUT-20261023：白天（工作日高峰）三个 Max 与 claude 都被 weak 档时段门关住，
        到期重裁决按层序落到兼容且无门的已授权 Flash。"""
        self.build_store()
        # 同阶段：指定已授权且能力兼容的 CN Flash（runtime qodercn fine_grained=True）
        # → 正常 temporary_fallback_selected；OpenCode 在候选集合中如实标 permission_incompatible。
        auth = self.auth('temporary_fallback', 'T', 's1', ALL_MAINS + ALL_FLASH,
                         fallback_scope={'task_id': 'T', 'stage': 's1'},
                         runtime='qodercn', model='Qwen3.8-Flash')
        prev = {'task_id': 'T', 'state': 'finished', 'acceptance_reviewed': True,
                'stage': 's1', 'runtime': 'qodercn', 'model': 'Qwen3.8-Flash'}
        r = self.plan(auth, previous=prev, stage='s1',
                      permission_grants=self.FINE)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected_reason'], 'temporary_fallback_selected', r)
        self.assertEqual(r['selected']['pool_key'], CN_FLASH, r)
        self.assertEqual(r['candidate_filters'][dp.OPENCODE_DEEPSEEK_POOL_KEY],
                         'permission_incompatible')
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY],
                         'permission_incompatible')
        # 到期经父授权重选：应用**当前**需求 → 白天（工作日高峰）三个 Max 与 claude 都被
        # weak 档时段门关住（BW-MANUAL-THREE-MODE-CLOSEOUT-20261023 恢复既有门），无逐文件
        # 能力候选权限不兼容，选择落兼容且无门的 qoder:Qwen3.8-Flash（层=8）；旧 s1 的需求
        # 绝不反向继承或改写本轮需求。
        # 父授权明确保留旧候选集（不含 CodeBuddy）：CodeBuddy 层=5 高于 Flash，若纳入父候选
        # 会先于 Flash 被选中，这与本例“临时 fallback 到期重裁决按当前权限需求过滤”的旧语义
        # 无关；CodeBuddy 的自动/显式入口由 test_codebuddy_reactivation.py 独立验证。
        parent = self.auth('auto', 'T', 's2', LEGACY_MAINS + ALL_FLASH)
        auth2 = self.auth('temporary_fallback', 'T', 's1', ALL_MAINS + ALL_FLASH,
                          fallback_scope={'task_id': 'T', 'stage': 's1'},
                          runtime='qodercn', model='Qwen3.8-Flash',
                          parent_authorization=parent)
        prev2 = {'task_id': 'T', 'state': 'finished', 'acceptance_reviewed': True,
                 'stage': 's1', 'runtime': 'qodercn', 'model': 'Qwen3.8-Flash'}
        r = self.plan(auth2, previous=prev2, stage='s2', now=T_DAY,
                      permission_grants=self.FINE)
        self.assertEqual(r['refused'], None, r)
        self.assertTrue(r['reselection_due'])
        self.assertEqual(r['selection_mode'], 'auto')
        self.assertEqual(r['permission_requirements'],
                         {'fine_grained': True, 'read_dirs': False})
        self.assertEqual(r['candidate_filters'][dp.OPENCODE_DEEPSEEK_POOL_KEY],
                         'permission_incompatible')
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY],
                         'permission_incompatible')
        self.assertEqual(r['candidate_filters'][dp.CLAUDE_POOL_KEY], 'window_closed')
        self.assertEqual(r['selected']['pool_key'], dp.QFLASH_POOL_KEY, r)
        self.assertEqual(r['selected_reason'], 'flash_fallback_selected', r)

    def test_edits_only_grants_select_zcode_with_controlled_profile(self):
        """仅需精确文件 Edit（无 Bash、无 read_dirs）：BW-ZCODE-RESTORE-1-20261022 起
        ZCode 恢复现役候选，纯精确 Edit 受控文件 profile 使其重新可选（层=2 直接选中）；
        回执按 grants 结构标 controlled_file_profile_required=True，且绝不修改 CAPABILITY
        的能力表。BW-QODER-INTL-MAX-ALLDAY-20261023：国际 Max 白天可派且能力兼容，故先
        占满其唯一名额，层=2 的 ZCode 才是最高可用层。"""
        self.build_store()
        self.reserve('im', 'qoder', 'Qwen3.8-Max', executor='qoder')
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH), now=T_DAY,
                      permission_grants={'edits': ['Edit(C:/a.py)'], 'bash': [],
                                         'read_dirs': []})
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'eligible', r)
        self.assertIn(dp.ZCODE_POOL_KEY,
                      [x['pool_key'] for x in r['eligible_candidates']])
        self.assertEqual(r['selected']['pool_key'], dp.ZCODE_POOL_KEY, r)
        self.assertEqual(r['permission_requirements'],
                         {'fine_grained': True, 'read_dirs': False})
        self.assertTrue(r['controlled_file_profile_required'], r)
        self._assert_capability_table_untouched()

    def test_fixed_z_edits_only_selected_with_controlled_profile(self):
        """fixed 明确 ZCode 且本轮仅需精确 Edit：BW-ZCODE-RESTORE-1-20261022 起恢复
        受控文件 profile 可选——正常选中 ZCode（绝不偷偷换执行器），回执标
        controlled_file_profile_required=True。"""
        self.build_store()
        auth = self.auth('fixed', 'T', 's2', [dp.ZCODE_POOL_KEY],
                         runtime='zcode', model='GLM-5.3')
        r = self.plan(auth, permission_grants={'edits': ['Edit(C:/a.py)'], 'bash': [],
                                               'read_dirs': []})
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected']['pool_key'], dp.ZCODE_POOL_KEY, r)
        self.assertEqual(r['selected_reason'], 'fixed_authorized', r)
        self.assertTrue(r['controlled_file_profile_required'], r)

    def test_mixed_edit_bash_task_remains_permission_incompatible(self):
        """混合 Edit+Bash 整段任务继续 permission_incompatible：受控文件 profile 只覆盖纯精确
        Edit，Bash 需求一律不成立，回执不标 controlled_file_profile_required。硬 Edit+Bash 不支持
        无逐文件能力的执行器直接承接，但可选能承接同一硬合同的已授权兼容执行器一轮完成；仅确有依赖且经任务
        授权时采用纯 Edit profile 与必要独立测试，不强制/机械拆分。
        BW-ZCODE-RESTORE-1-20261022：无逐文件能力的 OpenCode 与 ZCode 都如实不兼容。"""
        self.build_store()
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH), now=T_DAY,
                      permission_grants=self.FINE)
        self.assertEqual(r['candidate_filters'][dp.OPENCODE_DEEPSEEK_POOL_KEY],
                         'permission_incompatible', r)
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY],
                         'permission_incompatible', r)
        self.assertFalse(r['controlled_file_profile_required'], r)


class TaskScopeTests(_Base):
    """BW-PERMISSION-SCOPE-S1：普通隔离工程的文件/测试目标 task_scope 与硬合同
    permission_grants 分清。task_scope 只进回执/任务书，绝不驱动能力过滤/授权，
    也绝不弱化 hard grants；permission_incompatible 是能力冲突（带实际冲突明细与
    ordinary_task_tools 可选路线），不是模型/认证/额度失败。"""

    SCOPE = {'edits': [f'source/f{i}.cjs' for i in range(1, 6)],
             'bash': ['python -m unittest discover -s tests -p test_app.py'],
             'read_dirs': ['references/']}
    FINE = PermissionGrantsTests.FINE

    def test_ordinary_scope_selects_opencode_when_only_route_open(self):
        """白天三 Max 窗关、Claude 明确 limited，同 5 文件/1 测试只是 task_scope
        （无硬 grants）：requirements 全 false、不做能力过滤。BW-ZCODE-RESTORE-1-20261022：
        ZCode 恢复为层=2 现役候选并被直接选中。BW-QODER-INTL-MAX-ALLDAY-20261023：白天
        只有两个 CN Max 窗关，国际 Max 可派且层=1，故先占满其唯一名额。"""
        self.build_store()
        self.set_limit(dp.CLAUDE_POOL_KEY)
        self.reserve('im', 'qoder', 'Qwen3.8-Max', executor='qoder')
        r = self.plan(self.auth('auto', 'T', 's2', LEGACY_MAINS + ALL_FLASH), now=T_DAY,
                      task_scope=self.SCOPE)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected']['pool_key'], dp.ZCODE_POOL_KEY, r)
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'eligible')
        self.assertEqual(r['permission_requirements'],
                         {'fine_grained': False, 'read_dirs': False})
        self.assertFalse(r['controlled_file_profile_required'])
        self.assertEqual(r['task_scope'], self.SCOPE)
        self.assertEqual(r['permission_incompatibility_details'], {})

    def test_same_scope_plus_hard_grants_keeps_contract_and_inputs_untouched(self):
        """同样 scope 再传明确 hard Edit+Bash：无逐文件能力的现役候选（OpenCode）仍
        permission_incompatible，hard 合同完整生效、不被 task_scope 覆盖；回执保留
        task_scope；调用方原 scope/grants 对象不被突变。BW-ZCODE-RESTORE-1-20261022：
        ZCode 恢复枚举，其与 OpenCode 的冲突明细都如实出现。"""
        self.build_store()
        scope = dict(self.SCOPE)
        grants = dict(self.FINE)
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH), now=T_DAY,
                      permission_grants=grants, task_scope=scope)
        self.assertEqual(r['candidate_filters'][dp.OPENCODE_DEEPSEEK_POOL_KEY],
                         'permission_incompatible', r)
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY],
                         'permission_incompatible', r)
        self.assertEqual(r['permission_requirements'],
                         {'fine_grained': True, 'read_dirs': False})
        self.assertEqual(r['task_scope'], self.SCOPE)
        self.assertEqual(scope, self.SCOPE)   # 原输入不突变
        self.assertEqual(grants, self.FINE)
        details = r['permission_incompatibility_details'][dp.OPENCODE_DEEPSEEK_POOL_KEY]
        self.assertIn('per-file edits/verbatim Bash grants', details['unsupported'])
        self.assertIn('ordinary_task_tools', details['note'])
        self.assertIn(dp.ZCODE_POOL_KEY, r['permission_incompatibility_details'])
        self._assert_readonly_with_scope()

    def _assert_readonly_with_scope(self):
        before = self._digest()
        self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH), now=T_DAY,
                  task_scope=self.SCOPE)
        self.assertEqual(before, self._digest())

    def test_legacy_grants_only_unchanged_and_no_scope(self):
        """legacy 仅 permission_grants 混合需求：回执 task_scope=None、明细仍给冲突
        说明（未传新输入完全兼容旧行为）。BW-ZCODE-RESTORE-1-20261022：ZCode 恢复
        枚举，其冲突明细与 OpenCode 一并如实出现。"""
        self.build_store()
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH), now=T_DAY,
                      permission_grants=self.FINE)
        self.assertEqual(r['candidate_filters'][dp.OPENCODE_DEEPSEEK_POOL_KEY],
                         'permission_incompatible', r)
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY],
                         'permission_incompatible', r)
        self.assertIn(dp.ZCODE_POOL_KEY, r['permission_incompatibility_details'])
        self.assertIsNone(r['task_scope'])
        self.assertIn(dp.OPENCODE_DEEPSEEK_POOL_KEY,
                      r['permission_incompatibility_details'])

    def test_pure_hard_edits_keep_controlled_profile_consistency(self):
        """纯 hard Edit：仍 required controlled profile 与正式 preflight 同口径；
        两个 OpenCode 免费模型不支持逐文件硬授权，如实出现在明细里（绝不因"整池空诊断"
        而放过）。BW-ZCODE-RESTORE-1-20261022：ZCode 恢复后纯精确 Edit 受控 profile 重新
        可选（层=2 直接选中）。BW-QODER-INTL-MAX-ALLDAY-20261023：国际 Max 白天可派且
        能力兼容，故先占满其唯一名额。"""
        self.build_store()
        self.reserve('im', 'qoder', 'Qwen3.8-Max', executor='qoder')
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH), now=T_DAY,
                      permission_grants={'edits': ['Edit(source/f1.cjs)'], 'bash': [],
                                         'read_dirs': []},
                      task_scope={'edits': ['source/f1.cjs'], 'bash': [], 'read_dirs': []})
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'eligible', r)
        self.assertEqual(r['selected']['pool_key'], dp.ZCODE_POOL_KEY, r)
        self.assertTrue(r['controlled_file_profile_required'], r)
        details = r['permission_incompatibility_details']
        self.assertNotIn(dp.ZCODE_POOL_KEY, details, details)
        self.assertIn(dp.OPENCODE_DEEPSEEK_POOL_KEY, details, details)
        self.assertIn(dp.OPENCODE_GLM_POOL_KEY, details, details)

    def test_scope_never_bypasses_existing_guards(self):
        """fixed 不换执行器、缺授权、availability 阻断、在途、工作区冲突都不被
        task_scope 绕过，原因照旧可辨。BW-ZCODE-RESTORE-1-20261022：fixed 点名 ZCode
        且要求精确 Edit+Bash 时按能力冲突诚实拒绝（fixed_candidate_unavailable）；
        未授权 ZCode 如实 not_authorized；availability 阻断仍只作只读诊断回显。"""
        self.build_store()
        auth = self.auth('fixed', 'T', 's2', [dp.ZCODE_POOL_KEY],
                         runtime='zcode', model='GLM-5.3')
        r = self.plan(auth, permission_grants=self.FINE, task_scope=self.SCOPE)
        self.assertEqual(r['refused'], 'fixed_candidate_unavailable', r)
        self.assertEqual(r['refused_detail'], 'permission_incompatible', r)
        r = self.plan(self.auth('auto', 'T', 's2', [CN_FLASH]), now=T_DAY,
                      task_scope=self.SCOPE)
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'not_authorized')
        with _patch_zcode(False):
            # 历史 availability 阻断只作只读诊断回显，不产生候选过滤。
            r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH),
                          now=T_DAY, task_scope=self.SCOPE)
            self.assertEqual(r['zcode_blocked'], True)
            self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'eligible', r)
        self.reserve('T', 'qoder', 'Qwen3.8-Max', executor='qoder')
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH),
                      task_scope=self.SCOPE)
        self.assertEqual(r['refused'], 'task_in_flight')
        shared = self._ws('shared')
        self.reserve('a', 'qodercn', 'Qwen3.8-Flash', ws=shared, executor='qodercn')
        r = self.plan(self.auth('auto', 'T2', 's2', ALL_MAINS + ALL_FLASH), task='T2',
                      ws=shared, task_scope=self.SCOPE)
        self.assertEqual(r['refused'], 'workspace_conflict')

    def test_scope_does_not_change_routing_reasons_or_quota_semantics(self):
        """task_scope 不改任何逐候选原因/选择/availability 语义（带与不带完全一致）；
        正文 402/无调用之类的叙述不进入本入口，quota 仍只认既有 availability 口径。"""
        self.build_store()
        allowed = self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH)
        base = self.plan(allowed, now=T_DAY)
        with_scope = self.plan(allowed, now=T_DAY, task_scope=self.SCOPE)
        self.assertEqual(base['candidate_filters'], with_scope['candidate_filters'])
        self.assertEqual(base['selected'], with_scope['selected'])
        self.assertEqual(base['zcode_blocked'], with_scope['zcode_blocked'])
        self.assertFalse(with_scope['zcode_blocked'])

    def test_cli_task_scope_and_malformed_refused(self):
        """CLI 新输入 --task-scope-json 原样进回执；畸形 scope（非对象/未知键/非列表/
        空或带空白项）→ invalid_task_scope 拒绝、退出 2。
        BW-ZCODE-RETIRE-20261020：CLI 用真实时钟，正例候选由退役 ZCode 换成同样无
        时段门的现役 OpenCode DeepSeek。"""
        self.build_store()
        ws = self._ws('T')
        auth = self.auth('auto', 'T', 's2', [dp.OPENCODE_DEEPSEEK_POOL_KEY])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ss.main(['--store', self.store, '--task-id', 'T', '--stage', 's2',
                            '--workspace', ws,
                            '--authorization-json', json.dumps(auth),
                            '--task-scope-json', json.dumps(self.SCOPE)])
        self.assertEqual(code, 0)
        out = json.loads(buf.getvalue())
        self.assertEqual(out['selected']['pool_key'], dp.OPENCODE_DEEPSEEK_POOL_KEY)
        self.assertEqual(out['task_scope'], self.SCOPE)
        for bad in ([1], {'edits': 'x'}, {'edits': ['']}, {'edits': [' ']},
                    {'scope': []}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = ss.main(['--store', self.store, '--task-id', 'T', '--stage', 's2',
                                '--workspace', ws,
                                '--authorization-json', json.dumps(auth),
                                '--task-scope-json', json.dumps(bad)])
            self.assertEqual(code, 2, bad)
            self.assertEqual(json.loads(buf.getvalue())['refused'],
                             'invalid_task_scope', bad)
        with self.assertRaises(SystemExit) as ctx:
            with contextlib.redirect_stdout(io.StringIO()):
                ss.main(['--store', self.store, '--task-id', 'T', '--stage', 's2',
                         '--workspace', ws,
                         '--authorization-json', json.dumps(auth),
                         '--task-scope-json', '{not json'])
        self.assertIn('invalid JSON', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
