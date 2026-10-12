# Troubleshooting

## Exit codes

| Code | Meaning | Typical action |
|---|---|---|
| 0 | success | none |
| 1 | unexpected internal error (or unknown hook event) | read stderr; file a bug with the traceback |
| 2 | usage or validation error (bad flags, missing artifact, refused answer/adjudication, missing `--reason`) | fix the input named in the message |
| 3 | gate refused (`transition`, `gate`, `regression-run`, `validate-workflows` problems) | read the listed problems; produce the missing evidence |
| 4 | `verify` finished with a required check not PASS, or files changed during the run (`INCONSISTENT`) | fix the failure; re-run without editing files concurrently |

## A transition is refused

```bash
python .claude/skills/bug-assurance/scripts/ba.py status        # every transition + its problems
python .claude/skills/bug-assurance/scripts/ba.py gate clarification
```

Each problem names the artifact and field. Common causes:
- `Q-n (OPEN) is material and unresolved`: ask the stakeholder, record the answer with
  `ba.py answer`, complete `analysis`, then mark the question RESOLVED.
- `business rule cannot be settled by existing code/tests alone`: cite an
  AUTHORITATIVE_REQUIREMENT (verbatim quote from a repository document) or get an authorized
  decision.
- `quote not found verbatim`: the claim's `quote` must match the file text (whitespace and case
  are normalized).
- `regression test ... PASSED on the original code`: the test does not detect the defect, or the
  bug model is wrong. Revisit the test or the requirement. Never just change the expectation.

## Stale evidence

`all recorded verification results are stale`, `diff-audit.json is stale` or `review is stale`
means files changed after that evidence was produced. Re-run `ba.py diff-audit` / `ba.py verify`
and re-review the changed behavior. Caches such as `__pycache__`, `.pytest_cache`, `build/` and
`node_modules/` are excluded from the fingerprint. If another tool writes files, add them to
`fingerprint.exclude` in `.bug-assurance/config.json`, but only for generated, non-source files.

## The guard denied an edit

The denial reason names the run, its state and the failing condition. This is working as
intended. Advance the run through its gates, or return to CLARIFICATION. If a run was abandoned
and you need to edit freely:

```bash
python .claude/skills/bug-assurance/scripts/ba.py deactivate --reason "abandoned: <why>"
```

This clears only the active-run pointer. The run is preserved, and you can resume it with
`ba.py activate <id>`. To turn enforcement off for the whole repository, set
`"guard": {"enabled": false}` in `.bug-assurance/config.json`. This is a deliberate,
user-approved change; the hook asks before that file is edited during a run.

## Hooks do not fire

- Run `/hooks` in Claude Code and confirm the four `ba.py hook ...` entries from
  `.claude/settings.json`. Project hooks may need a session restart or approval after creation.
- On Windows, hook commands run in **Git Bash**. `python` must be on its PATH (`which python` in
  Git Bash). On Linux/macOS, use `python3` (install with `--interpreter python3`).
- Test a hook by hand:
  ```bash
  echo '{"cwd": ".", "tool_name": "Edit", "tool_input": {"file_path": "src/x.py"}}' \
    | python .claude/skills/bug-assurance/scripts/ba.py hook pre-tool-use
  ```
  No output means no decision (allowed). A denial prints JSON with `permissionDecision`.
- `ba.py doctor` reports missing hook wiring, agents or skill files.

## Interaction with ECC GateGuard

The user-level ECC GateGuard hook may ask Claude to state facts before the first Bash command or
the first creation of each file. This is independent of bug-assurance. Its denials are not
bug-assurance denials. Disabling it is the user's choice (`ECC_GATEGUARD=off`, or
`ECC_DISABLED_HOOKS` in user settings); bug-assurance never changes it.

## Windows notes

- Checks run without a shell. `.bat`/`.cmd` tools such as `flutter`, `dart` and `npm` are
  resolved via `PATH` (`shutil.which`) and executed directly.
- Use `{python}` in check `argv` to mean the interpreter running `ba.py`.
- Paths in artifacts are POSIX-style relative paths (`src/orders/cancellation.py`).

## Malformed or inconsistent artifacts

Gates report malformed JSON with file and position. The JSONL logs (`events.jsonl`,
`answers.jsonl`, `findings-log.jsonl`, `inbox.jsonl`) are append-only. If a finding logged as
ingested is missing from `findings.json`, the adjudication gate fails with `findings disappeared`.
Treat that as tampering or corruption, and do not finalize.
