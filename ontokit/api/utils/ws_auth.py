"""Shared WebSocket authentication and project access helper."""

import base64
import binascii
import logging
import re
from uuid import UUID

from fastapi import HTTPException, WebSocket

from ontokit.core.auth import (
    ANONYMOUS_USER,
    CurrentUser,
    fetch_userinfo,
    validate_token,
)
from ontokit.core.config import settings
from ontokit.core.database import async_session_maker
from ontokit.services.project_service import ProjectService

logger = logging.getLogger(__name__)


BEARER_SUBPROTOCOL = "ontokit.bearer.v1"
TOKEN_SUBPROTOCOL_PREFIX = "ontokit.token."


def _subprotocol_token(websocket: WebSocket) -> str | None:
    """Decode the single credential offer, never selecting it as a protocol."""
    protocols = websocket.scope.get("subprotocols", [])
    if BEARER_SUBPROTOCOL not in protocols:
        return None
    offers = [p for p in protocols if p.startswith(TOKEN_SUBPROTOCOL_PREFIX)]
    if len(offers) != 1:
        return None
    encoded = offers[0][len(TOKEN_SUBPROTOCOL_PREFIX) :]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", encoded):
        return None
    try:
        return (
            base64.b64decode(
                encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
            ).decode("utf-8")
            or None
        )
    except (binascii.Error, UnicodeDecodeError):
        return None


async def _build_user_from_token(token: str) -> CurrentUser:
    """Validate a JWT and assemble the ``CurrentUser``.

    Mirrors the user-assembly half of ``core.auth.get_current_user`` (userinfo
    backfill for missing name/email) so WebSocket callers get the same identity
    an HTTP request with the same token would.
    """
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
    return CurrentUser(
        id=payload.sub, email=email, name=name, username=username, roles=payload.roles
    )


async def authenticate_ws(
    websocket: WebSocket,
    project_id: UUID,
    token: str | None,
) -> bool:
    """Authenticate a WebSocket connection and verify project access.

    Accepts a base64url credential offered with ``ontokit.bearer.v1`` in the
    handshake subprotocols, preferring it to the legacy query ``token``. Only
    the bearer version protocol is echoed back to the client.

    Resolves the caller identity **honoring ``settings.auth_mode``**, then checks
    project access via ``ProjectService.get``. Returns ``True`` if the caller
    should proceed, or ``False`` after closing the WebSocket with an appropriate
    code when auth or access fails.

    ``auth_mode`` parity with the HTTP dependencies in ``ontokit.core.auth``
    (``get_current_user`` / ``get_current_user_optional``) — without this a
    ``disabled`` or ``optional`` deployment would admit anonymous callers on its
    HTTP API but still reject them at the WebSocket handshake:

    * ``disabled`` — everyone is the shared :data:`ANONYMOUS_USER`; **no token
      required** (matches ``get_current_user``'s disabled branch).
    * ``optional`` — an absent *or* invalid token downgrades to
      :data:`ANONYMOUS_USER` (matches ``get_current_user_optional``); the
      project-access check below still gates private projects.
    * ``required`` — a valid token is mandatory (original behavior).

    HTTP 401/403/404 from the auth/service layer are translated to WebSocket
    close codes:

    * **4001** – missing or invalid token (``required`` mode only)
    * **4003** – authenticated (or anonymous) but access denied
    * **4004** – project not found

    Unexpected server errors are closed with **1011** (internal error) and logged.

    The WebSocket is accepted before any error close so that the client receives
    a proper close frame rather than a raw HTTP 403.
    """
    header_token = _subprotocol_token(websocket)
    if header_token:
        token = header_token
        await websocket.accept(subprotocol=BEARER_SUBPROTOCOL)
    else:
        # Retain query-token compatibility for one deployment window.
        await websocket.accept()

    # --- Resolve caller identity per auth_mode (parity with core.auth) ---
    user: CurrentUser
    if settings.auth_mode == "disabled":
        # Auth fully disabled — the shared anonymous identity, no token needed.
        user = ANONYMOUS_USER
    elif settings.auth_mode == "optional":
        # Mirror get_current_user_optional: absent/invalid token → anonymous.
        # The project-access check still enforces private-project boundaries.
        if not token:
            user = ANONYMOUS_USER
        else:
            try:
                user = await _build_user_from_token(token)
            except HTTPException:
                user = ANONYMOUS_USER
            except Exception:
                logger.exception("Unexpected error during WebSocket token validation")
                await websocket.close(code=1011, reason="Internal server error")
                return False
    else:
        # "required" mode: a valid token is mandatory.
        if not token:
            await websocket.close(code=4001, reason="Authentication required")
            return False
        try:
            user = await _build_user_from_token(token)
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

    # The forwarder can re-check identity/access and validate the original token
    # for expiry (CurrentUser does not expose the JWT expiry claim).
    websocket.state.auth_user = user
    websocket.state.auth_token = token
    return True
