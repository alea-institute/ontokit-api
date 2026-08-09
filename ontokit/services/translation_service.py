"""Provider-agnostic translation generation and machine verification."""

from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
from enum import StrEnum

from rdflib import Graph, Literal, URIRef
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.core.constants import ONTOKIT_COMMITTER_EMAIL, ONTOKIT_COMMITTER_NAME
from ontokit.git.bare_repository import BareGitRepositoryService, CommitInfo
from ontokit.models.llm_config import LLMAuditLog, ProjectLLMConfig
from ontokit.models.translation import (
    ProjectTranslationConfig,
    TranslationRecord,
    hash_literal_value,
)
from ontokit.services.branch_lock import branch_write_lock
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
from ontokit.services.translation_annotations import (
    TranslationAnnotation,
    annotate,
    translation_record_digest,
)

logger = logging.getLogger(__name__)

ProviderFactory = Callable[..., LLMProvider]
BudgetChecker = Callable[
    [AsyncSession, uuid.UUID, ProjectLLMConfig], Awaitable[tuple[bool, str | None]]
]
AuditLogger = Callable[..., Awaitable[LLMAuditLog]]
PricingResolver = Callable[[str], Awaitable[tuple[float, float]]]
IndexEnqueuer = Callable[..., Awaitable[None]]


async def _enqueue_ontology_index(*, project_id: uuid.UUID, branch: str, commit_hash: str) -> None:
    """Queue one rebuild per pending/in-flight project branch via ARQ job identity."""
    from ontokit.api.utils.redis import get_arq_pool

    pool = await get_arq_pool()
    if pool is None:
        return
    await pool.enqueue_job(
        "run_ontology_index_task",
        str(project_id),
        branch,
        commit_hash,
        _job_id=f"ontology-index:{project_id}:{branch}",
    )


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


@dataclass(frozen=True, slots=True)
class TranslationCommitOutcome:
    """Result of persisting and, when eligible, applying translation results."""

    commit: CommitInfo | None
    committed_languages: tuple[str, ...] = ()
    discarded_languages: tuple[str, ...] = ()


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
        git_service: BareGitRepositoryService | None = None,
        index_enqueuer: IndexEnqueuer | None = None,
    ) -> None:
        self._db = db
        self._translation_config = translation_config
        self._llm_config = llm_config
        self._user_id = user_id
        self._provider_factory = provider_factory
        self._budget_checker = budget_checker
        self._audit_logger = audit_logger
        self._pricing_resolver = pricing_resolver
        self._git_service = git_service
        self._index_enqueuer = index_enqueuer or _enqueue_ontology_index

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

    @property
    def project_id(self) -> uuid.UUID:
        """Return the configured project identifier."""
        return self._translation_config.project_id

    async def apply_results(
        self,
        *,
        branch: str,
        filename: str,
        entity_iri: str,
        predicate: str,
        source_value: str,
        source_language: str,
        source_value_hash: str,
        results: dict[str, TranslationResult],
        model_version: str,
    ) -> TranslationCommitOutcome:
        """Persist results and coalesce eligible verified literals into one branch commit."""
        if self._git_service is None:
            raise RuntimeError("translation commit stage requires a git service")

        now = datetime.now(UTC)
        records: dict[str, TranslationRecord] = {}
        for language, result in results.items():
            if not result.succeeded or result.proposed_value is None:
                continue
            record = TranslationRecord(
                id=uuid.uuid4(),
                project_id=self.project_id,
                entity_iri=entity_iri,
                predicate=predicate,
                language=language,
                source_value=source_value,
                proposed_value=result.proposed_value,
                source_value_hash=source_value_hash,
                translated_value_hash=hash_literal_value(result.proposed_value),
                model_name=self._primary.model,
                model_version=model_version,
                method=result.method,
                score=result.score,
                state="verified" if result.accepted else "provisional",
                created_at=now,
            )
            self._db.add(record)
            records[language] = record

        verified = {
            language: result
            for language, result in results.items()
            if result.accepted and result.succeeded and result.proposed_value is not None
        }
        if not verified:
            await self._db.commit()
            return TranslationCommitOutcome(commit=None)

        discarded: list[str] = []
        committed: list[str] = []
        commit: CommitInfo | None = None
        async with branch_write_lock(self.project_id, branch):
            content = self._git_service.get_file_from_branch(self.project_id, branch, filename)
            graph = Graph().parse(data=content, format="turtle")
            subject = URIRef(entity_iri)
            property_iri = URIRef(predicate)
            expected_source = Literal(source_value, lang=source_language)
            source_is_current = (
                hash_literal_value(source_value) == source_value_hash
                and (subject, property_iri, expected_source) in graph
            )
            if not source_is_current:
                discarded.extend(verified)
            else:
                additions = Graph()
                for language in sorted(verified):
                    result = verified[language]
                    literal = Literal(result.proposed_value, lang=language)
                    if any(
                        isinstance(existing, Literal) and existing.language == language
                        for existing in graph.objects(subject, property_iri)
                    ):
                        discarded.append(language)
                        continue
                    record = records[language]
                    graph.add((subject, property_iri, literal))
                    additions.add((subject, property_iri, literal))
                    meta = TranslationAnnotation(
                        method=record.method,
                        state=record.state,
                        created=record.created_at,
                        record_digest=translation_record_digest(record),
                    )
                    annotate(graph, subject, property_iri, literal, meta)
                    annotate(additions, subject, property_iri, literal, meta)
                    committed.append(language)

                if committed:
                    serialized = additions.serialize(format="nt")
                    delta = "\n".join(sorted(line for line in serialized.splitlines() if line))
                    updated = content.rstrip() + b"\n\n" + delta.encode("utf-8") + b"\n"
                    languages = ", ".join(committed)
                    commit = self._git_service.commit_changes(
                        project_id=self.project_id,
                        ontology_content=updated,
                        filename=filename,
                        message=f"Add verified {languages} translations ({records[committed[0]].method})",
                        author_name=f"OntoKit Translation Engine ({self._primary.model})",
                        author_email="translation-engine@ontokit.dev",
                        branch_name=branch,
                        committer_name=ONTOKIT_COMMITTER_NAME,
                        committer_email=ONTOKIT_COMMITTER_EMAIL,
                    )

        for language in discarded:
            records[language].state = "rejected"
        await self._db.commit()
        if commit is not None:
            try:
                await self._index_enqueuer(
                    project_id=self.project_id, branch=branch, commit_hash=commit.hash
                )
            except Exception:
                logger.warning("Failed to queue translation ontology re-index", exc_info=True)
        return TranslationCommitOutcome(
            commit=commit,
            committed_languages=tuple(committed),
            discarded_languages=tuple(sorted(discarded)),
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


__all__ = [
    "TranslationCommitOutcome",
    "TranslationErrorCode",
    "TranslationResult",
    "TranslationService",
]
