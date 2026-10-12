"""Question/answer ledger: user-message capture, answer provenance, and the pending-question batch.

Answers are recorded only through ``ba.py answer`` into the script-owned ``answers.jsonl``.
An answer is AUTHORIZED only with verifiable provenance:
  * ``user``     - its quote must appear verbatim in a user message captured by the
                   UserPromptSubmit hook (``.bug-assurance/inbox.jsonl``), or
  * ``document`` - its quote must appear verbatim in an existing repository file.
Anything else (agent statements, unsourced ticket claims) is recorded as UNVERIFIED and cannot
resolve a business decision on its own.
"""

from __future__ import annotations

import re
from pathlib import Path

from .core import (
    ARTIFACT_DIR, BAError, append_jsonl, now_iso, read_jsonl, redact, rel_to, sha256_text,
)

SOURCE_KINDS = {"user", "document", "ticket", "agent"}
MAX_MESSAGE_CHARS = 8000


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().casefold()


# -- inbox (captured user prompts) ------------------------------------------------------------

def inbox_path(root: Path) -> Path:
    return root / ARTIFACT_DIR / "inbox.jsonl"


# Harness-generated turns (background-task notifications, subagent hand-backs, system reminders)
# reach UserPromptSubmit like typed prompts. They are model or system output, never user decisions.
_SYSTEM_MARKERS = re.compile(
    r"<task-notification>|<system-reminder\b|\[SYSTEM NOTIFICATION|<agent-message\b|"
    r"\[Subagent hand-back\]|<command-name>|<local-command-stdout>",
    re.I,
)


def is_system_generated(text: str) -> bool:
    return bool(_SYSTEM_MARKERS.search(text or ""))


def capture_message(root: Path, text: str, session_id: str = "") -> dict | None:
    if not text or not text.strip() or is_system_generated(text):
        return None
    clean = redact(text)[:MAX_MESSAGE_CHARS]
    digest = sha256_text(clean)
    msg_id = "M-" + now_iso().replace("-", "").replace(":", "").split(".")[0] + "-" + digest[:6]
    record = {"id": msg_id, "at": now_iso(), "sha256": digest, "text": clean,
              "session_id": session_id[:80]}
    append_jsonl(inbox_path(root), record)
    return record


def inbox(root: Path) -> dict[str, dict]:
    out = {}
    for rec in read_jsonl(inbox_path(root)):
        # Tampered records (hash mismatch) and system-generated text are never user evidence.
        if rec.get("id") and rec.get("sha256") == sha256_text(rec.get("text", "")) \
                and not is_system_generated(rec.get("text", "")):
            out[rec["id"]] = rec
    return out


# -- answers ------------------------------------------------------------------------------------

def answers(run) -> dict[str, dict]:
    return {a["id"]: a for a in read_jsonl(run.path("answers.jsonl")) if a.get("id")}


def provenance_problems(run, answer: dict) -> list[str]:
    """Re-verify an answer's authority claim against the captured evidence."""
    if answer.get("authority") != "AUTHORIZED":
        return []
    kind, quote = answer.get("source_kind"), answer.get("text", "")
    if not _norm(quote):
        return [f"{answer['id']}: empty answer text"]
    if kind == "user":
        msg = inbox(run.root).get(answer.get("inbox_ref") or "")
        if msg is None:
            return [f"{answer['id']}: inbox message {answer.get('inbox_ref')!r} not found or tampered"]
        if _norm(quote) not in _norm(msg["text"]):
            return [f"{answer['id']}: quoted text does not appear in captured message {msg['id']}"]
        return []
    if kind == "document":
        rel = rel_to(run.root, answer.get("source_name") or "")
        path = run.root / rel if rel is not None else None
        if path is None or not path.is_file():
            return [f"{answer['id']}: source document {answer.get('source_name')!r} not found in repository"]
        if _norm(quote) not in _norm(path.read_text(encoding="utf-8", errors="replace")):
            return [f"{answer['id']}: quoted text not found in {rel}"]
        return []
    return [f"{answer['id']}: source kind {kind!r} cannot carry AUTHORIZED authority"]


def record_answer(run, question_id: str, *, source_kind: str, text: str | None = None,
                  message_id: str | None = None, source_name: str = "", on_behalf_of: str = "",
                  claim_authorized: bool = True) -> dict:
    from .gates import load_questions  # local import to avoid a cycle

    if source_kind not in SOURCE_KINDS:
        raise BAError(f"--source-kind must be one of {sorted(SOURCE_KINDS)}")
    qs = {q["id"]: q for q in load_questions(run)}
    if question_id not in qs:
        raise BAError(f"unknown question {question_id}; add it to decisions.json first")
    if source_kind == "user":
        if not message_id:
            raise BAError("user answers require --from-message <inbox id> (see `ba.py inbox`)")
        msg = inbox(run.root).get(message_id)
        if msg is None:
            raise BAError(f"inbox message {message_id} not found (or failed its integrity hash)")
        text = text or msg["text"]
    if not text or not text.strip():
        raise BAError("answer text is empty")
    existing = answers(run)
    answer = {
        "id": f"A-{len(existing) + 1:03d}",
        "question": question_id,
        "text": redact(text.strip())[:MAX_MESSAGE_CHARS],
        "source_kind": source_kind,
        "source_name": source_name,
        "on_behalf_of": on_behalf_of,
        "inbox_ref": message_id or "",
        "authority": "AUTHORIZED" if claim_authorized and source_kind in ("user", "document") else "UNVERIFIED",
        "recorded_at": now_iso(),
    }
    problems = provenance_problems(run, answer)
    if problems:
        raise BAError("answer provenance check failed:\n  - " + "\n  - ".join(problems))
    append_jsonl(run.path("answers.jsonl"), answer)
    run.event("answer_recorded", answer=answer["id"], question=question_id, authority=answer["authority"])
    return answer


# -- pending question batch ---------------------------------------------------------------------

def pending_batch(run) -> dict:
    """Material questions still needing stakeholder input, with the context needed to ask well.

    Questions duplicating an already-resolved topic are reported separately and never re-asked.
    """
    from .gates import load_questions

    questions = load_questions(run)
    resolved_by_topic = {
        q.get("topic"): q for q in questions if q.get("status") == "RESOLVED" and q.get("topic")
    }
    batch, already_known, non_material = [], [], []
    for q in questions:
        if q.get("status") in ("RESOLVED", "SUPERSEDED"):
            continue
        prior = resolved_by_topic.get(q.get("topic"))
        if prior and prior["id"] != q["id"]:
            already_known.append({"id": q["id"], "resolved_by": prior["id"],
                                  "rule": (prior.get("resolution") or {}).get("rule", "")})
            continue
        if q.get("duplicate_of"):
            continue
        if not q.get("material", True):
            non_material.append(q["id"])
            continue
        res = q.get("resolution") or {}
        covered = res.get("decision_points") or {}
        analysis = q.get("analysis") or {}
        batch.append({
            "id": q["id"],
            "status": q.get("status"),
            "question": q.get("question"),
            "why_it_matters": [i.get("behavior_impact") for i in q.get("interpretations", [])],
            "interpretations": [i.get("description") for i in q.get("interpretations", [])],
            "unresolved_decision_points": [
                dp.get("description") for dp in q.get("decision_points", [])
                if not str(covered.get(dp.get("id"), "")).strip()
            ],
            "open_contradictions": [
                c.get("description") for c in analysis.get("contradictions", [])
                if c.get("resolution", "UNRESOLVED") == "UNRESOLVED"
            ],
            "unhandled_counterexamples": [
                c.get("scenario") for c in analysis.get("counterexamples", [])
                if c.get("handled", "UNHANDLED") == "UNHANDLED"
            ],
        })
    return {"ask": batch, "already_known": already_known, "non_material_open": non_material}
