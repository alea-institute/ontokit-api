"""Tests for the PR Party org webhook receiver (U4).

This is the only PR Party route with no authentication dependency: GitHub calls
it, and the HMAC over the raw body *is* the authentication. That inverts the
usual test emphasis — what matters most is everything that happens **before**
the payload is parsed:

- A missing or wrong ``X-Hub-Signature-256`` is a 401 and nothing is processed.
  The signature is verified against the exact bytes received, so the test signs
  real bodies and posts them as ``content=`` rather than ``json=`` (a
  re-serialized body would sign something the server never saw).
- With no secret configured the receiver is *not configured*, not open: 503.
- ``X-GitHub-Delivery`` dedupe makes a redelivery a no-op, and a Redis outage
  degrades to processing anyway — the upsert is idempotent (KTD14), so
  double-processing is strictly safer than dropping a delivery.

The dispatch itself is tested in ``test_pr_party_intake.py``; here it is a spy,
so a failure in this file always means the *receiver* changed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from ontokit.api.routes import include_pr_party_routes
from ontokit.core.config import settings
from ontokit.core.database import get_db
from ontokit.main import app
from ontokit.services.pr_party_intake import DELIVERY_KEY_PREFIX

URL = "/api/v1/pr-party/webhooks/github"
SECRET = "org-webhook-secret"


def _sign(secret: str, body: bytes) -> str:
    """Compute the GitHub webhook signature over the exact bytes sent."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class _Spy:
    """Records dispatches so the receiver can be tested without the intake path."""

    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.result = result or {"status": "processed"}

    async def __call__(
        self, _db: Any, event: str, payload: dict[str, Any], **_kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append((event, payload))
        return self.result


class _FakePool:
    """ArqRedis stand-in for the delivery-id dedupe."""

    def __init__(self, *, fail: bool = False) -> None:
        self.keys: dict[str, str] = {}
        self.fail = fail

    async def set(self, key: str, value: str, **_kwargs: Any) -> bool | None:
        if self.fail:
            raise RuntimeError("redis is down")
        if key in self.keys:
            return None
        self.keys[key] = value
        return True

    async def delete(self, key: str) -> int:
        if self.fail:
            raise RuntimeError("redis is down")
        return 1 if self.keys.pop(key, None) is not None else 0


@pytest.fixture
def receiver(monkeypatch: pytest.MonkeyPatch) -> Any:
    """(client, spy, pool) with a configured secret and a stubbed dispatcher."""
    monkeypatch.setattr(settings, "pr_party_webhook_secret", SECRET, raising=False)

    spy = _Spy()
    pool = _FakePool()
    monkeypatch.setattr("ontokit.api.routes.pr_party_webhooks.handle_webhook_event", spy)

    async def _pool() -> Any:
        return pool

    monkeypatch.setattr("ontokit.api.routes.pr_party_webhooks.get_arq_pool", _pool)

    async def _override_get_db() -> Any:
        yield object()

    app.dependency_overrides[get_db] = _override_get_db
    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield client, spy, pool
    finally:
        app.dependency_overrides.clear()


def _post(
    client: TestClient,
    payload: dict[str, Any] | None = None,
    *,
    event: str = "pull_request",
    secret: str | None = SECRET,
    signature: str | None = None,
    delivery: str | None = "delivery-1",
) -> Any:
    body = json.dumps(payload if payload is not None else {"action": "opened"}).encode()
    headers = {"x-github-event": event, "content-type": "application/json"}
    if signature is not None:
        headers["x-hub-signature-256"] = signature
    elif secret is not None:
        headers["x-hub-signature-256"] = _sign(secret, body)
    if delivery is not None:
        headers["x-github-delivery"] = delivery
    return client.post(URL, content=body, headers=headers)


# ---------------------------------------------------------------------------
# Authentication: the HMAC is the only credential
# ---------------------------------------------------------------------------


class TestSignature:
    def test_valid_signature_is_processed(self, receiver: Any) -> None:
        client, spy, _pool = receiver
        response = _post(client)
        assert response.status_code == 200
        assert len(spy.calls) == 1
        assert spy.calls[0][0] == "pull_request"

    def test_bad_signature_is_401_and_nothing_is_processed(self, receiver: Any) -> None:
        client, spy, _pool = receiver
        response = _post(client, secret="not-the-secret")
        assert response.status_code == 401
        assert spy.calls == []

    def test_missing_signature_is_401(self, receiver: Any) -> None:
        client, spy, _pool = receiver
        response = _post(client, secret=None)
        assert response.status_code == 401
        assert spy.calls == []

    def test_malformed_signature_header_is_401(self, receiver: Any) -> None:
        client, spy, _pool = receiver
        response = _post(client, signature="garbage-not-even-hex")
        assert response.status_code == 401
        assert spy.calls == []

    def test_signature_is_over_the_exact_bytes(self, receiver: Any) -> None:
        """Re-serializing the body must invalidate the signature."""
        client, spy, _pool = receiver
        original = json.dumps({"action": "opened", "a": 1}).encode()
        tampered = json.dumps({"action": "opened", "a": 2}).encode()
        response = client.post(
            URL,
            content=tampered,
            headers={
                "x-hub-signature-256": _sign(SECRET, original),
                "x-github-event": "pull_request",
                "x-github-delivery": "d",
                "content-type": "application/json",
            },
        )
        assert response.status_code == 401
        assert spy.calls == []

    def test_a_non_ascii_signature_header_is_401_not_500(self, receiver: Any) -> None:
        """The header is attacker-controlled; a str compare raises TypeError on it.

        ``hmac.compare_digest`` refuses non-ASCII str operands, so comparing the
        raw header as text turned one stray byte into a 500 — an unauthenticated
        caller's crash oracle — instead of the 401 it earns.
        """
        client, spy, _pool = receiver
        body = json.dumps({"action": "opened"}).encode()
        response = client.post(
            URL,
            content=body,
            headers={
                # Sent as raw bytes: httpx will not encode a non-ASCII str, but
                # GitHub's transport is bytes and Starlette hands the route a
                # latin-1 decode of whatever arrived.
                "x-hub-signature-256": "sha256=café".encode("latin-1") + b"0" * 60,
                "x-github-event": "pull_request",
                "x-github-delivery": "d",
                "content-type": "application/json",
            },
        )
        assert response.status_code == 401
        assert spy.calls == []

    def test_unset_secret_is_503_not_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "pr_party_webhook_secret", "", raising=False)
        spy = _Spy()
        monkeypatch.setattr("ontokit.api.routes.pr_party_webhooks.handle_webhook_event", spy)

        async def _override_get_db() -> Any:
            yield object()

        app.dependency_overrides[get_db] = _override_get_db
        try:
            client = TestClient(app, raise_server_exceptions=False)
            response = _post(client)
            assert response.status_code == 503
            assert spy.calls == []
        finally:
            app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Delivery-id dedupe (KTD14)
# ---------------------------------------------------------------------------


class TestDeliveryDedupe:
    def test_redelivered_id_is_not_processed_twice(self, receiver: Any) -> None:
        client, spy, _pool = receiver
        first = _post(client, delivery="dup-1")
        second = _post(client, delivery="dup-1")

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["status"] == "duplicate"
        assert len(spy.calls) == 1

    def test_distinct_delivery_ids_both_process(self, receiver: Any) -> None:
        client, spy, _pool = receiver
        _post(client, delivery="a")
        _post(client, delivery="b")
        assert len(spy.calls) == 2

    def test_dedupe_key_is_namespaced(self, receiver: Any) -> None:
        client, _spy, pool = receiver
        _post(client, delivery="abc")
        assert list(pool.keys) == ["pr_party:delivery:abc"]

    def test_redis_outage_processes_anyway(self, receiver: Any, monkeypatch: Any) -> None:
        """The upsert is idempotent; dropping a delivery is the worse failure."""
        client, spy, _pool = receiver
        broken = _FakePool(fail=True)

        async def _pool() -> Any:
            return broken

        monkeypatch.setattr("ontokit.api.routes.pr_party_webhooks.get_arq_pool", _pool)

        response = _post(client, delivery="x")
        assert response.status_code == 200
        assert len(spy.calls) == 1

    def test_missing_delivery_header_still_processes(self, receiver: Any) -> None:
        client, spy, _pool = receiver
        response = _post(client, delivery=None)
        assert response.status_code == 200
        assert len(spy.calls) == 1

    def test_a_failed_handler_releases_the_claim_for_the_redelivery(
        self, receiver: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The claim is taken before processing, so a crash must give it back.

        GitHub's redelivery is the only further copy of that fact; a claim left
        behind by a failed pass makes the retry look like a duplicate and the
        event is lost for the 24h TTL.
        """
        client, _spy, pool = receiver
        calls: list[str] = []

        async def _explode(_db: Any, event: str, _payload: dict[str, Any], **_kw: Any) -> Any:
            calls.append(event)
            raise RuntimeError("intake blew up")

        monkeypatch.setattr("ontokit.api.routes.pr_party_webhooks.handle_webhook_event", _explode)
        first = _post(client, delivery="boom-1")

        assert first.status_code == 500
        assert calls == ["pull_request"]
        assert f"{DELIVERY_KEY_PREFIX}boom-1" not in pool.keys

        # The redelivery is new work, not a duplicate.
        good = _Spy()
        monkeypatch.setattr("ontokit.api.routes.pr_party_webhooks.handle_webhook_event", good)
        second = _post(client, delivery="boom-1")

        assert second.status_code == 200
        assert second.json() == {"status": "processed"}
        assert len(good.calls) == 1

    def test_a_release_failure_does_not_mask_the_original_error(
        self, receiver: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Best-effort release: the 24h TTL is the backstop, not a second crash."""
        client, _spy, pool = receiver

        async def _explode(*_args: Any, **_kw: Any) -> Any:
            raise RuntimeError("intake blew up")

        async def _refuse(_key: str) -> int:
            raise RuntimeError("redis went away mid-request")

        monkeypatch.setattr("ontokit.api.routes.pr_party_webhooks.handle_webhook_event", _explode)
        monkeypatch.setattr(pool, "delete", _refuse)
        response = _post(client, delivery="boom-2")

        # The handler's failure surfaces; the release attempt is swallowed.
        assert response.status_code == 500
        assert f"{DELIVERY_KEY_PREFIX}boom-2" in pool.keys


# ---------------------------------------------------------------------------
# Payload handling
# ---------------------------------------------------------------------------


class TestPayload:
    def test_unparseable_body_is_400_after_a_valid_signature(self, receiver: Any) -> None:
        client, spy, _pool = receiver
        body = b"not json at all"
        response = client.post(
            URL,
            content=body,
            headers={
                "x-hub-signature-256": _sign(SECRET, body),
                "x-github-event": "pull_request",
                "x-github-delivery": "d",
            },
        )
        assert response.status_code == 400
        assert spy.calls == []

    def test_non_object_body_is_400(self, receiver: Any) -> None:
        client, spy, _pool = receiver
        body = b"[1, 2, 3]"
        response = client.post(
            URL,
            content=body,
            headers={
                "x-hub-signature-256": _sign(SECRET, body),
                "x-github-event": "pull_request",
                "x-github-delivery": "d",
            },
        )
        assert response.status_code == 400
        assert spy.calls == []

    def test_ping_is_acknowledged_without_dispatch(self, receiver: Any) -> None:
        """GitHub pings the hook the moment it is created; that is not an event."""
        client, spy, _pool = receiver
        response = _post(client, {"zen": "Non-blocking is better."}, event="ping")
        assert response.status_code == 200
        assert response.json()["status"] == "pong"
        assert spy.calls == []

    def test_missing_event_header_is_ignored(self, receiver: Any) -> None:
        client, spy, _pool = receiver
        body = json.dumps({"action": "opened"}).encode()
        response = client.post(
            URL,
            content=body,
            headers={
                "x-hub-signature-256": _sign(SECRET, body),
                "x-github-delivery": "d",
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == "ignored"
        assert spy.calls == []

    def test_dispatch_result_is_echoed(self, receiver: Any, monkeypatch: Any) -> None:
        client, _spy, _pool = receiver
        spy = _Spy({"status": "deferred", "handled": 0})
        monkeypatch.setattr("ontokit.api.routes.pr_party_webhooks.handle_webhook_event", spy)
        response = _post(client, event="issue_comment")
        assert response.json()["status"] == "deferred"


# ---------------------------------------------------------------------------
# Mounting (KTD19)
# ---------------------------------------------------------------------------


class TestMounting:
    @staticmethod
    def _probe(auth_mode: str) -> tuple[bool, int]:
        target = APIRouter()
        mounted = include_pr_party_routes(target, auth_mode=auth_mode)
        probe_app = FastAPI()
        probe_app.include_router(target, prefix="/api/v1")
        app.dependency_overrides.clear()
        client = TestClient(probe_app, raise_server_exceptions=False)
        return mounted, client.post(URL, content=b"{}").status_code

    def test_webhooks_mount_with_the_gated_pr_party_router(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "pr_party_webhook_secret", "", raising=False)
        mounted, status_code = self._probe("required")

        assert mounted is True
        # Present, and refusing on its own terms (unconfigured) rather than 404.
        assert status_code == 503

    def test_auth_disabled_takes_the_receiver_down_too(self) -> None:
        """KTD19: with PR Party unmounted there is no queue to deliver into."""
        mounted, status_code = self._probe("disabled")

        assert mounted is False
        assert status_code == 404

    def test_receiver_requires_no_session(self, receiver: Any) -> None:
        """GitHub holds no Zitadel session; the HMAC is the whole credential.

        The fixture installs no ``get_current_user`` override, so a 200 here
        proves the route carries no auth dependency.
        """
        client, spy, _pool = receiver
        assert _post(client).status_code == 200
        assert len(spy.calls) == 1
