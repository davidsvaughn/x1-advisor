# docs/archive/ — absorbed point-in-time documents

Documents whose conclusions have been **fully absorbed** into the living docs
(`PLAN.md` §R, `DECISIONS.md`, and the designs they reviewed). They are kept
verbatim as the evidence/audit trail — nothing here is current truth.

| Document | What it was | Absorbed into |
|---|---|---|
| `HANDOFF.md` | Project handoff snapshot (2026-07-07) | `PLAN.md` §R; read order now in `AGENTS.md` |
| `DESIGN-REVIEW-2026-07-30.md` | Self-audit, input to the second-agent review | `ARCHITECTURE-PLAN-REVIEW` → PLAN §R Step 0 |
| `ARCHITECTURE-PLAN-REVIEW-2026-07-30.md` | Independent second-agent review; defined Gates 1–6 | `PLAN.md` §R gate sequence |
| `QA-BANK-CONTEXT-REVIEW-2026-07-30.md` | Review of QA-loop / question-bank / context-snapshot proposals | Its §7 revisions folded into all three docs + QUESTION-BANK |
| `chats/` | June-2026 kickoff transcripts | Framing provenance for `ARCHITECTURE.md` |

**Convention:** when a dated point-in-time doc is fully absorbed and no longer
consulted for current decisions, it moves here (git mv, links fixed) and its
row in the `AGENTS.md` taxonomy is updated. Living docs and
still-consulted rationale (`ARCHITECTURE.md`, `ARCHITECTURE-REVIEW.md`,
`QA-LOOP-DESIGN`, `refs/`) stay at the top level.
