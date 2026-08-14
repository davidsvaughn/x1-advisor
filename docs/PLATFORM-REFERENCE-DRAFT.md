# X1 Platform Reference

> **DRAFT for David's edit** (2026-08-14, triage thread-021 issue 1). This
> document becomes a *citable corpus document*: once the content is approved
> it is ingested (source_type `platform_reference`, visibility `x1`) so the
> advisor can retrieve and cite it when users ask what evaluations are, what
> they cover, or how the platform's data fits together. This file stays in
> git as the source of truth; re-ingest picks up edits via content-hash
> versioning. Everything below was drafted from code, schema, and registry
> evidence — items marked **[DAVID]** need your confirmation or content;
> please also delete anything that shouldn't be user-visible.

## What X1 is

X1 is a startup/investor platform connecting startup companies, investors,
and talent (CVs). Each has a profile; startups additionally carry uploaded
documents (pitch decks and other files), links, team members, monthly
metrics, and X1 evaluations. **[DAVID: one-paragraph product framing in
your words — this is the paragraph users will most often be quoted.]**

## X1 evaluations

An X1 evaluation is a structured assessment of a startup company generated
by the platform **[DAVID: by what/whom exactly — AI pipeline, analyst
review, hybrid? users will ask]** from the company's own materials: its
uploaded pitch deck (text-extracted), its public website content, and its
platform profile data.

Each evaluation produces:

- **An overall score** (0–100 **[DAVID: confirm scale and what bands
  mean — observed values run roughly 40–85]**) plus an evaluation date.
  The overall score is the only score stored as structured platform data.
- **A basic evaluation report** — the standard write-up, available to
  signed-in users.
- **A premium investability report** — the full-depth report; its complete
  text is purchase-gated per evaluation. Its existence and score are
  visible to everyone; the text is not.
- **Seven section analyses**, the platform's stable dimension vocabulary:
  Problem Definition, Founder Background, Team Expertise, Technology & IP,
  Market Opportunity, Market Conditions, Traction & Growth. Each section
  contains analysis and research findings **[DAVID: sections can carry
  their own scores — are those user-facing?]**.

Companies can be evaluated multiple times; each evaluation is a dated
snapshot, so score history over time is meaningful. A company can also have
an evaluation on record whose report text is not in the advisor's indexed
corpus (e.g. legacy records) — the advisor can see the score but not the
text, and says so.

## What evaluations are based on (and not)

Based on: the pitch deck the company uploaded, the company's website, and
its platform profile — i.e. substantially company-provided material, plus
the platform's research findings per section **[DAVID: does research pull
external sources? this determines how users should weigh the findings]**.

Not based on: **[DAVID: e.g. audited financials? customer references?
independent diligence? saying what's NOT covered is what the advisor most
needs — this was the original turn-44 gap]**.

## Other platform data the advisor can see

- **Startup profiles** — company description, fundraising round/status, HQ,
  team; draft (unpublished) profiles are visible only to their owners.
- **Labels** — industries (LinkedIn-style categories), target sectors
  (including company-defined custom sectors), and regions.
- **Investor profiles and match records** — investor–startup associations
  with match scores **[DAVID: how are matches produced?]**.
- **CVs / talent profiles** — including open-to-work status.
- **Uploaded files** — pitch decks etc. are files on record; unless their
  text was extracted during evaluation, they are not searchable text.
- **Monthly metrics** — **[DAVID: what do companies self-report here, and
  is it user-visible?]**

## Visibility rules users may notice

- Draft/unpublished profiles: owner-only.
- Premium report full text: purchase-gated per evaluation.
- Platform-hidden evaluations: not shown (admin-only).
- Private uploads: owner-only.
