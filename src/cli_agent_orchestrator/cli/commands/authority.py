"""Manage persistent Codex/Claude authority sessions for one project."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import click
import requests

from cli_agent_orchestrator.services.authority_config import initialize_authority
from cli_agent_orchestrator.services.authority_discovery import (
    SessionCandidate,
    discover_sessions,
)
from cli_agent_orchestrator.services.authority_runtime import AuthorityRuntime


def _root(value: str) -> Path:
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise click.ClickException(f"project root is not a directory: {root}")
    return root


def _runtime_is_ignored(project_root: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", ".ai-collab-runtime"],
            cwd=project_root,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return result.returncode == 0


def _choose_candidate(
    provider: str,
    candidates: list[SessionCandidate],
) -> str:
    matching = [item for item in candidates if item.provider == provider]
    label = "Codex" if provider == "codex" else "Claude"
    if not matching:
        raise click.ClickException(
            f"No {label} session was discovered for this exact project. "
            f"Pass --{provider.replace('_code', '')}-session-id explicitly."
        )
    click.echo(f"\n{label} sessions:")
    for index, item in enumerate(matching, start=1):
        click.echo(f"  {index}. {item.session_id}  updated {item.updated_at}")
    choice = int(
        click.prompt(
            f"Select {label} session",
            type=click.IntRange(1, len(matching)),
        )
    )
    return matching[choice - 1].session_id


def _handle_error(exc: Exception) -> click.ClickException:
    if isinstance(exc, requests.RequestException):
        return click.ClickException(f"CAO request failed: {exc}")
    return click.ClickException(str(exc))


@click.group()
def authority() -> None:
    """Connect persistent Codex and Claude authority conversations."""


@authority.command("discover")
@click.option(
    "--project-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False, path_type=Path),
)
@click.option("--json", "as_json", is_flag=True, help="Output machine-readable JSON.")
def discover_command(project_root: Path, as_json: bool) -> None:
    """Find resumable sessions whose recorded cwd exactly matches the project."""
    root = _root(str(project_root))
    candidates = discover_sessions(root)
    if as_json:
        click.echo(json.dumps([item.as_dict() for item in candidates], indent=2))
        return
    if not candidates:
        click.echo(f"No resumable Codex or Claude sessions found for {root}")
        return
    click.echo(f"{'PROVIDER':<14} {'SESSION ID':<38} UPDATED")
    click.echo("-" * 84)
    for item in candidates:
        click.echo(f"{item.provider:<14} {item.session_id:<38} {item.updated_at}")


@authority.command("init")
@click.option(
    "--project-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False, path_type=Path),
)
@click.option("--codex-session-id", help="Existing Codex conversation UUID.")
@click.option("--claude-session-id", help="Existing Claude conversation UUID.")
@click.option("--codex-model", help="Optional Codex model override.")
@click.option("--claude-model", help="Optional Claude model override.")
@click.option(
    "--force", is_flag=True, help="Replace an existing private configuration."
)
def init_command(
    project_root: Path,
    codex_session_id: str | None,
    claude_session_id: str | None,
    codex_model: str | None,
    claude_model: str | None,
    force: bool,
) -> None:
    """Create private project-local profiles bound to existing conversations."""
    root = _root(str(project_root))
    if not codex_session_id or not claude_session_id:
        if not click.get_text_stream("stdin").isatty():
            raise click.ClickException(
                "non-interactive init requires both --codex-session-id and "
                "--claude-session-id"
            )
        candidates = discover_sessions(root)
        codex_session_id = codex_session_id or _choose_candidate("codex", candidates)
        claude_session_id = claude_session_id or _choose_candidate(
            "claude_code", candidates
        )
    try:
        config = initialize_authority(
            root,
            codex_session_id=codex_session_id,
            claude_session_id=claude_session_id,
            codex_model=codex_model,
            claude_model=claude_model,
            force=force,
        )
    except (ValueError, FileNotFoundError, FileExistsError, OSError) as exc:
        raise _handle_error(exc) from exc
    click.echo(f"Authority bridge initialized for {config.project_root}")
    click.echo(f"Private config: {config.config_path}")
    if not _runtime_is_ignored(root):
        click.secho(
            "Warning: .ai-collab-runtime is not ignored by Git; add it to a local "
            "or repository ignore file before committing.",
            fg="yellow",
            err=True,
        )


@authority.command("start")
@click.option(
    "--project-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False, path_type=Path),
)
@click.option("--attach/--no-attach", default=True, help="Attach tmux after startup.")
def start_command(project_root: Path, attach: bool) -> None:
    """Start the private CAO server and resume both authority conversations."""
    root = _root(str(project_root))
    try:
        terminals = AuthorityRuntime(root).start(attach=attach)
    except (
        ValueError,
        FileNotFoundError,
        RuntimeError,
        OSError,
        requests.RequestException,
    ) as exc:
        raise _handle_error(exc) from exc
    click.echo(f"Authority bridge started with {len(terminals)} terminals")


@authority.command("status")
@click.option(
    "--project-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False, path_type=Path),
)
@click.option("--json", "as_json", is_flag=True, help="Output machine-readable JSON.")
def status_command(project_root: Path, as_json: bool) -> None:
    """Show terminal state and pending inbox counts."""
    root = _root(str(project_root))
    try:
        items = AuthorityRuntime(root).status()
    except (
        ValueError,
        FileNotFoundError,
        RuntimeError,
        OSError,
        requests.RequestException,
    ) as exc:
        raise _handle_error(exc) from exc
    if as_json:
        click.echo(json.dumps(items, indent=2))
        return
    click.echo(
        f"{'ID':<12} {'PROFILE':<24} {'PROVIDER':<14} "
        f"{'STATUS':<14} {'PENDING':<8} {'AWAIT':<6} {'REMIND':<7} ESCALATE"
    )
    click.echo("-" * 92)
    for item in items:
        callback_states = item.get("callback_states", {})
        awaiting = sum(
            callback_states.get(state, 0)
            for state in (
                "waiting_delivery",
                "waiting_start",
                "running",
                "waiting_reply",
            )
        )
        click.echo(
            f"{str(item.get('id', 'N/A')):<12} "
            f"{str(item.get('agent_profile', 'N/A')):<24} "
            f"{str(item.get('provider', 'N/A')):<14} "
            f"{str(item.get('status', 'N/A')):<14} "
            f"{item.get('pending', 0):<8} {awaiting:<6} "
            f"{callback_states.get('reminded', 0):<7} "
            f"{callback_states.get('escalated', 0)}"
        )


@authority.command("attach")
@click.option(
    "--project-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False, path_type=Path),
)
def attach_command(project_root: Path) -> None:
    """Attach the tmux UI for the running authority bridge."""
    root = _root(str(project_root))
    try:
        AuthorityRuntime(root).attach()
    except (
        ValueError,
        FileNotFoundError,
        RuntimeError,
        OSError,
        requests.RequestException,
    ) as exc:
        raise _handle_error(exc) from exc


@authority.command("send")
@click.argument("message")
@click.option(
    "--to",
    "to_role",
    required=True,
    type=click.Choice(["project-director", "technical-director"]),
    help="Current authority role that receives the message.",
)
@click.option(
    "--from",
    "from_role",
    type=click.Choice(["project-director", "technical-director"]),
    help="Required outside an authority terminal; validated against the current generation.",
)
@click.option(
    "--project-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False, path_type=Path),
)
@click.option(
    "--wait-delivered",
    is_flag=True,
    help="Wait for inbox delivery, not for task completion or a reply.",
)
@click.option(
    "--require-callback/--no-require-callback",
    default=True,
    show_default=True,
    help="Track this authority task until a correlated reply is received.",
)
@click.option(
    "--reply-to",
    type=click.IntRange(min=1),
    help="Acknowledge an authority request by its message ID.",
)
@click.option(
    "--timeout", default=30.0, show_default=True, type=click.FloatRange(min=0.1)
)
@click.option("--json", "as_json", is_flag=True, help="Output machine-readable JSON.")
def send_command(
    message: str,
    to_role: str,
    from_role: str | None,
    project_root: Path,
    wait_delivered: bool,
    require_callback: bool,
    reply_to: int | None,
    timeout: float,
    as_json: bool,
) -> None:
    """Queue a durable message to the current terminal bound to a role."""
    root = _root(str(project_root))
    try:
        result = AuthorityRuntime(root).send(
            to_role=to_role,
            from_role=from_role,
            message=message,
            wait_delivered=wait_delivered,
            require_callback=require_callback,
            reply_to=reply_to,
            timeout=timeout,
        )
    except (
        ValueError,
        FileNotFoundError,
        RuntimeError,
        TimeoutError,
        OSError,
        requests.RequestException,
    ) as exc:
        raise _handle_error(exc) from exc
    if as_json:
        click.echo(json.dumps(result, indent=2))
        return
    suffix = ""
    if result.get("reply_to") is not None:
        suffix = f"; acknowledged request {result['reply_to']}"
    elif result.get("callback_request_id") is not None:
        suffix = f"; callback={result.get('callback_state')}"
    click.echo(
        f"Authority message {result['message_id']} accepted: "
        f"{result.get('queue_status', 'pending')}{suffix}"
    )


@authority.command("stop")
@click.option(
    "--project-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False, path_type=Path),
)
def stop_command(project_root: Path) -> None:
    """Stop only the CAO session and server proven to belong to this project."""
    root = _root(str(project_root))
    try:
        changed = AuthorityRuntime(root).stop()
    except (
        ValueError,
        FileNotFoundError,
        RuntimeError,
        OSError,
        requests.RequestException,
    ) as exc:
        raise _handle_error(exc) from exc
    click.echo(
        "Authority bridge stopped" if changed else "Authority bridge was not running"
    )
