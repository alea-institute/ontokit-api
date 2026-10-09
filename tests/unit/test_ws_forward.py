"""Regression tests for long-lived notification socket access and cleanup."""

import asyncio
import base64
import json
import logging
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException, WebSocket, WebSocketDisconnect

from ontokit.api.utils.ws_forward import forward_project_events, project_reauthorizer
from ontokit.core.auth import ANONYMOUS_USER, CurrentUser

PROJECT_ID = UUID("12345678-1234-5678-1234-567812345678")


@pytest.fixture
def socket() -> AsyncMock:
    ws = AsyncMock(spec=WebSocket)
    ws.scope = {"subprotocols": []}
    ws.state = Mock(auth_user=CurrentUser(id="member"), auth_token="original-test-token")

    async def wait_for_client():
        await asyncio.Event().wait()

    ws.receive_text.side_effect = wait_for_client
    return ws


@pytest.fixture
def pool() -> AsyncMock:
    redis = AsyncMock()
    redis.pubsub = Mock(return_value=AsyncMock())
    redis.pubsub().get_message.return_value = None
    return redis


@pytest.mark.parametrize("status,code", [(401, 4001), (403, 4003), (404, 4004)])
async def test_periodic_reauthorization_closes_quiet_socket(socket, pool, status, code):
    check = AsyncMock(side_effect=[None, HTTPException(status_code=status)])
    await forward_project_events(
        socket,
        "updates",
        PROJECT_ID,
        check,
        reauthorize_interval=0.01,
        pool_factory=AsyncMock(return_value=pool),
    )
    assert check.await_count == 2
    assert socket.close.await_args.kwargs["code"] == code
    socket.send_json.assert_not_awaited()
    pool.pubsub().unsubscribe.assert_awaited_once_with("updates")
    pool.pubsub().aclose.assert_awaited_once()


async def test_reauthorization_runs_during_stalled_redis_read(socket, pool):
    read_started = asyncio.Event()
    read_cancelled = asyncio.Event()

    async def stalled_read(**_kwargs):
        read_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            read_cancelled.set()

    pool.pubsub().get_message.side_effect = stalled_read
    check = AsyncMock(side_effect=[None, HTTPException(status_code=403)])
    await asyncio.wait_for(
        forward_project_events(
            socket,
            "updates",
            PROJECT_ID,
            check,
            reauthorize_interval=0.01,
            pool_factory=AsyncMock(return_value=pool),
        ),
        timeout=1,
    )
    assert read_started.is_set()
    assert read_cancelled.is_set()
    assert socket.close.await_args.kwargs["code"] == 4003


async def test_filtering_and_disconnect_cleanup(socket, pool):
    match = {"project_id": str(PROJECT_ID), "type": "done"}
    pool.pubsub().get_message.side_effect = [
        {"type": "message", "data": json.dumps({"project_id": "other"})},
        {"type": "message", "data": "invalid{"},
        {"type": "message", "data": json.dumps(match)},
    ]
    socket.receive_text.side_effect = [TimeoutError, WebSocketDisconnect()]
    await forward_project_events(
        socket, "updates", PROJECT_ID, AsyncMock(), pool_factory=AsyncMock(return_value=pool)
    )
    socket.send_json.assert_awaited_once_with(match)
    socket.close.assert_not_awaited()
    pool.pubsub().aclose.assert_awaited_once()


@pytest.mark.parametrize("failure_at", ["pool", "subscribe", "read", "reauthorize"])
async def test_unexpected_error_closes_and_logs_traceback(socket, pool, caplog, failure_at):
    failure = RuntimeError("unexpected failure")
    factory = AsyncMock(return_value=pool)
    check = AsyncMock()
    if failure_at == "pool":
        factory.side_effect = failure
    elif failure_at == "subscribe":
        pool.pubsub().subscribe.side_effect = failure
    elif failure_at == "read":
        pool.pubsub().get_message.side_effect = failure
    else:
        check.side_effect = failure
    with caplog.at_level(logging.ERROR):
        await forward_project_events(socket, "updates", PROJECT_ID, check, pool_factory=factory)
    socket.close.assert_awaited_once_with(code=1011, reason="Internal server error")
    assert any(record.exc_info is not None for record in caplog.records)
    if failure_at in ("subscribe", "read"):
        pool.pubsub().aclose.assert_awaited_once()


async def test_cancellation_cleans_subscription(socket, pool):
    subscribed = asyncio.Event()
    pool.pubsub().subscribe.side_effect = lambda _channel: subscribed.set()
    task = asyncio.create_task(
        forward_project_events(
            socket, "updates", PROJECT_ID, AsyncMock(), pool_factory=AsyncMock(return_value=pool)
        )
    )
    await asyncio.wait_for(subscribed.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    pool.pubsub().unsubscribe.assert_awaited_once()
    pool.pubsub().aclose.assert_awaited_once()


@pytest.mark.parametrize("auth_mode", ["required", "optional"])
@pytest.mark.parametrize("transport", ["query", "subprotocol"])
async def test_original_token_expiry_is_revalidated(socket, auth_mode, transport):
    token = "original-test-token"
    if transport == "subprotocol":
        encoded = base64.urlsafe_b64encode(token.encode()).decode().rstrip("=")
        socket.scope["subprotocols"] = ["ontokit.bearer.v1", "ontokit.token." + encoded]
    user = CurrentUser(id="member")
    with (
        patch("ontokit.api.utils.ws_forward.ws_auth.settings") as settings,
        patch(
            "ontokit.api.utils.ws_forward.ws_auth._build_user_from_token",
            AsyncMock(side_effect=[user, HTTPException(status_code=401)]),
        ) as build_user,
        patch("ontokit.api.utils.ws_forward.async_session_maker") as sessions,
        patch("ontokit.api.utils.ws_forward.ProjectService") as service,
    ):
        settings.auth_mode = auth_mode
        service.return_value.get = AsyncMock()
        check = project_reauthorizer(socket, PROJECT_ID)
        await check()
        with pytest.raises(HTTPException) as error:
            await check()
        assert error.value.status_code == 401
        assert build_user.await_count == 2
        build_user.assert_awaited_with(token)
        service.return_value.get.assert_awaited_once_with(PROJECT_ID, user)
        assert sessions.call_count == 1


async def test_access_rechecked_with_fresh_session(socket):
    user = CurrentUser(id="removed-member")
    with (
        patch("ontokit.api.utils.ws_forward.ws_auth.settings") as settings,
        patch(
            "ontokit.api.utils.ws_forward.ws_auth._build_user_from_token",
            AsyncMock(return_value=user),
        ),
        patch("ontokit.api.utils.ws_forward.async_session_maker") as sessions,
        patch("ontokit.api.utils.ws_forward.ProjectService") as service,
    ):
        settings.auth_mode = "required"
        service.return_value.get = AsyncMock(side_effect=[None, HTTPException(status_code=403)])
        check = project_reauthorizer(socket, PROJECT_ID)
        await check()
        with pytest.raises(HTTPException) as error:
            await check()
        assert error.value.status_code == 403
        assert sessions.call_count == 2
        service.return_value.get.assert_awaited_with(PROJECT_ID, user)


async def test_optional_authenticated_handshake_cannot_downgrade_on_first_check(socket):
    socket.state = Mock(auth_user=CurrentUser(id="member"), auth_token="expired")
    with (
        patch("ontokit.api.utils.ws_forward.ws_auth.settings") as settings,
        patch(
            "ontokit.api.utils.ws_forward.ws_auth._build_user_from_token",
            AsyncMock(side_effect=HTTPException(status_code=401)),
        ),
        patch("ontokit.api.utils.ws_forward.async_session_maker") as sessions,
        patch("ontokit.api.utils.ws_forward.ProjectService") as service,
    ):
        settings.auth_mode = "optional"
        service.return_value.get = AsyncMock()
        check = project_reauthorizer(socket, PROJECT_ID)
        with pytest.raises(HTTPException) as error:
            await check()
        assert error.value.status_code == 401
        sessions.assert_not_called()


@pytest.mark.parametrize(
    "auth_mode,token", [("disabled", "ignored"), ("optional", None), ("optional", "invalid")]
)
async def test_anonymous_auth_modes_preserved(socket, auth_mode, token):
    socket.state = Mock(auth_user=ANONYMOUS_USER, auth_token=token)
    with (
        patch("ontokit.api.utils.ws_forward.ws_auth.settings") as settings,
        patch(
            "ontokit.api.utils.ws_forward.ws_auth._build_user_from_token",
            AsyncMock(side_effect=HTTPException(status_code=401)),
        ) as build_user,
        patch("ontokit.api.utils.ws_forward.async_session_maker"),
        patch("ontokit.api.utils.ws_forward.ProjectService") as service,
    ):
        settings.auth_mode = auth_mode
        service.return_value.get = AsyncMock()
        check = project_reauthorizer(socket, PROJECT_ID)
        await check()
        await check()
        service.return_value.get.assert_awaited_with(PROJECT_ID, ANONYMOUS_USER)
        build_user.assert_not_awaited()
