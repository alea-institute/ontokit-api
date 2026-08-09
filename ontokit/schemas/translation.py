"""Public and write-only schemas for project translation settings."""

from __future__ import annotations

import re
from enum import StrEnum

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
