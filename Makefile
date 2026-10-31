.PHONY: setup lint format typecheck test

## First-time project setup: install dependencies and pre-commit hooks
setup:
	uv sync --group dev
	uv run pre-commit install

## Run linter with auto-fix
lint:
	uv run ruff check ontokit/ tests/ --fix

## Format code
format:
	uv run ruff format ontokit/ tests/

## Run type checker
typecheck:
	bash scripts/typecheck.sh

## Run tests with coverage
test:
	bash scripts/memory-cap.sh uv run pytest tests/ -v --cov=ontokit $(PYTEST_ARGS)
