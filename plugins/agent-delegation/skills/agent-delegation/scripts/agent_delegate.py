#!/usr/bin/env python3
"""Receipt-backed mission delegation through a pinned ACPX runtime."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import UTC, datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator
import uuid


VERSION = "0.1.2"
SCHEMA_VERSION = 1
TARGET_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
RESERVED_TARGETS = {
    "cancel",
    "compare",
    "config",
    "exec",
    "flow",
    "prompt",
    "sessions",
    "set",
    "set-mode",
    "status",
}
PERMISSION_FLAGS = {
    "approve-all": "--approve-all",
    "approve-reads": "--approve-reads",
    "deny-all": "--deny-all",
}


class DelegationError(RuntimeError):
    """A user-facing delegation failure."""


def _home() -> Path:
    configured = os.environ.get("AGENT_DELEGATION_HOME")
    return Path(configured).expanduser().resolve() if configured else Path.home()


def _registry_path() -> Path:
    configured = os.environ.get("AGENT_DELEGATION_CONFIG")
    if configured:
        return Path(configured).expanduser().resolve()
    return _home() / ".config" / "agent-delegation" / "config.json"


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DelegationError(
            f"Missing configuration {path}; run the reviewed install_user.py first."
        ) from exc
    except json.JSONDecodeError as exc:
        raise DelegationError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DelegationError(f"Expected a JSON object in {path}.")
    return value


def _atomic_write_json(path: Path, value: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _atomic_write_text(path: Path, value: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


@contextmanager
def _configuration_lock(registry_path: Path) -> Iterator[None]:
    lock_path = registry_path.parent / ".config.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_registry() -> tuple[Path, dict[str, Any]]:
    path = _registry_path()
    registry = _read_json_object(path)
    if registry.get("schema_version") != SCHEMA_VERSION:
        raise DelegationError(
            f"Unsupported registry schema {registry.get('schema_version')!r}; expected {SCHEMA_VERSION}."
        )
    targets = registry.get("targets")
    if not isinstance(targets, dict) or not targets:
        raise DelegationError("Registry must contain a non-empty targets object.")
    acpx = registry.get("acpx_path")
    if not isinstance(acpx, str) or not Path(acpx).is_absolute():
        raise DelegationError("Registry acpx_path must be an absolute path.")
    for name, target in targets.items():
        _validate_target_record(name, target)
    return path, registry


def _validate_target_record(name: object, target: object) -> None:
    if not isinstance(name, str) or not TARGET_NAME.fullmatch(name):
        raise DelegationError(f"Invalid target name {name!r}.")
    if name in RESERVED_TARGETS or name == "human":
        raise DelegationError(f"Reserved target name {name!r}.")
    if not isinstance(target, dict):
        raise DelegationError(f"Target {name!r} must be an object.")
    argv = target.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(item, str) and item for item in argv)
    ):
        raise DelegationError(f"Target {name!r} must have a non-empty string argv array.")
    if not Path(argv[0]).is_absolute():
        raise DelegationError(f"Target {name!r} argv[0] must be absolute.")


def _target(registry: dict[str, Any], name: str) -> dict[str, Any]:
    target = registry["targets"].get(name)
    if not isinstance(target, dict):
        available = ", ".join(sorted(registry["targets"]))
        raise DelegationError(f"Unknown target {name!r}; available targets: {available}.")
    return target


def _acpx_config_path(registry: dict[str, Any]) -> Path:
    configured = registry.get("acpx_config_path")
    if not isinstance(configured, str) or not Path(configured).is_absolute():
        raise DelegationError("Registry acpx_config_path must be an absolute path.")
    return Path(configured)


def _assert_acpx_mapping(registry: dict[str, Any], name: str, target: dict[str, Any]) -> None:
    config_path = _acpx_config_path(registry)
    config = _read_json_object(config_path)
    configured = (config.get("agents") or {}).get(name)
    expected = {"argv": target["argv"]}
    if configured != expected:
        raise DelegationError(
            f"ACPX mapping for {name!r} does not match the reviewed registry; run doctor/install before delegating."
        )


def _parse_chain(raw: str | None, caller: str) -> list[str]:
    chain = [part.strip() for part in raw.split(",")] if raw else [caller]
    if not chain or any(not part for part in chain):
        raise DelegationError("Delegation chain contains an empty entry.")
    if chain[-1] != caller:
        raise DelegationError("The last delegation-chain entry must be the current caller.")
    if len(chain) != len(set(chain)):
        raise DelegationError("Delegation chain already contains a cycle.")
    return chain


def _read_task(args: argparse.Namespace, cwd: Path, max_chars: int) -> tuple[str, str]:
    if args.task is not None:
        task = args.task
        source = "argument"
    elif args.task_file is not None:
        task_path = Path(args.task_file).expanduser()
        if not task_path.is_absolute():
            task_path = cwd / task_path
        task_path = task_path.resolve()
        try:
            task = task_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise DelegationError(f"Cannot read task file {task_path}: {exc}") from exc
        source = str(task_path)
    elif not sys.stdin.isatty():
        task = sys.stdin.read()
        source = "stdin"
    else:
        raise DelegationError("Provide --task, --task-file, or piped stdin.")
    if not task.strip():
        raise DelegationError("Task must not be empty.")
    if len(task) > max_chars:
        raise DelegationError(f"Task has {len(task)} characters; configured limit is {max_chars}.")
    return task, source


def _delegation_context(
    delegation_id: str,
    caller: str,
    target: str,
    chain: list[str],
    max_depth: int,
    cwd: Path,
    permissions: str,
    terminal: bool,
) -> str:
    context = {
        "schema": "agent-delegation-context/v1",
        "delegation_id": delegation_id,
        "caller": caller,
        "target": target,
        "chain": [*chain, target],
        "depth": len(chain),
        "max_depth": max_depth,
        "cwd": str(cwd),
        "permissions": permissions,
        "terminal": terminal,
        "authority": (
            "Available transport capabilities do not create authority. Follow the mission's "
            "explicit goal, requirements, inherited authority, and commit gates; pause only "
            "before an ungranted effect."
        ),
    }
    return (
        "<agent-delegation-context>\n"
        + json.dumps(context, ensure_ascii=False, sort_keys=True)
        + "\n</agent-delegation-context>\n\n"
    )


def _new_receipt_dir(registry: dict[str, Any], delegation_id: str) -> Path:
    root_raw = registry.get("receipt_root")
    if not isinstance(root_raw, str) or not Path(root_raw).is_absolute():
        raise DelegationError("Registry receipt_root must be an absolute path.")
    root = Path(root_raw)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = root / f"{stamp}-{delegation_id}"
    path.mkdir(mode=0o700)
    return path


def _session_update(item: dict[str, Any]) -> dict[str, Any]:
    params = item.get("params")
    if not isinstance(params, dict):
        return {}
    update = params.get("update")
    return update if isinstance(update, dict) else {}


def _extract_result(
    events: str, max_chars: int
) -> tuple[str, str | None, list[str], int, bool]:
    chunks: list[str] = []
    stop_reason: str | None = None
    errors: list[str] = []
    parsed = 0
    for line in events.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        parsed += 1
        if not isinstance(item, dict):
            continue
        update = _session_update(item)
        if update.get("sessionUpdate") == "agent_message_chunk":
            content = update.get("content") or {}
            if isinstance(content, dict) and content.get("type") == "text":
                text = content.get("text")
                if isinstance(text, str):
                    chunks.append(text)
        result = item.get("result")
        if isinstance(result, dict) and isinstance(result.get("stopReason"), str):
            stop_reason = result["stopReason"]
        error = item.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                errors.append(message)
    full_text = "".join(chunks)
    truncated = len(full_text) > max_chars
    assistant_text = full_text[:max_chars]
    return assistant_text, stop_reason, errors, parsed, truncated


def _normalized_exit_code(code: int) -> int:
    return code if 0 <= code <= 125 else 1


def _run(args: argparse.Namespace) -> int:
    _, registry = _load_registry()
    target = _target(registry, args.to)
    _assert_acpx_mapping(registry, args.to, target)

    caller = args.caller or os.environ.get("AGENT_DELEGATION_CALLER")
    if not caller:
        raise DelegationError("Caller is required; pass --caller or use an injected delegation environment.")
    if caller != "human" and caller not in registry["targets"]:
        raise DelegationError(f"Caller {caller!r} is neither human nor a registered target.")
    if caller == args.to:
        raise DelegationError("Direct self-delegation is not allowed.")

    chain_raw = args.chain
    if chain_raw is None:
        chain_raw = os.environ.get("AGENT_DELEGATION_CHAIN")
    chain = _parse_chain(chain_raw, caller)
    if args.to in chain:
        raise DelegationError(f"Target {args.to!r} already appears in the delegation chain.")

    configured_depth = int(registry.get("max_delegation_depth", 4))
    inherited_depth_raw = os.environ.get("AGENT_DELEGATION_MAX_DEPTH")
    try:
        inherited_depth = int(inherited_depth_raw) if inherited_depth_raw else configured_depth
    except ValueError as exc:
        raise DelegationError("Injected AGENT_DELEGATION_MAX_DEPTH is not an integer.") from exc
    max_depth = args.max_depth if args.max_depth is not None else inherited_depth
    if max_depth < 1 or max_depth > configured_depth:
        raise DelegationError(f"max-depth must be between 1 and configured ceiling {configured_depth}.")
    if len(chain) > max_depth:
        raise DelegationError(f"Delegation depth {len(chain)} exceeds maximum {max_depth}.")

    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        raise DelegationError(f"Delegation cwd is not an existing directory: {cwd}")

    configured_timeout = int(registry.get("default_timeout_seconds", 900))
    timeout = args.timeout if args.timeout is not None else configured_timeout
    max_timeout = int(registry.get("max_timeout_seconds", 7200))
    if timeout < 1 or timeout > max_timeout:
        raise DelegationError(f"timeout must be between 1 and {max_timeout} seconds.")

    permissions = args.permissions
    if (permissions == "approve-all" or args.terminal) and not (
        args.authorization_note and args.authorization_note.strip()
    ):
        raise DelegationError(
            "Full-capability delegation requires a concrete --authorization-note that records "
            "the existing owner authority and task boundary."
        )

    max_task_chars = int(registry.get("max_task_chars", 200000))
    task, task_source = _read_task(args, cwd, max_task_chars)
    delegation_id = uuid.uuid4().hex
    prompt = _delegation_context(
        delegation_id,
        caller,
        args.to,
        chain,
        max_depth,
        cwd,
        permissions,
        args.terminal,
    ) + task

    acpx = Path(registry["acpx_path"])
    if not acpx.is_file() or not os.access(acpx, os.X_OK):
        raise DelegationError(f"Pinned ACPX executable is missing or not executable: {acpx}")
    executable = Path(target["argv"][0])
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise DelegationError(f"Target executable is missing or not executable: {executable}")

    command = [
        str(acpx),
        "--cwd",
        str(cwd),
        "--timeout",
        str(timeout),
        "--format",
        "json",
        "--json-strict",
        "--suppress-reads",
        PERMISSION_FLAGS[permissions],
        "--non-interactive-permissions",
        "fail",
    ]
    if not args.terminal:
        command.append("--no-terminal")
    command.extend([args.to, "exec", "--file", "-"])

    if args.dry_run:
        print(
            json.dumps(
                {
                    "status": "dry_run",
                    "delegation_id": delegation_id,
                    "caller": caller,
                    "target": args.to,
                    "chain": [*chain, args.to],
                    "cwd": str(cwd),
                    "timeout_seconds": timeout,
                    "permissions": permissions,
                    "terminal": args.terminal,
                    "command": command,
                    "task_sha256": hashlib.sha256(task.encode()).hexdigest(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    receipt_dir = _new_receipt_dir(registry, delegation_id)
    request = {
        "schema": "agent-delegation-request/v1",
        "delegation_id": delegation_id,
        "created_at": datetime.now(UTC).isoformat(),
        "caller": caller,
        "target": args.to,
        "chain": [*chain, args.to],
        "cwd": str(cwd),
        "timeout_seconds": timeout,
        "permissions": permissions,
        "terminal": args.terminal,
        "authorization_note": args.authorization_note,
        "task_source": task_source,
        "task_chars": len(task),
        "task_sha256": hashlib.sha256(task.encode()).hexdigest(),
        "target_argv": target["argv"],
    }
    _atomic_write_json(receipt_dir / "request.json", request)

    child_env = os.environ.copy()
    child_env["AGENT_DELEGATION_CALLER"] = args.to
    child_env["AGENT_DELEGATION_CHAIN"] = ",".join([*chain, args.to])
    child_env["AGENT_DELEGATION_MAX_DEPTH"] = str(max_depth)
    started = time.monotonic()
    timed_out = False
    try:
        completed = subprocess.run(
            command,
            input=prompt,
            text=True,
            capture_output=True,
            env=child_env,
            timeout=timeout + 30,
            check=False,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        return_code = completed.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        stderr += "\n[agent-delegate] outer timeout expired after ACPX grace period.\n"
        return_code = 124
    duration = time.monotonic() - started

    _atomic_write_text(receipt_dir / "events.ndjson", stdout)
    _atomic_write_text(receipt_dir / "stderr.log", stderr)
    max_result_chars = int(registry.get("max_result_chars", 20000))
    assistant_text, stop_reason, errors, parsed_events, text_truncated = _extract_result(
        stdout, max_result_chars
    )
    status = "timeout" if timed_out else ("success" if return_code == 0 else "error")
    result = {
        "schema": "agent-delegation-result/v1",
        "status": status,
        "delegation_id": delegation_id,
        "caller": caller,
        "target": args.to,
        "chain": [*chain, args.to],
        "cwd": str(cwd),
        "exit_code": return_code,
        "stop_reason": stop_reason,
        "assistant_text": assistant_text,
        "assistant_text_truncated": text_truncated,
        "protocol_errors": errors,
        "parsed_event_count": parsed_events,
        "duration_seconds": round(duration, 3),
        "receipt_dir": str(receipt_dir),
    }
    _atomic_write_json(receipt_dir / "result.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return _normalized_exit_code(return_code)
def _list_targets(args: argparse.Namespace) -> int:
    _, registry = _load_registry()
    rows = []
    for name in sorted(registry["targets"]):
        target = registry["targets"][name]
        rows.append(
            {
                "name": name,
                "argv": target["argv"],
                "observed_version": target.get("observed_version"),
                "provenance": target.get("provenance"),
            }
        )
    if args.json:
        print(json.dumps({"schema_version": SCHEMA_VERSION, "targets": rows}, indent=2))
    else:
        for row in rows:
            print(f"{row['name']}\t{row['observed_version'] or '-'}\t{row['argv'][0]}")
    return 0


def _run_version_probe(argv: list[str]) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            argv,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = (completed.stdout + completed.stderr).strip().splitlines()
    summary = output[0] if output else f"exit {completed.returncode}"
    return completed.returncode == 0, summary


def _doctor(args: argparse.Namespace) -> int:
    _, registry = _load_registry()
    checks: list[dict[str, Any]] = []
    acpx = Path(registry["acpx_path"])
    checks.append(
        {
            "check": "acpx_executable",
            "ok": acpx.is_file() and os.access(acpx, os.X_OK),
            "detail": str(acpx),
        }
    )
    acpx_config = _read_json_object(_acpx_config_path(registry))
    configured_agents = acpx_config.get("agents") or {}
    for name, target in sorted(registry["targets"].items()):
        executable = Path(target["argv"][0])
        checks.append(
            {
                "check": f"target:{name}:executable",
                "ok": executable.is_file() and os.access(executable, os.X_OK),
                "detail": str(executable),
            }
        )
        checks.append(
            {
                "check": f"target:{name}:acpx_mapping",
                "ok": configured_agents.get(name) == {"argv": target["argv"]},
                "detail": "structured argv matches" if configured_agents.get(name) == {"argv": target["argv"]} else "registry mismatch",
            }
        )
        probe = target.get("version_argv")
        if isinstance(probe, list) and probe:
            ok, detail = _run_version_probe(probe)
            expected = target.get("observed_version")
            drift = isinstance(expected, str) and expected not in detail
            checks.append(
                {
                    "check": f"target:{name}:version_probe",
                    "ok": ok,
                    "warning": "observed version drift" if ok and drift else None,
                    "detail": detail,
                }
            )
    runtime_root_raw = registry.get("runtime_root")
    packages = registry.get("runtime_packages") or {}
    if isinstance(runtime_root_raw, str) and isinstance(packages, dict):
        runtime_root = Path(runtime_root_raw)
        for package, expected_version in sorted(packages.items()):
            package_json = runtime_root / "node_modules" / Path(*package.split("/")) / "package.json"
            observed = None
            try:
                observed = json.loads(package_json.read_text(encoding="utf-8")).get("version")
            except (OSError, json.JSONDecodeError, AttributeError):
                pass
            checks.append(
                {
                    "check": f"runtime_package:{package}",
                    "ok": observed == expected_version,
                    "detail": f"expected {expected_version}, observed {observed}",
                }
            )
    ok = all(check.get("ok") is True for check in checks)
    payload = {"status": "ok" if ok else "error", "checks": checks}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for check in checks:
            marker = "ok" if check["ok"] else "FAIL"
            suffix = f" ({check['warning']})" if check.get("warning") else ""
            print(f"{marker}\t{check['check']}\t{check['detail']}{suffix}")
    return 0 if ok else 1


def _backup_configuration(registry_path: Path, acpx_path: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = _home() / ".local" / "state" / "agent-delegation" / "backups" / f"{stamp}-register"
    backup.mkdir(parents=True, exist_ok=False, mode=0o700)
    for source, name in ((registry_path, "registry.json"), (acpx_path, "acpx-config.json")):
        if source.exists():
            _atomic_write_text(backup / name, source.read_text(encoding="utf-8"))
    return backup


def _register(args: argparse.Namespace) -> int:
    if not TARGET_NAME.fullmatch(args.name) or args.name in RESERVED_TARGETS or args.name == "human":
        raise DelegationError(f"Invalid or reserved target name {args.name!r}.")
    try:
        argv = json.loads(args.argv_json)
    except json.JSONDecodeError as exc:
        raise DelegationError(f"Invalid --argv-json: {exc}") from exc
    record: dict[str, Any] = {
        "argv": argv,
        "observed_version": args.observed_version,
        "provenance": args.provenance,
    }
    if args.version_argv_json:
        try:
            record["version_argv"] = json.loads(args.version_argv_json)
        except json.JSONDecodeError as exc:
            raise DelegationError(f"Invalid --version-argv-json: {exc}") from exc
    _validate_target_record(args.name, record)
    executable = Path(record["argv"][0])
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise DelegationError(f"Target executable is missing or not executable: {executable}")

    registry_path = _registry_path()
    with _configuration_lock(registry_path):
        registry = _read_json_object(registry_path)
        existing = (registry.get("targets") or {}).get(args.name)
        if existing is not None and not args.replace:
            raise DelegationError(f"Target {args.name!r} already exists; pass --replace to update it.")
        acpx_path = _acpx_config_path(registry)
        backup = _backup_configuration(registry_path, acpx_path)
        targets = registry.setdefault("targets", {})
        targets[args.name] = record
        acpx_config = _read_json_object(acpx_path)
        acpx_agents = acpx_config.setdefault("agents", {})
        if not isinstance(acpx_agents, dict):
            raise DelegationError("ACPX agents configuration must be an object.")
        acpx_agents[args.name] = {"argv": record["argv"]}
        _atomic_write_json(registry_path, registry)
        _atomic_write_json(acpx_path, acpx_config)
    print(
        json.dumps(
            {"status": "registered", "name": args.name, "backup_dir": str(backup)},
            indent=2,
        )
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-delegate",
        description="Delegate missions through a pinned ACPX runtime.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List reviewed ACP targets.")
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(handler=_list_targets)

    doctor_parser = subparsers.add_parser("doctor", help="Validate runtime, registry, and targets.")
    doctor_parser.add_argument("--json", action="store_true")
    doctor_parser.set_defaults(handler=_doctor)

    run_parser = subparsers.add_parser("run", help="Run one stateless delegated task.")
    run_parser.add_argument("--to", required=True, help="Registered target name.")
    run_parser.add_argument("--caller", help="Current host, or human for direct operator use.")
    run_parser.add_argument("--chain", help="Comma-separated chain ending in the current caller.")
    run_parser.add_argument("--max-depth", type=int, help="May lower but not raise the configured ceiling.")
    run_parser.add_argument("--cwd", required=True, help="Existing absolute or resolvable task directory.")
    task_group = run_parser.add_mutually_exclusive_group()
    task_group.add_argument("--task", help="Short task text; prefer --task-file for long packets.")
    task_group.add_argument("--task-file", help="UTF-8 mission envelope path, or pipe mission text on stdin.")
    run_parser.add_argument("--timeout", type=int, help="Timeout in seconds.")
    run_parser.add_argument(
        "--permissions",
        choices=sorted(PERMISSION_FLAGS),
        default="approve-all",
        help="ACPX transport policy; defaults to preserving the target's normal capabilities.",
    )
    terminal_group = run_parser.add_mutually_exclusive_group()
    terminal_group.add_argument(
        "--terminal",
        dest="terminal",
        action="store_true",
        help="Advertise ACP terminal capability (default).",
    )
    terminal_group.add_argument(
        "--no-terminal",
        dest="terminal",
        action="store_false",
        help="Explicitly remove ACP terminal capability for a restricted mission.",
    )
    run_parser.add_argument(
        "--authorization-note",
        help="Existing owner authority and prompt boundary recorded for full-capability transport.",
    )
    run_parser.add_argument("--dry-run", action="store_true", help="Validate and print the launch plan only.")
    run_parser.set_defaults(terminal=True, handler=_run)

    register_parser = subparsers.add_parser("register", help="Register an additional reviewed ACP target.")
    register_parser.add_argument("--name", required=True)
    register_parser.add_argument("--argv-json", required=True, help="JSON string array with absolute executable.")
    register_parser.add_argument("--version-argv-json", help="Optional JSON string array for a read-only version probe.")
    register_parser.add_argument("--observed-version", required=True)
    register_parser.add_argument("--provenance", required=True)
    register_parser.add_argument("--replace", action="store_true")
    register_parser.set_defaults(handler=_register)
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        return int(args.handler(args))
    except DelegationError as exc:
        print(
            json.dumps(
                {"status": "error", "type": "delegation_error", "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
