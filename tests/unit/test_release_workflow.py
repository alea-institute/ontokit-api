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


def _run_dev_deploy_step(tmp_path: Path, outputs: dict[str, str]) -> tuple[int, str]:
    """Run the real deploy step with ssh replaced by a stub that records its command."""
    workflow_path = Path(__file__).parents[2] / ".github" / "workflows" / "deploy-dev.yml"
    workflow = yaml.safe_load(workflow_path.read_text())
    step = next(
        step
        for step in workflow["jobs"]["deploy"]["steps"]
        if step["name"] == "Deploy approved pair"
    )
    assert step["env"] == {
        name: f"${{{{ steps.pair.outputs.{output} }}}}"
        for name, output in (
            ("SCHEMA_VERSION", "schema_version"),
            ("API_SHA", "api_sha"),
            ("WEB_SHA", "web_sha"),
            ("API_DIGEST", "api_digest"),
            ("WEB_DIGEST", "web_digest"),
        )
    }
    record = tmp_path / "ssh-command"
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    ssh = stub_dir / "ssh"
    ssh.write_text(f'#!/bin/sh\nfor arg; do last=$arg; done\nprintf %s "$last" >"{record}"\n')
    ssh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        "DEPLOY_HOST": "dev.example.org",
        "DEPLOY_KEY_FILE": str(tmp_path / "key"),
        "DEPLOY_KNOWN_HOSTS_FILE": str(tmp_path / "known_hosts"),
        **outputs,
    }
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, record.read_text() if record.exists() else ""


DEPLOY_OUTPUTS = {
    "API_SHA": "a" * 40,
    "WEB_SHA": "b" * 40,
    "API_DIGEST": "sha256:" + "c" * 64,
    "WEB_DIGEST": "sha256:" + "d" * 64,
}


def test_dev_deploy_schema1_requests_a_source_build(tmp_path: Path) -> None:
    code, command = _run_dev_deploy_step(
        tmp_path, {**DEPLOY_OUTPUTS, "SCHEMA_VERSION": "1", "API_DIGEST": "", "WEB_DIGEST": ""}
    )
    assert code == 0
    assert command == f"deploy {'a' * 40} {'b' * 40}"


def test_dev_deploy_schema2_requests_the_published_digests(tmp_path: Path) -> None:
    code, command = _run_dev_deploy_step(tmp_path, {**DEPLOY_OUTPUTS, "SCHEMA_VERSION": "2"})
    assert code == 0
    assert command == f"deploy-images {'a' * 40} {'b' * 40} sha256:{'c' * 64} sha256:{'d' * 64}"


def test_dev_deploy_refuses_unknown_schema(tmp_path: Path) -> None:
    code, command = _run_dev_deploy_step(tmp_path, {**DEPLOY_OUTPUTS, "SCHEMA_VERSION": "3"})
    assert code != 0
    assert command == ""


def test_prod_promotion_accepts_only_source_manifests() -> None:
    workflow_path = Path(__file__).parents[2] / ".github" / "workflows" / "promote-prod.yml"
    steps = yaml.safe_load(workflow_path.read_text())["jobs"]["validate-release"]["steps"]
    validation = next(step for step in steps if step.get("id") == "release")
    assert "--require-schema 1" in validation["run"]


def test_published_image_is_smoke_tested_and_deploy_contract_runs() -> None:
    workflow_path = Path(__file__).parents[2] / ".github" / "workflows" / "release.yml"
    jobs = yaml.safe_load(workflow_path.read_text())["jobs"]
    preflight = jobs["docker_preflight"]["steps"]
    prod_build = next(
        step
        for step in preflight
        if str(step.get("uses", "")).startswith("docker/build-push-action@")
        and step["with"]["file"] == "Dockerfile.prod"
    )
    assert prod_build["with"]["load"] is True
    smoke_runs = [step.get("run", "") for step in preflight]
    assert f"bash scripts/smoke-runtime-image.sh {prod_build['with']['tags']}" in smoke_runs
    assert "deploy/tests/*.sh" in jobs["deploy_contract"]["steps"][-1]["run"]
