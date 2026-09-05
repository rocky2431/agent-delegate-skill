#!/usr/bin/env python3
"""Receipt-backed mission delegation through the installed ACPX runtime."""

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
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterable, Iterator
import uuid


VERSION = "0.4.0"
SCHEMA_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 7200
MAX_TIMEOUT_SECONDS = 7200
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
    if not isinstance(targets, dict):
        raise DelegationError("Registry must contain a targets object.")
    acpx = registry.get("acpx_path")
    if not isinstance(acpx, str) or not Path(acpx).is_absolute():
        raise DelegationError("Registry acpx_path must be an absolute path.")
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
    launches = target.get("legacy_argv", [])
    if not isinstance(launches, list):
        raise DelegationError(f"Target {name!r} legacy_argv must be an array of argv arrays.")
    for value in [target.get("launch_argv"), *launches]:
        if value is not None and (not isinstance(value, list) or not value or
                not all(isinstance(item, str) and item for item in value) or not Path(value[0]).is_absolute()):
            raise DelegationError(f"Target {name!r} launch commands must have an absolute executable.")
    cli_env = target.get("cli_env", {})
    if not isinstance(cli_env, dict) or any(key not in ("CODEX_PATH", "CLAUDE_CODE_EXECUTABLE") or
            not isinstance(value, str) or not Path(value).is_absolute() for key, value in cli_env.items()):
        raise DelegationError(f"Target {name!r} cli_env must bind native CLI variables to absolute paths.")


def _target(registry: dict[str, Any], name: str) -> dict[str, Any]:
    target = registry["targets"].get(name)
    if not isinstance(target, dict):
        available = ", ".join(sorted(registry["targets"]))
        raise DelegationError(f"Unknown target {name!r}; available targets: {available}.")
    _validate_target_record(name, target)
    return target


def _acpx_config_path(registry: dict[str, Any]) -> Path:
    configured = registry.get("acpx_config_path")
    if not isinstance(configured, str) or not Path(configured).is_absolute():
        raise DelegationError("Registry acpx_config_path must be an absolute path.")
    return Path(configured)


def _parse_chain(raw: str | None, caller: str) -> list[str]:
    chain = [part.strip() for part in raw.split(",")] if raw else [caller]
    if not chain or any(not part for part in chain):
        raise DelegationError("Delegation chain contains an empty entry.")
    if chain[-1] != caller:
        chain.append(caller)
    return chain


def _read_task(
    args: argparse.Namespace, cwd: Path, max_chars: int | None
) -> tuple[str, str]:
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
        except (OSError, UnicodeError) as exc:
            raise DelegationError(f"Cannot read task file {task_path}: {exc}") from exc
        source = str(task_path)
    elif not sys.stdin.isatty():
        task = sys.stdin.read()
        source = "stdin"
    else:
        raise DelegationError("Provide --task, --task-file, or piped stdin.")
    if not task.strip():
        raise DelegationError("Task must not be empty.")
    if max_chars is not None and len(task) > max_chars:
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


def _extract_result(events: Iterable[str], max_chars: int | None) -> dict[str, Any]:
    contents: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    methods: dict[object, str] = {}
    result: dict[str, Any] = {
        "stop_reason": None, "acp_session_id": None, "acpx_record_id": None,
        "parsed_event_count": 0, "unparsed_event_count": 0,
    }
    for line in events:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            result["unparsed_event_count"] += 1
            continue
        if not isinstance(item, dict):
            result["unparsed_event_count"] += 1
            continue
        result["parsed_event_count"] += 1
        request_id = item.get("id")
        if isinstance(request_id, (str, int)) and isinstance(item.get("method"), str):
            methods[request_id] = item["method"]
        update = _session_update(item)
        if update.get("sessionUpdate") == "agent_message_chunk":
            content = update.get("content")
            if isinstance(content, dict):
                contents.append(content)
        params = item.get("params")
        if isinstance(params, dict) and isinstance(params.get("sessionId"), str):
            result["acp_session_id"] = params["sessionId"]
        response = item.get("result")
        if not isinstance(response, dict) and isinstance(item.get("action"), str):
            response = item
        if isinstance(response, dict):
            if isinstance(response.get("stopReason"), str):
                result["stop_reason"] = response["stopReason"]
            for source, dest in (("sessionId", "acp_session_id"), ("acpSessionId", "acp_session_id"),
                                 ("acpxSessionId", "acp_session_id"), ("acpxRecordId", "acpx_record_id"),
                                 ("agentSessionId", "agent_session_id")):
                if isinstance(response.get(source), str):
                    result[dest] = response[source]
            if isinstance(response.get("action"), str):
                result["action"] = response["action"]
            if isinstance(response.get("cancelled"), bool):
                result["cancel_requested"] = response["cancelled"]
        error = item.get("error")
        if isinstance(error, dict):
            method = methods.get(request_id) if isinstance(request_id, (str, int)) else None
            scope = "tool" if method and (method.startswith(("fs/", "terminal/")) or method == "session/request_permission") else "rpc"
            errors.append({**error, "request_id": request_id, "method": method, "scope": scope})
        if "result" in item or "error" in item:
            if isinstance(request_id, (str, int)):
                methods.pop(request_id, None)
    full_text = "".join(c.get("text", "") for c in contents if c.get("type") == "text" and isinstance(c.get("text"), str))
    result.update({
        "assistant_text": full_text if max_chars is None else full_text[:max_chars],
        "assistant_text_truncated": max_chars is not None and len(full_text) > max_chars,
        "assistant_content": contents,
        "rpc_errors": errors,
        "tool_errors": [error for error in errors if error["scope"] == "tool"],
        # Keep the legacy field, without labeling ordinary client operations as protocol failures.
        "protocol_errors": [error.get("message", "") for error in errors if error["scope"] != "tool"],
    })
    return result


def _result_status(code: int, parsed: dict[str, Any], interrupted: str | None, control: bool) -> str:
    if interrupted:
        return interrupted
    stop = parsed["stop_reason"]
    if stop == "cancelled" or code == 130:
        return "cancelled"
    if code == 3 or code == 124:
        return "timeout"
    if code == 5:
        return "denied"
    if code == 4:
        return "not_found"
    if code != 0:
        return "error"
    if control:
        return "success" if parsed.get("action") else "incomplete"
    if stop == "end_turn":
        return "success"
    if stop == "refusal":
        return "refused"
    return "error" if parsed["protocol_errors"] and not stop else "incomplete"


def _positive_int(raw: Any, label: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise DelegationError(f"{label} must be a positive integer.") from exc
    if isinstance(raw, bool) or str(value) != str(raw) or value < 1:
        raise DelegationError(f"{label} must be a positive integer.")
    return value


def _stop_process(process: subprocess.Popen) -> None:
    # ACPX gets a chance to cancel the turn and close its agent before forced cleanup.
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            continue
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return


def _stream_command(command: list[str], prompt: str, env: dict[str, str], timeout: float,
                    stdout: Any, stderr: Any,
                    on_interrupt: Callable[[], None] | None = None,
                    is_cancelled: Callable[[], bool] | None = None) -> tuple[int, str | None]:
    # File descriptors preserve streamed bytes even when the process is interrupted.
    with tempfile.TemporaryFile() as task_input:
        task_input.write(prompt.encode("utf-8"))
        task_input.seek(0)
        process = subprocess.Popen(command, stdin=task_input, stdout=stdout, stderr=stderr,
                                   env=env, start_new_session=True)
        try:
            deadline = time.monotonic() + timeout
            while True:
                if is_cancelled and is_cancelled():
                    raise KeyboardInterrupt
                remaining = max(0.001, deadline - time.monotonic())
                try:
                    return process.wait(timeout=min(0.1, remaining) if is_cancelled else remaining), None
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        raise
        except subprocess.TimeoutExpired:
            code, reason = 124, "timeout"
        except KeyboardInterrupt:
            code, reason = 130, "cancelled"
        if on_interrupt:
            # Startup may not have registered the native turn at the first cancel.
            # Keep ownership and the client until it confirms a terminal event.
            cancel_deadline = time.monotonic() + 10
            while True:
                on_interrupt()
                try:
                    process.wait(timeout=max(0.001, min(1, cancel_deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= cancel_deadline:
                        _stop_process(process)
                        break
        else:
            _stop_process(process)
        return code, reason


@contextmanager
def _session_turn_lock(receipt_dir: Path, argv: list[str], cwd: Path,
                       session: str | None, deadline: float | None,
                       is_cancelled: Callable[[], bool] | None = None) -> Iterator[None]:
    if session is None:
        yield
        return
    # ACPX can cancel the active turn, but cannot remove a particular queued turn.
    # Wait before submitting so interrupting a waiter cannot cancel another task.
    lock_root = receipt_dir.parent / ".session-locks"
    lock_root.mkdir(mode=0o700, exist_ok=True)
    lock_path = lock_root / (_session_key(argv, cwd, session) + ".lock")
    with lock_path.open("a+b") as handle:
        os.chmod(lock_path, 0o600)
        waiting = False
        while True:
            if is_cancelled and is_cancelled():
                raise KeyboardInterrupt
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Timed out waiting for the session's current turn.")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not waiting:
                    print(json.dumps({"status": "waiting", "session_name": session,
                                      "receipt_dir": str(receipt_dir)}), file=sys.stderr, flush=True)
                    waiting = True
                time.sleep(0.1)
        # Closing the descriptor releases the lock, including on interruption.
        yield


def _configured_char_limit(registry: dict[str, Any], key: str) -> int | None:
    raw = registry.get(key)
    if raw is None:
        return None
    return _positive_int(raw, f"Registry {key}")


def _normalized_exit_code(code: int) -> int:
    return code if 0 <= code <= 125 else 1


def _session_key(argv: list[str], cwd: Path, session: str) -> str:
    identity = json.dumps([argv, str(cwd), session], ensure_ascii=False)
    return hashlib.sha256(identity.encode()).hexdigest()


def _launch_argv(target: dict[str, Any], acpx: str, cwd: Path, session: str | None) -> list[str]:
    selected = target.get("launch_argv", target["argv"])
    legacy = target.get("legacy_argv", [])
    if session and legacy:
        # Use ACPX's local index, not agent-side session/list (which starts an
        # adapter). Keep pre-upgrade sessions in their original command scope.
        for argv in [selected, *legacy]:
            completed = subprocess.run([acpx, "--agent", shlex.join(argv), "--cwd", str(cwd),
                "--format", "json", "sessions", "list", "--local"],
                text=True, capture_output=True, timeout=30, check=False)
            try:
                rows = json.loads(completed.stdout)
            except ValueError as exc:
                raise DelegationError("Cannot read the native session index: " + completed.stderr[-2000:]) from exc
            if completed.returncode != 0 or not isinstance(rows, list):
                raise DelegationError("Cannot read the native session index: " + completed.stdout[-2000:])
            if any(row.get("name") == session and row.get("cwd") == str(cwd) and
                   not row.get("closed") for row in rows):
                return argv
    return selected


def _prepare_run(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    _, registry = _load_registry()
    target = _target(registry, args.to)
    control = args.command in ("cancel", "close")
    caller = (args.caller or os.environ.get("AGENT_DELEGATION_CALLER") or "unknown").strip()
    if not caller or "," in caller:
        raise DelegationError("Caller must be a non-empty label without commas.")
    chain = [caller] if control else _parse_chain(args.chain if args.chain is not None else os.environ.get("AGENT_DELEGATION_CHAIN"), caller)
    configured_depth = 1 if control else _positive_int(registry.get("max_delegation_depth", 4), "max_delegation_depth")
    inherited_depth = 1 if control else _positive_int(os.environ.get("AGENT_DELEGATION_MAX_DEPTH", configured_depth), "inherited max depth")
    ceiling = min(configured_depth, inherited_depth)
    max_depth = args.max_depth if args.max_depth is not None else ceiling
    if not control and (max_depth < 1 or max_depth > ceiling or len(chain) > max_depth):
        raise DelegationError(f"Delegation depth {len(chain)} must fit max-depth 1..{ceiling} (requested {max_depth}).")

    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        raise DelegationError(f"Delegation cwd is not an existing directory: {cwd}")
    timeout = args.timeout if args.timeout is not None else _positive_int(
        registry.get("default_timeout_seconds", DEFAULT_TIMEOUT_SECONDS), "default_timeout_seconds")
    max_timeout = timeout if control else _positive_int(registry.get("max_timeout_seconds", MAX_TIMEOUT_SECONDS), "max_timeout_seconds")
    if timeout < 1 or timeout > max_timeout:
        raise DelegationError(f"timeout must be between 1 and {max_timeout} seconds.")
    queue_timeout = getattr(args, "queue_timeout", None)
    if queue_timeout is not None:
        queue_timeout = _positive_int(queue_timeout, "queue-timeout")
    max_task_chars = None if control else _configured_char_limit(registry, "max_task_chars")
    max_result_chars = None if control else _configured_char_limit(registry, "max_result_chars")
    task, task_source = ("", "control") if control else _read_task(args, cwd, max_task_chars)
    if args.session is not None:
        args.session = args.session.strip()
        if not args.session:
            raise DelegationError("Session name must not be empty.")
    delegation_id = uuid.uuid4().hex
    prompt = "" if control else _delegation_context(delegation_id, caller, args.to, chain,
                    max_depth, cwd, args.permissions, args.terminal) + task
    for label, executable in (("ACPX", registry["acpx_path"]), ("Target", target["argv"][0]),
                              *[("Native CLI", value) for value in target.get("cli_env", {}).values()]):
        if control and label != "ACPX":
            continue
        if not Path(executable).is_file() or not os.access(executable, os.X_OK):
            raise DelegationError(f"{label} executable is missing or not executable: {executable}")

    # ACPX parses this quoted argv without a shell. The registry is the launch authority;
    # global/project agent aliases cannot silently select a different executable.
    selected_argv = _launch_argv(target, registry["acpx_path"], cwd, args.session)
    base = [registry["acpx_path"], "--agent", shlex.join(selected_argv), "--cwd", str(cwd),
            "--timeout", str(timeout), "--format", "json", "--json-strict", "--suppress-reads",
            PERMISSION_FLAGS[args.permissions], "--non-interactive-permissions", "fail"]
    if not args.terminal:
        base.append("--no-terminal")
    if args.model:
        base.extend(["--model", args.model])
    if control:
        suffix = ["cancel", "--session", args.session] if args.command == "cancel" else ["sessions", "close", args.session]
        commands = [(base + suffix, "")]
    elif args.session:
        commands = [(base + ["sessions", "ensure", "--name", args.session], ""),
                    (base + ["prompt", "--session", args.session, "--file", "-"], prompt)]
    else:
        commands = [(base + ["exec", "--file", "-"], prompt)]
    request = {
        "schema": "agent-delegation-request/v1", "delegation_id": delegation_id,
        "created_at": datetime.now(UTC).isoformat(), "caller": caller, "target": args.to,
        "chain": [*chain, args.to], "cwd": str(cwd), "timeout_seconds": timeout,
        "queue_timeout_seconds": queue_timeout, "max_depth": max_depth,
        "permissions": args.permissions, "terminal": args.terminal,
        "authorization_note": args.authorization_note, "task_source": task_source,
        "task_chars": len(task), "task_sha256": hashlib.sha256(task.encode()).hexdigest(),
        "target_argv": selected_argv if selected_argv in target.get("legacy_argv", []) else target["argv"],
        "session_name": args.session, "requested_model": args.model,
        "launch_argv": selected_argv,
        "acpx_path": registry["acpx_path"],
        "legacy_session": selected_argv != target.get("launch_argv", target["argv"]),
        "operation": args.command,
    }
    # Only public launch metadata travels in the receipt, never ambient secrets.
    runtime_launch = {"name": args.to, "acpx_path": registry["acpx_path"], "target": {
        key: target[key] for key in ("argv", "version_argv", "cli_path", "cli_env", "adapter_package", "adapter_version_argv") if key in target}}
    runtime_launch["target"]["argv"] = request["target_argv"]
    return registry, {"request": request, "commands": commands, "max_result_chars": max_result_chars,
                      "runtime_launch": runtime_launch, "use_launcher": selected_argv == target.get("launch_argv")}


def _run(args: argparse.Namespace) -> int:
    registry, launch = _prepare_run(args)
    request, commands = launch["request"], launch["commands"]
    if args.dry_run:
        print(json.dumps({**request, "status": "dry_run", "command": commands[-1][0],
                          "commands": [command for command, _ in commands]}, ensure_ascii=False, indent=2))
        return 0
    receipt_dir = _new_receipt_dir(registry, request["delegation_id"])
    _atomic_write_json(receipt_dir / "request.json", request)
    # The inherited descriptor keeps ownership continuous across background startup.
    # Closing the parent's copy must not explicitly unlock the worker's copy.
    with (receipt_dir / "worker.lock").open("xb") as owner:
        os.chmod(owner.name, 0o600)
        fcntl.flock(owner.fileno(), fcntl.LOCK_EX)
        _atomic_write_json(receipt_dir / "state.json", {
            "schema": "agent-delegation-status/v1", "status": "starting", "terminal": False,
            "delegation_id": request["delegation_id"], "receipt_dir": str(receipt_dir),
            "target": request["target"], "session_name": request["session_name"],
            "created_at": request["created_at"], "created_monotonic": time.monotonic(),
            "queue_wait_seconds": 0, "execution_seconds": 0,
        })
        print(json.dumps({"status": "starting", "delegation_id": request["delegation_id"],
                          "receipt_dir": str(receipt_dir)}), file=sys.stderr, flush=True)
        if args.command == "submit":
            _atomic_write_json(receipt_dir / "launch.json", launch)
            with (receipt_dir / "worker.log").open("xb", buffering=0) as log:
                os.chmod(log.name, 0o600)
                try:
                    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_worker",
                                      "--receipt-dir", str(receipt_dir), "--owner-fd", str(owner.fileno())],
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     start_new_session=True, pass_fds=(owner.fileno(),))
                except OSError:
                    (receipt_dir / "launch.json").unlink(missing_ok=True)
                    raise
            print(json.dumps(_task_snapshot(receipt_dir), ensure_ascii=False, indent=2))
            return 0
        return _execute_run(receipt_dir, launch)


def _worker(args: argparse.Namespace) -> int:
    receipt_dir = Path(args.receipt_dir)
    with os.fdopen(args.owner_fd, "rb"):
        launch = _read_json_object(receipt_dir / "launch.json")
        (receipt_dir / "launch.json").unlink()
        return _execute_run(receipt_dir, launch)


def _execute_run(receipt_dir: Path, launch: dict[str, Any]) -> int:
    request, commands = launch["request"], launch["commands"]
    timeout, session = request["timeout_seconds"], request["session_name"]
    control = request["operation"] in ("cancel", "close")
    child_env = {**os.environ, "AGENT_DELEGATION_CALLER": request["target"],
                 "AGENT_DELEGATION_CHAIN": ",".join(request["chain"]),
                 "AGENT_DELEGATION_MAX_DEPTH": str(request["max_depth"])}
    selected_argv = request.get("launch_argv", request["target_argv"])
    runtime_file = (receipt_dir.parent / ".session-locks" /
                    (_session_key(selected_argv, Path(request["cwd"]), session) + ".runtime.json")) if session else receipt_dir / "runtime.json"
    runtime_launch = launch.get("runtime_launch")
    child_env.pop("AGENT_DELEGATION_LAUNCH", None)
    child_env.pop("AGENT_DELEGATION_RUNTIME_RECEIPT", None)
    if runtime_launch:
        child_env.update(runtime_launch["target"].get("cli_env", {}))
        if launch.get("use_launcher", "_launch" in selected_argv):
            child_env["AGENT_DELEGATION_LAUNCH"] = json.dumps(runtime_launch)
            child_env["AGENT_DELEGATION_RUNTIME_RECEIPT"] = str(runtime_file)
    state = _read_json_object(receipt_dir / "state.json")
    started = state["created_monotonic"]
    execution_started = None
    queue_wait = 0.0
    phase = "setup"
    def set_phase(status: str) -> None:
        state.update(status=status, phase_started_monotonic=time.monotonic(),
                     queue_wait_seconds=round(queue_wait, 3), worker_pid=os.getpid())
        if status == "running":
            state["execution_started_at"] = datetime.now(UTC).isoformat()
        _atomic_write_json(receipt_dir / "state.json", state)
        print(json.dumps({"status": status, "delegation_id": request["delegation_id"],
                          "receipt_dir": str(receipt_dir)}), file=sys.stderr, flush=True)
    def is_cancelled() -> bool:
        return (receipt_dir / "cancel.json").exists()
    return_code, interrupted, launch_error, cancellation_exit_code = 1, None, None, None
    def handle_termination(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt
    previous_handler = signal.signal(signal.SIGTERM, handle_termination)
    try:
        with (receipt_dir / "events.ndjson").open("xb", buffering=0) as stdout, (receipt_dir / "stderr.log").open("xb", buffering=0) as stderr:
            os.chmod(stdout.name, 0o600)
            os.chmod(stderr.name, 0o600)
            def cancel_named_turn() -> None:
                nonlocal cancellation_exit_code
                try:
                    # Reuse the exact launch prefix, including its target and cwd.
                    base = commands[-1][0][:-5]
                    cancellation_exit_code, _ = _stream_command(
                        base + ["cancel", "--session", session], "", child_env, 10, stdout, stderr)
                except OSError as exc:
                    cancellation_exit_code = 1
                    stderr.write((str(exc) + "\n").encode())
            try:
                queue_timeout = request["queue_timeout_seconds"]
                queued = session is not None and not control
                phase = "queue" if queued else "setup"
                set_phase("queued" if queued else "starting")
                with _session_turn_lock(receipt_dir, selected_argv, Path(request["cwd"]),
                                        session if queued else None,
                                        started + queue_timeout if queue_timeout is not None else None,
                                        is_cancelled):
                    queue_wait = time.monotonic() - started if queued else 0.0
                    prompt_started = False
                    try:
                        for command, command_input in commands:
                            if is_cancelled():
                                raise KeyboardInterrupt
                            prompt_started = bool(command_input)
                            phase = "execution" if prompt_started else "setup"
                            if prompt_started:
                                execution_started = time.monotonic()
                            set_phase("running" if prompt_started else "starting")
                            return_code, interrupted = _stream_command(command, command_input, child_env,
                                timeout + 30, stdout, stderr,
                                cancel_named_turn if session and prompt_started and not control else None,
                                is_cancelled)
                            if return_code != 0 or interrupted:
                                break
                    except KeyboardInterrupt:
                        return_code, interrupted = 130, "cancelled"
                    finally:
                        if interrupted and session and prompt_started and not control and cancellation_exit_code is None:
                            cancel_named_turn()
            except KeyboardInterrupt:
                return_code, interrupted = 130, "cancelled"
            except TimeoutError as exc:
                return_code, interrupted = 124, "timeout"
                stderr.write((str(exc) + "\n").encode())
            except OSError as exc:
                launch_error = str(exc)
                stderr.write((launch_error + "\n").encode())
                return_code = 1
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
    if phase == "queue":
        queue_wait = time.monotonic() - started
    with (receipt_dir / "events.ndjson").open(encoding="utf-8", errors="replace") as events:
        parsed = _extract_result(events, launch["max_result_chars"])
    status = _result_status(return_code, parsed, interrupted, control)
    if cancellation_exit_code is not None and (
        cancellation_exit_code != 0 or parsed["stop_reason"] not in ("cancelled", "end_turn")
    ):
        status = "incomplete"
    runtime_identity = None
    if phase != "queue" and runtime_file.is_file():
        runtime_identity = _read_json_object(runtime_file)
    elif runtime_launch:
        runtime_identity = _runtime_identity(runtime_launch["target"], runtime_launch["acpx_path"], probe=False)
        runtime_identity["observation"] = "legacy_session_unverified" if request.get("legacy_session") else "launch_unobserved"
    if runtime_identity:
        runtime_identity["acpx"] = _package_identity(commands[-1][0][0], "acpx")
        _atomic_write_json(receipt_dir / "runtime.json", runtime_identity)
    result = {
        "schema": "agent-delegation-result/v1", **parsed, "status": status,
        **{key: request[key] for key in ("delegation_id", "caller", "target", "chain", "cwd",
                                         "session_name", "requested_model", "operation")},
        "exit_code": return_code, "terminal": True,
        "timeout_seconds": timeout, "cancellation_exit_code": cancellation_exit_code,
        "queue_timeout_seconds": request["queue_timeout_seconds"],
        "queue_wait_seconds": round(queue_wait, 3),
        "execution_seconds": round(time.monotonic() - execution_started, 3) if execution_started is not None else 0,
        "execution_started_at": state.get("execution_started_at"),
        "timeout_phase": phase if status == "timeout" or interrupted == "timeout" else None,
        "launch_error": launch_error, "duration_seconds": round(time.monotonic() - started, 3),
        "receipt_dir": str(receipt_dir),
        "runtime_identity": runtime_identity,
    }
    _atomic_write_json(receipt_dir / "result.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return _result_exit_code(result)


def _result_exit_code(result: dict[str, Any]) -> int:
    if result["status"] == "success":
        return 0
    return {"cancelled": 130, "timeout": 124, "denied": 5}.get(
        result["status"], _normalized_exit_code(result.get("exit_code", 1)) or 1)


def _task_receipt(task_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", task_id):
        raise DelegationError("Task id must be the full delegation_id returned by run or submit.")
    _, registry = _load_registry()
    root = registry.get("receipt_root")
    if not isinstance(root, str) or not Path(root).is_absolute():
        raise DelegationError("Registry receipt_root must be an absolute path.")
    matches = list(Path(root).glob(f"*-{task_id}"))
    if len(matches) != 1 or not (matches[0] / "request.json").is_file():
        raise DelegationError(f"Task {task_id} not found in {root}.")
    return matches[0]


def _task_snapshot(receipt_dir: Path) -> dict[str, Any]:
    result_path = receipt_dir / "result.json"
    if result_path.exists():
        return {**_read_json_object(result_path), "terminal": True}
    state = _read_json_object(receipt_dir / "state.json")
    with (receipt_dir / "worker.lock").open("rb") as owner:
        try:
            fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            alive = False
        except BlockingIOError:
            alive = True
    # Completion can race a read of state or the ownership check.
    if result_path.exists():
        return {**_read_json_object(result_path), "terminal": True}
    now = time.monotonic()
    state["duration_seconds"] = round(now - state.pop("created_monotonic"), 3)
    phase_started = state.pop("phase_started_monotonic", now)
    if state["status"] == "queued":
        state["queue_wait_seconds"] = state["duration_seconds"]
    elif state["status"] == "running":
        state["execution_seconds"] = round(now - phase_started, 3)
    state["cancel_requested"] = (receipt_dir / "cancel.json").exists()
    if not alive:
        state.update(status="incomplete", terminal=True, execution_state="unknown",
                     duration_seconds=None, queue_wait_seconds=None, execution_seconds=None,
                     message="Worker exited without a terminal receipt; inspect events before retrying.")
    return state


def _observe_task(args: argparse.Namespace) -> int:
    receipt_dir = _task_receipt(args.id)
    if args.command == "cancel":
        result = _task_snapshot(receipt_dir)
        if args.dry_run:
            result["dry_run"] = True
        if not result["terminal"] and not args.dry_run:
            _atomic_write_json(receipt_dir / "cancel.json", {
                "delegation_id": args.id, "requested_at": datetime.now(UTC).isoformat()})
            result = _task_snapshot(receipt_dir)
    else:
        if args.command == "wait" and args.timeout < 0:
            raise DelegationError("Wait timeout must be non-negative seconds.")
        deadline = time.monotonic() + (args.timeout if args.command == "wait" else 0)
        while True:
            result = _task_snapshot(receipt_dir)
            if args.command != "wait" or result["terminal"]:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result["wait_timed_out"] = True
                break
            time.sleep(min(0.1, remaining))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return _result_exit_code(result) if args.command == "wait" and result["terminal"] else 0


def _cancel(args: argparse.Namespace) -> int:
    if args.id:
        if args.to or args.cwd or args.session:
            raise DelegationError("Use --id alone, or --to/--cwd/--session for session-wide cancellation.")
        return _observe_task(args)
    if not all((args.to, args.cwd, args.session)):
        raise DelegationError("Cancel requires --id, or all of --to, --cwd, and --session.")
    return _run(args)


def _list_targets(args: argparse.Namespace) -> int:
    _, registry = _load_registry()
    rows = []
    for name in sorted(registry["targets"]):
        target = registry["targets"][name]
        if not isinstance(target, dict):
            rows.append({"name": name, "argv": [], "observed_version": None, "provenance": "invalid record; run doctor"})
            continue
        rows.append(
            {
                "name": name,
                "argv": target.get("argv", []),
                "observed_version": target.get("observed_version"),
                "provenance": target.get("provenance"),
            }
        )
    if args.json:
        print(json.dumps({"schema_version": SCHEMA_VERSION, "targets": rows}, indent=2))
    else:
        for row in rows:
            print(f"{row['name']}\t{row['observed_version'] or '-'}\t{row['argv'][0] if row['argv'] else '-'}")
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


def _package_identity(executable: str, name: str | None = None) -> dict[str, Any]:
    resolved = Path(executable).resolve()
    identity: dict[str, Any] = {"path": executable, "resolved_path": str(resolved), "version": None}
    for parent in resolved.parents:
        try:
            package = json.loads((parent / "package.json").read_text())
            if "version" in package and (name is None or package.get("name") == name):
                identity.update(package=package.get("name"), version=package["version"])
                break
        except (OSError, ValueError, TypeError):
            continue
    return identity


def _cli_version_argv(target: dict[str, Any]) -> list[str] | None:
    binding = target.get("cli_env", {})
    argv = [next(iter(binding.values())), "--version"] if binding else target.get("version_argv")
    if isinstance(argv, list) and argv and all(isinstance(item, str) and item for item in argv) and Path(argv[0]).is_absolute():
        return argv
    return None


def _runtime_identity(target: dict[str, Any], acpx: str, probe: bool = True) -> dict[str, Any]:
    cli = None
    version_argv = _cli_version_argv(target)
    if version_argv:
        path = target.get("cli_path", version_argv[0])
        cli = {"argv": version_argv, "path": path,
               "resolved_path": str(Path(path).resolve()), "version": None}
        if probe:
            ok, detail = _run_version_probe([str(Path(version_argv[0]).resolve()), *version_argv[1:]])
            cli.update(version=detail if ok else None, probe_ok=ok, probe_error=None if ok else detail)
    adapter = None
    if target.get("adapter_package"):
        adapter = _package_identity(target["argv"][0], target["adapter_package"])
    elif (argv := _cli_version_argv({"version_argv": target.get("adapter_version_argv")})):
        ok, detail = _run_version_probe(argv) if probe else (False, None)
        adapter = {"path": argv[0], "version": detail if ok else None}
    return {"observation": "configured", "acpx": _package_identity(acpx, "acpx"),
            "adapter": adapter, "cli": cli, "cli_binding": target.get("cli_env", {}),
            "target_argv": target["argv"]}


def _launch(args: argparse.Namespace) -> int:
    raw = os.environ.get("AGENT_DELEGATION_LAUNCH")
    if raw:
        try:
            launch = json.loads(raw)
        except ValueError as exc:
            raise DelegationError("Invalid internal launch metadata.") from exc
        if not isinstance(launch, dict) or launch.get("name") != args.to:
            raise DelegationError("Internal launch target does not match the requested target.")
        target, acpx = launch["target"], launch["acpx_path"]
    else:
        _, registry = _load_registry()
        target, acpx = _target(registry, args.to), registry["acpx_path"]
    _validate_target_record(args.to, target)
    env = dict(os.environ)
    receipt = env.pop("AGENT_DELEGATION_RUNTIME_RECEIPT", None)
    env.pop("AGENT_DELEGATION_LAUNCH", None)
    argv = [str(Path(target["argv"][0]).resolve()), *target["argv"][1:]]
    for key, value in target.get("cli_env", {}).items():
        path = Path(value).resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise DelegationError(f"Native CLI is missing or not executable: {value}")
        env[key] = str(path)
    # A warm ACPX owner retains this startup record across turns. Version drift
    # cannot masquerade as a hot swap of an already running native CLI.
    version_argv = _cli_version_argv(target)
    probe_target = dict(target)
    if version_argv and target.get("cli_env"):
        probe_target["cli_env"] = {key: env[key] for key in target["cli_env"]}
    identity = _runtime_identity(probe_target, acpx)
    if identity["cli"] and version_argv:
        identity["cli"].update(path=target.get("cli_path", version_argv[0]), argv=version_argv)
    identity.update(observation="adapter_launch", launched_at=datetime.now(UTC).isoformat(),
                    adapter_pid=os.getpid(), resolved_target_argv=argv)
    if receipt:
        _atomic_write_json(Path(receipt), identity)
    os.execvpe(argv[0], argv, env)
    return 0


def _doctor(args: argparse.Namespace) -> int:
    _, registry = _load_registry()
    checks: list[dict[str, Any]] = []
    acpx = Path(registry["acpx_path"])
    checks.append({"check": "acpx_executable", "ok": acpx.is_file() and os.access(acpx, os.X_OK), "detail": str(acpx)})
    names = [args.to] if args.to else sorted(registry["targets"])
    selected_argv: list[str] = [str(acpx)]
    runtimes = {}
    for name in names:
        try:
            target = _target(registry, name)
        except DelegationError as exc:
            checks.append({"check": f"target:{name}:record", "ok": False, "detail": str(exc)})
            continue
        executable = Path(target["argv"][0])
        selected_argv.extend(target["argv"])
        identity = _runtime_identity(target, str(acpx))
        runtimes[name] = identity
        checks.append({"check": f"target:{name}:executable", "ok": executable.is_file() and os.access(executable, os.X_OK),
                       "detail": str(executable), "launch_argv": target["argv"]})
        cli = identity["cli"]
        if cli:
            ok, detail = cli["probe_ok"], cli["version"] or cli["probe_error"]
            expected = target.get("observed_version")
            drift = isinstance(expected, str) and expected not in detail
            checks.append({"check": f"target:{name}:version_probe", "ok": ok, "required": False,
                           "warning": "informational probe unavailable" if not ok else ("observed version drift" if drift else None),
                           "detail": detail, "cli_binding": target.get("cli_env", {})})
        for key, value in target.get("cli_env", {}).items():
            checks.append({"check": f"target:{name}:{key}", "ok": Path(value).is_file() and os.access(value, os.X_OK),
                           "detail": value})
    runtime_root_raw = registry.get("runtime_root")
    packages = registry.get("runtime_packages") or {}
    if isinstance(runtime_root_raw, str) and isinstance(packages, dict):
        for package, expected in sorted(packages.items()):
            root = Path(runtime_root_raw) / "node_modules" / package
            if args.to and not any(Path(arg).is_absolute() and Path(arg).is_relative_to(root) for arg in selected_argv):
                continue
            observed = None
            try:
                observed = json.loads((root / "package.json").read_text())["version"]
            except (OSError, ValueError, KeyError, TypeError):
                pass
            checks.append({"check": f"runtime_package:{package}", "ok": observed is not None,
                           "warning": "recorded version drift" if observed and observed != expected else None,
                           "detail": f"recorded {expected}, installed {observed}"})
    limits = {"default_timeout_seconds": registry.get("default_timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
              "max_timeout_seconds": registry.get("max_timeout_seconds", MAX_TIMEOUT_SECONDS),
              "max_delegation_depth": registry.get("max_delegation_depth", 4),
              "max_task_chars": registry.get("max_task_chars"), "max_result_chars": registry.get("max_result_chars")}
    try:
        for key in ("default_timeout_seconds", "max_timeout_seconds", "max_delegation_depth"):
            limits[key] = _positive_int(limits[key], key)
        for key in ("max_task_chars", "max_result_chars"):
            _configured_char_limit(registry, key)
        if limits["default_timeout_seconds"] > limits["max_timeout_seconds"]:
            raise DelegationError("Default timeout exceeds configured maximum.")
    except DelegationError as exc:
        checks.append({"check": "limits", "ok": False, "detail": str(exc)})
    ok = all(check["ok"] for check in checks if check.get("required", True))
    payload = {"status": "ok" if ok else "error", "limits": limits, "checks": checks,
               "runtimes": runtimes,
               "launch_source": "registry argv; managed launchers snapshot the selected runtime"}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for check in checks:
            marker = "WARN" if check.get("warning") else ("ok" if check["ok"] else "FAIL")
            print(f"{marker}\t{check['check']}\t{check['detail']}")
        print(json.dumps(limits))
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
        description="Delegate missions through the installed ACPX runtime.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List reviewed ACP targets.")
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(handler=_list_targets)

    doctor_parser = subparsers.add_parser("doctor", help="Validate runtime, registry, and targets.")
    doctor_parser.add_argument("--json", action="store_true")
    doctor_parser.add_argument("--to", help="Check only this target and its runtime dependencies.")
    doctor_parser.set_defaults(handler=_doctor)

    for operation in ("run", "submit"):
        run_parser = subparsers.add_parser(operation, help=("Run and wait for a delegated task." if operation == "run" else "Submit a task and return its ID without waiting for completion."))
        run_parser.add_argument("--to", required=True, help="Registered target name.")
        run_parser.add_argument("--caller", help="Source label; defaults to the inherited caller or unknown.")
        run_parser.add_argument("--chain", help="Comma-separated provenance chain; repeated targets are allowed.")
        run_parser.add_argument("--max-depth", type=int, help="May lower the inherited/configured depth budget.")
        run_parser.add_argument("--cwd", required=True, help="Existing absolute or resolvable task directory.")
        task_group = run_parser.add_mutually_exclusive_group()
        task_group.add_argument("--task", help="Short task text; prefer --task-file for long packets.")
        task_group.add_argument("--task-file", help="UTF-8 mission envelope path, or pipe mission text on stdin.")
        run_parser.add_argument("--timeout", type=int, help="Execution budget in seconds, excluding queue wait and session setup.")
        run_parser.add_argument("--queue-timeout", type=int, help="Optional queue wait limit in seconds; omitted to wait until admitted or cancelled.")
        run_parser.add_argument("--session", help="Ensure and continue a named native ACPX session.")
        run_parser.add_argument("--model", help="Explicit target model; omitted to retain target defaults.")
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
            help="Optional existing owner authority or prompt-boundary note stored in the receipt.",
        )
        run_parser.add_argument("--dry-run", action="store_true", help="Validate and print the launch plan only.")
        run_parser.set_defaults(terminal=True, handler=_run)

    for operation in ("cancel", "close"):
        control = subparsers.add_parser(operation, help=("Cancel a task by ID or a named session's active turn."
                                                        if operation == "cancel" else "Close a named native ACPX session."))
        control.add_argument("--to", required=operation == "close")
        control.add_argument("--cwd", required=operation == "close")
        control.add_argument("--session", required=operation == "close")
        if operation == "cancel":
            control.add_argument("--id", help="Cancel only this delegation_id; safe for queued tasks.")
        control.add_argument("--timeout", type=int, default=30, help="Timeout for native session control (default: 30).")
        control.add_argument("--dry-run", action="store_true")
        control.set_defaults(handler=_cancel if operation == "cancel" else _run, caller=None, chain=None, max_depth=None,
                             model=None, permissions="approve-all", terminal=True, authorization_note=None)

    for operation in ("status", "wait"):
        observer = subparsers.add_parser(operation, help="Read task progress or result without owning its execution.")
        observer.add_argument("--id", required=True, help="Full delegation_id returned by run or submit.")
        if operation == "wait":
            observer.add_argument("--timeout", type=int, default=30,
                                  help="Seconds to wait for a result; expiration never cancels the task (default: 30).")
        observer.set_defaults(handler=_observe_task)

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
    if sys.argv[1:2] == ["_launch"]:
        parser = argparse.ArgumentParser()
        parser.add_argument("--to", required=True)
        parser.set_defaults(handler=_launch)
        args = parser.parse_args(sys.argv[2:])
    elif sys.argv[1:2] == ["_worker"]:
        parser = argparse.ArgumentParser()
        parser.add_argument("--receipt-dir", required=True)
        parser.add_argument("--owner-fd", required=True, type=int)
        parser.set_defaults(handler=_worker)
        args = parser.parse_args(sys.argv[2:])
    else:
        parser = _build_parser()
        args = parser.parse_args()
    try:
        return int(args.handler(args))
    except (DelegationError, OSError) as exc:
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
