"""Unit tests for workflow validation and honest GitHub status reporting (bug_assurance.ghstatus)."""

from __future__ import annotations

import shutil
import subprocess
import textwrap

import pytest

from bug_assurance.ghstatus import github_status, validate_workflow

pytest.importorskip("yaml")

SHA = "11bd71901bbe5b1630ceea73d27597364c9af683"


def wf(tmp_path, body, name="ci.yml"):
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(textwrap.dedent(body))
    return p


GOOD = f"""
on:
  pull_request:
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@{SHA}
      - run: python -m pytest
"""


def test_good_workflow_passes_and_bare_on_key_is_handled(tmp_path):
    r = validate_workflow(wf(tmp_path, GOOD))
    assert r["validated"] and r["problems"] == [] and r["warnings"] == []


def test_missing_permissions_and_unpinned_action(tmp_path):
    r = validate_workflow(wf(tmp_path, """
        on: [push]
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
              - uses: ./local-action
    """))
    assert any("permissions" in p for p in r["problems"])
    assert any("not pinned to a commit SHA" in w and "actions/checkout@v4" in w for w in r["warnings"])
    assert not any("local-action" in w for w in r["warnings"])
    assert any("timeout-minutes" in w for w in r["warnings"])


def test_pull_request_target_and_untrusted_interpolation(tmp_path):
    r = validate_workflow(wf(tmp_path, """
        on:
          pull_request_target:
        permissions: write-all
        jobs:
          greet:
            runs-on: ubuntu-latest
            timeout-minutes: 5
            steps:
              - run: echo "${{ github.event.pull_request.title }}"
    """))
    assert any("pull_request_target" in w for w in r["warnings"])
    assert any("write permissions" in w for w in r["warnings"])
    assert any("untrusted event text" in p for p in r["problems"])


def test_yaml_syntax_error_and_structure(tmp_path):
    r = validate_workflow(wf(tmp_path, "on: [push\njobs: {", name="bad.yml"))
    assert r["validated"] is False and "YAML syntax error" in r["problems"][0]
    r = validate_workflow(wf(tmp_path, "permissions: {}\njobs: {}\n", name="empty.yml"))
    assert any("missing `on:`" in p for p in r["problems"]) and any("no jobs" in p for p in r["problems"])
    r = validate_workflow(wf(tmp_path, "on: push\npermissions: {}\njobs:\n  x:\n    steps: []\n", name="norun.yml"))
    assert any("has no runs-on" in p for p in r["problems"])


@pytest.mark.skipif(not shutil.which("git"), reason="git required")
def test_github_status_without_remote_is_honest(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    wf(tmp_path, GOOD)
    (tmp_path / ".coderabbit.yaml").write_text("reviews:\n  profile: assertive\n")
    st = github_status(tmp_path)
    assert st["remote"] is None
    assert st["states"] == {
        "workflow_file_created": "YES", "syntax_validated": "YES", "executed_successfully": "UNKNOWN",
        "reported_by_github": "UNKNOWN", "configured_as_required": "UNKNOWN", "enforcement_verified": "NO",
    }
    assert st["coderabbit"] == {"config_file": "PRESENT", "config_valid_yaml": True,
                                "app_installed": "UNKNOWN", "observed_reviews": "UNKNOWN"}
    assert any("no GitHub remote" in n for n in st["notes"])


@pytest.mark.skipif(not shutil.which("git"), reason="git required")
def test_github_status_with_no_workflows(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    st = github_status(tmp_path)
    assert st["states"]["workflow_file_created"] == "NO" and st["states"]["syntax_validated"] == "N/A"
    assert st["coderabbit"]["config_file"] == "ABSENT"


def test_validate_workflows_cli_exit_codes(tmp_path):
    from conftest import run_ba

    (tmp_path / ".bug-assurance").mkdir()
    assert run_ba(tmp_path, "validate-workflows").rc == 2  # nothing to validate is not a pass
    wf(tmp_path, GOOD)
    assert run_ba(tmp_path, "validate-workflows").rc == 0
    wf(tmp_path, "on: push\njobs: {}\n", name="broken.yml")
    assert run_ba(tmp_path, "validate-workflows").rc == 3
