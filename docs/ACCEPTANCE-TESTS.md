# Acceptance tests

## Automated (executable)

```bash
python -m pytest tests -q -p no:cacheprovider                     # everything
python -m pytest tests/test_acceptance.py -q -p no:cacheprovider  # scenarios A–R only
```

Each acceptance test copies `examples/fixture-app/` into a temporary Git repository, configures
real checks (pytest and compileall on the fixture), and drives a run through the real CLI, gates,
verification runner and hook handlers. The model-driven parts (asking a human, reviewing code)
are represented by the artifacts those agents must produce. User answers are fed through the real
UserPromptSubmit capture handler.

This document does not report results. Run the command and read its output; a test counts only
once it has been executed.

| ID | Scenario | Test (`tests/test_acceptance.py`) | What it proves |
|---|---|---|---|
| A | Clear bug | `test_A_clear_bug_proceeds_to_pass` | Full path to FINAL_REPORT = PASS. App edits denied in CLARIFICATION and REGRESSION_TEST, test edits allowed in REGRESSION_TEST, app edits allowed in IMPLEMENTATION |
| A2 | Regression test does not detect the bug | `test_A2_regression_test_that_passes_on_original_code_is_rejected` | A test that passes on buggy code blocks IMPLEMENTATION |
| A3 | Pre-fix run on modified code | `test_A3_pre_fix_run_must_use_original_code` | The regression run refuses if the application differs from the baseline |
| B | Ambiguous requirement | `test_B_ambiguous_requirement_blocks_code_and_asks` | Gate refuses. `questions` lists the targeted question with interpretations. Code edits denied. Stop hook forces the wait to be persisted. SessionStart reports it |
| C | Incomplete answer | `test_C_incomplete_answer_requires_focused_follow_up` | An uncovered decision point blocks, and the follow-up names exactly that point |
| D | Contradictory answer | `test_D_contradictory_answer_is_exposed_not_accepted` | Unresolved contradiction blocks. Claiming resolution without `supersedes` blocks. Agent statements stay UNVERIFIED |
| E | Incorrect root-cause hypothesis | `test_E_incorrect_root_cause_hypothesis_forces_alternatives` | Unexplained contradicting evidence and an open competing hypothesis block. Confirming the alternative passes |
| F | Missing boundary condition | `test_F_missing_boundary_condition_needs_criterion` | An unhandled counterexample blocks. The added criterion must exist in the contract |
| G | Business-logic finding | `test_G_business_logic_finding_blocks_finalization` | Unadjudicated, then confirmed-open HIGH finding blocks FINAL_VERIFICATION. Status not PASS |
| H | Verification failure | `test_H_verification_failure_prevents_pass` | A non-zero exit gives FAIL, exit 4, transition refused, report FAIL |
| I | Missing executable | `test_I_missing_executable_is_blocked_never_passed` | BLOCKED with "executable not found", never PASS |
| J | Repair-loop limit | `test_J_repair_loop_stops_at_limit` | The third cycle is refused at max 2. FAILED_QUALITY_GATE needs a reason. Final status FAIL |
| K | Reviewer disagreement | `test_K_reviewer_disagreement_resolved_by_evidence_not_votes` | Vote/majority rationale refused. Rejection needs checkable evidence, and nonexistent evidence is refused |
| L | Stale evidence | `test_L_stale_evidence_is_invalidated` | A source change after `verify --final` invalidates verification, diff audit and reviews |
| M | Authorized business decision | `test_M_authorized_business_decision_is_recorded` | A captured user message becomes an AUTHORIZED answer. A fabricated quote is refused. The override is recorded. Tampering is detected |
| N | Already-known requirement | `test_N_already_known_requirement_is_not_re_asked` | A duplicate topic is listed as already known, not asked. Resolved by authoritative evidence with no answers |
| O | New ambiguity mid-implementation | `test_O_new_ambiguity_during_implementation_returns_to_clarification` | A new open question freezes code edits live. Moving forward is refused. Return to clarification needs a reason |
| P | Unsafe operations | `test_P_unsafe_operations_are_not_silent` | commit/push/PR/reset/clean/checkout ask. Artifact deletion and evidence writes are denied. Reviewer agents are read-only. Install is a dry-run. Deactivate needs a reason and preserves the run. The repair limit cannot be raised without authorization |
| Q | Report integrity | `test_Q_report_distinguishes_every_check_status` | PASS / FAIL / BLOCKED / NOT_APPLICABLE / NOT_RUN appear exactly as recorded, with no false PASS |
| R | No fabricated certainty | `test_R_unconfirmed_root_cause_is_reported_honestly` | UNCONFIRMED root cause at medium risk gives PASS_WITH_DOCUMENTED_RISKS with diagnostic confidence LOW. At high risk it is blocked at the gate |

Unit tests in `tests/test_core.py`, `test_checks.py`, `test_guard.py`, `test_machine.py`,
`test_bootstrap.py` and `test_ghstatus.py` cover the individual modules, including invoking the
real `ba.py hook` command as a subprocess.

## Limits of automation

The automated tests verify every **deterministic** control. They cannot verify that a live model
asks good questions, interprets answers faithfully, or reviews code competently. Those behaviors
need the manual procedure below. Its steps are **not automated**, and its results must be
recorded by the person running it.

## Manual end-to-end procedure (interactive, in Claude Code)

Prerequisites: open Claude Code in this repository and check that `/hooks` lists the project hooks
(PreToolUse, UserPromptSubmit, SessionStart, Stop → `ba.py hook ...`). Then run
`python .claude/skills/bug-assurance/scripts/ba.py doctor`, which should report `"ok": true`.

1. Run `/bug-assurance start examples/fixture-app/tickets/BUG-101.md`.
   Expect `ba.py start`, a `ticket.json`, and a transition to INVESTIGATION.
2. **Investigation.** Expect delegation to `ba-bug-investigator`, `understanding.json` with the
   policy rule as AUTHORITATIVE_REQUIREMENT (quoted from
   `examples/fixture-app/docs/cancellation-policy.md`), the existing PACKED test flagged as a
   CONTRADICTION, and the developer's courier-label remark as an UNVERIFIED_ASSUMPTION.
3. **Freeze check.** Ask Claude to "just fix the line now". Expect the PreToolUse hook to deny the
   edit to `examples/fixture-app/src/orders/cancellation.py`, citing the state.
4. **Questions.** Expect a targeted question that names the competing interpretations (until
   shipped vs. only before the courier label is printed) and their consequences. It should not be
   a vague "what should happen?". Expect the run to move to BLOCKED_AWAITING_CLARIFICATION.
5. **Incomplete answer.** Reply "yes, packed orders can be cancelled". Expect Claude to record the
   answer (`ba.py answer ... --from-message M-...`) and then ask a focused follow-up about the
   courier-label condition (decision point 2) instead of proceeding.
6. **Contradiction probe** (optional). Reply "customers can never cancel packed orders". Expect
   Claude to point out the conflict with policy rule 1 and ask whether you are authorized to
   override it, rather than accepting the reply.
7. **Authorized answer.** Reply "Follow the approved policy: until shipped; there is no label
   condition." Expect resolution, the CONTRACT (AC for SHIPPED boundary and other-customer
   rejection), a regression test that fails on the original code (`ba.py regression-run`), the
   one-line fix plus the outdated test update, `ba.py verify`, parallel reviewers, adjudication,
   `ba.py verify --final`, and FINAL_REPORT.
8. **Report.** Open `.bug-assurance/runs/<id>/final-report.md`. It must list the actual check
   commands and statuses, and state that no commit, push or PR was made.

Record for each step: pass or fail, plus the run id and a transcript excerpt.
