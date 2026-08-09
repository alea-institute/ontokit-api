"""Tests for per-project translation configuration and language palette."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from ontokit.api.routes.translation import get_translation_config, update_translation_config
from ontokit.core.auth import CurrentUser
from ontokit.main import app
from ontokit.models.translation import ProjectTranslationConfig
from ontokit.schemas.translation import TranslationConfigUpdate
from ontokit.services.language_palette import LANGUAGE_PALETTE

PROJECT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")


def _result(value: object) -> Mock:
    result = Mock()
    result.scalar_one_or_none.return_value = value
    return result


@pytest.mark.asyncio
async def test_non_admin_cannot_update_translation_config(mock_db_session: AsyncMock) -> None:
    mock_db_session.execute.return_value = _result(Mock(role="editor"))

    with pytest.raises(HTTPException) as exc:
        await update_translation_config(
            PROJECT_ID,
            TranslationConfigUpdate(language_tags=["es"]),
            mock_db_session,
            CurrentUser(id="editor"),
        )

    assert exc.value.status_code == 403
    mock_db_session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_update_persists_and_never_echoes_key(
    mock_db_session: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_db_session.execute.side_effect = [_result(Mock(role="admin")), _result(None)]
    monkeypatch.setattr(
        "ontokit.api.routes.translation.encrypt_secret",
        lambda plaintext: f"encrypted:{plaintext}",
    )

    response = await update_translation_config(
        PROJECT_ID,
        TranslationConfigUpdate(
            language_tags=["es", "fr"],
            verification_mechanism="confidence",
            confidence_threshold=0.91,
            translate_definitions=True,
            speed_mode="fast",
            provisional_gate=True,
            primary_provider="openai",
            primary_model="gpt-5.1",
            verifier_provider="anthropic",
            verifier_model="claude-sonnet-4-5",
            verifier_api_key="secret-value",
        ),
        mock_db_session,
        CurrentUser(id="admin"),
    )

    stored = mock_db_session.add.call_args.args[0]
    assert isinstance(stored, ProjectTranslationConfig)
    assert stored.primary_provider == "openai"
    assert stored.primary_model == "gpt-5.1"
    assert stored.verifier_api_key_encrypted == "encrypted:secret-value"
    assert response.language_tags == ["es", "fr"]
    assert response.primary_provider == "openai"
    assert response.primary_model == "gpt-5.1"
    assert response.verifier_api_key_set is True
    assert "key" not in response.model_dump_json().lower().replace("api_key_set", "")
    assert "secret-value" not in response.model_dump_json()
    mock_db_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_without_row_returns_defaults(mock_db_session: AsyncMock) -> None:
    mock_db_session.execute.side_effect = [_result(Mock(role="viewer")), _result(None)]

    response = await get_translation_config(PROJECT_ID, mock_db_session, CurrentUser(id="viewer"))

    assert response.language_tags == []
    assert response.verification_mechanism == "consensus"
    assert response.speed_mode == "batch"
    assert response.provisional_gate is False
    assert response.translate_definitions is False
    assert response.translate_examples is False
    assert response.primary_provider is None
    assert response.primary_model is None


@pytest.mark.asyncio
async def test_admin_can_clear_primary_translation_provider_and_model(
    mock_db_session: AsyncMock,
) -> None:
    config = ProjectTranslationConfig(
        project_id=PROJECT_ID,
        language_tags=[],
        verification_mechanism="consensus",
        consensus_threshold=0.85,
        confidence_threshold=0.80,
        translate_definitions=False,
        translate_examples=False,
        speed_mode="batch",
        provisional_gate=False,
        primary_provider="openai",
        primary_model="gpt-5.1",
    )
    mock_db_session.execute.side_effect = [_result(Mock(role="admin")), _result(config)]

    response = await update_translation_config(
        PROJECT_ID,
        TranslationConfigUpdate(primary_provider=None, primary_model=None),
        mock_db_session,
        CurrentUser(id="admin"),
    )

    assert config.primary_provider is None
    assert config.primary_model is None
    assert response.primary_provider is None
    assert response.primary_model is None


@pytest.mark.parametrize("tag", ["", " ", "not_a_tag", "x" * 36])
def test_invalid_language_tags_are_rejected(tag: str) -> None:
    with pytest.raises(ValidationError):
        TranslationConfigUpdate(language_tags=[tag])


def test_duplicate_language_tags_are_deduplicated_case_insensitively() -> None:
    update = TranslationConfigUpdate(language_tags=["pt-BR", "PT-br", "es"])
    assert update.language_tags == ["pt-BR", "es"]


def test_palette_contains_required_languages() -> None:
    tags = {entry.tag for entry in LANGUAGE_PALETTE}
    assert len(LANGUAGE_PALETTE) >= 30
    assert {
        "en",
        "zh",
        "hi",
        "es",
        "ar",
        "fr",
        "bn",
        "pt",
        "ru",
        "ur",
        "id",
        "de",
        "ja",
        "sw",
        "tl",
        "it",
        "ko",
        "vi",
        "pl",
        "la",
    } <= tags


def test_openapi_never_exposes_encrypted_or_response_key_fields() -> None:
    schema = app.openapi()
    response_schema = schema["components"]["schemas"]["TranslationConfigResponse"]
    assert "verifier_api_key" not in response_schema["properties"]
    assert "verifier_api_key_encrypted" not in response_schema["properties"]
    assert "verifier_api_key_set" in response_schema["properties"]
    assert (
        "verifier_api_key"
        in schema["components"]["schemas"]["TranslationConfigUpdate"]["properties"]
    )
