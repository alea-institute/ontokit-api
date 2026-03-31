"""OntoKit FOLIO Adapter - Main FastAPI application."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ontokit.folio_loader import load_folio
from ontokit.api.projects import router as projects_router
from ontokit.api.health import router as health_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load FOLIO ontology at startup."""
    logger.info("Loading FOLIO ontology...")
    app.state.folio = load_folio()
    class_count = len(app.state.folio.classes)
    prop_count = len(app.state.folio.object_properties)
    logger.info("FOLIO loaded: %d classes, %d properties", class_count, prop_count)
    yield
    logger.info("Shutting down.")


app = FastAPI(
    title="OntoKit FOLIO API",
    description="FOLIO ontology served via OntoKit-compatible API",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS - allow the Next.js frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount routes
app.include_router(health_router)
app.include_router(projects_router, prefix="/api/v1")
