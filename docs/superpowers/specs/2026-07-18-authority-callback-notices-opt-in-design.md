# Authority callback notices become opt-in

## Problem

Authority callbacks currently enqueue a reminder into the receiver's terminal three minutes after CAO observes a completed or idle transition without a correlated reply. Provider status can transiently report `completed` while the model is still working, so the reminder can be inserted into an active turn and distract the receiver. Reliable inbox delivery itself is working and must not be changed.

## Decision

Keep durable callback tracking and correlated `--reply-to` support, but make automatic reminder and escalation notices opt-in. The default installation must record callback state without injecting watchdog messages into either authority terminal.

Add one boolean environment setting:

```text
CAO_AUTHORITY_CALLBACK_NOTICES_ENABLED=false
```

- `false` or unset: record lifecycle and callback state, but do not enqueue reminder or escalation messages.
- `true`: retain the existing three-minute reminder and ten-minute escalation behavior.
- Invalid boolean values fail fast during configuration loading rather than silently enabling notices.

The existing timeout settings remain meaningful only when notices are enabled.

## Boundaries

- Do not change inbox delivery, idle wake-up, terminal status detection, `--require-callback`, `--reply-to`, callback persistence, or `cao authority status` counts.
- Do not delete unresolved callbacks when notices are disabled; a later correlated reply still closes them normally.
- Do not interrupt or restart active authority terminals merely to apply the source change. Installation and restart happen only after the current Claude task finishes.
- Document how to opt in for deployments that want watchdog notices.

## Implementation shape

The watchdog continues consuming terminal status events. Its reconciliation loop passes the new setting to the callback notice enqueue operation, or skips notice enqueueing when disabled. The database state machine remains unchanged so enabling the setting later preserves existing callback semantics.

## Verification

1. Default configuration: a completed unresolved callback produces no reminder or escalation inbox rows, while status still reports it unresolved.
2. Explicit opt-in: the existing exactly-once reminder and escalation tests continue to pass.
3. A correlated reply closes the callback in both modes.
4. Invalid environment values fail configuration loading.
5. Existing inbox delivery and authority runtime tests remain green.

## Rollout

Commit the implementation and tests in the repository, reinstall the local CAO tool, and restart the authority bridge only after the active technical-director task has returned. Verify the new process has notices disabled and that a normal project-director-to-technical-director message still delivers without an injected reminder.
