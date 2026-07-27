"""Notification request/response schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class NotificationResponse(BaseModel):
    """Single notification item."""

    id: UUID
    type: str
    title: str
    body: str | None = None
    # Nullable since KTD20: PR Party's `pr_party_ready` rows belong to no
    # OntoKit project and share this table and the one bell endpoint. Requiring
    # them here would not just drop those rows — response validation would 500
    # the whole page, taking every project notification on it down as well.
    project_id: UUID | None = None
    project_name: str | None = None
    target_id: str | None = None
    target_url: str | None = None
    is_read: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class NotificationListResponse(BaseModel):
    """Paginated notification list with unread count."""

    items: list[NotificationResponse]
    total: int
    unread_count: int
