from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = REPO_ROOT / "scripts" / "install_user.py"
SPEC = importlib.util.spec_from_file_location("install_user", INSTALLER_PATH)
assert SPEC and SPEC.loader
install_user = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(install_user)


class InstallerTests(unittest.TestCase):
    def test_only_selected_target_cli_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            kimi = home / ".kimi-code/bin/kimi"
            kimi.parent.mkdir(parents=True)
            kimi.write_text("#!/bin/sh\necho fixture-kimi\n")
            kimi.chmod(0o755)
            targets = install_user._build_managed_targets(home, home / "runtime", ["kimi"])
            self.assertEqual(set(targets), {"kimi"})
            self.assertEqual(targets["kimi"]["argv"], [str(kimi), "acp"])
            self.assertEqual(install_user._build_managed_targets(home, home / "runtime", []), {})

    def test_stable_cli_symlink_is_not_resolved_to_retired_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            executable = home / "version-1"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
            stable = home / "current"
            stable.symlink_to(executable)
            self.assertEqual(install_user._resolve_executable(home, [stable], []), stable)

    def test_zcode_adapter_keeps_existing_timeout_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            registry = home / ".config/agent-delegation/config.json"
            registry.parent.mkdir(parents=True)
            registry.write_text(json.dumps({"max_timeout_seconds": 43200}))
            with patch.object(install_user, "_resolve_executable", return_value=home / "cli"), \
                 patch.object(install_user, "_version_line", return_value="fixture"), \
                 patch.object(Path, "is_file", return_value=True):
                argv = install_user._build_managed_targets(home, home / "runtime", ["zcode"])["zcode"]["argv"]
            self.assertEqual(argv[argv.index("--prompt-timeout-secs") + 1], "43200")

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

    def test_legacy_generated_character_limits_are_removed(self) -> None:
        registry = {"max_task_chars": 200000, "max_result_chars": 20000}

        install_user._remove_legacy_default_char_limits(registry)

        self.assertNotIn("max_task_chars", registry)
        self.assertNotIn("max_result_chars", registry)

    def test_explicit_character_limits_are_preserved(self) -> None:
        registry = {"max_task_chars": 1234, "max_result_chars": 5678}

        install_user._remove_legacy_default_char_limits(registry)

        self.assertEqual(registry["max_task_chars"], 1234)
        self.assertEqual(registry["max_result_chars"], 5678)

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
                json.dumps({"schema_version": 1, "targets": {"custom": custom, "zcode": custom},
                            "default_timeout_seconds": 43200, "max_timeout_seconds": 43200}),
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
            self.assertEqual(registry["targets"]["zcode"], custom)
            self.assertEqual(registry["targets"]["hermes"], managed["hermes"])
            acpx = json.loads(acpx_path.read_text(encoding="utf-8"))
            self.assertEqual(acpx["auth"], {"kept": "redacted"})
            self.assertEqual(acpx["agents"]["custom"], {"argv": custom["argv"]})
            self.assertEqual(acpx["agents"]["hermes"], {"argv": managed["hermes"]["argv"]})
            self.assertEqual(registry["default_timeout_seconds"], 43200)
            self.assertEqual(registry["max_timeout_seconds"], 43200)
            self.assertEqual(acpx["defaultPermissions"], "approve-all")
            self.assertEqual(acpx["timeout"], 7200)


if __name__ == "__main__":
    unittest.main()
