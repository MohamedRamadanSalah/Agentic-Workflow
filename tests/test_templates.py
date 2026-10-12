"""An unedited template copied into a run must never pass its gate: scaffolding is not evidence."""

from __future__ import annotations

import json

import pytest
from builders import put
from conftest import SCRIPTS

TEMPLATES = SCRIPTS.parent / "templates"


def template(name: str) -> dict:
    return json.loads((TEMPLATES / f"{name}.json").read_text(encoding="utf-8"))


def test_every_template_is_valid_json_and_printable(ba):
    names = sorted(p.stem for p in TEMPLATES.glob("*.json"))
    assert names == ["clarification", "contract", "decisions", "implementation", "regression-plan",
                     "review", "ticket", "understanding"]
    for name in names:
        res = ba("template", name)
        assert res.rc == 0 and isinstance(json.loads(res.out), dict), name


@pytest.mark.parametrize("artifact,gate", [
    ("ticket", "ticket"),
    ("understanding", "investigation"),
    ("contract", "contract"),
    ("regression-plan", "regression"),
    ("implementation", "implementation"),
])
def test_unedited_template_fails_its_gate(ba, repo, artifact, gate):
    ba("start", "--title", "template check")
    put(repo, f"{artifact}.json", template(artifact))
    if gate == "investigation":
        put(repo, "investigation.md", "x" * 300)
    res = ba("gate", gate)
    assert res.rc == 3, f"unedited {artifact} template passed gate {gate}: {res.out}"


def test_unedited_decisions_and_clarification_fail_clarification_gate(ba, repo):
    ba("start", "--title", "template check")
    put(repo, "understanding.json", template("understanding"))
    put(repo, "decisions.json", template("decisions"))
    put(repo, "clarification.json", template("clarification"))
    assert ba("gate", "clarification").rc == 3


def test_unedited_review_template_is_refused(ba, repo):
    ba("start", "--title", "template check")
    p = put(repo, "reviews/business-logic.json", template("review"))
    res = ba("review-record", str(p))
    assert res.rc == 2, res.out
