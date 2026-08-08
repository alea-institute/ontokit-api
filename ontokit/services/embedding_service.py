"""Embedding service — manage embeddings, semantic search, similarity."""

import base64
import hashlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import partial
from typing import Protocol, TypeVar, cast, runtime_checkable
from uuid import UUID

from cryptography.fernet import Fernet
from rdflib import Literal as RDFLiteral
from rdflib import URIRef
from rdflib.namespace import OWL, RDF, RDFS
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.models.embedding import (
    EmbeddingJob,
    EntityEmbedding,
    ProjectEmbeddingConfig,
    Vector,
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
from ontokit.services.embedding_providers import get_embedding_provider
from ontokit.services.embedding_providers.base import EmbeddingProvider as EmbeddingProviderBase
from ontokit.services.embedding_text_builder import build_embedding_text
from ontokit.services.llm.audit import log_llm_call
from ontokit.services.llm.base import estimate_tokens
from ontokit.services.llm.budget import check_budget
from ontokit.services.llm.pricing import PricingUnavailableError, get_model_pricing
from ontokit.services.rdf_utils import get_entity_type as _get_entity_type
from ontokit.services.rdf_utils import is_deprecated as _is_deprecated

logger = logging.getLogger(__name__)


class EmbeddingBudgetExceeded(RuntimeError):
    """A paid embedding call was refused by the project budget."""


class EmbeddingPricingUnavailable(RuntimeError):
    """A paid embedding model cannot be metered safely."""
_EmbeddingResult = TypeVar("_EmbeddingResult")


def _get_fernet() -> Fernet:
    """Derive a Fernet key from the application secret."""
    from ontokit.core.config import settings

    key = hashlib.sha256(settings.secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


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
        """Run a paid embedding operation through budget and audit controls."""
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
                raise EmbeddingBudgetExceeded("Paid embeddings require a project budget")
            config = cast(ProjectLLMConfig, embedding_config)
        within_budget, reason = await check_budget(self._db, project_id, config)
        if not within_budget:
            raise EmbeddingBudgetExceeded(str(reason))

        try:
            input_price, _ = await get_model_pricing(model_name)
        except PricingUnavailableError as exc:
            raise EmbeddingPricingUnavailable(model_name) from exc
        result = await operation()
        tokens = estimate_tokens(input_text)
        from ontokit.core.database import async_session_maker

        async with async_session_maker() as audit_db:
            await log_llm_call(
                audit_db,
                str(project_id),
                user_id,
                model_name,
                provider_name,
                endpoint,
                tokens,
                0,
                tokens * input_price,
            )
            await audit_db.commit()
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

        model_changed = False
        if update.provider is not None and update.provider != config.provider:
            config.provider = update.provider
            model_changed = True
        if update.model_name is not None and update.model_name != config.model_name:
            config.model_name = update.model_name
            model_changed = True
        if model_changed:
            # Update dimensions based on new provider/model
            provider = get_embedding_provider(config.provider, config.model_name, None)
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
                    EmbeddingJob.status == "complete",
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
            try:
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

            # Batch embed
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

                for (uri, etype, embed_text), embedding in zip(batch, embeddings, strict=True):
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

                    # Upsert
                    existing_q = select(EntityEmbedding).where(
                        EntityEmbedding.project_id == project_id,
                        EntityEmbedding.branch == branch,
                        EntityEmbedding.entity_iri == iri,
                    )
                    existing = (await self._db.execute(existing_q)).scalar_one_or_none()

                    if existing:
                        existing.embedding = embedding
                        existing.embedding_text = embed_text
                        existing.label = label
                        existing.entity_type = etype
                        existing.deprecated = deprecated
                        existing.provider = provider.provider_name
                        existing.model_name = provider.model_id
                    else:
                        self._db.add(
                            EntityEmbedding(
                                project_id=project_id,
                                branch=branch,
                                entity_iri=iri,
                                entity_type=etype,
                                label=label,
                                embedding_text=embed_text,
                                embedding=embedding,
                                provider=provider.provider_name,
                                model_name=provider.model_id,
                                deprecated=deprecated,
                            )
                        )

                job.embedded_entities = min(i + batch_size, len(entities))
                await self._db.commit()

            # Prune embeddings for entities no longer in the ontology
            current_iris = {str(uri) for uri, _, _ in entities}
            if current_iris:
                await self._db.execute(
                    delete(EntityEmbedding).where(
                        EntityEmbedding.project_id == project_id,
                        EntityEmbedding.branch == branch,
                        ~EntityEmbedding.entity_iri.in_(current_iris),
                    )
                )
            else:
                # No entities at all — clear everything for this branch
                await self._db.execute(
                    delete(EntityEmbedding).where(
                        EntityEmbedding.project_id == project_id,
                        EntityEmbedding.branch == branch,
                    )
                )

            # Update job and config
            job.status = "completed"
            job.completed_at = datetime.now(UTC)
            await self._db.commit()

            # Update last_full_embed_at
            cfg_result = await self._db.execute(
                select(ProjectEmbeddingConfig).where(
                    ProjectEmbeddingConfig.project_id == project_id
                )
            )
            cfg = cfg_result.scalar_one_or_none()
            if cfg:
                cfg.last_full_embed_at = datetime.now(UTC)
                await self._db.commit()

        except Exception as e:
            await self._db.rollback()
            # Use a raw UPDATE to persist failure status — the ORM instance
            # may be expired/detached after rollback.
            await self._db.execute(
                update(EmbeddingJob)
                .where(EmbeddingJob.id == job.id)
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

        # Upsert
        existing_q = select(EntityEmbedding).where(
            EntityEmbedding.project_id == project_id,
            EntityEmbedding.branch == branch,
            EntityEmbedding.entity_iri == entity_iri,
        )
        existing = (await self._db.execute(existing_q)).scalar_one_or_none()

        if existing:
            existing.embedding = embedding
            existing.embedding_text = embed_text
            existing.label = label
            existing.entity_type = etype
            existing.deprecated = deprecated
            existing.provider = provider.provider_name
            existing.model_name = provider.model_id
        else:
            self._db.add(
                EntityEmbedding(
                    project_id=project_id,
                    branch=branch,
                    entity_iri=entity_iri,
                    entity_type=etype,
                    label=label,
                    embedding_text=embed_text,
                    embedding=embedding,
                    provider=provider.provider_name,
                    model_name=provider.model_id,
                    deprecated=deprecated,
                )
            )

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
        if Vector is None:
            raise RuntimeError(
                "pgvector is not installed. Semantic search requires the pgvector extension."
            )
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

        # pgvector cosine distance: <=> returns distance (0=identical), score = 1 - distance.
        # NOTE: must use CAST(:query_vec AS vector) — SQLAlchemy's text() parser silently
        # drops :name bindparams when immediately followed by ::type (Postgres cast syntax),
        # producing a syntax error at the literal ":" in the wire SQL.
        query_str = text("""
            SELECT entity_iri, label, entity_type, deprecated,
                   1 - (embedding <=> CAST(:query_vec AS vector)) AS score
            FROM entity_embeddings
            WHERE project_id = :pid AND branch = :br
            ORDER BY embedding <=> CAST(:query_vec AS vector)
            LIMIT :lim
        """)

        result = await self._db.execute(
            query_str,
            {
                "query_vec": _vec_to_str(query_vec),
                "pid": str(project_id),
                "br": branch,
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
        if Vector is None:
            raise RuntimeError(
                "pgvector is not installed. Semantic search requires the pgvector extension."
            )

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

        # pgvector cosine distance across ALL branches (no branch = :br filter).
        # See note in semantic_search() above re: CAST() vs ::vector.
        exclusions = ""
        params: dict[str, object] = {
            "query_vec": _vec_to_str(query_vec),
            "pid": str(project_id),
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

        query_str = text(f"""
            SELECT entity_iri, label, entity_type, branch, deprecated,
                   1 - (embedding <=> CAST(:query_vec AS vector)) AS score
            FROM entity_embeddings
            WHERE project_id = :pid
              AND (1 - (embedding <=> CAST(:query_vec AS vector))) >= :threshold
              {exclusions}
            ORDER BY embedding <=> CAST(:query_vec AS vector)
            LIMIT :lim
        """)  # nosec B608 -- only fixed SQL fragments and generated bind names are interpolated

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
        if Vector is None:
            raise RuntimeError(
                "pgvector is not installed. Similarity search requires the pgvector extension."
            )
        # Get entity's embedding
        emb_q = select(EntityEmbedding).where(
            EntityEmbedding.project_id == project_id,
            EntityEmbedding.branch == branch,
            EntityEmbedding.entity_iri == entity_iri,
        )
        emb = (await self._db.execute(emb_q)).scalar_one_or_none()
        if not emb:
            return []

        # kNN search excluding self. See note in semantic_search() above re: CAST() vs ::vector.
        query_str = text("""
            SELECT entity_iri, label, entity_type, deprecated,
                   1 - (embedding <=> CAST(:query_vec AS vector)) AS score
            FROM entity_embeddings
            WHERE project_id = :pid AND branch = :br AND entity_iri != :self_iri
            ORDER BY embedding <=> CAST(:query_vec AS vector)
            LIMIT :lim
        """)

        result = await self._db.execute(
            query_str,
            {
                "query_vec": _vec_to_str(emb.embedding),
                "pid": str(project_id),
                "br": branch,
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
        if Vector is None:
            raise RuntimeError(
                "pgvector is not installed. Ranking suggestions requires the pgvector extension."
            )
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

        # Get candidate embeddings
        candidates_q = select(EntityEmbedding).where(
            EntityEmbedding.project_id == project_id,
            EntityEmbedding.branch == resolved_branch,
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
