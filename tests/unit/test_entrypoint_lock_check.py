"""Run the real entrypoint to verify the development image lock-drift check."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

ENTRYPOINT = Path(__file__).parents[2] / "scripts" / "entrypoint.sh"
LOCK_CONTENT = b"version = 1\n"
LOCK_HASH = hashlib.sha256(LOCK_CONTENT).hexdigest()
STALE_HASH = hashlib.sha256(b"old lock\n").hexdigest()


def run_entrypoint(
    tmp_path: Path, *, image_hash: str | None, live_lock_exists: bool = True, skip: bool = False
) -> subprocess.CompletedProcess[str]:
    hash_file = tmp_path / ".uv-lock.sha256"
    if image_hash is not None:
        hash_file.write_text(image_hash + "\n", encoding="utf-8")
    live_lock = tmp_path / "uv.lock.live"
    if live_lock_exists:
        live_lock.write_bytes(LOCK_CONTENT)

    return subprocess.run(
        ["bash", str(ENTRYPOINT), "echo", "command ran"],
        env={
            **os.environ,
            "ONTOKIT_LOCK_HASH_FILE": str(hash_file),
            "ONTOKIT_LIVE_LOCK": str(live_lock),
            "ONTOKIT_SKIP_LOCK_CHECK": "1" if skip else "0",
            "RUN_MIGRATIONS": "0",
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_matching_lock_starts_command(tmp_path: Path) -> None:
    result = run_entrypoint(tmp_path, image_hash=LOCK_HASH)

    assert result.returncode == 0
    assert "command ran" in result.stdout
    assert result.stderr == ""


def test_mismatched_lock_stops_before_command(tmp_path: Path) -> None:
    result = run_entrypoint(tmp_path, image_hash=STALE_HASH)

    assert result.returncode != 0
    assert "docker compose up -d --build api worker" in result.stderr
    assert STALE_HASH in result.stderr
    assert LOCK_HASH in result.stderr
    assert "command ran" not in result.stdout
    assert "Starting application" not in result.stdout
    assert "Skipping migrations" not in result.stdout


def test_absent_live_lock_starts_command(tmp_path: Path) -> None:
    result = run_entrypoint(tmp_path, image_hash=STALE_HASH, live_lock_exists=False)

    assert result.returncode == 0
    assert "command ran" in result.stdout
    assert result.stderr == ""


def test_skip_lock_check_starts_command_despite_mismatch(tmp_path: Path) -> None:
    result = run_entrypoint(tmp_path, image_hash=STALE_HASH, skip=True)

    assert result.returncode == 0
    assert "command ran" in result.stdout
    assert "ONTOKIT_SKIP_LOCK_CHECK=1" in result.stderr
    assert len(result.stderr.splitlines()) == 1


def test_missing_recorded_hash_stops_before_command(tmp_path: Path) -> None:
    result = run_entrypoint(tmp_path, image_hash=None)

    assert result.returncode != 0
    assert "docker compose up -d --build api worker" in result.stderr
    assert "predates dependency-lock recording" in result.stderr
    assert LOCK_HASH in result.stderr
    assert "command ran" not in result.stdout
    assert "Starting application" not in result.stdout
    assert "Skipping migrations" not in result.stdout
