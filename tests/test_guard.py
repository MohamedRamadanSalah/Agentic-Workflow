"""Unit tests for the Claude Code hook handlers (bug_assurance.guard)."""

from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import sys

import pytest
from builders import Q1, drive, put, run_dir
from conftest import SCRIPTS, run_ba

from bug_assurance import guard
from bug_assurance.core import sha256_text

SRC = "src/orders/cancellation.py"
TEST = "tests/test_cancellation.py"


def decide(repo, tool, tool_input, agent_type=""):
    out = guard.handle_pre_tool_use({"cwd": str(repo), "tool_name": tool, "agent_type": agent_type,
                                     "tool_input": tool_input})
    return out["hookSpecificOutput"]["permissionDecision"] if out else "allow"


def write(repo, path, agent_type=""):
    p = path if os.path.isabs(str(path)) else str(repo / path)
    return decide(repo, "Write", {"file_path": p}, agent_type)


def cmd(repo, command, tool="Bash"):
    return decide(repo, tool, {"command": command})


def set_state(repo, name):
    p = run_dir(repo) / "state.json"
    st = json.loads(p.read_text())
    st["state"] = name
    p.write_text(json.dumps(st))


EARLY = ["INTAKE", "INVESTIGATION", "CLARIFICATION", "BLOCKED_AWAITING_CLARIFICATION", "CONTRACT",
         "BLOCKED_EXTERNAL_DEPENDENCY"]
LATE_FROZEN = ["VERIFICATION", "FAILED_VERIFICATION", "INDEPENDENT_REVIEW", "ADJUDICATION",
               "FINAL_VERIFICATION", "FAILED_QUALITY_GATE"]


@pytest.fixture
def valid_run(ba, repo):
    """A run whose artifacts satisfy the clarification gate (driven to REGRESSION_TEST)."""
    drive(ba, repo, "REGRESSION_TEST")
    return repo


@pytest.mark.parametrize("state,src,tests", [
    *[(s, "deny", "deny") for s in EARLY],
    ("REGRESSION_TEST", "deny", "allow"),
    ("IMPLEMENTATION", "allow", "allow"),
    ("REPAIR_LOOP", "allow", "allow"),
    *[(s, "deny", "deny") for s in LATE_FROZEN],
])
def test_write_policy_per_state(valid_run, state, src, tests):
    repo = valid_run
    set_state(repo, state)
    assert write(repo, SRC) == src
    assert write(repo, TEST) == tests
    assert write(repo, "src/orders/new_module.py") == src  # new files too
    assert write(repo, "setup.cfg") == src  # "other" files are treated like source
    assert write(repo, "docs/notes.md") == "allow"  # unrestricted
    assert write(repo, ".claude/settings.json") == "ask"
    assert write(repo, ".github/workflows/ci.yml") == "ask"
    assert write(repo, ".bug-assurance/config.json") == "ask"
    assert write(repo, str(repo.parent / "outside.py")) == "allow"


def test_edit_and_notebook_tools_are_guarded(valid_run):
    repo = valid_run
    set_state(repo, "CONTRACT")
    assert decide(repo, "Edit", {"file_path": str(repo / SRC)}) == "deny"
    assert decide(repo, "MultiEdit", {"file_path": str(repo / SRC)}) == "deny"
    assert decide(repo, "NotebookEdit", {"notebook_path": str(repo / "src/analysis.ipynb")}) == "deny"
    assert decide(repo, "Read", {"file_path": str(repo / SRC)}) == "allow"


def test_terminal_run_is_silent(valid_run):
    set_state(valid_run, "FINAL_REPORT")
    assert write(valid_run, SRC) == "allow"


def test_no_active_run_is_silent_but_evidence_stays_protected(ba, repo):
    assert write(repo, SRC) == "allow"
    assert cmd(repo, "echo x > src/orders/cancellation.py") == "allow"
    assert run_ba(repo, "start", "--title", "t").rc == 0
    rid = run_dir(repo).name
    assert run_ba(repo, "deactivate", "--reason", "pause").rc == 0
    assert write(repo, SRC) == "allow"
    assert write(repo, f".bug-assurance/runs/{rid}/state.json") == "deny"
    assert write(repo, f".bug-assurance/runs/{rid}/verification/001.json") == "deny"
    assert write(repo, ".bug-assurance/inbox.jsonl") == "deny"
    assert write(repo, ".bug-assurance/active-run") == "deny"


def test_not_installed_repo_is_silent(tmp_path):
    (tmp_path / "src").mkdir()
    assert write(tmp_path, "src/a.py") == "allow"
    assert cmd(tmp_path, "git push") == "allow"  # the system is not installed there


def test_review_only_agents_cannot_modify_project_files(ba, repo):
    drive(ba, repo, "IMPLEMENTATION")
    assert write(repo, SRC, agent_type="ba-regression-reviewer") == "deny"
    assert write(repo, SRC, agent_type="ba-findings-adjudicator") == "deny"
    assert write(repo, SRC, agent_type="ba-implementer") == "allow"
    rid = run_dir(repo).name
    assert write(repo, f".bug-assurance/runs/{rid}/reviews/regression.json",
                 agent_type="ba-regression-reviewer") == "allow"


@pytest.mark.parametrize("command", [
    "git commit -m x", "git -c user.name=x push", "git -c a=b -C . push origin main", "git push --force",
    "git merge feature", "git rebase main", "git reset --hard HEAD~1", "git clean -fdx", "git clean -f",
    "git checkout -- src/a.py", "git checkout .", "git restore src/a.py", "git branch -D topic",
    "git stash drop", "git tag v1", "gh pr create --fill", "gh pr merge 3", "gh repo delete x/y",
    "gh api -X DELETE repos/o/r/branches/main/protection", "gh api repos/o/r --method PATCH -f a=b",
    "npm test --no-verify",
])
def test_publishing_and_destructive_git_asks(repo, command):
    assert cmd(repo, command) == "ask"


@pytest.mark.parametrize("command", [
    "git status", "git diff", "git diff HEAD -- src", "git log --oneline -5", "ls src", "cat src/orders/cancellation.py",
    "python -m pytest tests -q", "python -m pytest tests -q 2>&1", "grep -rn PACKED src",
    "gh api repos/o/r", "gh pr view 3", "python .claude/skills/bug-assurance/scripts/ba.py status",
])
def test_harmless_commands_allowed_during_run(valid_run, command):
    set_state(valid_run, "CLARIFICATION")
    assert cmd(valid_run, command) == "allow"


def test_shell_writes_to_source_and_evidence(valid_run):
    repo = valid_run
    set_state(repo, "CLARIFICATION")
    rid = run_dir(repo).name
    assert cmd(repo, "echo x > src/orders/cancellation.py") == "deny"
    assert cmd(repo, "echo x >> src/orders/cancellation.py") == "deny"
    assert cmd(repo, "echo x | tee src/orders/cancellation.py") == "deny"
    assert cmd(repo, "sed -i 's/a/b/' src/orders/cancellation.py") == "ask"
    assert cmd(repo, "cp /tmp/x.py src/orders/cancellation.py") == "ask"
    assert cmd(repo, "Set-Content src/orders/cancellation.py -Value x", tool="PowerShell") == "deny"
    assert cmd(repo, "Set-Content -Path src/orders/cancellation.py -Value x", tool="PowerShell") == "deny"
    assert cmd(repo, f"echo [] > .bug-assurance/runs/{rid}/findings.json") == "deny"
    assert cmd(repo, "rm -rf .bug-assurance") == "deny"
    assert cmd(repo, "Remove-Item -Recurse .bug-assurance/runs", tool="PowerShell") == "deny"
    assert cmd(repo, "echo note > docs/scratch.md") == "allow"
    set_state(repo, "IMPLEMENTATION")
    assert cmd(repo, "echo x > src/orders/cancellation.py") == "allow"


def test_user_prompt_capture_redacts_and_hashes(ba, repo):
    assert run_ba(repo, "start", "--title", "t").rc == 0
    out = guard.handle_user_prompt({"cwd": str(repo), "prompt": "here is my password=hunter2 ok",
                                    "session_id": "s1"})
    assert out is None  # no pending questions -> no extra context
    rec = json.loads((repo / ".bug-assurance" / "inbox.jsonl").read_text().splitlines()[-1])
    assert "hunter2" not in rec["text"] and "[REDACTED]" in rec["text"]
    assert rec["sha256"] == sha256_text(rec["text"]) and rec["id"].startswith("M-")
    q = copy.deepcopy(Q1)
    q["status"] = "OPEN"
    put(repo, "decisions.json", {"questions": [q]})
    out = guard.handle_user_prompt({"cwd": str(repo), "prompt": "packed orders are cancellable", "session_id": "s1"})
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "Q-1" in ctx and "--from-message M-" in ctx
    assert guard.handle_user_prompt({"cwd": str(repo), "prompt": "   "}) is None


def test_stop_hook(valid_run):
    repo = valid_run
    set_state(repo, "IMPLEMENTATION")
    assert guard.handle_stop({"cwd": str(repo)})["decision"] == "block"
    assert guard.handle_stop({"cwd": str(repo), "stop_hook_active": True}) is None
    assert guard.handle_stop({"cwd": str(repo), "agent_type": "ba-implementer"}) is None
    set_state(repo, "REGRESSION_TEST")
    assert guard.handle_stop({"cwd": str(repo)}) is None
    set_state(repo, "CLARIFICATION")
    assert guard.handle_stop({"cwd": str(repo)}) is None  # no pending questions
    q = copy.deepcopy(Q1)
    q["status"] = "OPEN"
    put(repo, "decisions.json", {"questions": [q]})
    assert "BLOCKED_AWAITING_CLARIFICATION" in guard.handle_stop({"cwd": str(repo)})["reason"]


def test_session_start_context(ba, repo):
    assert guard.handle_session_start({"cwd": str(repo)}) is None
    assert run_ba(repo, "start", "--title", "resume me").rc == 0
    ctx = guard.handle_session_start({"cwd": str(repo)})["hookSpecificOutput"]["additionalContext"]
    assert "resume me" in ctx and "INTAKE" in ctx


def test_main_malformed_stdin_and_unknown_event():
    out = io.StringIO()
    assert guard.main("pre-tool-use", stdin=io.StringIO("{not json"), stdout=out) == 0
    assert out.getvalue() == ""
    assert guard.main("no-such-event", stdin=io.StringIO("{}"), stdout=out) == 1


def test_internal_error_during_active_run_asks(ba, repo):
    assert run_ba(repo, "start", "--title", "t").rc == 0
    (run_dir(repo) / "state.json").write_text("{corrupt")
    out = io.StringIO()
    payload = {"cwd": str(repo), "tool_name": "Write", "tool_input": {"file_path": str(repo / SRC)}}
    assert guard.main("pre-tool-use", stdin=io.StringIO(json.dumps(payload)), stdout=out) == 0
    decision = json.loads(out.getvalue())["hookSpecificOutput"]
    assert decision["permissionDecision"] == "ask" and "guard error" in decision["permissionDecisionReason"]


def test_real_hook_subprocess_invocation(ba, repo):
    """Exactly how Claude Code runs the hook: command line + JSON on stdin + CLAUDE_PROJECT_DIR."""
    drive(ba, repo, "CLARIFICATION")
    payload = {"session_id": "s", "hook_event_name": "PreToolUse", "cwd": str(repo), "tool_name": "Edit",
               "tool_input": {"file_path": str(repo / SRC), "old_string": "a", "new_string": "b"}}
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(repo))
    proc = subprocess.run([sys.executable, str(SCRIPTS / "ba.py"), "hook", "pre-tool-use"],
                          input=json.dumps(payload).encode(), capture_output=True, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse" and out["permissionDecision"] == "deny"
    assert "CLARIFICATION" in out["permissionDecisionReason"]
    payload["tool_input"] = {"file_path": str(repo / "docs/notes.md")}
    proc = subprocess.run([sys.executable, str(SCRIPTS / "ba.py"), "hook", "pre-tool-use"],
                          input=json.dumps(payload).encode(), capture_output=True, env=env, timeout=60)
    assert proc.returncode == 0 and proc.stdout == b""  # allowed: no decision emitted
