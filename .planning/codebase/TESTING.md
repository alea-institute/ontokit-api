# Testing Patterns

**Analysis Date:** 2026-05-02

## Test Framework

**Runner:**
- Framework: pytest 8.3.0+
- Config: `pyproject.toml` under `[tool.pytest.ini_options]`
- Async mode: `asyncio_mode = "auto"` — automatically handles async test functions
- Test paths: `tests/` directory (unit, integration, etc.)

**Assertion Library:**
- Built-in pytest assertions: `assert condition`, `assert value == expected`
- Pytest provides rich assertion introspection and detailed diff output

**Run Commands:**
```bash
pytest tests/ -v --cov=ontokit --cov-report=term-missing    # Run all tests with coverage
pytest tests/unit/ -v                                         # Run unit tests only
pytest tests/unit/test_ontology_service.py -v                # Run single test file
pytest tests/ -k "test_name" -v                              # Run tests matching pattern
pytest tests/unit/test_ontology_service.py::TestLabelPref... # Run specific test class
```

## Test File Organization

**Location:**
- Unit tests: `tests/unit/` (co-located with conftest.py at `tests/unit/conftest.py`)
- Global fixtures: `tests/conftest.py` (root conftest)
- Naming: `test_<module>.py` mirrors source module name

**Example structure:**
```
tests/
├── conftest.py                          # Global fixtures
├── unit/
│   ├── conftest.py                      # Unit-specific fixtures
│   ├── test_ontology_service.py
│   ├── test_lint_routes.py
│   ├── test_auth.py
│   └── ...
└── integration/
    └── (if any)
```

**Naming:**
- Test files: `test_*.py`
- Test functions: `test_<what_is_being_tested>()`
- Test classes: `Test<ComponentName>` (groups related tests)

**Test Class Structure:**
- Organize tests using classes (e.g., `TestLabelPreferenceParse`, `TestSelectPreferredLabel`)
- Classes improve readability and allow class-level fixtures
- Example: `tests/unit/test_ontology_service.py` has `TestLabelPreferenceParse`, `TestSelectPreferredLabel`

## Test Structure

**Suite Organization:**
```python
"""Tests for the ontology service label preference parsing and selection."""

import pytest
from rdflib import Graph, Namespace

class TestLabelPreferenceParse:
    """Tests for LabelPreference.parse class method."""
    
    def test_label_preference_parse_with_lang(self) -> None:
        """'rdfs:label@en' parses to RDFS.label with language 'en'."""
        pref = LabelPreference.parse("rdfs:label@en")
        assert pref is not None
        assert pref.property_uri == RDFS.label
        assert pref.language == "en"

class TestSelectPreferredLabel:
    """Tests for the select_preferred_label function."""
    
    def test_select_preferred_label_english(self, graph_with_labels: Graph) -> None:
        """Selects English label when preferences request 'rdfs:label@en'."""
        result = select_preferred_label(graph_with_labels, EX.Person, preferences=["rdfs:label@en"])
        assert result == "Person"
```

**Patterns:**
- Module-level docstring: Describes what is being tested
- Fixture definitions: Use `@pytest.fixture` at module or class scope
- Setup/Teardown: Use fixtures instead of `setup()` / `teardown()` methods (prefer fixtures)
- Assertions: Use simple `assert` statements with descriptive test names

## Mocking

**Framework:** unittest.mock (standard library)

**Patterns:**

```python
# AsyncMock for async functions
from unittest.mock import AsyncMock, MagicMock, Mock

@pytest.fixture
def mock_db_session() -> AsyncMock:
    """Create an async mock of an SQLAlchemy AsyncSession."""
    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.execute = AsyncMock()
    session.refresh = AsyncMock()
    session.add = Mock()  # Synchronous method
    session.delete = AsyncMock()
    return session

# Service mocking with spec
@pytest.fixture
def mock_github_service() -> Mock:
    """Create a mock of the GitHubService with canned responses."""
    service = Mock(spec=GitHubService)
    service.get_authenticated_user = AsyncMock(return_value=("testuser", "repo,read:org"))
    service.list_user_repos = AsyncMock(return_value=[])
    return service

# Usage in test
@pytest.mark.asyncio
async def test_updates_existing_config(
    self, service: EmbeddingService, mock_db: AsyncMock
) -> None:
    """Updates an existing ProjectEmbeddingConfig."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing_config
    mock_db.execute.return_value = result
    
    await service.update_config(PROJECT_ID, update)
    mock_db.commit.assert_awaited_once()
```

**What to Mock:**
- External services: Database sessions, HTTP clients, Redis, storage backends
- Service dependencies: When testing service A that uses service B, mock B
- I/O operations: File reads, API calls
- Avoid mocking: Internal pure functions, dataclass/Pydantic models, business logic being tested

**What NOT to Mock:**
- Pydantic models (use real ones)
- Pure utility functions (test with real calls)
- The code under test itself
- Standard library types (except when truly external)

## Fixtures and Factories

**Test Data:**

Example from `tests/conftest.py`:
```python
@pytest.fixture
def sample_ontology_turtle() -> str:
    """Sample ontology in Turtle format."""
    return """
@prefix : <http://example.org/ontology#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
...
:Person rdf:type owl:Class ;
    rdfs:label "Person"@en ;
    rdfs:comment "A human being"@en .
"""

@pytest.fixture
def sample_graph(sample_ontology_turtle: str) -> Graph:
    """Parse the sample ontology Turtle string into an RDFLib Graph."""
    graph = Graph()
    graph.parse(data=sample_ontology_turtle, format="turtle")
    return graph

@pytest.fixture
def authenticated_user() -> CurrentUser:
    """Create an authenticated test user."""
    return CurrentUser(
        id="test-user-id",
        email="test@example.com",
        name="Test User",
        username="testuser",
        roles=["editor"],
    )
```

**Mock Factories:**

Example from `tests/unit/test_embedding_service.py`:
```python
def _make_config_row(
    *,
    provider: str = "local",
    model_name: str = "all-MiniLM-L6-v2",
    api_key_encrypted: str | None = None,
    dimensions: int = 384,
    auto_embed_on_save: bool = False,
    last_full_embed_at: datetime | None = None,
) -> MagicMock:
    """Create a mock ProjectEmbeddingConfig ORM object."""
    cfg = MagicMock()
    cfg.provider = provider
    cfg.model_name = model_name
    cfg.api_key_encrypted = api_key_encrypted
    cfg.dimensions = dimensions
    cfg.auto_embed_on_save = auto_embed_on_save
    cfg.last_full_embed_at = last_full_embed_at
    cfg.project_id = PROJECT_ID
    return cfg

# Usage
cfg = _make_config_row(provider="local", auto_embed_on_save=False)
```

**Location:**
- Global fixtures: `tests/conftest.py`
- Unit-specific fixtures: `tests/unit/conftest.py`
- Module-specific fixtures: In the test file itself (at module scope before test classes)
- Mock factories: Helper functions in test files (prefixed with underscore: `_make_config_row()`)

**Fixture Scope:**
- Default: function scope (recreated per test)
- Module scope: For expensive setup (e.g., `bare_git_repo` in conftest.py)
- Parameterization: Use `@pytest.mark.parametrize` to test multiple inputs

## Coverage

**Requirements:** 
- Configured in `pyproject.toml`: `--cov=ontokit --cov-report=term-missing`
- No minimum coverage enforced; measured automatically in CI

**View Coverage:**
```bash
pytest tests/ -v --cov=ontokit --cov-report=term-missing
```

## Test Types

**Unit Tests:**
- Scope: Test individual functions, classes, and methods in isolation
- Location: `tests/unit/`
- Mocking: Mock external dependencies; test business logic in detail
- Speed: Fast (< 100ms per test)
- Examples: `test_ontology_service.py`, `test_auth.py`, `test_embedding_service.py`
- Isolation: Each test is independent; can run in any order

**Integration Tests:**
- Scope: Test components working together (e.g., route + service + database)
- Location: Not heavily used in codebase; would go in `tests/integration/`
- Mocking: Minimal; use real service instances with mocked I/O (DB, Redis)
- Speed: Slower than unit tests (0.5–5s)
- Example: Full HTTP request → service → database round-trip

**E2E Tests:**
- Not detected in codebase
- Would test complete request workflows with real infrastructure

## Common Patterns

**Async Testing:**

```python
@pytest.mark.asyncio
async def test_creates_new_config_when_none_exists(
    self, service: EmbeddingService, mock_db: AsyncMock
) -> None:
    """Creates a new ProjectEmbeddingConfig when none exists."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = result

    update = MagicMock()
    update.provider = "local"
    
    await service.update_config(PROJECT_ID, update)
    mock_db.commit.assert_awaited_once()
```

**Error Testing:**

From `tests/unit/test_auth.py`:
```python
class TestExtractRoles:
    """Tests for the _extract_roles helper function."""

    def test_extract_roles_with_roles(self) -> None:
        """Payload with the Zitadel roles claim returns role names."""
        payload = {
            ZITADEL_ROLES_CLAIM: {
                "admin": {"org_123": "My Org"},
                "editor": {"org_123": "My Org"},
            },
        }
        roles = _extract_roles(payload)
        assert sorted(roles) == ["admin", "editor"]

    def test_extract_roles_empty(self) -> None:
        """Payload without a roles claim returns an empty list."""
        payload = {"sub": "user-1", "exp": 0}
        roles = _extract_roles(payload)
        assert roles == []

    def test_extract_roles_not_dict(self) -> None:
        """Non-dict roles claim value returns an empty list."""
        payload_str = {ZITADEL_ROLES_CLAIM: "not-a-dict"}
        assert _extract_roles(payload_str) == []
```

**HTTP Testing with TestClient:**

```python
# From tests/unit/conftest.py
@pytest.fixture
def authed_client() -> Generator[tuple[TestClient, AsyncMock], None, None]:
    """TestClient with mocked DB and authenticated user.
    
    Returns (client, mock_session) so tests can configure DB responses.
    """
    mock_session = AsyncMock(spec=AsyncSession)
    user = CurrentUser(...)
    
    app.dependency_overrides[get_db] = lambda: mock_session
    app.dependency_overrides[get_current_user] = lambda: user
    
    client = TestClient(app, raise_server_exceptions=False)
    yield client, mock_session
    
    app.dependency_overrides.clear()

# Usage in test
def test_create_ontology(authed_client: tuple[TestClient, AsyncMock]) -> None:
    client, mock_db = authed_client
    
    response = client.post(
        "/api/v1/ontologies",
        json={"iri": "http://example.org/ontology", "title": "Test"},
    )
    assert response.status_code == 201
```

**Real Git Repository Testing:**

```python
@pytest.fixture
def bare_git_repo(tmp_path: Path, sample_ontology_turtle: str) -> BareOntologyRepository:
    """Create a real pygit2 bare repo with an initial Turtle commit."""
    repo_path = tmp_path / "test-project.git"
    raw_repo = pygit2.init_repository(str(repo_path), bare=True)
    raw_repo.set_head("refs/heads/main")

    repo = BareOntologyRepository(repo_path)
    repo.write_file(
        branch_name="main",
        filepath="ontology.ttl",
        content=sample_ontology_turtle.encode(),
        message="Initial commit",
        author_name="Test User",
        author_email="test@example.com",
    )
    return repo
```

## Test Authoring Conventions

**Test Naming:**
- Test name describes the behavior being tested (use when/then style):
  - `test_select_preferred_label_english` — selects English label
  - `test_label_preference_parse_with_lang` — parses with language tag
  - `test_returns_none_when_no_config` — returns None when resource missing
  - `test_creates_new_config_when_none_exists` — creates new when absent

**Docstrings:**
- Every test function has a one-line docstring describing expected behavior
- Use passive voice: "Returns...", "Raises...", "Creates...", "Selects..."
- Example: `"""Returns an EmbeddingConfig when config exists."""`

**Assertions:**
- One logical assertion per test (multiple assertions okay if testing same outcome)
- Descriptive assertion messages using `assert ... , "message"` when non-obvious
- Use mock assertion methods: `mock_db.commit.assert_awaited_once()`
- Use pytest helpers: `assert value in iterable`, `assert value == expected`

**Return Type Hints:**
- All test functions should have `-> None` return type hint
- Example: `def test_extract_roles_with_roles(self) -> None:`

---

*Testing analysis: 2026-05-02*
