"""Unit tests for the verification runner (real subprocess executions, no mocks)."""

from __future__ import annotations

import copy
import json

import pytest
from builders import put
from conftest import FIXTURE_CONFIG, make_repo, run_ba

from bug_assurance.checks import regression_run, run_command, run_verification
from bug_assurance.core import BAError, load_config, resolve_run

PY = "{python}"


def chk(cid, code="", **over):
    c = {"id": cid, "argv": [PY, "-c", code], "kind": "other", "required": True, "timeout_s": 60}
    c.update(over)
    return c


def test_pass_and_fail_exit_codes(tmp_path):
    ok = run_command(tmp_path, chk("ok", "print('hi')"))
    assert ok["status"] == "PASS" and ok["exit_code"] == 0 and "hi" in ok["stdout_tail"]
    bad = run_command(tmp_path, chk("bad", "import sys; sys.stderr.write('boom'); sys.exit(7)"))
    assert bad["status"] == "FAIL" and bad["exit_code"] == 7 and "boom" in bad["stderr_tail"]


def test_custom_success_exit_codes(tmp_path):
    r = run_command(tmp_path, chk("five", "import sys; sys.exit(5)", success_exit_codes=[0, 5]))
    assert r["status"] == "PASS" and r["exit_code"] == 5
    r = run_command(tmp_path, chk("zero", "", success_exit_codes=[1]))
    assert r["status"] == "FAIL"


def test_timeout_is_fail(tmp_path):
    r = run_command(tmp_path, chk("slow", "import time; time.sleep(30)", timeout_s=1))
    assert r["status"] == "FAIL" and "timed out after 1s" in r["detail"] and r["exit_code"] is None


def test_missing_executable_and_bad_cwd_are_blocked(tmp_path):
    r = run_command(tmp_path, {"id": "x", "argv": ["no-such-tool-zzz", "--v"]})
    assert r["status"] == "BLOCKED" and "executable not found" in r["detail"]
    r = run_command(tmp_path, {"id": "y", "argv": [str(tmp_path / "missing" / "tool.exe")]})
    assert r["status"] == "BLOCKED"
    r = run_command(tmp_path, chk("z", "print(1)", cwd="does/not/exist"))
    assert r["status"] == "BLOCKED" and "working directory not found" in r["detail"]


def test_not_applicable_status(tmp_path):
    r = run_command(tmp_path, {"id": "db", "not_applicable_reason": "no database"})
    assert r["status"] == "NOT_APPLICABLE_WITH_JUSTIFICATION" and r["detail"] == "no database"


def _cfg(checks):
    cfg = copy.deepcopy(FIXTURE_CONFIG)
    cfg["checks"] = checks
    return cfg


def test_unselected_checks_are_not_run_and_unknown_rejected(tmp_path):
    repo = make_repo(tmp_path, _cfg([chk("a", "print(1)"), chk("b", "print(2)")]))
    res = run_ba(repo, "verify", "--standalone", "--only", "a")
    assert res.rc == 0
    by = {r["id"]: r for r in res.json["results"]}
    assert by["a"]["status"] == "PASS" and by["b"]["status"] == "NOT_RUN"
    assert res.json["summary"]["NOT_RUN"] == 1
    res = run_ba(repo, "verify", "--standalone", "--only", "nope")
    assert res.rc == 2 and "unknown check" in res.err


def test_final_with_only_is_refused(tmp_path):
    repo = make_repo(tmp_path, _cfg([chk("a", "print(1)")]))
    with pytest.raises(BAError, match="--final runs every check"):
        run_verification(repo, load_config(repo), final=True, only=["a"])


def test_secrets_redacted_in_tail_and_log(tmp_path):
    token = "ghp_" + "Z" * 36
    repo = make_repo(tmp_path, _cfg([chk("leaky", f"print('token={token}'); print('{token}')")]))
    assert run_ba(repo, "start", "--title", "t").rc == 0
    res = run_ba(repo, "verify")
    assert res.rc == 0
    run = resolve_run(repo, None)
    rec = json.loads((run.path("verification") / "001.json").read_text())
    r = rec["results"][0]
    assert token not in r["stdout_tail"] and "[REDACTED]" in r["stdout_tail"]
    log = (run.dir / r["log"]).read_text()
    assert token not in log and "[REDACTED]" in log
    assert token not in json.dumps(rec)


def test_fingerprint_change_during_run_is_inconsistent(tmp_path):
    repo = make_repo(tmp_path, _cfg([chk("mutator", "open('src/generated.py','w').write('x = 1')")]))
    res = run_ba(repo, "verify", "--standalone")
    assert res.rc == 4
    assert res.json["fingerprint"] == "INCONSISTENT"
    assert "results are unusable" in res.err


def test_required_failure_sets_exit_code_and_out_file(tmp_path):
    repo = make_repo(tmp_path, _cfg([chk("req", "import sys; sys.exit(1)"),
                                      chk("opt", "import sys; sys.exit(1)", required=False)]))
    out = tmp_path / "result.json"
    res = run_ba(repo, "verify", "--standalone", "--out", str(out))
    assert res.rc == 4 and res.json["required_failures"] == ["req"]
    assert json.loads(out.read_text())["required_failures"] == ["req"]


def test_regression_run_outcomes(tmp_path):
    repo = make_repo(tmp_path)
    assert run_ba(repo, "start", "--title", "t").rc == 0
    put(repo, "regression-plan.json", {"feasible": True, "tests": [
        {"id": "passes", "argv": [PY, "-c", "pass"], "acceptance_criteria": ["AC-1"]},
        {"id": "fails", "argv": [PY, "-c", "import sys; sys.exit(1)"], "acceptance_criteria": ["AC-1"]},
        {"id": "collect-error", "argv": [PY, "-c", "import sys; sys.exit(2)"], "acceptance_criteria": ["AC-1"]},
        {"id": "usage-error", "argv": [PY, "-c", "import sys; sys.exit(4)"], "acceptance_criteria": ["AC-1"]},
        {"id": "custom", "argv": [PY, "-c", "import sys; sys.exit(4)"], "acceptance_criteria": ["AC-1"],
         "expected_failure_exit_codes": [4]},
        {"id": "missing", "argv": ["no-such-tool-zzz"], "acceptance_criteria": ["AC-1"]},
    ]})
    run = resolve_run(repo, None)
    rec = regression_run(run, load_config(repo))
    out = {r["id"]: r["outcome"] for r in rec["results"]}
    assert out == {"passes": "PASSED_UNEXPECTEDLY", "fails": "FAILED_AS_EXPECTED", "collect-error": "ERROR",
                   "usage-error": "ERROR", "custom": "FAILED_AS_EXPECTED", "missing": "BLOCKED"}
    assert rec["plan_digest"] and rec["app_fingerprint"]
    assert run_ba(repo, "regression-run").rc == 3  # not all FAILED_AS_EXPECTED


def test_regression_run_refuses_infeasible_plan(tmp_path):
    repo = make_repo(tmp_path)
    assert run_ba(repo, "start", "--title", "t").rc == 0
    put(repo, "regression-plan.json", {"feasible": False})
    res = run_ba(repo, "regression-run")
    assert res.rc == 2 and "infeasible" in res.err
