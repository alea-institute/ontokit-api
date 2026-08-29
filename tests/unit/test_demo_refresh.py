"""Security and sequencing tests for the demo repository refresh job."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from deploy import refresh_demo_repositories as refresh
from ontokit.services.demo_project_provisioning import build_demo_generation_key


def _git(*arguments: str, cwd: Path | None = None) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _manifest(tmp_path: Path, **mirror_override: str) -> Path:
    mirrors = [
        {
            "name": "folio",
            "source_repository": "alea-institute/FOLIO",
            "destination_repository": "alea-institute/ontokit-demo-folio",
            "default_branch": "main",
        },
        {
            "name": "semantic-canon",
            "source_repository": "CatholicOS/ontology-semantic-canon",
            "destination_repository": "alea-institute/ontokit-demo-semantic-canon",
            "default_branch": "main",
        },
    ]
    mirrors[0].update(mirror_override)
    path = tmp_path / "mirrors.json"
    path.write_text(json.dumps({"schema_version": 1, "mirrors": mirrors}), encoding="utf-8")
    return path


def test_manifest_accepts_only_the_two_approved_routes(tmp_path: Path) -> None:
    mirrors = refresh.load_manifest(_manifest(tmp_path))
    assert [mirror.name for mirror in mirrors] == ["folio", "semantic-canon"]

    with pytest.raises(SystemExit):
        refresh.load_manifest(
            _manifest(tmp_path, destination_repository="alea-institute/unapproved-target")
        )


def test_tokens_must_exist_and_be_distinct() -> None:
    with pytest.raises(SystemExit):
        refresh.require_tokens({})
    with pytest.raises(SystemExit):
        refresh.require_tokens(
            {
                refresh.SOURCE_TOKEN_ENV: "same-token",
                refresh.DESTINATION_TOKEN_ENV: "same-token",
            }
        )
    assert refresh.require_tokens(
        {
            refresh.SOURCE_TOKEN_ENV: "read-only-token",
            refresh.DESTINATION_TOKEN_ENV: "two-repo-write-token",
        }
    ) == ("read-only-token", "two-repo-write-token")


def test_git_credentials_use_askpass_not_command_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(refresh.SOURCE_TOKEN_ENV, "source-token")
    monkeypatch.setenv(refresh.DESTINATION_TOKEN_ENV, "destination-token")
    environment = refresh.git_environment("secret-token", tmp_path / "askpass")

    assert environment["DEMO_GIT_TOKEN"] == "secret-token"
    assert refresh.SOURCE_TOKEN_ENV not in environment
    assert refresh.DESTINATION_TOKEN_ENV not in environment
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert environment["GIT_CONFIG_VALUE_0"] == ""
    assert environment["GIT_HTTP_LOW_SPEED_LIMIT"] == str(
        refresh.GIT_LOW_SPEED_LIMIT_BYTES_PER_SECOND
    )
    assert environment["GIT_HTTP_LOW_SPEED_TIME"] == str(refresh.GIT_LOW_SPEED_TIME_SECONDS)
    assert "secret-token" not in refresh.repository_url("alea-institute/FOLIO")


def test_command_timeout_refuses_with_clear_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def time_out(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise subprocess.TimeoutExpired(cmd=["git", "clone"], timeout=0.25)

    monkeypatch.setattr(subprocess, "run", time_out)

    with pytest.raises(SystemExit) as exc:
        refresh.run_command(["git", "clone"], timeout_seconds=0.25)

    assert exc.value.code == 64
    assert "refused: git command timed out after 0.25 seconds" in capsys.readouterr().err


def test_resync_environment_never_receives_git_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(refresh.SOURCE_TOKEN_ENV, "read-only-token")
    monkeypatch.setenv(refresh.DESTINATION_TOKEN_ENV, "write-token")
    monkeypatch.setenv("DEMO_GIT_TOKEN", "transient-token")

    environment = refresh.scrubbed_environment()

    assert refresh.SOURCE_TOKEN_ENV not in environment
    assert refresh.DESTINATION_TOKEN_ENV not in environment
    assert "DEMO_GIT_TOKEN" not in environment


def test_destination_push_updates_only_the_default_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[tuple[list[str], str | None]] = []
    checkout = tmp_path / "folio"

    def fake_run(
        command: list[str] | tuple[str, ...],
        *,
        cwd: Path | None = None,
        environment: object = None,
    ) -> None:
        del environment
        commands.append((list(command), None if cwd is None else str(cwd)))
        if command[:2] == ["git", "clone"]:
            checkout.mkdir()

    class RevisionResult:
        stdout = "a" * 40 + "\n"

    def fake_revision_run(*args: object, **kwargs: object) -> RevisionResult:
        del args, kwargs
        return RevisionResult()

    monkeypatch.setattr(refresh, "run_command", fake_run)
    monkeypatch.setattr(subprocess, "run", fake_revision_run)
    mirror = refresh.load_manifest(_manifest(tmp_path))[0]

    destination_sha = refresh.refresh_one(
        mirror,
        tmp_path,
        tmp_path / "askpass",
        "read-token",
        "write-token",
        "2026-08-20T12:00:00+00:00",
    )

    assert destination_sha == "a" * 40

    push = next(command for command, _ in commands if command[:2] == ["git", "push"])
    assert push[-1] == "HEAD:refs/heads/main"
    assert "--force" in push
    assert "--mirror" not in push
    assert "--all" not in push


def test_refresh_hands_exact_complete_generation_to_resync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = _manifest(tmp_path)
    resync = tmp_path / "resync"
    resync.write_text("#!/bin/sh\n", encoding="utf-8")
    resync.chmod(0o700)
    lock = tmp_path / "refresh.lock"
    mirrors = refresh.load_manifest(manifest_path)
    commits = {
        "alea-institute/ontokit-demo-folio": "a" * 40,
        "alea-institute/ontokit-demo-semantic-canon": "b" * 40,
    }
    commands: list[list[str]] = []

    monkeypatch.setenv(refresh.SOURCE_TOKEN_ENV, "source-token")
    monkeypatch.setenv(refresh.DESTINATION_TOKEN_ENV, "destination-token")
    monkeypatch.setattr(
        refresh,
        "refresh_one",
        lambda mirror, *_args: commits[mirror.destination_repository],
    )
    monkeypatch.setattr(
        refresh,
        "run_command",
        lambda command, **_kwargs: commands.append(list(command)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "refresh-demo",
            "--manifest",
            str(manifest_path),
            "--resync-executable",
            str(resync),
            "--lock-file",
            str(lock),
        ],
    )

    assert len(mirrors) == 2
    refresh.main()

    assert commands == [
        [
            str(resync),
            str(manifest_path),
            "--generation-key",
            build_demo_generation_key(commits),
        ]
    ]


def test_real_refresh_preserves_demo_authored_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination.git"
    source.mkdir()
    _git("init", "--initial-branch=main", cwd=source)
    (source / "ontology.ttl").write_text("first\n", encoding="utf-8")
    _git("add", "ontology.ttl", cwd=source)
    _git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.test",
        "commit",
        "-m",
        "first",
        cwd=source,
    )
    first_sha = _git("rev-parse", "HEAD", cwd=source)
    _git("clone", "--bare", str(source), str(destination))
    _git("--git-dir", str(destination), "update-ref", "refs/heads/demo-work", first_sha)

    (source / "ontology.ttl").write_text("second\n", encoding="utf-8")
    _git("add", "ontology.ttl", cwd=source)
    _git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.test",
        "commit",
        "-m",
        "second",
        cwd=source,
    )

    urls = {
        "alea-institute/FOLIO": source.as_uri(),
        "alea-institute/ontokit-demo-folio": destination.as_uri(),
    }
    monkeypatch.setattr(refresh, "repository_url", urls.__getitem__)
    mirror = refresh.load_manifest(_manifest(tmp_path))[0]
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    destination_sha = refresh.refresh_one(
        mirror,
        workspace,
        tmp_path / "unused-askpass",
        "read-token",
        "write-token",
        "2026-08-20T12:00:00+00:00",
    )

    assert _git("--git-dir", str(destination), "rev-parse", "refs/heads/demo-work") == first_sha
    refreshed_main = _git("--git-dir", str(destination), "rev-parse", "refs/heads/main")
    assert destination_sha == refreshed_main
    assert refreshed_main != first_sha
    readme = _git(
        "--git-dir",
        str(destination),
        "show",
        "refs/heads/main:DEMO-README.md",
    )
    assert "demo-authored non-default branches are preserved" in readme
