"""Distribution workflow safety contracts."""

from pathlib import Path

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
