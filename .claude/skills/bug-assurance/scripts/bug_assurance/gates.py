"""Deterministic gates. Each gate reads persisted artifacts and returns a GateResult.

A gate never trusts a status an agent wrote about itself when the underlying evidence can be
checked: decision statuses are cross-checked against answers with provenance, root-cause claims
against hypothesis evidence, verification against fingerprinted check results, and so on.

Artifact formats are documented in ``reference/artifacts.md`` of the skill; the validators below
are the normative definition.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .core import BAError, sha256_text

RISK_LEVELS = ("low", "medium", "high", "critical")
CLAIM_CLASSES = {
    "VERIFIED_OBSERVATION", "AUTHORITATIVE_REQUIREMENT", "REPOSITORY_EVIDENCE",
    "CORROBORATED_INFERENCE", "UNVERIFIED_ASSUMPTION", "CONTRADICTION", "UNKNOWN",
}
STRONG_CLAIMS = {"VERIFIED_OBSERVATION", "AUTHORITATIVE_REQUIREMENT", "REPOSITORY_EVIDENCE"}
HYPOTHESIS_STATUS = {"CANDIDATE", "SUPPORTED", "CONFIRMED", "REFUTED"}
ROOT_CAUSE_STATUS = {"CONFIRMED", "PROBABLE", "UNCONFIRMED"}
DIRECT_EVIDENCE_KINDS = {"reproduction", "test", "trace", "log", "debugger", "experiment"}
Q_STATUS = {"OPEN", "ANSWERED", "NEEDS_FOLLOW_UP", "RESOLVED", "SUPERSEDED"}
Q_KINDS = {"business_rule", "technical", "environment"}
RESOLUTION_KINDS = {"AUTHORIZED_DECISION", "VERIFIED_EVIDENCE", "SUPPORTED_INFERENCE"}
CONTRADICTION_RES = {"UNRESOLVED", "RESOLVED_BY_AUTHORIZED_DECISION", "RESOLVED_BY_EVIDENCE",
                     "NOT_A_CONTRADICTION"}
COUNTEREXAMPLE_HANDLING = {"UNHANDLED", "COVERED_BY_RULE", "ADDED_ACCEPTANCE_CRITERION",
                           "FOLLOW_UP", "NOT_MATERIAL"}
DIM_STATUS = {"VERIFIED", "AUTHORIZED_DECISION", "SUPPORTED_INFERENCE", "UNRESOLVED",
              "NOT_APPLICABLE_WITH_JUSTIFICATION"}
DIMENSIONS = (
    "expected_behavior", "actual_behavior", "business_rules", "preconditions_and_states",
    "ambiguities", "root_cause", "acceptance_criteria", "negative_and_boundary_scenarios",
    "regression_risks", "verification_strategy",
)
BUSINESS_DIMENSIONS = {"expected_behavior", "business_rules", "acceptance_criteria"}
SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFORMATIONAL")
BLOCKING_SEVERITIES = {"CRITICAL", "HIGH", "MEDIUM"}
DISPOSITIONS = {"CONFIRMED", "REJECTED_WITH_EVIDENCE", "DUPLICATE", "NEEDS_CLARIFICATION", "UNRESOLVED"}


@dataclass
class GateResult:
    name: str
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)  # non-blocking risks to carry into the report

    @property
    def passed(self) -> bool:
        return not self.problems

    def merge(self, other: "GateResult") -> "GateResult":
        self.problems += [f"[{other.name}] {p}" for p in other.problems]
        self.warnings += [f"[{other.name}] {w}" for w in other.warnings]
        self.risks += [f"[{other.name}] {r}" for r in other.risks]
        return self

    def to_dict(self) -> dict:
        return {"gate": self.name, "passed": self.passed, "problems": self.problems,
                "warnings": self.warnings, "risks": self.risks}


# -- small validation helpers -------------------------------------------------------------------

def _s(v) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _list(obj: dict, key: str, where: str, g: GateResult, required: bool = False) -> list:
    val = obj.get(key)
    if val is None:
        if required:
            g.problems.append(f"{where}.{key} is required")
        return []
    if not isinstance(val, list):
        g.problems.append(f"{where}.{key} must be a list")
        return []
    return val


def _load(run, name: str, g: GateResult, default=None):
    if not run.has(name):
        if default is not None:
            return default
        g.problems.append(f"missing artifact {name}")
        return None
    try:
        data = run.load(name)
    except BAError as exc:
        g.problems.append(str(exc))
        return None
    return data


def file_digest(run, name: str) -> str | None:
    p = run.path(name)
    return sha256_text(p.read_text(encoding="utf-8")) if p.exists() else None


def risk_level(run) -> str:
    try:
        lvl = run.load("ticket.json").get("risk_level", "high")
    except BAError:
        return "high"
    return lvl if lvl in RISK_LEVELS else "high"


# -- INTAKE -------------------------------------------------------------------------------------

def gate_ticket(run, cfg) -> GateResult:
    g = GateResult("ticket")
    t = _load(run, "ticket.json", g)
    if not isinstance(t, dict):
        if t is not None:
            g.problems.append("ticket.json must be an object")
        return g
    for key in ("title", "symptom", "expected_behavior", "actual_behavior", "risk_rationale"):
        if not _s(t.get(key)):
            g.problems.append(f"ticket.{key} is required (use 'UNKNOWN' and list it in missing_information if unknown)")
    if t.get("risk_level") not in RISK_LEVELS:
        g.problems.append(f"ticket.risk_level must be one of {RISK_LEVELS}")
    src = t.get("source")
    if not isinstance(src, dict) or not _s(src.get("kind")) or not isinstance(src.get("accessed"), bool):
        g.problems.append("ticket.source must be {kind, ref, accessed: bool}")
    elif src.get("kind") in ("github_issue", "url", "jira") and not src["accessed"]:
        g.warnings.append("ticket source was not accessed; content comes from the user's description only")
    for key in ("reproduction_steps", "affected_components", "preconditions", "evidence",
                "acceptance_criteria", "missing_information", "potential_side_effects"):
        _list(t, key, "ticket", g)
    unknowns = [k for k in ("expected_behavior", "actual_behavior") if str(t.get(k, "")).strip().upper() == "UNKNOWN"]
    if unknowns and not t.get("missing_information"):
        g.problems.append(f"ticket marks {unknowns} UNKNOWN but missing_information is empty")
    return g


# -- INVESTIGATION ------------------------------------------------------------------------------

def gate_investigation(run, cfg) -> GateResult:
    g = GateResult("investigation")
    report = run.path("investigation.md")
    if not report.exists() or len(report.read_text(encoding="utf-8").strip()) < 200:
        g.problems.append("investigation.md is missing or too thin (< 200 chars) to be a traced investigation")
    u = _load(run, "understanding.json", g)
    if not isinstance(u, dict):
        return g
    for key in ("symptom", "expected_behavior", "actual_behavior", "trigger", "initial_state"):
        if not _s(u.get(key)):
            g.problems.append(f"understanding.{key} is required")
    bm = u.get("behavior_model")
    if not isinstance(bm, dict) or not all(_s(bm.get(k)) for k in (
            "preconditions", "action", "expected_state_transition", "expected_side_effects")):
        g.problems.append("understanding.behavior_model needs preconditions, action, "
                          "expected_state_transition, expected_side_effects")
    for key in ("actors", "business_rules", "side_effects", "missing_information",
                "implementation_changing_decisions"):
        _list(u, key, "understanding", g, required=True)
    ids = set()
    for i, c in enumerate(_list(u, "claims", "understanding", g, required=True)):
        where = f"understanding.claims[{i}]"
        if not isinstance(c, dict) or not re.fullmatch(r"C-\d+", str(c.get("id", ""))):
            g.problems.append(f"{where}.id must look like C-1")
            continue
        if c["id"] in ids:
            g.problems.append(f"duplicate claim id {c['id']}")
        ids.add(c["id"])
        if c.get("classification") not in CLAIM_CLASSES:
            g.problems.append(f"{c['id']}.classification must be one of {sorted(CLAIM_CLASSES)}")
        if not _s(c.get("statement")) or not _s(c.get("source")):
            g.problems.append(f"{c['id']} needs statement and source")
        if not isinstance(c.get("material"), bool):
            g.problems.append(f"{c['id']}.material must be true/false")
    if not u.get("claims"):
        g.problems.append("understanding.claims is empty; record the evidence behind the bug model")
    hyps = _list(u, "hypotheses", "understanding", g, required=True)
    if not hyps:
        g.problems.append("at least one root-cause hypothesis is required")
    for i, h in enumerate(hyps):
        where = f"understanding.hypotheses[{i}]"
        if not isinstance(h, dict) or not re.fullmatch(r"H-\d+", str(h.get("id", ""))):
            g.problems.append(f"{where}.id must look like H-1")
            continue
        if h.get("status") not in HYPOTHESIS_STATUS:
            g.problems.append(f"{h['id']}.status must be one of {sorted(HYPOTHESIS_STATUS)}")
        for key in ("supporting", "contradicting"):
            for j, e in enumerate(_list(h, key, h["id"], g, required=True)):
                if not isinstance(e, dict) or not _s(e.get("kind")) or not _s(e.get("ref")):
                    g.problems.append(f"{h['id']}.{key}[{j}] must be {{kind, ref, note}}")
        if not _s(h.get("distinguishing_test")):
            g.problems.append(f"{h['id']}.distinguishing_test is required (how would we tell it apart?)")
    rc = u.get("root_cause")
    if not isinstance(rc, dict) or rc.get("status") not in ROOT_CAUSE_STATUS:
        g.problems.append(f"understanding.root_cause.status must be one of {sorted(ROOT_CAUSE_STATUS)}")
    return g


# -- CLARIFICATION ------------------------------------------------------------------------------

def load_questions(run) -> list[dict]:
    if not run.has("decisions.json"):
        return []
    data = run.load("decisions.json")
    qs = data.get("questions", []) if isinstance(data, dict) else None
    if not isinstance(qs, list):
        raise BAError("decisions.json must be {\"questions\": [...]}")
    return [q for q in qs if isinstance(q, dict)]


def _validate_question_shape(q: dict, g: GateResult) -> bool:
    qid = q.get("id")
    if not re.fullmatch(r"Q-\d+", str(qid or "")):
        g.problems.append(f"question id {qid!r} must look like Q-1")
        return False
    ok = True
    if q.get("status") not in Q_STATUS:
        g.problems.append(f"{qid}.status must be one of {sorted(Q_STATUS)}")
        ok = False
    if q.get("kind") not in Q_KINDS:
        g.problems.append(f"{qid}.kind must be one of {sorted(Q_KINDS)}")
        ok = False
    if not isinstance(q.get("material"), bool):
        g.problems.append(f"{qid}.material must be true/false")
        ok = False
    for key in ("topic", "uncertainty", "question"):
        if not _s(q.get(key)):
            g.problems.append(f"{qid}.{key} is required")
            ok = False
    if q.get("material") and len(q.get("interpretations") or []) < 2:
        g.problems.append(f"{qid}: a material question must list at least two competing interpretations")
    for i in q.get("interpretations") or []:
        if not isinstance(i, dict) or not _s(i.get("description")) or not _s(i.get("behavior_impact")):
            g.problems.append(f"{qid}: each interpretation needs description and behavior_impact")
            break
    dps = q.get("decision_points")
    if not isinstance(dps, list) or not dps or not all(
            isinstance(d, dict) and _s(d.get("id")) and _s(d.get("description")) for d in dps):
        g.problems.append(f"{qid}.decision_points must be a non-empty list of {{id, description}}")
        ok = False
    return ok


def authoritative_claim_problems(run, claim: dict, answers: dict, ledger) -> list[str]:
    """An AUTHORITATIVE_REQUIREMENT must be checkable: a verbatim quote from a repository document
    (source_path + quote) or a reference to an authorized answer (answer_ref)."""
    cid = claim.get("id")
    if claim.get("answer_ref"):
        a = answers.get(claim["answer_ref"])
        if a is None or a.get("authority") != "AUTHORIZED":
            return [f"authoritative claim {cid} cites {claim['answer_ref']!r}, which is not an authorized answer"]
        return ledger.provenance_problems(run, a)
    path, quote = claim.get("source_path"), claim.get("quote")
    if not _s(path) or not _s(quote):
        return [f"authoritative claim {cid} needs source_path + verbatim quote, or answer_ref"]
    from .core import rel_to

    rel = rel_to(run.root, path)
    if rel is None or not (run.root / rel).is_file():
        return [f"authoritative claim {cid}: source_path {path!r} is not a repository file"]
    norm = lambda s: re.sub(r"\s+", " ", s).strip().casefold()  # noqa: E731
    if norm(quote) not in norm((run.root / rel).read_text(encoding="utf-8", errors="replace")):
        return [f"authoritative claim {cid}: quote not found verbatim in {rel}"]
    return []


def _check_resolved_question(q, qs_by_id, claims, answers, ledger, run, risk, g: GateResult) -> None:
    qid = q["id"]
    res = q.get("resolution")
    if not isinstance(res, dict):
        g.problems.append(f"{qid} is RESOLVED but has no resolution record")
        return
    kind = res.get("kind")
    if kind not in RESOLUTION_KINDS:
        g.problems.append(f"{qid}.resolution.kind must be one of {sorted(RESOLUTION_KINDS)}")
        return
    if not _s(res.get("rule")):
        g.problems.append(f"{qid}.resolution.rule must state a precise, testable rule")
    covered = res.get("decision_points") or {}
    missing = [d["id"] for d in q.get("decision_points", []) if not _s(str(covered.get(d["id"], "")))]
    if missing:
        g.problems.append(f"{qid}: answer leaves decision point(s) {missing} unresolved; ask a focused follow-up")
    if not res.get("acceptance_criteria"):
        g.problems.append(f"{qid}.resolution.acceptance_criteria must reference the AC ids that test this rule")

    answer_refs = res.get("answer_refs") or []
    for ref in answer_refs:
        a = answers.get(ref)
        if a is None:
            g.problems.append(f"{qid} references unknown answer {ref}")
        elif a.get("question") != qid and a.get("question") not in (q.get("follow_ups") or []):
            g.problems.append(f"{qid} references answer {ref}, which answers {a.get('question')}")
        else:
            g.problems += ledger.provenance_problems(run, a)
    authorized = [r for r in answer_refs if answers.get(r, {}).get("authority") == "AUTHORIZED"]

    if kind == "AUTHORIZED_DECISION" and not authorized:
        g.problems.append(f"{qid}: AUTHORIZED_DECISION requires an answer with verified authority "
                          "(captured user message or repository document)")
    if kind == "VERIFIED_EVIDENCE":
        refs = res.get("evidence_refs") or []
        for r in refs:
            if r not in claims:
                g.problems.append(f"{qid} cites unknown claim {r}")
            elif claims[r].get("classification") == "AUTHORITATIVE_REQUIREMENT":
                g.problems += [f"{qid}: {p}" for p in authoritative_claim_problems(run, claims[r], answers, ledger)]
        strong = [r for r in refs if claims.get(r, {}).get("classification") in STRONG_CLAIMS]
        if not strong:
            g.problems.append(f"{qid}: VERIFIED_EVIDENCE must cite claims classified as "
                              f"{sorted(STRONG_CLAIMS)}")
        if q.get("kind") == "business_rule" and not any(
                claims.get(r, {}).get("classification") == "AUTHORITATIVE_REQUIREMENT" for r in refs):
            g.problems.append(f"{qid}: a business rule cannot be settled by existing code/tests alone; "
                              "cite an AUTHORITATIVE_REQUIREMENT or obtain an authorized decision")
    if kind == "SUPPORTED_INFERENCE":
        if q.get("material") and q.get("kind") == "business_rule":
            g.problems.append(f"{qid}: a material business-rule ambiguity cannot be resolved by inference; "
                              "ask the responsible stakeholder")
        elif q.get("material") and risk in ("high", "critical"):
            g.problems.append(f"{qid}: SUPPORTED_INFERENCE is not acceptable for material questions at "
                              f"risk level {risk}")
        elif not res.get("evidence_refs"):
            g.problems.append(f"{qid}: SUPPORTED_INFERENCE must cite supporting claims")
        else:
            g.risks.append(f"{qid} resolved by inference: {res.get('rule', '')}")

    for sup in res.get("supersedes") or []:
        if kind != "AUTHORIZED_DECISION":
            g.problems.append(f"{qid} supersedes {sup} but only an authorized decision can override "
                              "authoritative evidence")
        elif sup not in claims:
            g.problems.append(f"{qid} supersedes unknown claim {sup}")

    analysis = q.get("analysis")
    if not isinstance(analysis, dict) or not _s(analysis.get("rule")):
        g.problems.append(f"{qid}: missing analysis (interpret -> challenge -> verify) with a restated rule")
        return
    if not isinstance(analysis.get("hidden_assumptions"), list):
        g.problems.append(f"{qid}.analysis.hidden_assumptions must be a list (empty if none found)")
    for c in analysis.get("contradictions") or []:
        cres = c.get("resolution", "UNRESOLVED")
        if cres not in CONTRADICTION_RES:
            g.problems.append(f"{qid}: contradiction resolution must be one of {sorted(CONTRADICTION_RES)}")
        elif cres == "UNRESOLVED":
            g.problems.append(f"{qid}: unresolved contradiction - {c.get('description', '?')}")
        elif cres == "RESOLVED_BY_AUTHORIZED_DECISION":
            ref = c.get("resolution_ref")
            if answers.get(ref, {}).get("authority") != "AUTHORIZED":
                g.problems.append(f"{qid}: contradiction claims an authorized resolution but {ref!r} is "
                                  "not an authorized answer")
            other = c.get("with")
            if claims.get(other, {}).get("classification") == "AUTHORITATIVE_REQUIREMENT" and \
                    other not in (res.get("supersedes") or []):
                g.problems.append(f"{qid}: overriding authoritative claim {other} must be recorded in "
                                  "resolution.supersedes")
        elif cres == "RESOLVED_BY_EVIDENCE" and not _s(c.get("resolution_ref")):
            g.problems.append(f"{qid}: contradiction resolved by evidence must cite resolution_ref")
    ces = analysis.get("counterexamples")
    if not isinstance(ces, list):
        g.problems.append(f"{qid}.analysis.counterexamples must be a list")
        ces = []
    if q.get("material") and q.get("kind") == "business_rule" and not ces:
        g.problems.append(f"{qid}: construct at least one realistic counterexample for this business rule")
    for c in ces:
        h = c.get("handled", "UNHANDLED")
        if h not in COUNTEREXAMPLE_HANDLING:
            g.problems.append(f"{qid}: counterexample handling must be one of {sorted(COUNTEREXAMPLE_HANDLING)}")
        elif h == "UNHANDLED":
            g.problems.append(f"{qid}: counterexample not handled by the rule - {c.get('scenario', '?')}")
        elif h == "FOLLOW_UP":
            fu = qs_by_id.get(c.get("ref"))
            if fu is None or fu.get("status") != "RESOLVED":
                g.problems.append(f"{qid}: counterexample follow-up {c.get('ref')!r} is not resolved")
        elif h == "NOT_MATERIAL" and not _s(c.get("justification")):
            g.problems.append(f"{qid}: NOT_MATERIAL counterexample needs a justification")
        elif h == "ADDED_ACCEPTANCE_CRITERION" and not _s(c.get("ref")):
            g.problems.append(f"{qid}: counterexample must reference the added acceptance criterion")
    for fu in q.get("follow_ups") or []:
        if qs_by_id.get(fu, {}).get("status") != "RESOLVED":
            g.problems.append(f"{qid}: follow-up question {fu} is not resolved")


def gate_clarification(run, cfg) -> GateResult:
    """The Clarification Completeness Gate. Application source may not change until it passes."""
    from . import ledger

    g = GateResult("clarification")
    risk = risk_level(run)
    u = _load(run, "understanding.json", g)
    if not isinstance(u, dict):
        return g
    claims = {c.get("id"): c for c in u.get("claims", []) if isinstance(c, dict)}
    hyps = {h.get("id"): h for h in u.get("hypotheses", []) if isinstance(h, dict)}
    try:
        questions = load_questions(run)
    except BAError as exc:
        g.problems.append(str(exc))
        return g
    answers = ledger.answers(run)
    qs_by_id = {q.get("id"): q for q in questions}
    if len(qs_by_id) != len(questions):
        g.problems.append("decisions.json contains duplicate question ids")

    for q in questions:
        if not _validate_question_shape(q, g):
            continue
        status = q["status"]
        if status == "SUPERSEDED":
            if q.get("superseded_by") not in qs_by_id:
                g.problems.append(f"{q['id']} is SUPERSEDED but superseded_by does not name a question")
            continue
        if q.get("duplicate_of"):
            target = qs_by_id.get(q["duplicate_of"])
            if target is None or target.get("status") != "RESOLVED":
                g.problems.append(f"{q['id']} duplicates {q['duplicate_of']}, which is not resolved")
            continue
        if status != "RESOLVED":
            if q["material"]:
                g.problems.append(f"{q['id']} ({status}) is material and unresolved: {q['question']}")
            else:
                g.warnings.append(f"{q['id']} is non-material and still {status}")
            continue
        _check_resolved_question(q, qs_by_id, claims, answers, ledger, run, risk, g)

    resolved = [q for q in questions if q.get("status") == "RESOLVED"]
    contradiction_refs = {c.get("with") for q in resolved
                          for c in (q.get("analysis") or {}).get("contradictions", [])
                          if c.get("resolution", "UNRESOLVED") != "UNRESOLVED"}
    addressed = {cid for q in resolved for cid in (q.get("addresses_claims") or [])}
    for cid, c in claims.items():
        if not c.get("material"):
            continue
        cls = c.get("classification")
        if cls == "CONTRADICTION" and cid not in contradiction_refs and cid not in addressed:
            g.problems.append(f"material contradiction {cid} is not resolved by any decision: {c.get('statement')}")
        if cls in ("UNKNOWN", "UNVERIFIED_ASSUMPTION") and cid not in addressed:
            g.problems.append(f"material {cls.lower()} {cid} is not addressed by a resolved question: "
                              f"{c.get('statement')}")

    # Root cause: plausible is not verified.
    rc = u.get("root_cause") or {}
    status = rc.get("status")
    if status == "CONFIRMED":
        h = hyps.get(rc.get("hypothesis"))
        if h is None:
            g.problems.append("root_cause.status is CONFIRMED but root_cause.hypothesis names no hypothesis")
        else:
            if h.get("status") != "CONFIRMED":
                g.problems.append(f"root cause hypothesis {h['id']} is {h.get('status')}, not CONFIRMED")
            direct = [e for e in h.get("supporting", []) if e.get("kind") in DIRECT_EVIDENCE_KINDS]
            if not direct:
                g.problems.append(f"{h['id']} lacks direct evidence ({sorted(DIRECT_EVIDENCE_KINDS)}); "
                                  "code reading alone does not confirm a root cause")
            for e in direct:
                ref = str(e.get("ref", ""))
                if ref.startswith("evidence/") and not (run.dir / ref).is_file():
                    g.problems.append(f"{h['id']} cites evidence file {ref} that does not exist in the run")
            unexplained = [e.get("ref") for e in h.get("contradicting", []) if not _s(e.get("explained"))]
            if unexplained:
                g.problems.append(f"{h['id']} has unexplained contradicting evidence {unexplained}; "
                                  "investigate alternative hypotheses")
            for alt_id, alt in hyps.items():
                if alt_id != h["id"] and alt.get("status") not in ("REFUTED",):
                    g.problems.append(f"competing hypothesis {alt_id} is still {alt.get('status')}; "
                                      "refute it or run the distinguishing test")
    elif status in ("PROBABLE", "UNCONFIRMED"):
        if not _s(rc.get("diagnostic_limitation")):
            g.problems.append("unconfirmed root cause requires root_cause.diagnostic_limitation")
        if risk in ("high", "critical"):
            g.problems.append(f"root cause is {status} at risk level {risk}; stronger diagnostic evidence "
                              "is required before changing code")
        else:
            g.risks.append(f"root cause {status}: {rc.get('diagnostic_limitation', '')}")
    if hyps and all(h.get("status") == "REFUTED" for h in hyps.values()):
        g.problems.append("every root-cause hypothesis is refuted; investigate new alternatives")

    # Dimension assessment
    a = _load(run, "clarification.json", g)
    dims = a.get("dimensions") if isinstance(a, dict) else None
    if not isinstance(dims, dict):
        if a is not None:
            g.problems.append("clarification.json must be {\"dimensions\": {...}}")
        return g
    for name in DIMENSIONS:
        d = dims.get(name)
        if not isinstance(d, dict) or d.get("status") not in DIM_STATUS:
            g.problems.append(f"dimension {name}: status must be one of {sorted(DIM_STATUS)}")
            continue
        st, refs = d["status"], d.get("refs") or []
        if st == "UNRESOLVED":
            g.problems.append(f"dimension {name} is UNRESOLVED")
        elif st == "NOT_APPLICABLE_WITH_JUSTIFICATION":
            if len(str(d.get("justification", "")).strip()) < 15:
                g.problems.append(f"dimension {name}: not-applicable needs a real justification")
        else:
            if not refs:
                g.problems.append(f"dimension {name} is {st} but cites no evidence refs")
            for r in refs:
                if r.startswith("Q-") and qs_by_id.get(r, {}).get("status") != "RESOLVED":
                    g.problems.append(f"dimension {name} cites {r}, which is not resolved")
                if r.startswith("C-") and r not in claims:
                    g.problems.append(f"dimension {name} cites unknown claim {r}")
            if st == "AUTHORIZED_DECISION" and not any(
                    (qs_by_id.get(r, {}).get("resolution") or {}).get("kind") == "AUTHORIZED_DECISION"
                    for r in refs):
                g.problems.append(f"dimension {name} is AUTHORIZED_DECISION but cites no authorized decision")
            if st == "SUPPORTED_INFERENCE":
                if name in BUSINESS_DIMENSIONS and risk in ("high", "critical"):
                    g.problems.append(f"dimension {name} rests on inference at risk level {risk}")
                else:
                    g.risks.append(f"dimension {name} accepted as supported inference: {d.get('justification', '')}")
    if dims.get("root_cause", {}).get("status") == "VERIFIED" and status != "CONFIRMED":
        g.problems.append("dimension root_cause is VERIFIED but understanding.root_cause is not CONFIRMED")
    return g


def has_pending_material_questions(run, cfg) -> GateResult:
    from . import ledger

    g = GateResult("pending-questions")
    batch = ledger.pending_batch(run)
    if not batch["ask"]:
        g.problems.append("no material question is pending; nothing to wait for")
    return g


# -- CONTRACT -----------------------------------------------------------------------------------

def gate_contract(run, cfg) -> GateResult:
    g = GateResult("contract")
    c = _load(run, "contract.json", g)
    if not isinstance(c, dict):
        return g
    scenarios = {}
    for i, s in enumerate(_list(c, "scenarios", "contract", g, required=True)):
        if not isinstance(s, dict) or not re.fullmatch(r"AC-\d+", str(s.get("id", ""))):
            g.problems.append(f"contract.scenarios[{i}].id must look like AC-1")
            continue
        if s["id"] in scenarios:
            g.problems.append(f"duplicate scenario id {s['id']}")
        scenarios[s["id"]] = s
        if s.get("kind") not in ("positive", "negative", "boundary"):
            g.problems.append(f"{s['id']}.kind must be positive|negative|boundary")
        if not all(_s(s.get(k)) for k in ("given", "when", "then")):
            g.problems.append(f"{s['id']} needs given / when / then")
    reqs = {}
    for i, r in enumerate(_list(c, "requirements", "contract", g, required=True)):
        if not isinstance(r, dict) or not re.fullmatch(r"R-\d+", str(r.get("id", ""))):
            g.problems.append(f"contract.requirements[{i}].id must look like R-1")
            continue
        reqs[r["id"]] = r
        for key in ("rule", "source", "authority", "expected_behavior"):
            if not _s(r.get(key)):
                g.problems.append(f"{r['id']}.{key} is required")
        acs = r.get("acceptance_criteria") or []
        if not acs:
            g.problems.append(f"{r['id']} has no acceptance criteria")
        for ac in acs:
            if ac not in scenarios:
                g.problems.append(f"{r['id']} references unknown scenario {ac}")
        test = r.get("verification")
        if not isinstance(test, dict) or not (_s(test.get("test")) or _s(test.get("impractical_reason"))):
            g.problems.append(f"{r['id']}.verification must name a test or an impractical_reason")
    if not reqs:
        g.problems.append("contract has no requirements")
    for sid, s in scenarios.items():
        for rid in s.get("requirement_ids") or []:
            if rid not in reqs:
                g.problems.append(f"{sid} references unknown requirement {rid}")
        if not s.get("requirement_ids"):
            g.problems.append(f"{sid} is not linked to any requirement")
    if not any(s.get("kind") in ("negative", "boundary") for s in scenarios.values()) and \
            not _s(c.get("no_negative_scenarios_justification")):
        g.problems.append("contract has no negative/boundary scenario and no justification")
    for i, a in enumerate(_list(c, "accepted_assumptions", "contract", g)):
        if not isinstance(a, dict) or not _s(a.get("statement")) or not _s(a.get("why_non_material")):
            g.problems.append(f"contract.accepted_assumptions[{i}] needs statement and why_non_material")
        else:
            g.risks.append(f"accepted assumption: {a['statement']}")
    for key in ("excluded_behavior", "invariants", "regression_risks"):
        _list(c, key, "contract", g, required=True)
    if c.get("open_conflicts"):
        g.problems.append(f"contract lists open conflicts between acceptance criteria: {c['open_conflicts']}")
    check_ids = {chk["id"] for chk in cfg.get("checks", [])}
    for cid in _list(c, "required_checks", "contract", g, required=True):
        if cid not in check_ids:
            g.problems.append(f"required check {cid!r} is not defined in .bug-assurance/config.json")

    try:
        questions = load_questions(run)
    except BAError as exc:
        g.problems.append(str(exc))
        questions = []
    for q in questions:
        if q.get("status") != "RESOLVED" or not q.get("material"):
            continue
        for ac in (q.get("resolution") or {}).get("acceptance_criteria") or []:
            if ac not in scenarios:
                g.problems.append(f"{q['id']} decision maps to {ac}, which is not in the contract")
        for ce in (q.get("analysis") or {}).get("counterexamples") or []:
            if ce.get("handled") == "ADDED_ACCEPTANCE_CRITERION" and ce.get("ref") not in scenarios:
                g.problems.append(f"{q['id']} counterexample criterion {ce.get('ref')} is missing from the contract")
    return g


# -- REGRESSION_TEST ----------------------------------------------------------------------------

def gate_regression(run, cfg) -> GateResult:
    g = GateResult("regression")
    plan = _load(run, "regression-plan.json", g)
    if not isinstance(plan, dict):
        return g
    if not isinstance(plan.get("feasible"), bool):
        g.problems.append("regression-plan.feasible must be true/false")
        return g
    if not plan["feasible"]:
        if len(str(plan.get("infeasible_reason", "")).strip()) < 30 or \
                len(str(plan.get("alternative_strategy", "")).strip()) < 30:
            g.problems.append("an infeasible regression test needs infeasible_reason and "
                              "alternative_strategy (>= 30 chars each)")
        else:
            g.risks.append("no failing-before-fix automated test: " + plan["infeasible_reason"])
        return g
    contract = run.load("contract.json", {}) if run.has("contract.json") else {}
    acs = {s.get("id") for s in contract.get("scenarios", []) if isinstance(s, dict)}
    tests = plan.get("tests") or []
    if not tests:
        g.problems.append("regression-plan.tests is empty")
    for t in tests:
        if not isinstance(t, dict) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", str(t.get("id", ""))):
            g.problems.append("each regression test needs an id matching [a-z0-9][a-z0-9._-]*")
            continue
        if not isinstance(t.get("argv"), list) or not t["argv"]:
            g.problems.append(f"regression test {t['id']}.argv must be a non-empty list")
        for ac in t.get("acceptance_criteria") or []:
            if ac not in acs:
                g.problems.append(f"regression test {t['id']} references unknown scenario {ac}")
        if not t.get("acceptance_criteria"):
            g.problems.append(f"regression test {t['id']} must cite the acceptance criteria it verifies")
    rr = run.load("regression-run.json") if run.has("regression-run.json") else None
    if rr is None:
        g.problems.append("regression tests have not been run against the original implementation "
                          "(`ba.py regression-run`)")
        return g
    if rr.get("plan_digest") != file_digest(run, "regression-plan.json"):
        g.problems.append("regression-plan.json changed after the pre-fix run; re-run `ba.py regression-run`")
    baseline = run.load("baseline.json")
    if rr.get("app_fingerprint") != baseline.get("app_fingerprint"):
        g.problems.append("pre-fix regression run did not execute against the original application code")
    if rr.get("contract_digest") != file_digest(run, "contract.json"):
        g.warnings.append("contract changed after the pre-fix regression run")
        g.risks.append("contract changed after the pre-fix regression run; failing-before-fix evidence "
                       "may not cover the final contract")
    by_id = {r.get("id"): r for r in rr.get("results", [])}
    for t in tests:
        r = by_id.get(t.get("id"))
        if r is None:
            g.problems.append(f"regression test {t.get('id')} has no pre-fix result")
        elif r.get("outcome") == "PASSED_UNEXPECTEDLY":
            g.problems.append(f"regression test {t['id']} PASSED on the original code: it does not detect "
                              "the defect (or the bug model is wrong); revisit the test or the requirement")
        elif r.get("outcome") != "FAILED_AS_EXPECTED":
            g.problems.append(f"regression test {t['id']} outcome {r.get('outcome')}: {r.get('detail', '')}")
    return g


# -- IMPLEMENTATION -----------------------------------------------------------------------------

def gate_implementation(run, cfg) -> GateResult:
    from .audit import current_fingerprint

    g = GateResult("implementation")
    impl = _load(run, "implementation.json", g)
    audit = _load(run, "diff-audit.json", g)
    if not isinstance(impl, dict) or not isinstance(audit, dict):
        return g
    if not _s(impl.get("summary")):
        g.problems.append("implementation.summary is required")
    for key in ("files_changed", "decisions", "limitations", "acknowledged_issues"):
        _list(impl, key, "implementation", g, required=True)
    if audit.get("fingerprint") != current_fingerprint(run.root, cfg):
        g.problems.append("diff-audit.json is stale (code changed after the audit); run `ba.py diff-audit`")
    acks = {(a.get("code"), a.get("path")): a for a in impl.get("acknowledged_issues", [])
            if isinstance(a, dict) and _s(a.get("justification"))}
    for issue in audit.get("issues", []):
        key = (issue["code"], issue["path"])
        if issue["severity"] == "INFO":
            g.warnings.append(f"{issue['code']}: {issue['detail']}")
            continue
        if issue["code"] == "SECRET":
            g.problems.append(f"possible secret in {issue['path']} (cannot be acknowledged; remove it)")
        elif key in acks:
            g.risks.append(f"acknowledged {issue['code']} {issue['path']}: {acks[key]['justification']}")
        else:
            g.problems.append(f"{issue['severity']} {issue['code']} {issue['path']}: {issue['detail']} "
                              "(fix it, or acknowledge with justification in implementation.json)")
    actual = set(audit["changed"]["added"]) | set(audit["changed"]["modified"]) | set(audit["changed"]["deleted"])
    claimed = {str(f).replace("\\", "/") for f in impl.get("files_changed", [])}
    if actual - claimed:
        g.problems.append(f"implementation summary omits changed files {sorted(actual - claimed)}")
    if claimed - actual:
        g.warnings.append(f"implementation summary lists unchanged files {sorted(claimed - actual)}")
    return g


# -- VERIFICATION -------------------------------------------------------------------------------

def required_check_ids(run, cfg) -> list[str]:
    ids = [c["id"] for c in cfg.get("checks", []) if c.get("required")]
    if run.has("contract.json"):
        for cid in run.load("contract.json").get("required_checks", []) or []:
            if cid not in ids:
                ids.append(cid)
    if run.has("regression-plan.json"):
        plan = run.load("regression-plan.json")
        if plan.get("feasible"):
            ids += [f"regression:{t['id']}" for t in plan.get("tests", []) if isinstance(t, dict) and t.get("id")]
    return ids


def gate_verification(run, cfg, final: bool = False) -> GateResult:
    from .audit import current_fingerprint
    from .checks import load_verification_runs

    g = GateResult("final-verification" if final else "verification")
    fp = current_fingerprint(run.root, cfg)
    vruns = load_verification_runs(run)
    fresh = [v for v in vruns if v.get("fingerprint") == fp]
    if final:
        fresh = [v for v in fresh if v.get("final")]
        if not fresh:
            g.problems.append("no final verification run at the current code (`ba.py verify --final`)")
            return g
        fresh = fresh[-1:]
    if vruns and not fresh:
        g.problems.append("all recorded verification results are stale: code changed after the last run")
    latest: dict[str, dict] = {}
    for v in fresh:
        for r in v.get("results", []):
            if r.get("status") != "NOT_RUN" or r["id"] not in latest:
                latest[r["id"]] = r
    for cid in required_check_ids(run, cfg):
        r = latest.get(cid)
        if r is None:
            g.problems.append(f"required check {cid} has no result at the current code")
        elif r.get("status") != "PASS":
            g.problems.append(f"required check {cid} is {r.get('status')}: {r.get('detail', '')}")
    required = set(required_check_ids(run, cfg))
    for cid, r in latest.items():
        if cid not in required and r.get("status") not in ("PASS", "NOT_APPLICABLE_WITH_JUSTIFICATION"):
            g.risks.append(f"optional check {cid} is {r.get('status')}: {r.get('detail', '')}")
    return g


# -- INDEPENDENT_REVIEW / ADJUDICATION ----------------------------------------------------------

def gate_reviews(run, cfg) -> GateResult:
    from .audit import current_fingerprint

    g = GateResult("reviews")
    idx = run.load("reviews-index.json", {}) if run.has("reviews-index.json") else {}
    fp = current_fingerprint(run.root, cfg)
    for reviewer in cfg["required_reviewers"]:
        rec = idx.get(reviewer)
        if rec is None:
            g.problems.append(f"mandatory reviewer {reviewer} has not reported")
        elif rec.get("status") != "COMPLETED":
            g.problems.append(f"mandatory reviewer {reviewer} cannot be {rec.get('status')}")
        elif rec.get("fingerprint") != fp:
            g.problems.append(f"{reviewer} review is stale (code changed since); re-review changed behavior")
    for reviewer in cfg.get("conditional_reviewers", []):
        rec = idx.get(reviewer)
        if rec is None:
            g.problems.append(f"{reviewer} reviewer must report COMPLETED or NOT_APPLICABLE with rationale")
        elif rec.get("fingerprint") != fp:
            g.problems.append(f"{reviewer} review is stale (code changed since)")
        elif rec.get("status") == "NOT_APPLICABLE" and len(str(rec.get("rationale", "")).strip()) < 20:
            g.problems.append(f"{reviewer} marked not applicable without an explicit rationale")
    return g


def load_findings(run) -> list[dict]:
    return run.load("findings.json", {"findings": []}).get("findings", []) if run.has("findings.json") else []


def is_blocking(f: dict) -> bool:
    return f.get("disposition") == "CONFIRMED" and f.get("severity") in BLOCKING_SEVERITIES


def gate_adjudication(run, cfg, final: bool = False) -> GateResult:
    from .core import read_jsonl

    g = GateResult("adjudication")
    findings = load_findings(run)
    ids = {f["id"] for f in findings}
    logged = {e.get("finding") for e in read_jsonl(run.path("findings-log.jsonl")) if e.get("event") == "ingested"}
    if logged - ids:
        g.problems.append(f"findings disappeared from findings.json: {sorted(logged - ids)}")
    for f in findings:
        disp = f.get("disposition")
        if disp is None:
            g.problems.append(f"{f['id']} ({f.get('severity')}) has not been adjudicated")
        elif disp == "NEEDS_CLARIFICATION":
            g.problems.append(f"{f['id']} needs a business decision ({f.get('question')}); return to CLARIFICATION")
        elif disp == "UNRESOLVED":
            g.problems.append(f"{f['id']} is UNRESOLVED: {f.get('summary', '')}")
        if final and disp == "CONFIRMED":
            res = (f.get("resolution") or {}).get("status", "OPEN")
            if is_blocking(f) and res not in ("FIXED", "ACCEPTED_RISK"):
                g.problems.append(f"blocking finding {f['id']} ({f['severity']}) is still {res}")
            elif res == "ACCEPTED_RISK":
                g.risks.append(f"{f['id']} accepted as risk: {f.get('summary', '')}")
            elif res == "OPEN":
                g.risks.append(f"non-blocking finding {f['id']} ({f['severity']}) left open: {f.get('summary', '')}")
    return g


def actionable_findings(run) -> list[dict]:
    return [f for f in load_findings(run) if f.get("disposition") == "CONFIRMED"
            and (f.get("resolution") or {}).get("status", "OPEN") == "OPEN"
            and f.get("severity") != "INFORMATIONAL"]


def gate_has_actionable(run, cfg) -> GateResult:
    g = GateResult("actionable-findings")
    if not actionable_findings(run):
        g.problems.append("no confirmed, open, actionable finding to repair")
    return g


def gate_no_open_blocking(run, cfg) -> GateResult:
    g = gate_adjudication(run, cfg, final=True)
    g.name = "no-open-blocking"
    return g


def gate_repair_budget(run, cfg) -> GateResult:
    g = GateResult("repair-budget")
    rep = run.state()["repair"]
    if rep["cycles"] >= rep["max_cycles"]:
        g.problems.append(f"repair cycle limit reached ({rep['cycles']}/{rep['max_cycles']}); "
                          "stop and report FAIL or BLOCKED")
    return g


def gate_verification_failed(run, cfg) -> GateResult:
    g = GateResult("verification-failed")
    if gate_verification(run, cfg).passed:
        g.problems.append("verification is passing; FAILED_VERIFICATION is not applicable")
    return g


def gate_final(run, cfg) -> GateResult:
    """Everything that must hold for a successful FINAL_REPORT."""
    g = GateResult("final")
    for sub in (gate_clarification(run, cfg), gate_contract(run, cfg), gate_regression(run, cfg),
                gate_implementation(run, cfg), gate_verification(run, cfg, final=True),
                gate_reviews(run, cfg), gate_adjudication(run, cfg, final=True)):
        g.merge(sub)
    return g


GATES = {
    "ticket": gate_ticket,
    "investigation": gate_investigation,
    "clarification": gate_clarification,
    "pending-questions": has_pending_material_questions,
    "contract": gate_contract,
    "regression": gate_regression,
    "implementation": gate_implementation,
    "verification": gate_verification,
    "verification-failed": gate_verification_failed,
    "final-verification": lambda run, cfg: gate_verification(run, cfg, final=True),
    "reviews": gate_reviews,
    "adjudication": gate_adjudication,
    "actionable-findings": gate_has_actionable,
    "no-open-blocking": gate_no_open_blocking,
    "repair-budget": gate_repair_budget,
    "final": gate_final,
}


def evaluate(run, cfg, names) -> GateResult:
    total = GateResult("+".join(names) if names else "none")
    for name in names:
        total.merge(GATES[name](run, cfg))
    return total
