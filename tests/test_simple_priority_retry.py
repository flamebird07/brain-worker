"""BW-SIMPLE-PRIORITY-RETRY-S3 离线回归：每轮从最高优先级真实候选起、本轮失败换下一
个、下一新轮重来。只用临时 SQLite（每用例独立 dispatch store + 隔离 quota 路径）、
mock 掉 ZCode availability 只读回核，绝不真实模型 / 真实共享库 / 凭据 / 余额 / 网络。

断言：
- 本轮失败列表 `round_failed_candidates` 由主脑当前轮经 authorization JSON 维护：命中
  本轮已试组合标 `already_tried_this_round`，同一 runtime:model 本轮最多一次，本轮换下
  一个更高优先级的合格候选；
- 下一新轮不传/传空列表即重新从最高优先级开始，绝不永久或到期后挡新轮；
- allowed_candidates 授权集合始终完整回显，失败项绝不扩权、也不缩权；
- 本轮所有候选都试过 → 诚实无候选（不再派），绝不偷偷复用已试组合或换模型；
- 历史 ZCode availability（旧 hard_hold/退避）或三 Qoder Max/Claude 明确限额都不再挡
  一个**新普通轮次**（真实尝试失败才落诊断、CAS 结算不变）；
  BW-ZCODE-RETIRE-20261020：zcode 池已全局退役（LEGACY_RETIRED，授权集合内即
  unauthorized_candidate、真实入口即 unknown_pool），原 zcode 重试/入口用例改用现役
  组合，qc 历史 availability 机器仅作只读诊断继续验证；
- 时段/容量/权限能力/明确 model 约束/同 workspace 单写/unknown 在途/审批拒绝照常，
  重试绝不绕过；
- 严格校验失败列表入参：裸字符串/非列表/空白项一律拒绝，不逐字符猜测；
- dispatch_pool AUTO 普通派工不再据历史 availability/限额剔除候选（真实正式入口无需
  quota-probe 即可启动）。
"""
import hashlib
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'scripts'
for p in (str(SCRIPTS), str(REPO / 'tests')):
    if p not in sys.path:
        sys.path.insert(0, p)

import dispatch_pool as dp  # noqa: E402
import stage_selection as ss  # noqa: E402
try:
    import quota_control as qc  # noqa: E402 - 入口整合用例需要真实 hard_hold 行
except Exception:  # noqa: BLE001 - 隔离树无 quota_control 时该用例自动跳过
    qc = None

QMAX = 'qoder:Qwen3.8-Max'
CN_MAX = 'qodercn:Qwen3.8-Max'
CN_CUSTOM_MAX = 'qodercn:Qwen-3.8-Max'
CN_FLASH = 'qodercn:Qwen3.8-Flash'
OC_DS = dp.OPENCODE_DEEPSEEK_POOL_KEY
ZCODE = dp.ZCODE_POOL_KEY  # zcode:GLM-5.3（BW-ZCODE-RETIRE-20261020 已退役：仅作诊断断言，不再入授权候选集合）
PROVIDER = 'account:bigmodel-individual-coding-plan'  # 与 qc 受信任渠道一致（合成，无凭据）

T_NIGHT = datetime(2026, 10, 8, 15, 0, 0, tzinfo=timezone.utc)  # 北京 23:00（主力窗内）
T_DAY = datetime(2026, 10, 8, 2, 0, 0, tzinfo=timezone.utc)     # 北京 10:00（高峰窗外）


def _patch_zcode(available=True):
    return unittest.mock.patch.object(
        dp, '_zcode_availability_blocked',
        lambda *a, **k: None if available else {'blocked': True, 'reasons': ['mock']})


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='bw-simple-retry-')
        self.base = Path(self._tmp.name)
        self.store = str(self.base / 'dispatch.sqlite3')
        self.quota = str(self.base / 'quota-isolated.sqlite3')
        self.assertNotEqual(self.store, str(dp.default_store_path()))
        self.saved = {k: os.environ.pop(k, None)
                      for k in ('BRAIN_WORKER_DISPATCH_STORE', 'BRAIN_WORKER_QUOTA_STORE')}
        os.environ['BRAIN_WORKER_QUOTA_STORE'] = self.quota
        self._zpatch = _patch_zcode(True)
        self._zpatch.start()
        self.addCleanup(self._zpatch.stop)
        self.addCleanup(self._tmp.cleanup)
        self._restore_env()
        self.build_store()

    def _restore_env(self):
        pass

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def build_store(self):
        with closing(dp.connect(self.store)) as conn:
            dp._rotation_row(conn)
            conn.execute('PRAGMA journal_mode=DELETE')

    def _normalize(self):
        with closing(dp.connect(self.store)) as conn:
            conn.execute('PRAGMA journal_mode=DELETE')

    def ws(self, name):
        d = self.base / 'ws' / str(name)
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def set_limit(self, pk):
        with closing(dp.connect(self.store)) as conn:
            conn.execute('INSERT INTO main_force_limits(pool_key, limited) VALUES(?,1)',
                         (pk,))
            conn.execute('PRAGMA journal_mode=DELETE')

    def fill(self, pk, task):
        """真实占用该池一个在途名额（走既有 reserve 路径），使容量门按事实收紧。
        显式点名该池所属执行器：fixture 要占的是指定池，不能让 AUTO 在 CodeBuddy 有空位
        时改派（routing_required），否则夹具本身就占不到目标池。"""
        runtime, model = pk.split(':', 1)
        r = dp.reserve(self.store, task_id=task, runtime=runtime, model=model,
                       workspace=self.ws(task), prompt_sha256='p', stage='s',
                       executor=runtime, now=T_NIGHT, _preclaim=False)
        self.assertTrue(r.get('reserved') or r.get('allowed'), r)
        self._normalize()

    def auth(self, mode, task, stage, allowed, **kw):
        a = {'selection_mode': mode, 'quote': '用户真实原话',
             'authorization_scope': {'task_id': task, 'stage': stage},
             'allowed_candidates': allowed}
        a.update(kw)
        return a

    def plan(self, allowed, *, task='T', stage='s2', now=T_NIGHT, ws=None,
             round_failed=None, mode='auto', permission_grants=None, **auth_kw):
        auth = self.auth(mode, task, stage, allowed, **auth_kw)
        if round_failed is not None:
            auth['round_failed_candidates'] = round_failed
        return ss.plan_stage_selection(
            auth, previous=None, store_path=self.store, task_id=task, stage=stage,
            workspace=ws or self.ws(task), now=now, permission_grants=permission_grants)


class RoundFailedRetryTests(_Base):
    # BW-CLAUDE-ALLDAY-QMAX-AFTER-20261023：国际 Qoder Max 层=4、CN 内置 Max 层=1，故同集合
    # 内的"最高优先主力"是 CN 内置 Max；本轮失败后换到下一候选（国际 Max）。
    def test_tried_combo_skipped_next_higher_priority_same_round(self):
        """首轮从最高优先层选；把该组合记入本轮失败列表后，同一轮换下一个更高优先级的
        合格候选；失败列表只剔本轮已试，不改授权集合。"""
        allowed = [QMAX, CN_MAX, CN_FLASH]
        r1 = self.plan(allowed)
        self.assertEqual(r1['refused'], None, r1)
        self.assertEqual(r1['selected']['pool_key'], CN_MAX, r1)   # 主力最高层优先
        self.assertEqual(r1['selected_priority'], 1, r1)
        self.assertEqual(r1['round_failed_candidates'], [])

        r2 = self.plan(allowed, round_failed=[CN_MAX])
        self.assertEqual(r2['refused'], None, r2)
        self.assertEqual(r2['candidate_filters'][CN_MAX], 'already_tried_this_round', r2)
        self.assertEqual(r2['selected']['pool_key'], QMAX, r2)  # 本轮换下一个主力
        self.assertEqual(r2['selected_priority'], 4, r2)
        self.assertEqual(sorted(r2['allowed_candidates']), sorted(allowed))  # 不缩权

    def test_next_new_round_empty_list_restarts_top_priority(self):
        """下一新轮不传/传空列表 → 重新从最高优先级开始，绝不永久挡本轮已试组合。"""
        allowed = [QMAX, CN_MAX]
        # 上一轮把两者都试尽 → 该轮诚实无候选。
        drained = self.plan(allowed, round_failed=[QMAX, CN_MAX])
        self.assertEqual(drained['refused'], 'no_eligible_candidate', drained)
        # 新轮：空列表重新最高优先
        fresh = self.plan(allowed, round_failed=[])
        self.assertEqual(fresh['refused'], None, fresh)
        self.assertEqual(fresh['selected']['pool_key'], CN_MAX, fresh)
        no_key = self.plan(allowed)  # 完全不传
        self.assertEqual(no_key['selected']['pool_key'], CN_MAX, no_key)

    def test_all_candidates_tried_this_round_honestly_no_dispatch(self):
        """本轮所有已授权候选都试过 → 诚实无候选（不再派），绝不偷偷复用已试组合、
        也绝不偷偷换到未授权池。"""
        allowed = [QMAX, CN_MAX]
        r = self.plan(allowed, round_failed=[QMAX, CN_MAX])
        self.assertIsNone(r['selected'], r)
        self.assertEqual(r['refused'], 'no_eligible_candidate', r)
        self.assertEqual(r['candidate_filters'][QMAX], 'already_tried_this_round')
        self.assertEqual(r['candidate_filters'][CN_MAX], 'already_tried_this_round')
        self.assertEqual(sorted(r['allowed_candidates']), sorted(allowed))

    def test_failed_item_outside_allowed_never_expands_authorization(self):
        """失败项若本就不在授权集合内只是被忽略，绝不因此把候选扩到别的池；
        完整授权集合原样回显。"""
        allowed = [QMAX, CN_MAX]
        r = self.plan(allowed, round_failed=[CN_FLASH])
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(sorted(r['allowed_candidates']), sorted(allowed))
        self.assertEqual(r['selected']['pool_key'], CN_MAX, r)   # 未被 CN_FLASH 干扰
        self.assertEqual(r['round_failed_candidates'], [CN_FLASH])

    def test_cli_passes_round_failed_via_authorization_json(self):
        """CLI 经 --authorization-json 携带 round_failed_candidates（本轮失败列表），
        与函数入参同口径，绝不新增后台自动派发引擎。
        BW-ZCODE-RETIRE-20261020：CLI 走真实时钟（北京白天，Max 夜间窗关闭），集合改用
        无时段门的现役池（OpenCode 免费槽 + CN Flash），换下一候选语义原样可验。"""
        import contextlib
        import io
        auth = self.auth('auto', 'T', 's2', [OC_DS, CN_FLASH],
                         round_failed_candidates=[OC_DS])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ss.main(['--store', self.store, '--task-id', 'T', '--stage', 's2',
                            '--workspace', self.ws('T'),
                            '--authorization-json', json.dumps(auth)])
        self.assertEqual(code, 0)
        out = json.loads(buf.getvalue())
        self.assertEqual(out['round_failed_candidates'], [OC_DS])
        self.assertEqual(out['candidate_filters'][OC_DS], 'already_tried_this_round')
        self.assertEqual(out['selected']['pool_key'], CN_FLASH)


class RoundFailedValidationTests(_Base):
    def test_bare_string_rejected_not_char_split(self):
        """裸字符串一律拒绝，绝不逐字符当集合猜测。"""
        r = self.plan([QMAX, ZCODE], round_failed=QMAX)
        self.assertEqual(r['refused'], 'invalid_round_failed_candidates', r)

    def test_non_list_and_blank_entries_rejected(self):
        for bad in (123, {'a': 1}, [QMAX, ''], [QMAX, ' x '], [QMAX, 42]):
            r = self.plan([QMAX, ZCODE], round_failed=bad)
            self.assertEqual(r['refused'], 'invalid_round_failed_candidates', bad)

    def test_keyword_round_failed_still_honored(self):
        """保留的关键字入参仍生效（与 JSON 入参一致），主脑当前轮可二选一维护。
        BW-ZCODE-RETIRE-20261020：下一主力改用现役 CN Max（zcode 已退役不可选）。"""
        r = ss.plan_stage_selection(
            self.auth('auto', 'T', 's2', [QMAX, CN_MAX]), store_path=self.store,
            task_id='T', stage='s2', workspace=self.ws('T'), now=T_NIGHT,
            round_failed_candidates=[QMAX])
        self.assertEqual(r['selected']['pool_key'], CN_MAX, r)
        self.assertEqual(r['candidate_filters'][QMAX], 'already_tried_this_round')

    def test_structured_event_entries_accepted_and_strictly_validated(self):
        """BW-ROUND-TRANSITION-FIX：round_failed_candidates 兼容结构化失败事件对象
        （分类/时间/事件引用），与纯 pool_key 字符串可混用、既有调用方不破坏；畸形
        事件（缺分类/缺时间/未知键/无时区/不可解析/空白项）一律拒绝，绝不猜。"""
        good = {'pool_key': QMAX, 'failure_class': 'network_failure',
                'failed_at_utc': dp._iso(T_NIGHT), 'event_ref': 'tk-net-1'}
        r = self.plan([QMAX, CN_MAX], round_failed=[good, CN_FLASH])
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['candidate_filters'][QMAX], 'already_tried_this_round', r)
        self.assertEqual(r['selected']['pool_key'], CN_MAX, r)
        self.assertEqual(sorted(r['round_failed_candidates']), sorted([QMAX, CN_FLASH]))
        self.assertEqual(r['round_failed_events'][0]['failure_class'],
                         'network_failure', r)
        self.assertEqual(r['round_failed_events'][0]['failed_at_utc'],
                         dp._iso(T_NIGHT), r)
        self.assertEqual(r['round_failed_events'][0]['event_ref'], 'tk-net-1', r)
        self.assertFalse(r['round_failed_events'][1]['structured'], r)
        bad_events = (
            {'pool_key': QMAX},                                   # 缺分类与时间
            {'pool_key': QMAX, 'failure_class': 'quota_429'},     # 缺 failed_at_utc
            dict(good, unexpected='x'),                           # 未知键
            dict(good, failed_at_utc='2026-10-08T15:00:00'),      # 无时区
            dict(good, failed_at_utc='not-a-time'),               # 不可解析
            dict(good, pool_key=''),                              # 空白 pool_key
            dict(good, pool_key=' x '),                           # 未去空白
            dict(good, failure_class=''),                         # 空白分类
            dict(good, event_ref=''),                             # 空白事件引用
        )
        for bad in bad_events:
            r = self.plan([QMAX, CN_MAX], round_failed=[bad])
            self.assertEqual(r['refused'], 'invalid_round_failed_candidates', bad)


class HistoryQuotaNoLongerBlocksFreshRoundTests(_Base):
    def test_explicit_limit_no_longer_blocks_new_round(self):
        """历史三 Qoder Max 明确限额不再挡新普通轮次：该池在新轮仍 eligible，
        授权集合完整保留（真实尝试失败才落诊断、CAS 结算不变）。"""
        self.set_limit(QMAX)
        allowed = [QMAX, CN_CUSTOM_MAX]
        r = self.plan(allowed, now=T_NIGHT)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['candidate_filters'][QMAX], 'eligible', r)
        self.assertNotIn('explicitly_limited', set(r['candidate_filters'].values()))

    def test_old_zcode_hold_or_backoff_no_longer_blocks_new_round(self):
        """旧 hard_hold / 退避 / 未来时间等 availability 历史阻断不再挡一个新正常轮次：
        receipt 仍只读诊断回显 zcode_blocked=True（不假装 healthy、不清 hard_hold）。
        BW-ZCODE-RESTORE-1-20261022：ZCode 恢复逐候选枚举——未授权时如实 not_authorized，
        历史 availability 阻断仍只作只读诊断、绝不产生过滤原因。"""
        with _patch_zcode(False):
            r = self.plan([QMAX], now=T_NIGHT)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['zcode_blocked'], True)               # 诊断如实保留
        self.assertEqual(r['candidate_filters'][QMAX], 'eligible', r)
        self.assertEqual(r['selected']['pool_key'], QMAX, r)
        self.assertEqual(r['candidate_filters'][ZCODE], 'not_authorized', r)


class GuardsStillHonoredTests(_Base):
    def test_window_capacity_permission_not_bypassed_by_retry(self):
        """重试绝不绕过时段/容量/权限能力：窗外 CN Max window_closed、满池 capacity_full、
        需精确 Edit+Bash 的能力不兼容候选，各归各原因。
        BW-ZCODE-RETIRE-20261020：原 ZCode permission_incompatible 用例改用现役无精确
        规则能力的 OpenCode deepseek 免费池（同样无 fine_grained 能力，语义平移）。
        BW-QODER-INTL-MAX-ALLDAY-20261023：国际 Max 已全天可派，夜间门改由 CN Max 承载。"""
        r = self.plan([CN_MAX], now=T_DAY)
        self.assertEqual(r['candidate_filters'][CN_MAX], 'window_closed', r)
        self.assertEqual(r['refused'], 'no_eligible_candidate', r)

        self.fill(QMAX, 'occ-max')   # QMAX 容量 1 → 满
        r = self.plan([QMAX], now=T_NIGHT)
        self.assertEqual(r['candidate_filters'][QMAX], 'capacity_full', r)

        r = self.plan([OC_DS], now=T_NIGHT,
                      permission_grants={'edits': ['Edit(C:/a.py)'],
                                         'bash': ['Bash(pytest)'], 'read_dirs': []})
        self.assertEqual(r['candidate_filters'][OC_DS], 'permission_incompatible', r)

    def test_workspace_single_write_and_inflight_not_bypassed(self):
        """同 workspace 单写 / 本任务在途照常拒绝，round_failed 不能绕过这些守卫。
        fixture 必须用 dp 现有 workspace 规范化（normcase(realpath)）写入，与 planner/
        dp 运行时读回时同一口径（Windows 大小写 / 8.3 短名 / 斜杠别名）；不改运行时路径
        保护、不删断言。
        BW-ZCODE-RETIRE-20261020：占用行组合由退役 zcode 改为现役 QMAX（守卫验的是
        workspace 冲突拒绝，组合无关）。"""
        shared = self.ws('shared')
        shared_norm = dp._norm_workspace(shared)   # 与 _workspace_conflict 查询同一归一
        with closing(dp.connect(self.store)) as conn:
            runtime, model = QMAX.split(':', 1)
            conn.execute('INSERT INTO attempts(token, task_id, runtime, model, pool_key, '
                         "state, workspace, reserved_at_utc) VALUES(?,?,?,?,?,?,?,?)",
                         ('tk-oc', 'B', runtime, model, QMAX, 'running', shared_norm,
                          dp._iso(T_NIGHT)))
            conn.execute('PRAGMA journal_mode=DELETE')
        r = self.plan([QMAX], ws=shared, round_failed=[CN_CUSTOM_MAX])
        self.assertEqual(r['refused'], 'workspace_conflict', r)

    def test_history_unknown_inflight_not_preempted(self):
        """历史 unknown 在途仍占容量、绝不被重试抢占：把该池填成 unknown 在途，本轮该池
        capacity_full，绝不因 round_failed 把它当空闲抢派。"""
        with closing(dp.connect(self.store)) as conn:
            runtime, model = QMAX.split(':', 1)
            conn.execute('INSERT INTO attempts(token, task_id, runtime, model, pool_key, '
                         "state, workspace, reserved_at_utc) VALUES(?,?,?,?,?,?,?,?)",
                         ('tk-unk', 'B', runtime, model, QMAX, 'unknown',
                          dp._norm_workspace(self.ws('B')), dp._iso(T_NIGHT)))
            conn.execute('PRAGMA journal_mode=DELETE')
        r = self.plan([QMAX], now=T_NIGHT)
        self.assertEqual(r['candidate_filters'][QMAX], 'capacity_full', r)
        self.assertEqual(r['refused'], 'no_eligible_candidate', r)


class DispatchPoolAutoNotBlockedByHistoryTests(_Base):
    def test_explicit_limit_does_not_block_qoder_max_claim(self):
        """普通派工（正式入口）不再据历史限额剔除：即便 QMAX 被明确限额标记，executor
        点名 QMAX 仍能真实 claim 到该池（历史标记仅诊断，真实尝试照常）。"""
        self.set_limit(QMAX)
        runtime, model = QMAX.split(':', 1)
        r = dp.reserve(self.store, task_id='D', runtime=runtime, model=model,
                       workspace=self.ws('D'), prompt_sha256='p', stage='s',
                       executor='qoder', now=T_NIGHT, _preclaim=False)
        self.assertTrue(r.get('reserved') or r.get('allowed'), r)
        self.assertEqual(r['pool_key'], QMAX, r)

    def test_auto_prefers_highest_priority_without_probing_quota(self):
        """AUTO 新轮直接从最高优先级层起（夜间两个 CN Max 优先），无需任何恢复 probe；
        绝不为绕限额把候选降到更低层。
        BW-CLAUDE-ALLDAY-QMAX-AFTER-20261023：授权集合同时含 CodeBuddy deepseek（层=5）
        时，夜间空库仍必须先落 CN Max 层（1），绝不回到旧「CodeBuddy 最高优先」口径；
        授权集合不含 ZCode（层=2），不参与本集合选择。"""
        allowed = [CN_MAX, QMAX, dp.CODEBUDDY_POOL_KEY]
        r = self.plan(allowed, now=T_NIGHT)
        self.assertIn(r['selected']['pool_key'], {QMAX, CN_MAX}, r)
        self.assertEqual(r['selected_priority'], dp.POOL_PRIORITY[r['selected']['pool_key']],
                         r)
        self.assertLess(r['selected_priority'],
                        dp.POOL_PRIORITY[CN_FLASH], r)  # 绝不降到 Flash 层
        self.assertEqual(dp.POOL_PRIORITY[dp.CODEBUDDY_POOL_KEY], 5)
        self.assertLess(r['selected_priority'],
                        dp.POOL_PRIORITY[dp.CODEBUDDY_POOL_KEY], r)  # CN Max(1) 先于 CodeBuddy(5)


@unittest.skipIf(qc is None, 'quota_control 不可用（隔离树）')
class ZcodeEntryPassesUnderHardHoldTests(_Base):
    """BW-SIMPLE-PRIORITY-RETRY-S4 入口整合证明（不真实调用任何模型）：临时 sqlite 落一条
    **真实 hard_hold** 行 → 普通 purpose='dispatch' 的 qc.zcode_availability_gate 允许通过
    （无需 probe、不谎称 healthy），随后 dp.reserve + dp.consume_for_entry 在同一 dispatch
    临时库上真实预留 / 消费成功，证明正式入口不再被历史 availability 挡；显式 purpose='probe'
    生命周期仍由 qc 独立保留，此处不改动。"""

    def _seed_hard_hold(self):
        routes_absent = str(self.base / 'no-routes.json')
        group = qc.resolve_group(qc.load_routes(routes_absent), 'zcode',
                                 {'provider': PROVIDER})['quota_group']
        classification = {'is_trusted_429': True, 'hard_quota': True, 'kind': 'hard_hold',
                          'provider_code': 1310, 'provider': PROVIDER,
                          'blocked_until_utc': None, 'reason': 'synthetic hard hold'}
        qc.record_zcode_unavailability(self.quota, provider=PROVIDER, quota_group=group,
                                       classification=classification, now=T_NIGHT)
        return routes_absent, group

    def test_normal_dispatch_gate_and_entry_pass_without_probe(self):
        routes, group = self._seed_hard_hold()
        # 1) 普通 purpose='dispatch' 无需 recovery probe 即允许——绝不假装 healthy、也不
        #    清 hold（历史 availability 只作诊断保留）。
        gate = qc.zcode_availability_gate(self.quota, provider=PROVIDER,
                                          routes_path=routes, purpose='dispatch',
                                          now=T_NIGHT)
        self.assertTrue(gate['allowed'], gate)
        self.assertFalse(gate['probe_granted'], gate)
        self.assertEqual(gate['state'], 'hard_hold', gate)  # 诊断原样，未清 hold
        # 状态侧也仍能看到 hard_hold 阻断行（诊断不丢）。
        blocking = qc.zcode_blocking_rows(self.quota, now=T_NIGHT)
        self.assertTrue(any(r['state'] == 'hard_hold' for r in blocking), blocking)

        # 2) 真实入口：现役 QMAX——dp.reserve 显式 executor='qoder' 预留成功、
        #    dp.consume_for_entry 消费成功，同一 dispatch 临时库上证明正式入口不再被
        #    consume_for_entry 里的 availability/main_force_limit 复核挡回（携带的
        #    quota_store 历史 hard_hold 只作诊断，绝不拦现役池）。
        r = dp.reserve(self.store, task_id='entry-z', runtime='qoder', model='Qwen3.8-Max',
                       workspace=self.ws('entry-z'), prompt_sha256='p', stage='s',
                       chat_id='c', executor='qoder', quota_store=self.quota,
                       now=T_NIGHT, _preclaim=False)
        self.assertTrue(r.get('reserved') and r.get('allowed'), r)
        self.assertEqual(r['pool_key'], QMAX, r)
        tok = r['token']

        c = dp.consume_for_entry(self.store, task_id='entry-z', runtime='qoder',
                                 model='Qwen3.8-Max', workspace=self.ws('entry-z'),
                                 prompt_sha256='p', stage='s', chat_id='c',
                                 claim_token=tok, executor='qoder',
                                 quota_store=self.quota, now=T_NIGHT)
        self.assertTrue(c.get('allowed'), c)
        self.assertEqual(c.get('pool_key'), QMAX, c)
        # 消费未把本 token 释放为 start_failed：capacity_released 不出现，占位保留到 running。
        self.assertNotIn('capacity_released', c)
        # 仍在途计数 = 1（未被历史 hold 复核踢掉）。
        self.assertEqual(dp.status(self.store, now=T_NIGHT)['pools'][QMAX]['active'], 1)
        # BW-ZCODE-RESTORE-1-20261022：恢复可派的 zcode 组合经同一真实入口照常预留
        # 成功（历史 hard_hold 只作只读诊断，绝不拦新轮真实尝试、不谎称 healthy）。
        rz = dp.reserve(self.store, task_id='entry-zz', runtime='zcode', model='GLM-5.3',
                        workspace=self.ws('entry-zz'), prompt_sha256='p', stage='s',
                        chat_id='c', executor='zcode', quota_store=self.quota,
                        now=T_NIGHT, _preclaim=False)
        self.assertTrue(rz.get('allowed') and rz.get('token'), rz)
        self.assertEqual(rz['pool_key'], ZCODE, rz)


if __name__ == '__main__':
    unittest.main()
