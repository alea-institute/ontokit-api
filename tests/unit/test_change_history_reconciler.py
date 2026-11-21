"""Exercise history reconciliation with real Git, RDF and database rows."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

import pygit2
import pytest
import sqlalchemy as sa
from rdflib.namespace import OWL, RDFS, SKOS
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ontokit.git.bare_repository import BareGitRepositoryService, BareOntologyRepository
from ontokit.models.change_event import EntityChangeEvent
from ontokit.models.llm_generated_entity import LLMGeneratedEntity
from ontokit.models.normalization import NormalizationRun
from ontokit.models.project import Project, ProjectMember
from ontokit.models.pull_request import GitHubIntegration, PullRequest
from ontokit.models.user_commit_identity import UserCommitIdentity
from ontokit.services.commit_identity import noreply_alias

PROJECT = UUID(int=1)
BATCH = UUID(int=2)
BRANCH = "main"
PATH = "ontology.ttl"
BASE_IRI = "https://example.org/ontology#"
PREFIXES = f"""
@prefix ex: <{BASE_IRI}> .
@prefix owl: <{OWL}> .
@prefix rdfs: <{RDFS}> .
@prefix skos: <{SKOS}> .
"""


class History:
    def __init__(self, tmp_path: Path, session: Session) -> None:
        self.git = BareGitRepositoryService(str(tmp_path))
        self.repo = self.git.get_repository(PROJECT)
        self.session = session
        self.db = AsyncMock()
        self.db.begin = self.transaction
        self.db.begin_nested = self.savepoint
        self.db.add = session.add
        self.db.flush.side_effect = session.flush
        self.db.commit.side_effect = self.commit_db
        self.db.get.side_effect = session.get
        self.db.get_bind = session.get_bind
        self.db.execute.side_effect = self.execute
        self.db.scalars.side_effect = session.scalars
        self.db.scalar.side_effect = self.scalar
        self.locked = False
        self.lock_available = True
        self.enqueue = AsyncMock()

    @asynccontextmanager
    async def transaction(self):
        try:
            with self.session.begin():
                yield
        finally:
            self.locked = False

    @asynccontextmanager
    async def savepoint(self):
        with self.session.begin_nested():
            yield

    def commit_db(self) -> None:
        self.session.commit()
        self.locked = False

    def execute(self, statement, params=None):
        if isinstance(statement, sa.TextClause):
            return self.scalar(statement, params)
        return self.session.execute(statement, params)

    def scalar(self, statement, params=None):
        if isinstance(statement, sa.TextClause):
            sql = str(statement)
            assert "pg_try_advisory_xact_lock" in sql or "pg_advisory_xact_lock" in sql
            assert params["lock_key"].startswith(f"suggestion:{PROJECT}:")
            if self.locked or not self.lock_available:
                return False
            self.locked = True
            return True
        return self.session.scalar(statement, params)

    def commit(
        self,
        content: str,
        *,
        branch: str = BRANCH,
        age: int = 1,
        name: str = "Remote Contributor",
        email: str = "remote@example.org",
        second_parent: str | None = None,
        filename: str = PATH,
        time: datetime | None = None,
    ) -> tuple[str, datetime]:
        git = self.repo.repo
        ref = f"refs/heads/{branch}"
        parent = git.references[ref].target if ref in git.references else None
        parents = [parent] if parent is not None else []
        if second_parent is not None:
            parents.append(pygit2.Oid(hex=second_parent))
        time = time or (datetime.now(UTC) - timedelta(days=age)).replace(microsecond=0)
        author = pygit2.Signature(name, email, int(time.timestamp()) - 3600, 0)
        committer = pygit2.Signature(
            "Integrator", "integrator@example.org", int(time.timestamp()), 0
        )
        tree = git.TreeBuilder()
        tree.insert(filename, git.create_blob(content.encode()), pygit2.GIT_FILEMODE_BLOB)
        oid = git.create_commit(ref, author, committer, "Ontology update", tree.write(), parents)
        return str(oid), time

    async def reconcile(self, branch: str = BRANCH, **kwargs):
        from ontokit.services.change_history_reconciler import ChangeHistoryReconciler

        return await ChangeHistoryReconciler(self.db, self.git).reconcile_branch(
            PROJECT, branch, **kwargs
        )

    def events(self) -> list[EntityChangeEvent]:
        with self.session.begin():
            return list(self.session.scalars(sa.select(EntityChangeEvent)))

    def watermark(self, branch: str = BRANCH) -> str | None:
        from ontokit.models.change_history_watermark import ChangeHistoryWatermark

        with self.session.begin():
            row = self.session.get(ChangeHistoryWatermark, (PROJECT, branch))
            return row.commit_hash if row else None


@pytest.fixture
def history(tmp_path: Path):
    from ontokit.models.change_history_watermark import ChangeHistoryWatermark

    engine = sa.create_engine("sqlite://")

    @sa.event.listens_for(engine, "connect")
    def disable_implicit_transactions(connection, _record):
        connection.isolation_level = None

    @sa.event.listens_for(engine, "begin")
    def begin_transaction(connection):
        connection.exec_driver_sql("BEGIN")

    models = (
        Project,
        ProjectMember,
        GitHubIntegration,
        PullRequest,
        UserCommitIdentity,
        EntityChangeEvent,
        LLMGeneratedEntity,
        NormalizationRun,
        ChangeHistoryWatermark,
    )
    with engine.begin() as connection:
        for model in models:
            sa.inspect(model).local_table.create(connection)
    with Session(engine, expire_on_commit=False) as session:
        with session.begin():
            session.add(Project(id=PROJECT, name="Example ontology", owner_id="owner"))
        yield History(tmp_path, session)
    engine.dispose()


async def save_inline(
    history, content, previous, *, branch=BRANCH, ontology=None, user_name="Owner"
):
    from types import SimpleNamespace

    from ontokit.api.routes.projects import save_source_content
    from ontokit.core.auth import CurrentUser
    from ontokit.schemas.project import SourceContentSave
    from ontokit.services.change_event_service import ChangeEventService
    from ontokit.services.ontology import OntologyService

    project = SimpleNamespace(user_role="owner", source_file_path=PATH, git_ontology_path=None)
    with (
        patch("ontokit.api.routes.projects.get_arq_pool", AsyncMock(return_value=None)),
        patch("ontokit.api.routes.projects.enqueue_change_history", history.enqueue),
        patch("ontokit.services.translation_jobs.enqueue_label_diff_after_commit", AsyncMock()),
    ):
        return await save_source_content(
            PROJECT,
            SourceContentSave(
                content=content, commit_message="Source save", base_revision=previous
            ),
            history.db,
            SimpleNamespace(get=AsyncMock(return_value=project)),
            SimpleNamespace(upload_file=AsyncMock()),
            ontology if ontology is not None else OntologyService(),
            history.git,
            ChangeEventService(history.db),
            SimpleNamespace(get_config=AsyncMock(return_value=None)),
            CurrentUser(id="owner", name=user_name, email="owner@example.org"),
            branch=branch,
        )


@pytest.mark.asyncio
async def test_history_enqueue_timeout_does_not_block_landing(monkeypatch, caplog):
    from ontokit.services import change_history_reconciler

    async def unresponsive_queue(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(change_history_reconciler, "_ENQUEUE_TIMEOUT_SECONDS", 0.01, raising=False)
    pool = Mock(enqueue_job=AsyncMock(side_effect=unresponsive_queue))
    await asyncio.wait_for(
        change_history_reconciler.enqueue_change_history(PROJECT, BRANCH, pool=pool), timeout=0.25
    )

    pool.enqueue_job.assert_awaited_once()
    assert "TimeoutError" in caplog.text


@pytest.mark.asyncio
async def test_each_landing_queues_its_own_job(history: History):
    """A job id that named only the branch dropped a landing made while its job was finishing."""
    from ontokit.services.change_history_reconciler import enqueue_change_history

    pool = Mock(enqueue_job=AsyncMock())
    first, _ = history.commit(PREFIXES + "ex:A a owl:Class .")
    await enqueue_change_history(PROJECT, BRANCH, pool=pool, git=history.git)
    await enqueue_change_history(PROJECT, BRANCH, pool=pool, git=history.git)
    second, _ = history.commit(PREFIXES + "ex:A a owl:Class . ex:B a owl:Class .")
    await enqueue_change_history(PROJECT, BRANCH, pool=pool, git=history.git)
    await enqueue_change_history(PROJECT, "missing", pool=pool, git=history.git)

    assert [call.kwargs["_job_id"] for call in pool.enqueue_job.await_args_list] == [
        f"change-history:{PROJECT}:{BRANCH}:{first}",
        f"change-history:{PROJECT}:{BRANCH}:{first}",
        f"change-history:{PROJECT}:{BRANCH}:{second}",
        f"change-history:{PROJECT}:missing",
    ]


@pytest.mark.asyncio
async def test_inline_save_diffs_against_git_parent_when_cache_is_stale(history: History):
    from ontokit.services.ontology import OntologyService

    content = PREFIXES + "ex:A a owl:Class ."
    history.commit(content)
    ontology = OntologyService()
    await ontology.load_from_git(PROJECT, BRANCH, PATH, history.git)
    content += "ex:B a owl:Class ."
    previous, _ = history.commit(content)
    await history.reconcile()

    saved = await save_inline(history, content + "ex:C a owl:Class .", previous, ontology=ontology)

    assert history.watermark() == saved.commit_hash
    assert [row.entity_iri for row in history.events() if row.commit_hash == saved.commit_hash] == [
        BASE_IRI + "C"
    ]


@pytest.mark.asyncio
async def test_consecutive_inline_saves_in_one_second_keep_landing_order(history: History):
    previous, _ = history.commit(PREFIXES)
    timestamp = (datetime.now(UTC) - timedelta(seconds=1)).replace(microsecond=0)
    signature = pygit2.Signature("Owner", "owner@example.org", int(timestamp.timestamp()), 0)

    with patch.object(BareOntologyRepository, "_get_signature", return_value=signature):
        created = await save_inline(history, PREFIXES + "ex:A a owl:Class .", previous)
        deleted = await save_inline(history, PREFIXES, created.commit_hash)

    assert {row.commit_hash: row.created_at.replace(tzinfo=UTC) for row in history.events()} == {
        created.commit_hash: timestamp,
        deleted.commit_hash: timestamp + timedelta(microseconds=1),
    }


@pytest.mark.asyncio
async def test_inline_save_does_not_persist_an_email_as_the_display_name(history: History):
    previous, _ = history.commit(PREFIXES)
    await save_inline(
        history, PREFIXES + "ex:A a owl:Class .", previous, user_name="owner@example.org"
    )

    (event,) = history.events()
    assert (event.user_id, event.user_name) == ("owner", None)


@pytest.mark.asyncio
async def test_history_reads_the_commit_even_if_a_branch_has_its_hash_as_a_name(history: History):
    prior, _ = history.commit(PREFIXES)
    head, _ = history.commit(PREFIXES + "ex:A a owl:Class .")
    history.repo.repo.references.create(f"refs/heads/{head}", pygit2.Oid(hex=prior))

    assert (await history.reconcile())["recorded"] == 1
    (event,) = history.events()
    assert (event.commit_hash, event.entity_iri) == (head, BASE_IRI + "A")


@pytest.mark.asyncio
async def test_zero_commit_budget_is_rejected_instead_of_retrying_forever(history: History):
    history.commit(PREFIXES)
    with pytest.raises(ValueError, match="max_commits"):
        await history.reconcile(max_commits=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", [BRANCH, "work"])
async def test_inline_flush_failure_can_be_reconciled_without_partial_events(
    history: History, branch: str
):
    content = PREFIXES + "ex:A a owl:Class ."
    previous, _ = history.commit(content)
    await history.reconcile()
    if branch != BRANCH:
        history.repo.create_branch(branch, BRANCH)

    # Use a real constraint violation after the recorder has flushed its events.
    def fail_watermark_write(*_args, **_kwargs):
        history.session.execute(sa.text("INSERT INTO projects (id) VALUES (NULL)"))

    with patch(
        "ontokit.api.routes.projects.advance_inline_watermark",
        AsyncMock(side_effect=fail_watermark_write),
    ):
        first_saved = await save_inline(
            history, content + "ex:B a owl:Class .", previous, branch=branch
        )

    assert history.repo.get_branch_commit_hash(branch) == first_saved.commit_hash
    history.db.rollback.assert_not_awaited()
    expected_watermark = previous if branch == BRANCH else None
    assert history.watermark(branch) == expected_watermark
    assert not [row for row in history.events() if row.commit_hash == first_saved.commit_hash]
    history.enqueue.assert_awaited_once_with(PROJECT, branch)

    saved = await save_inline(
        history,
        content + "ex:B a owl:Class . ex:C a owl:Class .",
        first_saved.commit_hash,
        branch=branch,
    )
    assert history.watermark(branch) == expected_watermark
    assert history.enqueue.await_count == 2
    result = await history.reconcile(branch)
    assert (result["recorded"], result["skipped"]) == (1, 1)
    assert history.watermark(branch) == saved.commit_hash
    assert (await history.reconcile(branch))["recorded"] == 0
    assert {
        (row.commit_hash, row.entity_iri) for row in history.events() if row.branch == branch
    } >= {(first_saved.commit_hash, BASE_IRI + "B"), (saved.commit_hash, BASE_IRI + "C")}


@pytest.mark.asyncio
async def test_merge_is_attributed_as_soon_as_the_branch_lock_is_free(history: History):
    """The merge holds the branch lock through its PR commit, so no waiting period applies."""
    content = PREFIXES + "ex:A a owl:Class ."
    history.commit(content)
    await history.reconcile()
    history.repo.create_branch("proposal", BRANCH)
    content += "ex:B a owl:Class ."
    side, _ = history.commit(content, branch="proposal")
    now = datetime.now(UTC)
    unlinked, _ = history.commit(content, time=now, second_parent=side)
    content += "ex:C a owl:Class ."
    side, _ = history.commit(content, branch="proposal")
    merged, _ = history.commit(content, time=now, second_parent=side)
    with history.session.begin():
        history.session.add(
            PullRequest(
                project_id=PROJECT,
                pr_number=1,
                title="Proposal",
                source_branch="proposal",
                target_branch=BRANCH,
                author_id="contributor",
                author_name="Contributor",
                merge_commit_hash=merged,
            )
        )

    result = await history.reconcile()

    assert result["processed"] == 2 and not result["more"]
    assert history.watermark() == merged
    attributed = {row.commit_hash: (row.user_id, row.origin) for row in history.events()}
    assert attributed[unlinked] == ("system", "system")
    assert attributed[merged] == ("contributor", "human")


@pytest.mark.asyncio
async def test_import_or_normalization_is_system_work_credited_to_the_member_who_ran_it(
    history: History,
):
    normalized, _ = history.commit(
        PREFIXES + "ex:A a owl:Class .", name="Owner", email=noreply_alias("owner", "Owner")
    )
    with history.session.begin():
        history.session.add(
            NormalizationRun(
                project_id=PROJECT,
                triggered_by="owner",
                report_json="{}",
                original_format="turtle",
                original_size_bytes=1,
                normalized_size_bytes=2,
                triple_count=1,
                commit_hash=normalized,
            )
        )

    unattributed, _ = history.commit(
        PREFIXES + "ex:A a owl:Class . ex:B a owl:Class .", name="Upstream Author"
    )

    assert (await history.reconcile())["recorded"] == 2
    events = {event.commit_hash: event for event in history.events()}
    # The member ran it, so they are its author; the origin says it was not hand-edited.
    event = events[normalized]
    assert (event.user_id, event.user_name, event.origin) == ("owner", "Owner", "system")
    # No account stands behind this commit, so the system author carries no person's name.
    event = events[unattributed]
    assert (event.user_id, event.user_name, event.origin) == ("system", None, "system")


@pytest.mark.asyncio
async def test_verified_commit_email_credits_only_project_members(history: History):
    with history.session.begin():
        history.session.add(ProjectMember(project_id=PROJECT, user_id="member", role="editor"))
        for user_id in ("member", "outsider"):
            history.session.add(
                UserCommitIdentity(
                    user_id=user_id,
                    commit_email=f"{user_id}@example.org",
                    commit_email_verified=True,
                    use_verified_email=True,
                )
            )
    by_member, _ = history.commit(
        PREFIXES + "ex:A a owl:Class .", name="Member Name", email="member@example.org"
    )
    by_outsider, _ = history.commit(
        PREFIXES + "ex:A a owl:Class . ex:B a owl:Class .",
        name="Claimed Name",
        email="outsider@example.org",
    )

    assert (await history.reconcile())["recorded"] == 2
    events = {event.commit_hash: event for event in history.events()}
    event = events[by_member]
    assert (event.user_id, event.user_name, event.origin) == ("member", "Member Name", "human")
    # An outsider's verified email neither credits them nor reveals their account.
    event = events[by_outsider]
    assert (event.user_id, event.user_name, event.origin) == ("system", None, "system")


@pytest.mark.asyncio
async def test_suggestion_branch_is_excluded_even_when_it_is_symbolic_head(history: History):
    history.commit(PREFIXES)
    branch = "suggest/contributor/proposal"
    history.repo.create_branch(branch, BRANCH)
    history.commit(PREFIXES + "ex:DoNotRecord a owl:Class .", branch=branch)
    history.repo.repo.set_head(f"refs/heads/{branch}")

    assert (await history.reconcile(branch))["processed"] == 0
    assert history.watermark(branch) is None
    assert history.events() == []


@pytest.mark.asyncio
async def test_rollback_after_events_are_flushed_keeps_watermark_and_retry_is_exact(
    history: History,
):
    previous, _ = history.commit(PREFIXES)
    await history.reconcile()
    head, _ = history.commit(PREFIXES + "ex:A a owl:Class .")
    flush = history.session.flush

    def interrupt_after_event_flush():
        flush()
        history.session.execute(sa.text("INSERT INTO projects (id) VALUES (NULL)"))

    history.db.flush.side_effect = interrupt_after_event_flush
    with pytest.raises(IntegrityError):
        await history.reconcile()
    assert history.watermark() == previous
    assert history.events() == []

    history.db.flush.side_effect = flush
    assert (await history.reconcile())["recorded"] == 1
    assert history.watermark() == head
    assert (await history.reconcile())["recorded"] == 0
    assert len(history.events()) == 1


@pytest.mark.asyncio
async def test_first_parent_history_has_commit_times_and_no_duplicate_events(history: History):
    from ontokit.services import change_history_reconciler

    a, ta = history.commit(PREFIXES + "ex:A a owl:Class .", age=3)
    history.repo.create_branch("proposal", BRANCH)
    side, _ = history.commit(
        PREFIXES + "ex:A a owl:Class . ex:Side a owl:Class .", branch="proposal"
    )
    b, tb = history.commit(PREFIXES + "ex:A a owl:Class . ex:B a owl:Class .", age=2)
    c, tc = history.commit(
        PREFIXES + "ex:A a owl:Class . ex:B a owl:Class . ex:C a owl:Class .", second_parent=side
    )
    with patch.object(
        change_history_reconciler,
        "read_history_graph",
        wraps=change_history_reconciler.read_history_graph,
    ) as reads:
        result = await history.reconcile()
        rerun = await history.reconcile()

    assert result == {"processed": 3, "recorded": 3, "skipped": 0, "parse_errors": 0, "more": False}
    assert rerun["processed"] == 0
    assert history.watermark() == c
    assert [(str(args[0].id), args[1]) for args, _ in reads.call_args_list] == [
        (a, PATH),
        (b, PATH),
        (c, PATH),
    ]
    assert {row.commit_hash: row.created_at.replace(tzinfo=UTC) for row in history.events()} == {
        a: ta,
        b: tb,
        c: tc,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("max_commits", [1, 50], ids=["separate-runs", "same-run"])
async def test_same_second_commit_burst_keeps_feed_order(history: History, max_commits: int):
    from ontokit.services.change_event_service import ChangeEventService

    timestamp = (datetime.now(UTC) - timedelta(days=1)).replace(microsecond=0)
    created, _ = history.commit(PREFIXES + "ex:A a owl:Class .", time=timestamp)
    deleted, _ = history.commit(PREFIXES, time=timestamp)
    content = PREFIXES
    commits = [created, deleted]
    for i in range(8):
        content += f"ex:Class{i} a owl:Class .\n"
        commit, _ = history.commit(content, time=timestamp)
        commits.append(commit)

    for _ in commits:
        if not (await history.reconcile(max_commits=max_commits))["more"]:
            break

    with history.session.begin():
        # UUID order is opposite to landing order for the entity's two events.
        for commit, event_id in [(created, UUID(int=2)), (deleted, UUID(int=1))]:
            history.session.execute(
                sa.update(EntityChangeEvent)
                .where(EntityChangeEvent.commit_hash == commit)
                .values(id=event_id)
            )
        feed = await ChangeEventService(history.db).list_changes(PROJECT, branch=BRANCH)

    item = next(item for item in feed.items if item.entity_iri == BASE_IRI + "A")
    assert item.net_change == "transient"
    assert [event.commit_hash for event in item.events] == [deleted, created]
    times = {row.commit_hash: row.created_at.replace(tzinfo=UTC) for row in history.events()}
    assert [times[commit] for commit in commits] == [
        timestamp + timedelta(microseconds=i) for i in range(len(commits))
    ]


@pytest.mark.asyncio
async def test_clock_skew_does_not_move_a_commit_before_its_parent(history: History):
    parent, parent_time = history.commit(PREFIXES + "ex:A a owl:Class .")
    child, _ = history.commit(PREFIXES, time=parent_time - timedelta(minutes=5))

    assert (await history.reconcile())["recorded"] == 2
    assert {row.commit_hash: row.created_at.replace(tzinfo=UTC) for row in history.events()} == {
        parent: parent_time,
        child: parent_time + timedelta(microseconds=1),
    }


@pytest.mark.asyncio
async def test_no_entity_change_advances_watermark_and_rerun_does_not_parse(history: History):
    from ontokit.services import change_history_reconciler

    content = PREFIXES + "ex:A a owl:Class ."
    history.commit(content)
    head, _ = history.commit(content + "\n# Same triples")
    result = await history.reconcile()
    assert result["processed"] == 2 and result["recorded"] == 1
    assert history.watermark() == head
    with patch.object(change_history_reconciler, "read_history_graph") as read:
        assert (await history.reconcile())["processed"] == 0
    read.assert_not_called()


@pytest.mark.asyncio
async def test_overlong_entity_iris_do_not_block_reconciliation(
    history: History, caplog: pytest.LogCaptureFixture
):
    with history.session.begin():
        # SQLite accepts oversized VARCHAR values unless the test enforces the limit.
        history.session.execute(
            sa.text("""
                CREATE TRIGGER enforce_entity_iri_length BEFORE INSERT ON entity_change_events
                WHEN length(NEW.entity_iri) > 2000
                BEGIN SELECT RAISE(ABORT, 'entity IRI exceeds column limit'); END
            """)
        )

    boundary_iri = BASE_IRI + "a" * (2000 - len(BASE_IRI))
    overlong_iri = boundary_iri + "b"
    content = PREFIXES + f"<{boundary_iri}> a owl:Class .\n"
    first, _ = history.commit(content + f"<{overlong_iri}> a owl:Class .")
    second_content = content + "ex:AlsoValid a owl:Class .\n"
    second, _ = history.commit(
        second_content + f'<{overlong_iri}> a owl:Class; rdfs:label "Updated" .'
    )
    history.commit(second_content)
    head, _ = history.commit(second_content + "ex:Later a owl:Class .")

    result = await history.reconcile()

    assert result["processed"] == 4
    assert result["recorded"] == 3
    assert history.watermark() == head
    assert {(row.commit_hash, row.entity_iri) for row in history.events()} == {
        (first, boundary_iri),
        (second, BASE_IRI + "AlsoValid"),
        (head, BASE_IRI + "Later"),
    }
    assert [r.message for r in caplog.records if "entity IRIs longer" in r.message] == [
        "Skipped 3 change events with entity IRIs longer than 2000 characters"
    ]
    assert overlong_iri not in caplog.text


@pytest.mark.asyncio
async def test_backfill_window_and_batch_limit(history: History):
    history.commit(PREFIXES + "ex:Ancient a owl:Class .", age=120)
    baseline, _ = history.commit(PREFIXES + "ex:Baseline a owl:Class .", age=91)
    first, _ = history.commit(PREFIXES + "ex:Baseline a owl:Class . ex:A a owl:Class .", age=88)
    head, _ = history.commit(
        PREFIXES + "ex:Baseline a owl:Class . ex:A a owl:Class . ex:B a owl:Class ."
    )
    result = await history.reconcile(max_commits=1)

    assert result["processed"] == 1 and result["more"]
    assert history.watermark() == first
    assert (await history.reconcile(max_commits=1))["more"] is False
    assert history.watermark() == head
    assert {row.commit_hash for row in history.events()} == {first, head}
    assert baseline not in {row.commit_hash for row in history.events()}


@pytest.mark.asyncio
async def test_parse_error_keeps_last_good_graph_across_batch_boundary(
    history: History, caplog: pytest.LogCaptureFixture
):
    content = PREFIXES + "ex:A a owl:Class ."
    history.commit(content)
    broken, _ = history.commit("sensitive invalid ontology contents")
    head, _ = history.commit(content + "ex:B a owl:Class .")
    result = await history.reconcile(max_commits=2)

    assert result["parse_errors"] == 1 and result["more"]
    assert history.watermark() == broken
    assert (await history.reconcile())["parse_errors"] == 0
    assert history.watermark() == head
    assert [
        (row.entity_iri, row.event_type) for row in history.events() if row.commit_hash == head
    ] == [(BASE_IRI + "B", "create")]
    assert broken in caplog.text
    assert "sensitive invalid" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("default_merged_in", [False, True])
async def test_work_branch_first_reconciliation_records_every_commit_since_its_fork(
    history: History, default_merged_in: bool
):
    content = PREFIXES + "ex:Base a owl:Class ."
    fork, _ = history.commit(content, age=5)
    history.repo.create_branch("work", BRANCH)
    work_content = content + "ex:A a owl:Class ."
    first, _ = history.commit(work_content, branch="work", age=4)
    expected = {(first, BASE_IRI + "A", "create")}
    if default_merged_in:
        content += "ex:Trunk a owl:Class ."
        trunk, _ = history.commit(content, age=3)
        work_content += "ex:Trunk a owl:Class ."
        merge, _ = history.commit(work_content, branch="work", age=2, second_parent=trunk)
        expected.add((merge, BASE_IRI + "Trunk", "create"))
    head, _ = history.commit(work_content + "ex:B a owl:Class .", branch="work")
    expected.add((head, BASE_IRI + "B", "create"))

    result = await history.reconcile("work")

    assert result["processed"] == len(expected) and not result["more"]
    assert history.watermark("work") == head
    assert {(row.commit_hash, row.entity_iri, row.event_type) for row in history.events()} == (
        expected
    )
    assert {row.branch for row in history.events()} == {"work"}
    assert fork not in {row.commit_hash for row in history.events()}
    assert (await history.reconcile("work"))["processed"] == 0


@pytest.mark.asyncio
async def test_work_branch_without_commits_of_its_own_starts_at_its_head(history: History):
    head, _ = history.commit(PREFIXES + "ex:Base a owl:Class .")
    history.repo.create_branch("work", BRANCH)

    assert (await history.reconcile("work"))["processed"] == 0
    assert history.watermark("work") == head
    assert history.events() == []


@pytest.mark.asyncio
async def test_first_inline_save_on_a_work_branch_leaves_the_baseline_to_reconciliation(
    history: History,
):
    content = PREFIXES + "ex:Base a owl:Class ."
    history.commit(content)
    history.repo.create_branch("work", BRANCH)
    content += "ex:Landed a owl:Class ."
    landed, _ = history.commit(content, branch="work")

    saved = await save_inline(history, content + "ex:Saved a owl:Class .", landed, branch="work")

    assert history.watermark("work") is None
    history.enqueue.assert_awaited_once_with(PROJECT, "work")
    result = await history.reconcile("work")
    assert (result["recorded"], result["skipped"]) == (1, 1)
    assert history.watermark("work") == saved.commit_hash
    assert {(row.commit_hash, row.entity_iri) for row in history.events()} == {
        (landed, BASE_IRI + "Landed"),
        (saved.commit_hash, BASE_IRI + "Saved"),
    }

    history.enqueue.reset_mock()
    again = await save_inline(history, content, saved.commit_hash, branch="work")
    assert history.watermark("work") == again.commit_hash
    history.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_rewritten_branch_resets_its_watermark(
    history: History, caplog: pytest.LogCaptureFixture
):
    base, _ = history.commit(PREFIXES)
    history.repo.create_branch("work", BRANCH)
    head, _ = history.commit(PREFIXES + "ex:A a owl:Class .", branch="work")
    assert (await history.reconcile("work"))["recorded"] == 1
    assert history.watermark("work") == head
    history.repo.repo.references["refs/heads/work"].set_target(pygit2.Oid(hex=base))
    orphan = history.repo.repo.create_commit(
        None,
        pygit2.Signature("Remote Contributor", "remote@example.org"),
        pygit2.Signature("Integrator", "integrator@example.org"),
        "Rewritten history",
        history.repo.repo.TreeBuilder().write(),
        [],
    )
    history.repo.repo.references["refs/heads/work"].set_target(orphan)

    assert (await history.reconcile("work"))["processed"] == 0
    assert history.watermark("work") == str(orphan)
    assert [row.commit_hash for row in history.events()] == [head]
    assert "watermark" in caplog.text


@pytest.mark.asyncio
async def test_contended_branch_job_does_no_work(history: History):
    history.commit(PREFIXES + "ex:A a owl:Class .")
    history.lock_available = False
    result = await history.reconcile()
    assert result["processed"] == 0 and result["more"]
    assert history.watermark() is None
    assert history.events() == []


@pytest.mark.asyncio
async def test_commit_attribution_and_real_ledger_origin(history: History):
    proposal = (
        PREFIXES + 'ex:Matched a owl:Class; rdfs:label "Proposal"; skos:definition "A class." .'
        'ex:Edited a owl:Class; rdfs:label "Changed" . ex:Other a owl:Class .'
    )
    merged, _ = history.commit(proposal)
    human, _ = history.commit(
        proposal + "ex:Known a owl:Class .", name="Owner", email=noreply_alias("owner", "Owner")
    )
    remote, _ = history.commit(
        proposal + "ex:Known a owl:Class . ex:Remote a owl:Class .",
        name="outside@example.org",
    )
    with history.session.begin():
        history.session.add(
            PullRequest(
                project_id=PROJECT,
                pr_number=1,
                title="Proposal",
                source_branch="proposal",
                target_branch=BRANCH,
                author_id="contributor",
                author_name="Contributor",
                merge_commit_hash=merged,
            )
        )
        for name in ("Matched", "Edited"):
            history.session.add(
                LLMGeneratedEntity(
                    project_id=PROJECT,
                    branch="proposal",
                    entity_iri=BASE_IRI + name,
                    batch_id=BATCH,
                    suggestion_type="children",
                    focus_iri=BASE_IRI + "Focus",
                    label="Proposal",
                    definition=" A   class. " if name == "Matched" else None,
                    model="example-model",
                    user_id="contributor",
                )
            )
        # Legacy synthetic PRs put the Git email in the author ID.
        history.session.add(
            PullRequest(
                project_id=PROJECT,
                pr_number=2,
                title="Legacy",
                source_branch="remote",
                target_branch=BRANCH,
                author_id="outside@example.org",
                author_name="outside@example.org",
                merge_commit_hash=remote,
            )
        )

    assert (await history.reconcile())["recorded"] == 5
    rows = {row.entity_iri.removeprefix(BASE_IRI): row for row in history.events()}
    assert {
        (rows[name].commit_hash, rows[name].user_id, rows[name].user_name)
        for name in ("Matched", "Edited", "Other")
    } == {(merged, "contributor", "Contributor")}
    assert (
        rows["Matched"].origin,
        rows["Matched"].origin_model,
        rows["Matched"].origin_batch_id,
    ) == (
        "ai",
        "example-model",
        BATCH,
    )
    assert rows["Edited"].origin == "ai_edited"
    assert rows["Other"].origin == "human"
    assert (rows["Known"].commit_hash, rows["Known"].user_id, rows["Known"].origin) == (
        human,
        "owner",
        "human",
    )
    assert (rows["Remote"].user_id, rows["Remote"].user_name, rows["Remote"].origin) == (
        "system",
        None,
        "system",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [None, "gap", "initial", "suggestion"])
async def test_direct_save_preserves_ai_provenance_and_watermark_contiguity(
    history: History, pending: str | None
):
    from types import SimpleNamespace

    from ontokit.api.routes.projects import save_source_content
    from ontokit.core.auth import CurrentUser
    from ontokit.schemas.project import SourceContentSave
    from ontokit.services.change_event_service import ChangeEventService
    from ontokit.services.ontology import OntologyService

    base, _ = history.commit(PREFIXES)
    if pending != "initial":
        await history.reconcile()
    previous = base
    branch = BRANCH
    old_content = PREFIXES
    if pending in ("gap", "initial"):
        old_content += "ex:Pending a owl:Class ."
        previous, _ = history.commit(old_content)
    elif pending == "suggestion":
        branch = "suggest/editor/session"
        history.repo.create_branch(branch, BRANCH)

    with history.session.begin():
        for name in ("Matched", "Edited"):
            history.session.add(
                LLMGeneratedEntity(
                    project_id=PROJECT,
                    branch=branch,
                    entity_iri=BASE_IRI + name,
                    batch_id=BATCH,
                    suggestion_type="children",
                    focus_iri=BASE_IRI + "Focus",
                    label="Proposal",
                    definition="A class." if name == "Matched" else None,
                    model="example-model",
                    user_id="owner",
                )
            )

    project = SimpleNamespace(user_role="owner", source_file_path=PATH, git_ontology_path=None)
    content = old_content + (
        'ex:Matched a owl:Class; rdfs:label "Proposal"; skos:definition "A class." .'
        'ex:Edited a owl:Class; rdfs:label "Changed" . ex:Other a owl:Class .'
    )
    with (
        patch("ontokit.api.routes.projects.get_arq_pool", AsyncMock(return_value=None)),
        patch("ontokit.api.routes.projects.enqueue_change_history", history.enqueue),
        patch("ontokit.services.translation_jobs.enqueue_label_diff_after_commit", AsyncMock()),
    ):
        saved = await save_source_content(
            PROJECT,
            SourceContentSave(
                content=content, commit_message="Source save", base_revision=previous
            ),
            history.db,
            SimpleNamespace(get=AsyncMock(return_value=project)),
            SimpleNamespace(upload_file=AsyncMock()),
            OntologyService(),
            history.git,
            ChangeEventService(history.db),
            SimpleNamespace(get_config=AsyncMock(return_value=None)),
            CurrentUser(id="owner", name="Owner", email="owner@example.org"),
            branch=branch,
        )

    if pending == "suggestion":
        assert history.watermark(branch) is None
        assert history.events() == []
        return

    assert history.watermark() == ({"gap": base, "initial": None}.get(pending, saved.commit_hash))
    assert history.enqueue.await_args_list == ([((PROJECT, BRANCH),)] if pending else [])
    rows = {row.entity_iri.removeprefix(BASE_IRI): row for row in history.events()}
    assert {(name, row.origin) for name, row in rows.items()} == {
        ("Matched", "ai"),
        ("Edited", "ai_edited"),
        ("Other", "human"),
    }
    assert (rows["Matched"].origin_model, rows["Matched"].origin_batch_id) == (
        "example-model",
        BATCH,
    )
    if pending:
        caught_up = await history.reconcile()
        assert caught_up["recorded"] == 1 and caught_up["skipped"] == 1
        assert history.watermark() == saved.commit_hash


@pytest.mark.asyncio
async def test_watermark_reachable_through_second_parent_catches_up(history: History):
    content = PREFIXES + "ex:Base a owl:Class ."
    history.commit(content)
    history.repo.create_branch("other", BRANCH)
    recorded, _ = history.commit(content + "ex:Local a owl:Class .")
    await history.reconcile()
    history.commit(content + "ex:Remote a owl:Class .", branch="other")
    joined, _ = history.commit(
        content + "ex:Local a owl:Class . ex:Remote a owl:Class .",
        branch="other",
        second_parent=recorded,
    )
    history.repo.repo.references["refs/heads/main"].set_target(pygit2.Oid(hex=joined))

    assert (await history.reconcile())["processed"] == 1
    assert history.watermark() == joined
    events = [row for row in history.events() if row.commit_hash == joined]
    assert [(row.entity_iri, row.event_type) for row in events] == [(BASE_IRI + "Remote", "create")]


@pytest.mark.asyncio
async def test_concurrent_reconciliation_returns_while_first_run_holds_lock(history: History):
    from ontokit.services.change_history_reconciler import ChangeHistoryReconciler

    history.commit(PREFIXES + "ex:Class a owl:Class .")
    locked = asyncio.Event()
    release = asyncio.Event()
    original_get = history.db.get.side_effect

    async def pause_locked_run(*args, **kwargs):
        if args[0] is Project:
            locked.set()
            await release.wait()
        return original_get(*args, **kwargs)

    @asynccontextmanager
    async def second_transaction():
        yield

    second_db = AsyncMock()
    second_db.begin = second_transaction
    second_db.scalar.side_effect = history.scalar
    history.db.get.side_effect = pause_locked_run
    first = asyncio.create_task(history.reconcile())
    try:
        await locked.wait()
        second = await ChangeHistoryReconciler(second_db, history.git).reconcile_branch(
            PROJECT, BRANCH
        )
        assert second["processed"] == 0
        second_db.get.assert_not_awaited()
    finally:
        release.set()
        assert (await first)["recorded"] == 1

    assert len(history.events()) == 1


@pytest.mark.asyncio
async def test_jsonld_history_parses_inline_context_without_fetching_remote_context(
    history: History,
):
    from rdflib.plugins.shared.jsonld.context import Context

    filename = "ontology.jsonld"
    with history.session.begin():
        history.session.add(
            GitHubIntegration(
                project_id=PROJECT,
                repo_owner="example",
                repo_name="ontology",
                ontology_file_path=filename,
                default_branch=BRANCH,
                connected_by_user_id="owner",
            )
        )
    content = {"@id": BASE_IRI + "Local", "@type": str(OWL.Class)}
    good, _ = history.commit(
        json.dumps({"@context": {"owl": str(OWL)}, **content}), filename=filename
    )
    remote, _ = history.commit(
        json.dumps({"@context": "https://example.org/context", **content}), filename=filename
    )
    with patch.object(
        Context, "_fetch_context", side_effect=RuntimeError("Remote context")
    ) as fetch:
        result = await history.reconcile()

    fetch.assert_not_called()
    assert result["parse_errors"] == 1
    assert history.watermark() == remote
    assert [row.commit_hash for row in history.events()] == [good]


@pytest.mark.asyncio
async def test_running_job_reports_more_when_another_landing_advances_the_ref(history: History):
    original, _ = history.commit(PREFIXES + "ex:A a owl:Class .")
    read_scalar = history.scalar

    def land_during_reconciliation(statement, params=None):
        if (
            isinstance(statement, sa.Select)
            and statement.column_descriptions[0]["entity"] is EntityChangeEvent
        ):
            history.db.scalar.side_effect = read_scalar
            history.commit(PREFIXES + "ex:A a owl:Class . ex:B a owl:Class .")
        return read_scalar(statement, params)

    history.db.scalar.side_effect = land_during_reconciliation

    result = await history.reconcile()

    assert result["processed"] == 1 and result["more"]
    assert history.watermark() == original
    assert (await history.reconcile())["recorded"] == 1


@pytest.mark.asyncio
async def test_history_parses_and_walks_off_the_event_loop(history: History):
    import threading

    from ontokit.services import change_history_reconciler

    history.commit(PREFIXES + "ex:A a owl:Class .")
    history.commit(PREFIXES + "ex:A a owl:Class . ex:B a owl:Class .")
    loop_thread = threading.get_ident()
    threads: list[tuple[str, int]] = []

    def on_thread(name, function):
        def call(*args, **kwargs):
            threads.append((name, threading.get_ident()))
            return function(*args, **kwargs)

        return call

    with (
        patch.object(
            change_history_reconciler,
            "read_history_graph",
            on_thread("parse", change_history_reconciler.read_history_graph),
        ),
        patch.object(
            change_history_reconciler,
            "_select_history",
            on_thread("walk", change_history_reconciler._select_history),
        ),
    ):
        assert (await history.reconcile())["recorded"] == 2

    assert {name for name, _ in threads} == {"parse", "walk"}
    assert loop_thread not in {thread for _, thread in threads}


@pytest.mark.asyncio
async def test_commit_with_an_unchanged_ontology_blob_is_not_reparsed(history: History):
    from ontokit.services import change_history_reconciler

    content = PREFIXES + "ex:A a owl:Class ."
    first, _ = history.commit(content)
    same, _ = history.commit(content)
    head, _ = history.commit(content + "ex:B a owl:Class .")
    with patch.object(
        change_history_reconciler,
        "read_history_graph",
        wraps=change_history_reconciler.read_history_graph,
    ) as reads:
        result = await history.reconcile()

    assert result["processed"] == 3 and result["recorded"] == 2
    assert [str(args[0].id) for args, _ in reads.call_args_list] == [first, head]
    assert history.watermark() == head
    assert sorted((row.commit_hash, row.entity_iri) for row in history.events()) == sorted(
        [(first, BASE_IRI + "A"), (head, BASE_IRI + "B")]
    )


@pytest.mark.asyncio
async def test_oversized_ontology_is_logged_and_history_resumes_after_it(
    history: History, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    from ontokit.services import change_history_reconciler

    content = PREFIXES + "ex:A a owl:Class ."
    first, _ = history.commit(content)
    oversized_content = content + "ex:Huge a owl:Class . # " + "x" * 400
    oversized, _ = history.commit(oversized_content)
    head, _ = history.commit(content + "ex:B a owl:Class .")
    monkeypatch.setattr(
        change_history_reconciler, "_MAX_HISTORY_BLOB_BYTES", len(oversized_content) - 1
    )
    with patch.object(
        change_history_reconciler,
        "read_history_graph",
        wraps=change_history_reconciler.read_history_graph,
    ) as reads:
        result = await history.reconcile()

    assert result["parse_errors"] == 1 and result["processed"] == 3
    assert oversized not in [str(args[0].id) for args, _ in reads.call_args_list]
    assert history.watermark() == head
    assert sorted((row.commit_hash, row.entity_iri) for row in history.events()) == sorted(
        [(first, BASE_IRI + "A"), (head, BASE_IRI + "B")]
    )
    assert any(
        oversized in record.getMessage() and "history limit" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_first_backfill_stops_at_its_commit_cap_regardless_of_commit_time(
    history: History, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    from ontokit.services import change_history_reconciler

    monkeypatch.setattr(change_history_reconciler, "_BACKFILL_MAX_COMMITS", 2)
    content = PREFIXES + "ex:A a owl:Class ."
    history.commit(content)
    baseline, _ = history.commit(content + "ex:B a owl:Class .")
    second, _ = history.commit(content + "ex:B a owl:Class . ex:C a owl:Class .")
    head, _ = history.commit(content + "ex:B a owl:Class . ex:C a owl:Class . ex:D a owl:Class .")

    result = await history.reconcile()

    assert result["processed"] == 2 and not result["more"]
    assert history.watermark() == head
    assert sorted((row.commit_hash, row.entity_iri) for row in history.events()) == sorted(
        [(second, BASE_IRI + "C"), (head, BASE_IRI + "D")]
    )
    assert any(
        "2 commits" in record.getMessage() and baseline in record.getMessage()
        for record in caplog.records
    )
