# Authority Bridge Runtime Reliability Implementation Plan

> Design: `docs/authority-runtime-reliability-design.md`
>
> Scope: CAO infrastructure only; no OG-Wargame product changes

## Phase 1: Current-generation truth source

1. Add a private, atomic `AuthorityRunManifest` with schema validation, lifecycle states and mode `0600`.
2. Add an exclusive lifecycle lock using `fcntl`.
3. Make start capture the two create responses and persist only their exact role bindings.
4. Make status and attach resolve exclusively through the running manifest.
5. Make stop wait for tmux, owned server PID, port and child cleanup before recording `stopped`.

Verification: focused authority runtime and CLI tests, including corrupt state, concurrent lifecycle calls, rollback and historical-row isolation.

## Phase 2: Role-addressed durable communication

1. Add `cao authority send --to ROLE --from ROLE MESSAGE`.
2. Resolve both roles from the current generation; reject stale or ambiguous identities.
3. Create a durable inbox message and report queue acceptance separately from delivery.
4. Add optional bounded delivery waiting; keep task completion dependent on a correlated reply.
5. Update generated authority profiles to prefer the CLI and treat MCP as optional.

Verification: runtime and CLI tests for role resolution, sender validation, queue response, delivery timeout and fail-closed behavior.

## Phase 3: Claude status regression

1. Add a sanitized fixture reproducing the live tool-row/spinner state that was misclassified as completed.
2. Change structural detection so newer live activity wins over an older completion summary.
3. Preserve waiting-user and completed-state precedence where no newer live activity exists.

Verification: focused Claude provider fixtures plus existing provider and status-monitor suites.

## Phase 4: Documentation, installation and physical gate

1. Update CAO authority documentation and OG-Wargame runbook.
2. Run formatting, lint, type checks and the affected test suites.
3. Install the exact tested local commit into the global `cao` executable and verify provenance.
4. With all non-CAO copies of both persisted conversations exited, run two consecutive cold-start bidirectional ACK cycles.
5. Accept CAO only on two clean cycles; otherwise stop it and switch exclusively to `director_mailbox`.

No product work resumes until the physical gate has a reproducible pass or the fallback has been selected and verified.
