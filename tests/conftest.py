"""Shared fixtures: an isolated copy of the fixture app with bug-assurance configured."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

TOOLKIT = Path(__file__).resolve().parents[1]
SCRIPTS = TOOLKIT / ".claude" / "skills" / "bug-assurance" / "scripts"
FIXTURE = TOOLKIT / "examples" / "fixture-app"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bug_assurance import cli  # noqa: E402

FIXTURE_CONFIG = {
    "schema_version": 1,
    "paths": {"application": ["src/**"], "tests": ["tests/**"], "unrestricted": ["docs/**", "tickets/**"]},
    "checks": [
        {"id": "fixture-tests", "kind": "test", "required": True, "timeout_s": 300,
         "argv": ["{python}", "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"]},
        {"id": "compile", "kind": "build", "required": True, "timeout_s": 120,
         "argv": ["{python}", "-m", "compileall", "-q", "src"]},
    ],
    "repair": {"max_cycles": 3, "hard_limit": 10},
}


@dataclass
class Result:
    rc: int
    out: str
    err: str

    @property
    def json(self):
        return json.loads(self.out)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    # Tests may run inside a Claude Code session; never let its project dir leak in.
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("BA_PROJECT_DIR", raising=False)


def make_repo(tmp_path: Path, config: dict | None = None, git: bool = True) -> Path:
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    (repo / ".bug-assurance").mkdir()
    (repo / ".bug-assurance" / "config.json").write_text(json.dumps(config or FIXTURE_CONFIG, indent=2))
    (repo / ".gitignore").write_text(".bug-assurance/runs/\n.bug-assurance/inbox.jsonl\n"
                                     ".bug-assurance/active-run\n__pycache__/\n")
    if git and shutil.which("git"):
        ident = ["-c", "user.name=ba-test", "-c", "user.email=ba-test@example.invalid", "-c", "commit.gpgsign=false"]
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", *ident, "commit", "-q", "-m", "fixture"], cwd=repo, check=True)
    return repo


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path)


def run_ba(repo: Path, *args: str) -> Result:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = cli.main(["--project", str(repo), *args])
        except SystemExit as exc:  # argparse errors
            rc = exc.code if isinstance(exc.code, int) else 2
    return Result(rc, out.getvalue(), err.getvalue())


@pytest.fixture
def ba(repo):
    def _ba(*args: str) -> Result:
        return run_ba(repo, *args)
    return _ba
