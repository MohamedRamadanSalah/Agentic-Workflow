# Questioning: Ask → Interpret → Challenge → Verify → Follow Up → Record → Gate

Questions are an engineering control, not a preliminary courtesy. The goal is to eliminate
implementation-critical uncertainty. Stop when the gate's evidence is complete, not after a
fixed number of questions. Never ask just to ask, and never stop questioning merely because
someone answered once.

## 1. Before asking: is it already known?

1. Search for authoritative evidence: approved policy, specifications, API contracts, ADRs, and
   tickets the user provided. If a source settles the rule, record it as an
   `AUTHORITATIVE_REQUIREMENT` claim with `source_path` + verbatim `quote` and resolve the question as
   `VERIFIED_EVIDENCE`. **Do not ask.**
2. Existing code and tests are `REPOSITORY_EVIDENCE`. They show what the system does, not what it
   should do, and they may encode the bug.
3. Run `ba.py questions`. Anything under `already_known` duplicates a resolved topic, so set
   `duplicate_of`.

## 2. Designing a high-value question

For each material uncertainty, record in `decisions.json`:

| Field | Content |
|---|---|
| `uncertainty` | What exactly is unknown |
| `interpretations` | ≥ 2 plausible readings, each with its `behavior_impact` |
| `affected` | Code paths and business rules that change with the answer |
| `resolving_evidence` | What could settle it without asking |
| `decision_points` | The separate facts an answer must settle (one DP per independent decision) |
| `question` | A specific question naming the alternatives |

Ask with AskUserQuestion, giving concrete alternatives built from the interpretations, each with
a one-line consequence, so the user can still answer "Other". Use small batches with the highest
correctness impact first.

Avoid: "Can you explain more?", "What should happen?", "Is this correct?", "Should I fix it?"

Prefer: "When a customer's order is PACKED but not SHIPPED, should cancellation be (a) allowed,
as approved policy rule 1 suggests, or (b) allowed only before the courier label is printed, as
the on-call developer suggested? (b) needs a label-state check that does not exist today."

## 3. Recording the answer (provenance)

- The UserPromptSubmit hook captures the reply as `M-…`, shown by `ba.py inbox`.
- `ba.py answer Q-1 --source-kind user --from-message M-… --quote "<exact excerpt>"`
  (`--on-behalf-of "Product owner"` when relayed). A quote that is not in the message is refused.
- Document answers: `--source-kind document --source-name docs/x.md --text "<verbatim>"`.
- Agent statements are recorded UNVERIFIED and can never authorize a business rule.

## 4. Adversarial verification of every consequential answer

1. **Restate** it as a precise, testable rule (`analysis.rule`).
2. **Coverage**: does it settle every decision point? If not, set `NEEDS_FOLLOW_UP` and ask about
   the missing point only.
3. **Hidden assumptions**: what must be true for the rule to work (status order fixed, single
   currency, UTC timestamps…)?
4. **Compare** with authoritative requirements and repository evidence. Record each conflict in
   `analysis.contradictions`.
5. **Counterexamples**: realistic scenarios that could expose an incomplete rule. Choose only
   relevant ones: null/empty, boundaries, repeated or duplicate requests, concurrency, read/write
   races, unauthorized actors, invalid transitions, partial failures and retries, stale caches,
   dates and time zones, legacy data, backward compatibility, UI/API/DB disagreement.
6. **Authority**: is this person authorized to decide this rule? Is the answer internally consistent?
7. **Decide** for each counterexample: COVERED_BY_RULE, ADDED_ACCEPTANCE_CRITERION (AC id),
   FOLLOW_UP (new question), or NOT_MATERIAL (justify).
8. **Record** the resolution (kind, rule, decision points, refs, AC ids) and mark it RESOLVED only when the
   evidence and the required authority support it.
9. **Do not re-litigate.** A clear, authorized answer with no remaining material contradiction is final.

## 5. Contradictions: challenge without inventing policy

Explain the conflict concretely: "Your answer says customers can never cancel PACKED orders, but
the approved policy `docs/cancellation-policy.md` rule 1 says until shipped. Which governs, and is
the policy being changed?" Possible outcomes:
- The stakeholder confirms the policy, so resolve as VERIFIED_EVIDENCE and mark the contradiction RESOLVED_BY_EVIDENCE.
- The stakeholder authorizes an override, so resolve as AUTHORIZED_DECISION with `supersedes: ["C-1"]` and
  mark the contradiction RESOLVED_BY_AUTHORIZED_DECISION citing the answer id. Record its implications in the contract.
- The answer is still ambiguous or unauthorized, so the run stays blocked (`BLOCKED_AWAITING_CLARIFICATION`).

## 6. Worked example: fixture BUG-101 (`examples/fixture-app`)

Ticket: customers cannot cancel PACKED orders. The on-call developer adds an opinion: "maybe only
before the courier label is printed?"

- C-1 AUTHORITATIVE_REQUIREMENT (quoted from `docs/cancellation-policy.md`): "A customer may cancel their own
  order until it has been shipped."
- C-2 REPOSITORY_EVIDENCE: `can_cancel` compares customer orders against `PACKED`.
- C-3 CONTRADICTION: `tests/test_cancellation.py::test_customer_cannot_cancel_packed_order` asserts the bug.
- C-4 UNVERIFIED_ASSUMPTION: the courier-label condition (developer opinion, no authority).

Q-1 (business_rule, material): DP1 "Is PACKED cancellable by the owning customer?", DP2 "Does a
courier-label condition apply?"
- C-1 settles DP1 and, by not mentioning any label condition, DP2. The question is resolved as
  VERIFIED_EVIDENCE without asking, and `addresses_claims: ["C-4"]` records why the developer
  opinion is rejected.
- C-3 is resolved by evidence (C-1): the existing test encodes the bug and must be updated, which
  is justified, not "weakened".
- Counterexamples: a SHIPPED order gets AC-2 (boundary, rejected); another customer's PACKED order
  gets AC-3 (negative, rejected).

Variants exercised by the acceptance tests:
- **Incomplete answer (C)**: an answer settling only DP1 leaves DP2 open, so the gate demands a focused follow-up on DP2.
- **Contradictory answer (D)**: "customers can never cancel packed orders" conflicts with C-1, so the contradiction is exposed and the gate stays closed.
- **Authorized override (M)**: a captured product-owner decision adding the label condition, recorded with `supersedes: ["C-1"]`, passes and is auditable.
- **Missing boundary (F)**: an unhandled SHIPPED counterexample blocks until an acceptance criterion exists in the contract.
