"""Route tests for privacy-safe per-call LLM audit history."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient

PROJECT_ID = UUID("11111111-1111-1111-1111-111111111111")
URL = f"/api/v1/projects/{PROJECT_ID}/llm/audit"


def _audit_row(created_at: datetime) -> MagicMock:
    return MagicMock(
        id=uuid4(),
        project_id=PROJECT_ID,
        user_id="user-1",
        model="claude-sonnet-4-6",
        provider="anthropic",
        endpoint="llm/generate-suggestions",
        input_tokens=120,
        output_tokens=30,
        cost_estimate_usd=0.0042,
        is_byo_key=False,
        created_at=created_at,
    )


def _scalar_rows(rows: list[MagicMock]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    return result


def test_audit_history_requires_authentication(client: TestClient) -> None:
    response = client.get(URL)
    assert response.status_code in (401, 403)


def test_owner_gets_metadata_only_keyset_page(
    authed_client: tuple[TestClient, AsyncMock],
) -> None:
    client, session = authed_client
    now = datetime(2026, 8, 22, tzinfo=UTC)
    rows = [_audit_row(now - timedelta(minutes=offset)) for offset in range(3)]
    session.execute = AsyncMock(return_value=_scalar_rows(rows))

    with patch(
        "ontokit.api.routes.llm._require_owner_or_admin",
        new=AsyncMock(return_value="owner"),
    ):
        response = client.get(URL, params={"limit": 2})

    assert response.status_code == 200
    body = response.json()
    assert len(body["entries"]) == 2
    assert body["entries"][0]["id"] == str(rows[0].id)
    assert body["next_cursor"]
    serialized = response.text.lower()
    assert "prompt" not in serialized
    assert "response_content" not in serialized
    assert "api_key" not in serialized


def test_invalid_audit_cursor_is_typed_422(
    authed_client: tuple[TestClient, AsyncMock],
) -> None:
    client, session = authed_client
    with patch(
        "ontokit.api.routes.llm._require_owner_or_admin",
        new=AsyncMock(return_value="owner"),
    ):
        response = client.get(URL, params={"cursor": "not-a-valid-cursor"})

    assert response.status_code == 422
    session.execute.assert_not_awaited()


def test_audit_cursor_cannot_replay_across_projects() -> None:
    from ontokit.api.routes.llm import _decode_audit_cursor, _encode_audit_cursor

    cursor = _encode_audit_cursor(_audit_row(datetime(2026, 8, 22, tzinfo=UTC)))
    try:
        _decode_audit_cursor(cursor, uuid4())
    except HTTPException as exc:
        assert exc.status_code == 422
    else:
        raise AssertionError("cross-project audit cursor unexpectedly accepted")
