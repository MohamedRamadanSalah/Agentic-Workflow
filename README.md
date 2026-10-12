# Bug Assurance Engineering System for Claude Code

A reusable, evidence-gated bug-fixing workflow. A Claude Code **skill** (`/bug-assurance`) makes
Claude the Orchestrator of a run. A deterministic **controller** (`ba.py`, Python standard
library) owns the run's state machine and computes every gate from persisted artifacts.
**Hooks** enforce the most important rule: *no application-source change until the
clarification gate passes*.

Specialized **agents** investigate, implement and review independently. Findings are settled by
evidence, never by vote.

The priority is to understand the problem correctly before implementing. Ambiguous business
rules are turned into targeted questions. Answers are challenged and recorded with provenance,
and the run blocks until implementation-critical uncertainty is resolved. A plausible root cause
is not treated as a verified one, and a check that was not run never counts as a pass.

This system reduces preventable defects. It does not guarantee their absence.

## Quick start

```bash
python .claude/skills/bug-assurance/scripts/ba.py doctor     # validate installation
python -m pytest tests -q -p no:cacheprovider                 # test the system itself
```

In Claude Code, in this repository:

```
/bug-assurance start examples/fixture-app/tickets/BUG-101.md
/bug-assurance resume | verify | review | report | status
```

Install into another repository (dry-run first):

```bash
python .claude/skills/bug-assurance/scripts/ba.py install --target ../other-repo
python .claude/skills/bug-assurance/scripts/ba.py install --target ../other-repo --apply
```

## Layout

| Path | Purpose |
|---|---|
| `.claude/skills/bug-assurance/` | `SKILL.md` (Orchestrator protocol), reference docs, templates, `scripts/ba.py` controller |
| `.claude/agents/ba-*.md` | orchestrator (optional main-thread agent) + investigator, implementer, 5 reviewers, adjudicator |
| `.claude/settings.json` | project hooks (PreToolUse, UserPromptSubmit, SessionStart, Stop) and permission rules |
| `.bug-assurance/config.json` | repository paths, checks, reviewers, repair limits |
| `.bug-assurance/runs/<id>/` | per-run artifacts (gitignored) |
| `.github/workflows/` | PR checks (validated locally; not yet run on GitHub) |
| `.coderabbit.yaml` | CodeRabbit configuration (pending GitHub App installation) |
| `examples/fixture-app/` | demo application with a seeded bug (BUG-101) |
| `tests/` | unit tests + executable acceptance scenarios A–R |

## Documentation

- [Repository assessment](docs/ASSESSMENT.md)
- [Architecture, state machine, gates, enforcement boundaries](docs/ARCHITECTURE.md)
- [Usage: init, start, resume, verify-only, review-only, reports](docs/USAGE.md)
- [CI and required checks](docs/CI-AND-REQUIRED-CHECKS.md)
- [CodeRabbit](docs/CODERABBIT.md)
- [Acceptance tests (automated and manual)](docs/ACCEPTANCE-TESTS.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Extending: checks, agents, reviewers, paths](docs/EXTENDING.md)

## Requires human action

- Commits, pushes and PRs: the workflow never performs them, and the hook asks first.
- Publishing to GitHub, making checks required, and verifying enforcement (needs repo admin).
- Installing the CodeRabbit GitHub App.
- Answering business questions the workflow cannot settle from authoritative evidence.
