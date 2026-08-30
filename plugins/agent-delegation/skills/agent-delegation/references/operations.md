# Operations

## Installation model

The portable Agent Skill is the shared contract. The Codex plugin is a native distribution wrapper for Codex only. Hermes, Claude Code, Kimi, zCode, and OpenCode use their native user Skill directories through `scripts/install_user.py` from the repository.

The installer creates a private local runtime under `~/.local/share/agent-delegation`, exact structured agent argv in `~/.acpx/config.json`, a richer registry in `~/.config/agent-delegation/config.json`, and commands in `~/.local/bin`.

## Recovery

Run:

```bash
agent-delegate doctor --json
```

Fix only the failed layer:

- missing Skill: rerun the installer for that host;
- missing runtime package: rerun `python3 scripts/install_user.py install --hosts none` from a reviewed checkout;
- registry mismatch: rerun the installer or repeat an explicit `agent-delegate register` operation;
- provider authentication failure: repair that provider directly without copying credentials into mission envelopes or receipts.
- `PERMISSION_PROMPT_UNAVAILABLE` during an explicitly restricted `approve-reads` or `deny-all` mission: inspect the receipt before retrying. Some targets use Shell or network access for ordinary discovery, and ACPX does not classify arbitrary commands by semantic effect. Remove the unintended restriction only when the prompt boundary and owner-controlled or actually sandboxed environment make normal capabilities appropriate; otherwise keep the result blocked. Do not add a command-text allowlist that pretends to prove shell semantics.

Normal task execution must not use `npx -y` or an unpinned remote package fallback.

## Register another ACP target

The executable must already be installed and reviewed. Use absolute argv and record provenance:

```bash
agent-delegate register \
  --name example \
  --argv-json '["/absolute/path/example", "acp"]' \
  --observed-version '1.2.3' \
  --provenance 'official package example@1.2.3'
```

Then run `agent-delegate doctor --json` and one no-write smoke before using it for real work.
