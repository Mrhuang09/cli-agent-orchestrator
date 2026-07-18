"""Inbox message models."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class OrchestrationType(str, Enum):
    """Orchestration mode for a message delivery."""

    SEND_MESSAGE = "send_message"
    HANDOFF = "handoff"
    ASSIGN = "assign"


class MessageStatus(str, Enum):
    """Message status enumeration."""

    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


class AuthorityCallbackState(str, Enum):
    """Durable lifecycle for an authority task that requires a correlated reply."""

    WAITING_DELIVERY = "waiting_delivery"
    WAITING_START = "waiting_start"
    RUNNING = "running"
    WAITING_REPLY = "waiting_reply"
    REMINDED = "reminded"
    ESCALATED = "escalated"
    ACKNOWLEDGED = "acknowledged"
    CANCELLED = "cancelled"


class InboxMessage(BaseModel):
    """Inbox message model."""

    id: int = Field(..., description="Message ID")
    sender_id: str = Field(..., description="Sender terminal ID")
    receiver_id: str = Field(..., description="Receiver terminal ID")
    message: str = Field(..., description="Message content")
    status: MessageStatus = Field(..., description="Message status")
    created_at: datetime = Field(..., description="Creation timestamp")


class AuthorityCallback(BaseModel):
    """Persisted callback correlation and watchdog state."""

    request_message_id: int
    generation_id: str
    sender_id: str
    receiver_id: str
    state: AuthorityCallbackState
    created_at: datetime
    delivered_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    reminded_at: datetime | None = None
    escalated_at: datetime | None = None
    acknowledged_at: datetime | None = None
    reply_message_id: int | None = None
