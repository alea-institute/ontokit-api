"""Release manifest contracts: schema 1 source pairs and schema 2 image pairs."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from deploy.validate_release_manifest import validate

ROOT = Path(__file__).parents[2]
VALIDATOR = ROOT / "deploy" / "validate_release_manifest.py"
API_SHA = "a" * 40
WEB_SHA = "b" * 40
API_DIGEST = "sha256:" + "c" * 64
WEB_DIGEST = "sha256:" + "d" * 64


def _schema1() -> dict[str, object]:
    return {
        "schema_version": 1,
        "api_repository": "alea-institute/ontokit-api",
        "api_sha": API_SHA,
        "web_repository": "alea-institute/ontokit-web",
        "web_sha": WEB_SHA,
    }


def _schema2() -> dict[str, object]:
    return {
        **_schema1(),
        "schema_version": 2,
        "api_image": f"ghcr.io/alea-institute/ontokit@{API_DIGEST}",
        "web_image": f"ghcr.io/alea-institute/ontokit-web@{WEB_DIGEST}",
    }


def _refused(manifest: dict[str, object], **kwargs: object) -> None:
    with pytest.raises(SystemExit) as excinfo:
        validate(manifest, None, None, **kwargs)  # type: ignore[arg-type]
    assert excinfo.value.code == 64


def test_committed_manifest_is_valid() -> None:
    manifest = json.loads((ROOT / "deploy" / "release-manifest.json").read_text())
    release = validate(manifest, None, None)
    assert release["schema_version"] in {"1", "2"}


def test_schema1_emits_empty_digests() -> None:
    assert validate(_schema1(), API_SHA, WEB_SHA) == {
        "schema_version": "1",
        "api_sha": API_SHA,
        "web_sha": WEB_SHA,
        "api_digest": "",
        "web_digest": "",
    }


def test_schema2_emits_both_digests() -> None:
    release = validate(_schema2(), None, None)
    assert release["schema_version"] == "2"
    assert release["api_digest"] == API_DIGEST
    assert release["web_digest"] == WEB_DIGEST


@pytest.mark.parametrize("missing", ["api_image", "web_image"])
def test_schema2_refuses_a_mixed_pair(missing: str) -> None:
    manifest = _schema2()
    del manifest[missing]
    _refused(manifest)


def test_schema1_refuses_image_keys() -> None:
    manifest = _schema1()
    manifest["api_image"] = f"ghcr.io/alea-institute/ontokit@{API_DIGEST}"
    _refused(manifest)


@pytest.mark.parametrize(
    "reference",
    [
        "ghcr.io/alea-institute/ontokit:sha-" + "a" * 40,  # mutable tag
        "ghcr.io/alea-institute/ontokit:latest@" + API_DIGEST,  # tag plus digest
        "docker.io/alea-institute/ontokit@" + API_DIGEST,  # other registry
        "ghcr.io/someone-else/ontokit@" + API_DIGEST,  # other owner
        "ghcr.io/alea-institute/ontokit-web@" + API_DIGEST,  # web image in API slot
        "ghcr.io/alea-institute/ontokit@sha256:" + "C" * 64,  # uppercase digest
        "ghcr.io/alea-institute/ontokit@sha256:" + "c" * 63,  # short digest
        "ghcr.io/alea-institute/ontokit@sha512:" + "c" * 128,  # other algorithm
        "ghcr.io/alea-institute/ontokit@" + API_DIGEST + " ; reboot",  # injection
    ],
)
def test_schema2_refuses_unpinned_or_foreign_images(reference: str) -> None:
    manifest = _schema2()
    manifest["api_image"] = reference
    _refused(manifest)


def test_schema2_refuses_non_string_image() -> None:
    manifest = _schema2()
    manifest["web_image"] = ["ghcr.io/alea-institute/ontokit-web", WEB_DIGEST]
    _refused(manifest)


@pytest.mark.parametrize("schema", [0, 3, "2", True, None, 1.0, 2.0])
def test_unknown_schema_versions_are_refused(schema: object) -> None:
    manifest = _schema2()
    manifest["schema_version"] = schema
    _refused(manifest)


def test_required_schema_refuses_the_other_schema() -> None:
    _refused(_schema2(), allowed_schemas=(1,))
    assert validate(_schema1(), None, None, allowed_schemas=(1,))["schema_version"] == "1"


def test_cli_writes_github_outputs(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(_schema2()))
    output = tmp_path / "github-output"
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), str(manifest), "--github-output", str(output)],
        capture_output=True,
        text=True,
        check=True,
    )
    expected = (
        f"schema_version=2\napi_sha={API_SHA}\nweb_sha={WEB_SHA}\n"
        f"api_digest={API_DIGEST}\nweb_digest={WEB_DIGEST}\n"
    )
    assert result.stdout == expected
    assert output.read_text() == expected


def test_cli_require_schema_refuses_schema2(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(_schema2()))
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), str(manifest), "--require-schema", "1"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 64
    assert "schema_version must be 1" in result.stderr
