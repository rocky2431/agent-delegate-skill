"""Resolve the selected ZCode installation without pinning its location or version."""

from __future__ import annotations

import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys


class ZCodeRuntimeError(ValueError):
    """An unusable or ambiguous ZCode installation."""


def _file(value: str, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ZCodeRuntimeError(f"{label} does not exist: {path}")
    return path


def discover_cli(env: dict[str, str], explicit: str | None = None) -> Path:
    selected = explicit or env.get("ZCODE_BIN") or env.get("ZCODE_ACP_ZCODE_PATH")
    if selected:
        return _file(selected, "Explicit ZCode CLI")
    on_path = shutil.which("zcode", path=env.get("PATH"))
    if on_path:
        return Path(on_path).resolve()
    candidates = set()
    finder = shutil.which("mdfind", path=env.get("PATH")) if sys.platform == "darwin" else None
    if finder:
        try:
            found = subprocess.run(
                [finder, 'kMDItemCFBundleIdentifier == "dev.zcode.app"'],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ZCodeRuntimeError(f"ZCode installation discovery failed: {exc}") from exc
        for location in found.stdout.splitlines():
            bundle = Path(location)
            try:
                metadata = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
            except (OSError, ValueError, plistlib.InvalidFileException):
                continue
            if metadata.get("CFBundleIdentifier") != "dev.zcode.app":
                continue
            resources = bundle / "Contents/Resources"
            for pattern in ("zcode.cjs", "*/zcode.cjs"):
                candidates.update(path.resolve() for path in resources.glob(pattern) if path.is_file())
    if len(candidates) == 1:
        return candidates.pop()
    detail = "No compatible CLI found" if not candidates else "Multiple CLIs found: " + ", ".join(map(str, sorted(candidates)))
    raise ZCodeRuntimeError(f"{detail}; set ZCODE_BIN (or ZCODE_ACP_ZCODE_PATH) to the selected CLI.")


def data_home(env: dict[str, str], home: Path | None = None) -> Path:
    base = Path(env.get("ZCODE_DATA_BASE_DIR") or home or Path.home()).expanduser().resolve()
    selected = Path(env.get("ZCODE_HOME") or base / ".zcode").expanduser().resolve()
    if selected.name != ".zcode":
        raise ZCodeRuntimeError("ZCODE_HOME must end in .zcode; the native CLI uses ZCODE_DATA_BASE_DIR/.zcode.")
    if env.get("ZCODE_DATA_BASE_DIR") and selected.parent != base:
        raise ZCodeRuntimeError("ZCODE_HOME and ZCODE_DATA_BASE_DIR select different accounts.")
    return selected


def model_selection(env: dict[str, str], root: Path) -> tuple[str, str]:
    provider, model = env.get("ZCODE_PROVIDER"), env.get("ZCODE_MODEL")
    if provider or model:
        if not provider or not model:
            raise ZCodeRuntimeError("Set both ZCODE_PROVIDER and ZCODE_MODEL to select a model explicitly.")
        return provider, model
    # Read only the selected account's model preference, never copy credentials.
    for relative in ("v2/provider_config.json", "cli/config.json"):
        path = root / relative
        if not path.is_file():
            continue
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
            selection = config.get("config", config).get("defaultModelSelection")
            if isinstance(selection, dict) and all(isinstance(selection.get(key), str) and selection[key].strip()
                                                   for key in ("providerId", "modelId")):
                return selection["providerId"], selection["modelId"]
            legacy = config.get("model", {}).get("main")
            if isinstance(legacy, str) and "/" in legacy:
                provider, model = legacy.split("/", 1)
                if provider and model:
                    return provider, model
        except (OSError, ValueError, AttributeError) as exc:
            raise ZCodeRuntimeError(f"Cannot read ZCode model selection from {path}: {exc}") from exc
    raise ZCodeRuntimeError("No configured ZCode model; set ZCODE_PROVIDER and ZCODE_MODEL. No default model is guessed.")


def prepare(target: dict, env: dict[str, str]) -> tuple[dict, dict[str, str]]:
    """Resolve per new adapter process, preserving the selected account and model."""
    cli = discover_cli(env)
    node_name = env.get("ZCODE_NODE") or env.get("ZCODE_ACP_NODE") or "node"
    node = shutil.which(str(Path(node_name).expanduser()), path=env.get("PATH"))
    if not node:
        raise ZCodeRuntimeError(f"ZCode Node executable is unavailable: {node_name}")
    node = str(Path(node).resolve())
    root = data_home(env)
    provider, model = model_selection(env, root)
    child_env = {**env, "ZCODE_BIN": str(cli), "ZCODE_NODE": node,
                 "ZCODE_HOME": str(root), "ZCODE_DATA_BASE_DIR": str(root.parent),
                 "ZCODE_PROVIDER": provider, "ZCODE_MODEL": model,
                 "ZCODE_ACP_RUNTIME": "node"}
    # The managed bridge owns provider-table discovery and account RPCs. Its
    # native data-root contract is checked above instead of guessing another home.
    resolved = dict(target)
    resolved["argv"] = [node, *target["argv"][1:]]
    resolved["cli_path"] = str(cli)
    cli_argv = [node, str(cli)] if cli.suffix in (".cjs", ".mjs", ".js") else [str(cli)]
    resolved["version_argv"] = [*cli_argv, "--version"]
    resolved["zcode_paths"] = {key: child_env[key] for key in ("ZCODE_BIN", "ZCODE_NODE", "ZCODE_HOME")}
    resolved["zcode_model"] = {"provider_id": provider, "model_id": model}
    return resolved, child_env


def probe(target: dict, env: dict[str, str]) -> tuple[bool, str]:
    """Check native backend readiness without a session or a model request."""
    adapter = Path(target["adapter_path"])
    resolver = adapter.parent / "backend/resolve.js"
    command = [target["argv"][0], "--input-type=module", "-e",
        "const r = await import(process.argv[1]); "
        "process.stdout.write(JSON.stringify(r.builtinProviderEnv(process.env.ZCODE_BIN)));",
        resolver.as_uri()]
    try:
        paths = subprocess.run(command, text=True, capture_output=True, env=env, timeout=10, check=True)
        backend_env = {**env, **json.loads(paths.stdout)}
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return False, f"Cannot resolve ZCode provider runtime: {exc}"
    command = [*target["version_argv"][:-1], "app-server"]
    try:
        completed = subprocess.run(command, input="", text=True, capture_output=True,
                                   env=backend_env, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"ZCode app-server startup failed: {exc}"
    for line in completed.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("method") == "startup/storageState" and \
                event.get("params", {}).get("phase") == "ready" and completed.returncode == 0:
            return True, "ZCode app-server reported startup/storageState ready; no model request."
    return False, f"ZCode app-server did not report ready (exit {completed.returncode}). " + completed.stderr.strip()[-1500:]
