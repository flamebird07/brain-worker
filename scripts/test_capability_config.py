"""Offline tests for Feishu config resolution in capability_ledger.

覆盖 BW-DUAL-03 验收点：
  - 环境变量与 YAML 两种入口；字段级优先级（环境变量 > YAML）
  - 缺配置 / 半套凭证 / 无效文件 / 缺 PyYAML 均明确失败
  - 缺配置时零网络请求（token/request 不被调用）
  - 虚构表标识进入正确请求目标（app_token + table id）
  - 旧台账漂移检查默认关闭；显式启用保持原有语义
全部离线，不读真实凭据、不请求真实飞书。
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import capability_ledger as ledger

FAKE_CFG = {
    "app_id": "cli_fake",
    "app_secret": "fake_secret",
    "app_token": "fake_app_token",
    "summary_table": "tbl_summary_fake",
    "events_table": "tbl_events_fake",
    "legacy_ledger": None,
}


def _clear_env():
    for name in ledger.ENV_KEYS.values():
        os.environ.pop(name, None)
    os.environ.pop("BRAIN_WORKER_PLATFORMS_CONFIG", None)


class LoadConfigTests(unittest.TestCase):
    def setUp(self):
        _clear_env()
        ledger._CONFIG = None
        self.tmp = tempfile.mkdtemp(prefix="bw-feishu-cfg-")
        self.addCleanup(_clear_env)
        self.addCleanup(lambda: setattr(ledger, "_CONFIG", None))

    def _write_yaml(self, extra):
        import yaml
        path = Path(self.tmp) / "platforms.yaml"
        path.write_text(
            yaml.safe_dump({"platforms": {"feishu": {"extra": extra}}},
                           allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return path

    def test_env_entry_without_yaml(self):
        os.environ.update({
            "FEISHU_APP_ID": "cli_env", "FEISHU_APP_SECRET": "sec_env",
            "FEISHU_APP_TOKEN": "tok_env", "FEISHU_SUMMARY_TABLE": "tbl_s_env",
            "FEISHU_EVENTS_TABLE": "tbl_e_env",
        })
        # 环境变量齐全时不应读取 YAML；patch _yaml_extra 以证明不被调用。
        with patch.object(ledger, "_yaml_extra", side_effect=AssertionError("YAML 不应被读取")):
            cfg = ledger.load_config()
        self.assertEqual(cfg["app_id"], "cli_env")
        self.assertEqual(cfg["summary_table"], "tbl_s_env")
        self.assertIsNone(cfg["legacy_ledger"])

    def test_yaml_entry(self):
        path = self._write_yaml({
            "app_id": "cli_yaml", "app_secret": "sec_yaml",
            "app_token": "tok_yaml", "summary_table": "tbl_s_yaml",
            "events_table": "tbl_e_yaml",
        })
        os.environ["BRAIN_WORKER_PLATFORMS_CONFIG"] = str(path)
        cfg = ledger.load_config()
        self.assertEqual(cfg["app_id"], "cli_yaml")
        self.assertEqual(cfg["events_table"], "tbl_e_yaml")
        self.assertIsNone(cfg["legacy_ledger"])

    def test_env_precedence_over_yaml(self):
        path = self._write_yaml({
            "app_id": "cli_yaml", "app_secret": "sec_yaml",
            "app_token": "tok_yaml", "summary_table": "tbl_s_yaml",
            "events_table": "tbl_e_yaml",
        })
        os.environ["BRAIN_WORKER_PLATFORMS_CONFIG"] = str(path)
        os.environ["FEISHU_APP_ID"] = "cli_env"  # 仅覆盖一个字段
        cfg = ledger.load_config()
        self.assertEqual(cfg["app_id"], "cli_env")          # 环境变量优先
        self.assertEqual(cfg["app_token"], "tok_yaml")       # 其余回退 YAML

    def test_missing_required_field_fails(self):
        os.environ["FEISHU_APP_ID"] = "cli_env"  # 缺 app_secret/app_token/两表
        with self.assertRaisesRegex(RuntimeError, "缺少必需飞书配置"):
            ledger.load_config()

    def test_half_credentials_fails(self):
        os.environ.update({
            "FEISHU_APP_ID": "cli_env", "FEISHU_APP_SECRET": "sec_env",
        })  # 有凭证但无 app_token/表
        with self.assertRaisesRegex(RuntimeError, "缺少必需飞书配置"):
            ledger.load_config()

    def test_missing_yaml_file_fails(self):
        os.environ["BRAIN_WORKER_PLATFORMS_CONFIG"] = str(Path(self.tmp) / "nonexistent.yaml")
        with self.assertRaisesRegex(RuntimeError, "无法读取飞书平台配置文件"):
            ledger.load_config()

    def test_invalid_yaml_file_fails(self):
        path = Path(self.tmp) / "platforms.yaml"
        path.write_text(": bad yaml: [unbalanced", encoding="utf-8")
        os.environ["BRAIN_WORKER_PLATFORMS_CONFIG"] = str(path)
        with self.assertRaisesRegex(RuntimeError, "不是合法 YAML"):
            ledger.load_config()

    def test_yaml_missing_pyyaml_fails(self):
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("No module named yaml")
            return real_import(name, *args, **kwargs)

        os.environ["BRAIN_WORKER_PLATFORMS_CONFIG"] = str(Path(self.tmp) / "platforms.yaml")
        with patch.object(builtins, "__import__", side_effect=fake_import):
            with self.assertRaisesRegex(RuntimeError, "PyYAML"):
                ledger.load_config()

    # ---------------- BW-DUAL-03A：YAML 节点类型逐层校验 ----------------

    def _yaml_with(self, content):
        path = Path(self.tmp) / "struct.yaml"
        path.write_text(content, encoding="utf-8")
        os.environ["BRAIN_WORKER_PLATFORMS_CONFIG"] = str(path)
        return path

    def test_yaml_root_list_fails_cleanly(self):
        self._yaml_with("- item\n")
        with self.assertRaisesRegex(RuntimeError, "根节点.*应为映射"):
            ledger.load_config()

    def test_yaml_platforms_list_fails_cleanly(self):
        self._yaml_with("platforms: []\n")
        with self.assertRaisesRegex(RuntimeError, "platforms 应为映射"):
            ledger.load_config()

    def test_yaml_feishu_list_fails_cleanly(self):
        self._yaml_with("platforms:\n  feishu: []\n")
        with self.assertRaisesRegex(RuntimeError, "platforms.feishu 应为映射"):
            ledger.load_config()

    def test_yaml_extra_list_fails_cleanly(self):
        self._yaml_with("platforms:\n  feishu:\n    extra: []\n")
        with self.assertRaisesRegex(RuntimeError, "extra 应为映射"):
            ledger.load_config()

    def test_yaml_root_scalar_fails_cleanly(self):
        self._yaml_with("hello\n")
        with self.assertRaisesRegex(RuntimeError, "根节点.*应为映射"):
            ledger.load_config()

    def test_yaml_null_intermediate_treated_as_missing_extra(self):
        # "platforms:"（空值）→ None 视为缺失，最终报缺少 extra，不崩溃。
        self._yaml_with("platforms:\n")
        with self.assertRaisesRegex(RuntimeError, "缺少 platforms.feishu.extra"):
            ledger.load_config()

    def test_windows_linux_path_resolution_is_platform_agnostic(self):
        # CONFIG_FILE 以 ~ 开头，expanduser 后在 Windows/Linux 均按用户目录解析。
        resolved = Path(os.path.expanduser(ledger.CONFIG_FILE))
        self.assertTrue(str(resolved).startswith(str(Path.home())))
        # 不包含任何硬编码平台路径段
        self.assertNotIn("AppData", ledger.CONFIG_FILE)
        self.assertNotIn("E:", ledger.CONFIG_FILE)


class ZeroNetworkTests(unittest.TestCase):
    def test_missing_config_makes_zero_network_calls(self):
        calls = []

        def token():
            calls.append("token")
            raise AssertionError("不应获取 token")

        def request(*_a, **_k):
            calls.append("request")
            raise AssertionError("不应发起请求")

        with patch.object(ledger, "cfg", side_effect=RuntimeError("缺少必需飞书配置：app_id")), \
             patch.object(ledger, "local_lock", lambda: contextlib.nullcontext()), \
             patch.object(ledger, "token", token), \
             patch.object(ledger, "request", request):
            with self.assertRaisesRegex(RuntimeError, "缺少必需飞书配置"):
                ledger.main(["status", "--agent", "ZCode", "--model", "GLM"])
        self.assertEqual(calls, [], "缺配置时 token/request 均不应被调用")

    def test_config_failure_precedes_lock_and_network(self):
        # 缺配置时 local_lock 也不应进入（配置校验在获取锁之前）。
        entered = []

        def lock():
            entered.append("lock")
            return contextlib.nullcontext()

        with patch.object(ledger, "cfg", side_effect=RuntimeError("缺少必需飞书配置")), \
             patch.object(ledger, "local_lock", side_effect=lock):
            with self.assertRaises(RuntimeError):
                ledger.main(["status", "--agent", "ZCode", "--model", "GLM"])
        self.assertEqual(entered, [])

    def test_yaml_structure_error_zero_network_zero_lock(self):
        # BW-DUAL-03A：显式 YAML 坏结构走真实 load_config 失败路径，
        # token/request/锁 进入次数均应为 0。
        import tempfile
        calls = []

        def token():
            calls.append("token")
            raise AssertionError("不应获取 token")

        def request(*_a, **_k):
            calls.append("request")
            raise AssertionError("不应发起请求")

        def lock():
            calls.append("lock")
            return contextlib.nullcontext()

        with tempfile.TemporaryDirectory(prefix="bw-03a-") as tmp:
            bad = Path(tmp) / "bad-struct.yaml"
            bad.write_text("platforms: []\n", encoding="utf-8")
            env = dict(os.environ)
            for name in ledger.ENV_KEYS.values():
                env.pop(name, None)
            env["BRAIN_WORKER_PLATFORMS_CONFIG"] = str(bad)
            with patch.dict(os.environ, env, clear=True), \
                 patch.object(ledger, "_CONFIG", None), \
                 patch.object(ledger, "local_lock", side_effect=lock), \
                 patch.object(ledger, "token", token), \
                 patch.object(ledger, "request", request):
                with self.assertRaisesRegex(RuntimeError, "应为映射"):
                    ledger.main(["status", "--agent", "ZCode", "--model", "GLM"])
        self.assertEqual(calls, [], "YAML 结构错误时 token/request/lock 均不应被调用")


class ExplicitYamlOptionalTests(unittest.TestCase):
    """BW-DUAL-03A：显式 YAML 不被静默忽略；纯环境变量保持零 YAML 依赖。"""

    def setUp(self):
        _clear_env()
        ledger._CONFIG = None
        self.tmp = tempfile.mkdtemp(prefix="bw-03a-opt-")
        self.addCleanup(_clear_env)
        self.addCleanup(lambda: setattr(ledger, "_CONFIG", None))
        self.env_complete = {
            "FEISHU_APP_ID": "cli_env", "FEISHU_APP_SECRET": "sec_env",
            "FEISHU_APP_TOKEN": "tok_env", "FEISHU_SUMMARY_TABLE": "tbl_s_env",
            "FEISHU_EVENTS_TABLE": "tbl_e_env",
        }

    def _write_opt_yaml(self, legacy="/tmp/my-legacy.md"):
        path = Path(self.tmp) / "platforms.yaml"
        path.write_text(
            "platforms:\n  feishu:\n    extra:\n      legacy_ledger: \"%s\"\n" % legacy,
            encoding="utf-8",
        )
        return path

    def test_explicit_yaml_optional_read_with_complete_env(self):
        path = self._write_opt_yaml()
        os.environ.update(self.env_complete)
        os.environ["BRAIN_WORKER_PLATFORMS_CONFIG"] = str(path)
        cfg = ledger.load_config()
        self.assertEqual(cfg["legacy_ledger"], "/tmp/my-legacy.md")
        # 必需字段仍来自环境变量
        self.assertEqual(cfg["app_id"], "cli_env")

    def test_env_legacy_overrides_explicit_yaml(self):
        path = self._write_opt_yaml(legacy="/tmp/yaml-legacy.md")
        os.environ.update(self.env_complete)
        os.environ["BRAIN_WORKER_PLATFORMS_CONFIG"] = str(path)
        os.environ["FEISHU_LEGACY_LEDGER"] = "D:/env/legacy.md"
        cfg = ledger.load_config()
        self.assertEqual(cfg["legacy_ledger"], "D:/env/legacy.md")

    def test_complete_env_without_explicit_yaml_never_imports_pyyaml(self):
        # 5 个必需环境变量齐全且未显式指定 YAML：不读取任何 YAML、不 import PyYAML。
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "yaml":
                raise AssertionError("纯环境变量方式不应 import PyYAML")
            return real_import(name, *args, **kwargs)

        os.environ.update(self.env_complete)
        with patch.object(builtins, "__import__", side_effect=fake_import), \
             patch.object(ledger, "_yaml_extra", side_effect=AssertionError("不应读取 YAML")):
            cfg = ledger.load_config()
        self.assertEqual(cfg["app_id"], "cli_env")
        self.assertIsNone(cfg["legacy_ledger"])

    def test_explicit_yaml_missing_file_fails_even_with_complete_env(self):
        os.environ.update(self.env_complete)
        os.environ["BRAIN_WORKER_PLATFORMS_CONFIG"] = str(Path(self.tmp) / "nonexistent.yaml")
        with self.assertRaisesRegex(RuntimeError, "无法读取飞书平台配置文件"):
            ledger.load_config()

    def test_explicit_yaml_structure_error_fails_even_with_complete_env(self):
        path = Path(self.tmp) / "platforms.yaml"
        path.write_text("- item\n", encoding="utf-8")
        os.environ.update(self.env_complete)
        os.environ["BRAIN_WORKER_PLATFORMS_CONFIG"] = str(path)
        with self.assertRaisesRegex(RuntimeError, "根节点.*应为映射"):
            ledger.load_config()


class RequestTargetTests(unittest.TestCase):
    def test_fake_table_enters_request_target(self):
        paths = []
        params_seen = []

        def request(method, path, _bearer=None, body=None, params=None):
            paths.append((method, path))
            params_seen.append(params)
            return {"items": [], "has_more": False}

        with patch.object(ledger, "cfg", lambda: FAKE_CFG), \
             patch.object(ledger, "request", side_effect=request):
            ledger.records("tbl_events_fake", "bearer")
        self.assertEqual(
            paths,
            [("GET", "bitable/v1/apps/fake_app_token/tables/tbl_events_fake/records")],
        )
        self.assertEqual(params_seen, [{"page_size": 100}])

    def test_token_uses_configured_credentials(self):
        captured = {}

        def request(method, path, _bearer=None, body=None, params=None):
            captured["method"] = method
            captured["path"] = path
            captured["body"] = body
            return {"tenant_access_token": "tok"}

        with patch.object(ledger, "cfg", lambda: FAKE_CFG), \
             patch.object(ledger, "request", side_effect=request):
            tok = ledger.token()
        self.assertEqual(tok, "tok")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["path"], "auth/v3/tenant_access_token/internal")
        self.assertEqual(captured["body"], {"app_id": "cli_fake", "app_secret": "fake_secret"})


class LegacyDriftTests(unittest.TestCase):
    def test_default_disabled_does_not_read_any_file(self):
        with patch.object(ledger, "cfg", lambda: dict(FAKE_CFG, legacy_ledger=None)):
            self.assertIsNone(ledger.check_legacy_drift([]))

    def test_explicit_path_missing_returns_none(self):
        with patch.object(ledger, "cfg", lambda: dict(FAKE_CFG, legacy_ledger="/nonexistent/legacy.md")):
            self.assertIsNone(ledger.check_legacy_drift([]))

    def test_explicit_enabled_detects_drift(self):
        import hashlib
        with tempfile.NamedTemporaryFile("wb", suffix=".md", delete=False) as fh:
            fh.write("legacy content".encode("utf-8"))
            legacy_path = fh.name
        all_events = [{"fields": {"事件类型": "baseline", "迁移源哈希": hashlib.sha256(b"different").hexdigest()}}]
        with patch.object(ledger, "cfg", lambda: dict(FAKE_CFG, legacy_ledger=legacy_path)):
            note = ledger.check_legacy_drift(all_events)
        self.assertIn("迁移后的写入", note)

    def test_explicit_enabled_no_drift_when_hash_matches(self):
        import hashlib
        content = b"legacy content"
        with tempfile.NamedTemporaryFile("wb", suffix=".md", delete=False) as fh:
            fh.write(content)
            legacy_path = fh.name
        all_events = [{"fields": {"事件类型": "baseline", "迁移源哈希": hashlib.sha256(content).hexdigest()}}]
        with patch.object(ledger, "cfg", lambda: dict(FAKE_CFG, legacy_ledger=legacy_path)):
            note = ledger.check_legacy_drift(all_events)
        self.assertIsNone(note)


if __name__ == "__main__":
    unittest.main()
