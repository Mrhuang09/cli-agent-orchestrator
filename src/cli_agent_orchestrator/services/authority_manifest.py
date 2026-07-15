"""Durable current-generation state for the authority bridge."""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Literal


MANIFEST_VERSION = 1
Lifecycle = Literal["starting", "running", "stopping", "stopped", "failed"]
_LIFECYCLES = {"starting", "running", "stopping", "stopped", "failed"}
_TERMINAL_ID = re.compile(r"^[a-f0-9]{8}$")


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class AuthorityRunManifest:
    """The only authority-role binding valid for one runtime generation."""

    schema_version: int
    generation_id: str
    project_root: str
    session_name: str
    lifecycle: Lifecycle
    server_pid: int | None
    project_director_terminal_id: str | None
    project_director_window: str | None
    technical_director_terminal_id: str | None
    technical_director_window: str | None
    created_at: str
    updated_at: str

    @classmethod
    def starting(
        cls,
        *,
        generation_id: str,
        project_root: Path,
        session_name: str,
        server_pid: int | None = None,
    ) -> AuthorityRunManifest:
        timestamp = _now()
        return cls(
            schema_version=MANIFEST_VERSION,
            generation_id=generation_id,
            project_root=str(project_root.resolve()),
            session_name=session_name,
            lifecycle="starting",
            server_pid=server_pid,
            project_director_terminal_id=None,
            project_director_window=None,
            technical_director_terminal_id=None,
            technical_director_window=None,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def evolve(self, **changes: Any) -> AuthorityRunManifest:
        return replace(self, updated_at=_now(), **changes)

    def validate(self) -> None:
        if self.schema_version != MANIFEST_VERSION:
            raise ValueError(
                f"unsupported authority manifest version {self.schema_version}; "
                f"expected {MANIFEST_VERSION}"
            )
        if self.lifecycle not in _LIFECYCLES:
            raise ValueError(f"invalid authority lifecycle: {self.lifecycle!r}")
        if not self.generation_id:
            raise ValueError("authority generation_id must not be empty")
        if not Path(self.project_root).is_absolute():
            raise ValueError("authority manifest project_root must be absolute")
        if not self.session_name:
            raise ValueError("authority manifest session_name must not be empty")
        if self.server_pid is not None and self.server_pid <= 0:
            raise ValueError("authority manifest server_pid must be positive")

        ids = (
            self.project_director_terminal_id,
            self.technical_director_terminal_id,
        )
        for terminal_id in ids:
            if terminal_id is not None and not _TERMINAL_ID.fullmatch(terminal_id):
                raise ValueError(f"invalid authority terminal ID: {terminal_id!r}")
        if self.lifecycle == "running":
            if self.server_pid is None or any(item is None for item in ids):
                raise ValueError("running authority manifest requires server and both roles")
            if self.project_director_terminal_id == self.technical_director_terminal_id:
                raise ValueError("authority roles cannot share one terminal")


class AuthorityManifestStore:
    """Atomic private manifest storage plus a process-level lifecycle lock."""

    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.path = state_dir / "authority-run.json"
        self.lock_path = state_dir / "authority-run.lock"

    def _ensure_dir(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_dir, 0o700)

    @contextmanager
    def lock(self) -> Iterator[None]:
        self._ensure_dir()
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("another authority lifecycle operation is in progress") from exc
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def load(self) -> AuthorityRunManifest | None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid authority runtime manifest: {self.path}") from exc
        try:
            manifest = AuthorityRunManifest(**raw)
            manifest.validate()
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"invalid authority runtime manifest: {self.path}") from exc
        return manifest

    def save(self, manifest: AuthorityRunManifest) -> None:
        manifest.validate()
        self._ensure_dir()
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.state_dir, text=True
        )
        temp_path = Path(temp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(asdict(manifest), stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if temp_path.exists():
                temp_path.unlink()
