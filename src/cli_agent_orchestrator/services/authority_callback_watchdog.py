"""Durable callback watchdog for project-director/technical-director tasks."""

import asyncio
import logging
from datetime import datetime

from cli_agent_orchestrator.clients.database import (
    enqueue_due_authority_callback_notices,
    record_authority_terminal_status,
)
from cli_agent_orchestrator.constants import (
    AUTHORITY_CALLBACK_ESCALATION_SECONDS,
    AUTHORITY_CALLBACK_RECONCILE_INTERVAL,
    AUTHORITY_CALLBACK_REMINDER_SECONDS,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.services.inbox_service import inbox_service
from cli_agent_orchestrator.utils.event import terminal_id_from_topic

logger = logging.getLogger(__name__)


class AuthorityCallbackWatchdog:
    """Advance callback state and emit idempotent reminders without model polling."""

    async def run(self, registry: PluginRegistry | None = None) -> None:
        queue = bus.subscribe("terminal.*.status")
        status_task = asyncio.create_task(self._consume_status(queue))
        reconcile_task = asyncio.create_task(self._reconcile(registry))
        logger.info("AuthorityCallbackWatchdog started")
        try:
            await asyncio.gather(status_task, reconcile_task)
        finally:
            for task in (status_task, reconcile_task):
                task.cancel()
            bus.unsubscribe("terminal.*.status", queue)

    async def _consume_status(self, queue: asyncio.Queue) -> None:
        while True:
            event = await queue.get()
            status_value = event["data"]["status"]
            if status_value not in {
                TerminalStatus.PROCESSING.value,
                TerminalStatus.IDLE.value,
                TerminalStatus.COMPLETED.value,
            }:
                continue
            terminal_id = terminal_id_from_topic(event["topic"])
            await asyncio.to_thread(
                record_authority_terminal_status, terminal_id, status_value
            )

    async def _reconcile(self, registry: PluginRegistry | None) -> None:
        while True:
            receivers = await asyncio.to_thread(
                enqueue_due_authority_callback_notices,
                now=datetime.now(),
                reminder_seconds=AUTHORITY_CALLBACK_REMINDER_SECONDS,
                escalation_seconds=AUTHORITY_CALLBACK_ESCALATION_SECONDS,
            )
            for receiver_id in receivers:
                try:
                    await asyncio.to_thread(
                        inbox_service.deliver_pending,
                        receiver_id,
                        registry=registry,
                    )
                except Exception as exc:
                    logger.warning(
                        "Immediate authority callback notice delivery failed for %s: %s",
                        receiver_id,
                        exc,
                    )
            await asyncio.sleep(AUTHORITY_CALLBACK_RECONCILE_INTERVAL)


authority_callback_watchdog = AuthorityCallbackWatchdog()
