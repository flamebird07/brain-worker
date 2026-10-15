"""BW-INTERRUPTION-20261015-S2 离线测试：Qoder 真实结构化失败错误载体中的明确认证
失败文案（"Custom model authentication failed."）映射为 authentication_failure，
不再归入 model_execution_failure / quota_429。

函数级离线样例：不访问网络、不读私有原始流/密钥、不发真实 CLI 请求、不触状态库。
覆盖：S3 合成结构（protocol_success=false、exit 1、result_errors 单条认证文案、
result_errors_info=null）、原输入不突变、成功结果/报告正文中同文案不触发、
取消退出码 4294967295、仅 401/403/429 或 quota 字样不误报认证、真实 quota /
权限拒绝 / 报告格式失败语义保持、并发错误保留拒绝、同 executor 不同
requested_model 不相互映射、execution_control.diagnose 兼容旧调用方。"""
import copy
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'scripts'))
import execution_control as ec  # noqa: E402
import qoder_direct as qd  # noqa: E402

AUTH_TEXT = ('Custom model authentication failed. Please check if your '
             'API Key is correctly configured and valid.')


def _s3_auth_summary(**over):
    """S3 真实终态的合成结构：CLI exit 1、protocol_success=false、
    result_errors 仅一条认证文案、result_errors_info=null。"""
    summary = {
        'protocol_success': False,
        'exit_code': 1,
        'result_errors': [AUTH_TEXT],
        'result_errors_info': None,
        'permission_denials': [],
        'parse_error': None,
        'stop_reason': 'error',
        'model_requested': 'some-custom-model',
    }
    summary.update(over)
    return summary


class AuthenticationFactsTests(unittest.TestCase):

    def test_s3_shape_auth_not_model_not_quota(self):
        facts = qd._qoder_failure_facts(_s3_auth_summary())
        self.assertTrue(facts['authentication_failure'])
        self.assertFalse(facts['model_execution_failure'])
        self.assertFalse(facts['quota_429'])
        d = ec.diagnose(facts)
        self.assertIn('authentication_failure', d['failure_types'])
        self.assertNotIn('model_execution_failure', d['failure_types'])
        self.assertNotIn('quota_429', d['failure_types'])

    def test_input_not_mutated(self):
        summary = _s3_auth_summary()
        before = copy.deepcopy(summary)
        qd._qoder_failure_facts(summary)
        self.assertEqual(summary, before)

    def test_success_with_auth_text_not_failure(self):
        facts = qd._qoder_failure_facts(_s3_auth_summary(protocol_success=True,
                                                         exit_code=0))
        self.assertFalse(facts['authentication_failure'])
        self.assertFalse(facts['model_execution_failure'])

    def test_report_body_text_not_scanned(self):
        summary = _s3_auth_summary(result_errors=[],
                                   report_body='... ' + AUTH_TEXT + ' ...')
        facts = qd._qoder_failure_facts(summary)
        self.assertFalse(facts['authentication_failure'])
        self.assertTrue(facts['model_execution_failure'])

    def test_cancel_exit_code_not_auth_not_quota(self):
        summary = _s3_auth_summary(exit_code=4294967295,
                                   result_errors=['Operation was canceled'])
        facts = qd._qoder_failure_facts(summary)
        self.assertFalse(facts['authentication_failure'])
        self.assertFalse(facts['quota_429'])

    def test_status_codes_alone_not_auth(self):
        for code in (401, 403, 429):
            summary = _s3_auth_summary(
                result_errors=[{'status': code, 'message': f'HTTP {code}'}])
            facts = qd._qoder_failure_facts(summary)
            self.assertFalse(facts['authentication_failure'],
                             f'status {code} must not imply auth')

    def test_quota_word_alone_not_auth(self):
        summary = _s3_auth_summary(result_errors=['429 上游限流，将在 10 分钟后重置'])
        facts = qd._qoder_failure_facts(summary)
        self.assertFalse(facts['authentication_failure'])
        self.assertTrue(facts['quota_429'])

    def test_nested_message_shapes(self):
        for errors in ([{'message': AUTH_TEXT}],
                       [{'error': {'message': AUTH_TEXT}}],
                       [{'error': {'errors': {'message': AUTH_TEXT}}}]):
            summary = _s3_auth_summary(result_errors=errors)
            facts = qd._qoder_failure_facts(summary)
            self.assertTrue(facts['authentication_failure'])

    def test_case_and_whitespace_insensitive(self):
        summary = _s3_auth_summary(result_errors=['  custom MODEL   authentication '
                                                  'FAILED. check api key'])
        facts = qd._qoder_failure_facts(summary)
        self.assertTrue(facts['authentication_failure'])

    def test_parse_failure_kept_with_auth_text_elsewhere(self):
        facts = qd._qoder_failure_facts(_s3_auth_summary(parse_error='bad json',
                                                         result_errors=[]))
        self.assertTrue(facts['protocol_parse_failure'])
        self.assertFalse(facts['authentication_failure'])

    def test_permission_denials_preserved_alongside_auth(self):
        summary = _s3_auth_summary(
            permission_denials=[{'tool': 'Bash', 'decision': 'deny'}],
            result_errors=[AUTH_TEXT])
        facts = qd._qoder_failure_facts(summary)
        self.assertTrue(facts['permission_rule_denied'])
        self.assertTrue(facts['authentication_failure'])
        self.assertFalse(facts['model_execution_failure'])

    def test_models_do_not_cross_map(self):
        for model in ('Qwen3.8-Max', 'qodercn:Qwen-3.8-Max', 'other-custom'):
            facts = qd._qoder_failure_facts(_s3_auth_summary(model_requested=model))
            self.assertTrue(facts['authentication_failure'])
            self.assertEqual(facts['evidence']['model_requested'], model)

    def test_evidence_preserves_raw_errors(self):
        summary = _s3_auth_summary()
        facts = qd._qoder_failure_facts(summary)
        self.assertEqual(facts['evidence']['result_errors'], [AUTH_TEXT])
        self.assertIsNone(facts['reset_hint'])


class DiagnoseCompatTests(unittest.TestCase):

    def test_legacy_caller_without_auth_field(self):
        d = ec.diagnose({'quota_429': True, 'model_execution_failure': True})
        self.assertEqual(d['failure_types'],
                         ['quota_429', 'model_execution_failure'])

    def test_failure_types_contains_auth(self):
        self.assertIn('authentication_failure', ec.FAILURE_TYPES)

    def test_existing_classifications_unchanged(self):
        d = ec.diagnose({'permission_rule_denied': True,
                         'protocol_parse_failure': True,
                         'test_exit_code': 2})
        self.assertEqual(d['failure_types'],
                         ['permission_rule_denied', 'protocol_parse_failure',
                          'test_failure'])


if __name__ == '__main__':
    unittest.main()
