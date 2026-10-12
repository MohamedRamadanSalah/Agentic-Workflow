"""Synthetic, valid run artifacts for the fixture bug (BUG-101) and a driver that walks a run
through the state machine. Tests mutate these to exercise specific gate failures."""

from __future__ import annotations

import copy
import json
from pathlib import Path

ORDER = ["INVESTIGATION", "CLARIFICATION", "CONTRACT", "REGRESSION_TEST", "IMPLEMENTATION",
         "VERIFICATION", "INDEPENDENT_REVIEW", "ADJUDICATION", "FINAL_VERIFICATION", "FINAL_REPORT"]


def run_dir(repo: Path) -> Path:
    run_id = (repo / ".bug-assurance" / "active-run").read_text().strip()
    return repo / ".bug-assurance" / "runs" / run_id


def put(repo: Path, name: str, data) -> Path:
    path = run_dir(repo) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data, indent=2), encoding="utf-8")
    return path


def get(repo: Path, name: str):
    return json.loads((run_dir(repo) / name).read_text(encoding="utf-8"))


TICKET = {
    "title": "BUG-101 customers cannot cancel packed orders",
    "source": {"kind": "file", "ref": "tickets/BUG-101.md", "accessed": True},
    "symptom": "Cancel is rejected for a customer's own PACKED order",
    "expected_behavior": "Customer can cancel own order until it is shipped",
    "actual_behavior": "Rejected once status is PACKED",
    "reproduction_steps": ["create order for c-1", "set PACKED", "cancel as c-1"],
    "affected_components": ["src/orders/cancellation.py"],
    "preconditions": ["actor is the owning customer", "status PACKED"],
    "evidence": ["tickets/BUG-101.md"],
    "acceptance_criteria": [],
    "missing_information": [],
    "potential_side_effects": ["support cancellation rules share the status ranking"],
    "risk_level": "medium",
    "risk_rationale": "Customer-facing order lifecycle rule; no payment or data-integrity change",
}

UNDERSTANDING = {
    "symptom": TICKET["symptom"],
    "expected_behavior": "Owning customer may cancel while status is before SHIPPED",
    "actual_behavior": "can_cancel returns False for PACKED",
    "preconditions": ["customer owns order"],
    "actors": ["customer", "support"],
    "business_rules": ["policy rule 1", "policy rule 2"],
    "initial_state": "order c-1 in PACKED",
    "trigger": "customer requests cancellation",
    "expected_transition": "PACKED -> cancellation allowed",
    "actual_transition": "request rejected",
    "side_effects": [],
    "failure_recovery": "none",
    "behavior_model": {
        "preconditions": "order owned by c-1, status PACKED",
        "action": "c-1 calls can_cancel",
        "expected_state_transition": "returns True",
        "expected_side_effects": "none in this module",
    },
    "claims": [
        {"id": "C-1", "classification": "AUTHORITATIVE_REQUIREMENT", "material": True,
         "statement": "A customer may cancel their own order until it has been shipped.",
         "source": "approved policy", "source_path": "docs/cancellation-policy.md",
         "quote": "A customer may cancel their own order until it has been shipped.", "evidence": []},
        {"id": "C-2", "classification": "REPOSITORY_EVIDENCE", "material": True,
         "statement": "customer branch compares against PACKED", "source": "src/orders/cancellation.py",
         "evidence": ["src/orders/cancellation.py:34"]},
        {"id": "C-3", "classification": "CONTRADICTION", "material": True,
         "statement": "existing test asserts packed orders cannot be cancelled, contrary to C-1",
         "source": "tests/test_cancellation.py", "evidence": []},
        {"id": "C-4", "classification": "UNVERIFIED_ASSUMPTION", "material": True,
         "statement": "packed orders cancellable only before courier label is printed (developer opinion)",
         "source": "tickets/BUG-101.md", "evidence": []},
    ],
    "hypotheses": [
        {"id": "H-1", "status": "CONFIRMED", "statement": "threshold uses PACKED instead of SHIPPED",
         "supporting": [{"kind": "reproduction", "ref": "evidence/repro.txt", "note": "returns False"}],
         "contradicting": [], "alternatives": ["H-2"],
         "distinguishing_test": "call can_cancel directly with PACKED", "remaining_uncertainty": "none"},
        {"id": "H-2", "status": "REFUTED", "statement": "UI sends a wrong status",
         "supporting": [], "contradicting": [{"kind": "reproduction", "ref": "evidence/repro.txt",
                                              "note": "domain function rejects without UI"}],
         "alternatives": [], "distinguishing_test": "domain-level call", "remaining_uncertainty": ""},
    ],
    "root_cause": {"status": "CONFIRMED", "hypothesis": "H-1"},
    "missing_information": [],
    "implementation_changing_decisions": ["is a courier-label condition part of the rule?"],
}

Q1 = {
    "id": "Q-1", "kind": "business_rule", "material": True, "topic": "customer-cancel-packed",
    "uncertainty": "whether PACKED is cancellable by the customer and whether a label condition applies",
    "question": "May a customer cancel their own PACKED order, and does a courier-label condition apply?",
    "interpretations": [
        {"id": "I1", "description": "allowed until SHIPPED", "behavior_impact": "PACKED returns True"},
        {"id": "I2", "description": "allowed only before label printed",
         "behavior_impact": "needs label state; PACKED may be False"},
    ],
    "affected": ["src/orders/cancellation.py:can_cancel"],
    "decision_points": [
        {"id": "DP1", "description": "Is PACKED cancellable by the owning customer?"},
        {"id": "DP2", "description": "Does a courier-label condition apply?"},
    ],
    "addresses_claims": ["C-4"],
    "status": "RESOLVED",
    "answers": [],
    "analysis": {
        "rule": "Owning customer may cancel iff status rank < SHIPPED; no label condition",
        "hidden_assumptions": ["status flow order is fixed"],
        "contradictions": [{"id": "X1", "with": "C-3", "description": "existing test encodes the bug",
                            "resolution": "RESOLVED_BY_EVIDENCE", "resolution_ref": "C-1"}],
        "counterexamples": [
            {"id": "CE1", "scenario": "customer cancels a SHIPPED order", "handled": "ADDED_ACCEPTANCE_CRITERION",
             "ref": "AC-2"},
            {"id": "CE2", "scenario": "another customer cancels a PACKED order", "handled": "COVERED_BY_RULE",
             "ref": "AC-3"},
        ],
    },
    "resolution": {
        "kind": "VERIFIED_EVIDENCE",
        "rule": "Owning customer may cancel while status is PENDING, PAID or PACKED",
        "decision_points": {"DP1": "yes, until shipped", "DP2": "no; policy has no label condition"},
        "evidence_refs": ["C-1"],
        "acceptance_criteria": ["AC-1", "AC-2", "AC-3"],
    },
    "follow_ups": [],
}

DIMS = {
    "expected_behavior": ["Q-1", "C-1"], "actual_behavior": ["C-2"], "business_rules": ["Q-1"],
    "preconditions_and_states": ["C-1"], "ambiguities": ["Q-1"], "root_cause": ["C-2"],
    "acceptance_criteria": ["Q-1"], "negative_and_boundary_scenarios": ["Q-1"],
    "regression_risks": ["C-3"], "verification_strategy": ["C-2"],
}
CLARIFICATION = {"dimensions": {k: {"status": "VERIFIED", "refs": v} for k, v in DIMS.items()}}

CONTRACT = {
    "requirements": [
        {"id": "R-1", "rule": "Owning customer may cancel until shipped", "source": "docs/cancellation-policy.md",
         "authority": "Product policy v3", "expected_behavior": "PENDING/PAID/PACKED -> True; later -> False",
         "acceptance_criteria": ["AC-1", "AC-2", "AC-3"], "decision_refs": ["Q-1"],
         "verification": {"test": "tests/test_regression_bug101.py"}},
        {"id": "R-2", "rule": "Support may cancel until delivered (unchanged)", "source": "policy rule 2",
         "authority": "Product policy v3", "expected_behavior": "unchanged",
         "acceptance_criteria": ["AC-4"], "verification": {"test": "tests/test_cancellation.py"}},
    ],
    "scenarios": [
        {"id": "AC-1", "kind": "positive", "given": "c-1 owns a PACKED order", "when": "c-1 cancels",
         "then": "allowed", "requirement_ids": ["R-1"]},
        {"id": "AC-2", "kind": "boundary", "given": "c-1 owns a SHIPPED order", "when": "c-1 cancels",
         "then": "rejected", "requirement_ids": ["R-1"]},
        {"id": "AC-3", "kind": "negative", "given": "c-2 does not own the PACKED order", "when": "c-2 cancels",
         "then": "rejected", "requirement_ids": ["R-1"]},
        {"id": "AC-4", "kind": "negative", "given": "a DELIVERED order", "when": "support cancels",
         "then": "rejected", "requirement_ids": ["R-2"]},
    ],
    "accepted_assumptions": [],
    "excluded_behavior": ["refund timing"],
    "invariants": ["unknown statuses raise ValueError"],
    "regression_risks": ["tests/test_cancellation.py encodes the old behavior"],
    "required_checks": ["fixture-tests"],
    "open_conflicts": [],
}

REGRESSION_TEST = '''from orders import Actor, Order, can_cancel

ALICE = Actor(id="c-1", role="customer")


def test_customer_can_cancel_packed_order():  # AC-1
    assert can_cancel(Order("o-1", "c-1", "PACKED"), ALICE)


def test_customer_cannot_cancel_shipped_order():  # AC-2
    assert not can_cancel(Order("o-1", "c-1", "SHIPPED"), ALICE)


def test_other_customer_cannot_cancel_packed_order():  # AC-3
    assert not can_cancel(Order("o-1", "c-1", "PACKED"), Actor(id="c-2", role="customer"))
'''

PLAN = {
    "feasible": True,
    "tests": [{"id": "bug101", "acceptance_criteria": ["AC-1", "AC-2", "AC-3"],
               "argv": ["{python}", "-m", "pytest", "tests/test_regression_bug101.py", "-q", "-p", "no:cacheprovider"],
               "expected_failure_exit_codes": [1]}],
}

IMPLEMENTATION = {
    "summary": "Customer cancellation threshold now SHIPPED (policy rule 1); outdated test updated.",
    "files_changed": ["src/orders/cancellation.py", "tests/test_cancellation.py", "tests/test_regression_bug101.py"],
    "decisions": ["kept support rule unchanged"],
    "limitations": [],
    "acknowledged_issues": [],
}


def review(reviewer: str, findings=None, status="COMPLETED", rationale="") -> dict:
    return {"reviewer": reviewer, "status": status, "rationale": rationale,
            "scope_inspected": ["src/orders/cancellation.py", "contract.json"], "findings": findings or []}


def finding(**over) -> dict:
    f = {"severity": "HIGH", "category": "business-logic", "file": "src/orders/cancellation.py", "line": 34,
         "summary": "support can cancel SHIPPED orders", "behavior": "returns True for support at SHIPPED",
         "failure_scenario": "support cancels after shipment", "requirement_violated": "R-2",
         "evidence": "src/orders/cancellation.py:31", "recommendation": "confirm policy",
         "confidence_rationale": "direct code reading and policy text"}
    f.update(over)
    return f


def apply_fix(repo: Path) -> None:
    src = repo / "src" / "orders" / "cancellation.py"
    src.write_text(src.read_text().replace('< _rank("PACKED")', '< _rank("SHIPPED")'))
    t = repo / "tests" / "test_cancellation.py"
    t.write_text(t.read_text().replace(
        'def test_customer_cannot_cancel_packed_order():\n    assert not can_cancel(order("PACKED"), ALICE)',
        'def test_customer_can_cancel_packed_order():\n    assert can_cancel(order("PACKED"), ALICE)'))


def ok(res, what=""):
    assert res.rc == 0, f"{what} failed rc={res.rc}\nSTDOUT:\n{res.out}\nSTDERR:\n{res.err}"
    return res


def drive(ba, repo: Path, until: str, *, before=None) -> None:
    """Walk a fresh run to state ``until``. ``before`` is an optional dict state->callable run just
    before that transition, letting tests tamper with artifacts at the right moment."""
    before = before or {}
    ok(ba("start", "--title", "BUG-101"), "start")
    for target in ORDER:
        if target == "INVESTIGATION":
            put(repo, "ticket.json", copy.deepcopy(TICKET))
        elif target == "CLARIFICATION":
            put(repo, "understanding.json", copy.deepcopy(UNDERSTANDING))
            put(repo, "evidence/repro.txt", "can_cancel(PACKED, owner) -> False on original code\n")
            put(repo, "investigation.md", "# Investigation\n\n" + ("Traced can_cancel customer branch. " * 12))
        elif target == "CONTRACT":
            put(repo, "decisions.json", {"questions": [copy.deepcopy(Q1)]})
            put(repo, "clarification.json", copy.deepcopy(CLARIFICATION))
        elif target == "REGRESSION_TEST":
            put(repo, "contract.json", copy.deepcopy(CONTRACT))
        elif target == "IMPLEMENTATION":
            (repo / "tests" / "test_regression_bug101.py").write_text(REGRESSION_TEST)
            put(repo, "regression-plan.json", copy.deepcopy(PLAN))
            assert ba("regression-run").rc == 0
        elif target == "VERIFICATION":
            apply_fix(repo)
            put(repo, "implementation.json", copy.deepcopy(IMPLEMENTATION))
            ok(ba("diff-audit"), "diff-audit")
        elif target == "INDEPENDENT_REVIEW":
            ba("verify")
        elif target == "ADJUDICATION":
            for name in ("business-logic", "regression", "test-quality"):
                p = put(repo, f"reviews/{name}.json", review(name))
                ok(ba("review-record", str(p)), f"review {name}")
            p = put(repo, "reviews/security.json",
                    review("security", status="NOT_APPLICABLE",
                           rationale="pure domain predicate; no auth boundary, input parsing or I/O changed"))
            ok(ba("review-record", str(p)), "review security")
        elif target == "FINAL_REPORT":
            ba("verify", "--final")
        if target in before:
            before[target]()
        res = ba("transition", target, "--reason", "test")
        ok(res, f"transition {target}")
        if target == until:
            return
