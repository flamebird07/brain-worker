"""BW-CODEBUDDY-MAIN-S3 离线回归：独立 CodeBuddy CLI 精确模型 deepseek-v4.1-flash 恢复。

全部使用临时 SQLite + 进程内合成 stub（tests/stub_codebuddy_live.py），零真实网络/账号/模型/
子进程、零真实池。覆盖任务政策：
- 独立池 codebuddy:deepseek-v4.1-flash=1（BW-WORKER-WIDGET-20261018-S3 起与 GLM 各自
  独立 1 槽），AUTO 层=3（BW-CB-REORDER-20261020 用户明确指令：由旧层=0 下调，排三
  Qwen3.8-Max（1）与 claude（2）之后、紧挨 opencode:deepseek-v4.1-flash-free（4）之前）；
  本轮真实失败（round 候选剔除）后落其余合格层；显式 executor/qodercn 不越权跨执行器改派；
- 仅 1 个 CodeBuddy deepseek，第二个拒绝，终态释放后可复用；总 16（BW-OPENCODE-20261019
  起含两 OpenCode 免费模型各 1 槽）与迁移债务守卫、同 workspace/unknown 不绕过；
- codebuddy 无时段门（PEAK/GAP/DAY 仍可选，不套 Claude 离峰窗 / Max 夜间窗）；
- WorkBuddy 与 CodeBuddy 旧模型（含旧 GLM）仍退休：execution_control 只为精确模型解禁；
- 正式 codebuddy_live 传输用可移植 synthetic stub 验证成功读写回执/报告绑定、模型漂移拒绝、
  exit0 但错误/无工具不伪成功、启动/取消/异常释放或 unknown 保留、不误清既有占位；
- 工具证据只按原始 tool_use/tool_result 统计：合成 Read+Write 成功回执、零工具格式合格报告
  （无工程执行证据）、缺 tool_result 回执如实列缺失；business_verified 恒 false；
- 超时口径：缺省不加墙钟限制、显式正数才生效、<=0 零副作用拒绝；起止时间真实采集，
  注入时钟保持离线确定性；
- 正式 main 只为该精确 model 开放，其它 model 派发前零副作用 manual_relay_only 拒绝。
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / 'scripts'
for p in (str(SCRIPTS), str(REPO / 'tests')):
    if p not in sys.path:
        sys.path.insert(0, p)

import dispatch_pool as dp  # noqa: E402
from dispatch_mode_support import dispatch_mode, user_disabled_pools  # noqa: E402
import dispatch_mode as dm  # noqa: E402
import execution_control as ec  # noqa: E402
import prompt_contract as pc  # noqa: E402
import codebuddy_direct as cb  # noqa: E402
import codebuddy_live as cl  # noqa: E402
import stub_codebuddy_live as stub  # noqa: E402

CB = 'codebuddy:deepseek-v4.1-flash'
CB_MODEL = 'deepseek-v4.1-flash'
QMAX = 'qoder:Qwen3.8-Max'
CN_MAX = 'qodercn:Qwen3.8-Max'
CN_CUSTOM_MAX = 'qodercn:Qwen-3.8-Max'
CLAUDE = 'claude:deepseek-flash'
ZCODE = 'zcode:GLM-5.3'
QFLASH = 'qoder:Qwen3.8-Flash'
CN_FLASH = 'qodercn:Qwen3.8-Flash'
MAXES = (QMAX, CN_MAX, CN_CUSTOM_MAX)
SHA = 'p' + '0' * 63

# 北京时间工作日高峰（两个 CN Max 窗外；claude 自 BW-CLAUDE-ALLDAY-QMAX-AFTER-20261023
# 起全天可派）/间隙/夜间，用于验证 codebuddy 无时段门。
PEAK = datetime(2026, 10, 12, 1, 0, 0, tzinfo=timezone.utc)    # 北京周一 09:00
GAP = datetime(2026, 10, 12, 4, 30, 0, tzinfo=timezone.utc)    # 北京周一 12:30
DAY = datetime(2026, 10, 8, 0, 0, 0, tzinfo=timezone.utc)      # 北京 08:00（窗外）
NIGHT = datetime(2026, 10, 8, 15, 0, 0, tzinfo=timezone.utc)   # 北京 23:00


def _valid_report(stage, workspace) -> str:
    """构造通过 analyze_report 正文格式门的合成九节报告：独占首末标记、阶段 token、路径等值、
    九标题顺序、结语。仅作离线绑定回归的合成正文，不代表任何真实执行结果。"""
    body = [pc.REPORT_START,
            f'阶段编号与执行方式：{stage}；direct。',
            f'实际项目绝对路径：{workspace}',
            '汇报时间与执行环境：offline-synthetic']
    for header in pc.SECTION_HEADERS:
        body.append('')
        body.append(header)
        body.append('- 合成小节内容')
    body.append('')
    body.append(pc.CLOSING_LINE)
    body.append(pc.REPORT_END)
    return '\n'.join(body)


def _success_stream(report_text, *, model=CB_MODEL, session='S1', errors=None,
                    tools=('Read', 'Write')):
    """构造合法成功 stream-json。tools 非空时附带**真实形状**的 tool_use 与同 ID 成功
    tool_result（Read + Write），供适配器统计工具证据；tools=() 时为零工具纯文本流。
    这是**合成协议**，不执行真实文件 IO，也不证明任何业务结果。errors 非空会破坏成功
    信封（用于 exit0 但不伪成功）。"""
    err = [] if errors is None else errors
    init = {'type': 'system', 'subtype': 'init', 'session_id': session,
            'model': model, 'tools': list(tools), 'mcp_servers': [],
            'permissionMode': 'dontAsk'}
    lines = [json.dumps(init, ensure_ascii=False)]
    if tools:
        calls = [{'type': 'tool_use', 'id': 'tu-read-1', 'name': 'Read',
                  'input': {'file_path': 'C:/ws/probe.txt'}},
                 {'type': 'tool_use', 'id': 'tu-write-1', 'name': 'Write',
                  'input': {'file_path': 'C:/ws/probe.txt', 'content': 'ok'}}]
        lines.append(json.dumps(
            {'type': 'assistant', 'session_id': session,
             'message': {'model': model,
                         'usage': {'input_tokens': 5, 'output_tokens': 7},
                         'content': calls}}, ensure_ascii=False))
        lines.append(json.dumps(
            {'type': 'user', 'session_id': session,
             'message': {'content': [
                 {'type': 'tool_result', 'tool_use_id': 'tu-read-1',
                  'is_error': False,
                  'content': [{'type': 'text', 'text': 'probe file body'}]},
                 {'type': 'tool_result', 'tool_use_id': 'tu-write-1',
                  'is_error': False,
                  'content': [{'type': 'text', 'text': 'file written'}]}]}},
            ensure_ascii=False))
    lines.append(json.dumps(
        {'type': 'assistant', 'session_id': session,
         'message': {'model': model, 'usage': {'input_tokens': 5, 'output_tokens': 7},
                     'content': [{'type': 'text', 'text': report_text}]}},
        ensure_ascii=False))
    result = {'type': 'result', 'subtype': 'success', 'is_error': False,
              'session_id': session, 'usage': {'input_tokens': 5, 'output_tokens': 7},
              'modelUsage': {}, 'result': report_text, 'errors': err}
    lines.append(json.dumps(result, ensure_ascii=False))
    return ('\n'.join(lines) + '\n').encode('utf-8')


class _PoolBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='bw-cb-react-')
        self.base = Path(self._tmp.name)
        self.store = str(self.base / 'dispatch.sqlite3')
        self.assertNotEqual(self.store, str(dp.default_store_path()))
        self._n = 0
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

    def ws(self, tid):
        d = self.base / 'ws' / str(tid)
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def seed(self, pk, n=1, state='reserved', task_prefix=None):
        runtime, model = pk.split(':', 1)
        with closing(dp.connect(self.store)) as conn:
            for _ in range(n):
                self._n += 1
                tok = f'cbseed-{self._n}'
                tid = (task_prefix or 'cbtask') + f'-{self._n}'
                conn.execute('INSERT INTO attempts(token, task_id, runtime, model, '
                             'pool_key, state, child_pid, reserved_at_utc, workspace) '
                             'VALUES(?,?,?,?,?,?,?,?,?)',
                             (tok, tid, runtime, model, pk, state, None,
                              dp._iso(NIGHT), str(self.base / (tid))))
            conn.execute('PRAGMA journal_mode=DELETE')

    def reserve(self, tid, runtime, model, executor='auto', now=NIGHT):
        return dp.reserve(self.store, task_id=tid, runtime=runtime, model=model,
                          workspace=self.ws(tid), prompt_sha256=SHA, stage='s',
                          now=now, executor=executor, _preclaim=False)


class TestCodebuddyPoolPriority(_PoolBase):
    def test_codebuddy_has_one_independent_slot(self):
        self.assertEqual(dp.capacity_for(CB), 1)
        self.assertIn(CB, dp.MAIN_FORCE_KEYS)
        # BW-STRONG-SONUS-20261024：物理总 16（原 15 + strong 专属 Sonus 1 槽）；
        # weak 可新派仍为 15。
        self.assertEqual(dp.DOMESTIC_TOTAL_CAPACITY, 16)
        self.assertEqual(sum(dp.capacity_for(k) for k in dm.ALL_POOL_KEYS), 15)

    def test_pool_priority_codebuddy_is_five(self):
        # BW-CLAUDE-ALLDAY-QMAX-AFTER-20261023：codebuddy:deepseek-v4.1-flash priority=5，
        # 紧挨 opencode:deepseek-v4.1-flash-free（6）之前；其上为 claude（3）与国际
        # Qoder Max（4）；ZCode 为层=2。
        self.assertEqual(dp.POOL_PRIORITY[CB], 5)
        self.assertEqual(dp.POOL_PRIORITY[dp.ZCODE_POOL_KEY], 2)
        self.assertEqual(dp.POOL_PRIORITY[CLAUDE], 3)
        self.assertEqual(dp.POOL_PRIORITY[QMAX], 4)
        self.assertEqual(dp.POOL_PRIORITY[dp.OPENCODE_DEEPSEEK_POOL_KEY], 6)
        self.assertLess(dp.POOL_PRIORITY[CLAUDE], dp.POOL_PRIORITY[QMAX])
        self.assertLess(dp.POOL_PRIORITY[QMAX], dp.POOL_PRIORITY[CB])
        self.assertLess(dp.POOL_PRIORITY[CB],
                        dp.POOL_PRIORITY[dp.OPENCODE_DEEPSEEK_POOL_KEY])
        for pk in (CN_MAX, CN_CUSTOM_MAX):
            self.assertEqual(dp.POOL_PRIORITY[pk], 1)

    def test_auto_prefers_max_over_codebuddy_when_all_free(self):
        # BW-CB-REORDER-20261020：空库 AUTO 不再落 CodeBuddy——三 Qoder Max（层=1）在先，
        # CodeBuddy（层=3）绝不在 Max/claude 有空位时被选中。
        out = self.reserve('t1', 'qoder', 'Qwen3.8-Max', 'auto')
        self.assertTrue(out.get('routing_required'), out)
        self.assertIn(out['selected']['pool_key'], MAXES, out)
        self.assertNotEqual(out['selected']['pool_key'], CB, out)

    def test_auto_lands_codebuddy_after_max_zcode_and_claude_full(self):
        # 新 AUTO 选择顺序的完整走位：三 Max（4 槽）、ZCode（1 槽）与 claude（2 槽）全满
        # → 下一层正是 CodeBuddy deepseek（4）；CodeBuddy 也满 → 才轮到两 OpenCode 免费
        # 模型（5）。
        self.reserve('qm', 'qoder', 'Qwen3.8-Max', 'qoder')
        self.reserve('cm', 'qodercn', 'Qwen3.8-Max', 'qodercn')
        self.reserve('cx1', 'qodercn', 'Qwen-3.8-Max', 'qodercn')
        self.reserve('cx2', 'qodercn', 'Qwen-3.8-Max', 'qodercn')
        self.reserve('z1', 'zcode', 'GLM-5.3', 'zcode')
        self.reserve('cl1', 'claude', 'deepseek-flash', 'claude')
        self.reserve('cl2', 'claude', 'deepseek-flash', 'claude')
        out = self.reserve('t3', 'qoder', 'Qwen3.8-Flash', 'auto')
        self.assertTrue(out.get('routing_required'), out)
        self.assertEqual(out['selected']['pool_key'], CB, out)
        self.reserve('cb1', 'codebuddy', CB_MODEL, 'codebuddy')
        out2 = self.reserve('t4', 'qoder', 'Qwen3.8-Flash', 'auto')
        self.assertTrue(out2.get('routing_required'), out2)
        self.assertIn(out2['selected']['pool_key'],
                      (dp.OPENCODE_DEEPSEEK_POOL_KEY, dp.OPENCODE_GLM_POOL_KEY), out2)

    def test_after_codebuddy_occupied_auto_lands_max(self):
        # CodeBuddy 唯一名额被占（本轮真实失败/在用）→ AUTO 仍按新层序从三 Qoder Max
        # （层=1，本就先于 CodeBuddy 层=4）中选择；ZCode（层=2）也在其前。
        self.reserve('occ', 'codebuddy', CB_MODEL, 'codebuddy')
        out = self.reserve('t2', 'qoder', 'Qwen3.8-Flash', 'auto')
        self.assertIn(out['selected']['pool_key'], MAXES)

    def test_round_failed_codebuddy_falls_to_next_qualified(self):
        # 本轮 codebuddy 真实失败 → 候选剔除后，_select_main_force 在剩余合格候选里仍按
        # 层序选择（三 Max 层=1 高于 CodeBuddy 层=3，落于 Max）。
        with closing(dp.connect(self.store)) as conn:
            elig = set(dp._eligible_main_forces(conn, False, NIGHT))
            self.assertIn(CB, elig)  # codebuddy 无时段门，默认可选
            elig.discard(CB)
            target, reason = dp._select_main_force(conn, CB, NIGHT, eligible=elig)
        self.assertEqual(reason, 'main_force_selected')
        self.assertIn(target, MAXES)

    def test_only_one_codebuddy_second_rejected(self):
        first = self.reserve('a', 'codebuddy', CB_MODEL, 'codebuddy')
        self.assertTrue(first.get('allowed'), first)
        second = self.reserve('b', 'codebuddy', CB_MODEL, 'codebuddy')
        self.assertFalse(second.get('allowed'), second)
        self.assertEqual(second['reason'], 'capacity_full')
        self.assertTrue(any('capacity of 1' in r for r in second['reasons']), second)

    def test_explicit_qoder_never_crosses_to_codebuddy(self):
        out = self.reserve('q', 'qoder', 'Qwen3.8-Max', 'qoder')
        self.assertEqual(out['pool_key'], QMAX)
        self.assertFalse(out.get('routing_required'))

    def test_codebuddy_no_time_window_gate_across_dayparts(self):
        # CodeBuddy deepseek 自身无任何时段门（weak 档下各时刻都在候选里）。
        for now in (PEAK, GAP, DAY, NIGHT):
            with closing(dp.connect(self.store)) as conn:
                elig = dp._eligible_main_forces(conn, False, now)
            self.assertIn(CB, elig, f'codebuddy must be eligible at {now}')
            self.assertFalse(dp._max_window_blocks(CB, now))
        # 对照（BW-MANUAL-THREE-MODE-20261023）：weak 档恢复 claude 工作日高峰门与三个 Max
        # 夜间门——PEAK 的 claude、DAY 的三个 Max 都不在候选；strong 档则 CodeBuddy 被档位
        # 排除（mode_excluded），claude 与三个 Max 全天在候选。
        with closing(dp.connect(self.store)) as conn:
            self.assertNotIn(CLAUDE, dp._eligible_main_forces(conn, False, PEAK))
            for pk in (CN_MAX, QMAX, CN_CUSTOM_MAX):
                self.assertNotIn(pk, dp._eligible_main_forces(conn, False, DAY))
        # BW-QODERCN-TOKEN-MAX-DISABLE-20261028：「strong 档三个 Max 全天无门」对照属
        # 禁用前三 Max 历史口径——目标池现行被用户禁用，临时空禁用集复现历史语义。
        with user_disabled_pools(frozenset()),                 dispatch_mode('strong'), closing(dp.connect(self.store)) as conn:
            elig_s = dp._eligible_main_forces(conn, False, DAY)
            self.assertNotIn(CB, elig_s)
            for pk in (CN_MAX, QMAX, CN_CUSTOM_MAX, CLAUDE):
                self.assertIn(pk, elig_s)

    def test_total_guard_blocks_sixteenth(self):
        # 真实填 15：zcode1 + codebuddy1 + glm1 + qmax1 + cnmax1 + cnx2 + claude2
        # + opencode-deepseek1 + opencode-glm1 + qflash2 + cnflash2 = 15
        self.seed(ZCODE, 1, task_prefix='full')
        self.seed(CB, 1, task_prefix='full')
        self.seed('codebuddy:glm-5.3-flash', 1, task_prefix='full')
        self.seed(QMAX, 1, task_prefix='full')
        self.seed(CN_MAX, 1, task_prefix='full')
        self.seed(CN_CUSTOM_MAX, 2, task_prefix='full')
        self.seed(CLAUDE, 2, task_prefix='full')
        self.seed(dp.OPENCODE_DEEPSEEK_POOL_KEY, 1, task_prefix='full')
        self.seed(dp.OPENCODE_GLM_POOL_KEY, 1, task_prefix='full')
        self.seed(QFLASH, 2, task_prefix='full')
        self.seed(CN_FLASH, 2, task_prefix='full')
        # BW-STRONG-SONUS-20261024：weak 不能新派 Sonus；按真实形态 seed 一条 strong
        # 遗留在途，使全局在途达到物理总 16。
        self.seed(dp.SONUS_POOL_KEY, 1, task_prefix='full')
        with closing(dp.connect(self.store)) as conn:
            self.assertEqual(dp._domestic_active(conn), 16)
        out = self.reserve('over', 'codebuddy', CB_MODEL, 'codebuddy')
        self.assertFalse(out.get('allowed'), out)
        self.assertTrue(out.get('domestic_full') or out['reason'] == 'capacity_full', out)

    def test_workspace_conflict_and_unknown_not_bypassed_for_codebuddy(self):
        shared = self.ws('shared')
        r1 = dp.reserve(self.store, task_id='w1', runtime='codebuddy', model=CB_MODEL,
                        workspace=shared, prompt_sha256=SHA, stage='s', now=NIGHT,
                        executor='codebuddy', _preclaim=False)
        self.assertTrue(r1.get('allowed'), r1)
        r2 = dp.reserve(self.store, task_id='w2', runtime='codebuddy', model=CB_MODEL,
                        workspace=shared, prompt_sha256=SHA, stage='s', now=NIGHT,
                        executor='codebuddy', _preclaim=False)
        self.assertFalse(r2.get('allowed'), r2)
        self.assertEqual(r2['reason'], 'workspace_in_flight')

    def test_release_allows_reuse_of_codebuddy_slot(self):
        first = self.reserve('r1', 'codebuddy', CB_MODEL, 'codebuddy')
        self.assertTrue(first.get('allowed'), first)
        # 终态释放本次 reserved 占位（无 child 的合法取消路径）。
        rel = dp.finish(self.store, first['token'], terminal='cancelled', now=NIGHT,
                        wrapper_pid=None)
        self.assertTrue(rel.get('released') or rel.get('settled'), rel)
        again = self.reserve('r2', 'codebuddy', CB_MODEL, 'codebuddy')
        self.assertTrue(again.get('allowed'), again)


class TestControlPlaneRetirement(unittest.TestCase):
    def _plan(self, runtime, model):
        return {'task_id': 'T', 'stage': 's', 'runtime': runtime, 'model': model,
                'workspace': '/tmp/x', 'cwd': '/tmp/x', 'prompt_sha256': SHA,
                'grants': {'edits': [], 'bash': [], 'read_dirs': []},
                'max_concurrency': 1, 'active_tasks': []}

    def _actual(self, runtime, model):
        return {'task_id': 'T', 'stage': 's', 'runtime': runtime, 'model': model,
                'workspace': '/tmp/x', 'cwd': '/tmp/x', 'prompt_sha256': SHA,
                'shell': False, 'argv': ['node', 'cli.js'],
                'grants': {'edits': [], 'bash': [], 'read_dirs': [],
                           'tool_visibility': [], 'allowed_tools': [],
                           'disallowed_tools': [], 'visible_tools': []}}

    def test_reactivated_model_not_retired(self):
        pf = ec.preflight(self._plan('codebuddy', CB_MODEL),
                          self._actual('codebuddy', CB_MODEL))
        self.assertFalse(pf.get('manual_relay_only'), pf)
        self.assertFalse(any('retired' in r for r in pf['reasons']), pf['reasons'])

    def test_old_codebuddy_model_still_retired(self):
        pf = ec.preflight(self._plan('codebuddy', 'glm-4'),
                          self._actual('codebuddy', 'glm-4'))
        self.assertTrue(pf.get('manual_relay_only'), pf)
        self.assertFalse(pf['ok'])
        self.assertTrue(any('retired' in r for r in pf['reasons']), pf['reasons'])

    def test_workbuddy_still_retired(self):
        pf = ec.preflight(self._plan('workbuddy', 'anything'),
                          self._actual('workbuddy', 'anything'))
        self.assertTrue(pf.get('manual_relay_only'), pf)
        self.assertFalse(pf['ok'])


class TestLiveTransport(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='bw-cb-live-')
        self.base = Path(self._tmp.name)
        self.store = str(self.base / 'dispatch.sqlite3')
        self.assertNotEqual(self.store, str(dp.default_store_path()))
        self.work = self.base / 'ws'
        self.work.mkdir(parents=True, exist_ok=True)
        self.stage = 'BW-CB-LIVE'
        self.report = _valid_report(self.stage, str(self.work))
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
        self.cfg = {'node': 'node', 'cli': 'cli.js'}

    def _prompt_file(self):
        pf = self.base / 'prompt.txt'
        pf.write_text('do a bounded read-only probe', encoding='utf-8')
        return pf

    def _run(self, *, stdout=b'', returncode=0, task='t', outname='out',
             raise_on_launch=False, raise_after_start=False, timeout=False,
             record=None, child_prober=None, tools_items=None, model=CB_MODEL,
             timeout_seconds=60, now=None):
        pf = self._prompt_file()
        out = self.base / outname
        factory = stub.make_popen_factory(stdout=stdout, returncode=returncode,
                                          raise_on_launch=raise_on_launch,
                                          raise_after_start=raise_after_start,
                                          timeout=timeout, record=record)
        if child_prober is None:
            child_prober = lambda pid: {'state': 'dead', 'created': None}  # noqa: E731
        return cl.run(self.cfg, workspace=str(self.work), prompt_file=pf, out_dir=out,
                      stage=self.stage, task_id=task, model=model,
                      tools_items=tools_items if tools_items is not None
                      else ['Read', 'Write'],
                      prompt_sha256=cl._sha(pf.read_bytes()),
                      dispatch_store=self.store, popen_factory=factory,
                      child_prober=child_prober, timeout_seconds=timeout_seconds,
                      now=now)

    def _request_times(self, outname='out'):
        req = json.loads((self.base / outname / 'request.json').read_text(encoding='utf-8'))
        return (datetime.fromisoformat(req['started_at_utc']),
                datetime.fromisoformat(req['finished_at_utc']))

    def _assert_request_times(self, out, *, outname='out', expected=None):
        """可复用时间断言：started <= finished；注入时钟时两者都等于注入值；未注入时
        落在调用方给出的真实边界内，且终态释放的 ended_at 与真实 finish 同一时点。"""
        started, finished = self._request_times(outname)
        self.assertLessEqual(started, finished)
        if expected is not None:
            self.assertEqual(started, expected)
            self.assertEqual(finished, expected)
        with closing(dp.connect(self.store)) as conn:
            row = conn.execute('SELECT started_at_utc, ended_at_utc FROM attempts '
                               'WHERE token=?', (out['token'],)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(datetime.fromisoformat(row['ended_at_utc']), finished)
        return started, finished

    def test_model_hard_gate_rejects_other_model_before_side_effects(self):
        out = cl.run(self.cfg, workspace=str(self.work), prompt_file=self._prompt_file(),
                     out_dir=self.base / 'nope', stage=self.stage, task_id='m',
                     model='glm-4', tools_items=[], prompt_sha256=SHA,
                     dispatch_store=self.store)
        self.assertTrue(out.get('manual_relay_only'), out)
        self.assertFalse(out.get('dispatched'))
        self.assertEqual(out['reason'], 'cli_model_rejected')
        self.assertFalse((self.base / 'nope').exists())  # 零副作用：不建目录

    def test_success_read_write_receipt_and_report_binding(self):
        # 合成协议桩：真实形状的 Read/Write tool_use + 对应成功 tool_result（同 ID），
        # 白名单匹配；这不执行任何真实文件 IO，只验证回执统计与传输绑定口径。
        stream = _success_stream(self.report)
        rec = {}
        out = self._run(stdout=stream, returncode=0, task='ok', record=rec,
                        tools_items=['Read', 'Write'])
        self.assertTrue(out['dispatched'], out)
        self.assertTrue(out['passed'], out)
        self.assertTrue(out['protocol_success'], out)
        self.assertTrue(out['report_bound'], out)
        self.assertEqual(out['exit_code'], 0)
        evidence = out['execution_evidence']
        self.assertEqual(evidence['tool_call_count'], 2, evidence)
        self.assertEqual(evidence['tool_result_count'], 2, evidence)
        self.assertEqual(evidence['successful_tool_receipts'], ['tu-read-1', 'tu-write-1'],
                         evidence)
        self.assertEqual(evidence['missing_tool_receipts'], [], evidence)
        self.assertTrue(evidence['execution_evidence_ok'], evidence)
        # business/backend identity never claimed free or verified.
        self.assertFalse(out['business_verified'])
        # shell=False, stdin payload received, cwd isolated, autoupdater disabled.
        self.assertIs(rec['shell'], False, rec)
        self.assertEqual(rec['cwd'], str(self.work), rec)
        self.assertEqual(rec['env_disable_autoupdater'], '1', rec)
        self.assertGreater(rec['stdin_sha256_len'], 0)
        # raw evidence retained on disk.
        outdir = self.base / 'out'
        self.assertEqual((outdir / 'stdout.jsonl').read_bytes(), stream)
        payload = (outdir / 'sent-payload-stdin.bin').read_bytes()
        self.assertTrue(payload)
        text = payload.decode('utf-8')
        self.assertIn(f'runtime=codebuddy；请求模型={CB_MODEL}', text)
        self.assertIn('不覆盖本轮身份', text)
        req = json.loads((outdir / 'request.json').read_text(encoding='utf-8'))
        self.assertEqual(req['executor_identity']['runtime'], 'codebuddy')
        self.assertEqual(req['executor_identity']['request_model'], CB_MODEL)
        task_text = self._prompt_file().read_text(encoding='utf-8')
        self.assertTrue(text.endswith('\n\n' + task_text), text)
        sent_contract = text[:-len('\n\n' + task_text)]
        self.assertEqual(req['contract_sha256'],
                         pc.sha256_hex(sent_contract.encode('utf-8')))
        self.assertEqual((outdir / 'response.md').read_text(encoding='utf-8'),
                         self.report)
        # codebuddy slot released after confirmed exit.
        with closing(dp.connect(self.store)) as conn:
            self.assertEqual(dp._count_active(conn, CB), 0)

    def test_zero_tool_formatted_report_has_no_engineering_execution_evidence(self):
        # 零工具、九节格式合格的“工程报告”仍可合法通过传输（protocol_success/report_bound
        # 含义不变），但没有工具回执，绝不据此声称工程执行已证明或业务已验收。
        stream = _success_stream(self.report, tools=())
        out = self._run(stdout=stream, returncode=0, task='zero', outname='zero',
                        tools_items=[])
        self.assertTrue(out['dispatched'], out)
        self.assertTrue(out['protocol_success'], out)
        self.assertTrue(out['report_bound'], out)
        self.assertTrue(out['passed'], out)
        evidence = out['execution_evidence']
        self.assertEqual(evidence['tool_call_count'], 0, evidence)
        self.assertEqual(evidence['tool_result_count'], 0, evidence)
        self.assertEqual(evidence['successful_tool_receipts'], [], evidence)
        self.assertEqual(evidence['missing_tool_receipts'], [], evidence)
        self.assertFalse(evidence['execution_evidence_ok'], evidence)
        self.assertFalse(out['business_verified'])

    def test_missing_tool_result_receipt_is_reported_not_engineering_evidence(self):
        # 只有 tool_use、没有同 ID tool_result：协议文本仍可合格、报告仍可绑定，但缺失
        # 回执必须如实列出，execution_evidence_ok=False；不得当作工程执行已证明。
        base = _success_stream(self.report).decode('utf-8').splitlines()
        kept = [line for line in base if '"tool_result"' not in line]
        stream = ('\n'.join(kept) + '\n').encode('utf-8')
        out = self._run(stdout=stream, returncode=0, task='miss', outname='miss',
                        tools_items=['Read', 'Write'])
        self.assertTrue(out['protocol_success'], out)
        self.assertTrue(out['report_bound'], out)
        evidence = out['execution_evidence']
        self.assertEqual(evidence['tool_call_count'], 2, evidence)
        self.assertEqual(evidence['tool_result_count'], 0, evidence)
        self.assertEqual(evidence['missing_tool_receipts'], ['tu-read-1', 'tu-write-1'],
                         evidence)
        self.assertFalse(evidence['execution_evidence_ok'], evidence)
        self.assertFalse(out['business_verified'])

    def test_omitted_timeout_adds_no_wall_clock_limit(self):
        stream = _success_stream(self.report)
        rec = {}
        out = self._run(stdout=stream, task='nt', outname='nt', record=rec,
                        tools_items=['Read', 'Write'], timeout_seconds=None)
        self.assertTrue(out['passed'], out)
        self.assertIsNone(rec['communicate_timeout'], rec)

    def test_explicit_positive_timeout_is_honored(self):
        stream = _success_stream(self.report)
        rec = {}
        out = self._run(stdout=stream, task='pt', outname='pt', record=rec,
                        tools_items=['Read', 'Write'], timeout_seconds=60)
        self.assertTrue(out['passed'], out)
        self.assertEqual(rec['communicate_timeout'], 60, rec)

    def test_nonpositive_timeout_refused_before_any_side_effect(self):
        for bad in (0, -5):
            outname = f'tobad-{bad}'
            out = cl.run(self.cfg, workspace=str(self.work), prompt_file=self._prompt_file(),
                         out_dir=self.base / outname, stage=self.stage, task_id='to-bad',
                         model=CB_MODEL, tools_items=[], prompt_sha256=SHA,
                         dispatch_store=self.store, timeout_seconds=bad)
            self.assertTrue(out['dispatch_failed'], out)
            self.assertFalse(out['dispatched'], out)
            self.assertEqual(out['reason'], 'timeout_out_of_bounds', out)
            self.assertFalse((self.base / outname).exists())
        with closing(dp.connect(self.store)) as conn:
            self.assertEqual(dp._count_active(conn, CB), 0)

    def test_request_records_real_start_and_finish(self):
        stream = _success_stream(self.report)
        before = datetime.now(timezone.utc)
        out = self._run(stdout=stream, task='times', outname='times',
                        tools_items=['Read', 'Write'])
        after = datetime.now(timezone.utc)
        started, finished = self._assert_request_times(out, outname='times')
        self.assertGreaterEqual(started, before)
        self.assertLessEqual(finished, after)
        # 未注入时钟：finish 取真实结束点，不能把派工开始值直接当结束值。
        request = json.loads((self.base / 'times' / 'request.json').read_text(
            encoding='utf-8'))
        self.assertEqual(request['started_at_utc'], out['started_at_utc'])
        self.assertEqual(request['finished_at_utc'], out['finished_at_utc'])

    def test_injected_clock_keeps_offline_determinism(self):
        stream = _success_stream(self.report)
        out = self._run(stdout=stream, task='clock', outname='clock',
                        tools_items=['Read', 'Write'], now=NIGHT)
        started, finished = self._assert_request_times(out, outname='clock',
                                                       expected=NIGHT)
        self.assertEqual(started, finished)

    def test_model_drift_rejected(self):
        # init.model 漂移到旧 GLM：协议失败，不伪成功、不绑定。
        stream = _success_stream(self.report, model='glm-4')
        out = self._run(stdout=stream, returncode=0, task='drift', outname='drift')
        self.assertTrue(out['dispatched'])
        self.assertFalse(out['protocol_success'], out)
        self.assertFalse(out['passed'], out)
        with closing(dp.connect(self.store)) as conn:
            self.assertEqual(dp._count_active(conn, CB), 0)  # 仍真实释放

    def test_exit0_with_errors_not_falsely_success(self):
        # CLI exit 0 但成功信封带非空 errors：不得绿灯。
        stream = _success_stream(self.report, errors=['some 429 upstream'])
        out = self._run(stdout=stream, returncode=0, task='err', outname='err')
        self.assertTrue(out['dispatched'])
        self.assertFalse(out['protocol_success'], out)
        self.assertFalse(out['passed'], out)

    def test_start_failure_releases_own_slot(self):
        out = self._run(raise_on_launch=True, task='sf', outname='sf')
        self.assertFalse(out.get('started'), out)
        self.assertTrue(out['capacity_release'].get('released'), out)
        with closing(dp.connect(self.store)) as conn:
            self.assertEqual(dp._count_active(conn, CB), 0)

    def test_exception_after_start_keeps_unknown(self):
        # 绑定成功后 communicate 异常、终态无法确认 → mark_unknown 保留占位，绝不假释放。
        out = self._run(raise_after_start=True, task='unk', outname='unk')
        self.assertTrue(out.get('launch_unknown'), out)
        self.assertTrue(out['started'])
        with closing(dp.connect(self.store)) as conn:
            self.assertEqual(dp._count_active(conn, CB), 1)
            row = conn.execute("SELECT state FROM attempts WHERE token=?",
                               (out['token'],)).fetchone()
            self.assertEqual(row['state'], 'unknown')  # 仅占位，未释放

    def test_timeout_never_healthy(self):
        # 有界墙钟超时：kill + 回收，按失败结算，绝不写 healthy、不伪成功。
        stream = _success_stream(self.report)
        out = self._run(stdout=stream, timeout=True, task='to', outname='to')
        self.assertTrue(out['dispatched'])
        self.assertTrue(out['timed_out'], out)
        self.assertFalse(out['passed'], out)

    def test_does_not_mis_clear_other_placeholder(self):
        # 预置另一 task 的在途占位；本次 codebuddy 传输的成功释放绝不触碰别人的锁。
        self.seed_foreign('qoder', 'Qwen3.8-Max', QMAX, 'foreign-task')
        stream = _success_stream(self.report)
        out = self._run(stdout=stream, returncode=0, task='iso', outname='iso')
        self.assertTrue(out['passed'], out)
        with closing(dp.connect(self.store)) as conn:
            row = conn.execute('SELECT state FROM attempts WHERE task_id=?',
                               ('foreign-task',)).fetchone()
            self.assertEqual(row['state'], 'reserved')
            self.assertEqual(dp._count_active(conn, QMAX), 1)

    def test_real_structured_429_records_codebuddy_limit_and_isolates_glm(self):
        """BW-WIDGET-QUOTA-20261019-S5：正式 CodeBuddy 入口真实结构化 429（result.errors 里
        独立 status=429）在终态原始字节落盘后、finish 释放前按精确 pool_key 落 main_force_limits；
        绑定真实 attempt/证据 sha256；DeepSeek 限额不污染 GLM（独立池）；落标不影响本任务释放。
        BW-WIDGET-QUOTA-20261019-S6：CodeBuddy 展示池不影响路由——_main_force_limit_active
        对 CodeBuddy 恒 False，不阻止下一轮真实尝试。"""
        stream = _success_stream(self.report, errors=[{'status': 429,
                                                       'message': 'rate limited'}])
        out = self._run(stdout=stream, returncode=1, task='q429', outname='q429')
        self.assertFalse(out['protocol_success'], out)
        self.assertTrue(out['main_force_limit']['recorded'], out['main_force_limit'])
        self.assertEqual(out['main_force_limit']['pool_key'], CB)
        # 表行确实写入（widget 能读出）。
        with closing(dp.connect(self.store)) as conn:
            row = conn.execute('SELECT limited FROM main_force_limits WHERE pool_key=?',
                               (CB,)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(int(row['limited']), 1)
        # 路由不受影响：_main_force_limit_active 对 CodeBuddy 恒 False。
        self.assertFalse(dp.main_force_limited(self.store, CB, now=NIGHT))
        # GLM 独立，绝不被 DeepSeek 限额连带。
        self.assertFalse(dp.main_force_limited(self.store, dp.CODEBUDDY_GLM_POOL_KEY,
                                               now=NIGHT))
        # 记录不阻塞释放：本任务 codebuddy 占位已真实释放。
        with closing(dp.connect(self.store)) as conn:
            self.assertEqual(dp._count_active(conn, CB), 0)

    def test_body_429_string_not_marked(self):
        """正文/错误串中间出现 '429' 字样（非开头独立码、非结构化 status/code）不误标。"""
        stream = _success_stream(self.report, errors=['upstream hiccup 429 retry later'])
        out = self._run(stdout=stream, returncode=1, task='body', outname='body')
        self.assertFalse(out['protocol_success'], out)
        self.assertIsNone(out['main_force_limit'], out)
        self.assertFalse(dp.main_force_limited(self.store, CB, now=NIGHT))

    def test_auth_error_not_marked_as_quota(self):
        """认证失败错误载体不串型为限额：结构化 auth 文案不触发 429 落标。"""
        stream = _success_stream(
            self.report,
            errors=[{'message': 'Custom model authentication failed. Please check '
                                'if your API Key is correctly configured and valid.'}])
        out = self._run(stdout=stream, returncode=1, task='auth', outname='auth')
        self.assertFalse(out['protocol_success'], out)
        self.assertIsNone(out['main_force_limit'], out)
        self.assertFalse(dp.main_force_limited(self.store, CB, now=NIGHT))

    def test_success_does_not_mark_limit(self):
        """成功终态绝不落限额标记（不假成功、不误标）。"""
        stream = _success_stream(self.report)
        out = self._run(stdout=stream, returncode=0, task='ok2', outname='ok2')
        self.assertTrue(out['passed'], out)
        self.assertIsNone(out['main_force_limit'], out)
        self.assertFalse(dp.main_force_limited(self.store, CB, now=NIGHT))

    def test_dispatch_store_none_with_env_var_still_records(self):
        """BW-WIDGET-QUOTA-20261019-S6：dispatch_store=None + BRAIN_WORKER_DISPATCH_STORE 环境变量
        指向临时库时，限额标记仍正确落盘（CLI 省略 --dispatch-store 走默认路径场景）。"""
        import tempfile as _tf
        tmp = _tf.NamedTemporaryFile(suffix='.sqlite3', delete=False)
        env_store = tmp.name
        tmp.close()
        os.unlink(env_store)
        self.addCleanup(lambda: os.path.exists(env_store) and os.unlink(env_store))
        stream = _success_stream(self.report, errors=[{'status': 429,
                                                      'message': 'rate limited'}])
        pf = self._prompt_file()
        out_dir = self.base / 'envstore'
        factory = stub.make_popen_factory(stdout=stream, returncode=1)
        with mock.patch.dict(os.environ, {'BRAIN_WORKER_DISPATCH_STORE': env_store}):
            result = cl.run(self.cfg, workspace=str(self.work), prompt_file=pf,
                            out_dir=out_dir, stage=self.stage, task_id='envq',
                            model=CB_MODEL, tools_items=['Read', 'Write'],
                            prompt_sha256=cl._sha(pf.read_bytes()),
                            dispatch_store=None, popen_factory=factory,
                            child_prober=lambda pid: {'state': 'dead', 'created': None},
                            timeout_seconds=60)
        self.assertFalse(result['protocol_success'])
        self.assertIsNotNone(result.get('main_force_limit'))
        self.assertTrue(result['main_force_limit']['recorded'], result['main_force_limit'])
        with closing(dp.connect(env_store)) as conn:
            row = conn.execute('SELECT limited FROM main_force_limits WHERE pool_key=?',
                               (CB,)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(int(row['limited']), 1)

    def seed_foreign(self, runtime, model, pk, task):
        with closing(dp.connect(self.store)) as conn:
            conn.execute('INSERT INTO attempts(token, task_id, runtime, model, pool_key, '
                         'state, child_pid, reserved_at_utc, workspace) '
                         'VALUES(?,?,?,?,?,?,?,?,?)',
                         ('foreign-tok', task, runtime, model, pk, 'reserved', None,
                          dp._iso(NIGHT), str(self.base / 'foreign-ws')))
            conn.execute('PRAGMA journal_mode=DELETE')


class TestFormalMainGate(unittest.TestCase):
    def test_formal_main_rejects_other_model_with_zero_side_effects(self):
        argv = ['codebuddy_direct', '--model', 'glm-4', '--workspace', '/tmp/x',
                '--prompt-file', '/tmp/p.txt', '--output-dir', '/tmp/o', '--stage', 's']
        buf = io.StringIO()
        with mock.patch.object(sys, 'argv', argv), redirect_stdout(buf):
            code = cb.main()
        self.assertEqual(code, 2)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload['manual_relay_only'])
        self.assertFalse(payload['sent'])

    def test_live_cli_rejects_other_model_before_any_io(self):
        argv = ['--model', 'glm-4', '--workspace', '/tmp/x',
                '--prompt-file', '/tmp/p.txt', '--output-dir', '/tmp/o', '--stage', 's',
                '--task-id', 't']
        buf = io.StringIO()
        with mock.patch.object(sys, 'argv', ['codebuddy_live'] + argv):
            with redirect_stdout(buf):
                code = cl.main(argv)
        self.assertEqual(code, 2)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload['manual_relay_only'])
        self.assertEqual(payload['reason'], 'cli_model_rejected')
        self.assertFalse(payload['dispatched'])

    def test_live_cli_rejects_nonpositive_timeout_before_config_read(self):
        argv = ['--workspace', '/tmp/x', '--prompt-file', '/tmp/missing.txt',
                '--output-dir', '/tmp/o', '--stage', 's', '--task-id', 't',
                '--config', '/tmp/no-such-config.json', '--timeout-seconds', '0']
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cl.main(argv)
        self.assertEqual(code, 2)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload['reason'], 'timeout_out_of_bounds')
        self.assertFalse(payload['dispatched'])


if __name__ == '__main__':
    unittest.main()
