"""Unit tests for bug_assurance.core: globbing, paths, redaction, config validation, JSON I/O."""

from __future__ import annotations

import copy
import json
import os

import pytest

from bug_assurance.core import (
    DEFAULT_CONFIG, BAError, load_config, match_any, read_json, read_jsonl, redact, rel_to,
    validate_config, write_json_atomic,
)


@pytest.mark.parametrize("path,patterns,expected", [
    ("src/orders/cancellation.py", ["src/**"], True),
    ("src", ["src/**"], False),
    ("lib/a/b/c.dart", ["lib/**"], True),
    ("tests/test_x.py", ["**/test_*.py"], True),
    ("test_x.py", ["**/test_*.py"], True),  # **/ matches zero directories
    ("deep/dir/notes.bak", ["*.bak"], True),  # slash-less pattern matches basename at any depth
    ("deep/dir/notes.bak", ["dir/*.bak"], False),
    ("src/a.py", ["src/*.py"], True),
    ("src/sub/a.py", ["src/*.py"], False),  # single * does not cross directories
    ("./src/a.py", ["src/**"], True),
    ("src\\win\\a.py", ["src/**"], True),
    ("a?c.txt", ["a?c.txt"], True),
    ("abc.txt", ["a?c.txt"], True),
    ("other.txt", [], False),
])
def test_match_any(path, patterns, expected):
    assert match_any(path, patterns) is expected


def test_rel_to_inside_and_outside(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    assert rel_to(root, root / "src" / "a.py") == "src/a.py"
    assert rel_to(root, "src/a.py") == "src/a.py"
    assert rel_to(root, root) == ""
    assert rel_to(root, tmp_path / "elsewhere" / "x.py") is None
    assert rel_to(root, root / ".." / "escape.py") is None
    # a sibling directory that shares the prefix must not count as inside
    assert rel_to(root, tmp_path / "repo2" / "x.py") is None


@pytest.mark.skipif(os.name != "nt", reason="drive letters are Windows-only")
def test_rel_to_other_drive(tmp_path):
    drive = os.path.splitdrive(str(tmp_path))[0].upper()
    other = "Z:" if drive != "Z:" else "Y:"
    assert rel_to(tmp_path, other + "\\x\\y.py") is None


@pytest.mark.parametrize("secret", [
    "ghp_" + "A" * 36,
    "github_pat_" + "B" * 30,
    "AKIA" + "ABCDEFGHIJKLMNOP",
    "sk-ant-" + "x" * 30,
    "xoxb-1234567890-abcdef",
])
def test_redact_token_patterns(secret):
    out = redact(f"value: {secret} end")
    assert secret not in out and "[REDACTED]" in out and out.endswith("end")


def test_redact_keeps_prefix_for_key_value_and_bearer():
    assert redact("password=hunter2 ok") == "password=[REDACTED] ok"
    assert redact('api_key: "abc def"') == "api_key: [REDACTED]"
    out = redact("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123")
    assert "abcdefghijklmnopqrstuvwxyz0123" not in out and "Bearer [REDACTED]" in out
    key = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"
    assert "MIIabc" not in redact(key)
    assert redact("") == "" and redact("nothing secret here") == "nothing secret here"


def _cfg(**over):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg.update(over)
    return cfg


def test_default_config_is_valid():
    assert validate_config(copy.deepcopy(DEFAULT_CONFIG)) == []


def test_config_mandatory_reviewers_cannot_be_removed():
    problems = validate_config(_cfg(required_reviewers=["business-logic"]))
    assert any("'regression' is mandatory" in p for p in problems)
    assert any("'test-quality' is mandatory" in p for p in problems)


def test_config_rejects_shell_string_argv_and_bad_fields():
    problems = validate_config(_cfg(checks=[
        {"id": "lint", "argv": "ruff check ."},
        {"id": "lint", "argv": ["x"], "kind": "nonsense", "timeout_s": 0, "success_exit_codes": []},
        {"id": "Bad Id", "argv": ["x"]},
    ]))
    text = "\n".join(problems)
    assert "argv must be a non-empty list of strings" in text
    assert "duplicates 'lint'" in text
    assert "kind must be one of" in text
    assert "timeout_s must be in" in text
    assert "success_exit_codes must be a non-empty list" in text
    assert "id must match" in text


@pytest.mark.parametrize("repair", [
    {"max_cycles": 3, "hard_limit": 11},
    {"max_cycles": 12, "hard_limit": 12},
    {"max_cycles": 5, "hard_limit": 4},
    {"max_cycles": 0, "hard_limit": 3},
])
def test_config_repair_limits(repair):
    assert any("repair" in p for p in validate_config(_cfg(repair=repair)))


def test_config_required_check_cannot_be_not_applicable():
    problems = validate_config(_cfg(checks=[{"id": "db", "required": True, "not_applicable_reason": "none"}]))
    assert any("cannot be not-applicable" in p for p in problems)


def test_config_unknown_reviewer_and_bad_regex():
    problems = validate_config(_cfg(optional_reviewers=["astrologer"], debug_patterns=["("]))
    assert any("unknown reviewer 'astrologer'" in p for p in problems)
    assert any("invalid debug pattern" in p for p in problems)


def test_load_config_merges_defaults_and_reports_errors(tmp_path):
    (tmp_path / ".bug-assurance").mkdir()
    cfg_path = tmp_path / ".bug-assurance" / "config.json"
    cfg_path.write_text(json.dumps({"schema_version": 1, "checks": []}))
    cfg = load_config(tmp_path)
    assert cfg["repair"]["max_cycles"] == 3 and cfg["paths"]["application"]
    cfg_path.write_text(json.dumps({"schema_version": 1, "repair": {"max_cycles": 99, "hard_limit": 99}}))
    with pytest.raises(BAError, match="invalid configuration"):
        load_config(tmp_path)
    cfg_path.write_text("[1, 2]")
    with pytest.raises(BAError, match="must contain a JSON object"):
        load_config(tmp_path)


def test_load_config_missing(tmp_path):
    with pytest.raises(BAError, match="ba.py init"):
        load_config(tmp_path)
    assert load_config(tmp_path, required=False)["schema_version"] == 1


def test_atomic_write_and_malformed_read(tmp_path):
    p = tmp_path / "nested" / "a.json"
    write_json_atomic(p, {"k": "välue"})
    assert read_json(p) == {"k": "välue"}
    assert [x.name for x in p.parent.iterdir()] == ["a.json"]  # no temp files left behind
    p.write_text("{not json")
    with pytest.raises(BAError, match="malformed JSON"):
        read_json(p)
    with pytest.raises(BAError, match="missing artifact"):
        read_json(tmp_path / "nope.json")
    assert read_json(tmp_path / "nope.json", {"d": 1}) == {"d": 1}


def test_read_jsonl_malformed(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"a": 1}\n\n{broken\n')
    with pytest.raises(BAError, match="line 3"):
        read_jsonl(p)
    assert read_jsonl(tmp_path / "missing.jsonl") == []
