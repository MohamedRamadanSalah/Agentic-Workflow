"""Claude Code hook handlers. Small, deterministic, and silent when the system is not in use.

PreToolUse        Enforces the edit policy of the active run (clarification gate before any
                  application-source change), protects script-owned evidence, and asks before
                  commits/pushes/PR creation or destructive git operations.
UserPromptSubmit  Captures user messages (redacted, hashed) so answers have verifiable provenance.
SessionStart      Injects the active run's status for safe resumption.
Stop              Blocks once if the run is left mid-flight without persisting a waiting state.

The shell-command analysis is defense in depth, not a security boundary: it catches common
write forms (redirects, tee, sed -i, cp/mv, Set-Content...) but cannot parse arbitrary programs.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

from .core import (
    ARTIFACT_DIR, SCRIPT_OWNED, SCRIPT_OWNED_DIRS, BAError, active_run, find_repo_root, load_config,
    rel_to,
)

WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}
REVIEW_ONLY_AGENTS = {"ba-bug-investigator", "ba-business-logic-reviewer", "ba-regression-reviewer",
                      "ba-architecture-reviewer", "ba-security-reviewer", "ba-test-quality-reviewer",
                      "ba-findings-adjudicator"}

_GIT_ASK = re.compile(
    r"\bgit(?:\s+-[cC]\s+\S+)*\s+(?:commit|push|merge|rebase|cherry-pick|revert|tag|filter-branch|"
    r"update-ref|reset\s+(?:.*\s)?--hard|clean\s+-\w*f\w*|checkout\s+(?:.*\s)?--(?:\s|$)|"
    r"restore\b|switch\s+(?:-f|--force|--discard-changes)|branch\s+-D|stash\s+(?:drop|clear))\b"
    r"|\bgit(?:\s+-[cC]\s+\S+)*\s+checkout\s+\.(?:\s|$)"
    r"|\bgh\s+(?:pr\s+(?:create|merge|close)|repo\s+(?:create|delete|edit)|release\s+create|"
    r"api\b.*(?:-X|--method)\s*(?:PUT|POST|PATCH|DELETE))"
    r"|--no-verify\b|--no-gpg-sign\b"
)
_DELETE = re.compile(r"(?:^|[\s;&|(])(?:rm|rmdir|del|erase|Remove-Item|ri|rd|unlink|shred)\b", re.I)
_WRITE_HINT = re.compile(
    r"(?<![0-9&])>>?(?!&)|\btee\b|\bsed\s+(?:-\w*\s+)*-i|\bperl\s+-\w*i|\bcp\b|\bmv\b|\bcopy\b|\bmove\b|"
    r"\bSet-Content\b|\bAdd-Content\b|\bOut-File\b|\bNew-Item\b|\bCopy-Item\b|\bMove-Item\b|"
    r"\btruncate\b|\bdd\b|\bpatch\b|\bgit\s+apply\b|\bwrite_text\b|\bopen\([^)]*['\"][wa]",
    re.I,
)
# Explicit write targets: redirections, tee, and PowerShell writers.
_STRONG_TARGET = re.compile(
    r"(?<![0-9&])>>?\s*['\"]?([^\s'\";&|]+)|\btee\s+(?:-a\s+)?['\"]?([^\s'\";&|]+)|"
    r"\b(?:Set-Content|Add-Content|Out-File)\s+(?:-(?:Path|FilePath|LiteralPath)\s+)?['\"]?([^\s'\";&|]+)",
    re.I,
)


def _decision(event: str, decision: str, reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event, "permissionDecision": decision,
                                   "permissionDecisionReason": reason}}


def _context(event: str, text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def _root(payload: dict) -> Path | None:
    start = Path(payload["cwd"]) if payload.get("cwd") and Path(payload["cwd"]).is_dir() else None
    root = find_repo_root(start)
    return root if (root / ARTIFACT_DIR).is_dir() else None


def _live_run(root: Path):
    run = active_run(root)
    if run is None:
        return None, None
    state = run.state()
    if state["state"] == "FINAL_REPORT":
        return None, None
    return run, state


def _artifact_policy(rel: str) -> str | None:
    """'deny' for script-owned evidence, None otherwise."""
    if not rel.startswith(ARTIFACT_DIR + "/"):
        return None
    parts = rel.split("/")
    if rel in (f"{ARTIFACT_DIR}/inbox.jsonl", f"{ARTIFACT_DIR}/active-run"):
        return "deny"
    if len(parts) >= 4 and parts[1] == "runs":
        name = parts[3]
        if name in SCRIPT_OWNED or name in SCRIPT_OWNED_DIRS or name.startswith("gate-") \
                or name == "draft-report.md":
            return "deny"
    return None


def evaluate_write(root: Path, cfg: dict, raw_path: str, agent_type: str = "") -> tuple[str, str] | None:
    from .audit import classify
    from .gates import gate_clarification
    from .machine import APP_EDIT_STATES, TEST_EDIT_STATES

    rel = rel_to(root, raw_path) if raw_path else None
    if rel is None:
        return None  # outside the project
    if _artifact_policy(rel) == "deny":
        return ("deny", f"{rel} is script-owned evidence. Use `ba.py` commands to change it; direct "
                        "edits would invalidate the audit trail.")
    run, state = _live_run(root)
    if run is None:
        return None
    cur = state["state"]
    if rel.startswith(ARTIFACT_DIR + "/"):
        if rel == f"{ARTIFACT_DIR}/config.json":
            return ("ask", f"Changing bug-assurance configuration during active run {run.id} ({cur}) can "
                           "weaken checks. Approve only if intended.")
        return None  # agent-owned run artifacts
    if rel.startswith(".claude/") or rel.startswith(".github/"):
        return ("ask", f"{rel} controls assurance hooks/agents/CI. Run {run.id} is active ({cur}); "
                       "approve only if this change is intended.")
    kind = classify(rel, cfg)
    if kind == "unrestricted":
        return None
    if agent_type in REVIEW_ONLY_AGENTS:
        return ("deny", f"{agent_type} is a read-only role and may not modify {rel}.")
    allowed = TEST_EDIT_STATES if kind == "tests" else APP_EDIT_STATES
    label = "test" if kind == "tests" else "application/source"
    if cur not in allowed:
        return ("deny", f"Bug-assurance run {run.id} is in {cur}. {label} files ({rel}) may only be "
                        f"modified in {sorted(allowed)}. Finish the earlier stages first "
                        "(`ba.py status`).")
    gate = gate_clarification(run, cfg)
    if not gate.passed:
        shown = "\n - ".join(gate.problems[:6])
        return ("deny", f"Clarification gate no longer passes for run {run.id}; {label} changes are "
                        f"frozen until it is resolved:\n - {shown}\nReturn to CLARIFICATION with "
                        "`ba.py transition CLARIFICATION --reason ...`.")
    return None


def _tokens(command: str) -> list[str]:
    try:
        toks = shlex.split(command, posix=True)
    except ValueError:
        toks = command.split()
    out = []
    for t in toks:
        out += [p for p in re.split(r"[<>|;&()=,]+", t) if p]
    return out


def evaluate_command(root: Path, cfg: dict, command: str, agent_type: str = "") -> tuple[str, str] | None:
    from .audit import classify
    from .machine import APP_EDIT_STATES, TEST_EDIT_STATES

    if not command:
        return None
    if cfg["guard"].get("ask_on_git_publish", True) and _GIT_ASK.search(command):
        return ("ask", "This command commits, publishes, rewrites history, discards changes or bypasses "
                       "hooks. The bug-assurance policy requires explicit user authorization.")
    toks = _tokens(command)
    rels = [r for r in (rel_to(root, t) for t in toks if ("/" in t or "\\" in t or "." in t)) if r]
    if _DELETE.search(command) and any(r == ARTIFACT_DIR or r.startswith(ARTIFACT_DIR + "/") for r in rels):
        return ("deny", "Deleting bug-assurance artifacts is not permitted; runs are an audit trail.")
    if not _WRITE_HINT.search(command):
        return None
    strong = {r for m in _STRONG_TARGET.finditer(command) for g in m.groups() if g
              for r in [rel_to(root, g)] if r}
    if any(_artifact_policy(r) == "deny" for r in strong | set(rels)):
        return ("deny", "Shell writes to script-owned bug-assurance evidence are not permitted.")
    run, state = _live_run(root)
    if run is None:
        return None
    cur = state["state"]
    # Strong targets are definitely written; other existing files named by a write-like command
    # might be (cp/mv/sed -i sources or destinations), so those are escalated to the user.
    weak = {r for r in rels if (root / r).is_file()} - strong
    for r, verdict in [(r, "deny") for r in sorted(strong)] + [(r, "ask") for r in sorted(weak)]:
        kind = classify(r, cfg)
        if kind in ("artifact", "unrestricted"):
            continue
        allowed = TEST_EDIT_STATES if kind == "tests" else APP_EDIT_STATES
        if agent_type in REVIEW_ONLY_AGENTS or cur not in allowed:
            return (verdict, f"Run {run.id} is in {cur}; this command appears to modify {r}, which is "
                             f"only permitted in {sorted(allowed)}.")
    return None


def handle_pre_tool_use(payload: dict) -> dict | None:
    root = _root(payload)
    if root is None:
        return None
    try:
        cfg = load_config(root, required=False)
    except BAError as exc:
        run, _ = (None, None)
        try:
            run, _ = _live_run(root)
        except Exception:
            pass
        if run is not None and payload.get("tool_name") in WRITE_TOOLS | SHELL_TOOLS:
            return _decision("PreToolUse", "ask", f"bug-assurance config is invalid ({exc}); the guard "
                                                  "cannot evaluate this change.")
        return None
    if not cfg["guard"].get("enabled", True):
        return None
    tool = payload.get("tool_name", "")
    ti = payload.get("tool_input") or {}
    agent_type = payload.get("agent_type") or ""
    if tool in WRITE_TOOLS:
        verdict = evaluate_write(root, cfg, ti.get("file_path") or ti.get("notebook_path") or "", agent_type)
    elif tool in SHELL_TOOLS:
        verdict = evaluate_command(root, cfg, ti.get("command") or "", agent_type)
    else:
        verdict = None
    return _decision("PreToolUse", *verdict) if verdict else None


def handle_user_prompt(payload: dict) -> dict | None:
    from . import ledger

    root = _root(payload)
    if root is None:
        return None
    cfg = load_config(root, required=False)
    if not cfg["guard"].get("capture_prompts", True):
        return None
    msg = ledger.capture_message(root, payload.get("prompt") or "", payload.get("session_id") or "")
    run, state = _live_run(root)
    if msg is None or run is None:
        return None
    pending = ledger.pending_batch(run)["ask"]
    if not pending:
        return None
    ids = ", ".join(q["id"] for q in pending)
    return _context("UserPromptSubmit",
                    f"[bug-assurance] Message captured as {msg['id']}. Run {run.id} is in {state['state']} "
                    f"with pending question(s) {ids}. If this message answers one, record it with "
                    f"`ba.py answer <Q-id> --source-kind user --from-message {msg['id']} --quote \"<exact "
                    "excerpt>\"`, then challenge and verify it before marking the question RESOLVED.")


def handle_session_start(payload: dict) -> dict | None:
    root = _root(payload)
    if root is None:
        return None
    run, state = _live_run(root)
    if run is None:
        return None
    return _context("SessionStart",
                    f"[bug-assurance] Active run {run.id} ('{state['title']}') is in state {state['state']} "
                    f"(repair cycles {state['repair']['cycles']}/{state['repair']['max_cycles']}). Resume "
                    "with /bug-assurance resume; run `ba.py status` before any action.")


def handle_stop(payload: dict) -> dict | None:
    from . import ledger

    if payload.get("stop_hook_active"):
        return None
    root = _root(payload)
    if root is None:
        return None
    cfg = load_config(root, required=False)
    if not cfg["guard"].get("stop_check", True):
        return None
    run, state = _live_run(root)
    if run is None or payload.get("agent_type"):
        return None
    cur = state["state"]
    if cur == "CLARIFICATION" and ledger.pending_batch(run)["ask"]:
        return {"decision": "block", "reason":
                f"Run {run.id} has pending material questions. Persist the wait with `ba.py transition "
                "BLOCKED_AWAITING_CLARIFICATION`, then end the turn with the questions."}
    if cur in ("IMPLEMENTATION", "REPAIR_LOOP", "VERIFICATION", "FINAL_VERIFICATION",
               "INDEPENDENT_REVIEW", "ADJUDICATION"):
        return {"decision": "block", "reason":
                f"Run {run.id} is mid-flight in {cur}. Advance it through the next gate, or record why it "
                "cannot proceed (`ba.py transition CLARIFICATION|BLOCKED_EXTERNAL_DEPENDENCY|"
                "FAILED_QUALITY_GATE --reason ...`), and report the actual status before stopping."}
    return None


HANDLERS = {
    "pre-tool-use": handle_pre_tool_use,
    "user-prompt-submit": handle_user_prompt,
    "session-start": handle_session_start,
    "stop": handle_stop,
}


def main(event: str, stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    try:
        payload = json.loads(stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0  # malformed input: make no decision rather than block unrelated work
    handler = HANDLERS.get(event)
    if handler is None:
        print(f"unknown hook event {event}", file=sys.stderr)
        return 1
    try:
        out = handler(payload)
    except Exception as exc:  # fail closed only for writes during an active run
        out = None
        if event == "pre-tool-use" and payload.get("tool_name") in WRITE_TOOLS:
            root = _root(payload)
            if root is not None and active_run(root) is not None:
                out = _decision("PreToolUse", "ask", f"bug-assurance guard error ({type(exc).__name__}: "
                                                      f"{exc}); approve manually if the change is intended.")
        if out is None:
            print(f"bug-assurance hook error: {exc}", file=sys.stderr)
            return 1
    if out:
        stdout.write(json.dumps(out))
    return 0
