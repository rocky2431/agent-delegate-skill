"""Pi installation and native-outcome regression checks, without a model."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/agent-delegation/skills/agent-delegation/scripts"))
import agent_delegate as delegate
from pi_result import reconcile_pi_result

spec = importlib.util.spec_from_file_location("pi_installer", ROOT / "scripts/install_user.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class PiSupportTests(unittest.TestCase):
    def test_install_binding_and_reject_unenforceable_restrictions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for file in (root / ".local/bin/pi", root / "runtime/node_modules/.bin/pi-acp"):
                file.parent.mkdir(parents=True)
                file.write_text("#!/bin/sh\necho fixture\n")
                file.chmod(0o755)
            target = installer._build_managed_targets(root, root / "runtime", ["pi"])["pi"]
            self.assertEqual(target["cli_env"], {"PI_ACP_PI_COMMAND": str(root / ".local/bin/pi")})
            delegate._validate_target_record("pi", target)
            with patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(root / "custom-pi")}):
                self.assertEqual(installer._skill_destination(root, "pi"), root / "custom-pi/skills/agent-delegation")
            for flags in (("--permissions", "deny-all"), ("--permissions", "approve-reads"), ("--no-terminal",)):
                args = delegate._build_parser().parse_args(["run", "--to", "pi", "--cwd", tmp, "--task", "test", *flags])
                with patch.object(delegate, "_load_registry", return_value=(root, {"targets": {"pi": target}})):
                    with self.assertRaisesRegex(delegate.DelegationError, "cannot be enforced"):
                        delegate._prepare_run(args)

    def test_native_result_requires_current_mission_and_active_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mapping, session = root / "map.json", root / "session.jsonl"
            mapping.write_text(json.dumps({"sessions": {"acp-id": {"cwd": tmp, "sessionFile": str(session)}}}))
            request = {"delegation_id": "mission-id", "cwd": tmp}
            for native, expected in (("stop", "end_turn"), ("error", "error"), ("aborted", "cancelled"), ("length", "max_tokens")):
                rows = [
                    {"id": "u", "parentId": None, "message": {"role": "user", "content": [{"type": "text", "text": "mission-id"}]}},
                    {"id": "old-branch", "parentId": "u", "message": {"role": "assistant", "stopReason": "stop"}},
                    {"id": "a", "parentId": "u", "message": {"role": "assistant", "stopReason": native}},
                ]
                session.write_text("\n".join(map(json.dumps, rows)))
                parsed = {"stop_reason": "end_turn", "acp_session_id": "acp-id"}
                reconcile_pi_result(parsed, request, str(mapping))
                self.assertEqual(parsed["stop_reason"], expected)
                self.assertTrue(parsed["pi_native_result"]["verified"])
                if native == "error":
                    self.assertEqual(delegate._result_status(0, {**parsed, "protocol_errors": []}, None, False), "error")
            # A later, unrelated mission must not borrow this mission's old success.
            rows += [{"id": "u2", "parentId": "a", "message": {"role": "user", "content": "other mission"}},
                     {"id": "a2", "parentId": "u2", "message": {"role": "assistant", "stopReason": "stop"}}]
            session.write_text("\n".join(map(json.dumps, rows)))
            parsed = {"stop_reason": "end_turn", "acp_session_id": "acp-id"}
            reconcile_pi_result(parsed, request, str(mapping))
            self.assertEqual(parsed["stop_reason"], "unknown")
            self.assertEqual(delegate._result_status(0, {**parsed, "protocol_errors": []}, None, False), "incomplete")
            session.unlink()
            reconcile_pi_result({"stop_reason": "end_turn", "acp_session_id": "acp-id"}, request, str(mapping))


if __name__ == "__main__":
    unittest.main()
