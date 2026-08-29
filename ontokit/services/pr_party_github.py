"""PR Party GitHub client — the single place PR Party talks to GitHub.

GitHub is PR Party's system of record, so every fact the dashboard shows and
every verdict it casts passes through this module. Three things live here that
do not belong anywhere else:

- **The privilege split (KTD13).** A client is constructed in exactly one mode.
  A ``generation``-mode client carries the shared read-only token that powers
  intake, briefs, and status; it *refuses* review-create, merge, and
  comment-create outright, before any HTTP call is made. An ``actuation``-mode
  client carries one reviewer's write PAT and is the only thing that can post a
  verdict (R8) or merge (R11). The refusal is a property of the object, not a
  rule callers have to remember, so a prompt-injected brief pipeline holding a
  generation client has nothing to actuate with.
- **The error taxonomy.** ``401 -> TokenExpiredError`` (the credential is dead;
  the verdict degrades to recorded intent per R12), ``403/429`` with an
  exhausted rate-limit budget ``-> RateLimitedError`` carrying the reset time,
  ``422`` on review-create ``-> SelfApprovalError`` (R18's own-PR guard as
  GitHub enforces it), and merge ``409 -> StaleCardError`` (the head moved out
  from under the card). A transport failure that never reaches a status at all
  becomes a ``GitHubAPIError`` with ``status_code=None``, so "GitHub was
  unreachable" is inside the taxonomy rather than beside it. Mapping in one
  place is what keeps "expired PAT" from reaching a caller as an anonymous 500.
- **The surfaces ontokit-api has never touched.** Check-run rollups and
  mergeability-with-a-computing-state (R4), and the org-scoped PR search that
  feeds the sweep (R1). Observing the AI reviewer's own review (R3) reads the
  same PR detail and check surfaces.

Two GitHub behaviors are encoded as hard rules rather than caller etiquette,
because getting either wrong produces a silent wrong answer:

1. **A review always pins ``commit_id`` and always sends a non-PENDING
   ``event``.** Omitting ``event`` creates a *draft* review that no one ever
   sees; a response whose ``state`` is ``PENDING`` is therefore treated as a
   failure, not a verdict.
2. **A merge always sends ``sha``.** Without it, a merge authorized against one
   revision can land a different one.

Every request carries an explicit timeout — deliberately unlike
``github_service._request``, which has none.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

import httpx

from ontokit.core.config import settings
from ontokit.core.demo_targets import refuse_unscoped_demo_target
from ontokit.services.github_service import _enc

__all__ = [
    "ChecksRollup",
    "GenerationModeError",
    "GitHubAPIError",
    "GitHubUserRef",
    "IssueComment",
    "MergeNotAllowedError",
    "MergeResult",
    "Mergeability",
    "PRDetail",
    "PRPartyClientMode",
    "PRPartyGitHubClient",
    "PRPartyGitHubError",
    "PRPartyReview",
    "RateLimitedError",
    "ReviewEvent",
    "ReviewNotSubmittedError",
    "SearchedPR",
    "SelfApprovalError",
    "StaleCardError",
    "TokenExpiredError",
    "actuation_client",
    "default_generation_client",
    "generation_client",
    "parse_dt",
    "scrub_error",
    "split_repo",
    "to_int",
]

GITHUB_API_BASE: Final = "https://api.github.com"
GITHUB_API_VERSION: Final = "2022-11-28"

#: GitHub attaches this to responses authenticated with an expiring PAT.
#: KTD13 stores it as the credential's ``expires_at``.
TOKEN_EXPIRATION_HEADER: Final = "github-authentication-token-expiration"

DEFAULT_TIMEOUT: Final = httpx.Timeout(30.0, connect=10.0)


# --- Modes ------------------------------------------------------------------


class PRPartyClientMode(StrEnum):
    """KTD13's domain split, made explicit at construction time."""

    #: Shared read-only token. Intake, briefs, status. Cannot actuate.
    GENERATION = "generation"
    #: One reviewer's write PAT. The only mode that can review, merge, comment.
    ACTUATION = "actuation"


class ReviewEvent(StrEnum):
    """Submittable review verdicts. ``PENDING`` is deliberately absent."""

    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    COMMENT = "COMMENT"


class Mergeability(StrEnum):
    """R4: GitHub computes mergeability lazily, so ``null`` is a third state."""

    MERGEABLE = "mergeable"
    NOT_MERGEABLE = "not_mergeable"
    #: ``mergeable: null`` — GitHub is still computing. Retry; never render "no".
    COMPUTING = "computing"


class ChecksRollup(StrEnum):
    """Summary of a head SHA's check runs (R4).

    ``NONE`` is separate from ``SUCCESS`` on purpose: a repo with no configured
    checks has not passed anything, and readiness gating (R17) needs to tell
    those apart.
    """

    SUCCESS = "success"
    FAILURE = "failure"
    PENDING = "pending"
    NONE = "none"


#: Conclusions that make a completed check run count against the rollup.
#: ``neutral`` and ``skipped`` do not fail a required check on GitHub, so they
#: do not fail ours either.
FAILING_CONCLUSIONS: Final[frozenset[str]] = frozenset(
    {"failure", "timed_out", "action_required", "cancelled", "stale"}
)


class _Op(StrEnum):
    """Which call is in flight, so status codes map to the right error.

    409 means "the head moved" only on merge; 422 means "you cannot approve
    your own PR" only on review-create. Mapping them globally would mislabel
    unrelated failures.
    """

    DEFAULT = "default"
    REVIEW_CREATE = "review_create"
    MERGE = "merge"


# --- Errors -----------------------------------------------------------------


class PRPartyGitHubError(Exception):
    """Root of every failure this client raises."""


class GenerationModeError(PRPartyGitHubError):
    """A write was attempted on a read-only generation-mode client (KTD13).

    Raised before any HTTP call, so the read-only token is never even offered
    to a write endpoint.
    """

    def __init__(self, operation: str) -> None:
        self.operation = operation
        super().__init__(
            f"{operation}() requires an actuation-mode client; this client holds the "
            "shared read-only generation token (KTD13 domain split)."
        )


class GitHubAPIError(PRPartyGitHubError):
    """An unmapped non-2xx response from GitHub, or a transport-level failure.

    ``status_code`` is ``None`` when the request never produced an HTTP
    response at all — a connect failure, a read timeout, a broken TLS or
    protocol exchange. Callers that branch on the status must treat ``None`` as
    "no status, and certainly not the one you were hoping for" rather than
    assuming an int.
    """

    def __init__(self, message: str, *, status_code: int | None) -> None:
        self.status_code = status_code
        super().__init__(message)


class TokenExpiredError(GitHubAPIError):
    """401 — the PAT is expired or revoked. Verdicts degrade to intent (R12)."""


class RateLimitedError(GitHubAPIError):
    """403/429 with an exhausted rate-limit budget.

    ``reset_at`` (from ``x-ratelimit-reset``) and ``retry_after_seconds`` (from
    ``retry-after``, which secondary limits use instead) tell the sweep when it
    may try again instead of hammering.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        reset_at: datetime | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        self.reset_at = reset_at
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message, status_code=status_code)


class SelfApprovalError(GitHubAPIError):
    """422 on review-create — GitHub refuses a review of one's own PR (R18)."""


class StaleCardError(GitHubAPIError):
    """409 on merge — the head SHA moved; the card the reviewer acted on is stale."""


class MergeNotAllowedError(GitHubAPIError):
    """405 on merge — the PR is not in a mergeable state. Distinct from stale."""


class ReviewNotSubmittedError(PRPartyGitHubError):
    """GitHub returned a review in ``PENDING`` state — a draft, not a verdict."""

    def __init__(self, review_id: int | None, state: str) -> None:
        self.review_id = review_id
        self.state = state
        super().__init__(
            f"GitHub returned review state {state!r} (id={review_id}); a PENDING review "
            "was never submitted and must not be recorded as a verdict."
        )


def scrub_error(error: BaseException) -> str:
    """What is safe to persist or surface about a failed GitHub call.

    The exception *class* and the HTTP status, never the message. GitHub error
    bodies quote the request — which for these calls contains the reviewer's
    prose and, on a misconfiguration, can echo header material. The scrubbed
    form is read back by the reconciler, stored as a credential's
    ``last_error``, and rendered in a settings surface, so the rule is that
    nothing GitHub said in prose is ever kept.

    Lives beside the taxonomy it scrubs: ``status_code`` is this module's
    attribute, and ``getattr`` keeps the function total for exceptions (a
    transport failure, a stdlib error) that never carried one.
    """
    name = type(error).__name__
    status_code = getattr(error, "status_code", None)
    return f"{name} (HTTP {status_code})" if status_code is not None else name


# --- Result shapes ----------------------------------------------------------


@dataclass(frozen=True)
class GitHubUserRef:
    """A GitHub account. ``node_id`` is the rename-proof identity (KTD12)."""

    login: str
    node_id: str
    user_type: str | None = None


@dataclass(frozen=True)
class AuthenticatedIdentity:
    """``GET /user`` plus the token's expiry, which U2 stores as ``expires_at``."""

    login: str
    node_id: str
    token_expires_at: datetime | None = None


@dataclass(frozen=True)
class PRDetail:
    """``GET /repos/{o}/{r}/pulls/{n}`` — the authoritative per-PR facts."""

    repo_full_name: str
    number: int
    title: str
    body: str | None
    state: str
    draft: bool
    merged: bool
    head_sha: str
    head_ref: str
    base_ref: str
    author_login: str | None
    author_node_id: str | None
    author_type: str | None
    node_id: str | None
    mergeability: Mergeability
    mergeable_state: str | None
    html_url: str
    created_at: datetime | None
    updated_at: datetime | None

    @property
    def is_computing(self) -> bool:
        """True while GitHub has not finished computing mergeability (R4)."""
        return self.mergeability is Mergeability.COMPUTING


@dataclass(frozen=True)
class SearchedPR:
    """One item from the org-scoped search.

    Search returns *issue*-shaped items: there is no ``head``, hence no head
    SHA and no mergeability. The sweep uses these to enumerate, then detail-
    fetches each one.
    """

    repo_full_name: str
    number: int
    title: str
    state: str
    updated_at: datetime | None
    created_at: datetime | None
    author_login: str | None
    author_node_id: str | None
    author_type: str | None
    html_url: str
    node_id: str | None
    draft: bool


@dataclass(frozen=True)
class PRPartyReview:
    """A submitted review (R8).

    ``user_node_id`` is the rename-proof identity (KTD12) and is what the
    reconciler matches a reviewer's hand-cast review by; ``user_login`` is only
    the fallback for a review whose node id GitHub omitted. It carries a default
    because a review parsed from a payload without a ``user`` object has neither.
    """

    id: int
    state: str
    body: str | None
    commit_id: str | None
    user_login: str | None
    submitted_at: datetime | None
    html_url: str | None
    user_node_id: str | None = None


@dataclass(frozen=True)
class MergeResult:
    """The merge receipt (R11). ``sha`` is the *merge commit*, not the head."""

    sha: str
    merged: bool
    message: str


@dataclass(frozen=True)
class IssueComment:
    """A PR comment — how questions and answers travel (R13)."""

    id: int
    body: str
    user_login: str | None
    html_url: str | None
    created_at: datetime | None
    updated_at: datetime | None


# --- Parsing helpers --------------------------------------------------------


def split_repo(repo_full_name: str) -> tuple[str, str]:
    """``owner/repo`` -> ``(owner, repo)``, the shape every endpoint here wants.

    Raises :class:`ValueError` on a malformed name rather than a service's own
    refusal type: this module has no opinion about how a caller reports it, and
    each caller converts to the refusal its own API contract promises.
    """
    owner, _, repo = repo_full_name.strip("/").partition("/")
    if not owner or not repo:
        raise ValueError(f"Malformed repository name: {repo_full_name!r}")
    return owner, repo


def parse_dt(value: Any) -> datetime | None:
    """Parse a GitHub ISO-8601 timestamp (``2026-07-25T18:30:00Z``)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def parse_token_expiration(raw: str | None) -> datetime | None:
    """Parse the ``github-authentication-token-expiration`` header.

    GitHub documents a space-separated UTC form (``2026-08-01 15:30:00 UTC``);
    some responses use plain ISO-8601. Both are accepted. A classic PAT with no
    expiry simply omits the header, which is ``None`` — not an error — and an
    unparseable value degrades to ``None`` rather than failing a validation
    call over a formatting change.
    """
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    if value.upper().endswith("UTC"):
        value = value[:-3].strip() + "+00:00"
    return parse_dt(value)


def _repo_full_name_from_url(repository_url: Any) -> str:
    """Derive ``owner/repo`` from a search item's ``repository_url``.

    Search items carry no ``repository`` object on the default media type, so
    ``https://api.github.com/repos/catholicos/liturgy`` is the only carrier of
    the repo identity.
    """
    if not isinstance(repository_url, str):
        return ""
    parts = [segment for segment in repository_url.rstrip("/").split("/") if segment]
    if len(parts) < 2:
        return ""
    return f"{parts[-2]}/{parts[-1]}"


def _sub_object(data: Mapping[str, Any], key: str) -> dict[str, Any]:
    """Return a nested JSON object, or an empty dict if GitHub omitted it."""
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _user_field(data: Mapping[str, Any], field: str) -> Any:
    user = data.get("user")
    return user.get(field) if isinstance(user, Mapping) else None


def summarize_check_runs(check_runs: list[dict[str, Any]]) -> ChecksRollup:
    """Roll a head SHA's check runs up to one status (R4).

    Precedence is failure-first: a definite failure is actionable now and does
    not become less true when a straggler finishes. Only then does an
    unfinished run make the rollup ``PENDING``.
    """
    if not check_runs:
        return ChecksRollup.NONE

    incomplete = False
    for run in check_runs:
        status = str(run.get("status") or "").lower()
        conclusion = str(run.get("conclusion") or "").lower()
        if status != "completed":
            incomplete = True
            continue
        if conclusion in FAILING_CONCLUSIONS:
            return ChecksRollup.FAILURE

    return ChecksRollup.PENDING if incomplete else ChecksRollup.SUCCESS


# --- Client -----------------------------------------------------------------


class PRPartyGitHubClient:
    """GitHub access for PR Party, scoped to one privilege domain (KTD13).

    Construct through :func:`generation_client` or :func:`actuation_client` so
    the mode is never a positional afterthought.
    """

    def __init__(
        self,
        token: str,
        mode: PRPartyClientMode,
        *,
        timeout: httpx.Timeout | None = None,
        api_base: str = GITHUB_API_BASE,
    ) -> None:
        if not token:
            raise ValueError("A PR Party GitHub client requires a token.")
        self._token = token
        self._mode = mode
        self._timeout = timeout or DEFAULT_TIMEOUT
        self._api_base = api_base.rstrip("/")
        #: Expiry seen on the most recent response, or None if GitHub sent no
        #: header. U2 reads this after a validation call (KTD13).
        self.last_token_expires_at: datetime | None = None

    @property
    def mode(self) -> PRPartyClientMode:
        return self._mode

    @property
    def can_write(self) -> bool:
        """Whether this client may review, merge, or comment."""
        return self._mode is PRPartyClientMode.ACTUATION

    # --- Transport ---

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }

    def _require_actuation(self, operation: str) -> None:
        if not self.can_write:
            raise GenerationModeError(operation)

    async def _send(
        self,
        method: str,
        endpoint: str,
        *,
        json: dict[str, Any] | None = None,
        op: _Op = _Op.DEFAULT,
        accept: str | None = None,
    ) -> httpx.Response:
        """Issue one authenticated request and map failures to the taxonomy.

        Unlike ``github_service._request`` this always sets an explicit timeout
        and never calls ``raise_for_status()`` — the status code has to reach
        :meth:`_map_error`, where PR Party's meaning is attached to it.

        ``accept`` overrides the media type for the surfaces that are not JSON
        objects (the ``.diff`` representation of a pull request).

        A failure that never reaches an HTTP status — connect refused, read
        timeout, protocol error — is folded into the same taxonomy as a
        ``GitHubAPIError`` with ``status_code=None``. Letting a raw
        ``httpx.TransportError`` out would walk straight past every caller's
        ``except GitHubAPIError`` and turn a degradable actuation into an
        unhandled 500. The ``try`` covers only the transport; status mapping
        happens below it, so :meth:`_map_error`'s work is never re-wrapped.
        """
        headers = self._headers()
        if accept is not None:
            headers["Accept"] = accept

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                # `endpoint` is assembled from _enc()-encoded segments at every
                # call site below (same convention as github_service.py, PR
                # #116), so no user-controlled value can escape its path
                # segment. The taint analyzer does not recognize
                # urllib.parse.quote() as a sanitizer; this justification covers
                # the single request sink in this module.
                # nosemgrep: python.fastapi.net.tainted-fastapi-http-request-httpx.tainted-fastapi-http-request-httpx
                response = await client.request(
                    method=method,
                    url=f"{self._api_base}{endpoint}",
                    headers=headers,
                    json=json,
                )
        except httpx.HTTPError as exc:
            # Class name only: httpx messages can quote the URL and, on a proxy
            # misconfiguration, connection material. Same rule as
            # :func:`scrub_error` applies to what gets persisted.
            raise GitHubAPIError(
                f"GitHub request failed before any response ({type(exc).__name__}).",
                status_code=None,
            ) from exc

        self.last_token_expires_at = parse_token_expiration(
            response.headers.get(TOKEN_EXPIRATION_HEADER)
        )

        if response.status_code >= 400:
            raise self._map_error(response, op)

        return response

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json: dict[str, Any] | None = None,
        op: _Op = _Op.DEFAULT,
    ) -> dict[str, Any]:
        """:meth:`_send`, insisting the body is a JSON *object*."""
        response = await self._send(method, endpoint, json=json, op=op)

        if response.status_code == 204:
            return {}

        data = _safe_json(response)
        if not isinstance(data, dict):
            raise GitHubAPIError(
                f"Expected a JSON object from {endpoint}, got {type(data).__name__}.",
                status_code=response.status_code,
            )
        return data

    async def _request_list(self, method: str, endpoint: str) -> list[Any]:
        """:meth:`_send`, insisting the body is a JSON *array* (collections)."""
        response = await self._send(method, endpoint)
        data = _safe_json(response)
        if not isinstance(data, list):
            raise GitHubAPIError(
                f"Expected a JSON array from {endpoint}, got {type(data).__name__}.",
                status_code=response.status_code,
            )
        return data

    def _map_error(self, response: httpx.Response, op: _Op) -> PRPartyGitHubError:
        """The one place a GitHub status code acquires a PR Party meaning."""
        status = response.status_code
        headers = response.headers
        message = _error_message(response)

        if status == 401:
            return TokenExpiredError(message, status_code=status)

        if status in (403, 429) and str(headers.get("x-ratelimit-remaining", "")) == "0":
            return RateLimitedError(
                message,
                status_code=status,
                reset_at=_epoch_to_datetime(headers.get("x-ratelimit-reset")),
                retry_after_seconds=to_int(headers.get("retry-after")),
            )

        if status == 422 and op is _Op.REVIEW_CREATE:
            return SelfApprovalError(message, status_code=status)

        if op is _Op.MERGE:
            if status == 409:
                return StaleCardError(message, status_code=status)
            if status == 405:
                return MergeNotAllowedError(message, status_code=status)

        return GitHubAPIError(message, status_code=status)

    # --- Read surfaces (both modes) ---

    async def get_authenticated_user(self) -> AuthenticatedIdentity:
        """``GET /user`` — validates the token and resolves its identity.

        Deliberately does not read ``x-oauth-scopes``: fine-grained PATs leave
        it empty, so a scope check would reject every correctly provisioned
        reviewer credential (KTD13).
        """
        data = await self._request("GET", "/user")
        return AuthenticatedIdentity(
            login=str(data.get("login", "")),
            node_id=str(data.get("node_id", "")),
            token_expires_at=self.last_token_expires_at,
        )

    async def get_user(self, login: str) -> GitHubUserRef:
        """``GET /users/{login}`` — resolves a login to its node id (KTD12)."""
        data = await self._request("GET", f"/users/{_enc(login)}")
        return GitHubUserRef(
            login=str(data.get("login", "")),
            node_id=str(data.get("node_id", "")),
            user_type=data.get("type"),
        )

    async def get_pull_request(self, owner: str, repo: str, number: int) -> PRDetail:
        """``GET /repos/{o}/{r}/pulls/{n}`` with three-state mergeability (R4)."""
        data = await self._request("GET", f"/repos/{_enc(owner)}/{_enc(repo)}/pulls/{int(number)}")
        return self._parse_pr_detail(data, owner=owner, repo=repo)

    async def get_check_runs_rollup(self, owner: str, repo: str, sha: str) -> ChecksRollup:
        """``GET /repos/{o}/{r}/commits/{sha}/check-runs``, summarized (R4).

        One page of 100 covers every CatholicOS repo comfortably; a repo that
        outgrows it would need pagination here.
        """
        data = await self._request(
            "GET",
            f"/repos/{_enc(owner)}/{_enc(repo)}/commits/{_enc(sha)}/check-runs?per_page=100",
        )
        runs = data.get("check_runs")
        return summarize_check_runs(runs if isinstance(runs, list) else [])

    async def search_org_open_prs(
        self,
        org: str,
        *,
        page: int = 1,
        per_page: int = 100,
    ) -> list[SearchedPR]:
        """``GET /search/issues?q=org:{org}+is:pr+is:open`` — the sweep's source (R1).

        One org-wide query replaces per-repo enumeration. Items are issue-
        shaped, so they carry no head SHA; the caller detail-fetches each PR it
        intends to act on. Callers paginate by incrementing ``page`` until a
        short page comes back (search caps at 1000 results).
        """
        query = _enc(f"org:{org} is:pr is:open")
        data = await self._request(
            "GET",
            f"/search/issues?q={query}&sort=updated&order=desc"
            f"&per_page={int(per_page)}&page={int(page)}",
        )
        items = data.get("items")
        if not isinstance(items, list):
            return []
        return [self._parse_search_item(item) for item in items if isinstance(item, dict)]

    # --- Brief context surfaces (U5; read-only, generation mode uses these) ---

    async def get_pr_diff(self, owner: str, repo: str, number: int) -> str:
        """``GET /repos/{o}/{r}/pulls/{n}`` under the ``.diff`` media type.

        The unified diff is the single largest piece of *untrusted* context a
        brief is built from (R21). It is returned verbatim as text; capping and
        delimiting it is the brief worker's job, not the transport's.
        """
        response = await self._send(
            "GET",
            f"/repos/{_enc(owner)}/{_enc(repo)}/pulls/{int(number)}",
            accept="application/vnd.github.v3.diff",
        )
        return response.text

    async def get_pr_commit_messages(
        self, owner: str, repo: str, number: int, *, per_page: int = 100
    ) -> list[str]:
        """``GET .../pulls/{n}/commits`` reduced to commit messages.

        Messages only, deliberately: authorship and timestamps are not brief
        material, and every field that never leaves this method is a field that
        can never end up in a prompt.
        """
        data = await self._request_list(
            "GET",
            f"/repos/{_enc(owner)}/{_enc(repo)}/pulls/{int(number)}/commits"
            f"?per_page={int(per_page)}",
        )
        messages: list[str] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            commit = _sub_object(item, "commit")
            message = commit.get("message")
            if isinstance(message, str) and message.strip():
                messages.append(message)
        return messages

    async def get_repo_file(
        self, owner: str, repo: str, path: str, ref: str, *, max_bytes: int = 50_000
    ) -> str | None:
        """``GET /repos/{o}/{r}/contents/{path}?ref={ref}`` — one file, as text.

        Scoped to a repo the caller names and a path the caller has already
        validated: ``path`` is encoded with ``allow_slash=True`` because
        in-repo paths contain meaningful separators, which is exactly why the
        *caller* must reject ``..`` and absolute forms before calling (the brief
        worker does, in ``extract_artifact_paths``).

        Returns ``None`` — never raises — when the entry is missing, is not a
        file, exceeds ``max_bytes``, or does not decode as UTF-8: a linked
        artifact is a nice-to-have, and no brief is worth failing over one.
        """
        try:
            data = await self._request(
                "GET",
                f"/repos/{_enc(owner)}/{_enc(repo)}/contents/{_enc(path, allow_slash=True)}"
                f"?ref={_enc(ref)}",
            )
        except PRPartyGitHubError:
            return None

        if data.get("type") != "file" or data.get("encoding") != "base64":
            return None
        size = to_int(data.get("size"))
        if size is not None and size > max_bytes:
            return None

        raw = data.get("content")
        if not isinstance(raw, str):
            return None
        try:
            decoded = base64.b64decode(raw)
        except (ValueError, binascii.Error):
            return None
        if len(decoded) > max_bytes:
            return None
        try:
            return decoded.decode("utf-8")
        except UnicodeDecodeError:
            return None

    # --- Write surfaces (actuation mode only) ---

    async def create_review(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        commit_id: str,
        event: ReviewEvent | str,
        body: str | None = None,
    ) -> PRPartyReview:
        """``POST .../pulls/{n}/reviews`` — a real review under the reviewer (R8).

        ``event`` is required and must be submittable: omitting it, or passing
        ``PENDING``, creates a draft review nobody ever sees. ``commit_id``
        pins the verdict to the revision the reviewer actually read, so a push
        that lands mid-review cannot silently inherit the approval.
        """
        self._require_actuation("create_review")
        refuse_unscoped_demo_target(owner, repo, "PR Party create_review")

        resolved_event = _validate_review_event(event)
        if not commit_id:
            raise ValueError("create_review requires a commit_id pinning the reviewed revision.")

        payload: dict[str, Any] = {"event": resolved_event.value, "commit_id": commit_id}
        if body:
            payload["body"] = body

        data = await self._request(
            "POST",
            f"/repos/{_enc(owner)}/{_enc(repo)}/pulls/{int(number)}/reviews",
            json=payload,
            op=_Op.REVIEW_CREATE,
        )

        state = str(data.get("state", ""))
        if state.upper() == "PENDING":
            raise ReviewNotSubmittedError(to_int(data.get("id")), state)

        return _parse_review(data)

    async def get_pr_reviews(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        per_page: int = 100,
    ) -> list[PRPartyReview]:
        """``GET .../pulls/{n}/reviews`` — what GitHub says actually happened (U8).

        A *read*, so the sweep's generation token is enough: the reconciler runs
        on the shared cron, and asking it for a reviewer's write PAT would both
        spend one principal's rate budget on another's row and put a credential
        in the reconciliation path for no gain.

        One page, oldest first. Dismissals are visible here as ``state ==
        "DISMISSED"`` on the review itself, which is why the reconciler does not
        need a separate dismissal feed.
        """
        data = await self._request_list(
            "GET",
            f"/repos/{_enc(owner)}/{_enc(repo)}/pulls/{int(number)}/reviews"
            f"?per_page={int(per_page)}",
        )
        return [_parse_review(item) for item in data if isinstance(item, dict)]

    async def merge_pull_request(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        sha: str,
        merge_method: str = "merge",
        commit_title: str | None = None,
        commit_message: str | None = None,
    ) -> MergeResult:
        """``PUT .../pulls/{n}/merge`` — always pinned to ``sha`` (R11).

        GitHub answers 409 when the head has moved since ``sha``; that is a
        stale card, and it is the whole reason ``sha`` is not optional here.
        """
        self._require_actuation("merge_pull_request")
        refuse_unscoped_demo_target(owner, repo, "PR Party merge_pull_request")

        if not sha:
            raise ValueError("merge_pull_request requires the head sha it was authorized against.")

        payload: dict[str, Any] = {"sha": sha, "merge_method": merge_method}
        if commit_title:
            payload["commit_title"] = commit_title
        if commit_message:
            payload["commit_message"] = commit_message

        data = await self._request(
            "PUT",
            f"/repos/{_enc(owner)}/{_enc(repo)}/pulls/{int(number)}/merge",
            json=payload,
            op=_Op.MERGE,
        )

        return MergeResult(
            sha=str(data.get("sha", "")),
            merged=bool(data.get("merged", False)),
            message=str(data.get("message", "")),
        )

    async def create_comment(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        body: str,
    ) -> IssueComment:
        """``POST .../issues/{n}/comments`` — a PR is an issue for comments (R13)."""
        self._require_actuation("create_comment")
        refuse_unscoped_demo_target(owner, repo, "PR Party create_comment")

        if not body:
            raise ValueError("create_comment requires a non-empty body.")

        data = await self._request(
            "POST",
            f"/repos/{_enc(owner)}/{_enc(repo)}/issues/{int(number)}/comments",
            json={"body": body},
        )

        return _parse_issue_comment(data)

    async def get_issue_comments(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        per_page: int = 100,
    ) -> list[IssueComment]:
        """``GET .../issues/{n}/comments`` — the Q&A thread itself (R13).

        A *read*, so it is available in generation mode: the card's Q&A thread
        is projected from these comments on every open (U7), and doing that with
        a reviewer's write PAT would spend one principal's rate budget to render
        another's dashboard.

        One page, oldest first — GitHub's default ordering, which is the order
        the thread is built in. A PR whose conversation outgrows a hundred
        comments has bigger problems than a truncated Q&A panel.
        """
        data = await self._request_list(
            "GET",
            f"/repos/{_enc(owner)}/{_enc(repo)}/issues/{int(number)}/comments"
            f"?per_page={int(per_page)}",
        )
        return [_parse_issue_comment(item) for item in data if isinstance(item, dict)]

    # --- Parsing ---

    def _parse_pr_detail(self, data: dict[str, Any], *, owner: str, repo: str) -> PRDetail:
        head = _sub_object(data, "head")
        base = _sub_object(data, "base")
        base_repo = _sub_object(base, "repo")

        mergeable = data.get("mergeable")
        if mergeable is None:
            mergeability = Mergeability.COMPUTING
        else:
            mergeability = Mergeability.MERGEABLE if bool(mergeable) else Mergeability.NOT_MERGEABLE

        full_name = base_repo.get("full_name") or f"{owner}/{repo}"

        return PRDetail(
            repo_full_name=str(full_name),
            number=int(data.get("number", 0)),
            title=str(data.get("title", "")),
            body=data.get("body"),
            state=str(data.get("state", "")),
            draft=bool(data.get("draft", False)),
            merged=bool(data.get("merged", False)),
            head_sha=str(head.get("sha", "")),
            head_ref=str(head.get("ref", "")),
            base_ref=str(base.get("ref", "")),
            author_login=_user_field(data, "login"),
            author_node_id=_user_field(data, "node_id"),
            author_type=_user_field(data, "type"),
            node_id=data.get("node_id"),
            mergeability=mergeability,
            mergeable_state=data.get("mergeable_state"),
            html_url=str(data.get("html_url", "")),
            created_at=parse_dt(data.get("created_at")),
            updated_at=parse_dt(data.get("updated_at")),
        )

    def _parse_search_item(self, item: dict[str, Any]) -> SearchedPR:
        return SearchedPR(
            repo_full_name=_repo_full_name_from_url(item.get("repository_url")),
            number=int(item.get("number", 0)),
            title=str(item.get("title", "")),
            state=str(item.get("state", "")),
            updated_at=parse_dt(item.get("updated_at")),
            created_at=parse_dt(item.get("created_at")),
            author_login=_user_field(item, "login"),
            author_node_id=_user_field(item, "node_id"),
            author_type=_user_field(item, "type"),
            html_url=str(item.get("html_url", "")),
            node_id=item.get("node_id"),
            draft=bool(item.get("draft", False)),
        )


# --- Module helpers ---------------------------------------------------------


def _parse_issue_comment(data: Mapping[str, Any]) -> IssueComment:
    """One comment object, however it arrived — posted or listed."""
    return IssueComment(
        id=int(data.get("id", 0)),
        body=str(data.get("body", "")),
        user_login=_user_field(data, "login"),
        html_url=data.get("html_url"),
        created_at=parse_dt(data.get("created_at")),
        updated_at=parse_dt(data.get("updated_at")),
    )


def _parse_review(data: Mapping[str, Any]) -> PRPartyReview:
    """One review object, however it arrived — posted or listed.

    ``id`` goes through :func:`to_int` because review ids exceed 32 bits and a
    payload that omitted one must land as ``0`` rather than raise here; the
    reconciler treats a zero id as "no id we can match on".
    """
    return PRPartyReview(
        id=to_int(data.get("id")) or 0,
        state=str(data.get("state", "")),
        body=data.get("body"),
        commit_id=data.get("commit_id"),
        user_login=_user_field(data, "login"),
        user_node_id=_user_field(data, "node_id"),
        submitted_at=parse_dt(data.get("submitted_at")),
        html_url=data.get("html_url"),
    )


def _validate_review_event(event: ReviewEvent | str) -> ReviewEvent:
    if isinstance(event, ReviewEvent):
        return event
    normalized = str(event).strip().upper()
    if normalized == "PENDING":
        raise ValueError(
            "PENDING is not a submittable review event — it creates a draft review "
            "that is never delivered. Use APPROVE, REQUEST_CHANGES, or COMMENT."
        )
    try:
        return ReviewEvent(normalized)
    except ValueError as exc:
        raise ValueError(
            f"Unknown review event {event!r}; expected one of "
            f"{', '.join(e.value for e in ReviewEvent)}."
        ) from exc


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _error_message(response: httpx.Response) -> str:
    """Build a human-readable message from GitHub's error envelope."""
    payload = _safe_json(response)
    if not isinstance(payload, dict):
        return f"GitHub API error {response.status_code}"

    message = str(payload.get("message") or f"GitHub API error {response.status_code}")
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        details = "; ".join(
            str(err.get("message", err)) if isinstance(err, dict) else str(err) for err in errors
        )
        if details:
            message = f"{message}: {details}"
    return message


def to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _epoch_to_datetime(value: Any) -> datetime | None:
    epoch = to_int(value)
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def generation_client(token: str, **kwargs: Any) -> PRPartyGitHubClient:
    """Read-only client for intake, briefs, and status (KTD13)."""
    return PRPartyGitHubClient(token, PRPartyClientMode.GENERATION, **kwargs)


def actuation_client(token: str, **kwargs: Any) -> PRPartyGitHubClient:
    """Write client bound to one reviewer's PAT (R8, R11)."""
    return PRPartyGitHubClient(token, PRPartyClientMode.ACTUATION, **kwargs)


def default_generation_client() -> PRPartyGitHubClient | None:
    """The shared read-only client (KTD13), or ``None`` if unconfigured.

    The single definition behind intake's sweep and webhook paths, the brief
    worker, and the Q&A thread read: an unset ``PR_PARTY_READONLY_TOKEN`` must
    mean "skip" identically everywhere, which it cannot if each caller decides
    for itself.
    """
    token = settings.pr_party_readonly_token
    return generation_client(token) if token else None
