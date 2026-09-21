from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
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
    def test_legacy_path_forwards_to_each_loaded_package_without_a_shared_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            share = home / ".local/share/agent-delegation"
            old = share / "skill"
            (old / "scripts").mkdir(parents=True)
            (old / "SKILL.md").write_text("old instructions")
            (old / ".agent-delegation-managed.json").write_text('{"package":"agent-delegation"}')
            (old / "scripts/agent_delegate.py").write_text("old implementation")
            entry = install_user._install_forwarder(share, home / "backup", False)
            self.assertFalse((old / "SKILL.md").exists())
            self.assertEqual((home / "backup/skills/canonical/SKILL.md").read_text(), "old instructions")
            env = {key: value for key, value in os.environ.items() if key != "AGENT_DELEGATION_ENTRY"}
            missing = subprocess.run([sys.executable, str(entry)], env=env, capture_output=True, text=True)
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("loaded agent-delegation Skill", missing.stderr)
            for version in ("host-a-v1", "host-a-v2", "host-b-v1"):
                script = home / version / "agent_delegate.py"
                script.parent.mkdir()
                script.write_text(f"print({version!r})\n")
                result = subprocess.run([sys.executable, str(entry), "--version"],
                    env={**env, "AGENT_DELEGATION_ENTRY": str(script)}, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), version)

    def test_native_plugin_blocks_a_second_user_skill_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            state = home / ".claude/plugins/installed_plugins.json"
            state.parent.mkdir(parents=True)
            state.write_text('{"plugins":{"agent-delegation@market":[{"scope":"user"}]}}')
            args = install_user.argparse.Namespace(home=str(home), hosts="claude", targets="none",
                replace_existing=True, update_runtime=False)
            with self.assertRaisesRegex(install_user.InstallError, "already owns"):
                install_user._install(args)
            self.assertFalse((home / ".local").exists())

    def write_runtime(self, root: Path, version: str = "1.2.3") -> None:
        root.mkdir(parents=True, exist_ok=True)
        (root / "package-lock.json").write_text(json.dumps({"fixture": version}))
        for name in install_user.RUNTIME_PACKAGES:
            package = root / "node_modules" / name
            package.mkdir(parents=True)
            (package / "package.json").write_text(json.dumps({"name": name, "version": version}))
            executable = root / "node_modules/.bin" / name.rsplit("/", 1)[-1]
            executable.parent.mkdir(exist_ok=True)
            executable.write_text("#!/bin/sh\necho adapter-fixture\n")
            executable.chmod(0o755)

    def test_skill_update_preserves_runtime_and_upgrade_stages_before_switching(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            share = home / ".local/share/agent-delegation"
            old = share / "runtime"
            self.write_runtime(old)
            (share / ".managed.json").write_text('{"package":"agent-delegation"}')
            registry_path = home / ".config/agent-delegation/config.json"
            registry_path.parent.mkdir(parents=True)
            registry_path.write_text(json.dumps({"runtime_root": str(old)}))
            registry_before = registry_path.read_bytes()
            lock_before = (old / "package-lock.json").read_bytes()
            backup = home / "backup"
            with patch.object(install_user.subprocess, "run") as npm:
                self.assertEqual(install_user._install_runtime(home, backup, False), (share, old))
                npm.assert_not_called()

            def install(argv, **kwargs):
                self.assertEqual(argv[1], "install")
                self.assertTrue(all(name + "@latest" in argv for name in install_user.RUNTIME_PACKAGES))
                self.write_runtime(kwargs["cwd"], "2.0.0")
                return subprocess.CompletedProcess(argv, 0, "", "")

            with patch.object(install_user, "_version_line", return_value="v24.0.0"), \
                 patch.object(install_user.subprocess, "run", side_effect=install):
                _, new = install_user._install_runtime(home, backup, False, update_runtime=True)
            self.assertNotEqual(new, old)
            self.assertEqual(install_user._runtime_versions(new), {name: "2.0.0" for name in install_user.RUNTIME_PACKAGES})
            self.assertEqual((old / "package-lock.json").read_bytes(), lock_before)
            self.assertEqual(registry_path.read_bytes(), registry_before)

            with patch.object(install_user, "_version_line", return_value="v24.0.0"), \
                 patch.object(install_user.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "fixture failure")):
                with self.assertRaisesRegex(install_user.InstallError, "previous runtime is unchanged"):
                    install_user._install_runtime(home, home / "failed-backup", False, update_runtime=True)
            self.assertEqual(list((share / "runtimes").iterdir()), [new])
            self.assertEqual(registry_path.read_bytes(), registry_before)
            self.assertEqual((old / "package-lock.json").read_bytes(), lock_before)

    def test_zcode_only_update_preserves_other_dependency_versions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            share = home / ".local/share/agent-delegation"
            old = share / "runtime"
            self.write_runtime(old)
            (old / "package.json").write_text(json.dumps({"dependencies": {"acpx": "1.2.3"}}))
            (share / ".managed.json").write_text('{"package":"agent-delegation"}')
            registry = home / ".config/agent-delegation/config.json"
            registry.parent.mkdir(parents=True)
            registry.write_text(json.dumps({"runtime_root": str(old)}))
            before = (old / "package-lock.json").read_bytes()

            def install(argv, **kwargs):
                self.assertEqual(argv[-1], "zcode-acp-server@latest")
                self.assertNotIn("acpx@latest", argv)
                root = kwargs["cwd"]
                self.assertEqual(json.loads((root / "package.json").read_text())["dependencies"], {"acpx": "1.2.3"})
                self.write_runtime(root)
                package = root / "node_modules/zcode-acp-server"
                package.mkdir()
                (package / "package.json").write_text('{"version":"future-adapter"}')
                return subprocess.CompletedProcess(argv, 0, "", "")

            with patch.object(install_user, "_version_line", return_value="v24.0.0"), \
                 patch.object(install_user.subprocess, "run", side_effect=install):
                _, new = install_user._install_runtime(home, home / "backup", False, update_zcode_adapter=True)
            self.assertEqual(install_user._runtime_versions(new)["acpx"], "1.2.3")
            self.assertEqual(install_user._runtime_versions(new)["zcode-acp-server"], "future-adapter")
            self.assertEqual((old / "package-lock.json").read_bytes(), before)
            self.assertEqual(json.loads(registry.read_text())["runtime_root"], str(old))

    def test_native_cli_binding_and_launcher_survive_runtime_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            old, new = home / "runtime-old", home / "runtime-new"
            self.write_runtime(old)
            self.write_runtime(new, "2.0.0")
            for name, key in (("claude", "CLAUDE_CODE_EXECUTABLE"), ("codex", "CODEX_PATH")):
                cli = home / ".local/bin" / name
                cli.parent.mkdir(parents=True, exist_ok=True)
                cli.write_text("#!/bin/sh\necho native-CLI\n")
                cli.chmod(0o755)
                first = install_user._build_managed_targets(home, old, [name])[name]
                second = install_user._build_managed_targets(home, new, [name])[name]
                self.assertEqual(first["cli_env"], {key: str(cli)})
                self.assertEqual(first["version_argv"][0], first["cli_env"][key])
                self.assertEqual(first["launch_argv"], second["launch_argv"])
                self.assertNotEqual(first["argv"], second["argv"])
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

    def test_zcode_uses_managed_adapter_without_baked_cli_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            registry = home / ".config/agent-delegation/config.json"
            registry.parent.mkdir(parents=True)
            registry.write_text(json.dumps({"max_timeout_seconds": 43200}))
            adapter = home / "runtime/node_modules/zcode-acp-server/dist/index.js"
            adapter.parent.mkdir(parents=True)
            adapter.write_text("// fixture")
            with patch.object(install_user, "_resolve_executable", return_value=home / "cli"), \
                 patch.object(install_user, "_version_line", return_value="fixture"), \
                 patch.object(install_user.zcode_runtime, "prepare", return_value=(
                     {"version_argv": [str(home / "discovered-node"), str(home / "discovered-cli"), "--version"]}, {})):
                target = install_user._build_managed_targets(home, home / "runtime", ["zcode"])["zcode"]
                argv = target["argv"]
            self.assertEqual(argv[1], str(adapter))
            self.assertEqual(json.loads(registry.read_text())["max_timeout_seconds"], 43200)
            self.assertTrue(target["zcode_runtime"])
            self.assertNotIn("--zcode-path", argv)
            self.assertNotIn("--node", argv)

    def test_zcode_data_root_controls_skill_and_plugin_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            data = home / "custom account"
            with patch.dict(os.environ, {"ZCODE_DATA_BASE_DIR": str(data)}):
                self.assertEqual(install_user._skill_destination(home, "zcode"), data.resolve() / ".zcode/skills/agent-delegation")
                state = data / ".zcode/cli/plugins/installed_plugins.json"
                state.parent.mkdir(parents=True)
                state.write_text('{"plugins":[{"id":"agent-delegation@market"}]}')
                with self.assertRaisesRegex(install_user.InstallError, "already owns"):
                    install_user._check_native_plugin(home, "zcode")

    def test_host_destinations_are_native_user_paths(self) -> None:
        home = Path("/tmp/example-home")
        expected = {
            "hermes": home / ".hermes/skills/agent-delegation",
            "claude": home / ".claude/skills/agent-delegation",
            "codex": home / ".agents/skills/agent-delegation",
            "kimi": home / ".kimi-code/skills/agent-delegation",
            "zcode": home.resolve() / ".zcode/skills/agent-delegation",
            "opencode": home / ".config/opencode/skills/agent-delegation",
            "pi": home / ".pi/agent/skills/agent-delegation",
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
            self.write_runtime(runtime)
            registry_path = home / ".config/agent-delegation/config.json"
            registry_path.parent.mkdir(parents=True)
            custom = {
                "argv": ["/absolute/custom", "acp"],
                "observed_version": "1.0.0",
                "provenance": "test",
            }
            registry_path.write_text(
                json.dumps({"schema_version": 1, "targets": {"custom": custom, "zcode": custom},
                            "default_timeout_seconds": 43200, "max_timeout_seconds": 43200,
                            "receipt_root": str(home / "custom-receipts")}),
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
            self.assertEqual(registry["receipt_root"], str(home / "custom-receipts"))
            self.assertEqual(registry["runtime_packages"], {name: "1.2.3" for name in install_user.RUNTIME_PACKAGES})
            self.assertEqual(acpx["defaultPermissions"], "approve-all")
            self.assertEqual(acpx["timeout"], 7200)

    def test_initial_install_delivers_delegate_entry_beside_the_selected_sdk(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)

            def install(argv, **kwargs):
                self.write_runtime(kwargs["cwd"], "0.13.2")
                return subprocess.CompletedProcess(argv, 0, "", "")

            with patch.object(install_user, "_version_line", return_value="v24.0.0"), \
                 patch.object(install_user.subprocess, "run", side_effect=install):
                _, runtime_root = install_user._install_runtime(home, home / "backup", False)
            entry = runtime_root / "acpx_delegate_entry.cjs"
            self.assertEqual(entry.read_bytes(), install_user.DELEGATE_ENTRY_SOURCE.read_bytes())
            # The entry resolves require('acpx/runtime') from its own node_modules.
            self.assertTrue((runtime_root / "node_modules/acpx/package.json").is_file())
            marker = json.loads((runtime_root / ".agent-delegation-managed.json").read_text())
            self.assertEqual(marker["runtime_entry_sha256"], install_user._sha256(entry))
            self.assertEqual(marker["runtime_packages"]["acpx"], "0.13.2")

    def test_skill_update_refreshes_only_the_delegate_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            share = home / ".local/share/agent-delegation"
            old = share / "runtime"
            self.write_runtime(old)
            (share / ".managed.json").write_text('{"package":"agent-delegation"}')
            registry_path = home / ".config/agent-delegation/config.json"
            registry_path.parent.mkdir(parents=True)
            registry_path.write_text(json.dumps({"runtime_root": str(old)}))
            registry_before = registry_path.read_bytes()
            lock_before = (old / "package-lock.json").read_bytes()
            stale = old / "acpx_delegate_entry.cjs"
            stale.write_text("// stale entry from an older source checkout\n")
            with patch.object(install_user.subprocess, "run") as npm:
                self.assertEqual(install_user._install_runtime(home, home / "backup", False), (share, old))
                npm.assert_not_called()
            self.assertEqual(stale.read_bytes(), install_user.DELEGATE_ENTRY_SOURCE.read_bytes())
            self.assertEqual(
                (home / "backup/runtime/acpx_delegate_entry.cjs").read_text(),
                "// stale entry from an older source checkout\n",
            )
            self.assertEqual((old / "package-lock.json").read_bytes(), lock_before)
            self.assertEqual(registry_path.read_bytes(), registry_before)
            # A byte-identical entry is left alone: no rewrite, no extra backup.
            with patch.object(install_user.subprocess, "run") as npm:
                install_user._install_runtime(home, home / "backup-second", False)
                npm.assert_not_called()
            self.assertFalse((home / "backup-second/runtime/acpx_delegate_entry.cjs").exists())

    def test_runtime_upgrade_delivers_the_entry_into_the_new_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            share = home / ".local/share/agent-delegation"
            old = share / "runtime"
            self.write_runtime(old)
            (share / ".managed.json").write_text('{"package":"agent-delegation"}')
            registry_path = home / ".config/agent-delegation/config.json"
            registry_path.parent.mkdir(parents=True)
            registry_path.write_text(json.dumps({"runtime_root": str(old)}))
            old_entry = old / "acpx_delegate_entry.cjs"
            old_entry.write_text("// previous generation entry\n")

            def install(argv, **kwargs):
                self.write_runtime(kwargs["cwd"], "2.0.0")
                return subprocess.CompletedProcess(argv, 0, "", "")

            with patch.object(install_user, "_version_line", return_value="v24.0.0"), \
                 patch.object(install_user.subprocess, "run", side_effect=install):
                _, new = install_user._install_runtime(home, home / "backup", False, update_runtime=True)
            self.assertNotEqual(new, old)
            self.assertEqual(
                (new / "acpx_delegate_entry.cjs").read_bytes(),
                install_user.DELEGATE_ENTRY_SOURCE.read_bytes(),
            )
            marker = json.loads((new / ".agent-delegation-managed.json").read_text())
            self.assertEqual(
                marker["runtime_entry_sha256"], install_user._sha256(new / "acpx_delegate_entry.cjs"))
            # The retired generation keeps its own entry and SDK untouched.
            self.assertEqual(old_entry.read_text(), "// previous generation entry\n")

    def test_registry_records_the_installed_delegate_entry_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            runtime = home / "runtime"
            self.write_runtime(runtime)
            entry = runtime / "acpx_delegate_entry.cjs"
            entry.write_bytes(install_user.DELEGATE_ENTRY_SOURCE.read_bytes())
            backup = home / "backup"
            backup.mkdir()
            _, registry, _ = install_user._merge_registry(home, runtime, {}, backup)
            discovered = Path(registry["acpx_delegate_entry"])
            self.assertEqual(discovered, entry.resolve())
            self.assertTrue(discovered.is_file())
            self.assertEqual(discovered.parent, Path(registry["runtime_root"]).resolve())

    def test_install_runtime_only_delivers_the_discoverable_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            args = install_user.argparse.Namespace(
                home=str(home), hosts="none", targets=None,
                replace_existing=False, update_runtime=False,
            )

            def install(argv, **kwargs):
                self.write_runtime(kwargs["cwd"], "0.13.2")
                return subprocess.CompletedProcess(argv, 0, "", "")

            with patch.object(install_user, "_version_line", return_value="v24.0.0"), \
                 patch.object(install_user.subprocess, "run", side_effect=install), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(install_user._install(args), 0)
            registry = json.loads(
                (home / ".config/agent-delegation/config.json").read_text(encoding="utf-8"))
            discovered = Path(registry["acpx_delegate_entry"])
            self.assertTrue(discovered.is_file())
            self.assertEqual(discovered.read_bytes(), install_user.DELEGATE_ENTRY_SOURCE.read_bytes())
            # The discovered entry sits beside the selected runtime's installed SDK.
            self.assertEqual(discovered.parent, Path(registry["runtime_root"]).resolve())
            self.assertEqual(install_user._runtime_versions(discovered.parent)["acpx"], "0.13.2")


class KimiHomeTests(unittest.TestCase):
    def test_native_default_and_custom_kimi_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            with patch.dict(install_user.os.environ, {"KIMI_CODE_HOME": ""}):
                self.assertEqual(install_user._skill_destination(home, "kimi"),
                                 home / ".kimi-code/skills/agent-delegation")
            custom = home / "custom kimi home"
            with patch.dict(install_user.os.environ, {"KIMI_CODE_HOME": str(custom)}):
                self.assertEqual(install_user._skill_destination(home, "kimi"),
                                 custom / "skills/agent-delegation")


if __name__ == "__main__":
    unittest.main()
