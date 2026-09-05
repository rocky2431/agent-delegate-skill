"""Opt-in real ACPX transport check; the ACP worker is local and uses no model."""

from __future__ import annotations

import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid


SCRIPT = Path(__file__).resolve().parents[1] / "plugins/agent-delegation/skills/agent-delegation/scripts/agent_delegate.py"


def serve_fixture() -> None:
    state = Path(os.environ["DELEGATION_FIXTURE_STATE"])
    output_lock = threading.Lock()
    cancelled = threading.Event()
    native_version = subprocess.check_output([os.environ["CODEX_PATH"], "--version"], text=True).strip() if os.environ.get("DELEGATION_FIXTURE_NATIVE") else "unused"

    def send(value: dict) -> None:
        with output_lock:
            print(json.dumps({"jsonrpc": "2.0", **value}), flush=True)

    def prompt(request: dict) -> None:
        params = request["params"]
        session = params["sessionId"]
        record = state / (session + ".json")
        turns = json.loads(record.read_text()) + 1
        record.write_text(json.dumps(turns))
        text = "".join(block.get("text", "") for block in params["prompt"])
        marker = text.rsplit("CASE:", 1)[-1].strip()
        cancelled.clear()
        response = native_version if marker == "identity" else f"{marker}:turn={turns}"
        send({"method": "session/update", "params": {"sessionId": session, "update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": response}}}})
        if marker == "crash":
            os._exit(8)
        stop = "max_tokens" if marker == "partial" else "end_turn"
        if marker == "wait" or marker.startswith("delay:"):
            duration = 30 if marker == "wait" else float(marker.split(":", 1)[1])
            stop = "cancelled" if cancelled.wait(duration) else "end_turn"
            (state / (session + ".finished")).write_text(stop)
        if marker == "brief":
            time.sleep(2)
        send({"id": request["id"], "result": {"stopReason": stop}})

    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        result: dict = {}
        if method == "initialize":
            time.sleep(float(os.environ.get("DELEGATION_FIXTURE_STARTUP_DELAY", "0")))
            result = {"protocolVersion": 1, "agentCapabilities": {"loadSession": True},
                      "authMethods": [], "agentInfo": {"name": "transport-fixture", "version": "1"}}
        elif method == "session/new":
            session = uuid.uuid4().hex
            (state / (session + ".json")).write_text("0")
            result = {"sessionId": session}
        elif method == "session/prompt":
            threading.Thread(target=prompt, args=(request,), daemon=True).start()
            continue
        elif method == "session/cancel":
            cancelled.set()
        if "id" in request:
            send({"id": request["id"], "result": result})


@unittest.skipUnless(os.environ.get("AGENT_DELEGATION_TEST_ACPX"),
                     "Set AGENT_DELEGATION_TEST_ACPX to the installed ACPX executable")
class NativeTransportTests(unittest.TestCase):
    def test_independent_tasks_continuation_and_interruption(self) -> None:
        with tempfile.TemporaryDirectory(prefix="delegation-acpx-") as temporary:
            root = Path(temporary)
            (root / "task").mkdir()
            (root / "state").mkdir()
            (root / "native-home").mkdir()
            # ACPX uses Node's os.homedir(); isolate it without touching user config.
            preload = root / "isolate-home.cjs"
            preload.write_text("require('node:os').homedir=()=>process.env.DELEGATION_FIXTURE_HOME;"
                               "require('node:module').syncBuiltinESMExports();\n")
            registry = root / "registry.json"
            native_cli = root / "native-cli"
            for version in (1, 2):
                executable = root / f"native-v{version}"
                executable.write_text(f"#!/bin/sh\necho native-v{version}\n")
                executable.chmod(0o755)
            native_cli.symlink_to(root / "native-v1")
            original_argv = [sys.executable, str(Path(__file__).resolve()), "--fixture"]
            registry.write_text(json.dumps({
                "schema_version": 1, "acpx_path": os.environ["AGENT_DELEGATION_TEST_ACPX"],
                "receipt_root": str(root / "receipts"), "default_timeout_seconds": 10,
                "max_timeout_seconds": 30, "max_delegation_depth": 4,
                "targets": {"fixture": {"argv": original_argv,
                    "version_argv": [str(native_cli), "--version"],
                    "cli_env": {"CODEX_PATH": str(native_cli)}}},
            }))
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("AGENT_DELEGATION_")}
            env.update(AGENT_DELEGATION_CONFIG=str(registry),
                       DELEGATION_FIXTURE_HOME=str(root / "native-home"),
                       DELEGATION_FIXTURE_STATE=str(root / "state"),
                       DELEGATION_FIXTURE_NATIVE="1",
                       NODE_OPTIONS="--require " + str(preload))

            def command(case: str = "", session: str | None = None, operation: str = "run",
                        timeout: int | None = None, queue_timeout: int | None = None) -> list[str]:
                args = [sys.executable, str(SCRIPT), operation, "--to", "fixture", "--cwd", str(root / "task")]
                if operation in ("run", "submit"):
                    args += ["--caller", "fixture", "--task", "CASE:" + case]
                if session:
                    args += ["--session", session]
                if timeout is not None:
                    args += ["--timeout", str(timeout)]
                if queue_timeout is not None:
                    args += ["--queue-timeout", str(queue_timeout)]
                return args

            def start(*args: str, **kwargs: str) -> subprocess.Popen:
                return subprocess.Popen(command(*args, **kwargs), env=env, text=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            def finish(process: subprocess.Popen, observation: bool = False) -> dict:
                try:
                    stdout, stderr = process.communicate(timeout=45)
                except BaseException:
                    process.kill()
                    process.communicate()
                    raise
                payload = json.loads(stdout)
                self.assertEqual(process.returncode == 0,
                                 observation or payload["status"] == "success" or not payload.get("terminal", True), stderr)
                return payload

            def run(*args: str, **kwargs: str) -> dict:
                return finish(start(*args, **kwargs), observation=kwargs.get("operation") == "submit")

            def wait_for_event(marker: str) -> None:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if any(marker in path.read_text() for path in (root / "receipts").glob("*/events.ndjson")):
                        return
                    time.sleep(0.05)
                self.fail("Turn did not stream: " + marker)

            def wait_for_session(process: subprocess.Popen) -> None:
                deadline = time.monotonic() + 10
                messages = b""
                while time.monotonic() < deadline:
                    if select.select([process.stderr], [], [], 0.1)[0]:
                        part = os.read(process.stderr.fileno(), 4096)
                        if not part:
                            break
                        messages += part
                        if b'"status": "waiting"' in messages:
                            return
                self.fail("Concurrent turn did not wait: " + messages.decode())

            waiting = None
            queued = None
            submitted_ids = []
            observer = None

            def submit(*args, **kwargs) -> dict:
                result = run(*args, operation="submit", **kwargs)
                submitted_ids.append(result["delegation_id"])
                return result

            def task_control(operation: str, task: dict, timeout: int = 0) -> dict:
                args = [sys.executable, str(SCRIPT), operation, "--id", task["delegation_id"]]
                if operation == "wait":
                    args += ["--timeout", str(timeout)]
                return finish(subprocess.Popen(args, env=env, text=True,
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE),
                              observation=operation != "wait")

            def wait_for_phase(task: dict, phase: str) -> dict:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    result = task_control("status", task)
                    if result["status"] == phase:
                        return result
                    time.sleep(0.05)
                self.fail("Task did not reach phase " + phase + ": " + str(result))

            try:
                legacy = run("fresh", session="legacy")
                config = json.loads(registry.read_text())
                config["targets"]["fixture"].update(
                    launch_argv=[sys.executable, str(SCRIPT), "_launch", "--to", "fixture"],
                    legacy_argv=[original_argv])
                registry.write_text(json.dumps(config))
                continued_legacy = run("continue", session="legacy")
                self.assertEqual(continued_legacy["acp_session_id"], legacy["acp_session_id"])
                self.assertEqual(continued_legacy["assistant_text"], "continue:turn=2")
                self.assertEqual(continued_legacy["runtime_identity"]["observation"], "legacy_session_unverified")
                run(operation="close", session="legacy")

                original = submit("wait", session="upgrade")
                wait_for_event("wait:turn=1")
                config["targets"]["fixture"]["argv"] = [*original_argv, "--generation-two"]
                registry.write_text(json.dumps(config))
                native_cli.unlink()
                native_cli.symlink_to(root / "native-v2")
                independent = run("identity")
                self.assertEqual(independent["assistant_text"], "native-v2")
                self.assertEqual(independent["runtime_identity"]["cli"]["version"], "native-v2")
                self.assertIn("--generation-two", independent["runtime_identity"]["resolved_target_argv"])
                self.assertEqual(task_control("status", original)["status"], "running")
                task_control("cancel", original)
                result = task_control("wait", original, 10)
                self.assertEqual(result["status"], "cancelled")
                self.assertEqual(result["runtime_identity"]["cli"]["version"], "native-v1")
                self.assertNotIn("--generation-two", result["runtime_identity"]["resolved_target_argv"])
                warm = run("identity", session="upgrade")
                self.assertEqual(warm["acp_session_id"], result["acp_session_id"])
                self.assertEqual(warm["assistant_text"], "native-v1")
                self.assertEqual(warm["runtime_identity"]["cli"]["version"], "native-v1")
                run(operation="close", session="upgrade")
                fresh = run("identity", session="upgrade")
                self.assertEqual(fresh["assistant_text"], "native-v2")
                self.assertNotEqual(fresh["acp_session_id"], warm["acp_session_id"])

                first, second = [finish(process) for process in [start("fresh"), start("fresh")]]
                self.assertEqual(first["assistant_text"], "fresh:turn=1")
                self.assertEqual(second["assistant_text"], "fresh:turn=1")
                self.assertNotEqual(first["acp_session_id"], second["acp_session_id"])
                partial = run("partial", session="review")
                self.assertEqual(partial["status"], "incomplete")
                continued = run("continue", session="review")
                self.assertEqual(continued["assistant_text"], "continue:turn=2")
                self.assertEqual(partial["acp_session_id"], continued["acp_session_id"])

                waiting = start("wait", session="review")
                wait_for_event("wait:turn=3")
                queued = start("must-not-run", session=" review ")
                wait_for_session(queued)
                queued.send_signal(signal.SIGTERM)
                abandoned = finish(queued)
                self.assertEqual(abandoned["status"], "cancelled")
                self.assertEqual(abandoned["assistant_text"], "")
                self.assertIsNone(abandoned["cancellation_exit_code"])
                self.assertIsNone(waiting.poll())
                waiting.send_signal(signal.SIGTERM)
                interrupted = finish(waiting)
                self.assertEqual(interrupted["status"], "cancelled")
                self.assertEqual(interrupted["assistant_text"], "wait:turn=3")
                self.assertEqual(interrupted["cancellation_exit_code"], 0)
                stopped = root / "state" / (continued["acp_session_id"] + ".finished")
                deadline = time.monotonic() + 5
                while not stopped.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertEqual(stopped.read_text(), "cancelled")
                self.assertEqual(run("resume", session="review")["assistant_text"], "resume:turn=4")
                waiting = start("brief", session="review")
                wait_for_event("brief:turn=5")
                queued = start("queued", session="review")
                wait_for_session(queued)
                self.assertEqual(finish(waiting)["status"], "success")
                self.assertEqual(finish(queued)["assistant_text"], "queued:turn=6")
                self.assertEqual(run(operation="close", session="review")["status"], "success")
                self.assertEqual(run("reopen", session="review")["assistant_text"], "reopen:turn=1")
                crashed = run("crash")
                self.assertNotEqual(crashed["status"], "success")
                self.assertEqual(crashed["assistant_text"], "crash:turn=1")

                active = submit("wait", session="jobs")
                wait_for_event("wait:turn=1")
                snapshot = task_control("wait", active, 1)
                self.assertTrue(snapshot["wait_timed_out"])
                self.assertFalse(snapshot["terminal"])
                observer = subprocess.Popen([sys.executable, str(SCRIPT), "wait", "--id",
                    active["delegation_id"], "--timeout", "60"], env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                time.sleep(0.2)
                self.assertIsNone(observer.poll())
                observer.send_signal(signal.SIGTERM)
                observer.communicate(timeout=5)
                self.assertEqual(task_control("status", active)["status"], "running")

                abandoned = submit("must-not-run", session="jobs")
                wait_for_phase(abandoned, "queued")
                task_control("cancel", abandoned)
                result = task_control("wait", abandoned, 10)
                self.assertEqual(result["status"], "cancelled")
                self.assertEqual(result["assistant_text"], "")
                self.assertEqual(result["execution_seconds"], 0)
                self.assertIsNone(result["cancellation_exit_code"])

                expired = submit("must-not-run", session="jobs", queue_timeout=1)
                result = task_control("wait", expired, 10)
                self.assertEqual(result["status"], "timeout")
                self.assertEqual(result["timeout_phase"], "queue")
                self.assertEqual(result["execution_seconds"], 0)
                self.assertIsNone(result["cancellation_exit_code"])

                lost = submit("must-not-run", session="jobs")
                lost_state = wait_for_phase(lost, "queued")
                os.kill(lost_state["worker_pid"], signal.SIGKILL)
                result = task_control("wait", lost, 5)
                self.assertEqual(result["status"], "incomplete")
                self.assertEqual(result["execution_state"], "unknown")
                for field in ("duration_seconds", "queue_wait_seconds", "execution_seconds"):
                    self.assertIsNone(result[field])
                self.assertEqual(task_control("status", active)["status"], "running")

                task_control("cancel", active)
                result = task_control("wait", active, 10)
                self.assertEqual(result["status"], "cancelled")
                self.assertEqual(result["stop_reason"], "cancelled")
                self.assertEqual(result["cancellation_exit_code"], 0)
                self.assertEqual(result["assistant_text"], "wait:turn=1")
                continued = submit("brief", session="jobs")
                wait_for_event("brief:turn=2")
                task_control("cancel", active)
                self.assertEqual(task_control("wait", continued, 10)["status"], "success")

                first = submit("delay:5", session="budget")
                wait_for_event("delay:5:turn=1")
                second = submit("brief", session="budget", timeout=3)
                wait_for_phase(second, "queued")
                self.assertEqual(task_control("wait", first, 10)["status"], "success")
                result = task_control("wait", second, 10)
                self.assertEqual(result["status"], "success")
                self.assertGreater(result["queue_wait_seconds"], result["timeout_seconds"])
                self.assertGreaterEqual(result["execution_seconds"], 2)
                self.assertIsNone(result["timeout_phase"])

                env["DELEGATION_FIXTURE_STARTUP_DELAY"] = "2"
                try:
                    early = submit("wait", session="startup")
                finally:
                    env.pop("DELEGATION_FIXTURE_STARTUP_DELAY")
                wait_for_phase(early, "running")
                task_control("cancel", early)
                result = task_control("wait", early, 15)
                self.assertEqual(result["status"], "cancelled")
                self.assertEqual(result["stop_reason"], "cancelled")
                self.assertEqual(result["cancellation_exit_code"], 0)
                timed_out = run("wait", timeout=2)
                self.assertEqual(timed_out["status"], "timeout")
                self.assertEqual(timed_out["timeout_phase"], "execution")
                self.assertEqual(timed_out["assistant_text"], "wait:turn=1")
            finally:
                if observer is not None and observer.poll() is None:
                    observer.send_signal(signal.SIGTERM)
                    observer.communicate(timeout=5)
                for task_id in submitted_ids:
                    with self.subTest(cleanup_task=task_id):
                        task_control("cancel", {"delegation_id": task_id})
                        task_control("wait", {"delegation_id": task_id}, 15)
                for session in ("jobs", "budget", "startup", "upgrade", "legacy"):
                    with self.subTest(close_session=session):
                        run(operation="close", session=session)
                if queued is not None and queued.poll() is None:
                    queued.send_signal(signal.SIGTERM)
                    finish(queued)
                run(operation="cancel", session="review")
                if waiting is not None and waiting.poll() is None:
                    waiting.send_signal(signal.SIGTERM)
                    finish(waiting)
                run(operation="close", session="review")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--fixture"]:
        serve_fixture()
    else:
        unittest.main()
