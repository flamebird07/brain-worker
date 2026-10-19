import json
import tempfile
import unittest
from pathlib import Path

from scripts import install_zcode_desktop_skill as installer


class ZCodeDesktopSkillTests(unittest.TestCase):
    def setUp(self):
        self.repo_root = Path(__file__).resolve().parent.parent

    def _make_engine(self, root: Path) -> Path:
        engine = root / "engine"
        (engine / "scripts").mkdir(parents=True)
        (engine / "SKILL.md").write_text("---\nname: brain-worker\n---\n", encoding="utf-8")
        for name in ("stage_selection.py", "dispatch_pool.py", "worker_status_widget.py"):
            (engine / "scripts" / name).write_text("# test\n", encoding="utf-8")
        return engine

    def test_template_is_native_zcode_skill_and_fits_budget(self):
        skill = self.repo_root / "zcode-skill" / "SKILL.md"
        installer._validate_template(skill)
        self.assertLess(skill.stat().st_size, installer.MAX_ZCODE_SKILL_BYTES)
        text = skill.read_text(encoding="utf-8")
        self.assertIn("${ZCODE_SKILL_DIR}/engine.json", text)
        self.assertIn("/brain-worker", text)
        self.assertIn('host_excluded_candidates=["zcode:GLM-5.3"]', text)
        self.assertIn("Luna 已删除并强制禁用", text)

    def test_install_and_check_use_same_engine_without_copying_it(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            engine = self._make_engine(root)
            target = root / ".zcode" / "skills"
            result = installer.install(
                source_root=self.repo_root, engine_root=engine, target_root=target
            )
            self.assertEqual(result["status"], "installed")
            destination = target / "brain-worker"
            payload = json.loads((destination / "engine.json").read_text(encoding="utf-8"))
            self.assertEqual(Path(payload["engine_root"]), engine.resolve())
            self.assertFalse((destination / "scripts").exists())
            checked = installer.check(source_root=self.repo_root, target_root=target)
            self.assertEqual(checked["status"], "ready")

    def test_refuses_to_overwrite_unmanaged_skill(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            engine = self._make_engine(root)
            destination = root / ".zcode" / "skills" / "brain-worker"
            destination.mkdir(parents=True)
            (destination / "SKILL.md").write_text("user content\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unmanaged"):
                installer.install(
                    source_root=self.repo_root,
                    engine_root=engine,
                    target_root=root / ".zcode" / "skills",
                )

    def test_check_detects_template_drift(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            engine = self._make_engine(root)
            target = root / ".zcode" / "skills"
            installer.install(source_root=self.repo_root, engine_root=engine, target_root=target)
            skill = target / "brain-worker" / "SKILL.md"
            skill.write_text(skill.read_text(encoding="utf-8") + "drift\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "differs"):
                installer.check(source_root=self.repo_root, target_root=target)


if __name__ == "__main__":
    unittest.main()
