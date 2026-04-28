# OntoKit API — Project Overview

Collaborative OWL ontology curation API built with **FastAPI** (Python 3.11+, target 3.13). Distributed as the `ontokit` package on PyPI.

## Purpose
Provides a RESTful API for managing ontologies, semantic web knowledge graphs, and team collaboration with git-based version control. Sister project to `ontokit-web` (frontend).

## Core Capabilities
- REST endpoints for ontologies, classes, properties, individuals
- Project management (public/private visibility, member roles)
- Git-based version control with branches + PR workflow (pygit2 bare repos for concurrent access)
- 20+ ontology linting/validation rules
- Semantic search via `sentence-transformers` + `pgvector`
- Real-time collaboration over WebSockets
- Background job queue (ARQ + Redis)
- GitHub App integration for syncing remote repos

## Tech Stack
- **Framework**: FastAPI, async-first
- **Database**: PostgreSQL 17 + SQLAlchemy 2.0 (async via asyncpg) + Alembic migrations
- **Cache/Queue**: Redis 7 + ARQ
- **Object Storage**: MinIO (S3-compatible)
- **Auth**: Zitadel (OIDC/OAuth2, JWT validation)
- **RDF**: RDFLib 7.1+, OWLReady2
- **Git**: pygit2 (bare repos); legacy GitPython implementation deprecated
- **Validation**: Pydantic v2 (strict mode)
- **Package Mgmt**: uv

## Repo Location

Companion repos in same parent directory: `ontokit-web` (frontend), `folio-api`, `ontokit-api.wiki`.
