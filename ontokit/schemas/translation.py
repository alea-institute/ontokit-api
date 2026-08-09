"""Public and write-only schemas for project translation settings."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

_BCP47_RE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")


class VerificationMechanism(StrEnum):
    consensus = "consensus"
    confidence = "confidence"


class TranslationSpeedMode(StrEnum):
    batch = "batch"
    fast = "fast"


class TranslationConfigResponse(BaseModel):
    language_tags: list[str] = Field(default_factory=list)
    verification_mechanism: VerificationMechanism = VerificationMechanism.consensus
    consensus_threshold: float = 0.85
    confidence_threshold: float = 0.80
    translate_definitions: bool = False
    translate_examples: bool = False
    speed_mode: TranslationSpeedMode = TranslationSpeedMode.batch
    provisional_gate: bool = False
    primary_provider: str | None = None
    primary_model: str | None = None
    verifier_provider: str | None = None
    verifier_model: str | None = None
    verifier_api_key_set: bool = False


class TranslationConfigUpdate(BaseModel):
    language_tags: list[str] | None = None
    verification_mechanism: VerificationMechanism | None = None
    consensus_threshold: float | None = Field(default=None, ge=0, le=1)
    confidence_threshold: float | None = Field(default=None, ge=0, le=1)
    translate_definitions: bool | None = None
    translate_examples: bool | None = None
    speed_mode: TranslationSpeedMode | None = None
    provisional_gate: bool | None = None
    primary_provider: str | None = Field(default=None, max_length=50)
    primary_model: str | None = Field(default=None, max_length=200)
    verifier_provider: str | None = Field(default=None, max_length=50)
    verifier_model: str | None = Field(default=None, max_length=200)
    # Write-only. It is intentionally absent from TranslationConfigResponse.
    verifier_api_key: str | None = None

    @field_validator("language_tags")
    @classmethod
    def validate_language_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        unique: list[str] = []
        seen: set[str] = set()
        for tag in value:
            if not tag or len(tag) > 35 or not _BCP47_RE.fullmatch(tag):
                raise ValueError(f"invalid BCP 47 language tag: {tag!r}")
            normalized = tag.casefold()
            if normalized not in seen:
                seen.add(normalized)
                unique.append(tag)
        return unique


class LanguagePaletteEntry(BaseModel):
    tag: str
    english_name: str
    native_name: str | None = None


class TranslateFieldRequest(BaseModel):
    entity_iri: str = Field(min_length=1, max_length=2000)
    predicate: Literal["skos:definition", "skos:example"]
    branch: str = Field(min_length=1, max_length=255)


class TranslationJobAccepted(BaseModel):
    job_id: str


class TranslationBackfillRequest(BaseModel):
    branch: str = Field(min_length=1, max_length=255)
    language: str | None = Field(default=None, min_length=1, max_length=35)
    era_before: datetime | None = None
    never_confirmed: bool | None = None


class TranslationBackfillPreview(BaseModel):
    literal_count: int
    expected_cost_usd: float
    upper_bound_cost_usd: float
    batch_discount_applied: bool


class TranslationBackfillStatus(BaseModel):
    job_id: str
    status: Literal["pending", "running", "completed", "failed"]
    total: int
    completed: int
    error: str | None = None


class ReviewerLanguagesUpdate(BaseModel):
    languages: list[str] = Field(default_factory=list)

    @field_validator("languages")
    @classmethod
    def validate_languages(cls, value: list[str]) -> list[str]:
        validated = TranslationConfigUpdate.validate_language_tags(value)
        return validated or []


class ReviewerEntry(BaseModel):
    member_id: UUID
    user_id: str
    languages: list[str] = Field(default_factory=list)


class ReviewerLanguagesResponse(BaseModel):
    languages: list[str] = Field(default_factory=list)


class TranslationReviewRequest(BaseModel):
    branch: str = Field(min_length=1, max_length=255)


class TranslationBulkConfirmRequest(TranslationReviewRequest):
    record_ids: list[UUID] = Field(min_length=1)


class TranslationRecordSummary(BaseModel):
    id: UUID
    project_id: UUID
    entity_iri: str
    predicate: str
    language: str
    proposed_value: str | None
    state: str
    confirming_member_id: UUID | None


class TranslationBulkResult(BaseModel):
    record_id: UUID
    ok: bool
    error: str | None = None


class TranslationBulkConfirmResponse(BaseModel):
    results: list[TranslationBulkResult]
