"""Contract tests for the immutable two-repository release manifest."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = ROOT / "deploy" / "validate_release_manifest.py"

API_SHA = "a" * 40
WEB_SHA = "b" * 40


def _write_manifest(tmp_path: Path, **overrides: object) -> Path:
    manifest: dict[str, object] = {
        "schema_version": 1,
        "api_repository": "alea-institute/ontokit-api",
        "api_sha": API_SHA,
        "web_repository": "alea-institute/ontokit-web",
        "web_sha": WEB_SHA,
    }
    manifest.update(overrides)
    path = tmp_path / "release-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _run(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), str(path), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def test_full_immutable_matched_pair_is_accepted(tmp_path: Path) -> None:
    result = _run(
        _write_manifest(tmp_path),
        "--expect-api-sha",
        API_SHA,
        "--expect-web-sha",
        WEB_SHA,
    )

    assert result.returncode == 0, result.stderr
    assert f"api_sha={API_SHA}" in result.stdout
    assert f"web_sha={WEB_SHA}" in result.stdout


def test_mutable_revision_is_refused(tmp_path: Path) -> None:
    result = _run(_write_manifest(tmp_path, api_sha="feat/pr-party"))

    assert result.returncode != 0
    assert "full lowercase 40-character commit SHA" in result.stderr


def test_pair_mismatch_is_refused(tmp_path: Path) -> None:
    result = _run(
        _write_manifest(tmp_path),
        "--expect-api-sha",
        "c" * 40,
        "--expect-web-sha",
        WEB_SHA,
    )

    assert result.returncode != 0
    assert "API SHA does not match" in result.stderr


def test_repository_substitution_is_refused(tmp_path: Path) -> None:
    result = _run(_write_manifest(tmp_path, web_repository="attacker/example"))

    assert result.returncode != 0
    assert "unexpected web repository" in result.stderr


def test_prod_workflow_promotes_only_manifest_outputs_after_smoke() -> None:
    workflow = (ROOT / ".github" / "workflows" / "promote-prod.yml").read_text(encoding="utf-8")

    assert "needs: [validate-release, smoke-dev, promotion-gate]" in workflow
    assert "API_SHA: ${{ needs.validate-release.outputs.api_sha }}" in workflow
    assert "WEB_SHA: ${{ needs.validate-release.outputs.web_sha }}" in workflow
    assert "if: vars.PROD_ENABLED == 'true'" in workflow
    assert "environment: production" in workflow
    assert "environment: dev-smoke" in workflow
    assert "PROD_ENVIRONMENT_READY" in workflow
    assert "bash deploy/check-prod-gate.sh" in workflow
    assert "SOURCE_RUN_ID: ${{ github.event.workflow_run.id }}" in workflow
    assert "did not execute a successful deploy job" in workflow
    assert "workflow_dispatch" not in workflow
    assert "value=latest" not in workflow.lower()
    assert "commits/feat%2fpr-party" not in workflow.lower()


def test_dev_deploy_has_no_runtime_revision_inputs_or_branch_head_fallback() -> None:
    workflow = (ROOT / ".github" / "workflows" / "deploy-dev.yml").read_text(encoding="utf-8")

    assert "api_sha:" not in workflow
    assert "web_sha:" not in workflow
    assert "validate_release_manifest.py" in workflow
    assert "commits/feat%2Fpr-party" not in workflow
