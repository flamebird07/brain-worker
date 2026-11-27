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
# BW-WEAK-RESTRUCTURE-20261116: 断言弱档层序/档位策略时直接对照单一来源 dispatch_mode。
import dispatch_mode as dm  # noqa: E402
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
        with dispatch_mode('weak'):
            r = self.plan(self.auth('auto', 'T', 's2', allowed), now=T_DAY)
            self.assertIsNone(r['refused'], r)
            self.assertEqual(r['dispatch_mode'], 'weak', r)
            # BW-WEAK-RESTRUCTURE-20261116: 新弱档层1=两 OpenCode（白天只剩 claude 高峰门）。
            self.assertEqual(r['selected_priority'], 1, r)
            self.assertIn(r['selected']['pool_key'], dp.OPENCODE_POOL_KEYS, r)
            eligible = {x['pool_key']: x for x in r['eligible_candidates']}
            # BW-WEAK-RESTRUCTURE-20261116: 弱档 ZCode 只剩 GLM-5.3-Flash（层=4、独立 1 槽）；
            # CodeBuddy GLM 层=2。BW-WEAK-TIER-ENFORCE-20261122: weak 无时段门——claude
            # 白天也合格（层=3、容量 2），不再被高峰门关住。
            self.assertEqual(eligible[dp.ZCODE_FLASH_POOL_KEY]['priority'], 4)
            self.assertEqual(eligible[dp.ZCODE_FLASH_POOL_KEY]['capacity'], 1)
            self.assertEqual(eligible[dp.OPENCODE_DEEPSEEK_POOL_KEY]['priority'], 1)
            self.assertEqual(eligible[dp.CODEBUDDY_GLM_POOL_KEY]['priority'], 2)
            self.assertEqual(eligible[dp.CODEBUDDY_GLM_POOL_KEY]['capacity'], 1)
            self.assertEqual(eligible[dp.CLAUDE_POOL_KEY]['priority'], 3)
            self.assertEqual(eligible[dp.CLAUDE_POOL_KEY]['capacity'], 2)
            for pk in (dp.QMAX_POOL_KEY, dp.CN_MAX_POOL_KEY,
                       dp.CN_CUSTOM_MAX_POOL_KEY, dp.ZCODE_POOL_KEY, dp.SONUS_POOL_KEY):
                self.assertNotIn(pk, eligible, pk)
            excluded = {x['pool_key']: x for x in r['excluded_candidates_with_reason']}
            # BW-WEAK-RESTRUCTURE-20261116: codebuddy:deepseek-v4.1-flash 已移出弱档——
            # 档位硬排除先于真实限额标记（即便 main_force_limits 里有它的限额行，
            # 也如实 mode_excluded，绝不冒充 main_force_limited）。
            self.assertEqual(excluded[dp.CODEBUDDY_POOL_KEY]['reason'],
                             'mode_excluded')
            # BW-WEAK-TIER-ENFORCE-20261122: weak 无时段门——claude 合格进 eligible，
            # 不再出现在 excluded（旧口径的 window_closed 已不存在）。
            self.assertNotIn(dp.CLAUDE_POOL_KEY, excluded)
            # BW-WEAK-RESTRUCTURE-20261116: 全部 Qoder 池与 zcode:GLM-5.3 已移出弱档，
            # 时段门不再对它们生效（档位排除先于时段门）。
            for pk in (dp.QMAX_POOL_KEY, dp.CN_MAX_POOL_KEY, dp.CN_CUSTOM_MAX_POOL_KEY,
                       dp.ZCODE_POOL_KEY, dp.SONUS_POOL_KEY,
                       dp.QFLASH_POOL_KEY, dp.CN_FLASH_POOL_KEY):
                self.assertEqual(excluded[pk]['reason'], 'mode_excluded', pk)
            # BW-WEAK-RESTRUCTURE-20261116: 选中层=1 已是弱档最高层 → 无更高层排除。
            self.assertEqual(r['higher_priority_exclusions'], [], r)
            self.assertEqual(r['next_candidate']['priority'], 2)
            self.assertEqual(r['next_candidate']['pool_key'], dp.CODEBUDDY_GLM_POOL_KEY)
        with dispatch_mode('strong'):
            r = self.plan(self.auth('auto', 'T', 's2', allowed), now=T_DAY)
            self.assertEqual(r['dispatch_mode'], 'strong', r)
            self.assertEqual(r['selected_priority'], 1, r)
            self.assertIn(r['selected']['pool_key'], (dp.QMAX_POOL_KEY, dp.CN_MAX_POOL_KEY), r)
            eligible = {x['pool_key']: x for x in r['eligible_candidates']}
            self.assertNotIn(dp.SONUS_POOL_KEY, eligible)
            self.assertEqual(eligible[dp.ZCODE_POOL_KEY]['priority'], 3)
            self.assertEqual(eligible[dp.CLAUDE_POOL_KEY]['priority'], 2)
            self.assertEqual(eligible[dp.CLAUDE_POOL_KEY]['capacity'], 2)
            self.assertEqual(eligible[dp.QMAX_POOL_KEY]['priority'], 1)
            self.assertEqual(eligible[dp.CN_MAX_POOL_KEY]['priority'], 1)
            excluded = {x['pool_key']: x for x in r['excluded_candidates_with_reason']}
            self.assertEqual(excluded[dp.CODEBUDDY_POOL_KEY]['reason'],
                             'mode_excluded')
            self.assertEqual(excluded[dp.OPENCODE_DEEPSEEK_POOL_KEY]['reason'],
                             'mode_excluded')
            self.assertEqual(r['next_candidate']['priority'], 1)
            self.assertEqual({r['selected']['pool_key'], r['next_candidate']['pool_key']},
                             {dp.QMAX_POOL_KEY, dp.CN_MAX_POOL_KEY})

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
        # BW-WEAK-RESTRUCTURE-20261116: qoder Max 只属 strong/medium——换档 medium
        # 保持本意（夜间 Max 可选，首次 Qoder 派发前按 task_scope 生成权限计划）。
        with dispatch_mode('medium'):
            r = self.plan(self.auth('auto', 'T', 's2', [dp.QMAX_POOL_KEY]),
                          now=T_NIGHT, task_scope=scope)
        self.assertIsNone(r['refused'], r)
        plan = r['qoder_permission_plan']
        self.assertEqual(plan['visible_tools'],
                         ['Read', 'Glob', 'Grep', 'Edit', 'Write', 'Bash'])
        self.assertIn('Edit(/app/main.py)', plan['allowed_tools'])
        self.assertIn('Bash(python -m unittest tests.test_main)',
                      plan['allowed_tools'])

    def test_planned_claim_respects_scope_and_round_failure_in_weak_mode(self):
        self.build_store()
        deepseek = dp.OPENCODE_DEEPSEEK_POOL_KEY
        cases = (
            ('auto', [dp.CLAUDE_POOL_KEY], {}, []),
            ('executor', [deepseek, dp.CLAUDE_POOL_KEY], {'executor': 'claude'}, []),
            ('auto', [deepseek, dp.CLAUDE_POOL_KEY], {}, [deepseek]),
        )
        with dispatch_mode('weak'):
            for n, (mode, candidates, extra, failed) in enumerate(cases):
                task = f'claim-{n}'
                authorization = self.auth(mode, task, 's2', candidates, **extra)
                receipt = ss.plan_and_claim_stage(
                    authorization, prompt_sha256='p', task_id=task, stage='s2',
                    workspace=self._ws(task), store_path=self.store, now=T_NIGHT,
                    round_failed_candidates=failed)
                self.assertEqual(receipt['selected']['pool_key'], dp.CLAUDE_POOL_KEY, receipt)
                claim = receipt['dispatch_claim']
                self.assertTrue(claim['allowed'], claim)
                self.assertTrue(dp.finish(self.store, claim['token'], terminal='cancelled',
                                          now=T_NIGHT)['released'])

    def test_claim_refuses_engine_revision_mismatch_before_allocating(self):
        self.build_store()
        authorization = self.auth('auto', 'T', 's2', [dp.OPENCODE_DEEPSEEK_POOL_KEY])
        preview = self.plan(authorization)
        self.assertEqual(len(preview['engine_revision']), 64)
        self.assertEqual(preview['candidate_tool_profiles'][dp.OPENCODE_DEEPSEEK_POOL_KEY],
                         {'allowed_tools': ['edit', 'read'], 'command_execution': False})
        claimed = ss.plan_and_claim_stage(
            authorization, prompt_sha256='p', task_id='T', stage='s2',
            workspace=self._ws('T'), store_path=self.store, now=T_NIGHT,
            expected_engine_revision='unexpected-old-version')
        self.assertEqual(claimed['refused'], 'engine_revision_changed')
        self.assertIsNone(claimed['dispatch_claim'])

    def test_existing_allowlist_filters_retired_opencode_glm(self):
        self.build_store()
        allowed = [dp.OPENCODE_GLM_POOL_KEY, dp.OPENCODE_DEEPSEEK_POOL_KEY]
        r = self.plan(self.auth('auto', 'T', 's2', allowed), now=T_DAY)
        self.assertEqual(r['allowed_candidates'], allowed)
        self.assertEqual(r['candidate_filters'][dp.OPENCODE_GLM_POOL_KEY], 'retired_pool')
        self.assertEqual(r['selected']['pool_key'], dp.OPENCODE_DEEPSEEK_POOL_KEY)

        r = self.plan(self.auth('auto', 'T', 's2', allowed[:1]), now=T_DAY)
        self.assertIsNone(r['selected'])
        self.assertEqual(r['refused'], 'no_eligible_candidate')

    def test_recent_limits_and_day_windows_select_opencode_before_flash(self):
        self.build_store()
        with closing(dp.connect(self.store)) as conn:
            # BW-WEAK-RESTRUCTURE-20261116: 限额短窗表的弱档可派池只剩 claude
            # （原 codebuddy deepseek 已移出弱档；GLM 额度独立不在短窗表）——真实限额
            # 标记改种在 claude 上，保持"层序受限 → OpenCode 先于弱档 Flash"的验证；
            # 限额检查先于时段门，白天 claude 如实 main_force_limited。
            for pk in (dp.CLAUDE_POOL_KEY,):
                conn.execute(
                    'INSERT INTO main_force_limits(pool_key, limited, recorded_at_utc) '
                    'VALUES(?,1,?)', (pk, dp._iso(T_DAY)))
            conn.execute('PRAGMA journal_mode=DELETE')
        # BW-WEAK-RESTRUCTURE-20261116: 旧国际 qoder Max 占位已不在弱档可派集合，改占
        # 一个 OpenCode 独立槽；白天弱档选择落另一个 OpenCode。
        self.reserve('o1', 'opencode', dp.OPENCODE_DEEPSEEK_MODEL, executor='opencode')
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH), now=T_DAY)
        self.assertIsNone(r['refused'], r)
        self.assertEqual(r['candidate_filters'][dp.CLAUDE_POOL_KEY],
                         'main_force_limited')
        self.assertEqual(r['candidate_filters'][dp.CODEBUDDY_GLM_POOL_KEY], 'eligible')
        # BW-WEAK-RESTRUCTURE-20261116: zcode:GLM-5.3 与旧 codebuddy deepseek 已移出弱档
        # （mode_excluded）；弱档新表下 OpenCode（层1）仍先于 zcode GLM-5.3-Flash（层4）。
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'mode_excluded', r)
        self.assertEqual(r['candidate_filters'][dp.CODEBUDDY_POOL_KEY], 'mode_excluded')
        self.assertEqual(r['candidate_filters'][dp.ZCODE_FLASH_POOL_KEY], 'eligible', r)
        self.assertEqual(r['selected']['pool_key'], dp.CODEBUDDY_GLM_POOL_KEY, r)
        self.assertNotIn(r['selected']['pool_key'], dp.OVERFLOW_KEYS)

    def test_fixed_cn_flash_kept_even_with_free_main(self):
        self.build_store()
        # BW-WEAK-RESTRUCTURE-20261116: 两个 Qwen3.8-Flash 已从所有档位移除（无任何档位
        # 可派）——fixed 点名 CN Flash 现在如实 mode_excluded 拒绝
        # （fixed_candidate_unavailable），绝不静默换模型；fixed"保持用户点名、不被免费
        # 主力顶替"的语义改用弱档可派的 zcode:GLM-5.3-Flash 验证：即便层1 两 OpenCode
        # 免费主力可选，也按点名选中。
        auth = self.auth('fixed', 'T', 's2', [CN_FLASH], runtime='qodercn',
                         model='Qwen3.8-Flash')
        r = self.plan(auth)
        self.assertEqual(r['refused'], 'fixed_candidate_unavailable', r)
        self.assertEqual(r['refused_detail'], 'mode_excluded', r)
        self.assertIsNone(r['selected'], r)
        auth2 = self.auth('fixed', 'T', 's2',
                          [dp.ZCODE_FLASH_POOL_KEY, *dp.OPENCODE_POOL_KEYS],
                          runtime='zcode', model='GLM-5.3-Flash')
        r2 = self.plan(auth2)
        self.assertEqual(r2['refused'], None, r2)
        self.assertEqual(r2['selected']['pool_key'], dp.ZCODE_FLASH_POOL_KEY, r2)

    def test_executor_cn_never_crosses_region(self):
        self.build_store()
        # BW-WEAK-RESTRUCTURE-20261116: qodercn 系候选只属 strong/medium（CN Flash 已无档
        # 可派）——换档 medium 保持"executor 不跨区"验证；夜间 CN 内置 Max 可选。
        with dispatch_mode('medium'):
            auth = self.auth('executor', 'T', 's2', ALL_MAINS + ALL_FLASH,
                             executor='qodercn')
            r = self.plan(auth)
        self.assertEqual(r['selected_reason'], 'executor_region_main', r)
        self.assertTrue(r['selected']['pool_key'].startswith('qodercn:'), r)
        # 别区/别执行器绝不显示为 eligible，如实 not_authorized（含恢复枚举的 ZCode）。
        self.assertEqual(r['candidate_filters'][dp.OPENCODE_DEEPSEEK_POOL_KEY],
                         'not_authorized')
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'not_authorized')
        self.assertEqual(r['candidate_filters']['qoder:Qwen4-Max-Preview'], 'not_authorized')
        # BW-WEAK-RESTRUCTURE-20261116: CN 自定义 Max（用户禁用）与 CN Flash 如实
        # mode_excluded，绝不冒充可选/撤权。
        self.assertEqual(r['candidate_filters'][dp.CN_CUSTOM_MAX_POOL_KEY],
                         'mode_excluded', r)
        self.assertEqual(r['candidate_filters'][CN_FLASH], 'mode_excluded', r)

    def test_executor_falls_to_own_flash_never_cross(self):
        self.build_store()
        # BW-WEAK-RESTRUCTURE-20261116: 两个 Qwen3.8-Flash 已从所有档位移除——executor=
        # 'qoder' 自区主力（国际 Max）占满后不再有同区 Flash 兜底：strong 档如实
        # no_eligible_candidate 拒绝，绝不跨区落到 CN Flash / CN Max。
        with dispatch_mode('strong'):
            self.reserve('im', 'qoder', 'Qwen4-Max-Preview', executor='qoder')
            auth = self.auth('executor', 'T', 's2', ALL_MAINS + ALL_FLASH,
                             executor='qoder')
            r = self.plan(auth, now=T_DAY)
        self.assertEqual(r['selected'], None, r)
        self.assertEqual(r['refused'], 'no_eligible_candidate', r)
        self.assertEqual(r['candidate_filters'][dp.QFLASH_POOL_KEY], 'mode_excluded', r)
        self.assertEqual(r['candidate_filters'][CN_FLASH], 'not_authorized', r)

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
        self.assertEqual(r['candidate_filters']['qoder:Qwen4-Max-Preview'], 'not_authorized')
        # claude 与恢复枚举的 zcode 都如实不跨执行器（not_authorized）。
        self.assertEqual(r['candidate_filters'][dp.CLAUDE_POOL_KEY], 'not_authorized')
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'not_authorized')

    def test_executor_opencode_full_refuses_never_crosses(self):
        self.build_store()
        # 两 OpenCode 独立槽都真实占满后，executor='opencode' 规划如实拒绝，绝不借
        # 别的执行器/别的模型（包括同区 Flash）顶替。
        self.reserve('o1', 'opencode', dp.OPENCODE_DEEPSEEK_MODEL, executor='opencode')
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
        """实测缺陷场景 1（stage-selection-r1-round2.json）：真实失败后按纯 pool_key
        字符串入参、全新调用未携带上一轮回执（round_started=None），同 pool 存在 6 小时
        前的旧过期限额行——绝不清空失败列表、绝不轮转、绝不重选该 pool，下一候选必须是
        层=2 的 codebuddy:glm-5.3-flash。
        BW-WEAK-RESTRUCTURE-20261116: 轮转身份用例的限额池换用弱档可派且在限额短窗表
        内的 claude:deepseek-flash（原 codebuddy:deepseek-v4.1-flash 已移出弱档）。"""
        self.build_store()
        old_ref = T_NIGHT - timedelta(hours=6)  # 早已过期的历史限额窗口
        self._seed_limit(dp.CLAUDE_POOL_KEY, old_ref)
        allowed = [dp.CLAUDE_POOL_KEY, dp.CODEBUDDY_GLM_POOL_KEY]
        auth = self.auth('auto', 'T', 's2', allowed,
                         round_failed_candidates=[dp.CLAUDE_POOL_KEY])
        r = self.plan(auth, now=T_NIGHT)
        self.assertIsNone(r['refused'], r)
        self.assertIsNone(r['round_transition'], r)
        self.assertEqual(r['dispatch_round']['id'], 1, r)
        self.assertEqual(r['effective_round_failed_candidates'],
                         [dp.CLAUDE_POOL_KEY], r)
        self.assertEqual(r['candidate_filters'][dp.CLAUDE_POOL_KEY],
                         'already_tried_this_round', r)
        self.assertEqual(r['selected']['pool_key'], dp.CODEBUDDY_GLM_POOL_KEY, r)

    def test_old_expired_limit_never_clears_structured_network_failure_with_receipt(self):
        """实测缺陷场景 2（login-log-repair TLS 502）：当前轮失败是结构化**网络**事件、
        携带上一轮回执，旧限额行（早于本轮开始）过期——网络失败保留到本轮结束，绝不
        被限额轮转逻辑清除，下一候选必须是 codebuddy:glm-5.3-flash；回执含当前轮失败
        事件（标识/分类/时间）。
        BW-WEAK-RESTRUCTURE-20261116: 限额/失败池换用弱档可派的 claude:deepseek-flash。"""
        self.build_store()
        old_ref = T_NIGHT - timedelta(hours=6)
        self._seed_limit(dp.CLAUDE_POOL_KEY, old_ref)
        round_started = T_NIGHT - timedelta(minutes=10)
        failed_at = T_NIGHT - timedelta(minutes=5)
        allowed = [dp.CLAUDE_POOL_KEY, dp.CODEBUDDY_GLM_POOL_KEY]
        auth = self.auth('auto', 'T', 's2', allowed,
                         round_failed_candidates=[
                             self._event(dp.CLAUDE_POOL_KEY, failed_at)],
                         dispatch_round={'id': 3,
                                         'started_at_utc': dp._iso(round_started)})
        r = self.plan(auth, now=T_NIGHT)
        self.assertIsNone(r['refused'], r)
        self.assertIsNone(r['round_transition'], r)
        self.assertEqual(r['dispatch_round']['id'], 3, r)
        self.assertEqual(r['effective_round_failed_candidates'],
                         [dp.CLAUDE_POOL_KEY], r)
        self.assertEqual(r['candidate_filters'][dp.CLAUDE_POOL_KEY],
                         'already_tried_this_round', r)
        self.assertEqual(r['selected']['pool_key'], dp.CODEBUDDY_GLM_POOL_KEY, r)
        self.assertEqual(r['round_failed_events'][0]['failure_class'],
                         'network_failure', r)
        self.assertEqual(r['round_failed_events'][0]['failed_at_utc'],
                         dp._iso(failed_at), r)

    def test_active_limit_window_never_rotates_round(self):
        """限额**未过期**（窗口仍 active）：即便当前轮失败是结构化 quota_429 同一事件，
        也绝不触发轮转；轮号/失败列表原样，该 pool 本轮不可重选。
        BW-WEAK-RESTRUCTURE-20261116: 限额/失败池换用弱档可派的 claude:deepseek-flash。"""
        self.build_store()
        ref = T_NIGHT - timedelta(seconds=60)  # 60 秒前，1800 秒窗口内仍 active
        self._seed_limit(dp.CLAUDE_POOL_KEY, ref)
        allowed = [dp.CLAUDE_POOL_KEY, dp.CODEBUDDY_GLM_POOL_KEY]
        auth = self.auth('auto', 'T', 's2', allowed,
                         round_failed_candidates=[
                             self._event(dp.CLAUDE_POOL_KEY, ref,
                                         failure_class='quota_429')],
                         dispatch_round={'id': 2, 'started_at_utc': dp._iso(ref)})
        r = self.plan(auth, now=T_NIGHT)
        self.assertIsNone(r['refused'], r)
        self.assertIsNone(r['round_transition'], r)
        self.assertEqual(r['dispatch_round']['id'], 2, r)
        self.assertEqual(r['effective_round_failed_candidates'],
                         [dp.CLAUDE_POOL_KEY], r)
        self.assertEqual(r['candidate_filters'][dp.CLAUDE_POOL_KEY],
                         'main_force_limited', r)
        self.assertEqual(r['selected']['pool_key'], dp.CODEBUDDY_GLM_POOL_KEY, r)

    def test_same_event_expired_limit_rotates_and_keeps_other_failures(self):
        """正例（携带回执）：当前轮失败本身是结构化 quota_429、过期窗口正是同一事件、
        参照时刻不早于本轮开始——允许轮转；只清该同一限额事件，同轮的结构化网络失败
        与纯字符串失败一律保留（绝不清空整表）。
        BW-WEAK-RESTRUCTURE-20261116: 轮转池换用弱档可派的 claude:deepseek-flash；
        保留失败改用弱档可派的 opencode/codebuddy glm（原 QMAX 已移出弱档）。"""
        self.build_store()
        ref = T_NIGHT - timedelta(seconds=dp.QUOTA_LIMIT_WINDOW_SECONDS)  # 恰满 1800 秒
        self._seed_limit(dp.CLAUDE_POOL_KEY, ref)
        round_started = ref - timedelta(minutes=5)
        net_failed_at = ref - timedelta(minutes=1)
        allowed = [dp.CLAUDE_POOL_KEY, dp.OPENCODE_DEEPSEEK_POOL_KEY,
                   dp.CODEBUDDY_GLM_POOL_KEY]
        auth = self.auth('auto', 'T', 's2', allowed,
                         round_failed_candidates=[
                             self._event(dp.CLAUDE_POOL_KEY, ref,
                                         failure_class='quota_429'),
                             self._event(dp.OPENCODE_DEEPSEEK_POOL_KEY, net_failed_at),
                             dp.CODEBUDDY_GLM_POOL_KEY],
                         dispatch_round={'id': 4,
                                         'started_at_utc': dp._iso(round_started)})
        r = self.plan(auth, now=T_NIGHT)
        self.assertIsNone(r['refused'], r)
        self.assertEqual(r['round_transition']['from_round'], 4, r)
        self.assertEqual(r['round_transition']['to_round'], 5, r)
        self.assertEqual(r['round_transition']['reason'],
                         'structured_limit_window_expired_no_inflight')
        self.assertEqual(sorted(r['effective_round_failed_candidates']),
                         sorted([dp.OPENCODE_DEEPSEEK_POOL_KEY,
                                 dp.CODEBUDDY_GLM_POOL_KEY]), r)
        trig = r['round_transition']['trigger_events']
        self.assertEqual(len(trig), 1, trig)
        self.assertEqual(trig[0]['pool_key'], dp.CLAUDE_POOL_KEY)
        self.assertEqual(trig[0]['failure_class'], 'quota_429')
        self.assertEqual(trig[0]['failed_at_utc'], dp._iso(ref))
        self.assertEqual(trig[0]['limit_reference_utc'], dp._iso(ref))
        # 到期后 claude 恢复原优先级层（弱档层=3；同轮 opencode/glm 仍失败保留被剔，
        # 真实尝试仍由正式入口复核），其余失败保留。
        self.assertEqual(r['selected']['pool_key'], dp.CLAUDE_POOL_KEY, r)
        self.assertEqual(r['candidate_filters'][dp.OPENCODE_DEEPSEEK_POOL_KEY],
                         'already_tried_this_round', r)
        self.assertEqual(r['candidate_filters'][dp.CODEBUDDY_GLM_POOL_KEY],
                         'already_tried_this_round', r)

    def test_expired_limit_without_receipt_same_event_rotates(self):
        """无上一轮回执（round_started=None）+ 结构化 quota_429 同一事件已到期：允许
        轮转（1→2）；这与旧缺陷的区别是身份绑定成立，而非任何旧窗口都能清表。
        BW-WEAK-RESTRUCTURE-20261116: 授权集合收窄为仅弱档可派且在限额短窗表内的
        claude:deepseek-flash（层=3），验证轮转清空失败集合后它恢复可选。"""
        self.build_store()
        ref = T_NIGHT - timedelta(seconds=dp.QUOTA_LIMIT_WINDOW_SECONDS)
        self._seed_limit(dp.CLAUDE_POOL_KEY, ref)
        allowed = [dp.CLAUDE_POOL_KEY]
        auth = self.auth('auto', 'T', 's2', allowed,
                         round_failed_candidates=[
                             self._event(dp.CLAUDE_POOL_KEY, ref,
                                         failure_class='quota_429')])
        r = self.plan(auth, now=T_NIGHT)
        self.assertIsNone(r['refused'], r)
        self.assertEqual(r['dispatch_round']['id'], 2, r)
        self.assertEqual(r['round_transition']['reason'],
                         'structured_limit_window_expired_no_inflight', r)
        self.assertEqual(r['effective_round_failed_candidates'], [], r)
        self.assertEqual(r['selected']['pool_key'], dp.CLAUDE_POOL_KEY, r)

    def test_expired_limit_event_time_mismatch_never_rotates(self):
        """身份绑定：结构化 quota_429 事件时间与其 pool 上过期窗口的参照时刻**不是同一
        事件**（当前失败晚于旧窗口）——旧窗口绝不清除当前失败、绝不轮转。
        BW-WEAK-RESTRUCTURE-20261116: 限额/失败池换用弱档可派的 claude:deepseek-flash。"""
        self.build_store()
        old_ref = T_NIGHT - timedelta(hours=6)
        self._seed_limit(dp.CLAUDE_POOL_KEY, old_ref)
        failed_at = T_NIGHT - timedelta(minutes=5)  # 当前轮真实失败，晚于旧窗口参照
        round_started = T_NIGHT - timedelta(minutes=10)
        allowed = [dp.CLAUDE_POOL_KEY, dp.CODEBUDDY_GLM_POOL_KEY]
        auth = self.auth('auto', 'T', 's2', allowed,
                         round_failed_candidates=[
                             self._event(dp.CLAUDE_POOL_KEY, failed_at,
                                         failure_class='quota_429')],
                         dispatch_round={'id': 9,
                                         'started_at_utc': dp._iso(round_started)})
        r = self.plan(auth, now=T_NIGHT)
        self.assertIsNone(r['refused'], r)
        self.assertIsNone(r['round_transition'], r)
        self.assertEqual(r['dispatch_round']['id'], 9, r)
        self.assertEqual(r['effective_round_failed_candidates'],
                         [dp.CLAUDE_POOL_KEY], r)
        self.assertEqual(r['candidate_filters'][dp.CLAUDE_POOL_KEY],
                         'already_tried_this_round', r)
        self.assertEqual(r['selected']['pool_key'], dp.CODEBUDDY_GLM_POOL_KEY, r)

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
    """BW-WEAK-RESTRUCTURE-20261116（用户明确指令，当前权威）：弱档新表 5 池 4 层——
    两 OpenCode 免费模型(1) > CodeBuddy GLM(2) > claude(3，倒数第二) >
    zcode:GLM-5.3-Flash(4，最后)。全部 Qoder 池与 codebuddy:deepseek-v4.1-flash 已移出
    弱档；Max 夜间层序改由 medium/strong 承载（Max=1 > claude=2 > ZCode=3）。"""

    def test_pool_priority_authoritative_numbers(self):
        # BW-WEAK-RESTRUCTURE-20261116: dp.POOL_PRIORITY 是 weak 档兼容整表（单一来源
        # dispatch_mode.WEAK_PRIORITY）：新弱档 5 池 4 层；被移出弱档的池不在表内
        # （priority_for_pool 返回 None）。medium/strong 层序另测。
        self.assertEqual(dp.POOL_PRIORITY, dict(dm.WEAK_PRIORITY))
        self.assertEqual(dp.POOL_PRIORITY, {
            dp.OPENCODE_DEEPSEEK_POOL_KEY: 1,
            dp.CODEBUDDY_GLM_POOL_KEY: 2,
            dp.CLAUDE_POOL_KEY: 3,
            dp.ZCODE_FLASH_POOL_KEY: 4})
        self.assertEqual(dp.priority_for_pool(dp.ZCODE_POOL_KEY, 'weak'), None)
        self.assertEqual(dp.priority_for_pool(dp.CODEBUDDY_POOL_KEY, 'weak'), None)
        self.assertEqual(dp.priority_for_pool(dp.ZCODE_FLASH_POOL_KEY, 'weak'), 4)
        # medium/strong 的 Max 夜间层序（权威数字）。
        self.assertEqual(dp.priority_for_pool(dp.QMAX_POOL_KEY, 'medium'), 1)
        self.assertEqual(dp.priority_for_pool(dp.CN_MAX_POOL_KEY, 'medium'), 1)
        self.assertEqual(dp.priority_for_pool(dp.CLAUDE_POOL_KEY, 'medium'), 2)
        self.assertEqual(dp.priority_for_pool(dp.ZCODE_POOL_KEY, 'medium'), 3)
        self.assertEqual(dp.priority_for_pool(dp.SONUS_POOL_KEY, 'strong'), 1)

    def test_auto_night_max_before_codebuddy(self):
        # BW-WEAK-RESTRUCTURE-20261116: 旧"层1 Max 先于 CodeBuddy deepseek"场景里两池
        # 已分属不同档位（Max 只在 medium/strong 可派、codebuddy deepseek 无档可派）——
        # 层序保持本意改在 medium 验证（Max=1 先于 claude=2），并补弱档新表内层序
        # （CodeBuddy GLM=2 先于 claude=3 先于 zcode GLM-5.3-Flash=4）。
        self.build_store()
        with dispatch_mode('medium'):
            allowed = [dp.QMAX_POOL_KEY, dp.CLAUDE_POOL_KEY]
            r = self.plan(self.auth('auto', 'T', 's2', allowed), now=T_NIGHT)
            self.assertIsNone(r['refused'], r)
            # 授权集合内 QMAX 层=1 高于 claude 层=2。
            self.assertEqual(r['selected']['pool_key'], dp.QMAX_POOL_KEY, r)
            self.assertEqual(r['selected_priority'], 1, r)
        with dispatch_mode('weak'):
            allowed = [dp.CODEBUDDY_GLM_POOL_KEY, dp.CLAUDE_POOL_KEY,
                       dp.ZCODE_FLASH_POOL_KEY]
            r = self.plan(self.auth('auto', 'T', 's2', allowed), now=T_NIGHT)
            self.assertIsNone(r['refused'], r)
            self.assertEqual(r['selected']['pool_key'], dp.CODEBUDDY_GLM_POOL_KEY, r)
            self.assertEqual(r['selected_priority'], 2, r)

    def test_auto_order_claude_then_codebuddy_then_opencode(self):
        # BW-WEAK-RESTRUCTURE-20261116: 同一授权集合内按本轮失败逐层下移，selected_
        # priority 与弱档优先级表完全一致：opencode(1) → codebuddy glm(2) → claude(3)
        # → zcode GLM-5.3-Flash(4)。
        self.build_store()
        allowed = [dp.OPENCODE_DEEPSEEK_POOL_KEY, dp.CODEBUDDY_GLM_POOL_KEY,
                   dp.CLAUDE_POOL_KEY, dp.ZCODE_FLASH_POOL_KEY]
        r = self.plan(self.auth('auto', 'T', 's2', allowed), now=T_NIGHT)
        self.assertIsNone(r['refused'], r)
        self.assertEqual(r['selected']['pool_key'], dp.OPENCODE_DEEPSEEK_POOL_KEY, r)
        self.assertEqual(r['selected_priority'], 1, r)
        r2 = self.plan(self.auth('auto', 'T', 's2', allowed,
                                 round_failed_candidates=[dp.OPENCODE_DEEPSEEK_POOL_KEY]),
                       now=T_NIGHT)
        self.assertEqual(r2['selected']['pool_key'], dp.CODEBUDDY_GLM_POOL_KEY, r2)
        self.assertEqual(r2['selected_priority'], 2, r2)
        r3 = self.plan(self.auth('auto', 'T', 's2', allowed,
                                 round_failed_candidates=[dp.OPENCODE_DEEPSEEK_POOL_KEY,
                                                          dp.CODEBUDDY_GLM_POOL_KEY]),
                       now=T_NIGHT)
        self.assertEqual(r3['selected']['pool_key'], dp.CLAUDE_POOL_KEY, r3)
        self.assertEqual(r3['selected_priority'], 3, r3)
        r4 = self.plan(self.auth('auto', 'T', 's2', allowed,
                                 round_failed_candidates=[dp.OPENCODE_DEEPSEEK_POOL_KEY,
                                                          dp.CODEBUDDY_GLM_POOL_KEY,
                                                          dp.CLAUDE_POOL_KEY]),
                       now=T_NIGHT)
        self.assertEqual(r4['selected']['pool_key'], dp.ZCODE_FLASH_POOL_KEY, r4)
        self.assertEqual(r4['selected_priority'], 4, r4)


class WindowAndLimitTests(_Base):
    def test_day_peak_gates_close_max_and_claude_select_opencode(self):
        self.build_store()
        r = self.plan(self.auth('auto', 'T', 's2', LEGACY_MAINS + ALL_FLASH), now=T_DAY)
        # BW-WEAK-TIER-ENFORCE-20261122: 白天弱档纯优先级队列、无任何时段门——claude
        # 合格；全部 Qoder 池与 zcode:GLM-5.3 已移出弱档 → mode_excluded（档位硬策略）。
        self.assertEqual(r['candidate_filters'][dp.CLAUDE_POOL_KEY], 'eligible', r)
        self.assertEqual(r['candidate_filters']['qoder:Qwen4-Max-Preview'],
                         'mode_excluded')
        self.assertEqual(r['candidate_filters'][dp.CN_MAX_POOL_KEY], 'mode_excluded')
        self.assertEqual(r['candidate_filters'][dp.CN_CUSTOM_MAX_POOL_KEY],
                         'mode_excluded')
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'mode_excluded', r)
        self.assertEqual(r['candidate_filters'][dp.ZCODE_FLASH_POOL_KEY], 'eligible', r)
        self.assertIn(r['selected']['pool_key'], dp.OPENCODE_POOL_KEYS, r)
        # BW-WEAK-RESTRUCTURE-20261116: 旧 zcode:GLM-5.3 占位已不在弱档可派集合，改占
        # 一个 OpenCode 独立槽；同层另一个 OpenCode 仍被选中（同层在途公平）。
        self.reserve('o1', 'opencode', dp.OPENCODE_DEEPSEEK_MODEL, executor='opencode')
        r2 = self.plan(self.auth('auto', 'T2', 's2', LEGACY_MAINS + ALL_FLASH),
                       task='T2', now=T_DAY)
        self.assertEqual(r2['selected']['pool_key'], dp.CLAUDE_POOL_KEY, r2)
        # BW-WEAK-TIER-ENFORCE-20261122: weak 无时段门——claude 合格。
        self.assertEqual(r2['candidate_filters'][dp.CLAUDE_POOL_KEY], 'eligible', r2)

    def test_weak_auto_prefers_flash_to_zcode(self):
        self.build_store()
        # BW-WEAK-RESTRUCTURE-20261116: 弱档 ZCode 只剩 GLM-5.3-Flash（层=4，最后），
        # zcode:GLM-5.3 与两区 Qwen3.8-Flash 已移出弱档（mode_excluded）；AUTO 在存在
        # 更高层候选时绝不落到 GLM-5.3-Flash。
        for higher in (dp.CODEBUDDY_GLM_POOL_KEY, dp.CLAUDE_POOL_KEY):
            r = self.plan(self.auth('auto', 'T', 's2', [dp.ZCODE_FLASH_POOL_KEY, higher]),
                          now=T_NIGHT)
            self.assertEqual(r['selected']['pool_key'], higher, r)
            self.assertEqual(r['next_candidate']['pool_key'], dp.ZCODE_FLASH_POOL_KEY, r)
        # 只授权弱档 zcode 两池时：GLM-5.3-Flash 真实可选，zcode:GLM-5.3 如实排除。
        r = self.plan(self.auth('auto', 'T', 's2', [dp.ZCODE_FLASH_POOL_KEY,
                                                   dp.ZCODE_POOL_KEY]), now=T_NIGHT)
        self.assertEqual(r['selected']['pool_key'], dp.ZCODE_FLASH_POOL_KEY, r)
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'mode_excluded', r)
        # BW-WEAK-RESTRUCTURE-20261116: 两区 Qwen3.8-Flash 兜底已死——weak 下一律
        # mode_excluded，绝不参与任何 AUTO 选择。
        r = self.plan(self.auth('auto', 'T', 's2',
                                [dp.ZCODE_FLASH_POOL_KEY, *dp.OVERFLOW_KEYS]),
                      now=T_NIGHT)
        for pk in dp.OVERFLOW_KEYS:
            self.assertEqual(r['candidate_filters'][pk], 'mode_excluded', r)
        self.assertEqual(r['selected']['pool_key'], dp.ZCODE_FLASH_POOL_KEY, r)

    def test_night_admits_gated_max_and_keeps_fairness(self):
        self.build_store()
        # BW-WEAK-RESTRUCTURE-20261116: Max 夜间门现在只属于 medium/strong 口径——夜间
        # medium 档 Max/claude/ZCode 照旧如实 eligible；weak 档已无 Max 与 zcode:GLM-5.3
        # 候选（mode_excluded），claude 与弱档新表池（含 zcode GLM-5.3-Flash）夜间全部
        # 如实 eligible。
        with dispatch_mode('medium'):
            r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH),
                          now=T_NIGHT)
            self.assertEqual(r['candidate_filters']['qoder:Qwen4-Max-Preview'],
                             'eligible')
            self.assertEqual(r['candidate_filters'][dp.CLAUDE_POOL_KEY], 'eligible')
            # BW-ZCODE-RESTORE-1-20261022：ZCode 恢复为逐候选枚举的现役候选（夜间亦然）。
            self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'eligible')
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH), now=T_NIGHT)
        self.assertEqual(r['candidate_filters'][dp.CLAUDE_POOL_KEY], 'eligible')
        self.assertEqual(r['candidate_filters'][dp.ZCODE_FLASH_POOL_KEY], 'eligible')
        self.assertEqual(r['candidate_filters']['qoder:Qwen4-Max-Preview'],
                         'mode_excluded')
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'mode_excluded')

    def test_explicit_limit_no_longer_blocks_new_round(self):
        """BW-SIMPLE-PRIORITY-RETRY-S3 新用户政策：历史明确限额不再挡一个正常新轮次的
        真实尝试——QMAX 带限额标记在本轮仍 eligible，绝不因历史标记永久/到期后剔除；
        真实 429/失败才落诊断、成功/失败 CAS 结算不变。
        BW-WEAK-RESTRUCTURE-20261116: qoder Max 只属 strong/medium——换档 medium 保持
        本意（弱档已无 Max 候选）。"""
        self.build_store()
        self.set_limit('qoder:Qwen4-Max-Preview')
        with dispatch_mode('medium'):
            r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH),
                          now=T_NIGHT)
        self.assertEqual(r['candidate_filters']['qoder:Qwen4-Max-Preview'], 'eligible')
        self.assertNotEqual(r['selected']['pool_key'], None)

    def test_capacity_full_filter_and_fallback(self):
        self.build_store()
        # BW-WEAK-RESTRUCTURE-20261116: 弱档新表下验证"满→如实 capacity_full、按层回退"：
        # 占满两 OpenCode(层1)、CodeBuddy GLM(层2)、claude(层3) 全部槽位后，最高可用层
        # 落到 zcode:GLM-5.3-Flash(层4)；被移出的旧主力/两区 Flash 绝不参与
        # （mode_excluded，绝不借道兜底）。
        # BW-WEAK-TIER-ENFORCE-20261122: 弱档纯优先级队列——真实 reserve 必须按队序
        # （层1 两 OpenCode → 层2 CodeBuddy GLM → 层3 claude×2）占位，跳队请求会被
        # routing_required 到队首而非 claim。
        self.reserve('o1', 'opencode', dp.OPENCODE_DEEPSEEK_MODEL, executor='opencode')
        self.reserve('g1', 'codebuddy', 'glm-5.3-flash', executor='codebuddy')
        self.reserve('a', 'claude', 'deepseek-flash', executor='claude')
        self.reserve('b', 'claude', 'deepseek-flash', executor='claude')
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH), now=T_NIGHT)
        self.assertEqual(r['candidate_filters'][dp.CLAUDE_POOL_KEY], 'capacity_full')
        self.assertEqual(r['candidate_filters'][dp.CODEBUDDY_GLM_POOL_KEY],
                         'capacity_full')
        self.assertEqual(r['candidate_filters'][dp.OPENCODE_DEEPSEEK_POOL_KEY],
                         'capacity_full')
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'mode_excluded')
        self.assertEqual(r['candidate_filters'][dp.QFLASH_POOL_KEY], 'mode_excluded')
        self.assertEqual(r['selected']['pool_key'], dp.ZCODE_FLASH_POOL_KEY, r)


class NightAutoCustomMaxRegression(_Base):
    """BW-CANDIDATE-SCOPE-S1 需求 3：独立夜间 AUTO 候选回归。绝不查询额度、不扩权、
    不把某 runtime:model 失败泛化成整个 executor，也不把 not_authorized 说成用户撤权。"""

    def test_night_auto_no_longer_drops_builtins_with_history_limit(self):
        """BW-SIMPLE-PRIORITY-RETRY-S3：历史明确限额不再挡新轮——内置 CN/国际 Max 即便带
        限额标记，本轮仍 eligible，绝不因历史标记把它们剔除；按既有 Max>Claude>Z 优先级
        选择最高优先层的空位池。allowed 全集与逐候选原因如实保留，不缩权、不假装撤权。
        BW-WEAK-RESTRUCTURE-20261116: 内置 Max 只属 strong/medium——换档 medium 保持
        本意（弱档已无 Max 候选）。"""
        self.build_store()
        self.set_limit('qoder:Qwen4-Max-Preview')
        self.set_limit('qodercn:Qwen4-Max-Preview')
        allowed = LEGACY_MAINS + ALL_FLASH
        with dispatch_mode('medium'):
            r = self.plan(self.auth('auto', 'T', 's2', allowed), now=T_NIGHT)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(sorted(r['allowed_candidates']), sorted(allowed))  # 不缩权
        self.assertEqual(r['candidate_filters']['qoder:Qwen4-Max-Preview'], 'eligible')
        self.assertEqual(r['candidate_filters']['qodercn:Qwen4-Max-Preview'], 'eligible')
        self.assertEqual(r['candidate_filters'][dp.CN_CUSTOM_MAX_POOL_KEY], 'mode_excluded')
        self.assertNotIn('explicitly_limited', set(r['candidate_filters'].values()))
        # 最高优先层（两内置 Max）有空位即被选中，绝不下探 Claude/Flash/Z；
        # BW-WEAK-RESTRUCTURE-20261116: CN 自定义 Max 在所有档位用户禁用（上一行已如实
        # mode_excluded），可选中集合只剩两内置 Max。
        self.assertIn(r['selected']['pool_key'], ['qoder:Qwen4-Max-Preview',
                                                   'qodercn:Qwen4-Max-Preview'], r)
        self.assertNotEqual(r['selected']['pool_key'], dp.CLAUDE_POOL_KEY)
        self.assertNotIn(r['selected']['pool_key'], ALL_FLASH)

    def test_auto_narrow_cn_scope_stays_cn_others_not_authorized(self):
        """合法用户把 AUTO 范围收窄到只 CN：选择仍在 CN 内，其余区/池如实 not_authorized
        ——这只表示不在本轮输入里，绝不伪造"用户撤权"，也绝不偷偷扩权到 qoder/claude。
        BW-WEAK-RESTRUCTURE-20261116: CN 内置 Max 只属 strong/medium——换档 medium；
        CN Flash 已无任何档位可派 → mode_excluded。"""
        self.build_store()
        cn_scope = CN_MAINS + [CN_FLASH]
        with dispatch_mode('medium'):
            r = self.plan(self.auth('auto', 'T', 's2', cn_scope), now=T_NIGHT)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(sorted(r['allowed_candidates']), sorted(cn_scope))
        self.assertTrue(r['selected']['pool_key'].startswith('qodercn:'), r)
        self.assertEqual(r['candidate_filters']['qoder:Qwen4-Max-Preview'], 'not_authorized')
        self.assertEqual(r['candidate_filters'][dp.CLAUDE_POOL_KEY], 'not_authorized')
        # 别区 OpenCode 与 ZCode 都如实 not_authorized（不在本轮输入里≠用户撤权）。
        self.assertEqual(r['candidate_filters'][dp.OPENCODE_DEEPSEEK_POOL_KEY],
                         'not_authorized')
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'not_authorized')
        # BW-WEAK-RESTRUCTURE-20261116: CN Flash 如实 mode_excluded（绝不冒充可选）；
        # CN 自定义 Max 用户禁用语义原样保留。
        self.assertEqual(r['candidate_filters'][CN_FLASH], 'mode_excluded', r)
        self.assertEqual(r['candidate_filters'][dp.CN_CUSTOM_MAX_POOL_KEY],
                         'mode_excluded', r)

    def test_only_flash_input_marks_mains_not_authorized_without_fake_revocation(self):
        """错误输入只给 Flash 时，主力的 not_authorized 只反映未列入输入（不是撤权）；
        合法窄范围不被偷偷扩到主力。
        BW-WEAK-RESTRUCTURE-20261116: 两个 Qwen3.8-Flash 已从所有档位移除——只给 Flash
        的输入不再可能选中它：如实 no_eligible_candidate 拒绝、selected=None，绝不偷偷
        扩权到主力；主力仍如实 not_authorized（未列入输入≠撤权），Flash 如实
        mode_excluded（不是撤权）。"""
        self.build_store()
        r = self.plan(self.auth('auto', 'T', 's2', [CN_FLASH]), now=T_NIGHT)
        self.assertEqual(r['refused'], 'no_eligible_candidate', r)
        self.assertIsNone(r['selected'], r)
        self.assertEqual(r['candidate_filters'][CN_FLASH], 'mode_excluded', r)
        self.assertEqual(r['candidate_filters']['qoder:Qwen4-Max-Preview'], 'not_authorized')
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
        # BW-WEAK-RESTRUCTURE-20261116: qoder Max 只属 strong/medium——换档 strong 保持
        # 在途守卫验证（守卫先于档位过滤，语义不变）。
        with dispatch_mode('strong'):
            self.reserve('T', 'qoder', 'Qwen4-Max-Preview', executor='qoder')
            r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH))
        self.assertEqual(r['refused'], 'task_in_flight')

    def test_workspace_conflict_rejects_whole_item(self):
        self.build_store()
        shared = self._ws('shared')
        # BW-WEAK-RESTRUCTURE-20261116: qoder Max 只属 strong/medium——换档 strong 保持
        # 工作区冲突守卫验证（守卫先于档位过滤，语义不变）。
        with dispatch_mode('strong'):
            self.reserve('a', 'qoder', 'Qwen4-Max-Preview', ws=shared, executor='qoder')
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
        parent = self.auth('auto', 'T', 's2', ['qoder:Qwen4-Max-Preview'])
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
        # BW-WEAK-RESTRUCTURE-20261116: 原 base 指定的 CN Flash 已无任何档位可派——
        # "交集不扩权、选中 base 指定池"的本意改用弱档可派的 zcode:GLM-5.3-Flash 验证。
        auth = self.auth('temporary_fallback', 'T', 's1', [dp.ZCODE_FLASH_POOL_KEY],
                         fallback_scope={'task_id': 'T', 'stage': 's1'},
                         runtime='zcode', model='GLM-5.3-Flash',
                         parent_authorization=parent)
        prev = {'task_id': 'T', 'state': 'finished', 'acceptance_reviewed': True,
                'stage': 's1'}
        r = self.plan(auth, previous=prev, stage='s2')
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['allowed_candidates'], [dp.ZCODE_FLASH_POOL_KEY])  # 交集不扩权
        self.assertEqual(r['selected']['pool_key'], dp.ZCODE_FLASH_POOL_KEY, r)

    def test_same_stage_fallback_uses_designated_flash(self):
        # BW-WEAK-RESTRUCTURE-20261116: 两个 Qwen3.8-Flash 已从所有档位移除——同阶段指定
        # Flash 临时降级不再可能：如实 temporary_candidate_unavailable（detail=
        # mode_excluded）拒绝、selected=None，绝不静默改派其它模型；候选集合中的
        # OpenCode 能力冲突标注保持原样可辨。
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
        self.assertEqual(r['refused'], 'temporary_candidate_unavailable', r)
        self.assertEqual(r['refused_detail'], 'mode_excluded', r)
        self.assertIsNone(r['selected'], r)
        self.assertFalse(r['session_reuse_allowed'])

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
        # BW-WEAK-RESTRUCTURE-20261116: 原 fixed CN Flash 组合已无档可派——会话复用语义
        # 改用弱档可派的 zcode:GLM-5.3-Flash 精确组合验证，三种情形断言原样保留。
        auth = self.auth('fixed', 'T', 's2', [dp.ZCODE_FLASH_POOL_KEY],
                         runtime='zcode', model='GLM-5.3-Flash')
        base = {'state': 'finished', 'acceptance_reviewed': True, 'stage': 's1',
                'runtime': 'zcode', 'model': 'GLM-5.3-Flash'}
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
        # BW-WEAK-RESTRUCTURE-20261116: qoder Max 只属 strong/medium——换档 strong 保持
        # WAL 可见性验证（占位组合与守卫语义均不变）。
        writer = dp.connect(self.store)
        try:
            dp._rotation_row(writer)
            with dispatch_mode('strong'):
                r = dp.reserve(self.store, task_id='Q', runtime='qoder',
                               model='Qwen4-Max-Preview',
                               workspace=self._ws('Q'), prompt_sha256='p',
                               executor='qoder', now=T_NIGHT, _preclaim=False)
                self.assertTrue(r.get('reserved') or r.get('allowed'), r)
                self.assertTrue(Path(self.store + '-wal').exists())
                self.assertTrue(Path(self.store + '-shm').exists())
                plan = self.plan(self.auth('auto', 'Q', 's2', ALL_MAINS + ALL_FLASH),
                                 task='Q')
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
        """白天（工作日高峰）精确 Edit/Bash 需求：能力不兼容的候选如实
        permission_incompatible，能力兼容且无门的最高层被选中。
        BW-WEAK-RESTRUCTURE-20261116: 新弱档白天只剩 claude 高峰门；两 OpenCode 与弱档
        zcode GLM-5.3-Flash 无逐文件能力 → permission_incompatible，CodeBuddy GLM 能力
        兼容且无门（层=2）→ 选中；claude 能力兼容但被高峰门关住（window_closed）；
        全部 Qoder 池与 zcode:GLM-5.3 档位排除（mode_excluded 先于能力/时段门），
        两区 Qwen3.8-Flash 兜底已死。旧 flash_fallback 路线不再存在。"""
        self.build_store()
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH),
                      now=T_DAY, permission_grants=self.FINE)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['permission_requirements'],
                         {'fine_grained': True, 'read_dirs': False})
        self.assertEqual(r['selected']['pool_key'], dp.CODEBUDDY_GLM_POOL_KEY, r)
        self.assertEqual(r['selected_reason'], 'main_force_selected', r)
        self.assertEqual(r['selected_priority'], 2, r)
        # 精确需求下无逐文件能力的现役候选如实 permission_incompatible。
        self.assertEqual(r['candidate_filters'][dp.OPENCODE_DEEPSEEK_POOL_KEY],
                         'permission_incompatible')
        self.assertEqual(r['candidate_filters'][dp.ZCODE_FLASH_POOL_KEY],
                         'permission_incompatible')
        # BW-WEAK-TIER-ENFORCE-20261122: weak 无时段门——claude 合格（队列接管排序）。
        self.assertEqual(r['candidate_filters'][dp.CLAUDE_POOL_KEY], 'eligible')
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'mode_excluded', r)
        self.assertEqual(r['candidate_filters'][dp.QFLASH_POOL_KEY], 'mode_excluded')
        self.assertNotEqual(r['selected']['runtime'], 'zcode')
        self._assert_capability_table_untouched()

    def test_day_same_window_without_exact_rules_z_still_eligible(self):
        """弱档按时段和权限筛选，在 ZCode 前选用可用的 OpenCode。
        BW-WEAK-RESTRUCTURE-20261116: 无精确权限需求时 zcode:GLM-5.3 已移出弱档
        （mode_excluded），弱档 ZCode 只剩 GLM-5.3-Flash（层=4，仍 eligible）；
        旧国际 qoder Max 占位改占一个 OpenCode 独立槽。"""
        self.build_store()
        self.reserve('o1', 'opencode', dp.OPENCODE_DEEPSEEK_MODEL, executor='opencode')
        r = self.plan(self.auth('auto', 'T', 's2', LEGACY_MAINS + ALL_FLASH), now=T_DAY)
        self.assertEqual(r['selected']['pool_key'], dp.CLAUDE_POOL_KEY, r)
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'mode_excluded', r)
        self.assertEqual(r['candidate_filters'][dp.ZCODE_FLASH_POOL_KEY], 'eligible', r)
        self.assertEqual(r['permission_requirements'],
                         {'fine_grained': False, 'read_dirs': False})

    def test_fixed_z_with_exact_requirement_refused_not_swapped(self):
        """fixed 明确 ZCode 且本轮要求精确 Edit+Bash：ZCode 无逐文件能力，本轮唯一
        授权候选如实 permission_incompatible——诚实拒绝（fixed_candidate_unavailable），
        绝不偷偷换执行器。
        BW-WEAK-RESTRUCTURE-20261116: zcode:GLM-5.3 只属 strong/medium——换档 strong
        保持能力冲突拒绝的本意；weak 档下档位硬排除先于能力过滤（detail=mode_excluded）。"""
        self.build_store()
        auth = self.auth('fixed', 'T', 's2', [dp.ZCODE_POOL_KEY],
                         runtime='zcode', model='GLM-5.3')
        with dispatch_mode('strong'):
            r = self.plan(auth, permission_grants=self.FINE)
        self.assertEqual(r['refused'], 'fixed_candidate_unavailable', r)
        self.assertEqual(r['refused_detail'], 'permission_incompatible', r)
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY],
                         'permission_incompatible', r)
        self.assertIsNone(r['selected'])
        # BW-WEAK-RESTRUCTURE-20261116: weak 档 zcode:GLM-5.3 已移出——mode_excluded
        # 如实呈现，绝不冒充能力冲突/时段/容量。
        r2 = self.plan(auth, permission_grants=self.FINE)
        self.assertEqual(r2['refused'], 'fixed_candidate_unavailable', r2)
        self.assertEqual(r2['refused_detail'], 'mode_excluded', r2)
        self.assertEqual(r2['candidate_filters'][dp.ZCODE_POOL_KEY], 'mode_excluded', r2)

    def test_read_dirs_requirement_is_independent_capability_filter(self):
        """外部 read_dirs 需求走独立能力过滤：只剔除无 read_dirs 能力的候选，与
        fine_grained 需求分别可辨。BW-WEAK-RESTRUCTURE-20261116: 新弱档白天下弱档可派
        池全部无 read_dirs 能力（两 OpenCode/CodeBuddy GLM/弱档 zcode GLM-5.3-Flash）
        → permission_incompatible，claude 有 read_dirs 能力但被高峰门关住（与能力过滤
        各自如实标注）→ 如实 no_eligible_candidate 拒绝，绝不把 mode_excluded 的
        qoder/qodercn 系候选偷偷拉回来；有 read_dirs 能力的候选改在 strong（无时段门）
        验证真实选中。原 codebuddy deepseek 与 zcode:GLM-5.3 已移出弱档（mode_excluded）。"""
        self.build_store()
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH), now=T_DAY,
                      permission_grants={'edits': [], 'bash': [],
                                         'read_dirs': ['D:/refs']})
        self.assertEqual(r['permission_requirements'],
                         {'fine_grained': False, 'read_dirs': True})
        self.assertEqual(r['candidate_filters'][dp.CODEBUDDY_GLM_POOL_KEY],
                         'permission_incompatible')
        self.assertEqual(r['candidate_filters'][dp.ZCODE_FLASH_POOL_KEY],
                         'permission_incompatible')
        self.assertEqual(r['candidate_filters'][dp.OPENCODE_DEEPSEEK_POOL_KEY],
                         'permission_incompatible')
        # BW-WEAK-TIER-ENFORCE-20261122: weak 无时段门，且 claude 支持 read_dirs 授权
        # （能力过滤独立于档位/时段——本用例的本意）——claude 合格并当选；其余弱档池
        # permission_incompatible，档外池 mode_excluded。
        self.assertEqual(r['candidate_filters'][dp.CLAUDE_POOL_KEY], 'eligible')
        self.assertEqual(r['candidate_filters'][dp.CODEBUDDY_POOL_KEY], 'mode_excluded')
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'mode_excluded')
        self.assertIsNone(r['refused'], r)
        self.assertEqual(r['selected']['pool_key'], dp.CLAUDE_POOL_KEY, r)
        # BW-WEAK-RESTRUCTURE-20261116: read_dirs 能力候选（qoder Max 层=1）在 strong
        # 白天（无时段门）真实选中——能力过滤与档位/时段各自如实、互不冒充。
        with dispatch_mode('strong'):
            r2 = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH), now=T_DAY,
                           permission_grants={'edits': [], 'bash': [],
                                              'read_dirs': ['D:/refs']})
        self.assertEqual(r2['refused'], None, r2)
        self.assertEqual(r2['candidate_filters'][dp.ZCODE_POOL_KEY],
                         'permission_incompatible', r2)
        self.assertEqual(r2['selected']['pool_key'], dp.QMAX_POOL_KEY, r2)
        self.assertEqual(r2['selected_reason'], 'main_force_selected', r2)
        self._assert_capability_table_untouched()

    def test_missing_qoder_authorization_never_expands_via_permission_filter(self):
        """能力过滤只作用于授权集合内候选：缺 Qoder 授权时精确需求绝不把候选扩到
        Qoder，非授权池仍如实 not_authorized，需求兼容也不构成授权。
        BW-WEAK-RESTRUCTURE-20261116: 授权集合改用弱档可派非 Qoder 池（codebuddy GLM +
        opencode）——精确需求下选择仍留在非 Qoder 候选内（能力兼容的 codebuddy GLM
        选中），Qoder 系与未授权 ZCode 一律 not_authorized（过滤不扩权）。"""
        self.build_store()
        auth = self.auth('auto', 'T', 's2', [dp.CODEBUDDY_GLM_POOL_KEY,
                                             dp.OPENCODE_DEEPSEEK_POOL_KEY])
        r = self.plan(auth, permission_grants=self.FINE)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected']['pool_key'], dp.CODEBUDDY_GLM_POOL_KEY, r)
        self.assertEqual(r['candidate_filters']['qoder:Qwen3.8-Flash'], 'not_authorized')
        self.assertEqual(r['candidate_filters'][dp.QMAX_POOL_KEY], 'not_authorized')
        self.assertEqual(r['candidate_filters'][dp.OPENCODE_DEEPSEEK_POOL_KEY],
                         'permission_incompatible')
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'not_authorized')

    def test_permission_incompatible_and_availability_now_orthogonal(self):
        """BW-SIMPLE-PRIORITY-RETRY-S3：历史 availability 阻断不再作为普通新轮的过滤
        原因——只作 receipt 只读诊断（zcode_blocked），绝不假装 healthy 或清除 hold；
        只有真正的权限能力冲突才记 permission_incompatible。
        BW-WEAK-RESTRUCTURE-20261116: 弱档 zcode 池 = GLM-5.3-Flash——混合 Edit+Bash 下
        如实 permission_incompatible，无精确需求时仅为 eligible 候选，availability 阻断
        只回显只读诊断；zcode:GLM-5.3 已移出弱档（mode_excluded 先于一切）。"""
        self.build_store()
        with _patch_zcode(False):
            # 混合 Edit+Bash：无逐文件能力的弱档候选（OpenCode/弱档 zcode GLM-5.3-Flash）如实不兼容。
            r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH),
                          permission_grants=self.FINE)
            self.assertEqual(r['zcode_blocked'], True)
            self.assertEqual(r['candidate_filters'][dp.OPENCODE_DEEPSEEK_POOL_KEY],
                             'permission_incompatible', r)
            self.assertEqual(r['candidate_filters'][dp.ZCODE_FLASH_POOL_KEY],
                             'permission_incompatible', r)
            self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY],
                             'mode_excluded', r)
        with _patch_zcode(False):
            # 无精确权限需求：旧的 availability 阻断只回显诊断，不产生任何过滤原因。
            r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH))
            self.assertEqual(r['zcode_blocked'], True)
            self.assertEqual(r['candidate_filters'][dp.ZCODE_FLASH_POOL_KEY], 'eligible', r)
            self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'mode_excluded', r)
            self.assertNotIn('zcode_unavailable', set(r['candidate_filters'].values()))
            self.assertNotEqual(r['selected'], None, r)

    def test_workspace_inflight_guards_and_readonly_hash_kept_with_grants(self):
        """权限过滤不替代既有守卫：同任务在途/工作区冲突照旧整项拒绝；
        带 grants 的规划仍只读，读写前后快照哈希不变。
        BW-ZCODE-RETIRE-20261020：在途占位改用现役 qoder Max 组合，守卫语义不变。
        BW-WEAK-RESTRUCTURE-20261116: qoder Max 只属 strong/medium——换档 strong；
        原 CN Flash 在途占位已无档可派，改用 strong 可派的 claude 组合（守卫语义不变）。"""
        self.build_store()
        shared = self._ws('shared')
        with dispatch_mode('strong'):
            self.reserve('a', 'qoder', 'Qwen4-Max-Preview', ws=shared, executor='qoder')
            r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH),
                          ws=shared, permission_grants=self.FINE)
            self.assertEqual(r['refused'], 'workspace_conflict')
            self.reserve('T', 'claude', 'deepseek-flash', executor='claude')
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
        父授权绕过过滤。
        BW-WEAK-RESTRUCTURE-20261116: 两个 Qwen3.8-Flash 已从所有档位移除——同阶段指定
        Flash 临时降级如实 mode_excluded 拒绝（绝不静默改派）；到期重裁决在弱档白天
        （只剩 claude 高峰门）按当前精确 Edit+Bash 需求过滤：两 OpenCode 与弱档 zcode
        GLM-5.3-Flash 能力不兼容、claude 被高峰门关住，选择落兼容且无门的
        codebuddy:glm-5.3-flash（层=2）；zcode:GLM-5.3 已移出弱档（mode_excluded）。"""
        self.build_store()
        # 同阶段：指定已授权但已无档可派的 CN Flash → 如实拒绝；OpenCode 在候选集合中
        # 仍按**当前**需求如实标 permission_incompatible。
        auth = self.auth('temporary_fallback', 'T', 's1', ALL_MAINS + ALL_FLASH,
                         fallback_scope={'task_id': 'T', 'stage': 's1'},
                         runtime='qodercn', model='Qwen3.8-Flash')
        prev = {'task_id': 'T', 'state': 'finished', 'acceptance_reviewed': True,
                'stage': 's1', 'runtime': 'qodercn', 'model': 'Qwen3.8-Flash'}
        r = self.plan(auth, previous=prev, stage='s1',
                      permission_grants=self.FINE)
        self.assertEqual(r['refused'], 'temporary_candidate_unavailable', r)
        self.assertEqual(r['refused_detail'], 'mode_excluded', r)
        self.assertIsNone(r['selected'], r)
        self.assertEqual(r['candidate_filters'][dp.OPENCODE_DEEPSEEK_POOL_KEY],
                         'permission_incompatible')
        self.assertEqual(r['candidate_filters'][dp.ZCODE_FLASH_POOL_KEY],
                         'permission_incompatible')
        # 到期经父授权重选：应用**当前**需求 → 弱档白天（只剩 claude 高峰门）无逐文件
        # 能力候选权限不兼容、claude 被门关住，选择落兼容且无门的 codebuddy:glm-5.3-flash
        # （层=2）；旧 s1 的需求绝不反向继承或改写本轮需求。
        parent = self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH)
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
        self.assertEqual(r['candidate_filters'][dp.ZCODE_FLASH_POOL_KEY],
                         'permission_incompatible')
        # BW-WEAK-TIER-ENFORCE-20261122: weak 无时段门——claude 合格（队列接管排序）。
        self.assertEqual(r['candidate_filters'][dp.CLAUDE_POOL_KEY], 'eligible')
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'mode_excluded')
        self.assertEqual(r['selected']['pool_key'], dp.CODEBUDDY_GLM_POOL_KEY, r)
        self.assertEqual(r['selected_reason'], 'main_force_selected', r)

    def test_edits_only_grants_select_zcode_with_controlled_profile(self):
        """在弱档 ZCode GLM-5.3-Flash 授权范围内，纯精确 Edit 可选受控文件配置。
        BW-WEAK-RESTRUCTURE-20261116: 受控文件 profile 的弱档池现在是 zcode:GLM-5.3-Flash
        （zcode:GLM-5.3 已移出弱档）；旧国际 qoder Max 占位已不在弱档可派集合，改占一个
        OpenCode 独立槽。"""
        self.build_store()
        self.reserve('o1', 'opencode', dp.OPENCODE_DEEPSEEK_MODEL, executor='opencode')
        r = self.plan(self.auth('auto', 'T', 's2', [dp.ZCODE_FLASH_POOL_KEY, *dp.OPENCODE_POOL_KEYS]), now=T_DAY,
                      permission_grants={'edits': ['Edit(C:/a.py)'], 'bash': [],
                                         'read_dirs': []})
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['candidate_filters'][dp.ZCODE_FLASH_POOL_KEY], 'eligible', r)
        self.assertIn(dp.ZCODE_FLASH_POOL_KEY,
                      [x['pool_key'] for x in r['eligible_candidates']])
        self.assertEqual(r['selected']['pool_key'], dp.ZCODE_FLASH_POOL_KEY, r)
        self.assertEqual(r['permission_requirements'],
                         {'fine_grained': True, 'read_dirs': False})
        self.assertTrue(r['controlled_file_profile_required'], r)
        self._assert_capability_table_untouched()

    def test_fixed_z_edits_only_selected_with_controlled_profile(self):
        """fixed 明确 ZCode 且本轮仅需精确 Edit：BW-ZCODE-RESTORE-1-20261022 起恢复
        受控文件 profile 可选——正常选中 ZCode（绝不偷偷换执行器），回执标
        controlled_file_profile_required=True。
        BW-WEAK-RESTRUCTURE-20261116: zcode:GLM-5.3 只属 strong/medium——strong 下原样
        选中；weak 档 fixed 点名弱档新池 zcode:GLM-5.3-Flash 同样走受控文件 profile。"""
        self.build_store()
        auth = self.auth('fixed', 'T', 's2', [dp.ZCODE_POOL_KEY],
                         runtime='zcode', model='GLM-5.3')
        grants = {'edits': ['Edit(C:/a.py)'], 'bash': [], 'read_dirs': []}
        with dispatch_mode('strong'):
            r = self.plan(auth, permission_grants=grants)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected']['pool_key'], dp.ZCODE_POOL_KEY, r)
        self.assertEqual(r['selected_reason'], 'fixed_authorized', r)
        self.assertTrue(r['controlled_file_profile_required'], r)
        auth2 = self.auth('fixed', 'T', 's2', [dp.ZCODE_FLASH_POOL_KEY],
                          runtime='zcode', model='GLM-5.3-Flash')
        r2 = self.plan(auth2, permission_grants=grants)
        self.assertEqual(r2['refused'], None, r2)
        self.assertEqual(r2['selected']['pool_key'], dp.ZCODE_FLASH_POOL_KEY, r2)
        self.assertEqual(r2['selected_reason'], 'fixed_authorized', r2)
        self.assertTrue(r2['controlled_file_profile_required'], r2)

    def test_mixed_edit_bash_task_remains_permission_incompatible(self):
        """混合 Edit+Bash 整段任务继续 permission_incompatible：受控文件 profile 只覆盖纯精确
        Edit，Bash 需求一律不成立，回执不标 controlled_file_profile_required。硬 Edit+Bash 不支持
        无逐文件能力的执行器直接承接，但可选能承接同一硬合同的已授权兼容执行器一轮完成；仅确有依赖且经任务
        授权时采用纯 Edit profile 与必要独立测试，不强制/机械拆分。
        BW-WEAK-RESTRUCTURE-20261116: 无逐文件能力的 OpenCode 与弱档 zcode GLM-5.3-Flash
        都如实不兼容；zcode:GLM-5.3 已移出弱档（mode_excluded 先于能力过滤）。"""
        self.build_store()
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH), now=T_DAY,
                      permission_grants=self.FINE)
        self.assertEqual(r['candidate_filters'][dp.OPENCODE_DEEPSEEK_POOL_KEY],
                         'permission_incompatible', r)
        self.assertEqual(r['candidate_filters'][dp.ZCODE_FLASH_POOL_KEY],
                         'permission_incompatible', r)
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'mode_excluded', r)
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
        """弱档按时段和权限筛选，在 ZCode 前选用可用的 OpenCode。
        BW-WEAK-RESTRUCTURE-20261116: task_scope 只进回执、不驱动过滤的原语义不变；
        旧国际 qoder Max 占位已无档可派（改占一个 OpenCode 独立槽）；zcode:GLM-5.3 已
        移出弱档（mode_excluded），弱档 ZCode 只剩 GLM-5.3-Flash（层=4，仍 eligible）。"""
        self.build_store()
        self.set_limit(dp.CLAUDE_POOL_KEY)
        self.reserve('o1', 'opencode', dp.OPENCODE_DEEPSEEK_MODEL, executor='opencode')
        r = self.plan(self.auth('auto', 'T', 's2', LEGACY_MAINS + ALL_FLASH), now=T_DAY,
                      task_scope=self.SCOPE)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected']['pool_key'], dp.CLAUDE_POOL_KEY, r)
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'mode_excluded')
        self.assertEqual(r['candidate_filters'][dp.ZCODE_FLASH_POOL_KEY], 'eligible')
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
        # BW-WEAK-RESTRUCTURE-20261116: 弱档 zcode 冲突明细现在由 GLM-5.3-Flash 承担；
        # zcode:GLM-5.3 已移出弱档（mode_excluded 先于能力过滤）。
        self.assertEqual(r['candidate_filters'][dp.ZCODE_FLASH_POOL_KEY],
                         'permission_incompatible', r)
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'mode_excluded', r)
        self.assertEqual(r['permission_requirements'],
                         {'fine_grained': True, 'read_dirs': False})
        self.assertEqual(r['task_scope'], self.SCOPE)
        self.assertEqual(scope, self.SCOPE)   # 原输入不突变
        self.assertEqual(grants, self.FINE)
        details = r['permission_incompatibility_details'][dp.OPENCODE_DEEPSEEK_POOL_KEY]
        self.assertIn('per-file edits/verbatim Bash grants', details['unsupported'])
        self.assertIn('ordinary_task_tools', details['note'])
        self.assertIn(dp.ZCODE_FLASH_POOL_KEY, r['permission_incompatibility_details'])
        self._assert_readonly_with_scope()

    def _assert_readonly_with_scope(self):
        before = self._digest()
        self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH), now=T_DAY,
                  task_scope=self.SCOPE)
        self.assertEqual(before, self._digest())

    def test_legacy_grants_only_unchanged_and_no_scope(self):
        """legacy 仅 permission_grants 混合需求：回执 task_scope=None、明细仍给冲突
        说明（未传新输入完全兼容旧行为）。
        BW-WEAK-RESTRUCTURE-20261116: 弱档 zcode 冲突明细现在由 GLM-5.3-Flash 承担
        （zcode:GLM-5.3 已移出弱档，mode_excluded 先于能力过滤）。"""
        self.build_store()
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH), now=T_DAY,
                      permission_grants=self.FINE)
        self.assertEqual(r['candidate_filters'][dp.OPENCODE_DEEPSEEK_POOL_KEY],
                         'permission_incompatible', r)
        self.assertEqual(r['candidate_filters'][dp.ZCODE_FLASH_POOL_KEY],
                         'permission_incompatible', r)
        self.assertIn(dp.ZCODE_FLASH_POOL_KEY, r['permission_incompatibility_details'])
        self.assertIsNone(r['task_scope'])
        self.assertIn(dp.OPENCODE_DEEPSEEK_POOL_KEY,
                      r['permission_incompatibility_details'])

    def test_pure_hard_edits_keep_controlled_profile_consistency(self):
        """在弱档 ZCode GLM-5.3-Flash 授权范围内，纯精确 Edit 可选受控文件配置。
        BW-WEAK-RESTRUCTURE-20261116: 受控文件 profile 的弱档池现在是 zcode:GLM-5.3-Flash
        （zcode:GLM-5.3 已移出弱档）；旧国际 qoder Max 占位已无档可派，改占一个 OpenCode
        独立槽。"""
        self.build_store()
        self.reserve('o1', 'opencode', dp.OPENCODE_DEEPSEEK_MODEL, executor='opencode')
        r = self.plan(self.auth('auto', 'T', 's2', [dp.ZCODE_FLASH_POOL_KEY, *dp.OPENCODE_POOL_KEYS]), now=T_DAY,
                      permission_grants={'edits': ['Edit(source/f1.cjs)'], 'bash': [],
                                         'read_dirs': []},
                      task_scope={'edits': ['source/f1.cjs'], 'bash': [], 'read_dirs': []})
        self.assertEqual(r['candidate_filters'][dp.ZCODE_FLASH_POOL_KEY], 'eligible', r)
        self.assertEqual(r['selected']['pool_key'], dp.ZCODE_FLASH_POOL_KEY, r)
        self.assertTrue(r['controlled_file_profile_required'], r)
        details = r['permission_incompatibility_details']
        self.assertNotIn(dp.ZCODE_FLASH_POOL_KEY, details, details)
        self.assertIn(dp.OPENCODE_DEEPSEEK_POOL_KEY, details, details)
        self.assertNotIn(dp.OPENCODE_GLM_POOL_KEY, details, details)

    def test_scope_never_bypasses_existing_guards(self):
        """fixed 不换执行器、缺授权、availability 阻断、在途、工作区冲突都不被
        task_scope 绕过，原因照旧可辨。BW-ZCODE-RESTORE-1-20261022：fixed 点名 ZCode
        且要求精确 Edit+Bash 时按能力冲突诚实拒绝（fixed_candidate_unavailable）；
        未授权 ZCode 如实 not_authorized；availability 阻断仍只作只读诊断回显。
        BW-WEAK-RESTRUCTURE-20261116: zcode:GLM-5.3 只属 strong/medium——整例换档
        strong 保持全部既有断言（能力冲突拒绝/诊断回显/守卫语义均不变）；原 CN Flash
        在途占位已无档可派，改用 strong 可派的 claude 组合。"""
        self.build_store()
        with dispatch_mode('strong'):
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
            self.reserve('T', 'qoder', 'Qwen4-Max-Preview', executor='qoder')
            r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH),
                          task_scope=self.SCOPE)
            self.assertEqual(r['refused'], 'task_in_flight')
            shared = self._ws('shared')
            self.reserve('a', 'claude', 'deepseek-flash', ws=shared, executor='claude')
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
