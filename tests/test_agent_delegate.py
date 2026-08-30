from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
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

    def test_direct_self_delegation_is_rejected(self) -> None:
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
        self.assertEqual(result.returncode, 2)
        self.assertIn("self-delegation", result.stderr)

    def test_indirect_cycle_is_rejected_from_injected_environment(self) -> None:
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
        self.assertEqual(result.returncode, 2)
        self.assertIn("already appears", result.stderr)

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


if __name__ == "__main__":
    unittest.main()
