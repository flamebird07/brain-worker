import json
import re
import tempfile
import unittest
from pathlib import Path

from scripts import dispatch_pool as dp
from scripts import install_desktop_skill as installer


class DesktopSkillTestBase(unittest.TestCase):
    def setUp(self):
        self.repo_root = Path(__file__).resolve().parent.parent
        self.template = self.repo_root / "desktop-skill" / "SKILL.md"
        self.template_text = self.template.read_text(encoding="utf-8")

    def _make_engine(self, root: Path, name: str = "engine") -> Path:
        engine = root / name
        (engine / "scripts").mkdir(parents=True)
        (engine / "SKILL.md").write_text(
            "---\nname: brain-worker\n---\n", encoding="utf-8"
        )
        for script in ("stage_selection.py", "dispatch_pool.py", "worker_status_widget.py"):
            (engine / "scripts" / script).write_text("# test\n", encoding="utf-8")
        return engine

    def _source_root_with(self, root: Path, text: str) -> Path:
        """A source tree whose template is `text`, so one rule can be broken at a time."""
        source = root / "source"
        (source / "desktop-skill").mkdir(parents=True, exist_ok=True)
        (source / "desktop-skill" / "SKILL.md").write_text(text, encoding="utf-8")
        return source

    def _target_roots(self, root: Path) -> dict[str, Path]:
        return {
            "zcode": root / ".zcode" / "skills",
            "qoder": root / ".qoder-cn" / "skills",
        }

    def _install(self, root: Path, engine: Path, hosts=installer.HOSTS):
        return installer.install(
            source_root=self.repo_root,
            engine_root=engine,
            target_roots=self._target_roots(root),
            hosts=hosts,
        )


class TemplateContractTests(DesktopSkillTestBase):
    def test_template_meets_desktop_skill_contract(self):
        installer.validate_template_text(self.template_text)
        encoded = self.template_text.encode("utf-8")
        self.assertLess(len(encoded), installer.MAX_DESKTOP_SKILL_BYTES)
        self.assertTrue(self.template_text.startswith("---\n"))
        self.assertIn("name: brain-worker", self.template_text)
        self.assertIn(installer.ENGINE_ROOT_PLACEHOLDER, self.template_text)

    def test_template_names_both_hosts_in_one_file(self):
        for host in installer.HOSTS:
            for signal in installer.HOST_SIGNALS[host]:
                self.assertIn(signal, self.template_text)

    def test_zcode_signals_are_live_verified(self):
        # 2026-10-20 在真实 ZCode 桌面 3.16.2 进程实测：ZCODE_SKILL_DIR 并不存在
        # （进程环境与注册表 User/Machine 两级均无），押它会令宿主识别永远失败、
        # fail-closed 并集过度排除 qoder Max 三池。实存标记是这两个，禁止静默移除。
        self.assertIn("ZCODE_DESKTOP_CONTEXT_PROMPT_ENABLED",
                      installer.HOST_SIGNALS["zcode"])
        self.assertIn("ZCODE_APP_VERSION", installer.HOST_SIGNALS["zcode"])

    def test_template_does_not_depend_on_runtime_env_vars(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            # Keep the placeholder so the env-var rule is what fires, not the
            # missing-placeholder rule.
            broken = self.template_text.replace(
                "# Brain Worker Desktop Entry",
                "# Brain Worker Desktop Entry\n\n${QODER_SKILL_DIR}/engine.json",
                1,
            )
            source = self._source_root_with(root, broken)
            with self.assertRaisesRegex(ValueError, "environment variable"):
                installer._validate_template(source / "desktop-skill" / "SKILL.md")

    def test_template_rejects_missing_host_exclusion(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            broken = self.template_text.replace("qodercn:Qwen-3.8-Max", "", 1)
            source = self._source_root_with(root, broken)
            with self.assertRaisesRegex(ValueError, "omits qoder host exclusion"):
                installer._validate_template(source / "desktop-skill" / "SKILL.md")

    def test_template_rejects_missing_detection_signal(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            broken = self.template_text.replace("ZCODE_SKILL_DIR", "ZCODE_HOME", 1)
            source = self._source_root_with(root, broken)
            with self.assertRaisesRegex(ValueError, "omits zcode detection signal"):
                installer._validate_template(source / "desktop-skill" / "SKILL.md")

    def test_template_rejects_missing_fail_closed_rule(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            broken = self.template_text.replace("fail-closed", "best-effort", 1)
            source = self._source_root_with(root, broken)
            with self.assertRaisesRegex(ValueError, "fail-closed"):
                installer._validate_template(source / "desktop-skill" / "SKILL.md")


class StructuralDriftGuardTests(DesktopSkillTestBase):
    """The adapter may carry host-specific facts only.

    Priority order, the night window, per-pool capacity and the pool key list are
    enforced by shared code, so a literal for any of them here is a second copy
    that will silently disagree after the next engine change.
    """

    def test_shipped_template_restates_no_engine_owned_literal(self):
        for pattern, label in installer.DRIFT_PRONE_PATTERNS:
            hit = pattern.search(self.template_text)
            self.assertIsNone(hit, f"template restates engine-owned state ({label})")

    def test_rejects_time_window_literal(self):
        self._assert_drift_rejected("主力时段为 22:00 到 08:00。", "time-window literal")

    def test_rejects_capacity_number(self):
        self._assert_drift_rejected("国内总池 容量 16 个并发。", "capacity number")

    def test_rejects_slot_count(self):
        self._assert_drift_rejected("当前配置总 16 槽。", "slot count")

    def test_rejects_priority_number(self):
        self._assert_drift_rejected("codebuddy 的 priority = 0，最先尝试。", "priority number")

    def test_rejects_byte_size_claim(self):
        self._assert_drift_rejected("主 Skill 已超过 二十万 字节。", "byte-size claim")

    def _assert_drift_rejected(self, injected: str, label: str) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            broken = self.template_text.replace(
                "# Brain Worker Desktop Entry",
                f"# Brain Worker Desktop Entry\n\n{injected}",
                1,
            )
            source = self._source_root_with(root, broken)
            with self.assertRaisesRegex(ValueError, label):
                installer._validate_template(source / "desktop-skill" / "SKILL.md")


class GuardOnePoolKeyRealityTests(DesktopSkillTestBase):
    """G1: an exclusion naming a pool key the engine cannot dispatch is a lie."""

    def test_every_excluded_key_is_really_dispatchable(self):
        for host, candidates in installer.HOST_EXCLUDED.items():
            for candidate in candidates:
                with self.subTest(host=host, candidate=candidate):
                    self.assertIn(candidate, dp.CAPACITY)
                    self.assertNotIn(candidate, dp.LEGACY_RETIRED_POOL_KEYS)
                    self.assertNotEqual(candidate, dp.LUNA_KEY)
        installer.validate_host_exclusions()

    def test_union_is_deduplicated_and_covers_both_hosts(self):
        union = installer.HOST_EXCLUDED_UNION
        self.assertEqual(len(union), len(set(union)))
        for host in installer.HOSTS:
            for candidate in installer.HOST_EXCLUDED[host]:
                self.assertIn(candidate, union)

    def test_retired_zcode_worker_is_not_relisted_by_any_host(self):
        # BW-ZCODE-RETIRE-20261020：zcode:GLM-5.3 已由引擎全局退役，宿主排除集不再
        # （G1 也禁止）列出已退役键；Qoder 宿主的递归防护由引擎退役本身兜底，
        # 排除集只保留各自真实占用的 Qwen Max 通道。
        for host in installer.HOSTS:
            self.assertNotIn("zcode:GLM-5.3", installer.HOST_EXCLUDED[host])
        self.assertIn("qoder:Qwen3.8-Max", installer.HOST_EXCLUDED["qoder"])

    def test_rejects_exclusion_naming_a_renamed_pool_key(self):
        with self.assertRaisesRegex(ValueError, "renamed or retired"):
            installer.validate_host_exclusions({"zcode": ("zcode:GLM-9.9",)})

    def test_rejects_exclusion_naming_a_retired_pool_key(self):
        for retired in dp.LEGACY_RETIRED_POOL_KEYS:
            with self.assertRaisesRegex(ValueError, "renamed or retired"):
                installer.validate_host_exclusions({"zcode": (retired,)})

    def test_rejects_exclusion_naming_luna(self):
        with self.assertRaisesRegex(ValueError, "renamed or retired"):
            installer.validate_host_exclusions({"zcode": (dp.LUNA_KEY,)})

    def test_install_and_check_both_enforce_the_guard(self):
        """A stale exclusion must break the install, not merely the test suite."""
        original = installer.HOST_EXCLUDED
        try:
            with tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                engine = self._make_engine(root)
                roots = self._target_roots(root)

                installer.HOST_EXCLUDED = {
                    "zcode": ("zcode:GLM-9.9",),
                    "qoder": original["qoder"],
                }
                with self.assertRaisesRegex(ValueError, "renamed or retired"):
                    self._install(root, engine)

                # A clean install, then the same rename: check has to notice too,
                # otherwise an already-installed host keeps lying about its pools.
                installer.HOST_EXCLUDED = original
                self._install(root, engine)
                installer.HOST_EXCLUDED = {
                    "zcode": ("zcode:GLM-9.9",),
                    "qoder": original["qoder"],
                }
                with self.assertRaisesRegex(ValueError, "renamed or retired"):
                    installer.check(source_root=self.repo_root, target_roots=roots)
        finally:
            installer.HOST_EXCLUDED = original


class GuardTwoLunaParityTests(DesktopSkillTestBase):
    """G2: the Luna disablement must stay in step with the canonical Skill."""

    LUNA_MARKER = "Luna 已删除并强制禁用"

    def test_canonical_still_carries_the_luna_rule(self):
        canonical = (self.repo_root / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn(
            self.LUNA_MARKER,
            canonical,
            "canonical SKILL.md no longer states the Luna disablement. If that "
            "removal was deliberate, update desktop-skill/SKILL.md and this test "
            "in the same change; do not let the adapter keep a rule the engine "
            "dropped.",
        )

    def test_adapter_carries_the_same_luna_rule(self):
        self.assertIn(self.LUNA_MARKER, self.template_text)

    def test_template_rejects_a_dropped_luna_rule(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            broken = self.template_text.replace(self.LUNA_MARKER, "Luna 可用", 1)
            source = self._source_root_with(root, broken)
            with self.assertRaisesRegex(ValueError, "Luna"):
                installer._validate_template(source / "desktop-skill" / "SKILL.md")


class GuardThreeByteIdenticalInstallTests(DesktopSkillTestBase):
    """G3: both hosts get the same bytes, and drift in either one is detected."""

    def test_install_writes_byte_identical_skill_to_both_hosts(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            engine = self._make_engine(root)
            result = self._install(root, engine)
            roots = self._target_roots(root)
            blobs = [
                (roots[h] / "brain-worker" / "SKILL.md").read_bytes()
                for h in installer.HOSTS
            ]
            self.assertEqual(blobs[0], blobs[1])
            self.assertEqual(result["skill_sha256"], installer._sha256_bytes(blobs[0]))
            checked = installer.check(
                source_root=self.repo_root, target_roots=roots
            )
            self.assertTrue(checked["byte_identical_across_hosts"])

    def test_check_detects_drift_in_each_host(self):
        for host in installer.HOSTS:
            with self.subTest(host=host), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                engine = self._make_engine(root)
                self._install(root, engine)
                roots = self._target_roots(root)
                skill = roots[host] / "brain-worker" / "SKILL.md"
                skill.write_text(
                    skill.read_text(encoding="utf-8") + "\nlocal edit\n", encoding="utf-8"
                )
                with self.assertRaisesRegex(ValueError, f"installed {host} desktop skill differs"):
                    installer.check(source_root=self.repo_root, target_roots=roots)

    def test_check_detects_divergence_between_hosts(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._install(root, self._make_engine(root, "engineA"))
            # A second engine root renders different bytes; installing it for one
            # host only is exactly the fork this guard exists to catch.
            self._install(root, self._make_engine(root, "engineB"), hosts=("qoder",))
            with self.assertRaisesRegex(ValueError, "divergent skill bytes"):
                installer.check(
                    source_root=self.repo_root, target_roots=self._target_roots(root)
                )

    def test_check_detects_engine_root_change(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._install(root, self._make_engine(root, "engineA"))
            engine_b = self._make_engine(root, "engineB")
            roots = self._target_roots(root)
            manifest = roots["zcode"] / "brain-worker" / "engine.json"
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["engine_root"] = str(engine_b.resolve())
            manifest.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "differs"):
                installer.check(source_root=self.repo_root, target_roots=roots)

    def test_check_reports_missing_install(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with self.assertRaisesRegex(ValueError, "not installed"):
                installer.check(
                    source_root=self.repo_root, target_roots=self._target_roots(root)
                )

    def test_reinstall_is_idempotent(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            engine = self._make_engine(root)
            first = self._install(root, engine)
            second = self._install(root, engine)
            self.assertEqual(first["skill_sha256"], second["skill_sha256"])
            self.assertEqual(first["template_sha256"], second["template_sha256"])


class InstallSafetyTests(DesktopSkillTestBase):
    def test_install_renders_absolute_engine_root_without_copying_engine(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            engine = self._make_engine(root)
            self._install(root, engine)
            destination = self._target_roots(root)["qoder"] / "brain-worker"
            text = (destination / "SKILL.md").read_text(encoding="utf-8")
            self.assertNotIn(installer.ENGINE_ROOT_PLACEHOLDER, text)
            self.assertIn(str(engine.resolve()), text)
            self.assertFalse((destination / "scripts").exists())
            payload = json.loads((destination / "engine.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["managed_by"], installer.MANAGED_BY)
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(payload["engine_root"], str(engine.resolve()))

    def test_rendered_body_stays_within_line_budget(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            engine = self._make_engine(root)
            self._install(root, engine)
            skill = self._target_roots(root)["qoder"] / "brain-worker" / "SKILL.md"
            text = skill.read_text(encoding="utf-8")
            body = re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.DOTALL)
            self.assertLessEqual(len(body.splitlines()), installer.MAX_BODY_LINES)

    def test_refuses_to_overwrite_unmanaged_skill(self):
        for host in installer.HOSTS:
            with self.subTest(host=host), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                engine = self._make_engine(root)
                destination = self._target_roots(root)[host] / "brain-worker"
                destination.mkdir(parents=True)
                (destination / "SKILL.md").write_text("user content\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "unmanaged"):
                    self._install(root, engine)

    def test_migrates_legacy_per_host_manifests(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            engine = self._make_engine(root)
            roots = self._target_roots(root)
            for host, legacy in zip(
                installer.HOSTS, ("brain-worker-zcode-adapter", "brain-worker-qoder-adapter")
            ):
                destination = roots[host] / "brain-worker"
                destination.mkdir(parents=True)
                (destination / "SKILL.md").write_text("old adapter\n", encoding="utf-8")
                (destination / "engine.json").write_text(
                    json.dumps({"managed_by": legacy, "engine_root": str(engine.resolve())}),
                    encoding="utf-8",
                )
            self._install(root, engine)
            for host in installer.HOSTS:
                payload = json.loads(
                    (roots[host] / "brain-worker" / "engine.json").read_text(encoding="utf-8")
                )
                self.assertEqual(payload["managed_by"], installer.MANAGED_BY)

    def test_rejects_unknown_host(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            engine = self._make_engine(root)
            with self.assertRaisesRegex(ValueError, "unknown desktop hosts"):
                installer.install(
                    source_root=self.repo_root,
                    engine_root=engine,
                    target_roots=self._target_roots(root),
                    hosts=("codex",),
                )

    def test_rejects_invalid_engine_root(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with self.assertRaisesRegex(ValueError, "invalid brain-worker engine root"):
                installer.install(
                    source_root=self.repo_root,
                    engine_root=root / "nowhere",
                    target_roots=self._target_roots(root),
                )


class SingleSourceTests(DesktopSkillTestBase):
    """The user's requirement: one version serving all hosts, not a fork per host."""

    def test_no_per_host_adapter_forks_remain(self):
        stale = [
            self.repo_root / "zcode-skill",
            self.repo_root / "qoder-skill",
            self.repo_root / "scripts" / "install_zcode_desktop_skill.py",
            self.repo_root / "scripts" / "install_qoder_desktop_skill.py",
            self.repo_root / "tests" / "test_zcode_desktop_skill.py",
            self.repo_root / "tests" / "test_qoder_desktop_skill.py",
        ]
        present = [str(p) for p in stale if p.exists()]
        self.assertEqual(
            present,
            [],
            f"per-host adapter forks must be deleted, still present: {present}",
        )

    def test_both_hosts_install_from_the_same_template_path(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            engine = self._make_engine(root)
            result = self._install(root, engine)
            for host in installer.HOSTS:
                payload = json.loads(
                    (self._target_roots(root)[host] / "brain-worker" / "engine.json")
                    .read_text(encoding="utf-8")
                )
                self.assertEqual(payload["template_sha256"], result["template_sha256"])

    def test_partial_host_install_still_records_the_full_exclusion_table(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            engine = self._make_engine(root)
            self._install(root, engine, hosts=("qoder",))
            payload = json.loads(
                (self._target_roots(root)["qoder"] / "brain-worker" / "engine.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(list(payload["host_excluded_candidates"]), ["qoder"])
            self.assertEqual(
                payload["host_excluded_union"], list(installer.HOST_EXCLUDED_UNION)
            )


class CommandLineTests(DesktopSkillTestBase):
    def test_cli_check_reports_ready_after_install(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            engine = self._make_engine(root)
            roots = self._target_roots(root)
            self.assertEqual(
                installer.main([
                    "install",
                    "--source-root", str(self.repo_root),
                    "--engine-root", str(engine),
                    "--zcode-root", str(roots["zcode"]),
                    "--qoder-root", str(roots["qoder"]),
                ]),
                0,
            )
            self.assertEqual(
                installer.main([
                    "check",
                    "--source-root", str(self.repo_root),
                    "--zcode-root", str(roots["zcode"]),
                    "--qoder-root", str(roots["qoder"]),
                ]),
                0,
            )

    def test_cli_returns_error_status_without_raising(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            exit_code = installer.main([
                "check",
                "--source-root", str(self.repo_root),
                "--zcode-root", str(root / "missing-zcode"),
                "--qoder-root", str(root / "missing-qoder"),
            ])
            self.assertEqual(exit_code, 2)

    def test_cli_rejects_empty_host_list(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.assertEqual(
                installer.main([
                    "install",
                    "--source-root", str(self.repo_root),
                    "--hosts", "",
                    "--zcode-root", str(root / "z"),
                    "--qoder-root", str(root / "q"),
                ]),
                2,
            )


if __name__ == "__main__":
    unittest.main()
