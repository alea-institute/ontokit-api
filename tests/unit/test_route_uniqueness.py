"""Every mounted method and path pair is registered exactly once."""

import re
from collections import Counter
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI, routing
from fastapi.routing import APIRoute

from ontokit.api.routes import include_pr_party_routes
from ontokit.api.routes import router as api_router
from ontokit.core.api_paths import API_V1_PREFIX
from ontokit.main import app

SAFE_PR_PARTY_REVIEWERS = "zit-1:octocat"


def build_pr_party_app() -> FastAPI:
    """Build an app with the same routes as production plus PR Party mounted.

    PR Party only mounts with authentication and a reviewer registry, so the
    default test app never contains its routes. This mirrors ``ontokit.main``
    mounting (router under the API prefix) and adds the PR Party routers.
    """
    pr_party_app = FastAPI()
    pr_party_app.include_router(api_router, prefix=API_V1_PREFIX)
    extra = APIRouter()
    assert include_pr_party_routes(extra, auth_mode="required", reviewers=SAFE_PR_PARTY_REVIEWERS)
    pr_party_app.include_router(extra, prefix=API_V1_PREFIX)
    return pr_party_app


def api_routes(target: Any) -> list[Any]:
    # Newer FastAPI keeps included routers lazy; the public iterator resolves
    # mounted paths. Older versions expose them directly on ``routes``.
    iterator = getattr(routing, "iter_route_contexts", iter)
    return [
        route
        for route in iterator(target.routes)
        if isinstance(getattr(route, "original_route", route), APIRoute)
    ]


_PATH_PARAM = re.compile(r"\{[^}:]+(:[^}]+)?\}")


def normalize_path(path: str) -> str:
    """Collapse every path parameter (any name, any converter) to ``{}``.

    ``/x/{a}`` and ``/x/{b:path}`` match the same request, so the first
    registration shadows the second even though the raw strings differ.
    """
    return _PATH_PARAM.sub("{}", path)


def duplicate_pairs(routes: list[Any]) -> dict[tuple[str, str], int]:
    counts = Counter(
        (method, normalize_path(route.path)) for route in routes for method in sorted(route.methods)
    )
    return {pair: n for pair, n in counts.items() if n > 1}


def test_default_app_has_no_duplicate_method_path_pairs() -> None:
    routes = api_routes(app)
    assert routes, "The route inventory must not silently become empty"
    assert duplicate_pairs(routes) == {}


def test_pr_party_app_has_no_duplicate_method_path_pairs() -> None:
    routes = api_routes(build_pr_party_app())
    assert any("/pr-party/" in route.path for route in routes), "PR Party must be mounted"
    assert duplicate_pairs(routes) == {}


def test_duplicate_detection_flags_a_double_registration() -> None:
    probe = FastAPI()
    router = APIRouter()

    @router.post("/things")
    async def first() -> None: ...

    probe.include_router(router)

    @probe.post("/things")
    async def second() -> None: ...

    assert duplicate_pairs(api_routes(probe)) == {("POST", "/things"): 2}


def test_duplicate_detection_flags_differently_named_parameters() -> None:
    probe = FastAPI()

    @probe.get("/x/{a}")
    async def first(a: str) -> None: ...

    @probe.get("/x/{b:path}")
    async def second(b: str) -> None: ...

    assert duplicate_pairs(api_routes(probe)) == {("GET", "/x/{}"): 2}


def test_distinct_literal_segments_are_not_duplicates() -> None:
    probe = FastAPI()

    @probe.get("/x/{a}/one")
    async def first(a: str) -> None: ...

    @probe.get("/x/{b}/two")
    async def second(b: str) -> None: ...

    assert duplicate_pairs(api_routes(probe)) == {}


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_branch_collection_route_is_registered_once(method: str) -> None:
    pair = (method, f"{API_V1_PREFIX}/projects/{{project_id}}/branches")
    routes = [r for r in api_routes(app) if (pair[1] == r.path and method in r.methods)]
    assert len(routes) == 1
    assert routes[0].endpoint.__module__ == "ontokit.api.routes.projects"


def test_branch_checkout_route_is_registered_once() -> None:
    routes = [
        r
        for r in api_routes(app)
        if r.path.startswith(f"{API_V1_PREFIX}/projects/{{project_id}}/branches/")
        and r.path.endswith("/checkout")
        and "POST" in r.methods
    ]
    assert len(routes) == 1
    assert routes[0].endpoint.__module__ == "ontokit.api.routes.projects"
