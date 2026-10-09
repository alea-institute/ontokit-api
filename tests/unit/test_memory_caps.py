"""Exercise memory protection in isolated children, without limiting the runner."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux memory limits")


def test_bare_pytest_guard_defaults_to_six_gib() -> None:
    environment = dict(os.environ)
    environment.pop("ONTOKIT_TEST_MEMORY_MIB", None)
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy, resource; runpy.run_path('conftest.py'); "
            "assert resource.getrlimit(resource.RLIMIT_AS) == (6 * 1024**3,) * 2",
        ],
        cwd=ROOT,
        env=environment,
        check=True,
        timeout=10,
    )


def test_bare_pytest_guard_rejects_oversized_allocation() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy, resource; runpy.run_path('conftest.py'); "
            "assert resource.getrlimit(resource.RLIMIT_AS) == (128 * 1024**2,) * 2; "
            "bytearray(256 * 1024**2)",
        ],
        cwd=ROOT,
        env={**os.environ, "ONTOKIT_TEST_MEMORY_MIB": "128"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "MemoryError" in result.stderr


def test_bare_pytest_guard_preserves_stricter_inherited_limit() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy, resource; "
            "resource.setrlimit(resource.RLIMIT_AS, (128 * 1024**2,) * 2); "
            "runpy.run_path('conftest.py'); "
            "assert resource.getrlimit(resource.RLIMIT_AS) == (128 * 1024**2,) * 2",
        ],
        cwd=ROOT,
        env={**os.environ, "ONTOKIT_TEST_MEMORY_MIB": "256"},
        check=True,
        timeout=10,
    )


def test_wrapper_fallback_enforces_cap_and_preserves_command_status(tmp_path: Path) -> None:
    # A present but unusable systemd user bus must still result in a capped run.
    systemd_stub = tmp_path / "systemd-run"
    systemd_stub.write_text("#!/bin/sh\nexit 1\n")
    systemd_stub.chmod(0o755)
    result = subprocess.run(
        [
            "bash",
            "scripts/memory-cap.sh",
            sys.executable,
            "-c",
            "import resource, sys; "
            "assert resource.getrlimit(resource.RLIMIT_AS) == (128 * 1024**2,) * 2; "
            "sys.exit(42)",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "ONTOKIT_TEST_MEMORY_MIB": "128",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 42
    assert "using prlimit" in result.stderr


def test_wrapper_systemd_route_sets_zero_swap_and_never_retries_command(tmp_path: Path) -> None:
    systemd_stub = tmp_path / "systemd-run"
    systemd_stub.write_text(
        "#!/bin/sh\nset -eu\n"
        '[ "$1" = "--user" ] && [ "$2" = "--scope" ]\n'
        '[ "$3" = "-p" ] && [ "$4" = "MemoryMax=134217728" ]\n'
        '[ "$5" = "-p" ] && [ "$6" = "MemorySwapMax=0" ]\n'
        'shift 6\nexec "$@"\n'
    )
    systemd_stub.chmod(0o755)
    result = subprocess.run(
        ["bash", "scripts/memory-cap.sh", "bash", "-c", "exit 42"],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "ONTOKIT_TEST_MEMORY_MIB": "128",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 42
    assert "using prlimit" not in result.stderr
