# AGENTS.md

## Project intent

Mouseion is a single-user, self-hosted research-paper library. The owner can add
a PDF or arXiv URL from desktop or phone; Mouseion ingests it asynchronously,
deduplicates it, extracts and indexes its contents, assigns controlled
hierarchical topics, writes summaries, and makes the collection searchable and
browsable. The longer-term goal is not only storage: Mouseion should support
grounded questions over one paper or the whole library and an examiner mode that
tests whether the owner can explain a paper and identifies gaps in understanding.

Read `CLAUDE.md` before making architectural changes. It is the authoritative
system specification. Read the newest section of `PROJECT_NOTES.md` before
starting phase work; it records what is implemented, deliberate decisions,
known gaps, verification history, and exact extension seams. Keep both documents
accurate when a change makes them stale.

## Current state and roadmap

Phases 1 through 4 are implemented:

- Foundation: SQLite schema, PDF/arXiv ingestion, resumable arq jobs, taxonomy,
  summaries, PageIndex-compatible trees, paper embeddings, and a basic API.
- Library UI: FTS5 search, descendant-aware topic browsing, paper detail,
  reading status, notes, taxonomy editing, and responsive HTMX/Jinja pages.
- Grounded QA: hybrid paper retrieval, bounded tree navigation, collection and
  single-paper SSE, Open WebUI Pipe Functions, inline paper QA, and `qa_log`
  model/token/latency visibility.
- Test mode: persisted EXPLAIN → PROBE → VERDICT sessions, grounded probes,
  validated rubric grading, Open WebUI voice-ready examiner transport,
  understanding history, and opt-in `understood` status offers.

The next planned work is:

1. Phase 5: phone/PWA capture, deployment hardening, vendored frontend assets,
   non-root services, and backups.

Do not describe planned Phase 5 behavior as already implemented.

## Repository map

- `backend/src/mouseion/main.py`: FastAPI app assembly and router registration.
- `backend/src/mouseion/config.py`: cached, typed environment configuration.
- `backend/src/mouseion/db.py`: SQLite connections, WAL/foreign keys,
  sqlite-vec loading, and transaction handling.
- `backend/src/mouseion/auth.py`: bearer-token middleware and narrowly defined
  public shell routes.
- `backend/src/mouseion/api/`: HTTP and HTML-fragment adapters. Keep business
  logic in services.
- `backend/src/mouseion/services/ingest.py`: resumable ingest orchestration:
  download/store, extract, tree-index, tag/summarize, embed, commit state.
- `backend/src/mouseion/services/llm.py`: the single OpenRouter client and model
  routing point for all LLM work.
- `backend/src/mouseion/services/prompts.py`: prompt construction; prompt output
  is protected by a golden-file test.
- `backend/src/mouseion/services/tree_indexer.py`: heuristic/PageIndex tree
  creation, persistence, loading, and the Phase 3 navigation seam.
- `backend/src/mouseion/services/embeddings.py`: one local embedding per paper
  and model-staleness metadata.
- `backend/src/mouseion/services/search.py`: safe FTS5 query building, filters,
  ranking, snippets, sorting, and pagination.
- `backend/src/mouseion/services/qa.py`: hybrid shortlist, tree-grounding,
  context budgets, citation checks, title resolution, and the Phase 4 paper
  grounding seam.
- `backend/src/mouseion/services/examiner.py`: explicit examiner state machine,
  grounded probe/verdict orchestration, session persistence, expiry, history,
  and reread page targets.
- `backend/src/mouseion/services/taxonomy.py`: controlled hierarchical topics,
  cycle-safe traversal/editing, split/merge/delete behavior, and paper-topic
  links.
- `backend/migrations/`: Alembic schema. Migration `0001` includes tables and
  virtual tables anticipated through test mode; `0002` adds QA cost logging;
  `0003` adds resumable examiner state to `test_sessions`.
- `backend/tests/`: pytest suite; LLMs and embeddings are mocked where needed.
- `frontend/templates/`: public data-free page shells and authenticated HTMX
  fragments, rendered by the backend.
- `frontend/static/app.js`: bearer-token handling, shared API wrapper, HTMX
  hooks, job polling, PDF loading, toasts, taxonomy interactions, and the small
  escape-first Markdown renderer.
- `frontend/static/app.css`: application styling and minimal offline fallback.
- `pipes/`: thin Open WebUI Pipe Functions for collection QA, single-paper QA,
  and test mode. Prompts, state transitions, grading, and LLM calls remain in
  the backend.
- `data/`: runtime SQLite database, content-addressed PDFs, and tree JSON. Treat
  it as user data, not source code; never delete or rewrite it casually.
- `docker-compose.yml`: Redis, one-shot migrations, API, worker, and optional
  Open WebUI profile.

## Architectural rules

- Topics are metadata, never directories. Papers may have multiple topics.
- Prevent taxonomy drift: ingestion prompts receive the current taxonomy and
  may select an existing topic or propose a new topic with an explicit parent.
  Never introduce uncontrolled free-form tags.
- Ingestion remains asynchronous, idempotent, and resumable. The API returns a
  job ID immediately; completed steps in `progress_json` must be safe to skip on
  retry. Tree-index failure is deliberately non-fatal.
- All LLM calls, including PageIndex-related calls, route through OpenRouter.
  Extend `LLMTask` and `LLMClient.model_for`; do not create another LLM client.
- Use `MODEL_INGEST` for inexpensive structured ingest work and `MODEL_QA` for
  QA/grading. Validate structured model output with Pydantic and retain the
  configured retry-on-validation-failure behavior.
- Embeddings are local sentence-transformers embeddings. Store one embedding
  per paper over title, abstract, topic names, and long summary—not chunks.
  `EMBEDDING_DIM` is part of the vec0 schema; changing dimensions requires a new
  migration that rebuilds the vector table.
- PDFs are content-addressed as `data/pdfs/<sha256>.pdf`; preserve hash-based
  deduplication. For arXiv, normalize `/abs/` to `/pdf/` and prefer arXiv API
  metadata over model-extracted metadata.
- SQLite is the system of record. Preserve foreign keys, case-insensitive topic
  uniqueness, FTS triggers, and the separation of large full text into
  `paper_texts`.
- Notes intentionally are not part of FTS. Do not change this without a new
  migration and an explicit product decision.
- Reading-status transitions are intentionally unrestricted among `to_read`,
  `reading`, `read`, and `understood`.
- The app is single-user but remotely reachable over Tailscale. All `/api/*`
  and `/ui/*` data routes require the bearer token. Public page shells must be
  data-free and public-path matching must remain narrow.
- The PDF viewer must fetch with the bearer token and use a blob URL; a direct
  iframe link cannot authenticate.

## API and UI conventions

- Use Python 3.12, full type hints, Pydantic v2 for API/LLM contracts, and
  parameterized SQL.
- Keep routers thin. Put reusable domain/database behavior in `services/`.
- Keep one Jinja environment (`api/ui.py::get_templates`) and one browser API
  path (`Mouseion.api`). Do not introduce parallel abstractions.
- This UI intentionally uses server-rendered Jinja + HTMX + vanilla JavaScript;
  there is no Node build. Do not add a SPA toolchain without an explicit
  architectural decision.
- Page shells are public and must not query private data. Authenticated `/ui/*`
  fragments populate them. New loadable fragments should respond to both
  `load` and `mouseion:auth from:body` where appropriate.
- Reuse `frontend/templates/partials/paper_card.html` for paper results.
  Retrieval results use the stable `{paper, score, snippet}` `SearchHit` shape.
  Search scores are `-bm25`, so higher is better only within a result set.
- Never put user-controlled values into inline JavaScript attributes. Use
  autoescaped `data-*` attributes and delegated handlers.
- Search snippets are safe HTML only because sentinel markers are inserted by
  SQLite, the entire snippet is escaped, and sentinels are then replaced with
  `<mark>`. Preserve that order.
- Blank HTML query parameters use the `Blankable*` annotated types. FastAPI
  parameters that depend on `BeforeValidator` must use `Annotated[..., Query]`.
- Reuse `Mouseion.renderMarkdown`; it is an intentionally small escape-first
  subset and avoids another CDN/parser dependency.

## Phase 5 extension seams

- Phone clients can start, advance, and reload examiner sessions through the
  short authenticated `/api/test/*` contracts. Reloading by session id replays
  persisted state; do not move active state into a browser, pipe, or worker.
- The share target should enqueue through the existing ingest API and hand
  progress to `Mouseion.watchJob`; ingestion remains resumable and async.
- Preserve test-session and reread page links when making the PDF reader more
  phone-native. `Mouseion.jumpToPdf` owns authenticated blob loading plus page
  fragments.
- Open WebUI pipes remain thin authenticated transport shims to
  `http://api:8000`. Voice/TTS is Open WebUI configuration, not backend or pipe
  code.

## Testing and verification

Run the smallest relevant tests while iterating, then the full suite before
handoff when practical:

```bash
make test
```

Without Docker/make, from `backend/`:

```bash
python -m pytest -q
```

Useful focused form:

```bash
python -m pytest -q tests/test_search.py
```

Prompt changes must go through `services/prompts.py`. Regenerate the golden
fixture intentionally with `python -m tests.regenerate_golden`, inspect the
diff, and run `tests/test_prompts.py`.

Add or update tests for behavioral changes, especially auth boundaries,
taxonomy cycles/merges, FTS query safety and escaping, ingest retry/idempotency,
API contracts, and Phase 3/4 seam attributes. Mock external LLM/arXiv calls in
tests. Never require real credentials for the suite.

For schema changes, create a new Alembic revision; do not edit migration `0001`
once a database may already exist. Verify both migration behavior and affected
service queries. For frontend changes, check authenticated and unauthenticated
states, HTMX swaps, error toasts, refresh persistence, and narrow-phone layout.

## Local operation and safety

- Configuration is environment-only; keep `.env.example` synchronized with
  every setting the application reads.
- `.env` contains secrets. Never print, commit, copy into docs, or expose its
  values. Use `.env.example` when inspecting configuration shape.
- Common commands are `make up`, `make down`, `make logs`, `make migrate`, and
  `make test`. Open WebUI is optional until its pipes exist:
  `docker compose --profile webui up -d`.
- Preserve unrelated work in the working tree. Inspect `git status` before and
  after edits and do not overwrite or revert user changes.
- Do not delete `data/`, SQLite files, PDFs, tree JSON, Docker volumes, or run
  `make clean` unless the user explicitly requests the destructive action and
  the exact scope is confirmed.
