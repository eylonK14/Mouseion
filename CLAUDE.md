# Paper Library — System Overview

## What this is
A single-user, self-hosted "smart paper library." The owner uploads research
papers (PDF or arXiv URL) from desktop or phone. The system ingests, tags,
summarizes, and indexes them, provides a browse/search web UI, LLM-based QA
over one paper or the whole collection, and a "test mode" where the owner
explains a paper back to an LLM examiner and gets graded.

## Architecture (3 services, 1 data volume)
1. **backend/** — FastAPI. Owns: ingest pipeline, taxonomy, search, QA
   orchestration, test-mode grading. ALL LLM calls go through OpenRouter
   (env: OPENROUTER_API_KEY), including PageIndex's internal calls.
2. **frontend/** — primary UI (upload, topic tree, search, paper detail,
   reading status, notes). Tailwind-based, styled to feel like Open WebUI
   (dark palette, same typographic family). Served as static build by the
   backend or its own container.
3. **Open WebUI** — stock container, chat surface only. Talks to the backend
   through custom pipes (Python) that live in this repo under `pipes/`.

## Storage
- PDFs on disk at `data/pdfs/<sha256>.pdf` (content-addressed; dedup by hash).
- Single SQLite DB at `data/library.db`:
  - `papers(id, sha256 UNIQUE, title, authors, year, venue, source_url,
     abstract, summary_short, summary_long, status, added_at)`
    - `status ∈ {to_read, reading, read, understood}` (default `to_read`)
  - `topics(id, name UNIQUE, parent_id NULLABLE → topics.id)` — hierarchy
  - `paper_topics(paper_id, topic_id)` — many-to-many
  - `notes(id, paper_id, content, created_at, updated_at)`
  - `test_sessions(id, paper_id, transcript_json, rubric_json, score,
     gaps_json, created_at)`
  - `ingest_jobs(id, kind, payload_json, state, error, created_at, ...)`
  - FTS5 virtual table over title/abstract/authors/summaries/full text
  - sqlite-vec virtual table: ONE embedding per paper (not per chunk),
    over `title + abstract + topic names + summary_long`
- PageIndex tree per paper stored as JSON at `data/trees/<sha256>.json`.

## Key design rules (do not violate)
- Topics are metadata, never directories. A paper can have many topics.
- Taxonomy drift control: the tagging prompt ALWAYS receives the current
  taxonomy and the model must pick existing topics OR explicitly propose a
  new one with a named parent. Never free-form tags.
- Ingest is asynchronous: API returns immediately with a job id; a
  background worker does extraction → PageIndex tree → tagging+summaries
  (one combined structured-output LLM call) → embedding → commit.
- Model routing via OpenRouter: cheap/fast model for tagging & summaries
  (env: MODEL_INGEST), frontier model for QA and grading (env: MODEL_QA).
- Embeddings are LOCAL: sentence-transformers (env: EMBEDDING_MODEL,
  default `sentence-transformers/all-MiniLM-L6-v2`). No embedding API calls.
- arXiv URLs: rewrite /abs/→/pdf/, fetch metadata from the arXiv API
  instead of LLM-extracting it. IEEE/paywalled: PDF upload only.
- Single user, but the API requires a bearer token (env: API_TOKEN) because
  it will be reachable from a phone over Tailscale.

## Conventions
- Python 3.12, fully type-hinted, Pydantic v2 models for all API and LLM
  I/O. LLM structured outputs validated with Pydantic; retry once on
  validation failure.
- Migrations with Alembic from day one.
- Tests: pytest; every phase ships tests for its core logic (LLM calls
  mocked; golden-file tests for prompt builders).
- Config via environment only (.env.example kept current).
- One docker-compose.yml at root runs everything.

## Phase roadmap (each phase = one Claude Code session)
1. Foundation: repo, schema, ingest pipeline, bare list view
2. Search & browse UI (FTS5, topic tree, paper detail, reading workflow)
3. QA: paper-level vectors + two-stage retrieval + Open WebUI pipes
4. Test mode (examiner pipe + rubric grading + history)
5. Phone capture (PWA share target), deployment hardening, backups