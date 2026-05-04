# Code Style & Conventions — ontokit-api

## Formatting
- **Line length**: 100 characters
- **Formatter**: `ruff format` (enforced by pre-commit)
- **Target**: Python 3.11

## Linting (ruff)
Selected rule sets:
- `E`, `W` — pycodestyle errors/warnings
- `F` — Pyflakes
- `I` — isort (`known-first-party = ["ontokit"]`)
- `B` — flake8-bugbear
- `C4` — flake8-comprehensions
- `UP` — pyupgrade
- `ARG` — flake8-unused-arguments
- `SIM` — flake8-simplify

Ignored: `E501` (line length handled by formatter, not linter).

## Type checking
- **mypy** in **strict mode** (`strict = true`)
- `warn_return_any = true`, `warn_unused_ignores = true`
- Plugin: `pydantic.mypy`
- Pyright also configured (uses `.venv`, py 3.11)

## Pydantic conventions
- Pydantic v2 (>=2.13.3, <2.14)
- `init_forbid_extra = true`, `init_typed = true`
- Strict validation everywhere, computed fields where appropriate

## Architectural patterns
- **Async-first**: all I/O uses async/await
- **Dependency injection**: FastAPI's `Depends()`
- **Service singletons**: obtained via `get_service_name()` dependency providers
- **UTC-aware datetimes** throughout (no naive datetimes)
- **Layered**: routes → services → models / schemas / core

## URL versioning
The `/api/v1/` prefix is set in `main.py` router registration — do NOT recreate the version in the directory tree.

## Git module guideline
Use `ontokit/git/bare_repository.py` (pygit2-based) for new code.
The GitPython-based `repository.py` is **deprecated** and kept only for backward compat.

## Pre-commit
Enabled hooks: ruff (lint + format) and mypy. Installed via `make setup`.

## Testing conventions
- Pytest with `asyncio_mode = "auto"` (no need for `@pytest.mark.asyncio`)
- `testpaths = ["tests"]`
- Default args: `-v --cov=ontokit --cov-report=term-missing`
- Layout: `tests/unit/` and `tests/integration/`
