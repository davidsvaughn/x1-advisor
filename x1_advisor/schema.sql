-- X1 Advisor schema (PLAN.md Appendix A, v1). Idempotent: safe to re-apply.
-- Lives in the dedicated `advisor` schema; app tables are never written.

CREATE SCHEMA IF NOT EXISTS advisor;

CREATE TABLE IF NOT EXISTS advisor.index_configs (
  id              text PRIMARY KEY,              -- e.g. 'te3small_1536_ck1'
  embedding_model text NOT NULL,
  dim             int  NOT NULL,
  distance        text NOT NULL DEFAULT 'cosine',
  chunker_version text NOT NULL,
  status          text NOT NULL DEFAULT 'experimental',  -- 'active' | 'experimental' | 'retired'
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS advisor.documents (
  id            bigserial PRIMARY KEY,
  source_type   text NOT NULL,   -- 'upload'|'website'|'eval_premium'|'eval_basic'|'eval_section'|'deck_extract'|'research_note'|'profile'
  entity_type   text, entity_id bigint,          -- loose FK to app entity
  title         text, markdown text NOT NULL,
  source_ref    text,                            -- GCS path of origin (bundle/binary), if any
  version       int  NOT NULL DEFAULT 1,
  superseded_by bigint REFERENCES advisor.documents(id),
  content_hash  text NOT NULL,
  extraction_model text, extraction_config text,
  -- ACL (denormalized onto chunk metadata at index time)
  visibility    text NOT NULL DEFAULT 'x1',      -- 'private'|'x1'|'public'
  is_published  boolean NOT NULL DEFAULT true,
  eval_is_visible boolean,
  acl_source    jsonb,                           -- provenance for derived docs (max-restrictive)
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS documents_entity_idx
  ON advisor.documents (source_type, entity_type, entity_id);
CREATE INDEX IF NOT EXISTS documents_live_idx
  ON advisor.documents (source_type) WHERE superseded_by IS NULL;

CREATE TABLE IF NOT EXISTS advisor.doc_chunks (
  id            bigserial PRIMARY KEY,
  document_id   bigint NOT NULL REFERENCES advisor.documents(id),
  block_index   int NOT NULL,                    -- stable; the citation primitive
  granularity   text NOT NULL DEFAULT 'block',   -- 'block'|'record_summary'  (no 'sentence' in v1)
  text          text NOT NULL,
  page_number   int,                             -- parsed from '# Page N'; null for non-paged
  char_span     int4range,
  metadata      jsonb NOT NULL DEFAULT '{}',     -- entity refs, stage, industry, region, ACL fields
  UNIQUE (document_id, block_index)
);

-- Lexical leg: native FTS (no BM25 extension on Cloud SQL; tuned per review §4.4)
CREATE INDEX IF NOT EXISTS doc_chunks_fts_idx
  ON advisor.doc_chunks USING gin (to_tsvector('english', text));

-- one embeddings table per index_config, created by the harness:
--   CREATE TABLE advisor.emb_{config_id} (
--     chunk_id bigint PRIMARY KEY REFERENCES advisor.doc_chunks(id),
--     embedding vector({dim}) NOT NULL );
--   + HNSW index (or exact scan at current scale)

CREATE TABLE IF NOT EXISTS advisor.entity_profiles (
  entity_type   text NOT NULL, entity_id bigint NOT NULL,
  document_id   bigint NOT NULL REFERENCES advisor.documents(id),
  content_hash  text NOT NULL,
  rendered_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (entity_type, entity_id)
);

CREATE TABLE IF NOT EXISTS advisor.threads (
  id bigserial PRIMARY KEY, user_id bigint NOT NULL,
  title text, created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS advisor.turns (
  id bigserial PRIMARY KEY,
  thread_id bigint NOT NULL REFERENCES advisor.threads(id),
  role text NOT NULL,                            -- 'user'|'assistant'
  content text NOT NULL,
  research_record jsonb,                         -- evidence ids, tool calls, citations, cost
  cost_usd numeric(10,6),
  created_at timestamptz NOT NULL DEFAULT now()
);
