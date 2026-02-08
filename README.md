# Axigraph API

The backend API for Axigraph - a collaborative OWL ontology curation platform.

## Features

- RESTful API for ontology management (classes, properties, individuals)
- Real-time collaboration via WebSocket
- OAuth 2.0 authentication with Zitadel (including Device Flow for desktop apps)
- Git-based version control with semantic diff
- Content negotiation for multiple RDF formats (Turtle, RDF/XML, JSON-LD, etc.)
- OpenAPI documentation with auto-generated SDKs

## Tech Stack

- **Framework**: FastAPI (Python 3.11+)
- **Ontology Processing**: RDFLib, Owlready2
- **Database**: PostgreSQL (async with asyncpg)
- **Cache/Queue**: Redis
- **Object Storage**: MinIO (S3-compatible)
- **Version Control**: Git (GitPython)

## Quick Start

### Prerequisites

- Python 3.11+
- PostgreSQL 16+
- Redis 7+
- MinIO (optional, for file storage)

### Development Setup

```bash
# Clone the repository
git clone https://github.com/axigraph/axigraph-api.git
cd axigraph-api

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -e ".[dev]"

# Copy environment configuration
cp .env.example .env
# Edit .env with your settings

# Run the development server
uvicorn app.main:app --reload
```

The API will be available at http://localhost:8000

### API Documentation

- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc
- OpenAPI spec: http://localhost:8000/openapi.json

## Docker

```bash
# Build the image
docker build -t axigraph-api .

# Run the container
docker run -p 8000:8000 --env-file .env axigraph-api
```

## Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=app --cov-report=html

# Run specific test file
pytest tests/unit/test_health.py
```

## Project Structure

```
axigraph-api/
├── app/
│   ├── api/v1/           # API route handlers
│   ├── core/             # Configuration, dependencies
│   ├── models/           # SQLAlchemy database models
│   ├── schemas/          # Pydantic request/response schemas
│   ├── services/         # Business logic
│   ├── collab/           # Real-time collaboration
│   └── git/              # Git version control
├── tests/
│   ├── unit/
│   └── integration/
├── scripts/              # Utility scripts
├── Dockerfile
├── pyproject.toml
└── README.md
```

## API Endpoints

### Ontologies
- `GET /api/v1/ontologies` - List ontologies
- `POST /api/v1/ontologies` - Create ontology
- `GET /api/v1/ontologies/{id}` - Get ontology (content negotiation)
- `PUT /api/v1/ontologies/{id}` - Update ontology
- `DELETE /api/v1/ontologies/{id}` - Delete ontology

### Classes
- `GET /api/v1/ontologies/{id}/classes` - List classes
- `POST /api/v1/ontologies/{id}/classes` - Create class
- `GET /api/v1/ontologies/{id}/classes/{iri}` - Get class
- `PUT /api/v1/ontologies/{id}/classes/{iri}` - Update class
- `DELETE /api/v1/ontologies/{id}/classes/{iri}` - Delete class

### Authentication
- `POST /api/v1/auth/device/code` - Request device code (for desktop apps)
- `POST /api/v1/auth/device/token` - Poll for token
- `POST /api/v1/auth/token/refresh` - Refresh access token

### WebSocket Collaboration
- `WS /ws/collab` - Real-time collaboration endpoint

## License

MIT License - see LICENSE file for details.
