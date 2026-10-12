# CodeRabbit integration

## Assessment

CodeRabbit is an AI pull-request reviewer delivered as a GitHub App. It is a useful
**supplementary** layer: another set of eyes on every PR, with path-specific instructions. It
**does not replace** the Clarification Gate, the Business Logic Reviewer, the failing-before-fix
regression test, deterministic verification or adjudication. Its comments carry no special
authority. Like any reviewer's output, they are findings to be adjudicated with evidence.

Suitability for this repository: appropriate once the repository is on GitHub. Nothing in this
system depends on it, so its absence never blocks a bug-assurance run.

## Status

| Item | Status |
|---|---|
| `.coderabbit.yaml` in repository root | see `ba.py github-status` → `coderabbit.config_file` / `config_valid_yaml` |
| GitHub repository / remote | **PENDING**: none (the user chose local-only) |
| CodeRabbit GitHub App installed with access to the repo | **PENDING**: requires an org/repo admin |
| Plan / subscription (free tier for public repos, paid for private) | **PENDING**: user decision |
| Reviews observed on PRs | **UNKNOWN**: `ba.py github-status` looks for reviews by `coderabbitai` on recent PRs |

Do not treat CodeRabbit as active until a PR shows a review from it.

## What the configuration does

`.coderabbit.yaml` (owned by the integration workstream; check the file for the exact keys)
configures automatic reviews on PRs and adds path-specific instructions that point CodeRabbit at
the same concerns as the `ba-*` reviewers: business rules and state transitions in application
code, tests that can pass while the bug remains, and changes to the controller or hooks. The
key names follow CodeRabbit's published schema
(`https://coderabbit.ai/integrations/schema.v2.json`). Re-check them against the current
CodeRabbit documentation before relying on them, because the schema evolves.

## Setup steps (external)

1. Push the repository to GitHub (see [CI-AND-REQUIRED-CHECKS.md](CI-AND-REQUIRED-CHECKS.md)).
2. Install the CodeRabbit GitHub App from https://github.com/apps/coderabbitai and grant it
   access to this repository only (least privilege).
3. Choose a plan in the CodeRabbit dashboard.
4. Open a PR and confirm a CodeRabbit review appears. Then run `ba.py github-status`; it should
   report `observed_reviews: YES`.
5. Optional: do **not** make CodeRabbit a required status check unless the team accepts the
   dependency on an external service. Required checks should stay deterministic.

## Importing CodeRabbit comments into a run

Treat CodeRabbit comments as findings from an optional reviewer named `coderabbit`. Write
`.bug-assurance/runs/<id>/reviews/coderabbit.json`:

```json
{
  "reviewer": "coderabbit",
  "status": "COMPLETED",
  "rationale": "imported from PR #12 review",
  "scope_inspected": ["PR #12 review comments"],
  "findings": [
    {
      "severity": "MEDIUM",
      "category": "business-logic",
      "file": "src/orders/cancellation.py",
      "line": 34,
      "summary": "...",
      "behavior": "...",
      "failure_scenario": "...",
      "requirement_violated": "R-1",
      "evidence": "CodeRabbit comment <url>",
      "recommendation": "...",
      "confidence_rationale": "third-party automated review; not independently verified yet"
    }
  ]
}
```

Then ingest it with `python .claude/skills/bug-assurance/scripts/ba.py review-record <file>`, and
adjudicate each finding (`ba.py adjudicate F-… --disposition … --rationale … --evidence …`) like
any other. Severity must reflect impact and likelihood, not the tool's label.
