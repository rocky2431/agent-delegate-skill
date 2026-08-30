#!/usr/bin/env python3
"""Install the pinned agent-delegation runtime and Skill at user scope."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any


VERSION = "0.1.1"
REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_SOURCE = (
    REPO_ROOT
    / "plugins"
    / "agent-delegation"
    / "skills"
    / "agent-delegation"
)
RUNTIME_SOURCE = REPO_ROOT / "runtime"
HOSTS = ("hermes", "claude", "codex", "kimi", "zcode", "opencode")
MANAGED_TARGETS = set(HOSTS)
RUNTIME_PACKAGES = {
    "acpx": "0.13.2",
    "@agentclientprotocol/claude-agent-acp": "0.70.0",
    "@agentclientprotocol/codex-acp": "1.7.0",
}


class InstallError(RuntimeError):
    """A user-facing installation failure."""


def _skill_destination(home: Path, host: str) -> Path:
    roots = {
        "hermes": home / ".hermes" / "skills",
        "claude": home / ".claude" / "skills",
        "codex": home / ".agents" / "skills",
        "kimi": home / ".kimi" / "skills",
        "zcode": home / ".zcode" / "skills",
        "opencode": home / ".config" / "opencode" / "skills",
    }
    return roots[host] / "agent-delegation"


def _parse_hosts(raw: str) -> list[str]:
    if raw.strip().lower() == "none":
        return []
    hosts: list[str] = []
    for item in raw.split(","):
        host = item.strip().lower()
        if not host or host in hosts:
            continue
        if host not in HOSTS:
            raise InstallError(f"Unsupported host {host!r}; choose from {', '.join(HOSTS)}, or none.")
        hosts.append(host)
    if not hosts:
        raise InstallError("At least one host is required, or pass --hosts none.")
    return hosts


def _atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _atomic_write_json(path: Path, value: dict[str, Any], mode: int = 0o600) -> None:
    _atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        mode,
    )


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InstallError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InstallError(f"Expected a JSON object in {path}.")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _managed_marker(path: Path) -> Path:
    return path / ".agent-delegation-managed.json"


def _is_managed_skill(path: Path) -> bool:
    try:
        marker = _read_json_object(_managed_marker(path))
    except InstallError:
        return False
    return marker.get("package") == "agent-delegation"


def _backup_item(source: Path, destination: Path) -> None:
    if not source.exists() and not source.is_symlink():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir() and not source.is_symlink():
        shutil.copytree(source, destination, symlinks=True)
    else:
        shutil.copy2(source, destination, follow_symlinks=False)


def _new_backup_dir(home: Path, operation: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = home / ".local" / "state" / "agent-delegation" / "backups" / f"{stamp}-{operation}"
    path.mkdir(parents=True, exist_ok=False, mode=0o700)
    return path


def _copy_skill(
    destination: Path,
    backup: Path,
    backup_name: str,
    replace_existing: bool,
) -> None:
    if destination.exists() or destination.is_symlink():
        if not _is_managed_skill(destination) and not replace_existing:
            raise InstallError(
                f"Refusing to replace unmanaged Skill {destination}; use --replace-existing after review."
            )
        _backup_item(destination, backup / "skills" / backup_name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging_parent = Path(tempfile.mkdtemp(prefix=".agent-delegation-", dir=destination.parent))
    staged = staging_parent / destination.name
    retired = staging_parent / "previous"
    try:
        shutil.copytree(
            SKILL_SOURCE,
            staged,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        _atomic_write_json(
            _managed_marker(staged),
            {
                "package": "agent-delegation",
                "version": VERSION,
                "installed_at": datetime.now(UTC).isoformat(),
            },
        )
        script = staged / "scripts" / "agent_delegate.py"
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        if destination.exists() or destination.is_symlink():
            destination.rename(retired)
        staged.rename(destination)
    except Exception:
        if not destination.exists() and retired.exists():
            retired.rename(destination)
        raise
    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)


def _resolve_executable(home: Path, candidates: list[Path], names: list[str]) -> Path:
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    for name in names:
        resolved = shutil.which(name)
        if resolved:
            path = Path(resolved).resolve()
            if path.is_file() and os.access(path, os.X_OK):
                return path
    readable = ", ".join(str(path) for path in candidates) or ", ".join(names)
    raise InstallError(f"Required executable was not found: {readable}")


def _version_line(argv: list[str]) -> str:
    try:
        completed = subprocess.run(
            argv,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallError(f"Version probe failed for {argv[0]}: {exc}") from exc
    if completed.returncode != 0:
        raise InstallError(f"Version probe failed for {argv[0]} with exit {completed.returncode}.")
    lines = (completed.stdout + completed.stderr).strip().splitlines()
    if not lines:
        raise InstallError(f"Version probe returned no output for {argv[0]}.")
    return lines[0]


def _install_runtime(home: Path, backup: Path, replace_existing: bool) -> tuple[Path, Path]:
    share_root = home / ".local" / "share" / "agent-delegation"
    marker_path = share_root / ".managed.json"
    if share_root.exists() and not marker_path.exists() and not replace_existing:
        raise InstallError(
            f"Refusing to replace unmanaged runtime root {share_root}; use --replace-existing after review."
        )
    runtime_root = share_root / "runtime"
    for name in ("package.json", "package-lock.json"):
        existing = runtime_root / name
        if existing.exists():
            _backup_item(existing, backup / "runtime" / name)
    runtime_root.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "package-lock.json"):
        source = RUNTIME_SOURCE / name
        if not source.is_file():
            raise InstallError(f"Missing reviewed runtime file {source}.")
        _atomic_write_text(runtime_root / name, source.read_text(encoding="utf-8"), 0o644)
    npm = shutil.which("npm")
    if not npm:
        raise InstallError("npm is required to install the pinned ACPX runtime.")
    node = shutil.which("node")
    if not node:
        raise InstallError("Node.js is required to install the pinned ACPX runtime.")
    node_version = _version_line([node, "--version"])
    try:
        node_parts = [int(part) for part in node_version.lstrip("v").split(".")[:2]]
        node_major, node_minor = node_parts
    except (IndexError, ValueError) as exc:
        raise InstallError(f"Cannot parse Node.js version {node_version!r}.") from exc
    if node_major < 22 or (node_major == 22 and node_minor < 13):
        raise InstallError(f"ACPX 0.13.2 requires Node.js >=22.13; observed {node_version}.")
    completed = subprocess.run(
        [npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=runtime_root,
        text=True,
        capture_output=True,
        timeout=300,
        check=False,
    )
    if completed.returncode != 0:
        raise InstallError(f"Pinned runtime npm ci failed:\n{completed.stderr[-4000:]}")
    _atomic_write_json(
        marker_path,
        {
            "package": "agent-delegation",
            "version": VERSION,
            "runtime_lock_sha256": _sha256(runtime_root / "package-lock.json"),
        },
    )
    return share_root, runtime_root


def _build_managed_targets(home: Path, runtime_root: Path) -> dict[str, dict[str, Any]]:
    hermes = _resolve_executable(home, [home / ".local" / "bin" / "hermes"], ["hermes"])
    claude = _resolve_executable(home, [home / ".local" / "bin" / "claude"], ["claude"])
    codex = _resolve_executable(home, [home / ".local" / "bin" / "codex"], ["codex"])
    kimi = _resolve_executable(home, [home / ".kimi-code" / "bin" / "kimi"], ["kimi"])
    zcode_acp = _resolve_executable(home, [home / ".local" / "bin" / "zcode-acp"], ["zcode-acp"])
    opencode = _resolve_executable(home, [home / ".opencode" / "bin" / "opencode"], ["opencode"])
    node = _resolve_executable(home, [], ["node"])
    claude_acp = (runtime_root / "node_modules" / ".bin" / "claude-agent-acp").resolve()
    codex_acp = (runtime_root / "node_modules" / ".bin" / "codex-acp").resolve()
    for adapter in (claude_acp, codex_acp):
        if not adapter.is_file() or not os.access(adapter, os.X_OK):
            raise InstallError(f"Pinned ACP adapter is missing or not executable: {adapter}")
    zcode_bundle = Path("/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs")
    if not zcode_bundle.is_file():
        raise InstallError(f"ZCode CLI bundle is missing: {zcode_bundle}")

    return {
        "hermes": {
            "argv": [str(hermes), "acp"],
            "version_argv": [str(hermes), "--version"],
            "observed_version": _version_line([str(hermes), "--version"]),
            "provenance": "existing local Hermes Agent installation",
        },
        "claude": {
            "argv": [str(claude_acp)],
            "version_argv": [str(claude), "--version"],
            "observed_version": _version_line([str(claude), "--version"]),
            "provenance": "@agentclientprotocol/claude-agent-acp@0.70.0 with existing Claude Code",
        },
        "codex": {
            "argv": [str(codex_acp)],
            "version_argv": [str(codex), "--version"],
            "observed_version": _version_line([str(codex), "--version"]),
            "provenance": "@agentclientprotocol/codex-acp@1.7.0 with existing Codex CLI",
        },
        "kimi": {
            "argv": [str(kimi), "acp"],
            "version_argv": [str(kimi), "--version"],
            "observed_version": _version_line([str(kimi), "--version"]),
            "provenance": "existing local Kimi Code installation with native ACP",
        },
        "zcode": {
            "argv": [
                str(zcode_acp),
                "--zcode-path",
                str(zcode_bundle),
                "--node",
                str(node),
                "--prompt-timeout-secs",
                "900",
                "--no-browser",
            ],
            "version_argv": [str(zcode_acp), "--version"],
            "observed_version": _version_line([str(zcode_acp), "--version"]),
            "provenance": "existing Ultra-pinned zcode-acp bridge with local ZCode bundle",
        },
        "opencode": {
            "argv": [str(opencode), "acp", "--pure"],
            "version_argv": [str(opencode), "--version"],
            "observed_version": _version_line([str(opencode), "--version"]),
            "provenance": "existing local OpenCode installation with native ACP; external plugins disabled",
        },
    }


def _merge_registry(
    home: Path,
    runtime_root: Path,
    managed_targets: dict[str, dict[str, Any]],
    backup: Path,
) -> tuple[Path, dict[str, Any], Path]:
    registry_path = home / ".config" / "agent-delegation" / "config.json"
    acpx_path = home / ".acpx" / "config.json"
    _backup_item(registry_path, backup / "config" / "registry.json")
    _backup_item(acpx_path, backup / "config" / "acpx.json")
    registry = _read_json_object(registry_path)
    existing_targets = registry.get("targets")
    if existing_targets is not None and not isinstance(existing_targets, dict):
        raise InstallError("Existing delegation targets must be a JSON object.")
    custom_targets = {
        name: record
        for name, record in (existing_targets or {}).items()
        if name not in MANAGED_TARGETS
    }
    targets = {**custom_targets, **managed_targets}
    registry.update(
        {
            "schema_version": 1,
            "acpx_path": str((runtime_root / "node_modules" / ".bin" / "acpx").resolve()),
            "acpx_config_path": str(acpx_path),
            "runtime_root": str(runtime_root),
            "runtime_packages": RUNTIME_PACKAGES,
            "runtime_lock_sha256": _sha256(runtime_root / "package-lock.json"),
            "receipt_root": str(home / ".local" / "state" / "agent-delegation" / "runs"),
            "targets": targets,
        }
    )
    registry.setdefault("default_timeout_seconds", 900)
    registry.setdefault("max_timeout_seconds", 7200)
    registry.setdefault("max_delegation_depth", 4)
    registry.setdefault("max_task_chars", 200000)
    registry.setdefault("max_result_chars", 20000)

    acpx = _read_json_object(acpx_path)
    agents = acpx.setdefault("agents", {})
    if not isinstance(agents, dict):
        raise InstallError("Existing ACPX agents must be a JSON object.")
    for name, record in targets.items():
        argv = record.get("argv") if isinstance(record, dict) else None
        if not isinstance(argv, list) or not argv:
            raise InstallError(f"Delegation target {name!r} has invalid argv.")
        agents[name] = {"argv": argv}
    acpx.setdefault("defaultPermissions", "approve-reads")
    acpx.setdefault("nonInteractivePermissions", "fail")
    acpx.setdefault("timeout", 900)
    _atomic_write_json(registry_path, registry)
    _atomic_write_json(acpx_path, acpx)
    return registry_path, registry, acpx_path


def _install_symlink(
    path: Path,
    target: Path,
    backup: Path,
    replace_existing: bool,
) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() and path.resolve() == target.resolve():
            return
        managed_root = path.parent.parent / "share" / "agent-delegation"
        is_managed = path.is_symlink() and str(path.resolve()).startswith(str(managed_root.resolve()))
        if not is_managed and not replace_existing:
            raise InstallError(
                f"Refusing to replace unmanaged command {path}; use --replace-existing after review."
            )
        _backup_item(path, backup / "bin" / path.name)
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)


def _install(args: argparse.Namespace) -> int:
    home = Path(args.home).expanduser().resolve() if args.home else Path.home()
    hosts = _parse_hosts(args.hosts)
    if not SKILL_SOURCE.is_dir():
        raise InstallError(f"Missing Skill source {SKILL_SOURCE}.")
    backup = _new_backup_dir(home, "install")
    share_root, runtime_root = _install_runtime(home, backup, args.replace_existing)
    canonical_skill = share_root / "skill"
    _copy_skill(canonical_skill, backup, "canonical", args.replace_existing)
    for host in hosts:
        _copy_skill(
            _skill_destination(home, host),
            backup,
            host,
            args.replace_existing,
        )
    targets = _build_managed_targets(home, runtime_root)
    registry_path, registry, acpx_config = _merge_registry(
        home, runtime_root, targets, backup
    )
    local_bin = home / ".local" / "bin"
    _install_symlink(
        local_bin / "agent-delegate",
        canonical_skill / "scripts" / "agent_delegate.py",
        backup,
        args.replace_existing,
    )
    _install_symlink(
        local_bin / "acpx",
        Path(registry["acpx_path"]),
        backup,
        args.replace_existing,
    )
    manifest = {
        "package": "agent-delegation",
        "version": VERSION,
        "installed_at": datetime.now(UTC).isoformat(),
        "hosts": hosts,
        "backup_dir": str(backup),
        "registry": str(registry_path),
        "acpx_config": str(acpx_config),
        "runtime_root": str(runtime_root),
        "runtime_lock_sha256": registry["runtime_lock_sha256"],
        "targets": sorted(targets),
    }
    manifest_path = home / ".local" / "state" / "agent-delegation" / "install-manifest.json"
    _atomic_write_json(manifest_path, manifest)
    print(json.dumps({"status": "installed", **manifest}, ensure_ascii=False, indent=2))
    return 0


def _doctor(args: argparse.Namespace) -> int:
    home = Path(args.home).expanduser().resolve() if args.home else Path.home()
    command = home / ".local" / "bin" / "agent-delegate"
    if not command.is_file() or not os.access(command, os.X_OK):
        raise InstallError(f"agent-delegate is not installed at {command}.")
    completed = subprocess.run(
        [str(command), "doctor", "--json"],
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
        env={**os.environ, "AGENT_DELEGATION_HOME": str(home)},
    )
    sys.stdout.write(completed.stdout)
    sys.stderr.write(completed.stderr)
    return completed.returncode


def _uninstall(args: argparse.Namespace) -> int:
    home = Path(args.home).expanduser().resolve() if args.home else Path.home()
    hosts = _parse_hosts(args.hosts)
    registry_path = home / ".config" / "agent-delegation" / "config.json"
    registry = _read_json_object(registry_path)
    registered_targets = registry.get("targets") or {}
    if not isinstance(registered_targets, dict):
        raise InstallError("Existing delegation targets must be a JSON object.")
    custom_targets = sorted(set(registered_targets) - MANAGED_TARGETS)
    if args.remove_runtime and custom_targets:
        raise InstallError(
            "Refusing to remove the shared runtime while custom targets remain: "
            + ", ".join(custom_targets)
        )
    backup = _new_backup_dir(home, "uninstall")
    removed: list[str] = []
    for host in hosts:
        destination = _skill_destination(home, host)
        if destination.exists() and _is_managed_skill(destination):
            _backup_item(destination, backup / "skills" / host)
            shutil.rmtree(destination)
            removed.append(str(destination))
    if args.remove_runtime:
        acpx_config_path = home / ".acpx" / "config.json"
        _backup_item(registry_path, backup / "config" / "registry.json")
        _backup_item(acpx_config_path, backup / "config" / "acpx.json")
        acpx_config = _read_json_object(acpx_config_path)
        agents = acpx_config.get("agents") or {}
        if not isinstance(agents, dict):
            raise InstallError("Existing ACPX agents must be a JSON object.")
        for name in MANAGED_TARGETS:
            record = registered_targets.get(name)
            expected = {"argv": record.get("argv")} if isinstance(record, dict) else None
            if expected is not None and agents.get(name) == expected:
                agents.pop(name, None)
        if agents:
            acpx_config["agents"] = agents
        else:
            acpx_config.pop("agents", None)
        _atomic_write_json(acpx_config_path, acpx_config)
        if registry_path.exists():
            registry_path.unlink()
            removed.append(str(registry_path))
        share_root = home / ".local" / "share" / "agent-delegation"
        marker = share_root / ".managed.json"
        if marker.exists():
            _backup_item(marker, backup / "runtime" / ".managed.json")
            shutil.rmtree(share_root)
            removed.append(str(share_root))
        for name in ("agent-delegate", "acpx"):
            command = home / ".local" / "bin" / name
            if command.is_symlink() and "agent-delegation" in str(command.resolve()):
                _backup_item(command, backup / "bin" / name)
                command.unlink()
                removed.append(str(command))
        manifest = home / ".local" / "state" / "agent-delegation" / "install-manifest.json"
        if manifest.exists():
            _backup_item(manifest, backup / "state" / "install-manifest.json")
            manifest.unlink()
            removed.append(str(manifest))
    print(
        json.dumps(
            {"status": "uninstalled", "removed": removed, "backup_dir": str(backup)},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", help="Override the target home directory.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    install_parser = subparsers.add_parser("install")
    install_parser.add_argument("--hosts", default=",".join(HOSTS))
    install_parser.add_argument("--replace-existing", action="store_true")
    install_parser.set_defaults(handler=_install)

    doctor_parser = subparsers.add_parser("doctor")
    doctor_parser.set_defaults(handler=_doctor)

    uninstall_parser = subparsers.add_parser("uninstall")
    uninstall_parser.add_argument("--hosts", default=",".join(HOSTS))
    uninstall_parser.add_argument("--remove-runtime", action="store_true")
    uninstall_parser.set_defaults(handler=_uninstall)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    try:
        return int(args.handler(args))
    except InstallError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
