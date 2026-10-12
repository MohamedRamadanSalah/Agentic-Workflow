"""Shared primitives: errors, time, JSON I/O, redaction, glob matching, configuration, run storage."""

from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import json
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import Any, Iterable

ARTIFACT_DIR = ".bug-assurance"
CONFIG_FILE = "config.json"
ACTIVE_POINTER = "active-run"
SCHEMA_VERSION = 1


class BAError(Exception):
    """A user-facing error. The CLI prints it and exits with ``exit_code``."""

    def __init__(self, message: str, exit_code: int = 2):
        super().__init__(message)
        self.exit_code = exit_code


# --------------------------------------------------------------------------------------------
# Time, hashing, JSON
# --------------------------------------------------------------------------------------------

def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        if default is not None:
            return copy.deepcopy(default)
        raise BAError(f"missing artifact: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BAError(f"malformed JSON in {path}: {exc}") from exc


def write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON via a temp file + rename so a crash never leaves a half-written artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, indent=2, sort_keys=False, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise BAError(f"malformed JSONL in {path} line {lineno}: {exc}") from exc
    return out


# --------------------------------------------------------------------------------------------
# Secret redaction: applied to every captured output and user message before it is persisted.
# --------------------------------------------------------------------------------------------

_SECRET_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![A-Za-z0-9])"),
    re.compile(r"sk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/]{16,}=*"),
    re.compile(r"(?i)\b((?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\s*[:=]\s*)(\"[^\"]+\"|'[^']+'|\S+)"),
]


def redact(text: str) -> str:
    if not text:
        return text
    for pat in _SECRET_PATTERNS:
        if pat.groups:
            text = pat.sub(lambda m: m.group(1) + "[REDACTED]", text)
        else:
            text = pat.sub("[REDACTED]", text)
    return text


def contains_secret(text: str) -> list[str]:
    hits = []
    for pat in _SECRET_PATTERNS:
        if pat.search(text or ""):
            hits.append(pat.pattern[:40])
    return hits


# --------------------------------------------------------------------------------------------
# Glob matching with ``**`` support on POSIX-style relative paths.
# --------------------------------------------------------------------------------------------

_GLOB_CACHE: dict[str, re.Pattern] = {}


def _glob_to_regex(pattern: str) -> re.Pattern:
    cached = _GLOB_CACHE.get(pattern)
    if cached is not None:
        return cached
    i, out = 0, []
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    rx = re.compile("^" + "".join(out) + "$")
    _GLOB_CACHE[pattern] = rx
    return rx


def match_any(rel_path: str, patterns: Iterable[str]) -> bool:
    rel_path = rel_path.replace("\\", "/")
    while rel_path.startswith("./"):
        rel_path = rel_path[2:]
    for pat in patterns:
        rx = _glob_to_regex(pat)
        if rx.match(rel_path):
            return True
        # A pattern without a slash also matches the basename at any depth (gitignore-like).
        if "/" not in pat and rx.match(rel_path.rsplit("/", 1)[-1]):
            return True
    return False


def rel_to(root: Path, path: str | Path) -> str | None:
    """Return ``path`` relative to ``root`` as POSIX, or None when it lies outside ``root``."""
    p = Path(path)
    if not p.is_absolute():
        p = root / p
    resolved = os.path.normcase(os.path.abspath(p))
    base = os.path.normcase(os.path.abspath(root))
    try:
        if os.path.commonpath([resolved, base]) != base:
            return None
    except ValueError:  # different drives on Windows
        return None
    rel = os.path.relpath(resolved, base)
    return "" if rel == "." else Path(rel).as_posix()


# --------------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------------

DEFAULT_CONFIG: dict = {
    "schema_version": SCHEMA_VERSION,
    "paths": {
        # Application source: modifiable only in IMPLEMENTATION / REPAIR_LOOP of an active run.
        "application": ["src/**", "lib/**", "app/**"],
        # Tests: modifiable from REGRESSION_TEST onward.
        "tests": ["tests/**", "test/**", "**/*_test.*", "**/test_*.py", "**/*.test.*", "**/*.spec.*"],
        # Paths the guard never restricts during a run (notes and docs).
        "unrestricted": ["docs/**"],
    },
    "checks": [],
    "required_reviewers": ["business-logic", "regression", "test-quality"],
    "conditional_reviewers": ["security"],
    "optional_reviewers": ["architecture", "coderabbit"],
    "repair": {"max_cycles": 3, "hard_limit": 10},
    "guard": {"enabled": True, "ask_on_git_publish": True, "stop_check": True, "capture_prompts": True},
    "fingerprint": {
        "exclude": [
            ".bug-assurance/**", ".git/**", "**/__pycache__/**", "**/.pytest_cache/**",
            "**/node_modules/**", "**/.dart_tool/**", "**/build/**", "**/bin/Debug/**",
            "**/bin/Release/**", "**/obj/**", "**/*.pyc",
        ]
    },
    "debug_patterns": [
        r"\bbreakpoint\(\)", r"\bpdb\.set_trace\(\)", r"\bconsole\.log\(", r"\bdebugger;",
        r"\bdebugPrint\(", r"#\s*HACK\b", r"\bprint\(\s*['\"]DEBUG",
    ],
    "temp_file_patterns": ["*.tmp", "*.bak", "*.orig", "*.rej", "*~", "*.swp"],
}

CHECK_KINDS = {"format", "lint", "static", "build", "test", "regression", "security", "coverage", "other"}
REVIEWERS = {"business-logic", "regression", "test-quality", "security", "architecture", "coderabbit"}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def validate_config(cfg: dict) -> list[str]:
    problems = []
    if cfg.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"config.schema_version must be {SCHEMA_VERSION}")
    paths = cfg.get("paths", {})
    for key in ("application", "tests", "unrestricted"):
        val = paths.get(key)
        if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
            problems.append(f"config.paths.{key} must be a list of glob strings")
    seen = set()
    for i, chk in enumerate(cfg.get("checks", [])):
        where = f"config.checks[{i}]"
        if not isinstance(chk, dict):
            problems.append(f"{where} must be an object")
            continue
        cid = chk.get("id")
        if not isinstance(cid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._:-]*", cid):
            problems.append(f"{where}.id must match [a-z0-9][a-z0-9._:-]*")
        elif cid in seen:
            problems.append(f"{where}.id duplicates '{cid}'")
        else:
            seen.add(cid)
        if chk.get("not_applicable_reason"):
            if chk.get("required"):
                problems.append(f"{where}: a required check cannot be not-applicable")
            continue
        argv = chk.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            problems.append(f"{where}.argv must be a non-empty list of strings (shell strings are not accepted)")
        if chk.get("kind", "other") not in CHECK_KINDS:
            problems.append(f"{where}.kind must be one of {sorted(CHECK_KINDS)}")
        t = chk.get("timeout_s", 600)
        if not isinstance(t, (int, float)) or isinstance(t, bool) or t <= 0 or t > 7200:
            problems.append(f"{where}.timeout_s must be in (0, 7200]")
        codes = chk.get("success_exit_codes", [0])
        if not isinstance(codes, list) or not codes or not all(isinstance(c, int) for c in codes):
            problems.append(f"{where}.success_exit_codes must be a non-empty list of integers")
    rep = cfg.get("repair", {})
    mx, hard = rep.get("max_cycles"), rep.get("hard_limit")
    if not isinstance(mx, int) or not isinstance(hard, int) or not (1 <= mx <= hard <= 10):
        problems.append("config.repair requires 1 <= max_cycles <= hard_limit <= 10")
    for key in ("required_reviewers", "conditional_reviewers", "optional_reviewers"):
        for r in cfg.get(key, []):
            if r not in REVIEWERS:
                problems.append(f"unknown reviewer '{r}' in {key}")
    for key in ("business-logic", "regression", "test-quality"):
        if key not in cfg.get("required_reviewers", []):
            problems.append(f"'{key}' is mandatory and cannot be removed from required_reviewers")
    for pat in cfg.get("debug_patterns", []):
        try:
            re.compile(pat)
        except re.error as exc:
            problems.append(f"invalid debug pattern {pat!r}: {exc}")
    return problems


def find_repo_root(start: Path | None = None) -> Path:
    """Locate the project root: $BA_PROJECT_DIR / $CLAUDE_PROJECT_DIR, else the nearest ancestor
    containing .bug-assurance/, then .git/, else the starting directory."""
    for var in ("BA_PROJECT_DIR", "CLAUDE_PROJECT_DIR"):
        env = os.environ.get(var)
        if env and Path(env).is_dir():
            return Path(env).resolve()
    here = (start or Path.cwd()).resolve()
    for marker in (ARTIFACT_DIR, ".git"):
        for cand in [here, *here.parents]:
            if (cand / marker).exists():
                return cand
    return here


def load_config(root: Path, required: bool = True) -> dict:
    path = root / ARTIFACT_DIR / CONFIG_FILE
    if not path.exists():
        if required:
            raise BAError(f"no configuration at {path}; run `ba.py init` first")
        return copy.deepcopy(DEFAULT_CONFIG)
    raw = read_json(path)
    if not isinstance(raw, dict):
        raise BAError(f"{path} must contain a JSON object")
    cfg = _merge(DEFAULT_CONFIG, raw)
    problems = validate_config(cfg)
    if problems:
        raise BAError("invalid configuration:\n  - " + "\n  - ".join(problems))
    return cfg


# --------------------------------------------------------------------------------------------
# Run storage
# --------------------------------------------------------------------------------------------

# Files only the scripts may write. The PreToolUse guard denies direct agent edits to them.
SCRIPT_OWNED = {
    "state.json", "events.jsonl", "answers.jsonl", "inbox.jsonl", "findings.json",
    "findings-log.jsonl", "reviews-index.json", "regression-run.json", "diff-audit.json",
    "baseline.json", "final-report.md", "final-report.json", "gate-results.json",
    "gate-history.jsonl", "draft-report.md",
}
SCRIPT_OWNED_DIRS = {"verification"}

# Files the orchestrator and agents author; scripts validate them.
AGENT_OWNED = {
    "ticket.json", "understanding.json", "investigation.md", "decisions.json", "clarification.json",
    "contract.json", "regression-plan.json", "implementation.json",
}
AGENT_OWNED_DIRS = {"reviews", "notes", "evidence"}


def new_run_id() -> str:
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"BA-{stamp}-{secrets.token_hex(2)}"


class Run:
    """A single bug-assurance run rooted at ``.bug-assurance/runs/<run_id>/``."""

    def __init__(self, root: Path, run_id: str):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}", run_id or ""):
            raise BAError(f"invalid run id: {run_id!r}")
        self.root = root
        self.id = run_id
        self.dir = root / ARTIFACT_DIR / "runs" / run_id

    def path(self, name: str) -> Path:
        return self.dir / name

    def exists(self) -> bool:
        return (self.dir / "state.json").exists()

    def state(self) -> dict:
        return read_json(self.path("state.json"))

    def save_state(self, state: dict) -> None:
        state["updated_at"] = now_iso()
        write_json_atomic(self.path("state.json"), state)

    def event(self, kind: str, **data: Any) -> None:
        append_jsonl(self.path("events.jsonl"), {"at": now_iso(), "event": kind, **data})

    def load(self, name: str, default: Any = None) -> Any:
        return read_json(self.path(name), default)

    def has(self, name: str) -> bool:
        return self.path(name).exists()


def active_run(root: Path) -> Run | None:
    pointer = root / ARTIFACT_DIR / ACTIVE_POINTER
    if not pointer.exists():
        return None
    run_id = pointer.read_text(encoding="utf-8").strip()
    if not run_id:
        return None
    try:
        run = Run(root, run_id)
    except BAError:
        return None
    return run if run.exists() else None


def set_active(root: Path, run: Run | None) -> None:
    pointer = root / ARTIFACT_DIR / ACTIVE_POINTER
    if run is None:
        if pointer.exists():
            write_text_atomic(pointer, "")
        return
    write_text_atomic(pointer, run.id + "\n")


def resolve_run(root: Path, run_id: str | None) -> Run:
    if run_id:
        run = Run(root, run_id)
        if not run.exists():
            raise BAError(f"run not found: {run_id}")
        return run
    run = active_run(root)
    if run is None:
        raise BAError("no active run; pass --run <id> or start one with `ba.py start`")
    return run


def list_runs(root: Path) -> list[Run]:
    base = root / ARTIFACT_DIR / "runs"
    if not base.is_dir():
        return []
    return [Run(root, p.name) for p in sorted(base.iterdir()) if (p / "state.json").exists()]
