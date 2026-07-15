import os
from pathlib import Path

import pytest

from cli_agent_orchestrator.services.authority_config import (
    AuthorityConfig,
    authority_runtime_dir,
    initialize_authority,
    load_authority_config,
)

CODEX_ID = "11111111-1111-4111-8111-111111111111"
CLAUDE_ID = "22222222-2222-4222-8222-222222222222"


def _mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


def test_initialize_authority_writes_private_config_and_profiles(tmp_path: Path):
    project = tmp_path / "sample-project"
    project.mkdir()

    config = initialize_authority(
        project,
        codex_session_id=CODEX_ID,
        claude_session_id=CLAUDE_ID,
        codex_model="gpt-5.6-sol",
        claude_model="sonnet",
    )

    runtime_dir = authority_runtime_dir(project)
    assert config.project_root == project.resolve()
    assert config.codex_session_id == CODEX_ID
    assert config.claude_session_id == CLAUDE_ID
    assert config.session_name.startswith("authority-sample-project-")
    assert _mode(runtime_dir) == 0o700
    assert _mode(runtime_dir / "profiles") == 0o700
    assert _mode(runtime_dir / "state") == 0o700
    assert _mode(runtime_dir / "config.toml") == 0o600
    assert _mode(runtime_dir / "profiles" / "project-director.md") == 0o600
    assert _mode(runtime_dir / "profiles" / "technical-director.md") == 0o600

    project_profile = (runtime_dir / "profiles" / "project-director.md").read_text()
    technical_profile = (runtime_dir / "profiles" / "technical-director.md").read_text()
    assert f"resumeSessionId: {CODEX_ID}" in project_profile
    assert 'allowedTools:\n  - "*"' in project_profile
    assert f"resumeSessionId: {CLAUDE_ID}" in technical_profile
    assert "permissionMode: bypassPermissions" in technical_profile
    assert str(project.resolve()) not in project_profile + technical_profile

    assert load_authority_config(project) == config


def test_initialize_authority_rejects_invalid_uuid(tmp_path: Path):
    project = tmp_path / "sample-project"
    project.mkdir()

    with pytest.raises(ValueError, match="Codex session ID"):
        initialize_authority(
            project,
            codex_session_id="$(touch /tmp/not-allowed)",
            claude_session_id=CLAUDE_ID,
        )


def test_initialize_authority_refuses_existing_config_without_force(tmp_path: Path):
    project = tmp_path / "sample-project"
    project.mkdir()
    initialize_authority(
        project,
        codex_session_id=CODEX_ID,
        claude_session_id=CLAUDE_ID,
    )

    with pytest.raises(FileExistsError, match="already exists"):
        initialize_authority(
            project,
            codex_session_id=CODEX_ID,
            claude_session_id=CLAUDE_ID,
        )


def test_load_authority_config_rejects_copy_from_other_project(tmp_path: Path):
    project = tmp_path / "sample-project"
    project.mkdir()
    config = initialize_authority(
        project,
        codex_session_id=CODEX_ID,
        claude_session_id=CLAUDE_ID,
    )
    copied_project = tmp_path / "copied-project"
    copied_project.mkdir()
    copied_runtime = authority_runtime_dir(copied_project)
    copied_runtime.mkdir(parents=True)
    (copied_runtime / "config.toml").write_text(config.config_path.read_text())

    with pytest.raises(ValueError, match="belongs to another project"):
        load_authority_config(copied_project)


def test_authority_config_effective_session_name():
    config = AuthorityConfig(
        project_root=Path("/tmp/project"),
        session_name="authority-project-deadbeef",
        codex_session_id=CODEX_ID,
        claude_session_id=CLAUDE_ID,
    )

    assert config.effective_session_name == "cao-authority-project-deadbeef"
