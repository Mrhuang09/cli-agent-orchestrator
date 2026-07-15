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

    runtime._stop_owned_server.assert_called_once_with()


def test_stop_does_not_kill_unowned_server(tmp_path: Path):
    runtime = _runtime(tmp_path)
    runtime.server_ready = Mock(return_value=False)  # type: ignore[method-assign]
    runtime._owned_server_pid = Mock(return_value=None)  # type: ignore[method-assign]

    with patch("cli_agent_orchestrator.services.authority_runtime.os.kill") as kill:
        assert runtime.stop() is False

    kill.assert_not_called()
