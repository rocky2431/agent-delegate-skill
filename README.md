# Agent Delegation

[English](README.md) · [简体中文](README.zh-CN.md)

Agent Delegation lets one local coding agent hand a mission to another. It works
with Hermes, Claude Code, Codex, Kimi Code, zCode, OpenCode, and other registered
ACP agents.

The worker receives the goal, relevant context, existing authority, and a useful
completion condition. It chooses how to investigate, which tools to use, and how
to present the result. A Python wrapper gives each task an identity, keeps its
time budgets separate, and records inspectable receipts. ACPX provides the native
agent session and transport.

Use it when another agent has a better tool, useful context, or an independent
perspective, and when a task should keep running after the caller stops waiting.
For a small task that the current agent can finish directly, delegation adds
nothing.

Version: 0.4.0.

- [Install and start](#install-and-start)
- [Your first delegation](#your-first-delegation)
- [How delegation works](#how-delegation-works)
- [Sessions, concurrency, and time](#sessions-concurrency-and-time)
- [Authority and effects](#authority-and-effects)
- [Receipts and recovery](#receipts-and-recovery)
- [Current limits](#current-limits)
- [Updates and removal](#updates-and-removal)
- [Documentation](#documentation)
- [Development](#development)

## Install and start

You need Python 3.11 or later, Node.js with npm, and at least one supported agent CLI. The
installer creates the shared runtime, registers reviewed targets, and copies the
portable Skill into the hosts you select.

Clone the repository and enter it:

```bash
git clone https://github.com/rocky2431/agent-delegate-skill.git
cd agent-delegate-skill
```

If all six supported CLIs are installed, install everything and check it:

```bash
python3 scripts/install_user.py install
python3 scripts/install_user.py doctor
```

To install only selected hosts and targets, pass comma-separated lists:

```bash
python3 scripts/install_user.py install \
  --hosts hermes,kimi \
  --targets hermes,kimi
```

The portable Skill is installed in these native user directories:

| Host | Skill directory |
|---|---|
| Hermes | `~/.hermes/skills/agent-delegation` |
| Claude Code | `~/.claude/skills/agent-delegation` |
| Codex portable discovery | `~/.agents/skills/agent-delegation` |
| Kimi Code | `$KIMI_CODE_HOME/skills/agent-delegation` (default `~/.kimi-code/skills/agent-delegation`) |
| zCode | `~/.zcode/skills/agent-delegation` |
| OpenCode | `~/.config/opencode/skills/agent-delegation` |

### Codex plugin

For Codex, the native plugin is the preferred Skill distribution. Install the
runtime and Codex target without a second portable Codex copy:

```bash
python3 scripts/install_user.py install --hosts none --targets codex
codex plugin marketplace add rocky2431/agent-delegate-skill --ref main
codex plugin add agent-delegation@rocky-agent-delegation
```

From a local checkout, replace the marketplace command with:

```bash
codex plugin marketplace add /absolute/path/to/agent-delegate-skill
```

Do not enable both the portable Codex copy and the same named plugin. Codex may
discover the Skill twice. `task-state-with-files` is a separate Skill and remains
a separate installation.

### Reviewed targets

| Target | ACP entry |
|---|---|
| Hermes | `hermes acp` |
| Claude Code | `claude-agent-acp`, bound to the local `claude` executable |
| Codex | `codex-acp`, bound to the local `codex` executable |
| Kimi Code | `kimi acp` |
| zCode | the installed `zcode-acp` bridge |
| OpenCode | `opencode acp` |

Managed targets keep their normal model, authentication, tools, and plugin
configuration. The zCode entry uses `--no-browser` to prevent an unattended
OAuth or device-login prompt. It does not disable ordinary network or web tools.

List the targets available in the current installation:

```bash
agent-delegate list --json
```

## Your first delegation

Start with a mission file. It can be short, but it should say what the worker owns
and what evidence would show that the work is done:

```text
Investigate why the import loses the final row. Reproduce it and fix the shared
cause in this checkout. Local edits and tests are authorized. Publishing is not.
Return the cause, the changed files, and the validation result.
```

Submit it once:

```bash
agent-delegate submit \
  --to codex \
  --cwd /absolute/path/to/project \
  --task-file /absolute/path/to/mission.md
```

Keep the returned `delegation_id`, then wait on that exact task:

```bash
agent-delegate wait --id <delegation_id> --timeout 30
```

Read the JSON on every return. A command exit code of zero does not mean the
mission is complete.

| Returned state | What to do |
|---|---|
| `terminal: false` | Keep the ID and wait again later. The task is starting, queued, or running. |
| `wait_timed_out: true` | Only this observation ended. The task continues. Do not submit a duplicate. |
| `terminal: true`, `status: success` | Read the text and content blocks, then verify any decision-critical claim. |
| `terminal: true`, another status | Inspect the reason, partial output, and receipt before retrying. |

Use `status` for an immediate snapshot:

```bash
agent-delegate status --id <delegation_id>
```

For a larger handoff, follow the
[mission context guide](plugins/agent-delegation/skills/agent-delegation/references/task-packet.md).
Suggested files and methods are leads unless the owner made them requirements.

## How delegation works

```text
Host agent
  -> portable Agent Delegation Skill
  -> agent-delegate task boundary
  -> installed ACPX runtime
  -> target ACP agent
```

1. The host chooses a suitable registered target and prepares the mission.
2. `agent-delegate` validates the target, working directory, budgets, and explicit
   capability mode. It creates a `delegation_id` before work starts.
3. ACPX starts a one-off agent or continues a named native session. The target
   keeps its own model and tools unless the caller makes an explicit choice.
4. Events and diagnostics are written while the task runs. The final result keeps
   the agent text, original content blocks, stop reason, errors, and runtime identity.

The Skill describes the mission. The wrapper handles task identity, boundaries,
and evidence. ACPX owns the agent session lifecycle. Agent Delegation is not a
workflow engine or a shared task database.

A `success` status means the ACP turn ended normally. It does not prove that the
business outcome or acceptance criteria passed. The caller should check important
claims in proportion to their risk.

## Sessions, concurrency, and time

A delegation ID identifies one submitted task. A session name carries conversation
context across separate tasks. Reuse the same target, working directory, and
session name only when the next task should continue that conversation:

```bash
agent-delegate submit --to codex --cwd /absolute/path/to/project \
  --session review --task 'Investigate the failure and report what is missing.'

agent-delegate submit --to codex --cwd /absolute/path/to/project \
  --session review --task 'Use this new detail and continue the investigation.'
```

Tasks in the same named session wait their turn. Different sessions and tasks
without `--session` can run independently, subject to the target's own capacity.
The worker does not inherit the caller's full conversation or model. Pass the
context it needs, and use `--model` only when a specific target model matters.

The three timeout options control different clocks:

| Option | Meaning |
|---|---|
| `submit --timeout N` | Execution budget after queue admission and session setup. |
| `submit --queue-timeout N` | Optional limit on waiting for a named-session turn. |
| `wait --timeout N` | How long this observer waits. Expiry never stops the task. |

Fresh installations use a 7200-second execution limit and delegation depth 4.
Existing registry values are preserved. Check the effective values with:

```bash
agent-delegate doctor --to codex --json
```

When one task must stop, cancel its full ID and keep observing it until a final
state is available:

```bash
agent-delegate cancel --id <delegation_id>
```

A cancellation acknowledgement is not proof that the model turn or its child
processes have stopped. Named-session cancellation and closure are operator
controls described in the [operations guide](plugins/agent-delegation/skills/agent-delegation/references/operations.md).

## Authority and effects

Delegation carries authority that the owner already granted. It cannot create new
authority, and moving work to another agent does not require asking for the same
in-scope permission again.

The default transport mode is `approve-all` with Terminal available. This keeps
the target's normal Shell, network, search, and tool choices available. Capability
does not authorize an effect. The mission still controls scope, and the worker
must pause before an ungranted effect such as publishing, deployment, purchase,
trading, deletion, a credential change, or an identity and permission change.

Use `approve-reads`, `deny-all`, or `--no-terminal` only when the task calls for
an intentional capability restriction. ACPX cannot determine the meaning of an
arbitrary Shell command, so restricted modes can return a real permission denial.

A working directory, prompt, permission mode, or `--authorization-note` is not an
OS sandbox. Tasks that can reach sensitive systems still need the host or tool
policy appropriate to that system.

## Receipts and recovery

Each submitted or synchronous task writes a private receipt under
`~/.local/state/agent-delegation/runs/`:

| File | Contents |
|---|---|
| `request.json` | Task identity, target, boundaries, hashes, session choice, and launch command. It omits the original mission text. |
| `events.ndjson` | Streamed ACPX events. Read-file bodies are suppressed from this transport log. |
| `stderr.log` | Runtime diagnostics written while the task runs. |
| `state.json` | Current phase, ownership, and timing. |
| `worker.log` | Background wrapper diagnostics for a submitted task. |
| `result.json` | Final state, text, content blocks, errors, timing, and session identity. |
| `runtime.json` | CLI, adapter, and ACPX identities observed for the session. |

`status`, `wait`, and task-ID cancellation use the original receipt. If the
submit response is lost, the startup message on stderr and `request.json` retain
the task ID.

If wrapper ownership disappears before a final result, the task becomes
`incomplete` with `execution_state: unknown`. This ends wrapper observation but
does not establish that native work stopped. Inspect the events, partial output,
native session, and possible effects before retrying.

## Current limits

- Agent Delegation passes explicit context. It does not copy the caller's complete
  conversation, ambient secrets, or model choice into the worker.
- Native turn cancellation does not guarantee terminal process cleanup. Inspect
  and stop the specific native job when cleanup matters.
- `cwd` and prompts are coordination inputs, not process isolation.
- The runtime records transport success and structured errors. Semantic acceptance
  still belongs to the caller or an independent verifier.
- OpenCode is registered, but the latest live checks did not establish a completed
  round trip because its native ACP path timed out and a separate run returned an
  authentication error.
- Kimi Code uses `$KIMI_CODE_HOME/skills`, defaulting to `~/.kimi-code/skills`. The legacy Python
  `kimi-cli` directory at `~/.kimi` is not migrated or removed, and this package
  does not install recovery hooks.

## Updates and removal

An ordinary update refreshes the managed Skill and wrapper entry. It preserves
the selected runtime, custom budgets, unselected targets, and old runtime
generations used by warm sessions:

```bash
python3 scripts/install_user.py install
```

Runtime dependencies change only through an explicit upgrade:

```bash
python3 scripts/install_user.py install --update-runtime
```

The installer stages a new runtime under
`~/.local/share/agent-delegation/runtimes/`, records its installed versions and
lock hash, then switches the registry. A failed upgrade leaves the previous
runtime selected. Managed files are backed up before replacement.

Remove installed Skills while retaining the shared runtime, or remove both:

```bash
python3 scripts/install_user.py uninstall
python3 scripts/install_user.py uninstall --remove-runtime
```

Only files with this package's managed marker are removed.

## Documentation

- [Skill instructions](plugins/agent-delegation/skills/agent-delegation/SKILL.md):
  the procedure loaded by an agent.
- [Mission context](plugins/agent-delegation/skills/agent-delegation/references/task-packet.md):
  what to include in a substantial handoff.
- [Operations guide](plugins/agent-delegation/skills/agent-delegation/references/operations.md):
  synchronous runs, session controls, receipts, diagnosis, registration, and rollback.
- [Kimi Code Skill discovery](https://www.kimi.com/code/docs/kimi-code-cli/customization/skills.html):
  the native Kimi Code Skill directory and loading behavior.

## Development

The repository keeps a reproducible development snapshot of ACPX 0.13.2,
`@agentclientprotocol/claude-agent-acp` 0.75.1, and
`@agentclientprotocol/codex-acp` 1.10.0 in `runtime/package-lock.json`. User
runtime upgrades are managed separately and do not silently return to this
snapshot.

Run the standard checks from the repository root:

```bash
python3 -m unittest discover -s tests -v
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-creator/scripts/quick_validate.py" \
  plugins/agent-delegation/skills/agent-delegation
```

With an installed ACPX runtime, run the transport regression separately:

```bash
AGENT_DELEGATION_TEST_ACPX=/absolute/path/to/runtime/node_modules/.bin/acpx \
  python3 -m unittest discover -s tests -p test_acpx_transport.py -v
```

The tests use isolated temporary configuration and local ACP fixtures. They do
not call a model or read user sessions.
