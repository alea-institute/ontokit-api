"""Guard against the return of OntologyService stub methods (R7).

The legacy ontology routers were replaced by the project-scoped API; the
service methods they used only raised ``NotImplementedError``. They were
deleted rather than implemented, so no code path exists only to raise.
"""

import ast
import importlib
import inspect
from pathlib import Path

import pytest

import ontokit
from ontokit.services.ontology import OntologyService

REMOVED_METHODS = [
    "create",
    "list_all",
    "get",
    "update",
    "delete",
    "import_from_file",
    "get_history",
    "diff",
    "list_classes",
    "create_class",
    "update_class",
    "delete_class",
    "get_class_hierarchy",
    "list_properties",
    "create_property",
    "get_property",
    "update_property",
    "delete_property",
]

PACKAGE_ROOT = Path(ontokit.__file__).resolve().parent


@pytest.mark.parametrize("name", REMOVED_METHODS)
def test_stub_method_is_gone(name: str) -> None:
    """Each deleted stub (and the uncalled create_class/list_classes) stays deleted."""
    assert not hasattr(OntologyService, name)


def test_ontology_service_never_raises_not_implemented() -> None:
    """No OntologyService method body raises NotImplementedError."""
    tree = ast.parse(inspect.getsource(OntologyService))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and node.exc is not None:
            target = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            if isinstance(target, ast.Name) and target.id == "NotImplementedError":
                offenders.append(node.lineno)
    assert offenders == []


def test_no_integration_pending_placeholders_in_package() -> None:
    """No module under ontokit/ carries a '... integration pending' placeholder."""
    hits = [
        str(path.relative_to(PACKAGE_ROOT))
        for path in PACKAGE_ROOT.rglob("*.py")
        if "integration pending" in path.read_text(encoding="utf-8")
    ]
    assert hits == []


def test_unused_stub_schemas_are_gone() -> None:
    """The schemas that only the stubs used are removed; LocalizedString remains."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("ontokit.schemas.owl_property")

    ontology_schemas = importlib.import_module("ontokit.schemas.ontology")
    for name in (
        "OntologyCreate",
        "OntologyUpdate",
        "OntologyResponse",
        "OntologyListResponse",
    ):
        assert not hasattr(ontology_schemas, name)
    assert hasattr(ontology_schemas, "LocalizedString")
