"""Review ingestion, evidence-based adjudication, and finding resolution.

Findings are append-only: the CLI offers no deletion, ingestion is logged to findings-log.jsonl,
and the adjudication gate fails if a logged finding is missing from findings.json.
Disagreements are settled by evidence references that the scripts can check, never by vote.
"""

from __future__ import annotations

import re
from pathlib import Path

from .core import REVIEWERS, BAError, append_jsonl, now_iso, read_json, rel_to, write_json_atomic
from .gates import DISPOSITIONS, SEVERITIES, load_findings, load_questions

FINDING_TEXT_FIELDS = ("category", "summary", "behavior", "failure_scenario", "requirement_violated",
                       "evidence", "recommendation", "confidence_rationale")
EVIDENCE_KINDS = {"verification", "decision", "code", "test", "reproduction", "regression"}
_VOTE_RX = re.compile(r"\b(majority|most reviewers|outvoted|out-voted|votes?|voted|consensus|"
                      r"\d+\s+(?:of|out of)\s+\d+\s+(?:reviewers|agents))\b", re.I)


def _save(run, findings: list[dict]) -> None:
    write_json_atomic(run.path("findings.json"), {"findings": findings})


def record_review(run, cfg: dict, review_path: Path) -> dict:
    from .audit import current_fingerprint

    review = read_json(review_path)
    if not isinstance(review, dict):
        raise BAError("review file must be a JSON object")
    reviewer = review.get("reviewer")
    if reviewer not in REVIEWERS:
        raise BAError(f"review.reviewer must be one of {sorted(REVIEWERS)}")
    status = review.get("status")
    if status not in ("COMPLETED", "NOT_APPLICABLE"):
        raise BAError("review.status must be COMPLETED or NOT_APPLICABLE")
    if status == "NOT_APPLICABLE" and len(str(review.get("rationale", "")).strip()) < 20:
        raise BAError("NOT_APPLICABLE reviews need an explicit rationale (>= 20 chars)")
    if status == "COMPLETED" and not review.get("scope_inspected"):
        raise BAError("review.scope_inspected must list the files/artifacts the reviewer actually read")
    raw = review.get("findings") or []
    if not isinstance(raw, list):
        raise BAError("review.findings must be a list")
    problems = []
    for i, f in enumerate(raw):
        if not isinstance(f, dict):
            problems.append(f"findings[{i}] must be an object")
            continue
        if f.get("severity") not in SEVERITIES:
            problems.append(f"findings[{i}].severity must be one of {SEVERITIES}")
        for key in FINDING_TEXT_FIELDS:
            if not isinstance(f.get(key), str) or not f[key].strip():
                problems.append(f"findings[{i}].{key} is required")
        if f.get("line") is not None and not isinstance(f.get("line"), int):
            problems.append(f"findings[{i}].line must be an integer or omitted")
    if problems:
        raise BAError("invalid review:\n  - " + "\n  - ".join(problems))

    fp = current_fingerprint(run.root, cfg)
    findings = load_findings(run)
    cycle = run.state()["repair"]["cycles"]
    new_ids = []
    for f in raw:
        fid = f"F-{len(findings) + 1:03d}"
        findings.append({
            "id": fid, "reviewer": reviewer, "cycle": cycle, "review_fingerprint": fp,
            **{k: f[k] for k in ("severity", *FINDING_TEXT_FIELDS)},
            "file": f.get("file", ""), "line": f.get("line"),
            "disposition": None, "disposition_rationale": "", "disposition_evidence": [],
            "duplicate_of": None, "question": None,
            "resolution": {"status": "OPEN", "evidence": [], "at": None},
            "ingested_at": now_iso(),
        })
        new_ids.append(fid)
        append_jsonl(run.path("findings-log.jsonl"), {"at": now_iso(), "event": "ingested",
                                                      "finding": fid, "reviewer": reviewer})
    _save(run, findings)
    idx = run.load("reviews-index.json", {}) if run.has("reviews-index.json") else {}
    rel = rel_to(run.root, review_path) or str(review_path)
    idx[reviewer] = {"status": status, "rationale": review.get("rationale", ""), "fingerprint": fp,
                     "recorded_at": now_iso(), "review_file": rel, "cycle": cycle,
                     "finding_ids": new_ids,
                     "history": (idx.get(reviewer, {}).get("history", []) +
                                 [{"at": now_iso(), "fingerprint": fp, "findings": new_ids}])}
    write_json_atomic(run.path("reviews-index.json"), idx)
    run.event("review_recorded", reviewer=reviewer, status=status, findings=new_ids)
    return {"reviewer": reviewer, "status": status, "new_findings": new_ids}


def _check_evidence(run, items: list[str]) -> list[str]:
    from .checks import load_verification_runs

    problems = []
    qs = {q.get("id"): q for q in load_questions(run)}
    for item in items:
        kind, _, ref = item.partition(":")
        if kind not in EVIDENCE_KINDS or not ref:
            problems.append(f"evidence {item!r} must be <kind>:<ref> with kind in {sorted(EVIDENCE_KINDS)}")
            continue
        if kind == "verification":
            seqs = {str(v.get("seq")) for v in load_verification_runs(run)}
            if ref.lstrip("0") not in {s.lstrip("0") for s in seqs}:
                problems.append(f"verification run {ref} does not exist")
        elif kind == "decision":
            if qs.get(ref, {}).get("status") != "RESOLVED":
                problems.append(f"decision {ref} is not a resolved question")
        elif kind in ("code", "test"):
            path = ref.split("::")[0]
            if re.search(r":\d+$", path):
                path = path.rsplit(":", 1)[0]
            rel = rel_to(run.root, path)
            if rel is None or not (run.root / rel).is_file():
                problems.append(f"{kind} reference {ref!r} does not point to a repository file")
        elif kind == "reproduction":
            if not (run.dir / ref).is_file():
                problems.append(f"reproduction artifact {ref!r} not found in the run directory")
        elif kind == "regression":
            rr = run.load("regression-run.json", {}) if run.has("regression-run.json") else {}
            if ref not in {r.get("id") for r in rr.get("results", [])}:
                problems.append(f"regression result {ref!r} not found")
    return problems


def adjudicate(run, finding_id: str, disposition: str, rationale: str, evidence: list[str],
               duplicate_of: str | None = None, question: str | None = None) -> dict:
    if disposition not in DISPOSITIONS:
        raise BAError(f"disposition must be one of {sorted(DISPOSITIONS)}")
    if not rationale or len(rationale.strip()) < 10:
        raise BAError("a disposition rationale is required")
    if _VOTE_RX.search(rationale):
        raise BAError("findings are resolved by evidence, tests, invariants or authorized decisions - "
                      "not by reviewer count, vote or consensus; cite evidence instead")
    findings = load_findings(run)
    by_id = {f["id"]: f for f in findings}
    f = by_id.get(finding_id)
    if f is None:
        raise BAError(f"unknown finding {finding_id}")
    problems = _check_evidence(run, evidence)
    if disposition == "REJECTED_WITH_EVIDENCE" and not evidence:
        problems.append("REJECTED_WITH_EVIDENCE requires at least one --evidence reference")
    if disposition == "DUPLICATE":
        if duplicate_of not in by_id or duplicate_of == finding_id:
            problems.append("DUPLICATE requires --duplicate-of naming another existing finding")
    if disposition == "NEEDS_CLARIFICATION":
        qs = {q.get("id"): q for q in load_questions(run)}
        if question not in qs:
            problems.append("NEEDS_CLARIFICATION requires --question naming a question in decisions.json")
        elif qs[question].get("status") == "RESOLVED":
            problems.append(f"{question} is already resolved; choose another disposition")
    if problems:
        raise BAError("adjudication refused:\n  - " + "\n  - ".join(problems))
    previous = f.get("disposition")
    f.update(disposition=disposition, disposition_rationale=rationale.strip(),
             disposition_evidence=evidence, duplicate_of=duplicate_of, question=question,
             adjudicated_at=now_iso())
    if disposition == "CONFIRMED" and f["resolution"]["status"] != "OPEN" and previous != "CONFIRMED":
        f["resolution"] = {"status": "OPEN", "evidence": [], "at": None}
    _save(run, findings)
    append_jsonl(run.path("findings-log.jsonl"), {"at": now_iso(), "event": "adjudicated",
                                                  "finding": finding_id, "from": previous,
                                                  "to": disposition})
    run.event("adjudicated", finding=finding_id, disposition=disposition)
    return f


def resolve(run, finding_id: str, status: str, evidence: list[str], authorization: str | None = None,
            note: str = "") -> dict:
    from . import ledger

    if status not in ("FIXED", "ACCEPTED_RISK", "OPEN"):
        raise BAError("--status must be FIXED, ACCEPTED_RISK or OPEN")
    findings = load_findings(run)
    f = next((x for x in findings if x["id"] == finding_id), None)
    if f is None:
        raise BAError(f"unknown finding {finding_id}")
    if f.get("disposition") != "CONFIRMED":
        raise BAError(f"{finding_id} is {f.get('disposition')}; only CONFIRMED findings are resolved")
    problems = _check_evidence(run, evidence)
    if status == "FIXED" and not any(e.split(":")[0] in ("verification", "test", "regression") for e in evidence):
        problems.append("FIXED requires verification:, test: or regression: evidence")
    if status == "ACCEPTED_RISK":
        a = ledger.answers(run).get(authorization or "")
        if a is None or a.get("authority") != "AUTHORIZED":
            problems.append("ACCEPTED_RISK requires --authorization naming an AUTHORIZED answer (A-xxx)")
    if problems:
        raise BAError("resolution refused:\n  - " + "\n  - ".join(problems))
    f["resolution"] = {"status": status, "evidence": evidence, "authorization": authorization,
                       "note": note, "at": now_iso(), "cycle": run.state()["repair"]["cycles"]}
    _save(run, findings)
    append_jsonl(run.path("findings-log.jsonl"), {"at": now_iso(), "event": "resolution",
                                                  "finding": finding_id, "status": status})
    run.event("finding_resolution", finding=finding_id, status=status)
    return f
