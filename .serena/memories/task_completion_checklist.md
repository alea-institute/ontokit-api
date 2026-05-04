# When a Coding Task Is Complete — ontokit-api

Run these BEFORE declaring work done or committing:

1. **Lint + auto-fix**
   ```bash
   make lint            # or: ruff check ontokit/ tests/ --fix
   ```

2. **Format**
   ```bash
   make format          # or: ruff format ontokit/ tests/
   ```

3. **Type check (strict mypy)**
   ```bash
   make typecheck       # or: mypy ontokit/
   ```

4. **Tests with coverage**
   ```bash
   make test            # or: pytest tests/ -v --cov=ontokit
   ```

5. **(Optional/CI) Security scan**
   ```bash
   semgrep --pro --config p/default --config p/owasp-top-ten --config p/python --config p/fastapi --config p/jwt
   # drop --pro if no Pro entitlement
   ```

6. **DB schema changes** — generate + commit a migration:
   ```bash
   alembic revision --autogenerate -m "description"
   alembic upgrade head    # verify it applies cleanly
   ```

7. **Pre-commit** runs ruff + mypy automatically on commit (installed via `make setup`).
   Don't bypass with `--no-verify` unless explicitly authorized.

8. **CI** runs `semgrep ci` (diff-aware) — keep `.semgrepignore` honest.

## Release-specific
For a release, follow `RELEASING.md`:
```bash
python scripts/prepare-release.py
git tag -s ontokit-X.Y.Z
git push --tags
python scripts/set-version.py X.Y.Z
```
