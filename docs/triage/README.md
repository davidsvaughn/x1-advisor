# Per-thread triage docs

One markdown doc per live advisor thread that produced issues — flagged by
David in the console/REPL (`.qa-artifacts/repl/flagged.jsonl`), raised in
teacher-session discussion, or found in review. Practice adopted 2026-08-12
(David's proposal); the per-RUN analogue is the Track-H1 triage report in
`.qa-artifacts/reports/`.

Each doc records, per issue: the source (flag turn + David's note, or
review), the diagnosis with evidence links, the planned fix, and its status
(`open` / `queued` / `David-gated` / `fixed <commit>` / `wontfix <why>`).
Update the doc as fixes land — it is the durable memory of the triage;
DECISIONS.md records only the decisions that come out of it.

**Body-free rule (same as manifests):** never quote answer text or corpus
content here — this folder is committed. Describe the issue; link the
evidence: turn ids, bundle paths under `.qa-artifacts/runs/`
(`turn_<id>_thread_<n>.json` holds everything the model saw and said),
code refs, commits. David's own flag notes may be quoted — they are his
authored commentary, not corpus content.

| Thread | Doc | Focus |
|---|---|---|
| 21 | [thread-021.md](thread-021.md) | first structured-prompt session: coverage semantics, label queries, formatting, provenance confusion |
