# PipesHub‑AI — Reference Mining for X1 Advisor

> Deep‑dive of `/home/david/code/x1/link/pipeshub-ai` (backend Python AI/retrieval/agent code) to mine ideas, mechanisms, prompts, and patterns for **X1 Advisor**.
> All file paths are absolute under `/home/david/code/x1/link/pipeshub-ai/backend/python/`. Line ranges are approximate (from the snapshot read) but the named symbols are exact. Where I could not fully verify a claim by reading the file, I say so.

---

## 1. What it is

PipesHub is an open‑source **workplace AI platform** for enterprise search + workflow automation over 30+ connectors (Google, M365, Slack, Jira, Confluence, …). It does **source‑grounded RAG with precise block‑level citations**, a **LangGraph tool‑calling agent** (single‑turn QnA) and a **multi‑agent "deep research" orchestrator**, on top of a **hybrid (BM25 + dense + RRF) Qdrant index** and an **ArangoDB knowledge/permission graph**. It is a polyglot system (5 Python FastAPI services + Node + Next.js); only the Python **Query** and **Indexing** services matter to us.

The architecture is strikingly close to what X1 Advisor is building: hybrid retrieval over a unified index, structured fields as metadata filters (graph‑enforced), block citations, bounded multi‑hop. It is the single best reference in the link/ set for our spine.

---

## 2. Architecture overview (retrieval/agent parts)

Polyglot services (from `CLAUDE.md` and `app/*_main.py`):

- **Query** (`app.query_main`, port 8000) — RAG pipeline + agent runtime. **This is the heart for us.**
- **Indexing** (`app.indexing_main`, port 8091) — parse → chunk → embed → write Qdrant + Arango.
- **Docling** (`app.docling_main`, port 8081) — advanced PDF/OCR/table parsing.
- **Embedding** (`app.embedding_main`, port 8002) — local dense embedding server, OpenAI‑compatible `/v1/embeddings`.

Directory map (the parts we care about), under `app/`:

```
models/blocks.py                      # THE block / block-group data model + CitationMetadata
modules/
  retrieval/retrieval_service.py      # hybrid search spine (dense+sparse+RRF, ACL, record hydration)
  reranker/reranker.py                # CrossEncoder reranker
  transformers/                       # indexing pipeline (parse→extract→chunk→embed→sink)
    pipeline.py  sink_orchestrator.py  vectorstore.py  document_extraction.py
    graphdb.py  blob_storage.py  block_container_validator.py
  parsers/{pdf,docx,excel,pptx,csv,html_parser,image_parser,markdown,sql}/
  indexing/run.py
  qna/                                # the *prompts* (citation rules live here)
    prompt_templates.py  response_prompt.py  prompt_templates.py
  reranker/  retrieval/
  agents/
    qna/                              # single-turn LangGraph tool-calling agent
      graph.py nodes.py(~8.9k) tool_system.py chat_state.py
      conversation_memory.py memory_optimizer.py cache_manager.py stream_utils.py
    deep/                             # multi-agent deep-research orchestrator
      graph.py orchestrator.py orchestrator_critic.py orchestrator_reflection.py
      sub_agent.py context_manager.py aggregator.py respond.py tool_router.py
      state.py prompts.py
agents/
  tools/{decorator.py,registry.py,factories/...}   # tool definition + registry framework
  actions/{retrieval,knowledge_hub,...}/            # concrete tool implementations
utils/
  chat_helpers.py                     # block flattening, CitationRefMapper, message assembly
  citations.py                        # citation normalization + post-validation (HIGH VALUE)
  converters/docling_doc_to_blocks.py # Docling DoclingDocument -> Block model
services/
  vector_db/qdrant/qdrant.py          # collection config, hybrid vectors, INT8 quantization
  graph_db/{arango,neo4j,interface}/  # ACL graph traversal
schema/arango/graph.py                # graph collections + edges
```

---

## 3. Key ideas & patterns for X1 Advisor

### 3.1 Retrieval pipeline (hybrid spine)

**Where:** `modules/retrieval/retrieval_service.py` (`RetrievalService`, 758 lines).

The single most directly reusable file. Core mechanism (`_execute_parallel_searches`, lines ~638‑700):

- **Hybrid = dense + sparse + RRF, server‑side in Qdrant.** Each query is embedded densely (`dense_embeddings.aembed_query`) **and** sparsely (`FastEmbedSparse(model_name="Qdrant/BM25")`). They issue a single Qdrant `QueryRequest` with two `Prefetch` legs (`using="dense"`, `using="sparse"`, each `limit = limit*2`) fused by `models.FusionQuery(fusion=models.Fusion.RRF)`. RRF fusion is done **inside Qdrant**, not in Python. Lines 659‑679.
- **Named vectors** `"dense"` and `"sparse"` in one collection (so a single point carries both). This is exactly our "unified index" idea.
- **Multi‑query fan‑out**: `search_with_filters(queries: list[str], ...)` embeds & searches *all* queries in parallel via `asyncio.gather` and dedups by `point.id` (`seen_points`). This is the natural home for query decomposition (issue N reformulations, merge).
- **Embedding‑model consistency guard** (lines 94‑99, 148‑186): the dense model is **deliberately not cached on the instance** — it is re‑resolved from config on *every* search so the query embeds with the exact model the indexing pipeline used (otherwise Qdrant dimension mismatch). They mirror the indexing pipeline's selection logic (`isDefault` config → first → built‑in default). **Lesson: pin the embedding model identically across index & query, resolved from one config.**
- **Lazy heavy‑model loading off the event loop**: `_ensure_sparse_embeddings()` and the reranker's `_ensure_model_loaded()` defer ONNX/CrossEncoder downloads into `asyncio.to_thread` behind an `asyncio.Lock` (cold load is 30‑60s and would block). Good pattern for any local model.
- **BGE query prefixing**: `_preprocess_query` adds `"Represent this document for retrieval: "` when model name contains `bge` (lines 216‑236). Reminder that some embedding models need instruction prefixes.

**Why it matters / how we adapt:** This is essentially our `search_corpus` tool. Copy the dense+sparse+RRF `QueryRequest` shape almost verbatim for pgvector? — Note: they get RRF *for free* from Qdrant; **on Postgres+pgvector we must implement RRF ourselves** (separate BM25 via `ts_rank`/`paradedb` + dense via pgvector, then RRF‑merge in SQL or app code). Their multi‑query gather + dedup is directly reusable. Their "complete results" filter (lines 532‑545: drop any result missing `origin/recordName/recordId/mimeType/orgId`) is a good guard so citations never break downstream.

### 3.2 Reranker

**Where:** `modules/reranker/reranker.py` (`RerankerService`).

- Default model `cross-encoder/ms-marco-MiniLM-L-6-v2`; docstring lists `BAAI/bge-reranker-base` (balanced) and `BAAI/bge-reranker-large` (accurate) as upgrades.
- CrossEncoder, fp16 on CUDA (`model.model.half()`), lazy‑loaded in a worker thread.
- **Score fusion**: `final_score = 0.3 * retriever_score + 0.7 * reranker_score` (line 113). Images are excluded from reranking and keep their original score.
- Skips TABLE blocks' structured content (reranks the table summary text instead).

**Adapt:** Start with `bge-reranker-base` or a hosted reranker (Cohere/Voyage) for X1; the 0.3/0.7 blend and "exclude non‑text blocks from the cross‑encoder" detail are worth copying.

### 3.3 Query transformation / decomposition / planning

There is **no HyDE / classic query‑rewrite module** in retrieval itself — query reformulation happens **at the agent layer**:

- **QnA agent**: the planner emits multiple `search_internal_knowledge(query=…)` calls; the prompt explicitly tells the model to *make separate search calls per topic* (`prompt_templates.py` lines 209, 185‑186). Multi‑query is thus model‑driven, executed in parallel by the retrieval service.
- **Deep agent**: the **orchestrator** does true **query decomposition into a typed task DAG** (see 3.5). Decomposition constraints, dependency edges (`depends_on`), and "one domain per task" are enforced and then **critiqued** before execution.

**Adapt:** This matches our "agent owns the loop" decision. We can let the Haystack agent emit several `search_corpus` reformulations per hop (cheap, parallel) and reserve a deep‑research orchestrator for explicit decomposition.

### 3.4 Citation / grounding mechanism — **the highest‑value finding**

This is a genuinely good, end‑to‑end **block‑level citation** system. Trace:

**(a) Block data model** — `models/blocks.py`:
- `Block` (lines ~296‑329): `id`, `index` (position in `blocks[]`), `parent_index`, `type` (`BlockType`: TEXT, IMAGE, TABLE_ROW, RECORD_SUMMARY…), `data`, `citation_metadata`, `semantic_metadata`.
- `CitationMetadata` (lines ~91‑131): `page_number`, **`bounding_boxes` (4 `Point`s)**, `line_number`, `paragraph_number`, plus Excel (`sheet_name`, `cell_reference`, `row_number`) and AV (`start_timestamp`/`end_timestamp`). **This is how a citation resolves to a precise span / box / cell on the page.**
- `BlockGroup` (lines ~429‑491) with `BlockGroupChildren` using **`IndexRange` (start,end)** instead of flat child lists — a 1000‑row table is `IndexRange(0,999)`, not 1000 entries. Compact and clever.

**(b) Tiny‑ref indirection** — `utils/chat_helpers.py`:
- `CitationRefMapper` (lines ~220‑252): bidirectional map `full_url ↔ refN`. Each block shown to the LLM is labelled with a short **Citation ID** like `ref1`, `ref2`. LLMs reliably reproduce `ref1`; they mangle long URLs. The full URL is `{frontend}/record/{record_id}/preview#blockIndex={block_index}`.
- The LLM context renders each block as `* Block Index: N / * Citation ID: refK / * Block Content: …` (templates in `qna/prompt_templates.py` lines 64‑88: `table_prompt`, `block_group_prompt`, `agent_block_group_prompt`).

**(c) Prompt instruction** — `qna/prompt_templates.py` (lines 253‑260) and `qna/response_prompt.py` (`<citation_rules>`):
> "Cite by embedding the Citation ID as a markdown link: `[source](ref1)`. Use EXACTLY the Citation ID shown in the context. Do NOT manually assign citation numbers — the system numbers them automatically. … **If you are unsure which block a fact came from, omit the citation rather than guessing.**"

The model emits `[source](ref1)` inline immediately after each claim, and ends with a `--- Confidence: <Very High|High|Medium|Low>` delimiter line (parsed out).

**(d) Post‑validation & normalization** — `utils/citations.py` (`normalize_citations_and_chunks`, lines ~452‑689). Three stages:
1. **Repair malformed citations** (`normalize_malformed_citations`): `[src](ref1, ref2)` → two links; `[ref3]` → `[source](ref3)`; bare `ref5` → wrapped link.
2. **De‑dupe** redundant adjacent citations to the same target.
3. **Resolve & renumber**: `refN → full_url → (record_id, block_index)`, fetch the block from the record's `block_containers.blocks[block_index]`, build a citation chunk `{content, chunkIndex, metadata{recordId, blockNum, pageNum, bounding_box, webUrl, …}, citationType}` and **renumber sequentially** to `[1], [2], …`. Web citations get `citationType: "web|url"`, internal get `"vectordb|document"`.

**Why it matters:** This is exactly our "precise block citations tying answers to source spans." The four ideas are individually stealable and language‑agnostic:
- **Stable block index as the citation primitive** (answer → ref → url → (record, block_index) → text+bbox).
- **Tiny opaque refs** instead of URLs in the model's output (massively reduces malformed citations).
- **"Omit rather than guess"** instruction + **server‑side renumbering** (model never picks numbers).
- **Post‑hoc citation validator** that resolves every emitted ref against the actual retrieved blocks and *drops/repairs* invalid ones — grounding is enforced in code, not trusted from the LLM.

**Adapt for X1:** Our markdown docs are chunked; assign each chunk a stable `block_index`/`chunk_id` within the doc and a `(page, char_span)` for PDFs. Render chunks to the agent with `refN`. Build a `citations.py`‑style resolver that maps emitted refs back to chunk rows in Postgres and emits citation objects with `doc_id`, `chunk_id`, `page`, `span`, `url`. For DB "profile documents," the citation target is the entity row + field, not a page.

### 3.5 Agent / deep‑research design

Two distinct LangGraph agents:

**QnA agent (single‑turn, fast)** — `modules/agents/qna/graph.py`:
- Two compiled graphs. The legacy 5‑node graph: `planner → execute → reflect → (prepare_retry|prepare_continue|respond) → END`, with `reflect` doing fast‑path error pattern matching before falling back to an LLM. The **modern graph** is a 2‑node `ReAct agent → respond` (1 LLM call, "4‑6s vs 12‑15s"). Reflection decisions: `respond_success | respond_error | respond_clarify | retry_with_fix` (max 1 retry).
- **`respond` is its own node** reused by both graphs so final formatting/citations are identical.

**Deep‑research agent (multi‑hop, multi‑agent)** — `modules/agents/deep/`:
- Graph (`graph.py`): `orchestrator → critic → execute_sub_agents → aggregator → respond`. Conditional routing (`route_after_orchestrator`, `route_after_critic`, `route_after_evaluation`) with **critic running exactly once** and a **single re‑plan** allowed.
- **Orchestrator** (`orchestrator.py`): compacts history → groups tools by domain → builds plan via `run_orchestrator_with_reflection` (2‑layer reflection: JSON‑parse retry, then plan‑validation retry against constraints: domains exist, no forward refs, no cycles, no dup task_ids). Output is a typed **task DAG** with `depends_on`, per‑task `scoped_instructions`, optional `complexity:"complex"` + `batch_strategy{page_size,max_pages,scope_query}`, and `multi_step` + `sub_steps`.
- **Critic** (`orchestrator_critic.py`): split **System vs Human** prompt so the plan's own `reasoning` can't bias the critic. 4‑dimension rubric (Structural=CRITICAL, Intent=MAJOR, Domain/Tool=MAJOR, Decomposition=MINOR) with numbered rules S1‑S10/I1‑I4/D1‑D4/Q1‑Q5; decision `approve|revise` with a **bias‑toward‑approve** rule (partial execution beats delay). Falls back to APPROVE on parse failure.
- **Sub‑agents** (`sub_agent.py`, 1847 lines): event‑based dependency resolution — *all* tasks launched at once via `asyncio.gather`, each gated by its own `asyncio.Event`, so a task waits only on its specific deps. Three execution modes: **simple** ReAct (tool‑call budget `_MAX_TOOL_CALLS_PER_AGENT=20`, retrieval `=10`), **complex** 3‑phase (FETCH budget 35 → parallel per‑batch LLM **summarize** → **consolidate**), **multi‑step** mini‑orchestrator. Each sub‑agent gets an **isolated context** (its task + its dependency results + last 3 turns only).
- **Aggregator** (`aggregator.py`): fast‑path decisions (all success / all error / partial at max‑iter) then an LLM `EVALUATOR_PROMPT` for ambiguous cases → `respond_success | respond_error | retry | continue`. Iteration capped (`deep_max_iterations=3`, `state.py`).

**Why/adapt:** This is a complete, production‑grade multi‑hop blueprint. The patterns most worth importing for our deep‑research loop: **plan → critic gate → execute → evaluate → (bounded) retry/continue**; **typed task DAG with explicit `depends_on` + event‑gated parallel dispatch**; **per‑task scoped instructions** (sub‑agents don't see global system prompt); **bias‑toward‑approve** critic to avoid stalling; **hard iteration + tool‑call budgets**. We can implement the same shape in Haystack with a planner component + a critic component + parallel tool runs.

### 3.6 Document parsing / ingestion / chunking / embedding

**Where:** `modules/transformers/*`, `utils/converters/docling_doc_to_blocks.py`, `modules/parsers/*`, `services/vector_db/qdrant/qdrant.py`.

- **Parse → blocks via Docling.** `DoclingProcessor.parse_document` (PyPdfiumDocumentBackend, `generate_picture_images=True`, `do_ocr=False` by default), then `docling_doc_to_blocks.py` converts the `DoclingDocument` into the `Block`/`BlockGroup` tree in reading order, carrying **page numbers and bounding boxes normalized to [0,1]**. Text→TEXT blocks; images→IMAGE blocks (base64 data‑uri + captions); tables→`BlockGroup(TABLE)` with `TABLE_ROW` children.
- **Multimodal → searchable text** (`utils/indexing_helpers.py`): tables get an LLM‑generated `get_table_summary_n_headers()` summary + per‑row `get_rows_text()` natural‑language descriptions (capped at 20 rows in the prompt); images either embedded directly (multimodal embedder) or **described by a VLM** (`describe_image_async`, concurrency 10) and the description is indexed as text.
- **Chunking = structure‑aware + sentence‑level (spaCy), no fixed size/overlap.** `modules/transformers/vectorstore.py` loads `en_core_web_sm` with a **custom sentence boundary** component (abbreviations, numbered lists, bullets, ALL‑CAPS headings are *not* boundaries). Each TEXT block is embedded both **as the whole block and as individual sentences** (lines ~1206‑1232). Tables embed the summary + each row description. **There is no `chunk_size`/`overlap` constant** — chunking follows document structure, not a sliding window.
- **RECORD_SUMMARY block** (`vectorstore.py` ~194‑239): a denormalized, document‑level LLM summary stored as its own indexed block (`isRecordSummary=True`, id `…:record_summary`). Lets a single semantic hit surface the whole doc's gist. **Directly relevant to our "profile documents."**
- **Embedding/store** (`qdrant.py`): named vectors `dense` (size = runtime `len(embed_query("test"))`, COSINE, default fallback 1024) + `sparse` (BM25, optional IDF `Modifier.IDF`). **INT8 scalar quantization** (`quantile=0.95`, `always_ram=True`) for memory. Payload indices on `virtualRecordId` and `orgId` for filtering. Batch upserts (50 cloud / 10 local).
- **Pipeline orchestration**: `pipeline.py` (`IndexingPipeline.apply`): validate blocks → **reconciliation** (1:1 same `virtual_record_id` ⇒ diff blocks and re‑index only changed ones, preserving block IDs for citation continuity; N:1 ⇒ full re‑index) → `document_extraction` (LLM semantic metadata: summary/topics/departments/categories) → `sink_orchestrator` (blob storage → vector store → graph). The **reconciliation/diff‑indexing** is the cleanest way to keep an index fresh without re‑embedding everything.

**Adapt for X1's three doc types:**
- **Pitch decks (visual PDFs):** Docling with picture images on, VLM‑describe each slide/image, table→NL — gives searchable text + bounding boxes for citations. Strongly consider per‑page IMAGE block + VLM caption since decks are visual.
- **CVs:** sentence‑level + RECORD_SUMMARY (a denormalized "candidate profile" summary block is essentially our profile doc).
- **Long reports:** structure‑aware blocks + per‑section grouping + RECORD_SUMMARY.
- **Open question (chunking):** their answer is *structure/sentence‑aware, dual‑granularity (sentence + block)*, **not** fixed windows. Worth trialing against a recursive/semantic splitter for our reports.

### 3.7 Context management (bounded multi‑hop) — high value for us

**Where:** `modules/agents/deep/context_manager.py` (1031 lines), `qna/memory_optimizer.py`, `qna/conversation_memory.py`.

Deep agent (`context_manager.py`) keeps context bounded via layered compaction:
- **Conversation compaction** (`compact_conversation_history_async`): keep last `MAX_RECENT_PAIRS=5` turns verbatim, **LLM‑summarize older turns** (multimodal — old images/PDFs replayed into the summary call), return `(summary, recent_messages)`.
- **Tool‑result compaction** (`compact_tool_results`, `max_chars≈3000`): keep priority keys (id/key/name/status/url) intact, truncate big string values to ~200 chars, recurse into nested structures, flag `_truncated`.
- **Isolated sub‑agent context** (`build_sub_agent_context`): each sub‑agent sees only its task + dependency results (compacted to ~2K each) + last ~3 turns — **not** the whole conversation. Returned as a content‑block list (text + image_url interleaved) for multimodal models.
- **Batch summarize → consolidate** for high‑volume "complex" tasks: `group_tool_results_into_batches` (20K‑char budget) → parallel `summarize_batch` (per batch, "do NOT omit any item") → `consolidate_batch_summaries` (merge into one markdown report, input capped at `_MAX_SUMMARIES_TEXT_LEN=50000`). Budgets in `state.py`: `context_budget_tokens=16000`, `deep_max_iterations=3`.

QnA agent (`memory_optimizer.py`) bounds the cheaper single‑turn path: `MAX_MESSAGE_HISTORY=20`, `MAX_TOOL_RESULTS=15`, `MAX_DOCUMENT_SIZE=5000`, `MAX_TOTAL_CONTEXT_SIZE=100000`; `prune_state()` drops intermediate fields (decomposed/rewritten/expanded queries, raw search_results); `compress_context()` keeps 40% head + 40% tail of overlong content.

**Note on our "rehydrate on demand":** PipesHub achieves the same *effect* differently. Retrieval returns block excerpts; if insufficient, the LLM calls **`fetch_full_record(record_ids=[…])`** to pull the complete record (`prompt_templates.py` lines 105‑127). That's their "offload then rehydrate" — excerpts in context, full doc fetched only when the model asks. Maps cleanly to our `get_source` tool.

**Adapt:** Import wholesale — recent‑turns‑verbatim + LLM summary of older turns; compact tool results by keeping IDs/links and truncating bodies; isolate sub‑agent context; explicit token/iteration budgets; and the `fetch_full_record`‑style rehydration tool (= our `get_source`). The "preserve every item, do NOT summarize away rows" instruction in the batch prompts also satisfies our **no‑silent‑truncation** rule.

### 3.8 Knowledge graph — what it actually does

**Where:** `schema/arango/graph.py`, `services/graph_db/arango/arango_http_provider.py`, used from `retrieval_service.py`.

- Vertex collections: `users, teams, roles, groups, orgs, records, files, mails, webpages, tickets, record_groups, departments, categories, subcategories1/2/3, topics, languages, agent_instances/toolsets/tools/knowledge, …`. Edge collections: `belongs_to, inherit_permissions, permission, is_of_type, record_relations, belongs_to_department/category/topic/language, agent_has_toolset/knowledge, …`.
- **The graph is used in retrieval primarily for ACL + metadata filtering, NOT entity‑relationship walk retrieval.** Flow (`retrieval_service.search_with_filters`): (1) `graph_provider.get_accessible_virtual_record_ids(user, org, filters)` walks up to **10 permission paths** (direct user, group, record‑group inheritance 0‑10 levels, org/domain, "anyone" public; role priority OWNER>WRITER>COMMENTER>READER) to produce the set of accessible `virtualRecordId→recordId`; (2) Qdrant hybrid search is filtered to those IDs (`should: virtualRecordId ∈ accessible`); (3) returned virtual IDs are mapped back to permission‑verified record IDs (anti cross‑connector leakage) and hydrated from Arango. `record_relations` and `belongs_to_*` exist but are used for **metadata filters** (department/category/topic/language) and hierarchy, not multi‑hop "find related entities."

**Adapt:** For X1 this is the model for **structured fields as metadata filters, not LLM SQL** — exactly our decision. We likely don't need ACL, but we *do* want their pattern of (a) resolving structured constraints to a filter set, (b) `must`/`should` Qdrant‑style filters, (c) hydrating full entity rows post‑search. If we later want graph‑walk retrieval (investor→fund→portfolio), note that PipesHub does **not** demonstrate that — it's a filtering graph, so we'd be designing that ourselves.

---

## 4. Specific components / tools worth stealing

1. **`utils/citations.py` — citation normalizer/validator.** The repair→dedupe→resolve→renumber pipeline; resolve every emitted `refN` against actual retrieved blocks; drop/repair invalid ones. Single highest‑value file.
2. **`CitationRefMapper` + tiny‑ref rendering** (`utils/chat_helpers.py`). Opaque `refN` shown to the model instead of URLs.
3. **Block / CitationMetadata model** (`models/blocks.py`) — `index`, `parent_index`, `IndexRange` children, `bounding_boxes`, page/cell/timestamp locators. Adopt the shape for our chunk/citation schema.
4. **Hybrid `QueryRequest`** (dense+sparse Prefetch + RRF FusionQuery) from `retrieval_service._execute_parallel_searches`. Multi‑query gather + `point.id` dedup.
5. **RECORD_SUMMARY denormalized summary block** (`vectorstore.py`) — our "profile document" rendered as an indexed summary unit.
6. **Reconciliation / diff‑indexing** (`pipeline.py`) — re‑embed only changed blocks, preserve IDs for citation continuity. Key for refreshing entity profile docs.
7. **Table→NL and image→VLM‑caption indexing** (`utils/indexing_helpers.py`) — make tables/images searchable text while keeping bbox citations.
8. **Deep‑research control loop** (`deep/graph.py` + `orchestrator.py` + `orchestrator_critic.py` + `aggregator.py`) — plan→critic→execute→evaluate with bounded retries, typed task DAG, event‑gated parallel dispatch.
9. **`context_manager.py` compaction toolkit** — recent‑verbatim + LLM summary, `compact_tool_results`, isolated sub‑agent context, batch‑summarize→consolidate. All reusable for context discipline.
10. **`fetch_full_record` rehydration tool + prompt** (`prompt_templates.py` 105‑127) — model‑driven "offload then rehydrate." Maps to our `get_source`.
11. **Tool‑definition framework** (`agents/tools/decorator.py`): `@tool(... args_schema=PydanticModel, llm_description=…, when_to_use=[…], when_not_to_use=[…], typical_queries=[…])`. Rich, structured tool metadata for planner few‑shotting — better than free‑text descriptions. The retrieval tool's `SearchInternalKnowledgeInput` with optional `connector_ids`/`collection_ids` (which become metadata filters) is a template for our `structured_query`/`search_corpus` schemas.
12. **Web research prompt** (`qna/prompt_templates.py` `web_search_user_prompt`, lines 22‑60): "only use info retrieved from web_search/fetch_url, never training knowledge; on `fetch_url` failure don't stop — try other URLs; cite `[source](url)`." Drop‑in for our `web_search`/`fetch_url`.
13. **Confidence + answerMatchType envelope** (`prompt_templates.py` `AnswerWithMetadataJSON`: `confidence ∈ {Very High..Low}`, `answerMatchType ∈ {Exact Match, Derived From Blocks, Derived From User Info, Enhanced With Full Record}`) — cheap, useful grounding signal to surface per answer.

---

## 5. Direct connections to our open design decisions

| Open decision | PipesHub evidence | Take‑away for X1 |
|---|---|---|
| **Embedding model/dim** | Dim is runtime‑detected (`len(embed_query("test"))`), default fallback **1024**, COSINE, **INT8 quantization**. Model re‑resolved from one config at index *and* query time. | Don't hard‑code dim; pin one embedding config used by both ingest & query. Consider INT8/quantization for pgvector‑scale memory. BGE needs an instruction prefix. |
| **Chunking per doc type** | **Structure‑aware + sentence‑level (spaCy custom boundaries), dual granularity (sentence + whole block), no fixed window.** Tables→summary+row‑NL; images→VLM caption; RECORD_SUMMARY per doc. | Adopt structure‑aware chunking; add a doc‑summary chunk. For visual decks, page‑image + VLM caption. Trial vs recursive splitter for long reports. |
| **Reranker choice** | CrossEncoder; default MiniLM, recommends `bge-reranker-base/large`; `final = 0.3*dense + 0.7*rerank`. | Start `bge-reranker-base` or hosted Cohere/Voyage; copy the score blend; exclude non‑text blocks. |
| **Web search provider** | Provider abstracted; agent‑level `web_search`+`fetch_url` tools with a strict "don't answer from training knowledge / retry other URLs on fetch fail" prompt. | Provider choice is swappable; **steal the prompt + fetch‑retry logic** rather than the provider. |
| **Profile documents** | **RECORD_SUMMARY** denormalized LLM summary indexed as its own block; semantic metadata extraction; reconciliation diff‑re‑index. | Render each entity (startup/investor/person) to a profile‑doc with a summary chunk; refresh via diff‑indexing, preserving chunk IDs for stable citations. |
| **Citations/grounding** | Tiny‑ref → url → (record, block_index) → text+bbox; "omit rather than guess"; server‑side renumber; `citations.py` post‑validator drops invalid refs. | Implement this end‑to‑end. It is the strongest piece of the repo. |
| **Knowledge‑graph retrieval** | Graph = **ACL + metadata filtering**, not relationship‑walk retrieval. Structured fields → Qdrant `must/should` filters. | Confirms our "fields as filters, not LLM SQL." Graph‑walk retrieval is *not* solved here — design ourselves if needed. |
| **Query transformation/planning** | Multi‑query at agent layer (parallel `search` calls); deep agent does typed‑DAG decomposition + critic gate. | Let Haystack agent fan out reformulations per hop; add a planner+critic for deep mode. |
| **Deep‑research loop** | `orchestrator→critic→execute→aggregator→respond`, bounded (`deep_max_iterations=3`, tool budgets 10/20/35), event‑gated parallel sub‑agents, batch‑summarize→consolidate. | Adopt this skeleton; enforce iteration + tool‑call budgets; isolate sub‑agent contexts. |
| **Context discipline/memory** | Recent‑verbatim + LLM summary; `compact_tool_results`; `fetch_full_record` rehydration; explicit budgets. | Import directly; our "offload + rehydrate" = excerpts + `get_source`. |

---

## 6. What to ignore (not relevant to us)

- **Connectors** (`app/connectors/**`, `app/sources/**`, 30+ sources, OAuth/token refresh, `ConnectorFactory`) — enterprise data‑source integrations; we have our own three sources.
- **Auth/permissions ACL graph traversal** (`get_accessible_virtual_record_ids`'s 10 permission paths) — borrow the *filter‑then‑hydrate* shape, skip multi‑tenant ACL.
- **Node.js API, billing, org/user management, Kafka/etcd/Redis‑streams plumbing, feature flags, code‑generator, sandbox/coding tools.**
- **MongoDB session storage, the connector "playbook," Docker/deployment.**
- Their **N:1 vs 1:1 `virtualRecordId`** dedup across connectors — solves a multi‑connector leakage problem we don't have (but the diff‑indexing idea is still useful).

---

## 7. Top 5 recommendations (highest leverage)

1. **Adopt the block‑level citation system wholesale.** Stable `chunk_id`/`block_index` per doc + `(page, span/bbox)` locator; render tiny `refN` to the agent; instruct "omit rather than guess"; server‑side renumber; and build a `citations.py`‑style **post‑validator that resolves every emitted ref against actually‑retrieved chunks and drops/repairs invalid ones.** This is the difference between "the model claims a citation" and "the citation is verified." (`utils/citations.py`, `utils/chat_helpers.py`, `models/blocks.py`).

2. **Implement hybrid retrieval with explicit RRF and dual granularity.** Dense (pgvector) + BM25 (`ts_rank`/ParadeDB) → **RRF‑merge in our code** (Qdrant gave PipesHub RRF for free; pgvector won't) → cross‑encoder rerank with `0.3*dense + 0.7*rerank`. Index both a per‑doc **summary chunk** and finer chunks; multi‑query fan‑out + `point_id` dedup. (`retrieval_service._execute_parallel_searches`, `reranker.py`).

3. **Render entity "profile documents" as RECORD_SUMMARY‑style summary chunks and refresh via diff‑indexing.** One LLM‑authored profile per startup/investor/person, indexed as a first‑class unit alongside doc chunks; re‑embed only changed sections, preserving chunk IDs so citations stay stable. (`vectorstore.py` RECORD_SUMMARY, `pipeline.py` reconciliation).

4. **Copy the deep‑research control loop: plan → critic → execute → evaluate, bounded.** Typed task DAG with `depends_on` + per‑task scoped instructions, event‑gated parallel dispatch, a **bias‑toward‑approve critic** to avoid stalling, and hard **iteration + tool‑call budgets**. Reserve it for "deep" queries; use a fast single‑pass ReAct loop for ordinary turns (their modern 2‑node graph). (`deep/graph.py`, `orchestrator.py`, `orchestrator_critic.py`, `aggregator.py`).

5. **Import their context‑discipline toolkit.** Recent‑turns‑verbatim + LLM summary of older turns; `compact_tool_results` (keep IDs/links, truncate bodies); isolated sub‑agent context; explicit token/iteration budgets; and a **`fetch_full_record`‑style rehydration tool = our `get_source`** so large results stay out of context until the agent explicitly pulls them. Honor "preserve every item, don't summarize rows away" to respect our no‑silent‑truncation rule. (`deep/context_manager.py`, `qna/memory_optimizer.py`, `prompt_templates.py`).

---

### Caveats / unverified
- Line ranges for `nodes.py` (~8.9k lines) and `citations.py` come from sub‑agent reads + grep, not a full personal read of every line; the **named symbols and prompt quotes are verbatim from files I read**, but a few line numbers may drift by a handful.
- I did not pull external Context7 docs (Qdrant/Docling/LangGraph are well‑known and the repo's own usage is concrete enough); flag if you want their official docs cross‑checked.
- PipesHub uses **LangGraph + LangChain + Qdrant**, not Haystack/pgvector — patterns transfer at the *mechanism* level; the RRF‑in‑Qdrant convenience and named‑vector collection do **not** transfer directly to pgvector and must be re‑implemented.
