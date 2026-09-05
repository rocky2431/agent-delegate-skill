# Operations

## Runtime and target configuration

The portable Skill is shared across hosts. Codex can receive it through the native
plugin; other hosts use their user Skill directories. `agent-delegate` runs the
canonical copy under `~/.local/share/agent-delegation/skill`.

The wrapper registry is `~/.config/agent-delegation/config.json`. Its `targets`
entries are the launch source: the wrapper passes that exact argv to ACPX without
a shell, so a project `.acpxrc.json` agent alias cannot select another executable.
ACPX still loads its native model, authentication, MCP, and other configuration.
The installer also maintains aliases in `~/.acpx/config.json` for direct operator
use; these aliases are not a second startup gate for the wrapper.

Fresh installations default to 7200 seconds and depth 4. Existing registry values
are preserved, including longer owner-configured budgets. `--timeout` must fit the
configured maximum; `--max-depth` may lower the inherited/configured ceiling.
Neither budget counts sibling tasks or constitutes a semantic authorization gate.

Task and result text have no default character cap. Optional positive
`max_task_chars` limits input; `max_result_chars` limits the convenience
`assistant_text` field. `assistant_content` and the event file preserve the full
content. All configured limits are validated before the worker starts.

## Task observation and time budgets

`submit` starts a task-owned background wrapper and returns its `delegation_id`.
`status --id <delegation_id>` reads its current snapshot or final result.
`wait --id <delegation_id> --timeout 30` waits up to 30 seconds for a final result;
zero requests an immediate snapshot. On expiry it returns `terminal: false` and
`wait_timed_out: true`, without cancelling execution. Interrupting the observer
also leaves the submitted task running. Reuse the same ID to collect its result.

`submit` and `status` returning exit zero only confirm submission or observation.
An expired `wait` also exits zero. Check `terminal` and `status` in the JSON.
A `wait` that returns a final result uses the task's outcome as its exit code.

| Option | Clock and effect |
|---|---|
| `submit --timeout N` (also `run`) | Execution budget in seconds, after queue admission and named-session setup. Defaults to the registry value. |
| `submit --queue-timeout N` (also `run`) | Optional positive limit on waiting for the wrapper session lock. Expiry ends only this waiting task. Omitted means no queue deadline. |
| `wait --timeout N` | Nonnegative observation duration, default 30 seconds. Expiry never stops execution. |

Named-session setup has its own bounded attempt and does not subtract from the
execution budget. Receipts expose `queue_wait_seconds`, `execution_seconds`,
`execution_started_at`, and `timeout_phase` (`queue`, `setup`, or `execution`).
Execution timing includes cleanup; native startup and cancellation grace can make
wall-clock duration exceed the requested execution budget.

`run` accepts the same mission options as `submit` but keeps execution attached to
the invoking process and returns the final JSON. Use it when synchronous execution
is specifically useful and the host can keep that process alive. Interrupting
`run` can stop its task; it does not have `wait`'s observer-only semantics.

## Sessions and cancellation

`submit --session <name>` (also `run`) ensures a native ACPX session and sends a
prompt. Reuse the same target argv, cwd, and name for continuation. Wrapper calls
sharing these values and the receipt root wait their turn before submitting to
ACPX. Queue wait does not consume execution budget. Different sessions, or calls
without `--session`, are independent; the target's own capacity still applies.
The target must implement the ACP capabilities needed for its session lifecycle.

For one task, including a queued task, use:

```bash
agent-delegate cancel --id <delegation_id>
```

This requests cancellation only for that invocation; an old task ID cannot cancel
a later turn. Observe the same ID until a final outcome is available. If wrapper
ownership is lost, the snapshot reports `incomplete` with
`execution_state: unknown`: this is not proof the native worker stopped. Inspect
events, partial output, and the native session before retrying a task with effects.
Elapsed timing fields are `null` when wrapper ownership is lost, because neither
the final duration nor time spent in each phase can be established.

Only when intentionally stopping a named session's active turn, use:

```bash
agent-delegate cancel --to codex --cwd /absolute/task/root --session review
```

Only when that conversation is finished and no remaining task needs it, close it:

```bash
agent-delegate close --to codex --cwd /absolute/task/root --session review
```

The OS releases a wrapper's session lock on exit. Direct native ACPX calls do not
participate in this lock; keep them sequential with wrapper turns. Native ACPX
operations remain available for capabilities not exposed here; preserve the same
target, task authority, and useful evidence.

## Receipts and recovery

Each `run`, `submit`, or native session control creates a private directory under
`~/.local/state/agent-delegation/runs/`:

- `request.json`: operation, requested model/session, argv, limits, and task hash;
  it does not contain the original task prompt.
- `events.ndjson` and `stderr.log`: streamed bytes, available while work runs.
  ACPX suppresses read-file bodies in its event output, not in the worker's tools.
- `state.json`: task ID, live phase, timing, and wrapper ownership metadata.
- `worker.log`: background wrapper diagnostics for `submit`. Its private
  `launch.json` holds the launch plan, including the mission, until the worker
  consumes and removes it; inherited environment secrets are not serialized.
- `result.json`: terminal state, text, original content blocks, session ids,
  structured RPC/tool errors, and receipt location.

Task-ID `status`, `wait`, and `cancel` reuse this receipt instead of creating new
tasks. Cancellation writes a task-specific request for the owning wrapper; it does
not signal a saved PID or blindly cancel the active session. If the submit response
was lost, the starting message on stderr and `request.json` preserve its ID.

Terminal states distinguish normal completion (`success`), `cancelled`, `timeout`,
`denied`, `not_found`, `refused`, `incomplete`, and `error`. The underlying exit
code is retained; wrapper cancellation and timeout exit with 130 and 124.
A cancellation request succeeding does not mean the active task has already
finished; read that task's final receipt.
When the executing wrapper is interrupted, it also cancels its submitted named
session turn. `cancellation_exit_code` records that control request; an unconfirmed
cancellation is `incomplete`. Recovery controls do not consume delegation depth
or depend on task character limits or the worker executable still being present.
If ACPX returns without a terminal event, the result remains `incomplete`, even
when its process exits zero; inspect the events and diagnostics before continuing.

`rpc_errors` preserves method, code, message, and data where available.
`tool_errors` identifies client file/terminal/permission operations; the legacy
`protocol_errors` field excludes those ordinary operations. A recovered tool
error does not turn `end_turn` into failure. Raw events remain the evidence when
an adapter cannot supply enough information to classify an event.

## Diagnose and recover

```bash
agent-delegate doctor --to codex --json
```

Doctor reports the effective registry budgets and launch argv. Version probes are
informational: a stale standalone CLI path does not prove that an adapter fails,
and an adapter may bundle a different CLI. Fix the failing target or configuration
layer. Do not treat an unrelated target warning as a global startup prohibition.

Repair authentication directly through the provider. The managed ZCode adapter's
`--no-browser` avoids unattended OAuth/device login; it does not disable ordinary
web tools. Normal task execution does not download a replacement runtime or use
`npx -y`.

## Install and register

From a reviewed checkout, `install --hosts kimi` installs that host's Skill and
configures only Kimi's target. Use `--targets` to select ACP targets separately
from Skill hosts; `--hosts none --targets none` installs only the shared runtime.
Existing unselected targets and custom budgets are preserved. Replacing an
unmanaged Skill or executable remains an explicit operation.

Register an already installed ACP executable when the owner requests a new target:

```bash
agent-delegate register --name example \
  --argv-json '["/absolute/path/example", "acp"]' \
  --observed-version '1.2.3' --provenance 'official package example@1.2.3'
```

Registration backs up the configuration. Verify the selected target and a small
appropriate round trip before relying on a new adapter for substantial work.
