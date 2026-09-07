"""Retention proofs without database, Redis, or object-storage connections."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from ontokit.models.demo_generation import DemoGeneration
from ontokit.models.project import Project
from ontokit.services.demo_project_provisioning import DemoProvisioningRefused
from ontokit.services.demo_retention import STEPS, DemoRetentionService
from ontokit.services.ontology_index import OntologyIndexService
from ontokit.services.storage import StorageService

NOW = datetime(2026, 9, 7, tzinfo=UTC)


def generation(status="retired", days=30):
    return DemoGeneration(
        id=uuid4(),
        generation_key=uuid4().hex * 2,
        status=status,
        retired_at=NOW - timedelta(days=days) if status == "retired" else None,
        last_failed_at=NOW - timedelta(days=days) if status == "failed" else None,
    )


class Session:
    def __init__(self, generations):
        self.generations = generations
        self.projects = [
            Project(id=uuid4(), demo_generation_id=g.id, is_demo=True)
            for g in generations
            for _ in range(2)
        ]
        self.integrations = [SimpleNamespace(id=uuid4(), project_id=p.id) for p in self.projects]
        self.commits = []

    async def execute(self, statement):
        sql = str(statement)
        params = statement.compile().params
        if sql.startswith("UPDATE demo_generations"):
            target = next(g for g in self.generations if g.id == params["id_1"])
            target.purge_receipt = params["purge_receipt"]
            if "purged_at" in params:
                target.purged_at = params["purged_at"]
            return SimpleNamespace()
        if "FROM demo_generations" in sql:
            rows = self.generations
        elif "FROM projects" in sql:
            rows = [
                p.id
                for p in self.projects
                if p.demo_generation_id == params["demo_generation_id_1"] and p.is_demo
            ]
        elif "FROM github_integrations" in sql:
            rows = [i.id for i in self.integrations if i.project_id in params["project_id_1"]]
        else:
            raise AssertionError(sql)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    async def commit(self):
        self.commits.append([g.purge_receipt for g in self.generations])

    async def rollback(self):
        pass


class Harness:
    def __init__(self, generations, **kwargs):
        self.db = Session(generations)
        self.contents = {step: {p.id for p in self.db.projects} for step in STEPS}
        self.calls = []
        self.held = False
        self.lease_calls = 0
        self.yield_first = False
        self.failure = None
        self.elapsed = 0

        @asynccontextmanager
        async def lease():
            self.lease_calls += 1
            assert not self.held
            if self.yield_first and self.lease_calls == 1:
                raise DemoProvisioningRefused("busy")
            self.held = True
            try:
                yield
            finally:
                self.held = False

        def operation(step):
            async def remove(project_id):
                assert self.held
                project = next(p for p in self.db.projects if p.id == project_id)
                target = next(g for g in self.db.generations if g.id == project.demo_generation_id)
                assert target.purged_at is None
                self.calls.append((step, project_id))
                if self.failure == step:
                    self.failure = None
                    raise RuntimeError("token=secret https://user:password@host <owl:Ontology>")
                count = int(project_id in self.contents[step])
                self.contents[step].discard(project_id)
                if step == "storage":
                    self.elapsed += 10
                return count

            return remove

        self.service = DemoRetentionService(
            self.db,
            clock=lambda: NOW,
            monotonic=lambda: self.elapsed,
            lease_factory=lease,
            min_age_days=7,
            **dict(
                zip(
                    ("git", "index", "embeddings", "lint", "normalization", "storage"),
                    (operation(step) for step in STEPS),
                    strict=True,
                )
            ),
            **kwargs,
        )


async def test_ae4_only_oldest_retired_purged_and_second_run_is_noop():
    old, rollback, active = generation(days=30), generation(days=20), generation("active")
    h = Harness([active, old, rollback])
    before = {step: set(ids) for step, ids in h.contents.items()}
    plan = await h.service.plan()
    assert [e.generation_id for e in plan.eligible] == [old.id]
    summary = await h.service.apply()
    assert summary.purged == [old.generation_key]
    old_ids = {p.id for p in h.db.projects if p.demo_generation_id == old.id}
    for step in STEPS:
        assert h.contents[step] == before[step] - old_ids
    assert len(h.db.projects) == 6 and len(h.db.generations) == 3
    assert old.purged_at == NOW
    receipt = json.loads(old.purge_receipt)
    assert set(receipt["project_ids"]) == {str(p) for p in old_ids}
    assert receipt["attempts"][-1]["completed_steps"] == list(STEPS)
    assert receipt["attempts"][-1]["counts"] == dict.fromkeys(STEPS, 2)
    assert receipt["retained_counts"] == {
        "demo_generations": 1,
        "projects": 2,
        "github_integrations": 2,
    }
    assert len(h.db.integrations) == 6
    assert [s for s, _ in h.calls] == [s for s in STEPS for _ in old_ids]
    assert not (await h.service.apply()).purged
    assert h.lease_calls == 1


async def test_age_keep_failed_and_protected_states():
    old, recent = generation(days=30), generation(days=2)
    newest, second = generation(days=0), generation(days=1)
    failed_recent, failed_old = generation("failed", 1), generation("failed", 60)
    active, preparing = generation("active", 100), generation("preparing", 100)
    h = Harness(
        [old, recent, newest, second, failed_recent, failed_old, active, preparing], keep_retired=2
    )
    plan = await h.service.plan()
    assert [e.generation_id for e in plan.eligible] == [failed_old.id, old.id]
    reasons = {e.generation_id: e.reason for e in plan.retained}
    assert reasons == {
        recent.id: "age",
        newest.id: "keep",
        second.id: "keep",
        failed_recent.id: "age",
        active.id: "active",
        preparing.id: "preparing",
    }
    for contents in h.contents.values():
        contents.difference_update(
            p.id for p in h.db.projects if p.demo_generation_id == failed_old.id
        )
    assert failed_old.generation_key in (await h.service.apply()).purged
    assert not any(json.loads(failed_old.purge_receipt)["attempts"][-1]["counts"].values())


async def test_ae6_failure_is_durable_redacted_and_retry_tolerates_absent_repository():
    old = generation("failed")
    h = Harness([old])
    h.failure = "index"
    summary = await h.service.apply()
    assert summary.failed == [old.generation_key] and old.purged_at is None
    first = json.loads(old.purge_receipt)["attempts"][-1]
    assert first["completed_steps"] == ["repositories"]
    assert first["counts"] == {"repositories": 2}
    assert first["failure_class"] == "RuntimeError"
    assert (await h.service.apply()).purged == [old.generation_key]
    attempts = json.loads(old.purge_receipt)["attempts"]
    assert [a["outcome"] for a in attempts] == ["failed", "success"]
    assert attempts[-1]["counts"]["repositories"] == 0
    assert old.purged_at == NOW
    for secret in ("token", "secret", "password", "https://", "owl:Ontology"):
        assert secret not in old.purge_receipt
    assert any(
        json.loads(r[0])["attempts"][-1]["outcome"] == "failed" for r in h.db.commits if r[0]
    )


async def test_lease_contention_yields_one_and_continues():
    old, newer = generation("failed", 40), generation("failed", 30)
    h = Harness([old, newer])
    h.yield_first = True
    summary = await h.service.apply()
    assert summary.yielded == [old.generation_key]
    assert summary.purged == [newer.generation_key]
    old_ids = {p.id for p in h.db.projects if p.demo_generation_id == old.id}
    assert not any(p in old_ids for _, p in h.calls)
    assert json.loads(old.purge_receipt)["attempts"][-1]["outcome"] == "yielded"


async def test_budget_defers_second_generation():
    old, newer = generation("failed", 40), generation("failed", 30)
    h = Harness([old, newer], run_budget_seconds=1)
    summary = await h.service.apply()
    assert summary.purged == [old.generation_key]
    assert summary.budget_deferred == [newer.generation_key]
    assert h.lease_calls == 1 and newer.purge_receipt is None
    assert (await h.service.apply()).purged == [newer.generation_key]


async def test_rechecks_eligibility_under_lease():
    old = generation("failed")
    h = Harness([old])

    @asynccontextmanager
    async def changed():
        old.status = "preparing"
        yield

    h.service.lease_factory = changed
    summary = await h.service.apply()
    assert not h.calls and not summary.purged
    assert [(e.generation_id, e.reason) for e in summary.retained] == [(old.id, "preparing")]


async def test_cancelled_step_is_recorded_and_lease_released():
    old = generation("failed")
    h = Harness([old])
    h.service.operations["index"] = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await h.service.apply()
    assert not h.held and old.purged_at is None
    attempt = json.loads(old.purge_receipt)["attempts"][-1]
    assert attempt["failure_class"] == "CancelledError"
    assert attempt["completed_steps"] == ["repositories"]


async def test_repository_offload_finishes_before_cancelled_purge_releases_lease(monkeypatch):
    old = generation("failed")
    h = Harness([old])
    started, finish = asyncio.Event(), asyncio.Event()

    async def offload(_fn):
        started.set()
        await finish.wait()
        assert h.held
        return 1

    monkeypatch.setattr("ontokit.services.demo_retention.asyncio.to_thread", offload)
    h.service.operations["repositories"] = h.service._delete_repository
    task = asyncio.create_task(h.service.apply())
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert h.held and not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not h.held and old.purged_at is None


async def test_concurrent_yield_receipt_does_not_overwrite_purge_progress():
    old = generation("failed")
    h = Harness([old])
    entry = (await h.service.plan()).eligible[0]
    active = await h.service._receipt(entry)
    await h.service._save(active)
    yielded = await h.service._receipt(entry)
    yielded.attempts[-1].outcome = "yielded"
    await h.service._save(yielded)
    active.attempts[-1].outcome = "failed"
    active.attempts[-1].failure_class = "RuntimeError"
    await h.service._save(active)
    attempts = json.loads(old.purge_receipt)["attempts"]
    assert sorted(a["outcome"] for a in attempts) == ["failed", "yielded"]
    assert (await h.service.apply()).purged == [old.generation_key]
    assert [a["outcome"] for a in json.loads(old.purge_receipt)["attempts"]] == [
        "yielded",
        "failed",
        "success",
    ]


async def test_project_wide_index_deletes_all_branches_and_child_rows():
    project_id = uuid4()
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalar_one=lambda: 3)
    assert await OntologyIndexService(db).delete_project_index(project_id) == 15
    deletes = [c.args[0] for c in db.execute.call_args_list if str(c.args[0]).startswith("DELETE")]
    assert [s.table.name for s in deletes] == [
        "indexed_labels",
        "indexed_annotations",
        "indexed_entities",
        "indexed_hierarchy",
        "ontology_index_status",
    ]
    for statement in deletes:
        assert project_id in statement.compile().params.values()
        assert "branch =" not in str(statement)
    db.commit.assert_not_awaited()


async def test_default_database_deletions_preserve_projects_and_integrations():
    project_id = uuid4()
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalar_one=lambda: 2, scalar_one_or_none=lambda: None)
    service = DemoRetentionService(db)
    assert await service.operations["embeddings"](project_id) == 6
    assert await service.operations["lint"](project_id) == 4
    assert await service.operations["normalization"](project_id) == 2
    deletes = [c.args[0] for c in db.execute.call_args_list if str(c.args[0]).startswith("DELETE")]
    assert [s.table.name for s in deletes] == [
        "entity_embeddings",
        "embedding_jobs",
        "lint_issues",
        "lint_runs",
        "normalization_runs",
    ]
    for statement in deletes:
        assert project_id in statement.compile().params.values()


@pytest.mark.parametrize("bad", ["../outside", str(uuid4()), None, 123])
async def test_storage_refuses_non_uuid(bad):
    storage = StorageService()
    storage.client = Mock()
    with pytest.raises(TypeError):
        await storage.delete_project_files(bad, db=AsyncMock())
    storage.client.list_objects.assert_not_called()


async def test_storage_prefix_and_all_projects_source_objects_are_protected(monkeypatch):
    offload = AsyncMock(side_effect=lambda fn: fn())
    monkeypatch.setattr("ontokit.services.storage.asyncio.to_thread", offload)
    project_id, other_id = uuid4(), uuid4()
    prefix = f"projects/{project_id}/"
    storage = StorageService()
    protected = [prefix + "source.ttl", storage.bucket + "/" + prefix + "shared.ttl"]
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: protected)
    )
    storage.client = Mock()
    storage.client.list_objects.return_value = iter(
        SimpleNamespace(object_name=k)
        for k in [
            prefix + "derived.json",
            prefix + "source.ttl",
            prefix + "shared.ttl",
            f"projects/{other_id}/keep.ttl",
            prefix[:-1] + "-other/keep.ttl",
        ]
    )
    assert await storage.delete_project_files(project_id, db=db) == 1
    storage.client.list_objects.assert_called_once_with(
        storage.bucket, prefix=prefix, recursive=True
    )
    storage.client.remove_object.assert_called_once_with(storage.bucket, prefix + "derived.json")
    query = str(db.execute.call_args.args[0])
    assert "projects.source_file_path" in query and "projects.id =" not in query
    storage.client.list_objects.return_value = iter([])
    assert await storage.delete_project_files(project_id, db=db) == 0
    assert offload.await_count == 2
