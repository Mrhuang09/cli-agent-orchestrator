from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests

from cli_agent_orchestrator.services.authority_config import initialize_authority
from cli_agent_orchestrator.services.authority_runtime import (
    AuthorityRuntime,
    filesystem_type,
    find_authority_processes,
    find_mailbox_watchers,
)
from cli_agent_orchestrator.services.authority_manifest import AuthorityRunManifest


def _proc_entry(proc_root: Path, pid: int, command: list[str]) -> None:
    entry = proc_root / str(pid)
    entry.mkdir()
    (entry / "cmdline").write_bytes(b"\0".join(part.encode() for part in command) + b"\0")


def test_find_authority_processes_matches_provider_and_exact_uuid(tmp_path: Path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    session_id = "11111111-1111-4111-8111-111111111111"
    _proc_entry(proc_root, 101, ["codex", "resume", session_id])
    _proc_entry(proc_root, 102, ["claude", "--resume", "22222222-2222-4222-8222-222222222222"])
    _proc_entry(proc_root, 103, ["bash", "-c", session_id])

    assert find_authority_processes("codex", session_id, proc_root=proc_root) == [101]


def test_find_mailbox_watchers(tmp_path: Path):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _proc_entry(proc_root, 201, ["python", "director_mailbox.py", "watch"])
    _proc_entry(proc_root, 202, ["python", "director_mailbox.py", "send"])

    assert find_mailbox_watchers(proc_root=proc_root) == [201]


def test_filesystem_type_uses_longest_mountpoint(tmp_path: Path):
    mounts = tmp_path / "mounts"
    mounts.write_text("/dev/sdd / ext4 rw 0 0\n" "C:\\040drive /mnt/c 9p rw,aname=drvfs 0 0\n")

    assert filesystem_type(Path("/mnt/c/project"), mounts_path=mounts) == "9p"
    assert filesystem_type(Path("/home/user/project"), mounts_path=mounts) == "ext4"


def _runtime(tmp_path: Path) -> AuthorityRuntime:
    project = tmp_path / "project"
    project.mkdir()
    initialize_authority(
        project,
        codex_session_id="11111111-1111-4111-8111-111111111111",
        claude_session_id="22222222-2222-4222-8222-222222222222",
    )
    return AuthorityRuntime(project)


def _running_manifest(runtime: AuthorityRuntime) -> AuthorityRunManifest:
    return AuthorityRunManifest.starting(
        generation_id="generation-1",
        project_root=runtime.project_root,
        session_name=runtime.config.effective_session_name,
        server_pid=4321,
    ).evolve(
        lifecycle="running",
        project_director_terminal_id="aaaaaaaa",
        project_director_window="project-director",
        technical_director_terminal_id="bbbbbbbb",
        technical_director_window="technical-director",
    )


def _terminal(runtime: AuthorityRuntime, role: str) -> dict[str, str]:
    if role == "project-director":
        return {
            "id": "aaaaaaaa",
            "name": "project-director",
            "provider": "codex",
            "session_name": runtime.config.effective_session_name,
            "agent_profile": role,
        }
    return {
        "id": "bbbbbbbb",
        "name": "technical-director",
        "provider": "claude_code",
        "session_name": runtime.config.effective_session_name,
        "agent_profile": role,
    }


def test_assert_startable_rejects_non_posix_runtime(tmp_path: Path):
    runtime = _runtime(tmp_path)

    with (
        patch(
            "cli_agent_orchestrator.services.authority_runtime.filesystem_type",
            return_value="9p",
        ),
        pytest.raises(RuntimeError, match="POSIX filesystem"),
    ):
        runtime._assert_startable()


def test_assert_startable_rejects_open_authority_session(tmp_path: Path):
    runtime = _runtime(tmp_path)

    with (
        patch(
            "cli_agent_orchestrator.services.authority_runtime.filesystem_type",
            return_value="ext4",
        ),
        patch(
            "cli_agent_orchestrator.services.authority_runtime.find_authority_processes",
            side_effect=[[4321], []],
        ),
        pytest.raises(RuntimeError, match="already open"),
    ):
        runtime._assert_startable()


def test_create_session_rolls_back_when_second_terminal_fails(tmp_path: Path):
    runtime = _runtime(tmp_path)
    first = Mock()
    first.json.return_value = {
        "id": "aaaaaaaa",
        "name": "project-director",
        "provider": "codex",
        "session_name": runtime.config.effective_session_name,
        "agent_profile": "project-director",
    }
    second = Mock()
    second.raise_for_status.side_effect = requests.HTTPError("second terminal failed")
    deleted = Mock()
    runtime._request = Mock(side_effect=[first, second, deleted])  # type: ignore[method-assign]

    with pytest.raises(requests.HTTPError, match="second terminal failed"):
        runtime._create_session()

    assert runtime._request.call_args_list[-1].args == (
        "DELETE",
        f"/sessions/{runtime.config.effective_session_name}",
    )


def test_start_stops_owned_server_when_session_creation_fails(tmp_path: Path):
    runtime = _runtime(tmp_path)
    runtime.server_ready = Mock(return_value=False)  # type: ignore[method-assign]
    runtime._assert_startable = Mock()  # type: ignore[method-assign]
    runtime._start_server = Mock(return_value=1234)  # type: ignore[method-assign]
    runtime._create_session = Mock(side_effect=RuntimeError("create failed"))  # type: ignore[method-assign]
    runtime._stop_owned_server = Mock(return_value=True)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="create failed"):
        runtime.start(attach=False)

    runtime._stop_owned_server.assert_called_once_with(wait=True)


def test_start_persists_only_the_two_create_response_roles(tmp_path: Path):
    runtime = _runtime(tmp_path)
    project = _terminal(runtime, "project-director")
    technical = _terminal(runtime, "technical-director")
    runtime._assert_startable = Mock()  # type: ignore[method-assign]
    runtime._start_server = Mock(return_value=4321)  # type: ignore[method-assign]
    runtime.session_exists = Mock(return_value=False)  # type: ignore[method-assign]
    runtime._create_session = Mock(return_value=(project, technical))  # type: ignore[method-assign]
    runtime._terminals_from_manifest = Mock(return_value=[project, technical])  # type: ignore[method-assign]

    assert runtime.start(attach=False) == [project, technical]

    manifest = runtime.manifests.load()
    assert manifest is not None
    assert manifest.lifecycle == "running"
    assert manifest.server_pid == 4321
    assert manifest.project_director_terminal_id == "aaaaaaaa"
    assert manifest.technical_director_terminal_id == "bbbbbbbb"


def test_stop_does_not_kill_unowned_server(tmp_path: Path):
    runtime = _runtime(tmp_path)
    runtime.server_ready = Mock(return_value=False)  # type: ignore[method-assign]
    runtime._owned_server_pid = Mock(return_value=None)  # type: ignore[method-assign]

    with patch("cli_agent_orchestrator.services.authority_runtime.os.kill") as kill:
        assert runtime.stop() is False

    kill.assert_not_called()


def test_stop_cleans_tmux_even_when_server_api_is_unavailable(tmp_path: Path):
    runtime = _runtime(tmp_path)
    runtime.manifests.save(_running_manifest(runtime))
    runtime._delete_current_session_if_present = Mock(return_value=False)  # type: ignore[method-assign]
    runtime._kill_authority_tmux_if_present = Mock(return_value=True)  # type: ignore[method-assign]
    runtime._stop_owned_server = Mock(return_value=False)  # type: ignore[method-assign]

    with (
        patch(
            "cli_agent_orchestrator.services.authority_runtime._tmux_windows",
            return_value=[],
        ),
        patch(
            "cli_agent_orchestrator.services.authority_runtime._port_in_use",
            return_value=False,
        ),
    ):
        assert runtime.stop() is True

    manifest = runtime.manifests.load()
    assert manifest is not None
    assert manifest.lifecycle == "stopped"
    assert manifest.server_pid is None
    assert manifest.project_director_terminal_id is None


def test_running_manifest_requires_verified_owned_server(tmp_path: Path):
    runtime = _runtime(tmp_path)
    manifest = _running_manifest(runtime)
    runtime.manifests.save(manifest)
    runtime._owned_server_pid = Mock(return_value=9999)  # type: ignore[method-assign]
    runtime.server_ready = Mock(return_value=True)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="verified owned server"):
        runtime._running_manifest()


def test_current_generation_filters_historical_terminal_rows(tmp_path: Path):
    runtime = _runtime(tmp_path)
    manifest = _running_manifest(runtime)
    responses = []
    for payload in (_terminal(runtime, "project-director"), _terminal(runtime, "technical-director")):
        response = Mock()
        response.json.return_value = payload
        responses.append(response)
    runtime._request = Mock(side_effect=responses)  # type: ignore[method-assign]

    with patch(
        "cli_agent_orchestrator.services.authority_runtime._tmux_windows",
        return_value=["project-director", "technical-director"],
    ):
        assert runtime._terminals_from_manifest(manifest) == [
            _terminal(runtime, "project-director"),
            _terminal(runtime, "technical-director"),
        ]

    requested = [call.args[1] for call in runtime._request.call_args_list]
    assert requested == ["/terminals/aaaaaaaa", "/terminals/bbbbbbbb"]


def test_role_send_uses_current_generation_and_reports_pending(tmp_path: Path):
    runtime = _runtime(tmp_path)
    manifest = _running_manifest(runtime)
    runtime._running_manifest = Mock(return_value=manifest)  # type: ignore[method-assign]
    response = Mock()
    response.json.return_value = {"success": True, "message_id": 17}
    runtime._request = Mock(return_value=response)  # type: ignore[method-assign]

    result = runtime.send(
        from_role="project-director",
        to_role="technical-director",
        message="review S1",
    )

    assert result["message_id"] == 17
    assert result["queue_status"] == "pending"
    runtime._request.assert_called_once_with(
        "POST",
        "/terminals/bbbbbbbb/inbox/messages",
        params={"sender_id": "aaaaaaaa", "message": "review S1"},
    )


def test_role_send_rejects_self_send(tmp_path: Path):
    runtime = _runtime(tmp_path)
    runtime._running_manifest = Mock(return_value=_running_manifest(runtime))  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="must be different"):
        runtime.send(
            from_role="project-director",
            to_role="project-director",
            message="loop",
        )
