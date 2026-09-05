---
name: agent-delegation
description: Delegate missions between Hermes, Claude Code, Codex, Kimi, zCode, OpenCode, or another installed ACP agent, with native session continuation and inspectable results.
---

# Agent Delegation

Give the worker a mission, relevant context, existing authority, and a useful completion condition. Let it choose the reasoning, exploration, tools, and solution. Delegation is not limited to coding. A short prompt is enough; use [task-packet.md](references/task-packet.md) for a more involved handoff.

Use `agent-delegate list --json` if targets are unknown. Choose a target for its useful context, tools, or independent perspective; the same agent type can run another independent task.

## Submit and collect

For ordinary delegation, submit once and keep the returned `delegation_id` with the task it identifies:

```bash
agent-delegate submit --to codex \
  --cwd /absolute/task/root --task-file /absolute/mission.md
```

Then wait using that full ID. Replace `<delegation_id>` with the returned value:

```bash
agent-delegate wait --id <delegation_id> --timeout 30
```

Read the JSON on every return; a successful command exit is not task completion.

| Returned state | Next action |
|---|---|
| `terminal: false` (`starting`, `queued`, or `running`) | Keep this ID. Wait again, or do independent work and return to it. |
| `wait_timed_out: true` | Only this observation ended; the task continues. Wait again on the same ID. Do not resubmit or cancel because a wait expired. |
| `terminal: true`, `status: success` | Read `assistant_text` and `assistant_content`, integrate the result, and verify decision-critical claims proportionally. |
| `terminal: true`, another status | Inspect the reason, partial output, and receipt before deciding how to recover. `incomplete` or `execution_state: unknown` needs inspection before retrying; the worker may have performed effects. |

`execution_state: unknown` means the wrapper cannot establish the worker's outcome.
A saved `queued` phase, zero execution time, or empty logs alone do not prove that
work never started or has stopped. Report the outcome as unresolved unless native
state or effects establish it; do not describe that delegated task as completed.
For example, `{"terminal": true, "status": "incomplete", "execution_state": "unknown", "execution_seconds": null}`
means: "The wrapper ended without a final result; native execution remains unknown.
Inspect native state and effects before retrying." Here `terminal` ends wrapper
observation; it does not establish that the native task stopped.

`status --id <delegation_id>` reads progress immediately and retrieves the same full result after completion. If a submit response is lost, recover the ID from its stderr receipt or saved request before submitting again. If the host interrupts a `wait`, resume observation with the same ID; the submitted task runs independently of that observer.

The timeouts have different meanings: `wait --timeout` limits one observation; `submit --timeout` limits execution, excluding queue wait and session setup. Omit the execution override to use the configured budget. Add `--queue-timeout` only when the task needs a queue deadline; by default it waits for admission or cancellation.

## Follow up or cancel when needed

A delegation ID identifies one submitted task. A session name carries conversation context across tasks. Omit `--session` for independent work, even when several tasks go to the same target. To continue a conversation, submit a new follow-up with the same target, cwd, and `--session <name>` used for the first task. Wrapper turns sharing that session wait automatically; separate sessions or one-shot tasks can run independently.

When a specific task should stop, cancel its ID and then observe that ID until the outcome is known:

```bash
agent-delegate cancel --id <delegation_id>
```

A cancellation acknowledgement is not proof that work stopped. Use task-ID cancellation for a queued task; session-wide cancellation targets the session's active turn. Cancellation and session closure are conditional controls, not routine result-collection steps. See [operations.md](references/operations.md) for synchronous `run`, session controls, receipts, and recovery.

A native `cancelled` stop reason confirms that the model turn was interrupted. Spawned terminal commands or background jobs may survive it, as observed with Codex. When those jobs also need to stop, inspect and stop the specific jobs through the target's native controls; do not infer process cleanup from the turn's stop reason.

The worker does not automatically inherit the caller's conversation or model. Pass the context it needs; use `--model` only for an explicit model choice, otherwise retain the target's defaults. The wrapper launches the registered argv and records provenance. Agent names may repeat in the chain. Nested workers inherit caller/chain metadata. When metadata is absent, set `--caller` to your actual host label if known; otherwise retain `unknown`. Supply `--chain` only from known provenance.

## Authority and capabilities

Capability is not authority. Delegation carries the owner's existing authorization and cannot enlarge it. Pause only before an ungranted effect; continue unrelated analysis and preparation. Do not ask for the same in-scope authorization again merely because work moved to another agent.

The wrapper defaults to `approve-all` with Terminal advertised. Network, Shell, and tool choices are not removed merely because a task is described as read-only. `--authorization-note` is optional receipt metadata, never a startup gate or a permission grant.

Use `approve-reads`, `deny-all`, or `--no-terminal` only for an intentional capability restriction. ACPX cannot infer the semantic safety of arbitrary Shell commands; a restricted non-interactive permission request can return `denied`. Do not add command-text allowlists. A cwd and a prompt are not an OS sandbox; preserve any real host or tool policy required by the task.

## Read the result

The start message on stderr identifies a private receipt directory. Events and diagnostics are written there during execution. The final JSON includes the stop reason, text, content blocks, session identity, structured errors, and receipt path; partial output survives timeout and cancellation.

`success` means the ACP turn ended normally, not that the user's outcome is proven. Integrate the result and verify decision-critical claims proportionally. Tool errors such as a missing optional file do not automatically invalidate a completed turn. Inspect their details instead of requiring an empty error list or repeating all of the worker's work.

There is no default task/result character cap. Timeout and depth come from the registry; `doctor --to <target> --json` shows effective limits and target health. Run it when diagnosing a failure, not as a mandatory gate before each delegation.

Managed targets use the installed local CLI; Codex and Claude adapters bind it explicitly. `runtime_identity` separates CLI, adapter, and ACPX versions observed at startup. A warm session may still use its earlier process. A legacy session without a startup record is marked unverified. Ordinary Skill updates preserve the installed runtime; an explicit `install --update-runtime` upgrades it. See [operations.md](references/operations.md) for update and recovery details.
