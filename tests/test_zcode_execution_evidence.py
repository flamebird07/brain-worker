#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BW-TRUTH-ROUTING-20261007-S1(-REPAIR2) 执行证据门禁离线测试：不登录、不联网、不调模型。

覆盖 REPAIR1 全部分支，另加 REPAIR2 对抗反例：result.success='false' 字符串、
测试证据 name/旧 inputs 哈希/fingerprint 伪造、evidence.json='[]' 不崩溃、
required_test_command 与 argv 口径矛盾、工程只读未声明 allow_no_changes、
布尔/数值伪路径、scheduled payload malformed、新产物派工前已存在等。

S2 追加 tool_call_error 明确失败终态归并兼容：普通 error/仅 committed/error+committed/
明确权限 error/跨 session-turn/孤立/畸形/一致重放/success 与 error 冲突，并保留合法
success、伪布尔拒绝与写入证据验收。
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import execution_control as ec  # noqa: E402
import zcode_execution_evidence as zee  # noqa: E402


def has(reasons, needle):
    return any(needle in r for r in reasons)


def call(call_id, tool, file_path, success=True, content='ok', kind='result',
         session='s1', turn='t1', raw_result=None, raw_payload=None):
    if kind == 'scheduled':
        payload = {'toolCallId': call_id, 'toolName': tool,
                   'input': {'file_path': file_path}}
    else:
        payload = raw_payload if raw_payload is not None else {
            'toolCallId': call_id,
            'result': raw_result if raw_result is not None
            else {'success': success, 'content': content}}
    return {'sessionId': session, 'turnId': turn, 'type':
            'tool_call_scheduled' if kind == 'scheduled' else 'tool_call_result',
            'payload': payload}


def err_event(call_id, message='tool execution failed', error=None, session='s1',
              turn='t1', raw_payload=None):
    """tool_call_error 明确失败终态：payload.error 为对象（无 result.success）。"""
    if raw_payload is not None:
        payload = raw_payload
    elif error is not None:
        payload = {'toolCallId': call_id, 'error': error}
    else:
        payload = {'toolCallId': call_id, 'error': {
            'code': 'TOOL_EXEC_FAILED', 'type': 'ToolError', 'message': message,
            'detail': '', 'stack': ''}}
    return {'id': f'{call_id}-e', 'sessionId': session, 'turnId': turn,
            'type': 'tool_call_error', 'traceId': 'tr1', 'timestamp': 1,
            'sequenceNumber': 2, 'payload': payload}


def ledger_committed(call_id, session='s1', turn='t1'):
    """streaming_tool_ledger_updated 的 payload.status=tool_result_committed：
    仅是状态字段，不是事件 type，也不代表成功。"""
    return {'sessionId': session, 'turnId': turn,
            'type': 'streaming_tool_ledger_updated',
            'payload': {'toolCallId': call_id, 'status': 'tool_result_committed'}}


def sched(call_id, tool, file_path, session='s1', turn='t1'):
    return call(call_id, tool, file_path, kind='scheduled', session=session,
                turn=turn)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        (self.work / 'src.py').write_text('print(1)\n', encoding='utf-8')
        (self.work / 'out.md').write_text('merged doc\n', encoding='utf-8')
        sub = self.work / 'other'
        sub.mkdir()
        (sub / 'src.py').write_text('other\n', encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    def reader(self, path):
        path = Path(path)
        return path.read_bytes() if path.is_file() else None

    def eval_(self, events, contract, baseline='auto', session='s1', turn='t1',
              preflight_only=False):
        if baseline == 'auto':
            baseline = zee.build_baseline(contract, str(self.work), self.reader) \
                if contract is not None else None
        return zee.evaluate_execution_evidence(
            events, contract, session_id=session, turn_id=turn,
            workspace=str(self.work), read_bytes=self.reader,
            baseline=baseline, preflight_only=preflight_only)

    def eng_contract(self, **kw):
        c = {'task_type': 'engineering', 'required_reads': ['src.py'],
             'expected_artifacts': [], 'required_modified_files': [],
             'allow_no_changes': False}
        c.update(kw)
        return c

    def reads_events(self):
        return [call('a', 'Read', str(self.work / 'src.py'), kind='scheduled'),
                call('a', 'Read', str(self.work / 'src.py'))]


class VerifyPaths(Base):
    def test_normal_read_modify_with_baseline_change(self):
        contract = self.eng_contract(required_modified_files=['out.md'])
        baseline = zee.build_baseline(contract, str(self.work), self.reader)
        events = self.reads_events() + [
            call('b', 'Edit', str(self.work / 'out.md'), kind='scheduled'),
            call('b', 'Edit', str(self.work / 'out.md'))]
        (self.work / 'out.md').write_text('merged doc v2\n', encoding='utf-8')
        out = self.eval_(events, contract, baseline=baseline)
        self.assertIs(out['execution_evidence_ok'], True)
        self.assertEqual(out['execution_evidence_status'], 'verified')

    def test_write_receipt_but_unchanged_baseline(self):
        contract = self.eng_contract(required_modified_files=['out.md'])
        baseline = zee.build_baseline(contract, str(self.work), self.reader)
        events = self.reads_events() + [
            call('b', 'Edit', str(self.work / 'out.md'), kind='scheduled'),
            call('b', 'Edit', str(self.work / 'out.md'))]
        out = self.eval_(events, contract, baseline=baseline)
        self.assertEqual(out['execution_evidence_status'], 'execution_claim_mismatch')
        self.assertTrue(any('no real change' in r for r in out['reasons']))

    def test_new_artifact_and_old_file_cannot_pose_as_new(self):
        contract = self.eng_contract(expected_artifacts=['new.md'])
        baseline = zee.build_baseline(contract, str(self.work), self.reader)
        (self.work / 'new.md').write_text('new\n', encoding='utf-8')
        events = self.reads_events() + [
            call('b', 'Write', str(self.work / 'new.md'), kind='scheduled'),
            call('b', 'Write', str(self.work / 'new.md'))]
        self.assertIs(self.eval_(events, contract, baseline=baseline)
                      ['execution_evidence_ok'], True)
        # 基线里已存在的文件混进 expected_artifacts：派工前即拒绝
        self.assertTrue(any('already exists' in r for r in
                            zee.validate_execution_contract(
                                self.eng_contract(expected_artifacts=['src.py']),
                                self.work)))

    def test_wrong_same_name_and_prefix_do_not_match(self):
        (self.work / 'src.py.bak').write_text('bak\n', encoding='utf-8')
        events = [call('a', 'Read', str(self.work / 'other' / 'src.py'),
                       kind='scheduled'),
                  call('a', 'Read', str(self.work / 'other' / 'src.py')),
                  call('b', 'Read', str(self.work / 'src.py.bak'), kind='scheduled'),
                  call('b', 'Read', str(self.work / 'src.py.bak'))]
        out = self.eval_(events, self.eng_contract())
        self.assertEqual(out['execution_evidence_status'], 'no_required_execution')

    def test_out_of_bounds_read_and_write_refused(self):
        outside = self.work.parent / (self.work.name + '-outside.txt')
        outside.write_text('x', encoding='utf-8')
        try:
            events = [call('a', 'Read', str(outside), kind='scheduled'),
                      call('a', 'Read', str(outside))]
            out = self.eval_(events, self.eng_contract())
            self.assertEqual(out['execution_evidence_status'],
                             'execution_claim_mismatch')
        finally:
            outside.unlink()

    def test_read_only_engineering_needs_allow_no_changes(self):
        contract = self.eng_contract(required_modified_files=[], allow_no_changes=True)
        out = self.eval_(self.reads_events(), contract)
        self.assertIs(out['execution_evidence_ok'], True)
        reasons = zee.validate_execution_contract(
            self.eng_contract(required_modified_files=[]), self.work)
        self.assertTrue(any('allow_no_changes' in r for r in reasons))


class Statuses(Base):
    def test_zero_tools(self):
        self.assertEqual(self.eval_([], self.eng_contract())
                         ['execution_evidence_status'], 'no_required_execution')

    def test_claimed_write_artifact_missing(self):
        gone = self.work / 'ghost.md'
        events = self.reads_events() + [
            call('b', 'Write', str(gone), kind='scheduled'),
            call('b', 'Write', str(gone))]
        self.assertEqual(self.eval_(events, self.eng_contract())
                         ['execution_evidence_status'], 'execution_claim_mismatch')

    def test_legitimate_no_change_review(self):
        contract = {'task_type': 'review_no_change', 'required_reads': ['src.py'],
                    'allow_no_changes': True}
        out = self.eval_(self.reads_events(), contract)
        self.assertIs(out['execution_evidence_ok'], True)

    def test_permission_denial(self):
        events = [call('a', 'Read', str(self.work / 'src.py'), kind='scheduled'),
                  call('a', 'Read', str(self.work / 'src.py'), success=False,
                       content='permission denied by approval policy')]
        self.assertEqual(self.eval_(events, self.eng_contract())
                         ['execution_evidence_status'], 'permission_denied')

    def test_truthy_string_success_is_malformed_not_verified(self):
        events = [call('a', 'Read', str(self.work / 'src.py'), kind='scheduled'),
                  call('a', 'Read', str(self.work / 'src.py'),
                       raw_result={'success': 'false', 'content': 'x'})]
        out = self.eval_(events, self.eng_contract())
        self.assertIs(out['execution_evidence_ok'], False)
        self.assertEqual(out['execution_evidence_status'], 'tool_result_malformed')
        for bad in (1, None, 'true', 0):
            events[1] = call('a', 'Read', str(self.work / 'src.py'),
                             raw_result={'success': bad})
            self.assertEqual(self.eval_(events, self.eng_contract())
                             ['execution_evidence_status'], 'tool_result_malformed')

    def test_malformed_scheduled_payload_rejected(self):
        bad = [{'sessionId': 's1', 'turnId': 't1', 'type': 'tool_call_scheduled',
                'payload': {'toolCallId': 'a'}}]  # toolName 缺失
        self.assertEqual(self.eval_(bad, self.eng_contract())
                         ['execution_evidence_status'], 'tool_result_malformed')
        bad2 = [{'sessionId': 's1', 'turnId': 't1', 'type': 'tool_call_scheduled',
                 'payload': 'not-a-dict'}]
        self.assertEqual(self.eval_(bad2, self.eng_contract())
                         ['execution_evidence_status'], 'tool_result_malformed')

    def test_pure_reasoning_and_missing_variants(self):
        self.assertIs(self.eval_(
            [], {'task_type': 'reasoning'})['execution_evidence_ok'], True)
        no_contract = self.eval_([], None)
        self.assertIsNone(no_contract['execution_evidence_ok'])
        self.assertEqual(no_contract['execution_evidence_status'],
                         'unverified_no_contract')
        self.assertEqual(self.eval_([], self.eng_contract(), preflight_only=True)
                         ['execution_evidence_status'], 'unverified_preflight_only')
        self.assertEqual(self.eval_([], self.eng_contract(), baseline=None)
                         ['execution_evidence_status'], 'unverified_no_baseline')
        self.assertEqual(self.eval_([], self.eng_contract(), session=None)
                         ['execution_evidence_status'], 'unverified_no_session_turn')

    def test_other_session_turn_events_do_not_count(self):
        events = [call('z', 'Read', str(self.work / 'src.py'), session='s2', turn='t9',
                       kind='scheduled'),
                  call('z', 'Read', str(self.work / 'src.py'), session='s2', turn='t9')]
        self.assertEqual(self.eval_(events, self.eng_contract())
                         ['execution_evidence_status'], 'no_required_execution')

    def test_missing_failed_orphan_results(self):
        events = [call('a', 'Read', str(self.work / 'src.py'), kind='scheduled')]
        self.assertEqual(self.eval_(events, self.eng_contract())
                         ['execution_evidence_status'], 'tool_result_missing')
        events.append(call('a', 'Read', str(self.work / 'src.py'), success=False,
                           content='boom'))
        self.assertEqual(self.eval_(events, self.eng_contract())
                         ['execution_evidence_status'], 'tool_result_failed')
        orphan = [call('q', 'Read', str(self.work / 'src.py'))]
        self.assertEqual(self.eval_(orphan, self.eng_contract())
                         ['execution_evidence_status'], 'tool_result_orphan')

    def test_duplicate_conflict_and_identical_replay(self):
        events = [call('a', 'Read', str(self.work / 'src.py'), kind='scheduled'),
                  call('a', 'Read', str(self.work / 'src.py')),
                  call('a', 'Read', str(self.work / 'src.py'), success=False,
                       content='flipped')]
        self.assertEqual(self.eval_(events, self.eng_contract())
                         ['execution_evidence_status'], 'tool_result_conflict')
        replay = [call('a', 'Read', str(self.work / 'src.py'), kind='scheduled'),
                  call('a', 'Read', str(self.work / 'src.py')),
                  call('a', 'Read', str(self.work / 'src.py'))]
        self.assertIs(self.eval_(replay, self.eng_contract())
                      ['execution_evidence_ok'], True)


class TestEvidence(Base):
    def make_evidence(self, **kw):
        argv = kw.pop('argv', [sys.executable, '-m', 'unittest'])
        inputs = kw.pop('inputs', ['src.py'])
        # producer 口径：登记输入用原口径绝对路径字符串参与 fingerprint（normpath
        # 保留大小写）；evidence.inputs 的哈希键用 normcase(realpath)。
        raw_inputs = [str(self.work / rel) for rel in inputs]
        folder = self.work / 'handoff' / 'tests' / 'reg'
        folder.mkdir(parents=True, exist_ok=True)
        stdout = kw.pop('stdout', b'ok\n')
        stderr = kw.pop('stderr', b'')
        (folder / 'stdout.log').write_bytes(stdout)
        (folder / 'stderr.log').write_bytes(stderr)
        input_hashes = {zee.norm_within(self.work, p):
                        hashlib.sha256(Path(p).read_bytes()).hexdigest()
                        for p in raw_inputs}
        env_sha = hashlib.sha256(b'stable-env').hexdigest()
        runtime_version = '%d.%d.%d' % sys.version_info[:3]
        # 与真实 producer 一致：run_registered_test 先 realpath 展开 cwd 再参与
        # compute_fingerprint；输入 raw 登记路径保留原口径（含大小写），两种口径分开。
        cwd_real = os.path.realpath(str(self.work))
        prior = {'name': 'reg', 'argv': argv, 'cwd': cwd_real,
                 'inputs': input_hashes, 'inputs_unchanged': True, 'exit_code': 0,
                 'stdout_sha256': hashlib.sha256(stdout).hexdigest(),
                 'stderr_sha256': hashlib.sha256(stderr).hexdigest(),
                 'env_sha256': env_sha, 'runtime_version': runtime_version,
                 'fingerprint': ec.compute_fingerprint(
                     argv, cwd_real, raw_inputs,
                     {'env_sha256': env_sha, 'runtime_version': runtime_version})}
        prior.update(kw)
        (folder / 'evidence.json').write_text(json.dumps(prior), encoding='utf-8')
        return {'name': 'reg', 'argv': argv, 'cwd': '.',
                'evidence_path': 'handoff/tests/reg/evidence.json',
                'inputs': raw_inputs}

    def test_wrote_files_but_no_test_evidence(self):
        contract = self.eng_contract(required_modified_files=['out.md'],
                                     required_test_command='placeholder')
        baseline = zee.build_baseline(contract, str(self.work), self.reader)
        events = self.reads_events() + [
            call('b', 'Edit', str(self.work / 'out.md'), kind='scheduled'),
            call('b', 'Edit', str(self.work / 'out.md'))]
        (self.work / 'out.md').write_text('v2\n', encoding='utf-8')
        out = self.eval_(events, contract, baseline=baseline)
        self.assertEqual(out['execution_evidence_status'], 'tests_not_run')

    def test_ran_other_command_is_not_target_test(self):
        self.make_evidence(argv=[sys.executable, 'other.py'])
        spec = {'name': 'reg', 'argv': [sys.executable, '-m', 'unittest'],
                'cwd': '.', 'evidence_path': 'handoff/tests/reg/evidence.json',
                'inputs': [str(self.work / 'src.py')]}
        contract = self.eng_contract(
            required_test_command=zee.command_caliber(spec['argv']),
            test_evidence=spec)
        self.assertEqual(self.eval_(self.reads_events(), contract)
                         ['execution_evidence_status'], 'tests_not_run')

    def test_forged_name_inputs_and_fingerprint_rejected(self):
        spec = self.make_evidence()
        good = self.eng_contract(
            required_test_command=zee.command_caliber(spec['argv']),
            test_evidence=spec)
        self.assertIs(self.eval_(self.reads_events(), good)
                      ['execution_evidence_ok'], True)
        # name 伪造
        forged = dict(spec, name='other-test')
        contract = self.eng_contract(
            required_test_command=zee.command_caliber(spec['argv']),
            test_evidence=forged)
        self.assertEqual(self.eval_(self.reads_events(), contract)
                         ['execution_evidence_status'], 'tests_not_run')
        # 旧 inputs 哈希（输入事后被改）
        (self.work / 'src.py').write_text('print(2)\n', encoding='utf-8')
        self.assertEqual(self.eval_(self.reads_events(), good)
                         ['execution_evidence_status'], 'tests_not_run')
        (self.work / 'src.py').write_text('print(1)\n', encoding='utf-8')
        # fingerprint 伪造
        self.make_evidence(fingerprint='invalid')
        self.assertEqual(self.eval_(self.reads_events(), good)
                         ['execution_evidence_status'], 'tests_not_run')

    def test_bad_evidence_json_does_not_crash(self):
        folder = self.work / 'handoff' / 'tests' / 'reg'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'evidence.json').write_text('[]', encoding='utf-8')
        spec = {'name': 'reg', 'argv': [sys.executable], 'cwd': '.',
                'evidence_path': 'handoff/tests/reg/evidence.json',
                'inputs': ['src.py']}
        contract = self.eng_contract(required_test_command='x', test_evidence=spec)
        out = self.eval_(self.reads_events(), contract)
        self.assertEqual(out['execution_evidence_status'], 'tests_not_run')
        self.assertTrue(any('unreadable' in r or 'object' in r
                            for r in out['reasons']))

    def test_nonzero_exit_and_tampered_log_rejected(self):
        self.make_evidence(exit_code=1)
        spec = {'name': 'reg', 'argv': [sys.executable, '-m', 'unittest'],
                'cwd': '.', 'evidence_path': 'handoff/tests/reg/evidence.json',
                'inputs': [str(self.work / 'src.py')]}
        contract = self.eng_contract(
            required_test_command=zee.command_caliber(spec['argv']),
            test_evidence=spec)
        self.assertEqual(self.eval_(self.reads_events(), contract)
                         ['execution_evidence_status'], 'tests_not_run')
        self.make_evidence()
        folder = self.work / 'handoff' / 'tests' / 'reg'
        (folder / 'stdout.log').write_bytes(b'tampered\n')
        self.assertEqual(self.eval_(self.reads_events(), contract)
                         ['execution_evidence_status'], 'tests_not_run')

    def test_missing_evidence_file_and_missing_spec_rejected(self):
        spec = {'name': 'reg', 'argv': [sys.executable], 'cwd': '.',
                'evidence_path': 'handoff/tests/reg/absent.json',
                'inputs': [str(self.work / 'src.py')]}
        contract = self.eng_contract(required_test_command='x', test_evidence=spec)
        self.assertEqual(self.eval_(self.reads_events(), contract)
                         ['execution_evidence_status'], 'tests_not_run')

    def test_real_run_registered_test_compat_mixed_case(self):
        """真实登记执行器兼容：调用 ec.run_registered_test 生成证据（混合大小写
        输入路径，无害打印命令），门禁必须放行；测试由主脑运行。"""
        target = self.work / 'CaseSensitiveInput.py'
        target.write_text('compat input\n', encoding='utf-8')
        argv = [sys.executable, '-c', 'print("compat-ok")']
        registry = self.work / 'registry.json'
        registry.write_text(json.dumps({'runs': {
            'compat': {'argv': argv, 'cwd': str(self.work),
                       'inputs': [str(target)], 'env': {}}}}), encoding='utf-8')
        result = ec.run_registered_test(str(registry), 'compat', str(self.work),
                                        str(self.work / 'handoff' / 'ev'))
        self.assertEqual(result['exit_code'], 0)
        # 真实 producer 的证据路径是长路径（realpath 展开后），与 self.work 的短路径
        # 形式可能不同：直接用解析后的绝对 evidence_path，归属仍由 norm_within 确认，
        # 不放宽越界检测。
        evidence_abs = str(Path(result['evidence']).resolve())
        self.assertIsNotNone(zee.norm_within(self.work, evidence_abs))
        spec = {'name': 'compat', 'argv': argv, 'cwd': str(self.work),
                'evidence_path': evidence_abs, 'inputs': [str(target)]}
        contract = self.eng_contract(
            allow_no_changes=True,
            required_test_command=zee.command_caliber(argv), test_evidence=spec)
        out = self.eval_(self.reads_events(), contract)
        self.assertIs(out['execution_evidence_ok'], True)
        self.assertEqual(out['execution_evidence_status'], 'verified')


class ContractValidation(Base):
    def test_rejections(self):
        def has(reasons, needle):
            return any(needle in r for r in reasons)

        self.assertTrue(has(zee.validate_execution_contract(self.eng_contract(
            required_reads=['../outside.py']), self.work), 'escapes workspace'))
        self.assertTrue(has(zee.validate_execution_contract(self.eng_contract(
            required_reads=['no-such.py']), self.work), 'missing on disk'))
        self.assertTrue(has(zee.validate_execution_contract(self.eng_contract(
            expected_artifacts=['src.py']), self.work), 'already exists'))
        self.assertTrue(has(zee.validate_execution_contract(self.eng_contract(
            surprise=True), self.work), 'unknown contract field'))
        self.assertTrue(has(zee.validate_execution_contract(self.eng_contract(
            required_reads=[True]), self.work), 'booleans are not paths'))
        self.assertTrue(has(zee.validate_execution_contract(self.eng_contract(
            required_reads=[7]), self.work), 'non-blank string paths'))
        self.assertTrue(has(zee.validate_execution_contract(self.eng_contract(
            required_reads=['   ']), self.work), 'non-blank'))
        self.assertTrue(has(zee.validate_execution_contract(
            {'task_type': 'reasoning', 'required_reads': ['src.py']},
            self.work), 'reasoning task cannot'))
        self.assertTrue(has(zee.validate_execution_contract(
            {'task_type': 'review_no_change',
             'required_modified_files': ['out.md']}, self.work), 'review_no_change'))
        self.assertTrue(has(zee.validate_execution_contract(self.eng_contract(
            allow_no_changes=True, required_modified_files=['out.md']),
            self.work), 'contradicts'))
        self.assertTrue(has(zee.validate_execution_contract(self.eng_contract(
            required_modified_files=[]), self.work), 'allow_no_changes'))

    def test_command_caliber_binding(self):
        spec = {'name': 'reg', 'argv': [sys.executable, '-m', 'unittest'],
                'cwd': '.', 'evidence_path': 'handoff/tests/reg/evidence.json',
                'inputs': ['src.py']}
        reasons = zee.validate_execution_contract(self.eng_contract(
            required_test_command='some other command', test_evidence=spec), self.work)
        self.assertTrue(has(reasons, 'list2cmdline')
                        or any('required_test_command' in r for r in reasons))
        good = self.eng_contract(
            required_test_command=zee.command_caliber(spec['argv']),
            test_evidence=spec)
        self.assertEqual([r for r in zee.validate_execution_contract(good, self.work)
                          if 'test_evidence' in r or 'required_test_command' in r], [])

    def test_test_evidence_spec_strictness(self):
        bad_spec = {'name': 'reg', 'argv': [sys.executable], 'cwd': '.',
                    'evidence_path': 'handoff/tests/reg/evidence.json'}  # 缺 inputs
        reasons = zee.validate_execution_contract(self.eng_contract(
            required_test_command='x', test_evidence=bad_spec), self.work)
        self.assertTrue(any('exactly name/argv' in r for r in reasons))
        bool_spec = {'name': True, 'argv': [sys.executable], 'cwd': '.',
                     'evidence_path': 'handoff/tests/reg/evidence.json',
                     'inputs': ['src.py']}
        reasons = zee.validate_execution_contract(self.eng_contract(
            required_test_command='x', test_evidence=bool_spec), self.work)
        self.assertTrue(any('name must be a non-empty string' in r for r in reasons))
        out_inputs = {'name': 'reg', 'argv': [sys.executable], 'cwd': '.',
                      'evidence_path': 'handoff/tests/reg/evidence.json',
                      'inputs': ['../outside.py']}
        reasons = zee.validate_execution_contract(self.eng_contract(
            required_test_command='x', test_evidence=out_inputs), self.work)
        self.assertTrue(any('input escapes workspace' in r for r in reasons))

    def test_valid_contract_passes(self):
        self.assertEqual(zee.validate_execution_contract(self.eng_contract(
            required_modified_files=['out.md']), self.work), [])
        self.assertEqual(zee.validate_execution_contract(
            {'task_type': 'reasoning'}, self.work), [])


class ErrorTerminalCompatibility(Base):
    """BW-ZCODE-MANUAL-20261008-S2：tool_call_error 作为明确失败终态归并。"""

    def test_plain_error_is_failed_not_missing(self):
        events = [sched('a', 'Read', str(self.work / 'src.py')),
                  err_event('a', message='provider stream aborted')]
        out = self.eval_(events, self.eng_contract())
        self.assertEqual(out['execution_evidence_status'], 'tool_result_failed')
        self.assertIs(out['execution_evidence_ok'], False)

    def test_error_plus_committed_is_failed_not_success(self):
        # F3 形态：error 终态在前，随后 ledger 的 tool_result_committed 只是状态字段，
        # 绝不能被当成功而放行。
        events = [sched('a', 'Read', str(self.work / 'src.py')),
                  err_event('a', message='edit rejected'),
                  ledger_committed('a')]
        out = self.eval_(events, self.eng_contract())
        self.assertEqual(out['execution_evidence_status'], 'tool_result_failed')

    def test_explicit_permission_error_is_denied(self):
        events = [sched('a', 'Read', str(self.work / 'src.py')),
                  err_event('a', error={'code': 'PERMISSION_DENIED',
                                       'type': 'AuthError',
                                       'message': 'permission denied by approval policy',
                                       'detail': '', 'stack': ''})]
        out = self.eval_(events, self.eng_contract())
        self.assertEqual(out['execution_evidence_status'], 'permission_denied')

    def test_only_committed_is_missing_never_success(self):
        events = [sched('a', 'Read', str(self.work / 'src.py')),
                  ledger_committed('a')]
        out = self.eval_(events, self.eng_contract())
        self.assertEqual(out['execution_evidence_status'], 'tool_result_missing')
        self.assertIs(out['execution_evidence_ok'], False)

    def test_error_in_other_session_turn_does_not_resolve(self):
        # 错误落在别的 session/turn：本 turn 的 scheduled 仍无终态 → missing。
        events = [sched('a', 'Read', str(self.work / 'src.py')),
                  err_event('a', message='x', session='s2', turn='t9')]
        out = self.eval_(events, self.eng_contract())
        self.assertEqual(out['execution_evidence_status'], 'tool_result_missing')

    def test_orphan_error_without_scheduled(self):
        out = self.eval_([err_event('q', message='stray')], self.eng_contract())
        self.assertEqual(out['execution_evidence_status'], 'tool_result_orphan')

    def test_malformed_error_payload_rejected(self):
        no_err = [sched('a', 'Read', str(self.work / 'src.py')),
                  err_event('a', raw_payload={'toolCallId': 'a'})]  # 缺 error 对象
        self.assertEqual(self.eval_(no_err, self.eng_contract())
                         ['execution_evidence_status'], 'tool_result_malformed')
        no_id = [sched('a', 'Read', str(self.work / 'src.py')),
                 err_event('a', raw_payload={'error': {'message': 'x'}})]  # 缺 toolCallId
        self.assertEqual(self.eval_(no_id, self.eng_contract())
                         ['execution_evidence_status'], 'tool_result_malformed')

    def test_identical_error_replay_is_idempotent(self):
        e1 = err_event('a', error={'code': 'E', 'type': 'T', 'message': 'same',
                                   'detail': 'd', 'stack': 's'})
        e2 = err_event('a', error={'code': 'E', 'type': 'T', 'message': 'same',
                                   'detail': 'd', 'stack': 's'})
        events = [sched('a', 'Read', str(self.work / 'src.py')), e1, e2]
        out = self.eval_(events, self.eng_contract())
        self.assertEqual(out['execution_evidence_status'], 'tool_result_failed')

    def test_success_error_conflict_rejected(self):
        ok_then_err = [sched('a', 'Read', str(self.work / 'src.py')),
                       call('a', 'Read', str(self.work / 'src.py')),
                       err_event('a', message='late error')]
        self.assertEqual(self.eval_(ok_then_err, self.eng_contract())
                         ['execution_evidence_status'], 'tool_result_conflict')
        err_then_ok = [sched('a', 'Read', str(self.work / 'src.py')),
                       err_event('a', message='early error'),
                       call('a', 'Read', str(self.work / 'src.py'))]
        self.assertEqual(self.eval_(err_then_ok, self.eng_contract())
                         ['execution_evidence_status'], 'tool_result_conflict')

    def test_error_terminal_does_not_break_success_path(self):
        # 合法 success 写入证据验收仍保留：成功 result 与 error 终态共存于不同 call，
        # error call 归 failed；整体仍是失败终态优先，不冒充通过。
        contract = self.eng_contract(required_modified_files=['out.md'])
        baseline = zee.build_baseline(contract, str(self.work), self.reader)
        events = self.reads_events() + [
            sched('b', 'Edit', str(self.work / 'out.md')),
            err_event('b', message='write failed')]
        (self.work / 'out.md').write_text('merged doc v2\n', encoding='utf-8')
        out = self.eval_(events, contract, baseline=baseline)
        self.assertEqual(out['execution_evidence_status'], 'tool_result_failed')

    # ---- S3 边界收紧：严格 error schema / 权限只查已知文本 / 重放按 kind+identity ----

    def test_empty_or_unknown_only_error_is_malformed(self):
        # {} 与只有陌生键的 dict 都不能伪造失败终态。
        for err in ({}, {'foo': 1, 'bar': 'x'}):
            events = [sched('a', 'Read', str(self.work / 'src.py')),
                      err_event('a', raw_payload={'toolCallId': 'a', 'error': err})]
            out = self.eval_(events, self.eng_contract())
            self.assertEqual(out['execution_evidence_status'], 'tool_result_malformed',
                             f'{err!r} must be malformed, not a failed terminal')

    def test_message_required_nonempty_string(self):
        for bad in (123, None, True, '   ', ''):
            events = [sched('a', 'Read', str(self.work / 'src.py')),
                      err_event('a', raw_payload={'toolCallId': 'a',
                                                  'error': {'message': bad}})]
            out = self.eval_(events, self.eng_contract())
            self.assertEqual(out['execution_evidence_status'], 'tool_result_malformed',
                             f'message={bad!r} must be malformed')
        # 合法 message-only 仍归失败（不是成功、不是 malformed）。
        ok = [sched('a', 'Read', str(self.work / 'src.py')),
              err_event('a', raw_payload={'toolCallId': 'a',
                                          'error': {'message': 'boom'}})]
        self.assertEqual(self.eval_(ok, self.eng_contract())
                         ['execution_evidence_status'], 'tool_result_failed')

    def test_known_error_fields_must_be_strings(self):
        for key in ('code', 'type', 'detail', 'stack'):
            events = [sched('a', 'Read', str(self.work / 'src.py')),
                      err_event('a', raw_payload={'toolCallId': 'a',
                                                  'error': {'message': 'boom',
                                                            key: 7}})]
            out = self.eval_(events, self.eng_contract())
            self.assertEqual(out['execution_evidence_status'], 'tool_result_malformed',
                             f'{key} 非字符串必须 malformed')
        # 未知附加元数据被忽略（类型错误也不触发拒绝），合法 message 仍失败。
        meta = [sched('a', 'Read', str(self.work / 'src.py')),
                err_event('a', raw_payload={'toolCallId': 'a',
                                            'error': {'message': 'boom',
                                                      'duration': 1234, 'seq': None}})]
        self.assertEqual(self.eval_(meta, self.eng_contract())
                         ['execution_evidence_status'], 'tool_result_failed')

    def test_permission_denial_scans_only_known_text_fields(self):
        # 无关 permission 键与 quota 文案不应被判权限拒绝。
        events = [sched('a', 'Read', str(self.work / 'src.py')),
                  err_event('a', raw_payload={'toolCallId': 'a', 'error': {
                      'message': 'quota exceeded', 'permission': False}})]
        self.assertEqual(self.eval_(events, self.eng_contract())
                         ['execution_evidence_status'], 'tool_result_failed')
        # stack 里的 PermissionBroker/denied 噪音不应触发（stack 不扫描）。
        noise = [sched('a', 'Read', str(self.work / 'src.py')),
                 err_event('a', raw_payload={'toolCallId': 'a', 'error': {
                     'message': 'generic tool failure',
                     'stack': 'at PermissionBroker.requestPermission denied trace'}})]
        self.assertEqual(self.eval_(noise, self.eng_contract())
                         ['execution_evidence_status'], 'tool_result_failed')
        # 真正明确权限拒绝（即便 code 命中）→ denied；外层 is_error 标志不影响。
        real = [sched('a', 'Read', str(self.work / 'src.py')),
                err_event('a', raw_payload={'toolCallId': 'a', 'error': {
                    'code': 'PERMISSION_DENIED', 'message': 'tool blocked',
                    'is_error': False}})]
        self.assertEqual(self.eval_(real, self.eng_contract())
                         ['execution_evidence_status'], 'permission_denied')

    def test_replay_idempotent_ignores_outer_metadata(self):
        # result：同 result 正文、payload 级 duration 不同 → 幂等（不误报 conflict）。
        r1 = {'toolCallId': 'a', 'duration': 1,
              'result': {'success': True, 'content': 'ok'}}
        r2 = {'toolCallId': 'a', 'duration': 99,
              'result': {'success': True, 'content': 'ok'}}
        events = [sched('a', 'Read', str(self.work / 'src.py')),
                  call('a', 'Read', str(self.work / 'src.py'), raw_payload=r1),
                  call('a', 'Read', str(self.work / 'src.py'), raw_payload=r2)]
        out = self.eval_(events, self.eng_contract())
        self.assertEqual(out['execution_evidence_status'], 'verified')
        # error：同已知字段正文、含不同未知元数据 → 幂等，仍归失败。
        e1 = err_event('a', raw_payload={'toolCallId': 'a',
                                          'error': {'message': 'boom', 'duration': 5}})
        e2 = err_event('a', raw_payload={'toolCallId': 'a',
                                          'error': {'message': 'boom', 'duration': 9}})
        events = [sched('a', 'Read', str(self.work / 'src.py')), e1, e2]
        self.assertEqual(self.eval_(events, self.eng_contract())
                         ['execution_evidence_status'], 'tool_result_failed')

    def test_true_body_conflict_still_rejected(self):
        # 正文（message）漂移 → conflict，即便 kind 相同。
        e1 = err_event('a', raw_payload={'toolCallId': 'a', 'error': {'message': 'x'}})
        e2 = err_event('a', raw_payload={'toolCallId': 'a', 'error': {'message': 'y'}})
        events = [sched('a', 'Read', str(self.work / 'src.py')), e1, e2]
        self.assertEqual(self.eval_(events, self.eng_contract())
                         ['execution_evidence_status'], 'tool_result_conflict')
        # result 正文冲突（success True vs False）→ conflict。
        r_ok = {'toolCallId': 'a', 'result': {'success': True, 'content': 'ok'}}
        r_bad = {'toolCallId': 'a', 'result': {'success': False, 'content': 'ok'}}
        events = [sched('a', 'Read', str(self.work / 'src.py')),
                  call('a', 'Read', str(self.work / 'src.py'), raw_payload=r_ok),
                  call('a', 'Read', str(self.work / 'src.py'), raw_payload=r_bad)]
        self.assertEqual(self.eval_(events, self.eng_contract())
                         ['execution_evidence_status'], 'tool_result_conflict')


class EntryPoints(unittest.TestCase):
    def test_contract_file_errors(self):
        with self.assertRaises(ValueError):
            zee.load_execution_contract(ROOT / 'no-such-contract.json')
        bad = HERE / 'tmp-bad-contract.json'
        bad.write_text('[]', encoding='utf-8')
        try:
            with self.assertRaises(ValueError):
                zee.load_execution_contract(bad)
        finally:
            bad.unlink()

    def test_command_caliber_matches_list2cmdline(self):
        argv = ['python', '-m', 'unittest', '-v']
        self.assertEqual(zee.command_caliber(argv),
                         subprocess.list2cmdline(argv))


if __name__ == '__main__':
    unittest.main()
