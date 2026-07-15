"""Lifecycle management for a project's persistent authority bridge."""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import requests

from cli_agent_orchestrator.constants import API_BASE_URL, SERVER_HOST, SERVER_PORT
from cli_agent_orchestrator.services.authority_config import (
    PROJECT_PROFILE_NAME,
    TECHNICAL_PROFILE_NAME,
    load_authority_config,
)
from cli_agent_orchestrator.services.authority_manifest import (
    AuthorityManifestStore,
    AuthorityRunManifest,
)


ROLE_PROJECT_DIRECTOR = "project-director"
ROLE_TECHNICAL_DIRECTOR = "technical-director"
_ROLE_FIELDS = {
    ROLE_PROJECT_DIRECTOR: (
        "project_director_terminal_id",
        "project_director_window",
    ),
    ROLE_TECHNICAL_DIRECTOR: (
        "technical_director_terminal_id",
        "technical_director_window",
    ),
}


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


def _pid_alive(pid: int) -> bool:
    return (Path("/proc") / str(pid)).exists()


def _tmux_windows(session_name: str) -> list[str]:
    result = subprocess.run(
        ["tmux", "list-windows", "-t", session_name, "-F", "#{window_name}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


class AuthorityRuntime:
    """Start, inspect, attach to, and stop one project's CAO authority session."""

    def __init__(self, project_root: Path):
        self.project_root = project_root.expanduser().resolve()
        self.config = load_authority_config(self.project_root)
        self.base_url = API_BASE_URL
        self.manifests = AuthorityManifestStore(self.config.state_dir)

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
        env["CAO_EAGER_INBOX_DELIVERY"] = "false"
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

    def _create_session(self) -> tuple[dict[str, Any], dict[str, Any]]:
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
        first_payload = first.json()
        if not isinstance(first_payload, dict):
            raise RuntimeError("CAO returned an invalid project-director terminal")
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
            second_payload = second.json()
            if not isinstance(second_payload, dict):
                raise RuntimeError("CAO returned an invalid technical-director terminal")
        except Exception:
            try:
                self._request(
                    "DELETE",
                    f"/sessions/{self.config.effective_session_name}",
                    timeout=30,
                )
            finally:
                raise
        return first_payload, second_payload

    def _validate_role_terminal(
        self,
        terminal: dict[str, Any],
        *,
        role: str,
        provider: str,
    ) -> None:
        if terminal.get("agent_profile") != role:
            raise RuntimeError(
                f"authority role mismatch: expected {role}, got {terminal.get('agent_profile')!r}"
            )
        if terminal.get("provider") != provider:
            raise RuntimeError(
                f"authority provider mismatch for {role}: expected {provider}, "
                f"got {terminal.get('provider')!r}"
            )
        if terminal.get("session_name") != self.config.effective_session_name:
            raise RuntimeError(f"authority terminal {role} belongs to the wrong session")
        if not terminal.get("id") or not terminal.get("name"):
            raise RuntimeError(f"authority terminal {role} lacks an ID or tmux window")

    def _running_manifest(
        self,
        manifest: AuthorityRunManifest | None = None,
    ) -> AuthorityRunManifest:
        manifest = manifest or self.manifests.load()
        if manifest is None or manifest.lifecycle != "running":
            raise RuntimeError(
                f"authority bridge is not running: {self.config.effective_session_name}"
            )
        if (
            manifest.project_root != str(self.project_root)
            or manifest.session_name != self.config.effective_session_name
        ):
            raise RuntimeError("authority manifest does not belong to this project")
        if self._owned_server_pid() != manifest.server_pid or not self.server_ready():
            raise RuntimeError("authority manifest server is not the verified owned server")
        return manifest

    def _terminal_for_id(self, terminal_id: str) -> dict[str, Any]:
        response = self._request("GET", f"/terminals/{terminal_id}")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"CAO returned invalid terminal detail for {terminal_id}")
        return payload

    def _terminals_from_manifest(
        self,
        manifest: AuthorityRunManifest,
    ) -> list[dict[str, Any]]:
        project = self._terminal_for_id(str(manifest.project_director_terminal_id))
        technical = self._terminal_for_id(str(manifest.technical_director_terminal_id))
        self._validate_role_terminal(project, role=ROLE_PROJECT_DIRECTOR, provider="codex")
        self._validate_role_terminal(
            technical,
            role=ROLE_TECHNICAL_DIRECTOR,
            provider="claude_code",
        )
        windows = _tmux_windows(self.config.effective_session_name)
        expected = [
            str(manifest.project_director_window),
            str(manifest.technical_director_window),
        ]
        if len(windows) != 2 or sorted(windows) != sorted(expected):
            raise RuntimeError(
                f"authority tmux windows do not match current generation: "
                f"expected {expected}, got {windows}"
            )
        return [project, technical]

    def start(self, *, attach: bool = True) -> list[dict[str, Any]]:
        """Transactionally start exactly one terminal for each authority role."""
        with self.manifests.lock():
            manifest = self.manifests.load()
            if manifest is not None and manifest.lifecycle == "running":
                terminals = self._terminals_from_manifest(self._running_manifest(manifest))
                if attach:
                    self.attach()
                return terminals

            if manifest is not None and manifest.lifecycle in {
                "starting",
                "stopping",
                "failed",
            }:
                recovery_errors: list[Exception] = []
                for action in (
                    self._delete_current_session_if_present,
                    self._kill_authority_tmux_if_present,
                    lambda: self._stop_owned_server(wait=True),
                ):
                    try:
                        action()
                    except Exception as recovery_error:
                        recovery_errors.append(recovery_error)
                if (
                    recovery_errors
                    or _tmux_windows(self.config.effective_session_name)
                    or _port_in_use(SERVER_HOST, SERVER_PORT)
                ):
                    details = "; ".join(str(item) for item in recovery_errors)
                    raise RuntimeError(
                        "interrupted authority generation could not be reconciled"
                        + (f": {details}" if details else "")
                    )
                self.manifests.save(
                    manifest.evolve(
                        lifecycle="stopped",
                        server_pid=None,
                        project_director_terminal_id=None,
                        project_director_window=None,
                        technical_director_terminal_id=None,
                        technical_director_window=None,
                    )
                )

            self._assert_startable()
            generation = AuthorityRunManifest.starting(
                generation_id=str(uuid.uuid4()),
                project_root=self.project_root,
                session_name=self.config.effective_session_name,
            )
            self.manifests.save(generation)
            try:
                server_pid = self._start_server()
                generation = generation.evolve(server_pid=server_pid)
                self.manifests.save(generation)

                if self.session_exists():
                    if _tmux_windows(self.config.effective_session_name):
                        raise RuntimeError(
                            "an untracked live authority tmux session already exists; "
                            "refusing destructive recovery"
                        )
                    stale = self._request(
                        "DELETE",
                        f"/sessions/{self.config.effective_session_name}",
                        timeout=60,
                    )
                    stale.raise_for_status()
                    if self.session_exists():
                        raise RuntimeError("stale authority session rows could not be removed")

                project, technical = self._create_session()
                self._validate_role_terminal(
                    project,
                    role=ROLE_PROJECT_DIRECTOR,
                    provider="codex",
                )
                self._validate_role_terminal(
                    technical,
                    role=ROLE_TECHNICAL_DIRECTOR,
                    provider="claude_code",
                )
                generation = generation.evolve(
                    lifecycle="running",
                    project_director_terminal_id=project["id"],
                    project_director_window=project["name"],
                    technical_director_terminal_id=technical["id"],
                    technical_director_window=technical["name"],
                )
                self.manifests.save(generation)
                terminals = self._terminals_from_manifest(generation)
            except Exception as start_error:
                cleanup_errors: list[Exception] = []
                for action in (
                    self._delete_current_session_if_present,
                    self._kill_authority_tmux_if_present,
                    lambda: self._stop_owned_server(wait=True),
                ):
                    try:
                        action()
                    except Exception as cleanup_error:  # keep trying every owned resource
                        cleanup_errors.append(cleanup_error)
                failed = generation.evolve(
                    lifecycle="failed",
                    server_pid=None,
                    project_director_terminal_id=None,
                    project_director_window=None,
                    technical_director_terminal_id=None,
                    technical_director_window=None,
                )
                self.manifests.save(failed)
                if cleanup_errors:
                    details = "; ".join(str(item) for item in cleanup_errors)
                    raise RuntimeError(
                        f"authority start failed and cleanup was incomplete: {details}"
                    ) from start_error
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
        manifest = self._running_manifest()
        result: list[dict[str, Any]] = []
        for terminal in self._terminals_from_manifest(manifest):
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
        manifest = self._running_manifest()
        self._terminals_from_manifest(manifest)
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

    def _stop_owned_server(self, *, wait: bool = False, timeout: float = 10.0) -> bool:
        pid = self._owned_server_pid()
        if pid is None:
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return False
        if wait:
            deadline = time.monotonic() + timeout
            while _pid_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            if _pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
                deadline = time.monotonic() + 2
                while _pid_alive(pid) and time.monotonic() < deadline:
                    time.sleep(0.05)
            if _pid_alive(pid):
                raise RuntimeError(f"owned CAO server PID {pid} did not stop")
        return True

    def _delete_current_session_if_present(self) -> bool:
        if not self.server_ready() or not self.session_exists():
            return False
        response = self._request(
            "DELETE",
            f"/sessions/{self.config.effective_session_name}",
            timeout=60,
        )
        response.raise_for_status()
        deadline = time.monotonic() + 10
        while _tmux_windows(self.config.effective_session_name) and time.monotonic() < deadline:
            time.sleep(0.1)
        if _tmux_windows(self.config.effective_session_name):
            raise RuntimeError("authority tmux session did not stop")
        return True

    def _kill_authority_tmux_if_present(self) -> bool:
        if not _tmux_windows(self.config.effective_session_name):
            return False
        result = subprocess.run(
            ["tmux", "kill-session", "-t", self.config.effective_session_name],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 and _tmux_windows(self.config.effective_session_name):
            raise RuntimeError(
                f"failed to stop authority tmux session: {result.stderr.strip()}"
            )
        deadline = time.monotonic() + 5
        while _tmux_windows(self.config.effective_session_name) and time.monotonic() < deadline:
            time.sleep(0.1)
        if _tmux_windows(self.config.effective_session_name):
            raise RuntimeError("authority tmux session remains after forced cleanup")
        return True

    def role_terminal_id(
        self,
        role: str,
        *,
        manifest: AuthorityRunManifest | None = None,
    ) -> str:
        if role not in _ROLE_FIELDS:
            raise ValueError(f"unknown authority role: {role}")
        running = self._running_manifest(manifest)
        field, _ = _ROLE_FIELDS[role]
        terminal_id = getattr(running, field)
        if terminal_id is None:  # validated running manifests cannot reach this branch
            raise RuntimeError(f"authority role {role} has no current terminal")
        return str(terminal_id)

    def send(
        self,
        *,
        to_role: str,
        message: str,
        from_role: str | None = None,
        wait_delivered: bool = False,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        if not message.strip():
            raise ValueError("authority message must not be empty")
        manifest = self._running_manifest()
        receiver_id = self.role_terminal_id(to_role, manifest=manifest)
        env_sender = os.environ.get("CAO_TERMINAL_ID")
        if from_role is None:
            valid_ids = {
                str(manifest.project_director_terminal_id),
                str(manifest.technical_director_terminal_id),
            }
            if env_sender not in valid_ids:
                raise RuntimeError(
                    "outside an authority terminal, --from must name the sender role"
                )
            sender_id = str(env_sender)
        else:
            sender_id = self.role_terminal_id(from_role, manifest=manifest)
            current_ids = {
                str(manifest.project_director_terminal_id),
                str(manifest.technical_director_terminal_id),
            }
            # Resumed Claude background sessions can retain a terminal ID from
            # the generation that originally spawned their daemon. Treat that
            # stale value like an outside shell: the explicit current role is
            # required and authoritative. A *current-generation* mismatch is
            # still a hard impersonation error.
            if env_sender in current_ids and env_sender != sender_id:
                raise RuntimeError("--from role does not match CAO_TERMINAL_ID")
        if sender_id == receiver_id:
            raise ValueError("authority sender and receiver must be different roles")

        response = self._request(
            "POST",
            f"/terminals/{receiver_id}/inbox/messages",
            params={"sender_id": sender_id, "message": message},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or "message_id" not in payload:
            raise RuntimeError("CAO returned an invalid inbox acknowledgement")
        payload["queue_status"] = "pending"
        if wait_delivered:
            message_id = payload["message_id"]
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                current = self.manifests.load()
                if current is None or current.generation_id != manifest.generation_id:
                    raise RuntimeError("authority generation changed while waiting for delivery")
                inbox = self._request(
                    "GET",
                    f"/terminals/{receiver_id}/inbox/messages",
                    params={"limit": 100},
                )
                inbox.raise_for_status()
                matches = [item for item in inbox.json() if item.get("id") == message_id]
                if matches and matches[0].get("status") in {"delivered", "failed"}:
                    payload["queue_status"] = matches[0]["status"]
                    return payload
                time.sleep(0.2)
            raise TimeoutError(
                f"authority message {message_id} was not delivered within {timeout:g}s"
            )
        return payload

    def stop(self) -> bool:
        with self.manifests.lock():
            manifest = self.manifests.load()
            if manifest is None or manifest.lifecycle == "stopped":
                return False
            self.manifests.save(manifest.evolve(lifecycle="stopping"))
            cleanup_errors: list[Exception] = []
            results: list[bool] = []
            for action in (
                self._delete_current_session_if_present,
                self._kill_authority_tmux_if_present,
                lambda: self._stop_owned_server(wait=True),
            ):
                try:
                    results.append(bool(action()))
                except Exception as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            if _tmux_windows(self.config.effective_session_name):
                cleanup_errors.append(RuntimeError("authority tmux session remains after stop"))
            if _port_in_use(SERVER_HOST, SERVER_PORT):
                cleanup_errors.append(
                    RuntimeError(f"CAO server port {SERVER_PORT} remains occupied after stop")
                )
            if cleanup_errors:
                self.manifests.save(manifest.evolve(lifecycle="failed"))
                details = "; ".join(str(item) for item in cleanup_errors)
                raise RuntimeError(f"authority stop cleanup incomplete: {details}")
            self.manifests.save(
                manifest.evolve(
                    lifecycle="stopped",
                    server_pid=None,
                    project_director_terminal_id=None,
                    project_director_window=None,
                    technical_director_terminal_id=None,
                    technical_director_window=None,
                )
            )
            return any(results) or manifest.lifecycle != "stopped"
