"""Route-level security and billing tests for semantic search."""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from ontokit.schemas.embeddings import SemanticSearchResponse

PROJECT_ID = "11111111-1111-1111-1111-111111111111"
URL = f"/api/v1/projects/{PROJECT_ID}/search/semantic?q=contract&branch=main"


def test_semantic_search_requires_authentication(client: TestClient) -> None:
    resp = client.get(URL)
    assert resp.status_code in (401, 403)


def test_semantic_search_attributes_provider_call_to_user(
    authed_client: tuple[TestClient, AsyncMock],
) -> None:
    client, _session = authed_client
    search = AsyncMock(return_value=SemanticSearchResponse(results=[], search_mode="semantic"))

    with (
        patch(
            "ontokit.api.routes.semantic_search._verify_access",
            new=AsyncMock(),
        ),
        patch(
            "ontokit.api.routes.semantic_search.require_embedding_query_access",
            new=AsyncMock(return_value="editor"),
        ),
        patch(
            "ontokit.api.routes.semantic_search.EmbeddingService.semantic_search",
            new=search,
        ),
    ):
        resp = client.get(URL)

    assert resp.status_code == 200
    assert search.await_args.kwargs["billing_user_id"] == "test-user-id"
