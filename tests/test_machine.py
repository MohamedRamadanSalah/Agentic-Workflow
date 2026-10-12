"""Unit tests for the orchestrator state machine (bug_assurance.machine)."""

from __future__ import annotations

import copy
import json

from builders import Q1, TICKET, drive, get, ok, put, run_dir
from conftest import run_ba

from bug_assurance import guard
from bug_assurance.machine import STATES, allowed_targets


def open_q():
    q = copy.deepcopy(Q1)
    q["status"] = "OPEN"
    q.pop("resolution")
    q.pop("analysis")
    return q


def test_every_transition_target_is_a_known_state():
    for name, spec in STATES.items():
        for target in spec["next"]:
            assert target in STATES, (name, target)
    assert STATES["FINAL_REPORT"]["next"] == {}


def test_illegal_and_unknown_transitions(ba, repo):
    ok(ba("start", "--title", "t"))
    res = ba("transition", "IMPLEMENTATION")
    assert res.rc == 2 and "INTAKE -> IMPLEMENTATION is not allowed" in res.err
    assert "BLOCKED_EXTERNAL_DEPENDENCY" in res.err and "INVESTIGATION" in res.err
    res = ba("transition", "DONE")
    assert res.rc == 2 and "unknown state DONE" in res.err
    assert get(repo, "state.json")["state"] == "INTAKE"


def test_refused_transition_is_recorded(ba, repo):
    ok(ba("start", "--title", "t"))
    res = ba("transition", "INVESTIGATION")  # no ticket.json yet
    assert res.rc == 3 and "missing artifact ticket.json" in res.err
    history = [json.loads(line) for line in (run_dir(repo) / "gate-history.jsonl").read_text().splitlines()]
    assert history[-1]["to"] == "INVESTIGATION" and history[-1]["passed"] is False
    events = (run_dir(repo) / "events.jsonl").read_text()
    assert "transition_refused" in events
    put(repo, "ticket.json", copy.deepcopy(TICKET))
    ok(ba("transition", "INVESTIGATION"))
    st = get(repo, "state.json")
    assert st["state"] == "INVESTIGATION" and st["history"][-1]["from"] == "INTAKE"


def test_single_active_run_unless_replaced(ba, repo):
    first = ok(ba("start", "--title", "first")).json["run_id"]
    res = ba("start", "--title", "second")
    assert res.rc == 2 and first in res.err and "--replace-active" in res.err
    second = ok(ba("start", "--title", "second", "--replace-active")).json["run_id"]
    assert second != first
    runs = {r["run_id"] for r in ok(ba("runs")).json}
    assert runs == {first, second}  # nothing deleted
    assert run_dir(repo).name == second
    ok(ba("activate", first))
    assert run_dir(repo).name == first


def test_start_requires_title(ba):
    assert ba("start", "--title", "   ").rc == 2


def test_external_block_requires_reason_and_resumes_only_to_prior_state(ba, repo):
    ok(ba("start", "--title", "t"))
    put(repo, "ticket.json", copy.deepcopy(TICKET))
    ok(ba("transition", "INVESTIGATION"))
    assert ba("transition", "BLOCKED_EXTERNAL_DEPENDENCY").rc == 2
    ok(ba("transition", "BLOCKED_EXTERNAL_DEPENDENCY", "--reason", "waiting for prod log access"))
    st = get(repo, "state.json")
    assert st["resume_to"] == "INVESTIGATION"
    assert set(allowed_targets(st)) == {"FINAL_REPORT", "INVESTIGATION"}
    res = ba("transition", "CLARIFICATION")
    assert res.rc == 2 and "not allowed" in res.err
    ok(ba("transition", "INVESTIGATION"))
    assert get(repo, "state.json")["resume_to"] is None


def test_return_to_clarification_from_late_state_requires_reason(ba, repo):
    drive(ba, repo, "CONTRACT")
    res = ba("transition", "CLARIFICATION")
    assert res.rc == 2 and "--reason" in res.err
    ok(ba("transition", "CLARIFICATION", "--reason", "contract review exposed a gap"))
    assert get(repo, "state.json")["history"][-1]["reason"] == "contract review exposed a gap"


def test_final_report_clears_active_pointer_and_records_outcome(ba, repo):
    ok(ba("start", "--title", "t"))
    rid = run_dir(repo).name
    ok(ba("transition", "BLOCKED_EXTERNAL_DEPENDENCY", "--reason", "no reproduction environment"))
    out = ok(ba("transition", "FINAL_REPORT")).json
    assert out["outcome"] == "BLOCKED"
    assert (repo / ".bug-assurance" / "active-run").read_text().strip() == ""
    st = json.loads((repo / ".bug-assurance" / "runs" / rid / "state.json").read_text())
    assert st["state"] == "FINAL_REPORT" and st["outcome"] == "BLOCKED"
    assert (repo / ".bug-assurance" / "runs" / rid / "final-report.md").exists()
    res = ba("transition", "INTAKE", "--run", rid)
    assert res.rc == 2  # terminal


def test_set_repair_limit_requires_authorized_answer_and_hard_limit(ba, repo):
    drive(ba, repo, "CLARIFICATION")
    put(repo, "decisions.json", {"questions": [open_q()]})
    guard.handle_user_prompt({"cwd": str(repo), "prompt": "You may use up to 5 repair cycles.", "session_id": "s"})
    msg = json.loads((repo / ".bug-assurance" / "inbox.jsonl").read_text().splitlines()[-1])["id"]
    ok(ba("answer", "Q-1", "--source-kind", "user", "--from-message", msg))
    ok(ba("answer", "Q-1", "--source-kind", "agent", "--text", "ten cycles is fine"))
    assert ba("set-repair-limit", "5", "--authorization", "A-002").rc == 2  # agent: unverified
    assert ok(ba("set-repair-limit", "5", "--authorization", "A-001")).json["max_cycles"] == 5
    res = ba("set-repair-limit", "11", "--authorization", "A-001")
    assert res.rc == 2 and "hard limit" in res.err
    assert "repair_limit_changed" in (run_dir(repo) / "events.jsonl").read_text()


def test_hand_edited_state_does_not_bypass_live_gate(ba, repo):
    """Forging state.json cannot unlock application edits: the guard re-evaluates the
    clarification gate from artifacts, and later transitions re-check it too."""
    drive(ba, repo, "CLARIFICATION")
    put(repo, "decisions.json", {"questions": [open_q()]})
    p = run_dir(repo) / "state.json"
    st = json.loads(p.read_text())
    st["state"] = "IMPLEMENTATION"
    p.write_text(json.dumps(st))  # tampering outside the tools (the hook blocks it inside Claude Code)
    out = guard.handle_pre_tool_use({"cwd": str(repo), "tool_name": "Edit",
                                     "tool_input": {"file_path": str(repo / "src/orders/cancellation.py")}})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "Clarification gate no longer passes" in out["hookSpecificOutput"]["permissionDecisionReason"]
    res = ba("transition", "VERIFICATION")
    assert res.rc == 3 and "[clarification]" in res.err


def test_status_reports_gate_readiness(ba, repo):
    ok(ba("start", "--title", "t"))
    st = ok(ba("status", "--json")).json
    assert st["state"] == "INTAKE" and st["transitions"]["INVESTIGATION"]["passes"] is False
    put(repo, "ticket.json", copy.deepcopy(TICKET))
    st = ok(ba("status", "--json")).json
    assert st["transitions"]["INVESTIGATION"]["passes"] is True
    human = ok(ba("status")).out
    assert "[INTAKE]" in human and "OK -> INVESTIGATION" in human
