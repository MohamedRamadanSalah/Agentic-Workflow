# Implementation report: Bug Assurance Engineering System

**Overall status: PASS_WITH_DOCUMENTED_RISKS.** Every gate and test that can run locally passes.
The risks are listed under *Pending* and *Limitations* below.

Date: 2026-10-13 · Environment: Windows 10, Claude Code 2.1.296, Python 3.13.2, Git Bash.
Published with the user's approval to https://github.com/MohamedRamadanSalah/Agentic-Workflow.

## Verified results (commands actually run)

| Command | Result |
|---|---|
| `python -m pytest tests -q -p no:cacheprovider` | **169 passed**, 0 failed (≈2 min), including `test_templates.py`: no unedited template passes its gate |
| `ba.py doctor` | ok, no problems or warnings |
| `ba.py validate-workflows` | exit 0, no problems or warnings |
| `ba.py verify --standalone` | 4/4 PASS (system-tests, fixture-tests, compile, validate-workflows) |
| GitHub Actions run 38318943070 on `5903d82` (push to `main`) | **success**: `self-test` 168 passed, 1 skipped (Windows-only drive-letter test); `assurance-checks` 3/3 PASS, no required failures; `workflow-lint` success |
| `ba.py github-status` (remote `MohamedRamadanSalah/Agentic-Workflow`) | file created YES · syntax YES · executed YES · reported YES · **required NO** · enforcement NO |
| Live PreToolUse hook in this session | Edit to `examples/fixture-app/src/orders/cancellation.py` during run `BA-20261012-222556-b97b` (INTAKE) was **denied** by the hook |
| Live UserPromptSubmit hook | fired on real session turns, writing records to `.bug-assurance/inbox.jsonl` (this exposed bug 1 below) |
| Hook command lines run exactly as configured (`bash -c`, `CLAUDE_PROJECT_DIR`) | ask on `git commit` / `gh pr create`; deny on `rm -rf .bug-assurance`; silent otherwise |

`ba.py` is short for `python .claude/skills/bug-assurance/scripts/ba.py`.

## Acceptance tests A–R (executed, automated)

All of them are in `tests/test_acceptance.py` and pass. The mapping is in `docs/ACCEPTANCE-TESTS.md`.
A clear bug → PASS · A2 a regression test that passes on the original code is rejected · A3 the pre-fix run
must use the original code · B ambiguity blocks edits and asks · C an incomplete answer gets a follow-up · D a
contradiction is exposed · E a wrong hypothesis forces alternatives · F a missing boundary needs an AC · G a
business-logic finding blocks · H a failed check blocks PASS · I a missing tool is BLOCKED · J the repair limit
holds · K disagreements are settled by evidence, not votes · L stale evidence is invalidated · M an authorized
decision is recorded with provenance · M2 system turns are never user answers · N known requirements are not
re-asked · O a new ambiguity returns the run to clarification · P unsafe operations are not silent · Q the report
shows every check status · R an unconfirmed root cause is reported honestly.

## Bugs found and fixed during the build

1. **Answer provenance (security-relevant).** Background-task notifications reach UserPromptSubmit and were
   being captured as user messages, so an agent's report could have authorized a business decision.
   Fix: `ledger.is_system_generated()` now filters them at capture time and when the inbox is read.
   Test: `test_M2_*`. Legacy records stay in the append-only inbox but are ignored.
2. Verification records stored check argv without redaction (`checks._redact_argv`).
3. `git clean -fdx` and `git checkout .` were not caught by the git-safety pattern.
4. `install` resolved the toolkit root one level too shallow.
5. With no HEAD commit, every file was flagged as a "pre-existing user change".

## Implemented components

- Skill: `.claude/skills/bug-assurance/SKILL.md` with `reference/` and `templates/`. Invoke as `/bug-assurance`.
- Orchestrator: the skill-driven main thread or `claude --agent ba-orchestrator`, over the deterministic
  state machine `bug_assurance/machine.py`, where every transition is gated in `gates.py`.
- Agents: `.claude/agents/ba-*.md` (investigator, 5 reviewers, implementer, adjudicator, orchestrator).
- Hooks: `.claude/settings.json` (PreToolUse, UserPromptSubmit, SessionStart, Stop → `ba.py hook …`).
- CI: `.github/workflows/bug-assurance.yml`, with jobs self-test, assurance-checks and workflow-lint, all
  SHA-pinned. Plus a PR template and `.coderabbit.yaml`.
- Config: `.bug-assurance/config.json`. Fixture: `examples/fixture-app/` (BUG-101, deliberately left unfixed).
- Docs: `README.md`, `docs/{ASSESSMENT,ARCHITECTURE,USAGE,CI-AND-REQUIRED-CHECKS,CODERABBIT,ACCEPTANCE-TESTS,TROUBLESHOOTING,EXTENDING}.md`.

## Pending (needs external action or your decision)

- **GitHub:** pushed to https://github.com/MohamedRamadanSalah/Agentic-Workflow (public), and CI ran green.
  Still pending: no branch protection or ruleset makes `self-test`, `assurance-checks` and `workflow-lint` required,
  and enforcement has not been observed (it needs a deliberately failing PR). Steps are in `docs/CI-AND-REQUIRED-CHECKS.md`.
- **CodeRabbit:** the config file is present and schema-checked. The GitHub App is not installed and no
  reviews have been observed (`docs/CODERABBIT.md`).
- **Manual end-to-end run:** a real `/bug-assurance` session on BUG-101 with a human answering the questions
  has **not** been executed. The procedure is in `docs/ACCEPTANCE-TESTS.md`.
- **Commit:** the initial commit `5903d82` was pushed with your approval, authored by the GitHub noreply identity.

## Limitations (by design or by environment)

- Shell-command guarding is heuristic defense in depth, not a security boundary. Edit and Write tools are
  enforced precisely.
- Gates verify structure, cross-references and provenance. They cannot judge whether an agent's reasoning
  is wise. Reviewer quality depends on the model.
- There is no lint or type checker for the toolkit's own Python: ruff and mypy are not installed, and none
  was added as a dependency.
- The Stop and SessionStart hooks were verified by subprocess tests, not observed live.
- Prompt capture: every prompt typed in this repository is logged, redacted, to the gitignored
  `.bug-assurance/inbox.jsonl`. This is the provenance mechanism; turn it off with `guard.capture_prompts: false`
  (user answers then cannot be AUTHORIZED).
- CI pins `pytest==9.0.2` and `pyyaml==6.0.3`. The `assurance-checks` job skips `system-tests` because `self-test`
  already runs that suite.
- The ECC GateGuard hook coexists with this system. It added an extra confirmation step for every new file
  but caused no conflicts.
- Local evidence was preserved, not deleted: one live-check run (`.bug-assurance/runs/BA-20261012-222556-b97b`)
  and 10 inbox records, of which 9 are system notifications that are now ignored. Both are gitignored.

## Using it on a bug

```
/bug-assurance start <ticket text | issue URL | path to ticket file>
/bug-assurance resume        # after answering questions or in a new session
python .claude/skills/bug-assurance/scripts/ba.py status
```
