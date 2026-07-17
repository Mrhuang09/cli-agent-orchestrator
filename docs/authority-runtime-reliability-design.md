# Authority Bridge Runtime Reliability Design

> Status: proposed for implementation after owner review
>
> Date: 2026-07-15
>
> Scope: persistent Codex project-director and Claude technical-director bridge

## 1. Problem statement

The authority bridge can physically move messages in both directions, but the current runtime is not safe enough to be the primary OG-Wargame coordination channel.

The 2026-07-15 physical diagnostic reproduced five failures:

1. the private project configuration contained two valid authority profiles, but the globally installed CAO build lagged two already-tested local fixes;
2. an interrupted startup left the server and tmux session alive after `stop` had reported success;
3. restarting against the same SQLite state returned historical terminal rows, so `status` reported eight terminals while tmux contained only two current windows;
4. the session conductor was resolved as the oldest historical terminal instead of the current project-director terminal;
5. Claude was visibly processing a command while the status API reported `completed`, allowing a message to be delivered before the turn was actually idle.

Claude's resumed background conversation also did not expose the injected `cao-mcp-server` tools. The return message succeeded only through `cao session send --terminal ...`. This proves that MCP availability cannot be a correctness dependency.

## 2. Decision

Keep CAO as the primary bridge, but make the **role-addressed CLI** the reliable communication contract. MCP remains an optional convenience layer.

The bridge is not accepted for production use until it passes two consecutive cold-start cycles:

```text
start -> exactly two current authority roles -> bidirectional ACK
      -> pending=0 -> stop with zero residue
      -> restart same persisted conversations -> bidirectional ACK
      -> pending=0 -> stop with zero residue
```

If the repaired implementation cannot pass both cycles, CAO is removed from the primary path and OG-Wargame switches to the mutually exclusive `director_mailbox` fallback. The two executors must never run together.

## 3. Considered approaches

### 3.1 Repair CAO with a project-local run manifest — selected

Use the existing CAO server, tmux backend, provider adapters and inbox, but add an authority-specific runtime manifest, role addressing and transactional lifecycle checks.

Advantages:

- preserves the already-proven session resume and visible terminal workflow;
- avoids changing the general CAO terminal database schema for one specialized workflow;
- isolates authority correctness from historical generic CAO rows;
- keeps the implementation small enough to test exhaustively.

### 3.2 Replace CAO immediately with `director_mailbox`

This provides a simpler durable queue, but loses CAO's visible dual-terminal operation and native conversation resume. It remains the failover if the selected repair cannot pass the acceptance gate.

### 3.3 Run CAO and mailbox simultaneously

Rejected. Two consumers can deliver the same instruction twice, produce conflicting acknowledgements and create two apparent truth sources for message state.

## 4. Runtime authority manifest

Each project gets one private manifest:

```text
<project>/.ai-collab-runtime/cao-authority/state/authority-run.json
```

It is mode `0600`, ignored by Git, and contains only runtime metadata:

- `schema_version`;
- `generation_id` (new UUID for every startup attempt; it becomes an accepted generation only after the manifest reaches `running`);
- `project_root` and deterministic tmux `session_name`;
- lifecycle state: `starting | running | stopping | stopped | failed`;
- owned server PID;
- exact current `project-director` terminal ID and tmux window;
- exact current `technical-director` terminal ID and tmux window;
- creation/update timestamps.

The manifest is the authority bridge's current-run truth source. Generic CAO's terminal table remains historical infrastructure metadata, not the authority role resolver.

All lifecycle operations hold an exclusive `fcntl` lock on a sibling lock file. A second `start`, `stop` or role mutation fails rather than racing the first operation.

## 5. Transactional lifecycle

### 5.1 Start

`cao authority start` executes these steps under the lifecycle lock:

1. validate private config, filesystem, profile permissions, alternate executor absence and known CLI process occupancy;
2. reconcile a prior manifest:
   - a fully verified `running` generation is reused;
   - `starting`, `stopping`, `failed` or unverifiable state is cleaned before proceeding;
3. start the owned CAO server and write `starting` with its verified PID;
4. if the database contains rows for the deterministic session but tmux has no matching live session, delete those stale rows through the normal session deletion path and verify they are gone;
5. create project-director and technical-director, capturing their IDs directly from the two create responses rather than re-listing all historical rows;
6. verify exactly one live tmux window for each expected role, correct provider, project cwd and profile;
7. atomically replace the manifest with `running`;
8. only then print success or attach the UI.

Any failure after step 3 deletes the partially created session, terminates only the proven-owned server, verifies port/tmux/process cleanup, records `failed`, and exits non-zero. A startup interrupted by the caller is recovered by the next lifecycle command from the durable `starting` record.

### 5.2 Status

`cao authority status` reads only the two terminal IDs in the current `running` manifest and cross-checks each against:

- terminal API metadata;
- tmux session/window existence;
- role/profile/provider;
- live status;
- pending inbox count.

Historical rows are never displayed as current authority terminals. Missing, duplicated or mismatched roles make status unhealthy and non-zero; they are not silently guessed.

### 5.3 Stop

`cao authority stop`:

1. acquires the lifecycle lock and writes `stopping`;
2. deletes the exact current session through CAO;
3. waits for both tmux windows and the tmux session to disappear;
4. terminates the proven-owned server and waits for the PID and port to disappear;
5. escalates only that owned PID to `SIGKILL` after a bounded timeout;
6. atomically records `stopped` and clears its owned PID and role terminal references only after all residue checks pass.

It must never print `stopped` while a server listener, owned tmux session or authority child process remains.

## 6. Role-addressed durable messaging

Add a stable command:

```text
cao authority send --to technical-director MESSAGE
cao authority send --to project-director MESSAGE
```

Options include `--project-root`, optional explicit `--from`, `--wait-delivered` and a bounded `--timeout`.

Rules:

1. `--to` resolves only through the current `running` manifest; no terminal ID is accepted as a substitute for a missing role.
2. Sender identity is taken from a current-generation `CAO_TERMINAL_ID` when available. Outside an authority terminal, `--from` is required and must name one of the two roles.
3. The command creates a durable CAO inbox message; it does not paste directly into a terminal.
4. Queue acceptance returns the message ID and `pending`; it is not reported as completion.
5. `--wait-delivered` waits only for inbox delivery. Task completion still requires the receiving role to send an explicit correlated ACK.
6. Unknown status, generation mismatch, missing role, stale terminal or server mismatch fails closed.
7. The authority server runs with eager mid-turn delivery disabled. A pending message is delivered only when the receiver is reliably idle/completed.

MCP `send_message` may continue to exist. Authority profiles must instruct agents to prefer the role-addressed CLI. A resumed Claude background session without MCP therefore remains fully functional and does not need global Claude MCP configuration.

## 7. Exclusive project-director surface

The persisted Codex project-director UUID must not be open in the desktop/IDE surface and CAO CLI at the same time. The 2026-07-15 diagnostic showed the CAO Codex window mirroring the active project-director conversation, followed by an interruption, which is evidence of concurrent resume risk.

Production operation therefore has one explicit cutover rule:

- while CAO authority mode is active, the CEO uses the attached CAO project-director terminal as the project-director work surface;
- the same persisted Codex conversation must be fully exited from every other CLI, desktop, IDE or app-server surface before startup; merely leaving another surface idle does not satisfy this rule;
- diagnostics launched from an active copy of that conversation may validate transport, but cannot count as production acceptance.

Process-based UUID checks remain useful but cannot prove that an IDE/app-server surface is detached. The runbook must state this limitation rather than claiming perfect automatic detection.

## 8. Status correctness

The Claude status regression must be fixed with a captured, sanitized fixture from the reproduced screen shape:

```text
Running 1 shell command...
<live gerund spinner below the latest prompt/rail>
```

Required behavior:

- a live processing spinner or active tool row newer than the last idle prompt is `processing`;
- a past-tense summary is not completion when a newer live tool/spinner exists;
- `unknown` never upgrades to ready for delivery;
- only a stable idle prompt or completed turn with no newer activity permits inbox delivery.

The fix must preserve existing Claude status fixtures and must not enable global eager delivery.

## 9. Verification

### 9.1 Automated

Tests must cover:

- manifest creation, permissions, atomic replace and corrupt-state rejection;
- lifecycle lock and concurrent start/stop rejection;
- stale DB rows with absent tmux session;
- partial first/second terminal creation rollback;
- exact two-role status filtering despite historical rows;
- conductor/role resolution always selects the current generation;
- role-addressed inbox creation and sender validation;
- pending is not completion; delivery and correlated ACK are separate facts;
- stop waits for port, tmux and owned children;
- interrupted start recovery;
- reproduced Claude active-tool status fixture;
- existing authority, provider, inbox, profile and status-monitor regressions.

### 9.2 Physical acceptance

Run two consecutive cold cycles using the real OG-Wargame private profiles after the active non-CAO copies of both authority conversations are exited:

1. start without attach and verify exactly two current roles;
2. attach and visually verify both terminals restored the intended conversations and cwd;
3. project-director sends a unique marker to technical-director through role CLI;
4. technical-director sends a correlated unique ACK through role CLI;
5. repeat in the opposite direction;
6. verify both pending counts are zero;
7. stop and independently verify no listener, tmux session or authority child remains;
8. restart and repeat steps 1-7 with new markers and new terminal IDs.

The bridge is accepted only if both cycles pass without manual terminal-ID lookup, duplicate delivery, status override or cleanup intervention.

## 10. Documentation and rollout

After implementation and acceptance:

- update `docs/authority-bridge.md` in this repository;
- update OG-Wargame's `CAO-AUTHORITY-BRIDGE-RUNBOOK.md` with role commands, exclusive-surface cutover and verified limitations;
- reinstall the exact tested local commit into the global `cao` tool;
- record the installed commit in a machine-readable `cao authority doctor` output or equivalent provenance check;
- keep CAO stopped until the CEO intentionally enters authority mode.

No OG-Wargame product source, contract or execution tracker is changed by this infrastructure repair.
