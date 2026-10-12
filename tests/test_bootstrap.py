"""Unit tests for discovery, init and install (bug_assurance.bootstrap)."""

from __future__ import annotations

import json

import pytest

from bug_assurance.bootstrap import GITIGNORE_BLOCK, discover, ensure_gitignore, init, install, merge_settings
from bug_assurance.core import BAError


def proposed_ids(report):
    return {c["id"] for c in report["proposed_config"]["checks"]} | {m["check"] for m in report["missing"]}


def test_discover_flutter(tmp_path):
    (tmp_path / "pubspec.yaml").write_text("name: app\ndependencies:\n  flutter:\n    sdk: flutter\n")
    rep = discover(tmp_path)
    assert rep["stacks"] == ["flutter"]
    assert {"dart-format", "flutter-analyze", "flutter-test"} <= proposed_ids(rep)
    paths = rep["proposed_config"]["paths"]
    assert "lib/**" in paths["application"] and "test/**" in paths["tests"]
    fmt = next((c for c in rep["proposed_config"]["checks"] if c["id"] == "dart-format"), None)
    if fmt:
        assert fmt["argv"][:4] == ["dart", "format", "--output=none", "--set-exit-if-changed"]


def test_discover_pure_dart(tmp_path):
    (tmp_path / "pubspec.yaml").write_text("name: cli\n")
    rep = discover(tmp_path)
    assert rep["stacks"] == ["dart"] and "dart-analyze" in proposed_ids(rep)


def test_discover_dotnet_in_subdirectory(tmp_path):
    (tmp_path / "src" / "Api").mkdir(parents=True)
    (tmp_path / "src" / "Api" / "Api.csproj").write_text("<Project/>")
    rep = discover(tmp_path)
    assert "dotnet" in rep["stacks"]
    assert {"dotnet-format", "dotnet-build", "dotnet-test"} <= proposed_ids(rep)
    assert "**/*.cs" in rep["proposed_config"]["paths"]["application"]


def test_discover_node_scripts(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"lint": "eslint .", "test": "vitest",
                                                                   "format:check": "prettier -c ."}}))
    rep = discover(tmp_path)
    assert "node" in rep["stacks"]
    ids = proposed_ids(rep)
    assert {"npm-lint", "npm-test", "npm-format-check"} <= ids and "npm-build" not in ids


def test_discover_python(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    rep = discover(tmp_path)
    ids = {c["id"] for c in rep["proposed_config"]["checks"]}
    assert "python" in rep["stacks"] and {"pytest", "compileall"} <= ids
    pytest_check = next(c for c in rep["proposed_config"]["checks"] if c["id"] == "pytest")
    assert pytest_check["argv"][0] == "{python}"  # resolved at run time, never a shell string


def test_discover_unknown_stack(tmp_path):
    rep = discover(tmp_path)
    assert rep["stacks"] == [] and rep["notes"] and rep["proposed_config"]["checks"] == []


def test_init_never_overwrites_config(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    init(tmp_path)
    cfg = tmp_path / ".bug-assurance" / "config.json"
    cfg.write_text(json.dumps({"schema_version": 1, "checks": [], "custom": "mine"}))
    rep = init(tmp_path)
    assert json.loads(cfg.read_text())["custom"] == "mine"
    assert (tmp_path / ".bug-assurance" / "config.proposed.json").exists()
    assert any("config.proposed.json" in a and "unchanged" in a for a in rep["actions"])


def test_init_dry_run_writes_nothing(tmp_path):
    rep = init(tmp_path, apply=False)
    assert rep["applied"] is False and list(tmp_path.iterdir()) == []


def test_gitignore_is_idempotent_and_preserves_content(tmp_path):
    gi = tmp_path / ".gitignore"
    gi.write_text("node_modules/\n.env")
    assert ensure_gitignore(tmp_path) == GITIGNORE_BLOCK
    assert ensure_gitignore(tmp_path) == []
    text = gi.read_text()
    assert text.startswith("node_modules/\n.env\n")
    for line in GITIGNORE_BLOCK:
        assert text.count(line + "\n") == 1


@pytest.fixture
def toolkit(tmp_path):
    root = tmp_path / "toolkit"
    skill = root / ".claude" / "skills" / "bug-assurance"
    (skill / "scripts" / "bug_assurance" / "__pycache__").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: bug-assurance\n---\n")
    (skill / "scripts" / "ba.py").write_text("print('ba')\n")
    (skill / "scripts" / "bug_assurance" / "__pycache__" / "x.cpython-313.pyc").write_bytes(b"\0")
    agents = root / ".claude" / "agents"
    agents.mkdir()
    (agents / "ba-implementer.md").write_text("---\nname: ba-implementer\n---\n")
    (agents / "unrelated.md").write_text("not ours")
    return root


@pytest.fixture
def target(tmp_path):
    t = tmp_path / "target"
    (t / ".claude").mkdir(parents=True)
    (t / ".claude" / "settings.json").write_text(json.dumps({
        "permissions": {"deny": ["Read(./.env)"]},
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}]},
    }))
    (t / "pyproject.toml").write_text("")
    return t


def test_install_dry_run_writes_nothing(toolkit, target):
    before = sorted(p.relative_to(target) for p in target.rglob("*"))
    plan = install(toolkit, target, apply=False)
    assert sorted(p.relative_to(target) for p in target.rglob("*")) == before
    assert ".claude/skills/bug-assurance/SKILL.md" in plan["copy"]
    assert ".claude/agents/ba-implementer.md" in plan["copy"]
    assert not any("__pycache__" in c or c.endswith(".pyc") for c in plan["copy"])
    assert ".claude/agents/unrelated.md" not in plan["copy"]
    assert set(plan["settings"]["hook_events_added"]) == {"PreToolUse", "UserPromptSubmit", "SessionStart", "Stop"}


def test_install_apply_merges_and_is_idempotent(toolkit, target):
    plan = install(toolkit, target, apply=True, interpreter="python3")
    assert (target / ".claude/skills/bug-assurance/scripts/ba.py").exists()
    assert (target / ".claude/agents/ba-implementer.md").exists()
    assert not (target / ".claude/agents/unrelated.md").exists()
    assert (target / ".bug-assurance" / "config.json").exists()
    settings = json.loads((target / ".claude" / "settings.json").read_text())
    assert settings["permissions"] == {"deny": ["Read(./.env)"]}
    pre = settings["hooks"]["PreToolUse"]
    assert pre[0]["hooks"][0]["command"] == "echo mine"  # existing hook preserved
    assert 'python3 "$CLAUDE_PROJECT_DIR/.claude/skills/bug-assurance/scripts/ba.py" hook pre-tool-use' == \
        pre[1]["hooks"][0]["command"]
    assert "Edit" in pre[1]["matcher"] and "PowerShell" in pre[1]["matcher"]
    backups = list((target / ".claude").glob("settings.json.bak-*"))
    assert len(backups) == 1 and plan["settings"]["backup"] == backups[0].name
    assert json.loads(backups[0].read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "echo mine"

    again = install(toolkit, target, apply=True, interpreter="python3")
    assert again["copy"] == [] and again["settings"]["hook_events_added"] == []
    assert ".claude/skills/bug-assurance/SKILL.md" in again["identical"]
    assert len(list((target / ".claude").glob("settings.json.bak-*"))) == 1  # nothing to back up


def test_install_never_overwrites_conflicts(toolkit, target):
    install(toolkit, target, apply=True)
    skill_md = target / ".claude/skills/bug-assurance/SKILL.md"
    skill_md.write_text("locally customized")
    plan = install(toolkit, target, apply=True)
    assert ".claude/skills/bug-assurance/SKILL.md" in plan["conflicts"] and "note" in plan
    assert skill_md.read_text() == "locally customized"


def test_install_rejects_bad_targets(toolkit, tmp_path):
    with pytest.raises(BAError, match="not a directory"):
        install(toolkit, tmp_path / "missing", apply=False)
    with pytest.raises(BAError, match="toolkit repository itself"):
        install(toolkit, toolkit, apply=False)
    empty = tmp_path / "empty-toolkit"
    empty.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(BAError, match="toolkit skill not found"):
        install(empty, dest, apply=False)


def test_merge_settings_recognizes_existing_ba_hooks():
    existing = {"hooks": {"Stop": [{"hooks": [{"type": "command",
                                                "command": 'python "x/ba.py" hook stop'}]}]}}
    merged, added = merge_settings(existing)
    assert "Stop" not in added and len(merged["hooks"]["Stop"]) == 1
    assert existing["hooks"].keys() == {"Stop"}  # input not mutated
