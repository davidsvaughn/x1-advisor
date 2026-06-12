# Chroma — Reference for X1 Advisor

Deep-dive of the Chroma vector DB codebase (`/home/david/code/x1/dev/chroma`,
Rust core + Python/JS clients) to (a) mine retrieval/search ideas for the X1
Advisor research agent and (b) pressure-test **pgvector vs Chroma** for our
document store. All claims cite real files; where a claim is uncertain it is
flagged.

---

## 1. What it is

Chroma is an open-source vector database. The query/index core is written in
**Rust** (`rust/` — 40 crates), exposed to Python via PyO3 bindings
(`chromadb/chromadb_rust_bindings.pyi`, `rust/python_bindings/`) and to JS/TS.
A collection holds items of `{id, embedding, optional metadata, document}` and
is "independently indexed and optimized for vector similarity, full-text
search, and metadata filtering" (`docs/mintlify/reference/architecture/overview.mdx`).
It runs **embedded (local), single-node server, or distributed (Chroma Cloud)**
with a consistent client API across all three.

Distance functions and many query semantics are first-class in the Rust types
crate (`rust/types/src/`), and there is a brand-new **Search API** (`Search`/
`K`/`Knn`/`Rrf`/`RankExpr`) that unifies `query()` + `get()` and adds
hybrid/rank-fusion — currently **Cloud-only**, OSS support "planned"
(`docs/mintlify/cloud/search-api/overview.mdx`).

---

## 2. Architecture overview

### Data model (`reference/architecture/overview.mdx`)
- **Tenant** → **Database** → **Collection**. Tenants give full isolation
  (access control, quota, billing). A collection is the unit of storage and
  query; names unique within a database.

### Three deployment modes (same docs)
- **Local**: embedded library (SQLite-backed; `rust/sqlite/`, `chromadb/db/`).
- **Single-Node**: one server, "typically fewer than 10M records." Uses an
  in-RAM **HNSW** index (fork of `hnswlib`) + SQLite for docs/metadata
  (`docs/mintlify/guides/performance/single-node.mdx`).
- **Distributed / Cloud**: services split out; uses **SPANN** on object
  storage with SSD caches.

### Five core components, distributed (`reference/architecture/distributed.mdx`)
- **Gateway** — auth, rate-limit, quota, request validation, turns a request
  into a *logical plan*; routes via **rendezvous hashing on collection ID** to
  preserve cache coherence.
- **Log** — write-ahead log (`rust/log/`, `rust/wal3/`). Records writes before
  ack; gives atomicity across multi-record writes + replay.
- **Query Executor** — all reads (vector + full-text + metadata). Turns logical
  plan → *physical plan*; mixes in-memory and on-disk indexes; consults the log
  for read-your-writes consistency.
- **Compactor** — async; reads the log, builds new vector/full-text/metadata
  index versions, writes them to storage, registers versions in the SysDB.
- **System Database (SysDB)** — catalog of tenants/dbs/collections + cluster
  metadata, backed by SQL (`rust/sysdb/`, `rust/rust-sysdb/`).

Key architectural pattern: **compute/storage separation** — log + built
indexes live in object storage; SSDs are caches. Cold-start latency on first
query of a collection (cache warm-up), then served from local SSD. This is the
"object storage as source of truth, local disk as cache" pattern.

### Indexing internals (Rust)
- `rust/index/` — `hnsw.rs`, `hnsw_provider.rs`, `spann.rs`, `spann/`,
  `usearch.rs`, `quantization/`, `sparse/`, `fulltext/`, `metadata/`.
- `rust/segment/` — segment implementations: `distributed_hnsw.rs`,
  `local_hnsw.rs`, `distributed_spann.rs`, `quantized_spann.rs`,
  `blockfile_metadata.rs`, `blockfile_record.rs`, `sqlite_metadata.rs`,
  `bloom_filter.rs`. So a collection is materialized as multiple **segments**
  (vector, metadata, record) each with local vs distributed variants.
- **Blockstore** (`rust/blockstore/`) — an Arrow-backed, object-storage-friendly
  key→value store (`blockfile`) that underpins both the full-text bitmap index
  and SPANN posting lists.

### HNSW vs SPANN (`docs/mintlify/docs/collections/configure.mdx`)
- **HNSW** (single-node): graph ANN, *must reside in RAM*. Tunables: `space`,
  `ef_construction` (def 100), `ef_search` (def 100, modifiable),
  `max_neighbors` (def 16), `num_threads`, `batch_size`, `sync_threshold`,
  `resize_factor`.
- **SPANN** (distributed/cloud): "Spatial Approximate NN" — broad clusters +
  small per-cluster indexes, designed to search billions of vectors on disk /
  across machines. Tunables (server-fixed, *cannot be customized today*):
  `search_nprobe` (def 64, cap 128), `write_nprobe` (def 64),
  `ef_construction` (def 200), `ef_search` (def 200), `max_neighbors` (def 64),
  `reassign_neighbor_count` (def 64). Config types in
  `rust/types/src/spann_configuration.rs`, `hnsw_configuration.rs`.

---

## 3. Search & retrieval capabilities (the meat)

Chroma has **two API surfaces**: the classic `query()`/`get()` (works
everywhere incl. OSS/local) and the new **Search API** (`collection.search()`,
Cloud-only).

### 3a. Classic API — `query()` and `get()`
- `query(query_texts | query_embeddings, n_results, where, where_document, ...)`
  — ANN vector search + optional filters. Auto-embeds text via the collection's
  embedding function.
- `get(ids, where, where_document, limit, offset, ...)` — pure filter/scan, no
  vector. Supports pagination (`limit`/`offset`).
- Both accept `where` (metadata) **and** `where_document` (full-text/regex) and
  combine them. Rust signature (from docs):
  `collection.query(embeddings, Some(n), Some(where_clause), None, None)`.

### 3b. Metadata filtering (`docs/.../metadata-filtering.mdx`, `rust/types/src/metadata.rs`)
Operators (confirmed in `metadata.rs` enums):
- Primitive: `$eq $ne $gt $gte $lt $lte` (`PrimitiveOperator::{Equal, NotEqual,
  GreaterThan, GreaterThanOrEqual, LessThan, LessThanOrEqual}`).
- Set: `$in $nin` (`SetOperator::{In, NotIn}`).
- Array metadata: `$contains $not_contains` over `StringArray/IntArray/
  FloatArray/BoolArray` (`ContainsOperator`, `MetadataComparison::ArrayContains`).
- Logical: `$and $or` (`BooleanOperator`, `CompositeExpression` with `children`).
- Bare `{"field": value}` is sugar for `$eq`.
- The Rust where-clause is a recursive enum:
  `Where::{Metadata(MetadataExpression), Document(DocumentExpression),
  Composite(CompositeExpression)}` — a clean, composable filter AST worth
  studying. Parsing in `rust/types/src/where_parsing.rs`; validation in
  `validators.rs`.

### 3c. Full-text + regex search (`full-text-search.mdx`, `rust/index/src/fulltext/`)
- `where_document` operators: `$contains`, `$not_contains`, **`$regex`**,
  **`$not_regex`** (`DocumentOperator::{Contains, NotContains, Regex, NotRegex}`
  in `metadata.rs`). Combine with `$and`/`$or`. **Note: full-text is
  case-sensitive** per docs (but see tokenizer note below — there is a
  lowercasing path; the substring/regex verification stage is exact).
- Implementation (`rust/index/src/fulltext/README.md`) is notable:
  - **Trigram bitmap index** over a single Arrow blockfile typed
    `(prefix:&str, key:u32) → RoaringBitmap`. Tokens hashed (murmur3) into
    24-bit buckets (16M buckets).
  - **3-stage query pipeline**: (1) candidate resolution via token buckets /
    trigram positional keys (prefix=0/infix=1/suffix=2) + bigram *transition*
    bitmaps between adjacent tokens; (2) doc-bitmap intersection (OR within
    token, AND across tokens, sorted by cardinality for early termination);
    (3) **brute-force verification** — the index is an over-estimating *sieve*,
    the caller scans candidate raw text to remove false positives. Exactness is
    achieved in stage 3, so the index can be lossy/stale on delete and still
    correct.
  - Regex is supported by this same engine
    (`rust/types/src/regex/`) — meaning **true regex search is a built-in
    primitive, not a SQL `~` afterthought.**
- Tokenizer uses `tantivy::tokenizer` (`fulltext/tokenizer.rs`): word
  splitting, lowercasing, ASCII folding, short/long filtering, then murmur3
  bucketing.

### 3d. Vector / ANN
- Dense KNN over HNSW (single-node) or SPANN (distributed). Distance functions
  (`rust/distance/src/types.rs`, `DistanceFunction` enum): **`Euclidean` (l2,
  default), `Cosine` (1 − cosine), `InnerProduct` (1 − ip)`**. SIMD kernels for
  AVX/AVX512/SSE/NEON (`distance_avx*.rs`, `distance_neon.rs`). `space` is
  per-collection config; must match what the embedding function supports.
- Dimension must match the index; mismatch errors (`ranking.mdx`).

### 3e. The new Search API — hybrid, rank fusion, scoring (Cloud-only)
This is the most relevant surface for X1 Advisor. Imports:
`from chromadb import Search, K, Knn, Rrf, Val, GroupBy, MinK, MaxK`.

A **Search dictionary** has five optional parts (`reference/search.mdx`):
`where`, `rank`, `group_by`, `limit{limit,offset}`, `select{keys}`. SDKs are a
DSL that compiles to one JSON shape (so you could replicate the JSON contract
without the SDK). Example compiled JSON:
```json
{
  "where":   {"status": {"$eq": "active"}},
  "rank":    {"$knn": {"query": "machine learning research", "limit": 100}},
  "group_by":{"keys": ["category"], "aggregate": {"$min_k": {"keys": ["#score"], "k": 2}}},
  "limit":   {"limit": 10, "offset": 0},
  "select":  {"keys": ["#document", "#score", "category"]}
}
```

**`Knn`** (`ranking.mdx`): `query` (text | dense vec | `{indices,values}`
sparse), `key` (`"#embedding"` default for dense, or a metadata key holding a
sparse vector), `limit` (candidates to consider, def **16**), `default`
(score for docs absent from this Knn's results), `return_rank` (bool).

**`RankExpr` algebra** (`rust/types/src/execution/operator.rs:1153`) — a full
scoring expression tree, serde-tagged:
`$knn $val $sum $mul $max $min $sub{left,right} $div{left,right} $abs $exp
$log`. SDK overloads Python/JS operators (`+ - * /`, `.exp() .log() .abs()
.min() .max()`) onto these. So ranking is a **composable arithmetic DSL over
sub-rankers**, e.g. `Knn(dense)*0.7 + Knn(sparse)*0.3`, or
`(Knn(a)*0.5 + Knn(b)*0.3).exp().min(0.0)`.

**Hybrid search via RRF** (`cloud/search-api/hybrid-search.mdx`): `Rrf(ranks=
[...], k=60, weights=[...], normalize=False)`. Formula (verbatim):
`score = -Σ_i w_i / (k + r_i)` (negative because Chroma orders ascending /
lower-is-better). Requires `return_rank=True` on each component Knn (uses rank
positions, not raw distances, so it's scale-agnostic across dense+sparse).
`Rrf` is sugar — you can hand-build the equivalent RankExpr:
`-0.7/(60+rank1) - 0.3/(60+rank2)`. Rust: `rrf(vec![dense, sparse], Some(60),
Some(vec![0.7,0.3]), false)` in `operator.rs`. The doc explicitly contrasts
**RRF (different scales) vs linear combination (same scales)** and discusses
`default` to control whether a doc must appear in ALL vs ANY component rankers.

**Sparse / lexical** (`cloud/schema/sparse-vector-search.mdx`): you add a
`SparseVectorIndexConfig(source_key=K.DOCUMENT, embedding_function=splade_ef)`
to the collection **Schema**; sparse vectors auto-generated on insert (SPLADE /
BM25-style) and stored under a metadata key; then `Knn(query=..,
key="sparse_embedding")`. Today: **one sparse index per collection, dense only
in `#embedding`** (multi-vector "coming"). BM25 itself exists in the Rust
embed layer (`rust/chroma/src/embed/bm25.rs`, `bm25_tokenizer.rs`) and as the
`chroma-bm25` integration.

**Group-by / diversification** (`reference/search.mdx`, `group-by.mdx`):
`GroupBy(keys=[...], aggregate=MinK(keys=[K.SCORE], k=2))` — dedupe/diversify
results by a metadata key, keeping top-k per group. Directly useful for
"don't return 5 chunks from the same pitch deck."

**Field selection**: `select=[K.ID, K.DOCUMENT, K.METADATA, K.SCORE,
"author"]` — projection to cut payload, using built-ins `#id #document
#embedding #metadata #score` plus metadata names.

### 3f. Embedding functions (`docs/.../embeddings/embedding-functions.mdx`,
`chromadb/utils/embedding_functions/`)
- **Default EF**: `DefaultEmbeddingFunction` = `ONNXMiniLM_L6_V2`
  (all-MiniLM-L6-v2, 384-dim, runs locally via ONNX —
  `onnx_mini_lm_l6_v2.py`). No API key needed.
- Pluggable providers: OpenAI, Cohere, Jina, Mistral, Gemini, HuggingFace,
  Nomic, Bedrock, Cloudflare, Baseten, Instructor, plus Chroma Cloud SPLADE /
  Qwen / BM25 sparse. The EF (with its params) is **persisted in the collection
  configuration** so it reconstructs correctly across clients — a nice
  "schema-of-record" idea. Auto-reads standard `*_API_KEY` env vars, overridable
  via `api_key_env_var`.

---

## 4. Ideas & patterns worth borrowing for X1 Advisor (even on pgvector)

1. **Rank/Score expression tree (`RankExpr`).** A serializable algebra of
   sub-rankers (`$knn`, arithmetic, `exp/log/abs/min/max`) is exactly what a
   research agent wants: the agent (or a tool) can *emit a ranking expression*
   rather than hard-coded fusion. Even on pgvector we can model our fusion
   layer as a small expression tree {dense, bm25, recency-boost, metadata-boost}
   → weighted-sum / RRF, serialized for logging & reproducibility. See
   `rust/types/src/execution/operator.rs:1153`.

2. **RRF with `return_rank` + `default` semantics.** Their explicit handling of
   "must appear in ALL rankers vs ANY ranker" via per-ranker `default` is the
   subtle correctness issue every hybrid system hits. Mirror this: decide
   whether a doc missing from BM25 but present in dense is dropped or gets a
   penalty rank. RRF formula `-Σ w_i/(k+r_i)`, k=60 default.

3. **Recursive `Where` AST** (`Metadata | Document | Composite`). A clean,
   typed filter representation that compiles to a backend — for us it would
   compile to a parameterized SQL `WHERE` over the pgvector schema. Worth
   copying the shape so the agent builds structured filters, not SQL strings.
   `rust/types/src/where_parsing.rs`.

4. **Document + metadata + embedding colocation** in one collection, with
   `select` projection. Our "profile documents" (structured entities rendered
   to markdown) + doc chunks living in one unified index matches Chroma's model
   exactly; their `select{keys}` projection pattern is a good way to keep
   tool-call payloads small.

5. **Full-text as an over-estimating sieve + exact verification stage**
   (`fulltext/README.md`). The "lossy bitmap index → brute-force verify"
   split is a great mental model: cheap recall-oriented candidate gen, then
   exact filter. For us this maps to Postgres GIN/`tsvector` (candidates) +
   exact `ILIKE`/regex verification when precision matters.

6. **Trigram + regex as first-class.** Their regex search is a real index
   primitive. We get the same via Postgres `pg_trgm` (GIN trigram) for fuzzy
   substring and `~`/`~*` regex — confirms our plan can match Chroma's
   regex/keyword capability without a separate engine.

7. **Group-by diversification (`MinK`/`MaxK` per group key).** Directly useful
   to avoid one source dominating; implement as `DISTINCT ON (source_id)` /
   window-function top-k per entity in SQL.

8. **Embedding function persisted in collection config.** Store the embedding
   model id + dim + distance space as schema-of-record in our dedicated
   Postgres schema so retrieval is reproducible and migrations are explicit.

9. **Compute/storage separation w/ object-storage source-of-truth.** Their
   pattern (binaries/indexes in object store, local SSD cache) validates our
   "binaries in GCS" decision; the index itself we keep in Postgres.

10. **Agentic-search guide** (`guides/build/agentic-search.mdx`) is essentially
    a spec for X1 Advisor: Plan → Execute (multi-collection / multi-tool) →
    Evaluate → Iterate → Synthesize, with worked examples of query decomposition
    and disambiguation ("Q3", "sales growth"). Worth reading directly as a
    design reference for the Haystack tool loop.

---

## 5. pgvector vs Chroma for OUR document store

Our context: small-but-growing corpus; **already on Postgres** (startups /
investors / people live there); need hybrid (BM25 + dense + RRF + reranker) +
metadata filtering; want single-store simplicity; binaries in GCS; Haystack
owns the loop.

### Where Chroma is genuinely strong
- **First-class hybrid + RRF + rank algebra** out of the box (`Rrf`, `RankExpr`)
  — but **Cloud-only today**. OSS/local Chroma does *not* yet expose the Search
  API (`cloud/search-api/overview.mdx`: "Support for local deployments will be
  available in a future release"). So the headline hybrid feature is **not**
  available in self-hosted OSS at the time of this repo snapshot.
- Built-in trigram **full-text + regex** index with exact verification.
- SPANN on object storage → billions of vectors, disk-resident, the real
  scaling story (Cloud).
- Default local embedding (MiniLM) — zero-config prototyping.

### Where Chroma is weak for us
- **OSS single-node hybrid gap**: self-hosted OSS gives you `query()` +
  `where_document` ($contains/$regex) but **not** the dense+sparse RRF Search
  API. To get the marquee hybrid behavior you must use **Chroma Cloud** — a
  second managed system, second data store, second auth/quota model, network
  hop. That directly conflicts with "single-store simplicity."
- **Single-node HNSW must fit in RAM** (`guides/performance/single-node.mdx`:
  `N_millions ≈ 0.245 × RAM_GB`; r7i.2xlarge/64GB ≈ 15M vectors). Fine for our
  corpus size, but it's a hard memory wall, separate from our DB.
- **No joins to our relational data.** Our startups/investors/people graph
  lives in Postgres. Chroma can only filter on metadata you denormalize into
  each item; any real join (e.g. "investors who funded startups in sector X")
  means round-tripping between Chroma and Postgres and stitching in app code.
- **Two systems to operate, back up, and keep consistent** (dual-write,
  reconciliation, transactions across stores). pgvector keeps it one DB with
  ACID writes alongside the entities the docs describe.

### pgvector for our use case
- **One store, transactional** with the existing relational data; dedicated
  schema; can `JOIN` doc chunks ↔ entities directly — a capability Chroma simply
  doesn't have.
- **Hybrid is fully achievable today**: dense via `pgvector` (HNSW/IVFFlat,
  cosine/L2/IP), lexical via Postgres FTS (`tsvector` + GIN) and/or `pg_trgm`
  for fuzzy/substring, regex via `~`/`~*`, then **RRF in SQL or in the Haystack
  fusion layer**, then a cross-encoder reranker. This reproduces Chroma's
  dense+sparse+RRF pipeline without Cloud lock-in. (Haystack already ships
  `PgvectorEmbeddingRetriever` + `PgvectorKeywordRetriever` + a
  reciprocal-rank-fusion joiner. Confirmed via Context7: Haystack ships
  `PgvectorDocumentStore`, `PgvectorEmbeddingRetriever` (vector_function ∈
  `cosine_similarity` / `inner_product` / `l2_distance`),
  `PgvectorKeywordRetriever` (Postgres FTS), and
  `DocumentJoiner(join_mode="reciprocal_rank_fusion")` — so the full hybrid+RRF
  pipeline is a supported, off-the-shelf Haystack composition.)
- **Metadata filtering** via normal SQL `WHERE` / JSONB — strictly more
  expressive than Chroma's `Where` AST (we get joins, subqueries, ranges,
  arbitrary SQL).
- **Scaling path**: pgvector HNSW is solid into the low tens of millions of
  rows on a well-provisioned instance — comfortably beyond a private doc store
  of pitch decks/CVs/reports for the foreseeable horizon. If we ever truly need
  billion-scale ANN, *that* is when a dedicated SPANN-class store (Chroma Cloud,
  or others) earns its place.

### Recommendation
**Stay on pgvector.** For X1 Advisor's profile — modest corpus, already on
Postgres, need to join docs to relational entities, want one store — pgvector
wins on operational simplicity, transactional consistency, and especially the
ability to **join document retrieval to the startups/investors/people graph**,
which Chroma cannot do. Chroma's standout differentiator (the `Rrf`/`RankExpr`
hybrid Search API) is **Cloud-only**; in self-hosted OSS you'd be hand-rolling
fusion anyway — and we can hand-roll the same fusion over pgvector while keeping
a single store. Adopt Chroma's *ideas* (RankExpr algebra, RRF `default`
semantics, Where AST, sieve+verify full-text, group-by diversification) at the
retrieval layer, not Chroma the system. Revisit only if (a) the corpus heads
toward 10s–100s of millions of chunks with strict latency SLOs, or (b) we want a
managed ANN tier — at which point Chroma Cloud's SPANN is a credible option.

---

## 6. Top 5 takeaways

1. **Chroma's killer hybrid feature (`Rrf` + `RankExpr` Search API) is
   Cloud-only**; self-hosted OSS only has `query()` + `where_document`
   ($contains/$regex). This undercuts Chroma-OSS as a single-store hybrid
   solution for us.
2. **Borrow the `RankExpr` scoring algebra and RRF semantics** (formula
   `-Σ w_i/(k+r_i)`, k=60; per-ranker `default`/`return_rank` controlling
   ALL-vs-ANY membership) for our pgvector fusion layer — it's the cleanest
   model of hybrid scoring we've seen and it's serializable/loggable.
3. **Their full-text design (trigram roaring-bitmap sieve → brute-force exact
   verification, with regex as a primitive)** maps cleanly to Postgres
   `tsvector`/GIN + `pg_trgm` + `~` regex; confirms pgvector can match Chroma's
   keyword/regex capability.
4. **pgvector wins for X1 Advisor** mainly because it can **JOIN doc chunks to
   the relational startups/investors/people graph** and stay one transactional
   store — Chroma can only filter on denormalized per-item metadata.
5. **The recursive `Where` AST, group-by diversification (`MinK`/`MaxK`), and
   persisted embedding-function config** are concrete, copyable patterns; and
   the repo's own `guides/build/agentic-search.mdx` reads like a spec for the
   Plan→Execute→Evaluate→Iterate→Synthesize loop we're building in Haystack.
</content>
</invoke>
