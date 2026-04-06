"""GitHub Models provider — extends OpenAICompatProvider with GitHub catalog listing.

Ported from folio-enrich/backend/app/services/llm/github_models_provider.py.

GitHub Models uses an Azure OpenAI-compatible endpoint at models.github.ai/inference,
so we get the chat() and test_connection() implementations for free from
OpenAICompatProvider. This subclass only overrides list_models() to pull from the
GitHub Models catalog API.
"""

from __future__ import annotations

import logging

import httpx

from ontokit.services.llm.openai_compat import OpenAICompatProvider

logger = logging.getLogger(__name__)

_GITHUB_CATALOG_URL = "https://models.github.ai/catalog/models"


class GitHubModelsProvider(OpenAICompatProvider):
    """GitHub Models — extends OpenAI-compatible with GitHub catalog model listing."""

    async def list_models(self) -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    _GITHUB_CATALOG_URL,
                    headers={
                        "Authorization": f"Bearer {self.api_key or ''}",
                        "Accept": "application/json",
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            items = (
                data
                if isinstance(data, list)
                else data.get("models", data.get("value", []))
            )
            model_ids = []
            for m in items:
                model_id = m.get("id") or m.get("name", "")
                if model_id:
                    model_ids.append(model_id)
            return sorted(model_ids) if model_ids else await super().list_models()
        except Exception:
            logger.debug(
                "Failed to list GitHub Models from catalog; falling back to OpenAI compat",
                exc_info=True,
            )
            return await super().list_models()
