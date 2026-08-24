"""Embedding service — manage embeddings, semantic search, similarity."""

import logging
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import partial
from typing import Protocol, TypeVar, cast, runtime_checkable
from uuid import UUID

from cryptography.fernet import MultiFernet
from rdflib import Literal as RDFLiteral
from rdflib import URIRef
from rdflib.namespace import OWL, RDF, RDFS
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import Insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.models.embedding import (
    EmbeddingJob,
    EntityEmbedding,
    EntityEmbeddingStaging,
    ProjectEmbeddingConfig,
)
from ontokit.models.llm_config import ProjectLLMConfig
from ontokit.schemas.embeddings import (
    EmbeddingConfig,
    EmbeddingConfigUpdate,
    EmbeddingStatus,
    RankedCandidate,
    RankSuggestionRequest,
    SemanticSearchResponse,
    SemanticSearchResult,
    SemanticSearchResultWithBranch,
    SimilarEntity,
)
from ontokit.schemas.embeddings import EmbeddingProvider as EmbeddingProviderLiteral
from ontokit.services.branch_lock import branch_write_lock
from ontokit.services.embedding_providers import get_embedding_provider
from ontokit.services.embedding_providers.base import EmbeddingProvider as EmbeddingProviderBase
from ontokit.services.embedding_text_builder import build_embedding_text
from ontokit.services.llm.audit import finalize_llm_call, reserve_llm_call
from ontokit.services.llm.base import estimate_tokens
from ontokit.services.llm.budget import BudgetLimits
from ontokit.services.llm.pricing import PricingUnavailableError, get_model_pricing
from ontokit.services.rdf_utils import get_entity_type as _get_entity_type
from ontokit.services.rdf_utils import is_deprecated as _is_deprecated

logger = logging.getLogger(__name__)


class EmbeddingBudgetExceeded(RuntimeError):
    """A paid embedding call was refused by the project budget."""


class EmbeddingPricingUnavailable(RuntimeError):
    """A paid embedding model cannot be metered safely."""


_EmbeddingResult = TypeVar("_EmbeddingResult")


def _get_fernet() -> MultiFernet:
    """Share the versioned provider-key KDF and legacy fallback."""
    from ontokit.services.llm.crypto import _get_fernet as get_provider_key_fernet

    return get_provider_key_fernet()


def _encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret string using Fernet symmetric encryption."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def _decrypt_secret(ciphertext: str) -> str:
    """Decrypt a Fernet-encrypted secret string."""
    return _get_fernet().decrypt(ciphertext.encode()).decode()


@runtime_checkable
class _HasToList(Protocol):
    """Anything that exposes a ``tolist()`` method returning a list of floats.

    Matches numpy's ``ndarray.tolist()`` and torch tensors' equivalent
    without pulling either library into the dependency surface here.
    """

    def tolist(self) -> list[float]: ...


def _vec_to_str(vec: list[float] | _HasToList) -> str:
    """Convert an embedding vector to a pgvector-compatible string.

    Accepts either a Python ``list[float]`` (newly-embedded queries) or any
    object exposing ``tolist()`` — typically a numpy array, which is what
    pgvector deserializes ``Vector`` column reads into. pgvector's text input
    format is ``[v1,v2,...]``: space-separated values are rejected.
    ``str(list)`` produces commas; ``str(np.ndarray)`` produces spaces.
    Normalize via ``.tolist()`` so the output is always pgvector-parseable
    regardless of the input source.
    """
    if isinstance(vec, _HasToList):
        vec = vec.tolist()
    return str(vec)


_HNSW_VECTOR_DIMENSIONS = frozenset({384, 1024, 1536})
_HNSW_HALF_VECTOR_DIMENSIONS = frozenset({3072})


def _validate_embedding_dimensions(embedding: list[float], *, expected: int) -> None:
    """Refuse provider drift before a mixed-dimension or non-finite vector reaches Postgres."""
    actual = len(embedding)
    if actual != expected:
        raise ValueError(f"Embedding provider expected {expected} dimensions, received {actual}")
    if not all(math.isfinite(value) for value in embedding):
        raise ValueError("Embedding provider returned a non-finite vector")


def _distance_operands(dimensions: int) -> tuple[str, str]:
    """Return SQL operands matching an installed partial HNSW expression index."""
    if dimensions in _HNSW_VECTOR_DIMENSIONS:
        return (
            f"embedding::vector({dimensions})",
            f"CAST(:query_vec AS vector({dimensions}))",
        )
    if dimensions in _HNSW_HALF_VECTOR_DIMENSIONS:
        return (
            f"embedding::halfvec({dimensions})",
            f"CAST(:query_vec AS halfvec({dimensions}))",
        )
    return ("embedding", "CAST(:query_vec AS vector)")


_EMBEDDING_UPDATE_COLUMNS = (
    "entity_type",
    "label",
    "embedding_text",
    "embedding",
    "dimensions",
    "provider",
    "model_name",
    "deprecated",
)


def _active_embedding_upsert(values: dict[str, object]) -> Insert:
    """Build a race-safe incremental upsert for the live embedding index."""
    statement = pg_insert(EntityEmbedding).values(**values)
    return statement.on_conflict_do_update(
        constraint="uq_entity_embedding",
        set_={column: getattr(statement.excluded, column) for column in _EMBEDDING_UPDATE_COLUMNS},
    )


def _staged_embedding_upsert(values: list[dict[str, object]]) -> Insert:
    """Build a retry-safe batch write into one job's private snapshot."""
    statement = pg_insert(EntityEmbeddingStaging).values(values)
    return statement.on_conflict_do_update(
        index_elements=["job_id", "entity_iri"],
        set_={
            column: getattr(statement.excluded, column)
            for column in (
                "project_id",
                "branch",
                *_EMBEDDING_UPDATE_COLUMNS,
            )
        },
    )


def _staged_snapshot_activation(job_id: UUID) -> Insert:
    """Build the atomic copy from one complete private snapshot to the live index."""
    active_columns = [
        "id",
        "project_id",
        "branch",
        "entity_iri",
        "entity_type",
        "label",
        "embedding_text",
        "embedding",
        "dimensions",
        "provider",
        "model_name",
        "deprecated",
    ]
    staged_snapshot = select(
        func.gen_random_uuid(),
        EntityEmbeddingStaging.project_id,
        EntityEmbeddingStaging.branch,
        EntityEmbeddingStaging.entity_iri,
        EntityEmbeddingStaging.entity_type,
        EntityEmbeddingStaging.label,
        EntityEmbeddingStaging.embedding_text,
        EntityEmbeddingStaging.embedding,
        EntityEmbeddingStaging.dimensions,
        EntityEmbeddingStaging.provider,
        EntityEmbeddingStaging.model_name,
        EntityEmbeddingStaging.deprecated,
    ).where(EntityEmbeddingStaging.job_id == job_id)
    return pg_insert(EntityEmbedding).from_select(active_columns, staged_snapshot)


class EmbeddingService:
    def __init__(self, db: AsyncSession):
        self._db = db

    async def _check_and_audit_embedding(
        self,
        project_id: UUID,
        provider: EmbeddingProviderBase,
        input_text: str,
        endpoint: str,
        user_id: str,
        operation: Callable[[], Awaitable[_EmbeddingResult]],
    ) -> _EmbeddingResult:
        """Reserve paid embedding budget before actuation and retain its receipt."""
        provider_name = provider.provider_name
        if provider_name == "local":
            return await operation()

        model_name = provider.model_id
        config = (
            await self._db.execute(
                select(ProjectLLMConfig).where(ProjectLLMConfig.project_id == project_id)
            )
        ).scalar_one_or_none()
        if config is None:
            embedding_config = (
                await self._db.execute(
                    select(ProjectEmbeddingConfig).where(
                        ProjectEmbeddingConfig.project_id == project_id
                    )
                )
            ).scalar_one_or_none()
            if embedding_config is None:
                raise EmbeddingBudgetExceeded(
                    "Paid embeddings require an embedding budget configuration"
                )
            limits = BudgetLimits(
                monthly_budget_usd=embedding_config.monthly_budget_usd,
                daily_cap_usd=embedding_config.daily_cap_usd,
            )
        else:
            limits = BudgetLimits(
                monthly_budget_usd=config.monthly_budget_usd,
                daily_cap_usd=config.daily_cap_usd,
            )
        try:
            input_price, _ = await get_model_pricing(model_name)
        except PricingUnavailableError as exc:
            raise EmbeddingPricingUnavailable(model_name) from exc
        tokens = estimate_tokens(input_text)
        reserved_cost = tokens * input_price
        from ontokit.core.database import async_session_maker

        async with async_session_maker() as reservation_db:
            reservation_id, reason = await reserve_llm_call(
                reservation_db,
                project_id=project_id,
                config=limits,
                user_id=user_id,
                model=model_name,
                provider=provider_name,
                endpoint=endpoint,
                input_tokens=tokens,
                output_tokens=0,
                cost_estimate_usd=reserved_cost,
            )
        if reservation_id is None:
            raise EmbeddingBudgetExceeded(str(reason))

        async def finalize(*, succeeded: bool) -> None:
            try:
                async with async_session_maker() as audit_db:
                    await finalize_llm_call(
                        audit_db,
                        reservation_id,
                        endpoint,
                        succeeded=succeeded,
                    )
            except Exception as exc:
                # The committed reservation remains budget-visible and explicitly
                # indeterminate, so a bookkeeping outage cannot reopen the cap.
                logger.error(
                    "ALERT llm_audit_finalize_failed: project=%s provider=%s error_type=%s",
                    project_id,
                    provider_name,
                    type(exc).__name__,
                    extra={
                        "event": "llm_audit_finalize_failed",
                        "project_id": str(project_id),
                        "provider": provider_name,
                        "error_type": type(exc).__name__,
                    },
                )

        try:
            result = await operation()
        except BaseException:
            await finalize(succeeded=False)
            raise
        await finalize(succeeded=True)
        return result

    async def get_config(self, project_id: UUID) -> EmbeddingConfig | None:
        result = await self._db.execute(
            select(ProjectEmbeddingConfig).where(ProjectEmbeddingConfig.project_id == project_id)
        )
        config = result.scalar_one_or_none()
        if not config:
            return None
        return EmbeddingConfig(
            provider=cast(EmbeddingProviderLiteral, config.provider),
            model_name=config.model_name,
            api_key_set=config.api_key_encrypted is not None,
            dimensions=config.dimensions,
            auto_embed_on_save=config.auto_embed_on_save,
            monthly_budget_usd=config.monthly_budget_usd,
            daily_cap_usd=config.daily_cap_usd,
            last_full_embed_at=config.last_full_embed_at.isoformat()
            if config.last_full_embed_at
            else None,
        )

    async def update_config(
        self, project_id: UUID, update: EmbeddingConfigUpdate
    ) -> EmbeddingConfig:
        result = await self._db.execute(
            select(ProjectEmbeddingConfig).where(ProjectEmbeddingConfig.project_id == project_id)
        )
        config = result.scalar_one_or_none()

        if not config:
            config = ProjectEmbeddingConfig(project_id=project_id)
            self._db.add(config)

        previous_provider = config.provider
        target_provider = update.provider or config.provider
        target_model = update.model_name or config.model_name
        model_changed = target_provider != config.provider or target_model != config.model_name
        if model_changed:
            # Paid providers validate key presence in their constructors. Use a
            # key supplied in this request, or retain the encrypted key only
            # when changing models within the same provider.
            provider_api_key = update.api_key
            if (
                provider_api_key is None
                and config.api_key_encrypted
                and target_provider == previous_provider
            ):
                provider_api_key = _decrypt_secret(config.api_key_encrypted)
            provider = get_embedding_provider(target_provider, target_model, provider_api_key)
            config.provider = target_provider
            config.model_name = target_model
            config.dimensions = provider.dimensions
            # Invalidate stale embeddings and reset full-embed marker
            config.last_full_embed_at = None
            await self._db.execute(
                delete(EntityEmbedding).where(EntityEmbedding.project_id == project_id)
            )
        if update.api_key is not None:
            config.api_key_encrypted = _encrypt_secret(update.api_key)
        if update.auto_embed_on_save is not None:
            config.auto_embed_on_save = update.auto_embed_on_save
        if update.monthly_budget_usd is not None:
            config.monthly_budget_usd = update.monthly_budget_usd
        if update.daily_cap_usd is not None:
            config.daily_cap_usd = update.daily_cap_usd

        await self._db.commit()
        await self._db.refresh(config)

        return EmbeddingConfig(
            provider=cast(EmbeddingProviderLiteral, config.provider),
            model_name=config.model_name,
            api_key_set=config.api_key_encrypted is not None,
            dimensions=config.dimensions,
            auto_embed_on_save=config.auto_embed_on_save,
            monthly_budget_usd=config.monthly_budget_usd,
            daily_cap_usd=config.daily_cap_usd,
            last_full_embed_at=config.last_full_embed_at.isoformat()
            if config.last_full_embed_at
            else None,
        )

    async def get_status(self, project_id: UUID, branch: str) -> EmbeddingStatus:
        config = await self._db.execute(
            select(ProjectEmbeddingConfig).where(ProjectEmbeddingConfig.project_id == project_id)
        )
        cfg = config.scalar_one_or_none()

        # Count embedded entities
        embedded_q = (
            select(func.count())
            .select_from(EntityEmbedding)
            .where(
                EntityEmbedding.project_id == project_id,
                EntityEmbedding.branch == branch,
            )
        )
        embedded_count = (await self._db.execute(embedded_q)).scalar() or 0

        # Check for active job
        job_q = (
            select(EmbeddingJob)
            .where(
                EmbeddingJob.project_id == project_id,
                EmbeddingJob.branch == branch,
                EmbeddingJob.status.in_(["pending", "running"]),
            )
            .order_by(EmbeddingJob.started_at.desc())
            .limit(1)
        )
        job_result = await self._db.execute(job_q)
        active_job = job_result.scalar_one_or_none()

        job_in_progress = active_job is not None
        job_progress = None
        total_entities = embedded_count

        if active_job and active_job.total_entities > 0:
            job_progress = round(active_job.embedded_entities / active_job.total_entities * 100, 1)
            total_entities = max(total_entities, active_job.total_entities)
        else:
            # Use last completed job's total as denominator for accurate coverage
            last_job_q = (
                select(EmbeddingJob.total_entities)
                .where(
                    EmbeddingJob.project_id == project_id,
                    EmbeddingJob.branch == branch,
                    EmbeddingJob.status == "completed",
                    EmbeddingJob.total_entities > 0,
                )
                .order_by(EmbeddingJob.completed_at.desc())
                .limit(1)
            )
            last_total = (await self._db.execute(last_job_q)).scalar()
            if last_total:
                total_entities = max(total_entities, last_total)

        coverage = round(embedded_count / total_entities * 100, 1) if total_entities > 0 else 0.0

        return EmbeddingStatus(
            total_entities=total_entities,
            embedded_entities=embedded_count,
            coverage_percent=coverage,
            provider=cfg.provider if cfg else "local",
            model_name=cfg.model_name if cfg else "all-MiniLM-L6-v2",
            job_in_progress=job_in_progress,
            job_progress_percent=job_progress,
            last_full_embed_at=cfg.last_full_embed_at.isoformat()
            if cfg and cfg.last_full_embed_at
            else None,
        )

    async def clear_embeddings(self, project_id: UUID) -> None:
        await self._db.execute(
            delete(EntityEmbedding).where(EntityEmbedding.project_id == project_id)
        )
        await self._db.execute(delete(EmbeddingJob).where(EmbeddingJob.project_id == project_id))
        # Reset last_full_embed_at
        result = await self._db.execute(
            select(ProjectEmbeddingConfig).where(ProjectEmbeddingConfig.project_id == project_id)
        )
        cfg = result.scalar_one_or_none()
        if cfg:
            cfg.last_full_embed_at = None
        await self._db.commit()

    async def _get_provider(self, project_id: UUID) -> "EmbeddingProviderBase":
        """Get the embedding provider for a project."""
        result = await self._db.execute(
            select(ProjectEmbeddingConfig).where(ProjectEmbeddingConfig.project_id == project_id)
        )
        cfg = result.scalar_one_or_none()

        provider_name = cfg.provider if cfg else "local"
        model_name = cfg.model_name if cfg else "all-MiniLM-L6-v2"
        api_key = None
        if cfg and cfg.api_key_encrypted:
            api_key = _decrypt_secret(cfg.api_key_encrypted)

        return get_embedding_provider(provider_name, model_name, api_key)

    async def embed_project(self, project_id: UUID, branch: str, job_id: UUID) -> None:
        """Full project embedding (called from ARQ worker)."""
        # Get or create job
        result = await self._db.execute(select(EmbeddingJob).where(EmbeddingJob.id == job_id))
        job = result.scalar_one_or_none()
        if not job:
            job = EmbeddingJob(
                id=job_id,
                project_id=project_id,
                branch=branch,
                status="running",
            )
            self._db.add(job)
        else:
            job.status = "running"
        await self._db.commit()

        try:
            # Load graph
            from sqlalchemy.orm import selectinload

            from ontokit.git.bare_repository import BareGitRepositoryService
            from ontokit.models.project import Project, get_git_ontology_path
            from ontokit.services.ontology import get_ontology_service
            from ontokit.services.storage import get_storage_service

            proj_result = await self._db.execute(
                select(Project)
                .options(selectinload(Project.github_integration))
                .where(Project.id == project_id)
            )
            project = proj_result.scalar_one_or_none()
            if not project:
                raise ValueError("Project not found")
            if not project.source_file_path and not project.github_integration:
                raise ValueError("Project has no ontology file")

            storage = get_storage_service()
            ontology = get_ontology_service(storage)
            git = BareGitRepositoryService()

            filename = get_git_ontology_path(project)
            repository = None
            source_revision: str | None = None
            try:
                repository = git.get_repository(project_id)
                source_revision = repository.get_branch_commit_hash(branch)
                graph = await ontology.load_from_git(project_id, branch, filename, git)
            except (FileNotFoundError, KeyError, ValueError):
                # Only fall back to storage for the default branch; a specific
                # branch that doesn't exist in git should fail the job rather
                # than silently embedding the storage snapshot under that branch.
                if not project.source_file_path:
                    raise
                default_branch = git.get_default_branch(project_id)
                if branch and branch != default_branch:
                    raise
                graph = await ontology.load_from_storage(
                    project_id, project.source_file_path, branch
                )
                source_revision = None

            # Extract entities
            entities: list[tuple[URIRef, str, str]] = []  # (uri, type, text)
            seen: set[URIRef] = set()
            for s in graph.subjects(RDF.type, None):
                if not isinstance(s, URIRef) or s == OWL.Thing or s in seen:
                    continue
                seen.add(s)
                etype = _get_entity_type(graph, s)
                if etype == "unknown":
                    continue
                embed_text = build_embedding_text(graph, s, etype)
                entities.append((s, etype, embed_text))

            job.total_entities = len(entities)
            await self._db.commit()

            # Get provider
            provider = await self._get_provider(project_id)
            expected_dimensions = provider.dimensions

            # A worker retry owns the same job id. Remove any crash-left staging
            # rows before rebuilding its private snapshot.
            await self._db.execute(
                delete(EntityEmbeddingStaging).where(EntityEmbeddingStaging.job_id == job_id)
            )
            await self._db.commit()

            # Batch embed into the job-private snapshot. The live index remains
            # untouched until the complete snapshot is activated below.
            batch_size = 64
            for i in range(0, len(entities), batch_size):
                batch = entities[i : i + batch_size]
                texts = [t[2] for t in batch]
                embeddings = cast(
                    list[list[float]],
                    await self._check_and_audit_embedding(
                        project_id,
                        provider,
                        "\n".join(texts),
                        "embeddings/full-project",
                        "system:embedding-worker",
                        partial(provider.embed_batch, texts),
                    ),
                )
                staged_rows: list[dict[str, object]] = []
                for (uri, etype, embed_text), embedding in zip(batch, embeddings, strict=True):
                    _validate_embedding_dimensions(embedding, expected=expected_dimensions)
                    iri = str(uri)
                    label = next(
                        (
                            str(o)
                            for o in graph.objects(uri, RDFS.label)
                            if isinstance(o, RDFLiteral)
                        ),
                        None,
                    )
                    deprecated = _is_deprecated(graph, uri)
                    staged_rows.append(
                        {
                            "job_id": job_id,
                            "project_id": project_id,
                            "branch": branch,
                            "entity_iri": iri,
                            "entity_type": etype,
                            "label": label,
                            "embedding_text": embed_text,
                            "embedding": embedding,
                            "dimensions": expected_dimensions,
                            "provider": provider.provider_name,
                            "model_name": provider.model_id,
                            "deprecated": deprecated,
                        }
                    )

                if staged_rows:
                    await self._db.execute(_staged_embedding_upsert(staged_rows))

                job.embedded_entities = min(i + batch_size, len(entities))
                await self._db.commit()

            # Activation is one transaction under the same cross-process branch
            # lock used by ontology writers. A changed source revision aborts
            # instead of publishing a stale snapshot or pruning new entities.
            async with branch_write_lock(self._db, project_id, branch):
                if source_revision is not None:
                    if repository is None:
                        raise RuntimeError("Embedding source repository was not retained")
                    current_revision = repository.get_branch_commit_hash(branch)
                    if current_revision != source_revision:
                        raise RuntimeError(
                            "Ontology branch changed during embedding refresh; retry the job"
                        )

                await self._db.execute(
                    delete(EntityEmbedding).where(
                        EntityEmbedding.project_id == project_id,
                        EntityEmbedding.branch == branch,
                    )
                )
                await self._db.execute(_staged_snapshot_activation(job_id))
                await self._db.execute(
                    delete(EntityEmbeddingStaging).where(EntityEmbeddingStaging.job_id == job_id)
                )

                completed_at = datetime.now(UTC)
                job.status = "completed"
                job.completed_at = completed_at
                cfg_result = await self._db.execute(
                    select(ProjectEmbeddingConfig).where(
                        ProjectEmbeddingConfig.project_id == project_id
                    )
                )
                cfg = cfg_result.scalar_one_or_none()
                if cfg:
                    cfg.last_full_embed_at = completed_at
                await self._db.commit()

        except Exception as e:
            await self._db.rollback()
            # Discard an incomplete private snapshot; the prior live index was
            # never touched. Use a raw UPDATE because rollback may expire job.
            await self._db.execute(
                delete(EntityEmbeddingStaging).where(EntityEmbeddingStaging.job_id == job_id)
            )
            await self._db.execute(
                update(EmbeddingJob)
                .where(EmbeddingJob.id == job_id)
                .values(
                    status="failed",
                    error_message=str(e),
                    completed_at=datetime.now(UTC),
                )
            )
            await self._db.commit()
            raise

    async def embed_single_entity(self, project_id: UUID, branch: str, entity_iri: str) -> None:
        """Re-embed one entity (for auto_embed_on_save)."""
        from ontokit.services.ontology import get_ontology_service

        ontology = get_ontology_service()
        if not ontology.is_loaded(project_id, branch):
            logger.warning(
                "Ontology not loaded for project %s branch %s — skipping auto-embed of %s",
                project_id,
                branch,
                entity_iri,
            )
            return

        graph = await ontology._get_graph(project_id, branch)
        uri = URIRef(entity_iri)

        etype = _get_entity_type(graph, uri)
        if etype == "unknown":
            return

        embed_text = build_embedding_text(graph, uri, etype)
        provider = await self._get_provider(project_id)
        embedding = cast(
            list[float],
            await self._check_and_audit_embedding(
                project_id,
                provider,
                embed_text,
                "embeddings/single-entity",
                "system:embedding-worker",
                lambda: provider.embed_text(embed_text),
            ),
        )

        label = next(
            (str(o) for o in graph.objects(uri, RDFS.label) if isinstance(o, RDFLiteral)),
            None,
        )
        deprecated = _is_deprecated(graph, uri)
        dimensions = provider.dimensions
        _validate_embedding_dimensions(embedding, expected=dimensions)
        values: dict[str, object] = {
            "project_id": project_id,
            "branch": branch,
            "entity_iri": entity_iri,
            "entity_type": etype,
            "label": label,
            "embedding_text": embed_text,
            "embedding": embedding,
            "dimensions": dimensions,
            "provider": provider.provider_name,
            "model_name": provider.model_id,
            "deprecated": deprecated,
        }
        async with branch_write_lock(self._db, project_id, branch):
            await self._db.execute(_active_embedding_upsert(values))
            await self._db.commit()

    async def semantic_search(
        self,
        project_id: UUID,
        branch: str,
        query: str,
        limit: int = 20,
        threshold: float = 0.3,
        billing_user_id: str = "system:semantic-search",
    ) -> SemanticSearchResponse:
        """Semantic search using cosine similarity."""
        # Check if embeddings exist
        count_q = (
            select(func.count())
            .select_from(EntityEmbedding)
            .where(
                EntityEmbedding.project_id == project_id,
                EntityEmbedding.branch == branch,
            )
        )
        count = (await self._db.execute(count_q)).scalar() or 0

        if count == 0:
            return SemanticSearchResponse(results=[], search_mode="text_fallback")

        # Embed query
        provider = await self._get_provider(project_id)
        query_vec = cast(
            list[float],
            await self._check_and_audit_embedding(
                project_id,
                provider,
                query,
                "embeddings/semantic-search",
                billing_user_id,
                lambda: provider.embed_text(query),
            ),
        )
        dimensions = provider.dimensions
        _validate_embedding_dimensions(query_vec, expected=dimensions)
        column_operand, query_operand = _distance_operands(dimensions)

        # pgvector cosine distance: <=> returns distance (0=identical), score = 1 - distance.
        # NOTE: must use CAST(:query_vec AS vector) — SQLAlchemy's text() parser silently
        # drops :name bindparams when immediately followed by ::type (Postgres cast syntax),
        # producing a syntax error at the literal ":" in the wire SQL.
        # Dynamic operands come only from _distance_operands()'s fixed dimension map.
        query_str = text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
            f"""
                SELECT entity_iri, label, entity_type, deprecated,
                       1 - ({column_operand} <=> {query_operand}) AS score
                FROM entity_embeddings
                WHERE project_id = :pid AND branch = :br AND dimensions = :dimensions
                ORDER BY {column_operand} <=> {query_operand}
                LIMIT :lim
            """
        )  # nosec B608

        result = await self._db.execute(
            query_str,
            {
                "query_vec": _vec_to_str(query_vec),
                "pid": str(project_id),
                "br": branch,
                "dimensions": dimensions,
                "lim": limit,
            },
        )

        results = []
        for row in result:
            if row.score >= threshold:
                results.append(
                    SemanticSearchResult(
                        iri=row.entity_iri,
                        label=row.label or "",
                        entity_type=row.entity_type,
                        score=round(row.score, 4),
                        deprecated=row.deprecated,
                    )
                )

        return SemanticSearchResponse(results=results, search_mode="semantic")

    async def semantic_search_all_branches(
        self,
        project_id: UUID,
        query: str,
        limit: int = 20,
        threshold: float = 0.3,
        billing_user_id: str = "system:duplicate-check",
        exclude_branch: str | None = None,
        exclude_iris: set[str] | None = None,
    ) -> list[SemanticSearchResultWithBranch]:
        """Search across ALL branches for a project (DEDUP-08).

        Unlike semantic_search() which filters by branch, this queries across all
        branches to catch parallel work collisions between users on different
        suggestion branches. Used by DuplicateCheckService for cross-branch
        duplicate detection.
        """
        # Check if any embeddings exist for this project
        count_q = (
            select(func.count())
            .select_from(EntityEmbedding)
            .where(EntityEmbedding.project_id == project_id)
        )
        count = (await self._db.execute(count_q)).scalar() or 0
        if count == 0:
            return []

        # Embed query
        provider = await self._get_provider(project_id)
        query_vec = cast(
            list[float],
            await self._check_and_audit_embedding(
                project_id,
                provider,
                query,
                "embeddings/duplicate-check",
                billing_user_id,
                lambda: provider.embed_text(query),
            ),
        )
        dimensions = provider.dimensions
        _validate_embedding_dimensions(query_vec, expected=dimensions)
        column_operand, query_operand = _distance_operands(dimensions)

        # pgvector cosine distance across ALL branches (no branch = :br filter).
        # See note in semantic_search() above re: CAST() vs ::vector.
        exclusions = ""
        params: dict[str, object] = {
            "query_vec": _vec_to_str(query_vec),
            "pid": str(project_id),
            "dimensions": dimensions,
            "threshold": threshold,
            "lim": limit,
        }
        if exclude_branch is not None:
            exclusions += " AND branch != :exclude_branch"
            params["exclude_branch"] = exclude_branch
        for index, iri in enumerate(sorted(exclude_iris or set())):
            key = f"exclude_iri_{index}"
            exclusions += f" AND entity_iri != :{key}"
            params[key] = iri

        # Operands are allowlisted; exclusions contain fixed SQL and generated bind names only.
        query_str = text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
            f"""
                SELECT entity_iri, label, entity_type, branch, deprecated, embedding_text,
                       1 - ({column_operand} <=> {query_operand}) AS score
                FROM entity_embeddings
                WHERE project_id = :pid
                  AND dimensions = :dimensions
                  AND (1 - ({column_operand} <=> {query_operand})) >= :threshold
                  {exclusions}
                ORDER BY {column_operand} <=> {query_operand}
                LIMIT :lim
            """
        )  # nosec B608

        result = await self._db.execute(
            query_str,
            params,
        )

        return [
            SemanticSearchResultWithBranch(
                iri=row.entity_iri,
                label=row.label or "",
                entity_type=row.entity_type,
                score=round(float(row.score), 4),
                deprecated=row.deprecated,
                branch=row.branch,
                embedding_text=row.embedding_text,
            )
            for row in result
        ]

    async def find_similar(
        self,
        project_id: UUID,
        branch: str,
        entity_iri: str,
        limit: int = 10,
        threshold: float = 0.5,
    ) -> list[SimilarEntity]:
        """Find entities similar to a given entity."""
        # Get entity's embedding
        emb_q = select(EntityEmbedding).where(
            EntityEmbedding.project_id == project_id,
            EntityEmbedding.branch == branch,
            EntityEmbedding.entity_iri == entity_iri,
        )
        emb = (await self._db.execute(emb_q)).scalar_one_or_none()
        if not emb:
            return []
        dimensions = emb.dimensions
        column_operand, query_operand = _distance_operands(dimensions)

        # kNN search excluding self. See note in semantic_search() above re: CAST() vs ::vector.
        # Dynamic operands come only from _distance_operands()'s fixed dimension map.
        query_str = text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
            f"""
                SELECT entity_iri, label, entity_type, deprecated,
                       1 - ({column_operand} <=> {query_operand}) AS score
                FROM entity_embeddings
                WHERE project_id = :pid AND branch = :br
                  AND dimensions = :dimensions AND entity_iri != :self_iri
                ORDER BY {column_operand} <=> {query_operand}
                LIMIT :lim
            """
        )  # nosec B608

        result = await self._db.execute(
            query_str,
            {
                "query_vec": _vec_to_str(emb.embedding),
                "pid": str(project_id),
                "br": branch,
                "dimensions": dimensions,
                "self_iri": entity_iri,
                "lim": limit,
            },
        )

        results = []
        for row in result:
            if row.score >= threshold:
                results.append(
                    SimilarEntity(
                        iri=row.entity_iri,
                        label=row.label or "",
                        entity_type=row.entity_type,
                        score=round(row.score, 4),
                        deprecated=row.deprecated,
                    )
                )

        return results

    async def rank_suggestions(
        self,
        project_id: UUID,
        body: RankSuggestionRequest,
    ) -> list[RankedCandidate]:
        """Rank candidate entities by similarity to context entity."""
        if not body.candidates:
            return []

        # Resolve branch — caller should have done this, but guard defensively
        resolved_branch = body.branch
        if not resolved_branch:
            from ontokit.git import get_git_service

            resolved_branch = get_git_service().get_default_branch(project_id)

        # Get context embedding
        emb_q = select(EntityEmbedding).where(
            EntityEmbedding.project_id == project_id,
            EntityEmbedding.branch == resolved_branch,
            EntityEmbedding.entity_iri == body.context_iri,
        )
        ctx_emb = (await self._db.execute(emb_q)).scalar_one_or_none()
        if not ctx_emb:
            return []
        dimensions = ctx_emb.dimensions

        # Get candidate embeddings
        candidates_q = select(EntityEmbedding).where(
            EntityEmbedding.project_id == project_id,
            EntityEmbedding.branch == resolved_branch,
            EntityEmbedding.dimensions == dimensions,
            EntityEmbedding.entity_iri.in_(body.candidates),
        )
        cand_result = await self._db.execute(candidates_q)
        candidates = cand_result.scalars().all()

        # Compute cosine similarity using numpy
        import numpy as np

        ctx_vec = np.array(ctx_emb.embedding)
        ctx_norm = np.linalg.norm(ctx_vec)
        if ctx_norm == 0:
            return []

        ranked = []
        for cand in candidates:
            cand_vec = np.array(cand.embedding)
            if cand_vec.shape != ctx_vec.shape:
                continue
            cand_norm = np.linalg.norm(cand_vec)
            if cand_norm == 0:
                continue
            sim = float(np.dot(ctx_vec, cand_vec) / (ctx_norm * cand_norm))
            ranked.append(
                RankedCandidate(
                    iri=cand.entity_iri,
                    label=cand.label or "",
                    score=round(sim, 4),
                )
            )

        ranked.sort(key=lambda x: x.score, reverse=True)
        return ranked
