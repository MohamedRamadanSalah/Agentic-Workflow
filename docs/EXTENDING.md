# Extending and maintaining

All repository-specific behavior lives in `.bug-assurance/config.json`. `ba.py doctor` and
every command validate it. Invalid configuration is an error, never silently ignored.

## Add a repository check

```json
{
  "id": "dotnet-test",
  "description": "unit tests",
  "kind": "test",
  "required": true,
  "timeout_s": 1800,
  "argv": ["dotnet", "test", "--nologo"],
  "cwd": ".",
  "success_exit_codes": [0],
  "env": {"DOTNET_CLI_TELEMETRY_OPTOUT": "1"}
}
```

Rules enforced by `validate_config`:
- `id` matches `[a-z0-9][a-z0-9._:-]*` and is unique.
- `argv` is a list (no shell strings, no interpolation).
- `kind` is one of `format, lint, static, build, test, regression, security, coverage, other`.
- `0 < timeout_s <= 7200`.
- A check that does not apply uses `"not_applicable_reason": "..."` instead of `argv`, and cannot
  be `required`.

`ba.py discover` proposes checks for Flutter/Dart, .NET, Node (npm scripts), Python, Go and Rust.
It proposes only checks whose executables exist, and lists the missing ones. Try a new check with
`ba.py verify --standalone --only <id>` before relying on it. A contract can also require checks
for one run through `contract.required_checks`.

## Change which paths are protected

`paths.application` (frozen until IMPLEMENTATION and a passing clarification gate),
`paths.tests` (writable from REGRESSION_TEST) and `paths.unrestricted` (never restricted) are
glob lists, with `**` supported. Tests are matched before application paths. Anything matching
none of them is classified `other`. The guard treats `other` like application code, and the diff
audit flags it as `UNRELATED_CHANGE`. Include build manifests (for example `pubspec.yaml`,
`*.csproj`) in `application`.

## Repair limits

`"repair": {"max_cycles": 3, "hard_limit": 10}`. Constraint: `1 <= max_cycles <= hard_limit <= 10`.
The limit is copied into the run at `start`, so editing the config does not affect a running run.
Raising it mid-run requires `ba.py set-repair-limit N --authorization A-xxx` with an AUTHORIZED
answer.

## Reviewers

- `required_reviewers` must always contain `business-logic`, `regression` and `test-quality`.
  Config validation rejects removing them.
- `conditional_reviewers` (default `security`) must report COMPLETED or NOT_APPLICABLE with a
  rationale.
- `optional_reviewers` (default `architecture`, `coderabbit`) are ingested when present.

To add a new reviewer kind:
1. Add its name to `REVIEWERS` in `scripts/bug_assurance/core.py`.
2. Create `.claude/agents/ba-<name>-reviewer.md` with read-only tools (Read, Grep, Glob, Bash,
   Write for the review file only) and the review JSON output contract (`ba.py template review`).
3. Add it to the appropriate reviewer list in config, and to `REVIEW_ONLY_AGENTS` in
   `scripts/bug_assurance/guard.py` so the hook keeps it read-only.
4. Mention it in the INDEPENDENT_REVIEW section of `SKILL.md`.
5. Add a test in `tests/` showing a finding from it is ingested and must be adjudicated.

## Modify an agent

Agents are Markdown files with YAML frontmatter in `.claude/agents/` (`name`, `description`,
`tools`, `disallowedTools`, `model`, …). Keep responsibilities distinct and do not give reviewers
editing tools. Each agent's output contract must still satisfy the validators in `gates.py` and
`findings.py`. Changing a contract means changing the validator and its tests together.

## Change a gate or the state machine

- Transitions and the gates evaluated on entry: `STATES` in `scripts/bug_assurance/machine.py`.
- Gate logic: `scripts/bug_assurance/gates.py`.
- Every change needs a test. Add or extend an acceptance scenario in `tests/test_acceptance.py`
  and a unit test. Run `python -m pytest tests -q -p no:cacheprovider`.
- Never add a bypass flag that skips a gate. If a legitimate case is blocked, model it explicitly
  (for example an `infeasible_reason` with a recorded risk), so the report shows it.

## Upgrading an installed copy

`ba.py install --target <repo>` reports differing files as conflicts and never overwrites them.
Review the diff and copy deliberately. Your local config is never replaced, because `init` writes
`config.proposed.json` when a config already exists.
