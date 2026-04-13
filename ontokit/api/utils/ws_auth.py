"""Shared WebSocket authentication and project access helper."""

import logging
from uuid import UUID

from fastapi import HTTPException, WebSocket

from ontokit.core.auth import CurrentUser, fetch_userinfo, validate_token
from ontokit.core.database import async_session_maker
from ontokit.services.project_service import ProjectService

logger = logging.getLogger(__name__)


async def authenticate_ws(
    websocket: WebSocket,
    project_id: UUID,
    token: str | None,
) -> bool:
    """Authenticate a WebSocket connection and verify project access.

    Validates the JWT token, builds a ``CurrentUser``, and checks project
    access via ``ProjectService.get``.  Returns ``True`` if the caller
    should proceed (auth + access succeeded).  Returns ``False`` after
    closing the WebSocket with an appropriate code when auth or access
    fails.

    HTTP 401/403/404 from the auth/service layer are translated to
    WebSocket close codes:

    * **4001** – missing or invalid token
    * **4003** – authenticated but access denied
    * **4004** – project not found

    Unexpected server errors are closed with **1011** (internal error)
    and logged.
    """
    # --- Token required ---
    if not token:
        await websocket.close(code=4001, reason="Authentication required")
        return False

    # --- Validate JWT ---
    try:
        payload = await validate_token(token)
        name = payload.name
        email = payload.email
        username = payload.preferred_username
        if not name or not email:
            userinfo = await fetch_userinfo(token)
            if userinfo:
                name = name or userinfo.get("name") or userinfo.get("preferred_username")
                email = email or userinfo.get("email")
                username = username or userinfo.get("preferred_username")
        user = CurrentUser(
            id=payload.sub, email=email, name=name, username=username, roles=payload.roles
        )
    except HTTPException:
        await websocket.close(code=4001, reason="Invalid or expired token")
        return False
    except Exception:
        logger.exception("Unexpected error during WebSocket token validation")
        await websocket.close(code=1011, reason="Internal server error")
        return False

    # --- Verify project access ---
    try:
        async with async_session_maker() as db:
            svc = ProjectService(db)
            await svc.get(project_id, user)
    except HTTPException as exc:
        if exc.status_code == 404:
            await websocket.close(code=4004, reason="Project not found")
        else:
            await websocket.close(code=4003, reason="Access denied")
        return False
    except Exception:
        logger.exception("Unexpected error during WebSocket project access check")
        await websocket.close(code=1011, reason="Internal server error")
        return False

    return True
