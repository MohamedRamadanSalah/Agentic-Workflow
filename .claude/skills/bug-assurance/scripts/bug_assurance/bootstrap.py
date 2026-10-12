"""Repository discovery, initialization, and installation into another repository.

Nothing here overwrites existing user files: configuration is written only when absent (a
proposal file is written otherwise), installs are dry-run unless --apply is given, differing
files are reported as conflicts, and settings.json is merged after a timestamped backup.
"""

from __future__ import annotations

import copy
import filecmp
import json
import os
import shutil
import sys
from pathlib import Path

from .core import (
    ARTIFACT_DIR, CONFIG_FILE, DEFAULT_CONFIG, BAError, now_iso, read_json, write_json_atomic,
    write_text_atomic,
)

GITIGNORE_BLOCK = [
    "# bug-assurance local evidence (may contain captured user messages)",
    ".bug-assurance/runs/",
    ".bug-assurance/inbox.jsonl",
    ".bug-assurance/active-run",
    ".bug-assurance/config.proposed.json",
]

HOOK_EVENTS = {
    "PreToolUse": ("Edit|Write|MultiEdit|NotebookEdit|Bash|PowerShell", "pre-tool-use"),
    "UserPromptSubmit": (None, "user-prompt-submit"),
    "SessionStart": (None, "session-start"),
    "Stop": (None, "stop"),
}


def _which(name: str) -> str | None:
    return shutil.which(name)


def _check(cid, argv, kind, required=True, timeout=900, note=""):
    chk = {"id": cid, "argv": argv, "kind": kind, "required": required, "timeout_s": timeout}
    if note:
        chk["description"] = note
    return chk


def discover(root: Path) -> dict:
    """Detect stacks and propose checks. Only checks whose executables exist are proposed as
    required; missing tools are listed so the gap is visible instead of silently skipped."""
    stacks, checks, missing, app, tests = [], [], [], [], []
    tools = {t: _which(t) for t in ("git", "gh", "python", "node", "npm", "dart", "flutter",
                                     "dotnet", "go", "cargo", "ruff", "actionlint")}

    def add(chk, exe):
        if tools.get(exe) or _which(exe) or exe == "{python}":
            checks.append(chk)
        else:
            missing.append({"check": chk["id"], "missing_executable": exe})

    pubspec = root / "pubspec.yaml"
    if pubspec.exists():
        is_flutter = "flutter:" in pubspec.read_text(encoding="utf-8", errors="replace")
        tool = "flutter" if is_flutter else "dart"
        stacks.append("flutter" if is_flutter else "dart")
        add(_check("dart-format", ["dart", "format", "--output=none", "--set-exit-if-changed", "lib", "test"],
                   "format"), "dart")
        add(_check(f"{tool}-analyze", [tool, "analyze"], "static"), tool)
        add(_check(f"{tool}-test", [tool, "test"], "test", timeout=1800), tool)
        app += ["lib/**", "pubspec.yaml", "analysis_options.yaml"]
        tests += ["test/**", "integration_test/**"]
    if list(root.glob("*.sln")) or list(root.glob("*.slnx")) or list(root.glob("**/*.csproj"))[:1]:
        stacks.append("dotnet")
        add(_check("dotnet-format", ["dotnet", "format", "--verify-no-changes"], "format"), "dotnet")
        add(_check("dotnet-build", ["dotnet", "build", "--nologo"], "build", timeout=1800), "dotnet")
        add(_check("dotnet-test", ["dotnet", "test", "--nologo"], "test", timeout=1800), "dotnet")
        app += ["**/*.cs", "**/*.csproj", "**/*.sln", "**/*.slnx"]
        tests += ["**/*Tests/**", "**/*.Tests/**", "tests/**", "test/**"]
    pkg = root / "package.json"
    if pkg.exists():
        stacks.append("node")
        try:
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {})
        except json.JSONDecodeError:
            scripts = {}
        for name, kind in (("format:check", "format"), ("lint", "lint"), ("typecheck", "static"),
                           ("build", "build"), ("test", "test")):
            if name in scripts:
                add(_check(f"npm-{name.replace(':', '-')}", ["npm", "run", name, "--silent"], kind), "npm")
        app += ["src/**", "app/**", "lib/**", "package.json"]
        tests += ["**/*.test.*", "**/*.spec.*", "__tests__/**", "tests/**", "test/**"]
    if any((root / f).exists() for f in ("pyproject.toml", "setup.cfg", "setup.py", "pytest.ini",
                                         "requirements.txt")) or list(root.glob("tests/test_*.py")):
        stacks.append("python")
        add(_check("pytest", ["{python}", "-m", "pytest", "-q"], "test", timeout=1800), "{python}")
        add(_check("compileall", ["{python}", "-m", "compileall", "-q", "."], "build", timeout=600), "{python}")
        if tools["ruff"]:
            checks.append(_check("ruff", ["ruff", "check", "."], "lint"))
        else:
            missing.append({"check": "ruff (optional lint)", "missing_executable": "ruff"})
        app += ["src/**", "app/**", "**/*.py"]
        tests += ["tests/**", "**/test_*.py", "**/*_test.py"]
    if (root / "go.mod").exists():
        stacks.append("go")
        add(_check("go-vet", ["go", "vet", "./..."], "static"), "go")
        add(_check("go-test", ["go", "test", "./..."], "test", timeout=1800), "go")
        app += ["**/*.go", "go.mod"]
        tests += ["**/*_test.go"]
    if (root / "Cargo.toml").exists():
        stacks.append("rust")
        add(_check("cargo-fmt", ["cargo", "fmt", "--check"], "format"), "cargo")
        add(_check("cargo-test", ["cargo", "test"], "test", timeout=1800), "cargo")
        app += ["src/**", "Cargo.toml"]
        tests += ["tests/**"]

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if app:
        cfg["paths"]["application"] = sorted(set(app))
    if tests:
        cfg["paths"]["tests"] = sorted(set(tests))
    cfg["checks"] = checks
    return {"root": str(root), "stacks": stacks, "tools": tools, "missing": missing,
            "proposed_config": cfg,
            "notes": [] if stacks else ["no known stack detected; define checks manually in config.json"]}


def ensure_gitignore(root: Path, apply: bool = True) -> list[str]:
    path = root / ".gitignore"
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    missing = [line for line in GITIGNORE_BLOCK if line not in existing]
    if missing and apply:
        text = "\n".join(existing)
        if text and not text.endswith("\n"):
            text += "\n"
        text += ("\n" if existing else "") + "\n".join(missing) + "\n"
        write_text_atomic(path, text)
    return missing


def init(root: Path, apply: bool = True) -> dict:
    report = discover(root)
    base = root / ARTIFACT_DIR
    cfg_path = base / CONFIG_FILE
    actions = []
    if cfg_path.exists():
        target = base / "config.proposed.json"
        verb = "proposal written to" if apply else "would write proposal to"
        actions.append(f"config exists (left unchanged); {verb} {target.relative_to(root).as_posix()}")
    else:
        target = cfg_path
        actions.append(f"{'wrote' if apply else 'would write'} {target.relative_to(root).as_posix()}")
    if apply:
        write_json_atomic(target, report["proposed_config"])
    for line in ensure_gitignore(root, apply=apply):
        actions.append(f".gitignore += {line}")
    report["actions"] = actions
    report["applied"] = apply
    return report


def hook_command(event_arg: str, interpreter: str | None = None) -> str:
    interp = interpreter or ("python" if os.name == "nt" else "python3")
    return f'{interp} "$CLAUDE_PROJECT_DIR/.claude/skills/bug-assurance/scripts/ba.py" hook {event_arg}'


def hooks_settings(interpreter: str | None = None) -> dict:
    hooks = {}
    for event, (matcher, arg) in HOOK_EVENTS.items():
        entry = {"hooks": [{"type": "command", "command": hook_command(arg, interpreter), "timeout": 30}]}
        if matcher:
            entry["matcher"] = matcher
        hooks[event] = [entry]
    return hooks


def merge_settings(existing: dict, interpreter: str | None = None) -> tuple[dict, list[str]]:
    merged = copy.deepcopy(existing)
    added = []
    hooks = merged.setdefault("hooks", {})
    for event, entries in hooks_settings(interpreter).items():
        arg = HOOK_EVENTS[event][1]
        current = hooks.setdefault(event, [])
        present = any(f"ba.py\" hook {arg}" in h.get("command", "") or f"ba.py hook {arg}" in h.get("command", "")
                      for e in current for h in e.get("hooks", []))
        if not present:
            current.extend(entries)
            added.append(event)
    return merged, added


def install(source_root: Path, target: Path, apply: bool = False, interpreter: str | None = None) -> dict:
    if not target.is_dir():
        raise BAError(f"target is not a directory: {target}")
    if target.resolve() == source_root.resolve():
        raise BAError("target is the toolkit repository itself")
    plan = {"target": str(target), "apply": apply, "copy": [], "identical": [], "conflicts": [],
            "settings": None, "init": None}
    sources = []
    skill = source_root / ".claude" / "skills" / "bug-assurance"
    if not (skill / "SKILL.md").is_file():
        raise BAError(f"toolkit skill not found at {skill}")
    for p in sorted(skill.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
            sources.append(p)
    sources += sorted((source_root / ".claude" / "agents").glob("ba-*.md"))
    for src in sources:
        rel = src.relative_to(source_root)
        dst = target / rel
        if not dst.exists():
            plan["copy"].append(rel.as_posix())
            if apply:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        elif filecmp.cmp(src, dst, shallow=False):
            plan["identical"].append(rel.as_posix())
        else:
            plan["conflicts"].append(rel.as_posix())
    settings_path = target / ".claude" / "settings.json"
    existing = read_json(settings_path) if settings_path.exists() else {}
    merged, added = merge_settings(existing, interpreter)
    plan["settings"] = {"path": ".claude/settings.json", "hook_events_added": added}
    if apply and added:
        if settings_path.exists():
            backup = settings_path.with_name(f"settings.json.bak-{now_iso().replace(':', '')}")
            shutil.copy2(settings_path, backup)
            plan["settings"]["backup"] = backup.name
        write_json_atomic(settings_path, merged)
    plan["init"] = init(target, apply=apply)["actions"]
    if plan["conflicts"]:
        plan["note"] = ("conflicting files were NOT overwritten; compare them manually and copy the "
                        "toolkit version if you intend to upgrade")
    return plan


def self_path() -> Path:
    return Path(sys.argv[0]).resolve()
