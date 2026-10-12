"""Evidence-based final report. Every statement is derived from run artifacts; nothing is assumed."""

from __future__ import annotations

from .core import now_iso, write_json_atomic, write_text_atomic
from .gates import (
    gate_adjudication, gate_clarification, gate_final, gate_regression, gate_reviews,
    gate_verification, is_blocking, load_findings, load_questions,
)

STATUSES = ("PASS", "PASS_WITH_DOCUMENTED_RISKS", "BLOCKED", "FAIL")


def _safe(run, name, default):
    try:
        return run.load(name) if run.has(name) else default
    except Exception:  # malformed artifacts are reported by the gates, not here
        return default


def _verification_view(run, cfg):
    from .audit import current_fingerprint
    from .checks import load_verification_runs

    vruns = load_verification_runs(run)
    if not vruns:
        return None, False
    finals = [v for v in vruns if v.get("final")]
    chosen = finals[-1] if finals else vruns[-1]
    return chosen, chosen.get("fingerprint") == current_fingerprint(run.root, cfg)


def compute_status(run, cfg) -> dict:
    state = run.state()
    cur = state["state"]
    prev = state["history"][-1]["from"] if cur == "FINAL_REPORT" and state["history"] else cur
    final = gate_final(run, cfg)
    clar = gate_clarification(run, cfg)
    findings = load_findings(run)
    open_blocking = [f for f in findings if is_blocking(f)
                     and (f.get("resolution") or {}).get("status") not in ("FIXED", "ACCEPTED_RISK")]
    vview, fresh = _verification_view(run, cfg)
    results = (vview or {}).get("results", [])
    req_fail = [r["id"] for r in results if r.get("required") and r.get("status") == "FAIL"]
    req_blocked = [r["id"] for r in results if r.get("required") and r.get("status") == "BLOCKED"]
    needs_decision = [f["id"] for f in findings if f.get("disposition") in ("NEEDS_CLARIFICATION", "UNRESOLVED")]
    reasons = []
    if prev == "FINAL_VERIFICATION" and final.passed:
        status = "PASS_WITH_DOCUMENTED_RISKS" if final.risks else "PASS"
    elif cur not in ("FINAL_REPORT",) and final.passed:
        status = "PASS_WITH_DOCUMENTED_RISKS" if final.risks else "PASS"
        reasons.append("provisional: run has not reached FINAL_REPORT")
    elif prev in ("BLOCKED_AWAITING_CLARIFICATION", "BLOCKED_EXTERNAL_DEPENDENCY"):
        status = "BLOCKED"
    elif req_fail or open_blocking:
        status = "FAIL"
    elif not clar.passed or needs_decision or req_blocked or cur in ("BLOCKED_AWAITING_CLARIFICATION",
                                                                     "BLOCKED_EXTERNAL_DEPENDENCY"):
        status = "BLOCKED"
    else:
        status = "FAIL"
    if req_fail:
        reasons.append(f"required checks failed: {req_fail}")
    if req_blocked:
        reasons.append(f"required checks could not run: {req_blocked}")
    if open_blocking:
        reasons.append(f"blocking findings unresolved: {[f['id'] for f in open_blocking]}")
    if needs_decision:
        reasons.append(f"findings awaiting decisions: {needs_decision}")
    if not clar.passed:
        reasons.append("clarification gate does not pass")
    if vview is not None and not fresh:
        reasons.append("latest verification results are stale relative to the current code")
    return {"status": status, "provisional": cur != "FINAL_REPORT", "final_gate": final.to_dict(),
            "reasons": reasons, "verification": vview, "verification_fresh": fresh}


def _confidence(run, cfg, final_status: dict) -> dict:
    u = _safe(run, "understanding.json", {})
    rc = (u.get("root_cause") or {}).get("status", "UNCONFIRMED")
    clar = gate_clarification(run, cfg)
    reg = gate_regression(run, cfg)
    ver = gate_verification(run, cfg, final=True)
    rev = gate_reviews(run, cfg)
    adj = gate_adjudication(run, cfg, final=True)
    return {
        "requirement": ("INSUFFICIENT" if not clar.passed else "MEDIUM" if clar.risks else "HIGH"),
        "diagnostic": {"CONFIRMED": "HIGH", "PROBABLE": "MEDIUM"}.get(rc, "LOW"),
        "implementation": ("INSUFFICIENT" if not (rev.passed and adj.passed)
                           else "MEDIUM" if adj.risks else "HIGH"),
        "verification": ("INSUFFICIENT" if not (ver.passed and reg.passed)
                         else "MEDIUM" if (ver.risks or reg.risks) else "HIGH"),
        "basis": {
            "requirement": "clarification gate result and inference/assumption risks",
            "diagnostic": f"root_cause.status = {rc}",
            "implementation": "mandatory reviews current at final code + adjudication of findings",
            "verification": "final fingerprinted verification run + failing-before-fix regression evidence",
        },
    }


def _table(rows: list[list[str]], header: list[str]) -> str:
    def cell(v):
        return str(v if v is not None else "").replace("|", "\\|").replace("\n", " ")[:240]
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(cell(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def build_report(run, cfg) -> tuple[dict, str]:
    state = run.state()
    st = compute_status(run, cfg)
    conf = _confidence(run, cfg, st)
    ticket = _safe(run, "ticket.json", {})
    u = _safe(run, "understanding.json", {})
    contract = _safe(run, "contract.json", {})
    impl = _safe(run, "implementation.json", {})
    audit = _safe(run, "diff-audit.json", {})
    plan = _safe(run, "regression-plan.json", {})
    rr = _safe(run, "regression-run.json", {})
    try:
        questions = load_questions(run)
    except Exception:
        questions = []
    findings = load_findings(run)
    v = st["verification"] or {}
    final_gate = st["final_gate"]

    rc = u.get("root_cause") or {}
    hyps = {h.get("id"): h for h in u.get("hypotheses", []) if isinstance(h, dict)}
    rch = hyps.get(rc.get("hypothesis"), {})
    md = [f"# Bug Assurance Report: {state['title']}", ""]
    md += [f"**Overall status: {st['status']}**" + (" (provisional; run not finalized)" if st["provisional"] else ""),
           "",
           "> PASS means the defined workflow succeeded and the recorded evidence supports readiness for "
           "human review. It does not guarantee the absence of defects.", ""]
    md += [f"- Run: `{run.id}`  ", f"- Generated: {now_iso()}  ",
           f"- Final state: `{state['state']}`; path: " +
           " -> ".join([h["from"] for h in state["history"][:1]] + [h["to"] for h in state["history"]]), ""]
    if st["reasons"]:
        md += ["**Status reasons:**", *[f"- {r}" for r in st["reasons"]], ""]

    md += ["## Ticket summary", "",
           f"- Title: {ticket.get('title', '-')}", f"- Symptom: {ticket.get('symptom', '-')}",
           f"- Expected: {ticket.get('expected_behavior', '-')}", f"- Actual: {ticket.get('actual_behavior', '-')}",
           f"- Risk level: {ticket.get('risk_level', '-')} ({ticket.get('risk_rationale', '')})",
           f"- Source: {(ticket.get('source') or {}).get('kind', '-')} "
           f"{(ticket.get('source') or {}).get('ref', '')} (accessed: {(ticket.get('source') or {}).get('accessed', False)})", ""]

    md += ["## Root cause", "", f"- Status: **{rc.get('status', 'UNCONFIRMED')}**"]
    if rch:
        md.append(f"- Hypothesis {rch.get('id')}: {rch.get('statement')}")
        md += [f"  - supporting ({e.get('kind')}): {e.get('ref')} {e.get('note', '')}" for e in rch.get("supporting", [])]
        md += [f"  - contradicting ({e.get('kind')}): {e.get('ref')} -> explained: {e.get('explained', 'NO')}"
               for e in rch.get("contradicting", [])]
    if rc.get("status") != "CONFIRMED":
        md.append(f"- Diagnostic limitation: {rc.get('diagnostic_limitation', 'not recorded')}")
    other = [h for hid, h in hyps.items() if hid != rc.get("hypothesis")]
    if other:
        md.append("- Alternative hypotheses: " + "; ".join(f"{h.get('id')} {h.get('status')}" for h in other))
    md.append("")

    md += ["## Confirmed requirements and decisions", ""]
    if contract.get("requirements"):
        md.append(_table([[r.get("id"), r.get("rule"), r.get("authority"), ", ".join(r.get("acceptance_criteria") or [])]
                          for r in contract["requirements"]], ["Req", "Rule", "Authority", "Acceptance criteria"]))
        md.append("")
    if questions:
        md.append(_table([[q.get("id"), q.get("status"), (q.get("resolution") or {}).get("kind", "-"),
                           (q.get("resolution") or {}).get("rule", q.get("question", ""))]
                          for q in questions], ["Question", "Status", "Resolution", "Rule / question"]))
        md.append("")
    assumptions = contract.get("accepted_assumptions") or []
    md += ["Accepted non-material assumptions: " + ("none" if not assumptions else ""),
           *[f"- {a.get('statement')} (why non-material: {a.get('why_non_material')})" for a in assumptions], ""]

    changed = audit.get("changed") or {}
    md += ["## Files changed", ""]
    files = sorted(set(changed.get("added", [])) | set(changed.get("modified", [])) | set(changed.get("deleted", [])))
    md += [f"- `{f}`" for f in files] or ["- none recorded (no diff audit)"]
    md += ["", "## Behavioral changes", "", impl.get("summary", "No implementation summary recorded."), ""]
    if impl.get("limitations"):
        md += ["Known limitations:", *[f"- {x}" for x in impl["limitations"]], ""]

    md += ["## Tests added or updated (regression evidence)", ""]
    if plan.get("feasible") is False:
        md += [f"- No failing-before-fix test: {plan.get('infeasible_reason')}",
               f"- Alternative: {plan.get('alternative_strategy')}"]
    else:
        pre = {r.get("id"): r for r in rr.get("results", [])}
        md.append(_table([[t.get("id"), " ".join(t.get("argv", [])), ", ".join(t.get("acceptance_criteria", [])),
                           pre.get(t.get("id"), {}).get("outcome", "NOT RUN")]
                          for t in plan.get("tests", [])], ["Test", "Command", "Covers", "Pre-fix outcome"]))
    md.append("")

    md += ["## Verification (actual commands and outcomes)", ""]
    if v:
        md.append(f"Run #{v.get('seq')} at {v.get('at')} (final: {v.get('final')}; "
                  f"fresh against current code: {st['verification_fresh']}).")
        md.append("")
        md.append(_table([[r.get("id"), "yes" if r.get("required") else "no", r.get("status"),
                           r.get("exit_code"), " ".join(r.get("argv") or []), r.get("detail")]
                          for r in v.get("results", [])],
                         ["Check", "Required", "Status", "Exit", "Command", "Detail"]))
    else:
        md.append("No verification has been run. **No check is reported as passing.**")
    md.append("")
    not_ok = [r for r in v.get("results", []) if r.get("status") != "PASS"] if v else []
    md += ["## Blocked, skipped or not-applicable checks", ""]
    md += [f"- `{r['id']}`: {r['status']} - {r.get('detail', '')}" for r in not_ok] or ["- none"]
    md.append("")

    md += ["## Review findings and resolutions", ""]
    if findings:
        md.append(_table([[f["id"], f.get("reviewer"), f.get("severity"), f.get("category"), f.get("summary"),
                           f.get("disposition") or "NOT ADJUDICATED",
                           (f.get("resolution") or {}).get("status")]
                          for f in findings],
                         ["ID", "Reviewer", "Severity", "Category", "Summary", "Disposition", "Resolution"]))
    else:
        md.append("No findings were recorded.")
    idx = _safe(run, "reviews-index.json", {})
    md += ["", "Reviews: " + (", ".join(f"{k}={r.get('status')}" for k, r in idx.items()) or "none recorded"), ""]

    md += ["## Confidence (qualitative, derived from evidence)", ""]
    md += [f"- {k.capitalize()} confidence: **{conf[k]}** ({conf['basis'][k]})"
           for k in ("requirement", "diagnostic", "implementation", "verification")]
    md.append("")
    md += ["## Remaining risks", ""]
    md += [f"- {r}" for r in final_gate["risks"]] or ["- none recorded"]
    md.append("")
    if final_gate["problems"]:
        md += ["## Unmet gate conditions", "", *[f"- {p}" for p in final_gate["problems"]], ""]
    ready = st["status"] in ("PASS", "PASS_WITH_DOCUMENTED_RISKS") and not st["provisional"]
    md += ["## Readiness for human PR review", "",
           f"- {'Ready for human review.' if ready else 'Not ready.'}",
           "- This workflow does not commit, push, or open pull requests; none was created by it.",
           "- CI execution, required-check enforcement and CodeRabbit review are not asserted by this report; "
           "see `ba.py github-status` for what can be verified.", ""]

    data = {"run_id": run.id, "generated_at": now_iso(), "status": st["status"], "provisional": st["provisional"],
            "reasons": st["reasons"], "confidence": conf, "root_cause_status": rc.get("status", "UNCONFIRMED"),
            "checks": [{k: r.get(k) for k in ("id", "required", "status", "exit_code", "detail")}
                       for r in v.get("results", [])] if v else [],
            "verification_fresh": st["verification_fresh"],
            "findings": [{k: f.get(k) for k in ("id", "severity", "disposition")} |
                         {"resolution": (f.get("resolution") or {}).get("status")} for f in findings],
            "risks": final_gate["risks"], "problems": final_gate["problems"], "files_changed": files}
    return data, "\n".join(md)


def write_final_report(run, cfg) -> dict:
    data, md = build_report(run, cfg)
    write_text_atomic(run.path("final-report.md"), md)
    write_json_atomic(run.path("final-report.json"), data)
    run.event("final_report", status=data["status"])
    return data
