"""Tests for PR Party intake — the reconciliation sweep and the upsert path (U4).

Intake is the one place where GitHub's view of the org becomes rows, so the
properties worth pinning are the ones whose failure is *silent*:

- **The sweep is complete intake on its own (KTD14).** Discovery is one org
  search; the expensive detail + check-runs fetches happen only for rows whose
  ``updated_at`` actually moved. The call-budget test is therefore a correctness
  test, not a performance test: a sweep that re-fetches everything exhausts the
  hourly limit and then stops seeing new PRs at all.
- **Column ownership (KTD15).** The sweep writes poller-owned columns and
  nothing else. ``test_refresh_preserves_brief_columns`` is the DB form of
  prototype finding B3: it folds a brief in exactly as U5 would, re-sweeps the
  same head, and asserts the brief survived.
- **A revision is a head SHA.** A new head restarts brewing; the same head is a
  no-op. Everything downstream (C1's stale-verdict detection, U5's job-id
  dedupe) is keyed off that boundary being drawn in exactly one place.
- **``mergeable: null`` is "computing", never "not mergeable" (R4).** Storing
  the falsy reading would render a hard "cannot merge" on a PR GitHub simply
  had not finished thinking about.

The DB is a hand-rolled fake with a small ``WHERE``-clause interpreter rather
than an ``AsyncMock``: the sweep is a multi-statement routine whose whole
behavior is *which* rows it selected and mutated, and a mock returning one
canned result for every ``execute`` cannot express that. The interpreter covers
the comparison forms this module actually emits (``==``, ``!=``, ``<``,
``is None``, ``IN``, ``AND``); it is not a SQL engine, and anything richer
belongs in an integration test against Postgres.
"""

from __future__ import annotations

import logging
import operator
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ontokit.models.pr_party import (
    PRPartyAuthorKind,
    PRPartyBriefStatus,
    PRPartyPR,
    PRPartyReviewer,
)
from ontokit.services.pr_party_github import ChecksRollup, Mergeability, PRDetail, SearchedPR
from ontokit.services.pr_party_intake import (
    BREWING_TIMEOUT,
    BRIEF_TASK_NAME,
    MISSING_MISS_THRESHOLD,
    PR_STATE_CLOSED,
    PR_STATE_DRAFT,
    PR_STATE_MERGED,
    PR_STATE_OPEN,
    ReviewerRegistry,
    apply_brewing_timeout,
    brief_job_id,
    classify_author,
    facts_from_detail,
    facts_from_webhook_pr,
    handle_webhook_event,
    load_reviewer_registry,
    missing_long_enough_to_retire,
    ready_transition_hooks,
    refresh_pull_request,
    resolve_mergeable_state,
    sweep_open_prs,
    upsert_pr,
)

# ---------------------------------------------------------------------------
# Fake session (see module docstring for why this is not an AsyncMock)
# ---------------------------------------------------------------------------


def _clause_value(node: Any, row: Any) -> Any:
    """Resolve one side of a comparison against a candidate row."""
    if hasattr(node, "value") and not hasattr(node, "table"):
        return node.value
    if hasattr(node, "key"):
        return getattr(row, node.key)
    return node


def _ordered(fn: Any) -> Any:
    """SQL three-valued logic: an ordering comparison against NULL is not true."""

    def _compare(a: Any, b: Any) -> bool:
        if a is None or b is None:
            return False
        return bool(fn(a, b))

    return _compare


_COMPARATORS: dict[str, Any] = {
    "eq": operator.eq,
    "ne": operator.ne,
    "lt": _ordered(operator.lt),
    "le": _ordered(operator.le),
    "gt": _ordered(operator.gt),
    "ge": _ordered(operator.ge),
    "is_": lambda a, b: a is b,
    "is_not": lambda a, b: a is not b,
    "in_op": lambda a, b: a in list(b or []),
    "not_in_op": lambda a, b: a not in list(b or []),
}


def _matches(clause: Any, row: Any) -> bool:
    if clause is None:
        return True
    name = getattr(getattr(clause, "operator", None), "__name__", "")
    if name in {"and_", "or_"}:
        results = [_matches(child, row) for child in clause.clauses]
        return all(results) if name == "and_" else any(results)
    comparator = _COMPARATORS.get(name)
    if comparator is None:  # pragma: no cover — guard against silent mismatches
        raise AssertionError(f"Fake session cannot evaluate operator {name!r}")
    return bool(comparator(_clause_value(clause.left, row), _clause_value(clause.right, row)))


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class _FakeSession:
    """AsyncSession stand-in: routes a SELECT by entity, then filters in Python."""

    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows: list[Any] = list(rows or [])
        self.commits = 0
        self.statements: list[Any] = []

    async def execute(self, stmt: Any) -> _FakeResult:
        self.statements.append(stmt)
        entity = stmt.column_descriptions[0]["entity"]
        candidates = [r for r in self.rows if isinstance(r, entity)]
        return _FakeResult([r for r in candidates if _matches(stmt.whereclause, r)])

    def add(self, obj: Any) -> None:
        self.rows.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def flush(self) -> None:
        return None


class _FakePool:
    """ArqRedis stand-in for the enqueue seam and the delivery-id dedupe."""

    def __init__(self, *, set_result: bool | None = True, fail: bool = False) -> None:
        self.jobs: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.keys: dict[str, str] = {}
        self._set_result = set_result
        self._fail = fail

    async def enqueue_job(self, *args: Any, **kwargs: Any) -> Any:
        if self._fail:
            raise RuntimeError("redis is down")
        self.jobs.append((args, kwargs))
        return object()

    async def set(self, key: str, value: str, **_kwargs: Any) -> bool | None:
        if self._fail:
            raise RuntimeError("redis is down")
        if self._set_result is None:
            return None
        if key in self.keys:
            return None
        self.keys[key] = value
        return True

    @property
    def job_ids(self) -> list[str]:
        return [kwargs["_job_id"] for _args, kwargs in self.jobs]


class _FakeClient:
    """PRPartyGitHubClient stand-in that records its call budget."""

    def __init__(
        self,
        *,
        pages: list[list[SearchedPR]] | None = None,
        details: dict[tuple[str, int], PRDetail] | None = None,
        rollups: dict[str, ChecksRollup] | None = None,
        search_error: Exception | None = None,
        detail_error: Exception | None = None,
    ) -> None:
        self.pages = pages if pages is not None else [[]]
        self.details = details or {}
        self.rollups = rollups or {}
        self.search_error = search_error
        self.detail_error = detail_error
        self.search_calls = 0
        self.detail_calls: list[tuple[str, int]] = []
        self.rollup_calls: list[str] = []

    async def search_org_open_prs(
        self, org: str, *, page: int = 1, per_page: int = 100
    ) -> list[SearchedPR]:
        del org, per_page
        self.search_calls += 1
        if self.search_error is not None:
            raise self.search_error
        index = page - 1
        return self.pages[index] if index < len(self.pages) else []

    async def get_pull_request(self, owner: str, repo: str, number: int) -> PRDetail:
        key = (f"{owner}/{repo}", number)
        self.detail_calls.append(key)
        if self.detail_error is not None:
            raise self.detail_error
        return self.details[key]

    async def get_check_runs_rollup(self, owner: str, repo: str, sha: str) -> ChecksRollup:
        del owner, repo
        self.rollup_calls.append(sha)
        return self.rollups.get(sha, ChecksRollup.SUCCESS)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
REPO = "CatholicOS/liturgy"


def _reviewer(login: str = "damienriehl", node_id: str | None = "MDQ6VXNlcjE=") -> PRPartyReviewer:
    row = PRPartyReviewer(
        zitadel_user_id=f"zid-{login}",
        github_login=login,
        github_node_id=node_id,
    )
    row.id = uuid.uuid4()
    return row


def _searched(
    number: int = 7,
    *,
    updated_at: datetime | None = None,
    login: str = "outsider",
    node_id: str = "MDQ6VXNlcjk5",
    user_type: str = "User",
    draft: bool = False,
    repo: str = REPO,
) -> SearchedPR:
    return SearchedPR(
        repo_full_name=repo,
        number=number,
        title=f"PR {number}",
        state="open",
        updated_at=updated_at or NOW - timedelta(minutes=10),
        created_at=NOW - timedelta(days=1),
        author_login=login,
        author_node_id=node_id,
        author_type=user_type,
        html_url=f"https://github.com/{repo}/pull/{number}",
        node_id="I_kwDO",
        draft=draft,
    )


def _detail(
    number: int = 7,
    *,
    head_sha: str = "a" * 40,
    login: str = "outsider",
    node_id: str = "MDQ6VXNlcjk5",
    user_type: str = "User",
    draft: bool = False,
    state: str = "open",
    merged: bool = False,
    mergeability: Mergeability = Mergeability.MERGEABLE,
    mergeable_state: str | None = "clean",
    updated_at: datetime | None = None,
    repo: str = REPO,
) -> PRDetail:
    return PRDetail(
        repo_full_name=repo,
        number=number,
        title=f"PR {number}",
        body="body",
        state=state,
        draft=draft,
        merged=merged,
        head_sha=head_sha,
        head_ref="feat/x",
        base_ref="main",
        author_login=login,
        author_node_id=node_id,
        author_type=user_type,
        node_id="PR_kwDO",
        mergeability=mergeability,
        mergeable_state=mergeable_state,
        html_url=f"https://github.com/{repo}/pull/{number}",
        created_at=NOW - timedelta(days=1),
        updated_at=updated_at or NOW - timedelta(minutes=10),
    )


def _webhook_payload(
    *,
    action: str = "opened",
    number: int = 7,
    head_sha: str = "a" * 40,
    draft: bool = False,
    state: str = "open",
    merged: bool = False,
    login: str = "outsider",
    node_id: str = "MDQ6VXNlcjk5",
    user_type: str = "User",
    mergeable: bool | None = True,
    repo: str = REPO,
) -> dict[str, Any]:
    return {
        "action": action,
        "repository": {"full_name": repo},
        "pull_request": {
            "number": number,
            "node_id": "PR_kwDO",
            "state": state,
            "draft": draft,
            "merged": merged,
            "title": f"PR {number}",
            "mergeable": mergeable,
            "mergeable_state": "clean" if mergeable else "dirty",
            "updated_at": "2026-07-26T11:50:00Z",
            "head": {"sha": head_sha, "ref": "feat/x"},
            "base": {"ref": "main", "repo": {"full_name": repo}},
            "user": {"login": login, "node_id": node_id, "type": user_type},
        },
    }


def _existing_pr(
    *,
    number: int = 7,
    head_sha: str = "a" * 40,
    updated_at: datetime | None = None,
    state: str = PR_STATE_OPEN,
    brief_status: str = PRPartyBriefStatus.READY,
    repo: str = REPO,
) -> PRPartyPR:
    row = PRPartyPR(
        repo_full_name=repo,
        pr_number=number,
        head_sha=head_sha,
        state=state,
        author_kind=PRPartyAuthorKind.THIRD_PARTY,
        author_github_login="outsider",
        author_node_id="MDQ6VXNlcjk5",
        brief_status=brief_status,
        updated_at_github=updated_at or NOW - timedelta(minutes=10),
    )
    row.id = uuid.uuid4()
    return row


def _registry(*reviewers: PRPartyReviewer) -> ReviewerRegistry:
    return ReviewerRegistry.from_rows(list(reviewers))


# ---------------------------------------------------------------------------
# Author classification (R19)
# ---------------------------------------------------------------------------


class TestAuthorClassification:
    def test_bot_account_type_wins(self) -> None:
        kind = classify_author(
            login="dependabot[bot]", node_id="BOT_1", user_type="Bot", registry=_registry()
        )
        assert kind is PRPartyAuthorKind.BOT

    def test_bot_login_suffix_without_type(self) -> None:
        """Webhook payloads occasionally omit ``user.type``; the suffix still says bot."""
        kind = classify_author(
            login="renovate[bot]", node_id=None, user_type=None, registry=_registry()
        )
        assert kind is PRPartyAuthorKind.BOT

    def test_unknown_human_is_third_party(self) -> None:
        kind = classify_author(
            login="drive-by", node_id="MDQ6VXNlcjc=", user_type="User", registry=_registry()
        )
        assert kind is PRPartyAuthorKind.THIRD_PARTY

    def test_registry_member_by_node_id_is_counterpart(self) -> None:
        reviewer = _reviewer(login="renamed-since", node_id="NODE_A")
        kind = classify_author(
            login="brand-new-login",
            node_id="NODE_A",
            user_type="User",
            registry=_registry(reviewer),
        )
        assert kind is PRPartyAuthorKind.COUNTERPART

    def test_registry_member_by_login_when_node_ids_absent(self) -> None:
        """KTD12 node ids are best-effort and can be NULL on either side."""
        reviewer = _reviewer(login="DamienRiehl", node_id=None)
        kind = classify_author(
            login="damienriehl", node_id=None, user_type="User", registry=_registry(reviewer)
        )
        assert kind is PRPartyAuthorKind.COUNTERPART

    def test_own_is_never_stored_on_the_row(self) -> None:
        """Row-level ``own`` is not expressible: own-vs-counterpart is per caller.

        The row records *that a principal authored it* plus the author identity;
        U15 derives own-vs-counterpart by comparing that identity to the reader.
        """
        reviewer = _reviewer()
        kind = classify_author(
            login=reviewer.github_login,
            node_id=reviewer.github_node_id,
            user_type="User",
            registry=_registry(reviewer),
        )
        assert kind is not PRPartyAuthorKind.OWN
        assert kind is PRPartyAuthorKind.COUNTERPART

    async def test_registry_loads_from_db(self) -> None:
        reviewer = _reviewer()
        db = _FakeSession([reviewer])
        registry = await load_reviewer_registry(db)  # type: ignore[arg-type]
        assert registry.is_member(login="damienriehl", node_id=None) is True
        assert registry.is_member(login="stranger", node_id=None) is False


# ---------------------------------------------------------------------------
# Mergeability (R4)
# ---------------------------------------------------------------------------


class TestMergeability:
    def test_null_mergeable_persists_as_computing(self) -> None:
        detail = _detail(mergeability=Mergeability.COMPUTING, mergeable_state=None)
        assert detail.is_computing is True
        assert resolve_mergeable_state(detail) == "computing"

    def test_computing_beats_a_stale_raw_state(self) -> None:
        """``mergeable: null`` with a leftover ``mergeable_state`` is still computing."""
        detail = _detail(mergeability=Mergeability.COMPUTING, mergeable_state="unknown")
        assert resolve_mergeable_state(detail) == "computing"

    def test_resolved_state_persists_githubs_raw_word(self) -> None:
        detail = _detail(mergeability=Mergeability.NOT_MERGEABLE, mergeable_state="dirty")
        assert resolve_mergeable_state(detail) == "dirty"

    def test_resolved_without_raw_state_falls_back_to_the_tri_state(self) -> None:
        detail = _detail(mergeability=Mergeability.NOT_MERGEABLE, mergeable_state=None)
        assert resolve_mergeable_state(detail) == "not_mergeable"

    async def test_sweep_stores_computing_not_not_mergeable(self) -> None:
        db = _FakeSession()
        detail = _detail(mergeability=Mergeability.COMPUTING, mergeable_state=None)
        client = _FakeClient(details={(REPO, 7): detail})
        result = await refresh_pull_request(
            db,  # type: ignore[arg-type]
            client,  # type: ignore[arg-type]
            REPO,
            7,
            registry=_registry(),
            now=NOW,
        )
        assert result.pr is not None
        assert result.pr.mergeable_state == "computing"
        assert result.pr.mergeable_state != "not_mergeable"


# ---------------------------------------------------------------------------
# Upsert path (KTD14/KTD15)
# ---------------------------------------------------------------------------


class TestUpsert:
    async def test_new_pr_is_created_brewing(self) -> None:
        db = _FakeSession()
        facts = facts_from_detail(_detail(), rollup=ChecksRollup.PENDING)
        result = await upsert_pr(db, facts, registry=_registry(), now=NOW)  # type: ignore[arg-type]

        assert result.created is True
        assert result.new_revision is True
        pr = result.pr
        assert pr is not None
        assert pr.repo_full_name == REPO
        assert pr.pr_number == 7
        assert pr.head_sha == "a" * 40
        assert pr.state == PR_STATE_OPEN
        assert pr.brief_status == PRPartyBriefStatus.BREWING
        assert pr.brewing_since == NOW
        assert pr.checks_rollup == "pending"
        assert pr.author_kind == PRPartyAuthorKind.THIRD_PARTY
        assert pr.author_github_login == "outsider"
        assert pr.author_node_id == "MDQ6VXNlcjk5"

    async def test_zero_configured_checks_persist_as_none_not_success(self) -> None:
        """U3 added ``NONE`` because "nothing ran" is not "everything passed"."""
        db = _FakeSession()
        facts = facts_from_detail(_detail(), rollup=ChecksRollup.NONE)
        result = await upsert_pr(db, facts, registry=_registry(), now=NOW)  # type: ignore[arg-type]
        assert result.pr is not None
        assert result.pr.checks_rollup == "none"

    async def test_same_head_resweep_is_a_no_op_for_revision_state(self) -> None:
        existing = _existing_pr(brief_status=PRPartyBriefStatus.READY)
        existing.brewing_since = NOW - timedelta(hours=5)
        existing.ready_at = NOW - timedelta(hours=4)
        db = _FakeSession([existing])

        facts = facts_from_detail(_detail(), rollup=ChecksRollup.SUCCESS)
        result = await upsert_pr(db, facts, registry=_registry(), now=NOW)  # type: ignore[arg-type]

        assert result.created is False
        assert result.new_revision is False
        assert existing.brief_status == PRPartyBriefStatus.READY
        assert existing.brewing_since == NOW - timedelta(hours=5)
        assert existing.ready_at == NOW - timedelta(hours=4)

    async def test_force_push_supersedes_and_rebrews(self) -> None:
        existing = _existing_pr(head_sha="a" * 40, brief_status=PRPartyBriefStatus.READY)
        existing.ready_at = NOW - timedelta(hours=4)
        db = _FakeSession([existing])

        facts = facts_from_detail(_detail(head_sha="b" * 40), rollup=ChecksRollup.PENDING)
        result = await upsert_pr(db, facts, registry=_registry(), now=NOW)  # type: ignore[arg-type]

        assert result.new_revision is True
        assert existing.head_sha == "b" * 40
        assert existing.brief_status == PRPartyBriefStatus.BREWING
        assert existing.brewing_since == NOW
        assert existing.ready_at is None

    async def test_refresh_preserves_brief_columns(self) -> None:
        """KTD15 / prototype finding B3, in its DB form.

        The brief worker folds content in; a later same-head sweep must not
        touch a single ``brief_*`` column.
        """
        existing = _existing_pr(brief_status=PRPartyBriefStatus.BREWING)
        # Fold a brief in exactly as U5 will.
        existing.brief_status = PRPartyBriefStatus.READY
        existing.brief_what = "Adds the liturgical calendar importer."
        existing.brief_why = "The manual import was the last hand-run step."
        existing.brief_decisions = ["Chose ICS over CSV"]
        existing.brief_links = ["https://github.com/CatholicOS/liturgy/pull/7/files"]
        existing.brief_truncated = True
        existing.ready_at = NOW - timedelta(hours=1)
        existing.brewing_since = NOW - timedelta(hours=2)
        db = _FakeSession([existing])

        facts = facts_from_detail(
            _detail(mergeable_state="behind", updated_at=NOW), rollup=ChecksRollup.FAILURE
        )
        await upsert_pr(db, facts, registry=_registry(), now=NOW)  # type: ignore[arg-type]

        # Poller columns moved...
        assert existing.mergeable_state == "behind"
        assert existing.checks_rollup == "failure"
        assert existing.updated_at_github == NOW
        # ...and every brief column survived untouched.
        assert existing.brief_status == PRPartyBriefStatus.READY
        assert existing.brief_what == "Adds the liturgical calendar importer."
        assert existing.brief_why == "The manual import was the last hand-run step."
        assert existing.brief_decisions == ["Chose ICS over CSV"]
        assert existing.brief_links == ["https://github.com/CatholicOS/liturgy/pull/7/files"]
        assert existing.brief_truncated is True
        assert existing.ready_at == NOW - timedelta(hours=1)
        assert existing.brewing_since == NOW - timedelta(hours=2)

    async def test_seeing_a_pr_clears_missing_since(self) -> None:
        existing = _existing_pr()
        existing.missing_since = NOW - timedelta(hours=3)
        db = _FakeSession([existing])

        facts = facts_from_detail(_detail(), rollup=ChecksRollup.SUCCESS)
        await upsert_pr(db, facts, registry=_registry(), now=NOW)  # type: ignore[arg-type]

        assert existing.missing_since is None

    async def test_draft_pr_never_enters(self) -> None:
        db = _FakeSession()
        facts = facts_from_detail(_detail(draft=True), rollup=ChecksRollup.SUCCESS)
        result = await upsert_pr(db, facts, registry=_registry(), now=NOW)  # type: ignore[arg-type]

        assert result.pr is None
        assert result.created is False
        assert result.skipped_reason == "draft"
        assert db.rows == []

    async def test_converted_to_draft_parks_an_existing_row(self) -> None:
        existing = _existing_pr()
        db = _FakeSession([existing])
        facts = facts_from_detail(_detail(draft=True), rollup=ChecksRollup.SUCCESS)
        result = await upsert_pr(db, facts, registry=_registry(), now=NOW)  # type: ignore[arg-type]

        assert result.parked is True
        assert existing.state == PR_STATE_DRAFT

    async def test_ready_for_review_admits_the_pr(self) -> None:
        db = _FakeSession()
        facts = facts_from_webhook_pr(_webhook_payload(action="ready_for_review", draft=False))
        assert facts is not None
        result = await upsert_pr(db, facts, registry=_registry(), now=NOW)  # type: ignore[arg-type]

        assert result.created is True
        assert result.pr is not None
        assert result.pr.state == PR_STATE_OPEN

    async def test_closed_and_merged_states_are_recorded(self) -> None:
        existing = _existing_pr()
        db = _FakeSession([existing])

        facts = facts_from_detail(_detail(state="closed", merged=True), rollup=ChecksRollup.SUCCESS)
        await upsert_pr(db, facts, registry=_registry(), now=NOW)  # type: ignore[arg-type]
        assert existing.state == PR_STATE_MERGED

        facts = facts_from_detail(
            _detail(state="closed", merged=False), rollup=ChecksRollup.SUCCESS
        )
        await upsert_pr(db, facts, registry=_registry(), now=NOW)  # type: ignore[arg-type]
        assert existing.state == PR_STATE_CLOSED

    async def test_webhook_and_sweep_converge_on_one_row(self) -> None:
        """KTD14: one authoritative key, so double delivery is a no-op."""
        db = _FakeSession()
        await upsert_pr(  # type: ignore[arg-type]
            db,
            facts_from_webhook_pr(_webhook_payload(action="opened")),  # type: ignore[arg-type]
            registry=_registry(),
            now=NOW,
        )
        await upsert_pr(  # type: ignore[arg-type]
            db,
            facts_from_detail(_detail(), rollup=ChecksRollup.SUCCESS),
            registry=_registry(),
            now=NOW,
        )
        prs = [r for r in db.rows if isinstance(r, PRPartyPR)]
        assert len(prs) == 1

    async def test_webhook_facts_leave_checks_alone(self) -> None:
        """A ``pull_request`` payload carries no check runs — don't blank the column."""
        existing = _existing_pr()
        existing.checks_rollup = "success"
        db = _FakeSession([existing])

        facts = facts_from_webhook_pr(_webhook_payload(action="edited"))
        assert facts is not None
        assert facts.checks_known is False
        await upsert_pr(db, facts, registry=_registry(), now=NOW)  # type: ignore[arg-type]

        assert existing.checks_rollup == "success"

    async def test_bot_authored_pr_is_classified_bot(self) -> None:
        db = _FakeSession()
        facts = facts_from_detail(
            _detail(login="dependabot[bot]", node_id="BOT_1", user_type="Bot"),
            rollup=ChecksRollup.SUCCESS,
        )
        result = await upsert_pr(db, facts, registry=_registry(), now=NOW)  # type: ignore[arg-type]
        assert result.pr is not None
        assert result.pr.author_kind == PRPartyAuthorKind.BOT

    async def test_registry_author_persists_identity_for_per_caller_derivation(self) -> None:
        reviewer = _reviewer(login="damienriehl", node_id="NODE_A")
        db = _FakeSession([reviewer])
        facts = facts_from_detail(
            _detail(login="damienriehl", node_id="NODE_A"), rollup=ChecksRollup.SUCCESS
        )
        result = await upsert_pr(  # type: ignore[arg-type]
            db, facts, registry=_registry(reviewer), now=NOW
        )
        assert result.pr is not None
        assert result.pr.author_kind == PRPartyAuthorKind.COUNTERPART
        # U15 needs both to answer own-vs-counterpart for a given caller.
        assert result.pr.author_github_login == "damienriehl"
        assert result.pr.author_node_id == "NODE_A"


# ---------------------------------------------------------------------------
# Brief enqueue seam (U5)
# ---------------------------------------------------------------------------


class TestBriefEnqueue:
    def test_job_id_is_revision_scoped(self) -> None:
        assert brief_job_id(REPO, 7, "a" * 40) == f"brief:{REPO}#7:{'a' * 40}"

    async def test_new_revision_enqueues_one_brief_job(self) -> None:
        db = _FakeSession()
        pool = _FakePool()
        facts = facts_from_detail(_detail(), rollup=ChecksRollup.PENDING)
        result = await upsert_pr(  # type: ignore[arg-type]
            db, facts, registry=_registry(), now=NOW, pool=pool
        )
        assert result.enqueued is True
        assert pool.job_ids == [f"brief:{REPO}#7:{'a' * 40}"]
        args, _kwargs = pool.jobs[0]
        # The contract U5 registers against: (pr_id, repo, number, head_sha).
        assert args[0] == BRIEF_TASK_NAME
        assert args[1] == str(result.pr.id)  # type: ignore[union-attr]
        assert args[2:] == (REPO, 7, "a" * 40)

    async def test_same_head_resweep_does_not_re_enqueue(self) -> None:
        existing = _existing_pr(brief_status=PRPartyBriefStatus.READY)
        db = _FakeSession([existing])
        pool = _FakePool()
        facts = facts_from_detail(_detail(), rollup=ChecksRollup.SUCCESS)
        result = await upsert_pr(  # type: ignore[arg-type]
            db, facts, registry=_registry(), now=NOW, pool=pool
        )
        assert result.enqueued is False
        assert pool.jobs == []

    async def test_bot_pr_gets_no_brief_job(self) -> None:
        """R19: bot PRs render as link-only rows; no LLM spend on them."""
        db = _FakeSession()
        pool = _FakePool()
        facts = facts_from_detail(
            _detail(login="dependabot[bot]", node_id="BOT_1", user_type="Bot"),
            rollup=ChecksRollup.SUCCESS,
        )
        result = await upsert_pr(  # type: ignore[arg-type]
            db, facts, registry=_registry(), now=NOW, pool=pool
        )
        assert result.enqueued is False
        assert pool.jobs == []

    async def test_enqueue_failure_does_not_lose_the_row(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        db = _FakeSession()
        pool = _FakePool(fail=True)
        facts = facts_from_detail(_detail(), rollup=ChecksRollup.PENDING)
        with caplog.at_level(logging.WARNING):
            result = await upsert_pr(  # type: ignore[arg-type]
                db, facts, registry=_registry(), now=NOW, pool=pool
            )
        assert result.pr is not None
        assert result.enqueued is False
        assert any("brief job" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Brewing timeout (R17)
# ---------------------------------------------------------------------------


class TestBrewingTimeout:
    async def test_brewing_past_ninety_minutes_becomes_ready_with_warning(self) -> None:
        stale = _existing_pr(brief_status=PRPartyBriefStatus.BREWING)
        stale.brewing_since = NOW - BREWING_TIMEOUT - timedelta(minutes=1)
        db = _FakeSession([stale])

        transitions = await apply_brewing_timeout(db, now=NOW)  # type: ignore[arg-type]

        assert stale.brief_status == PRPartyBriefStatus.READY_WITH_WARNING
        assert stale.ready_at == NOW
        assert [t.pr_number for t in transitions] == [7]
        assert transitions[0].brief_status == PRPartyBriefStatus.READY_WITH_WARNING

    async def test_brewing_within_the_window_is_left_alone(self) -> None:
        fresh = _existing_pr(brief_status=PRPartyBriefStatus.BREWING)
        fresh.brewing_since = NOW - timedelta(minutes=89)
        db = _FakeSession([fresh])

        transitions = await apply_brewing_timeout(db, now=NOW)  # type: ignore[arg-type]

        assert fresh.brief_status == PRPartyBriefStatus.BREWING
        assert fresh.ready_at is None
        assert transitions == []

    async def test_timeout_fires_the_ready_hook_seam_for_u9(self) -> None:
        stale = _existing_pr(brief_status=PRPartyBriefStatus.BREWING)
        stale.brewing_since = NOW - BREWING_TIMEOUT - timedelta(minutes=1)
        db = _FakeSession([stale])

        seen: list[Any] = []

        async def _hook(transition: Any) -> None:
            seen.append(transition)

        ready_transition_hooks.append(_hook)
        try:
            await apply_brewing_timeout(db, now=NOW)  # type: ignore[arg-type]
        finally:
            ready_transition_hooks.remove(_hook)

        assert len(seen) == 1
        assert seen[0].pr_number == 7

    async def test_a_failing_hook_cannot_break_the_sweep(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        stale = _existing_pr(brief_status=PRPartyBriefStatus.BREWING)
        stale.brewing_since = NOW - BREWING_TIMEOUT - timedelta(minutes=1)
        db = _FakeSession([stale])

        async def _boom(_transition: Any) -> None:
            raise RuntimeError("ntfy is down")

        ready_transition_hooks.append(_boom)
        try:
            with caplog.at_level(logging.WARNING):
                transitions = await apply_brewing_timeout(db, now=NOW)  # type: ignore[arg-type]
        finally:
            ready_transition_hooks.remove(_boom)

        assert len(transitions) == 1
        assert stale.brief_status == PRPartyBriefStatus.READY_WITH_WARNING


# ---------------------------------------------------------------------------
# Sweep (KTD14)
# ---------------------------------------------------------------------------


class TestSweep:
    async def test_new_pr_via_sweep_is_created_brewing(self) -> None:
        db = _FakeSession()
        client = _FakeClient(pages=[[_searched()]], details={(REPO, 7): _detail()})

        result = await sweep_open_prs(  # type: ignore[arg-type]
            db, client=client, org="CatholicOS", now=NOW
        )

        assert result.total == 1
        assert result.created == 1
        assert result.errors == 0
        prs = [r for r in db.rows if isinstance(r, PRPartyPR)]
        assert len(prs) == 1
        assert prs[0].brief_status == PRPartyBriefStatus.BREWING

    async def test_unchanged_updated_at_skips_the_detail_fetch(self) -> None:
        """KTD14's call budget: 1 search + 3 calls per *changed* PR, not per PR."""
        stamp = NOW - timedelta(minutes=30)
        existing = _existing_pr(updated_at=stamp)
        db = _FakeSession([existing])
        client = _FakeClient(pages=[[_searched(updated_at=stamp)]], details={(REPO, 7): _detail()})

        result = await sweep_open_prs(  # type: ignore[arg-type]
            db, client=client, org="CatholicOS", now=NOW
        )

        assert client.search_calls == 1
        assert client.detail_calls == []
        assert client.rollup_calls == []
        assert result.detail_fetches == 0
        assert result.total == 1

    async def test_moved_updated_at_triggers_detail_and_checks(self) -> None:
        existing = _existing_pr(updated_at=NOW - timedelta(hours=2))
        db = _FakeSession([existing])
        client = _FakeClient(
            pages=[[_searched(updated_at=NOW - timedelta(minutes=1))]],
            details={(REPO, 7): _detail(head_sha="b" * 40)},
            rollups={"b" * 40: ChecksRollup.PENDING},
        )

        result = await sweep_open_prs(  # type: ignore[arg-type]
            db, client=client, org="CatholicOS", now=NOW
        )

        assert client.detail_calls == [(REPO, 7)]
        assert client.rollup_calls == ["b" * 40]
        assert result.detail_fetches == 1
        assert existing.head_sha == "b" * 40
        assert existing.checks_rollup == "pending"

    async def test_one_failing_pr_does_not_abort_the_sweep(self) -> None:
        db = _FakeSession()
        detail_map = {(REPO, 8): _detail(number=8)}
        client = _FakeClient(pages=[[_searched(number=7), _searched(number=8)]], details=detail_map)

        result = await sweep_open_prs(  # type: ignore[arg-type]
            db, client=client, org="CatholicOS", now=NOW
        )

        assert result.total == 2
        assert result.errors == 1
        assert result.synced == 1
        prs = [r for r in db.rows if isinstance(r, PRPartyPR)]
        assert [p.pr_number for p in prs] == [8]

    async def test_discovery_paginates_until_a_short_page(self) -> None:
        full_page = [
            _searched(number=n, updated_at=NOW - timedelta(minutes=30)) for n in range(100)
        ]
        rows = [_existing_pr(number=n, updated_at=NOW - timedelta(minutes=30)) for n in range(100)]
        db = _FakeSession(list(rows))
        client = _FakeClient(pages=[full_page, [_searched(number=500)]])
        client.details = {(REPO, 500): _detail(number=500)}

        result = await sweep_open_prs(  # type: ignore[arg-type]
            db, client=client, org="CatholicOS", now=NOW
        )

        assert client.search_calls == 2
        assert result.total == 101

    async def test_missing_from_a_complete_search_stamps_missing_since(self) -> None:
        vanished = _existing_pr(number=9)
        present = _existing_pr(number=7, updated_at=NOW - timedelta(minutes=30))
        db = _FakeSession([vanished, present])
        client = _FakeClient(pages=[[_searched(number=7, updated_at=NOW - timedelta(minutes=30))]])

        result = await sweep_open_prs(  # type: ignore[arg-type]
            db, client=client, org="CatholicOS", now=NOW
        )

        assert vanished.missing_since == NOW
        assert present.missing_since is None
        assert result.missing_stamped == 1

    async def test_reappearance_clears_missing_since(self) -> None:
        back = _existing_pr(number=9, updated_at=NOW - timedelta(minutes=30))
        back.missing_since = NOW - timedelta(hours=1)
        db = _FakeSession([back])
        client = _FakeClient(pages=[[_searched(number=9, updated_at=NOW - timedelta(minutes=30))]])

        await sweep_open_prs(db, client=client, org="CatholicOS", now=NOW)  # type: ignore[arg-type]

        assert back.missing_since is None

    async def test_an_incomplete_search_never_stamps_missing_since(self) -> None:
        """Search is eventually consistent; a failed page is not evidence of absence."""
        row = _existing_pr(number=9)
        db = _FakeSession([row])
        client = _FakeClient(search_error=RuntimeError("502 from search"))

        result = await sweep_open_prs(  # type: ignore[arg-type]
            db, client=client, org="CatholicOS", now=NOW
        )

        assert row.missing_since is None
        assert result.discovery_complete is False
        assert result.errors == 1

    async def test_missing_threshold_needs_three_consecutive_misses(self) -> None:
        row = _existing_pr(number=9)
        row.missing_since = NOW - timedelta(minutes=10)
        # Three misses at a 5-minute cadence is 15 minutes of absence.
        assert missing_long_enough_to_retire(row, now=NOW, sweep_minutes=5) is False
        row.missing_since = NOW - timedelta(minutes=16)
        assert missing_long_enough_to_retire(row, now=NOW, sweep_minutes=5) is True
        assert MISSING_MISS_THRESHOLD == 3

    async def test_missing_never_retires_without_a_stamp(self) -> None:
        assert missing_long_enough_to_retire(_existing_pr(), now=NOW, sweep_minutes=5) is False

    async def test_draft_pr_in_discovery_does_not_create_a_row(self) -> None:
        db = _FakeSession()
        client = _FakeClient(
            pages=[[_searched(draft=True)]], details={(REPO, 7): _detail(draft=True)}
        )

        result = await sweep_open_prs(  # type: ignore[arg-type]
            db, client=client, org="CatholicOS", now=NOW
        )

        assert [r for r in db.rows if isinstance(r, PRPartyPR)] == []
        assert result.created == 0
        assert result.skipped == 1

    async def test_sweep_runs_the_brewing_timeout(self) -> None:
        stale = _existing_pr(number=9, brief_status=PRPartyBriefStatus.BREWING)
        stale.brewing_since = NOW - BREWING_TIMEOUT - timedelta(minutes=5)
        stale.missing_since = None
        db = _FakeSession([stale])
        client = _FakeClient(pages=[[_searched(number=9, updated_at=stale.updated_at_github)]])

        result = await sweep_open_prs(  # type: ignore[arg-type]
            db, client=client, org="CatholicOS", now=NOW
        )

        assert stale.brief_status == PRPartyBriefStatus.READY_WITH_WARNING
        assert result.timed_out == 1
        assert len(result.transitions) == 1

    async def test_unconfigured_generation_token_is_a_clean_skip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ontokit.core.config import settings as app_settings

        monkeypatch.setattr(app_settings, "pr_party_readonly_token", "", raising=False)
        db = _FakeSession()

        result = await sweep_open_prs(db, org="CatholicOS", now=NOW)  # type: ignore[arg-type]

        assert result.skipped_reason == "no_generation_token"
        assert result.errors == 0
        assert result.as_dict()["total"] == 0

    async def test_sweep_after_brief_fold_leaves_the_brief_intact(self) -> None:
        """B3's DB form, through the real sweep entry point.

        The PR moved (so the sweep *does* detail-fetch and rewrite poller
        columns) but the head did not, so the folded brief must survive whole.
        """
        row = _existing_pr(updated_at=NOW - timedelta(hours=2))
        row.brief_status = PRPartyBriefStatus.READY
        row.brief_what = "Ports the psalter importer."
        row.brief_why = "Last hand-run step in the pipeline."
        row.brief_decisions = ["Kept the ICS parser"]
        row.brief_links = ["https://github.com/CatholicOS/liturgy/pull/7/files"]
        row.brief_truncated = False
        row.ready_at = NOW - timedelta(hours=1)
        db = _FakeSession([row])
        client = _FakeClient(
            pages=[[_searched(updated_at=NOW - timedelta(minutes=1))]],
            details={(REPO, 7): _detail(mergeable_state="behind", updated_at=NOW)},
            rollups={"a" * 40: ChecksRollup.FAILURE},
        )
        pool = _FakePool()

        result = await sweep_open_prs(  # type: ignore[arg-type]
            db, client=client, org="CatholicOS", now=NOW, pool=pool
        )

        assert result.detail_fetches == 1
        assert row.mergeable_state == "behind"
        assert row.checks_rollup == "failure"
        assert row.brief_status == PRPartyBriefStatus.READY
        assert row.brief_what == "Ports the psalter importer."
        assert row.brief_decisions == ["Kept the ICS parser"]
        assert row.ready_at == NOW - timedelta(hours=1)
        # Same revision, so no second brief job either.
        assert pool.jobs == []

    async def test_result_shape_matches_the_cron_convention(self) -> None:
        db = _FakeSession()
        client = _FakeClient(pages=[[_searched()]], details={(REPO, 7): _detail()})
        result = await sweep_open_prs(  # type: ignore[arg-type]
            db, client=client, org="CatholicOS", now=NOW
        )
        payload = result.as_dict()
        assert {"total", "synced", "errors"} <= set(payload)


# ---------------------------------------------------------------------------
# Webhook event dispatch (the same upsert path)
# ---------------------------------------------------------------------------


@dataclass
class _Dispatch:
    db: _FakeSession
    pool: _FakePool
    client: _FakeClient


@pytest.fixture
def dispatch() -> _Dispatch:
    return _Dispatch(_FakeSession(), _FakePool(), _FakeClient(details={(REPO, 7): _detail()}))


class TestWebhookDispatch:
    async def test_opened_creates_the_row(self, dispatch: _Dispatch) -> None:
        outcome = await handle_webhook_event(
            dispatch.db,  # type: ignore[arg-type]
            "pull_request",
            _webhook_payload(action="opened"),
            pool=dispatch.pool,
            client=dispatch.client,
            now=NOW,
        )
        assert outcome["status"] == "processed"
        prs = [r for r in dispatch.db.rows if isinstance(r, PRPartyPR)]
        assert len(prs) == 1
        # The fast path upserts from the payload — no extra detail call.
        assert dispatch.client.detail_calls == []

    async def test_synchronize_supersedes_the_head_and_rebrews(self, dispatch: _Dispatch) -> None:
        existing = _existing_pr(brief_status=PRPartyBriefStatus.READY)
        dispatch.db.rows.append(existing)

        await handle_webhook_event(
            dispatch.db,  # type: ignore[arg-type]
            "pull_request",
            _webhook_payload(action="synchronize", head_sha="c" * 40),
            pool=dispatch.pool,
            client=dispatch.client,
            now=NOW,
        )

        assert existing.head_sha == "c" * 40
        assert existing.brief_status == PRPartyBriefStatus.BREWING
        assert dispatch.pool.job_ids == [f"brief:{REPO}#7:{'c' * 40}"]

    async def test_converted_to_draft_parks(self, dispatch: _Dispatch) -> None:
        existing = _existing_pr()
        dispatch.db.rows.append(existing)

        await handle_webhook_event(
            dispatch.db,  # type: ignore[arg-type]
            "pull_request",
            _webhook_payload(action="converted_to_draft", draft=True),
            pool=dispatch.pool,
            client=dispatch.client,
            now=NOW,
        )

        assert existing.state == PR_STATE_DRAFT

    async def test_uninteresting_pull_request_action_is_ignored(self, dispatch: _Dispatch) -> None:
        outcome = await handle_webhook_event(
            dispatch.db,  # type: ignore[arg-type]
            "pull_request",
            _webhook_payload(action="labeled"),
            pool=dispatch.pool,
            client=dispatch.client,
            now=NOW,
        )
        assert outcome["status"] == "ignored"
        assert [r for r in dispatch.db.rows if isinstance(r, PRPartyPR)] == []

    async def test_review_event_triggers_a_single_pr_refresh(self, dispatch: _Dispatch) -> None:
        payload = _webhook_payload(action="submitted")
        payload["review"] = {"state": "approved"}

        outcome = await handle_webhook_event(
            dispatch.db,  # type: ignore[arg-type]
            "pull_request_review",
            payload,
            pool=dispatch.pool,
            client=dispatch.client,
            now=NOW,
        )

        assert outcome["status"] == "processed"
        assert dispatch.client.detail_calls == [(REPO, 7)]

    async def test_check_suite_refreshes_each_attached_pr(self, dispatch: _Dispatch) -> None:
        payload = {
            "action": "completed",
            "repository": {"full_name": REPO},
            "check_suite": {"head_sha": "a" * 40, "pull_requests": [{"number": 7}]},
        }
        await handle_webhook_event(
            dispatch.db,  # type: ignore[arg-type]
            "check_suite",
            payload,
            pool=dispatch.pool,
            client=dispatch.client,
            now=NOW,
        )
        assert dispatch.client.detail_calls == [(REPO, 7)]
        assert dispatch.client.rollup_calls == ["a" * 40]

    async def test_issue_comment_hands_off_to_the_u7_seam(self, dispatch: _Dispatch) -> None:
        from ontokit.services.pr_party_intake import issue_comment_hooks

        seen: list[dict[str, Any]] = []

        async def _hook(payload: dict[str, Any]) -> None:
            seen.append(payload)

        issue_comment_hooks.append(_hook)
        try:
            outcome = await handle_webhook_event(
                dispatch.db,  # type: ignore[arg-type]
                "issue_comment",
                {"action": "created", "repository": {"full_name": REPO}},
                pool=dispatch.pool,
                client=dispatch.client,
                now=NOW,
            )
        finally:
            issue_comment_hooks.remove(_hook)

        assert outcome["status"] == "deferred"
        assert len(seen) == 1

    async def test_push_is_a_no_op(self, dispatch: _Dispatch) -> None:
        outcome = await handle_webhook_event(
            dispatch.db,  # type: ignore[arg-type]
            "push",
            {"ref": "refs/heads/main"},
            pool=dispatch.pool,
            client=dispatch.client,
            now=NOW,
        )
        assert outcome["status"] == "ignored"

    async def test_unknown_event_is_ignored_not_an_error(self, dispatch: _Dispatch) -> None:
        outcome = await handle_webhook_event(
            dispatch.db,  # type: ignore[arg-type]
            "deployment_status",
            {},
            pool=dispatch.pool,
            client=dispatch.client,
            now=NOW,
        )
        assert outcome["status"] == "ignored"

    async def test_review_event_without_a_client_degrades_to_the_payload(self) -> None:
        """No generation token configured: the embedded PR object is still usable."""
        db = _FakeSession()
        payload = _webhook_payload(action="submitted")
        payload["review"] = {"state": "approved"}

        outcome = await handle_webhook_event(
            db,  # type: ignore[arg-type]
            "pull_request_review",
            payload,
            pool=None,
            client=None,
            now=NOW,
        )

        assert outcome["status"] == "processed"
        assert len([r for r in db.rows if isinstance(r, PRPartyPR)]) == 1
