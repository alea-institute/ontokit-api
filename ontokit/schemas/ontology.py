"""Ontology schemas."""

from pydantic import BaseModel, Field


class LocalizedString(BaseModel):
    """A string with language tag."""

    value: str = Field(..., max_length=5000)
    lang: str = Field(default="en", max_length=10)
