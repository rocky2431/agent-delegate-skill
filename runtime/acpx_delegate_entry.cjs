#!/usr/bin/env node
'use strict';

// Thin public programming consumer of the pinned ACPX runtime for callers
// that must own the permission decision (for example Ultra Builder Pro's
// delegated execution boundary). One request line goes in; runtime events,
// raw permission requests, and one typed result line come out. The entry
// owns no policy of its own:
//
// - The transport permission mode is hard-required to be `deny-all`, so a
//   callback that throws, times out, or returns nothing fails closed instead
//   of widening authority (the runtime falls back to the mode when the
//   callback does not decide).
// - Permission requests are forwarded with the raw RequestPermissionRequest
//   only (session id, toolCall with kind/locations, options). The caller's
//   decision names an optionId from that request and the entry maps it to
//   that option's kind; the pinned runtime then selects the first option of
//   that kind, the same first-matching-kind choice the current UBP driver
//   makes. The preserved semantics are therefore kind-level, not arbitrary
//   exact optionId: a caller cannot select a later duplicate of one kind.
//   Unknown, missing, or malformed decisions fail closed to `reject_once`,
//   and the entry never upgrades `allow_once` to `allow_always`.
// - Sessions live in process memory only: no transcript, session record, or
//   environment value is persisted anywhere, and no sessionOptions.env is
//   passed to the runtime.
// - Protocol negotiation is observed, never assumed. The pinned runtime
//   persists the initialization result's protocolVersion on the public
//   session record and flushes that record through the session store while
//   ensureSession is still awaited, so the store is the one public place the
//   actually negotiated version becomes observable. Unless stable protocol
//   v1 was actually observed, the entry refuses the turn before startTurn:
//   the pinned manager creates the agent session before that record exists,
//   so an unproven or unsupported session may already exist, but no prompt,
//   tool call, or model turn is ever submitted on it. The result line
//   reports the observed version and omits it when no negotiation was
//   observed (for example a session that failed to initialize).
// - Cancellation is a request: SIGTERM/SIGINT latches a sticky abort before
//   any prompt is submitted (a cancel that lands while the session
//   initializes wins over the later prompt) and waits a bounded grace for
//   the terminal result, but the caller remains responsible for proving the
//   complete writer boundary stopped.
//
// Protocol (newline-delimited JSON on stdin/stdout, diagnostics on stderr):
//   -> {"schema":"acpx-delegate-turn-request/v1","agent_argv":[...],"cwd":"/abs",
//       "prompt":"...","timeout_ms":60000}
//   <- {"type":"started","pid":123,"node":"v26.8.1","acpx_version":"0.13.2"}
//   <- {"type":"event","event":{...AcpRuntimeEvent...}}
//   <- {"type":"permission_request","id":1,"request":{"sessionId":"...","raw":{...}}}
//   -> {"type":"permission_decision","id":1,"optionId":"allow-once"}
//   <- {"type":"result","result":{"status":"completed"|"cancelled"|"failed",...}
//       [,"protocolVersion":1]}
//
// The result line's optional protocolVersion sibling is the actually
// negotiated version observed through the session store, never a constant:
// it is present exactly when the negotiation was observed, and absent when
// it was not (for example an initialization failure before any record was
// persisted).
//
// A failed result's error carries an optional structured "phase":
// "ensure_session" only when the turn was refused before runtime.startTurn
// was ever invoked — an ensureSession failure or this entry's unsupported or
// unproven protocol-version refusal — (no prompt was submitted), and "turn"
// once the startTurn boundary was reached (tools may already have run, so a
// stopped writer alone does not make replaying the task safe). The phase is
// never inferred from error text, and an absent or unknown phase is not
// before-prompt proof.
//
// Exit codes: 0 for completed/cancelled turns, 1 for failed turns, 2 for
// protocol misuse before a turn starts.

const crypto = require('node:crypto');
const path = require('node:path');
const { StringDecoder } = require('node:string_decoder');

const REQUEST_SCHEMA = 'acpx-delegate-turn-request/v1';
const ALLOWED_PERMISSION_MODES = new Set(['deny-all']);
// The single stable protocol version this delegate transport speaks; the
// pinned runtime already sends it as the initialize request version. The
// refusal below compares the negotiated response against exactly this.
const REQUIRED_PROTOCOL_VERSION = 1;
const CANCEL_GRACE_MS = 10000;
const TEARDOWN_TIMEOUT_MS = 10000;

function emit(value) {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

function diagnose(message) {
  process.stderr.write(`acpx-delegate-entry: ${message}\n`);
}

function fail(message) {
  diagnose(message);
  process.exit(2);
}

function validateRequest(request) {
  if (!request || typeof request !== 'object' || Array.isArray(request)) {
    throw new Error('request must be a JSON object');
  }
  if (request.schema !== REQUEST_SCHEMA) {
    throw new Error(`request schema must be ${REQUEST_SCHEMA}`);
  }
  const argv = request.agent_argv;
  if (!Array.isArray(argv) || argv.length === 0
      || !argv.every((item) => typeof item === 'string' && item)) {
    throw new Error('agent_argv must be a non-empty string array');
  }
  if (!path.isAbsolute(argv[0])) {
    throw new Error('agent_argv[0] must be an absolute executable path');
  }
  if (typeof request.cwd !== 'string' || !path.isAbsolute(request.cwd)) {
    throw new Error('cwd must be an absolute directory');
  }
  if (typeof request.prompt !== 'string' || !request.prompt) {
    throw new Error('prompt must be a non-empty string');
  }
  const timeoutMs = request.timeout_ms;
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 24 * 60 * 60 * 1000) {
    throw new Error('timeout_ms must be a positive integer of milliseconds');
  }
  const permissionMode = request.permission_mode === undefined ? 'deny-all' : request.permission_mode;
  if (typeof permissionMode !== 'string' || !ALLOWED_PERMISSION_MODES.has(permissionMode)) {
    throw new Error('permission_mode must be deny-all for the programming consumer');
  }
  return { ...request, permissionMode };
}

// The runtime persists whatever a session store saves and re-loads records
// by their acpxRecordId. An in-memory store keyed on every record identity
// keeps every record — including the environment merge the runtime threads
// into session records — inside this one bounded process. Every saved record
// is also offered to `onSavedRecord`: the public AcpSessionRecord carries
// the negotiated protocolVersion populated from the initialization result,
// which makes the store the one public observation point for the version
// this entry must verify before any turn starts.
function memorySessionStore(onSavedRecord) {
  const records = new Map();
  return {
    async load(sessionId) {
      return records.get(sessionId);
    },
    async save(record) {
      if (!record || typeof record !== 'object') return;
      if (onSavedRecord) onSavedRecord(record);
      for (const key of ['acpxRecordId', 'sessionId', 'acpSessionId', 'id']) {
        if (typeof record[key] === 'string' && record[key]) records.set(record[key], record);
      }
    },
  };
}

// One persistent UTF-8 line reader for stdin's whole lifetime: the initial
// request line and every later permission decision share it, so a multibyte
// character or a JSON line split across chunks is reassembled exactly instead
// of decaying into replacement characters or dropped non-newline tails.
function createStdinLineReader(onLine, onEnd) {
  const decoder = new StringDecoder('utf8');
  let buffered = '';
  const takeLines = (text) => {
    buffered += text;
    let newline = buffered.indexOf('\n');
    while (newline >= 0) {
      const line = buffered.slice(0, newline);
      buffered = buffered.slice(newline + 1);
      onLine(line);
      newline = buffered.indexOf('\n');
    }
  };
  process.stdin.on('data', (chunk) => takeLines(decoder.write(chunk)));
  process.stdin.on('end', () => {
    takeLines(decoder.end());
    onEnd();
  });
}

function errorTerminal(error, phase) {
  return {
    status: 'failed',
    error: {
      message: error && typeof error.message === 'string' ? error.message : String(error),
      code: error && typeof error.code === 'string' ? error.code : undefined,
      retryable: undefined,
      ...(phase ? { phase } : {}),
    },
  };
}

async function main() {
  const acpx = require('acpx/runtime');
  if (typeof acpx.createAcpRuntime !== 'function') {
    throw new Error('pinned acpx runtime does not export createAcpRuntime');
  }
  const acpxVersion = require('acpx/package.json').version;

  // Raw permission requests wait for the caller's optionId decision. Every
  // waiter must settle: the runtime awaits this callback directly and the
  // deny-all fallback only applies once the callback returns, so a waiter
  // left pending by a fragmented line, stdin EOF, or an aborted request
  // would hang the turn instead of failing closed.
  const decisionWaiters = new Map();
  let permissionSeq = 0;
  let inputEnded = false;
  const settleAllWaiters = () => {
    const waiters = [...decisionWaiters.values()];
    decisionWaiters.clear();
    for (const settle of waiters) settle(undefined);
  };

  const requestLine = await new Promise((resolve, reject) => {
    let sawRequest = false;
    createStdinLineReader(
      (line) => {
        if (!sawRequest) {
          sawRequest = true;
          resolve(line);
          return;
        }
        let message;
        try {
          message = JSON.parse(line);
        } catch {
          return;
        }
        if (!message || typeof message !== 'object') return;
        if (message.type === 'permission_decision' && Number.isInteger(message.id)) {
          const settle = decisionWaiters.get(message.id);
          if (settle) settle({ optionId: message.optionId });
        }
      },
      () => {
        inputEnded = true;
        if (!sawRequest) {
          reject(new Error('stdin closed before a request line arrived'));
          return;
        }
        settleAllWaiters();
      },
    );
  });
  let request;
  try {
    request = validateRequest(JSON.parse(requestLine));
  } catch (error) {
    fail(`invalid request: ${error.message}`);
    return;
  }

  const permissionCallback = (req, ctx) => {
    if (!req || typeof req !== 'object' || !req.raw || typeof req.raw !== 'object') {
      return Promise.resolve({ outcome: 'reject_once' });
    }
    permissionSeq += 1;
    const id = permissionSeq;
    emit({
      type: 'permission_request',
      id,
      request: {
        sessionId: typeof req.sessionId === 'string' ? req.sessionId : null,
        raw: req.raw,
      },
    });
    const signal = ctx && typeof ctx === 'object' ? ctx.signal : undefined;
    return new Promise((resolve) => {
      let settled = false;
      const settle = (decision) => {
        if (settled) return;
        settled = true;
        decisionWaiters.delete(id);
        if (signal) signal.removeEventListener('abort', onAbort);
        if (!decision) {
          // The caller went away (stdin EOF) or the runtime aborted the
          // request: return nothing so the pinned deny-all mode fails closed.
          resolve(undefined);
          return;
        }
        const raw = req.raw || {};
        const options = Array.isArray(raw.options) ? raw.options : [];
        const option = typeof decision?.optionId === 'string'
          ? options.find((item) => item && typeof item === 'object' && item.optionId === decision.optionId)
          : null;
        if (!option) {
          // Unknown, missing, or malformed decisions reject once: fail closed.
          resolve({ outcome: 'reject_once' });
          return;
        }
        const kind = typeof option.kind === 'string' ? option.kind : '';
        const outcome = kind === 'allow_once' || kind === 'allow_always'
          || kind === 'reject_once' || kind === 'reject_always' ? kind : 'reject_once';
        resolve({ outcome });
      };
      const onAbort = () => settle(undefined);
      if (signal) {
        if (signal.aborted) {
          resolve(undefined);
          return;
        }
        signal.addEventListener('abort', onAbort, { once: true });
      }
      if (inputEnded) {
        resolve(undefined);
        return;
      }
      decisionWaiters.set(id, settle);
    });
  };

  // The one public negotiation observation: the pinned runtime fills the
  // session record's protocolVersion from the initialization result and
  // flushes that first record save while ensureSession is still awaited.
  // Values that are not numbers violate the public record contract and stay
  // unobserved, which the pre-turn refusal below treats as unproven.
  let negotiatedProtocolVersion;
  const runtime = acpx.createAcpRuntime({
    cwd: request.cwd,
    sessionStore: memorySessionStore((record) => {
      if (typeof record.protocolVersion === 'number') {
        negotiatedProtocolVersion = record.protocolVersion;
      }
    }),
    agentRegistry: {
      resolve: () => request.agent_argv,
      list: () => ['delegate'],
    },
    permissionMode: request.permissionMode,
    nonInteractivePermissions: 'fail',
    timeoutMs: request.timeout_ms,
    onPermissionRequest: permissionCallback,
  });

  let handle = null;
  let turn = null;
  let terminal = null;
  // Sticky cancellation: latched before the session starts initializing so a
  // cancel that lands during ensureSession still wins over the later prompt
  // submission. The pinned runtime accepts this signal on startTurn and
  // settles a cancelled turn without submitting when it arrives aborted;
  // ensureSession itself takes no abort input, so interrupting the
  // initialization wait stays the caller's process-boundary responsibility.
  const turnAbort = new AbortController();
  let cancelRequested = null;
  const cancelSignal = new Promise((resolve) => {
    cancelRequested = resolve;
  });
  const settleCancel = () => {
    turnAbort.abort();
    if (cancelRequested) {
      cancelRequested();
      cancelRequested = null;
    }
  };
  process.on('SIGTERM', settleCancel);
  process.on('SIGINT', settleCancel);

  emit({ type: 'started', pid: process.pid, node: process.version, acpx_version: acpxVersion });

  let eventPump = null;
  // The failure-phase boundary: set exactly when runtime.startTurn is
  // invoked, never inferred from error text. Before it, a failure means no
  // prompt was submitted; at or past it, tools may already have run.
  let startTurnReached = false;
  try {
    handle = await runtime.ensureSession({
      sessionKey: `delegate-entry-${crypto.randomUUID()}`,
      agent: 'delegate',
      mode: 'oneshot',
      cwd: request.cwd,
    });
    if (negotiatedProtocolVersion !== REQUIRED_PROTOCOL_VERSION) {
      // Refuse before startTurn: the pinned manager creates the agent
      // session before the record this observation rides on is persisted,
      // so a session may already exist, but no prompt, tool call, or model
      // turn is ever submitted on an unsupported or unproven negotiation.
      terminal = {
        status: 'failed',
        error: {
          message: typeof negotiatedProtocolVersion === 'number'
            ? `Agent negotiated ACP protocolVersion ${negotiatedProtocolVersion}; this delegate transport requires ${REQUIRED_PROTOCOL_VERSION}.`
            : `No ACP protocolVersion was observed on the initialized session record; this delegate transport requires ${REQUIRED_PROTOCOL_VERSION}.`,
          code: 'unsupported_protocol_version',
          phase: 'ensure_session',
        },
      };
    } else if (turnAbort.signal.aborted) {
      // Cancelled while the session initialized: no turn starts and no prompt
      // is submitted. This mirrors the runtime's own pre-submission result.
      terminal = { status: 'cancelled', stopReason: 'cancelled' };
    } else {
      startTurnReached = true;
      turn = runtime.startTurn({
        handle,
        text: request.prompt,
        mode: 'prompt',
        requestId: crypto.randomUUID(),
        timeoutMs: request.timeout_ms,
        signal: turnAbort.signal,
      });
      try {
        await turn.promptStarted;
      } catch (error) {
        if (!turnAbort.signal.aborted) throw error;
        // Cancelled before the prompt went out: promptStarted rejects, and
        // the turn's canonical result below still carries the cancelled
        // terminal state — the rejection is not a turn failure.
      }
      eventPump = (async () => {
        try {
          for await (const event of turn.events) {
            emit({ type: 'event', event });
          }
        } catch {
          // Event-stream failures surface through the turn result; the result
          // line stays the single terminal truth.
        }
      })();
      // After a cancel signal the turn result gets a bounded grace window; a
      // result that never arrives is reported as cancelled-without-result, and
      // the adapter teardown below still runs. This line is transport truth
      // only — the caller keeps the writer-boundary stop proof.
      const grace = cancelSignal.then(() => new Promise(
        (resolve) => setTimeout(resolve, CANCEL_GRACE_MS, null),
      ));
      terminal = await Promise.race([turn.result, grace]);
      if (terminal === null) {
        terminal = { status: 'cancelled', stopReason: 'cancelled', result_timeout: true };
      } else if (terminal.status === 'failed') {
        // Turn-phase failures reach the entry through the resolved turn
        // result, not the catch below; stamp the boundary-derived phase.
        terminal = { ...terminal, error: { ...terminal.error, phase: 'turn' } };
      }
    }
  } catch (error) {
    terminal = errorTerminal(error, startTurnReached ? 'turn' : 'ensure_session');
  }

  // Drain the event stream ahead of the result line: the terminal line stays
  // last, and the runtime's queue still yields buffered events after it
  // closes. A turn whose stream cannot drain inside the teardown bound is
  // reported on stderr as possibly-incomplete output, never silently cut.
  if (eventPump) {
    const drained = await Promise.race([
      eventPump.then(() => true, () => true),
      new Promise((resolve) => setTimeout(resolve, TEARDOWN_TIMEOUT_MS, false)),
    ]);
    if (!drained) {
      diagnose('event stream did not drain within the teardown bound; emitted events may be incomplete');
    }
  }
  if (handle) {
    // Process-local oneshot sessions keep no persistent state to discard, and
    // the discarding close path would respawn the agent only to attempt
    // session/close — this close just releases this process's session owner.
    const closed = await Promise.race([
      runtime.close({ handle, reason: 'delegate-entry turn complete' }).then(
        () => true,
        (error) => {
          diagnose(`runtime close failed: ${error instanceof Error ? error.message : String(error)}`);
          return true;
        },
      ),
      new Promise((resolve) => setTimeout(resolve, TEARDOWN_TIMEOUT_MS, false)),
    ]);
    if (!closed) {
      diagnose('runtime close exceeded the teardown bound; descendant stop proof stays with the caller');
    }
  }
  if (turn) {
    try {
      await turn.closeStream({ reason: 'delegate-entry turn complete' });
    } catch {}
  }
  // The result line is the terminal truth and must reach the caller whole:
  // flush every queued stdout byte before leaving instead of letting
  // process.exit discard the tail of a large event backlog. The optional
  // protocolVersion sibling reports exactly the negotiated version the
  // session store observed — never a constant, and absent when no
  // negotiation was observed.
  await new Promise((resolve, reject) => {
    process.stdout.write(`${JSON.stringify({
      type: 'result',
      result: terminal,
      ...(typeof negotiatedProtocolVersion === 'number'
        ? { protocolVersion: negotiatedProtocolVersion }
        : {}),
    })}\n`, (error) => {
      if (error) reject(error); else resolve();
    });
  });
  process.exit(terminal.status === 'failed' ? 1 : 0);
}

main().catch((error) => {
  process.stderr.write(`acpx-delegate-entry: ${(error && error.stack) || error}\n`);
  process.exitCode = 1;
});
