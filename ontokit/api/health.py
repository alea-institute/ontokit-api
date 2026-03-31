"""Health check endpoint."""

from fastapi import APIRouter, Request

router = APIRouter()

FOLIO_PROJECT_ID = "00000000-0000-0000-0000-000000000001"


@router.get("/health")
async def health(request: Request):
    folio = request.app.state.folio
    return {
        "status": "healthy",
        "ontology": "FOLIO",
        "classes": len(folio.classes),
        "properties": len(folio.object_properties),
    }


@router.get("/")
async def root():
    return {
        "name": "OntoKit FOLIO API",
        "version": "0.1.0",
        "docs": "/docs",
    }
