"""Pydantic schemas for analytics (change events, activity, contributors)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from ontokit.models.change_event import ChangeOrigin

NetChange = Literal["added", "modified", "removed", "transient"]
ChangeField = Literal["labels", "definitions", "parents", "deprecated", "comments", "other"]
ChangeFieldValue = list[str] | bool | None


class ChangeEvent(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    branch: str
    entity_iri: str
    entity_type: str
    event_type: Literal["create", "update", "delete", "rename", "reparent", "deprecate"]
    user_id: str
    user_name: str | None = None
    commit_hash: str | None = None
    origin: ChangeOrigin = ChangeOrigin.HUMAN
    origin_model: str | None = None
    origin_batch_id: str | None = None
    changed_fields: list[str] = []
    old_values: dict[str, Any] | None = None
    new_values: dict[str, Any] | None = None
    created_at: str


class EntityHistoryResponse(BaseModel):
    entity_iri: str
    events: list[ChangeEvent]
    total: int


class ChangesWindow(BaseModel):
    days: int
    since: str
    until: str
    branch: str


class ChangeAuthor(BaseModel):
    user_id: str
    user_name: str | None


class ChangeAuthorCount(ChangeAuthor):
    event_count: int


class ChangesSummary(BaseModel):
    total_events: int
    entities_changed: int
    by_net_change: dict[NetChange, int]
    by_origin: dict[ChangeOrigin, int]
    authors: list[ChangeAuthorCount]


class FieldChange(BaseModel):
    field: ChangeField
    before: ChangeFieldValue
    after: ChangeFieldValue


class ChangedEntity(BaseModel):
    entity_iri: str
    net_change: NetChange
    origins: list[ChangeOrigin]
    last_changed_at: str


class ProjectChangeItem(ChangedEntity):
    entity_type: str
    label: str | None
    event_count: int
    authors: list[ChangeAuthor]
    field_changes: list[FieldChange]
    events: list[ChangeEvent]


class ProjectChangesResponse(BaseModel):
    window: ChangesWindow
    summary: ChangesSummary
    items: list[ProjectChangeItem]
    next_cursor: str | None


class ChangedEntitiesResponse(BaseModel):
    items: list[ChangedEntity]
    truncated: bool


class ActivityDay(BaseModel):
    date: str  # YYYY-MM-DD
    count: int


class TopEditor(BaseModel):
    user_id: str
    user_name: str
    edit_count: int


class ProjectActivity(BaseModel):
    daily_counts: list[ActivityDay]
    total_events: int
    top_editors: list[TopEditor]


class HotEntity(BaseModel):
    entity_iri: str
    entity_type: str
    label: str | None = None
    edit_count: int
    editor_count: int
    last_edited_at: str


class ContributorStats(BaseModel):
    user_id: str
    user_name: str
    create_count: int
    update_count: int
    delete_count: int
    total_count: int
    last_active_at: str
