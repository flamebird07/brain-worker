"""execution_control 控制面测试：任务级预检、六类失败分型、已登记测试执行器，
并真实调用现有入口（subprocess）验证 --dispatch-plan 拒绝零派工与放行后建目录。

不访问网络、不读凭据、不调用真实模型；入口用现有离线 stub（tests/stub_qodercli.py）
作为运行时载体。测试用 unittest，无 pytest 依赖。"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'scripts'))
import execution_control as ec  # noqa: E402

STUB_QODER = REPO / 'tests' / 'stub_qodercli.py'


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _grants(rules=('Read',), add_dirs=(), disallowed=(), tools=()):
    return ec.grants_from_rules(list(rules), list(add_dirs), list(disallowed),
                                list(tools))


def _plan(**over):
    g = _grants()
    plan = {
        'task_id': 'T01', 'stage': 'S-1', 'runtime': 'qoder',
        'model': 'Qwen3.8-Max', 'workspace': 'C:\\ws\\T01',
        'cwd': 'C:\\ws\\T01', 'prompt_sha256': 'a' * 64,
        'grants': {'edits': g['edits'], 'bash': g['bash'], 'read_dirs': g['read_dirs']},
        'tool_visibility': g['tool_visibility'], 'visible_tools': g['visible_tools'],
        'allowed_tools': g['allowed_tools'], 'disallowed_tools': g['disallowed_tools'],
        'active_tasks': [], 'depends_on': [], 'shared_writes': [],
        'max_concurrency': 1, 'isolation': 'independent_workspace',
    }
    plan.update(over)
    return plan


def _actual(**over):
    actual = {
        'task_id': 'T01', 'stage': 'S-1', 'runtime': 'qoder',
        'model': 'Qwen3.8-Max', 'workspace': 'C:\\ws\\T01',
        'cwd': 'C:\\ws\\T01', 'prompt_sha256': 'a' * 64,
        'argv': ['node', 'cli', '-p'], 'shell': False,
        'grants': _grants(),
    }
    actual.update(over)
    return actual


class PreflightTests(unittest.TestCase):
    def test_matching_plan_ok(self):
        r = ec.preflight(_plan(), _actual())
        self.assertTrue(r['ok'], r['reasons'])
        self.assertFalse(r['sent'])
        self.assertFalse(r['is_atomic_lock'])
        # argv 记录为 argv_sha256 供追溯，但不宣称全 argv 已比对
        self.assertEqual(len(r['argv_sha256']), 64)
        self.assertIn('not diffed', r['note'])

    def test_unplanned_edit_grant_rejected(self):
        g = _grants(rules=('Read', 'Edit(/etc/passwd)'))
        r = ec.preflight(_plan(), _actual(grants=g))
        self.assertFalse(r['ok'])
        self.assertTrue(any('unplanned/expansion' in x for x in r['reasons']),
                        r['reasons'])

    def test_missing_planned_grant_rejected(self):
        pg = _grants(rules=('Read', 'Edit(/scripts/x.py)'))
        r = ec.preflight(
            _plan(grants={'edits': pg['edits'], 'bash': pg['bash'],
                          'read_dirs': pg['read_dirs']},
                  allowed_tools=pg['allowed_tools']), _actual())
        self.assertFalse(r['ok'])
        self.assertTrue(any('missing planned grant' in x for x in r['reasons']),
                        r['reasons'])

    def test_bash_verbatim_must_match(self):
        pg = _grants(rules=('Read', 'Bash(python -V)'))
        plan = _plan(grants={'edits': pg['edits'], 'bash': pg['bash'],
                             'read_dirs': pg['read_dirs']},
                     allowed_tools=pg['allowed_tools'])
        ok = ec.preflight(plan, _actual(grants=pg))
        self.assertTrue(ok['ok'], ok['reasons'])
        drift = _grants(rules=('Read', 'Bash(python -VV)'))
        self.assertFalse(ec.preflight(plan, _actual(grants=drift))['ok'])

    def test_missing_read_visibility_rejected(self):
        # 计划声明 tool_visibility/allowed_tools=['Read']，实际把 Read 拿掉：
        # 双向比对必须报“缺计划可见性”，不能因为只比“新增”而漏判。
        g = _grants(rules=())  # 实际：无任何可见工具
        r = ec.preflight(_plan(), _actual(grants=g))
        self.assertFalse(r['ok'])
        self.assertTrue(any('tool_visibility' in x and 'missing planned' in x
                            for x in r['reasons']), r['reasons'])

    def test_bare_global_write_unplanned_rejected(self):
        # 实际塞进未计划的全局 bare Write（不是 Edit(...) 细规则）：可见性扩大须拒。
        g = _grants(rules=('Read', 'Write'))
        r = ec.preflight(_plan(), _actual(grants=g))
        self.assertFalse(r['ok'])
        self.assertTrue(any('unplanned/expansion' in x for x in r['reasons']),
                        r['reasons'])

    def test_prompt_hash_mismatch_rejected(self):
        r = ec.preflight(_plan(), _actual(prompt_sha256='b' * 64))
        self.assertFalse(r['ok'])
        self.assertTrue(any('prompt_sha256' in x for x in r['reasons']))

    def test_model_workspace_drift_rejected(self):
        r = ec.preflight(_plan(model='GLM'), _actual())
        self.assertFalse(r['ok'])
        self.assertTrue(any('model mismatch' in x for x in r['reasons']))

    def test_shell_true_rejected(self):
        r = ec.preflight(_plan(), _actual(shell=True))
        self.assertFalse(r['ok'])
        self.assertTrue(any('shell=True' in x for x in r['reasons']))

    def test_zcode_fine_grained_capability_refused(self):
        g = _grants(rules=('Bash(python -V)',), tools=('Bash',))
        plan = _plan(runtime='zcode', model='GLM-5.3-Flash',
                     grants={'edits': g['edits'], 'bash': g['bash'],
                             'read_dirs': g['read_dirs']},
                     tool_visibility=g['tool_visibility'],
                     visible_tools=g['visible_tools'],
                     allowed_tools=g['allowed_tools'],
                     disallowed_tools=g['disallowed_tools'])
        actual = _actual(runtime='zcode', model='GLM-5.3-Flash', grants=g)
        r = ec.preflight(plan, actual)
        self.assertFalse(r['ok'])
        self.assertTrue(any('cannot express per-file' in x for x in r['reasons']),
                        r['reasons'])

    def test_codebuddy_read_dirs_refused(self):
        g = _grants(add_dirs=('C:\\ref',))
        plan = _plan(runtime='codebuddy', model='CB',
                     grants={'edits': g['edits'], 'bash': g['bash'],
                             'read_dirs': g['read_dirs']},
                     tool_visibility=g['tool_visibility'],
                     visible_tools=g['visible_tools'],
                     allowed_tools=g['allowed_tools'],
                     disallowed_tools=g['disallowed_tools'])
        actual = _actual(runtime='codebuddy', model='CB', grants=g)
        r = ec.preflight(plan, actual)
        self.assertFalse(r['ok'])
        self.assertTrue(any('no external read-only directory' in x for x in r['reasons']))

    def test_parallel_plan_requires_active_tasks_snapshot(self):
        # 并行（max_concurrency>1）却没声明 active_tasks：缺失不等于“无在途”。
        plan = _plan(max_concurrency=4)
        plan.pop('active_tasks')
        with self.assertRaises(ValueError):
            ec.validate_plan(plan)


class ParallelConflictTests(unittest.TestCase):
    def test_default_concurrency_one_blocks_second(self):
        active = [{'task_id': 'T00', 'state': 'executing', 'workspace': 'C:\\ws\\T00',
                   'prompt_sha256': 'c' * 64, 'writes': True, 'shared_writes': []}]
        r = ec.preflight(_plan(task_id='T01'), _actual(), active_tasks=active)
        self.assertFalse(r['ok'])
        self.assertTrue(any('concurrency limit 1' in x for x in r['reasons']), r['reasons'])

    def test_explicit_concurrency_four_allows(self):
        active = [{'task_id': f'T0{i}', 'state': 'executing',
                   'workspace': f'C:\\ws\\T0{i}', 'prompt_sha256': 'd' * 64,
                   'writes': True, 'shared_writes': []} for i in range(3)]
        plan = _plan(task_id='T03', workspace='C:\\ws\\T03', cwd='C:\\ws\\T03',
                     max_concurrency=4)
        actual = _actual(task_id='T03', workspace='C:\\ws\\T03', cwd='C:\\ws\\T03')
        r = ec.preflight(plan, actual, active_tasks=active, max_concurrency=4)
        self.assertTrue(r['ok'], r['reasons'])

    def test_same_workspace_write_conflict_rejected(self):
        active = [{'task_id': 'T00', 'state': 'executing', 'workspace': 'C:\\ws\\T01',
                   'prompt_sha256': 'e' * 64, 'writes': True, 'shared_writes': []}]
        g = _grants(rules=('Read', 'Edit(/a.py)'))
        plan = _plan(max_concurrency=4,
                     grants={'edits': g['edits'], 'bash': g['bash'],
                             'read_dirs': g['read_dirs']},
                     allowed_tools=g['allowed_tools'])
        actual = _actual(grants=g)
        r = ec.preflight(plan, actual, active_tasks=active, max_concurrency=4)
        self.assertFalse(r['ok'])
        self.assertTrue(any('workspace write conflict' in x for x in r['reasons']))

    def test_same_relative_file_independent_workspaces_allowed(self):
        active = [{'task_id': 'T00', 'state': 'executing', 'workspace': 'C:\\ws\\T00',
                   'prompt_sha256': 'f' * 64, 'writes': True, 'shared_writes': []}]
        g = _grants(rules=('Read', 'Edit(/output.py)'))
        plan = _plan(max_concurrency=4,
                     grants={'edits': g['edits'], 'bash': g['bash'],
                             'read_dirs': g['read_dirs']},
                     allowed_tools=g['allowed_tools'])
        actual = _actual(grants=g)
        r = ec.preflight(plan, actual, active_tasks=active, max_concurrency=4)
        self.assertTrue(r['ok'], r['reasons'])

    def test_blocked_and_awaiting_acceptance_keep_write_reservations(self):
        for state in ('blocked', 'awaiting_acceptance'):
            with self.subTest(state=state):
                active = [{'task_id': 'other', 'state': state, 'workspace': 'C:\\ws\\T01',
                           'writes': True, 'shared_writes': []}]
                result = ec.preflight(_plan(max_concurrency=4), _actual(), active_tasks=active)
                self.assertFalse(result['ok'])
                self.assertTrue(any('workspace write conflict' in x for x in result['reasons']))

    def test_awaiting_acceptance_frees_live_slot_but_protects_origin(self):
        # 容量与业务占位分离：子进程已退出、仅待业务验收（awaiting_acceptance）的任务
        # 不占用活进程并发槽——默认 max_concurrency=1 下仍允许为**另一个工作区**派新执行器。
        active = [{'task_id': 'T00', 'state': 'awaiting_acceptance',
                   'workspace': 'C:\\ws\\T00', 'prompt_sha256': 'c' * 64,
                   'writes': True, 'shared_writes': []}]
        other_ws = ec.preflight(_plan(task_id='T01', workspace='C:\\ws\\T01',
                                      cwd='C:\\ws\\T01'),
                                _actual(task_id='T01', workspace='C:\\ws\\T01',
                                        cwd='C:\\ws\\T01'),
                                active_tasks=active)
        self.assertTrue(other_ws['ok'], other_ws['reasons'])
        self.assertFalse(any('concurrency limit' in x for x in other_ws['reasons']))
        # 但原工作区仍受保护：对同一工作区的写入照拒（HELD_STATES 独立维持）。
        same_ws = ec.preflight(_plan(task_id='T02', workspace='C:\\ws\\T00',
                                     cwd='C:\\ws\\T00'),
                               _actual(task_id='T02', workspace='C:\\ws\\T00',
                                       cwd='C:\\ws\\T00'),
                               active_tasks=active)
        self.assertFalse(same_ws['ok'])
        self.assertTrue(any('workspace write conflict' in x for x in same_ws['reasons']))
        # 原任务 id 仍不可重发（同 prompt 也被在途门禁锁住）。
        resubmit = ec.preflight(_plan(task_id='T00', workspace='C:\\ws\\T00',
                                      cwd='C:\\ws\\T00', prompt_sha256='c' * 64),
                                _actual(task_id='T00', workspace='C:\\ws\\T00',
                                        cwd='C:\\ws\\T00', prompt_sha256='c' * 64),
                                active_tasks=active)
        self.assertFalse(resubmit['ok'])
        self.assertTrue(any('already in flight' in x for x in resubmit['reasons']))

    def test_executing_still_counts_against_live_concurrency(self):
        # 对照：真正持有活子进程的 executing 任务仍占并发槽，默认上限 1 时第二个被拒。
        active = [{'task_id': 'T00', 'state': 'executing', 'workspace': 'C:\\ws\\T00',
                   'prompt_sha256': 'c' * 64, 'writes': True, 'shared_writes': []}]
        r = ec.preflight(_plan(task_id='T01', workspace='C:\\ws\\T01', cwd='C:\\ws\\T01'),
                         _actual(task_id='T01', workspace='C:\\ws\\T01', cwd='C:\\ws\\T01'),
                         active_tasks=active)
        self.assertFalse(r['ok'])
        self.assertTrue(any('concurrency limit 1' in x for x in r['reasons']), r['reasons'])

    def test_six_luna_executing_do_not_block_domestic_gate(self):
        # S5 复现缺陷：domestic=0、6 个 Luna executing、max_concurrency=6，旧全局快照
        # 把 Luna 当成统一容量闸误报“concurrency limit 6 exceeded”。修复后 Luna 不占
        # 国内六名额且无上限：一个新的国内 qoder 派工必须能进入真实国内闸。
        active = [{'task_id': f'luna-{i}', 'state': 'executing', 'runtime': 'luna',
                   'workspace': f'C:\\ws\\luna-{i}', 'prompt_sha256': 'b' * 64,
                   'writes': True, 'shared_writes': []} for i in range(6)]
        plan = _plan(task_id='T01', workspace='C:\\ws\\T01', cwd='C:\\ws\\T01',
                     max_concurrency=6)
        actual = _actual(task_id='T01', workspace='C:\\ws\\T01', cwd='C:\\ws\\T01')
        r = ec.preflight(plan, actual, active_tasks=active, max_concurrency=6)
        self.assertFalse(any('concurrency limit' in x for x in r['reasons']), r['reasons'])
        self.assertTrue(r['ok'], r['reasons'])

    def test_domestic_snapshot_cap_still_rejects_when_six_domestic_live(self):
        # 分离 Luna 后，真实国内活进程槽仍被严格强制：6 个国内 executing + 第 7 个国内
        # 派工（max_concurrency=6）→ 快照二级保护仍报 concurrency limit（绝不因排除 Luna
        # 而放松国内上限；原子池容量另由 dispatch_pool 权威强制）。
        active = []
        for i in range(6):
            rt = 'zcode' if i % 2 == 0 else 'qoder'
            active.append({'task_id': f'D0{i}', 'state': 'executing', 'runtime': rt,
                           'workspace': f'C:\\ws\\D0{i}', 'prompt_sha256': 'd' * 64,
                           'writes': True, 'shared_writes': []})
        plan = _plan(task_id='D99', workspace='C:\\ws\\D99', cwd='C:\\ws\\D99',
                     max_concurrency=6)
        actual = _actual(task_id='D99', workspace='C:\\ws\\D99', cwd='C:\\ws\\D99')
        r = ec.preflight(plan, actual, active_tasks=active, max_concurrency=6)
        self.assertFalse(r['ok'])
        self.assertTrue(any('concurrency limit 6' in x for x in r['reasons']), r['reasons'])

    def test_luna_inflight_does_not_count_toward_default_cap(self):
        # Luna 是宿主原生救援通道，不是 dispatch_plan 运行时刻，永不进 preflight 计划校验；
        # 但在途 Luna 活进程不占国内并发槽：默认上限 1、已有 1 个 Luna executing 时，
        # 一个新的国内 qoder 派工仍放行（不因 Luna 触发 concurrency limit）。
        active = [{'task_id': 'LUNA-0', 'state': 'executing', 'runtime': 'luna',
                   'workspace': 'C:\\ws\\LUNA-0', 'prompt_sha256': 'e' * 64,
                   'writes': True, 'shared_writes': []}]
        r = ec.preflight(_plan(task_id='T01', workspace='C:\\ws\\T01', cwd='C:\\ws\\T01'),
                         _actual(task_id='T01', workspace='C:\\ws\\T01', cwd='C:\\ws\\T01'),
                         active_tasks=active)
        self.assertFalse(any('concurrency limit' in x for x in r['reasons']), r['reasons'])
        self.assertTrue(r['ok'], r['reasons'])

    def test_retired_direct_runtime_excluded_from_domestic_concurrency(self):
        # 已退休的 CodeBuddy/WorkBuddy 直连不计入国内活进程闸（默认上限 1 下仍放行新国内派工）。
        active = [{'task_id': 'CB0', 'state': 'executing', 'runtime': 'codebuddy',
                   'workspace': 'C:\\ws\\CB0', 'prompt_sha256': 'c' * 64,
                   'writes': True, 'shared_writes': []}]
        r = ec.preflight(_plan(task_id='T01', workspace='C:\\ws\\T01', cwd='C:\\ws\\T01'),
                         _actual(task_id='T01', workspace='C:\\ws\\T01', cwd='C:\\ws\\T01'),
                         active_tasks=active)
        self.assertFalse(any('concurrency limit' in x for x in r['reasons']), r['reasons'])
        self.assertTrue(r['ok'], r['reasons'])

    def test_shared_external_write_rejected(self):
        active = [{'task_id': 'T00', 'state': 'executing', 'workspace': 'C:\\ws\\T00',
                   'prompt_sha256': 'g' * 64, 'writes': False,
                   'shared_writes': ['feishu:base/x']}]
        plan = _plan(task_id='T01', shared_writes=['feishu:base/x'], max_concurrency=4)
        r = ec.preflight(plan, _actual(task_id='T01'), active_tasks=active,
                         max_concurrency=4)
        self.assertFalse(r['ok'])
        self.assertTrue(any('shared external object' in x for x in r['reasons']))

    def test_same_task_id_inflight_rejects_prompt_change_and_resubmit(self):
        active = [{'task_id': 'T01', 'state': 'awaiting_acceptance',
                   'workspace': 'C:\\ws\\T01',
                   'prompt_sha256': 'a' * 64, 'writes': False, 'shared_writes': []}]
        # 改 prompt：看板记录的 hash 与实际下发 hash 双向核验不符 → 拒。
        changed = ec.preflight(_plan(), _actual(prompt_sha256='h' * 64),
                               active_tasks=active)
        self.assertFalse(changed['ok'])
        self.assertTrue(any('different board prompt' in x
                            for x in changed['reasons']), changed['reasons'])
        # 相同 prompt 重发同样被 awaiting_acceptance 门禁锁住（不可绕过）。
        resubmit = ec.preflight(_plan(), _actual(), active_tasks=active)
        self.assertFalse(resubmit['ok'])
        self.assertTrue(any('already in flight' in x for x in resubmit['reasons']),
                        resubmit['reasons'])

    def test_dependency_requires_completed_and_passed(self):
        # 五状态无 verified；executing 依赖直接拒。
        plan = _plan(depends_on=['T00'])
        r = ec.preflight(plan, _actual(), active_tasks=[
            {'task_id': 'T00', 'state': 'executing', 'workspace': 'C:\\ws\\T00',
             'prompt_sha256': 'a' * 64, 'writes': False, 'shared_writes': []}])
        self.assertFalse(r['ok'])
        self.assertTrue(any('not completed' in x for x in r['reasons']), r['reasons'])
        # completed 但验收结果非 passed（如 failed）→ 仍不放行。
        r2 = ec.preflight(plan, _actual(), active_tasks=[
            {'task_id': 'T00', 'state': 'completed', 'acceptance_result': 'failed',
             'workspace': 'C:\\ws\\T00', 'prompt_sha256': 'a' * 64, 'writes': False,
             'shared_writes': []}])
        self.assertFalse(r2['ok'])
        self.assertTrue(any('acceptance_result' in x for x in r2['reasons']),
                        r2['reasons'])
        # completed 且 acceptance_result=passed → 依赖满足。
        r3 = ec.preflight(plan, _actual(), active_tasks=[
            {'task_id': 'T00', 'state': 'completed', 'acceptance_result': 'passed',
             'workspace': 'C:\\ws\\T00', 'prompt_sha256': 'a' * 64, 'writes': False,
             'shared_writes': []}])
        self.assertTrue(r3['ok'], r3['reasons'])


class DiagnoseTests(unittest.TestCase):
    def test_quota_needs_explicit_429(self):
        self.assertTrue(ec.explicit_429([], [{'status': 429, 'category': 'quota'}]))
        # 明确位于错误文本开头的独立 429 / HTTP 429（含带 reset 提示）算数。
        self.assertTrue(ec.explicit_429(['429 too many'], None))
        self.assertTrue(ec.explicit_429(
            ['HTTP 429 上游限流，将在 2026-10-06 14:00 UTC+8 重置'], None))
        self.assertTrue(ec.explicit_429([{'code': 429}], None))
        # 仅 quota 类别无 429 → 不算；正文/id 中间的 429 数字不算
        self.assertFalse(ec.explicit_429(['quota exceeded'], [{'category': 'quota'}]))
        self.assertFalse(ec.explicit_429(
            ['processed 429 records'], [{'category': 'other'}]))

    def test_reset_hint_extracted_verbatim(self):
        hint = ec.extract_reset_hint(
            ['429 上游限流，将在 2026-10-06 14:00 UTC+8 重置'], None)
        self.assertIsNotNone(hint)
        self.assertIn('重置', hint)
        self.assertIsNone(ec.extract_reset_hint(['no window here'], None))

    def test_diagnose_maps_types_and_coexist(self):
        d = ec.diagnose({'quota_429': True, 'model_execution_failure': True})
        self.assertIn('quota_429', d['failure_types'])
        self.assertIn('model_execution_failure', d['failure_types'])
        self.assertEqual(d['source_attribution'][:9], 'platform/')

    def test_diagnose_not_from_report_text(self):
        # 成功报告正文含 429 说明不应被分类：调用方只传结构布尔，此处结构无 429
        d = ec.diagnose({'quota_429': ec.explicit_429([], None)})
        self.assertEqual(d['failure_types'], [])

    def test_permission_client_missing_from_real_events(self):
        events = [
            {'type': 'permission_resolved',
             'payload': {'decision': 'deny',
                         'reason': 'No permission client configured for Bash'}},
            {'type': 'tool_call_result',
             'payload': {'isError': True,
                         'error': 'No permission client configured for Bash'}},
            {'type': 'turn_started', 'turnId': 'x'},
        ]
        client = ec.permission_client_missing_from_events(events)
        self.assertEqual(len(client), 2)
        # 规则拒绝（原因不是缺客户端）单独归类：这批事件里没有。
        rule = ec.permission_rule_denied_from_events(events)
        self.assertEqual(rule, [])
        d = ec.diagnose({'permission_client_missing': bool(client)})
        self.assertEqual(d['failure_types'], ['permission_client_missing'])

    def test_permission_rule_denied_separate_from_client_missing(self):
        events = [
            {'type': 'permission_resolved',
             'payload': {'decision': 'deny', 'reason': 'Rule denies Bash execution'}},
        ]
        rule = ec.permission_rule_denied_from_events(events)
        client = ec.permission_client_missing_from_events(events)
        self.assertEqual(len(rule), 1)
        self.assertEqual(client, [])
        d = ec.diagnose({'permission_rule_denied': bool(rule)})
        self.assertEqual(d['failure_types'], ['permission_rule_denied'])

    def test_model_request_text_never_scanned(self):
        # 正常 model_request 携带历史/prompt/report，即便含 429/缺 client 也不误报。
        events = [
            {'type': 'model_request', 'payload': {'messages': [
                {'role': 'user', 'content': '上次报 429，且 No permission client '
                                            'configured for Bash'}]}},
        ]
        self.assertEqual(ec.permission_client_missing_from_events(events), [])
        self.assertEqual(ec.permission_rule_denied_from_events(events), [])

    def test_permission_client_missing_from_history_fixture(self):
        # Portable sanitized historical event shape; full raw replay is independent evidence.
        events = [{'id': f'sanitized-event-{i}', 'sessionId': 'sanitized-session',
                   'turnId': 'sanitized-turn', 'type': 'permission_resolved',
                   'payload': {'requestId': f'sanitized-request-{i}', 'toolCallId': f'sanitized-tool-{i}',
                               'decision': 'deny', 'reason': 'No permission client configured for Bash'}}
                  for i in range(3)]
        client = ec.permission_client_missing_from_events(events)
        self.assertEqual(len(client), 3)
        self.assertEqual(ec.permission_rule_denied_from_events(events), [])

    def test_test_failure_only_from_exit_code(self):
        self.assertEqual(ec.diagnose({'test_exit_code': 1})['failure_types'],
                         ['test_failure'])
        self.assertEqual(ec.diagnose({'test_exit_code': 0})['failure_types'], [])
        self.assertEqual(ec.diagnose({})['failure_types'], [])


class RunTestExecutorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self.ev_root = self.ws / 'evidence'
        (self.ws / 'handoff').mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _registry(self, argv, inputs, name='r1', env=None):
        reg = self.ws / 'registry.json'
        reg.write_text(json.dumps({'runs': {name: {
            'argv': argv, 'cwd': str(self.ws), 'inputs': inputs,
            'env': env if env is not None else {'PYTHONIOENCODING': 'utf-8'}}}}),
            encoding='utf-8', newline='\n')
        return str(reg)

    def test_success_then_reuse(self):
        fixture = self.ws / 'fixture.txt'
        fixture.write_text('v1', encoding='utf-8')
        script = self.ws / 'job.py'
        script.write_text('import pathlib,sys;'
                          'pathlib.Path("ok_marker").write_text("x");'
                          'sys.stdout.write("done")', encoding='utf-8')
        argv = [sys.executable, str(script)]
        reg = self._registry(argv, [str(fixture)])
        first = ec.run_registered_test(reg, 'r1', str(self.ws), str(self.ev_root))
        self.assertFalse(first['reused'])
        self.assertEqual(first['exit_code'], 0)
        self.assertEqual(first['failure_types'], [])
        second = ec.run_registered_test(reg, 'r1', str(self.ws), str(self.ev_root))
        self.assertTrue(second['reused'])
        self.assertEqual(second['fingerprint'], first['fingerprint'])

    def test_fixture_change_forces_retest(self):
        fixture = self.ws / 'fixture.txt'
        fixture.write_text('v1', encoding='utf-8')
        script = self.ws / 'job.py'
        script.write_text('sys_out=1', encoding='utf-8')
        reg = self._registry([sys.executable, str(script)], [str(fixture)])
        first = ec.run_registered_test(reg, 'r1', str(self.ws), str(self.ev_root))
        fixture.write_text('v2', encoding='utf-8')
        second = ec.run_registered_test(reg, 'r1', str(self.ws), str(self.ev_root))
        self.assertFalse(second['reused'])
        self.assertNotEqual(first['fingerprint'], second['fingerprint'])

    def test_corrupt_log_forces_retest_no_overwrite(self):
        fixture = self.ws / 'fixture.txt'
        fixture.write_text('v1', encoding='utf-8')
        script = self.ws / 'job.py'
        script.write_text('import sys; sys.stdout.write("hi")', encoding='utf-8')
        reg = self._registry([sys.executable, str(script)], [str(fixture)])
        first = ec.run_registered_test(reg, 'r1', str(self.ws), str(self.ev_root))
        fp_dir = self.ev_root / 'r1' / first['fingerprint']
        stdout_logs = list(fp_dir.glob('*/stdout.log'))
        self.assertTrue(stdout_logs)
        stdout_logs[0].write_bytes(b'CORRUPTED')
        second = ec.run_registered_test(reg, 'r1', str(self.ws), str(self.ev_root))
        self.assertFalse(second['reused'])
        # 旧证据目录不被覆盖：新增一个 run 子目录
        self.assertGreaterEqual(len(list(fp_dir.glob('*/evidence.json'))), 2)

    def test_real_exit_one_is_test_failure(self):
        fixture = self.ws / 'fixture.txt'
        fixture.write_text('v1', encoding='utf-8')
        script = self.ws / 'job.py'
        script.write_text('import sys; sys.exit(1)', encoding='utf-8')
        reg = self._registry([sys.executable, str(script)], [str(fixture)])
        out = ec.run_registered_test(reg, 'r1', str(self.ws), str(self.ev_root))
        self.assertEqual(out['exit_code'], 1)
        self.assertEqual(out['failure_types'], ['test_failure'])

    def test_input_escaping_workspace_refused(self):
        outside = Path(self._tmp.name).parent / 'outside-secret.txt'
        script = self.ws / 'job.py'
        script.write_text('pass', encoding='utf-8')
        reg = self._registry([sys.executable, str(script)], [str(outside)])
        with self.assertRaises(ValueError):
            ec.run_registered_test(reg, 'r1', str(self.ws), str(self.ev_root))

    def test_unregistered_name_refused(self):
        reg = self.ws / 'reg.json'
        reg.write_text(json.dumps({'runs': {}}), encoding='utf-8')
        with self.assertRaises(ValueError):
            ec.run_registered_test(str(reg), 'nope', str(self.ws), str(self.ev_root))

    def test_env_change_forces_retest(self):
        fixture = self.ws / 'fixture.txt'
        fixture.write_text('v1', encoding='utf-8')
        script = self.ws / 'job.py'
        script.write_text('import sys; sys.stdout.write("hi")', encoding='utf-8')
        self._registry([sys.executable, str(script)], [str(fixture)],
                       env={'PYTHONIOENCODING': 'utf-8'})
        ec.run_registered_test(str(self.ws / 'registry.json'), 'r1', str(self.ws),
                               str(self.ev_root))
        self._registry([sys.executable, str(script)], [str(fixture)],
                       env={'PYTHONIOENCODING': 'gbk'})
        second = ec.run_registered_test(str(self.ws / 'registry.json'), 'r1',
                                        str(self.ws), str(self.ev_root))
        self.assertFalse(second['reused'])

    def test_missing_stderr_forces_fresh_attempt(self):
        fixture = self.ws / 'fixture.txt'
        fixture.write_text('v1', encoding='utf-8')
        script = self.ws / 'job.py'
        script.write_text('import sys; sys.stdout.write("hi")', encoding='utf-8')
        reg = self._registry([sys.executable, str(script)], [str(fixture)])
        first = ec.run_registered_test(reg, 'r1', str(self.ws), str(self.ev_root))
        fp_dir = self.ev_root / 'r1' / first['fingerprint']
        run0 = sorted(fp_dir.glob('run-*'))[0]
        (run0 / 'stderr.log').unlink()  # 丢一份日志：不可 reuse，新起 attempt
        second = ec.run_registered_test(reg, 'r1', str(self.ws), str(self.ev_root))
        self.assertFalse(second['reused'])
        self.assertEqual(len(list(fp_dir.glob('run-*'))), 2)

    def test_empty_inputs_refused(self):
        script = self.ws / 'job.py'
        script.write_text('pass', encoding='utf-8')
        reg = self._registry([sys.executable, str(script)], [])
        with self.assertRaises(ValueError):
            ec.run_registered_test(reg, 'r1', str(self.ws), str(self.ev_root))

    def test_inherited_env_change_forces_retest(self):
        from unittest.mock import patch
        script = self.ws / 'job.py'
        script.write_text('print("ok")', encoding='utf-8')
        reg = self._registry([sys.executable, str(script)], [str(script)])
        with patch.dict(os.environ, {'BW_TEST_ENV': 'first'}):
            first = ec.run_registered_test(reg, 'r1', str(self.ws), str(self.ev_root))
        with patch.dict(os.environ, {'BW_TEST_ENV': 'second'}):
            second = ec.run_registered_test(reg, 'r1', str(self.ws), str(self.ev_root))
        self.assertFalse(second['reused'])
        self.assertNotEqual(first['fingerprint'], second['fingerprint'])

    def test_changed_input_during_run_never_reused(self):
        fixture = self.ws / 'fixture.txt'
        fixture.write_text('before', encoding='utf-8')
        script = self.ws / 'job.py'
        script.write_text('from pathlib import Path; Path("fixture.txt").write_text("after")', encoding='utf-8')
        reg = self._registry([sys.executable, str(script)], [str(script), str(fixture)])
        first = ec.run_registered_test(reg, 'r1', str(self.ws), str(self.ev_root))
        self.assertFalse(json.loads(Path(first['evidence']).read_text(encoding='utf-8'))['inputs_unchanged'])
        fixture.write_text('before', encoding='utf-8')
        second = ec.run_registered_test(reg, 'r1', str(self.ws), str(self.ev_root))
        self.assertFalse(second['reused'])

    def test_test_name_cannot_escape_evidence_root(self):
        script = self.ws / 'job.py'
        script.write_text('print("ok")', encoding='utf-8')
        reg = self._registry([sys.executable, str(script)], [str(script)], name='../outside')
        with self.assertRaises(ValueError):
            ec.run_registered_test(reg, '../outside', str(self.ws), str(self.ev_root))

    def test_evidence_root_outside_workspace_refused(self):
        fixture = self.ws / 'fixture.txt'
        fixture.write_text('v1', encoding='utf-8')
        script = self.ws / 'job.py'
        script.write_text('pass', encoding='utf-8')
        reg = self._registry([sys.executable, str(script)], [str(fixture)])
        outside = Path(self._tmp.name).parent / 'outside-evidence'
        with self.assertRaises(ValueError):
            ec.run_registered_test(reg, 'r1', str(self.ws), str(outside))


class AdapterDispatchPlanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name).resolve()
        self.entry = self.ws / 'entry.json'
        self.entry.write_text(json.dumps(
            {'node': sys.executable, 'qodercli': str(STUB_QODER)}), encoding='utf-8')
        self.stage = 'BW-PLAN-TEST-01'
        # BW-MAX-WINDOW-20261010-S2：计划默认用内置 Qwen3.8-Max（主力时段门约束），真实入口
        # 在子进程用墙钟判定 → 白天会如实拒绝。本组只验 dispatch plan 预检/Popen 链路，故用
        # 测试创建的临时 wrapper 把 dispatch_pool.utcnow 固定到主力时段内（北京 23:00）再原样
        # 运行 qoder_direct 入口；时钟控制仅在此离线夹具，生产入口不加 --now/环境开关。
        self.runner = self.ws / 'qd_in_window_runner.py'
        self.runner.write_text(
            'import sys\n'
            f'sys.path.insert(0, {str(REPO / "scripts")!r})\n'
            'from datetime import datetime, timezone\n'
            'import dispatch_pool as dp\n'
            'dp.utcnow = lambda: datetime(2026, 10, 8, 15, 0, 0, tzinfo=timezone.utc)\n'
            'import qoder_direct\n'
            'sys.exit(qoder_direct.main())\n',
            encoding='utf-8')

    def tearDown(self):
        self._tmp.cleanup()

    def _write_prompt(self):
        prompt = self.ws / 'prompt.txt'
        # newline='\n'：写入 LF 原字节，使 read_bytes 与入口 read_text→encode 的哈希一致，
        # 避免 Windows 默认 CRLF 翻译令 prompt_sha256 与计划绑定不符而误拒。
        prompt.write_text('离线测试任务，请原样汇报。\n', encoding='utf-8', newline='\n')
        return prompt

    def _plan_for(self, prompt, allowed=('Read',)):
        p = self.ws / 'plan.json'
        # 入口未传 --tools（默认空）→ visible_tools 空；--allowed-tools 逐项进 allowed_tools。
        g = _grants(rules=allowed, tools=())
        plan = {
            'task_id': 'TP', 'stage': self.stage, 'runtime': 'qoder',
            'model': 'Qwen3.8-Max', 'workspace': str(self.ws), 'cwd': str(self.ws),
            'prompt_sha256': _sha(prompt.read_bytes()),
            'grants': {'edits': g['edits'], 'bash': g['bash'],
                       'read_dirs': g['read_dirs']},
            'tool_visibility': g['tool_visibility'], 'visible_tools': g['visible_tools'],
            'allowed_tools': g['allowed_tools'], 'disallowed_tools': g['disallowed_tools'],
            'active_tasks': [], 'depends_on': [], 'shared_writes': [],
            'max_concurrency': 1, 'isolation': 'independent_workspace',
        }
        p.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
        return p

    def _run(self, plan_path, out_dir, allowed):
        env = os.environ.copy()
        env['STUB_MODE'] = 'not_json'  # 让协议失败，但派工已发生（证明预检放行）
        # 跨会话并发容量池隔离：每个测试方法用 self.ws 下的临时 store，绝不读写真实
        # ~/.brain-worker 池，也不继承全局 BRAIN_WORKER_DISPATCH_STORE 的在途名额。
        env['BRAIN_WORKER_DISPATCH_STORE'] = str(self.ws / 'dispatch-pool.sqlite3')
        cmd = [sys.executable, str(self.runner),
               '--workspace', str(self.ws), '--prompt-file', str(self._prompt),
               '--output-dir', str(out_dir), '--stage', self.stage,
               '--config', str(self.entry), '--dispatch-plan', str(plan_path)]
        for rule in allowed:
            cmd += ['--allowed-tools', rule]
        return subprocess.run(cmd, capture_output=True, env=env, timeout=120,
                              cwd=str(self.ws))

    def test_rejected_plan_zero_spawn(self):
        self._prompt = self._write_prompt()
        # 计划要求一个入口未实际授予的 Edit → 缺 grant → 退出 2、零目录
        plan = self._plan_for(self._prompt, allowed=('Read', 'Edit(/never/granted.py)'))
        out = self.ws / 'out-reject'
        proc = self._run(plan, out, allowed=['Read'])
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(out.exists())
        self.assertIn('dispatch_plan_rejected', proc.stdout.decode('utf-8', 'replace'))

    def test_matching_plan_dispatches_then_popen(self):
        self._prompt = self._write_prompt()
        plan = self._plan_for(self._prompt, allowed=('Read',))
        out = self.ws / 'out-ok'
        proc = self._run(plan, out, allowed=['Read'])
        # 计划放行 → 建目录并 Popen（stub not_json 令 stdout 不可解析 → 退出 3）
        self.assertTrue(out.is_dir())
        request = json.loads((out / 'request.json').read_text(encoding='utf-8'))
        self.assertTrue(request['dispatch_plan']['ok'])
        self.assertEqual(len(request['dispatch_plan']['plan_hash']), 64)
        self.assertEqual(len(request['dispatch_plan']['argv_sha256']), 64)
        summary = json.loads((out / 'summary.json').read_text(encoding='utf-8'))
        self.assertIn('diagnostics', summary)
        # 纠正错误断言：stub 输出 not_json 是信封/流结构解析失败，不能改实现说成 model。
        self.assertIn('protocol_parse_failure', summary['diagnostics']['failure_types'])
        self.assertNotIn('model_execution_failure', summary['diagnostics']['failure_types'])


if __name__ == '__main__':
    unittest.main()
