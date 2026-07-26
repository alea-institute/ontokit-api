"""PR Party org webhook receiver (KTD14).

``POST /api/v1/pr-party/webhooks/github`` — the one PR Party route with no
authentication dependency, because the caller is GitHub and the HMAC over the
raw request body *is* the credential. That inverts the usual ordering: nothing
about the payload is trusted, or even parsed, until the signature verifies.

Three deliberate choices:

- **No secret configured means 503, not "open".** An empty
  ``PR_PARTY_WEBHOOK_SECRET`` is an unconfigured receiver, and the only honest
  answer to a delivery it cannot authenticate is that the service is not
  available. Accepting unsigned deliveries "until the secret is set" would make
  the endpoint an unauthenticated write into the review queue.
- **Bad or missing signature is 401 before parsing.** The existing per-project
  receiver in ``pull_requests.py`` answers 403; 401 is the more accurate code
  for "you presented no valid credential", and this route has no notion of a
  principal who could be forbidden. That module's ``get_webhook_secret`` helper
  is deliberately not reused — it derives a callback path that does not match
  its own route, and it is scoped to a project this receiver does not have.
- **Delivery-id dedupe is best-effort.** ``X-GitHub-Delivery`` is claimed with a
  Redis ``SETNX`` under a 24h TTL; a repeat is a 200 no-op. If Redis is down the
  event is processed anyway and a warning is logged: the upsert path is
  idempotent by construction (KTD14), so processing twice is harmless, whereas
  dropping a delivery loses a fact GitHub will not resend.

This receiver mounts inside ``include_pr_party_routes`` — under
``AUTH_MODE=disabled`` PR Party is not mounted at all (KTD19), and the webhook
goes down with it rather than becoming the one live surface of an otherwise
absent feature.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.api.utils.redis import get_arq_pool
from ontokit.core.config import settings
from ontokit.core.database import get_db
from ontokit.services.pr_party_intake import (
    DELIVERY_DEDUPE_TTL_SECONDS,
    DELIVERY_KEY_PREFIX,
    handle_webhook_event,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _signature_matches(secret: str, body: bytes, provided: str) -> bool:
    """Constant-time compare against ``sha256=<hexdigest>`` over the raw bytes."""
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided, expected)


async def _claim_delivery(delivery_id: str) -> bool:
    """Claim a delivery id. ``True`` means "first time — process it".

    Redis being unavailable returns ``True``: see the module docstring on why
    double-processing is the safe failure here.
    """
    if not delivery_id:
        return True
    try:
        pool = await get_arq_pool()
        claimed = await pool.set(
            f"{DELIVERY_KEY_PREFIX}{delivery_id}",
            "1",
            ex=DELIVERY_DEDUPE_TTL_SECONDS,
            nx=True,
        )
    except Exception as exc:  # noqa: BLE001 — dedupe is an optimization, not a gate
        logger.warning(
            "PR Party webhook dedupe unavailable for delivery %s (processing anyway): %s",
            delivery_id,
            exc,
        )
        return True
    return bool(claimed)


@router.post("/webhooks/github")
async def receive_github_webhook(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_hub_signature_256: Annotated[str | None, Header()] = None,
    x_github_event: Annotated[str | None, Header()] = None,
    x_github_delivery: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Accept one org-level GitHub delivery and route it onto the intake path."""
    secret = settings.pr_party_webhook_secret
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The PR Party webhook receiver is not configured.",
        )

    body = await request.body()
    if not x_hub_signature_256 or not _signature_matches(secret, body, x_hub_signature_256):
        # No payload detail in the message: an unauthenticated caller learns
        # nothing about what a valid delivery would look like.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature.",
        )

    event = (x_github_event or "").strip()
    if not event:
        return {"status": "ignored", "reason": "no event header"}
    if event == "ping":
        # GitHub pings the moment a hook is created; acknowledging it is what
        # turns the hook green in the org settings UI.
        return {"status": "pong"}

    if not await _claim_delivery((x_github_delivery or "").strip()):
        logger.info("PR Party webhook: duplicate delivery %s ignored", x_github_delivery)
        return {"status": "duplicate", "event": event}

    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook body is not valid JSON.",
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook body must be a JSON object.",
        )

    pool = None
    try:
        pool = await get_arq_pool()
    except Exception as exc:  # noqa: BLE001 — briefs can wait; the row cannot
        logger.warning("PR Party webhook: brief queue unavailable: %s", exc)

    return await handle_webhook_event(db, event, payload, pool=pool)
