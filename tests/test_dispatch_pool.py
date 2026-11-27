"""dispatch_pool 离线验收：并发容量池 + 1:1 路由 + Luna 票据竞争裁决。

只用临时 sqlite（每个用例独立 store），绝不触碰真实 ~/.brain-worker 池，绝不 Popen 真实
CLI。reconcile 用可注入 prober（假 PID 存活/创建身份），不做任何真实进程探测。
覆盖 references/global-dispatch.md 列出的全部裁决口径。

Z4/最终候选修订（BW-GITHUB-CLOSEOUT-20261008-S6）：
- fixture 修正：不同 task 不再共享同一虚拟 workspace（真实单写入守卫下会被正确拒绝），
  每个 task 用 base/ws/<task> 独立真实目录；生产守卫语义不放松，只改错误 fixture。
- 用户语义：2 Zcode + 2 Max + 2 Flash，六满 + 真实询问 + 300 秒才 Luna 救援（无 cap）；
  国内释放优先同事务回收，同 task 绝不双派。fill_domestic 按 1:1 轮转交替填充。
- 移除已废弃的 exact_combo（plan 不得绕过 1:1 轮转）。
- finish/reconcile 断言对齐当前语义：reserved 无 child 只能 start_failed/cancelled 释放；
  child 死（真实 Windows 死 PID created=None）释放；child 活且出生匹配保持；child 活但
  出生漂移/未知（PID 复用嫌疑）→ unknown 不抢占；从未消费的合法预留 reconcile 保持
  reserved（绝不翻 unknown），仍可被 consume 或 cancel 单赢家结算。
"""
import os
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'scripts'
for p in (str(SCRIPTS), str(REPO / 'tests')):
    if p not in sys.path:
        sys.path.insert(0, p)

import dispatch_pool as dp  # noqa: E402
# BW-WEAK-RESTRUCTURE-20261116：medium 档构造 qodercn:Qwen-3.8-Max 占位需临时清空用户禁用集。
from dispatch_mode_support import (dispatch_mode, insert_sonus_leftover,
                                   user_disabled_pools,
                                   weak_new_dispatch_capacity)  # noqa: E402

# 历史 Luna 状态机回归只在测试进程显式解锁，生产常量仍固定禁用；新禁用用例会单独恢复真值。
dp.LUNA_DISABLED = False
dp.CAPACITY[dp.LUNA_KEY] = None

T0 = datetime(2026, 10, 8, 15, 0, 0, tzinfo=timezone.utc)  # 北京时间 23:00（主力时段内）


class _PoolBase(unittest.TestCase):
    """每个用例一个独立临时 store；workspace 用真实存在的临时目录，避免 realpath 归一化噪声。

    不同 task 使用 base/ws/<task> 独立真实目录——真实单写入守卫下不同写入者共享同一目录
    会被正确拒绝，fixture 绝不虚拟同一路径来掩盖守卫。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='bw-pool-w-')
        self.base = Path(self._tmp.name)
        self.store = str(self.base / 'dispatch.sqlite3')
        self.assertNotEqual(str(self.store), str(dp.default_store_path()))
        self.saved_env = os.environ.pop('BRAIN_WORKER_DISPATCH_STORE', None)
        # 单写入者用例可复用的默认目录。
        self.ws = self._ws_for('default')
        # BW-CODEBUDDY-MAIN-S3 + BW-ZCODE-RESTORE-1-20261022：本文件验证现行层序
        # 与满池/生命周期守卫。这里先真实占住 CodeBuddy deepseek 唯一名额（reserved 行），
        # 使 `_select_main_force` 因 active>=capacity=1 跳过它。
        # BW-WEAK-RESTRUCTURE-20261116：codebuddy:deepseek-v4.1-flash 自弱档移除（所有档位
        # 均不可新派），本占位只能按真实形态直插（同 strong 遗留 Sonus 的直插模式）。
        # BW-WORKER-WIDGET-20261018-S3：GLM 独立 1 槽，其占位由 fill_domestic 经真实
        # reserve 补齐。总量 17 = 本占位 1 + fill 15 + strong 遗留 Sonus 1
        # （BW-WEAK-RESTRUCTURE-20261116：物理总 16→17，新增 zcode:GLM-5.3-Flash 1 槽）。
        with closing(dp.connect(self.store)) as conn:
            conn.execute('INSERT INTO attempts(token, task_id, runtime, model, pool_key, '
                         "state, reserved_at_utc) VALUES(?,?,?,?,?,?,?)",
                         ('cb-fixture', 'cb-fixture-task', 'codebuddy',
                          'deepseek-v4.1-flash', dp.CODEBUDDY_POOL_KEY, 'reserved',
                          dp._iso(T0)))
            conn.execute('PRAGMA journal_mode=DELETE')
        self.addCleanup(self._tmp.cleanup)

    def tearDown(self):
        if self.saved_env is not None:
            os.environ['BRAIN_WORKER_DISPATCH_STORE'] = self.saved_env

    def _ws_for(self, task):
        d = self.base / 'ws' / str(task)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def seed_attempt(self, token, task_id, pool, state='reserved'):
        """BW-WEAK-RESTRUCTURE-20261116：为「当前没有任何档位可新派」的池（两个地区
        Qwen3.8-Flash、codebuddy:deepseek 等）按真实在途形态直插占位行（同
        insert_sonus_leftover 的 strong 遗留模式）。仅用于容量/兜底场景的物理占位，
        绝不用于伪造可新派断言。"""
        with closing(dp.connect(self.store)) as conn:
            runtime, model = pool.split(':', 1)
            conn.execute(
                'INSERT INTO attempts(token, task_id, runtime, model, pool_key, state, '
                'reserved_at_utc) VALUES(?,?,?,?,?,?,?)',
                (token, task_id, runtime, model, pool, state, dp._iso(T0)))
            conn.execute('PRAGMA journal_mode=DELETE')

    def _scope(self, task_id):
        return {'task_id': task_id, 'stage': 's1', 'chat_id': 'c1',
                'workspace': str(self._ws_for(task_id)), 'prompt_sha256': 'p' * 64}

    def _state(self, tok):
        with closing(dp.connect(self.store)) as conn:
            r = conn.execute('SELECT state FROM attempts WHERE token=?', (tok,)).fetchone()
            return r['state'] if r is not None else None

    # ---- 便捷断言 ----
    def assertClaimed(self, res, pk):
        self.assertTrue(res.get('allowed'), res)
        self.assertTrue(res.get('claimed') or res.get('reserved'), res)
        self.assertEqual(res['pool_key'], pk, res)
        self.assertFalse(res.get('routing_required'), res)
        return res['token']

    def assertRouting(self, res, target_pk):
        self.assertFalse(res.get('allowed'), res)
        self.assertTrue(res.get('routing_required'), res)
        self.assertFalse(res.get('sent', True), res)
        self.assertEqual(res['selected']['pool_key'], target_pk, res)

    def assertRejected(self, res, reason):
        self.assertFalse(res.get('allowed'), res)
        self.assertEqual(res.get('reason'), reason, res)

    # ---- 便捷动作 ----
    def legacy_custom_claim(self, task, ws=None, prompt='p', stage=None, chat_id=None):
        """历史占位 fixture；禁用后的新派测试必须直接调用生产 reserve。"""
        with closing(dp.connect(self.store)) as conn:
            return dp._insert_claim(conn, task_id=task, runtime='qodercn',
                                    model='Qwen-3.8-Max', pk=dp.CN_CUSTOM_MAX_POOL_KEY,
                                    workspace=str(ws or self._ws_for(task)),
                                    prompt_sha256=prompt, stage=stage, chat_id=chat_id,
                                    token=None, now=T0, origin='offline-legacy-fixture')

    def reserve(self, task, runtime, model, pk=None, ws=None, prompt='p',
                stage=None, chat_id=None, executor=None):
        """确定性占位准备：默认把 executor 固定为所请求的 runtime，绕过 AUTO 跨层优先级
        改派（BW-MAIN-PRIORITY-S1：夜间 AUTO 会把 zcode 请求改派到 Qoder Max）。本 helper
        只服务于生命周期/容量/工作区/owner 等非 AUTO 排序用例的准备，绝不改生产算法；真正
        验证 AUTO 排序的用例走 self.select（保留 AUTO 路径）。"""
        if dp.pool_key(runtime, model) == dp.CN_CUSTOM_MAX_POOL_KEY:
            return self.legacy_custom_claim(task, ws, prompt, stage, chat_id)
        r = dp.reserve(self.store, task_id=task, runtime=runtime, model=model,
                       workspace=str(ws if ws is not None else self._ws_for(task)),
                       prompt_sha256=prompt, stage=stage, chat_id=chat_id,
                       executor=executor if executor is not None else runtime,
                       now=T0, _preclaim=False)
        self.assertClaimed(r, pk or dp.pool_key(runtime, model))
        return r['token']

    def select(self, task, runtime, model):
        return dp.select_and_claim(self.store, task_id=task, runtime=runtime, model=model,
                                   workspace=str(self._ws_for(task)), prompt_sha256='p',
                                   now=T0, _preclaim=False)

    def reserve_exec(self, task, runtime, model, executor, pk=None):
        """显式执行器入口的确定性 claim：CN 内置只能经 executor='qodercn' 单独 claim，
        目标禁用池仅用于遗留占位，其它显式 executor 取真实 reserved token。
        BW-WEAK-TIER-ENFORCE-20261122：弱档纯优先级队列接管一切显式请求——只有请求组合
        恰为队首时才被真实 claim，否则请先用 occupy_weak_above 排空上方槽位。"""
        if dp.pool_key(runtime, model) == dp.CN_CUSTOM_MAX_POOL_KEY:
            return self.legacy_custom_claim(task)
        r = dp.reserve(self.store, task_id=task, runtime=runtime, model=model,
                       workspace=str(self._ws_for(task)), prompt_sha256='p',
                       executor=executor, now=T0, _preclaim=False)
        self.assertClaimed(r, pk or dp.pool_key(runtime, model))
        return r['token']

    # BW-WEAK-TIER-ENFORCE-20261122：弱档纯优先级队列序（唯一判断=层序+空位；同层由
    # pool_key stable tie 定序，与 _domestic_policy weak 分支同源）：
    # 层1 opencode:deepseek-free(1) > 层1 opencode:glm-free(1) > 层2 codebuddy:glm(1)
    # > 层3 claude(2) > 层4 zcode:GLM-5.3-Flash(1)，合计 6 槽。
    WEAK_QUEUE_COMBOS = (
        ('opencode', dp.OPENCODE_DEEPSEEK_MODEL),
        ('codebuddy', dp.CODEBUDDY_GLM_MODEL),
        ('claude', dp.CLAUDE_MODEL),
        ('claude', dp.CLAUDE_MODEL),
        ('zcode', 'GLM-5.3-Flash'),
    )

    def occupy_weak_above(self, target_pk, prefix='weak-above'):
        """BW-WEAK-TIER-ENFORCE-20261122：弱档纯优先级队列——要把 target_pk 变成队首
        （显式或 AUTO 请求才能被真实 claim），按队列序经 consume_for_entry（入口同款
        路径）把排在其前面的全部空槽真实占满（到 running）；已占用槽自动跳过。
        弱档队列不查 availability/限额短窗/时段门/executor 冲突，占位即真实占位。"""
        toks = []
        for rt, m in self.WEAK_QUEUE_COMBOS:
            pk = dp.pool_key(rt, m)
            if pk == target_pk:
                break
            while dp.status(self.store, now=T0)['pools'][pk]['active'] \
                    < dp.capacity_for(pk):
                task = f'{prefix}-{len(toks)}'
                r = dp.consume_for_entry(
                    self.store, task_id=task, runtime=rt, model=m,
                    workspace=str(self._ws_for(task)), prompt_sha256='p',
                    executor=rt, wrapper_pid=4321, wrapper_created='w1', now=T0)
                self.assertClaimed(r, pk)
                toks.append(r['token'])
        return toks

    def fill_domestic(self):
        """补满全部 17 个物理在途名额（BW-WEAK-RESTRUCTURE-20261116：DOMESTIC_TOTAL_
        CAPACITY 16→17）。BW-WEAK-TIER-ENFORCE-20261122：弱档是**纯优先级队列**——
        weak 可新派 6 槽必须按队列序真实 claim（层1 两 OpenCode 免费模型各 1 →
        层2 codebuddy:glm-5.3-flash 1 → 层3 claude:deepseek-flash 2 →
        层4 zcode:GLM-5.3-Flash 1；显式 executor 请求同样由队列接管，队首组合才被
        claim），弱档无任何时段门/限额短窗/availability 判断。其余填法：
        - strong/medium 专属池（zcode:GLM-5.3=1、两内置 Qwen4-Max-Preview 各 1、
          qodercn:Qwen-3.8-Max=2）经 `dispatch_mode('medium')` 真实 claim 填入
          （自定义 Max 需临时清空用户禁用集复现禁用前物理满池）；
        - 现行无任何档位可新派的 qoder:Qwen3.8-Flash=2、qodercn:Qwen3.8-Flash=2 按真实
          在途形态直插（同 strong 遗留模式）；Sonus 弱档不可新派，用 strong 遗留在途
          补足最后一个物理名额（BW-STRONG-SONUS-20261024）。
        CN 池只经 executor='qodercn' 入口，claude 经 executor='claude'，GLM 经
        executor='codebuddy' 且显式 codebuddy_glm_allowed=True，OpenCode 经
        executor='opencode'，ZCode 两池经 executor='zcode'。
        返回 token 列表，顺序为
        [glm, zcode, qoderMax, cnMax, cnCustomA, cnCustomB, claude, claude,
         qoderFlash, qoderFlash, cnFlash, cnFlash, ocDeepseek, ocGlm]。
        """
        # BW-WEAK-TIER-ENFORCE-20261122：弱档 6 槽按队列序 claim——层1 两 OpenCode 先行，
        # CBGLM 层2 次之（其上方层1 已满），claude 层3、zcode:GLM-5.3-Flash 层4 收尾。
        weak_plan = [('f12', 'opencode', dp.OPENCODE_DEEPSEEK_MODEL, 'opencode'),
                     ('f6', 'claude', 'deepseek-flash', 'claude'),
                     ('f7', 'claude', 'deepseek-flash', 'claude'),
                     ('fzflash', 'zcode', 'GLM-5.3-Flash', 'zcode')]
        oc_toks = [self.reserve_exec(t, rt, md, ex)
                   for (t, rt, md, ex) in weak_plan[:1]]
        glm = dp.reserve(self.store, task_id='fglm', runtime='codebuddy',
                         model='glm-5.3-flash',
                         workspace=str(self._ws_for('fglm')), prompt_sha256='p',
                         executor='codebuddy', codebuddy_glm_allowed=True,
                         now=T0, _preclaim=False)
        self.assertClaimed(glm, 'codebuddy:glm-5.3-flash')
        weak_toks = [self.reserve_exec(t, rt, md, ex)
                     for (t, rt, md, ex) in weak_plan[1:]]
        # strong/medium 专属池经 medium 档真实 claim；自定义 Max 临时清空用户禁用集。
        with dispatch_mode('medium'):
            with user_disabled_pools(frozenset()):
                medium_plan = [('fz', 'zcode', 'GLM-5.3', 'zcode'),
                               ('f2', 'qoder', 'Qwen4-Max-Preview', 'qoder'),
                               ('f3', 'qodercn', 'Qwen4-Max-Preview', 'qodercn'),
                               ('f4', 'qodercn', 'Qwen-3.8-Max', 'qodercn'),
                               ('f5', 'qodercn', 'Qwen-3.8-Max', 'qodercn')]
                medium_toks = [self.reserve_exec(t, rt, md, ex)
                               for (t, rt, md, ex) in medium_plan]
        # 现行无任何档位可新派的两个地区 Flash 槽按真实在途形态直插占位。
        self.seed_attempt('f8', 'f8-task', 'qoder:Qwen3.8-Flash')
        self.seed_attempt('f9', 'f9-task', 'qoder:Qwen3.8-Flash')
        self.seed_attempt('f10', 'f10-task', 'qodercn:Qwen3.8-Flash')
        self.seed_attempt('f11', 'f11-task', 'qodercn:Qwen3.8-Flash')
        toks = [glm['token'], medium_toks[0], medium_toks[1], medium_toks[2],
                medium_toks[3], medium_toks[4], weak_toks[0], weak_toks[1],
                'f8', 'f9', 'f10', 'f11', oc_toks[0], weak_toks[2]]
        # 弱档不能新派 Sonus；用 strong 遗留在途补足第 17 个物理名额。
        insert_sonus_leftover(self.store, reserved_at_utc=dp._iso(T0))
        st = dp.status(self.store, now=T0)
        self.assertEqual(st['domestic']['active'], dp.DOMESTIC_TOTAL_CAPACITY)
        self.assertEqual(st['domestic']['active'], 16)
        self.assertTrue(st['domestic']['full'])
        self.assertEqual(st['pools'][dp.SONUS_POOL_KEY]['active'], 1)
        self.assertEqual(st['pools']['codebuddy:glm-5.3-flash']['active'], 1)
        self.assertEqual(st['pools']['qoder:Qwen4-Max-Preview']['active'], 1)
        self.assertEqual(st['pools']['qodercn:Qwen4-Max-Preview']['active'], 1)
        self.assertEqual(st['pools']['qodercn:Qwen-3.8-Max']['active'], 2)
        self.assertEqual(st['pools']['claude:deepseek-flash']['active'], 2)
        self.assertEqual(st['pools']['opencode:deepseek-v4.1-flash-free']['active'], 1)
        self.assertEqual(st['pools']['opencode:glm-5.3-flash-free']['active'], 0)
        self.assertEqual(st['pools']['qoder:Qwen3.8-Flash']['active'], 2)
        self.assertEqual(st['pools']['qodercn:Qwen3.8-Flash']['active'], 2)
        self.assertEqual(st['pools']['zcode:GLM-5.3']['active'], 1)
        self.assertEqual(st['pools']['zcode:GLM-5.3-Flash']['active'], 1)
        return toks


class CapacityAndKnownPoolTests(_PoolBase):
    def test_capacity_table_matches_allocation_decision(self):
        # BW-CODEBUDDY-MAIN-S3 + BW-WORKER-WIDGET-20261018-S3 + BW-OPENCODE-20261019
        # + BW-ZCODE-RESTORE-1-20261022 真实主力 + BW-STRONG-SONUS-20261024
        # + BW-WEAK-RESTRUCTURE-20261116（弱档收窄 5 池、新增 zcode:GLM-5.3-Flash 独立
        # 1 槽，物理总 16→17）：
        # zcode:GLM-5.3=1（恢复可派、容量严格 1）、zcode:GLM-5.3-Flash=1（弱档专用独立
        # 1 槽）、两内置 Max 各 1、CN 自定义 Max=2、claude:deepseek-flash=2、
        # codebuddy:deepseek-v4.1-flash=1、codebuddy:glm-5.3-flash=1
        # （两 CodeBuddy 模型各自独立 1 槽）、opencode:deepseek-v4.1-flash-free=1、
        # opencode:glm-5.3-flash-free=1（两 OpenCode 免费模型各独立 1 槽）、
        # qoder:Sonus=1（strong 专属）、两地区 Flash 各 2；weak 可新派 6
        # （两 OpenCode 1+1 + CodeBuddy GLM 1 + claude 2 + GLM-5.3-Flash 1）。
        # CN DeepSeek-Flash 退出正常池，保留池名但 capacity=0（在途排干用）。
        self.assertEqual(dp.capacity_for('zcode:GLM-5.3'), 1)
        self.assertEqual(dp.capacity_for('zcode:GLM-5.3-Flash'), 1)
        self.assertEqual(dp.capacity_for('qoder:Qwen4-Max-Preview'), 1)
        self.assertEqual(dp.capacity_for('qodercn:Qwen4-Max-Preview'), 1)
        self.assertEqual(dp.capacity_for('qodercn:Qwen-3.8-Max'), 2)
        self.assertEqual(dp.capacity_for('claude:deepseek-flash'), 2)
        self.assertEqual(dp.capacity_for('qoder:Qwen3.8-Flash'), 2)
        self.assertEqual(dp.capacity_for('qodercn:Qwen3.8-Flash'), 2)
        self.assertEqual(dp.capacity_for('qodercn:DeepSeek-Flash'), 0)
        self.assertIsNone(dp.capacity_for('luna:native'))
        self.assertEqual(dp.capacity_for('qoder:Unknown-Model'), 0)
        self.assertEqual(dp.capacity_for('codebuddy:deepseek-v4.1-flash'), 1)
        self.assertEqual(dp.capacity_for('codebuddy:glm-5.3-flash'), 1)
        self.assertEqual(dp.capacity_for('opencode:deepseek-v4.1-flash-free'), 1)
        self.assertEqual(dp.capacity_for('opencode:glm-5.3-flash-free'), 0)
        self.assertEqual(dp.capacity_for('qoder:Sonus'), 1)
        self.assertEqual(dp.DOMESTIC_TOTAL_CAPACITY, 16)
        self.assertEqual(weak_new_dispatch_capacity(), 5)

    def test_is_known_pool(self):
        for pk in ('zcode:GLM-5.3', 'qoder:Qwen4-Max-Preview',
                   'qoder:Qwen3.8-Flash', 'qodercn:DeepSeek-Flash', 'luna:native'):
            self.assertTrue(dp.is_known_pool(pk), pk)
        self.assertFalse(dp.is_known_pool('qoder:GLM-5.3'))
        self.assertFalse(dp.is_known_pool('zcode:Qwen4-Max-Preview'))

    def test_unknown_pool_refused_on_reserve_and_select(self):
        # BW-WEAK-TIER-ENFORCE-20261122：弱档纯优先级队列会把**完全未知组合**也接管成
        # routing_required 到队首（不查池合法性）——与 _domestic_policy 文档字符串
        # 「完全未知的组合仍走下方 unknown_pool」矛盾，疑似引擎回归、已单独报告；
        # unknown_pool 拒绝机制本身与档位无关，改在 medium 档验证原意。
        with dispatch_mode('medium'):
            r = dp.reserve(self.store, task_id='u1', runtime='qoder', model='Bogus',
                           workspace=str(self._ws_for('u1')), prompt_sha256='p',
                           now=T0, _preclaim=False)
            self.assertRejected(r, 'unknown_pool')
            self.assertFalse(r.get('sent', True))
            r2 = dp.select_and_claim(self.store, task_id='u2', runtime='openai',
                                     model='gpt-x',
                                     workspace=str(self._ws_for('u2')),
                                     prompt_sha256='p', now=T0, _preclaim=False)
            self.assertRejected(r2, 'unknown_pool')

    def test_luna_never_auto_selected_or_reserved_as_normal(self):
        r = dp.select_and_claim(self.store, task_id='l1', runtime='luna', model='native',
                                workspace=str(self._ws_for('l1')), prompt_sha256='p',
                                now=T0, _preclaim=False)
        self.assertRejected(r, 'luna_disabled')
        self.assertFalse(r.get('sent', True))

    def test_reserve_respects_pool_capacity(self):
        # 1:1 轮转下不能连续预留同一主力；合法填满全部 17 物理槽后新派一律 capacity_full
        # （BW-WEAK-RESTRUCTURE-20261116 起总量 17）。两个 Qwen3.8-Flash 池已无任何档位
        # 可新派，总守卫改由 medium 档可派的 CN 内置 Max 入口承载（机制不变）。
        self.fill_domestic()
        with dispatch_mode('medium'):
            over = dp.reserve(self.store, task_id='c2', runtime='qodercn',
                              model='Qwen4-Max-Preview',
                              workspace=str(self._ws_for('c2')), prompt_sha256='p',
                              executor='qodercn', now=T0, _preclaim=False)
        self.assertRejected(over, 'capacity_full')
        self.assertTrue(over.get('domestic_full'))
        # 单池上限 2 从未被突破（两地区 Flash 占位为真实在途直插行）。
        self.assertEqual(dp.status(self.store, now=T0)['pools']['qoder:Qwen3.8-Flash']['active'],
                         2)

    def test_duplicate_task_in_flight_refused(self):
        self.reserve('dup', 'qodercn', 'Qwen-3.8-Max')
        r = dp.reserve(self.store, task_id='dup', runtime='qoder', model='Qwen4-Max-Preview',
                       workspace=str(self._ws_for('dup')), prompt_sha256='p',
                       now=T0, _preclaim=False)
        self.assertRejected(r, 'duplicate_task_in_flight')
        r2 = dp.select_and_claim(self.store, task_id='dup', runtime='qoder',
                                 model='Qwen4-Max-Preview', workspace=str(self._ws_for('dup')),
                                 prompt_sha256='p', now=T0, _preclaim=False)
        self.assertRejected(r2, 'duplicate_task_in_flight')


class MainForceRotationTests(_PoolBase):
    def test_select_prefers_max_then_claude_then_opencode(self):
        # BW-WEAK-RESTRUCTURE-20261116：AUTO 层序按档取自 dispatch_mode——medium 档层序为
        # 三 Max(1) > claude(2) > zcode:GLM-5.3(3)；两 OpenCode/CBGLM 已不在该档集合，
        # Max 全满且 Flash 兜底不在档时如实 mode_excluded。两 OpenCode 免费模型改由
        # weak 档独立验证（弱档最高层=1）。本用例先在 medium 档验证 Max→claude 走位，
        # 再回 weak 档验证 OpenCode 免费槽真实 claim。
        with dispatch_mode('medium'):
            r1 = self.select('t1', 'qoder', 'Qwen4-Max-Preview')
            # 层1 三 Max 平票时请求组合优先（CN 自定义 Max 已被用户禁用、不进候选）。
            self.assertClaimed(r1, dp.QMAX_POOL_KEY)
            self.reserve_exec('m1', 'qodercn', 'Qwen4-Max-Preview', 'qodercn')
            self.reserve_exec('m2', 'qodercn', 'Qwen-3.8-Max', 'qodercn')
            self.reserve_exec('m3', 'qodercn', 'Qwen-3.8-Max', 'qodercn')
            r2 = self.select('t2', 'qoder', 'Qwen4-Max-Preview')
            self.assertRouting(r2, 'claude:deepseek-flash')
            self.assertFalse(r2.get('sent', True))
            self.reserve_exec('z0', 'zcode', 'GLM-5.3', 'zcode')
            r2b = self.select('t2b', 'qoder', 'Qwen4-Max-Preview')
            self.assertRouting(r2b, 'claude:deepseek-flash')
            self.assertFalse(r2b.get('sent', True))
            self.reserve_exec('c0', 'claude', 'deepseek-flash', 'claude')
            self.reserve_exec('c1', 'claude', 'deepseek-flash', 'claude')
            r3 = self.select('t3', 'qoder', 'Qwen4-Max-Preview')
            # medium 主力全满且两 Flash 兜底不在该档 → mode_excluded（绝不跨档兜底）。
            self.assertFalse(r3.get('allowed'), r3)
            self.assertRejected(r3, 'mode_excluded')
        # weak 档：两 OpenCode 免费模型是最高层（层1），AUTO 请求即真实 claim。
        r4 = self.select('t4w', 'opencode', dp.OPENCODE_DEEPSEEK_MODEL)
        self.assertClaimed(r4, dp.OPENCODE_DEEPSEEK_POOL_KEY)

    def test_select_prefers_requested_combo_on_tie(self):
        # 同层平票时优先请求组合：BW-WEAK-TIER-ENFORCE-20261122 后弱档是纯优先级队列，
        # 层1 两 OpenCode 平票由队序（pool_key stable tie）定序——deepseek-free 恒为
        # 队首；请求组合恰为队首时仍直接 claim（原「0 在途平票时请求组合优先」原意
        # 保留为：AUTO 请求队首组合即真实 claim）。先在 medium 档占满 strong/medium
        # 专属主力（保持原「占满上方层」的场景形态），再回 weak 档按队列序验证：
        # 请求 opencode:deepseek → 真实 claim；排空层1 后 CBGLM（层2）真实 claim。
        with dispatch_mode('medium'):
            self.reserve_exec('t0', 'zcode', 'GLM-5.3', 'zcode')
            self.reserve_exec('t1', 'qodercn', 'Qwen4-Max-Preview', 'qodercn')
            self.reserve_exec('t2', 'qodercn', 'Qwen-3.8-Max', 'qodercn')
            self.reserve_exec('t3', 'qodercn', 'Qwen-3.8-Max', 'qodercn')
            self.reserve_exec('t4', 'qoder', 'Qwen4-Max-Preview', 'qoder')
            self.reserve_exec('t5', 'claude', 'deepseek-flash', 'claude')
            self.reserve_exec('t6', 'claude', 'deepseek-flash', 'claude')
        r = self.select('tie', 'opencode', dp.OPENCODE_DEEPSEEK_MODEL)
        # BW-WEAK-TIER-ENFORCE-20261122：层1 两 OpenCode 0 在途平票、队首=deepseek-free
        # → 请求组合即队首，被直接真实 claim。
        self.assertClaimed(r, dp.OPENCODE_DEEPSEEK_POOL_KEY)
        # CBGLM 为弱档层2：按队列序排空层1 剩余槽（glm-free）后，显式 executor 请求
        # 仍由队列接管、此时队首=CBGLM 被真实 claim。
        self.occupy_weak_above(dp.CODEBUDDY_GLM_POOL_KEY, prefix='tie-above')
        self.reserve_exec('t7', 'codebuddy', dp.CODEBUDDY_GLM_MODEL, 'codebuddy')

    def test_select_when_both_main_full_no_cross_mode_overflow(self):
        # BW-WEAK-RESTRUCTURE-20261116：两地区 Qwen3.8-Flash 已从**所有档位**移除（纯
        # mode_excluded），原「主力全满 → 同级 Flash 兜底」路径随溢出池移除而消失。
        # BW-WEAK-TIER-ENFORCE-20261122：弱档 6 槽全满后队列走完 → 任何 AUTO 请求如实
        # capacity_full（原意「绝不偷换模型落库/绝不跨档兜底」保留：sent=false、
        # 零新占位，绝不偷换记到 Flash 池）。
        for i, pool in enumerate([dp.OPENCODE_DEEPSEEK_POOL_KEY,
                                  dp.CODEBUDDY_GLM_POOL_KEY,
                                  dp.CLAUDE_POOL_KEY, dp.CLAUDE_POOL_KEY,
                                  'zcode:GLM-5.3-Flash']):
            self.seed_attempt(f'of-main-{i}', f'of-main-task-{i}', pool)
        r = self.select('of', 'claude', 'deepseek-flash')
        self.assertFalse(r.get('allowed'), r)
        # BW-WEAK-TIER-ENFORCE-20261122：队列全满 → ('full', None) → capacity_full。
        self.assertRejected(r, 'capacity_full')
        self.assertFalse(r.get('routing_required'), r)
        self.assertFalse(r.get('sent', True), r)
        with closing(dp.connect(self.store)) as conn:
            rows = conn.execute('SELECT token FROM attempts '
                                "WHERE pool_key='qoder:Qwen3.8-Flash'").fetchall()
        self.assertEqual(rows, [])

    def test_select_capacity_full_when_all_main_occupied(self):
        self.fill_domestic()
        # BW-WEAK-RESTRUCTURE-20261116：weak 默认档 AUTO 请求已移出弱档的 qoder Max →
        # mode_excluded；「全满 → capacity_full」机制按现行口径在 medium 档验证：
        # 显式 executor 入口由物理总守卫（17）如实 capacity_full，绝不跨档兜底。
        r = self.select('full-weak', 'qoder', 'Qwen4-Max-Preview')
        self.assertRejected(r, 'mode_excluded')
        with dispatch_mode('medium'):
            over = dp.select_and_claim(self.store, task_id='full', runtime='qoder',
                                       model='Qwen4-Max-Preview',
                                       workspace=str(self._ws_for('full')),
                                       prompt_sha256='p', executor='qoder',
                                       now=T0, _preclaim=False)
        self.assertRejected(over, 'capacity_full')
        self.assertTrue(over.get('domestic_full'))
        self.assertFalse(over.get('sent', True))


class OverflowRoutingTests(_PoolBase):
    def test_flash_refused_while_main_force_has_room(self):
        # BW-WEAK-RESTRUCTURE-20261116：qoder:Qwen3.8-Flash 已从**所有档位**的可新派集合
        # 移除（纯 mode_excluded），原「主力有空 → 拒 Flash 并 routing 到主力」随溢出池
        # 移除而消失。改验现行语义：AUTO 请求 Flash 一律 mode_excluded（sent=false、
        # 零新占位、绝不偷换模型）。
        r = self.select('f0', 'qoder', 'Qwen3.8-Flash')
        self.assertFalse(r.get('allowed'), r)
        self.assertRejected(r, 'mode_excluded')
        self.assertFalse(r.get('routing_required'), r)
        self.assertFalse(r.get('sent', True), r)

    def test_flash_never_claimed_even_when_main_force_full(self):
        # BW-WEAK-RESTRUCTURE-20261116：原「主力全满 → Flash 即 claim」路径随溢出池从
        # 所有档位移除而消失。改验：即便全部主力占满（strong/medium 专属池按真实形态
        # 直插占位），AUTO/显式请求 Flash 仍一律 mode_excluded，绝不落库占槽。
        for i, pool in enumerate(['zcode:GLM-5.3', 'qoder:Qwen4-Max-Preview',
                                  'qodercn:Qwen4-Max-Preview',
                                  'qodercn:Qwen-3.8-Max', 'qodercn:Qwen-3.8-Max',
                                  dp.CLAUDE_POOL_KEY, dp.CLAUDE_POOL_KEY,
                                  dp.OPENCODE_DEEPSEEK_POOL_KEY,
                                  dp.CODEBUDDY_GLM_POOL_KEY,
                                  'zcode:GLM-5.3-Flash']):
            self.seed_attempt(f'ff-m{i}', f'ff-main-task-{i}', pool)
        r = self.select('fok', 'qoder', 'Qwen3.8-Flash')
        self.assertFalse(r.get('allowed'), r)
        self.assertRejected(r, 'mode_excluded')
        q = dp.reserve(self.store, task_id='fq', runtime='qoder', model='Qwen3.8-Flash',
                       workspace=str(self._ws_for('fq')), prompt_sha256='p',
                       executor='qoder', now=T0, _preclaim=False)
        self.assertFalse(q.get('allowed'), q)
        self.assertRejected(q, 'mode_excluded')
        with closing(dp.connect(self.store)) as conn:
            rows = conn.execute("SELECT token FROM attempts WHERE task_id IN "
                                "('fok', 'fq')").fetchall()
        self.assertEqual(rows, [])

    def test_flash_capacity_never_exceeded_after_two(self):
        # BW-WEAK-RESTRUCTURE-20261116：两地区 Flash 池已无任何档位可新派；容量 2 的
        # 物理登记与在途计数保留——按真实形态直插 2 条国际 Flash 在途占位后，第三个
        # Flash 请求（AUTO 与显式 executor）一律 mode_excluded，在途恒为 2、绝不超容。
        self.seed_attempt('f0', 'f0-task', 'qoder:Qwen3.8-Flash')
        self.seed_attempt('f1', 'f1-task', 'qoder:Qwen3.8-Flash')
        r = self.select('f2', 'qoder', 'Qwen3.8-Flash')
        self.assertFalse(r.get('allowed'), r)
        self.assertRejected(r, 'mode_excluded')
        # 显式 executor='qoder'：同样 mode_excluded（档位硬策略先于容量/兜底判断）。
        q = dp.reserve(self.store, task_id='fq', runtime='qoder', model='Qwen3.8-Flash',
                       workspace=str(self._ws_for('fq')), prompt_sha256='p',
                       executor='qoder', now=T0, _preclaim=False)
        self.assertFalse(q.get('allowed'), q)
        self.assertRejected(q, 'mode_excluded')
        self.assertEqual(dp.status(self.store, now=T0)['pools']['qoder:Qwen3.8-Flash']
                         ['active'], 2)


class SameWorkspaceGuardTests(_PoolBase):
    def test_same_real_workspace_two_writers_only_one_claims(self):
        # 真正同一真实目录：不同 task 竞争同一 workspace，只有一个能 claim（守卫不放松）。
        # 准备用显式 executor 固定组合，绕过 AUTO 跨层改派（BW-MAIN-PRIORITY-S1），单写入
        # 守卫断言原样保留。
        shared = self._ws_for('shared')
        # BW-ZCODE-RETIRE-20261020：首位写入者改用现役 OpenCode 免费槽（原 zcode 占位已退役）。
        r1 = dp.reserve(self.store, task_id='w1', runtime='opencode',
                        model=dp.OPENCODE_DEEPSEEK_MODEL,
                        workspace=str(shared), prompt_sha256='p', executor='opencode',
                        now=T0, _preclaim=False)
        self.assertClaimed(r1, dp.OPENCODE_DEEPSEEK_POOL_KEY)
        # BW-WEAK-RESTRUCTURE-20261116：qoder:Qwen4-Max-Preview 已移出弱档——单写入守卫
        # 机制改在 medium 档验证（该池 medium 可派、T0 北京 23:00 无夜间门）。
        with dispatch_mode('medium'):
            r2 = dp.reserve(self.store, task_id='w2', runtime='qoder',
                            model='Qwen4-Max-Preview',
                            workspace=str(shared), prompt_sha256='p', executor='qoder',
                            now=T0, _preclaim=False)
            self.assertFalse(r2.get('allowed'), r2)
            self.assertEqual(r2.get('reason'), 'workspace_in_flight')
            # 释放 w1 后，同一真实目录恢复可写。
            dp.finish(self.store, r1['token'], terminal='cancelled', now=T0)
            r3 = dp.reserve(self.store, task_id='w2', runtime='qoder',
                            model='Qwen4-Max-Preview',
                            workspace=str(shared), prompt_sha256='p', executor='qoder',
                            now=T0, _preclaim=False)
            self.assertTrue(r3.get('allowed'), r3)


class ValidateClaimTests(_PoolBase):
    def test_validate_ok_when_exact(self):
        # BW-ZCODE-RETIRE-20261020：原 zcode 夹具组合已退役，改用现役 qoder Max。
        # BW-WEAK-RESTRUCTURE-20261116：qoder Max 已移出弱档，占位准备在 medium 档完成
        # （validate/finish 生命周期本身与档位无关）。
        with dispatch_mode('medium'):
            tok = self.reserve('v', 'qoder', 'Qwen4-Max-Preview', prompt='abc123')
        chk = dp.validate_claim(self.store, tok, task_id='v', runtime='qoder',
                                model='Qwen4-Max-Preview', workspace=str(self._ws_for('v')),
                                prompt_sha256='abc123')
        self.assertTrue(chk['ok'], chk['reasons'])
        self.assertFalse(chk['drift'])

    def test_validate_detects_each_drift(self):
        with dispatch_mode('medium'):  # BW-WEAK-RESTRUCTURE-20261116：占位经 medium 档。
            tok = self.reserve('v', 'qoder', 'Qwen4-Max-Preview', prompt='abc123')
        ws = str(self._ws_for('v'))
        self.assertTrue(dp.validate_claim(self.store, tok, model='Qwen3.8-Flash')['drift'])
        self.assertTrue(dp.validate_claim(self.store, tok, runtime='codebuddy')['drift'])
        self.assertTrue(dp.validate_claim(self.store, tok, task_id='other')['drift'])
        self.assertTrue(dp.validate_claim(self.store, tok, prompt_sha256='zzz')['drift'])
        self.assertTrue(dp.validate_claim(self.store, tok,
                                          workspace=str(self._ws_for('other')))['drift'])

    def test_validate_unknown_token(self):
        chk = dp.validate_claim(self.store, 'no-such-token')
        self.assertFalse(chk['ok'])
        self.assertTrue(chk['drift'])

    def test_validate_terminal_claim_cannot_consume(self):
        with dispatch_mode('medium'):  # BW-WEAK-RESTRUCTURE-20261116：占位经 medium 档。
            tok = self.reserve('v', 'qoder', 'Qwen4-Max-Preview')
        # reserved 无 child：只有 cancelled/start_failed 合法释放（finished 需 child 证据）。
        dp.finish(self.store, tok, terminal='cancelled', now=T0)
        chk = dp.validate_claim(self.store, tok, task_id='v')
        self.assertFalse(chk['ok'])
        self.assertTrue(any('terminal' in r for r in chk['reasons']), chk['reasons'])


class ConsumeForEntryTests(_PoolBase):
    def test_consume_with_valid_claim_token_marks_running(self):
        # BW-WEAK-RESTRUCTURE-20261116：qoder Max 已移出弱档，reserve/consume 全程在
        # medium 档进行（T0 北京 23:00 无夜间门）。
        with dispatch_mode('medium'):
            tok = self.reserve('e', 'qoder', 'Qwen4-Max-Preview', prompt='pp')
            r = dp.consume_for_entry(self.store, task_id='e', runtime='qoder',
                                     model='Qwen4-Max-Preview',
                                     workspace=str(self._ws_for('e')),
                                     prompt_sha256='pp', claim_token=tok,
                                     wrapper_pid=4321, wrapper_created='w1', now=T0)
        self.assertTrue(r['allowed'], r)
        self.assertTrue(r['claimed'])
        self.assertEqual(r['token'], tok)
        self.assertEqual(self._state(tok), 'running')
        st = dp.status(self.store, now=T0)['pools']['qoder:Qwen4-Max-Preview']
        self.assertEqual(st['active'], 1)

    def test_consume_with_drifted_claim_refused(self):
        with dispatch_mode('medium'):  # BW-WEAK-RESTRUCTURE-20261116：占位经 medium 档。
            tok = self.reserve('e', 'qoder', 'Qwen4-Max-Preview', prompt='pp')
            r = dp.consume_for_entry(self.store, task_id='e', runtime='qoder',
                                     model='Qwen3.8-Flash', workspace=str(self._ws_for('e')),
                                     prompt_sha256='pp', claim_token=tok,
                                     wrapper_pid=4321, wrapper_created='w1', now=T0)
        self.assertFalse(r['allowed'], r)
        self.assertEqual(r['reason'], 'claim_invalid')
        self.assertFalse(r.get('sent', True))
        self.assertTrue(r.get('drift'))
        # 漂移拒绝后 token 从未被消费，仍 reserved。
        self.assertEqual(self._state(tok), 'reserved')

    def test_consume_without_token_routes(self):
        # 无 token → 走 select_and_claim：空池请求层1 组合即被 AUTO 选中并直接 claim
        # （无需改派）。BW-WEAK-RESTRUCTURE-20261116：CN 自定义 Max 已被用户禁用、
        # 整个 qoder Max 组移出弱档——机制改在 medium 档用层1 国际 Max（同层平票时
        # 请求组合优先）承载。
        with dispatch_mode('medium'):
            r = dp.consume_for_entry(self.store, task_id='n', runtime='qoder',
                                     model='Qwen4-Max-Preview',
                                     workspace=str(self._ws_for('n')),
                                     prompt_sha256='p', wrapper_pid=4321,
                                     wrapper_created='w1', now=T0)
        self.assertTrue(r['allowed'], r)
        self.assertTrue(r['claimed'])
        self.assertEqual(r['pool_key'], dp.QMAX_POOL_KEY)

    def test_consume_without_token_can_route_wrong_combo(self):
        # BW-ZCODE-RETIRE-20261020：原 zcode 请求组合已退役（会先撞 unknown_pool），
        # 改用现役 qoder Max：占满其唯一槽后，无 token 的同组合消费必须 routing 改道。
        # BW-WEAK-RESTRUCTURE-20261116：qoder Max 移出弱档，机制改在 medium 档验证。
        with dispatch_mode('medium'):
            self.reserve('q0', 'qoder', 'Qwen4-Max-Preview')
            r = dp.consume_for_entry(self.store, task_id='n2', runtime='qoder',
                                     model='Qwen4-Max-Preview',
                                     workspace=str(self._ws_for('n2')),
                                     prompt_sha256='p', wrapper_pid=4321,
                                     wrapper_created='w1', now=T0)
        self.assertFalse(r['allowed'], r)
        self.assertTrue(r['routing_required'])
        self.assertFalse(r.get('sent', True))

    def test_consume_without_token_enforces_capacity_and_dedup(self):
        # exact_combo 已废弃：无 token 的自动消费仍严格受容量与防重约束，不得绕过 1:1。
        self.fill_domestic()
        # BW-WEAK-RESTRUCTURE-20261116：qoder Flash 已无任何档位可派，容量守卫改用
        # medium 档可派的 CN 内置 Max 无 token 消费承载（17 槽物理总守卫）。
        with dispatch_mode('medium'):
            over = dp.consume_for_entry(self.store, task_id='new', runtime='qodercn',
                                        model='Qwen4-Max-Preview',
                                        workspace=str(self._ws_for('new')),
                                        prompt_sha256='p', wrapper_pid=4321,
                                        wrapper_created='w1', now=T0)
        self.assertRejected(over, 'capacity_full')
        # 防重断言用在途的 f2（qoder Max，fill 的第三个 token）。
        dup = dp.consume_for_entry(self.store, task_id='f2', runtime='qoder',
                                   model='Qwen4-Max-Preview', workspace=str(self._ws_for('f2')),
                                   prompt_sha256='p', wrapper_pid=4321,
                                   wrapper_created='w1', now=T0)
        self.assertRejected(dup, 'duplicate_task_in_flight')


class ReleaseAndUnknownTests(_PoolBase):
    # BW-WEAK-RESTRUCTURE-20261116：qoder:Qwen4-Max-Preview 已移出弱档——本类全部
    # 生命周期用例的占位准备统一在 medium 档完成（T0 北京 23:00 无夜间门）；
    # finish/mark_unknown/reconcile 生命周期语义本身与档位无关。
    def test_cancel_releases_reserved_capacity_immediately(self):
        with dispatch_mode('medium'):
            tok = self.reserve('r', 'qoder', 'Qwen4-Max-Preview')
        self.assertEqual(dp.status(self.store, now=T0)['pools']['qoder:Qwen4-Max-Preview']['active'], 1)
        res = dp.finish(self.store, tok, terminal='cancelled', now=T0)
        self.assertTrue(res['released'])
        self.assertEqual(dp.status(self.store, now=T0)['pools']['qoder:Qwen4-Max-Preview']['active'], 0)

    def test_finished_on_reserved_no_child_refused(self):
        # finished 需要 child 真实退出证据；reserved 无 child 用 finished → 拒绝，不释放。
        with dispatch_mode('medium'):
            tok = self.reserve('r', 'qoder', 'Qwen4-Max-Preview')
        res = dp.finish(self.store, tok, terminal='finished', now=T0)
        self.assertFalse(res['released'], res)
        self.assertEqual(res.get('reason'), 'no_child_terminal_unverified')
        self.assertEqual(dp.status(self.store, now=T0)['pools']['qoder:Qwen4-Max-Preview']['active'], 1)

    def test_start_failed_and_cancelled_release(self):
        t1 = self.reserve('a', 'qodercn', 'Qwen-3.8-Max')
        with dispatch_mode('medium'):
            t2 = self.reserve('b', 'qoder', 'Qwen4-Max-Preview')
        dp.finish(self.store, t1, terminal='start_failed', now=T0)
        dp.finish(self.store, t2, terminal='cancelled', now=T0)
        st = dp.status(self.store, now=T0)['pools']
        self.assertEqual(st['qodercn:Qwen-3.8-Max']['active'], 0)
        self.assertEqual(st['qoder:Qwen4-Max-Preview']['active'], 0)

    def test_finish_rejects_non_release_terminal(self):
        with dispatch_mode('medium'):
            tok = self.reserve('r', 'qoder', 'Qwen4-Max-Preview')
        with self.assertRaises(ValueError):
            dp.finish(self.store, tok, terminal='unknown')

    def test_double_finish_second_is_noop(self):
        with dispatch_mode('medium'):
            tok = self.reserve('r', 'qoder', 'Qwen4-Max-Preview')
        self.assertTrue(dp.finish(self.store, tok, terminal='cancelled', now=T0)['released'])
        self.assertFalse(dp.finish(self.store, tok, terminal='cancelled', now=T0)['released'])

    def test_mark_unknown_holds_capacity(self):
        with dispatch_mode('medium'):
            tok = self.reserve('u', 'qoder', 'Qwen4-Max-Preview')
        res = dp.mark_unknown(self.store, tok, now=T0)
        self.assertTrue(res['capacity_held'])
        self.assertFalse(res['released'])
        self.assertEqual(dp.status(self.store, now=T0)['pools']['qoder:Qwen4-Max-Preview']['active'], 1)

    def test_finish_unknown_no_child_requires_owner(self):
        # 进入 Popen-bind 窗口（有 wrapper）后 mark_unknown：无 owner 身份绝不释放；
        # 记录的真实 owner 亲证取消才释放（绝不凭一个终态参数假定未启动）。
        with dispatch_mode('medium'):
            tok = self.reserve('u', 'qoder', 'Qwen4-Max-Preview')
        dp.mark_running(self.store, tok, wrapper_pid=7000, wrapper_created='c7000', now=T0)
        dp.mark_unknown(self.store, tok, now=T0)
        refused = dp.finish(self.store, tok, terminal='cancelled', now=T0)
        self.assertFalse(refused['released'], refused)
        self.assertEqual(refused.get('reason'), 'owner_unverified')
        self.assertEqual(dp.status(self.store, now=T0)['pools']['qoder:Qwen4-Max-Preview']['active'], 1)
        released = dp.finish(self.store, tok, terminal='cancelled', now=T0,
                             wrapper_pid=7000, wrapper_created='c7000')
        self.assertTrue(released['released'], released)
        self.assertEqual(dp.status(self.store, now=T0)['pools']['qoder:Qwen4-Max-Preview']['active'], 0)

    def test_real_child_death_releases_before_report(self):
        # 绑定真实 child 后其死亡（真实 Windows 死 PID created=None）→ finish 释放容量。
        # BW-ZCODE-RETIRE-20261020：原 zcode 夹具已退役，改用现役 qoder Max。
        with dispatch_mode('medium'):
            tok = self.reserve('d', 'qoder', 'Qwen4-Max-Preview')
        dp.mark_running(self.store, tok, wrapper_pid=8000, wrapper_created='c8000', now=T0)
        dp.bind_child(self.store, tok, 8001, child_created='c8001',
                      wrapper_pid=8000, wrapper_created='c8000', now=T0)
        res = dp.finish(self.store, tok, terminal='finished', now=T0,
                        prober=_fake_prober({8001: ('dead', None)}))
        self.assertTrue(res['released'], res)
        self.assertEqual(dp.status(self.store, now=T0)['pools']['qoder:Qwen4-Max-Preview']['active'], 0)


def _fake_prober(live_map):
    """可注入 prober：pid -> (state, created)。绝不真实探测进程。"""
    def probe(pid):
        state, created = live_map.get(int(pid), ('dead', None))
        return {'pid': int(pid), 'state': state, 'created': created}
    return probe


class ReconcileTests(_PoolBase):
    def setUp(self):
        # BW-WEAK-RESTRUCTURE-20261116：本类占位组合 qoder:Qwen4-Max-Preview 已移出弱档
        # （weak 一律 mode_excluded），占位准备统一在 medium 档执行；reconcile/释放语义
        # 本身与档位无关，断言原样保留。
        ctx = dispatch_mode('medium')
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        super().setUp()

    def _bind(self, tok, wrapper_pid, child_pid=None, wrapper_created='w',
              child_created=None):
        dp.mark_running(self.store, tok, wrapper_pid=wrapper_pid,
                        wrapper_created=wrapper_created, now=T0)
        if child_pid is not None:
            dp.bind_child(self.store, tok, child_pid, child_created=child_created,
                          wrapper_pid=wrapper_pid, wrapper_created=wrapper_created, now=T0)

    def test_wrapper_alive_promotes_reserved_to_running(self):
        # BW-ZCODE-RETIRE-20261020：原 zcode 夹具已退役，reconcile 断言改用现役 qoder Max。
        tok = self.reserve('p', 'qoder', 'Qwen4-Max-Preview')
        dp.mark_running(self.store, tok, wrapper_pid=1000, wrapper_created='c1000', now=T0)
        # 手动退回 reserved 以验证 promote 分支（closing 确保 Windows 下句柄释放）。
        with closing(dp.connect(self.store)) as conn:
            conn.execute("UPDATE attempts SET state='reserved' WHERE token=?", (tok,))
        res = dp.reconcile(self.store, prober=_fake_prober({1000: ('alive', 'c1000')}),
                           now=T0)
        self.assertIn(tok, res['promoted'])
        self.assertEqual(self._state(tok), 'running')

    def test_wrapper_dead_child_alive_match_kept(self):
        tok = self.reserve('q', 'qoder', 'Qwen4-Max-Preview')
        self._bind(tok, wrapper_pid=2000, child_pid=2001,
                   wrapper_created='c2000', child_created='c2001')
        res = dp.reconcile(self.store, prober=_fake_prober(
            {2000: ('dead', 'c2000'), 2001: ('alive', 'c2001')}), now=T0)
        # child 活且出生匹配 → 保持在途，绝不释放（wrapper 死不影响正在运行的 child）。
        self.assertNotIn(tok, res['released'])
        self.assertFalse(any(h['token'] == tok for h in res['held']), res)
        self.assertEqual(self._state(tok), 'running')
        self.assertEqual(dp.status(self.store, now=T0)['pools']['qoder:Qwen4-Max-Preview']['active'], 1)

    def test_wrapper_dead_child_dead_created_match_released(self):
        tok = self.reserve('r', 'qoder', 'Qwen4-Max-Preview')
        self._bind(tok, wrapper_pid=3000, child_pid=3001,
                   wrapper_created='c3000', child_created='c3001')
        res = dp.reconcile(self.store, prober=_fake_prober(
            {3000: ('dead', 'c3000'), 3001: ('dead', 'c3001')}), now=T0)
        self.assertIn(tok, res['released'])
        self.assertEqual(dp.status(self.store, now=T0)['pools']['qoder:Qwen4-Max-Preview']['active'], 0)

    def test_real_dead_pid_created_none_released(self):
        # 真实 Windows 死 PID 的 created=None 属正常，不额外要求创建时刻。
        tok = self.reserve('rd', 'qoder', 'Qwen4-Max-Preview')
        self._bind(tok, wrapper_pid=3100, child_pid=3101,
                   wrapper_created='c3100', child_created='c3101')
        res = dp.reconcile(self.store, prober=_fake_prober(
            {3100: ('dead', None), 3101: ('dead', None)}), now=T0)
        self.assertIn(tok, res['released'])

    def test_child_alive_pid_reuse_held(self):
        tok = self.reserve('s', 'qoder', 'Qwen4-Max-Preview')
        self._bind(tok, wrapper_pid=4000, child_pid=4001,
                   wrapper_created='c4000', child_created='c4001')
        # child 现在活着但出生时刻与记录不同（PID 复用嫌疑）→ unknown 不抢占。
        res = dp.reconcile(self.store, prober=_fake_prober(
            {4000: ('dead', 'c4000'), 4001: ('alive', 'DIFFERENT')}), now=T0)
        self.assertTrue(any(h['token'] == tok for h in res['held']), res)
        self.assertNotIn(tok, res['released'])
        self.assertEqual(self._state(tok), 'unknown')

    def test_child_alive_created_unknown_held(self):
        tok = self.reserve('t', 'qoder', 'Qwen4-Max-Preview')
        self._bind(tok, wrapper_pid=5000, child_pid=5001,
                   wrapper_created='c5000', child_created='c5001')
        res = dp.reconcile(self.store, prober=_fake_prober(
            {5000: ('dead', 'c5000'), 5001: ('alive', None)}), now=T0)
        self.assertTrue(any(h['token'] == tok for h in res['held']), res)
        self.assertEqual(self._state(tok), 'unknown')

    def test_wrapper_dead_child_never_bound_held(self):
        tok = self.reserve('u', 'qoder', 'Qwen4-Max-Preview')
        dp.mark_running(self.store, tok, wrapper_pid=6000, wrapper_created='c6000', now=T0)
        res = dp.reconcile(self.store, prober=_fake_prober({6000: ('dead', 'c6000')}), now=T0)
        self.assertTrue(any(h['token'] == tok for h in res['held']), res)
        self.assertEqual(self._state(tok), 'unknown')

    def test_never_consumed_reserved_left_reserved(self):
        # 从未消费的合法预留（reserved、无 wrapper、无 child）：reconcile 保持 reserved，
        # 绝不翻 unknown（否则会破坏 consume 的同事务 CAS 竞争并令合法 cancel 误报）。
        tok = self.reserve('v', 'qoder', 'Qwen4-Max-Preview')
        res = dp.reconcile(self.store, prober=_fake_prober({}), now=T0)
        self.assertNotIn(tok, res['released'])
        self.assertFalse(any(h['token'] == tok for h in res['held']), res)
        self.assertEqual(self._state(tok), 'reserved')
        self.assertEqual(dp.status(self.store, now=T0)['pools']['qoder:Qwen4-Max-Preview']['active'], 1)


class NeverConsumedReservationTests(_PoolBase):
    """item-2 第二缺陷：合法未消费预留必须保持可合法取消，且与 consume 单赢家结算。"""

    def setUp(self):
        # BW-WEAK-RESTRUCTURE-20261116：本类占位/消费组合 qoder:Qwen4-Max-Preview 已移出
        # 弱档（weak 下 reserve/consume 一律 mode_excluded），整类用例统一在 medium 档
        # 执行；reserve/consume/cancel 单赢家语义与档位无关，断言原样保留。
        ctx = dispatch_mode('medium')
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        super().setUp()

    def test_reserve_reconcile_cancel_releases(self):
        # BW-ZCODE-RETIRE-20261020：原 zcode 夹具已退役，改用现役 qoder Max。
        tok = self.reserve('nc', 'qoder', 'Qwen4-Max-Preview')
        # reconcile 绝不把从未消费的 reserved 翻成 unknown。
        res = dp.reconcile(self.store, prober=_fake_prober({}), now=T0)
        self.assertNotIn(tok, res['released'])
        self.assertFalse(any(h['token'] == tok for h in res['held']), res)
        self.assertEqual(self._state(tok), 'reserved')
        self.assertEqual(dp.status(self.store, now=T0)['pools']['qoder:Qwen4-Max-Preview']['active'], 1)
        # 合法取消释放这个干净名额（不再误报 owner_unverified）。
        out = dp.finish(self.store, tok, terminal='cancelled', now=T0)
        self.assertTrue(out['released'], out)
        self.assertEqual(self._state(tok), 'cancelled')
        self.assertEqual(dp.status(self.store, now=T0)['pools']['qoder:Qwen4-Max-Preview']['active'], 0)

    def test_consume_wins_then_non_owner_cancel_refused(self):
        tok = self.reserve('sw', 'qoder', 'Qwen4-Max-Preview', stage='s1', chat_id='c1')
        ws = str(self._ws_for('sw'))
        c = dp.consume_for_entry(self.store, task_id='sw', runtime='qoder',
                                 model='Qwen4-Max-Preview',
                                 workspace=ws, prompt_sha256='p', stage='s1', chat_id='c1',
                                 claim_token=tok, wrapper_pid=4321, wrapper_created='w1',
                                 now=T0)
        self.assertTrue(c['allowed'], c)
        self.assertEqual(self._state(tok), 'running')
        # consume 已赢：非 owner 的 cancel 不得抢占释放（单赢家，绝不双结算）。
        out = dp.finish(self.store, tok, terminal='cancelled', now=T0)
        self.assertFalse(out['released'], out)
        self.assertEqual(out.get('reason'), 'owner_unverified')
        self.assertEqual(self._state(tok), 'running')

    def test_cancel_wins_then_consume_refused(self):
        tok = self.reserve('cf', 'qoder', 'Qwen4-Max-Preview', stage='s1', chat_id='c1')
        ws = str(self._ws_for('cf'))
        out = dp.finish(self.store, tok, terminal='cancelled', now=T0)
        self.assertTrue(out['released'], out)
        # cancel 已赢：终态 claim 不能再被消费。
        c = dp.consume_for_entry(self.store, task_id='cf', runtime='qoder',
                                 model='Qwen4-Max-Preview',
                                 workspace=ws, prompt_sha256='p', stage='s1', chat_id='c1',
                                 claim_token=tok, wrapper_pid=4321, wrapper_created='w1',
                                 now=T0)
        self.assertFalse(c['allowed'], c)
        self.assertEqual(c['reason'], 'claim_invalid')


class LunaTicketTests(_PoolBase):
    def test_ask_record_starts_300s_and_persists(self):
        self.fill_domestic()
        r = dp.ask_record(self.store, task_id='L', scope=self._scope('L'),
                          ask_message_id='m1', now=T0)
        self.assertTrue(r['recorded'], r)
        self.assertEqual(r['state'], 'pending')
        self.assertEqual(r['deadline_seconds'], dp.LUNA_ASK_TIMEOUT_SECONDS)
        deadline = dp._parse(r['deadline_utc'])
        asked = dp._parse(r['asked_at_utc'])
        self.assertEqual(int((deadline - asked).total_seconds()),
                         dp.LUNA_ASK_TIMEOUT_SECONDS)

    def test_ask_record_requires_full_domestic(self):
        # 六未满 → 优先国内，不该升级询问。
        r = dp.ask_record(self.store, task_id='L', scope=self._scope('L'),
                          ask_message_id='m1', now=T0)
        self.assertFalse(r['recorded'], r)
        self.assertTrue(any('not full' in x for x in r['reasons']), r['reasons'])

    def test_ask_record_requires_message_id(self):
        self.fill_domestic()
        r = dp.ask_record(self.store, task_id='L', scope=self._scope('L'),
                          ask_message_id=None, now=T0)
        self.assertFalse(r['recorded'], r)
        self.assertTrue(any('ask_message_id' in x for x in r['reasons']), r['reasons'])

    def test_ask_record_not_reset_by_restart(self):
        self.fill_domestic()
        first = dp.ask_record(self.store, task_id='L', scope=self._scope('L'),
                              ask_message_id='m1', now=T0)
        later = dp.ask_record(self.store, task_id='L', scope=self._scope('L'),
                              ask_message_id='m1', now=T0 + timedelta(seconds=500))
        self.assertFalse(later['recorded'])
        self.assertEqual(later['deadline_utc'], first['deadline_utc'])
        self.assertEqual(later['state'], 'pending')

    def test_reply_records_choice(self):
        self.fill_domestic()
        dp.ask_record(self.store, task_id='L', scope=self._scope('L'),
                      ask_message_id='m1', now=T0)
        r = dp.reply(self.store, task_id='L', choice='external_agent', note='user picked',
                     now=T0)
        self.assertTrue(r['recorded'])
        self.assertEqual(r['state'], 'replied')
        self.assertEqual(r['choice'], 'external_agent')

    def test_reply_invalid_choice_raises(self):
        with self.assertRaises(ValueError):
            dp.reply(self.store, task_id='L', choice='bogus')

    def test_reply_without_ticket(self):
        r = dp.reply(self.store, task_id='ghost', choice='luna', now=T0)
        self.assertFalse(r['recorded'])

    def test_claim_due_not_yet_due(self):
        self.fill_domestic()
        dp.ask_record(self.store, task_id='L', scope=self._scope('L'),
                      ask_message_id='m1', now=T0)
        r = dp.claim_due(self.store, task_id='L', now=T0 + timedelta(seconds=10),
                         _preclaim=False)
        self.assertFalse(r['claimed'])
        self.assertTrue(r.get('not_due'))

    def test_claim_due_after_external_reply_refused(self):
        self.fill_domestic()
        dp.ask_record(self.store, task_id='L', scope=self._scope('L'),
                      ask_message_id='m1', now=T0)
        dp.reply(self.store, task_id='L', choice='external_agent', now=T0)
        r = dp.claim_due(self.store, task_id='L', now=T0 + timedelta(seconds=400),
                         _preclaim=False)
        self.assertFalse(r['claimed'])
        self.assertEqual(r.get('state'), 'replied')
        self.assertEqual(r.get('reply_choice'), 'external_agent')

    def test_claim_due_domestic_slot_freed_reclaims(self):
        toks = self.fill_domestic()
        dp.ask_record(self.store, task_id='L', scope=self._scope('L'),
                      ask_message_id='m1', now=T0)
        # 释放一个国内名额（BW-WEAK-TIER-ENFORCE-20261122：弱档纯优先级队列下 claim_due
        # 的国内回收候选=弱档可派主力 claude(层3)/zcode:GLM-5.3-Flash(层4)，改释放
        # claude 槽——fill 顺序 toks[6]=claude:deepseek-flash，弱档无任何时段门，释放即
        # 空位）→ claim_due 竞争发现空位 → 同事务原子回收，不双派、不丢槽。
        dp.finish(self.store, toks[6], terminal='cancelled', now=T0)
        r = dp.claim_due(self.store, task_id='L', now=T0 + timedelta(seconds=400),
                         _preclaim=False)
        self.assertTrue(r['claimed'], r)
        self.assertEqual(r['mode'], 'domestic_reclaim')
        self.assertTrue(r['token'])
        self.assertIn(r['pool_key'], dp.MAIN_FORCE_KEYS)

    def test_claim_due_six_full_no_reply_claims_luna(self):
        self.fill_domestic()
        dp.ask_record(self.store, task_id='L', scope=self._scope('L'),
                      ask_message_id='m1', now=T0)
        r = dp.claim_due(self.store, task_id='L', now=T0 + timedelta(seconds=400),
                         _preclaim=False)
        self.assertTrue(r['claimed'], r)
        self.assertEqual(r['pool_key'], dp.LUNA_KEY)
        self.assertEqual(r['launch_state'], 'host_must_call_native')
        self.assertTrue(r['token'])

    def test_claim_due_without_ticket_refused(self):
        self.fill_domestic()
        r = dp.claim_due(self.store, task_id='ghost', now=T0, _preclaim=False)
        self.assertFalse(r['claimed'])

    def test_claim_due_non_idempotent(self):
        self.fill_domestic()
        dp.ask_record(self.store, task_id='L', scope=self._scope('L'),
                      ask_message_id='m1', now=T0)
        first = dp.claim_due(self.store, task_id='L', now=T0 + timedelta(seconds=400),
                             _preclaim=False)
        self.assertTrue(first['claimed'])
        second = dp.claim_due(self.store, task_id='L', now=T0 + timedelta(seconds=401),
                              _preclaim=False)
        self.assertFalse(second['claimed'])
        self.assertEqual(second.get('state'), 'claimed')
        self.assertTrue(second.get('already_claimed'))

    def test_luna_has_no_global_cap(self):
        self.fill_domestic()
        for i in range(3):
            dp.ask_record(self.store, task_id=f'L{i}', scope=self._scope(f'L{i}'),
                          ask_message_id=f'm{i}', now=T0)
            r = dp.claim_due(self.store, task_id=f'L{i}',
                             now=T0 + timedelta(seconds=400), _preclaim=False)
            self.assertTrue(r['claimed'], r)
        st = dp.status(self.store, now=T0)['pools'][dp.LUNA_KEY]
        self.assertEqual(st['active'], 3)
        self.assertIsNone(st['capacity'])

    def test_mark_launch_unknown_records(self):
        self.fill_domestic()
        dp.ask_record(self.store, task_id='L', scope=self._scope('L'),
                      ask_message_id='m1', now=T0)
        dp.claim_due(self.store, task_id='L', now=T0 + timedelta(seconds=400),
                     _preclaim=False)
        r = dp.mark_launch_unknown(self.store, task_id='L', now=T0)
        self.assertTrue(r['recorded'])
        self.assertEqual(r['launch_state'], 'launch_unknown')
        tickets = dp.status(self.store, now=T0)['luna_tickets']
        self.assertEqual(tickets[0]['launch_state'], 'launch_unknown')

    def test_cancel_pending_only_cancels_pending(self):
        self.fill_domestic()
        dp.ask_record(self.store, task_id='L', scope=self._scope('L'),
                      ask_message_id='m1', now=T0)
        r = dp.cancel_pending(self.store, task_id='L', now=T0)
        self.assertTrue(r['cancelled'])
        # 再次取消（已非 pending）→ no-op。
        r2 = dp.cancel_pending(self.store, task_id='L', now=T0)
        self.assertFalse(r2['cancelled'])

    def test_cancel_pending_after_reply_is_noop(self):
        self.fill_domestic()
        dp.ask_record(self.store, task_id='L', scope=self._scope('L'),
                      ask_message_id='m1', now=T0)
        dp.reply(self.store, task_id='L', choice='luna', now=T0)
        r = dp.cancel_pending(self.store, task_id='L', now=T0)
        self.assertFalse(r['cancelled'])


class StatusTests(_PoolBase):
    def test_status_shape_and_domestic_total(self):
        # BW-STRONG-SONUS-20261024 + BW-WEAK-RESTRUCTURE-20261116：物理总量 17
        # （ZCode GLM-5.3 与弱档专用 GLM-5.3-Flash 各 1 槽 + strong 专属 Sonus 1 槽）。
        self.reserve('z', 'qodercn', 'Qwen-3.8-Max')
        with dispatch_mode('medium'):  # BW-WEAK-RESTRUCTURE-20261116：qoder Max 经 medium。
            self.reserve('q', 'qoder', 'Qwen4-Max-Preview')
        st = dp.status(self.store, now=T0)
        self.assertEqual(set(st['pools']), set(dp.CAPACITY))
        self.assertEqual(st['domestic']['capacity'], 16)
        self.assertEqual(st['domestic']['active'], 3)   # 2 本次 + 1 CodeBuddy 基线占位
        self.assertEqual(st['domestic']['free'], 13)
        self.assertFalse(st['domestic']['full'])
        self.assertEqual(st['rotation']['committed_zcode'], 0)
        self.assertEqual(st['rotation']['committed_qoder'], 1)

    def test_luna_not_counted_in_domestic(self):
        self.fill_domestic()
        dp.ask_record(self.store, task_id='L', scope=self._scope('L'),
                      ask_message_id='m1', now=T0)
        dp.claim_due(self.store, task_id='L', now=T0 + timedelta(seconds=400),
                     _preclaim=False)
        st = dp.status(self.store, now=T0)
        # Luna 在途不占国内名额口径（国内仍为 17/17，Luna 单列且无上限）。
        self.assertEqual(st['domestic']['active'], 16)
        self.assertEqual(st['pools'][dp.LUNA_KEY]['active'], 1)


class CliSmokeTests(_PoolBase):
    def test_parser_builds_and_status_runs(self):
        ap = dp.build_parser()
        args = ap.parse_args(['--store', self.store, 'status'])
        rc = dp.main(['--store', self.store, 'status'])
        self.assertEqual(rc, 0)
        self.assertEqual(args.command, 'status')

    def test_cli_reserve_then_status(self):
        # CLI 冒烟：仍真实跑 parser+main+reserve+status 全链路（拒绝路径 rc 恒 0，由
        # JSON 决策）。BW-ZCODE-RESTORE-1-20261022：zcode:GLM-5.3 恢复为正常可派主力
        # （容量 1）——显式 --executor zcode 的同组合真实 claim 成功并占其唯一槽。
        # BW-WEAK-RESTRUCTURE-20261116：zcode:GLM-5.3 已移出弱档，链路在 medium 档跑。
        with dispatch_mode('medium'):
            self.assertEqual(dp.main(['--store', self.store, 'reserve', '--task-id', 'cli',
                                      '--runtime', 'zcode', '--model', 'GLM-5.3',
                                      '--workspace', str(self._ws_for('cli')),
                                      '--prompt-sha256', 'deadbeef',
                                      '--executor', 'zcode']), 0)
        st = dp.status(self.store)
        self.assertEqual(st['pools']['zcode:GLM-5.3']['active'], 1)


# ============================================================================
# BW-POOL-SPLIT-20261010-S5 需求 1 回归：明确限额落标/读取/解除 + consume 前检查
# ============================================================================
class MainForceLimitTests(_PoolBase):
    """record_main_force_limit 绑定真实 attempt + 错误证据；写读解再读全程往返；
    drift（task/pool 漂移、证据 sha 不一致、attempt 缺失、非限额池）一律拒；
    consume_for_entry 消费预留票据前先查限额并把本 reserved 释放为 start_failed。"""

    QMAX = 'qoder:Qwen4-Max-Preview'
    CNMAX = 'qodercn:Qwen4-Max-Preview'
    CUSTOMMAX = 'qodercn:Qwen-3.8-Max'  # 自定义 CN 主力：可限额、与内置同套时段门（用例经 T0 夜间 claim）、复用同一标记机制

    def setUp(self):
        # BW-WEAK-RESTRUCTURE-20261116：本类占位组合（两内置 Qwen4-Max-Preview）已移出
        # 弱档，reserve/consume 统一在 medium 档执行（T0 北京 23:00 无夜间门）；限额
        # 标记 record/read/release 机制与档位无关，断言原样保留。
        ctx = dispatch_mode('medium')
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        super().setUp()

    def _evidence(self, name='stdout.json', payload='{"result_errors":["You\'ve reached '
                                                        'your credit usage limit."]}\n'):
        """写一个真实存在的证据文件并返回 (path_str, sha256_hex)。"""
        import hashlib as _h
        p = self.base / name
        p.write_text(payload, encoding='utf-8', newline='\n')
        return str(p), _h.sha256(p.read_bytes()).hexdigest()

    def test_record_read_release_read_roundtrip(self):
        """temp 库完整往返：record → main_force_limited/Limits → release → 再读恒 False。"""
        tok = self.reserve_exec('rt', 'qoder', 'Qwen4-Max-Preview', 'qoder', pk=self.QMAX)
        ev_path, ev_sha = self._evidence()
        rec = dp.record_main_force_limit(self.store, self.QMAX, task_id='rt',
                                         attempt_token=tok, evidence_path=ev_path,
                                         evidence_sha256=ev_sha, now=T0)
        self.assertTrue(rec['recorded'], rec)
        self.assertTrue(rec['limited'], rec)
        self.assertEqual(rec['pool_key'], self.QMAX)
        self.assertEqual(rec['task_id'], 'rt')
        self.assertEqual(rec['attempt_token'], tok)
        self.assertEqual(rec['evidence_sha256'], ev_sha)
        self.assertTrue(dp.main_force_limited(self.store, self.QMAX, now=T0))
        # CN Max 独立池不受国际 QMAX 标记影响。
        self.assertFalse(dp.main_force_limited(self.store, self.CNMAX, now=T0))
        snaps = dp.main_force_limits(self.store, now=T0)
        self.assertIn(self.QMAX, snaps)
        self.assertEqual(snaps[self.QMAX]['limited'], 1)
        rel = dp.release_main_force_limit(self.store, self.QMAX, note='user confirmed '
                                          'credit refill', now=T0 + timedelta(minutes=5))
        self.assertTrue(rel['released'], rel)
        self.assertFalse(dp.main_force_limited(self.store, self.QMAX, now=T0))
        # 保留历史行：main_force_limits 仍可见，limited=0 + released_at/note 落库。
        snaps2 = dp.main_force_limits(self.store, now=T0)
        self.assertEqual(snaps2[self.QMAX]['limited'], 0)
        self.assertEqual(snaps2[self.QMAX]['release_note'], 'user confirmed credit refill')
        self.assertIsNotNone(snaps2[self.QMAX]['released_at_utc'])

    def test_record_rejects_non_limitable_pools(self):
        """两地区 Flash、退休 DeepSeek、Luna 均不落标；limited 恒 False。ZCode 自
        BW-LIMIT30-20261019-S3 起可承载真实限额标记（见下一用例），不再是拒绝项。"""
        for pk in ('qoder:Qwen3.8-Flash',
                   'qodercn:Qwen3.8-Flash', 'qodercn:DeepSeek-Flash',
                   dp.LUNA_KEY):
            with self.subTest(pk=pk):
                r = dp.record_main_force_limit(self.store, pk, task_id='t',
                                               attempt_token='x',
                                               evidence_path=str(self.base),
                                               evidence_sha256='0' * 64, now=T0)
                self.assertFalse(r['recorded'], r)
                self.assertFalse(r.get('drift'), r)
                self.assertIn('not a quota-limitable', r['reasons'][0])
                self.assertFalse(dp.main_force_limited(self.store, pk, now=T0))

    def test_zcode_can_carry_real_limit_marker(self):
        """BW-LIMIT30-20261019-S3：ZCode/GLM-5.3 真实结构化 429 可绑定真实 attempt 与
        错误证据落标；30 分钟窗口由 `_quota_limit_window_active` 只读判定。
        BW-ZCODE-RETIRE-20261020：ZCode 已全局退役、不再可经 reserve claim 新在途，
        历史在途 attempt 由 SQL 直接构造（与 setUp 的 CodeBuddy 占位同款），仅验证
        历史在途仍可落标与窗口判定（在途排干/诊断口径保留）。"""
        with closing(dp.connect(self.store)) as conn:
            conn.execute('INSERT INTO attempts(token, task_id, runtime, model, pool_key, '
                         "state, reserved_at_utc) VALUES(?,?,?,?,?,?,?)",
                         ('zmark', 'zmark', 'zcode', 'GLM-5.3',
                          'zcode:GLM-5.3', 'reserved', dp._iso(T0)))
        ev_path, ev_sha = self._evidence('zcode-limit.json')
        rec = dp.record_main_force_limit(self.store, 'zcode:GLM-5.3', task_id='zmark',
                                         attempt_token='zmark', evidence_path=ev_path,
                                         evidence_sha256=ev_sha, now=T0)
        self.assertTrue(rec['recorded'], rec)
        self.assertTrue(dp.main_force_limited(self.store, 'zcode:GLM-5.3', now=T0))
        with closing(dp.connect(self.store)) as conn:
            self.assertTrue(dp._quota_limit_window_active(
                conn, 'zcode:GLM-5.3', T0 + timedelta(seconds=1799)))
            self.assertFalse(dp._quota_limit_window_active(
                conn, 'zcode:GLM-5.3', T0 + timedelta(seconds=1800)))

    def test_record_rejects_missing_attempt(self):
        ev_path, ev_sha = self._evidence()
        r = dp.record_main_force_limit(self.store, self.QMAX, task_id='ghost',
                                       attempt_token='no-such-token',
                                       evidence_path=ev_path, evidence_sha256=ev_sha,
                                       now=T0)
        self.assertFalse(r['recorded'], r)
        self.assertTrue(r['drift'], r)
        self.assertIn('no attempt found', r['reasons'][0])
        self.assertFalse(dp.main_force_limited(self.store, self.QMAX, now=T0))

    def test_record_rejects_task_and_pool_drift(self):
        """attempt 已存在但 pool_key 或 task_id 与 marker 参数不一致 → drift 拒。"""
        tok = self.reserve_exec('real', 'qoder', 'Qwen4-Max-Preview', 'qoder', pk=self.QMAX)
        ev_path, ev_sha = self._evidence('ev1.json')
        # task_id 漂移
        r1 = dp.record_main_force_limit(self.store, self.QMAX, task_id='someone-else',
                                        attempt_token=tok, evidence_path=ev_path,
                                        evidence_sha256=ev_sha, now=T0)
        self.assertFalse(r1['recorded'], r1)
        self.assertTrue(r1['drift'], r1)
        self.assertIn('task_id drift', r1['reasons'][0])
        # pool_key 漂移（拿 QMax 的 token 想给 CNMax 落标）
        r2 = dp.record_main_force_limit(self.store, self.CNMAX, task_id='real',
                                        attempt_token=tok, evidence_path=ev_path,
                                        evidence_sha256=ev_sha, now=T0)
        self.assertFalse(r2['recorded'], r2)
        self.assertTrue(r2['drift'], r2)
        self.assertIn('pool_key drift', r2['reasons'][0])
        self.assertFalse(dp.main_force_limited(self.store, self.QMAX, now=T0))
        self.assertFalse(dp.main_force_limited(self.store, self.CNMAX, now=T0))

    def test_record_rejects_evidence_sha_mismatch(self):
        """证据文件存在但 sha256 与声明值不一致 → drift 拒。"""
        tok = self.reserve_exec('es', 'qoder', 'Qwen4-Max-Preview', 'qoder', pk=self.QMAX)
        ev_path, _ = self._evidence('ev2.json', payload='real bytes\n')
        r = dp.record_main_force_limit(self.store, self.QMAX, task_id='es',
                                       attempt_token=tok, evidence_path=ev_path,
                                       evidence_sha256='f' * 64, now=T0)
        self.assertFalse(r['recorded'], r)
        self.assertTrue(r['drift'], r)
        self.assertIn('evidence_sha256 does not match', r['reasons'][0])
        self.assertFalse(dp.main_force_limited(self.store, self.QMAX, now=T0))

    def test_record_rejects_missing_evidence_file(self):
        tok = self.reserve_exec('ef', 'qoder', 'Qwen4-Max-Preview', 'qoder', pk=self.QMAX)
        missing = str(self.base / 'no-such-file.json')
        r = dp.record_main_force_limit(self.store, self.QMAX, task_id='ef',
                                       attempt_token=tok, evidence_path=missing,
                                       evidence_sha256='0' * 64, now=T0)
        self.assertFalse(r['recorded'], r)
        self.assertTrue(r['drift'], r)
        self.assertIn('does not exist', r['reasons'][0])

    def test_record_requires_binding_fields(self):
        """缺 attempt_token/task_id 或 evidence_path/evidence_sha256 → drift 拒。"""
        tok = self.reserve_exec('bf', 'qoder', 'Qwen4-Max-Preview', 'qoder', pk=self.QMAX)
        ev_path, ev_sha = self._evidence('ev3.json')
        for kwargs in (
            dict(task_id='bf', attempt_token=tok),  # 缺 evidence 字段
            dict(evidence_path=ev_path, evidence_sha256=ev_sha),  # 缺 attempt 字段
            dict(task_id='bf', evidence_path=ev_path, evidence_sha256=ev_sha),  # 缺 token
        ):
            with self.subTest(kwargs=sorted(kwargs)):
                r = dp.record_main_force_limit(self.store, self.QMAX, now=T0, **kwargs)
                self.assertFalse(r['recorded'], r)
                self.assertTrue(r['drift'], r)

    def test_record_is_idempotent_and_clears_release_traces(self):
        """同一 pool 重复 record 只刷新证据/时间戳，仍 limited=1，清空旧的 released_at/note。"""
        tok = self.reserve_exec('idp', 'qoder', 'Qwen4-Max-Preview', 'qoder', pk=self.QMAX)
        ev1, s1 = self._evidence('a.json', payload='first\n')
        r1 = dp.record_main_force_limit(self.store, self.QMAX, task_id='idp',
                                        attempt_token=tok, evidence_path=ev1,
                                        evidence_sha256=s1, now=T0)
        self.assertTrue(r1['recorded'])
        dp.release_main_force_limit(self.store, self.QMAX, note='temp release',
                                    now=T0 + timedelta(seconds=10))
        # 再次落标：应覆盖 limited=1，清空 released_at/note，指向新证据。
        (self.base / 'a.json').write_text('second\n', encoding='utf-8', newline='\n')
        import hashlib as _h
        s2 = _h.sha256((self.base / 'a.json').read_bytes()).hexdigest()
        r2 = dp.record_main_force_limit(self.store, self.QMAX, task_id='idp',
                                        attempt_token=tok, evidence_path=ev1,
                                        evidence_sha256=s2,
                                        now=T0 + timedelta(seconds=20))
        self.assertTrue(r2['recorded'])
        snap = dp.main_force_limits(self.store, now=T0)[self.QMAX]
        self.assertEqual(snap['limited'], 1)
        self.assertEqual(snap['evidence_sha256'], s2)
        self.assertIsNone(snap['released_at_utc'])
        self.assertIsNone(snap['release_note'])

    def test_consume_for_entry_releases_reserved_on_limited(self):
        """BW-SIMPLE-PRIORITY-RETRY-S4 政策：consume_for_entry 消费已预留 token 前不再据
        main_force_limit 释放本 reserved 拒启动——新一轮从最高优先级真实尝试，历史限额标记
        仅作诊断（main_force_limits 表/limited 状态/limits API 均原样保留）。同 workspace 单
        写、时段门、unknown 在途、审批拒绝等守卫照常，本用例只验证 consume 真跑通到 running、
        不越权释放别人的占位。"""
        tok = self.reserve_exec('c1', 'qoder', 'Qwen4-Max-Preview', 'qoder', pk=self.QMAX)
        tok_cn = self.reserve_exec('c2', 'qodercn', 'Qwen4-Max-Preview', 'qodercn',
                                   pk=self.CNMAX)
        ev, sha = self._evidence('c1.json', payload='limited evidence\n')
        # BW-QWEN-MAX-LIMIT15-20261028：QMAX 限额标记已入 900 秒短窗——本用例验证窗口
        # 外（历史标记）consume 放行；窗口内拒绝/释放由 test_qwen_max_limit15 覆盖。
        rec = dp.record_main_force_limit(self.store, self.QMAX, task_id='c1',
                                         attempt_token=tok, evidence_path=ev,
                                         evidence_sha256=sha,
                                         now=T0 - timedelta(seconds=2000))
        self.assertTrue(rec['recorded'], rec)
        # 诊断口径：main_force_limited 仍如实反映限额行（不清、不假称 healthy）。
        self.assertTrue(dp.main_force_limited(self.store, self.QMAX, now=T0))
        # 但入口 consume 不再据此前置挡：reserved→running 正常放行、capacity 由本 attempt 持有。
        out = dp.consume_for_entry(self.store, task_id='c1', runtime='qoder',
                                   model='Qwen4-Max-Preview',
                                   workspace=str(self._ws_for('c1')),
                                   prompt_sha256='p', claim_token=tok,
                                   now=T0 + timedelta(seconds=2))
        self.assertTrue(out['allowed'], out)
        self.assertTrue(out['claimed'], out)
        self.assertEqual(out['pool_key'], self.QMAX, out)
        self.assertNotIn('capacity_released', out)
        self.assertEqual(self._state(tok), 'running')
        # 别人的 CN Max reserved 完全不动（同 workspace 单写/去重仍照常）。
        self.assertEqual(self._state(tok_cn), 'reserved')

    def test_consume_for_entry_running_token_not_falsely_released(self):
        """BW-SIMPLE-PRIORITY-RETRY-S4：consume 不再据 main_force_limit 前置挡；running
        旧 token 重放仍走 CAS reserved→running 单赢家路径，UPDATE WHERE state=reserved 命中
        0 行 → 拒重复启动、绝不谎称 capacity_released，保留原 running 状态与在途占位。"""
        tok = self.reserve_exec('rn', 'qoder', 'Qwen4-Max-Preview', 'qoder', pk=self.QMAX)
        ws = str(self._ws_for('rn'))
        first = dp.consume_for_entry(self.store, task_id='rn', runtime='qoder',
                                     model='Qwen4-Max-Preview', workspace=ws,
                                     prompt_sha256='p', claim_token=tok,
                                     wrapper_pid=4321, wrapper_created='w1', now=T0)
        self.assertTrue(first['allowed'], first)
        self.assertEqual(self._state(tok), 'running')
        ev, sha = self._evidence('rn.json', payload='limited after running\n')
        # BW-QWEN-MAX-LIMIT15-20261028：历史标记（窗口外）不挡 consume，仍走 CAS 冲突路径。
        self.assertTrue(dp.record_main_force_limit(
            self.store, self.QMAX, task_id='rn', attempt_token=tok,
            evidence_path=ev, evidence_sha256=sha,
            now=T0 - timedelta(seconds=2000))['recorded'])
        out = dp.consume_for_entry(self.store, task_id='rn', runtime='qoder',
                                   model='Qwen4-Max-Preview', workspace=ws,
                                   prompt_sha256='p', claim_token=tok,
                                   wrapper_pid=4321, wrapper_created='w1',
                                   now=T0 + timedelta(seconds=2))
        self.assertFalse(out['allowed'], out)
        self.assertFalse(out['sent'], out)
        # CAS 命中 0 行 → 拒重复消费，绝不假释放。
        self.assertEqual(out['reason'], 'consume_conflict', out)
        self.assertEqual(out['pool_key'], self.QMAX, out)
        self.assertNotIn('capacity_released', out)
        self.assertEqual(self._state(tok), 'running')
        self.assertEqual(dp.status(self.store, now=T0)['pools'][self.QMAX]['active'], 1)

    def test_consume_for_entry_unknown_token_not_falsely_released(self):
        """BW-SIMPLE-PRIORITY-RETRY-S4：consume 不再据 main_force_limit 前置挡；unknown
        旧 token 重放走 CAS reserved→running 单赢家路径，UPDATE WHERE state=reserved 命中 0
        行 → 拒重复启动，绝不假释放，保留原 unknown 状态与在途占位。"""
        self.assertIn('unknown', dp.ACTIVE_STATES)
        tok = self.reserve_exec('uk', 'qoder', 'Qwen4-Max-Preview', 'qoder', pk=self.QMAX)
        ws = str(self._ws_for('uk'))
        dp.mark_running(self.store, tok, wrapper_pid=5000, wrapper_created='c5000', now=T0)
        dp.mark_unknown(self.store, tok, now=T0)
        self.assertEqual(self._state(tok), 'unknown')
        ev, sha = self._evidence('uk.json', payload='limited while unknown\n')
        self.assertTrue(dp.record_main_force_limit(
            self.store, self.QMAX, task_id='uk', attempt_token=tok,
            evidence_path=ev, evidence_sha256=sha,
            now=T0 + timedelta(seconds=1))['recorded'])
        out = dp.consume_for_entry(self.store, task_id='uk', runtime='qoder',
                                   model='Qwen4-Max-Preview', workspace=ws,
                                   prompt_sha256='p', claim_token=tok,
                                   wrapper_pid=5000, wrapper_created='c5000',
                                   now=T0 + timedelta(seconds=2))
        self.assertFalse(out['allowed'], out)
        self.assertFalse(out['sent'], out)
        # BW-QWEN-MAX-LIMIT15-20261028：unknown 旧 token 在 900 秒短窗内重放按窗口拒绝
        # （main_force_limited），但 CAS 语义不变——绝不假释放，原 unknown 状态与在途占位保留。
        self.assertEqual(out['reason'], 'main_force_limited', out)
        self.assertEqual(out['pool_key'], self.QMAX, out)
        self.assertFalse(out.get('capacity_released'), out)
        self.assertEqual(self._state(tok), 'unknown')
        self.assertEqual(dp.status(self.store, now=T0)['pools'][self.QMAX]['active'], 1)

    def test_consume_for_entry_allows_when_pool_released(self):
        """release_main_force_limit 后同一 reserved 可正常 consume 到 running。"""
        tok = self.reserve_exec('rl', 'qoder', 'Qwen4-Max-Preview', 'qoder', pk=self.QMAX)
        ev, sha = self._evidence('rl.json', payload='limited once\n')
        self.assertTrue(dp.record_main_force_limit(
            self.store, self.QMAX, task_id='rl', attempt_token=tok,
            evidence_path=ev, evidence_sha256=sha, now=T0)['recorded'])
        self.assertTrue(dp.release_main_force_limit(
            self.store, self.QMAX, note='user refill',
            now=T0 + timedelta(seconds=5))['released'])
        out = dp.consume_for_entry(self.store, task_id='rl', runtime='qoder',
                                   model='Qwen4-Max-Preview',
                                   workspace=str(self._ws_for('rl')),
                                   prompt_sha256='p', claim_token=tok,
                                   now=T0 + timedelta(seconds=6))
        self.assertTrue(out['allowed'], out)
        self.assertEqual(self._state(tok), 'running')

    def test_custom_max_limit_record_read_release_no_builtin_collateral(self):
        """自定义 CN 主力 qodercn:Qwen-3.8-Max 复用同一限额标记机制：record/read/release
        全程往返可用；给自定义落标绝不误伤两内置 Max（无连带 collateral），释放亦只解自定义。"""
        tok = self.reserve_exec('cx', 'qodercn', 'Qwen-3.8-Max', 'qodercn', pk=self.CUSTOMMAX)
        ev, sha = self._evidence('cx.json')
        rec = dp.record_main_force_limit(self.store, self.CUSTOMMAX, task_id='cx',
                                         attempt_token=tok, evidence_path=ev,
                                         evidence_sha256=sha, now=T0)
        self.assertTrue(rec['recorded'], rec)
        self.assertTrue(rec['limited'], rec)
        self.assertEqual(rec['pool_key'], self.CUSTOMMAX)
        self.assertTrue(dp.main_force_limited(self.store, self.CUSTOMMAX, now=T0))
        # 绝不连带：自定义被限额，两内置 Max 完全不受影响。
        self.assertFalse(dp.main_force_limited(self.store, self.QMAX, now=T0))
        self.assertFalse(dp.main_force_limited(self.store, self.CNMAX, now=T0))
        snaps = dp.main_force_limits(self.store, now=T0)
        self.assertEqual(snaps[self.CUSTOMMAX]['limited'], 1)
        rel = dp.release_main_force_limit(self.store, self.CUSTOMMAX,
                                          note='user confirmed refill', now=T0)
        self.assertTrue(rel['released'], rel)
        self.assertFalse(dp.main_force_limited(self.store, self.CUSTOMMAX, now=T0))

    def test_consume_for_entry_custom_max_limited_releases_to_start_failed(self):
        """BW-SIMPLE-PRIORITY-RETRY-S4：consume_for_entry 不再据 main_force_limit 前置挡。
        用户对自定义 CN 主力 qodercn:Qwen-3.8-Max 的**明确暂停**通过候选授权范围
        （allowed_candidates 不含该组合）实现——不是靠历史限额标记。限额标记仅作诊断保留
        （record/read/release API 与 main_force_limits 表原样存在）。同区内置 CN Max 的
        reserved 完全不受影响。"""
        tok_cx = self.reserve_exec('cxl', 'qodercn', 'Qwen-3.8-Max', 'qodercn',
                                   pk=self.CUSTOMMAX)
        tok_cn = self.reserve_exec('cnkeep', 'qodercn', 'Qwen4-Max-Preview', 'qodercn',
                                   pk=self.CNMAX)
        ev, sha = self._evidence('cxl.json', payload='custom limit hit\n')
        self.assertTrue(dp.record_main_force_limit(
            self.store, self.CUSTOMMAX, task_id='cxl', attempt_token=tok_cx,
            evidence_path=ev, evidence_sha256=sha, now=T0)['recorded'])
        out = dp.consume_for_entry(self.store, task_id='cxl', runtime='qodercn',
                                   model='Qwen-3.8-Max',
                                   workspace=str(self._ws_for('cxl')),
                                   prompt_sha256='p', claim_token=tok_cx,
                                   now=T0 + timedelta(seconds=2))
        # 新政策：consume 正常放行到 running，绝不因历史限额标记虚假释放为 start_failed。
        self.assertTrue(out['allowed'], out)
        self.assertTrue(out['claimed'], out)
        self.assertEqual(out['pool_key'], self.CUSTOMMAX, out)
        self.assertNotIn('capacity_released', out)
        self.assertEqual(self._state(tok_cx), 'running')
        # 内置 CN Max 的 reserved 不受自定义限额影响。
        self.assertEqual(self._state(tok_cn), 'reserved')


class QuotaLimitWindowTests(_PoolBase):
    """BW-LIMIT30-20261019-S3/BW-PRIORITY-REPAIR-20261019：zcode:GLM-5.3 与
    claude:deepseek-flash / codebuddy:deepseek-v4.1-flash 的真实失败（Claude 402 余额不足
    或明确配额 429）落 main_force_limits 后，新派发在该池 1800 秒内跳过；恰好 1800 秒后按
    原优先级恢复可选，原错误证据不清、不声称额度恢复、无需手动重置。已运行 attempt 不受
    影响；其它池独立。BW-ZCODE-RESTORE-1-20261022：ZCode 恢复可派后其 1800 秒短窗语义
    完整生效（1799 拒 / 1800 放）。"""

    ZC = 'zcode:GLM-5.3'
    CL = 'claude:deepseek-flash'
    CB = 'codebuddy:deepseek-v4.1-flash'

    def _mark(self, pk, task, token):
        import hashlib as _h
        ev = self.base / f'{task}-limit.json'
        ev.write_text('{"errors":[{"status":429}]}\n', encoding='utf-8', newline='\n')
        rec = dp.record_main_force_limit(
            self.store, pk, task_id=task, attempt_token=token,
            evidence_path=str(ev), evidence_sha256=_h.sha256(ev.read_bytes()).hexdigest(),
            now=T0)
        self.assertTrue(rec['recorded'], rec)

    def _claim(self, task, runtime, model, now):
        return dp.reserve(self.store, task_id=task, runtime=runtime, model=model,
                          workspace=str(self._ws_for(task)), prompt_sha256='p',
                          executor=runtime, now=now, _preclaim=False)

    def test_zcode_restored_1799_refuses_1800_allows(self):
        # BW-ZCODE-RESTORE-1-20261022：ZCode 恢复可派后，最近一次真实结构化限额的
        # 1800 秒短窗对 executor=zcode 新 claim 完整生效：1799 秒内零 Popen 如实拒绝
        # （main_force_limited），恰满 1800 秒按原优先级重新可选（不删原标记、不宣称恢复）。
        # BW-WEAK-RESTRUCTURE-20261116：zcode:GLM-5.3 已移出弱档，整条链路在 medium 档跑
        # （medium 档 ZCode 无时段门）。
        with dispatch_mode('medium'):
            tok = self.reserve_exec('zw0', 'zcode', 'GLM-5.3', 'zcode')
            self._mark(self.ZC, 'zw0', tok)
            # 释放 zw0 的占位（容量 1）：本用例验证的是新派发限额窗口，不是容量满。
            dp.finish(self.store, tok, terminal='cancelled', now=T0)
            limited = self._claim('zw1799', 'zcode', 'GLM-5.3',
                                  T0 + timedelta(seconds=1799))
            self.assertFalse(limited.get('allowed'), limited)
            self.assertEqual(limited['reason'], 'main_force_limited', limited)
            ok = self._claim('zw1800', 'zcode', 'GLM-5.3',
                             T0 + timedelta(seconds=1800))
            self.assertTrue(ok.get('allowed'), ok)
            self.assertEqual(ok['pool_key'], self.ZC)

    def test_claude_1799_refuses_1800_allows(self):
        # BW-WEAK-RESTRUCTURE-20261116：ZCode 连带断言需 zcode:GLM-5.3 可派，整条链路在
        # medium 档跑（claude 在 T0 北京 23:00 离峰、无高峰门）。
        with dispatch_mode('medium'):
            tok = self.reserve_exec('cw', 'claude', 'deepseek-flash', 'claude')
            self._mark(self.CL, 'cw', tok)
            r = self._claim('cw2', 'claude', 'deepseek-flash',
                            T0 + timedelta(seconds=1799))
            self.assertFalse(r.get('allowed'), r)
            self.assertEqual(r['reason'], 'main_force_limited', r)
            ok = self._claim('cw3', 'claude', 'deepseek-flash',
                             T0 + timedelta(seconds=1800))
            self.assertTrue(ok.get('allowed'), ok)
            self.assertEqual(ok['pool_key'], self.CL)
            # 两池互相独立：Claude 限额绝不连带 ZCode——ZCode 无标记时按自身容量照常可选
            # （BW-ZCODE-RESTORE-1-20261022 恢复可派、容量 1）。
            z = self._claim('cw4', 'zcode', 'GLM-5.3', T0 + timedelta(seconds=60))
            self.assertTrue(z.get('allowed'), z)
            self.assertEqual(z['pool_key'], self.ZC, z)

    def test_codebuddy_deepseek_mode_excluded_glm_unaffected(self):
        # BW-WEAK-RESTRUCTURE-20261116：codebuddy:deepseek-v4.1-flash 已从所有档位的可新派
        # 集合移除（纯 mode_excluded）——原「1799 拒 / 1800 放」的 claim 路径随移除而消失；
        # 1800 秒窗口机制本身保留（窗口内/外的只读判定仍生效），GLM 额度独立不被连带、
        # weak 档照常真实 claim。
        tok = 'cb-fixture'
        with closing(dp.connect(self.store)) as conn:
            conn.execute("UPDATE attempts SET state='finished', ended_at_utc=? "
                         'WHERE token=?', (dp._iso(T0), tok))
        self._mark(self.CB, 'cb-fixture-task', tok)
        with closing(dp.connect(self.store)) as conn:
            self.assertTrue(dp._quota_limit_window_active(
                conn, self.CB, T0 + timedelta(seconds=1799)))
            self.assertFalse(dp._quota_limit_window_active(
                conn, self.CB, T0 + timedelta(seconds=1800)))
        r = self._claim('cbw2', 'codebuddy', 'deepseek-v4.1-flash',
                        T0 + timedelta(seconds=1799))
        self.assertFalse(r.get('allowed'), r)
        self.assertRejected(r, 'mode_excluded')
        # BW-WEAK-TIER-ENFORCE-20261122：弱档纯优先级队列——CBGLM 是层2，显式 claim 前
        # 先按队列序排空层1 两 OpenCode 槽（队列不看限额短窗/时段门）。
        self.occupy_weak_above(dp.CODEBUDDY_GLM_POOL_KEY, prefix='cbw-above')
        glm = self._claim('cbwg', 'codebuddy', 'glm-5.3-flash',
                          T0 + timedelta(seconds=60))
        self.assertTrue(glm.get('allowed'), glm)
        self.assertEqual(glm['pool_key'], dp.CODEBUDDY_GLM_POOL_KEY)
        ok = self._claim('cbw3', 'codebuddy', 'deepseek-v4.1-flash',
                         T0 + timedelta(seconds=1800))
        self.assertFalse(ok.get('allowed'), ok)
        self.assertRejected(ok, 'mode_excluded')

    def test_reserved_consume_refused_and_released_inside_window(self):
        # BW-ZCODE-RETIRE-20261020：原 ZCode 变体随池退役改用同为限额窗口池的
        # claude:deepseek-flash：reserved 票据在 1800 秒窗口内 consume → 拒绝并把本
        # reserved 真实释放为 start_failed，绝不泄漏占位。
        # BW-WEAK-TIER-ENFORCE-20261122：弱档纯优先级队列——claude 是层3，显式 claim 前
        # 先按队列序排空层1-2；claim-token 消费路径的限额短窗检查保留（新派队列不看
        # 短窗，但已预留票据的消费照旧拒绝+真实释放）。
        self.occupy_weak_above(dp.CLAUDE_POOL_KEY, prefix='zc-above')
        tok = self.reserve_exec('zc', 'claude', 'deepseek-flash', 'claude')
        self._mark(self.CL, 'zc', tok)
        out = dp.consume_for_entry(self.store, task_id='zc', runtime='claude',
                                   model='deepseek-flash',
                                   workspace=str(self._ws_for('zc')),
                                   prompt_sha256='p', claim_token=tok,
                                   now=T0 + timedelta(seconds=1))
        self.assertFalse(out.get('allowed'), out)
        self.assertEqual(out['reason'], 'main_force_limited', out)
        self.assertTrue(out.get('capacity_released'), out)
        self.assertEqual(self._state(tok), 'start_failed')
        # 记录/拒绝/释放全程不遗留写锁：后续普通预留仍可立即成功。
        # BW-WEAK-TIER-ENFORCE-20261122：zcode:GLM-5.3-Flash 是弱档层4——先按队列序
        # 占满其上方全部槽（层1-2 已满，补占 claude 层3 两槽）再真实 claim。
        self.occupy_weak_above('zcode:GLM-5.3-Flash', prefix='zc-above2')
        nxt = self._claim('zc-next', 'zcode', 'GLM-5.3-Flash', T0 + timedelta(seconds=2))
        self.assertTrue(nxt.get('allowed'), nxt)
        self.assertEqual(nxt['pool_key'], 'zcode:GLM-5.3-Flash', nxt)

    def test_auto_zcode_combo_restored_selectable(self):
        # BW-WEAK-RESTRUCTURE-20261116：zcode:GLM-5.3 只属 strong/medium；弱档 ZCode 只用
        # zcode:GLM-5.3-Flash。medium 档工作日高峰（北京周一 10:00）Claude 高峰门与 Max
        # 夜间门都关 → AUTO 请求 zcode 组合直接 claim zcode:GLM-5.3 本池（恢复可派语义
        # 保留）；弱档同一请求如实 mode_excluded，GLM-5.3-Flash 则可 AUTO 真实 claim。
        WEEKDAY_PEAK = datetime(2026, 10, 12, 2, 0, 0, tzinfo=timezone.utc)
        with dispatch_mode('medium'):
            r = dp.select_and_claim(
                self.store, task_id='aauto', runtime='zcode', model='GLM-5.3',
                workspace=str(self._ws_for('aauto')), prompt_sha256='p',
                now=WEEKDAY_PEAK, executor='auto', _preclaim=False)
            self.assertTrue(r.get('allowed'), r)
            self.assertEqual(r['pool_key'], self.ZC, r)
        rw = dp.select_and_claim(
            self.store, task_id='aauto-weak', runtime='zcode', model='GLM-5.3',
            workspace=str(self._ws_for('aauto-weak')), prompt_sha256='p',
            now=WEEKDAY_PEAK, executor='auto', _preclaim=False)
        self.assertFalse(rw.get('allowed'), rw)
        self.assertRejected(rw, 'mode_excluded')
        # 弱档层序（BW-WEAK-TIER-ENFORCE-20261122 纯优先级队列）：GLM-5.3-Flash 是层4、
        # 队尾——先按真实形态直插占位填满其上方全部槽（两 OpenCode 层1 + CBGLM 层2 +
        # claude 层3 两槽），AUTO 请求才轮到它真实 claim。
        for i, pool in enumerate([dp.OPENCODE_DEEPSEEK_POOL_KEY,
                                  dp.CODEBUDDY_GLM_POOL_KEY,
                                  dp.CLAUDE_POOL_KEY, dp.CLAUDE_POOL_KEY]):
            self.seed_attempt(f'aauto-above-{i}', f'aauto-above-task-{i}', pool)
        rf = dp.select_and_claim(
            self.store, task_id='aauto-flash', runtime='zcode', model='GLM-5.3-Flash',
            workspace=str(self._ws_for('aauto-flash')), prompt_sha256='p',
            now=WEEKDAY_PEAK, executor='auto', _preclaim=False)
        self.assertTrue(rf.get('allowed'), rf)
        self.assertEqual(rf['pool_key'], 'zcode:GLM-5.3-Flash', rf)

    def test_marker_older_than_30min_never_blocks(self):
        # 旧“永久 hold/未来 hard_hold”语义不得延长：标记的真实失败已超过 30 分钟 → 窗口
        # 判定不再命中（旧的 availability 未来阻断另由 test_simple_priority_retry 覆盖）。
        # BW-ZCODE-RETIRE-20261020：ZCode 池不可再 claim，改用同为限额窗口池的
        # codebuddy:deepseek-v4.1-flash（SQL 先把 setUp 占位释放为终态）验证同一语义。
        # BW-WEAK-RESTRUCTURE-20261116：codebuddy:deepseek-v4.1-flash 已从所有档位移除
        # （纯 mode_excluded）——窗口过期语义保留在只读判定层；新派发一律 mode_excluded，
        # eligible 主力集合也不含该池。
        tok = 'cb-fixture'
        with closing(dp.connect(self.store)) as conn:
            conn.execute("UPDATE attempts SET state='finished', ended_at_utc=? "
                         'WHERE token=?', (dp._iso(T0), tok))
        self._mark(self.CB, 'cb-fixture-task', tok)
        later = T0 + timedelta(hours=6)
        with closing(dp.connect(self.store)) as conn:
            self.assertFalse(dp._quota_limit_window_active(conn, self.CB, later))
            elig = dp._eligible_main_forces(conn, False, later)
        self.assertNotIn(self.CB, elig)
        ok = self._claim('oz2', 'codebuddy', 'deepseek-v4.1-flash', later)
        self.assertFalse(ok.get('allowed'), ok)
        self.assertRejected(ok, 'mode_excluded')


class BeijingMainForceWindowTests(_PoolBase):
    """BW-MAX-WINDOW-20261010-S1 + BW-CUSTOM-MAX-NIGHT-20261010-S1 + BW-QODER-INTL-MAX-
    ALLDAY-20261023（当前权威）：夜间时段门只约束**两个 CN Max**（CN 内置
    qodercn:Qwen4-Max-Preview、CN 自定义 qodercn:Qwen-3.8-Max），它们只在北京时间 22:00（含）
    至次日 08:00（不含）作为主力；**国际内置 qoder:Qwen4-Max-Preview 全天可派**，任何注入时间
    （北京 08:00 / 10:00 / 21:59 等）都不得被挡。全部使用确定性时间夹具，绝不依赖宿主
    墙钟；跨午夜/08:00 边界、AUTO 白天口径、显式 Max 白天如实拒绝、reserved 跨 08 点真实
    释放、running/unknown 不假释放、降级 claim_due 使用同一时段候选过滤都覆盖。"""

    QMAX = 'qoder:Qwen4-Max-Preview'
    CNMAX = 'qodercn:Qwen4-Max-Preview'
    CXMAX = 'qodercn:Qwen-3.8-Max'
    CN_GATED = (CNMAX, CXMAX)
    ALL_QMAX = (QMAX, CNMAX, CXMAX)

    # 确定性时间：一律 UTC datetime，按固定 +08:00 折算北京时间。
    IN_2200 = datetime(2026, 10, 8, 14, 0, 0, tzinfo=timezone.utc)   # 北京 22:00 含 → 可选
    IN_0759 = datetime(2026, 10, 8, 23, 59, 0, tzinfo=timezone.utc)  # 北京 07:59 → 可选
    OUT_0800 = datetime(2026, 10, 8, 0, 0, 0, tzinfo=timezone.utc)   # 北京 08:00 不含 → 不可选
    OUT_2159 = datetime(2026, 10, 8, 13, 59, 0, tzinfo=timezone.utc)  # 北京 21:59 → 不可选
    NOON = datetime(2026, 10, 8, 4, 0, 0, tzinfo=timezone.utc)       # 北京 12:00 → 不可选
    BJ_1000 = datetime(2026, 10, 8, 2, 0, 0, tzinfo=timezone.utc)    # 北京 10:00 → CN 不可选
    LATE = datetime(2026, 10, 8, 0, 30, 0, tzinfo=timezone.utc)      # 北京 08:30 → 不可选

    def test_window_boundary_inclusive_22_exclusive_08(self):
        self.assertTrue(dp._main_force_window_open(self.IN_2200))
        self.assertTrue(dp._main_force_window_open(self.IN_0759))
        self.assertFalse(dp._main_force_window_open(self.OUT_0800))
        self.assertFalse(dp._main_force_window_open(self.OUT_2159))
        self.assertFalse(dp._main_force_window_open(self.NOON))
        self.assertEqual(dp._beijing_hour(self.IN_2200), 22)
        self.assertEqual(dp._beijing_hour(self.OUT_0800), 8)

    def test_beijing_hour_independent_of_host_timezone(self):
        # 同一 UTC 瞬间，用非 UTC tzinfo 表达也必须得到相同北京小时（先归一到 UTC）。
        from datetime import timezone as _tz
        other = datetime(2026, 10, 8, 9, 0, 0, tzinfo=_tz(timedelta(hours=-5)))  # = 14:00 UTC
        self.assertEqual(dp._beijing_hour(other), 22)
        self.assertEqual(dp._beijing_hour(self.IN_2200), 22)
        # naive 按 UTC 理解。
        self.assertEqual(dp._beijing_hour(datetime(2026, 10, 8, 14, 0, 0)), 22)

    def test_three_max_window_gated_only_in_medium(self):
        # BW-MANUAL-THREE-MODE-20261023：三个 Qwen4-Max-Preview 的北京 22:00–08:00 夜间门
        # （含自定义/内置/国际）边界左闭右开；时段门机制本身改在 medium 档逐字验证。
        # BW-WEAK-TIER-ENFORCE-20261122: 弱档纯优先级队列、无任何时段门——同一边界
        # 断言在 weak 全部恒 False（不再释放/不再挡下）。
        for pk in self.ALL_QMAX:
            with dispatch_mode('medium'):
                self.assertTrue(dp._max_window_blocks(pk, self.NOON), pk)
                self.assertTrue(dp._max_window_blocks(pk, self.BJ_1000), pk)
                self.assertTrue(dp._max_window_blocks(pk, self.OUT_0800), pk)
                self.assertTrue(dp._max_window_blocks(pk, self.OUT_2159), pk)
                self.assertFalse(dp._max_window_blocks(pk, self.IN_2200), pk)
                self.assertFalse(dp._max_window_blocks(pk, self.IN_0759), pk)
            with dispatch_mode('weak'):
                self.assertFalse(dp._max_window_blocks(pk, self.NOON), pk)
                self.assertFalse(dp._max_window_blocks(pk, self.BJ_1000), pk)
                self.assertFalse(dp._max_window_blocks(pk, self.OUT_0800), pk)
                self.assertFalse(dp._max_window_blocks(pk, self.OUT_2159), pk)
                self.assertFalse(dp._max_window_blocks(pk, self.IN_2200), pk)
                self.assertFalse(dp._max_window_blocks(pk, self.IN_0759), pk)

    def test_intl_max_gate_is_mode_specific(self):
        # strong：五个可派池全部无时段门（国际 Max 白天/任意时刻都不挡）；
        # medium：国际 Max 与另两个 Max 同受夜间门；claude 受工作日高峰门。
        # BW-WEAK-TIER-ENFORCE-20261122：弱档是纯优先级队列、**无任何时段门**——权威
        # 口径 window_gated_pools('weak') 为空元组；_max_window_blocks 在 weak 恒 False
        # （含 Claude 高峰门与 Max 夜间门）；被移出弱档的国际 Max 在 weak 一律
        # mode_excluded（档位硬策略先于一切，时段门判定对弱档已不可达）。
        for now in (self.OUT_0800, self.BJ_1000, self.OUT_2159, self.NOON, self.LATE,
                    self.IN_2200, self.IN_0759, T0):
            with dispatch_mode('strong'):
                self.assertFalse(dp._max_window_blocks(self.QMAX, now), now)
            with dispatch_mode('medium'):
                self.assertEqual(dp._max_window_blocks(self.QMAX, now),
                                 not dp._main_force_window_open(now), now)
            # BW-WEAK-TIER-ENFORCE-20261122: 弱档纯优先级队列、无任何时段门——
            # _max_window_blocks 在 weak 恒 False（含 Claude 高峰门与 Max 夜间门）。
            with dispatch_mode('weak'):
                self.assertFalse(dp._max_window_blocks(self.QMAX, now), now)
        # strong 为空集；medium = 三个 Max + Claude；weak（BW-WEAK-TIER-ENFORCE-
        # 20261122）纯优先级队列 → 无任何时段门（空元组）。
        self.assertEqual(tuple(dp.window_gated_pools('strong')), ())
        self.assertEqual(set(dp.window_gated_pools('medium')),
                         set(self.ALL_QMAX) | {dp.CLAUDE_POOL_KEY})
        self.assertEqual(tuple(dp.window_gated_pools('weak')), ())
        self.assertEqual(tuple(dp.WINDOW_GATED_MAIN_FORCES),
                         tuple(dp.window_gated_pools('weak')))
        # ZCode 主力、两地区 Flash 兜底、Luna、retired DeepSeek 一律不受时段门约束。
        for pk in ('zcode:GLM-5.3',
                   'qoder:Qwen3.8-Flash', 'qodercn:Qwen3.8-Flash',
                   'qodercn:DeepSeek-Flash', dp.LUNA_KEY):
            self.assertFalse(dp._max_window_blocks(pk, self.NOON), pk)

    def test_auto_daytime_claims_intl_max_in_strong_mode(self):
        # BW-MANUAL-THREE-MODE-20261023：**strong 档**六个可派池全部无时段门——白天（北京
        # 12:00）AUTO 请求国际内置 Max 直接 claim 该池，绝不因时段门改派到别池。
        # strong 层序 ZCode(2) > claude(3) > 三个 Max(4)，故先占满上方 ZCode、claude
        # 各槽。BW-QWEN4-SONUS-REMOVE-20261115：Sonus 从所有档位去除——显式 reserve
        # 一律 mode_excluded，绝不占用其槽。
        with dispatch_mode('strong'):
            sonus_refused = dp.reserve(self.store, task_id='s0x', runtime='qoder',
                                       model='Sonus', workspace=str(self._ws_for('s0x')),
                                       prompt_sha256='p', executor='qoder', now=self.NOON,
                                       _preclaim=False)
            self.assertFalse(sonus_refused['allowed'], sonus_refused)
            self.assertEqual(sonus_refused['reason'], 'mode_excluded', sonus_refused)
            self.reserve_exec('z0', 'zcode', 'GLM-5.3', 'zcode', pk='zcode:GLM-5.3')
            self.reserve_exec('c1', 'claude', 'deepseek-flash', 'claude',
                              pk=dp.CLAUDE_POOL_KEY)
            self.reserve_exec('c2', 'claude', 'deepseek-flash', 'claude',
                              pk=dp.CLAUDE_POOL_KEY)
            r = dp.select_and_claim(self.store, task_id='a1', runtime='qoder',
                                    model='Qwen4-Max-Preview', workspace=str(self._ws_for('a1')),
                                    prompt_sha256='p', now=self.NOON, _preclaim=False)
            self.assertTrue(r.get('allowed'), r)
            self.assertFalse(r.get('routing_required'), r)
            self.assertEqual(r['pool_key'], self.QMAX, r)
        # weak（默认）同一白天时刻：BW-WEAK-RESTRUCTURE-20261116 起三个 Max 已整体移出
        # 弱档——AUTO 请求国际 Max 如实 mode_excluded（绝不改派、绝不落任何 Max）。
        r2 = dp.select_and_claim(self.store, task_id='a1w', runtime='qoder',
                                 model='Qwen4-Max-Preview', workspace=str(self._ws_for('a1w')),
                                 prompt_sha256='p', now=self.NOON, _preclaim=False)
        self.assertFalse(r2.get('allowed'), r2)
        self.assertRejected(r2, 'mode_excluded')

    def test_auto_daytime_falls_to_claude_then_refuses_cross_mode_fallback(self):
        # BW-WEAK-RESTRUCTURE-20261116：三个 Qoder Max 与两 OpenCode/CBGLM 不在同一档
        # 集合——「白天 Max 被挡 → routing claude」机制改在 medium 档验证（北京周一
        # 13:00 离峰：Max 夜间门外被 eligible 剔除、claude 不在高峰门内）→ AUTO 请求
        # CN 内置 Max 应 routing 到 claude。再把 claude 两槽与 zcode 占满 → 主力全满且
        # 两 Flash 兜底不在该档 → 如实 mode_excluded（绝不跨档兜底、绝不落任一 Max）。
        WEEKDAY_OFFPEAK = datetime(2026, 10, 12, 5, 0, 0, tzinfo=timezone.utc)  # 北京 13:00 周一
        with dispatch_mode('medium'):
            self.reserve_exec('z0', 'zcode', 'GLM-5.3', 'zcode',
                              pk='zcode:GLM-5.3')
            self.reserve_exec('cu1', 'qodercn', 'Qwen-3.8-Max', 'qodercn',
                              pk='qodercn:Qwen-3.8-Max')
            self.reserve_exec('cu2', 'qodercn', 'Qwen-3.8-Max', 'qodercn',
                              pk='qodercn:Qwen-3.8-Max')
            r = dp.select_and_claim(self.store, task_id='a2', runtime='qodercn',
                                    model='Qwen4-Max-Preview',
                                    workspace=str(self._ws_for('a2')),
                                    prompt_sha256='p', now=WEEKDAY_OFFPEAK,
                                    _preclaim=False)
            self.assertTrue(r['routing_required'], r)
            self.assertEqual(r['selected']['pool_key'], dp.CLAUDE_POOL_KEY, r)
            # claude 两槽占满后才无主力可派。
            self.reserve_exec('c1', 'claude', 'deepseek-flash', 'claude',
                              pk=dp.CLAUDE_POOL_KEY)
            self.reserve_exec('c2', 'claude', 'deepseek-flash', 'claude',
                              pk=dp.CLAUDE_POOL_KEY)
            r2 = dp.select_and_claim(self.store, task_id='a3', runtime='qodercn',
                                     model='Qwen4-Max-Preview',
                                     workspace=str(self._ws_for('a3')),
                                     prompt_sha256='p', now=WEEKDAY_OFFPEAK,
                                     _preclaim=False)
            self.assertFalse(r2.get('allowed'), r2)
            self.assertRejected(r2, 'mode_excluded')

    def test_auto_daytime_never_falls_to_cn_max_in_strong_mode(self):
        # strong 档三个 Max 同层且无门；占满上方 ZCode 与 claude 后，AUTO 请求
        # 国际 Max 时由「请求组合优先」命中国际 Max，绝不落到 CN 自定义或 CN 内置 Max。
        # BW-QWEN4-SONUS-REMOVE-20261115：Sonus 从所有档位去除——显式 reserve 被拒
        # （mode_excluded、零占位），「占满上方层后由请求组合命中」断言强度不变。
        with dispatch_mode('strong'):
            r0 = dp.reserve(self.store, task_id='s0', runtime='qoder',
                            model='Sonus', workspace=str(self._ws_for('s0')),
                            prompt_sha256='p', executor='qoder', now=self.NOON,
                            _preclaim=False)
            self.assertFalse(r0['allowed'], r0)
            self.assertEqual(r0['reason'], 'mode_excluded', r0)
            self.reserve_exec('z0', 'zcode', 'GLM-5.3', 'zcode', pk='zcode:GLM-5.3')
            self.reserve_exec('c1', 'claude', 'deepseek-flash', 'claude',
                              pk=dp.CLAUDE_POOL_KEY)
            self.reserve_exec('c2', 'claude', 'deepseek-flash', 'claude',
                              pk=dp.CLAUDE_POOL_KEY)
            r = dp.select_and_claim(self.store, task_id='cu', runtime='qoder',
                                    model='Qwen4-Max-Preview', workspace=str(self._ws_for('cu')),
                                    prompt_sha256='p', now=self.NOON, _preclaim=False)
            self.assertTrue(r.get('allowed'), r)
            self.assertEqual(r['pool_key'], self.QMAX, r)
            self.assertNotEqual(r['pool_key'], self.CXMAX, r)
            self.assertNotEqual(r['pool_key'], self.CNMAX, r)

    def test_explicit_reserve_custom_max_daytime_honestly_refused(self):
        # BW-CUSTOM-MAX-NIGHT-20261010-S1：白天显式 executor='qodercn' 请求自定义 Max →
        # 如实拒绝（0 提交、main_force_window_closed），绝不静默换成同区 CN Flash。
        r = dp.reserve(self.store, task_id='cdmax', runtime='qodercn',
                       model='Qwen-3.8-Max', workspace=str(self._ws_for('cdmax')),
                       prompt_sha256='p', now=self.NOON, executor='qodercn',
                       _preclaim=False)
        self.assertFalse(r['allowed'], r)
        self.assertFalse(r.get('sent', True), r)
        self.assertEqual(r['reason'], 'mode_excluded', r)
        self.assertIn('用户要求禁用', r['reasons'][0])
        self.assertFalse(r['routing_required'], r)

    def test_explicit_reserve_custom_max_night_refused(self):
        # 时段内（北京 23:00 / 22:00 含）显式请求自定义 Max 正常 claim，与内置时段口径一致。
        for now in (self.IN_2200, T0):
            r = dp.reserve(self.store, task_id=f'cdmax-{now.hour}', runtime='qodercn',
                           model='Qwen-3.8-Max',
                           workspace=str(self._ws_for(f'cdmax-{now.hour}')),
                           prompt_sha256='p', now=now, executor='qodercn',
                           _preclaim=False)
            self.assertFalse(r['allowed'], r)
            self.assertEqual(r['reason'], 'mode_excluded', r)
            self.assertIn('用户要求禁用', r['reasons'][0])

    def test_explicit_reserve_cn_builtin_max_daytime_honestly_refused(self):
        # 主脑显式 reserve 新任务到 CN 内置 Max、白天 → 如实拒绝（时段门机制），绝不静默
        # 换成 Flash。BW-WEAK-RESTRUCTURE-20261116：CN 内置 Max 已移出弱档，夜间门机制
        # 改在 medium 档验证。
        with dispatch_mode('medium'):
            r = dp.reserve(self.store, task_id='r-cn', runtime='qodercn',
                           model='Qwen4-Max-Preview', workspace=str(self._ws_for('r-cn')),
                           prompt_sha256='p', now=self.NOON, executor='qodercn',
                           _preclaim=False)
        self.assertFalse(r['allowed'], r)
        self.assertFalse(r.get('sent', True), r)
        self.assertEqual(r['reason'], 'main_force_window_closed', r)
        self.assertFalse(r['routing_required'], r)

    def test_explicit_reserve_intl_max_daytime_allowed_in_strong_mode(self):
        # strong 档：国际内置 Max 无夜间门，白天显式 executor='qoder' 真实 claim 本池
        # （绝不 main_force_window_closed、绝不改派 Flash）。
        # medium 档同一时刻如实 main_force_window_closed（夜间门对照，见下方）；
        # BW-WEAK-RESTRUCTURE-20261116：weak（默认）档国际 Max 已整体移出 → mode_excluded。
        for now in (self.NOON, self.OUT_0800, self.OUT_2159, self.LATE):
            with dispatch_mode('strong'):
                r = dp.reserve(self.store, task_id=f'ri-{now.hour}', runtime='qoder',
                               model='Qwen4-Max-Preview',
                               workspace=str(self._ws_for(f'ri-{now.hour}')),
                               prompt_sha256='p', now=now, executor='qoder',
                               _preclaim=False)
                # 每次 claim 后立即释放，保证同一容量 1 槽可重复验证多个时间点。
                self.assertTrue(r['allowed'], r)
                self.assertEqual(r['pool_key'], self.QMAX, r)
                dp.finish(self.store, r['token'], terminal='cancelled', now=now)
            with dispatch_mode('medium'):
                rw = dp.reserve(self.store, task_id=f'rw-{now.hour}', runtime='qoder',
                                model='Qwen4-Max-Preview',
                                workspace=str(self._ws_for(f'rw-{now.hour}')),
                                prompt_sha256='p', now=now, executor='qoder',
                                _preclaim=False)
            self.assertFalse(rw['allowed'], rw)
            self.assertEqual(rw['reason'], 'main_force_window_closed', rw)
        with dispatch_mode('weak'):
            rwk = dp.reserve(self.store, task_id='rw-weak', runtime='qoder',
                             model='Qwen4-Max-Preview',
                             workspace=str(self._ws_for('rw-weak')),
                             prompt_sha256='p', now=self.NOON, executor='qoder',
                             _preclaim=False)
        self.assertFalse(rwk['allowed'], rwk)
        self.assertRejected(rwk, 'mode_excluded')

    def test_explicit_executor_flash_pool_mode_excluded(self):
        # BW-WEAK-RESTRUCTURE-20261116：qoder:Qwen3.8-Flash 已从所有档位可新派集合移除
        # （纯 mode_excluded）——原「明确 executor 请求本地区 Flash 正常 claim」路径随
        # 溢出池移除而消失；改验显式 executor='qoder' 请求 Flash 一律 mode_excluded
        # （sent=false、零新占位），时段门/容量永不先于档位硬策略生效。
        r = dp.reserve(self.store, task_id='fl', runtime='qoder', model='Qwen3.8-Flash',
                       workspace=str(self._ws_for('fl')), prompt_sha256='p',
                       now=self.NOON, executor='qoder', _preclaim=False)
        self.assertFalse(r['allowed'], r)
        self.assertRejected(r, 'mode_excluded')
        self.assertFalse(r.get('sent', True), r)

    def test_reserve_in_window_builtin_max_allowed(self):
        # BW-WEAK-RESTRUCTURE-20261116：qoder Max 移出弱档，窗口内 claim 机制改在
        # medium 档验证（北京 22:00 含夜间门开启）。
        with dispatch_mode('medium'):
            r = dp.reserve(self.store, task_id='ok', runtime='qoder',
                           model='Qwen4-Max-Preview',
                           workspace=str(self._ws_for('ok')), prompt_sha256='p',
                           now=self.IN_2200, executor='qoder', _preclaim=False)
        self.assertTrue(r['allowed'], r)
        self.assertEqual(r['pool_key'], self.QMAX, r)

    def test_reserved_cn_builtin_max_crossing_08_is_really_released(self):
        # 22:00 时段内预留 CN 内置 Max，跨过 08:00 后才启动 → 真实释放本 reserved 占位为
        # start_failed（国际 Max 已全天，本门只对两个 CN Max 生效）。
        # BW-WEAK-RESTRUCTURE-20261116：CN 内置 Max 移出弱档，reserve/consume 在 medium 档。
        with dispatch_mode('medium'):
            r = dp.reserve(self.store, task_id='x08', runtime='qodercn',
                           model='Qwen4-Max-Preview',
                           workspace=str(self._ws_for('x08')), prompt_sha256='p',
                           now=self.IN_2200, executor='qodercn', _preclaim=False)
            tok = self.assertClaimed(r, self.CNMAX)
            self.assertEqual(dp.status(self.store,
                                       now=self.IN_2200)['pools'][self.CNMAX]['active'], 1)
            out = dp.consume_for_entry(self.store, task_id='x08', runtime='qodercn',
                                       model='Qwen4-Max-Preview',
                                       workspace=str(self._ws_for('x08')),
                                       prompt_sha256='p', claim_token=tok,
                                       wrapper_pid=4321, wrapper_created='w1',
                                       now=self.LATE)
        self.assertFalse(out['allowed'], out)
        self.assertFalse(out.get('sent', True), out)
        self.assertEqual(out['reason'], 'main_force_window_closed', out)
        self.assertTrue(out['capacity_released'], out)
        self.assertEqual(self._state(tok), 'start_failed')
        self.assertEqual(dp.status(self.store, now=self.LATE)['pools'][self.CNMAX]['active'], 0)

    def test_reserved_intl_max_crossing_08_still_starts_in_strong_mode(self):
        # strong 档：国际 Max 无夜间门——22:00 预留、08:30 才启动必须照常放行到 running，
        # 绝不释放占位、绝不误判 main_force_window_closed。
        with dispatch_mode('strong'):
            r = dp.reserve(self.store, task_id='xi08', runtime='qoder',
                           model='Qwen4-Max-Preview',
                           workspace=str(self._ws_for('xi08')), prompt_sha256='p',
                           now=self.IN_2200, executor='qoder', _preclaim=False)
            tok = self.assertClaimed(r, self.QMAX)
            out = dp.consume_for_entry(
                self.store, task_id='xi08', runtime='qoder', model='Qwen4-Max-Preview',
                workspace=str(self._ws_for('xi08')), prompt_sha256='p',
                claim_token=tok, wrapper_pid=4321, wrapper_created='w1', now=self.LATE)
            self.assertTrue(out['allowed'], out)
            self.assertEqual(self._state(tok), 'running')
            self.assertEqual(dp.status(self.store, now=self.LATE)['pools'][self.QMAX]['active'],
                             1)

    def test_reserved_intl_max_crossing_08_is_really_released_in_medium_mode(self):
        # medium 档：国际 Max 与另两个 Max 同受北京 22:00–08:00 夜间门——22:00 预留、
        # 08:30 启动必须真实释放本 reserved 占位为 start_failed（绝不泄漏、绝不抢占
        # 别人），且不冒充时段外改派。
        # BW-WEAK-RESTRUCTURE-20261116：三个 Max 已整体移出弱档，本机制改在 medium 档验证。
        with dispatch_mode('medium'):
            r = dp.reserve(self.store, task_id='wi08', runtime='qoder',
                           model='Qwen4-Max-Preview',
                           workspace=str(self._ws_for('wi08')), prompt_sha256='p',
                           now=self.IN_2200, executor='qoder', _preclaim=False)
            tok = self.assertClaimed(r, self.QMAX)
            out = dp.consume_for_entry(self.store, task_id='wi08', runtime='qoder',
                                       model='Qwen4-Max-Preview',
                                       workspace=str(self._ws_for('wi08')),
                                       prompt_sha256='p', claim_token=tok,
                                       wrapper_pid=4321, wrapper_created='w1',
                                       now=self.LATE)
        self.assertFalse(out['allowed'], out)
        self.assertEqual(out['reason'], 'main_force_window_closed', out)
        self.assertTrue(out['capacity_released'], out)
        self.assertEqual(self._state(tok), 'start_failed')

    def test_running_cn_builtin_max_crossing_08_not_falsely_released(self):
        # 时段内已 consume 到 running，跨 08 点后重复启动 → 拒绝但不假释放，保留 running。
        # BW-WEAK-RESTRUCTURE-20261116：CN 内置 Max 移出弱档，reserve/consume 在 medium 档。
        with dispatch_mode('medium'):
            r = dp.reserve(self.store, task_id='rn', runtime='qodercn',
                           model='Qwen4-Max-Preview',
                           workspace=str(self._ws_for('rn')), prompt_sha256='p',
                           now=self.IN_2200, executor='qodercn', _preclaim=False)
            tok = self.assertClaimed(r, self.CNMAX)
            c1 = dp.consume_for_entry(self.store, task_id='rn', runtime='qodercn',
                                      model='Qwen4-Max-Preview',
                                      workspace=str(self._ws_for('rn')),
                                      prompt_sha256='p', claim_token=tok,
                                      wrapper_pid=7000, wrapper_created='c7000',
                                      now=self.IN_2200 + timedelta(seconds=1))
            self.assertTrue(c1['allowed'], c1)
            c2 = dp.consume_for_entry(self.store, task_id='rn', runtime='qodercn',
                                      model='Qwen4-Max-Preview',
                                      workspace=str(self._ws_for('rn')),
                                      prompt_sha256='p', claim_token=tok,
                                      wrapper_pid=7000, wrapper_created='c7000',
                                      now=self.LATE)
        self.assertEqual(self._state(tok), 'running')
        self.assertFalse(c2['allowed'], c2)
        self.assertFalse(c2['capacity_released'], c2)
        self.assertEqual(self._state(tok), 'running')
        self.assertEqual(dp.status(self.store, now=self.LATE)['pools'][self.CNMAX]['active'], 1)

    def test_reserved_custom_max_crossing_08_is_really_released(self):
        # BW-CUSTOM-MAX-NIGHT-20261010-S1：时段内预留自定义 Max、跨过 08:00 后才启动 →
        # 真实释放本 reserved 占位为 start_failed（与内置 Max 同一 consume 门，绝不泄漏）。
        # BW-WEAK-TIER-ENFORCE-20261122: 弱档纯优先级队列、无任何时段门——「跨 08:00
        # 真实释放」语义改在 medium 档逐字验证（夜间门在 medium 照旧生效）；弱档同一
        # consume 恒放行（allowed=True、占位转 running、绝不释放）。
        tok = self.legacy_custom_claim('cx08')
        self.assertEqual(dp.status(self.store, now=self.IN_2200)['pools'][self.CXMAX]['active'], 1)
        with dispatch_mode('medium'):
            out = dp.consume_for_entry(self.store, task_id='cx08', runtime='qodercn',
                                       model='Qwen-3.8-Max', workspace=str(self._ws_for('cx08')),
                                       prompt_sha256='p', claim_token=tok,
                                       wrapper_pid=4321, wrapper_created='w1', now=self.LATE)
        self.assertFalse(out['allowed'], out)
        self.assertFalse(out.get('sent', True), out)
        self.assertEqual(out['reason'], 'main_force_window_closed', out)
        self.assertTrue(out['capacity_released'], out)
        self.assertEqual(self._state(tok), 'start_failed')
        self.assertEqual(dp.status(self.store, now=self.LATE)['pools'][self.CXMAX]['active'], 0)
        # BW-WEAK-TIER-ENFORCE-20261122: 弱档对照——同一「时段内预留、跨 08:00 后
        # consume」不再有门 → 如实放行启动，绝不释放占位。
        tok_w = self.legacy_custom_claim('cx08w')
        out_w = dp.consume_for_entry(self.store, task_id='cx08w', runtime='qodercn',
                                     model='Qwen-3.8-Max', workspace=str(self._ws_for('cx08w')),
                                     prompt_sha256='p', claim_token=tok_w,
                                     wrapper_pid=4321, wrapper_created='w1', now=self.LATE)
        self.assertTrue(out_w['allowed'], out_w)
        self.assertFalse(out_w.get('capacity_released'), out_w)
        self.assertEqual(self._state(tok_w), 'running')
        self.assertEqual(dp.status(self.store, now=self.LATE)['pools'][self.CXMAX]['active'], 1)

    def test_running_custom_max_crossing_08_not_falsely_released(self):
        # 时段内已 consume 到 running 的自定义 Max，跨 08 点后重复启动 → 拒绝但不假释放，
        # 保留 running（活任务保护），绝不抢占别的在途名额。
        tok = self.legacy_custom_claim('cxrn')
        c1 = dp.consume_for_entry(self.store, task_id='cxrn', runtime='qodercn',
                                  model='Qwen-3.8-Max', workspace=str(self._ws_for('cxrn')),
                                  prompt_sha256='p', claim_token=tok,
                                  wrapper_pid=7000, wrapper_created='c7000',
                                  now=self.IN_2200 + timedelta(seconds=1))
        self.assertTrue(c1['allowed'], c1)
        self.assertEqual(self._state(tok), 'running')
        c2 = dp.consume_for_entry(self.store, task_id='cxrn', runtime='qodercn',
                                  model='Qwen-3.8-Max', workspace=str(self._ws_for('cxrn')),
                                  prompt_sha256='p', claim_token=tok,
                                  wrapper_pid=7000, wrapper_created='c7000', now=self.LATE)
        self.assertFalse(c2['allowed'], c2)
        self.assertFalse(c2['capacity_released'], c2)
        self.assertEqual(self._state(tok), 'running')
        self.assertEqual(dp.status(self.store, now=self.LATE)['pools'][self.CXMAX]['active'], 1)

    def test_custom_max_night_two_slots_then_full(self):
        # 夜间自定义 Max 容量 2：两槽填满后 active=2；第三个显式自定义请求绝不落第 3 个
        # 自定义槽（改道同区 CN Flash 兜底，capacity/锁/限额口径不弱化）。
        for tid in ('cn0', 'cn1'):
            self.legacy_custom_claim(tid)
        self.assertEqual(dp.status(self.store, now=self.IN_2200)['pools'][self.CXMAX]['active'], 2)
        r3 = dp.reserve(self.store, task_id='cn2', runtime='qodercn',
                        model='Qwen-3.8-Max', workspace=str(self._ws_for('cn2')),
                        prompt_sha256='p', now=self.IN_2200, executor='qodercn',
                        _preclaim=False)
        # 第三个自定义绝不落第 3 个自定义槽：allowed=False、改道同区 CN Flash（selected），
        # 自定义 active 仍为 2（不弱化 capacity/锁/限额）。
        self.assertFalse(r3['allowed'], r3)
        self.assertFalse(r3['routing_required'], r3)
        self.assertEqual(r3['reason'], 'mode_excluded', r3)
        self.assertEqual(dp.status(self.store, now=self.IN_2200)['pools'][self.CXMAX]['active'], 2)

    def test_claim_due_degradation_uses_same_window_filter(self):
        # 白天降级 claim_due：原 scope 钉住 CN 内置 Max、该 Max 恰好有空位，也不回收 Max
        # （同一时段候选过滤），保持 pending、如实 main_force_window_closed，绝不静默换
        # 模型或偷偷启动 Max。先填满全部国内槽（BW-WEAK-RESTRUCTURE-20261116 起现 17）记录
        # 票据，再释放 CN 内置 Max 制造"有空位但在时段外"。
        # BW-WEAK-RESTRUCTURE-20261116：CN Max 池已移出弱档——pinned combo 的时段过滤
        # 机制改在 medium 档验证（CN 内置 Max 在 medium 受同一夜间门）。
        toks = self.fill_domestic()
        ws = str(self._ws_for('cd'))
        scope = {'task_id': 'cd', 'stage': 's1', 'chat_id': 'c1', 'workspace': ws,
                 'prompt_sha256': 'p' * 64, 'executor': 'qodercn',
                 'model': 'Qwen4-Max-Preview'}
        with dispatch_mode('medium'):
            rec = dp.ask_record(self.store, task_id='cd', scope=scope,
                                ask_message_id='m1', now=self.IN_2200)
            self.assertTrue(rec['recorded'], rec)
            # 释放 CN 内置 Max（fill 顺序 [glm, zcode, qoderMax, cnMax, ...] 的第四个
            # token），腾出一个"时段外仍不该回收"的空位。
            dp.finish(self.store, toks[3], terminal='cancelled', now=self.IN_2200)
            self.assertEqual(dp.status(self.store,
                                       now=self.NOON)['pools'][self.CNMAX]['free'], 1)
            out = dp.claim_due(self.store, task_id='cd', scope=scope, now=self.NOON)
        self.assertFalse(out['claimed'], out)
        self.assertTrue(out.get('main_force_window_closed'), out)
        self.assertTrue(out.get('model_unavailable'), out)
        self.assertNotIn('token', out)
        # Max 空位保持不被回收、也没有被换成别的模型提交。
        self.assertEqual(dp.status(self.store, now=self.NOON)['pools'][self.CNMAX]['active'], 0)


class LunaDisabledTests(_PoolBase):
    """BW-LUNA-DISABLED-20261019：所有新 Luna 路径机械拒绝且不落票据/attempt。"""

    def setUp(self):
        super().setUp()
        self._old_disabled = dp.LUNA_DISABLED
        self._old_cap = dp.CAPACITY.pop(dp.LUNA_KEY, None)
        dp.LUNA_DISABLED = True

    def tearDown(self):
        dp.LUNA_DISABLED = self._old_disabled
        dp.CAPACITY[dp.LUNA_KEY] = self._old_cap
        super().tearDown()

    def test_luna_removed_from_capacity_and_all_entry_points_refuse(self):
        self.assertNotIn(dp.LUNA_KEY, dp.CAPACITY)
        common = dict(task_id='L-disabled', runtime='luna', model='native',
                      workspace=self._ws_for('L-disabled'), prompt_sha256='a' * 64, stage='s')
        for out in (dp.reserve(self.store, **common, _preclaim=False),
                    dp.select_and_claim(self.store, **common, _preclaim=False)):
            self.assertFalse(out.get('allowed'), out)
            self.assertEqual(out.get('reason'), 'luna_disabled', out)
        ask = dp.ask_record(self.store, task_id='L-disabled', scope=self._scope('L-disabled'),
                            ask_message_id='msg')
        self.assertEqual(ask.get('reason'), 'luna_disabled', ask)
        due = dp.claim_due(self.store, task_id='L-disabled', _preclaim=False)
        self.assertEqual(due.get('reason'), 'luna_disabled', due)
        self.assertEqual(dp.record_agent_id(self.store, task_id='L-disabled', agent_id='a')
                         .get('reason'), 'luna_disabled')
        self.assertEqual(dp.settle_native(self.store, task_id='L-disabled', token='t',
                                          agent_id='a', terminal='finished')
                         .get('reason'), 'luna_disabled')
        st = dp.status(self.store)
        self.assertNotIn(dp.LUNA_KEY, st['pools'])

if __name__ == '__main__':
    unittest.main(verbosity=2)

