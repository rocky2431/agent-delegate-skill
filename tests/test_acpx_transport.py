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
        send({"method": "session/update", "params": {"sessionId": session, "update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": f"{marker}:turn={turns}"}}}})
        if marker == "crash":
            os._exit(8)
        stop = "max_tokens" if marker == "partial" else "end_turn"
        if marker == "wait":
            stop = "cancelled" if cancelled.wait(30) else "end_turn"
            (state / (session + ".finished")).write_text(stop)
        if marker == "brief":
            time.sleep(2)
        send({"id": request["id"], "result": {"stopReason": stop}})

    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        result: dict = {}
        if method == "initialize":
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
                     "Set AGENT_DELEGATION_TEST_ACPX to the installed pinned ACPX executable")
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
            registry.write_text(json.dumps({
                "schema_version": 1, "acpx_path": os.environ["AGENT_DELEGATION_TEST_ACPX"],
                "receipt_root": str(root / "receipts"), "default_timeout_seconds": 10,
                "max_timeout_seconds": 30, "max_delegation_depth": 4,
                "targets": {"fixture": {"argv": [sys.executable, str(Path(__file__).resolve()), "--fixture"]}},
            }))
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("AGENT_DELEGATION_")}
            env.update(AGENT_DELEGATION_CONFIG=str(registry),
                       DELEGATION_FIXTURE_HOME=str(root / "native-home"),
                       DELEGATION_FIXTURE_STATE=str(root / "state"),
                       NODE_OPTIONS="--require " + str(preload))

            def command(case: str = "", session: str | None = None, operation: str = "run") -> list[str]:
                args = [sys.executable, str(SCRIPT), operation, "--to", "fixture", "--cwd", str(root / "task")]
                if operation == "run":
                    args += ["--caller", "fixture", "--task", "CASE:" + case]
                if session:
                    args += ["--session", session]
                return args

            def start(*args: str, **kwargs: str) -> subprocess.Popen:
                return subprocess.Popen(command(*args, **kwargs), env=env, text=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            def finish(process: subprocess.Popen) -> dict:
                try:
                    stdout, stderr = process.communicate(timeout=45)
                except BaseException:
                    process.kill()
                    process.communicate()
                    raise
                payload = json.loads(stdout)
                self.assertEqual(process.returncode == 0, payload["status"] == "success", stderr)
                return payload

            def run(*args: str, **kwargs: str) -> dict:
                return finish(start(*args, **kwargs))

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
            try:
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
            finally:
                if queued is not None and queued.poll() is None:
                    queued.send_signal(signal.SIGTERM)
                    finish(queued)
                run(operation="cancel", session="review")
                if waiting is not None and waiting.poll() is None:
                    waiting.send_signal(signal.SIGTERM)
                    finish(waiting)
                run(operation="close", session="review")


if __name__ == "__main__":
    if sys.argv[1:] == ["--fixture"]:
        serve_fixture()
    else:
        unittest.main()
