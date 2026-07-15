"""Private per-project configuration for persistent authority sessions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[import-not-found,no-redef]


CONFIG_VERSION = 1
RUNTIME_RELATIVE_PATH = Path(".ai-collab-runtime") / "cao-authority"
PROJECT_PROFILE_NAME = "project-director"
TECHNICAL_PROFILE_NAME = "technical-director"


def authority_runtime_dir(project_root: Path) -> Path:
    """Return the private authority runtime directory for a project."""
    return project_root.expanduser().resolve() / RUNTIME_RELATIVE_PATH


def canonical_session_id(value: str, label: str) -> str:
    """Validate and normalize an authority session UUID."""
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{label} must be a canonical UUID") from exc
    normalized = str(parsed)
    if value.lower() != normalized:
        raise ValueError(f"{label} must be a canonical UUID")
    return normalized


def _session_name(project_root: Path) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", project_root.name.lower()).strip("-") or "project"
    slug = slug[:32].rstrip("-") or "project"
    digest = hashlib.sha256(str(project_root).encode("utf-8")).hexdigest()[:8]
    return f"authority-{slug}-{digest}"


@dataclass(frozen=True)
class AuthorityConfig:
    """Validated per-project authority bridge configuration."""

    project_root: Path
    session_name: str
    codex_session_id: str
    claude_session_id: str
    codex_model: str | None = None
    claude_model: str | None = None
    version: int = CONFIG_VERSION

    def __post_init__(self) -> None:
        root = self.project_root.expanduser().resolve()
        object.__setattr__(self, "project_root", root)
        object.__setattr__(
            self,
            "codex_session_id",
            canonical_session_id(self.codex_session_id, "Codex session ID"),
        )
        object.__setattr__(
            self,
            "claude_session_id",
            canonical_session_id(self.claude_session_id, "Claude session ID"),
        )
        if self.version != CONFIG_VERSION:
            raise ValueError(
                f"unsupported authority config version {self.version}; expected {CONFIG_VERSION}"
            )
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", self.session_name):
            raise ValueError("authority session name must be filesystem-safe and at most 60 chars")

    @property
    def runtime_dir(self) -> Path:
        return authority_runtime_dir(self.project_root)

    @property
    def profiles_dir(self) -> Path:
        return self.runtime_dir / "profiles"

    @property
    def state_dir(self) -> Path:
        return self.runtime_dir / "state"

    @property
    def config_path(self) -> Path:
        return self.runtime_dir / "config.toml"

    @property
    def effective_session_name(self) -> str:
        if self.session_name.startswith("cao-"):
            return self.session_name
        return f"cao-{self.session_name}"


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _config_text(config: AuthorityConfig) -> str:
    lines = [
        f"version = {config.version}",
        f"project_root = {_toml_string(str(config.project_root))}",
        f"session_name = {_toml_string(config.session_name)}",
        f"codex_session_id = {_toml_string(config.codex_session_id)}",
        f"claude_session_id = {_toml_string(config.claude_session_id)}",
    ]
    if config.codex_model:
        lines.append(f"codex_model = {_toml_string(config.codex_model)}")
    if config.claude_model:
        lines.append(f"claude_model = {_toml_string(config.claude_model)}")
    return "\n".join(lines) + "\n"


def _profile_text(config: AuthorityConfig, provider: str) -> str:
    if provider == "codex":
        name = PROJECT_PROFILE_NAME
        description = "Persistent project director authority session"
        model = config.codex_model
        session_id = config.codex_session_id
        permission_line = ""
        title = "PROJECT DIRECTOR"
        role_text = (
            "Continue this persisted project-director conversation. Direct the technical "
            "director through CAO messaging, follow the project's governance files, and "
            "report only evidence-backed results."
        )
    elif provider == "claude_code":
        name = TECHNICAL_PROFILE_NAME
        description = "Persistent technical director authority session"
        model = config.claude_model
        session_id = config.claude_session_id
        permission_line = "permissionMode: bypassPermissions\n"
        title = "TECHNICAL DIRECTOR"
        role_text = (
            "Continue this persisted technical-director conversation. Work under the "
            "project director, organize execution roles, and return evidence through CAO "
            "messaging while following the project's governance files."
        )
    else:  # pragma: no cover - internal caller contract
        raise ValueError(f"unsupported authority provider: {provider}")

    model_line = f"model: {_toml_string(model)}\n" if model else ""
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {_toml_string(description)}\n"
        f"provider: {provider}\n"
        f"{model_line}"
        "role: supervisor\n"
        "allowedTools:\n"
        '  - "*"\n'
        f"{permission_line}"
        f"resumeSessionId: {session_id}\n"
        "mcpServers:\n"
        "  cao-mcp-server:\n"
        "    type: stdio\n"
        "    command: cao-mcp-server\n"
        "    args: []\n"
        "---\n\n"
        f"# AUTHORITY BRIDGE — {title}\n\n"
        f"{role_text}\n\n"
        "Maximum local CLI permission removes per-tool prompts; it does not authorize "
        "external irreversible actions or decisions reserved for the human owner.\n"
    )


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def initialize_authority(
    project_root: Path,
    *,
    codex_session_id: str,
    claude_session_id: str,
    codex_model: str | None = None,
    claude_model: str | None = None,
    force: bool = False,
) -> AuthorityConfig:
    """Create or replace a project's private authority configuration."""
    root = project_root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"project root is not a directory: {root}")
    runtime_dir = authority_runtime_dir(root)
    config_path = runtime_dir / "config.toml"
    if config_path.exists() and not force:
        raise FileExistsError(f"authority config already exists: {config_path}")

    config = AuthorityConfig(
        project_root=root,
        session_name=_session_name(root),
        codex_session_id=codex_session_id,
        claude_session_id=claude_session_id,
        codex_model=codex_model,
        claude_model=claude_model,
    )

    runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    config.profiles_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for directory in (runtime_dir, config.profiles_dir, config.state_dir):
        os.chmod(directory, 0o700)

    _write_private(config.config_path, _config_text(config))
    _write_private(
        config.profiles_dir / "project-director.md",
        _profile_text(config, "codex"),
    )
    _write_private(
        config.profiles_dir / "technical-director.md",
        _profile_text(config, "claude_code"),
    )
    return config


def load_authority_config(project_root: Path) -> AuthorityConfig:
    """Load and validate the authority configuration for a project."""
    requested_root = project_root.expanduser().resolve()
    path = authority_runtime_dir(requested_root) / "config.toml"
    if not path.is_file():
        raise FileNotFoundError(f"authority config not found: {path}; run 'cao authority init'")
    with path.open("rb") as stream:
        raw: dict[str, Any] = tomllib.load(stream)
    try:
        config = AuthorityConfig(
            project_root=Path(raw["project_root"]),
            session_name=raw["session_name"],
            codex_session_id=raw["codex_session_id"],
            claude_session_id=raw["claude_session_id"],
            codex_model=raw.get("codex_model"),
            claude_model=raw.get("claude_model"),
            version=raw.get("version", 0),
        )
    except (KeyError, TypeError) as exc:
        raise ValueError(f"invalid authority config: {path}") from exc
    if config.project_root != requested_root:
        raise ValueError(f"authority config belongs to another project: {config.project_root}")
    return config
