---
name: agent-delegation
description: Use when delegating a bounded research, analysis, writing, operations, or coding task between Hermes, Claude Code, Codex, Kimi, zCode, OpenCode, or another registered ACP agent while preserving caller authority, permissions, receipts, and cycle limits.
---

# Agent Delegation

Use `agent-delegate` as the normal cross-agent entry point. ACPX is the transport; this Skill defines when a delegation is useful and the wrapper enforces the mechanical boundary.

## Delegate when

- another registered agent has materially better context, tools, model access, or an already-useful session boundary;
- a bounded packet can be completed independently and returned to the caller;
- the caller can state the expected output and evidence without transferring ownership of the whole task.

Do not delegate merely to create activity, evade a permission boundary, or replace a simple direct step. Delegation is not limited to coding.

## Procedure

1. Run `agent-delegate list --json` when the available targets are not already known. Run `agent-delegate doctor --json` after installation changes or when a target fails to start.
2. Keep the caller as the task authority. Choose the target semantically; do not encode task quality or target suitability into scripts.
3. Prepare one bounded task packet. Read [references/task-packet.md](references/task-packet.md) when the packet has dependencies, side effects, or a required output schema.
4. Use a fixed existing `--cwd`, a finite `--timeout`, and the least capable permission mode that can complete the packet.
5. Run the task. Prefer a task file for long or sensitive instructions:

   ```bash
   agent-delegate run \
     --to codex \
     --caller hermes \
     --cwd /absolute/task/root \
     --task-file /absolute/task-packet.md \
     --permissions approve-reads
   ```

   A nested delegated agent should rely on the injected `AGENT_DELEGATION_CALLER` and `AGENT_DELEGATION_CHAIN` environment when available. If the host drops them, pass the current host with `--caller` and the received chain with `--chain`.
6. Inspect the structured result and receipt. A zero exit code proves transport completion, not semantic correctness. The caller remains responsible for validating evidence, integrating the result, and reporting incomplete work.

## Authority and permissions

- `deny-all`: reasoning without approved tool access.
- `approve-reads`: default; auto-approve ACP read/search requests and fail closed on non-interactive escalation.
- `approve-all`: only after the owner explicitly authorizes the concrete side effects. Pass the real approval basis with `--authorization-note`; never invent one.
- `--terminal`: process execution capability. It also requires an explicit `--authorization-note`.

The worker never inherits authority to send messages, publish, deploy, purchase, trade, change identity/access, delete data, rotate credentials, or approve its own effects. Split preparation from commitment and return approval-required work to the caller.

## Delegation chain

The wrapper rejects direct self-delegation, repeated agents in a chain, and depth beyond the configured maximum. Preserve the injected chain when delegating again. Do not work around a cycle rejection by renaming the same target.

## Operations

- `agent-delegate register` adds an exact executable argv to both the wrapper registry and ACPX structured agent map. Use it only when the owner asks to add a new local ACP target and provenance has been reviewed.
- `acpx` remains installed for operator-managed persistent sessions, comparison, and cancellation. Do not bypass `agent-delegate` for ordinary model-initiated handoffs because raw ACPX lacks this Skill's receipt and cycle contract.
- If `doctor` reports a missing runtime or registry mismatch, read [references/operations.md](references/operations.md). Do not silently download a replacement during a task.
