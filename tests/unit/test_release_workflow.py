"""Distribution workflow safety contracts."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml


def test_production_image_is_built_before_any_tag_publication() -> None:
    workflow_path = Path(__file__).parents[2] / ".github" / "workflows" / "release.yml"
    workflow = yaml.safe_load(workflow_path.read_text())
    jobs = workflow["jobs"]

    preflight = jobs["docker_preflight"]
    image_build = next(
        step
        for step in preflight["steps"]
        if str(step.get("uses", "")).startswith("docker/build-push-action@")
    )
    assert image_build["with"]["file"] == "Dockerfile.prod"
    assert image_build["with"]["push"] is False

    assert "docker_preflight" in jobs["publish_docker"]["needs"]
    for publisher in ("publish_pypi", "publish_github"):
        assert "publish_docker" in jobs[publisher]["needs"]


def test_dev_deploy_requires_an_explicit_host() -> None:
    workflow_path = Path(__file__).parents[2] / ".github" / "workflows" / "deploy-dev.yml"
    workflow = yaml.safe_load(workflow_path.read_text())
    deploy = workflow["jobs"]["deploy"]
    assert deploy["env"]["DEPLOY_HOST"] == "${{ vars.DEV_DEPLOY_HOST }}"
    assert deploy["environment"] == "dev-deploy"
    steps = deploy["steps"]
    names = [step["name"] for step in steps]
    assert names.index("Validate deployment host") < names.index("Install SSH credentials")
    assert names.index("Validate deployment host") < names.index("Deploy approved pair")
    assert "StrictHostKeyChecking=yes" in steps[names.index("Deploy approved pair")]["run"]


@pytest.mark.parametrize(
    "host",
    [
        None,
        "",
        " ",
        "\t\n",
        "-oProxyCommand=bad",
        "root@example.com",
        "host;id",
        "$(id)",
        "host name",
        "host\nother",
        "host/path",
        "-bad.example",
        "bad..example",
    ],
)
def test_dev_deploy_rejects_missing_or_malformed_host(host: str | None) -> None:
    result = _validate_dev_host(host)
    assert result.returncode != 0
    assert "DEV_DEPLOY_HOST" in result.stderr


@pytest.mark.parametrize("host", ["192.0.2.1", "dev.example.org", "dev-01", "2001:db8::1"])
def test_dev_deploy_accepts_explicit_host(host: str) -> None:
    assert _validate_dev_host(host).returncode == 0


def _validate_dev_host(host: str | None) -> subprocess.CompletedProcess[str]:
    workflow_path = Path(__file__).parents[2] / ".github" / "workflows" / "deploy-dev.yml"
    workflow = yaml.safe_load(workflow_path.read_text())
    step = next(
        step
        for step in workflow["jobs"]["deploy"]["steps"]
        if step["name"] == "Validate deployment host"
    )
    env = os.environ.copy()
    env.pop("DEPLOY_HOST", None)
    if host is not None:
        env["DEPLOY_HOST"] = host
    return subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_dev_publication_is_push_only_and_preserves_tag_releases() -> None:
    workflow_path = Path(__file__).parents[2] / ".github" / "workflows" / "release.yml"
    jobs = yaml.safe_load(workflow_path.read_text())["jobs"]
    assert jobs["publish_docker"]["if"] == (
        "github.event_name == 'push' && (github.ref == 'refs/heads/dev' || "
        "startsWith(github.ref, 'refs/tags/ontokit-'))"
    )
    for publisher in ("publish_pypi", "publish_github"):
        assert jobs[publisher]["if"] == (
            "github.event_name == 'push' && "
            "startsWith(github.event.ref, 'refs/tags/ontokit-')"
        )
    assert set(jobs["publish_docker"]["needs"]) == {"lint", "test", "docker_preflight"}
    assert jobs["publish_docker"]["permissions"] == {"contents": "read", "packages": "write"}


def test_dev_image_has_full_revision_and_cannot_replace_latest() -> None:
    workflow_path = Path(__file__).parents[2] / ".github" / "workflows" / "release.yml"
    steps = yaml.safe_load(workflow_path.read_text())["jobs"]["publish_docker"]["steps"]
    meta = next(step for step in steps if step.get("id") == "meta")["with"]
    assert "type=raw,value=sha-${{ github.sha }}" in meta["tags"].splitlines()
    assert meta["flavor"] == "latest=false"
    assert (
        "type=raw,value=latest,enable=${{ startsWith(github.ref, 'refs/tags/ontokit-') }}"
        in meta["tags"].splitlines()
    )
    assert "org.opencontainers.image.revision=${{ github.sha }}" in meta["labels"]
    build = next(step for step in steps if step.get("id") == "publish")
    assert build["with"]["push"] is True
    assert build["with"]["file"] == "Dockerfile.prod"
    receipt = next(step for step in steps if step.get("name") == "Record published image")
    assert receipt["env"]["IMAGE_DIGEST"] == "${{ steps.publish.outputs.digest }}"
    assert receipt["env"]["SOURCE_REVISION"] == "${{ github.sha }}"
    assert "$GITHUB_STEP_SUMMARY" in receipt["run"]
