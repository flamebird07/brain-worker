"""Install the single shared brain-worker desktop entry for ZCode and Qoder.

One host-agnostic template is rendered once and installed byte-identically into
both desktop hosts' skill directories, so a rule change upgrades ZCode and
Qoder together.  Neither host exposes a shared skill-directory variable, so the
engine root is rendered in as a literal path.

The canonical brain-worker instructions stay authoritative for the Codex host
and are deliberately not copied here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

MAX_DESKTOP_SKILL_BYTES = 100_000
MAX_BODY_LINES = 500
MAX_DESCRIPTION_CHARS = 1024
ENGINE_ROOT_PLACEHOLDER = "{{BRAIN_WORKER_ENGINE_ROOT}}"
MANAGED_BY = "brain-worker-desktop-adapter"
# Earlier per-host adapters are recognised so an install can migrate them
# instead of refusing to touch a directory this script family created.
LEGACY_MANAGED_BY = ("brain-worker-zcode-adapter", "brain-worker-qoder-adapter")

HOSTS = ("zcode", "qoder")

HOST_EXCLUDED = {
    "qoder": (
        "qoder:Qwen3.8-Max",
        "qodercn:Qwen-3.8-Max",
        "qodercn:Qwen3.8-Max",
        "zcode:GLM-5.3",
    ),
    "zcode": ("zcode:GLM-5.3",),
}
# Fail-closed set applied when the host cannot be identified at runtime.
HOST_EXCLUDED_UNION = tuple(
    dict.fromkeys(sum(HOST_EXCLUDED.values(), ()))
)

HOST_SIGNALS = {
    "qoder": ("QODER_CONFIG_DIR", "QODERCN_CONFIG_DIR", "QODER_PRODUCT_ID"),
    # ZCODE_SKILL_DIR 在真实宿主进程并不存在（2026-10-20 实测 ZCode 桌面 3.16.2 进程
    # 及其子进程环境，注册表两级也无），仅作前向兼容保留；实存标记是后两个，由桌面
    # 应用注入并继承到子进程。识别失败会 fail-closed 取并集，把 qoder Max 三池一并
    # 排除、过度削弱 ZCode 宿主，因此必须用实测存在的信号。
    "zcode": ("ZCODE_SKILL_DIR", "ZCODE_DESKTOP_CONTEXT_PROMPT_ENABLED",
              "ZCODE_APP_VERSION"),
}

# The adapter must carry only host-specific information.  Anything the shared
# engine already enforces is a drift hazard if restated here, so literal values
# for it are rejected outright.
DRIFT_PRONE_PATTERNS = (
    (re.compile(r"\d{1,2}:\d{2}"), "time-window literal"),
    (re.compile(r"容量\s*\d+"), "capacity number"),
    (re.compile(r"\d+\s*槽"), "slot count"),
    (re.compile(r"总容量|合计\s*\d+"), "total capacity figure"),
    (re.compile(r"priority\s*[=:]?\s*\d+", re.IGNORECASE), "priority number"),
    (re.compile(r"\b\d{4,}\s*字节|二十万|十万字节"), "byte-size claim"),
    (re.compile(r"1[0-9]{2},?[0-9]{3}"), "byte-count figure"),
)

LUNA_RULE = "Luna 已删除并强制禁用"


def _default_engine_root() -> Path:
    configured = os.environ.get("BRAIN_WORKER_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".ccsc" / "codex-mimo" / "skills" / "brain-worker"


def _default_target_roots() -> dict[str, Path]:
    home = Path.home()
    return {
        "zcode": home / ".zcode" / "skills",
        "qoder": home / ".qoder-cn" / "skills",
    }


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


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


def _validate_frontmatter(text: str) -> None:
    if not text.startswith("---\n"):
        raise ValueError("desktop skill template must start with YAML frontmatter")
    match = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
    if match is None:
        raise ValueError("desktop skill template has unterminated frontmatter")
    block = match.group(1)
    if "\nname: brain-worker\n" not in f"\n{block}\n":
        raise ValueError("desktop skill template must declare name: brain-worker")
    description = re.search(r"^description:[ \t]*(.*)$", block, re.MULTILINE)
    if description is None:
        raise ValueError("desktop skill template is missing a description")
    if len(description.group(1)) > MAX_DESCRIPTION_CHARS:
        raise ValueError(
            f"description is {len(description.group(1))} chars; at most "
            f"{MAX_DESCRIPTION_CHARS} allowed"
        )


def validate_host_exclusions(host_excluded: dict[str, tuple[str, ...]] | None = None) -> None:
    """Reject any exclusion naming a pool key the engine cannot dispatch.

    The whole point of the exclusion table is to narrow a real candidate set.
    If the engine renames or retires a pool key, a stale entry here would
    silently exclude nothing while looking authoritative, so the mismatch has to
    fail loudly at install time instead of at dispatch time.
    """
    mapping = HOST_EXCLUDED if host_excluded is None else host_excluded
    try:
        from scripts import dispatch_pool as dp
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from scripts import dispatch_pool as dp

    stale: list[str] = []
    for host, candidates in mapping.items():
        for candidate in candidates:
            dispatchable = (
                candidate in dp.CAPACITY
                and candidate not in dp.LEGACY_RETIRED_POOL_KEYS
                and candidate != dp.LUNA_KEY
            )
            if not dispatchable:
                stale.append(f"{host}: {candidate}")
    if stale:
        raise ValueError(
            "desktop skill excludes pool keys the engine cannot dispatch "
            f"(renamed or retired?): {stale}"
        )


def validate_template_text(text: str) -> None:
    """Host-agnostic contract checks; shared by install, check and the tests."""
    _validate_frontmatter(text)
    if ENGINE_ROOT_PLACEHOLDER not in text:
        raise ValueError("desktop skill template does not declare its engine root placeholder")
    if "${" in text:
        raise ValueError(
            "desktop skill template must not depend on runtime environment variable expansion"
        )
    # Validate the table against the engine before checking that the prose carries
    # it, so a renamed pool key reports itself instead of an "omitted" exclusion.
    validate_host_exclusions()
    for host in HOSTS:
        for candidate in HOST_EXCLUDED[host]:
            if candidate not in text:
                raise ValueError(
                    f"desktop skill template omits {host} host exclusion: {candidate}"
                )
        for signal in HOST_SIGNALS[host]:
            if signal not in text:
                raise ValueError(
                    f"desktop skill template omits {host} detection signal: {signal}"
                )
    if "fail-closed" not in text:
        raise ValueError("desktop skill template must keep the fail-closed unknown-host rule")
    if LUNA_RULE not in text:
        raise ValueError("desktop skill template must keep the Luna disablement rule")
    for pattern, label in DRIFT_PRONE_PATTERNS:
        hit = pattern.search(text)
        if hit:
            raise ValueError(
                f"desktop skill template restates engine-owned state ({label}): "
                f"{hit.group(0)!r}"
            )


def _validate_template(template: Path) -> str:
    if not template.is_file():
        raise ValueError(f"desktop skill template not found: {template}")
    text = template.read_text(encoding="utf-8")
    validate_template_text(text)
    return text


def _validate_rendered(text: str) -> None:
    encoded = text.encode("utf-8")
    if len(encoded) >= MAX_DESKTOP_SKILL_BYTES:
        raise ValueError(
            f"rendered desktop skill is {len(encoded)} bytes; it must stay below "
            f"{MAX_DESKTOP_SKILL_BYTES} bytes"
        )
    if ENGINE_ROOT_PLACEHOLDER in text:
        raise ValueError("rendered desktop skill still contains an unresolved placeholder")
    body = re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.DOTALL)
    lines = len(body.splitlines())
    if lines > MAX_BODY_LINES:
        raise ValueError(
            f"rendered desktop skill body is {lines} lines; at most {MAX_BODY_LINES} "
            f"recommended"
        )


def _render(template_text: str, engine_root: Path) -> str:
    rendered = template_text.replace(ENGINE_ROOT_PLACEHOLDER, str(engine_root))
    _validate_rendered(rendered)
    return rendered


def _load_existing_manifest(destination: Path) -> dict | None:
    manifest = destination / "engine.json"
    if not manifest.is_file():
        return None
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"existing engine.json is unreadable: {exc}") from exc
    if value.get("managed_by") not in (MANAGED_BY, *LEGACY_MANAGED_BY):
        raise ValueError(
            f"refusing to overwrite unmanaged desktop skill directory: {destination}"
        )
    return value


def _guard_destination(destination: Path) -> None:
    if destination.exists() and _load_existing_manifest(destination) is None:
        raise ValueError(
            f"refusing to overwrite unmanaged desktop skill directory: {destination}"
        )


def _install_one(*, rendered: str, payload: dict, target_root: Path) -> dict:
    destination = target_root / "brain-worker"
    _guard_destination(destination)
    target_root.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=".brain-worker-desktop-", dir=target_root))
    try:
        skill_target = temp_dir / "SKILL.md"
        skill_target.write_bytes(rendered.encode("utf-8"))
        record = dict(payload)
        record["skill_sha256"] = _sha256(skill_target)
        (temp_dir / "engine.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if destination.exists():
            shutil.rmtree(destination)
        temp_dir.replace(destination)
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return {**record, "destination": str(destination)}


def install(*, source_root: Path, engine_root: Path, target_roots: dict[str, Path],
            hosts: tuple[str, ...] = HOSTS) -> dict:
    source_root = source_root.resolve()
    engine_root = engine_root.resolve()
    unknown = [h for h in hosts if h not in HOSTS]
    if unknown:
        raise ValueError(f"unknown desktop hosts: {unknown}")
    template = source_root / "desktop-skill" / "SKILL.md"

    _validate_engine(engine_root)
    template_text = _validate_template(template)
    rendered = _render(template_text, engine_root)

    payload = {
        "schema_version": 2,
        "managed_by": MANAGED_BY,
        "engine_root": str(engine_root),
        "source_root": str(source_root),
        "hosts": list(hosts),
        "host_excluded_candidates": {h: list(HOST_EXCLUDED[h]) for h in hosts},
        "host_excluded_union": list(HOST_EXCLUDED_UNION),
        "template_sha256": _sha256(template),
        "installed_at_utc": datetime.now(timezone.utc).isoformat(),
    }

    installed = {}
    for host in hosts:
        root = Path(target_roots[host]).resolve()
        installed[host] = _install_one(rendered=rendered, payload=payload, target_root=root)

    digests = {h: rec["skill_sha256"] for h, rec in installed.items()}
    if len(set(digests.values())) != 1:
        raise ValueError(f"hosts received divergent skill bytes: {digests}")

    return {
        **payload,
        "skill_sha256": digests[hosts[0]],
        "installed": installed,
        "status": "installed",
    }


def check(*, source_root: Path, target_roots: dict[str, Path],
          hosts: tuple[str, ...] = HOSTS) -> dict:
    source_root = source_root.resolve()
    template = source_root / "desktop-skill" / "SKILL.md"
    template_text = _validate_template(template)

    checked = {}
    digests = {}
    for host in hosts:
        destination = Path(target_roots[host]).resolve() / "brain-worker"
        payload = _load_existing_manifest(destination)
        if payload is None:
            raise ValueError(f"{host} brain-worker desktop skill is not installed: {destination}")
        installed_skill = destination / "SKILL.md"
        if not installed_skill.is_file():
            raise ValueError(f"installed SKILL.md is missing: {installed_skill}")
        engine_root = Path(payload["engine_root"])
        _validate_engine(engine_root)
        expected = _sha256_bytes(_render(template_text, engine_root).encode("utf-8"))
        actual = _sha256(installed_skill)
        if actual != expected or payload.get("skill_sha256") != actual:
            raise ValueError(
                f"installed {host} desktop skill differs from the repository template"
            )
        digests[host] = actual
        checked[host] = {
            "status": "ready",
            "destination": str(destination),
            "engine_root": str(engine_root.resolve()),
            "managed_by": payload.get("managed_by"),
            "skill_bytes": installed_skill.stat().st_size,
            "skill_sha256": actual,
        }

    if len(set(digests.values())) != 1:
        raise ValueError(f"hosts hold divergent skill bytes: {digests}")

    return {
        "status": "ready",
        "hosts": list(hosts),
        "skill_sha256": digests[hosts[0]],
        "byte_identical_across_hosts": True,
        "checked": checked,
    }


def _resolve_target_roots(args) -> dict[str, Path]:
    roots = _default_target_roots()
    if args.zcode_root is not None:
        roots["zcode"] = Path(args.zcode_root)
    if args.qoder_root is not None:
        roots["qoder"] = Path(args.qoder_root)
    return roots


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("install", "check"))
    parser.add_argument(
        "--source-root", type=Path, default=Path(__file__).resolve().parent.parent
    )
    parser.add_argument("--engine-root", type=Path, default=_default_engine_root())
    parser.add_argument("--zcode-root", dest="zcode_root", type=Path, default=None)
    parser.add_argument("--qoder-root", dest="qoder_root", type=Path, default=None)
    parser.add_argument(
        "--hosts", default=",".join(HOSTS),
        help=f"comma-separated subset of {','.join(HOSTS)}",
    )
    args = parser.parse_args(argv)
    hosts = tuple(h.strip() for h in args.hosts.split(",") if h.strip())
    try:
        if not hosts:
            raise ValueError("no hosts selected")
        target_roots = _resolve_target_roots(args)
        if args.command == "install":
            result = install(
                source_root=args.source_root,
                engine_root=args.engine_root,
                target_roots=target_roots,
                hosts=hosts,
            )
        else:
            result = check(
                source_root=args.source_root,
                target_roots=target_roots,
                hosts=hosts,
            )
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
