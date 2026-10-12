# CI and required checks

## What exists

The workflow file under `.github/workflows/` runs the system's own tests and the repository
checks configured in `.bug-assurance/config.json` (via `ba.py verify --standalone`) on pull
requests and pushes to `main`. It uploads the verification JSON as an artifact.
`.github/workflows/bug-assurance.yml` uses `permissions: contents: read`, a concurrency group with
cancel-in-progress, per-job `timeout-minutes`, `persist-credentials: false`, no secrets, no
`pull_request_target`, and actions pinned to commit SHAs (checkout v4.4.0 `11d5960a…`,
setup-python v5.6.0 `a26af69b…`, upload-artifact v4.6.2 `ea165f8d…`). Its three jobs, which are
the status-check contexts to mark as required, are:

| Job | What it runs |
|---|---|
| `self-test` | `python -m pytest tests -q -p no:cacheprovider` (the system's own test suite) |
| `assurance-checks` | `ba.py verify --standalone --only fixture-tests,compile,validate-workflows --out ba-verify.json`, artifact upload (`system-tests` is omitted because `self-test` already runs it) |
| `workflow-lint` | `ba.py validate-workflows` |

Validate locally (PyYAML is required for the syntax check):

```bash
python .claude/skills/bug-assurance/scripts/ba.py validate-workflows
python .claude/skills/bug-assurance/scripts/ba.py github-status
```

`validate-workflows` checks YAML syntax, triggers, top-level `permissions`, `runs-on`, job
timeouts, SHA-pinned actions, `pull_request_target` usage, and untrusted
`${{ github.event.* }}` text interpolated into shell steps. It exits 0 when valid, 3 on problems,
and 2 if no workflow exists. It is not a full actionlint replacement. Install `actionlint` for
deeper checks.

## The six states, reported separately

| # | State | How it is established | Current status |
|---|---|---|---|
| 1 | Workflow file created | file exists in `.github/workflows/` | YES |
| 2 | Syntax validated | `ba.py validate-workflows` (PyYAML) | verify by running it |
| 3 | Workflow executed successfully | a GitHub Actions run concluded `success` | **UNKNOWN**: no GitHub remote |
| 4 | Check reported by GitHub | the run appears on a commit/PR | **UNKNOWN**: no GitHub remote |
| 5 | Check configured as required | branch protection / ruleset lists the context | **UNKNOWN**: no GitHub remote |
| 6 | Required-check enforcement verified | a PR with a failing check is observed blocked from merging | **NO** |

`ba.py github-status` reports these states from real `git`/`gh` read-only calls. Anything it
cannot observe is `UNKNOWN`. A workflow file existing never implies the check is required.

## Steps to complete (require the user / a repository admin)

1. Create the repository and push. This is an outward-facing action, so do it only when you
   intend to publish:
   ```bash
   gh repo create <owner>/<name> --private --source . --remote origin
   git add -A && git commit -m "Add bug assurance system"   # only when you decide to commit
   git push -u origin main
   ```
2. Confirm the workflow ran: `gh run list --limit 5`, then `ba.py github-status` (states 3 and 4).
3. Mark the jobs as required. Use the job names exactly as they appear in the PR checks list.
   - UI: *Settings → Branches → Add branch protection rule* (or *Settings → Rules → Rulesets*) →
     target `main` → *Require status checks to pass* → add the job names → *Require branches to
     be up to date*.
   - CLI, classic branch protection (admin rights required):
     ```bash
     gh api -X PUT repos/<owner>/<name>/branches/main/protection \
       -H "Accept: application/vnd.github+json" --input protection.json
     ```
     with `protection.json`:
     ```json
     {
       "required_status_checks": {"strict": true, "contexts": ["self-test", "assurance-checks", "workflow-lint"]},
       "enforce_admins": true,
       "required_pull_request_reviews": null,
       "restrictions": null
     }
     ```
   - CLI, ruleset: `gh api -X POST repos/<owner>/<name>/rulesets --input ruleset.json`, where
     `ruleset.json` targets `refs/heads/main` and contains a `required_status_checks` rule listing
     the contexts.
4. Re-run `ba.py github-status`. State 5 should now be `YES`. It reads both
   `branches/<b>/protection/required_status_checks` and `rules/branches/<b>`.
5. **Verify enforcement** (state 6). Open a throwaway PR that makes a required check fail (for
   example a test asserting `False`). Confirm that GitHub shows the PR as blocked and the merge
   button disabled. Close the PR without merging. Record the PR URL as evidence. `ba.py` never
   creates PRs itself, so this step is always manual.

Never weaken or remove a required check to get a green PR. Fix the cause.

## Cost and reliability notes

- The checks are pure Python with no services, so runs should be short on `ubuntu-latest`.
  Measure this after the first real run.
- Use `pull_request` (not `pull_request_target`) and no secrets, so untrusted PR code never sees
  privileged credentials. `validate-workflows` warns if `pull_request_target` appears.
