# Suggested Commands — ontokit-api

## First-time setup
```bash
make setup              # uv sync --extra dev + pre-commit install
cp .env.example .env    # then edit .env
./scripts/setup-zitadel.sh --update-env   # provision Zitadel OIDC apps
```

## Dev server
```bash
uvicorn ontokit.main:app --reload
ontokit --reload                    # equivalent CLI (installed entry point)
```

## Docker (full stack)
```bash
docker compose up -d                                  # full local stack
docker compose -f compose.prod.yaml up -d             # infra only (hybrid mode)
docker compose exec api alembic upgrade head          # migrate inside container
docker compose up -d --force-recreate api worker      # restart after .env change
```

## Linting / Formatting / Type checking
```bash
make lint        # uv run ruff check ontokit/ tests/ --fix
make format      # uv run ruff format ontokit/ tests/
make typecheck   # uv run mypy ontokit/

# raw equivalents:
ruff check ontokit/ --fix
ruff format ontokit/
mypy ontokit/
```

## Tests
```bash
make test                                          # full suite w/ coverage
pytest tests/ -v --cov=ontokit                     # explicit
pytest tests/unit/test_health.py -v                # single file
pytest tests/ -k "test_name" -v                    # by keyword
```

## Security scan (Semgrep)
With Pro:
```bash
semgrep --pro --config p/default --config p/owasp-top-ten --config p/python --config p/fastapi --config p/jwt
```
Without Pro: drop `--pro`.

## DB migrations
```bash
alembic upgrade head
alembic downgrade -1
alembic revision --autogenerate -m "description"
```

## Build / Publish
```bash
uv build
uv run twine check --strict dist/*
uv publish
```

## Release flow
```bash
python scripts/prepare-release.py          # strip -dev suffix, commit
git tag -s ontokit-X.Y.Z
git push --tags                            # CI/CD publishes
python scripts/set-version.py X.Y.Z        # set next dev version (adds -dev)
```

## Migration: old → bare git repos
```bash
python scripts/migrate_to_bare_repos.py --dry-run
python scripts/migrate_to_bare_repos.py
python scripts/migrate_to_bare_repos.py --keep-old
```

## System utilities (Linux/WSL2)
Standard GNU coreutils. `cd`, `ls`, `grep`, `find`, `git` behave normally.
Prefer Serena's `find_file`, `search_for_pattern`, `find_symbol` over shell `find`/`grep` when working inside the repo.
