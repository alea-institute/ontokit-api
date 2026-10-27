"""Contracts for the shared local/CI type-checking entry point."""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]


@pytest.fixture
def checker_project(tmp_path: Path) -> Path:
    project = tmp_path / "project with spaces"
    (project / "scripts").mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts/typecheck.sh", project / "scripts/typecheck.sh")
    (project / ".venv").mkdir()
    (project / ".venv" / "runtime-marker").write_text("preserve runtime")
    return project


def _run_checker(
    project: Path, tmp_path: Path, *, ambient: bool = False, exit_code: int = 0
) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "uv-call.json"
    uv = bin_dir / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['UV_CALL_RECORD']).write_text(json.dumps({\n"
        "    'cwd': os.getcwd(), 'args': sys.argv[1:],\n"
        "    'environment': os.environ.get('UV_PROJECT_ENVIRONMENT'),\n"
        "    'no_sync': os.environ.get('UV_NO_SYNC'),\n"
        "    'frozen': os.environ.get('UV_FROZEN'),\n"
        "    'project': os.environ.get('UV_PROJECT'),\n"
        "    'working_dir': os.environ.get('UV_WORKING_DIR'),\n"
        "    'active': os.environ.get('VIRTUAL_ENV'),\n"
        "}))\n"
        "print('checker diagnostic', file=sys.stderr)\n"
        "sys.exit(int(os.environ['UV_EXIT_CODE']))\n"
    )
    uv.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "UV_CALL_RECORD": str(record),
        "UV_EXIT_CODE": str(exit_code),
    }
    if ambient:
        env.update(
            UV_PROJECT_ENVIRONMENT=str(project / ".venv"),
            UV_PYTHON="3.13",
            VIRTUAL_ENV=str(project / ".venv"),
            UV_NO_SYNC="1",
            UV_FROZEN="1",
            UV_PROJECT=str(tmp_path / "another-project"),
            UV_WORKING_DIR=str(tmp_path / "another-directory"),
        )
    result = subprocess.run(
        ["bash", str(project / "scripts/typecheck.sh")],
        cwd=tmp_path if ambient else project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, json.loads(record.read_text())


@pytest.mark.parametrize("ambient", [False, True])
def test_checker_uses_locked_python311_without_touching_runtime(
    checker_project: Path, tmp_path: Path, ambient: bool
) -> None:
    result, call = _run_checker(checker_project, tmp_path, ambient=ambient)

    assert result.returncode == 0, result.stderr
    assert call["cwd"] == str(checker_project)
    assert call["environment"] == str(checker_project / ".venv-mypy")
    args = call["args"]
    assert isinstance(args, list)
    assert args[0] == "run"
    assert args[args.index("--python") + 1] == "3.11"
    assert args[args.index("--group") + 1] == "dev"
    assert "--locked" in args
    assert args[-2:] == ["mypy", "ontokit/"]
    for override in ("no_sync", "frozen", "project", "working_dir", "active"):
        assert call[override] is None
    assert (checker_project / ".venv" / "runtime-marker").read_text() == "preserve runtime"


@pytest.mark.parametrize("exit_code", [1, 2, 37])
def test_checker_preserves_uv_failure_and_diagnostic(
    checker_project: Path, tmp_path: Path, exit_code: int
) -> None:
    result, _ = _run_checker(checker_project, tmp_path, exit_code=exit_code)

    assert result.returncode == exit_code
    assert "checker diagnostic" in result.stderr


def test_checker_rejects_stale_lock_without_rewriting_it(checker_project: Path) -> None:
    manifest = checker_project / "pyproject.toml"
    manifest.write_text(
        '[project]\nname = "checker-lock-fixture"\nversion = "0.1.0"\n'
        'requires-python = ">=3.11"\n[dependency-groups]\ndev = []\n'
    )
    locked = subprocess.run(
        ["uv", "lock", "--python", "3.11", "--offline"],
        cwd=checker_project,
        env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(checker_project / ".venv-mypy")},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert locked.returncode == 0, locked.stderr
    lock = checker_project / "uv.lock"
    original_lock = lock.read_bytes()
    manifest.write_text(manifest.read_text().replace(">=3.11", ">=3.10"))

    result = subprocess.run(
        ["bash", "scripts/typecheck.sh"],
        cwd=checker_project,
        env={**os.environ, "UV_OFFLINE": "1"},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert result.returncode != 0
    assert "lockfile" in result.stderr and "--locked" in result.stderr
    assert lock.read_bytes() == original_lock
    assert (checker_project / ".venv" / "runtime-marker").read_text() == "preserve runtime"


def test_checker_environment_is_excluded_from_git_and_docker() -> None:
    result = subprocess.run(
        ["git", "check-ignore", ".venv-mypy/probe"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert ".venv-mypy/" in (ROOT / ".dockerignore").read_text().splitlines()


def _mypy_hook() -> dict:
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    return next(
        hook
        for repo in config["repos"]
        if repo["repo"] == "local"
        for hook in repo["hooks"]
        if hook["id"] == "mypy"
    )


@pytest.mark.parametrize("entrypoint", ["make", "hook", "ci"])
@pytest.mark.parametrize("exit_code", [0, 37])
def test_entrypoints_run_shared_checker_and_propagate_failure(
    tmp_path: Path, entrypoint: str, exit_code: int
) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/typecheck.sh").write_text(f"echo shared-checker-ran\nexit {exit_code}\n")
    if entrypoint == "make":
        shutil.copyfile(ROOT / "Makefile", tmp_path / "Makefile")
        command = ["make", "typecheck"]
    elif entrypoint == "hook":
        hook = _mypy_hook()
        assert hook["language"] == "system"
        assert hook["pass_filenames"] is False
        assert hook["require_serial"] is True
        assert hook["files"] == "^ontokit/"
        assert hook["types"] == ["python"]
        command = shlex.split(hook["entry"])
    else:
        workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
        lint = workflow["jobs"]["lint"]
        assert not lint.get("continue-on-error", False)
        step = next(s for s in lint["steps"] if s.get("run") == "bash scripts/typecheck.sh")
        assert not step.get("continue-on-error", False)
        command = ["bash", "-e", "-o", "pipefail", "-c", step["run"]]
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, check=False)
    assert "shared-checker-ran" in result.stdout
    assert (result.returncode == 0) == (exit_code == 0)


def test_ci_lint_uses_one_locked_python311_environment() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    lint = workflow["jobs"]["lint"]
    assert lint["env"]["UV_PROJECT_ENVIRONMENT"] == "${{ github.workspace }}/.venv-mypy"
    commands = [step["run"] for step in lint["steps"] if "run" in step]
    assert commands[0] == "uv sync --python 3.11 --locked --group dev"
    for command in commands[1:]:
        if command.startswith("uv "):
            args = shlex.split(command)
            assert args[args.index("--python") + 1] == "3.11"
            assert "--locked" in args


def test_setup_installs_dev_group_and_hooks() -> None:
    result = subprocess.run(
        ["make", "--dry-run", "setup"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    commands = result.stdout.splitlines()
    assert commands == ["uv sync --group dev", "uv run pre-commit install"]
