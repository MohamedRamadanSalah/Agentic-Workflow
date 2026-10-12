# Run artifacts (normative source: `scripts/bug_assurance/gates.py`, `ledger.py`, `findings.py`, `checks.py`)

Location: `.bug-assurance/runs/<run-id>/`. The active run id is in `.bug-assurance/active-run`.
Captured user messages are in `.bug-assurance/inbox.jsonl`. Runs and the inbox are gitignored
(they may contain user messages). Get any agent-authored template with `ba.py template <name>`.
Keys starting with `_` are ignored.

## Ownership

| Owner | Files | Rule |
|---|---|---|
| Agent-authored, script-validated | `ticket.json`, `understanding.json`, `investigation.md`, `decisions.json`, `clarification.json`, `contract.json`, `regression-plan.json`, `implementation.json`, `reviews/*.json`, `notes/`, `evidence/` | Write with Edit/Write; gates validate them. |
| Script-owned (hook denies direct edits) | `state.json`, `events.jsonl`, `baseline.json`, `answers.jsonl`, `findings.json`, `findings-log.jsonl`, `reviews-index.json`, `regression-run.json`, `diff-audit.json`, `verification/`, `gate-results.json`, `gate-history.jsonl`, `draft-report.md`, `final-report.md/json`; `.bug-assurance/inbox.jsonl`, `.bug-assurance/active-run` | Change only via `ba.py` commands. |

## ticket.json (gate `ticket`)
Required non-empty strings: `title`, `symptom`, `expected_behavior`, `actual_behavior`, `risk_rationale`.
`risk_level` ∈ low|medium|high|critical. `source` = `{kind, ref, accessed: bool}`; kinds
github_issue/url/jira with `accessed: false` produce a warning. Lists (if present):
`reproduction_steps`, `affected_components`, `preconditions`, `evidence`, `acceptance_criteria`,
`missing_information`, `potential_side_effects`. An `UNKNOWN` expected/actual behavior requires a
non-empty `missing_information`.

## understanding.json + investigation.md (gate `investigation`)
- `investigation.md` ≥ 200 characters: the traced call path, competing hypotheses, supporting and contradicting evidence.
- Required strings: `symptom`, `expected_behavior`, `actual_behavior`, `trigger`, `initial_state`.
- `behavior_model` = {`preconditions`, `action`, `expected_state_transition`, `expected_side_effects`}.
- Required lists: `actors`, `business_rules`, `side_effects`, `missing_information`, `implementation_changing_decisions`.
- `claims[]`: `id` C-n (unique), `classification` (7 classes), `statement`, `source`, `material` (bool), `evidence[]`.
  `AUTHORITATIVE_REQUIREMENT` claims used as evidence must carry `source_path` + `quote` (found
  verbatim in that repository file, whitespace/case-insensitive) or `answer_ref` (an AUTHORIZED answer).
- `hypotheses[]`: `id` H-n, `status` CANDIDATE|SUPPORTED|CONFIRMED|REFUTED, `supporting[]`/`contradicting[]`
  items `{kind, ref, note, explained?}`, `distinguishing_test` (required), `alternatives[]`, `remaining_uncertainty`.
- `root_cause` = {`status` CONFIRMED|PROBABLE|UNCONFIRMED, `hypothesis`, `diagnostic_limitation`}.

Root-cause rules (checked by the `clarification` gate):
- CONFIRMED requires the named hypothesis to be CONFIRMED, have at least one direct-evidence item
  (reproduction|test|trace|log|debugger|experiment; refs under `evidence/` must exist), explain every
  contradicting item, and every other hypothesis to be REFUTED.
- PROBABLE/UNCONFIRMED requires a `diagnostic_limitation`, is refused at high/critical risk, and is
  reported as a documented risk.
- All hypotheses REFUTED → fail (investigate new alternatives).

## decisions.json (gate `clarification`)
`{"questions": [...]}`. Each question has:
- `id` Q-n, `kind` business_rule|technical|environment, `material` bool, `topic`, `uncertainty`, `question`.
- `interpretations[]` {id, description, behavior_impact}: at least two when material.
- `decision_points[]` {id, description}: non-empty.
- `status` OPEN|ANSWERED|NEEDS_FOLLOW_UP|RESOLVED|SUPERSEDED (`superseded_by` required for SUPERSEDED).
- `duplicate_of` (must name a RESOLVED question), `addresses_claims[]`, `follow_ups[]` (must be resolved).
- `analysis`: `rule`, `hidden_assumptions[]`, `contradictions[]` {id, with, description, resolution, resolution_ref}, `counterexamples[]` {id, scenario, handled, ref, justification}.
- `resolution`: `kind`, `rule`, `decision_points` {DP-id: text}: every DP must be settled. Also `answer_refs[]`, `evidence_refs[]`, `supersedes[]`, `acceptance_criteria[]` (non-empty).

Resolution kinds:
- `AUTHORIZED_DECISION`: at least one referenced answer with authority AUTHORIZED whose provenance re-verifies.
- `VERIFIED_EVIDENCE`: cites claims classified VERIFIED_OBSERVATION / AUTHORITATIVE_REQUIREMENT /
  REPOSITORY_EVIDENCE. For a business rule at least one must be AUTHORITATIVE_REQUIREMENT, because code and
  tests alone cannot settle intended behavior.
- `SUPPORTED_INFERENCE`: never for a material business rule, never for material questions at
  high/critical risk, must cite claims, recorded as a risk.
- `supersedes` (overriding authoritative claims) is allowed only for AUTHORIZED_DECISION.

Contradiction resolutions: `UNRESOLVED` (fails the gate), `RESOLVED_BY_AUTHORIZED_DECISION` (resolution_ref must be an
AUTHORIZED answer, and an overridden AUTHORITATIVE_REQUIREMENT must be listed in `supersedes`), `RESOLVED_BY_EVIDENCE`
(resolution_ref required), `NOT_A_CONTRADICTION`.
Counterexample handling: `UNHANDLED` (fails), `COVERED_BY_RULE`, `ADDED_ACCEPTANCE_CRITERION` (ref = AC id that must
exist in the contract), `FOLLOW_UP` (ref = a RESOLVED question), `NOT_MATERIAL` (justification required). A material
business-rule question needs at least one counterexample.
Claims: a material CONTRADICTION must be referenced by a resolved contradiction or by `addresses_claims`; a material
UNKNOWN/UNVERIFIED_ASSUMPTION must be listed in a resolved question's `addresses_claims`.

## answers.jsonl (script-owned; `ba.py answer`)
`{id A-nnn, question, text, source_kind user|document|ticket|agent, source_name, on_behalf_of, inbox_ref, authority AUTHORIZED|UNVERIFIED, recorded_at}`.
AUTHORIZED only for `user` (quote must appear in the captured inbox message `inbox_ref`, whose sha256 must match) or
`document` (quote must appear in the named repository file). Provenance is re-verified on every gate evaluation, so a
tampered inbox breaks the decision. The question must already exist in decisions.json.

## inbox.jsonl (script-owned; UserPromptSubmit hook)
`{id M-<timestamp>-<hash6>, at, sha256, text (redacted, ≤ 8000 chars), session_id}`. Records whose hash does not match are ignored.
System-generated turns (`<task-notification>`, `<system-reminder>`, subagent hand-backs, slash-command output) are
never captured, and any such record already present is ignored (`ledger.is_system_generated`): model or harness
output can never become a user answer. Capture can be disabled with `guard.capture_prompts: false`.

## clarification.json
`{"dimensions": {...}}` with all 10 keys: expected_behavior, actual_behavior, business_rules,
preconditions_and_states, ambiguities, root_cause, acceptance_criteria, negative_and_boundary_scenarios,
regression_risks, verification_strategy. Each is `{status, refs[], justification}`. UNRESOLVED fails. N/A needs a
justification ≥ 15 chars. Other statuses need refs; Q- refs must be RESOLVED and C- refs must exist. AUTHORIZED_DECISION
must cite an authorized decision. SUPPORTED_INFERENCE on business dimensions is refused at high/critical risk. root_cause
VERIFIED requires a CONFIRMED root cause.

## contract.json (gate `contract`)
`requirements[]` {id R-n, rule, source, authority, expected_behavior, acceptance_criteria[] (must exist), decision_refs[],
verification {test | impractical_reason}}. `scenarios[]` {id AC-n, kind positive|negative|boundary, given, when, then,
requirement_ids[] (non-empty, must exist)}. Required lists: `excluded_behavior`, `invariants`, `regression_risks`,
`required_checks` (ids defined in config). `accepted_assumptions[]` {statement, why_non_material} become risks.
`open_conflicts` must be empty. A negative/boundary scenario is needed unless `no_negative_scenarios_justification` is
given. All AC ids from resolved material decisions and ADDED_ACCEPTANCE_CRITERION counterexamples must exist.

## regression-plan.json / regression-run.json (gate `regression`)
Plan: `feasible` bool. For feasible plans, `tests[]` {id, argv[], cwd, timeout_s, acceptance_criteria[] (must exist),
expected_failure_exit_codes (default [1])}. For infeasible plans, `infeasible_reason` and `alternative_strategy` (≥ 30 chars each).
Run (`ba.py regression-run`) is refused unless the application fingerprint equals the baseline (original code).
It records `{app_fingerprint, plan_digest, contract_digest, results[{id, exit_code, outcome FAILED_AS_EXPECTED|PASSED_UNEXPECTEDLY|ERROR|BLOCKED}]}`.
The gate requires every planned test to be FAILED_AS_EXPECTED and the plan unchanged since the run. A changed contract
only warns and is recorded as a risk.

## implementation.json + diff-audit.json (gate `implementation`)
Implementation: `summary`, `files_changed[]`, `decisions[]`, `limitations[]`, `acknowledged_issues[]` {code, path, justification}.
The diff audit (`ba.py diff-audit`) compares the tree with `baseline.json`. Issue codes and severities:
UNRELATED_CHANGE (BLOCKING; neither application nor tests), SECRET (BLOCKING; never acknowledgeable),
DELETED_FILE (BLOCKING), PREEXISTING_USER_CHANGE_TOUCHED (WARNING; the file was dirty versus HEAD at run start),
TEMP_FILE (WARNING), DEBUG_CODE (WARNING; debug-marker count increased), NO_APPLICATION_CHANGE (INFO).
The audit must be fresh (same fingerprint). `files_changed` must include every changed file.

## verification/NNN.json (script-owned; gates `verification`, `final-verification`)
`{seq, at, final, fingerprint, fingerprint_changed_during_run, summary, required_failures, results[{id, kind, required,
argv, status PASS|FAIL|BLOCKED|NOT_RUN|NOT_APPLICABLE_WITH_JUSTIFICATION, exit_code, duration_s, detail, stdout_tail,
stderr_tail, log}]}`. Full redacted logs are in `verification/logs/`. A fingerprint that changes during the run is
recorded as `INCONSISTENT` (unusable). Required checks = config `required` + contract `required_checks` + `regression:<id>`.

## reviews/<reviewer>.json → reviews-index.json + findings.json (gates `reviews`, `adjudication`)
Review: `{reviewer, status COMPLETED|NOT_APPLICABLE, rationale, scope_inspected[] (required if COMPLETED), findings[]}`.
Each finding needs `severity` (CRITICAL|HIGH|MEDIUM|LOW|INFORMATIONAL), `category`, `summary`, `behavior`,
`failure_scenario`, `requirement_violated`, `evidence`, `recommendation`, `confidence_rationale`, and optionally `file` and `line` (int).
Ingestion assigns ids F-nnn, stamps the fingerprint, and logs `ingested` in `findings-log.jsonl`. Findings are never removed.
Adjudication (`ba.py adjudicate`): REJECTED_WITH_EVIDENCE needs evidence; DUPLICATE needs `--duplicate-of`;
NEEDS_CLARIFICATION needs an unresolved `--question`. Rationales mentioning votes, a majority or consensus are refused.
Evidence refs are `verification:<seq>`, `decision:<Q-id resolved>`, `code:<path>[:line]`, `test:<path>[::name]`,
`reproduction:<run-relative file>` and `regression:<test-id>`, and each is checked to exist.
Resolution (`ba.py finding-resolve`): FIXED needs verification/test/regression evidence. ACCEPTED_RISK needs `--authorization` (an AUTHORIZED answer).
Blocking = CONFIRMED and severity CRITICAL/HIGH/MEDIUM and not FIXED/ACCEPTED_RISK.

## state.json, baseline.json, events, reports (script-owned)
`state.json`: {run_id, title, state, history[{from,to,at,reason,warnings,risks}], repair{cycles,max_cycles,hard_limit}, resume_to, outcome}.
`baseline.json`: file hashes at run start, git HEAD, git-dirty paths, fingerprints, debug-marker counts.
`final-report.md/json`: written on FINAL_REPORT. `draft-report.md`: written by `ba.py report`.
