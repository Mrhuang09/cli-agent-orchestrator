# Persistent Codex–Claude Authority Bridge

The authority bridge is a fork extension for a two-level project leadership workflow:

- an existing Codex conversation continues as the project director;
- an existing Claude Code conversation continues as the technical director;
- both CLIs run in the same project checkout and exchange messages through CAO;
- each provider resumes its native conversation UUID, so the relationship does not restart
  from an empty prompt.

This feature currently targets Codex CLI plus Claude Code. It does not import, copy, or print
conversation bodies.

## Install this fork

Requirements: Python 3.10+, `uv`, tmux 3.3+, authenticated `codex` and `claude` CLIs.

```bash
uv tool install --upgrade \
  git+https://github.com/Mrhuang09/cli-agent-orchestrator.git@main
```

Verify the extension is present:

```bash
cao authority --help
```

## One-time setup for a project

Open Codex and Claude in the project at least once so both providers have a native conversation
to resume. Then run:

```bash
cd /path/to/project
cao authority discover
cao authority init
```

`discover` returns only provider, session UUID, exact recorded project root, and update time. It
does not return message text. `init` requires an explicit selection for each provider; it never
silently chooses the newest conversation.

For unattended initialization, pass both UUIDs explicitly:

```bash
cao authority init \
  --codex-session-id <codex-uuid> \
  --claude-session-id <claude-uuid>
```

The command writes private configuration and generated profiles under:

```text
.ai-collab-runtime/cao-authority/
```

Files use mode `0600` and directories use `0700`. Add `.ai-collab-runtime/` to the project's
Git ignore rules. The command warns but does not edit repository policy on your behalf.

## Daily operation

Before starting, finish the active turn and exit any ordinary Codex or Claude CLI process that
uses the selected UUIDs. A conversation UUID must not be opened by two processes at once.

```bash
cao authority start          # start and attach to tmux
cao authority status         # terminal state and pending inbox count
cao authority attach         # attach again later
cao authority stop           # stop only the owned session/server
```

Messages use stable authority roles rather than transient terminal IDs:

```bash
# Run inside the project-director terminal (sender inferred from CAO_TERMINAL_ID)
cao authority send --to technical-director "Review the current contract"

# Run from an ordinary shell (sender must be explicit; request above returned ID 41)
cao authority send \
  --project-root /path/to/project \
  --from technical-director \
  --to project-director \
  --reply-to 41 \
  "Review completed; evidence follows"
```

Authority messages require a correlated callback by default. The request command prints its
message ID; the receiver returns that ID with `--reply-to`. Replies never recursively require a
second callback. Use `--no-require-callback` only for an intentional one-way notice.

The command reports durable inbox acceptance as `pending`. That is not task completion.
`--wait-delivered` waits only until CAO delivers the message to the receiving terminal. After the
receiver actually enters processing and then becomes completed/idle, callback state remains visible
in `cao authority status` until a correlated reply arrives. Automatic reminder and escalation
messages are disabled by default so transient provider status cannot inject notices into an active
model turn. Deployments that explicitly want the old three-minute reminder and ten-minute
escalation behavior can set `CAO_AUTHORITY_CALLBACK_NOTICES_ENABLED=true` before starting CAO. The
timers use the local SQLite state machine and do not call either model or consume model tokens.
Silence is reported only as a missing callback, never as inferred task failure. MCP messaging is
optional and is not required for authority bridge correctness.

For automation or diagnostics:

```bash
cao authority start --no-attach
cao authority status --json
```

Text status shows awaiting/reminded/escalated counts. JSON status also includes unresolved request
IDs and, when callback notices are enabled, reminder/escalation deadlines. Replacing or stopping an
authority generation cancels its unresolved callback rows so notices cannot cross sessions.

`start` refuses to proceed when it detects any of the following:

- the same Codex or Claude UUID is already open;
- the legacy `director_mailbox watch` executor is running;
- port 9889 belongs to another service or CAO project;
- CAO state is on Windows drvfs/9p instead of a POSIX filesystem.

Every successful start writes a private `state/authority-run.json` manifest. It binds exactly one
current project-director terminal and one current technical-director terminal to a new generation.
`status`, `attach`, `send`, and `stop` resolve through this manifest, so historical SQLite terminal
rows cannot become the current conductor or receive authority messages.

On WSL, keep the project and `.ai-collab-runtime` on the Linux filesystem (for example under
`/home/...`), not `/mnt/c/...`; CAO uses POSIX FIFOs and file permissions.

## Changing conversations or moving a checkout

Configuration is bound to the exact resolved project root. Copying it to another checkout is
rejected. In the new checkout, run `cao authority init` again.

To replace either authority conversation in the same checkout:

```bash
cao authority init --force \
  --codex-session-id <new-codex-uuid> \
  --claude-session-id <new-claude-uuid>
```

Stop the bridge before replacing its configuration.

## Security boundary

Provider profiles use maximum local CLI permission so routine engineering work does not pause on
tool confirmations. This does not authorize irreversible external actions, publication, spending,
credential disclosure, or decisions reserved for the human owner. Session UUIDs, state databases,
logs, and generated profiles must stay out of public repositories.

`stop` reads the saved server PID and verifies both the executable and the project's
`CAO_HOME_DIR` marker before sending a signal. It waits for the exact tmux session, owned server
PID, and port to disappear before recording `stopped`; it will not report success while owned
runtime residue remains. It will not kill an unverified process merely because it occupies the
expected PID.

## Exclusive authority work surface

The persisted Codex or Claude UUID must be fully exited from every other CLI, desktop, IDE, or
app-server surface before `start`. Merely leaving another copy idle is not sufficient. While
authority mode is active, use the attached CAO project-director terminal as the project-director
work surface. Process inspection cannot prove that a desktop/IDE app-server has released a
conversation, so this cutover remains an explicit operator responsibility.
