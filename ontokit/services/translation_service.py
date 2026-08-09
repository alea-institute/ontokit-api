"""Provider-agnostic translation generation and machine verification."""

from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.models.llm_config import LLMAuditLog, ProjectLLMConfig
from ontokit.models.translation import ProjectTranslationConfig
from ontokit.services.llm.audit import log_llm_call
from ontokit.services.llm.base import LLMProvider
from ontokit.services.llm.budget import check_budget
from ontokit.services.llm.crypto import decrypt_secret
from ontokit.services.llm.pricing import get_model_pricing
from ontokit.services.llm.prompts.translation import (
    build_back_translate_messages,
    build_translate_messages,
    build_verify_messages,
    parse_translation_response,
    parse_verification_response,
)
from ontokit.services.llm.registry import get_provider

logger = logging.getLogger(__name__)

ProviderFactory = Callable[..., LLMProvider]
BudgetChecker = Callable[
    [AsyncSession, uuid.UUID, ProjectLLMConfig], Awaitable[tuple[bool, str | None]]
]
AuditLogger = Callable[..., Awaitable[LLMAuditLog]]
PricingResolver = Callable[[str], Awaitable[tuple[float, float]]]


class TranslationErrorCode(StrEnum):
    configuration_error = "configuration_error"
    budget_exhausted = "budget_exhausted"
    provider_error = "provider_error"
    invalid_response = "invalid_response"
    metering_error = "metering_error"


@dataclass(frozen=True, slots=True)
class TranslationResult:
    language: str
    proposed_value: str | None
    score: float
    method: str
    method_inputs: dict[str, float] = field(default_factory=dict)
    threshold: float = 1.0
    succeeded: bool = True
    accepted: bool = False
    error_code: TranslationErrorCode | None = None
    error_message: str | None = None


class _TranslationCallError(RuntimeError):
    def __init__(self, code: TranslationErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class _ResolvedProvider:
    name: str
    model: str
    api_key: str | None
    base_url: str | None


def _similarity(left: str, right: str) -> float:
    """Return deterministic Unicode-aware lexical similarity on the [0, 1] range."""

    def normalize(value: str) -> str:
        value = unicodedata.normalize("NFKC", value).casefold()
        return " ".join(re.findall(r"\w+", value, flags=re.UNICODE))

    normalized_left = normalize(left)
    normalized_right = normalize(right)
    if not normalized_left or not normalized_right:
        return 0.0
    return round(SequenceMatcher(None, normalized_left, normalized_right).ratio(), 6)


class TranslationService:
    """Generate deterministic per-language translation results above LLM providers."""

    def __init__(
        self,
        db: AsyncSession,
        translation_config: ProjectTranslationConfig,
        llm_config: ProjectLLMConfig,
        user_id: str,
        *,
        provider_factory: ProviderFactory = get_provider,
        decrypt_key: Callable[[str], str] = decrypt_secret,
        budget_checker: BudgetChecker = check_budget,
        audit_logger: AuditLogger = log_llm_call,
        pricing_resolver: PricingResolver = get_model_pricing,
    ) -> None:
        self._db = db
        self._translation_config = translation_config
        self._llm_config = llm_config
        self._user_id = user_id
        self._provider_factory = provider_factory
        self._budget_checker = budget_checker
        self._audit_logger = audit_logger
        self._pricing_resolver = pricing_resolver

        primary_model = translation_config.primary_model
        if not primary_model:
            raise ValueError("translation primary_model must be configured")
        primary_key = (
            decrypt_key(llm_config.api_key_encrypted) if llm_config.api_key_encrypted else None
        )
        primary_name = translation_config.primary_provider or llm_config.provider
        self._primary = _ResolvedProvider(
            primary_name, primary_model, primary_key, llm_config.base_url
        )
        verifier_key = (
            decrypt_key(translation_config.verifier_api_key_encrypted)
            if translation_config.verifier_api_key_encrypted
            else primary_key
        )
        self._verifier = _ResolvedProvider(
            translation_config.verifier_provider or primary_name,
            translation_config.verifier_model or primary_model,
            verifier_key,
            llm_config.base_url
            if (translation_config.verifier_provider or primary_name) == llm_config.provider
            else None,
        )

    async def translate(
        self,
        source_literal: str,
        source_language: str,
        target_languages: Sequence[str] | None = None,
        *,
        context_labels: Sequence[str] = (),
    ) -> dict[str, TranslationResult]:
        """Translate every requested language; one failure never aborts its siblings."""
        languages = list(target_languages or self._translation_config.language_tags)
        return {
            language: await self._translate_one(
                source_literal, source_language, language, list(context_labels)
            )
            for language in languages
        }

    async def _translate_one(
        self, source: str, source_language: str, target_language: str, context: list[str]
    ) -> TranslationResult:
        method = self._translation_config.verification_mechanism
        threshold = (
            self._translation_config.consensus_threshold
            if method == "consensus"
            else self._translation_config.confidence_threshold
        )
        proposed: str | None = None
        try:
            if method == "consensus":
                proposed, inputs = await self._consensus(
                    source, source_language, target_language, context
                )
                score = round(
                    0.40 * inputs["candidate_agreement"]
                    + 0.35 * inputs["back_translation_similarity"]
                    + 0.25 * inputs["verifier_agreement"],
                    6,
                )
            elif method == "confidence":
                proposed, inputs = await self._confidence(
                    source, source_language, target_language, context
                )
                score = round(
                    0.60 * inputs["self_reported_confidence"]
                    + 0.40 * inputs["back_translation_similarity"],
                    6,
                )
            else:
                raise _TranslationCallError(
                    TranslationErrorCode.configuration_error,
                    "unsupported translation verification mechanism",
                )
        except _TranslationCallError as exc:
            return TranslationResult(
                language=target_language,
                proposed_value=proposed,
                score=0.0,
                method=method,
                threshold=threshold,
                succeeded=False,
                accepted=False,
                error_code=exc.code,
                error_message=str(exc),
            )
        return TranslationResult(
            language=target_language,
            proposed_value=proposed,
            score=score,
            method=method,
            method_inputs=inputs,
            threshold=threshold,
            accepted=score >= threshold,
        )

    async def _consensus(
        self, source: str, source_language: str, target: str, context: list[str]
    ) -> tuple[str, dict[str, float]]:
        primary_text = await self._call(
            self._primary,
            build_translate_messages(source, source_language, target, context),
            "translation/translate",
        )
        proposed, _ = self._parse_translation(primary_text)
        verifier_text = await self._call(
            self._verifier,
            build_translate_messages(source, source_language, target, context),
            "translation/independent-translate",
        )
        verifier_candidate, _ = self._parse_translation(verifier_text)
        back_text = await self._call(
            self._verifier,
            build_back_translate_messages(proposed, source_language, target),
            "translation/back-translate",
        )
        back_translation, _ = self._parse_translation(back_text)
        verify_text = await self._call(
            self._verifier,
            build_verify_messages(
                source,
                source_language,
                target,
                proposed,
                verifier_candidate,
                back_translation,
            ),
            "translation/verify",
        )
        try:
            verifier_agreement = parse_verification_response(verify_text)
        except ValueError as exc:
            raise _TranslationCallError(TranslationErrorCode.invalid_response, str(exc)) from exc
        return proposed, {
            "candidate_agreement": _similarity(proposed, verifier_candidate),
            "back_translation_similarity": _similarity(source, back_translation),
            "verifier_agreement": verifier_agreement,
        }

    async def _confidence(
        self, source: str, source_language: str, target: str, context: list[str]
    ) -> tuple[str, dict[str, float]]:
        primary_text = await self._call(
            self._primary,
            build_translate_messages(
                source, source_language, target, context, include_confidence=True
            ),
            "translation/translate-confidence",
        )
        proposed, confidence = self._parse_translation(primary_text, require_confidence=True)
        assert confidence is not None
        back_text = await self._call(
            self._verifier,
            build_back_translate_messages(proposed, source_language, target),
            "translation/back-translate",
        )
        back_translation, _ = self._parse_translation(back_text)
        return proposed, {
            "self_reported_confidence": confidence,
            "back_translation_similarity": _similarity(source, back_translation),
        }

    @staticmethod
    def _parse_translation(
        text: str, *, require_confidence: bool = False
    ) -> tuple[str, float | None]:
        try:
            return parse_translation_response(text, require_confidence=require_confidence)
        except ValueError as exc:
            raise _TranslationCallError(TranslationErrorCode.invalid_response, str(exc)) from exc

    async def _call(
        self, resolved: _ResolvedProvider, messages: list[dict[str, str]], endpoint: str
    ) -> str:
        allowed, reason = await self._budget_checker(
            self._db, self._translation_config.project_id, self._llm_config
        )
        if not allowed:
            raise _TranslationCallError(
                TranslationErrorCode.budget_exhausted, reason or "budget exhausted"
            )
        try:
            input_price, output_price = await self._pricing_resolver(resolved.model)
        except Exception as exc:
            raise _TranslationCallError(
                TranslationErrorCode.metering_error, "pricing unavailable before provider call"
            ) from exc

        provider = self._provider_factory(
            resolved.name,
            api_key=resolved.api_key,
            base_url=resolved.base_url,
            model=resolved.model,
        )
        input_tokens = output_tokens = 0
        provider_error: Exception | None = None
        text = ""
        try:
            text, input_tokens, output_tokens = await provider.chat(messages)
        except Exception as exc:
            provider_error = exc

        cost = input_tokens * input_price + output_tokens * output_price
        try:
            await self._audit_logger(
                db=self._db,
                project_id=str(self._translation_config.project_id),
                user_id=self._user_id,
                model=resolved.model,
                provider=resolved.name,
                endpoint=endpoint,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_estimate_usd=cost,
                is_byo_key=False,
            )
        except Exception as exc:
            raise _TranslationCallError(
                TranslationErrorCode.metering_error, "provider call could not be audited"
            ) from exc
        if provider_error is not None:
            logger.warning(
                "Translation provider call failed (provider=%s model=%s endpoint=%s)",
                resolved.name,
                resolved.model,
                endpoint,
            )
            raise _TranslationCallError(
                TranslationErrorCode.provider_error, "translation provider call failed"
            ) from provider_error
        return text


__all__ = ["TranslationErrorCode", "TranslationResult", "TranslationService"]
