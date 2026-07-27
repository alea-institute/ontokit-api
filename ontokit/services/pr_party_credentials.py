"""PR Party reviewer registry and credential handling (KTD12, KTD13).

Three responsibilities live here, and they are together because they are the
same secret's lifecycle seen from three angles.

**1. The registry is provisioned from configuration (KTD12).**
``reconcile_reviewers`` reads ``PR_PARTY_REVIEWERS`` (``zitadel_id:github_login``
pairs, the same shape as ``SUPERADMIN_USER_IDS``) and makes ``pr_party_reviewer``
match it at startup. There is no seed migration and no admin mutation endpoint:
reviewer identity is environment data. The reconciliation is deliberately
opinionated in three places:

- A row whose ``github_login`` CHANGED keeps its credential row but has it
  marked ``last_error='login_changed'`` with ``last_validated_at`` cleared. The
  stored PAT almost certainly still belongs to the same human, but it now proves
  nothing about the *newly configured* login, so it must be re-validated before
  it can actuate again. Destroying it instead would punish a rename.
- A row absent from the config is **deleted**, and U1's ``ON DELETE CASCADE``
  takes its credential with it. De-registering someone must not leave their
  encrypted write PAT sitting in the table.
- An **empty** ``PR_PARTY_REVIEWERS`` is treated as *unconfigured*, not as "no
  reviewers". A dropped environment variable would otherwise delete every
  reviewer and every stored credential on the next boot — a config typo should
  not be able to do that.

Node-id resolution (rename-proof identity for R18's own-PR detection) is
best-effort: GitHub being unreachable at boot leaves ``github_node_id`` NULL
with a WARNING and is retried on the next startup or sweep. Reconcile never
fails boot.

**2. Credentials are validated before they are stored (KTD13).**
``save_credential`` proves the submitted PAT authenticates as the registered
login and can perform a real read *before* it writes anything. Two consequences
that matter: a token belonging to a different account is rejected with nothing
stored, and a failed rotation leaves the previously working credential intact —
the write only happens on the success path.

The capability probe is a real API read, never an inspection of
``x-oauth-scopes``: fine-grained PATs leave that header empty, so a scope check
would reject exactly the credentials the deployment is supposed to use.

**3. Encryption is MultiFernet, domain-separated from LLM provider keys.**
Modeled on :mod:`ontokit.services.llm.crypto` — the current ``SECRET_KEY``
encrypts, retired keys in ``SECRET_KEY_PREVIOUS`` still decrypt, and the shipped
default secret hard-fails outside local development. The key derivation is
domain-separated (``pr-party-credential:`` prefix), so an oracle over one
ciphertext domain does not extend to the other. Deliberately NOT the bare-Fernet
``core.encryption``, which has no rotation path, and deliberately not the
deprecated ``user_github_tokens`` table, whose write path is retired.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.fernet import Fernet, MultiFernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.core.config import settings
from ontokit.models.pr_party import PRPartyCredential, PRPartyReviewer
from ontokit.schemas.pr_party import PRPartyCredentialHealth, PRPartyGenerationTokenStatus
from ontokit.services.pr_party_github import (
    AuthenticatedIdentity,
    PRPartyGitHubClient,
    PRPartyGitHubError,
    TokenExpiredError,
    actuation_client,
    generation_client,
)

logger = logging.getLogger(__name__)

__all__ = [
    "EXPIRY_WARNING_DAYS",
    "GENERATION_STATUS_TTL",
    "GITHUB_TOKEN_SETTINGS_URL",
    "LOGIN_CHANGED_ERROR",
    "CredentialRejected",
    "CredentialValidationUnavailable",
    "PRPartyCredentialError",
    "PRPartyCredentialService",
    "ReconcileResult",
    "credential_health",
    "decrypt_reviewer_token",
    "encrypt_reviewer_token",
    "get_generation_token_status",
    "is_degraded",
    "reconcile_reviewers",
    "reset_generation_token_cache",
    "rotate_reviewer_token",
]

#: Written to ``last_error`` when config renames a reviewer's GitHub login: the
#: stored PAT is retained but must be re-validated before it can actuate.
LOGIN_CHANGED_ERROR = "login_changed"

#: Deleting our copy of a PAT does not revoke it on GitHub, and the app cannot
#: do that on the reviewer's behalf (KTD13). This is where they can.
GITHUB_TOKEN_SETTINGS_URL = "https://github.com/settings/tokens"

#: How far ahead an expiry is worth warning about. A fine-grained PAT dies on a
#: fixed date; finding out at merge time is finding out too late.
EXPIRY_WARNING_DAYS = 30

#: The shared generation token has no table row (U1 is closed and this is
#: environment data), so its health is computed live and cached this long.
GENERATION_STATUS_TTL = timedelta(hours=1)

#: The shipped default for ``settings.secret_key``. Encrypting a write PAT under
#: a publicly known constant is equivalent to storing it in plaintext.
_INSECURE_DEFAULT_SECRET = "change-me-in-production"  # noqa: S105 (not a real secret)

#: Domain separator: a PR Party ciphertext must not be readable in the LLM
#: provider-key domain, and vice versa, even though both derive from SECRET_KEY.
_KEY_DOMAIN = "pr-party-credential:"

#: The only fields a reviewer may write on their own registry row (R23).
_MUTABLE_SETTINGS_FIELDS = frozenset({"merge_default", "ntfy_topic"})

GenerationClientFactory = Callable[[str], PRPartyGitHubClient]
ActuationClientFactory = Callable[[str], PRPartyGitHubClient]


# --- Encryption -------------------------------------------------------------


def _derive_fernet(secret: str) -> Fernet:
    key = hashlib.sha256(f"{_KEY_DOMAIN}{secret}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _previous_secrets() -> list[str]:
    raw = settings.secret_key_previous or ""
    return [
        s.strip() for s in raw.split(",") if s.strip() and s.strip() != _INSECURE_DEFAULT_SECRET
    ]


def _get_fernet() -> MultiFernet:
    """Current ``SECRET_KEY`` encrypts; retired keys decrypt (zero-downtime rotation)."""
    if settings.secret_key == _INSECURE_DEFAULT_SECRET:
        # Any deployed environment is shared and network-reachable, and these
        # ciphertexts are GitHub *write* credentials for a real org.
        if not settings.is_development:
            raise RuntimeError(
                f"SECRET_KEY is the insecure shipped default in a deployed environment "
                f"(app_env={settings.app_env!r}). PR Party reviewer write PATs would be "
                "encrypted under a publicly-known constant. Set a strong SECRET_KEY."
            )
        logger.warning(
            "SECRET_KEY is the insecure shipped default; PR Party reviewer PATs are "
            "encrypted under a publicly-known constant. Local development only."
        )

    return MultiFernet(
        [_derive_fernet(settings.secret_key), *(_derive_fernet(s) for s in _previous_secrets())]
    )


def encrypt_reviewer_token(plaintext: str) -> str:
    """Encrypt a reviewer's write PAT under the current ``SECRET_KEY``."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_reviewer_token(ciphertext: str) -> str:
    """Decrypt a stored PAT, falling back to retired keys after a rotation."""
    return _get_fernet().decrypt(ciphertext.encode()).decode()


def rotate_reviewer_token(ciphertext: str) -> str:
    """Re-encrypt stored ciphertext under the current key, so a retired key can be dropped."""
    return _get_fernet().rotate(ciphertext.encode()).decode()


# --- Errors -----------------------------------------------------------------


class PRPartyCredentialError(Exception):
    """Root of the credential-service failures the routes translate."""


class CredentialRejected(PRPartyCredentialError):
    """The submitted token is wrong — the caller can fix it. Nothing was stored."""


class CredentialValidationUnavailable(PRPartyCredentialError):
    """GitHub could not be reached to validate. Not the caller's fault; retry."""


# --- Health -----------------------------------------------------------------


def _as_utc(value: datetime) -> datetime:
    """Treat a naive timestamp as UTC rather than crashing the comparison."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def credential_health(
    credential: PRPartyCredential | None,
    *,
    now: datetime | None = None,
) -> PRPartyCredentialHealth | None:
    """Project a stored credential onto its public health shape (never the token)."""
    if credential is None:
        return None

    moment = now or datetime.now(UTC)
    expires_at = _as_utc(credential.expires_at) if credential.expires_at is not None else None

    return PRPartyCredentialHealth(
        expires_at=expires_at,
        last_validated_at=credential.last_validated_at,
        last_error=credential.last_error,
        expired=expires_at is not None and expires_at <= moment,
        expires_soon=expires_at is not None
        and expires_at <= moment + timedelta(days=EXPIRY_WARNING_DAYS),
    )


def is_degraded(health: PRPartyCredentialHealth | None) -> bool:
    """R12: registered but unable to actuate — the dashboard still renders."""
    return health is None or health.expired or health.last_error is not None


# --- Shared generation token (KTD13) ----------------------------------------


@dataclass(frozen=True)
class _GenerationStatusCache:
    token: str
    checked_at: datetime
    status: PRPartyGenerationTokenStatus


_generation_status_cache: _GenerationStatusCache | None = None


def reset_generation_token_cache() -> None:
    """Drop the in-process cache (tests, and any future config-reload path)."""
    global _generation_status_cache  # noqa: PLW0603
    _generation_status_cache = None


async def get_generation_token_status(
    *,
    client_factory: GenerationClientFactory = generation_client,
    now: datetime | None = None,
) -> PRPartyGenerationTokenStatus | None:
    """Health of the shared read-only token, computed live and cached ~1h.

    The shared token lives in the deployment's environment and has no row of its
    own — U1's schema is closed, and inventing a key-value table for one value
    would be worse than paying one lightweight ``GET /user`` per hour. Failures
    are recorded as ``last_error`` rather than raised: a dead generation token
    must show up on the settings surface, not 500 the capability read.
    """
    global _generation_status_cache  # noqa: PLW0603

    token = settings.pr_party_readonly_token
    if not token:
        return None

    moment = now or datetime.now(UTC)
    cached = _generation_status_cache
    if (
        cached is not None
        and cached.token == token
        and moment - cached.checked_at < (GENERATION_STATUS_TTL)
    ):
        return cached.status

    try:
        identity = await client_factory(token).get_authenticated_user()
        status = PRPartyGenerationTokenStatus(expires_at=identity.token_expires_at, last_error=None)
    except Exception as e:  # noqa: BLE001 — status surface, never a raise path
        logger.warning("PR Party generation token check failed: %r", e)
        status = PRPartyGenerationTokenStatus(expires_at=None, last_error=str(e) or repr(e))

    _generation_status_cache = _GenerationStatusCache(token=token, checked_at=moment, status=status)
    return status


# --- Startup reconcile (KTD12) ----------------------------------------------


@dataclass(frozen=True)
class ReconcileResult:
    """What one reconciliation pass did, for the startup log and for tests."""

    added: int = 0
    updated: int = 0
    removed: int = 0
    resolved: int = 0
    unresolved: int = 0
    #: True when ``PR_PARTY_REVIEWERS`` is unset — the registry was left alone.
    skipped: bool = False


async def reconcile_reviewers(
    db: AsyncSession,
    *,
    client_factory: GenerationClientFactory = generation_client,
) -> ReconcileResult:
    """Make ``pr_party_reviewer`` match ``PR_PARTY_REVIEWERS`` (KTD12).

    See the module docstring for the three deliberate behaviors: a renamed login
    invalidates (but keeps) the credential, a removed reviewer's row and
    credential are deleted, and an empty config is "unconfigured", not "nobody".
    """
    configured = settings.pr_party_reviewer_map
    if not configured:
        logger.warning(
            "PR_PARTY_REVIEWERS is unset — leaving the PR Party reviewer registry "
            "untouched. Set it to 'zitadel_id:github_login,...' to provision reviewers."
        )
        return ReconcileResult(skipped=True)

    declared = len([entry for entry in settings.pr_party_reviewers.split(",") if entry.strip()])
    if declared > len(configured):
        logger.warning(
            "PR_PARTY_REVIEWERS: ignored %d malformed entr(y/ies); expected "
            "'zitadel_id:github_login' pairs.",
            declared - len(configured),
        )

    result = await db.execute(select(PRPartyReviewer))
    existing = {row.zitadel_user_id: row for row in result.scalars().all()}

    added = updated = removed = 0
    needs_resolution: list[PRPartyReviewer] = []

    for zitadel_id, login in configured.items():
        row = existing.get(zitadel_id)
        if row is None:
            row = PRPartyReviewer(zitadel_user_id=zitadel_id, github_login=login)
            db.add(row)
            added += 1
            needs_resolution.append(row)
        elif row.github_login != login:
            logger.info(
                "PR Party reviewer %s renamed %s -> %s; stored credential invalidated "
                "pending re-validation.",
                zitadel_id,
                row.github_login,
                login,
            )
            row.github_login = login
            row.github_node_id = None
            await _invalidate_credential(db, row)
            updated += 1
            needs_resolution.append(row)
        elif row.github_node_id is None:
            needs_resolution.append(row)

    for zitadel_id, row in existing.items():
        if zitadel_id not in configured:
            # ON DELETE CASCADE (U1) removes the credential with the row — a
            # de-registered reviewer leaves no encrypted PAT behind.
            logger.info("PR Party reviewer %s removed from config; deleting row.", zitadel_id)
            await db.delete(row)
            removed += 1

    resolved, unresolved = await _resolve_node_ids(needs_resolution, client_factory)

    await db.commit()

    logger.info(
        "PR Party reviewer registry reconciled: +%d ~%d -%d (node ids: %d resolved, %d pending)",
        added,
        updated,
        removed,
        resolved,
        unresolved,
    )
    return ReconcileResult(
        added=added,
        updated=updated,
        removed=removed,
        resolved=resolved,
        unresolved=unresolved,
    )


async def _invalidate_credential(db: AsyncSession, reviewer: PRPartyReviewer) -> None:
    """Mark a stored PAT as needing re-validation without destroying it."""
    result = await db.execute(
        select(PRPartyCredential).where(PRPartyCredential.reviewer_id == reviewer.id)
    )
    credential = result.scalar_one_or_none()
    if credential is None:
        return
    credential.last_error = LOGIN_CHANGED_ERROR
    credential.last_validated_at = None


async def _resolve_node_ids(
    rows: list[PRPartyReviewer],
    client_factory: GenerationClientFactory,
) -> tuple[int, int]:
    """Best-effort ``login -> node_id``. Never raises: boot must not depend on GitHub."""
    if not rows:
        return (0, 0)

    token = settings.pr_party_readonly_token
    if not token:
        logger.warning(
            "PR_PARTY_READONLY_TOKEN is unset — %d reviewer GitHub node id(s) left "
            "unresolved; own-PR detection falls back to login matching until it is set.",
            len(rows),
        )
        return (0, len(rows))

    client = client_factory(token)
    resolved = unresolved = 0
    for row in rows:
        try:
            ref = await client.get_user(row.github_login)
        except Exception as e:  # noqa: BLE001 — startup must survive any GitHub failure
            logger.warning(
                "PR Party reconcile: could not resolve the GitHub node id for %r (%r); "
                "retrying on the next startup.",
                row.github_login,
                e,
            )
            unresolved += 1
            continue

        if ref.node_id:
            row.github_node_id = ref.node_id
            resolved += 1
        else:
            unresolved += 1

    return (resolved, unresolved)


# --- Credential service -----------------------------------------------------


class PRPartyCredentialService:
    """Reads the registry and owns the lifecycle of one reviewer's write PAT."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        actuation_factory: ActuationClientFactory = actuation_client,
    ) -> None:
        self.db = db
        self._actuation_factory = actuation_factory

    # --- Registry reads ---

    async def get_reviewer(self, zitadel_user_id: str) -> PRPartyReviewer | None:
        """The caller's registry row, or None if they are not a reviewer."""
        result = await self.db.execute(
            select(PRPartyReviewer).where(PRPartyReviewer.zitadel_user_id == zitadel_user_id)
        )
        return result.scalar_one_or_none()

    async def get_credential(self, reviewer_id: uuid.UUID) -> PRPartyCredential | None:
        result = await self.db.execute(
            select(PRPartyCredential).where(PRPartyCredential.reviewer_id == reviewer_id)
        )
        return result.scalar_one_or_none()

    async def resolve_token(self, reviewer: PRPartyReviewer) -> str | None:
        """Decrypt the reviewer's PAT for actuation (U6), or None if unusable.

        Returns None rather than raising when there is no credential: a missing
        or undecryptable PAT is R12's degraded path, where the verdict is
        recorded as intent instead of posted.
        """
        credential = await self.get_credential(reviewer.id)
        if credential is None:
            return None
        try:
            return decrypt_reviewer_token(credential.encrypted_token)
        except Exception as e:  # noqa: BLE001 — degrade, don't 500 the verdict path
            logger.warning(
                "PR Party: stored credential for %s could not be decrypted (%r); "
                "treating the reviewer as degraded.",
                reviewer.zitadel_user_id,
                e,
            )
            return None

    async def save(self) -> None:
        """Commit a mutation made directly on a credential row.

        U6 marks a dead PAT through its action store, which already owns a
        commit; U7 writes no rows of its own and so has no store to borrow one
        from. Rather than let the Q&A service reach into ``self.db``, the
        credential's owner exposes the one commit it needs.
        """
        await self.db.commit()

    # --- Credential lifecycle ---

    async def save_credential(self, reviewer: PRPartyReviewer, token: str) -> PRPartyCredential:
        """Validate a submitted PAT, then store it. Rotation uses this same path.

        Every failure mode raises *before* the write, so an invalid replacement
        can never destroy a working credential.
        """
        clean = token.strip()
        if not clean:
            raise CredentialRejected("A GitHub token is required.")

        client = self._actuation_factory(clean)
        identity = await self._authenticate(client)

        if identity.login.casefold() != reviewer.github_login.casefold():
            raise CredentialRejected(
                f"That token authenticates as {identity.login!r}, but this account is "
                f"registered for {reviewer.github_login!r}. Nothing was stored."
            )

        await self._probe_capability(client, identity.login)

        credential = await self.get_credential(reviewer.id)
        now = datetime.now(UTC)
        ciphertext = encrypt_reviewer_token(clean)

        if credential is None:
            credential = PRPartyCredential(
                reviewer_id=reviewer.id,
                encrypted_token=ciphertext,
                expires_at=identity.token_expires_at,
                last_validated_at=now,
                last_error=None,
            )
            self.db.add(credential)
        else:
            credential.encrypted_token = ciphertext
            credential.expires_at = identity.token_expires_at
            credential.last_validated_at = now
            credential.last_error = None

        # Backfill the rename-proof identity if reconcile could not reach GitHub.
        if identity.node_id and reviewer.github_node_id != identity.node_id:
            reviewer.github_node_id = identity.node_id

        await self.db.commit()
        return credential

    async def delete_credential(self, reviewer: PRPartyReviewer) -> bool:
        """Forget our copy of the PAT. Returns whether one existed."""
        credential = await self.get_credential(reviewer.id)
        if credential is None:
            return False
        await self.db.delete(credential)
        await self.db.commit()
        return True

    # --- Settings ---

    async def update_settings(
        self,
        reviewer: PRPartyReviewer,
        updates: Mapping[str, Any],
    ) -> PRPartyReviewer:
        """Write the caller's own settings (R23).

        The target is the reviewer row passed in — resolved from the
        authenticated caller, never from the request body — and only the
        allow-listed fields are assignable, so an unexpected key cannot reach
        ``github_login`` or ``zitadel_user_id``.
        """
        for field, value in updates.items():
            if field not in _MUTABLE_SETTINGS_FIELDS:
                logger.debug("Ignoring non-settable PR Party settings field %r", field)
                continue
            setattr(reviewer, field, value)

        await self.db.commit()
        return reviewer

    # --- GitHub validation ---

    async def _authenticate(self, client: PRPartyGitHubClient) -> AuthenticatedIdentity:
        try:
            return await client.get_authenticated_user()
        except TokenExpiredError as e:
            raise CredentialRejected(
                "GitHub rejected that token (401). It may be expired, revoked, or mistyped."
            ) from e
        except PRPartyGitHubError as e:
            raise CredentialValidationUnavailable(
                f"Could not verify the token with GitHub: {e}"
            ) from e
        except Exception as e:  # noqa: BLE001 — network faults are not user error
            raise CredentialValidationUnavailable(
                f"Could not reach GitHub to verify the token: {e!r}"
            ) from e

    async def _probe_capability(self, client: PRPartyGitHubClient, login: str) -> None:
        """One real read with the submitted token.

        Deliberately not a scope inspection: fine-grained PATs send an empty
        ``x-oauth-scopes``, so reading it would reject every correctly
        provisioned reviewer credential (KTD13).
        """
        try:
            await client.get_user(login)
        except TokenExpiredError as e:
            raise CredentialRejected(
                "GitHub rejected that token when reading with it. Check that it is active."
            ) from e
        except PRPartyGitHubError as e:
            raise CredentialValidationUnavailable(
                f"Could not complete the GitHub capability check: {e}"
            ) from e
        except Exception as e:  # noqa: BLE001 — network faults are not user error
            raise CredentialValidationUnavailable(
                f"Could not reach GitHub for the capability check: {e!r}"
            ) from e
