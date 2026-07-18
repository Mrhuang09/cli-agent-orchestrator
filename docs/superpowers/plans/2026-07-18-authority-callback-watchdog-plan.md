# Authority Callback Watchdog Implementation Plan

> Design: `docs/superpowers/specs/2026-07-18-authority-callback-watchdog-design.md`

## Task 1: Durable callback model and database operations

Files:

- `src/cli_agent_orchestrator/models/inbox.py`
- `src/cli_agent_orchestrator/clients/database.py`
- `test/clients/test_database.py`

Work:

1. Add callback state enum/model and the `authority_callback` SQLAlchemy table.
2. Add transactional operations for request creation, delivery/start/completion transitions, acknowledgement, due-row claiming, and generation cancellation.
3. Use compare-and-set state transitions and deterministic reminder/escalation message identity.
4. Add database tests for fresh schema, state transitions, acknowledgement validation inputs, concurrency/idempotency, and restart persistence.

## Task 2: Authority CLI and runtime protocol

Files:

- `src/cli_agent_orchestrator/cli/commands/authority.py`
- `src/cli_agent_orchestrator/services/authority_runtime.py`
- `src/cli_agent_orchestrator/api/main.py`
- `test/cli/test_authority_cmd.py`
- `test/services/test_authority_runtime.py`
- relevant API tests

Work:

1. Add `--require-callback/--no-require-callback` with authority-task default enabled.
2. Add `--reply-to MESSAGE_ID`; replies never require another callback.
3. Validate current generation and exact reverse sender/receiver roles.
4. Create the request inbox row and callback row atomically through an authority-specific API operation.
5. Create a correlated reply and acknowledge the callback atomically.
6. Return callback metadata in text and JSON CLI output.

## Task 3: Status-driven watchdog and reconciliation

Files:

- new `src/cli_agent_orchestrator/services/authority_callback_watchdog.py`
- server task registration in `src/cli_agent_orchestrator/api/main.py`
- settings/constants files
- new focused service tests

Work:

1. Subscribe to terminal status events and advance delivered requests through `waiting_start -> running -> waiting_reply`.
2. Add a 30-second reconciliation loop using injected clock/sleep in tests.
3. At 180 seconds after reliable completion, claim and enqueue exactly one receiver reminder.
4. At 600 seconds, claim and enqueue exactly one sender escalation.
5. System messages opt out of callback tracking and cannot recurse.
6. Resume unresolved deadlines after restart and cancel rows belonging to replaced authority generations.

## Task 4: Authority status and documentation

Files:

- `src/cli_agent_orchestrator/services/authority_runtime.py`
- `src/cli_agent_orchestrator/cli/commands/authority.py`
- `docs/authority-bridge.md`
- focused CLI/runtime tests

Work:

1. Add awaiting/reminded/escalated counts to role status.
2. Include unresolved request IDs and deadlines in JSON status.
3. Document default callback behavior, opt-out, correlated replies, timers, and the fact that alerts do not infer task success/failure.

## Task 5: Verification and release

1. Run focused database, authority CLI/runtime, API, inbox, and watchdog tests.
2. Run affected provider tests for Codex and Claude status detection.
3. Run full test suite, mypy, ruff, and `git diff --check`.
4. Reinstall the local CAO build.
5. Run a real two-terminal authority smoke test with shortened test-only timers: deliver task, intentionally omit reply, observe one receiver reminder, send correlated reply, verify sender wakeup and unresolved count zero.
6. Re-run with normal defaults and verify configuration reports 180/600/30 seconds.
7. Commit intentionally, push a feature branch to `Mrhuang09/cli-agent-orchestrator`, open a draft PR, review CI, and merge only after green verification.

## Scope controls

- Do not change generic assign/handoff callback semantics.
- Do not parse model output or invoke a model from the watchdog.
- Do not auto-cancel, retry, approve, or decide technical work.
- Preserve existing authority inbox delivery and Codex idle-detection behavior.
- Do not mix OG-Wargame contract work into CAO commits.
