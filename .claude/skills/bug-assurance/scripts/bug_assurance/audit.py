"""Project snapshots, fingerprints (stale-evidence detection) and the independent diff audit."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .core import (
    contains_secret, match_any, now_iso, sha256_bytes, sha256_text, write_json_atomic,
)

MAX_SCAN_BYTES = 1_000_000


def _git(root: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["git", *args], cwd=str(root), capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def is_git_repo(root: Path) -> bool:
    res = _git(root, "rev-parse", "--is-inside-work-tree")
    return bool(res and res.returncode == 0 and res.stdout.strip() == b"true")


def git_head(root: Path) -> str | None:
    res = _git(root, "rev-parse", "--verify", "-q", "HEAD")
    return res.stdout.decode().strip() if res and res.returncode == 0 else None


def git_dirty(root: Path) -> list[str]:
    """Paths with uncommitted changes relative to HEAD. Without a commit there is no reference
    point, so nothing can be classified as a pre-existing user modification."""
    if git_head(root) is None:
        return []
    res = _git(root, "status", "--porcelain=v1", "-uall", "-z")
    if not res or res.returncode != 0:
        return []
    entries = [e for e in res.stdout.decode("utf-8", "replace").split("\0") if e]
    out = []
    skip_next = False
    for e in entries:
        if skip_next:
            skip_next = False
            continue
        status, path = e[:2], e[3:]
        if "R" in status or "C" in status:
            skip_next = True  # the original path of a rename follows
        out.append(path)
    return sorted(out)


def project_files(root: Path, cfg: dict) -> list[str]:
    """Tracked + untracked-but-not-ignored files (git), or a filtered walk outside git."""
    excludes = cfg["fingerprint"]["exclude"]
    files: list[str] = []
    res = _git(root, "ls-files", "-co", "--exclude-standard", "-z") if is_git_repo(root) else None
    if res is not None and res.returncode == 0:
        files = [p for p in res.stdout.decode("utf-8", "replace").split("\0") if p]
    else:
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = Path(dirpath).relative_to(root).as_posix()
            rel_dir = "" if rel_dir == "." else rel_dir + "/"
            dirnames[:] = [d for d in dirnames if not match_any(rel_dir + d + "/x", excludes)]
            files.extend(rel_dir + f for f in filenames)
    return sorted(f for f in files if not match_any(f, excludes) and (root / f).is_file())


def snapshot(root: Path, cfg: dict) -> dict[str, str]:
    out = {}
    for rel in project_files(root, cfg):
        try:
            out[rel] = sha256_bytes((root / rel).read_bytes())
        except OSError:
            out[rel] = "unreadable"
    return out


def fingerprint_of(files: dict[str, str], only: list[str] | None = None) -> str:
    items = sorted((p, h) for p, h in files.items() if only is None or match_any(p, only))
    return sha256_text("".join(f"{p}\0{h}\n" for p, h in items))


def current_fingerprint(root: Path, cfg: dict) -> str:
    return fingerprint_of(snapshot(root, cfg))


def classify(rel: str, cfg: dict) -> str:
    paths = cfg["paths"]
    if rel.startswith(".bug-assurance/"):
        return "artifact"
    if match_any(rel, paths["tests"]):
        return "tests"
    if match_any(rel, paths["application"]):
        return "application"
    if match_any(rel, paths.get("unrestricted", [])):
        return "unrestricted"
    return "other"


def _read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > MAX_SCAN_BYTES or b"\0" in data[:4096]:
        return None
    return data.decode("utf-8", "replace")


def pattern_counts(root: Path, files: list[str], patterns: list[str]) -> dict[str, int]:
    compiled = [re.compile(p) for p in patterns]
    counts = {}
    for rel in files:
        text = _read_text(root / rel)
        if text is None:
            continue
        n = sum(len(rx.findall(text)) for rx in compiled)
        if n:
            counts[rel] = n
    return counts


def make_baseline(root: Path, cfg: dict) -> dict:
    files = snapshot(root, cfg)
    code_files = [p for p in files if classify(p, cfg) in ("application", "tests")]
    return {
        "created_at": now_iso(),
        "git_repo": is_git_repo(root),
        "git_head": git_head(root),
        "git_dirty": git_dirty(root),
        "files": files,
        "fingerprint": fingerprint_of(files),
        "app_fingerprint": fingerprint_of(files, cfg["paths"]["application"]),
        "pattern_counts": pattern_counts(root, code_files, cfg["debug_patterns"]),
    }


def diff_audit(root: Path, cfg: dict, baseline: dict) -> dict:
    """Compare the working tree against the run baseline and flag risky changes.

    Severities: BLOCKING (gate fails unless acknowledged; secrets can never be acknowledged),
    WARNING (gate fails unless acknowledged), INFO (reported only).
    """
    now = snapshot(root, cfg)
    base = baseline["files"]
    added = sorted(set(now) - set(base))
    deleted = sorted(set(base) - set(now))
    modified = sorted(p for p in set(now) & set(base) if now[p] != base[p])
    dirty_at_start = set(baseline.get("git_dirty", []))
    issues: list[dict] = []

    def issue(code: str, severity: str, path: str, detail: str) -> None:
        issues.append({"code": code, "severity": severity, "path": path, "detail": detail})

    changed = added + modified
    for rel in changed:
        kind = classify(rel, cfg)
        if kind == "other":
            issue("UNRELATED_CHANGE", "BLOCKING", rel,
                  "changed file is neither application source nor tests; justify or revert")
        if rel in dirty_at_start:
            issue("PREEXISTING_USER_CHANGE_TOUCHED", "WARNING", rel,
                  "file had uncommitted user modifications before the run; verify they were preserved")
        if match_any(rel, cfg["temp_file_patterns"]) and rel in added:
            issue("TEMP_FILE", "WARNING", rel, "temporary/backup file added")
        text = _read_text(root / rel)
        if text is not None and contains_secret(text):
            issue("SECRET", "BLOCKING", rel, "possible secret material detected; remove it")
    for rel in deleted:
        issue("DELETED_FILE", "BLOCKING", rel, "file deleted during the run; deletions require justification")

    code_changed = [p for p in changed if classify(p, cfg) in ("application", "tests")]
    after = pattern_counts(root, code_changed, cfg["debug_patterns"])
    before = baseline.get("pattern_counts", {})
    for rel, n in after.items():
        if n > before.get(rel, 0):
            issue("DEBUG_CODE", "WARNING", rel, f"debug/hack markers increased {before.get(rel, 0)} -> {n}")

    app_changed = [p for p in changed + deleted if classify(p, cfg) == "application"]
    if not app_changed:
        issue("NO_APPLICATION_CHANGE", "INFO", "", "no application source file changed")
    return {
        "at": now_iso(),
        "fingerprint": fingerprint_of(now),
        "changed": {"added": added, "modified": modified, "deleted": deleted},
        "application_files_changed": app_changed,
        "issues": issues,
    }


def write_diff_audit(run, cfg: dict) -> dict:
    baseline = run.load("baseline.json")
    result = diff_audit(run.root, cfg, baseline)
    write_json_atomic(run.path("diff-audit.json"), result)
    run.event("diff_audit", issues=len(result["issues"]), fingerprint=result["fingerprint"])
    return result
