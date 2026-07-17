import json
import os
from pathlib import Path

import pytest

from cli_agent_orchestrator.services.authority_manifest import (
    AuthorityManifestStore,
    AuthorityRunManifest,
)


def _running(tmp_path: Path) -> AuthorityRunManifest:
    return AuthorityRunManifest.starting(
        generation_id="generation-1",
        project_root=tmp_path,
        session_name="cao-authority-test",
        server_pid=1234,
    ).evolve(
        lifecycle="running",
        project_director_terminal_id="aaaaaaaa",
        project_director_window="project-director",
        technical_director_terminal_id="bbbbbbbb",
        technical_director_window="technical-director",
    )


def test_manifest_round_trip_is_private_and_atomic(tmp_path: Path):
    store = AuthorityManifestStore(tmp_path / "state")
    manifest = _running(tmp_path)

    store.save(manifest)

    assert store.load() == manifest
    assert os.stat(store.path).st_mode & 0o777 == 0o600
    assert os.stat(store.state_dir).st_mode & 0o777 == 0o700
    assert not list(store.state_dir.glob(".authority-run.json.*"))


def test_manifest_rejects_corrupt_json(tmp_path: Path):
    store = AuthorityManifestStore(tmp_path / "state")
    store.state_dir.mkdir()
    store.path.write_text("{broken", encoding="utf-8")

    with pytest.raises(RuntimeError, match="invalid authority runtime manifest"):
        store.load()


def test_running_manifest_requires_two_distinct_valid_terminal_ids(tmp_path: Path):
    raw = _running(tmp_path)
    invalid = raw.evolve(technical_director_terminal_id="aaaaaaaa")

    with pytest.raises(ValueError, match="cannot share"):
        invalid.validate()


def test_manifest_rejects_unknown_fields(tmp_path: Path):
    store = AuthorityManifestStore(tmp_path / "state")
    store.save(_running(tmp_path))
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    raw["unexpected"] = True
    store.path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(RuntimeError, match="invalid authority runtime manifest"):
        store.load()


def test_lifecycle_lock_rejects_second_holder(tmp_path: Path):
    first = AuthorityManifestStore(tmp_path / "state")
    second = AuthorityManifestStore(tmp_path / "state")

    with first.lock():
        with pytest.raises(RuntimeError, match="lifecycle operation"):
            with second.lock():
                raise AssertionError("unreachable")
