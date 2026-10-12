"""Command-line interface. Exit codes: 0 ok, 2 usage/validation error, 3 gate refused or failed,
4 verification finished with a required check not passing, 1 unexpected internal error."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .core import BAError, find_repo_root, list_runs, load_config, resolve_run, set_active

SKILL_DIR = Path(__file__).resolve().parents[2]
TEMPLATES = SKILL_DIR / "templates"


def _print(data) -> None:
    sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")


def _root(args) -> Path:
    return Path(args.project).resolve() if getattr(args, "project", None) else find_repo_root()


def cmd_discover(args):
    from .bootstrap import discover
    _print(discover(_root(args)))


def cmd_init(args):
    from .bootstrap import init
    _print(init(_root(args), apply=not args.dry_run))


def cmd_install(args):
    from .bootstrap import install
    toolkit = SKILL_DIR.parents[2]  # <toolkit>/.claude/skills/bug-assurance
    _print(install(toolkit, Path(args.target).resolve(), apply=args.apply, interpreter=args.interpreter))


def cmd_doctor(args):
    """Validate the installation: config, hooks wiring, agents, toolchain, git."""
    from .audit import is_git_repo
    from .bootstrap import HOOK_EVENTS
    from .core import read_json

    root = _root(args)
    report = {"root": str(root), "python": sys.version.split()[0], "ok": True, "problems": [], "warnings": []}
    try:
        cfg = load_config(root)
        report["checks"] = [c["id"] for c in cfg["checks"]]
        if not cfg["checks"]:
            report["warnings"].append("no verification checks configured")
    except BAError as exc:
        report["problems"].append(str(exc))
    settings = root / ".claude" / "settings.json"
    if settings.exists():
        hooks = read_json(settings).get("hooks", {})
        for event, (_, arg) in HOOK_EVENTS.items():
            cmds = [h.get("command", "") for e in hooks.get(event, []) for h in e.get("hooks", [])]
            if not any(f"hook {arg}" in c and "ba.py" in c for c in cmds):
                report["problems"].append(f"hook {event} -> ba.py hook {arg} is not configured")
    else:
        report["problems"].append(".claude/settings.json missing: hooks not installed")
    for agent in ("ba-bug-investigator", "ba-business-logic-reviewer", "ba-regression-reviewer",
                  "ba-architecture-reviewer", "ba-security-reviewer", "ba-test-quality-reviewer",
                  "ba-implementer", "ba-findings-adjudicator", "ba-orchestrator"):
        if not (root / ".claude" / "agents" / f"{agent}.md").exists():
            report["problems"].append(f"agent definition missing: .claude/agents/{agent}.md")
    if not (root / ".claude" / "skills" / "bug-assurance" / "SKILL.md").exists():
        report["problems"].append("skill missing: .claude/skills/bug-assurance/SKILL.md")
    if not is_git_repo(root):
        report["warnings"].append("not a git repository: diff inspection uses content hashing only")
    report["ok"] = not report["problems"]
    _print(report)
    return 0 if report["ok"] else 2


def cmd_start(args):
    from .machine import start_run
    root = _root(args)
    run = start_run(root, load_config(root), args.title, args.ticket_ref or "", args.replace_active)
    _print({"run_id": run.id, "dir": run.dir.relative_to(root).as_posix(), "state": "INTAKE",
            "next": "write ticket.json (see `ba.py template ticket`), then `ba.py transition INVESTIGATION`"})


def cmd_status(args):
    from .machine import status
    root = _root(args)
    st = status(resolve_run(root, args.run), load_config(root))
    if args.json:
        _print(st)
        return 0
    print(f"Run {st['run_id']}  [{st['state']}]  {st['title']}")
    print(f"  purpose: {st['purpose']}")
    print(f"  artifacts: {', '.join(st['expected_artifacts']) or '-'}")
    print(f"  repair cycles: {st['repair']['cycles']}/{st['repair']['max_cycles']}")
    for target, info in st["transitions"].items():
        mark = "OK " if info["passes"] else "-- "
        print(f"  {mark}-> {target}  gates={info['gates'] or ['none']}")
        for p in info["problems"][:8]:
            print(f"        - {p}")
    return 0


def cmd_runs(args):
    root = _root(args)
    _print([{"run_id": r.id, "state": r.state()["state"], "title": r.state()["title"],
             "outcome": r.state().get("outcome")} for r in list_runs(root)])


def cmd_activate(args):
    root = _root(args)
    run = resolve_run(root, args.run_id)
    set_active(root, run)
    run.event("activated")
    _print({"active": run.id, "state": run.state()["state"]})


def cmd_deactivate(args):
    root = _root(args)
    run = resolve_run(root, None)
    if not args.reason:
        raise BAError("--reason is required; deactivation lifts edit enforcement for this run")
    run.event("deactivated", reason=args.reason)
    set_active(root, None)
    _print({"deactivated": run.id, "state": run.state()["state"], "note": "run preserved; nothing deleted"})


def cmd_transition(args):
    from .machine import transition
    root = _root(args)
    run = resolve_run(root, args.run)
    state = transition(run, load_config(root), args.target, args.reason or "")
    out = {"run_id": run.id, "state": state["state"], "repair": state["repair"]}
    if state["state"] == "FINAL_REPORT":
        out["outcome"] = state.get("outcome")
        out["report"] = run.path("final-report.md").relative_to(root).as_posix()
    _print(out)


def cmd_gate(args):
    from .gates import GATES, evaluate
    root = _root(args)
    if args.name not in GATES:
        raise BAError(f"unknown gate; choose from {sorted(GATES)}")
    res = evaluate(resolve_run(root, args.run), load_config(root), [args.name])
    _print(res.to_dict())
    return 0 if res.passed else 3


def cmd_template(args):
    path = TEMPLATES / f"{args.name}.json"
    if not path.exists():
        names = sorted(p.stem for p in TEMPLATES.glob("*.json"))
        raise BAError(f"no template {args.name!r}; available: {names}")
    sys.stdout.write(path.read_text(encoding="utf-8"))


def cmd_questions(args):
    from .ledger import pending_batch
    _print(pending_batch(resolve_run(_root(args), args.run)))


def cmd_inbox(args):
    from .ledger import inbox
    msgs = list(inbox(_root(args)).values())[-args.last:]
    _print([{"id": m["id"], "at": m["at"], "text": m["text"][:600]} for m in msgs])


def cmd_answer(args):
    from .ledger import record_answer
    run = resolve_run(_root(args), args.run)
    a = record_answer(run, args.question, source_kind=args.source_kind, text=args.quote or args.text,
                      message_id=args.from_message, source_name=args.source_name or "",
                      on_behalf_of=args.on_behalf_of or "", claim_authorized=not args.unverified)
    _print(a)


def cmd_regression_run(args):
    from .checks import regression_run
    root = _root(args)
    rec = regression_run(resolve_run(root, args.run), load_config(root))
    _print({k: v for k, v in rec.items() if k != "results"} |
           {"results": [{k: r[k] for k in ("id", "exit_code", "outcome", "detail")} for r in rec["results"]]})
    return 0 if all(r["outcome"] == "FAILED_AS_EXPECTED" for r in rec["results"]) else 3


def cmd_diff_audit(args):
    from .audit import write_diff_audit
    root = _root(args)
    _print(write_diff_audit(resolve_run(root, args.run), load_config(root)))


def cmd_diff_scope(args):
    """Classify changed files (git working tree vs HEAD/index) for review-only mode."""
    from .audit import classify, git_dirty
    root = _root(args)
    cfg = load_config(root, required=False)
    _print([{"path": p, "kind": classify(p, cfg)} for p in git_dirty(root)])


def cmd_verify(args):
    from .checks import run_verification
    root = _root(args)
    cfg = load_config(root)
    only = [x for x in (args.only or "").split(",") if x] or None
    run = None if args.standalone else resolve_run(root, args.run)
    rec = run_verification(root, cfg, run=run, only=only, final=args.final,
                           out=Path(args.out) if args.out else None)
    summary = {"seq": rec.get("seq"), "final": rec["final"], "fingerprint": rec["fingerprint"],
               "summary": rec["summary"], "required_failures": rec["required_failures"],
               "results": [{k: r.get(k) for k in ("id", "required", "status", "exit_code", "duration_s", "detail")}
                           for r in rec["results"]]}
    _print(summary)
    if rec["fingerprint"] == "INCONSISTENT":
        print("files changed while checks ran; results are unusable", file=sys.stderr)
        return 4
    return 4 if rec["required_failures"] else 0


def cmd_review_record(args):
    from .findings import record_review
    root = _root(args)
    _print(record_review(resolve_run(root, args.run), load_config(root), Path(args.file).resolve()))


def cmd_adjudicate(args):
    from .findings import adjudicate
    run = resolve_run(_root(args), args.run)
    f = adjudicate(run, args.finding, args.disposition, args.rationale, args.evidence or [],
                   duplicate_of=args.duplicate_of, question=args.question)
    _print({k: f[k] for k in ("id", "severity", "disposition", "disposition_evidence")})


def cmd_finding_resolve(args):
    from .findings import resolve
    run = resolve_run(_root(args), args.run)
    f = resolve(run, args.finding, args.status, args.evidence or [], args.authorization, args.note or "")
    _print({"id": f["id"], "resolution": f["resolution"]})


def cmd_findings(args):
    from .gates import load_findings
    _print(load_findings(resolve_run(_root(args), args.run)))


def cmd_set_repair_limit(args):
    from .machine import set_repair_limit
    st = set_repair_limit(resolve_run(_root(args), args.run), args.max_cycles, args.authorization)
    _print(st["repair"])


def cmd_report(args):
    from .core import write_text_atomic
    from .report import build_report
    root = _root(args)
    run = resolve_run(root, args.run)
    data, md = build_report(run, load_config(root))
    write_text_atomic(run.path("draft-report.md"), md)
    _print({"status": data["status"], "provisional": data["provisional"], "reasons": data["reasons"],
            "draft": run.path("draft-report.md").relative_to(root).as_posix()})


def cmd_validate_workflows(args):
    from .ghstatus import validate_workflow
    root = _root(args)
    wf = root / ".github" / "workflows"
    results = [validate_workflow(p) for p in sorted(wf.glob("*.y*ml"))] if wf.is_dir() else []
    _print(results)
    if not results:
        return 2
    return 0 if all(r["validated"] and not r["problems"] for r in results) else 3


def cmd_github_status(args):
    from .ghstatus import github_status
    _print(github_status(_root(args), args.branch))


def cmd_hook(args):
    from .guard import main as hook_main
    return hook_main(args.event)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ba.py", description="Bug Assurance workflow controller")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--project", help="project root (default: auto-detect)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    def with_run(sp):
        sp.add_argument("--run", help="run id (default: the active run)")
        return sp

    add("discover", cmd_discover, "detect stacks, tools and propose checks (read-only)")
    sp = add("init", cmd_init, "create .bug-assurance/config.json (never overwrites) and .gitignore entries")
    sp.add_argument("--dry-run", action="store_true")
    sp = add("install", cmd_install, "install the system into another repository (dry-run by default)")
    sp.add_argument("--target", required=True)
    sp.add_argument("--apply", action="store_true", help="actually write files")
    sp.add_argument("--interpreter", help="python executable name for hook commands")
    add("doctor", cmd_doctor, "validate configuration, hooks, agents and skill wiring")
    sp = add("start", cmd_start, "start a new run (state INTAKE)")
    sp.add_argument("--title", required=True)
    sp.add_argument("--ticket-ref")
    sp.add_argument("--replace-active", action="store_true")
    sp = with_run(add("status", cmd_status, "show state, expected artifacts and gate status of each transition"))
    sp.add_argument("--json", action="store_true")
    add("runs", cmd_runs, "list runs")
    sp = add("activate", cmd_activate, "make an existing run the active run (resume)")
    sp.add_argument("run_id")
    sp = add("deactivate", cmd_deactivate, "clear the active-run pointer (run is preserved)")
    sp.add_argument("--reason")
    sp = with_run(add("transition", cmd_transition, "move the run to a new state through its gates"))
    sp.add_argument("target")
    sp.add_argument("--reason")
    sp = with_run(add("gate", cmd_gate, "evaluate one gate (exit 3 if it fails)"))
    sp.add_argument("name")
    sp = add("template", cmd_template, "print an artifact template")
    sp.add_argument("name")
    with_run(add("questions", cmd_questions, "pending material questions to ask (batch)"))
    sp = add("inbox", cmd_inbox, "show recently captured user messages")
    sp.add_argument("--last", type=int, default=5)
    sp = with_run(add("answer", cmd_answer, "record an answer with provenance"))
    sp.add_argument("question")
    sp.add_argument("--source-kind", required=True, choices=["user", "document", "ticket", "agent"])
    sp.add_argument("--from-message", help="inbox message id (required for user answers)")
    sp.add_argument("--quote", help="exact excerpt of the message/document that answers the question")
    sp.add_argument("--text", help="answer text (non-user sources)")
    sp.add_argument("--source-name", help="document path or person")
    sp.add_argument("--on-behalf-of", help="stakeholder the user relays, e.g. 'Product owner'")
    sp.add_argument("--unverified", action="store_true", help="record without claiming authority")
    with_run(add("regression-run", cmd_regression_run, "run regression tests on the ORIGINAL code (must fail)"))
    with_run(add("diff-audit", cmd_diff_audit, "audit changes since the run baseline"))
    add("diff-scope", cmd_diff_scope, "classify changed files (review-only mode)")
    sp = with_run(add("verify", cmd_verify, "run configured checks and record fingerprinted results"))
    sp.add_argument("--only", help="comma-separated check ids")
    sp.add_argument("--final", action="store_true", help="final verification (all checks)")
    sp.add_argument("--standalone", action="store_true", help="no run (CI / verification-only mode)")
    sp.add_argument("--out", help="also write the result JSON to this path")
    sp = with_run(add("review-record", cmd_review_record, "validate a reviewer's JSON output and ingest findings"))
    sp.add_argument("file")
    sp = with_run(add("adjudicate", cmd_adjudicate, "record a finding disposition with evidence"))
    sp.add_argument("finding")
    sp.add_argument("--disposition", required=True)
    sp.add_argument("--rationale", required=True)
    sp.add_argument("--evidence", action="append", help="kind:ref (repeatable)")
    sp.add_argument("--duplicate-of")
    sp.add_argument("--question")
    sp = with_run(add("finding-resolve", cmd_finding_resolve, "mark a confirmed finding FIXED / ACCEPTED_RISK / OPEN"))
    sp.add_argument("finding")
    sp.add_argument("--status", required=True)
    sp.add_argument("--evidence", action="append")
    sp.add_argument("--authorization")
    sp.add_argument("--note")
    with_run(add("findings", cmd_findings, "list findings"))
    sp = with_run(add("set-repair-limit", cmd_set_repair_limit, "change max repair cycles (authorized)"))
    sp.add_argument("max_cycles", type=int)
    sp.add_argument("--authorization", required=True)
    with_run(add("report", cmd_report, "generate a draft report with the current computed status"))
    add("validate-workflows", cmd_validate_workflows, "validate .github/workflows syntax and safety")
    sp = add("github-status", cmd_github_status, "report CI / required-check / CodeRabbit state (read-only)")
    sp.add_argument("--branch")
    sp = add("hook", cmd_hook, "Claude Code hook entry point (reads JSON on stdin)")
    sp.add_argument("event", choices=["pre-tool-use", "user-prompt-submit", "session-start", "stop"])
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    args = build_parser().parse_args(argv)
    try:
        rc = args.fn(args)
        return rc if isinstance(rc, int) else 0
    except BAError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        return 130
