"""The Orchestrator's state machine. The only way to change a run's state is ``transition()``,
which evaluates the target's gates from artifacts and refuses the move when any gate fails."""

from __future__ import annotations

from .core import BAError, Run, active_run, append_jsonl, now_iso, set_active, write_json_atomic
from .gates import evaluate

TERMINAL = {"FINAL_REPORT"}
WAITING = {"BLOCKED_AWAITING_CLARIFICATION", "BLOCKED_EXTERNAL_DEPENDENCY"}

# State -> metadata and allowed transitions {target: [gates evaluated on entry]}.
STATES: dict[str, dict] = {
    "INTAKE": {
        "purpose": "Normalize the ticket into ticket.json (symptom, expected/actual, repro, risk).",
        "artifacts": ["ticket.json"],
        "next": {"INVESTIGATION": ["ticket"]},
    },
    "INVESTIGATION": {
        "purpose": "Read-only trace by ba-bug-investigator: understanding.json + investigation.md.",
        "artifacts": ["understanding.json", "investigation.md"],
        "next": {"CLARIFICATION": ["investigation"]},
    },
    "CLARIFICATION": {
        "purpose": "Ask -> Interpret -> Challenge -> Verify -> Follow up -> Record -> Gate.",
        "artifacts": ["decisions.json", "clarification.json", "answers.jsonl"],
        "next": {
            "CONTRACT": ["investigation", "clarification"],
            "BLOCKED_AWAITING_CLARIFICATION": ["pending-questions"],
            "INVESTIGATION": [],
        },
    },
    "BLOCKED_AWAITING_CLARIFICATION": {
        "purpose": "Waiting for a stakeholder answer. Application code is frozen.",
        "artifacts": ["decisions.json"],
        "next": {"CLARIFICATION": [], "FINAL_REPORT": []},
    },
    "CONTRACT": {
        "purpose": "Behavioral contract with requirements, Given/When/Then scenarios, required checks.",
        "artifacts": ["contract.json"],
        "next": {"REGRESSION_TEST": ["clarification", "contract"], "CLARIFICATION": []},
    },
    "REGRESSION_TEST": {
        "purpose": "Write the regression test from the contract; prove it fails on the original code.",
        "artifacts": ["regression-plan.json", "regression-run.json"],
        "next": {
            "IMPLEMENTATION": ["clarification", "contract", "regression"],
            "CONTRACT": [], "CLARIFICATION": [],
        },
    },
    "IMPLEMENTATION": {
        "purpose": "ba-implementer makes the smallest correct change; then `ba.py diff-audit`.",
        "artifacts": ["implementation.json", "diff-audit.json"],
        "next": {"VERIFICATION": ["clarification", "implementation"], "CLARIFICATION": []},
    },
    "VERIFICATION": {
        "purpose": "Run repository checks with `ba.py verify`; results are fingerprinted.",
        "artifacts": ["verification/*.json"],
        "next": {
            "INDEPENDENT_REVIEW": ["clarification", "implementation", "verification"],
            "FAILED_VERIFICATION": ["verification-failed"],
            "CLARIFICATION": [],
        },
    },
    "FAILED_VERIFICATION": {
        "purpose": "A required check failed or was blocked. Repair (bounded) or stop.",
        "artifacts": [],
        "next": {"REPAIR_LOOP": ["repair-budget"], "FAILED_QUALITY_GATE": [], "CLARIFICATION": []},
    },
    "INDEPENDENT_REVIEW": {
        "purpose": "Mandatory reviewers inspect the code and contract; `ba.py review-record` each.",
        "artifacts": ["reviews/*.json", "reviews-index.json", "findings.json"],
        "next": {"ADJUDICATION": ["reviews"], "CLARIFICATION": []},
    },
    "ADJUDICATION": {
        "purpose": "ba-findings-adjudicator dispositions every finding with evidence.",
        "artifacts": ["findings.json"],
        "next": {
            "REPAIR_LOOP": ["adjudication", "actionable-findings", "repair-budget"],
            "FINAL_VERIFICATION": ["adjudication", "no-open-blocking"],
            "CLARIFICATION": [],
            "FAILED_QUALITY_GATE": [],
        },
    },
    "REPAIR_LOOP": {
        "purpose": "Targeted corrections for confirmed findings or failed checks (bounded cycles).",
        "artifacts": ["implementation.json", "diff-audit.json"],
        "next": {"VERIFICATION": ["clarification", "implementation"], "CLARIFICATION": [],
                 "FAILED_QUALITY_GATE": []},
    },
    "FAILED_QUALITY_GATE": {
        "purpose": "Blocking problems remain and cannot be repaired within the limits. Report it.",
        "artifacts": [],
        "next": {"FINAL_REPORT": []},
    },
    "FINAL_VERIFICATION": {
        "purpose": "Full required suite at the final code: `ba.py verify --final`.",
        "artifacts": ["verification/*.json"],
        "next": {
            "FINAL_REPORT": ["final"],
            "FAILED_VERIFICATION": ["verification-failed"],
            "REPAIR_LOOP": ["repair-budget"],
            "CLARIFICATION": [],
        },
    },
    "BLOCKED_EXTERNAL_DEPENDENCY": {
        "purpose": "Waiting on permissions, infrastructure or an external party.",
        "artifacts": [],
        "next": {"FINAL_REPORT": []},  # plus a return to the state it was blocked from
    },
    "FINAL_REPORT": {"purpose": "Terminal. final-report.md / final-report.json written.",
                     "artifacts": ["final-report.md", "final-report.json"], "next": {}},
}

# States in which application source may be modified (the guard also requires the live
# clarification gate to pass).
APP_EDIT_STATES = {"IMPLEMENTATION", "REPAIR_LOOP"}
TEST_EDIT_STATES = {"REGRESSION_TEST", "IMPLEMENTATION", "REPAIR_LOOP"}
# Returning to CLARIFICATION from these states is a "newly discovered ambiguity".
LATE_STATES = {"CONTRACT", "REGRESSION_TEST", "IMPLEMENTATION", "VERIFICATION", "FAILED_VERIFICATION",
               "INDEPENDENT_REVIEW", "ADJUDICATION", "REPAIR_LOOP", "FINAL_VERIFICATION"}


def allowed_targets(state: dict) -> dict[str, list[str]]:
    cur = state["state"]
    targets = dict(STATES[cur]["next"])
    if cur == "BLOCKED_EXTERNAL_DEPENDENCY" and state.get("resume_to"):
        targets[state["resume_to"]] = []
    if cur not in TERMINAL and cur != "BLOCKED_EXTERNAL_DEPENDENCY":
        targets.setdefault("BLOCKED_EXTERNAL_DEPENDENCY", [])
    return targets


def start_run(root, cfg: dict, title: str, ticket_ref: str = "", replace_active: bool = False) -> Run:
    from .audit import make_baseline
    from .core import new_run_id

    current = active_run(root)
    if current is not None and current.state()["state"] not in TERMINAL and not replace_active:
        raise BAError(f"run {current.id} is active in state {current.state()['state']}; resume it with "
                      "`ba.py status` or pass --replace-active (nothing is deleted)")
    if not title or not title.strip():
        raise BAError("--title is required")
    run = Run(root, new_run_id())
    run.dir.mkdir(parents=True, exist_ok=False)
    for sub in ("reviews", "notes", "evidence"):
        (run.dir / sub).mkdir()
    baseline = make_baseline(root, cfg)
    write_json_atomic(run.path("baseline.json"), baseline)
    state = {
        "schema_version": 1, "run_id": run.id, "title": title.strip()[:200], "ticket_ref": ticket_ref,
        "state": "INTAKE", "created_at": now_iso(), "history": [],
        "repair": {"cycles": 0, "max_cycles": cfg["repair"]["max_cycles"],
                   "hard_limit": cfg["repair"]["hard_limit"]},
        "resume_to": None, "outcome": None,
    }
    run.save_state(state)
    run.event("run_started", title=title, baseline_fingerprint=baseline["fingerprint"])
    set_active(root, run)
    return run


def check_transition(run: Run, cfg: dict, target: str):
    state = run.state()
    targets = allowed_targets(state)
    if target not in STATES:
        raise BAError(f"unknown state {target}")
    if target not in targets:
        raise BAError(f"transition {state['state']} -> {target} is not allowed; allowed: {sorted(targets)}")
    return state, evaluate(run, cfg, targets[target])


def transition(run: Run, cfg: dict, target: str, reason: str = "", outcome: str | None = None) -> dict:
    state, result = check_transition(run, cfg, target)
    cur = state["state"]
    append_jsonl(run.path("gate-history.jsonl"), {"at": now_iso(), "from": cur, "to": target,
                                                     **result.to_dict()})
    if not result.passed:
        run.event("transition_refused", src=cur, dst=target, problems=result.problems)
        raise BAError(f"transition {cur} -> {target} refused by gate(s):\n  - " +
                      "\n  - ".join(result.problems), exit_code=3)
    if target in ("BLOCKED_EXTERNAL_DEPENDENCY", "FAILED_QUALITY_GATE") and not reason.strip():
        raise BAError(f"--reason is required to enter {target}")
    if target == "CLARIFICATION" and cur in LATE_STATES and not reason.strip():
        raise BAError("returning to CLARIFICATION requires --reason describing the new ambiguity")
    if target == "REPAIR_LOOP":
        state["repair"]["cycles"] += 1
    if target == "BLOCKED_EXTERNAL_DEPENDENCY":
        state["resume_to"] = cur
    elif cur == "BLOCKED_EXTERNAL_DEPENDENCY":
        state["resume_to"] = None
    state["history"].append({"from": cur, "to": target, "at": now_iso(), "reason": reason,
                             "warnings": result.warnings, "risks": result.risks})
    state["state"] = target
    write_json_atomic(run.path("gate-results.json"), {"at": now_iso(), "from": cur, "to": target,
                                                      **result.to_dict()})
    run.save_state(state)
    run.event("transition", src=cur, dst=target, reason=reason)
    if target == "FINAL_REPORT":
        from .report import write_final_report

        report = write_final_report(run, cfg)
        state = run.state()
        state["outcome"] = report["status"]
        run.save_state(state)
        set_active(run.root, None)
    return state


def set_repair_limit(run: Run, new_max: int, authorization: str) -> dict:
    from . import ledger

    state = run.state()
    rep = state["repair"]
    a = ledger.answers(run).get(authorization)
    if a is None or a.get("authority") != "AUTHORIZED":
        raise BAError("changing the repair limit requires --authorization naming an AUTHORIZED answer")
    if not (1 <= new_max <= rep["hard_limit"]):
        raise BAError(f"max cycles must be within 1..{rep['hard_limit']} (hard limit)")
    if new_max < rep["cycles"]:
        raise BAError(f"{rep['cycles']} cycles already used")
    old = rep["max_cycles"]
    rep["max_cycles"] = new_max
    run.save_state(state)
    run.event("repair_limit_changed", old=old, new=new_max, authorization=authorization)
    return state


def status(run: Run, cfg: dict) -> dict:
    state = run.state()
    targets = allowed_targets(state)
    evaluated = {}
    for target, names in targets.items():
        res = evaluate(run, cfg, names)
        evaluated[target] = {"gates": names, "passes": res.passed, "problems": res.problems[:15]}
    return {
        "run_id": run.id, "title": state["title"], "state": state["state"],
        "purpose": STATES[state["state"]]["purpose"],
        "expected_artifacts": STATES[state["state"]]["artifacts"],
        "repair": state["repair"], "resume_to": state.get("resume_to"), "outcome": state.get("outcome"),
        "transitions": evaluated,
    }
