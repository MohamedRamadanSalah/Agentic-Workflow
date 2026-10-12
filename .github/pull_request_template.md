## Summary

<!-- What changed and why. Link the ticket. -->

## Bug-assurance evidence

> This checklist records the automated workflow's evidence. It does **not** replace human review:
> a PASS means the workflow's gates passed, not that the change is defect-free.

- Run id: `BA-...` (`.bug-assurance/runs/<id>/final-report.md`)
- Final status: `PASS` / `PASS_WITH_DOCUMENTED_RISKS` / `BLOCKED` / `FAIL`

### Clarification decisions
<!-- Each material question, its resolution kind (AUTHORIZED_DECISION / VERIFIED_EVIDENCE /
SUPPORTED_INFERENCE), who authorized it, and the acceptance criteria it maps to. -->
| Question | Resolution | Authority / evidence | Acceptance criteria |
|---|---|---|---|

### Root cause
- Status: `CONFIRMED` / `PROBABLE` / `UNCONFIRMED` (with diagnostic limitation)
- Evidence:

### Regression evidence
- [ ] Regression test failed on the original code (`ba.py regression-run` outcome FAILED_AS_EXPECTED)
- [ ] Regression test passes after the fix
- [ ] Or: infeasibility reason and alternative strategy documented

### Verification (final run, fresh against the PR head)
| Check | Required | Status | Exit code |
|---|---|---|---|

### Review findings
| ID | Reviewer | Severity | Disposition | Resolution |
|---|---|---|---|---|

### Remaining risks / unexecuted checks
-

## Human reviewer checklist
- [ ] The behavioral contract matches the intended business rule
- [ ] Tests assert requirements rather than mirror the implementation
- [ ] No unrelated changes, debug code or weakened tests
