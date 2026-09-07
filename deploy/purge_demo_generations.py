#!/usr/bin/env python3
"""Inspect or purge retired demo content using the worker's retention service."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from typing import Any, NoReturn

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ontokit.core.config import settings
from ontokit.models.demo_generation import DemoGeneration
from ontokit.services.demo_project_provisioning import demo_generation_attempt_lease
from ontokit.services.demo_retention import DemoRetentionService, PurgeReceipt


def refuse(message: str) -> NoReturn:
    print(f"refused: {message}", file=sys.stderr)
    raise SystemExit(64)


def print_json(value: Any) -> None:
    print(json.dumps(value, default=str, sort_keys=True, indent=2))


async def status(db: AsyncSession) -> dict[str, Any]:
    """Read content counts and the latest historical failure, even after a retry."""
    generations = (await db.execute(select(DemoGeneration))).scalars().all()
    purge_times = [g.purged_at for g in generations if g.purged_at is not None]
    failures: list[tuple[datetime, dict[str, Any]]] = []
    for generation in generations:
        if generation.purge_receipt is None:
            continue
        receipt = PurgeReceipt.model_validate_json(generation.purge_receipt)
        for attempt in receipt.attempts:
            if attempt.outcome == "failed":
                timestamp = attempt.finished_at or attempt.started_at
                failures.append(
                    (
                        timestamp,
                        {
                            "generation_key": receipt.generation_key,
                            "failed_at": timestamp.isoformat(),
                            "failure_class": attempt.failure_class,
                        },
                    )
                )
    return {
        "retained_count": len(generations) - len(purge_times),
        "purged_count": len(purge_times),
        "last_purged_at": max(purge_times).isoformat() if purge_times else None,
        "last_failure": max(failures, key=lambda item: item[0])[1] if failures else None,
    }


async def run(*, apply: bool, show_status: bool, env: str | None) -> int:
    # Guard before engine creation, including for callers bypassing main().
    if apply and (env is None or env != settings.app_env):
        refuse(f"--apply requires --env {settings.app_env} to match the running configuration")
    engine = create_async_engine(str(settings.database_url), hide_parameters=True)

    @asynccontextmanager
    async def lease() -> AsyncIterator[None]:
        async with engine.connect() as connection, demo_generation_attempt_lease(connection):
            yield

    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            if show_status:
                print_json(await status(db))
                return 0
            service = DemoRetentionService(db, lease_factory=lease)
            if not apply:
                plan = asdict(await service.plan())
                for entry in plan["eligible"]:
                    entry["reason"] = "eligible"
                print_json(plan)
                return 0
            summary = await service.apply()
            attempted = summary.purged + summary.failed + summary.yielded
            generations = (
                (
                    await db.execute(
                        select(DemoGeneration)
                        .where(DemoGeneration.generation_key.in_(attempted))
                        .execution_options(populate_existing=True)
                    )
                )
                .scalars()
                .all()
            )
            receipts = [
                PurgeReceipt.model_validate_json(g.purge_receipt).model_dump(mode="json")
                for g in generations
                if g.purge_receipt is not None
            ]
            print_json({"summary": asdict(summary), "receipts": receipts})
            return 1 if summary.failed else 0
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="print the plan without changes")
    mode.add_argument("--status", action="store_true", help="read persisted purge status")
    mode.add_argument("--apply", action="store_true", help="purge eligible demo content")
    parser.add_argument("--env", help="running APP_ENV: development, staging, or production")
    args = parser.parse_args(argv)
    raise SystemExit(asyncio.run(run(apply=args.apply, show_status=args.status, env=args.env)))


if __name__ == "__main__":
    main()
