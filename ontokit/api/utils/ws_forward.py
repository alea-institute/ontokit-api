"""Project-scoped notification forwarding and long-lived socket authorization."""

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

from arq.connections import ArqRedis
from fastapi import HTTPException, WebSocket, WebSocketDisconnect

from ontokit.api.utils import ws_auth
from ontokit.api.utils.redis import get_arq_pool
from ontokit.core.auth import ANONYMOUS_USER
from ontokit.core.database import async_session_maker
from ontokit.services.project_service import ProjectService

logger = logging.getLogger(__name__)
REAUTHORIZE_INTERVAL = 60.0
CLEANUP_TIMEOUT = 5.0


def project_reauthorizer(websocket: WebSocket, project_id: UUID) -> Callable[[], Awaitable[None]]:
    """Recheck the original identity without accepting the socket a second time.

    ``authenticate_ws`` returns a bool, not a user or expiry. Reuse its token
    verification/user assembly and open a fresh session for each access check.
    Optional anonymous connections remain anonymous; an authenticated connection
    whose token expires must close rather than silently downgrade its identity.
    """
    # Preserve the identity actually admitted at the handshake. In optional
    # mode a token can expire immediately after acceptance; that must close an
    # authenticated connection, not downgrade it to anonymous.
    token = websocket.state.auth_token
    anonymous = websocket.state.auth_user.is_anonymous

    async def reauthorize() -> None:
        user = ANONYMOUS_USER
        if not anonymous:
            if not token:
                raise HTTPException(status_code=401, detail="Authentication required")
            try:
                user = await ws_auth._build_user_from_token(token)
            except HTTPException as exc:
                if exc.status_code == 401:
                    raise HTTPException(status_code=401, detail="Invalid or expired token") from exc
                else:
                    raise RuntimeError("WebSocket token verification unavailable") from exc
        async with async_session_maker() as db:
            await ProjectService(db).get(project_id, user)

    return reauthorize


async def forward_project_events(
    websocket: WebSocket,
    channel: str,
    project_id: UUID,
    reauthorize: Callable[[], Awaitable[None]],
    *,
    reauthorize_interval: float = REAUTHORIZE_INTERVAL,
    pool_factory: Callable[[], Awaitable[ArqRedis]] = get_arq_pool,
) -> None:
    """Forward Redis notifications until disconnect, revocation, or server error.

    Authorization has its own task so quiet channels and a stalled Redis read
    cannot postpone the 60-second check. Check again before sending a frame when
    the deadline has elapsed, including after a slow Redis read.
    """
    pubsub = None
    tasks: list[asyncio.Task[None]] = []
    project_id_str = str(project_id)
    loop = asyncio.get_running_loop()
    next_check = loop.time()
    auth_lock = asyncio.Lock()
    auth_failure: Exception | None = None

    async def check_access() -> None:
        nonlocal next_check, auth_failure
        async with auth_lock:
            if auth_failure is not None:
                raise auth_failure
            if loop.time() >= next_check:
                next_check = loop.time() + reauthorize_interval
                try:
                    await reauthorize()
                except Exception as exc:
                    auth_failure = exc
                    raise

    async def watch_access() -> None:
        while True:
            await asyncio.sleep(max(0, next_check - loop.time()))
            await check_access()

    try:
        await check_access()

        async def forward() -> None:
            nonlocal pubsub
            pool = await pool_factory()
            pubsub = pool.pubsub()
            await pubsub.subscribe(channel)
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
                await check_access()
                if message and message["type"] == "message":
                    try:
                        data = json.loads(message["data"])
                    except json.JSONDecodeError:
                        continue
                    if isinstance(data, dict) and data.get("project_id") == project_id_str:
                        await websocket.send_json(data)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(websocket.receive_text(), timeout=0.1)

        tasks = [asyncio.create_task(forward()), asyncio.create_task(watch_access())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except WebSocketDisconnect:
        pass
    except HTTPException as exc:
        if exc.status_code in (401, 403, 404):
            code, reason = {
                401: (4001, "Invalid or expired token"),
                403: (4003, "Access denied"),
                404: (4004, "Project not found"),
            }[exc.status_code]
            with contextlib.suppress(Exception):
                await websocket.close(code=code, reason=reason)
        else:
            logger.exception("WebSocket authorization error for project %s", project_id_str)
            with contextlib.suppress(Exception):
                await websocket.close(code=1011, reason="Internal server error")
    except Exception:
        logger.exception("WebSocket forwarding error for project %s", project_id_str)
        with contextlib.suppress(Exception):
            await websocket.close(code=1011, reason="Internal server error")
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if pubsub is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(pubsub.unsubscribe(channel), timeout=CLEANUP_TIMEOUT)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    pubsub.aclose(),  # type: ignore[no-untyped-call]
                    timeout=CLEANUP_TIMEOUT,
                )
