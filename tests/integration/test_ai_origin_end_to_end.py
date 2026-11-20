"""End-to-end proof of AI origin through the real HTTP application.

PostgreSQL, Redis, Git, the ontology index, the rate limiter, the budget
ledger, and the provider client are real. The LLM provider is replaced at the
HTTP boundary by a local server that speaks the OpenAI chat protocol. Only
authentication and the object-store mirror are substituted.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy import delete, select

from ontokit import main, worker
from ontokit.api.routes import projects
from ontokit.api.utils.redis import close_arq_pool, get_arq_pool
from ontokit.core.auth import CurrentUser, get_current_user, get_current_user_optional
from ontokit.core.config import settings
from ontokit.core.database import async_session_maker, engine
from ontokit.git.bare_repository import BareGitRepositoryService
from ontokit.models.llm_config import ProjectLLMConfig
from ontokit.models.llm_generated_entity import LLMGeneratedEntity
from ontokit.models.project import Project, ProjectMember

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("DATABASE_URL") and os.environ.get("REDIS_URL")),
        reason="DATABASE_URL and REDIS_URL not set",
    ),
]

NAMESPACE = "https://example.org/agreements#"
FOCUS = NAMESPACE + "Contract"
MODEL = "stub-model"
USER = CurrentUser(id="e2e-editor", name="Edith Editor", email="edith@example.test")
ONTOLOGY = f"""\
@prefix ex: <{NAMESPACE}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .

ex:Agreement a owl:Class ; rdfs:label "Agreement"@en .
ex:Contract a owl:Class ; rdfs:subClassOf ex:Agreement ; rdfs:label "Contract"@en ;
    skos:definition "A legally binding agreement between parties."@en .
ex:Treaty a owl:Class ; rdfs:subClassOf ex:Agreement ; rdfs:label "Treaty"@en .
"""
PROPOSALS = [
    {"label": "Lease", "definition": "A contract granting use of property for a term."},
    {"label": "Licence", "definition": "A contract granting permission to use a right."},
    {"label": "Wager", "definition": "A contract that turns on an uncertain event."},
]


Stack = tuple[httpx.AsyncClient, UUID, BareGitRepositoryService, Callable[[], Awaitable[None]]]


class _StubProvider(BaseHTTPRequestHandler):
    """Answer chat completions the way an OpenAI-compatible server does."""

    requests: list[dict[str, object]] = []

    def do_POST(self) -> None:  # noqa: N802 - name set by the standard library
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append({"path": self.path, "body": body})
        payload = json.dumps(
            {
                "id": "chatcmpl-stub",
                "object": "chat.completion",
                "created": 0,
                "model": MODEL,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"suggestions": PROPOSALS}),
                        },
                    }
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 80, "total_tokens": 200},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_arguments: object) -> None:
        pass


@pytest.fixture
def provider_url() -> Iterator[str]:
    _StubProvider.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubProvider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _MemoryStorage:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def upload_file(self, path: str, content: bytes, _content_type: str) -> str:
        self.files[path] = content
        return path


@pytest_asyncio.fixture
async def stack(
    tmp_path: Path, provider_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Stack]:
    """A project on the real services, with its history baseline and index built."""
    await engine.dispose()  # Drop connections another test's event loop opened.
    monkeypatch.setattr(settings, "git_repos_base_path", str(tmp_path))
    # The operator allowlist is the supported way to reach a provider on a private address.
    monkeypatch.setenv("ONTOKIT_PRIVATE_LLM_ORIGINS", provider_url.removesuffix("/v1"))
    rate_limit_redis = aioredis.from_url(os.environ["REDIS_URL"])
    monkeypatch.setattr(main, "redis_pool", rate_limit_redis)

    project_id = uuid4()
    git = BareGitRepositoryService()
    git.initialize_repository(project_id, ONTOLOGY.encode(), "ontology.ttl")
    async with async_session_maker() as db:
        project = Project(
            id=project_id,
            name="Agreements",
            owner_id="e2e-owner",
            source_file_path="ontology.ttl",
            ontology_iri=NAMESPACE,
        )
        project.members.append(ProjectMember(user_id=USER.id, role="editor"))
        db.add_all(
            [
                project,
                ProjectLLMConfig(
                    project_id=project_id, provider="ollama", model=MODEL, base_url=provider_url
                ),
            ]
        )
        await db.commit()

    async def current_user() -> CurrentUser:
        return USER

    overrides = main.app.dependency_overrides
    overrides[get_current_user] = current_user
    overrides[get_current_user_optional] = current_user
    overrides[projects.get_storage] = _MemoryStorage
    try:
        # What the worker does after project creation queues its jobs.
        context = {"session_factory": async_session_maker, "redis": await get_arq_pool()}
        baseline = await worker.reconcile_change_history_task(context, str(project_id), "main")
        assert baseline["recorded"] == 3

        async def reindex() -> None:
            async with async_session_maker() as db:
                indexed = await worker.run_ontology_index_task(
                    {**context, "db": db}, str(project_id), "main"
                )
            assert indexed["status"] == "completed"

        await reindex()
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
            yield client, project_id, git, reindex
    finally:
        for dependency in (get_current_user, get_current_user_optional, projects.get_storage):
            overrides.pop(dependency, None)
        async with async_session_maker() as db:
            await db.execute(delete(Project).where(Project.id == project_id))
            await db.commit()
        await rate_limit_redis.aclose()
        await close_arq_pool()
        await engine.dispose()


def _accepted_class(suggestion: dict[str, object], *, label: str | None = None) -> str:
    """Render a proposal the way the editor adds an accepted class to its draft."""
    return (
        f"<{suggestion['iri']}> a owl:Class ; rdfs:subClassOf ex:Contract ;\n"
        f"    rdfs:label {json.dumps(label or suggestion['label'])}@en ;\n"
        f"    skos:definition {json.dumps(suggestion['definition'])}@en .\n"
    )


@pytest.mark.asyncio
async def test_accepted_edited_and_rejected_suggestions_reach_the_changes_feed(
    stack: Stack,
) -> None:
    client, project_id, git, reindex = stack
    api = f"/api/v1/projects/{project_id}"

    generated = await client.post(
        f"{api}/llm/generate-suggestions",
        json={
            "class_iri": FOCUS,
            "branch": "main",
            "suggestion_type": "children",
            "batch_size": 3,
        },
    )
    assert generated.status_code == 200, generated.text
    batch_id = generated.json()["batch_id"]
    unchanged, edited, rejected = generated.json()["suggestions"]
    assert [item["label"] for item in (unchanged, edited, rejected)] == [
        proposal["label"] for proposal in PROPOSALS
    ]
    for item in (unchanged, edited, rejected):
        assert item["iri"].startswith(NAMESPACE) and item["model"] == MODEL

    # The provider was called once, over HTTP, with the class's grounding.
    (call,) = _StubProvider.requests
    assert call["path"] == "/v1/chat/completions"
    prompt = json.dumps(call["body"])
    for grounding in (
        "Ontology class: Contract",
        "Definition: A legally binding agreement between parties.",
        "Parent classes: Agreement",
        "Sibling classes (for context): Treaty",
    ):
        assert grounding in prompt

    # The ledger is durable by the time the response arrives.
    async with async_session_maker() as db:
        ledger = (
            await db.scalars(
                select(LLMGeneratedEntity).where(LLMGeneratedEntity.batch_id == UUID(batch_id))
            )
        ).all()
    assert {row.entity_iri for row in ledger} == {
        item["iri"] for item in (unchanged, edited, rejected)
    }

    # Accept one as proposed, accept one after editing its label, reject the third.
    saved = await client.put(
        f"{api}/source",
        json={
            "content": ONTOLOGY
            + _accepted_class(unchanged)
            + _accepted_class(edited, label="Software licence"),
            "commit_message": "Accept suggested subclasses of Contract",
            "base_revision": git.get_repository(project_id).get_branch_commit_hash("main"),
        },
    )
    assert saved.status_code == 200, saved.text

    feed = await client.get(f"{api}/analytics/changes", params={"days": 1})
    assert feed.status_code == 200, feed.text
    changes = feed.json()
    items = {item["entity_iri"]: item for item in changes["items"]}

    assert rejected["iri"] not in items
    assert items[unchanged["iri"]]["origins"] == ["ai"]
    assert items[edited["iri"]]["origins"] == ["ai_edited"]
    for suggestion, origin in ((unchanged, "ai"), (edited, "ai_edited")):
        item = items[suggestion["iri"]]
        assert (item["net_change"], item["entity_type"]) == ("added", "class")
        assert item["authors"] == [{"user_id": USER.id, "user_name": USER.name}]
        (event,) = item["events"]
        assert (event["origin"], event["origin_model"], event["origin_batch_id"]) == (
            origin,
            MODEL,
            batch_id,
        )
        assert event["commit_hash"] == saved.json()["commit_hash"]
    assert items[unchanged["iri"]]["label"] == "Lease"
    assert items[edited["iri"]]["label"] == "Software licence"
    assert {"field": "labels", "before": [], "after": ["Software licence"]} in (
        items[edited["iri"]]["field_changes"]
    )
    # The three baseline classes came from history reconciliation, not a person or the LLM.
    assert changes["summary"]["by_origin"] == {"human": 0, "ai": 1, "ai_edited": 1, "system": 3}
    assert changes["summary"]["by_net_change"]["added"] == 5

    only_ai = await client.get(f"{api}/analytics/changes", params={"days": 1, "origin": "ai"})
    assert [item["entity_iri"] for item in only_ai.json()["items"]] == [unchanged["iri"]]

    markers = await client.get(f"{api}/analytics/changed-entities", params={"days": 1})
    assert markers.status_code == 200, markers.text
    marked = {item["entity_iri"]: item["origins"] for item in markers.json()["items"]}
    assert marked[unchanged["iri"]] == ["ai"] and marked[edited["iri"]] == ["ai_edited"]
    assert rejected["iri"] not in marked

    # Asked again once the index reflects the save, the provider repeats itself.
    # No embedding index exists, yet the label that now names a subclass is caught.
    await reindex()
    repeated = await client.post(
        f"{api}/llm/generate-suggestions",
        json={"class_iri": FOCUS, "branch": "main", "suggestion_type": "children", "batch_size": 3},
    )
    assert repeated.status_code == 200, repeated.text
    again = {item["label"]: item for item in repeated.json()["suggestions"]}
    assert again["Lease"]["duplicate_verdict"] == "block"
    assert again["Lease"]["duplicate_candidates"] == [
        {"iri": unchanged["iri"], "label": "Lease", "score": 1.0}
    ]
    assert again["Licence"]["duplicate_verdict"] == again["Wager"]["duplicate_verdict"] == "pass"
