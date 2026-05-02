# Coding Conventions

**Analysis Date:** 2026-05-02

## Naming Patterns

**Files:**
- Module files: lowercase with underscores (e.g., `ontology_service.py`, `bare_repository.py`)
- Test files: `test_*.py` prefix (e.g., `test_ontology_service.py`)
- Naming mirrors module structure: `ontokit/services/ontology.py` → `tests/unit/test_ontology_service.py`

**Functions:**
- Lowercase with underscores: `select_preferred_label()`, `create_ontology()`, `_resolve_ref()`
- Private functions (module scope): prefix with underscore: `_extract_roles()`, `_get_signature()`, `_validate_iri()`
- Dependency injectors use `get_*` pattern: `get_ontology_service()`, `get_github_service()`, `get_user_service()`

**Variables:**
- Lowercase with underscores: `graph_with_labels`, `mock_db_session`, `author_email`
- Constants (module-level): UPPERCASE: `LABEL_PROPERTY_MAP`, `FORMAT_MAP`, `DEFAULT_LABEL_PREFERENCES`, `ANNOTATION_PROPERTIES`
- Private module constants: underscore prefix: `_JWKS_CACHE_TTL`, `_IRI_PATTERN`
- Loop counters and comprehensions: single letters acceptable: `for item in items`

**Types and Classes:**
- PascalCase: `CurrentUser`, `TokenPayload`, `OntologyResponse`, `LintResult`
- Dataclasses and Pydantic models: PascalCase with full naming: `LabelPreference`, `CommitInfo`, `FileChange`, `DiffInfo`
- Exception classes: PascalCase ending with `Error`: `OntoKitError`, `NotFoundError`, `ValidationError`, `ConflictError`, `ForbiddenError`

**Type hints:**
- Use `from typing import Annotated` for dependency injection: `Annotated[OntologyService, Depends(get_ontology_service)]`
- Use `str | None` syntax (PEP 604) over `Optional[str]`
- Union types: `list[str] | None`, `dict[str, Any] | None`
- Literal types: `from typing import Literal as TypingLiteral` to avoid confusion with RDF `Literal`

## Code Style

**Formatting:**
- Tool: Ruff (via `ruff format`)
- Line length: 100 characters (configured in `pyproject.toml`)
- Ruff ignores E501 (line too long) — auto-wrapping is preferred over line-length enforcement

**Linting:**
- Tool: Ruff (via `ruff check --fix`)
- Enabled rules: E, W, F, I, B, C4, UP, ARG, SIM
  - E: pycodestyle errors (whitespace, indentation)
  - W: pycodestyle warnings
  - F: Pyflakes (undefined names, unused imports)
  - I: isort (import ordering)
  - B: flake8-bugbear (common bugs)
  - C4: flake8-comprehensions (list/dict/set syntax)
  - UP: pyupgrade (modern Python syntax)
  - ARG: flake8-unused-arguments (unused parameters)
  - SIM: flake8-simplify (code simplification)
- Type checking: mypy strict mode enabled
- Known first-party: `ontokit` (for isort grouping)

**Special conventions:**
- Use `# noqa: <rule>` sparingly, only when necessary (e.g., `# noqa: ARG002` for intentional unused fixture parameters)
- Ruff config in `pyproject.toml` under `[tool.ruff]` and `[tool.ruff.lint]`

## Import Organization

**Order:**
1. `from __future__ import annotations` (if needed for PEP 563 forward references)
2. Standard library (`import logging`, `from pathlib import Path`, `from typing import ...`)
3. Third-party (`import pytest`, `from fastapi import ...`, `from rdflib import ...`)
4. Local/first-party (`from ontokit.services.ontology import ...`, `from ontokit.models.project import ...`)
5. Relative imports (rare; use full paths instead)

**Path Aliases:**
- No path aliases in use; full import paths from `ontokit` package root are standard
- Example: `from ontokit.services.ontology import OntologyService`
- Example: `from ontokit.models.project import Project, ProjectMember`

**Module docstrings:**
- First line: brief description of module purpose (e.g., `"""Authentication and authorization utilities."""`)
- Extended docstrings for complex modules include additional context (e.g., see `ontokit/git/bare_repository.py`)

## Error Handling

**Patterns:**
- Domain errors: Custom exception hierarchy inheriting from `OntoKitError` (`NotFoundError`, `ValidationError`, `ConflictError`, `ForbiddenError`)
- All custom exceptions accept `message` (required) and optional `detail` kwargs
- HTTP errors: `raise HTTPException(status_code=404, detail="Resource not found")`
- Return `None` for missing resources in service layers, translate to HTTPException in routes
- Error classes defined in `ontokit/core/exceptions.py`

**Example pattern:**
```python
# Service layer — return None or raise domain exception
def get(resource_id: UUID) -> Resource | None:
    return await db.get(resource_id)

# Route layer — translate to HTTP response
async def get_resource(resource_id: UUID, ...) -> ResourceResponse:
    resource = await service.get(resource_id)
    if not resource:
        raise HTTPException(status_code=404, detail="Resource not found")
    return resource
```

## Logging

**Framework:** Python `logging` module (standard library)

**Patterns:**
- Module-level logger: `logger = logging.getLogger(__name__)` at file top after imports
- Log levels used: `logger.debug()`, `logger.info()`, `logger.exception()`
- Use positional formatting: `logger.info("Starting %s (env=%s)", component, env)` not f-strings
- Exception logging: `logger.exception()` preserves stack trace; used in except blocks
- Example locations: `ontokit/main.py`, `ontokit/api/routes/analytics.py`, `ontokit/api/dependencies.py`

## Comments

**When to Comment:**
- Explain "why", not "what" — code should be readable without comments
- Document non-obvious business logic: why a particular algorithm is used
- Warn about gotchas: e.g., threading concerns, cache invalidation, ref resolution edge cases
- Example: `# Ensure HEAD points to refs/heads/main regardless of system git config`

**Docstrings:**
- Module docstrings: Brief description + extended context if needed (first line + blank line + details)
- Function/method docstrings: Describe parameters, return type, and important behavior
- Use Google-style docstrings (seen in `auth.py`):
  ```python
  def function(arg: str) -> bool:
      """Brief description.
      
      Args:
          arg: Description of argument.
      
      Returns:
          Description of return value.
      """
  ```
- Class docstrings: Describe the class purpose; document constructor args if complex
- Dataclass/Pydantic model docstrings: Brief description; field docstrings via Field descriptions

**Example locations:**
- `ontokit/core/auth.py`: Function docstrings with Args/Returns sections
- `ontokit/git/bare_repository.py`: Module docstring + class docstrings + method docstrings
- `ontokit/services/ontology.py`: Function docstrings explaining label preference logic

## Function Design

**Size:** 
- Target 20–50 lines per function (soft guideline)
- Extract helper functions for complex steps (e.g., `_resolve_ref()` in bare_repository.py)
- Keep async functions focused on I/O coordination; push business logic to pure functions

**Parameters:**
- Use `Annotated` for dependency-injected parameters in FastAPI routes
- Use keyword-only arguments for optional flags: `def function(..., *, force_refresh: bool = False)`
- Keep parameter count low; use dataclass/Pydantic model for related parameters

**Return Values:**
- Prefer explicit types: `-> Resource | None` not bare `-> Any`
- Return `None` for missing resources (service layer); translate to HTTP errors in routes
- Async functions return coroutines: `async def function() -> Resource`
- Consistency: If a function can return None, document it in docstring and type hint

**Example pattern (from `ontokit/core/auth.py`):**
```python
async def get_jwks(*, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch and cache the JWKS from Zitadel.
    
    Args:
        force_refresh: If True, bypass the cache TTL and fetch fresh keys.
    """
    # Implementation
```

## Module Design

**Exports:**
- No `__all__` used; imports from `ontokit.*` are explicit by file path
- Modules export classes, functions, and constants defined within them
- Example: `from ontokit.services.ontology import OntologyService, select_preferred_label, DEFAULT_LABEL_PREFERENCES`

**Barrel Files:**
- `ontokit/__init__.py`: Exports `__version__`
- `ontokit/api/routes/__init__.py`: Re-exports router for main.py
- Minimal re-exports; avoid deep nesting of imports

**Module Organization:**
- Keep related functionality together: `ontology_service.py` has service class + helper functions + module constants
- Separate concerns: schemas in `ontokit/schemas/`, models in `ontokit/models/`, services in `ontokit/services/`
- Utility functions: Grouped at module scope before class definitions

**Dataclass and Pydantic usage:**
- Dataclasses: For simple data holders with no validation (e.g., `CommitInfo`, `FileChange`, `DiffInfo`)
- Pydantic models: For request/response schemas with validation (e.g., `OntologyResponse`, `CurrentUser`)
- Pydantic `model_config = ConfigDict(from_attributes=True)`: Used for ORM-to-schema conversion
- Field validators: Use `@field_validator()` classmethod decorator (Pydantic v2)

## Async/Await Patterns

**Async-first approach:**
- All I/O operations are async: database queries, HTTP calls, file operations
- Service methods accepting `AsyncSession` are async
- Route handlers are async
- Use `async with`, `async for` appropriately

**Example (from routes/ontologies.py):**
```python
@router.get("/{ontology_id}")
async def get_ontology(
    ontology_id: UUID,
    service: Annotated[OntologyService, Depends(get_ontology_service)],
) -> Response:
    """Get an ontology by ID."""
    ontology = await service.get(ontology_id)
    if not ontology:
        raise HTTPException(status_code=404, detail="Ontology not found")
    return Response(content=content, media_type=media_type)
```

## FastAPI Dependency Injection

**Pattern:**
- Service accessor functions: `get_ontology_service()`, `get_github_service()`
- Used with `Annotated[ServiceType, Depends(get_service_func)]`
- Dependency functions return service instances (often singletons)
- Override dependencies in tests via `app.dependency_overrides[get_db] = override_func`

**Example (from routes/ontologies.py):**
```python
def get_ontology_service() -> OntologyService:
    """Dependency to get ontology service."""
    return OntologyService()

@router.post("")
async def create_ontology(
    ontology: OntologyCreate,
    service: Annotated[OntologyService, Depends(get_ontology_service)],
) -> OntologyResponse:
    return await service.create(ontology)
```

## Datetime Handling

**Convention:** All datetimes are UTC-aware
- Use `from datetime import UTC, datetime`
- Default to UTC: `datetime.now(UTC)` or `datetime.utcnow()`
- Pydantic model fields: `created_at: datetime` with no default timezone assumption
- Never use naive datetimes; always include timezone info

---

*Convention analysis: 2026-05-02*
