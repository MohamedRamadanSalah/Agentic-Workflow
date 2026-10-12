"""GitHub Actions validation and honest reporting of CI / required-check / CodeRabbit state.

All GitHub calls are read-only. Each of the six CI states is reported separately; a state that
cannot be observed is UNKNOWN, never assumed.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

_SHA = re.compile(r"@[0-9a-f]{40}$")


def _yaml():
    try:
        import yaml  # type: ignore
    except ImportError:
        return None
    return yaml


def validate_workflow(path: Path) -> dict:
    yaml = _yaml()
    out = {"file": path.as_posix(), "problems": [], "warnings": [], "validated": False}
    if yaml is None:
        out["warnings"].append("PyYAML not installed: syntax NOT validated")
        return out
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        out["problems"].append(f"YAML syntax error: {exc}")
        return out
    out["validated"] = True
    if not isinstance(doc, dict):
        out["problems"].append("workflow must be a mapping")
        return out
    on = doc.get("on", doc.get(True))  # PyYAML parses a bare `on:` key as boolean True
    if on is None:
        out["problems"].append("missing `on:` triggers")
    triggers = on if isinstance(on, (dict, list)) else [on]
    if "pull_request_target" in (triggers if isinstance(triggers, (dict, list)) else []):
        out["warnings"].append("pull_request_target runs with secrets; never check out untrusted PR code there")
    if "permissions" not in doc:
        out["problems"].append("top-level `permissions:` missing (least privilege requires it)")
    elif doc["permissions"] in ("write-all",) or (isinstance(doc["permissions"], dict) and any(
            v == "write" for v in doc["permissions"].values())):
        out["warnings"].append("workflow grants write permissions at top level")
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        out["problems"].append("no jobs defined")
        return out
    for name, job in jobs.items():
        if not isinstance(job, dict):
            out["problems"].append(f"job {name} must be a mapping")
            continue
        if "runs-on" not in job and "uses" not in job:
            out["problems"].append(f"job {name} has no runs-on")
        if "timeout-minutes" not in job and "uses" not in job:
            out["warnings"].append(f"job {name} has no timeout-minutes")
        for i, step in enumerate(job.get("steps") or []):
            uses = step.get("uses") if isinstance(step, dict) else None
            if uses and not uses.startswith("./") and not _SHA.search(uses.split("#")[0].strip()):
                out["warnings"].append(f"job {name} step {i}: action {uses!r} is not pinned to a commit SHA")
            run = step.get("run", "") if isinstance(step, dict) else ""
            if "${{ github.event." in run and any(k in run for k in ("title", "body", "head_ref", "comment")):
                out["problems"].append(f"job {name} step {i}: untrusted event text interpolated into a shell")
    return out


def _run(argv: list[str], cwd: Path, timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, cwd=str(cwd), capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return p.returncode, (p.stdout or p.stderr).decode("utf-8", "replace").strip()


def _remote(root: Path) -> tuple[str, str] | None:
    code, out = _run(["git", "remote", "get-url", "origin"], root)
    if code != 0:
        return None
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?$", out)
    return (m.group(1), m.group(2)) if m else None


def github_status(root: Path, branch: str | None = None) -> dict:
    wf_dir = root / ".github" / "workflows"
    files = sorted(list(wf_dir.glob("*.yml")) + list(wf_dir.glob("*.yaml"))) if wf_dir.is_dir() else []
    workflows = [validate_workflow(f) for f in files]
    states = {
        "workflow_file_created": "YES" if files else "NO",
        "syntax_validated": ("YES" if files and all(w["validated"] and not w["problems"] for w in workflows)
                             else "NO" if files else "N/A"),
        "executed_successfully": "UNKNOWN",
        "reported_by_github": "UNKNOWN",
        "configured_as_required": "UNKNOWN",
        "enforcement_verified": "NO",
    }
    notes = []
    remote = _remote(root)
    cr_file = root / ".coderabbit.yaml"
    coderabbit = {"config_file": "PRESENT" if cr_file.exists() else "ABSENT",
                  "config_valid_yaml": None, "app_installed": "UNKNOWN", "observed_reviews": "UNKNOWN"}
    yaml = _yaml()
    if cr_file.exists() and yaml is not None:
        try:
            yaml.safe_load(cr_file.read_text(encoding="utf-8"))
            coderabbit["config_valid_yaml"] = True
        except yaml.YAMLError:
            coderabbit["config_valid_yaml"] = False
    if remote is None:
        notes.append("no GitHub remote 'origin': CI execution, required checks and CodeRabbit cannot be observed")
        return {"remote": None, "workflows": workflows, "states": states, "coderabbit": coderabbit, "notes": notes}
    owner, repo = remote
    code, out = _run(["gh", "api", f"repos/{owner}/{repo}", "--jq", ".default_branch"], root)
    if code != 0:
        notes.append(f"gh api unavailable or unauthorized: {out[:200]}")
        return {"remote": f"{owner}/{repo}", "workflows": workflows, "states": states,
                "coderabbit": coderabbit, "notes": notes}
    branch = branch or out or "main"
    code, out = _run(["gh", "run", "list", "--branch", branch, "--limit", "20", "--json",
                      "name,conclusion,status,workflowName"], root)
    if code == 0:
        runs = json.loads(out or "[]")
        states["reported_by_github"] = "YES" if runs else "NO"
        states["executed_successfully"] = "YES" if any(r.get("conclusion") == "success" for r in runs) else "NO"
    code, out = _run(["gh", "api", f"repos/{owner}/{repo}/branches/{branch}/protection/required_status_checks"], root)
    if code == 0:
        contexts = json.loads(out).get("contexts", [])
        states["configured_as_required"] = "YES" if contexts else "NO"
        notes.append(f"required status checks on {branch}: {contexts}")
    elif "404" in out or "Not Found" in out or "not protected" in out.lower():
        states["configured_as_required"] = "NO"
    else:
        notes.append(f"branch protection not readable (admin rights needed?): {out[:160]}")
    code, out = _run(["gh", "api", f"repos/{owner}/{repo}/rules/branches/{branch}"], root)
    if code == 0:
        rules = json.loads(out or "[]")
        req = [r for r in rules if r.get("type") == "required_status_checks"]
        if req:
            states["configured_as_required"] = "YES"
            notes.append("required status checks enforced by a ruleset")
    notes.append("enforcement_verified requires observing a PR blocked by a failing required check; "
                 "this command does not create PRs, so it stays NO")
    code, out = _run(["gh", "api", f"repos/{owner}/{repo}/pulls?state=all&per_page=10", "--jq", ".[].number"], root)
    if code == 0:
        seen = False
        for num in out.split():
            c2, o2 = _run(["gh", "api", f"repos/{owner}/{repo}/pulls/{num}/reviews", "--jq", ".[].user.login"], root)
            if c2 == 0 and "coderabbitai" in o2:
                seen = True
                break
        coderabbit["observed_reviews"] = "YES" if seen else "NONE_OBSERVED"
    return {"remote": f"{owner}/{repo}", "branch": branch, "workflows": workflows, "states": states,
            "coderabbit": coderabbit, "notes": notes}
