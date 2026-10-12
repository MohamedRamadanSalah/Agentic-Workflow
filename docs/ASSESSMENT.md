# Repository assessment (Phase 1: discovery)

Recorded 2026-10-12, before any file was created. Read-only discovery only.

## Environment found

| Item | Finding |
|---|---|
| Target directory | `C:\Users\dell\Documents\testing`: **empty and not a Git repository** |
| Other projects in `Documents/` | None of them is a Git repository (`roamly_flutter` is a Flutter app, `project2` is a .NET solution) |
| Claude Code | 2.1.296 |
| Python | 3.13.2, with pytest 9.0.2 and PyYAML 6.0.3 installed (`jsonschema` is not installed) |
| Node / npm | 22.17.1 |
| Flutter / Dart | Flutter 3.41.6 (stable) |
| .NET SDKs | 8.0.417, 10.0.302 |
| Git | Git for Windows (Git Bash available, so hooks run under Git Bash) |
| GitHub CLI | gh 2.102.0. Two accounts logged in (`mramadan-cell` active); token scopes include `repo` and `workflow` |
| Linters | `ruff`, `actionlint` and `yamllint` are **not installed** |
| Claude Code config | User-level ECC plugin (v2.0.0) with many hooks, including **GateGuard** (fact-forcing PreToolUse gate on first Bash use and on first creation of each file). Figma plugin. User skills: `pr-learning`, `stitch-pro`, `landing-page-designer`. No user-level agents |
| Project config | None. There was no `CLAUDE.md`, `.claude/`, workflows, PR template, CodeRabbit config, branch protection or security scanners |

## Decisions taken with the user

1. **Target:** build a standalone, reusable toolkit in `testing/`, with a small Python fixture
   application (`examples/fixture-app/`) so the repository-specific checks and acceptance tests
   have real code to run against. `ba.py install` can install it into other repositories.
2. **Git:** `git init` locally is allowed. **No commits** are made without explicit approval.
3. **GitHub:** stay local with **no remote**. Workflow files are created and validated locally.
   Execution, required checks and CodeRabbit are reported as **PENDING / UNKNOWN**.

Decided without asking (routine and reversible):
- Python 3 standard library for all scripts, so there are no new dependencies.
- Artifacts go under `.bug-assurance/runs/<id>/`, which is gitignored.

## Existing vs. missing capabilities

| Capability | Existing | Action |
|---|---|---|
| Bug-fix workflow with clarification gate | none | implemented (`bug-assurance` skill + `ba.py` state machine) |
| Specialized reviewer agents with finding schema | none in project (ECC offers generic reviewers) | implemented `ba-*` agents |
| Deterministic verification runner | none | implemented `ba.py verify` |
| Hooks enforcing workflow | ECC hooks (generic, user level) | added project hooks; they coexist with ECC |
| CI | none | `.github/workflows/` workflow (local validation only) |
| Required checks / branch protection | not possible (no remote) | guidance + `ba.py github-status` |
| CodeRabbit | none | `.coderabbit.yaml` + setup guide (PENDING) |
| Tests of the system | none | `tests/` (unit + acceptance A–R) |

## Reuse assessment: ECC plugin components

| ECC component | Decision | Rationale |
|---|---|---|
| `ecc:orch-fix-defect` | not reused as the workflow | It has no clarification gate, no answer provenance, no evidence hierarchy and no artifact-backed transitions. It can still be used ad hoc outside a run |
| `ecc:code-reviewer`, `ecc:security-reviewer` | not reused as gate reviewers | Their output lacks the required finding schema (ID, severity, failure scenario, requirement violated, evidence) and gives no fingerprint binding. They are fine for extra, non-gating review |
| `ecc:pr-test-analyzer` | not reused | Same reason. `ba-test-quality-reviewer` must check that the regression test detects *this* defect against the contract |
| ECC GateGuard hook | coexists | It is a user-level hook outside this project's control. It adds a fact prompt before file creation but does not interfere with `ba.py` |

## External actions still required (cannot be done from this environment)

| Action | Who | Status |
|---|---|---|
| Create a GitHub repository and push (the user chose to stay local) | user | PENDING |
| Observe the workflow run on GitHub | user/CI | PENDING |
| Mark the CI jobs as required status checks (branch protection or ruleset; needs admin) | repo admin | PENDING |
| Verify enforcement with a deliberately failing PR | repo admin | PENDING |
| Install the CodeRabbit GitHub App, choose a plan and grant repo access | org/repo admin | PENDING |
| Optional: install `ruff` / `actionlint` for stronger lint and workflow checks | user | OPTIONAL |
