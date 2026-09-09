# Operations

## Runtime and target configuration

The portable Skill is shared across hosts. Codex can receive it through the native
plugin; other hosts use their user Skill directories. `agent-delegate` runs the
canonical copy under `~/.local/share/agent-delegation/skill`.

The wrapper registry is `~/.config/agent-delegation/config.json`. Its `targets`
entries are the launch source: custom targets pass their exact argv to ACPX without
a shell. Managed targets use a stable `launch_argv` entry that executes a snapshot
of the registered `argv`. A project `.acpxrc.json` agent alias cannot select another executable.
ACPX still loads its native model, authentication, MCP, and other configuration.
The installer also maintains aliases in `~/.acpx/config.json` for direct operator
use; these aliases are not a second startup gate for the wrapper.

Codex binds `CODEX_PATH`; Claude binds `CLAUDE_CODE_EXECUTABLE`. Each points to the
stable local CLI entry, rather than the adapter's bundled CLI. At adapter startup
the launcher resolves that entry once, probes the selected executable, and passes
its resolved path to the adapter. Missing executables and compatibility errors
are surfaced; there is no bundled-CLI fallback or dependency download per task.

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
`wait --id <delegation_id>` blocks on the worker's ownership lock until it exits,
then reads the final receipt (or reports lost ownership). No timer wakes the model
and no repeated file reads are needed. An optional `--timeout N` bounds one
diagnostic observation; zero requests an immediate snapshot. On expiry it returns `terminal: false` and
`wait_timed_out: true`, without cancelling execution. Interrupting the observer
also leaves the submitted task running. Reuse the same ID to collect its result.

`submit` and `status` returning exit zero only confirm submission or observation.
An expired `wait` also exits zero. Check `terminal` and `status` in the JSON.
A `wait` that returns a final result uses the task's outcome as its exit code.

| Option | Clock and effect |
|---|---|
| `submit --timeout N` (also `run`) | Execution budget in seconds, after queue admission and named-session setup. Defaults to the registry value. |
| `submit --queue-timeout N` (also `run`) | Optional positive limit on waiting for the wrapper session lock. Expiry ends only this waiting task. Omitted means no queue deadline. |
| `wait --timeout N` | Optional nonnegative observation duration. Omitted means wait for completion. Expiry never stops execution. |

Named-session setup has its own bounded attempt and does not subtract from the
execution budget. Receipts expose `queue_wait_seconds`, `execution_seconds`,
`execution_started_at`, and `timeout_phase` (`queue`, `setup`, or `execution`).
Execution timing includes cleanup; native startup and cancellation grace can make
wall-clock duration exceed the requested execution budget.

`run` accepts the same mission options as `submit` but keeps execution attached to
the invoking process and returns the final JSON. Use it when synchronous execution
is specifically useful and the host can keep that process alive. Interrupting
`run` can stop its task; it does not have `wait`'s observer-only semantics.

## Host completion delivery

The originating host owns result delivery. The execution target may be any
registered external ACP agent; no native subagent routing is involved.

### Claude Code and zCode

Submit once. Start `agent-delegate wait --id <delegation_id>` through the host's
native Bash tool with `run_in_background: true`. Keep both the delegation ID and
the host's background-task ID. The waiter remains attached to that background
task and exits only when the real worker ends. Do not add `&`, detach the waiter,
or background only `submit`: those report the launcher's exit rather than the
mission's outcome. Let the host's task notification trigger collection; read the
output after that event, not on a timer.

When a host tears down its background tasks, the observer can end while the
submitted mission continues. Keep the delegation ID for later recovery. An output
notification about a killed observer does not prove the mission stopped.

CC's `asyncRewake` command hooks can also wake an idle session on exit code 2;
ordinary `async` hooks only deliver on a subsequent turn. Native background Bash
already covers ordinary delegation, so no global hook is installed here. zCode's
native background task notification similarly avoids a global hook.

### Codex

Background process execution and starting a model turn are separate capabilities.
The official App Server interface documents `process/outputDelta` and
`process/exited` for client-owned processes, and `turn/start` with `toolOutput` to
start a turn from an external result. A client already connected to the server
owning the originating thread can await `agent-delegate wait --id <id> --event`
and deliver that compact event through `turn/start`. Use the actual thread ID;
retain its receipt and event ID for recovery and deduplication. The existing
external worker and blocking observer are sufficient; a new process-control
system is not required.

This is a documented client integration path, not an installed Desktop adapter.
Confirm the authorized connection to the server that owns this thread before
claiming it works. Starting an unrelated app-server or resuming the same saved
thread under another owner does not establish delivery to the current session.

**The local code-runner callback failed idle acceptance.** On Desktop 26.903.61454
with bundled Codex 0.153.4, a five-minute external Pi mission completed at
2026-09-09 16:33:01 UTC. The `functions.exec` watcher did not issue its callback
until 16:41:14, after a user message started a turn at 16:41:10. A surviving cell
and successful `send_message_to_thread` call during an active turn do not prove
independent execution after the parent final response. Do not use that recipe
for unattended wakeup. Keep one attached wait when the host can leave the turn
active without repeated model calls; otherwise retain the ID and report pending
delivery. Do not emulate a callback with repeated `functions.wait` or status calls.

Official interfaces: [process events](https://learn.chatgpt.com/docs/app-server#process-execution),
[starting a turn with tool output](https://learn.chatgpt.com/docs/app-server#start-a-turn).

For Codex hosts that consume their persistent message queue (including the
Desktop version verified below):

```bash
agent-delegate submit --caller codex --notify codex --to zcode \
  --cwd /absolute/task/root --task-file /absolute/mission.md
```

The optional CLI path captures the actual `CODEX_THREAD_ID` and available `codex` executable at
submission. A separate observer waits on the existing worker lock and invokes
`codex queue --thread <original-id> --message <completion-event>` once. It also
reports a worker that exited without a result as `incomplete` / unknown execution.
Worker termination releases the OS lock; no completion-file polling is required.

`notification.json` tracks `pending`, `sending`, `queued`, `failed`, or `unknown`.
The event contains a stable `event_id`, delegation ID, wrapper outcome and receipt
path; it does not copy the worker's conversation into the parent. Delivery is
serialized per task. A repeated `notify` does not enqueue an already queued event.
The recipient should integrate each `event_id` once in its existing task record.

**Queue acceptance is not automatic wakeup.** Some hosts may only drain queued
input at a later user turn. Verify an idle-session probe in the actual Codex host
before relying on unattended continuation. On 2026-09-09, Desktop 26.903.61454
with Codex 0.153.4 passed an isolated idle probe through this independent CLI
notifier. The parent turn ended at 17:03:26 UTC; Pi finished one `sleep 300` and
returned `PI_IDLE_R2_OK` at 17:06:11.195. The notifier attempted enqueueing at
17:06:11.222, and the completion event started a new turn in the original thread
at 17:06:13. No intervening user turn or scheduled run occurred. Delegation
`6b6350c9e2b14717a7f1d3222ef0b4b8` took 306.91 execution seconds and ended with a
verified native Pi stop. This establishes idle delivery for that configuration,
not every Codex host or recovery after an app restart.

Codex ordinary async hooks do not start a new turn. Do not repeatedly call
`wait`, use timer prompts, or spawn a new Codex
session to disguise this capability gap. Keep the receipt and recover on the next
user turn if this host does not drain the queue.

Inspect `status --id <delegation_id>` when diagnosing delivery; it includes the
notification state. To complete a saved pending notification, or retry a known
failed delivery after correcting its cause:

```bash
agent-delegate notify --id <delegation_id>
agent-delegate notify --id <delegation_id> --retry
```

Both commands retain the saved destination even if invoked from another shell.
A timeout or a crash during `sending` is `unknown`: the queue may have accepted
the event. Inspect the original queue before an explicit retry. There is no
automatic retry of uncertain delivery and no claim of exactly-once handling
across process failure. Task execution is never resubmitted by notification recovery.

Native references checked 2026-09-09:
[Claude background Bash](https://code.claude.com/docs/en/interactive-mode#background-bash-commands),
[Claude hooks](https://code.claude.com/docs/en/hooks#run-hooks-in-the-background),
[Codex async hooks](https://learn.chatgpt.com/docs/hooks#run-hooks-in-the-background).
The local `codex queue --help` exposes the queue command. The installed zCode
runtime exposes background Bash, completion-notification enqueueing and task IDs;
runtime presence alone is not end-to-end host acceptance.

Observed host checks on 2026-09-09: the independent Codex queue notifier passed
idle wakeup. The earlier five-minute Pi test rejected the code-runner recipe as
an idle wakeup mechanism. Earlier code-runner cross-turn delivery was confounded by another message
opening the receiving turn first. CC 2.1.266 emitted a native `task_notification`
and automatically continued after its background observer completed. The zCode
0.16.5 live check stopped at provider quota error 429 / 1310 before tool execution,
so its delivery remains unverified. The first ten-minute interval heartbeat had
no recorded last run before the owner returned. A second, fixed-time one-run
heartbeat was scheduled for 17:11:04 UTC and independently started the original
thread at 17:11:17, after the callback-handling turn had ended at 17:09:56.
Its saved `last_run_at` was 17:11:17.218, 13.218 seconds after the due time.
It was then paused, with no next run. The callback and fixed-time fallback both
passed separately in this experiment; the original interval blocker remains
undetermined.
These checks separate host delivery from the external worker's business outcome.

For an explicitly requested callback experiment, a scheduled fallback is a separate
delivery channel with its own acceptance. Local Desktop source inspection found
that the notification policy affects completion notifications, not whether a run
starts. Interval heartbeats calculate their next eligible time from recent thread
activity and may defer for a busy thread or an input draft. A fixed-time, one-run
fallback avoids that interval calculation, but still requires the host scheduler
to run. Verify the actual scheduled timestamp, heartbeat `last_run_at`, and
receiving turn. The successful heartbeat above created no `automation_runs` row;
that table alone cannot establish whether a heartbeat ran. Saving the
automation or selecting important updates proves neither. Do not treat an idle
callback as accepted if a fallback or user message opened the receiving turn first.

## Sessions and cancellation

`submit --session <name>` (also `run`) ensures a native ACPX session and sends a
prompt. Reuse the same target, cwd, and name for continuation. Wrapper calls
sharing these values and the receipt root wait their turn before submitting to
ACPX. Queue wait does not consume execution budget. Different sessions, or calls
without `--session`, are independent; the target's own capacity still applies.
The target must implement the ACP capabilities needed for its session lifecycle.

The managed launch command stays stable across runtime upgrades. An existing warm
ACPX owner keeps its original adapter and CLI process; its startup identity stays
in subsequent receipts. The next newly started adapter uses the selected runtime
and current local CLI. Pre-launcher named sessions are discovered through ACPX's
local index and continue under their original command. They remain in the old
runtime until closed; their CLI identity is unverified without a startup record.

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

Native cancellation interrupts a model turn; it does not guarantee that every
spawned terminal command has stopped. In the Codex 0.153.4 / codex-acp 1.10.0 live
check, `stopReason: cancelled` arrived while a previously started terminal sleep
still ran. Both Codex and Claude confirmed turn cancellation; that alone does not
establish terminal cleanup. Inspect native terminal/job state when cleanup matters, and stop the specific job using
the target's controls. The wrapper does not kill an entire warm session or guess
which unrelated/background processes should be terminated.

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
  structured RPC/tool errors, runtime identity, and receipt location.
- `runtime.json`: CLI path/version, adapter path/version, ACPX path/version, and
  adapter launch PID/time. Named sessions reuse the startup record under
  `.session-locks/`; the final receipt gets its own copy. `observation` distinguishes
  `adapter_launch`, `legacy_session_unverified`, and `launch_unobserved`.
- `notification.json`, `notification.log`, `notification.lock`: optional Codex
  completion destination, delivery state, diagnostics, and duplicate-send lock.

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

Doctor reports effective budgets and separates the configured CLI, adapter, and
ACPX identities in `runtimes`. This is the selection for a new process, not proof
that a warm session has upgraded. Read its receipt for the observed startup.
Version drift and unavailable version probes are informational. A missing bound
CLI or adapter is an actionable failure; fix that target or configuration layer.
Do not treat an unrelated target warning as a global startup prohibition.

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

Ordinary `python3 scripts/install_user.py install` preserves an existing runtime,
including its lockfile. It does not run npm against it. The first installation,
or the following explicit upgrade, installs current stable releases:

```bash
python3 scripts/install_user.py install --update-runtime
```

The installer uses `npm install --save-exact --ignore-scripts` in a new directory
under `~/.local/share/agent-delegation/runtimes/`. It records installed versions and
the lock hash before updating the registry and command links. Failed installation
leaves the previous runtime selected. Old directories are retained for active
processes, legacy sessions, and rollback. An explicit runtime upgrade also refreshes
previously managed Codex/Claude adapter targets even if those Skill hosts are not
selected. Custom targets remain unchanged.

The repository's `runtime/package-lock.json` is a reproducible development/test
snapshot, not an installer version policy. To roll back, restore the backed-up
registry and ACPX configuration and repoint the `acpx` command to that registry's
`acpx_path`. Retain any runtime directories still needed by active tasks or sessions.

Register an already installed ACP executable when the owner requests a new target:

```bash
agent-delegate register --name example \
  --argv-json '["/absolute/path/example", "acp"]' \
  --observed-version '1.2.3' --provenance 'official package example@1.2.3'
```

Registration backs up the configuration. Verify the selected target and a small
appropriate round trip before relying on a new adapter for substantial work.
