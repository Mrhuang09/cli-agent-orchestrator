"""Read-only discovery of resumable Codex and Claude sessions."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from cli_agent_orchestrator.services.authority_config import canonical_session_id


@dataclass(frozen=True)
class SessionCandidate:
    provider: str
    session_id: str
    project_root: Path
    updated_at: str

    def as_dict(self) -> dict[str, str]:
        data = asdict(self)
        data["project_root"] = str(self.project_root)
        return data


def _updated_at(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def _json_lines(path: Path, limit: int) -> Iterator[dict]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for index, line in enumerate(stream):
                if index >= limit:
                    break
                try:
                    item = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(item, dict):
                    yield item
    except OSError:
        return


def _codex_candidate(path: Path, project_root: Path) -> SessionCandidate | None:
    first = next(_json_lines(path, 1), None)
    if not first or first.get("type") != "session_meta":
        return None
    payload = first.get("payload")
    if not isinstance(payload, dict):
        return None
    session_id = payload.get("id") or payload.get("session_id")
    cwd = payload.get("cwd")
    if not isinstance(session_id, str) or not isinstance(cwd, str):
        return None
    try:
        session_id = canonical_session_id(session_id, "Codex session ID")
    except ValueError:
        return None
    candidate_root = Path(cwd).expanduser().resolve()
    if candidate_root != project_root:
        return None
    return SessionCandidate(
        provider="codex",
        session_id=session_id,
        project_root=candidate_root,
        updated_at=_updated_at(path),
    )


def _claude_candidate(path: Path, project_root: Path) -> SessionCandidate | None:
    fallback_id = path.stem
    session_id: str | None = None
    cwd: str | None = None
    for item in _json_lines(path, 200):
        item_id = item.get("sessionId")
        item_cwd = item.get("cwd")
        if isinstance(item_id, str):
            session_id = item_id
        if isinstance(item_cwd, str):
            cwd = item_cwd
        if session_id and cwd:
            break
    session_id = session_id or fallback_id
    if cwd is None:
        return None
    try:
        session_id = canonical_session_id(session_id, "Claude session ID")
    except ValueError:
        return None
    candidate_root = Path(cwd).expanduser().resolve()
    if candidate_root != project_root:
        return None
    return SessionCandidate(
        provider="claude_code",
        session_id=session_id,
        project_root=candidate_root,
        updated_at=_updated_at(path),
    )


def discover_sessions(
    project_root: Path,
    *,
    codex_root: Path | None = None,
    claude_root: Path | None = None,
) -> list[SessionCandidate]:
    """List resumable sessions for exactly one project without returning message bodies."""
    root = project_root.expanduser().resolve()
    codex_root = codex_root or (
        Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions"
    )
    claude_root = claude_root or (
        Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))) / "projects"
    )

    candidates: list[SessionCandidate] = []
    if codex_root.is_dir():
        for path in codex_root.rglob("*.jsonl"):
            candidate = _codex_candidate(path, root)
            if candidate:
                candidates.append(candidate)
    if claude_root.is_dir():
        for path in claude_root.rglob("*.jsonl"):
            candidate = _claude_candidate(path, root)
            if candidate:
                candidates.append(candidate)

    deduplicated: dict[tuple[str, str], SessionCandidate] = {}
    for candidate in candidates:
        key = (candidate.provider, candidate.session_id)
        existing = deduplicated.get(key)
        if existing is None or candidate.updated_at > existing.updated_at:
            deduplicated[key] = candidate

    provider_order = {"codex": 0, "claude_code": 1}
    newest_first = sorted(deduplicated.values(), key=lambda item: item.updated_at, reverse=True)
    return sorted(newest_first, key=lambda item: provider_order.get(item.provider, 99))
