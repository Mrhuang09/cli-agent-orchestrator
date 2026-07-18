"""Authority callback watchdog notice-policy tests."""

import pytest

from cli_agent_orchestrator.services import authority_callback_watchdog as watchdog_mod


@pytest.mark.asyncio
async def test_reconcile_once_does_not_enqueue_notices_by_default(monkeypatch):
    called = False

    def unexpected_enqueue(**_kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(watchdog_mod, "AUTHORITY_CALLBACK_NOTICES_ENABLED", False)
    monkeypatch.setattr(
        watchdog_mod, "enqueue_due_authority_callback_notices", unexpected_enqueue
    )

    await watchdog_mod.authority_callback_watchdog._reconcile_once(None)

    assert called is False


@pytest.mark.asyncio
async def test_reconcile_once_enqueues_and_delivers_when_opted_in(monkeypatch):
    enqueued = False
    delivered: list[str] = []

    def enqueue(**_kwargs):
        nonlocal enqueued
        enqueued = True
        return ["technical-director"]

    def deliver(receiver_id, *, registry):
        assert registry == "registry"
        delivered.append(receiver_id)

    monkeypatch.setattr(watchdog_mod, "AUTHORITY_CALLBACK_NOTICES_ENABLED", True)
    monkeypatch.setattr(
        watchdog_mod, "enqueue_due_authority_callback_notices", enqueue
    )
    monkeypatch.setattr(watchdog_mod.inbox_service, "deliver_pending", deliver)

    await watchdog_mod.authority_callback_watchdog._reconcile_once("registry")

    assert enqueued is True
    assert delivered == ["technical-director"]
