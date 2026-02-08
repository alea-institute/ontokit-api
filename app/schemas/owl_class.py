"""OWL Class schemas."""

from pydantic import BaseModel, Field, HttpUrl

from app.schemas.ontology import LocalizedString


class OWLClassBase(BaseModel):
    """Base OWL class fields."""

    iri: HttpUrl = Field(..., description="The class IRI")
    labels: list[LocalizedString] = Field(default_factory=list)
    comments: list[LocalizedString] = Field(default_factory=list)
    deprecated: bool = False


class OWLClassCreate(OWLClassBase):
    """Schema for creating an OWL class."""

    parent_iris: list[HttpUrl] = Field(
        default_factory=lambda: [],
        description="Parent class IRIs (subClassOf)",
    )
    equivalent_iris: list[HttpUrl] = Field(
        default_factory=list,
        description="Equivalent class IRIs",
    )
    disjoint_iris: list[HttpUrl] = Field(
        default_factory=list,
        description="Disjoint class IRIs",
    )


class OWLClassUpdate(BaseModel):
    """Schema for updating an OWL class."""

    labels: list[LocalizedString] | None = None
    comments: list[LocalizedString] | None = None
    deprecated: bool | None = None
    parent_iris: list[HttpUrl] | None = None
    equivalent_iris: list[HttpUrl] | None = None
    disjoint_iris: list[HttpUrl] | None = None


class OWLClassResponse(OWLClassBase):
    """Schema for OWL class responses."""

    parent_iris: list[str] = Field(default_factory=list)
    equivalent_iris: list[str] = Field(default_factory=list)
    disjoint_iris: list[str] = Field(default_factory=list)
    child_count: int = 0
    instance_count: int = 0
    is_defined: bool = True  # vs just declared
    source_ontology: str | None = None  # If imported

    class Config:
        from_attributes = True


class OWLClassListResponse(BaseModel):
    """List of OWL classes."""

    items: list[OWLClassResponse]
    total: int
