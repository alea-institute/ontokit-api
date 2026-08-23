"""Route tests for privacy-safe per-call LLM audit history."""

import base64
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from ontokit.core.auth import ANONYMOUS_USER

PROJECT_ID = UUID("11111111-1111-1111-1111-111111111111")
URL = f"/api/v1/projects/{PROJECT_ID}/llm/audit"


def _encode_cursor_payload(payload: dict[str, object]) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode())
    return encoded.rstrip(b"=").decode()


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


@pytest.mark.asyncio
async def test_audit_history_rejects_disabled_auth_identity_before_database_access() -> None:
    from ontokit.api.routes.llm import get_llm_audit_history

    session = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await get_llm_audit_history(
            PROJECT_ID,
            session,
            ANONYMOUS_USER,
            cursor=None,
            limit=50,
        )

    assert exc_info.value.status_code == 403
    session.execute.assert_not_awaited()


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


def test_audit_history_cursor_fetches_the_next_page(
    authed_client: tuple[TestClient, AsyncMock],
) -> None:
    client, session = authed_client
    now = datetime(2026, 8, 22, tzinfo=UTC)
    first_page_rows = [_audit_row(now - timedelta(minutes=offset)) for offset in range(3)]
    second_page_row = _audit_row(now - timedelta(minutes=3))
    session.execute = AsyncMock(
        side_effect=[
            _scalar_rows(first_page_rows),
            _scalar_rows([second_page_row]),
        ]
    )

    with patch(
        "ontokit.api.routes.llm._require_owner_or_admin",
        new=AsyncMock(return_value="owner"),
    ):
        first = client.get(URL, params={"limit": 2})
        second = client.get(
            URL,
            params={"limit": 2, "cursor": first.json()["next_cursor"]},
        )

    assert second.status_code == 200
    assert [entry["id"] for entry in second.json()["entries"]] == [str(second_page_row.id)]
    assert second.json()["next_cursor"] is None
    second_query = session.execute.await_args_list[1].args[0]
    assert "(llm_audit_logs.created_at, llm_audit_logs.id) <" in str(second_query)


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


@pytest.mark.parametrize("field", ["project_id", "created_at", "id"])
@pytest.mark.parametrize("invalid_value", [None, {}, [], True, 42])
def test_audit_cursor_rejects_non_string_fields_with_typed_422(
    field: str,
    invalid_value: object,
) -> None:
    from ontokit.api.routes.llm import _decode_audit_cursor

    payload: dict[str, object] = {
        "v": 1,
        "project_id": str(PROJECT_ID),
        "created_at": "2026-08-22T12:00:00+00:00",
        "id": str(uuid4()),
    }
    payload[field] = invalid_value

    with pytest.raises(HTTPException) as exc_info:
        _decode_audit_cursor(_encode_cursor_payload(payload), PROJECT_ID)

    assert exc_info.value.status_code == 422


@pytest.mark.parametrize("invalid_version", [None, {}, [], True, 1.0, "1", 0, 2])
def test_audit_cursor_rejects_invalid_version_with_typed_422(
    invalid_version: object,
) -> None:
    from ontokit.api.routes.llm import _decode_audit_cursor

    payload: dict[str, object] = {
        "v": invalid_version,
        "project_id": str(PROJECT_ID),
        "created_at": "2026-08-22T12:00:00+00:00",
        "id": str(uuid4()),
    }

    with pytest.raises(HTTPException) as exc_info:
        _decode_audit_cursor(_encode_cursor_payload(payload), PROJECT_ID)

    assert exc_info.value.status_code == 422


def test_audit_cursor_cannot_replay_across_projects() -> None:
    from ontokit.api.routes.llm import _decode_audit_cursor, _encode_audit_cursor

    cursor = _encode_audit_cursor(_audit_row(datetime(2026, 8, 22, tzinfo=UTC)))
    try:
        _decode_audit_cursor(cursor, uuid4())
    except HTTPException as exc:
        assert exc.status_code == 422
    else:
        raise AssertionError("cross-project audit cursor unexpectedly accepted")
