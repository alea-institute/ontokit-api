#!/usr/bin/env python3
"""Validate and emit OntoKit's immutable API/web release pair."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import NoReturn

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
EXPECTED_REPOSITORIES = {
    "api_repository": "alea-institute/ontokit-api",
    "web_repository": "alea-institute/ontokit-web",
}
EXPECTED_KEYS = {
    "schema_version",
    "api_repository",
    "api_sha",
    "web_repository",
    "web_sha",
}


def _refuse(message: str) -> NoReturn:
    print(f"refused: {message}", file=sys.stderr)
    raise SystemExit(64)


def _load(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _refuse(f"cannot read valid JSON from {path}: {exc}")
    if not isinstance(data, dict):
        _refuse("release manifest must be a JSON object")
    return data


def validate(
    manifest: dict[str, object], expected_api_sha: str | None, expected_web_sha: str | None
) -> tuple[str, str]:
    unknown = set(manifest) - EXPECTED_KEYS
    missing = EXPECTED_KEYS - set(manifest)
    if missing:
        _refuse(f"release manifest is missing keys: {', '.join(sorted(missing))}")
    if unknown:
        _refuse(f"release manifest has unknown keys: {', '.join(sorted(unknown))}")
    if manifest["schema_version"] != 1:
        _refuse("release manifest schema_version must be 1")

    for key, expected in EXPECTED_REPOSITORIES.items():
        if manifest[key] != expected:
            component = key.removesuffix("_repository")
            _refuse(f"unexpected {component} repository; expected {expected}")

    api_sha = manifest["api_sha"]
    web_sha = manifest["web_sha"]
    for component, value in (("API", api_sha), ("web", web_sha)):
        if not isinstance(value, str) or not SHA_RE.fullmatch(value):
            _refuse(f"{component} revision must be a full lowercase 40-character commit SHA")

    assert isinstance(api_sha, str)
    assert isinstance(web_sha, str)
    if expected_api_sha is not None and api_sha != expected_api_sha:
        _refuse("API SHA does not match the expected deployed revision")
    if expected_web_sha is not None and web_sha != expected_web_sha:
        _refuse("web SHA does not match the expected deployed revision")
    return api_sha, web_sha


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--expect-api-sha")
    parser.add_argument("--expect-web-sha")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    api_sha, web_sha = validate(_load(args.manifest), args.expect_api_sha, args.expect_web_sha)
    output = f"api_sha={api_sha}\nweb_sha={web_sha}\n"
    sys.stdout.write(output)
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(output)


if __name__ == "__main__":
    main()
