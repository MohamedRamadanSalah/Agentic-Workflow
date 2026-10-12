# Usage

All controller commands run from the project root:

```bash
BA="python .claude/skills/bug-assurance/scripts/ba.py"   # python3 on Linux/macOS
$BA --help                     # full command list; `$BA <cmd> --help` for flags
```

Exit codes: `0` ok, `2` usage/validation error, `3` gate refused or failed, `4` verification
finished with a required check not passing, `1` unexpected internal error.

## 1. Initialize and validate

```bash
$BA discover          # read-only: detected stacks, tools, missing executables, proposed config
$BA init              # writes .bug-assurance/config.json ONLY if absent (else config.proposed.json)
$BA init --dry-run    # show what would be written
$BA doctor            # validates config, hook wiring in .claude/settings.json, agents, skill, git
```

Review `.bug-assurance/config.json` (paths and checks) before the first run.
See [EXTENDING.md](EXTENDING.md).

### Install into another repository

```bash
$BA install --target ../my-service            # dry-run: prints the plan, writes nothing
$BA install --target ../my-service --apply    # copies the skill + ba-* agents, merges hooks
$BA install --target ../my-service --apply --interpreter python3
```

Install never overwrites differing files. They are listed as `conflicts`. It merges only missing
hook entries into `.claude/settings.json`, after writing a timestamped `settings.json.bak-*`
backup, and runs `init` in the target. Restart Claude Code (or review `/hooks`) in the target
afterwards.

## 2. Start a run (in Claude Code)

```
/bug-assurance start <ticket text | issue URL | path/to/ticket.md>
```

The skill makes Claude act as the Orchestrator. It runs `$BA doctor`, `$BA start --title ...`, and
walks the states, delegating to the `ba-*` agents. Optionally run the whole session as the
orchestrator agent: `claude --agent ba-orchestrator`.

Expect questions. When an implementation-critical business rule is ambiguous, the run moves to
`BLOCKED_AWAITING_CLARIFICATION` and **application code stays frozen** until you answer. Your
messages are captured with a hash, so your answer becomes a verifiable decision record.

## 3. Resume a blocked run

```
/bug-assurance resume
```

or manually:

```bash
$BA runs                       # list runs with state and outcome
$BA activate <run-id>          # make it the active run if it is not
$BA status                     # state, expected artifacts, each transition's gate result
$BA questions                  # the pending question batch (already-answered topics excluded)
```

Verification results and reviews are fingerprinted. After any code change they are stale and
must be re-run; old results are never reused.

## 4. Verification only

```bash
$BA verify                              # active run: all checks, results recorded in the run
$BA verify --only fixture-tests,compile # subset (others recorded as NOT_RUN)
$BA verify --final                      # final verification (all checks; cannot combine with --only)
$BA verify --standalone --out verify.json   # no run needed (CI or ad hoc)
```

Statuses: `PASS`, `FAIL` (non-success exit code or timeout), `BLOCKED` (could not execute, for
example a missing executable), `NOT_RUN`, `NOT_APPLICABLE_WITH_JUSTIFICATION`. Only PASS is a pass.

## 5. Review a diff without changing code

```
/bug-assurance review
```

The Orchestrator runs `$BA diff-scope`, which classifies changed files as application, tests,
other or unrestricted. It then delegates to the business-logic, regression and test-quality
reviewers (plus security and architecture when relevant). They are read-only: the hook denies
`ba-*` reviewer writes to project files.

## 6. Reports and status

```bash
$BA report     # draft-report.md with the computed (provisional) status
$BA status --json
```

The final report is written only by `$BA transition FINAL_REPORT`, to
`.bug-assurance/runs/<id>/final-report.md` and `.json`. Overall status is one of `PASS`,
`PASS_WITH_DOCUMENTED_RISKS`, `BLOCKED` or `FAIL`.

## 7. Inspecting a run

`.bug-assurance/runs/<run-id>/`:

| File | Content |
|---|---|
| `state.json`, `events.jsonl` | current state, transition history, every event |
| `gate-results.json`, `gate-history.jsonl` | last and all gate evaluations (problems, warnings, risks) |
| `ticket.json`, `understanding.json`, `investigation.md` | intake and bug-understanding model |
| `decisions.json`, `answers.jsonl`, `clarification.json` | questions, recorded answers with provenance, dimension assessment |
| `contract.json` | behavioral contract (requirements, Given/When/Then scenarios, required checks) |
| `regression-plan.json`, `regression-run.json` | regression tests and their pre-fix (original code) outcomes |
| `implementation.json`, `diff-audit.json`, `baseline.json` | change summary, audit, start-of-run snapshot |
| `verification/NNN.json`, `verification/logs/` | fingerprinted check results and full (redacted) logs |
| `reviews/*.json`, `reviews-index.json`, `findings.json`, `findings-log.jsonl` | reviews, findings, dispositions |
| `final-report.md` / `.json` | the final report |

Templates for agent-written files: `$BA template <name>`. Run it with an unknown name to list the
available templates.

## 8. Worked example: BUG-101 in the fixture app

`examples/fixture-app/` contains a seeded bug. `can_cancel` rejects a customer's own PACKED order,
but `docs/cancellation-policy.md` (approved policy) says customers may cancel "until it has been
shipped". The existing test `test_customer_cannot_cancel_packed_order` *encodes the bug*, and the
ticket contains a developer *opinion* about a courier-label condition.

What the workflow does with it:

1. **INTAKE.** `ticket.json` records the symptom and risk `medium`.
2. **INVESTIGATION** (read-only). Claims are classified: the policy rule is
   AUTHORITATIVE_REQUIREMENT (with a verbatim quote, checked against the file); the code
   threshold is REPOSITORY_EVIDENCE; the existing test is a CONTRADICTION of the policy; the
   courier-label idea is an UNVERIFIED_ASSUMPTION. Hypotheses H-1 (wrong threshold) and H-2 (UI
   sends wrong status) are recorded, and H-2 is refuted by a direct domain-level reproduction.
3. **CLARIFICATION.** Q-1 asks whether PACKED is cancellable and whether a label condition
   applies (two decision points). If the policy settles both, Q-1 is resolved as
   VERIFIED_EVIDENCE citing the authoritative claim. If you instead give a different rule, it is
   recorded as an AUTHORIZED_DECISION that explicitly `supersedes` the policy claim.
4. **CONTRACT.** AC-1 (PACKED → allowed), AC-2 boundary (SHIPPED → rejected), AC-3 (other
   customer → rejected), AC-4 (support unchanged).
5. **REGRESSION_TEST.** `tests/test_regression_bug101.py` is written and `$BA regression-run`
   proves it fails on the original code.
6. **IMPLEMENTATION.** A one-line threshold change, plus updating the outdated test (justified by
   the policy). Then `$BA diff-audit`.
7. **VERIFICATION → REVIEW → ADJUDICATION → FINAL_VERIFICATION → FINAL_REPORT.**

The automated acceptance test `test_A_clear_bug_proceeds_to_pass` drives exactly this path. See
[ACCEPTANCE-TESTS.md](ACCEPTANCE-TESTS.md) for the manual, interactive version.
