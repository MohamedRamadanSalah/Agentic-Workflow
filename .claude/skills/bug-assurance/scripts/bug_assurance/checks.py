"""Verification command execution with real exit codes, timeouts and honest statuses.

Status semantics (a check that did not run is never a pass):
  PASS                              exit code in success_exit_codes
  FAIL                              ran and exited otherwise, or timed out
  BLOCKED                           could not execute (missing executable, OS error, bad cwd)
  NOT_RUN                           not selected in this invocation
  NOT_APPLICABLE_WITH_JUSTIFICATION configured as not applicable, with the stated reason
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .core import BAError, now_iso, read_json, redact, write_json_atomic, write_text_atomic

TAIL_CHARS = 4000


def _tail(text: str) -> str:
    text = redact(text or "")
    return text if len(text) <= TAIL_CHARS else "...[truncated]...\n" + text[-TAIL_CHARS:]


def _redact_argv(argv):
    """Command lines are persisted in evidence; never store secrets passed as arguments."""
    return [redact(a) for a in argv] if isinstance(argv, list) else argv


def resolve_argv(argv: list[str]) -> tuple[list[str] | None, str]:
    """Substitute {python} and resolve the executable without invoking a shell."""
    argv = [sys.executable if a == "{python}" else a for a in argv]
    exe = argv[0]
    if os.path.isabs(exe) or os.sep in exe or "/" in exe:
        return (argv, "") if Path(exe).exists() else (None, f"executable not found: {exe}")
    found = shutil.which(exe)
    if not found:
        return None, f"executable not found on PATH: {exe}"
    return [found, *argv[1:]], ""


def run_command(root: Path, check: dict, success_codes: list[int] | None = None) -> dict:
    cid = check["id"]
    result = {
        "id": cid,
        "kind": check.get("kind", "other"),
        "required": bool(check.get("required")),
        "argv": _redact_argv(check.get("argv")),
        "status": "BLOCKED",
        "exit_code": None,
        "duration_s": 0.0,
        "detail": "",
        "stdout_tail": "",
        "stderr_tail": "",
        "started_at": now_iso(),
    }
    if check.get("not_applicable_reason"):
        result.update(status="NOT_APPLICABLE_WITH_JUSTIFICATION", detail=check["not_applicable_reason"])
        return result
    argv, err = resolve_argv(list(check["argv"]))
    cwd = (root / check.get("cwd", ".")).resolve()
    if argv is None:
        result["detail"] = err
        return result
    if not cwd.is_dir():
        result["detail"] = f"working directory not found: {cwd}"
        return result
    env = dict(os.environ)
    env.update({k: str(v) for k, v in (check.get("env") or {}).items()})
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    timeout = float(check.get("timeout_s", 600))
    codes = success_codes if success_codes is not None else check.get("success_exit_codes", [0])
    start = time.monotonic()
    try:
        proc = subprocess.run(argv, cwd=str(cwd), env=env, capture_output=True, timeout=timeout,
                              check=False, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        result.update(status="FAIL", detail=f"timed out after {timeout:g}s",
                      stdout_tail=_tail((exc.stdout or b"").decode("utf-8", "replace")),
                      stderr_tail=_tail((exc.stderr or b"").decode("utf-8", "replace")))
        result["duration_s"] = round(time.monotonic() - start, 3)
        return result
    except OSError as exc:
        result["detail"] = f"could not execute: {exc}"
        return result
    out = proc.stdout.decode("utf-8", "replace")
    errout = proc.stderr.decode("utf-8", "replace")
    result.update(
        exit_code=proc.returncode,
        duration_s=round(time.monotonic() - start, 3),
        status="PASS" if proc.returncode in codes else "FAIL",
        detail=f"exit code {proc.returncode}",
        stdout_tail=_tail(out),
        stderr_tail=_tail(errout),
    )
    result["_full_output"] = redact(out + ("\n--- stderr ---\n" + errout if errout else ""))
    return result


def _regression_checks(run) -> list[dict]:
    if run is None or not run.has("regression-plan.json"):
        return []
    plan = run.load("regression-plan.json")
    if not plan.get("feasible"):
        return []
    return [{"id": f"regression:{t['id']}", "argv": t["argv"], "kind": "regression", "required": True,
             "timeout_s": t.get("timeout_s", 600), "cwd": t.get("cwd", ".")}
            for t in plan.get("tests", []) if isinstance(t, dict) and t.get("id") and t.get("argv")]


def run_verification(root: Path, cfg: dict, run=None, only: list[str] | None = None,
                     final: bool = False, out: Path | None = None) -> dict:
    from .audit import current_fingerprint

    if final and only:
        raise BAError("--final runs every check; it cannot be combined with --only")
    checks = list(cfg.get("checks", [])) + _regression_checks(run)
    known = {c["id"] for c in checks}
    for cid in only or []:
        if cid not in known:
            raise BAError(f"unknown check {cid!r}; known: {sorted(known)}")
    fingerprint_before = current_fingerprint(root, cfg)
    results = []
    for chk in checks:
        if only and chk["id"] not in only:
            results.append({"id": chk["id"], "kind": chk.get("kind", "other"),
                            "required": bool(chk.get("required")), "argv": _redact_argv(chk.get("argv")),
                            "status": "NOT_RUN", "exit_code": None, "duration_s": 0.0,
                            "detail": "not selected in this invocation"})
            continue
        results.append(run_command(root, chk))
    fingerprint_after = current_fingerprint(root, cfg)
    record = {
        "at": now_iso(),
        "final": final,
        "fingerprint": fingerprint_after,
        "fingerprint_changed_during_run": fingerprint_before != fingerprint_after,
        "python": sys.version.split()[0],
        "results": results,
    }
    if record["fingerprint_changed_during_run"]:
        # Results cannot be attributed to one code state; mark them unusable.
        record["fingerprint"] = "INCONSISTENT"
    summary = {s: sum(1 for r in results if r["status"] == s)
               for s in ("PASS", "FAIL", "BLOCKED", "NOT_RUN", "NOT_APPLICABLE_WITH_JUSTIFICATION")}
    record["summary"] = summary
    required_bad = [r["id"] for r in results if r.get("required") and r["status"] != "PASS"
                    and r["status"] != "NOT_RUN"]
    record["required_failures"] = required_bad

    if run is not None:
        vdir = run.path("verification")
        seq = len(list(vdir.glob("*.json"))) + 1 if vdir.exists() else 1
        record["seq"] = seq
        logs = vdir / "logs"
        for r in results:
            full = r.pop("_full_output", None)
            if full is not None:
                log = logs / f"{seq:03d}-{r['id'].replace(':', '_')}.log"
                write_text_atomic(log, full)
                r["log"] = log.relative_to(run.dir).as_posix()
        write_json_atomic(vdir / f"{seq:03d}.json", record)
        run.event("verification", seq=seq, final=final, summary=summary)
    else:
        for r in results:
            r.pop("_full_output", None)
    if out is not None:
        write_json_atomic(out, record)
    return record


def load_verification_runs(run) -> list[dict]:
    vdir = run.path("verification")
    if not vdir.exists():
        return []
    return [read_json(p) for p in sorted(vdir.glob("*.json"))]


def regression_run(run, cfg: dict) -> dict:
    """Run the planned regression tests against the ORIGINAL implementation; they must fail."""
    from .audit import fingerprint_of, snapshot
    from .gates import file_digest

    plan = run.load("regression-plan.json")
    if not plan.get("feasible"):
        raise BAError("regression plan is marked infeasible; nothing to run")
    files = snapshot(run.root, cfg)
    app_fp = fingerprint_of(files, cfg["paths"]["application"])
    baseline = run.load("baseline.json")
    if app_fp != baseline["app_fingerprint"]:
        raise BAError("application code differs from the run baseline; the pre-fix run must execute "
                      "against the original implementation")
    results = []
    for t in plan.get("tests", []):
        chk = {"id": t["id"], "argv": t["argv"], "timeout_s": t.get("timeout_s", 600),
               "cwd": t.get("cwd", ".")}
        expected = t.get("expected_failure_exit_codes", [1])
        r = run_command(run.root, chk, success_codes=[0])
        r.pop("_full_output", None)
        if r["status"] == "PASS":
            outcome, detail = "PASSED_UNEXPECTEDLY", "test passed on the original code"
        elif r["exit_code"] in expected:
            outcome, detail = "FAILED_AS_EXPECTED", f"exit code {r['exit_code']}"
        elif r["exit_code"] is None:
            outcome, detail = "BLOCKED", r["detail"]
        else:
            outcome, detail = "ERROR", (f"exit code {r['exit_code']} is not an expected test-failure code "
                                        f"{expected} (collection/usage error?)")
        results.append({"id": t["id"], "exit_code": r["exit_code"], "outcome": outcome, "detail": detail,
                        "stdout_tail": r["stdout_tail"], "stderr_tail": r["stderr_tail"]})
    record = {"at": now_iso(), "app_fingerprint": app_fp,
              "plan_digest": file_digest(run, "regression-plan.json"),
              "contract_digest": file_digest(run, "contract.json"), "results": results}
    write_json_atomic(run.path("regression-run.json"), record)
    run.event("regression_run", outcomes=[r["outcome"] for r in results])
    return record
