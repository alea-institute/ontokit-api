#!/usr/bin/env python3
"""Enqueue and await the operator-only PR Party credential rewrap task.

Dry-run is the default. Applying the rewrap requires an explicit confirmation
phrase, and both modes use stable ARQ job ids so concurrent duplicate requests
collapse instead of running twice.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any
from uuid import UUID

from arq import create_pool

from ontokit.core.constants import (
    PR_PARTY_CREDENTIAL_REWRAP_APPLY_JOB_ID,
    PR_PARTY_CREDENTIAL_REWRAP_CONFIRMATION,
    PR_PARTY_CREDENTIAL_REWRAP_DRY_RUN_JOB_ID,
    PR_PARTY_CREDENTIAL_REWRAP_TASK,
)
from ontokit.core.redis import get_redis_settings

TASK_NAME = PR_PARTY_CREDENTIAL_REWRAP_TASK
DRY_RUN_JOB_ID = PR_PARTY_CREDENTIAL_REWRAP_DRY_RUN_JOB_ID
APPLY_JOB_ID = PR_PARTY_CREDENTIAL_REWRAP_APPLY_JOB_ID
CONFIRMATION = PR_PARTY_CREDENTIAL_REWRAP_CONFIRMATION


class OperatorRewrapError(RuntimeError):
    """An operator request failed without exposing task or credential internals."""


def _validated_receipt(value: object, *, apply: bool) -> dict[str, Any]:
    """Accept only the task's audit-safe receipt schema before printing it."""
    if not isinstance(value, dict):
        raise OperatorRewrapError("task returned an invalid receipt")

    expected = {
        "status",
        "dry_run",
        "credentials_scanned",
        "credentials_verified",
        "credentials_rewrapped",
        "credential_ids",
    }
    if set(value) != expected or value.get("status") != "completed":
        raise OperatorRewrapError("task returned an invalid receipt")
    if value.get("dry_run") is not (not apply):
        raise OperatorRewrapError("task receipt mode did not match the request")

    counts = (
        value.get("credentials_scanned"),
        value.get("credentials_verified"),
        value.get("credentials_rewrapped"),
    )
    if any(not isinstance(count, int) or isinstance(count, bool) or count < 0 for count in counts):
        raise OperatorRewrapError("task returned invalid receipt counts")

    credential_ids = value.get("credential_ids")
    if not isinstance(credential_ids, list) or any(
        not isinstance(item, str) for item in credential_ids
    ):
        raise OperatorRewrapError("task returned invalid credential identifiers")
    try:
        parsed_ids = [UUID(item) for item in credential_ids]
    except ValueError:
        raise OperatorRewrapError("task returned invalid credential identifiers") from None

    scanned, verified, rewrapped = counts
    if scanned != verified or scanned != len(parsed_ids) or len(set(parsed_ids)) != len(parsed_ids):
        raise OperatorRewrapError("task returned inconsistent receipt counts")
    if rewrapped != (scanned if apply else 0):
        raise OperatorRewrapError("task returned inconsistent receipt counts")

    return {key: value[key] for key in sorted(expected)}


async def enqueue_rewrap(*, apply: bool, timeout: float) -> dict[str, Any]:
    """Enqueue one deduplicated task and wait for its safe audit receipt."""
    pool = await create_pool(get_redis_settings())
    try:
        job = await pool.enqueue_job(
            TASK_NAME,
            apply,
            CONFIRMATION if apply else None,
            _job_id=APPLY_JOB_ID if apply else DRY_RUN_JOB_ID,
        )
        if job is None:
            raise OperatorRewrapError(
                "a credential rewrap in this mode is already queued or retained"
            )
        try:
            result = await job.result(timeout=timeout)
        except Exception:
            raise OperatorRewrapError(
                "credential rewrap task failed; inspect the worker's sanitized receipt"
            ) from None
        return _validated_receipt(result, apply=apply)
    finally:
        await pool.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run or atomically rewrap encrypted PR Party reviewer PATs under "
            "the current SECRET_KEY via the operator-only ARQ task."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="commit the rewrap; without this flag the task only verifies",
    )
    parser.add_argument(
        "--confirm",
        metavar="PHRASE",
        help=f"required with --apply; exact phrase: {CONFIRMATION}",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=360.0,
        help="seconds to wait for the ARQ receipt (default: 360)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Validate operator intent, run the task, and print only safe JSON."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if args.apply and args.confirm != CONFIRMATION:
        parser.error(f"--apply requires --confirm {CONFIRMATION}")
    if not args.apply and args.confirm is not None:
        parser.error("--confirm is valid only with --apply")

    try:
        receipt = asyncio.run(enqueue_rewrap(apply=args.apply, timeout=args.timeout))
    except OperatorRewrapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
