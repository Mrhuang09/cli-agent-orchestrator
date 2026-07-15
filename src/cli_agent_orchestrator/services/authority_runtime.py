"""Lifecycle management for a project's persistent authority bridge."""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests

from cli_agent_orchestrator.constants import API_BASE_URL, SERVER_HOST, SERVER_PORT
from cli_agent_orchestrator.services.authority_config import (
    PROJECT_PROFILE_NAME,
    TECHNICAL_PROFILE_NAME,
    AuthorityConfig,
    load_authority_config,
)


def _read_cmdline(path: Path) -> list[str]:
    try:
        return [
            part.decode("utf-8", errors="replace")
            for part in path.read_bytes().split(b"\0")
            if part
        ]
    except OSError:
        return []


def find_authority_processes(
    provider: str,
    session_id: str,
    *,
    proc_root: Path = Path("/proc"),
) -> list[int]:
    """Return PIDs whose command line opens an exact provider session UUID."""
    matches: list[int] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        command = _read_cmdline(entry / "cmdline")
        if session_id not in command:
            continue
        provider_seen = any(Path(token).name == provider for token in command)
        if provider_seen:
            matches.append(int(entry.name))
    return sorted(matches)


def find_mailbox_watchers(*, proc_root: Path = Path("/proc")) -> list[int]:
    """Return PIDs running the mutually exclusive director_mailbox watcher."""
    matches: list[int] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        command = _read_cmdline(entry / "cmdline")
        if "watch" not in command:
            continue
        if any("director_mailbox" in token for token in command):
            matches.append(int(entry.name))
    return sorted(matches)


def _unescape_mount(value: str) -> str:
    return value.replace("\\040", " ").replace("\\011", "\t").replace("\\134", "\\")


def filesystem_type(path: Path, *, mounts_path: Path = Path("/proc/mounts")) -> str | None:
    """Return the filesystem type for a path using the longest matching mountpoint."""
    target = path.expanduser().resolve()
    best: tuple[int, str] | None = None
    try:
        lines = mounts_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue
        mountpoint = Path(_unescape_mount(fields[1]))
        try:
            target.relative_to(mountpoint)
        except ValueError:
            continue
        candidate = (len(str(mountpoint)), fields[2])
        if best is None or candidate[0] > best[0]:
            best = candidate
    return best[1] if best else None


def _port_in_use(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


class AuthorityRuntime:
    """Start, inspect, attach to, and stop one project's CAO authority session."""

    def __init__(self, project_root: Path):
        self.project_root = project_root.expanduser().resolve()
        self.config = load_authority_config(self.project_root)
        self.base_url = API_BASE_URL

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", 30)
        return requests.request(method, f"{self.base_url}{path}", **kwargs)

    def server_ready(self) -> bool:
        try:
            response = self._request("GET", "/health", timeout=2)
            payload = response.json()
            return bool(
                response.status_code == 200
                and isinstance(payload, dict)
                and payload.get("service") == "cli-agent-orchestrator"
            )
        except (requests.RequestException, ValueError):
            return False

    def session_exists(self) -> bool:
        if not self.server_ready():
            return False
        response = self._request("GET", f"/sessions/{self.config.effective_session_name}")
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True

    def _assert_startable(self) -> None:
        state_fs = filesystem_type(self.config.state_dir)
        if state_fs in {"9p", "drvfs", "fuseblk"}:
            raise RuntimeError(
                f"authority state must be on a POSIX filesystem, not {state_fs}: "
                f"{self.config.state_dir}"
            )
        codex_pids = find_authority_processes("codex", self.config.codex_session_id)
        if codex_pids:
            raise RuntimeError(
                f"project director session is already open in PID(s) {codex_pids}; "
                "exit that Codex CLI before starting CAO"
            )
        claude_pids = find_authority_processes("claude", self.config.claude_session_id)
        if claude_pids:
            raise RuntimeError(
                f"technical director session is already open in PID(s) {claude_pids}; "
                "exit that Claude CLI before starting CAO"
            )
        mailbox_pids = find_mailbox_watchers()
        if mailbox_pids:
            raise RuntimeError(
                f"director_mailbox watch is running in PID(s) {mailbox_pids}; "
                "stop the fallback executor before CAO"
            )

    @staticmethod
    def _server_executable() -> str:
        executable = shutil.which("cao-server")
        if executable:
            return executable
        sibling = Path(sys.executable).with_name("cao-server")
        if sibling.is_file():
            return str(sibling)
        raise RuntimeError("cao-server executable not found in PATH or beside the Python runtime")

    def _start_server(self) -> int:
        if _port_in_use(SERVER_HOST, SERVER_PORT):
            raise RuntimeError(f"port {SERVER_PORT} is already occupied by an unrecognized service")
        self.config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.config.state_dir, 0o700)
        log_path = self.config.state_dir / "server.stdout.log"
        env = os.environ.copy()
        env["CAO_HOME_DIR"] = str(self.config.state_dir)
        env["CAO_AGENTS_DIR"] = str(self.config.profiles_dir)
        with log_path.open("ab") as log_stream:
            process = subprocess.Popen(
                [
                    self._server_executable(),
                    "--agents-dir",
                    str(self.config.profiles_dir),
                    "--host",
                    SERVER_HOST,
                    "--port",
                    str(SERVER_PORT),
                ],
                cwd=self.project_root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        pid_path = self.config.state_dir / "server.pid"
        pid_path.write_text(f"{process.pid}\n", encoding="utf-8")
        os.chmod(pid_path, 0o600)
        for _ in range(60):
            if self.server_ready():
                return process.pid
            if process.poll() is not None:
                break
            time.sleep(0.5)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        raise RuntimeError(f"CAO server failed to start; inspect {log_path}")

    def _create_session(self) -> list[dict[str, Any]]:
        first = self._request(
            "POST",
            "/sessions",
            params={
                "agent_profile": PROJECT_PROFILE_NAME,
                "provider": "codex",
                "session_name": self.config.session_name,
                "working_directory": str(self.project_root),
                "allowed_tools": "*",
            },
            timeout=120,
        )
        first.raise_for_status()
        try:
            second = self._request(
                "POST",
                f"/sessions/{self.config.effective_session_name}/terminals",
                params={
                    "agent_profile": TECHNICAL_PROFILE_NAME,
                    "provider": "claude_code",
                    "working_directory": str(self.project_root),
                    "allowed_tools": "*",
                },
                timeout=120,
            )
            second.raise_for_status()
        except Exception:
            try:
                self._request(
                    "DELETE",
                    f"/sessions/{self.config.effective_session_name}",
                    timeout=30,
                )
            finally:
                raise
        return self.terminals()

    def start(self, *, attach: bool = True) -> list[dict[str, Any]]:
        """Start both authority terminals, or attach to the existing project session."""
        if self.server_ready():
            if not self.session_exists():
                raise RuntimeError(
                    f"a CAO server is already running on {self.base_url}, but it does not "
                    "own this project's authority session"
                )
            terminals = self.terminals()
        else:
            self._assert_startable()
            self._start_server()
            try:
                terminals = self._create_session()
            except Exception:
                self._stop_owned_server()
                raise
        if attach:
            self.attach()
        return terminals

    def terminals(self) -> list[dict[str, Any]]:
        response = self._request(
            "GET",
            f"/sessions/{self.config.effective_session_name}/terminals",
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError("CAO returned an invalid terminal list")
        return payload

    def status(self) -> list[dict[str, Any]]:
        if not self.session_exists():
            raise RuntimeError(
                f"authority bridge is not running: {self.config.effective_session_name}"
            )
        result: list[dict[str, Any]] = []
        for terminal in self.terminals():
            terminal_id = terminal["id"]
            detail = self._request("GET", f"/terminals/{terminal_id}")
            detail.raise_for_status()
            item = detail.json()
            pending = self._request(
                "GET",
                f"/terminals/{terminal_id}/inbox/messages",
                params={"limit": 100, "status": "pending"},
            )
            pending.raise_for_status()
            item["pending"] = len(pending.json())
            result.append(item)
        return result

    def attach(self) -> None:
        if not self.session_exists():
            raise RuntimeError(
                f"authority bridge is not running: {self.config.effective_session_name}"
            )
        if os.environ.get("TMUX"):
            os.execvp(
                "tmux",
                ["tmux", "switch-client", "-t", self.config.effective_session_name],
            )
        os.execvp(
            "tmux",
            ["tmux", "attach-session", "-t", self.config.effective_session_name],
        )

    def _owned_server_pid(self) -> int | None:
        pid_path = self.config.state_dir / "server.pid"
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
        command = _read_cmdline(Path("/proc") / str(pid) / "cmdline")
        if not any(Path(token).name == "cao-server" for token in command):
            return None
        try:
            environ = (Path("/proc") / str(pid) / "environ").read_bytes().split(b"\0")
        except OSError:
            return None
        marker = f"CAO_HOME_DIR={self.config.state_dir}".encode()
        return pid if marker in environ else None

    def _stop_owned_server(self) -> bool:
        pid = self._owned_server_pid()
        if pid is None:
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return False
        return True

    def stop(self) -> bool:
        deleted = False
        if self.server_ready() and self.session_exists():
            response = self._request(
                "DELETE",
                f"/sessions/{self.config.effective_session_name}",
                timeout=60,
            )
            response.raise_for_status()
            deleted = True
        stopped = self._stop_owned_server()
        return deleted or stopped
