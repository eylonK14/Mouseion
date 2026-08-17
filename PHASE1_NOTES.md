# Phase 1 — what exists, what doesn't, where Phase 2 plugs in

`CLAUDE.md` is the spec. This file is the delta: what was built, the decisions
that are not obvious from the code, and the exact seams Phase 2 should use.

---

## 1. What exists

```
CLAUDE.md               authoritative system overview
docker-compose.yml      redis + migrate + api + worker (+ open-webui, profile "webui")
Makefile                up / down / logs / migrate / test / test-local
.env.example            every variable the stack reads, 24 of them
backend/
  migrations/versions/0001_initial_schema.py   the ENTIRE schema, incl. Phase 2-4 tables
  src/mouseion/
    config.py           env → typed Settings (cached)
    db.py               connections, WAL, sqlite-vec loading, transaction()
    auth.py             bearer middleware
    main.py             app factory; mounts frontend/ as static
    api/                papers.py, jobs.py, health.py, models.py
    services/
      hashing.py        sha256 → identity + dedup
      pdfs.py           content-addressed store, PyMuPDF extraction, outline
      arxiv.py          URL normalization + Atom parsing
      llm.py            THE OpenRouter client, per-task model routing
      schemas.py        Pydantic contracts for LLM structured output
      prompts.py        prompt builder (golden-file tested)
      taxonomy.py       topic canonicalization + transactional proposals
      tree_indexer.py   TreeIndexer interface, PageIndex + heuristic impls
      embeddings.py     local sentence-transformers → sqlite-vec
      papers.py/jobs.py row-level repositories
      ingest.py         the pipeline
      queue.py          enqueue side
    worker.py           arq worker (+ the queue-choice rationale)
  tests/                92 tests
frontend/index.html     the bare list view (one file, no build)
pipes/README.md         stub; Phase 3-4 fill it
```

### Running it

```bash
make up      # builds, migrates, starts; list view at http://localhost:8000/
make test    # pytest inside the api image
```

**Running without make** (this machine has neither `make` nor Docker):

```bash
cd backend && python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt
.venv/Scripts/pip install --no-deps -e .
.venv/Scripts/python -m pytest -q
```

---

## 2. What was actually verified, and how

| Acceptance item | Status |
| --- | --- |
| Migration 0001 applies; FTS triggers, vec0, NOCASE uniqueness all work | ✅ run against real SQLite |
| Upload → job progresses → paper in list with topics + both summaries | ✅ full pipeline, LLM mocked |
| Re-upload → duplicate short-circuit | ✅ stops after `download`, zero LLM calls |
| arXiv URL → API-sourced metadata beats the model's | ✅ `test_arxiv_url_ingest_uses_api_metadata_over_the_model` |
| New-topic proposal lands under the right parent; second paper reuses it | ✅ tests + live demo |
| Same name in different case reuses instead of duplicating | ✅ `ATTENTION mechanisms` → existing topic |
| Failed job resumes from last completed step | ✅ resumes at `embed`, LLM not re-called |
| Bearer auth on everything except /health | ✅ live server, incl. `/openapi.json` → 401 |
| List view renders title/authors/year/topics/status/summary | ✅ live browser |
| `make test` green | ✅ 92 passed |

**Not verified here: `make up` itself.** This machine has no Docker and no
`make`, so the compose file was validated by parsing (services, anchors,
dependency conditions, profiles) but never started. The image build is the one
thing in Phase 1 that has not been executed — run `make up` once and expect the
first build to be slow (CPU torch wheel + sentence-transformers).

Everything else ran for real: the migration, the pipeline, the API, the UI.

---

## 3. Decisions you would otherwise have to reverse-engineer

**`papers.authors` is a `"; "`-joined string.** CLAUDE.md specifies one
`authors` column; keeping it a plain string is what lets the FTS triggers index
it verbatim. Split/join lives in `services/papers.py::split_authors` /
`join_authors` and nowhere else — the API always speaks lists.

**Full text lives in `paper_texts`, not in `papers`.** It is megabytes per row
and every list query would otherwise drag it through the page cache. Its own
FTS triggers keep the index in sync (`paper_texts_ai/au/ad`).

**`paper_embeddings` is a companion table to the vec0 table.** vec0 metadata
column support varies by sqlite-vec release, and a plain table is what makes
"which papers were embedded with an old model?" an ordinary query —
`embeddings.py::papers_with_stale_embeddings`.

**Case-insensitive topic names are enforced by the schema** (`COLLATE NOCASE
UNIQUE`), not only by service code, so a duplicate cannot be created even by a
direct SQL write.

**The UI shell is exempt from auth; the API is not.** A browser cannot attach an
`Authorization` header to a top-level navigation, so a protected shell would be
unreachable. `/`, `/index.html`, `/favicon.ico`, `/static/*` and `/health` are
public; every `/api/*` route plus `/docs` and `/openapi.json` require the token.
The shell contains no library data — it prompts for the token and sends it on
each API call. See `auth.py`, which documents this in place.

**Tree-index failure is non-fatal.** A paper without a PageIndex tree is still
worth having; the job records a warning and continues. Phase 3 can rebuild trees
for papers that lack one.

**`POST /api/jobs/{id}/retry` exists** though it is not in the build list.
"Failed jobs resumable from last completed step" needs a trigger to be usable;
this is it.

**arq, and why** — see the module docstring in `worker.py`. Short version: the
pipeline is async I/O, so the worker runs the same `run_ingest` coroutine the
tests call directly. `max_tries = 1`; retry semantics live in the job row.

---

## 4. Known gaps

1. **PageIndex is opt-in, not installed by default.** The PyPI `pageindex`
   release is the cloud SDK; the local vectorless tree builder is the git
   version, which drags litellm + openai-agents. `requirements-pageindex.txt`
   pins it to a specific commit; install it and `TREE_INDEXER=auto` picks it up.
   Until then the `HeuristicTreeIndexer` (bookmarks → headings → page blocks)
   runs. **The OpenRouter routing for it is written but has never executed
   against the real library** — `PageIndexTreeIndexer._run` sets
   `OPENAI_API_KEY`/`OPENAI_BASE_URL` and passes `openai/$MODEL_INGEST` (the
   prefix keeps PageIndex on its OpenAI-SDK path instead of litellm, which would
   ignore the base URL). Verify this the first time you enable it.
2. **`EMBEDDING_DIM` is baked into migration 0001.** Changing the embedding
   model to one with a different dimension needs a new migration that rebuilds
   `paper_vectors`. The dimension is read from settings at migration time.
3. **No search endpoint yet** — the FTS index is populated and live, but nothing
   queries it. That is Phase 2's first task, by design.
4. **`notes` and `test_sessions` are empty tables.** Created now to avoid
   migration churn; no code touches them.
5. **The worker runs as root and there is no backup story.** Both are Phase 5
   ("deployment hardening, backups").
6. **`summary_short` "one sentence" is enforced loosely** (non-empty, ≤400
   chars). Tightening it risks failing ingests over punctuation.
7. **Local venv here lacks `sentence-transformers`** (torch is large and the
   tests stub the embedder). `make test` in Docker has the real thing.

---

## 5. Where Phase 2 hooks in

Phase 2 is "Search & browse UI (FTS5, topic tree, paper detail, reading
workflow)". Nothing below needs a schema change or a backfill.

**Search.** The FTS table is `papers_fts(title, authors, abstract,
summary_short, summary_long, full_text, paper_id UNINDEXED)`, `rowid =
papers.id`, tokenizer `porter unicode61`. Triggers are live from day one, so
every paper ingested in Phase 1 is already indexed.
→ Add `services/search.py` and a `GET /api/search` route in
`api/papers.py`. Rank with `bm25(papers_fts)`; snippets via
`snippet(papers_fts, ...)`.

**Topic tree.** `taxonomy.py::list_topics` returns `TopicRow(id, name,
parent_id)`; `render_taxonomy_tree` already walks it into a hierarchy — reuse
that walk for a JSON tree endpoint. `topics_for_papers` is the batched
paper→topics lookup that keeps the list view at two queries.
→ New router `api/topics.py`: `GET /api/topics`, `GET /api/topics/{id}/papers`.

**Paper detail.** `GET /api/papers/{id}` already returns everything except full
text and the tree. `papers.py::get_full_text` and
`tree_indexer.py::load_tree(paper_id, conn)` are the two additions.

**Reading workflow.** `papers.status` has the CHECK constraint and defaults to
`to_read`; `papers_repo.update_paper` already whitelists `status`.
→ Add `PATCH /api/papers/{id}` in `api/papers.py`. `PaperStatus` in
`api/models.py` is the literal type to validate against.

**Notes.** Table exists (`notes`). Add `services/notes.py` + routes; the FTS
table deliberately does **not** index notes — decide in Phase 2 whether it
should, and if so add a trigger in a new migration.

**Replacing the UI.** `frontend/` is served as static files by
`main.py::create_app` (`FRONTEND_DIR`, mounted last so API routes win). Drop a
real build in there; nothing else changes. Keep the localStorage token flow, or
move to a cookie and revisit `auth.py::PUBLIC_PATHS`.

---

## 6. Seams for Phases 3-5

| Phase | Seam | Location |
| --- | --- | --- |
| 3 | `load_tree(paper_id, conn)` → `TreeDocument`; `navigate(tree, question)` → ranked nodes. Phase 1 ranks lexically; replace the body with LLM descent, keep the signature. | `services/tree_indexer.py` |
| 3 | Vector shortlist: `paper_vectors` (vec0, `k=` KNN) + `paper_embeddings.model` for staleness. | `services/embeddings.py` |
| 3-4 | New LLM tasks: add to `LLMTask`, add a branch in `LLMClient.model_for`. Do **not** add a second HTTP client. | `services/llm.py` |
| 4 | `test_sessions(transcript_json, rubric_json, score, gaps_json)` is ready. | migration 0001 |
| 3-4 | Pipes talk to `http://api:8000` with the same bearer token. | `pipes/README.md` |

One rule worth keeping: **prompt changes go through `services/prompts.py` and
its golden file.** `python -m tests.regenerate_golden` rewrites it; the diff is
the review.
