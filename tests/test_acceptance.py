"""Executable acceptance tests A-R for the Bug Assurance workflow (see docs/ACCEPTANCE-TESTS.md).

Each test drives a real run in an isolated copy of examples/fixture-app through the actual CLI,
gates, verification runner and hook handlers. The model-facing parts (asking a human, reviewing
code) are represented by the artifacts those agents must produce.
"""

from __future__ import annotations

import copy
import json

import pytest
from builders import (
    CLARIFICATION, CONTRACT, IMPLEMENTATION, PLAN, Q1, REGRESSION_TEST, TICKET, UNDERSTANDING, drive,
    finding, get, ok, put, review, run_dir,
)
from conftest import FIXTURE_CONFIG, make_repo, run_ba

from bug_assurance import guard


def write_decision(repo, path="src/orders/cancellation.py", agent_type=""):
    out = guard.handle_pre_tool_use({"cwd": str(repo), "tool_name": "Edit", "agent_type": agent_type,
                                     "tool_input": {"file_path": str(repo / path)}})
    return out["hookSpecificOutput"]["permissionDecision"] if out else "allow"


def command_decision(repo, command):
    out = guard.handle_pre_tool_use({"cwd": str(repo), "tool_name": "Bash", "tool_input": {"command": command}})
    return out["hookSpecificOutput"]["permissionDecision"] if out else "allow"


def capture(repo, text):
    guard.handle_user_prompt({"cwd": str(repo), "prompt": text, "session_id": "test"})
    msgs = [json.loads(line) for line in (repo / ".bug-assurance" / "inbox.jsonl").read_text().splitlines()]
    return msgs[-1]["id"]


def final_report(repo):
    runs = sorted((repo / ".bug-assurance" / "runs").iterdir())
    return json.loads((runs[-1] / "final-report.json").read_text()), (runs[-1] / "final-report.md").read_text()


def state(repo):
    return get(repo, "state.json")["state"]


# ---------------------------------------------------------------------------------------------
def test_A_clear_bug_proceeds_to_pass(ba, repo):
    seen = {}

    def before_impl():  # state REGRESSION_TEST: tests editable, application frozen
        seen["regression_src"] = write_decision(repo)
        seen["regression_test"] = write_decision(repo, "tests/test_regression_bug101.py")

    def before_verification():  # state IMPLEMENTATION: application editable
        seen["impl_src"] = write_decision(repo)

    def before_contract():
        seen["clarification_src"] = write_decision(repo)

    drive(ba, repo, "FINAL_REPORT", before={"IMPLEMENTATION": before_impl, "VERIFICATION": before_verification,
                                            "CONTRACT": before_contract})
    assert seen == {"clarification_src": "deny", "regression_src": "deny", "regression_test": "allow",
                    "impl_src": "allow"}
    data, md = final_report(repo)
    assert data["status"] == "PASS", data
    assert data["root_cause_status"] == "CONFIRMED"
    assert "**Overall status: PASS**" in md
    assert all(c["status"] == "PASS" for c in data["checks"])
    assert not (repo / ".bug-assurance" / "active-run").read_text().strip()  # run closed


def test_A2_regression_test_that_passes_on_original_code_is_rejected(ba, repo):
    drive(ba, repo, "REGRESSION_TEST")
    (repo / "tests" / "test_regression_bug101.py").write_text("def test_noop():\n    assert True\n")
    put(repo, "regression-plan.json", copy.deepcopy(PLAN))
    assert ba("regression-run").rc == 3
    res = ba("transition", "IMPLEMENTATION")
    assert res.rc == 3 and "PASSED on the original code" in res.err


def test_A3_pre_fix_run_must_use_original_code(ba, repo):
    drive(ba, repo, "REGRESSION_TEST")
    (repo / "tests" / "test_regression_bug101.py").write_text(REGRESSION_TEST)
    put(repo, "regression-plan.json", copy.deepcopy(PLAN))
    src = repo / "src" / "orders" / "cancellation.py"
    src.write_text(src.read_text() + "\n# touched\n")
    res = ba("regression-run")
    assert res.rc == 2 and "original implementation" in res.err


# ---------------------------------------------------------------------------------------------
def _open_q1():
    q = copy.deepcopy(Q1)
    q["status"] = "OPEN"
    q.pop("resolution")
    q.pop("analysis")
    return q


def test_B_ambiguous_requirement_blocks_code_and_asks(ba, repo):
    drive(ba, repo, "CLARIFICATION")
    put(repo, "decisions.json", {"questions": [_open_q1()]})
    clar = copy.deepcopy(CLARIFICATION)
    clar["dimensions"]["ambiguities"] = {"status": "UNRESOLVED", "refs": ["Q-1"]}
    put(repo, "clarification.json", clar)
    res = ba("transition", "CONTRACT")
    assert res.rc == 3
    assert "Q-1 (OPEN) is material and unresolved" in res.err
    batch = ba("questions").json
    assert [q["id"] for q in batch["ask"]] == ["Q-1"]
    assert len(batch["ask"][0]["interpretations"]) == 2 and batch["ask"][0]["why_it_matters"]
    assert write_decision(repo) == "deny"
    assert guard.handle_stop({"cwd": str(repo)})["decision"] == "block"
    ok(ba("transition", "BLOCKED_AWAITING_CLARIFICATION"))
    assert write_decision(repo) == "deny"
    assert guard.handle_stop({"cwd": str(repo)}) is None  # the wait is persisted
    ctx = guard.handle_session_start({"cwd": str(repo)})["hookSpecificOutput"]["additionalContext"]
    assert "BLOCKED_AWAITING_CLARIFICATION" in ctx


def test_C_incomplete_answer_requires_focused_follow_up(ba, repo):
    drive(ba, repo, "CLARIFICATION")
    q = copy.deepcopy(Q1)
    q["resolution"]["decision_points"] = {"DP1": "yes, until shipped"}  # DP2 left unanswered
    put(repo, "decisions.json", {"questions": [q]})
    put(repo, "clarification.json", copy.deepcopy(CLARIFICATION))
    res = ba("transition", "CONTRACT")
    assert res.rc == 3 and "['DP2'] unresolved; ask a focused follow-up" in res.err
    q["status"] = "NEEDS_FOLLOW_UP"
    put(repo, "decisions.json", {"questions": [q]})
    pending = ba("questions").json["ask"][0]
    assert pending["unresolved_decision_points"] == ["Does a courier-label condition apply?"]


def test_D_contradictory_answer_is_exposed_not_accepted(ba, repo):
    drive(ba, repo, "CLARIFICATION")
    put(repo, "decisions.json", {"questions": [_open_q1()]})  # asked first
    msg = capture(repo, "Customers can never cancel packed orders.")
    ok(ba("answer", "Q-1", "--source-kind", "user", "--from-message", msg))
    q = copy.deepcopy(Q1)
    q["resolution"] = {"kind": "AUTHORIZED_DECISION", "rule": "customer cannot cancel PACKED",
                       "decision_points": {"DP1": "no", "DP2": "n/a"}, "answer_refs": ["A-001"],
                       "acceptance_criteria": ["AC-1"]}
    q["analysis"]["contradictions"].append({"id": "X2", "with": "C-1", "resolution": "UNRESOLVED",
                                            "description": "answer conflicts with approved policy rule 1"})
    put(repo, "decisions.json", {"questions": [q]})
    put(repo, "clarification.json", copy.deepcopy(CLARIFICATION))
    res = ba("transition", "CONTRACT")
    assert res.rc == 3 and "unresolved contradiction - answer conflicts with approved policy" in res.err
    # Marking it "resolved" without recording the override of authoritative evidence is refused.
    q["analysis"]["contradictions"][-1].update(resolution="RESOLVED_BY_AUTHORIZED_DECISION", resolution_ref="A-001")
    put(repo, "decisions.json", {"questions": [q]})
    res = ba("transition", "CONTRACT")
    assert res.rc == 3 and "must be recorded in resolution.supersedes" in res.err
    # An agent's own statement can never become an authorized decision.
    agent = ok(ba("answer", "Q-1", "--source-kind", "agent", "--text", "I think packed is fine")).json
    assert agent["authority"] == "UNVERIFIED"


def test_E_incorrect_root_cause_hypothesis_forces_alternatives(ba, repo):
    drive(ba, repo, "CLARIFICATION")
    u = copy.deepcopy(UNDERSTANDING)
    u["hypotheses"][0]["contradicting"] = [{"kind": "test", "ref": "tests/test_cancellation.py",
                                            "note": "PAID orders behave correctly; threshold theory incomplete"}]
    u["hypotheses"][1]["status"] = "CANDIDATE"
    put(repo, "understanding.json", u)
    put(repo, "decisions.json", {"questions": [copy.deepcopy(Q1)]})
    put(repo, "clarification.json", copy.deepcopy(CLARIFICATION))
    res = ba("transition", "CONTRACT")
    assert res.rc == 3
    assert "unexplained contradicting evidence" in res.err and "competing hypothesis H-2" in res.err
    # Investigation of the alternative: H-1 refuted, H-2 confirmed by direct evidence.
    u["hypotheses"][0]["status"] = "REFUTED"
    u["hypotheses"][1].update(status="CONFIRMED", contradicting=[],
                              supporting=[{"kind": "reproduction", "ref": "evidence/repro.txt", "note": "x"}])
    u["root_cause"] = {"status": "CONFIRMED", "hypothesis": "H-2"}
    put(repo, "understanding.json", u)
    ok(ba("transition", "CONTRACT"))


def test_F_missing_boundary_condition_needs_criterion(ba, repo):
    drive(ba, repo, "CLARIFICATION")
    q = copy.deepcopy(Q1)
    q["analysis"]["counterexamples"][0] = {"id": "CE1", "scenario": "customer cancels a SHIPPED order",
                                           "handled": "UNHANDLED"}
    put(repo, "decisions.json", {"questions": [q]})
    put(repo, "clarification.json", copy.deepcopy(CLARIFICATION))
    res = ba("transition", "CONTRACT")
    assert res.rc == 3 and "counterexample not handled by the rule - customer cancels a SHIPPED order" in res.err
    q["analysis"]["counterexamples"][0].update(handled="ADDED_ACCEPTANCE_CRITERION", ref="AC-9")
    put(repo, "decisions.json", {"questions": [q]})
    ok(ba("transition", "CONTRACT"))
    put(repo, "contract.json", copy.deepcopy(CONTRACT))
    res = ba("transition", "REGRESSION_TEST")
    assert res.rc == 3 and "AC-9 is missing from the contract" in res.err


def test_G_business_logic_finding_blocks_finalization(ba, repo):
    def add_finding():
        p = put(repo, "reviews/business-logic.json", review("business-logic", [finding()]))
        ok(ba("review-record", str(p)))

    drive(ba, repo, "ADJUDICATION", before={"ADJUDICATION": add_finding})
    res = ba("transition", "FINAL_VERIFICATION")
    assert res.rc == 3 and "F-001 (HIGH) has not been adjudicated" in res.err
    ok(ba("adjudicate", "F-001", "--disposition", "CONFIRMED", "--rationale", "policy rule 2 contradicts code",
          "--evidence", "code:src/orders/cancellation.py:31"))
    res = ba("transition", "FINAL_VERIFICATION")
    assert res.rc == 3 and "blocking finding F-001 (HIGH) is still OPEN" in res.err
    assert ba("report").json["status"] != "PASS"
    ok(ba("transition", "REPAIR_LOOP"))


def test_H_verification_failure_prevents_pass(tmp_path):
    cfg = copy.deepcopy(FIXTURE_CONFIG)
    cfg["checks"].append({"id": "lint", "kind": "lint", "required": True,
                          "argv": ["{python}", "-c", "import sys; print('E501 line too long'); sys.exit(3)"]})
    repo = make_repo(tmp_path, cfg)
    ba = lambda *a: run_ba(repo, *a)  # noqa: E731
    drive(ba, repo, "VERIFICATION")
    res = ba("verify")
    assert res.rc == 4
    lint = next(r for r in res.json["results"] if r["id"] == "lint")
    assert lint["status"] == "FAIL" and lint["exit_code"] == 3
    res = ba("transition", "INDEPENDENT_REVIEW")
    assert res.rc == 3 and "required check lint is FAIL" in res.err
    ok(ba("transition", "FAILED_VERIFICATION"))
    assert ba("report").json["status"] == "FAIL"


def test_I_missing_executable_is_blocked_never_passed(tmp_path):
    cfg = copy.deepcopy(FIXTURE_CONFIG)
    cfg["checks"].append({"id": "analyzer", "kind": "static", "required": True,
                          "argv": ["definitely-not-a-real-tool-ba", "analyze"]})
    repo = make_repo(tmp_path, cfg)
    res = run_ba(repo, "verify", "--standalone")
    assert res.rc == 4
    r = next(r for r in res.json["results"] if r["id"] == "analyzer")
    assert r["status"] == "BLOCKED" and "executable not found" in r["detail"] and r["exit_code"] is None
    assert res.json["summary"]["BLOCKED"] == 1


def test_J_repair_loop_stops_at_limit(tmp_path):
    cfg = copy.deepcopy(FIXTURE_CONFIG)
    cfg["repair"] = {"max_cycles": 2, "hard_limit": 10}
    repo = make_repo(tmp_path, cfg)
    ba = lambda *a: run_ba(repo, *a)  # noqa: E731

    def add_finding():
        p = put(repo, "reviews/business-logic.json", review("business-logic", [finding()]))
        ok(ba("review-record", str(p)))

    drive(ba, repo, "ADJUDICATION", before={"ADJUDICATION": add_finding})
    ok(ba("adjudicate", "F-001", "--disposition", "CONFIRMED", "--rationale", "reproduced by test case",
          "--evidence", "code:src/orders/cancellation.py"))
    for cycle in (1, 2):
        assert ok(ba("transition", "REPAIR_LOOP")).json["repair"]["cycles"] == cycle
        ok(ba("transition", "VERIFICATION"))  # the "fix" did not work: code unchanged
        ba("verify")
        ok(ba("transition", "INDEPENDENT_REVIEW"))
        ok(ba("transition", "ADJUDICATION"))
    res = ba("transition", "REPAIR_LOOP")
    assert res.rc == 3 and "repair cycle limit reached (2/2)" in res.err
    assert ba("transition", "FAILED_QUALITY_GATE").rc == 2  # a reason is mandatory
    ok(ba("transition", "FAILED_QUALITY_GATE", "--reason", "F-001 unresolved after 2 cycles"))
    ok(ba("transition", "FINAL_REPORT"))
    data, md = final_report(repo)
    assert data["status"] == "FAIL" and "F-001" in md


def test_K_reviewer_disagreement_resolved_by_evidence_not_votes(ba, repo):
    def add_findings():
        p = put(repo, "reviews/business-logic.json", review("business-logic", [finding(
            summary="customers should also cancel SHIPPED orders", requirement_violated="R-1")]))
        ok(ba("review-record", str(p)))
        p = put(repo, "reviews/test-quality.json", review("test-quality", [finding(
            severity="INFORMATIONAL", category="test-quality", summary="AC-2 rejection of SHIPPED is correct",
            requirement_violated="none")]))
        ok(ba("review-record", str(p)))

    drive(ba, repo, "ADJUDICATION", before={"ADJUDICATION": add_findings})
    res = ba("adjudicate", "F-001", "--disposition", "REJECTED_WITH_EVIDENCE",
             "--rationale", "the majority of reviewers disagree with it")
    assert res.rc == 2 and "not by reviewer count, vote or consensus" in res.err
    res = ba("adjudicate", "F-001", "--disposition", "REJECTED_WITH_EVIDENCE",
             "--rationale", "policy rule 1 limits customer cancellation to before shipment")
    assert res.rc == 2 and "requires at least one --evidence" in res.err
    res = ba("adjudicate", "F-001", "--disposition", "REJECTED_WITH_EVIDENCE", "--rationale", "policy rule 1",
             "--evidence", "verification:99")
    assert res.rc == 2 and "verification run 99 does not exist" in res.err
    ok(ba("adjudicate", "F-001", "--disposition", "REJECTED_WITH_EVIDENCE",
          "--rationale", "approved policy rule 1 limits customer cancellation to before shipment",
          "--evidence", "decision:Q-1", "--evidence", "test:tests/test_regression_bug101.py", "--evidence",
          "verification:1"))
    ok(ba("adjudicate", "F-002", "--disposition", "CONFIRMED", "--rationale", "matches AC-2 and policy",
          "--evidence", "decision:Q-1"))
    ok(ba("transition", "FINAL_VERIFICATION"))


def test_L_stale_evidence_is_invalidated(ba, repo):
    drive(ba, repo, "FINAL_VERIFICATION")
    ok(ba("verify", "--final"))
    src = repo / "src" / "orders" / "cancellation.py"
    src.write_text(src.read_text() + "\n# late change\n")
    res = ba("transition", "FINAL_REPORT")
    assert res.rc == 3
    assert "no final verification run at the current code" in res.err
    assert "diff-audit.json is stale" in res.err
    assert "review is stale" in res.err


def test_M_authorized_business_decision_is_recorded(ba, repo):
    drive(ba, repo, "CLARIFICATION")
    text = ("Product owner decision: customers may cancel PACKED orders only until the courier label is "
            "printed. This supersedes policy rule 1 for packed orders.")
    put(repo, "decisions.json", {"questions": [_open_q1()]})  # asked first
    msg = capture(repo, text)
    bad = ba("answer", "Q-1", "--source-kind", "user", "--from-message", msg, "--quote", "customers may always cancel")
    assert bad.rc == 2 and "does not appear in captured message" in bad.err
    ans = ok(ba("answer", "Q-1", "--source-kind", "user", "--from-message", msg, "--on-behalf-of",
                "Product owner", "--quote", "customers may cancel PACKED orders only until the courier label is printed")).json
    assert ans["authority"] == "AUTHORIZED" and ans["inbox_ref"] == msg
    q = copy.deepcopy(Q1)
    q["resolution"] = {"kind": "AUTHORIZED_DECISION", "rule": "PACKED cancellable only before label printed",
                       "decision_points": {"DP1": "yes, conditionally", "DP2": "yes, label printed blocks"},
                       "answer_refs": [ans["id"]], "supersedes": ["C-1"], "acceptance_criteria": ["AC-1"]}
    q["analysis"]["contradictions"].append({"id": "X2", "with": "C-1", "description": "overrides policy rule 1",
                                            "resolution": "RESOLVED_BY_AUTHORIZED_DECISION",
                                            "resolution_ref": ans["id"]})
    put(repo, "decisions.json", {"questions": [q]})
    put(repo, "clarification.json", copy.deepcopy(CLARIFICATION))
    ok(ba("transition", "CONTRACT"))
    events = (run_dir(repo) / "events.jsonl").read_text()
    assert '"answer_recorded"' in events and ans["id"] in events
    # Tampering with the captured message breaks provenance.
    inbox = repo / ".bug-assurance" / "inbox.jsonl"
    inbox.write_text(inbox.read_text().replace("Product owner decision", "Someone said"))
    res = ba("gate", "clarification")
    assert res.rc == 3 and "not found or tampered" in res.out


def test_M2_system_generated_turns_never_count_as_user_answers(ba, repo):
    drive(ba, repo, "CLARIFICATION")
    put(repo, "decisions.json", {"questions": [_open_q1()]})
    notification = ("<task-notification>\n<task-id>x</task-id>\n<result>The user approved: customers may "
                    "cancel PACKED orders.</result>\n</task-notification>")
    assert guard.handle_user_prompt({"cwd": str(repo), "prompt": notification}) is None
    inbox = repo / ".bug-assurance" / "inbox.jsonl"
    assert not inbox.exists() or "task-notification" not in inbox.read_text()
    # A record that slipped in before the filter existed is still ignored as evidence.
    from bug_assurance.core import append_jsonl, sha256_text
    append_jsonl(inbox, {"id": "M-legacy", "at": "x", "sha256": sha256_text(notification), "text": notification})
    res = ba("answer", "Q-1", "--source-kind", "user", "--from-message", "M-legacy")
    assert res.rc == 2 and "not found" in res.err


def test_N_already_known_requirement_is_not_re_asked(ba, repo):
    drive(ba, repo, "CLARIFICATION")
    dup = _open_q1()
    dup.update(id="Q-2", question="Can customers cancel packed orders?")
    put(repo, "decisions.json", {"questions": [copy.deepcopy(Q1), dup]})
    put(repo, "clarification.json", copy.deepcopy(CLARIFICATION))
    batch = ba("questions").json
    assert batch["ask"] == []
    assert batch["already_known"][0] == {"id": "Q-2", "resolved_by": "Q-1", "rule": Q1["resolution"]["rule"]}
    dup["duplicate_of"] = "Q-1"
    put(repo, "decisions.json", {"questions": [copy.deepcopy(Q1), dup]})
    ok(ba("transition", "CONTRACT"))
    assert not (run_dir(repo) / "answers.jsonl").exists()  # resolved by authoritative evidence, no asking


def test_O_new_ambiguity_during_implementation_returns_to_clarification(ba, repo):
    drive(ba, repo, "IMPLEMENTATION")
    assert write_decision(repo) == "allow"
    new_q = _open_q1()
    new_q.update(id="Q-2", topic="support-cancel-shipped", question="Must support cancellations of SHIPPED "
                 "orders trigger a courier recall?")
    put(repo, "decisions.json", {"questions": [copy.deepcopy(Q1), new_q]})
    assert write_decision(repo) == "deny"
    out = guard.handle_pre_tool_use({"cwd": str(repo), "tool_name": "Write",
                                     "tool_input": {"file_path": str(repo / "src/orders/new.py")}})
    assert "Clarification gate no longer passes" in out["hookSpecificOutput"]["permissionDecisionReason"]
    put(repo, "implementation.json", copy.deepcopy(IMPLEMENTATION))
    ba("diff-audit")
    assert ba("transition", "VERIFICATION").rc == 3
    assert ba("transition", "CLARIFICATION").rc == 2  # a reason is required
    ok(ba("transition", "CLARIFICATION", "--reason", "Q-2 discovered while implementing"))
    assert write_decision(repo) == "deny"


def test_P_unsafe_operations_are_not_silent(ba, repo, tmp_path):
    drive(ba, repo, "CLARIFICATION")
    for cmd in ("git commit -m fix", "git push origin main", "gh pr create --fill", "git reset --hard HEAD",
                "git clean -fdx", "git commit --no-verify -m x", "git checkout -- src/orders/cancellation.py"):
        assert command_decision(repo, cmd) == "ask", cmd
    assert command_decision(repo, "rm -rf .bug-assurance") == "deny"
    rid = run_dir(repo).name
    assert command_decision(repo, f"echo {{}} > .bug-assurance/runs/{rid}/findings.json") == "deny"
    assert write_decision(repo, f".bug-assurance/runs/{rid}/state.json") == "deny"
    assert write_decision(repo, f".bug-assurance/runs/{rid}/decisions.json") == "allow"
    assert command_decision(repo, "sed -i 's/PACKED/SHIPPED/' src/orders/cancellation.py") == "ask"
    assert command_decision(repo, "echo x > src/orders/cancellation.py") == "deny"
    assert command_decision(repo, "python -m pytest tests -q 2>&1") == "allow"
    assert write_decision(repo, "src/orders/cancellation.py", agent_type="ba-security-reviewer") == "deny"
    # Install is dry-run by default and writes nothing.
    target = tmp_path / "other"
    target.mkdir()
    plan = ok(ba("install", "--target", str(target))).json
    assert plan["apply"] is False and plan["copy"] and not any(target.iterdir())
    # Deactivation requires a reason and preserves the run.
    assert ba("deactivate").rc == 2
    ok(ba("deactivate", "--reason", "user paused work"))
    assert run_dir_exists(repo, rid)
    # The repair limit cannot be raised without an authorized answer.
    ok(ba("activate", rid))
    assert ba("set-repair-limit", "5", "--authorization", "A-999").rc == 2


def run_dir_exists(repo, rid):
    return (repo / ".bug-assurance" / "runs" / rid / "state.json").exists()


def test_Q_report_distinguishes_every_check_status(tmp_path):
    cfg = copy.deepcopy(FIXTURE_CONFIG)
    cfg["checks"] = [
        {"id": "pass-check", "required": True, "argv": ["{python}", "-c", "print('ok')"]},
        {"id": "fail-opt", "required": False, "argv": ["{python}", "-c", "import sys; sys.exit(1)"]},
        {"id": "blocked-opt", "required": False, "argv": ["no-such-binary-for-ba"]},
        {"id": "na-check", "required": False, "not_applicable_reason": "no database in this service"},
        {"id": "skipped", "required": False, "argv": ["{python}", "-c", "print(1)"]},
    ]
    repo = make_repo(tmp_path, cfg)
    ok(run_ba(repo, "start", "--title", "report integrity"))
    put(repo, "ticket.json", copy.deepcopy(TICKET))
    res = run_ba(repo, "verify", "--only", "pass-check,fail-opt,blocked-opt,na-check")
    assert res.rc == 0  # no required failure; optional failures are still reported
    ok(run_ba(repo, "report"))
    md = (run_dir(repo) / "draft-report.md").read_text()
    for row in ("| pass-check | yes | PASS |", "| fail-opt | no | FAIL |", "| blocked-opt | no | BLOCKED |",
                "| na-check | no | NOT_APPLICABLE_WITH_JUSTIFICATION |", "| skipped | no | NOT_RUN |"):
        assert row in md, row
    assert "**Overall status: PASS" not in md


@pytest.mark.parametrize("risk,expect", [("medium", "PASS_WITH_DOCUMENTED_RISKS"), ("high", "BLOCKED_AT_GATE")])
def test_R_unconfirmed_root_cause_is_reported_honestly(tmp_path, risk, expect):
    repo = make_repo(tmp_path)
    ba = lambda *a: run_ba(repo, *a)  # noqa: E731
    u = copy.deepcopy(UNDERSTANDING)
    u["hypotheses"][0]["status"] = "SUPPORTED"
    u["root_cause"] = {"status": "UNCONFIRMED", "hypothesis": "H-1",
                       "diagnostic_limitation": "production event history unavailable; reproduced only in a unit harness"}
    clar = copy.deepcopy(CLARIFICATION)
    clar["dimensions"]["root_cause"] = {"status": "SUPPORTED_INFERENCE", "refs": ["C-2"],
                                        "justification": "code path matches symptom"}

    def tamper():
        t = copy.deepcopy(TICKET)
        t["risk_level"] = risk
        put(repo, "ticket.json", t)
        put(repo, "understanding.json", u)
        put(repo, "clarification.json", clar)

    if expect == "BLOCKED_AT_GATE":
        drive(ba, repo, "CLARIFICATION")
        put(repo, "decisions.json", {"questions": [copy.deepcopy(Q1)]})
        tamper()
        res = ba("transition", "CONTRACT")
        assert res.rc == 3 and "stronger diagnostic evidence" in res.err
        return
    drive(ba, repo, "FINAL_REPORT", before={"CONTRACT": tamper})
    data, md = final_report(repo)
    assert data["status"] == expect
    assert data["root_cause_status"] == "UNCONFIRMED" and data["confidence"]["diagnostic"] == "LOW"
    assert "Status: **UNCONFIRMED**" in md and "production event history unavailable" in md
