from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = REPO_ROOT / "scripts" / "install_user.py"
SPEC = importlib.util.spec_from_file_location("install_user", INSTALLER_PATH)
assert SPEC and SPEC.loader
install_user = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(install_user)


class InstallerTests(unittest.TestCase):
    def test_host_destinations_are_native_user_paths(self) -> None:
        home = Path("/tmp/example-home")
        expected = {
            "hermes": home / ".hermes/skills/agent-delegation",
            "claude": home / ".claude/skills/agent-delegation",
            "codex": home / ".agents/skills/agent-delegation",
            "kimi": home / ".kimi/skills/agent-delegation",
            "zcode": home / ".zcode/skills/agent-delegation",
            "opencode": home / ".config/opencode/skills/agent-delegation",
        }
        self.assertEqual(
            {host: install_user._skill_destination(home, host) for host in install_user.HOSTS},
            expected,
        )

    def test_none_installs_runtime_without_a_skill_copy(self) -> None:
        self.assertEqual(install_user._parse_hosts("none"), [])

    def test_registry_merge_preserves_custom_target_and_other_acpx_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            runtime = home / "runtime"
            runtime.mkdir()
            (runtime / "package-lock.json").write_text("{}\n", encoding="utf-8")
            registry_path = home / ".config/agent-delegation/config.json"
            registry_path.parent.mkdir(parents=True)
            custom = {
                "argv": ["/absolute/custom", "acp"],
                "observed_version": "1.0.0",
                "provenance": "test",
            }
            registry_path.write_text(
                json.dumps({"schema_version": 1, "targets": {"custom": custom}}),
                encoding="utf-8",
            )
            acpx_path = home / ".acpx/config.json"
            acpx_path.parent.mkdir(parents=True)
            acpx_path.write_text(json.dumps({"auth": {"kept": "redacted"}}), encoding="utf-8")
            backup = home / "backup"
            backup.mkdir()
            managed = {
                "hermes": {
                    "argv": ["/absolute/hermes", "acp"],
                    "observed_version": "test",
                    "provenance": "test",
                }
            }

            _, registry, _ = install_user._merge_registry(home, runtime, managed, backup)

            self.assertEqual(registry["targets"]["custom"], custom)
            self.assertEqual(registry["targets"]["hermes"], managed["hermes"])
            acpx = json.loads(acpx_path.read_text(encoding="utf-8"))
            self.assertEqual(acpx["auth"], {"kept": "redacted"})
            self.assertEqual(acpx["agents"]["custom"], {"argv": custom["argv"]})
            self.assertEqual(acpx["agents"]["hermes"], {"argv": managed["hermes"]["argv"]})
            self.assertEqual(acpx["defaultPermissions"], "approve-reads")


if __name__ == "__main__":
    unittest.main()
