"""Programming-consumer entry for the pinned ACPX runtime; local fixture, no model."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRY_SOURCE = REPO_ROOT / "runtime" / "acpx_delegate_entry.cjs"

RUNTIME_ROOT = os.environ.get("AGENT_DELEGATION_TEST_RUNTIME")


def serve_fixture() -> None:
    """A minimal ACP agent that exercises turns, permissions, and crashes."""
    outcome_lock = threading.Lock()
    observed: dict[str, object] = {}
    pending: dict[str, object] = {}
    log_path = os.environ.get("FIXTURE_LOG")
    log_lock = threading.Lock()

    def log_event(value: dict) -> None:
        if not log_path:
            return
        with log_lock, open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"pid": os.getpid(), **value}) + "\n")

    def send(value: dict) -> None:
        print(json.dumps({"jsonrpc": "2.0", **value}), flush=True)

    def send_chunk(session_id: str, text: str) -> None:
        send({"method": "session/update", "params": {"sessionId": session_id, "update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": text}}}})

    def prompt(request: dict) -> None:
        params = request["params"]
        marker = "".join(
            block.get("text", "") for block in params["prompt"]
        ).rsplit("CASE:", 1)[-1].strip()
        if marker.startswith("echo:"):
            send_chunk(params["sessionId"], marker[len("echo:"):])
            send({"id": request["id"], "result": {"stopReason": "end_turn"}})
            return
        if marker.startswith("burst:"):
            count = int(marker[len("burst:"):])
            for index in range(count):
                send_chunk(params["sessionId"], f"burst-{index:04d}-" + "x" * 980)
            send({"id": request["id"], "result": {"stopReason": "end_turn"}})
            return
        send_chunk(params["sessionId"], f"{marker}:done")
        if marker == "permission":
            with outcome_lock:
                pending["prompt_id"] = request["id"]
            send({"id": 900, "method": "session/request_permission", "params": {
                "sessionId": params["sessionId"],
                "toolCall": {
                    "toolCallId": "tool-1", "kind": "edit", "status": "pending",
                    "title": "edit target", "locations": [{"path": os.environ["FIXTURE_EDIT_PATH"]}],
                },
                "options": [
                    {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "reject-once", "name": "Reject once", "kind": "reject_once"},
                ],
            }})
            return
        if marker == "hold":
            # Spec-shaped cancellation: the prompt response is withheld until
            # the client sends session/cancel, answered below with cancelled.
            with outcome_lock:
                pending["held_prompt_id"] = request["id"]
            return
        if marker == "authfail":
            # An authentication-looking failure after the prompt arrived: it
            # must surface as a turn-phase failure, never as before-prompt.
            send({"id": request["id"], "error": {
                "code": -32000, "message": "authentication required"}})
            return
        if marker == "crash":
            import os as _os
            _os._exit(8)
        send({"id": request["id"], "result": {"stopReason": "end_turn"}})

    session_delay = float(os.environ.get("FIXTURE_SESSION_DELAY_MS", "0")) / 1000.0

    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        log_event({"kind": "request", "method": method, "id": request.get("id")})
        result: dict = {}
        if method == "initialize":
            # FIXTURE_PROTOCOL_VERSION selects the negotiated version: an int
            # (default 1), or "none" for an initialize result that carries no
            # protocolVersion at all.
            version = os.environ.get("FIXTURE_PROTOCOL_VERSION", "1")
            if version != "none":
                result["protocolVersion"] = int(version)
            result.update({"agentCapabilities": {}, "authMethods": [],
                           "agentInfo": {"name": "entry-fixture", "version": "1"}})
        elif method == "session/new":
            if os.environ.get("FIXTURE_FAIL_SESSION_NEW") == "1":
                send({"id": request["id"], "error": {
                    "code": -32000, "message": "authentication required"}})
                continue
            if session_delay:
                time.sleep(session_delay)
            result = {"sessionId": uuid.uuid4().hex}
        elif method == "session/prompt":
            threading.Thread(target=prompt, args=(request,), daemon=True).start()
            continue
        elif method == "session/cancel":
            with outcome_lock:
                held_id = pending.pop("held_prompt_id", None)
            if held_id is not None:
                send({"id": held_id, "result": {"stopReason": "cancelled"}})
            continue
        elif isinstance(request.get("id"), int) and request["id"] == 900 and "result" in request:
            with outcome_lock:
                observed["permission_response"] = request["result"]
                prompt_id = pending.pop("prompt_id", None)
            state = Path(os.environ["FIXTURE_STATE"])
            state.write_text(json.dumps(observed, default=str), encoding="utf-8")
            if prompt_id is not None:
                send({"id": prompt_id, "result": {"stopReason": "end_turn"}})
            continue
        if "id" in request:
            send({"id": request["id"], "result": result})


@unittest.skipUnless(RUNTIME_ROOT, "Set AGENT_DELEGATION_TEST_RUNTIME to the installed pinned runtime root")
class DelegateEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="delegation-entry-")
        self.root = Path(self.temporary.name)
        # The entry resolves require('acpx/runtime') from runtime/node_modules;
        # a symlink to the reviewed installed runtime keeps that resolution
        # without modifying the install itself.
        link = self.root / "runtime"
        link.mkdir()
        (link / "node_modules").symlink_to(Path(RUNTIME_ROOT) / "node_modules")
        self.entry = link / "acpx_delegate_entry.cjs"
        self.entry.write_text(ENTRY_SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
        self.state = self.root / "fixture-state.json"
        self.log = self.root / "fixture-log.jsonl"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(self, prompt: str, **overrides: object) -> dict:
        payload = {
            "schema": "acpx-delegate-turn-request/v1",
            "agent_argv": [sys.executable, str(Path(__file__).resolve()), "--fixture"],
            "cwd": str(self.root),
            "prompt": prompt,
            "timeout_ms": 20000,
        }
        payload.update(overrides)
        return payload

    def spawn_entry(self, extra_env: dict | None = None) -> subprocess.Popen:
        env = {
            key: value for key, value in os.environ.items() if not key.startswith("AGENT_DELEGATION_")
        }
        env["FIXTURE_STATE"] = str(self.state)
        env["FIXTURE_EDIT_PATH"] = str(self.root / "granted")
        env["FIXTURE_LOG"] = str(self.log)
        if extra_env:
            env.update(extra_env)
        process = subprocess.Popen(
            ["node", str(self.entry)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, cwd=str(self.root),
        )
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        return process

    def write_bytes(self, process: subprocess.Popen, data: bytes) -> None:
        process.stdin.write(data)
        process.stdin.flush()

    def collect(
        self,
        process: subprocess.Popen,
        *,
        on_started=None,
        on_event=None,
        on_permission=None,
        pre_read_delay: float = 0.0,
        deadline_s: float = 45.0,
    ) -> tuple[int, list[dict], str]:
        """Read stdout/stderr lines until the result line plus process exit.

        Returns the exit code, the stdout messages in arrival order (result
        line included where it arrived), and the decoded stderr text.
        """
        import selectors
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        selector.register(process.stderr, selectors.EVENT_READ)
        if pre_read_delay:
            time.sleep(pre_read_delay)
        messages: list[dict] = []
        stderr_chunks: list[bytes] = []
        result_seen = False
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            if not selector.get_map():
                break
            for key, _ in selector.select(0.1):
                line = key.fileobj.readline()
                if not line:
                    selector.unregister(key.fileobj)
                    continue
                if key.fileobj is process.stderr:
                    stderr_chunks.append(line)
                    continue
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except json.JSONDecodeError:
                    messages.append({"raw": text})
                    continue
                messages.append(message)
                if message.get("type") == "started" and on_started is not None:
                    on_started(process)
                elif message.get("type") == "event" and on_event is not None:
                    on_event(process, message)
                elif message.get("type") == "permission_request" and on_permission is not None:
                    on_permission(process, message)
                elif message.get("type") == "result":
                    result_seen = True
            if result_seen and process.poll() is not None:
                break
        try:
            process.kill()
        except OSError:
            pass
        remainder = process.stdout.read() or b""
        for raw_line in remainder.splitlines():
            text = raw_line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                messages.append(json.loads(text))
            except json.JSONDecodeError:
                messages.append({"raw": text})
        stderr_text = (b"".join(stderr_chunks) + (process.stderr.read() or b"")).decode(
            "utf-8", errors="replace")
        process.wait(timeout=10)
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass
        return process.returncode, messages, stderr_text

    def run_entry(self, payload: dict, decide: str | None = None) -> tuple[int, list[dict], str]:
        process = self.spawn_entry()
        self.write_bytes(process, (json.dumps(payload) + "\n").encode("utf-8"))
        decisions: list[dict] = []

        def answer_permission(proc: subprocess.Popen, message: dict) -> None:
            raw = message.get("request", {}).get("raw", {})
            options = raw.get("options") or []
            option = None
            if decide == "allow":
                option = next((o for o in options if o.get("kind") == "allow_once"), None)
            elif decide == "reject":
                option = next((o for o in options if o.get("kind") == "reject_once"), None)
            decisions.append({
                "kind": raw.get("toolCall", {}).get("kind"),
                "locations": raw.get("toolCall", {}).get("locations"),
                "option_ids": [o.get("optionId") for o in options],
            })
            chosen = option["optionId"] if option else "no-such-option"
            self.write_bytes(proc, (json.dumps({
                "type": "permission_decision", "id": message["id"], "optionId": chosen,
            }) + "\n").encode("utf-8"))

        code, messages, stderr = self.collect(process, on_permission=answer_permission)
        for decision in decisions:
            messages.append({"type": "fixture_decision", **decision})
        return code, messages, stderr

    def fixture_methods(self) -> list[str | None]:
        if not self.log.exists():
            return []
        return [
            json.loads(line).get("method")
            for line in self.log.read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line).get("kind") == "request"
        ]

    def test_happy_turn_streams_events_and_completes(self) -> None:
        code, messages, stderr = self.run_entry(self.request("CASE: happy"))
        self.assertEqual(code, 0, stderr)
        started = next(m for m in messages if m.get("type") == "started")
        self.assertEqual(started["acpx_version"], "0.13.2")
        result = next(m for m in messages if m.get("type") == "result")
        self.assertEqual(result["result"]["status"], "completed")
        self.assertEqual(result["result"]["stopReason"], "end_turn")
        # The observed negotiation report travels beside the typed terminal:
        # a completed turn means the store actually observed version 1.
        self.assertEqual(result.get("protocolVersion"), 1)
        self.assertTrue(any(m.get("type") == "event" and "happy:done" in json.dumps(m) for m in messages))

    def test_unsupported_protocol_version_is_refused_before_any_prompt(self) -> None:
        process = self.spawn_entry(extra_env={"FIXTURE_PROTOCOL_VERSION": "2"})
        self.write_bytes(process, (json.dumps(self.request("CASE: happy")) + "\n").encode("utf-8"))
        code, messages, stderr = self.collect(process)
        self.assertEqual(code, 1, stderr)
        result = next(m for m in messages if m.get("type") == "result")
        self.assertEqual(result["result"]["status"], "failed")
        error = result["result"]["error"]
        self.assertEqual(error.get("code"), "unsupported_protocol_version")
        self.assertEqual(error.get("phase"), "ensure_session")
        self.assertIn("Agent negotiated ACP protocolVersion 2", str(error.get("message", "")))
        # The refused negotiation is still the observed fact: the result line
        # reports version 2 so no consumer can claim a v1 transport.
        self.assertEqual(result.get("protocolVersion"), 2)
        methods = self.fixture_methods()
        # Honest limit: the pinned manager creates the agent session before
        # the negotiated version reaches the persisted session record, so
        # initialize and session/new may both have happened — but the prompt
        # is never submitted and no turn events stream.
        self.assertIn("initialize", methods)
        self.assertIn("session/new", methods)
        self.assertNotIn("session/prompt", methods)
        self.assertFalse(any(m.get("type") == "event" for m in messages))

    def test_unobserved_protocol_version_is_refused_as_unproven(self) -> None:
        process = self.spawn_entry(extra_env={"FIXTURE_PROTOCOL_VERSION": "none"})
        self.write_bytes(process, (json.dumps(self.request("CASE: happy")) + "\n").encode("utf-8"))
        code, messages, stderr = self.collect(process)
        self.assertEqual(code, 1, stderr)
        result = next(m for m in messages if m.get("type") == "result")
        self.assertEqual(result["result"]["status"], "failed")
        error = result["result"]["error"]
        self.assertEqual(error.get("code"), "unsupported_protocol_version")
        self.assertEqual(error.get("phase"), "ensure_session")
        self.assertIn("No ACP protocolVersion was observed", str(error.get("message", "")))
        # An unproven negotiation stays unobserved: no version sibling, so a
        # consumer can never read a synthesized 1 out of this refusal.
        self.assertNotIn("protocolVersion", result)
        self.assertNotIn("session/prompt", self.fixture_methods())

    def test_permission_round_trip_uses_the_exact_allow_once_option(self) -> None:
        code, messages, stderr = self.run_entry(self.request("CASE: permission"), decide="allow")
        self.assertEqual(code, 0, stderr)
        decision = next(m for m in messages if m.get("type") == "fixture_decision")
        self.assertEqual(decision["kind"], "edit")
        self.assertEqual(decision["locations"], [{"path": str(self.root / "granted")}])
        self.assertEqual(decision["option_ids"], ["allow-once", "reject-once"])
        result = next(m for m in messages if m.get("type") == "result")
        self.assertEqual(result["result"]["status"], "completed")
        observed = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(observed["permission_response"],
                         {"outcome": {"outcome": "selected", "optionId": "allow-once"}})

    def test_unknown_or_missing_option_fails_closed_to_rejection(self) -> None:
        code, messages, stderr = self.run_entry(self.request("CASE: permission"), decide=None)
        self.assertEqual(code, 0, stderr)
        observed = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(observed["permission_response"],
                         {"outcome": {"outcome": "selected", "optionId": "reject-once"}})

    def test_rejected_permission_keeps_the_turn_terminal(self) -> None:
        code, messages, stderr = self.run_entry(self.request("CASE: permission"), decide="reject")
        self.assertEqual(code, 0, stderr)
        observed = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(observed["permission_response"],
                         {"outcome": {"outcome": "selected", "optionId": "reject-once"}})
        result = next(m for m in messages if m.get("type") == "result")
        self.assertEqual(result["result"]["status"], "completed")

    def test_agent_crash_reports_a_failed_turn(self) -> None:
        code, messages, stderr = self.run_entry(self.request("CASE: crash"))
        self.assertEqual(code, 1, stderr)
        result = next(m for m in messages if m.get("type") == "result")
        self.assertEqual(result["result"]["status"], "failed")
        self.assertIn("message", result["result"]["error"])
        self.assertEqual(result["result"]["error"].get("phase"), "turn")

    def test_session_init_auth_failure_is_phase_ensure_session_before_prompt(self) -> None:
        process = self.spawn_entry(extra_env={"FIXTURE_FAIL_SESSION_NEW": "1"})
        self.write_bytes(process, (json.dumps(self.request("CASE: happy")) + "\n").encode("utf-8"))
        code, messages, stderr = self.collect(process)
        self.assertEqual(code, 1, stderr)
        result = next(m for m in messages if m.get("type") == "result")
        self.assertEqual(result["result"]["status"], "failed")
        error = result["result"]["error"]
        self.assertEqual(error.get("phase"), "ensure_session")
        self.assertIn("authentication required", str(error.get("message", "")))
        # An initialization failure never persisted a session record, so no
        # negotiation was observed: the version stays absent, never 1.
        self.assertNotIn("protocolVersion", result)
        self.assertNotIn("session/prompt", self.fixture_methods())

    def test_auth_like_failure_after_prompt_is_phase_turn(self) -> None:
        code, messages, stderr = self.run_entry(self.request("CASE: authfail"))
        self.assertEqual(code, 1, stderr)
        result = next(m for m in messages if m.get("type") == "result")
        self.assertEqual(result["result"]["status"], "failed")
        error = result["result"]["error"]
        self.assertEqual(error.get("phase"), "turn")
        self.assertIn("authentication required", str(error.get("message", "")))
        self.assertIn("session/prompt", self.fixture_methods())

    def test_protocol_misuse_is_rejected_before_any_turn(self) -> None:
        code, messages, stderr = self.run_entry({"schema": "acpx-delegate-turn-request/v1"})
        self.assertEqual(code, 2, stderr)
        self.assertFalse(any(m.get("type") == "started" for m in messages))

    def test_only_deny_all_is_accepted_and_nothing_persists_sessions(self) -> None:
        code, _, stderr = self.run_entry(self.request("CASE: happy", permission_mode="approve-all"))
        self.assertEqual(code, 2, stderr)
        source = ENTRY_SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("createFileSessionStore", source)
        self.assertNotIn("sessionOptions:", source)
        self.assertNotIn("stateDir", source)

    def test_split_multibyte_request_text_arrives_unchanged(self) -> None:
        chinese = "中文路径/委派使命"
        payload = self.request(f"CASE: echo:{chinese}")
        raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        split = raw.index("中".encode("utf-8")) + 1  # inside the 3-byte UTF-8 sequence
        process = self.spawn_entry()
        self.write_bytes(process, raw[:split])
        time.sleep(0.4)
        self.write_bytes(process, raw[split:])
        code, messages, stderr = self.collect(process)
        self.assertEqual(code, 0, stderr)
        result = next(m for m in messages if m.get("type") == "result")
        self.assertEqual(result["result"]["status"], "completed")
        event_text = "".join(
            str(m.get("event", {}).get("text", "")) for m in messages if m.get("type") == "event")
        self.assertIn(chinese, event_text)

    def test_fragmented_permission_decision_settles(self) -> None:
        process = self.spawn_entry()
        self.write_bytes(process, (json.dumps(self.request("CASE: permission")) + "\n").encode("utf-8"))

        def decide_in_fragments(proc: subprocess.Popen, message: dict) -> None:
            decision = (json.dumps({
                "type": "permission_decision", "id": message["id"], "optionId": "allow-once",
            }) + "\n").encode("utf-8")
            for offset in range(0, len(decision), 9):
                self.write_bytes(proc, decision[offset:offset + 9])
                time.sleep(0.12)

        code, messages, stderr = self.collect(process, on_permission=decide_in_fragments)
        self.assertEqual(code, 0, stderr)
        observed = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(observed["permission_response"],
                         {"outcome": {"outcome": "selected", "optionId": "allow-once"}})
        result = next(m for m in messages if m.get("type") == "result")
        self.assertEqual(result["result"]["status"], "completed")

    def test_stdin_eof_settles_the_pending_permission_waiter(self) -> None:
        process = self.spawn_entry()
        self.write_bytes(process, (json.dumps(self.request("CASE: permission")) + "\n").encode("utf-8"))

        def hang_up(proc: subprocess.Popen, message: dict) -> None:
            proc.stdin.close()

        code, messages, stderr = self.collect(process, on_permission=hang_up)
        self.assertEqual(code, 0, stderr)
        # No decision ever arrives; the pending waiter must settle on EOF so the
        # pinned deny-all mode fails closed instead of hanging the turn.
        observed = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(observed["permission_response"],
                         {"outcome": {"outcome": "selected", "optionId": "reject-once"}})
        result = next(m for m in messages if m.get("type") == "result")
        self.assertEqual(result["result"]["status"], "completed")

    def test_cancel_during_delayed_session_submits_no_prompt(self) -> None:
        process = self.spawn_entry(extra_env={"FIXTURE_SESSION_DELAY_MS": "3000"})
        self.write_bytes(process, (json.dumps(self.request("CASE: happy")) + "\n").encode("utf-8"))
        started_at = time.monotonic()

        def cancel(proc: subprocess.Popen) -> None:
            proc.send_signal(signal.SIGTERM)

        code, messages, stderr = self.collect(process, on_started=cancel, deadline_s=40.0)
        elapsed = time.monotonic() - started_at
        self.assertEqual(code, 0, stderr)
        result = next(m for m in messages if m.get("type") == "result")
        self.assertEqual(result["result"]["status"], "cancelled")
        self.assertEqual(result["result"].get("stopReason"), "cancelled")
        methods = self.fixture_methods()
        self.assertIn("session/new", methods)
        self.assertNotIn("session/prompt", methods)
        self.assertLess(elapsed, 25.0)

    def test_cancel_active_turn_keeps_the_canonical_cancelled_result(self) -> None:
        process = self.spawn_entry()
        self.write_bytes(process, (json.dumps(self.request("CASE: hold")) + "\n").encode("utf-8"))
        cancelled = False

        def cancel_on_first_event(proc: subprocess.Popen, message: dict) -> None:
            nonlocal cancelled
            if not cancelled:
                cancelled = True
                proc.send_signal(signal.SIGTERM)

        code, messages, stderr = self.collect(process, on_event=cancel_on_first_event, deadline_s=40.0)
        self.assertEqual(code, 0, stderr)
        result = next(m for m in messages if m.get("type") == "result")
        self.assertEqual(result["result"]["status"], "cancelled")
        self.assertEqual(result["result"].get("stopReason"), "cancelled")
        self.assertNotIn("result_timeout", result["result"])
        self.assertIn("session/prompt", self.fixture_methods())

    def test_tail_events_all_arrive_before_the_result_line(self) -> None:
        count = 150
        process = self.spawn_entry()
        self.write_bytes(process, (json.dumps(self.request(f"CASE: burst:{count}")) + "\n").encode("utf-8"))
        code, messages, stderr = self.collect(process, pre_read_delay=2.0, deadline_s=60.0)
        self.assertEqual(code, 0, stderr)
        self.assertFalse(any("raw" in m for m in messages),
                         f"unparseable stdout line: {[m for m in messages if 'raw' in m][:1]}")
        result_indexes = [i for i, m in enumerate(messages) if m.get("type") == "result"]
        self.assertEqual(len(result_indexes), 1, messages[-3:])
        self.assertEqual(result_indexes[0], len(messages) - 1, "result must stay the last stdout line")
        result = messages[result_indexes[0]]
        self.assertEqual(result["result"]["status"], "completed")
        seen = set()
        for m in messages:
            if m.get("type") != "event":
                continue
            text = str(m.get("event", {}).get("text", ""))
            if text.startswith("burst-"):
                seen.add(text.split("-", 2)[1])
        self.assertEqual(seen, {f"{index:04d}" for index in range(count)})

    def test_oneshot_close_does_not_respawn_the_agent(self) -> None:
        code, messages, stderr = self.run_entry(self.request("CASE: happy"))
        self.assertEqual(code, 0, stderr)
        result = next(m for m in messages if m.get("type") == "result")
        self.assertEqual(result["result"]["status"], "completed")
        methods = self.fixture_methods()
        self.assertEqual(methods.count("initialize"), 1, methods)


if __name__ == "__main__":
    if sys.argv[1:] == ["--fixture"]:
        serve_fixture()
    else:
        unittest.main()
