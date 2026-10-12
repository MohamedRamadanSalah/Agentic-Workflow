# Project instructions

This repository is the **Bug Assurance** toolkit for Claude Code plus a demo target
(`examples/fixture-app/`).

- Fix bugs with `/bug-assurance` (skill in `.claude/skills/bug-assurance/`). The controller
  `python .claude/skills/bug-assurance/scripts/ba.py` is the only authority for run state; never
  edit script-owned run artifacts (`state.json`, `answers.jsonl`, `findings.json`,
  `verification/`, ...). The PreToolUse hook enforces this.
- No application-source change before the clarification gate passes (`ba.py gate clarification`).
- Never commit, push, open a PR, or change repository settings without explicit user approval.
- Never weaken gates, tests or required checks to make a run pass.
- Tests: `python -m pytest tests -q -p no:cacheprovider`. Health check: `ba.py doctor`.
- Repository checks are defined in `.bug-assurance/config.json`.
