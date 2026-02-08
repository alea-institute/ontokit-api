"""Pydantic schemas for request/response validation."""

from app.schemas.auth import DeviceCodeRequest, DeviceCodeResponse, TokenRequest, TokenResponse
from app.schemas.ontology import OntologyCreate, OntologyResponse, OntologyUpdate
from app.schemas.owl_class import OWLClassCreate, OWLClassResponse, OWLClassUpdate
from app.schemas.owl_property import OWLPropertyCreate, OWLPropertyResponse, OWLPropertyUpdate

__all__ = [
    "DeviceCodeRequest",
    "DeviceCodeResponse",
    "TokenRequest",
    "TokenResponse",
    "OntologyCreate",
    "OntologyResponse",
    "OntologyUpdate",
    "OWLClassCreate",
    "OWLClassResponse",
    "OWLClassUpdate",
    "OWLPropertyCreate",
    "OWLPropertyResponse",
    "OWLPropertyUpdate",
]
