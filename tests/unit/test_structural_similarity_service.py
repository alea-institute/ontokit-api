"""Contract tests for the optional folio-python structural signal."""

from collections.abc import Generator
from types import ModuleType

import pytest

from ontokit.services import structural_similarity_service as structural


@pytest.fixture(autouse=True)
def _clear_folio_instances() -> Generator[None]:
    structural.clear_folio_cache()
    yield
    structural.clear_folio_cache()


def test_pinned_folio_normalizes_current_and_legacy_iris() -> None:
    """The pinned release retains the normalization API used by its graph traversal."""
    from folio.graph import FOLIO  # type: ignore[import-untyped]

    assert FOLIO.normalize_iri("folio:Actor") == "https://folio.openlegalstandard.org/Actor"
    assert FOLIO.normalize_iri("soli:Actor") == "https://folio.openlegalstandard.org/Actor"
    assert FOLIO.normalize_iri("https://folio.openlegalstandard.org/Actor") == (
        "https://folio.openlegalstandard.org/Actor"
    )


def test_factory_constructs_folio_with_supported_cache_option(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lazy factory invokes the verified folio-python 0.4.0 constructor contract."""
    calls: list[bool] = []

    class FakeFOLIO:
        def __init__(self, *, use_cache: bool) -> None:
            calls.append(use_cache)

    fake_graph = ModuleType("folio.graph")
    fake_graph.FOLIO = FakeFOLIO  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "folio.graph", fake_graph)

    instance = structural._get_folio_instance()
    assert isinstance(instance, FakeFOLIO)
    assert structural._get_folio_instance() is instance
    assert calls == [True]


def test_similarity_uses_folio_parent_graph() -> None:
    """Structural scoring consumes the parent objects returned by FOLIO."""
    calls: list[tuple[str, int]] = []

    class Concept:
        def __init__(self, iri: str) -> None:
            self.iri = iri

    class FakeFOLIO:
        def get_parents(self, iri: str, max_depth: int) -> list[Concept]:
            calls.append((iri, max_depth))
            parents = {
                "folio:Contract": [Concept("folio:Agreement"), Concept("folio:LegalThing")],
                "soli:Lease": [Concept("folio:Agreement"), Concept("folio:AssetUse")],
            }
            return parents[iri]

    structural._folio_cache["default"] = FakeFOLIO()

    score = structural.StructuralSimilarityService().try_compute_similarity(
        "folio:Contract", "soli:Lease", max_depth=4
    )

    assert score == pytest.approx(1 / 3)
    assert calls == [("folio:Contract", 4), ("soli:Lease", 4)]


def test_unavailable_folio_preserves_graceful_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A dependency load/initialization failure remains a missing, not zero, signal."""

    class UnavailableFOLIO:
        def __init__(self, *, use_cache: bool) -> None:
            del use_cache
            raise RuntimeError("ontology source unavailable")

    fake_graph = ModuleType("folio.graph")
    fake_graph.FOLIO = UnavailableFOLIO  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "folio.graph", fake_graph)

    service = structural.StructuralSimilarityService()

    assert service.try_compute_similarity("folio:A", "folio:B") is None
    assert service.compute_similarity("folio:A", "folio:B") == 0.0
    assert service.get_structural_context("folio:A") == {"parents": [], "siblings": []}
    assert "structural similarity disabled" in caplog.text
