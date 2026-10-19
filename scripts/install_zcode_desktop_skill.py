"""Install the compact brain-worker entry used by ZCode Desktop.

The ZCode Skill tool reads at most 100,000 bytes.  The canonical brain-worker
instructions are intentionally larger, so ZCode receives a compact adapter
that points at the same installed engine instead of a truncated copy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

MAX_ZCODE_SKILL_BYTES = 100_000
MANAGED_BY = "brain-worker-zcode-adapter"


def _default_engine_root() -> Path:
    configured = os.environ.get("BRAIN_WORKER_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".ccsc" / "codex-mimo" / "skills" / "brain-worker"


def _default_target_root() -> Path:
    return Path.home() / ".zcode" / "skills"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_engine(engine_root: Path) -> None:
    required = (
        "SKILL.md",
        "scripts/stage_selection.py",
        "scripts/dispatch_pool.py",
        "scripts/worker_status_widget.py",
    )
    missing = [item for item in required if not (engine_root / item).is_file()]
    if missing:
        raise ValueError(f"invalid brain-worker engine root; missing: {missing}")


def _validate_template(template: Path) -> None:
    if not template.is_file():
        raise ValueError(f"ZCode skill template not found: {template}")
    size = template.stat().st_size
    if size >= MAX_ZCODE_SKILL_BYTES:
        raise ValueError(
            f"ZCode skill template is {size} bytes; it must stay below "
            f"{MAX_ZCODE_SKILL_BYTES} bytes"
        )
    text = template.read_text(encoding="utf-8")
    if not text.startswith("---\n") or "\nname: brain-worker\n" not in text[:2048]:
        raise ValueError("ZCode skill template has invalid frontmatter")
    if "${ZCODE_SKILL_DIR}/engine.json" not in text:
        raise ValueError("ZCode skill template does not reference its engine manifest")


def _load_existing_manifest(destination: Path) -> dict | None:
    manifest = destination / "engine.json"
    if not manifest.is_file():
        return None
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"existing engine.json is unreadable: {exc}") from exc
    if value.get("managed_by") != MANAGED_BY:
        raise ValueError(
            f"refusing to overwrite unmanaged ZCode skill directory: {destination}"
        )
    return value


def install(*, source_root: Path, engine_root: Path, target_root: Path) -> dict:
    source_root = source_root.resolve()
    engine_root = engine_root.resolve()
    target_root = target_root.resolve()
    template = source_root / "zcode-skill" / "SKILL.md"
    destination = target_root / "brain-worker"

    _validate_engine(engine_root)
    _validate_template(template)
    if destination.exists():
        existing = _load_existing_manifest(destination)
        if existing is None:
            raise ValueError(
                f"refusing to overwrite unmanaged ZCode skill directory: {destination}"
            )

    target_root.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=".brain-worker-zcode-", dir=target_root))
    try:
        skill_target = temp_dir / "SKILL.md"
        shutil.copyfile(template, skill_target)
        payload = {
            "schema_version": 1,
            "managed_by": MANAGED_BY,
            "engine_root": str(engine_root),
            "source_root": str(source_root),
            "skill_sha256": _sha256(skill_target),
            "installed_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        (temp_dir / "engine.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if destination.exists():
            shutil.rmtree(destination)
        temp_dir.replace(destination)
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return {**payload, "destination": str(destination), "status": "installed"}


def check(*, source_root: Path, target_root: Path) -> dict:
    source_root = source_root.resolve()
    destination = target_root.resolve() / "brain-worker"
    template = source_root / "zcode-skill" / "SKILL.md"
    _validate_template(template)
    payload = _load_existing_manifest(destination)
    if payload is None:
        raise ValueError(f"ZCode brain-worker skill is not installed: {destination}")
    installed_skill = destination / "SKILL.md"
    if not installed_skill.is_file():
        raise ValueError(f"installed SKILL.md is missing: {installed_skill}")
    engine_root = Path(payload["engine_root"])
    _validate_engine(engine_root)
    expected = _sha256(template)
    actual = _sha256(installed_skill)
    if actual != expected or payload.get("skill_sha256") != actual:
        raise ValueError("installed ZCode skill differs from the repository template")
    return {
        "status": "ready",
        "destination": str(destination),
        "engine_root": str(engine_root.resolve()),
        "skill_bytes": installed_skill.stat().st_size,
        "skill_sha256": actual,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("install", "check"))
    parser.add_argument(
        "--source-root", type=Path, default=Path(__file__).resolve().parent.parent
    )
    parser.add_argument("--engine-root", type=Path, default=_default_engine_root())
    parser.add_argument("--target-root", type=Path, default=_default_target_root())
    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            result = install(
                source_root=args.source_root,
                engine_root=args.engine_root,
                target_root=args.target_root,
            )
        else:
            result = check(source_root=args.source_root, target_root=args.target_root)
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

