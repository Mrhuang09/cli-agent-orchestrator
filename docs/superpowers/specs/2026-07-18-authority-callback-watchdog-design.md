# CAO Authority Callback Watchdog Design

## Goal

Prevent an authority task from silently ending when the receiving agent finishes a turn but forgets to send its result back. The watchdog must wake the receiver once, then alert the sender once, without polling an LLM or scraping prose from the terminal.

This is an authority-bridge feature. Generic `send_message`, `assign`, and `handoff` behavior stays unchanged.

## User-visible protocol

`cao authority send` gains two related options:

```text
--require-callback / --no-require-callback
--reply-to MESSAGE_ID
```

Authority task messages require a callback by default. Callers can explicitly opt out for one-way notices. A reply uses `--reply-to` and never creates another callback requirement.

Examples:

```bash
cao authority send --from project-director --to technical-director \
  "Review contract X"

cao authority send --from technical-director --to project-director \
  --reply-to 213 \
  "Review complete; commit=..."
```

The CLI prints the request message ID and whether a callback is required. A reply is accepted only when its roles are the exact reverse of the original request and the request belongs to the current authority generation.

## Durable state

Add an `authority_callback` table rather than overloading generic inbox status:

- `request_message_id` — unique foreign reference to the original inbox row;
- `generation_id` — prevents callbacks crossing authority restarts;
- `sender_id`, `receiver_id` — frozen role bindings for the generation;
- `state` — `waiting_delivery`, `waiting_start`, `running`, `waiting_reply`, `reminded`, `escalated`, `acknowledged`, `cancelled`;
- `delivered_at`, `started_at`, `completed_at`, `reminded_at`, `escalated_at`, `acknowledged_at`;
- `reply_message_id` when acknowledged.

Creation of the inbox request and callback row is one database transaction. Existing databases receive the new table through normal `create_all`; no destructive migration is needed.

## State machine

```text
request accepted
  -> waiting_delivery
  -> waiting_start       (original inbox row delivered)
  -> running             (receiver first enters processing)
  -> waiting_reply       (receiver later enters completed/idle)
  -> reminded            (+3 minutes without correlated reply)
  -> escalated           (+10 minutes without correlated reply)
  -> acknowledged        (valid reverse-role --reply-to accepted at any point)
```

Rules:

1. Timers start only after a real `processing -> completed|idle` lifecycle. Long legitimate processing does not generate reminders.
2. A valid correlated reply can acknowledge the request from `waiting_start`, `running`, `waiting_reply`, `reminded`, or `escalated`.
3. The three-minute action enqueues one system reminder to the original receiver. It is a normal durable inbox message, does not require a callback itself, and includes the original message ID plus the exact reply command shape.
4. The ten-minute action enqueues one system alert to the original sender. It states only that the receiver completed without a correlated callback; it does not impersonate the receiver or claim task success/failure.
5. Each transition is compare-and-set. Concurrent status events and periodic reconciliation cannot create duplicate reminders or alerts.
6. A stopped/replaced authority generation cancels unresolved rows for that generation. They never attach to newly resumed terminal IDs.

## Runtime components

### Authority send path

`AuthorityRuntime.send()` and the authority CLI validate flags and call an authority-specific API operation that atomically creates the inbox request and optional callback row. `--reply-to` validates reverse roles and current generation, creates the reply inbox row, then marks the callback acknowledged in the same transaction.

### Callback watchdog

Add `AuthorityCallbackWatchdog` to the CAO server alongside `StatusMonitor` and `InboxService`.

- It subscribes to `terminal.*.status` and advances callback rows for that receiver.
- A cheap 30-second reconciliation loop checks only unresolved rows whose deadlines are due. This provides restart recovery and covers missed/coalesced status events.
- It uses database timestamps and injected clock functions in tests; it does not invoke any model and does not parse terminal response text.
- Reminder and escalation delivery reuse the existing inbox service, status gate, and authority terminal bindings.

### Authority status

`cao authority status` adds compact callback counts per role: `awaiting`, `reminded`, and `escalated`. JSON output includes unresolved request IDs and deadlines; normal text output remains one line per authority role.

## Error handling

- Unknown, already acknowledged, cancelled, wrong-generation, same-direction, or wrong-role `--reply-to` fails closed and does not send a message.
- If reminder enqueue fails, the row remains due and reconciliation retries. Compare-and-set plus a deterministic idempotency key prevents duplicate inbox rows.
- If terminal status is unavailable or ambiguous, the watchdog does not infer completion. It waits for a reliable status event/reconciliation result.
- If the CAO server restarts, unresolved rows and timestamps survive; reconciliation resumes without resetting deadlines.
- A failed task is not inferred from silence. Only “completed without correlated callback” is reported.

## Defaults and configuration

- receiver reminder: 180 seconds after reliable completion;
- sender escalation: 600 seconds after reliable completion;
- reconciliation interval: 30 seconds;
- authority sends require callbacks by default unless `--no-require-callback` or `--reply-to` is used.

The values are named settings with environment overrides, but no per-message timer options are added in the first version.

## Tests

1. CLI parsing/defaults, opt-out, and `--reply-to` validation.
2. Atomic request + callback creation and reverse-role acknowledgment.
3. Full state machine, including reply while running and after reminder.
4. No reminder during long processing.
5. Exactly one receiver reminder at 180 seconds and one sender escalation at 600 seconds.
6. Duplicate/coalesced status events and concurrent reconciliation remain idempotent.
7. Restart recovery preserves deadlines.
8. Generation replacement cancels old callbacks.
9. Reminder/alert messages do not recursively require callbacks.
10. Existing authority delivery, inbox, Codex idle detection, and Claude status tests remain green.
11. Real two-terminal smoke test: task delivered, receiver intentionally completes without reply, reminder wakes receiver, correlated reply wakes sender, pending and unresolved callback counts return to zero.

## Non-goals

- No periodic LLM invocation or model-token-consuming patrol.
- No terminal prose scraping to guess whether a task succeeded.
- No automatic technical decision, task cancellation, retry, or code dispatch.
- No change to collab-bus physical blinding or coder/tester workflows.
- No watchdog for generic CAO orchestration in this increment.
