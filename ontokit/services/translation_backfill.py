"""Side-effect-free scope selection and per-call backfill cost estimation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from rdflib import Literal, URIRef
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.git import GitRepositoryService
from ontokit.models.translation import TranslationRecord, hash_literal_value
from ontokit.services.llm.base import estimate_message_tokens
from ontokit.services.llm.pricing import get_model_pricing
from ontokit.services.llm.prompts.translation import (
    build_back_translate_messages,
    build_translate_messages,
    build_verify_messages,
)
from ontokit.services.translation_annotations import read_annotation, translation_record_digest
from ontokit.services.translation_coverage import TranslationCoverageService

# Neither current provider adapter submits to a true asynchronous batch endpoint yet.
TRUE_BATCH_PROVIDERS: frozenset[str] = frozenset()
EXPECTED_OUTPUT_TOKENS = 64
OUTPUT_TOKEN_CEILING = 256
PricingResolver = Callable[[str], Awaitable[tuple[float, float]]]


@dataclass(frozen=True, slots=True)
class BackfillLiteral:
    entity_iri: str
    predicate: str
    source_value: str
    source_language: str
    target_language: str
    context_labels: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BackfillCostPreview:
    literal_count: int
    expected_cost_usd: float
    upper_bound_cost_usd: float
    batch_discount_applied: bool


async def select_backfill_literals(
    db: AsyncSession,
    git: GitRepositoryService,
    project_id: UUID,
    branch: str,
    *,
    language: str | None = None,
    era_before: datetime | None = None,
    never_confirmed: bool | None = None,
) -> list[BackfillLiteral]:
    """Select current gaps, or exact unconfirmed machine-era replacements.

    Era selection delegates to ``TranslationRecord.unconfirmed_machine_records_before``;
    therefore native-confirmed records never enter the replacement set.
    """
    if era_before is not None:
        result = await db.execute(
            TranslationRecord.unconfirmed_machine_records_before(project_id, era_before)
        )
        era_records = list(result.scalars().all())
        graph = await TranslationCoverageService(db, git).branch_graph(project_id, branch)
        output: list[BackfillLiteral] = []
        for record in era_records:
            if (
                record.state == "rejected"
                or record.source_value is None
                or (language is not None and record.language.casefold() != language.casefold())
                or (never_confirmed is True and record.confirmed_at is not None)
            ):
                continue
            subject, predicate = URIRef(record.entity_iri), URIRef(record.predicate)
            source = next(
                (
                    value
                    for value in graph.objects(subject, predicate)
                    if isinstance(value, Literal)
                    and str(value) == record.source_value
                    and hash_literal_value(str(value)) == record.source_value_hash
                ),
                None,
            )
            if source is None:
                continue
            if record.proposed_value is not None:
                target = Literal(record.proposed_value, lang=record.language)
                annotation = read_annotation(graph, subject, predicate, target)
                if (
                    (subject, predicate, target) in graph
                    and annotation is not None
                    and annotation.record_digest == translation_record_digest(record)
                ):
                    continue
            output.append(
                BackfillLiteral(
                    record.entity_iri,
                    record.predicate,
                    record.source_value,
                    source.language or "und",
                    record.language,
                )
            )
        return output

    service = TranslationCoverageService(db, git)
    return [
        BackfillLiteral(
            source.entity_iri,
            source.predicate,
            source.value,
            source.language or "und",
            target,
            context,
        )
        for source, target, context in await service.missing_literals(
            project_id, branch, language
        )
    ]


def _call_cost(messages: list[dict[str, str]], prices: tuple[float, float], output: int) -> float:
    return estimate_message_tokens(messages) * prices[0] + output * prices[1]


async def preview_backfill_cost(
    literals: list[BackfillLiteral],
    *,
    mechanism: str,
    primary_model: str,
    verifier_model: str,
    primary_provider: str,
    speed_mode: str,
    pricing_resolver: PricingResolver = get_model_pricing,
    batch_capable_providers: frozenset[str] = TRUE_BATCH_PROVIDERS,
) -> BackfillCostPreview:
    """Price every real call without invoking a provider.

    The upper bound is 1.5× prompt/input cost plus a fixed 256-token output ceiling for
    every call. This deliberately bounds JSON response growth independently of expected cost.
    No current adapter is declared to earn true batch pricing; callers may inject a
    capability set for a future true-batch adapter without changing the arithmetic.
    """
    primary_prices = await pricing_resolver(primary_model)
    verifier_prices = (
        primary_prices
        if verifier_model == primary_model
        else await pricing_resolver(verifier_model)
    )
    expected = upper = 0.0
    placeholder = "translated literal"
    for literal in literals:
        context = list(literal.context_labels)
        translate = build_translate_messages(
            literal.source_value,
            literal.source_language,
            literal.target_language,
            context,
            include_confidence=mechanism == "confidence",
        )
        calls: list[tuple[list[dict[str, str]], tuple[float, float]]] = [
            (translate, primary_prices)
        ]
        if mechanism == "consensus":
            calls.append(
                (
                    build_translate_messages(
                        literal.source_value,
                        literal.source_language,
                        literal.target_language,
                        context,
                    ),
                    verifier_prices,
                )
            )
        calls.append(
            (
                build_back_translate_messages(
                    placeholder, literal.source_language, literal.target_language
                ),
                verifier_prices,
            )
        )
        if mechanism == "consensus":
            calls.append(
                (
                    build_verify_messages(
                        literal.source_value,
                        literal.source_language,
                        literal.target_language,
                        placeholder,
                        placeholder,
                        literal.source_value,
                    ),
                    verifier_prices,
                )
            )
        for messages, prices in calls:
            expected += _call_cost(messages, prices, EXPECTED_OUTPUT_TOKENS)
            upper += estimate_message_tokens(messages) * prices[0] * 1.5
            upper += OUTPUT_TOKEN_CEILING * prices[1]
    discounted = speed_mode == "batch" and primary_provider in batch_capable_providers
    factor = 0.5 if discounted else 1.0
    return BackfillCostPreview(len(literals), expected * factor, upper * factor, discounted)


__all__ = [
    "BackfillCostPreview",
    "BackfillLiteral",
    "preview_backfill_cost",
    "select_backfill_literals",
]
