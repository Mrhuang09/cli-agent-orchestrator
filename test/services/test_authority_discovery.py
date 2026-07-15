import json
from pathlib import Path

from cli_agent_orchestrator.services.authority_discovery import discover_sessions

CODEX_ID = "11111111-1111-4111-8111-111111111111"
CLAUDE_ID = "22222222-2222-4222-8222-222222222222"


def test_discover_sessions_filters_by_exact_project_without_reading_body(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    codex_root = tmp_path / "codex-sessions"
    claude_root = tmp_path / "claude-projects"
    codex_root.mkdir()
    claude_root.mkdir()

    codex_file = codex_root / "rollout.jsonl"
    codex_file.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": CODEX_ID, "cwd": str(project)},
            }
        )
        + "\n"
        + json.dumps({"type": "message", "content": "PRIVATE BODY MUST NOT APPEAR"})
        + "\n"
    )
    (codex_root / "other.jsonl").write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": "019f617c-8d90-7e63-8873-3d4e3a47ba0d",
                    "cwd": str(other),
                },
            }
        )
        + "\n"
    )

    claude_dir = claude_root / "encoded-project"
    claude_dir.mkdir()
    (claude_dir / f"{CLAUDE_ID}.jsonl").write_text(
        json.dumps({"type": "last-prompt", "sessionId": CLAUDE_ID})
        + "\n"
        + json.dumps(
            {
                "type": "attachment",
                "sessionId": CLAUDE_ID,
                "cwd": str(project),
                "content": "PRIVATE BODY MUST NOT APPEAR",
            }
        )
        + "\n"
    )

    found = discover_sessions(project, codex_root=codex_root, claude_root=claude_root)

    assert [(item.provider, item.session_id) for item in found] == [
        ("codex", CODEX_ID),
        ("claude_code", CLAUDE_ID),
    ]
    assert all(item.project_root == project.resolve() for item in found)
    assert "PRIVATE BODY" not in json.dumps([item.as_dict() for item in found])


def test_discover_sessions_skips_malformed_and_noncanonical_records(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    codex_root = tmp_path / "codex-sessions"
    claude_root = tmp_path / "claude-projects"
    codex_root.mkdir()
    claude_root.mkdir()
    (codex_root / "broken.jsonl").write_text("not json\n")
    (codex_root / "bad-id.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "not-a-uuid", "cwd": str(project)}})
        + "\n"
    )

    assert discover_sessions(project, codex_root=codex_root, claude_root=claude_root) == []
