"""Tests for OWL reasoner and pre-commit validation — Plan 02 (TOOL-03, TOOL-04)."""
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from ontokit.services.reasoner_service import ReasonerResult, ReasonerService

# Minimal valid OWL/XML ontology (no classes, no cycles)
MINIMAL_OWL = """<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Ontology rdf:about="http://example.org/test"/>
</rdf:RDF>"""

# OWL/XML with A subClassOf B and B subClassOf A (a cycle)
OWL_WITH_CYCLE = """<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Ontology rdf:about="http://example.org/cycle-test"/>
  <owl:Class rdf:about="http://example.org/ClassA">
    <rdfs:subClassOf rdf:resource="http://example.org/ClassB"/>
  </owl:Class>
  <owl:Class rdf:about="http://example.org/ClassB">
    <rdfs:subClassOf rdf:resource="http://example.org/ClassA"/>
  </owl:Class>
</rdf:RDF>"""


def test_reasoner_loads_owl_file():
    """owlready2 loads an OWL/XML file without errors and returns consistent=True (TOOL-03)."""
    service = ReasonerService()
    result = service.check_consistency(MINIMAL_OWL)

    assert isinstance(result, ReasonerResult)
    assert result.consistent is True
    # Should use owlready2 if available, rdflib_fallback otherwise
    assert result.reasoner_used in ("owlready2", "rdflib_fallback")
    assert isinstance(result.issues, list)


def test_reasoner_detects_cycle():
    """OWL reasoner reports a hierarchy_cycle error when A subClassOf B and B subClassOf A (TOOL-03)."""
    service = ReasonerService()
    result = service.check_consistency(OWL_WITH_CYCLE)

    cycle_issues = [i for i in result.issues if i.rule_id == "hierarchy_cycle"]
    assert len(cycle_issues) > 0, (
        f"Expected hierarchy_cycle issues but got: {[i.rule_id for i in result.issues]}"
    )
    # The ontology with a cycle should not be considered consistent
    assert result.consistent is False


def test_pre_commit_validation_endpoint():
    """POST /api/v1/projects/{id}/validate accepts owl_content and returns a validation result (TOOL-04)."""
    from ontokit.main import app

    project_id = uuid4()

    # Mock the reasoner dependency to return a predictable result
    mock_result = ReasonerResult(consistent=True, issues=[], reasoner_used="test_mock")

    with patch(
        "ontokit.api.routes.validation.ReasonerService.check_consistency",
        return_value=mock_result,
    ):
        client = TestClient(app, raise_server_exceptions=True)
        response = client.post(
            f"/api/v1/projects/{project_id}/validate",
            json={"owl_content": MINIMAL_OWL},
        )

    assert response.status_code == 200
    data = response.json()
    assert "consistent" in data
    assert data["consistent"] is True
    assert "issues" in data
    assert "reasoner_used" in data


def test_pre_commit_validates_provided_content():
    """POST /api/v1/projects/{id}/validate with cyclic OWL returns consistent=False (TOOL-04 pre-commit check).

    This test proves the endpoint validates the provided owl_content directly,
    not the stored content — the core requirement of TOOL-04.
    """
    from ontokit.main import app

    project_id = uuid4()

    # Use the real ReasonerService (no mocking) to validate cyclic OWL content
    client = TestClient(app, raise_server_exceptions=True)
    response = client.post(
        f"/api/v1/projects/{project_id}/validate",
        json={"owl_content": OWL_WITH_CYCLE},
    )

    assert response.status_code == 200
    data = response.json()
    assert "consistent" in data
    assert data["consistent"] is False, (
        "Cyclic OWL content should be flagged as inconsistent"
    )
    cycle_issue_ids = [i["rule_id"] for i in data["issues"]]
    assert "hierarchy_cycle" in cycle_issue_ids, (
        f"Expected hierarchy_cycle in issue rule_ids but got: {cycle_issue_ids}"
    )
