from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    REPO_ROOT
    / "plugins"
    / "agent-delegation"
    / "skills"
    / "agent-delegation"
    / "scripts"
    / "agent_delegate.py"
)


class AgentDelegateCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.cwd = self.root / "task"
        self.cwd.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.acpx = self.bin / "acpx"
        self.target = self.bin / "target-acp"
        self.acpx.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'params': {'update': {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'delegated ok'}}}}))\n"
            "print(json.dumps({'result': {'stopReason': 'end_turn'}}))\n",
            encoding="utf-8",
        )
        self.target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.acpx.chmod(0o755)
        self.target.chmod(0o755)
        self.acpx_config = self.root / "acpx.json"
        targets = {
            "alpha": {
                "argv": [str(self.target)],
                "observed_version": "test",
                "provenance": "test fixture",
            },
            "beta": {
                "argv": [str(self.target)],
                "observed_version": "test",
                "provenance": "test fixture",
            },
        }
        self.registry = self.root / "registry.json"
        self.registry.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "acpx_path": str(self.acpx),
                    "acpx_config_path": str(self.acpx_config),
                    "receipt_root": str(self.root / "receipts"),
                    "default_timeout_seconds": 10,
                    "max_timeout_seconds": 30,
                    "max_delegation_depth": 4,
                    "targets": targets,
                }
            ),
            encoding="utf-8",
        )
        self.acpx_config.write_text(
            json.dumps({"agents": {name: {"argv": record["argv"]} for name, record in targets.items()}}),
            encoding="utf-8",
        )
        clean_environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("AGENT_DELEGATION_")
        }
        self.environment = {
            **clean_environment,
            "AGENT_DELEGATION_CONFIG": str(self.registry),
            "AGENT_DELEGATION_HOME": str(self.root),
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *arguments: str, environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            text=True,
            capture_output=True,
            env=environment or self.environment,
            timeout=20,
            check=False,
        )

    def test_dry_run_preserves_normal_capabilities_without_executing(self) -> None:
        result = self.run_cli(
            "run",
            "--to",
            "beta",
            "--caller",
            "alpha",
            "--cwd",
            str(self.cwd),
            "--task",
            "summarize the input",
            "--dry-run",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["chain"], ["alpha", "beta"])
        self.assertEqual(payload["permissions"], "approve-all")
        self.assertTrue(payload["terminal"])
        self.assertIn("--approve-all", payload["command"])
        self.assertNotIn("--no-terminal", payload["command"])
        self.assertFalse((self.root / "receipts").exists())

    def test_launcher_binds_and_records_the_native_cli_without_fallback(self) -> None:
        native = self.bin / "native cli"
        native.write_text("#!/bin/sh\necho native-v2\n")
        native.chmod(0o755)
        stable = self.bin / "native-current"
        stable.symlink_to(native)
        self.target.write_text("#!/usr/bin/env python3\nimport os, subprocess\n"
                               "print(subprocess.check_output([os.environ['CODEX_PATH'], '--version'], text=True).strip())\n")
        target = {"argv": [str(self.target)], "version_argv": ["/stale/standalone-cli", "--version"],
                  "cli_env": {"CODEX_PATH": str(stable)}}
        runtime_file = self.root / "runtime.json"
        env = {**self.environment, "CODEX_PATH": "/must-not-run-bundled-cli",
               "AGENT_DELEGATION_LAUNCH": json.dumps({"name": "beta", "target": target, "acpx_path": str(self.acpx)}),
               "AGENT_DELEGATION_RUNTIME_RECEIPT": str(runtime_file)}
        result = self.run_cli("_launch", "--to", "beta", environment=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "native-v2")
        identity = json.loads(runtime_file.read_text())
        self.assertEqual(identity["observation"], "adapter_launch")
        self.assertEqual(identity["cli"]["path"], str(stable))
        self.assertEqual(identity["cli"]["resolved_path"], str(native.resolve()))
        self.assertEqual(identity["cli"]["version"], "native-v2")
        native.unlink()
        result = self.run_cli("_launch", "--to", "beta", environment=env)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Native CLI is missing", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_doctor_reports_installed_package_drift_without_a_version_gate(self) -> None:
        registry = json.loads(self.registry.read_text())
        runtime = self.root / "runtime"
        package = runtime / "node_modules/acpx"
        package.mkdir(parents=True)
        (package / "package.json").write_text('{"name":"acpx","version":"2.0.0"}')
        registry.update(runtime_root=str(runtime), runtime_packages={"acpx": "1.0.0"})
        self.registry.write_text(json.dumps(registry))
        result = self.run_cli("doctor", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        check = next(check for check in json.loads(result.stdout)["checks"] if check["check"] == "runtime_package:acpx")
        self.assertTrue(check["ok"])
        self.assertEqual(check["warning"], "recorded version drift")

    def test_interpreted_cli_identity_names_the_script_not_its_interpreter(self) -> None:
        cli = self.bin / "cli.py"
        cli.write_text("print('script-cli 3.0')\n")
        registry = json.loads(self.registry.read_text())
        registry["targets"]["beta"].update(cli_path=str(cli), version_argv=[sys.executable, str(cli), "--version"])
        self.registry.write_text(json.dumps(registry))
        result = self.run_cli("doctor", "--to", "beta", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        identity = json.loads(result.stdout)["runtimes"]["beta"]["cli"]
        self.assertEqual(identity["path"], str(cli))
        self.assertEqual(identity["version"], "script-cli 3.0")

    def test_custom_targets_do_not_inherit_internal_launcher_metadata(self) -> None:
        self.acpx.write_text(self.acpx.read_text().replace('import json, sys',
            "import json, sys, os\nassert 'AGENT_DELEGATION_LAUNCH' not in os.environ\n"
            "assert 'AGENT_DELEGATION_RUNTIME_RECEIPT' not in os.environ"))
        result = self.run_cli("run", "--to", "beta", "--cwd", str(self.cwd), "--task", "reply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "success")

    def test_explicit_restricted_mode_removes_terminal_without_authorization_note(self) -> None:
        result = self.run_cli(
            "run",
            "--to",
            "beta",
            "--caller",
            "alpha",
            "--cwd",
            str(self.cwd),
            "--task",
            "reason without shell access",
            "--permissions",
            "approve-reads",
            "--no-terminal",
            "--dry-run",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["permissions"], "approve-reads")
        self.assertFalse(payload["terminal"])
        self.assertIn("--approve-reads", payload["command"])
        self.assertIn("--no-terminal", payload["command"])

    def test_same_target_can_start_an_independent_task(self) -> None:
        result = self.run_cli(
            "run",
            "--to",
            "alpha",
            "--caller",
            "alpha",
            "--cwd",
            str(self.cwd),
            "--task",
            "noop",
            "--dry-run",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["chain"], ["alpha", "alpha"])

    def test_repeated_target_is_allowed_within_depth_budget(self) -> None:
        environment = {
            **self.environment,
            "AGENT_DELEGATION_CALLER": "beta",
            "AGENT_DELEGATION_CHAIN": "alpha,beta",
            "AGENT_DELEGATION_MAX_DEPTH": "4",
        }
        result = self.run_cli(
            "run",
            "--to",
            "alpha",
            "--cwd",
            str(self.cwd),
            "--task",
            "loop",
            "--dry-run",
            environment=environment,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["chain"], ["alpha", "beta", "alpha"])

    def test_full_capability_default_does_not_require_authorization_note(self) -> None:
        result = self.run_cli(
            "run",
            "--to",
            "beta",
            "--caller",
            "alpha",
            "--cwd",
            str(self.cwd),
            "--task",
            "change something",
            "--dry-run",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["permissions"], "approve-all")
        self.assertTrue(payload["terminal"])

    def test_missing_timeout_config_defaults_to_two_hours(self) -> None:
        registry = json.loads(self.registry.read_text(encoding="utf-8"))
        registry.pop("default_timeout_seconds")
        registry.pop("max_timeout_seconds")
        self.registry.write_text(json.dumps(registry), encoding="utf-8")
        result = self.run_cli(
            "run",
            "--to",
            "beta",
            "--caller",
            "alpha",
            "--cwd",
            str(self.cwd),
            "--task",
            "use default timeout",
            "--dry-run",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["timeout_seconds"], 7200)
        self.assertIn("7200", payload["command"])

    def test_missing_task_limit_accepts_more_than_legacy_default(self) -> None:
        task_file = self.cwd / "large-task.txt"
        task_file.write_text("x" * 200001, encoding="utf-8")
        result = self.run_cli(
            "run",
            "--to",
            "beta",
            "--caller",
            "alpha",
            "--cwd",
            str(self.cwd),
            "--task-file",
            str(task_file),
            "--dry-run",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_explicit_task_limit_is_enforced(self) -> None:
        registry = json.loads(self.registry.read_text(encoding="utf-8"))
        registry["max_task_chars"] = 3
        self.registry.write_text(json.dumps(registry), encoding="utf-8")
        result = self.run_cli(
            "run",
            "--to",
            "beta",
            "--caller",
            "alpha",
            "--cwd",
            str(self.cwd),
            "--task",
            "four",
            "--dry-run",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("configured limit is 3", result.stderr)

    def test_real_run_writes_private_receipt_and_normalizes_result(self) -> None:
        result = self.run_cli(
            "run",
            "--to",
            "beta",
            "--caller",
            "alpha",
            "--cwd",
            str(self.cwd),
            "--task",
            "return a short result",
            "--authorization-note",
            "Owner authorized this fixture mission within its prompt boundary.",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["assistant_text"], "delegated ok")
        self.assertFalse(payload["assistant_text_truncated"])
        self.assertEqual(payload["stop_reason"], "end_turn")
        receipt = Path(payload["receipt_dir"])
        self.assertTrue((receipt / "request.json").is_file())
        self.assertTrue((receipt / "events.ndjson").is_file())
        self.assertTrue((receipt / "result.json").is_file())
        request = json.loads((receipt / "request.json").read_text(encoding="utf-8"))
        self.assertEqual(request["permissions"], "approve-all")
        self.assertTrue(request["terminal"])
        self.assertEqual(
            request["authorization_note"],
            "Owner authorized this fixture mission within its prompt boundary.",
        )
        self.assertNotIn("return a short result", json.dumps(request))

    def test_missing_result_limit_preserves_more_than_legacy_default(self) -> None:
        self.acpx.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "sys.stdin.read()\n"
            "text = 'x' * 20001 + 'TAIL'\n"
            "print(json.dumps({'params': {'update': {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': text}}}}))\n"
            "print(json.dumps({'result': {'stopReason': 'end_turn'}}))\n",
            encoding="utf-8",
        )
        result = self.run_cli(
            "run",
            "--to",
            "beta",
            "--caller",
            "alpha",
            "--cwd",
            str(self.cwd),
            "--task",
            "return the full result",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["assistant_text"], "x" * 20001 + "TAIL")
        self.assertFalse(payload["assistant_text_truncated"])

    def test_explicit_result_limit_is_enforced(self) -> None:
        registry = json.loads(self.registry.read_text(encoding="utf-8"))
        registry["max_result_chars"] = 5
        self.registry.write_text(json.dumps(registry), encoding="utf-8")
        result = self.run_cli(
            "run",
            "--to",
            "beta",
            "--caller",
            "alpha",
            "--cwd",
            str(self.cwd),
            "--task",
            "return a limited result",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["assistant_text"], "deleg")
        self.assertTrue(payload["assistant_text_truncated"])

    def mission(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return self.run_cli("run", "--to", "beta", "--caller", "alpha", "--cwd", str(self.cwd),
                            "--task", "fixture mission", *extra)

    def emit_events(self, events: list[dict], exit_code: int = 0) -> None:
        self.acpx.write_text("#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\nprint(" +
                             repr("\n".join(json.dumps(event) for event in events)) +
                             f")\nsys.exit({exit_code})\n")

    def test_unregistered_caller_and_unrelated_bad_record_do_not_block(self) -> None:
        registry = json.loads(self.registry.read_text())
        registry["targets"]["unused"] = {"argv": ["relative"]}
        self.registry.write_text(json.dumps(registry))
        self.acpx_config.write_text("{}")
        result = self.mission("--caller", "desktop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["caller"], "desktop")

    def test_reviewed_argv_is_passed_directly_and_project_alias_is_not_used(self) -> None:
        registry = json.loads(self.registry.read_text())
        registry["targets"]["beta"]["argv"].append("literal 'quote' $() ; space")
        self.registry.write_text(json.dumps(registry))
        result = self.mission("--dry-run")
        import shlex
        command = json.loads(result.stdout)["command"]
        self.assertEqual(shlex.split(command[command.index("--agent") + 1]), registry["targets"]["beta"]["argv"])

    def test_depth_budget_still_applies_to_repeated_names(self) -> None:
        result = self.mission("--chain", "alpha,beta,alpha,beta,alpha", "--dry-run")
        self.assertEqual(result.returncode, 2)
        self.assertIn("depth", result.stderr)
        environment = {**self.environment, "AGENT_DELEGATION_MAX_DEPTH": "1"}
        result = self.run_cli("run", "--to", "beta", "--cwd", str(self.cwd), "--task", "fixture",
                              "--max-depth", "4", "--dry-run", environment=environment)
        self.assertEqual(result.returncode, 2)

    def test_result_configuration_is_checked_before_execution(self) -> None:
        registry = json.loads(self.registry.read_text())
        registry["max_result_chars"] = 0
        self.registry.write_text(json.dumps(registry))
        for extra in ([], ["--dry-run"]):
            result = self.mission(*extra)
            self.assertEqual(result.returncode, 2)
        self.assertFalse((self.root / "receipts").exists())

    def test_transport_terminal_states_are_distinct(self) -> None:
        for native_code, stop, expected in [(0, "cancelled", "cancelled"), (3, None, "timeout"),
                                          (5, None, "denied"), (4, None, "not_found"),
                                          (0, None, "incomplete"), (0, "max_tokens", "incomplete")]:
            with self.subTest(expected=expected, stop=stop):
                self.emit_events([{"result": {"stopReason": stop}}] if stop else [], native_code)
                result = self.mission()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(json.loads(result.stdout)["status"], expected)

    def test_tool_error_details_and_resource_content_survive_success(self) -> None:
        resource = {"type": "resource_link", "uri": "file:///tmp/report.csv", "name": "report.csv"}
        self.emit_events([
            {"id": 11, "method": "fs/read_text_file", "params": {"path": "/missing"}},
            {"id": 11, "error": {"code": -32603, "message": "Internal error", "data": {"details": "ENOENT"}}},
            {"params": {"sessionId": "native-123", "update": {"sessionUpdate": "agent_message_chunk", "content": resource}}},
            {"result": {"stopReason": "end_turn"}},
        ])
        result = self.mission()
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["assistant_content"], [resource])
        self.assertEqual(payload["protocol_errors"], [])
        self.assertEqual(payload["tool_errors"][0]["data"], {"details": "ENOENT"})
        self.assertEqual(payload["acp_session_id"], "native-123")

    def test_named_session_and_model_use_native_commands(self) -> None:
        result = self.mission("--session", "review", "--model", "selected-model", "--dry-run")
        commands = json.loads(result.stdout)["commands"]
        self.assertEqual(commands[0][-4:], ["sessions", "ensure", "--name", "review"])
        self.assertEqual(commands[1][-5:], ["prompt", "--session", "review", "--file", "-"])
        self.assertIn("selected-model", commands[1])
        result = self.run_cli("cancel", "--to", "beta", "--cwd", str(self.cwd), "--session", "review", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["command"][-3:], ["cancel", "--session", "review"])

    def test_native_control_action_and_session_ids_are_not_jsonrpc_wrapped(self) -> None:
        for operation, action in (("cancel", "cancel_result"), ("close", "session_closed")):
            with self.subTest(operation=operation):
                self.emit_events([{"action": action, "acpxRecordId": "record-123",
                                   "acpxSessionId": "session-123", "cancelled": True}])
                result = self.run_cli(operation, "--to", "beta", "--cwd", str(self.cwd), "--session", "review")
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["acpx_record_id"], "record-123")
                self.assertEqual(payload["acp_session_id"], "session-123")

    def test_recovery_controls_do_not_require_valid_task_budgets_or_worker_binary(self) -> None:
        registry = json.loads(self.registry.read_text())
        registry.update(max_result_chars=0, max_task_chars=0, max_delegation_depth=0,
                        max_timeout_seconds=0)
        self.registry.write_text(json.dumps(registry))
        self.target.unlink()
        environment = {**self.environment, "AGENT_DELEGATION_CHAIN": "alpha,,beta",
                       "AGENT_DELEGATION_MAX_DEPTH": "invalid"}
        for operation in ("cancel", "close"):
            self.emit_events([{"action": "cancel_result" if operation == "cancel" else "session_closed"}])
            result = self.run_cli(operation, "--to", "beta", "--cwd", str(self.cwd),
                                  "--session", "review", environment=environment)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "success")

    def test_streaming_and_sigterm_preserve_partial_output(self) -> None:
        self.acpx.write_text("#!/usr/bin/env python3\nimport json,sys,time\nsys.stdin.read()\n"
            "print(json.dumps({'params':{'update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'PARTIAL'}}}}),flush=True)\ntime.sleep(60)\n")
        process = subprocess.Popen([sys.executable, str(SCRIPT), "run", "--to", "beta", "--cwd", str(self.cwd),
                                    "--task", "fixture"], env=self.environment, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            events = []
            while time.monotonic() < deadline:
                events = list((self.root / "receipts").glob("*/events.ndjson"))
                if events and "PARTIAL" in events[0].read_text():
                    break
                time.sleep(0.02)
            self.assertTrue(events)
            self.assertIn("PARTIAL", events[0].read_text())
            self.assertIsNone(process.poll())
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=10)
            payload = json.loads(stdout)
            self.assertEqual(payload["status"], "cancelled", stderr)
            self.assertEqual(payload["assistant_text"], "PARTIAL")
            self.assertEqual(process.returncode, 130)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_outer_deadline_preserves_streamed_bytes(self) -> None:
        spec = importlib.util.spec_from_file_location("wrapper", SCRIPT)
        wrapper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wrapper)
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            code, reason = wrapper._stream_command([sys.executable, "-c",
                "import sys,time; print('PARTIAL',flush=True); time.sleep(60)"], "", self.environment, 0.25, output, errors)
            output.seek(0)
            self.assertEqual((code, reason), (124, "timeout"))
            self.assertEqual(output.read(), b"PARTIAL\n")

    def test_doctor_target_scope_and_stale_probe_warning(self) -> None:
        registry = json.loads(self.registry.read_text())
        registry["targets"]["beta"]["version_argv"] = ["/missing-old-version", "--version"]
        registry["targets"]["unused"] = {"argv": ["relative"]}
        registry["default_timeout_seconds"] = registry["max_timeout_seconds"] = 43200
        self.registry.write_text(json.dumps(registry))
        result = self.run_cli("doctor", "--to", "beta", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["limits"]["default_timeout_seconds"], 43200)
        self.assertTrue(any(c.get("warning") for c in payload["checks"]))

    def test_submit_wait_timeout_does_not_end_execution(self) -> None:
        self.acpx.write_text("#!/usr/bin/env python3\nimport json,sys,time\nsys.stdin.read()\n"
            "print(json.dumps({'params':{'update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'PARTIAL'}}}}),flush=True)\n"
            "time.sleep(1.5)\nprint(json.dumps({'result':{'stopReason':'end_turn'}}))\n")
        submitted = self.run_cli("submit", "--to", "beta", "--cwd", str(self.cwd), "--task", "fixture")
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        task = json.loads(submitted.stdout)
        task_id = task["delegation_id"]
        self.assertFalse(task["terminal"])
        try:
            waiting = self.run_cli("wait", "--id", task_id, "--timeout", "0")
            snapshot = json.loads(waiting.stdout)
            self.assertEqual(waiting.returncode, 0, waiting.stderr)
            self.assertTrue(snapshot["wait_timed_out"])
            self.assertFalse(snapshot["terminal"])
            self.assertFalse(snapshot["cancel_requested"])
            # Observation remains available even if launch configuration later changes.
            self.target.unlink()
            registry = json.loads(self.registry.read_text())
            registry.update(max_delegation_depth=0, max_timeout_seconds=0)
            self.registry.write_text(json.dumps(registry))
            completed = self.run_cli("wait", "--id", task_id, "--timeout", "5")
            result = json.loads(completed.stdout)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(result["terminal"])
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["assistant_text"], "PARTIAL")
            self.assertGreater(result["execution_seconds"], 1)
            self.assertEqual(result["queue_wait_seconds"], 0)
            self.assertIsNone(result["timeout_phase"])
            self.assertEqual(json.loads(self.run_cli("status", "--id", task_id).stdout), result)
            self.assertFalse((Path(result["receipt_dir"]) / "launch.json").exists())
        finally:
            self.run_cli("cancel", "--id", task_id)
            self.run_cli("wait", "--id", task_id, "--timeout", "5")

    def test_task_control_rejects_ambiguous_identity_and_invalid_timeouts(self) -> None:
        for arguments in (("status", "--id", "../anything"),
                          ("cancel", "--id", "a" * 32, "--to", "beta")):
            result = self.run_cli(*arguments)
            self.assertEqual(result.returncode, 2)
        for seconds in ("0", "-1"):
            result = self.mission("--queue-timeout", seconds)
            self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse((self.root / "receipts").exists())


if __name__ == "__main__":
    unittest.main()
