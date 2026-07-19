# Postmortem: authority bridge fails to start after environment restart

**Issue:** (unfiled — local record; see "Reporting upstream" below)
**Status:** Immediate band-aid landed; structural fixes NOT yet done
**Date:** 2026-07-19
**Component:** `cao authority start` → `providers/claude_code.py`, `services/authority_runtime.py`
**Environment:** WSL2 (Ubuntu on ext4), claude CLI v2.1.215, cao 2.3.0

---

## Why this record exists

`cao authority start` has failed to come back up on **two separate occasions** after
an environment restart, each time costing real debugging time because the surfaced
error points at the wrong thing. This document captures the *actual* root causes and
the structural fixes needed so we stop fixing the same class of failure once per use.

## Symptom (what the operator sees)

```
$ cao authority start
Error: authority start failed and cleanup was incomplete: owned CAO server PID <pid> did not stop
```

This message is **misleading**. The server not stopping is a *secondary* effect of a
failed cleanup path; the *primary* failure happened earlier and is invisible here.

## What actually happens

`authority start` creates the project-director (codex) terminal — `201 Created` — and
then the technical-director (claude_code) terminal, which returns **`500 Internal
Server Error`**. The 500's real cause is in the per-run log
(`state/logs/cao_*.log`): `Failed to create terminal: Claude Code initialization
timed out after 60s`. Because terminal creation raised, `start()` runs its cleanup
block (`_delete_current_session_if_present` → `_kill_authority_tmux_if_present` →
`_stop_owned_server(wait=True)`). `_stop_owned_server` waits ~12s for the uvicorn
server to exit; uvicorn's graceful shutdown can take longer, so the wait times out and
raises `owned CAO server PID <pid> did not stop`, which `start()` re-wraps as
`authority start failed and cleanup was incomplete: …`. The server *does* exit a moment
later ("Finished server process" appears in `server.stdout.log`), confirming the
cleanup error is a red herring.

So the surfaced error is two layers removed from the real problem: **the claude_code
technical-director terminal never reaches idle within 60s.**

## Root causes

There are **two independent** reasons the TD terminal fails to initialize. Either one
alone is enough to break `authority start`.

### Cause A — new large-session "Resume from summary" prompt is not auto-answered

`_build_claude_command()` launches `claude --resume <claude_session_id> …`. When the
persisted session is large, claude 2.1.x shows an interactive menu *before* the REPL:

```
This session is Xh Ym old and Nk tokens.
Resuming the full session will consume a substantial portion of your usage limits.
❯ 1. Resume from summary (recommended)   2. Resume full session   3. Don't ask me again
```

The **direct-resume path**'s `_handle_startup_prompts()` only knows how to answer the
bypass-permissions and workspace-trust dialogs. It does **not** answer this menu, so
claude sits at the prompt, never reaches `{IDLE, COMPLETED}`, and the 60s init timer
fires. (The other launch path, `_attach_background_agent()`, *did* already handle this
menu at its header-verification step — the two paths had drifted out of sync.)

**Band-aid applied** (commit on this branch): added a branch to
`_handle_startup_prompts()` that detects `"Resume from summary (recommended)"` and
sends `Enter` (accepts the pre-selected, non-forking option), guarded by a
one-shot flag so the lingering buffer text can't re-trigger it. This mirrors the
existing `_attach_background_agent()` handling.

### Cause B (structural, NOT fixed) — the bridge tries to attach a session that is still live

`claude_session_id` binds the bridge to one specific persisted session. If that exact
session is **currently open elsewhere** — e.g. the operator opened it directly (or a
background-job worker is executing it) — `claude agents --json` reports it as `busy`,
and neither launch path can turn a live/busy session into a fresh idle terminal, so
init times out (a different route to the same 500).

The `_assert_startable()` guard is *supposed* to catch exactly this
("technical director session is already open in PID(s) …"), but it detects occupancy
by scanning `/proc/<pid>/cmdline` for the **literal session UUID**. A claude
background-job worker runs under a *different* worker/session id in its cmdline, so the
guard's UUID scan misses it. The guard has a blind spot, `start` proceeds, and instead
of failing fast with a clear message it fails deep with a 60s timeout + the misleading
cleanup error.

## Structural fixes to invest in (stop fixing-per-use)

1. **Table-driven startup-prompt handling with an explicit "unhandled menu" fallback.**
   Enumerate every known claude startup prompt (bypass, trust, resume-summary, future
   ones) in one place shared by *both* launch paths. If an interactive menu is detected
   that no rule matches, surface it verbatim ("claude is blocked on an unrecognized
   prompt: …") instead of blindly waiting out a 60s timeout. This is the recurring
   failure class: the tool screen-scrapes claude's TUI, and every claude release can add
   or reword a prompt.
2. **Authoritative occupancy check in `_assert_startable()`.** Use `claude agents --json`
   (which knows session `busy`/`blocked` state) as the source of truth, not `/proc`
   cmdline UUID matching. Fail fast and clearly: "session <id> is busy/open; free it
   before starting CAO."
3. **Surface the root error, not the cleanup noise.** On start failure, propagate the
   original terminal-init error (the 500 + its cause) as the primary message; demote the
   `_stop_owned_server` "did not stop" to a secondary detail. Also raise its wait window
   (or SIGKILL sooner) so a slow-but-successful uvicorn shutdown doesn't masquerade as a
   cleanup failure.
4. **Decouple bridge startup from TD availability.** Consider letting `start` bring up the
   server + PD terminal even when the TD session can't be resumed, marking TD "detached,
   attach later," instead of failing the whole bridge.
5. **Guard against claude-CLI TUI drift.** Add an integration check that launches the
   *installed* claude version through the provider and asserts it reaches idle; run it in
   CI / after claude upgrades so prompt-format drift is caught before it breaks a live
   restart.

## Operational workaround (until B is fixed)

Before `cao authority start`, make sure the bridge's `claude_session_id` is **not open
elsewhere**:

```bash
# confirm the TD session is not busy/open
claude agents --json --cwd <project-root> | grep <claude_session_id>
# if it shows "busy": close that session first, then start
cao authority start --project-root <project-root>
cao authority status --project-root <project-root>   # verify it actually came up
```

## Evidence / where to look next time

- `state/logs/cao_*.log` — the **real** error (`… initialization timed out after 60s`).
- `state/logs/terminal/<terminal-id>.log` — raw tmux capture of what claude showed
  (this is what revealed both the resume-summary menu and the busy-session attach).
- `state/server.stdout.log` — the 500 on `POST …/terminals`, and the misleading cleanup.
- `state/authority-run.json` — `lifecycle: failed` after a failed start.

## Reporting upstream

Upstream is `awslabs/cli-agent-orchestrator`. Both causes (A: resume-summary prompt not
handled on the direct-resume path; B: `_assert_startable` UUID-scan blind spot) are
genuine upstream bugs worth filing. Filing a public issue is an outward-facing action —
do it deliberately, not automatically.
