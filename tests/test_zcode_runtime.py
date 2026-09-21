from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/agent-delegation/skills/agent-delegation/scripts"
sys.path.insert(0, str(SCRIPTS))
import zcode_runtime


class ZCodeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cli = self.root / "renamed app/Contents/Resources/glm/zcode.cjs"
        self.cli.parent.mkdir(parents=True)
        self.cli.write_text("print('future-cli-version')\n")
        self.provider = self.cli.parent.parent / "config/provider/zcode-builtin.json"
        self.provider.parent.mkdir(parents=True)
        self.provider.write_text("{}")
        self.account = self.root / "account/.zcode"
        (self.account / "cli").mkdir(parents=True)
        (self.account / "cli/config.json").write_text('{"model":{"main":"configured-provider/future-model"}}')
        self.env = {"PATH": os.defpath, "ZCODE_ACP_ZCODE_PATH": str(self.cli),
                    "ZCODE_ACP_NODE": sys.executable, "ZCODE_HOME": str(self.account)}
        self.target = {"argv": [sys.executable, "/external/adapter.js"], "adapter_path": "/external/adapter.js", "zcode_runtime": True}

    def test_prepare_preserves_configured_model_and_account(self):
        target, env = zcode_runtime.prepare(self.target, self.env)
        self.assertEqual(env["ZCODE_BIN"], str(self.cli.resolve()))
        self.assertEqual(env["ZCODE_PROVIDER"], "configured-provider")
        self.assertEqual(env["ZCODE_MODEL"], "future-model")
        self.assertEqual(env["ZCODE_DATA_BASE_DIR"], str(self.account.parent.resolve()))
        self.assertEqual(target["cli_path"], str(self.cli.resolve()))
        self.assertEqual(self.target["argv"][1], "/external/adapter.js")

    def test_new_launch_follows_symlink_upgrade(self):
        stable = self.root / "current.cjs"
        stable.symlink_to(self.cli)
        self.env["ZCODE_ACP_ZCODE_PATH"] = str(stable)
        first, _ = zcode_runtime.prepare(self.target, self.env)
        upgraded = self.cli.parent / "new-entry.cjs"
        upgraded.write_text("new CLI")
        stable.unlink()
        stable.symlink_to(upgraded)
        second, _ = zcode_runtime.prepare(self.target, self.env)
        self.assertNotEqual(first["cli_path"], second["cli_path"])
        self.assertEqual(second["cli_path"], str(upgraded.resolve()))

    def test_explicit_missing_cli_fails_without_discovery_fallback(self):
        self.env["ZCODE_ACP_ZCODE_PATH"] = str(self.root / "missing.cjs")
        with patch.object(zcode_runtime.subprocess, "run") as discovery:
            with self.assertRaisesRegex(zcode_runtime.ZCodeRuntimeError, "Explicit ZCode CLI"):
                zcode_runtime.prepare(self.target, self.env)
            discovery.assert_not_called()

    def test_install_metadata_handles_renamed_bundle_and_rejects_ambiguity(self):
        contents = self.cli.parent.parent.parent
        (contents / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "dev.zcode.app"}))
        bundle = contents.parent
        with patch.object(zcode_runtime.sys, "platform", "darwin"), \
             patch.object(zcode_runtime.shutil, "which", side_effect=lambda name, **kw: "/finder" if name == "mdfind" else None), \
             patch.object(zcode_runtime.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, str(bundle), "")):
            self.assertEqual(zcode_runtime.discover_cli({}), self.cli.resolve())
            (self.cli.parent.parent / "zcode.cjs").write_text("another CLI")
            with self.assertRaisesRegex(zcode_runtime.ZCodeRuntimeError, "Multiple CLIs"):
                zcode_runtime.discover_cli({})

    def test_missing_or_partial_model_never_falls_back_to_a_fixed_model(self):
        (self.account / "cli/config.json").write_text("{}")
        with self.assertRaisesRegex(zcode_runtime.ZCodeRuntimeError, "No configured ZCode model"):
            zcode_runtime.prepare(self.target, self.env)
        self.env["ZCODE_MODEL"] = "explicit-model"
        with self.assertRaisesRegex(zcode_runtime.ZCodeRuntimeError, "both"):
            zcode_runtime.prepare(self.target, self.env)
        self.env["ZCODE_PROVIDER"] = "explicit-provider"
        _, env = zcode_runtime.prepare(self.target, self.env)
        self.assertEqual(env["ZCODE_MODEL"], "explicit-model")
        self.assertEqual(env["ZCODE_PROVIDER"], "explicit-provider")

    def test_native_default_selection_and_conflicting_account_roots(self):
        (self.account / "v2").mkdir()
        (self.account / "v2/provider_config.json").write_text(json.dumps({"config": {
            "defaultModelSelection": {"providerId": "native-provider", "modelId": "native-model"}}}))
        _, env = zcode_runtime.prepare(self.target, self.env)
        self.assertEqual(env["ZCODE_MODEL"], "native-model")
        self.env["ZCODE_DATA_BASE_DIR"] = str(self.root / "different-account")
        with self.assertRaisesRegex(zcode_runtime.ZCodeRuntimeError, "different accounts"):
            zcode_runtime.prepare(self.target, self.env)

    def test_zero_exit_without_ready_is_failure(self):
        target, env = zcode_runtime.prepare(self.target, self.env)
        with patch.object(zcode_runtime.subprocess, "run", side_effect=[subprocess.CompletedProcess([], 0, "{}", ""), subprocess.CompletedProcess([], 0, "", "missing provider")]):
            ok, detail = zcode_runtime.probe(target, env)
        self.assertFalse(ok)
        self.assertIn("missing provider", detail)
        ready = json.dumps({"method": "startup/storageState", "params": {"phase": "ready"}})
        with patch.object(zcode_runtime.subprocess, "run", side_effect=[subprocess.CompletedProcess([], 0, "{}", ""), subprocess.CompletedProcess([], 0, ready, "Protocol input closed")]):
            self.assertTrue(zcode_runtime.probe(target, env)[0])

    def test_real_launcher_passes_paths_and_records_actual_runtime(self):
        adapter = self.root / "adapter"
        adapter.write_text("#!" + sys.executable + "\nimport json,os,sys\n"
                           "print(json.dumps({'argv':sys.argv,'provider':os.environ.get('ZCODE_PROVIDER')}))\n")
        adapter.chmod(0o755)
        self.target["argv"][1] = str(adapter)
        self.target["adapter_path"] = str(adapter)
        receipt = self.root / "runtime.json"
        env = {**os.environ, **self.env,
               "AGENT_DELEGATION_RUNTIME_RECEIPT": str(receipt),
               "AGENT_DELEGATION_LAUNCH": json.dumps({"name": "zcode", "target": self.target, "acpx_path": sys.executable})}
        result = subprocess.run([sys.executable, str(SCRIPTS / "agent_delegate.py"), "_launch", "--to", "zcode"],
                                env=env, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["provider"], "configured-provider")
        identity = json.loads(receipt.read_text())
        self.assertEqual(identity["cli"]["version"], "future-cli-version")
        self.assertEqual(identity["zcode_model"]["provider_id"], "configured-provider")


if __name__ == "__main__":
    unittest.main()
