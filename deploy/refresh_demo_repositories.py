#!/usr/bin/env python3
"""Refresh only the default branches of OntoKit's two demo repositories."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

SOURCE_TOKEN_ENV = "GITHUB_DEMO_SOURCE_TOKEN"
DESTINATION_TOKEN_ENV = "GITHUB_DEMO_MIRROR_TOKEN"
EXPECTED_MIRRORS = {
    "folio": ("alea-institute/FOLIO", "alea-institute/ontokit-demo-folio", "main"),
    "semantic-canon": (
        "CatholicOS/ontology-semantic-canon",
        "alea-institute/ontokit-demo-semantic-canon",
        "main",
    ),
}


@dataclass(frozen=True)
class Mirror:
    name: str
    source_repository: str
    destination_repository: str
    default_branch: str


def refuse(message: str) -> NoReturn:
    print(f"refused: {message}", file=sys.stderr)
    raise SystemExit(64)


def load_manifest(path: Path) -> tuple[Mirror, ...]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        refuse(f"cannot read valid mirror JSON from {path}: {exc}")
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "mirrors"}:
        refuse("mirror manifest must contain only schema_version and mirrors")
    if raw["schema_version"] != 1 or not isinstance(raw["mirrors"], list):
        refuse("mirror manifest schema_version must be 1 and mirrors must be a list")

    mirrors: list[Mirror] = []
    for value in raw["mirrors"]:
        if not isinstance(value, dict) or set(value) != {
            "name",
            "source_repository",
            "destination_repository",
            "default_branch",
        }:
            refuse("each mirror must contain exactly the four declared fields")
        if not all(isinstance(item, str) for item in value.values()):
            refuse("mirror fields must be strings")
        mirror = Mirror(**value)
        if EXPECTED_MIRRORS.get(mirror.name) != (
            mirror.source_repository,
            mirror.destination_repository,
            mirror.default_branch,
        ):
            refuse(f"mirror {mirror.name!r} is not an approved source/destination contract")
        mirrors.append(mirror)

    if {mirror.name for mirror in mirrors} != set(EXPECTED_MIRRORS) or len(mirrors) != 2:
        refuse("mirror manifest must declare each approved demo repository exactly once")
    return tuple(mirrors)


def require_tokens(environment: Mapping[str, str]) -> tuple[str, str]:
    source = environment.get(SOURCE_TOKEN_ENV, "")
    destination = environment.get(DESTINATION_TOKEN_ENV, "")
    if not source:
        refuse(f"{SOURCE_TOKEN_ENV} is required")
    if not destination:
        refuse(f"{DESTINATION_TOKEN_ENV} is required")
    if source == destination:
        refuse("source-read and destination-write credentials must be different tokens")
    return source, destination


def git_environment(token: str, askpass: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "DEMO_GIT_TOKEN": token,
            "GIT_ASKPASS": str(askpass),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def run_command(
    command: Sequence[str], *, cwd: Path | None = None, environment: Mapping[str, str] | None = None
) -> None:
    subprocess.run(command, cwd=cwd, env=environment, check=True)  # noqa: S603


def repository_url(repository: str) -> str:
    return f"https://github.com/{repository}.git"


def write_askpass(path: Path) -> None:
    path.write_text(
        "#!/usr/bin/env sh\n"
        'case "$1" in\n'
        '  *Username*) printf "%s\\n" x-access-token ;;\n'
        '  *Password*) printf "%s\\n" "$DEMO_GIT_TOKEN" ;;\n'
        "  *) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    path.chmod(0o700)


def refresh_one(
    mirror: Mirror,
    workspace: Path,
    askpass: Path,
    source_token: str,
    destination_token: str,
    refreshed_at: str,
) -> None:
    checkout = workspace / mirror.name
    run_command(
        [
            "git",
            "clone",
            "--single-branch",
            "--branch",
            mirror.default_branch,
            "--no-tags",
            repository_url(mirror.source_repository),
            str(checkout),
        ],
        environment=git_environment(source_token, askpass),
    )
    source_sha = subprocess.run(  # noqa: S603
        ["git", "rev-parse", "HEAD"],
        cwd=checkout,
        env=scrubbed_environment(),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    readme = (
        "# OntoKit demo mirror\n\n"
        "This private repository is an automated OntoKit demo target. Its default branch is "
        "force-refreshed from the source below; demo-authored non-default branches are preserved.\n\n"
        f"- Source: `{mirror.source_repository}`\n"
        f"- Source revision: `{source_sha}`\n"
        f"- Last refresh (UTC): `{refreshed_at}`\n"
    )
    (checkout / "DEMO-README.md").write_text(readme, encoding="utf-8")
    run_command(
        ["git", "add", "DEMO-README.md"],
        cwd=checkout,
        environment=scrubbed_environment(),
    )
    run_command(
        [
            "git",
            "-c",
            "user.name=OntoKit Demo Refresh",
            "-c",
            "user.email=demo-refresh@users.noreply.github.com",
            "commit",
            "-m",
            f"chore(demo): refresh from {mirror.source_repository}@{source_sha[:12]}",
        ],
        cwd=checkout,
        environment=scrubbed_environment(),
    )
    run_command(
        [
            "git",
            "push",
            "--force",
            repository_url(mirror.destination_repository),
            f"HEAD:refs/heads/{mirror.default_branch}",
        ],
        cwd=checkout,
        environment=git_environment(destination_token, askpass),
    )
    print(f"refreshed={mirror.name} source_sha={source_sha}")


def scrubbed_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop(SOURCE_TOKEN_ENV, None)
    environment.pop(DESTINATION_TOKEN_ENV, None)
    environment.pop("DEMO_GIT_TOKEN", None)
    return environment


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--resync-executable", type=Path, required=True)
    parser.add_argument(
        "--lock-file", type=Path, default=Path("/run/lock/ontokit-demo-refresh.lock")
    )
    parser.add_argument("--refreshed-at")
    args = parser.parse_args()

    mirrors = load_manifest(args.manifest)
    source_token, destination_token = require_tokens(os.environ)
    if not args.resync_executable.is_file() or not os.access(args.resync_executable, os.X_OK):
        refuse("resync executable is missing or not executable")
    refreshed_at = args.refreshed_at or datetime.now(UTC).replace(microsecond=0).isoformat()

    args.lock_file.parent.mkdir(parents=True, exist_ok=True)
    with args.lock_file.open("w", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            refuse("another demo refresh is already running")
        with tempfile.TemporaryDirectory(prefix="ontokit-demo-refresh-") as temp_dir:
            workspace = Path(temp_dir)
            askpass = workspace / "askpass.sh"
            write_askpass(askpass)
            for mirror in mirrors:
                refresh_one(
                    mirror,
                    workspace,
                    askpass,
                    source_token,
                    destination_token,
                    refreshed_at,
                )
            run_command(
                [str(args.resync_executable), str(args.manifest)],
                environment=scrubbed_environment(),
            )
    print("demo_refresh=passed mirrors=2 resync=passed")


if __name__ == "__main__":
    main()
