#!/usr/bin/env python3
"""Provision, refresh, and reindex the two resettable OntoKit demo projects."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import stat
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from ontokit.core.config import settings
from ontokit.core.demo_targets import DEMO_REPOSITORY_PAIRS
from ontokit.git.bare_repository import BareGitRepositoryService, BareOntologyRepository
from ontokit.models.project import Project, get_git_ontology_path
from ontokit.services.demo_project_provisioning import ensure_demo_projects
from ontokit.services.ontology import get_ontology_service
from ontokit.services.ontology_index import OntologyIndexService
from ontokit.services.storage import get_storage_service


def refuse(message: str) -> NoReturn:
    print(f"refused: {message}", file=sys.stderr)
    raise SystemExit(64)


def validate_manifest(path: Path) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        refuse(f"cannot read valid mirror manifest: {exc}")
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        refuse("mirror manifest must use schema_version 1")
    entries = data.get("mirrors")
    if not isinstance(entries, list):
        refuse("mirror manifest must contain a mirrors list")
    observed: set[tuple[tuple[str, str], tuple[str, str]]] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            refuse("mirror entries must be objects")
        try:
            source = tuple(str(entry["source_repository"]).lower().split("/", 1))
            destination = tuple(str(entry["destination_repository"]).lower().split("/", 1))
        except KeyError as exc:
            refuse(f"mirror entry is missing {exc.args[0]}")
        if len(source) != 2 or len(destination) != 2:
            refuse("mirror repositories must be owner/name pairs")
        observed.add((source, destination))
    if observed != set(DEMO_REPOSITORY_PAIRS.items()):
        refuse("mirror manifest does not match the immutable demo repository contract")


def read_demo_token(token_file: Path | None) -> str:
    if token_file is None:
        token = settings.github_demo_mirror_token
    else:
        try:
            mode = token_file.stat().st_mode
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                refuse("demo token file must not grant group or other permissions")
            token = token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            refuse(f"cannot read demo token file: {exc}")
    if not token:
        refuse("dedicated demo mirror credential is not configured")
    return token


def _safe_demo_repo_path(base: Path, project_id: uuid.UUID) -> Path:
    base = base.resolve()
    target = (base / f"{project_id}.git").resolve()
    if target.parent != base or target.name != f"{project_id}.git":
        refuse("resolved demo repository path escaped GIT_REPOS_BASE_PATH")
    return target


@contextmanager
def refreshed_repository(
    git_service: BareGitRepositoryService,
    project_id: uuid.UUID,
    repository: str,
    token: str,
) -> Iterator[None]:
    """Swap in a fresh bare clone and restore the prior clone on failure."""
    base = git_service.base_path.resolve()
    base.mkdir(parents=True, exist_ok=True)
    target = _safe_demo_repo_path(base, project_id)
    suffix = uuid.uuid4().hex
    staging = base / f".{project_id}.refresh-{suffix}.git"
    backup = base / f".{project_id}.previous-{suffix}.git"
    if staging.exists() or backup.exists():
        refuse("generated demo refresh paths unexpectedly exist")

    BareOntologyRepository.clone_bare(
        f"https://github.com/{repository}.git",
        staging,
        token,
    )
    had_target = target.exists()
    try:
        if had_target:
            target.rename(backup)
        staging.rename(target)
        yield
    except BaseException:
        if target.exists():
            shutil.rmtree(target)
        if backup.exists():
            backup.rename(target)
        raise
    else:
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


async def resync(manifest: Path, token_file: Path | None) -> None:
    validate_manifest(manifest)
    token = read_demo_token(token_file)
    engine = create_async_engine(str(settings.database_url))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    git_service = BareGitRepositoryService()

    try:
        async with sessions() as db:
            provisioned = await ensure_demo_projects(db)
            for item in provisioned:
                project_result = await db.execute(
                    select(Project)
                    .options(selectinload(Project.github_integration))
                    .where(Project.id == item.project_id)
                )
                project = project_result.scalar_one()
                integration = project.github_integration
                if integration is None:
                    refuse(f"demo project {project.id} has no GitHub integration")
                branch = integration.default_branch or "main"
                with refreshed_repository(
                    git_service,
                    project.id,
                    item.destination_repository,
                    token,
                ):
                    repository = git_service.get_repository(project.id)
                    commit_hash = repository.get_branch_commit_hash(branch)
                    ontology = get_ontology_service(get_storage_service())
                    graph = await ontology.load_from_git(
                        project.id,
                        branch,
                        get_git_ontology_path(project),
                        git_service,
                    )
                    count = await OntologyIndexService(db).full_reindex(
                        project.id,
                        branch,
                        graph,
                        commit_hash,
                    )
                print(
                    f"demo_project={project.id} repository={item.destination_repository} "
                    f"commit={commit_hash} entities={count} created={str(item.created).lower()}"
                )
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path(os.environ["ONTOKIT_DEMO_TOKEN_FILE"])
        if os.environ.get("ONTOKIT_DEMO_TOKEN_FILE")
        else None,
    )
    args = parser.parse_args()
    asyncio.run(resync(args.manifest, args.token_file))


if __name__ == "__main__":
    main()
