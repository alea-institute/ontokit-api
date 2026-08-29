"""Tests for PR Party ready notifications (U9, R22/R27, KTD20).

The properties worth pinning here are the ones whose failure is either silent
or loud in the wrong direction:

- **Exactly once per reviewer per revision.** The guarantee is a partial unique
  index on ``(user_id, type, target_id)``; this module's job is to *hit* it with
  the right ``target_id`` (``{repo}#{number}:{head_sha}``) and to treat the
  resulting violation as a no-op rather than an error. A re-swept card that
  re-pings every cycle is how a reviewer learns to ignore the channel.
- **A new head SHA is a new revision.** Same reviewer, new SHA, new ping.
- **A reviewer registered after the first ping still gets their first one.**
  The uniqueness key is per reviewer, not per card, so a late registration is
  not swallowed by "this revision already notified".
- **ntfy payloads carry no PR-derived text (KTD20).** A topic is a bearer
  capability in a URL — anyone holding it can read the channel — so the message
  is a fixed title plus the card link, never the repo name, PR title, or brief.
- **The bell tolerates a NULL project (F5).** PR Party notifications belong to
  no OntoKit project. The response schema used to require ``project_id`` /
  ``project_name``, which would 500 the *entire* notification list the first
  time one of these rows appeared on a page — including every project
  notification sharing that page.

The DB is a small fake rather than an ``AsyncMock``: the behavior under test is
which rows were inserted and which insert collided, which a mock returning one
canned result per ``execute`` cannot express.
"""

from __future__ import annotations

import logging
import operator
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy.exc import IntegrityError

from ontokit.models.notification import Notification
from ontokit.models.pr_party import (
    PRPartyAuthorKind,
    PRPartyBriefStatus,
    PRPartyPR,
    PRPartyReviewer,
)
from ontokit.schemas.notification import NotificationResponse
from ontokit.services.notification_service import NotificationService
from ontokit.services.pr_party_intake import ReadyTransition, ready_transition_hooks
from ontokit.services.pr_party_notifications import (
    NOTIFICATION_TYPE,
    NTFY_FAILURE_MARKER,
    NTFY_TITLE,
    card_path,
    emit_ready_notification,
    notify_ready_transition,
    publish_ntfy,
    register_ready_hook,
    revision_target_id,
)

REPO = "CatholicOS/liturgy"
PR_NUMBER = 42
SHA_A = "a" * 40
SHA_B = "b" * 40
NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fake session
# ---------------------------------------------------------------------------


def _clause_value(node: Any, row: Any) -> Any:
    if hasattr(node, "value") and not hasattr(node, "table"):
        return node.value
    if hasattr(node, "key"):
        return getattr(row, node.key)
    return node


_COMPARATORS: dict[str, Any] = {
    "eq": operator.eq,
    "ne": operator.ne,
    "is_": lambda a, b: a is b,
    "is_not": lambda a, b: a is not b,
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

    def scalar(self) -> Any:
        return self._rows[0] if self._rows else None

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class _Savepoint:
    """``begin_nested()`` stand-in: discards rows added inside it on error."""

    def __init__(self, session: _FakeSession) -> None:
        self.session = session
        self._mark = 0

    async def __aenter__(self) -> _Savepoint:
        self._mark = len(self.session.rows)
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is not None:
            del self.session.rows[self._mark :]
            self.session.rollbacks += 1
        return False


class _FakeSession:
    """AsyncSession stand-in that enforces the partial unique index (U1).

    ``flush`` raises ``IntegrityError`` for a second ``pr_party_ready`` row with
    the same ``(user_id, type, target_id)`` — the constraint this module relies
    on for its once-per-revision guarantee.
    """

    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows: list[Any] = list(rows or [])
        self.commits = 0
        self.rollbacks = 0
        self._committed_keys: set[tuple[str, str, str | None]] = set()
        self._flushed: set[int] = set()

    async def execute(self, stmt: Any) -> _FakeResult:
        entity = stmt.column_descriptions[0]["entity"]
        candidates = [r for r in self.rows if isinstance(r, entity)]
        return _FakeResult([r for r in candidates if _matches(stmt.whereclause, r)])

    def add(self, obj: Any) -> None:
        self.rows.append(obj)

    def begin_nested(self) -> _Savepoint:
        return _Savepoint(self)

    async def flush(self) -> None:
        for row in self.rows:
            if not isinstance(row, Notification) or row.type != NOTIFICATION_TYPE:
                continue
            if id(row) in self._flushed:
                continue  # already written; only pending rows can collide
            key = (row.user_id, row.type, row.target_id)
            if key in self._committed_keys:
                raise IntegrityError("INSERT", {}, Exception("duplicate key"))
            self._committed_keys.add(key)
            self._flushed.add(id(row))

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    @property
    def notifications(self) -> list[Notification]:
        return [r for r in self.rows if isinstance(r, Notification)]


class _Recorder:
    """Publisher stand-in: records ``(topic, link)`` instead of sending."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self._fail = fail

    async def __call__(self, topic: str, link: str) -> None:
        self.calls.append((topic, link))
        if self._fail:
            raise httpx.ConnectError("ntfy is unreachable")


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _reviewer(
    login: str,
    *,
    node_id: str | None = None,
    topic: str | None = None,
) -> PRPartyReviewer:
    row = PRPartyReviewer(
        zitadel_user_id=f"zid-{login}",
        github_login=login,
        github_node_id=node_id,
        ntfy_topic=topic,
    )
    row.id = uuid.uuid4()
    return row


def _card(
    *,
    author_login: str | None = "outsider",
    author_node_id: str | None = "MDQ6VXNlck9VVA==",
    head_sha: str = SHA_A,
) -> PRPartyPR:
    row = PRPartyPR(
        repo_full_name=REPO,
        pr_number=PR_NUMBER,
        title="Add the Sanctoral cycle to the liturgy ontology",
        head_sha=head_sha,
        state="open",
        author_kind=PRPartyAuthorKind.COUNTERPART.value,
        author_github_login=author_login,
        author_node_id=author_node_id,
        brief_status=PRPartyBriefStatus.READY.value,
    )
    row.id = uuid.uuid4()
    return row


def _transition(card: PRPartyPR, *, head_sha: str | None = None) -> ReadyTransition:
    return ReadyTransition(
        pr_id=card.id,
        repo_full_name=card.repo_full_name,
        pr_number=card.pr_number,
        head_sha=head_sha or card.head_sha,
        brief_status=PRPartyBriefStatus.READY.value,
        reason="brief_ready",
    )


# ---------------------------------------------------------------------------
# Routing / recipients
# ---------------------------------------------------------------------------


class TestRecipients:
    @pytest.mark.asyncio
    async def test_notifies_every_registered_reviewer_for_a_third_party_pr(self) -> None:
        """A stranger's PR routes to the whole registry (R22)."""
        card = _card()
        reviewers = [_reviewer("damienriehl", node_id="N1"), _reviewer("mjbommar", node_id="N2")]
        db = _FakeSession([card, *reviewers])
        publish = _Recorder()

        notified = await emit_ready_notification(db, _transition(card), publish=publish)

        assert sorted(notified) == ["zid-damienriehl", "zid-mjbommar"]
        assert len(db.notifications) == 2
        assert db.commits == 1

    @pytest.mark.asyncio
    async def test_excludes_the_author_of_their_own_pr(self) -> None:
        """An own-PR card is a read-only strip; its author is not a recipient."""
        author = _reviewer("damienriehl", node_id="N1")
        other = _reviewer("mjbommar", node_id="N2")
        card = _card(author_login="damienriehl", author_node_id="N1")
        db = _FakeSession([card, author, other])

        notified = await emit_ready_notification(db, _transition(card), publish=_Recorder())

        assert notified == ["zid-mjbommar"]

    @pytest.mark.asyncio
    async def test_author_match_falls_back_to_login_without_node_ids(self) -> None:
        """Node-id-first, login fallback — a NULL node id must not re-notify."""
        author = _reviewer("damienriehl", node_id=None)
        card = _card(author_login="DamienRiehl", author_node_id=None)
        db = _FakeSession([card, author])

        notified = await emit_ready_notification(db, _transition(card), publish=_Recorder())

        assert notified == []
        assert db.notifications == []

    @pytest.mark.asyncio
    async def test_unknown_card_is_a_logged_no_op(self, caplog: pytest.LogCaptureFixture) -> None:
        """A transition whose row vanished must not raise into the caller."""
        db = _FakeSession([_reviewer("damienriehl", node_id="N1")])
        orphan = ReadyTransition(
            pr_id=uuid.uuid4(),
            repo_full_name=REPO,
            pr_number=PR_NUMBER,
            head_sha=SHA_A,
            brief_status=PRPartyBriefStatus.READY.value,
            reason="brewing_timeout",
        )

        with caplog.at_level(logging.WARNING):
            notified = await emit_ready_notification(db, orphan, publish=_Recorder())

        assert notified == []
        assert db.notifications == []
        assert any("PR Party" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Once per revision
# ---------------------------------------------------------------------------


class TestOncePerRevision:
    @pytest.mark.asyncio
    async def test_refiring_the_same_revision_creates_no_duplicate(self) -> None:
        """The partial unique index is the guarantee; a violation is a no-op."""
        card = _card()
        db = _FakeSession([card, _reviewer("damienriehl", node_id="N1")])
        publish = _Recorder()

        first = await emit_ready_notification(db, _transition(card), publish=publish)
        second = await emit_ready_notification(db, _transition(card), publish=publish)

        assert first == ["zid-damienriehl"]
        assert second == []
        assert len(db.notifications) == 1
        assert len(publish.calls) == 0  # no topic registered on this reviewer

    @pytest.mark.asyncio
    async def test_a_new_head_sha_notifies_again(self) -> None:
        """A re-push is a new revision — same reviewer, new ping."""
        card = _card()
        db = _FakeSession([card, _reviewer("damienriehl", node_id="N1")])

        await emit_ready_notification(db, _transition(card), publish=_Recorder())
        card.head_sha = SHA_B
        again = await emit_ready_notification(
            db, _transition(card, head_sha=SHA_B), publish=_Recorder()
        )

        assert again == ["zid-damienriehl"]
        assert len(db.notifications) == 2
        assert {n.target_id for n in db.notifications} == {
            revision_target_id(REPO, PR_NUMBER, SHA_A),
            revision_target_id(REPO, PR_NUMBER, SHA_B),
        }

    @pytest.mark.asyncio
    async def test_a_later_registered_reviewer_gets_their_first_ping(self) -> None:
        """Uniqueness is per reviewer, so a late registration is not swallowed."""
        card = _card()
        early = _reviewer("damienriehl", node_id="N1", topic="early-topic")
        db = _FakeSession([card, early])
        publish = _Recorder()

        await emit_ready_notification(db, _transition(card), publish=publish)

        late = _reviewer("mjbommar", node_id="N2", topic="late-topic")
        db.rows.append(late)
        second = await emit_ready_notification(db, _transition(card), publish=publish)

        assert second == ["zid-mjbommar"]
        assert len(db.notifications) == 2
        assert [topic for topic, _link in publish.calls] == ["early-topic", "late-topic"]


# ---------------------------------------------------------------------------
# Row shape (what U12's bell renders)
# ---------------------------------------------------------------------------


class TestRowShape:
    @pytest.mark.asyncio
    async def test_row_carries_the_card_deep_link_and_no_project(self) -> None:
        card = _card()
        db = _FakeSession([card, _reviewer("damienriehl", node_id="N1")])

        await emit_ready_notification(db, _transition(card), publish=_Recorder())

        row = db.notifications[0]
        assert row.type == NOTIFICATION_TYPE
        assert row.target_id == f"{REPO}#{PR_NUMBER}:{SHA_A}"
        assert row.target_url == f"/pr-party?card={card.id}"
        assert row.target_url == card_path(card.id)
        assert row.project_id is None
        assert row.project_name is None
        assert row.is_read is False or row.is_read is None

    @pytest.mark.asyncio
    async def test_title_and_body_carry_no_pr_derived_text(self) -> None:
        """KTD20: the visible copy is fixed; the card link carries the identity."""
        card = _card()
        db = _FakeSession([card, _reviewer("damienriehl", node_id="N1")])

        await emit_ready_notification(db, _transition(card), publish=_Recorder())

        row = db.notifications[0]
        visible = f"{row.title} {row.body or ''}"
        assert row.title == NTFY_TITLE
        assert REPO not in visible
        assert "Sanctoral" not in visible
        assert str(PR_NUMBER) not in visible


# ---------------------------------------------------------------------------
# ntfy (KTD20)
# ---------------------------------------------------------------------------


class TestNtfy:
    @pytest.mark.asyncio
    async def test_publishes_only_to_reviewers_with_a_topic(self) -> None:
        card = _card()
        with_topic = _reviewer("damienriehl", node_id="N1", topic="damien-topic")
        without_topic = _reviewer("mjbommar", node_id="N2", topic=None)
        db = _FakeSession([card, with_topic, without_topic])
        publish = _Recorder()

        notified = await emit_ready_notification(db, _transition(card), publish=publish)

        assert sorted(notified) == ["zid-damienriehl", "zid-mjbommar"]
        assert len(db.notifications) == 2  # in-app lands either way
        assert publish.calls == [("damien-topic", card_path(card.id))]

    @pytest.mark.asyncio
    async def test_published_payload_is_the_link_only(self) -> None:
        card = _card()
        db = _FakeSession([card, _reviewer("damienriehl", node_id="N1", topic="damien-topic")])
        publish = _Recorder()

        await emit_ready_notification(db, _transition(card), publish=publish)

        _topic, link = publish.calls[0]
        assert link == card_path(card.id)
        assert REPO not in link
        assert "Sanctoral" not in link

    @pytest.mark.asyncio
    async def test_publish_failure_leaves_the_notification_committed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        card = _card()
        db = _FakeSession([card, _reviewer("damienriehl", node_id="N1", topic="damien-topic")])
        publish = _Recorder(fail=True)

        with caplog.at_level(logging.WARNING):
            notified = await emit_ready_notification(db, _transition(card), publish=publish)

        assert notified == ["zid-damienriehl"]
        assert len(db.notifications) == 1
        assert db.commits == 1
        assert any(NTFY_FAILURE_MARKER in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_no_base_url_skips_publishing_entirely(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ontokit.core.config import settings

        monkeypatch.setattr(settings, "pr_party_ntfy_base_url", "")
        card = _card()
        db = _FakeSession([card, _reviewer("damienriehl", node_id="N1", topic="damien-topic")])
        publish = _Recorder()

        notified = await emit_ready_notification(db, _transition(card), publish=publish)

        assert notified == ["zid-damienriehl"]
        assert publish.calls == []

    @pytest.mark.asyncio
    async def test_publish_ntfy_posts_fixed_title_and_link_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ontokit.core.config import settings

        monkeypatch.setattr(settings, "pr_party_ntfy_base_url", "https://ntfy.example/")
        seen: list[httpx.Request] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200)

        sent = await publish_ntfy(
            "damien-topic", "/pr-party?card=abc", transport=httpx.MockTransport(_handler)
        )

        assert sent is True
        assert len(seen) == 1
        assert str(seen[0].url) == "https://ntfy.example/damien-topic"
        assert seen[0].headers["Title"] == NTFY_TITLE
        assert seen[0].content.decode() == "/pr-party?card=abc"

    @pytest.mark.asyncio
    async def test_publish_ntfy_rejects_a_topic_that_could_escape_its_segment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ontokit.core.config import settings

        monkeypatch.setattr(settings, "pr_party_ntfy_base_url", "https://ntfy.example")
        calls: list[httpx.Request] = []

        def _handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover — must not run
            calls.append(request)
            return httpx.Response(200)

        sent = await publish_ntfy(
            "../../admin", "/pr-party?card=abc", transport=httpx.MockTransport(_handler)
        )

        assert sent is False
        assert calls == []

    @pytest.mark.asyncio
    async def test_publish_ntfy_makes_no_call_without_a_base_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ontokit.core.config import settings

        monkeypatch.setattr(settings, "pr_party_ntfy_base_url", "")
        calls: list[httpx.Request] = []

        def _handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover — must not run
            calls.append(request)
            return httpx.Response(200)

        sent = await publish_ntfy(
            "damien-topic", "/pr-party?card=abc", transport=httpx.MockTransport(_handler)
        )

        assert sent is False
        assert calls == []


# ---------------------------------------------------------------------------
# Hook registration
# ---------------------------------------------------------------------------


class TestHookRegistration:
    def test_registration_is_idempotent(self) -> None:
        """Worker startup and API lifespan both call it; a double ping is not ok."""
        before = list(ready_transition_hooks)
        try:
            ready_transition_hooks[:] = [h for h in before if h is not notify_ready_transition]
            register_ready_hook()
            register_ready_hook()
            assert ready_transition_hooks.count(notify_ready_transition) == 1
        finally:
            ready_transition_hooks[:] = before


# ---------------------------------------------------------------------------
# The bell tolerates a NULL project (F5)
# ---------------------------------------------------------------------------


def _bell_row(*, project: bool) -> Notification:
    row = Notification(
        user_id="zid-damienriehl",
        type="pr_created" if project else NOTIFICATION_TYPE,
        title="New pull request" if project else NTFY_TITLE,
        body=None,
        project_id=uuid.uuid4() if project else None,
        project_name="Liturgy" if project else None,
        target_id=None if project else f"{REPO}#{PR_NUMBER}:{SHA_A}",
        target_url=None if project else f"/pr-party?card={uuid.uuid4()}",
    )
    row.id = uuid.uuid4()
    row.is_read = False
    row.created_at = NOW
    return row


class TestNullProjectSerialization:
    def test_response_schema_accepts_a_null_project_row(self) -> None:
        model = NotificationResponse.model_validate(_bell_row(project=False))

        assert model.project_id is None
        assert model.project_name is None
        assert model.type == NOTIFICATION_TYPE

    @pytest.mark.asyncio
    async def test_list_serializes_a_mixed_page(self) -> None:
        """F5: one PR Party row must not 500 the whole bell page."""
        rows = [_bell_row(project=False), _bell_row(project=True)]

        class _ListSession:
            def __init__(self) -> None:
                self.calls = 0

            async def execute(self, _stmt: Any) -> _FakeResult:
                self.calls += 1
                return _FakeResult(rows) if self.calls == 1 else _FakeResult([2])

        service = NotificationService(_ListSession())  # type: ignore[arg-type]
        result = await service.list_notifications("zid-damienriehl")

        assert len(result.items) == 2
        assert result.items[0].project_id is None
        assert result.items[1].project_id is not None
