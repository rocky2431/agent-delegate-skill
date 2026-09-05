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

## Sessions and receipts

`run --session <name>` ensures a native ACPX session and sends a prompt. Reuse the
same target argv, cwd, and name for continuation. Wrapper calls sharing these values
and the receipt root wait their turn before submitting to ACPX. The wait consumes
the call's time budget; cancelling a waiting wrapper affects only that call.
The OS releases its session lock when the wrapper exits. Direct native ACPX calls
do not participate in this lock; keep them sequential with wrapper turns.
`cancel` requests cancellation of its active turn; `close` closes the
native session when it is no longer needed. One-shot `run` remains independent.
The target must implement the ACP capabilities needed for its session lifecycle.

Each operation creates a private directory under
`~/.local/state/agent-delegation/runs/`:

- `request.json`: operation, requested model/session, argv, limits, and task hash;
  it does not contain the original task prompt.
- `events.ndjson` and `stderr.log`: streamed bytes, available while work runs.
  ACPX suppresses read-file bodies in its event output, not in the worker's tools.
- `result.json`: terminal state, text, original content blocks, session ids,
  structured RPC/tool errors, and receipt location.

Terminal states distinguish normal completion (`success`), `cancelled`, `timeout`,
`denied`, `not_found`, `refused`, `incomplete`, and `error`. The underlying exit
code is retained; wrapper cancellation and timeout exit with 130 and 124.
A cancellation request succeeding does not mean the active task has already
finished; read that task's final receipt.
When the wrapper is interrupted, it also cancels a named session's background
turn. `cancellation_exit_code` records that control request; an unconfirmed
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
