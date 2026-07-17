from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.authority import authority
from cli_agent_orchestrator.services.authority_discovery import SessionCandidate

CODEX_ID = "11111111-1111-4111-8111-111111111111"
CLAUDE_ID = "22222222-2222-4222-8222-222222222222"


def test_authority_discover_json(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    candidate = SessionCandidate(
        provider="codex",
        session_id=CODEX_ID,
        project_root=project.resolve(),
        updated_at="2026-07-14T19:00:00+00:00",
    )
    runner = CliRunner()

    with patch(
        "cli_agent_orchestrator.cli.commands.authority.discover_sessions",
        return_value=[candidate],
    ):
        result = runner.invoke(
            authority,
            ["discover", "--project-root", str(project), "--json"],
        )

    assert result.exit_code == 0
    assert CODEX_ID in result.output
    assert "codex" in result.output


def test_authority_init_non_interactive(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    runner = CliRunner()

    result = runner.invoke(
        authority,
        [
            "init",
            "--project-root",
            str(project),
            "--codex-session-id",
            CODEX_ID,
            "--claude-session-id",
            CLAUDE_ID,
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Authority bridge initialized" in result.output
    assert (project / ".ai-collab-runtime/cao-authority/config.toml").exists()


def test_authority_start_delegates_to_runtime(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    runner = CliRunner()

    with patch("cli_agent_orchestrator.cli.commands.authority.AuthorityRuntime") as runtime_cls:
        result = runner.invoke(
            authority,
            ["start", "--project-root", str(project), "--no-attach"],
        )

    assert result.exit_code == 0, result.output
    runtime_cls.assert_called_once_with(project.resolve())
    runtime_cls.return_value.start.assert_called_once_with(attach=False)


def test_authority_send_requires_roles_and_reports_queue_acceptance(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    runner = CliRunner()

    with patch("cli_agent_orchestrator.cli.commands.authority.AuthorityRuntime") as runtime_cls:
        runtime_cls.return_value.send.return_value = {
            "message_id": 41,
            "queue_status": "pending",
        }
        result = runner.invoke(
            authority,
            [
                "send",
                "review S1",
                "--project-root",
                str(project),
                "--from",
                "project-director",
                "--to",
                "technical-director",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "message 41 accepted: pending" in result.output
    runtime_cls.return_value.send.assert_called_once_with(
        to_role="technical-director",
        from_role="project-director",
        message="review S1",
        wait_delivered=False,
        timeout=30.0,
    )
