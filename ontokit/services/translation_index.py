"""Public queue seam for commit-specific ontology index rebuilds."""

from __future__ import annotations

import uuid


async def enqueue_ontology_index(
    *, project_id: uuid.UUID, branch: str, commit_hash: str
) -> None:
    """Queue an index rebuild uniquely identified by the exact source commit."""
    from ontokit.api.utils.redis import get_arq_pool

    pool = await get_arq_pool()
    if pool is None:
        return
    await pool.enqueue_job(
        "run_ontology_index_task",
        str(project_id),
        branch,
        commit_hash,
        _job_id=f"ontology-index:{project_id}:{branch}:{commit_hash}",
    )


__all__ = ["enqueue_ontology_index"]
