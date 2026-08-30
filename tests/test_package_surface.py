from __future__ import annotations

import json
from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class PackageSurfaceTests(unittest.TestCase):
    def test_plugin_and_marketplace_point_to_canonical_skill(self) -> None:
        plugin_root = REPO_ROOT / "plugins/agent-delegation"
        plugin = json.loads(
            (plugin_root / ".codex-plugin/plugin.json").read_text(encoding="utf-8")
        )
        marketplace = json.loads(
            (REPO_ROOT / ".agents/plugins/marketplace.json").read_text(encoding="utf-8")
        )
        self.assertEqual(plugin["name"], "agent-delegation")
        self.assertEqual(plugin["skills"], "./skills/")
        self.assertTrue((plugin_root / "skills/agent-delegation/SKILL.md").is_file())
        entry = marketplace["plugins"][0]
        self.assertEqual(entry["name"], "agent-delegation")
        self.assertEqual(entry["source"]["path"], "./plugins/agent-delegation")

    def test_package_versions_are_consistent(self) -> None:
        plugin = json.loads(
            (
                REPO_ROOT
                / "plugins/agent-delegation/.codex-plugin/plugin.json"
            ).read_text(encoding="utf-8")
        )
        runtime = json.loads((REPO_ROOT / "runtime/package.json").read_text(encoding="utf-8"))
        lock = json.loads((REPO_ROOT / "runtime/package-lock.json").read_text(encoding="utf-8"))
        version = plugin["version"]
        self.assertEqual(runtime["version"], version)
        self.assertEqual(lock["version"], version)
        self.assertEqual(lock["packages"][""]["version"], version)
        self.assertIn(
            f'VERSION = "{version}"',
            (REPO_ROOT / "scripts/install_user.py").read_text(encoding="utf-8"),
        )
        self.assertIn(
            f'VERSION = "{version}"',
            (
                REPO_ROOT
                / "plugins/agent-delegation/skills/agent-delegation/scripts/agent_delegate.py"
            ).read_text(encoding="utf-8"),
        )

    def test_runtime_direct_dependencies_and_integrities_are_exact(self) -> None:
        package = json.loads((REPO_ROOT / "runtime/package.json").read_text(encoding="utf-8"))
        lock = json.loads((REPO_ROOT / "runtime/package-lock.json").read_text(encoding="utf-8"))
        expected = {
            "acpx": (
                "0.13.2",
                "sha512-4hOLEo2kE/nCrPr50StbzU3G1WvzHkmKE/r3vxFAIr6GRI3VSmSRH62XCtnDpVcQNpBM8fVPAeTj39ewVhJwdQ==",
            ),
            "@agentclientprotocol/claude-agent-acp": (
                "0.70.0",
                "sha512-Psqj6fhV4pQ8IM480zpJ+xGiMMIqNLxlsTj5Mzn+T8KSURCVNJdl0ktcqLMjgHJC/QnOvDdDkFf3xTW9VIV9aQ==",
            ),
            "@agentclientprotocol/codex-acp": (
                "1.7.0",
                "sha512-+nUhAJyunx8Zc7r3jjLPoMPPUkkk02TmBIosln4l+ugRNUOdNQAMm6toZo7xb+mF1yM5zxJB83qvy/bPmOTaaw==",
            ),
        }
        self.assertEqual(
            package["dependencies"],
            {name: version for name, (version, _) in expected.items()},
        )
        for name, (version, integrity) in expected.items():
            lock_key = f"node_modules/{name}"
            self.assertEqual(lock["packages"][lock_key]["version"], version)
            self.assertEqual(lock["packages"][lock_key]["integrity"], integrity)

    def test_skill_is_generic_and_has_no_unfinished_scaffold(self) -> None:
        text = (
            REPO_ROOT
            / "plugins/agent-delegation/skills/agent-delegation/SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertNotIn("TODO", text)
        self.assertIn("Delegation is not limited to coding", text)
        for target in ("Hermes", "Claude Code", "Codex", "Kimi", "zCode", "OpenCode"):
            self.assertIn(target, text)

    def test_skill_preserves_capabilities_and_documents_explicit_restrictions(self) -> None:
        skill = (
            REPO_ROOT
            / "plugins/agent-delegation/skills/agent-delegation/SKILL.md"
        ).read_text(encoding="utf-8")
        wrapper = (
            REPO_ROOT
            / "plugins/agent-delegation/skills/agent-delegation/scripts/agent_delegate.py"
        ).read_text(encoding="utf-8")
        self.assertIn("Capability is not authority", skill)
        self.assertIn("defaults to `approve-all` with Terminal advertised", skill)
        self.assertIn('default="approve-all"', wrapper)
        self.assertIn('run_parser.set_defaults(terminal=True, handler=_run)', wrapper)
        self.assertIn('"--no-terminal"', wrapper)


if __name__ == "__main__":
    unittest.main()
