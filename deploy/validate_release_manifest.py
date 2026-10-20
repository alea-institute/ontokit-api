#!/usr/bin/env python3
"""Validate and emit OntoKit's immutable API/web release pair.

Schema 1 names a source commit pair that the host builds. Schema 2 adds the
registry image digest CI published for each commit, and the host runs those
exact images instead of building.
"""

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
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
EXPECTED_KEYS = {
    "schema_version",
    "api_repository",
    "api_sha",
    "web_repository",
    "web_sha",
}
IMAGE_KEYS = {"api_image", "web_image"}
# The only registry repositories a schema 2 manifest may name.
EXPECTED_IMAGE_REPOSITORIES = {
    "api_image": "ghcr.io/alea-institute/ontokit",
    "web_image": "ghcr.io/alea-institute/ontokit-web",
}
SUPPORTED_SCHEMAS = (1, 2)


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


def _image_digest(key: str, value: object) -> str:
    """Return the digest of a pinned image reference, refusing anything mutable."""
    component = key.removesuffix("_image")
    expected = EXPECTED_IMAGE_REPOSITORIES[key]
    if not isinstance(value, str):
        _refuse(f"{component} image must be a string")
    repository, separator, digest = value.partition("@")
    if not separator or repository != expected:
        _refuse(f"{component} image must be {expected}@sha256:<digest>")
    if not DIGEST_RE.fullmatch(digest):
        _refuse(f"{component} image digest must be sha256 followed by 64 lowercase hex characters")
    return digest


def validate(
    manifest: dict[str, object],
    expected_api_sha: str | None,
    expected_web_sha: str | None,
    allowed_schemas: tuple[int, ...] = SUPPORTED_SCHEMAS,
) -> dict[str, str]:
    schema = manifest.get("schema_version")
    # bool is an int subclass; a JSON true must not pass as schema 1.
    if isinstance(schema, bool) or schema not in allowed_schemas:
        allowed = " or ".join(str(version) for version in allowed_schemas)
        _refuse(f"release manifest schema_version must be {allowed}")
    expected_keys = EXPECTED_KEYS | IMAGE_KEYS if schema == 2 else EXPECTED_KEYS
    unknown = set(manifest) - expected_keys
    missing = expected_keys - set(manifest)
    if missing:
        _refuse(f"release manifest is missing keys: {', '.join(sorted(missing))}")
    if unknown:
        _refuse(f"release manifest has unknown keys: {', '.join(sorted(unknown))}")

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

    release = {
        "schema_version": str(schema),
        "api_sha": api_sha,
        "web_sha": web_sha,
        "api_digest": "",
        "web_digest": "",
    }
    if schema == 2:
        release["api_digest"] = _image_digest("api_image", manifest["api_image"])
        release["web_digest"] = _image_digest("web_image", manifest["web_image"])
    return release


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--expect-api-sha")
    parser.add_argument("--expect-web-sha")
    parser.add_argument("--github-output", type=Path)
    parser.add_argument(
        "--require-schema",
        type=int,
        choices=SUPPORTED_SCHEMAS,
        help="refuse any other schema, for consumers that support only one",
    )
    args = parser.parse_args()

    allowed = (args.require_schema,) if args.require_schema else SUPPORTED_SCHEMAS
    release = validate(_load(args.manifest), args.expect_api_sha, args.expect_web_sha, allowed)
    output = "".join(f"{key}={value}\n" for key, value in release.items())
    sys.stdout.write(output)
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(output)


if __name__ == "__main__":
    main()
