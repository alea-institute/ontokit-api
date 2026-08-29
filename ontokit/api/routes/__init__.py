"""API v1 routes."""

from fastapi import APIRouter

from ontokit.api.routes import (
    analytics,
    anonymous_suggestions,
    auth,
    classes,
    duplicate_check,
    embeddings,
    generation,
    join_requests,
    lint,
    normalization,
    notifications,
    ontologies,
    pr_party,
    pr_party_settings,
    pr_party_webhooks,
    projects,
    properties,
    pull_requests,
    quality,
    remote_sync,
    search,
    semantic_search,
    suggestions,
    translation,
    trust,
    user_settings,
)
from ontokit.api.routes import (
    llm as llm_routes,
)
from ontokit.core.api_paths import PROJECTS_PREFIX
from ontokit.core.config import settings

router = APIRouter()


def include_pr_party_routes(
    target: APIRouter,
    auth_mode: str | None = None,
    reviewers: str | None = None,
) -> bool:
    """Mount PR Party only with authentication and a reviewer registry (KTD19).

    Every PR Party route binds to a *named* reviewer: the registry is keyed by
    Zitadel user id, and the write PAT it stores acts on GitHub as that person.
    With ``AUTH_MODE=disabled`` every caller is the same anonymous principal, so
    mounting these routes would let anyone read a reviewer's settings and rotate
    their credential. There is no safe degraded behavior — the honest answer is
    a 404, so the router is not mounted at all.

    The org webhook receiver rides the same gate even though it authenticates by
    HMAC rather than by session: with PR Party unmounted there is no queue for a
    delivery to land in, and leaving the write surface up as the feature's only
    live endpoint would be strictly worse than a 404.

    Returns whether it mounted, so the gate is testable without an app rebuild.
    """
    if not settings.is_pr_party_enabled(auth_mode=auth_mode, reviewers=reviewers):
        return False
    target.include_router(pr_party_settings.router, prefix="/pr-party", tags=["PR Party"])
    target.include_router(pr_party_webhooks.router, prefix="/pr-party", tags=["PR Party"])
    # Queue/card reads (U15) ride the same gate: with AUTH_MODE=disabled there
    # is no caller to project own-vs-counterpart against, and the cards are
    # private-repo content, so a 404 is the only honest degraded behavior.
    target.include_router(pr_party.router, prefix="/pr-party", tags=["PR Party"])
    return True


router.include_router(auth.router, prefix="/auth", tags=["Authentication"])
router.include_router(projects.router, prefix="/projects", tags=["Projects"])
router.include_router(pull_requests.router, prefix="/projects", tags=["Pull Requests"])
router.include_router(join_requests.router, prefix="/projects", tags=["Join Requests"])
router.include_router(lint.router, prefix="/projects", tags=["Lint"])
router.include_router(normalization.router, prefix="/projects", tags=["Normalization"])
router.include_router(quality.router, prefix="/projects", tags=["Quality"])
router.include_router(analytics.router, prefix="/projects", tags=["Analytics"])
router.include_router(embeddings.router, prefix="/projects", tags=["Embeddings"])
router.include_router(semantic_search.router, prefix="/projects", tags=["Semantic Search"])
router.include_router(ontologies.router, prefix="/ontologies", tags=["Ontologies"])
router.include_router(classes.router, tags=["Classes"])
router.include_router(properties.router, tags=["Properties"])
router.include_router(suggestions.router, prefix="/projects", tags=["Suggestions"])
router.include_router(trust.router, prefix="/projects", tags=["Trust Ladder"])
router.include_router(
    anonymous_suggestions.router,
    prefix=PROJECTS_PREFIX,
    tags=["anonymous-suggestions"],
)
router.include_router(remote_sync.router, prefix="/projects", tags=["Sync from Remote"])
router.include_router(notifications.router, prefix="/notifications", tags=["Notifications"])
router.include_router(search.router, prefix="/search", tags=["Search"])
router.include_router(user_settings.router, prefix="/users", tags=["User Settings"])
# LLM: project-scoped routes under /projects; public catalogue routes at root
router.include_router(llm_routes.router, prefix="/projects", tags=["LLM"])
router.include_router(llm_routes.public_router, tags=["LLM"])
router.include_router(translation.router, prefix="/projects", tags=["Translations"])
router.include_router(translation.public_router, tags=["Translations"])
# Generation: LLM suggestion generation + entity validation (Phase 13)
router.include_router(generation.router, tags=["Generation"])
# Duplicate check: composite scoring endpoint for pre-submission duplicate detection (DEDUP-04)
router.include_router(duplicate_check.router, tags=["duplicate-check"])
# PR Party: org-scoped (not project-scoped), so it mounts at the API root.
include_pr_party_routes(router)
