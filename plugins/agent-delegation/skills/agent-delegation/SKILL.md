---
name: agent-delegation
description: Use when handing a meaningful research, analysis, writing, operations, or coding mission between Hermes, Claude Code, Codex, Kimi, zCode, OpenCode, or another registered ACP agent while preserving worker agency, inherited authority, effect gates, receipts, and cycle limits.
---

# Agent Delegation

Delegate a mission, not a procedure. Use `agent-delegate` as the normal cross-agent entry point. ACPX is the transport; this Skill preserves the worker's freedom to solve the problem while the wrapper enforces exact runtime contracts.

## Delegate when

- another registered agent has materially better context, tools, model access, or an already-useful session boundary;
- a meaningful outcome or problem can be handed over, rather than a trivial instruction that the caller can complete just as well;
- the caller can state the goal, relevant context, existing authorization, and observable completion without prescribing the solution.

Do not delegate merely to create activity or evade a permission boundary. Have a concrete reason for choosing the target, but do not turn target selection into a scripted quality score or routing gate. Delegation is not limited to coding.

## Agency boundary

Inside the authority already granted by the owner, the worker owns interpretation, decomposition, strategy, exploration, tool choice, prioritization, and expression. It may inspect adjacent context, challenge assumptions, revise the plan, perform necessary related work, and return a better framing or alternative solution.

Suggested steps and files are starting points, not constraints, unless the owner made them explicit requirements. Require an exact output schema only when a real machine consumer needs it. A denied effect must not prevent unrelated reasoning, drafting, or preparation from continuing.

The caller remains accountable for the overall user outcome and for effects that were not delegated. It should integrate the worker's result and verify decision-critical evidence in proportion to risk, not redo the delegated work by default.

## Procedure

1. Run `agent-delegate list --json` when the available targets are not already known. Run `agent-delegate doctor --json` after installation changes or when a target fails to start.
2. Choose the target semantically and give it a meaningful slice of the outcome. State the capability or context advantage that makes the handoff useful.
3. Send a concise mission. Read [references/task-packet.md](references/task-packet.md) when the handoff needs several inputs, carries existing effect authorization, has commit gates, or feeds a machine consumer.
4. Use a fixed existing `--cwd`. The default and maximum turn timeout are both two hours; pass a lower `--timeout` only when the mission needs a tighter execution budget. Preserve the target's normal tool, Terminal, network, and plugin capabilities unless the owner or the actual execution environment requires a capability reduction. Do not translate a semantic prompt boundary into unrelated tool bans.
5. Run the task. Prefer a task file for long or sensitive instructions:

   ```bash
   agent-delegate run \
     --to codex \
     --caller hermes \
     --cwd /absolute/task/root \
     --task-file /absolute/delegation-envelope.md
   ```

   A nested delegated agent should rely on the injected `AGENT_DELEGATION_CALLER` and `AGENT_DELEGATION_CHAIN` environment when available. If the host drops them, pass the current host with `--caller` and the received chain with `--chain`.
6. Inspect the structured result and receipt. A zero exit code proves transport completion, not semantic correctness. Validate observable claims in proportion to risk, integrate the result, and report material incomplete work.

Task text and normalized assistant text are complete by default. Do not add character-count caps merely to make a delegation look bounded: provider context limits and transport failures should remain visible as their real errors. `max_task_chars` and `max_result_chars` are optional registry controls for an owner who explicitly needs those limits; absent keys mean no wrapper-level character cap.

## Authority and permissions

- Delegation may carry authority the owner already granted; it never creates new authority. Record the real basis instead of asking the owner to approve the same in-scope work again.
- Capability is not authority. Normal delegation keeps the target's ordinary tools, Terminal, and network access available so it can choose its own exploration and solution path. The mission prompt defines the goal, requirements, inherited authority, and effects that still need approval.
- The wrapper therefore defaults to `approve-all` with Terminal advertised and does not require a separate authorization flag. The mission prompt carries the actual boundary. `--authorization-note` is optional receipt metadata and never creates authority.
- `deny-all`: use for transport smokes or genuinely tool-free reasoning, not as the normal delegation mode.
- `approve-reads`: an explicit restriction for work known to stay on ACP read/search requests. A target that chooses Shell even for read-only discovery will fail closed in non-interactive mode.
- `--no-terminal`: an explicit capability reduction. Pair it with the intended restricted permission mode; do not use it merely because a mission is described as read-only.
- `approve-all` and Terminal availability let ACP carry the target's tool choices; they do not authorize every effect those tools could produce.

Sending messages, publishing, deploying, purchasing, trading, changing identity or access, deleting data, and rotating credentials are examples of commit effects: carry them only when the owner explicitly authorized that exact effect and scope. Otherwise the worker may prepare the action but must pause before commitment. A worker can never approve its own effects.

`--cwd` supplies task context; it is not an operating-system sandbox. Prompt gates are appropriate for ordinary work in an owner-controlled environment. If the target can reach high-risk external systems or irreversible effects, use real host isolation or a tool-specific policy, or keep that capability outside the delegation; neither ACPX's coarse modes nor an authorization note can prove semantic safety.

The managed ZCode adapter uses `--no-browser` only to prevent an unattended delegation from starting an interactive OAuth or device-login flow. It does not disable ZCode's ordinary browser, web, network, or Terminal tools. Repair provider authentication directly when needed rather than letting a delegated turn change identity state.

Do not add command-text allowlists that pretend to infer whether arbitrary Shell or network use is semantically safe. When capability restriction is intentional, invoke it explicitly, for example `--permissions approve-reads --no-terminal`, and treat any resulting permission failure as an honest blocked result.

## Delegation chain

The wrapper rejects direct self-delegation, repeated agents in a chain, and depth beyond the configured maximum. Preserve the injected chain when delegating again. Do not work around a cycle rejection by renaming the same target.

## Operations

- `agent-delegate register` adds an exact executable argv to both the wrapper registry and ACPX structured agent map. Use it only when the owner asks to add a new local ACP target and provenance has been reviewed.
- `acpx` remains installed for operator-managed persistent sessions, comparison, and cancellation. Do not bypass `agent-delegate` for ordinary model-initiated handoffs because raw ACPX lacks this Skill's receipt and cycle contract.
- If `doctor` reports a missing runtime or registry mismatch, read [references/operations.md](references/operations.md). Do not silently download a replacement during a task.
