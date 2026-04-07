"""OntologyContextAssembler — gathers ontology context for LLM prompt generation.

Assembles ~2-4K tokens of ontology context (current class, parents, siblings,
existing children) for use in all suggestion type prompt templates (GEN-06).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.services.ontology_index import OntologyIndexService


class OntologyContextAssembler:
    """Assembles ~2-4K tokens of ontology context for LLM prompt generation.

    Shared utility across all suggestion types (per D-03 in the architecture).
    Uses OntologyIndexService to fetch class details, parents, siblings, and
    existing children from the PostgreSQL ontology index.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._index = OntologyIndexService(db)

    async def assemble(
        self,
        project_id: UUID,
        branch: str,
        class_iri: str,
        max_siblings: int = 10,
        max_ancestor_annotations: int = 3,
    ) -> dict:
        """Assemble ontology context for LLM prompt generation.

        Args:
            project_id: Project UUID.
            branch: Git branch name.
            class_iri: IRI of the class to assemble context for.
            max_siblings: Maximum number of sibling classes to include.
            max_ancestor_annotations: Max annotations to include per ancestor.

        Returns:
            Dict with keys:
                - current_class: {iri, labels, annotations}
                - parents: [{iri, label, annotations}]
                - siblings: [{iri, label}]
                - existing_children: [{iri, label}]

        Raises:
            ValueError: If the class_iri is not found in the index.
        """
        # Step 1: Fetch current class detail
        detail = await self._index.get_class_detail(project_id, branch, class_iri)
        if detail is None:
            raise ValueError(
                f"Class {class_iri!r} not found in ontology index for "
                f"project {project_id} branch {branch!r}"
            )

        # Step 2: Fetch parent details (up to 3 parents)
        parent_iris: list[str] = detail.get("parent_iris", []) or []
        parents = []
        for parent_iri in parent_iris[:3]:
            parent_detail = await self._index.get_class_detail(
                project_id, branch, parent_iri
            )
            if parent_detail is not None:
                parent_labels = parent_detail.get("labels", [])
                # Use the first label value as the display label
                label = (
                    parent_labels[0]["value"]
                    if parent_labels
                    else parent_iri.rsplit("/", 1)[-1].rsplit("#", 1)[-1]
                )
                # Cap annotations per ancestor
                annotations = (parent_detail.get("annotations") or [])[:max_ancestor_annotations]
                parents.append(
                    {
                        "iri": parent_iri,
                        "label": label,
                        "annotations": annotations,
                    }
                )
            else:
                # Parent not indexed — use label from parent_labels map if available
                parent_labels_map: dict[str, str] = detail.get("parent_labels", {}) or {}
                label = parent_labels_map.get(parent_iri) or (
                    parent_iri.rsplit("/", 1)[-1].rsplit("#", 1)[-1]
                )
                parents.append(
                    {
                        "iri": parent_iri,
                        "label": label,
                        "annotations": [],
                    }
                )

        # Step 3: Get siblings — children of the first parent, excluding self
        siblings: list[dict] = []
        if parent_iris:
            primary_parent_iri = parent_iris[0]
            raw_siblings = await self._index.get_class_children(
                project_id, branch, primary_parent_iri
            )
            # Exclude current class and cap at max_siblings
            siblings = [
                {"iri": s["iri"], "label": s["label"]}
                for s in raw_siblings
                if s["iri"] != class_iri
            ][:max_siblings]

        # Step 4: Get existing children
        raw_children = await self._index.get_class_children(
            project_id, branch, class_iri
        )
        existing_children = [
            {"iri": c["iri"], "label": c["label"]} for c in raw_children
        ]

        return {
            "current_class": {
                "iri": class_iri,
                "labels": detail.get("labels", []),
                "annotations": detail.get("annotations", []),
            },
            "parents": parents,
            "siblings": siblings,
            "existing_children": existing_children,
        }
