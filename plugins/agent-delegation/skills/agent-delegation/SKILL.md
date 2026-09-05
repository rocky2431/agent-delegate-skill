---
name: agent-delegation
description: Delegate missions between Hermes, Claude Code, Codex, Kimi, zCode, OpenCode, or another installed ACP agent, with native session continuation and inspectable results.
---

# Agent Delegation

Give the worker a mission, relevant context, existing authority, and a useful completion condition. Let it choose the reasoning, exploration, tools, and solution. Delegation is not limited to coding. A short prompt is enough; use [task-packet.md](references/task-packet.md) for a more involved handoff.

Use `agent-delegate list --json` if targets are unknown. Choose a target for its useful context, tools, or independent perspective; the same agent type can run another independent task.

```bash
agent-delegate run --to codex --caller hermes \
  --cwd /absolute/task/root --task-file /absolute/mission.md
```

The wrapper passes the registered executable argv directly to ACPX. It records provenance and applies the configured depth and time budgets. Agent names may repeat in the chain. Nested workers inherit caller/chain metadata; provide `--caller` or `--chain` when the host cannot preserve it.

## Continue or cancel

Use `--session <name>` when work needs follow-up. Repeat the same target, cwd, and session name to continue. Omit it for an independent one-shot task. Wrapper calls sharing a session wait their turn automatically; interrupting a waiting call does not cancel the active one.

```bash
agent-delegate run --to codex --caller hermes --cwd /absolute/task/root \
  --session review --task 'Investigate the issue and identify missing information.'
agent-delegate run --to codex --caller hermes --cwd /absolute/task/root \
  --session review --task 'Here is the requested detail. Continue the investigation.'
agent-delegate cancel --to codex --cwd /absolute/task/root --session review
agent-delegate close --to codex --cwd /absolute/task/root --session review
```

`--model` passes an explicitly chosen model to the target. Omit it to use the target's defaults; the caller's current model and conversation are not automatically inherited. Use [operations.md](references/operations.md) for receipts, configuration, and recovery. Native ACPX operations remain available when a needed capability is not exposed here; preserve the same target, task authority, and useful evidence.

## Authority and capabilities

Capability is not authority. Delegation carries the owner's existing authorization and cannot enlarge it. Pause only before an ungranted effect; continue unrelated analysis and preparation. Do not ask for the same in-scope authorization again merely because work moved to another agent.

The wrapper defaults to `approve-all` with Terminal advertised. Network, Shell, and tool choices are not removed merely because a task is described as read-only. `--authorization-note` is optional receipt metadata, never a startup gate or a permission grant.

Use `approve-reads`, `deny-all`, or `--no-terminal` only for an intentional capability restriction. ACPX cannot infer the semantic safety of arbitrary Shell commands; a restricted non-interactive permission request can return `denied`. Do not add command-text allowlists. A cwd and a prompt are not an OS sandbox; preserve any real host or tool policy required by the task.

## Read the result

The start message on stderr identifies a private receipt directory. Events and diagnostics are written there during execution. The final JSON includes the stop reason, text, content blocks, session identity, structured errors, and receipt path; partial output survives timeout and cancellation.

`success` means the ACP turn ended normally, not that the user's outcome is proven. Integrate the result and verify decision-critical claims proportionally. Tool errors such as a missing optional file do not automatically invalidate a completed turn. Inspect their details instead of requiring an empty error list or repeating all of the worker's work.

There is no default task/result character cap. Timeout and depth come from the registry; `doctor --to <target> --json` shows effective limits and target health. Run it when diagnosing a failure, not as a mandatory gate before each delegation.
