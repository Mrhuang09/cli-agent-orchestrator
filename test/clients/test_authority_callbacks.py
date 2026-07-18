"""Durable authority callback lifecycle tests."""

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as db_mod
from cli_agent_orchestrator.clients.database import Base, TerminalModel
from cli_agent_orchestrator.models.inbox import AuthorityCallbackState, MessageStatus


@pytest.fixture
def callback_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'callbacks.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", session)
    with session() as handle:
        handle.add_all(
            [
                TerminalModel(
                    id="director",
                    tmux_session="authority",
                    tmux_window="project-director",
                    provider="codex",
                ),
                TerminalModel(
                    id="claude",
                    tmux_session="authority",
                    tmux_window="technical-director",
                    provider="claude_code",
                ),
            ]
        )
        handle.commit()
    yield
    engine.dispose()


def test_callback_lifecycle_reminds_escalates_and_acknowledges(callback_db):
    request, callback = db_mod.create_authority_message(
        "director", "claude", "review this", "generation-1"
    )
    assert callback is not None
    assert callback.state == AuthorityCallbackState.WAITING_DELIVERY

    assert db_mod.update_message_status(request.id, MessageStatus.DELIVERED)
    assert (
        db_mod.get_authority_callback(request.id).state
        == AuthorityCallbackState.WAITING_START
    )

    assert db_mod.record_authority_terminal_status("claude", "processing") == 1
    assert db_mod.record_authority_terminal_status("claude", "completed") == 1
    waiting = db_mod.get_authority_callback(request.id)
    assert waiting.state == AuthorityCallbackState.WAITING_REPLY

    due = waiting.completed_at + timedelta(seconds=181)
    assert db_mod.enqueue_due_authority_callback_notices(
        now=due, reminder_seconds=180, escalation_seconds=600
    ) == ["claude"]
    assert (
        db_mod.get_authority_callback(request.id).state
        == AuthorityCallbackState.REMINDED
    )

    later = waiting.completed_at + timedelta(seconds=601)
    assert db_mod.enqueue_due_authority_callback_notices(
        now=later, reminder_seconds=180, escalation_seconds=600
    ) == ["director"]
    assert (
        db_mod.get_authority_callback(request.id).state
        == AuthorityCallbackState.ESCALATED
    )

    reply, acknowledged = db_mod.create_authority_message(
        "claude",
        "director",
        "done",
        "generation-1",
        require_callback=False,
        reply_to=request.id,
    )
    assert acknowledged is not None
    assert acknowledged.state == AuthorityCallbackState.ACKNOWLEDGED
    assert acknowledged.reply_message_id == reply.id

    # Acknowledged callbacks never remind or escalate again.
    assert (
        db_mod.enqueue_due_authority_callback_notices(
            now=later + timedelta(hours=1), reminder_seconds=180, escalation_seconds=600
        )
        == []
    )


def test_reply_requires_reversed_roles_and_same_generation(callback_db):
    request, _ = db_mod.create_authority_message(
        "director", "claude", "review", "generation-1"
    )
    with pytest.raises(ValueError, match="different generation"):
        db_mod.create_authority_message(
            "claude", "director", "done", "generation-2", reply_to=request.id
        )
    with pytest.raises(ValueError, match="do not reverse"):
        db_mod.create_authority_message(
            "director", "claude", "fake reply", "generation-1", reply_to=request.id
        )


def test_second_reply_is_rejected_without_creating_an_inbox_message(callback_db):
    request, _ = db_mod.create_authority_message(
        "director", "claude", "review", "generation-1"
    )
    reply, _ = db_mod.create_authority_message(
        "claude", "director", "done", "generation-1", reply_to=request.id
    )

    with pytest.raises(ValueError, match="already closed"):
        db_mod.create_authority_message(
            "claude", "director", "duplicate", "generation-1", reply_to=request.id
        )

    messages = db_mod.get_inbox_messages("director")
    assert [message.id for message in messages] == [reply.id]


def test_generation_replacement_cancels_only_unresolved_old_callbacks(callback_db):
    old, _ = db_mod.create_authority_message(
        "director", "claude", "old", "generation-old"
    )
    current, _ = db_mod.create_authority_message(
        "director", "claude", "current", "generation-current"
    )

    assert db_mod.cancel_authority_callbacks_except("generation-current") == 1
    assert (
        db_mod.get_authority_callback(old.id).state == AuthorityCallbackState.CANCELLED
    )
    assert (
        db_mod.get_authority_callback(current.id).state
        == AuthorityCallbackState.WAITING_DELIVERY
    )


def test_notice_claim_is_idempotent(callback_db):
    request, _ = db_mod.create_authority_message(
        "director", "claude", "review", "generation-1"
    )
    db_mod.update_message_status(request.id, MessageStatus.DELIVERED)
    db_mod.record_authority_terminal_status("claude", "processing")
    db_mod.record_authority_terminal_status("claude", "completed")
    completed = db_mod.get_authority_callback(request.id).completed_at
    now = completed + timedelta(seconds=181)

    assert db_mod.enqueue_due_authority_callback_notices(
        now=now, reminder_seconds=180, escalation_seconds=600
    ) == ["claude"]
    assert (
        db_mod.enqueue_due_authority_callback_notices(
            now=now, reminder_seconds=180, escalation_seconds=600
        )
        == []
    )
