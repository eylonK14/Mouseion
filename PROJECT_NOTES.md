# Project notes — complete system, decisions, and operating limits

`CLAUDE.md` is the spec. This file is the delta: what was built, the decisions
that are not obvious from the code, and the stable seams future work should
use. One section per phase, newest last.

* [Phase 1 — foundation](#phase-1--foundation) (schema, ingest, bare list view)
* [Phase 2 — search & browse UI](#phase-2--search--browse-ui)
* [Phase 3 — grounded QA](#phase-3--grounded-qa)
* [Phase 4 — test mode](#phase-4--test-mode)
* [Phase 5 — phone capture and production](#phase-5--phone-capture-and-production) (current)

---

# Phase 1 — foundation

> Historical. Section 5 below ("Where Phase 2 hooks in") describes work that is
> now done; it is kept because it explains *why* Phase 2 is shaped the way it
> is. Where Phase 2 diverged from the plan, the Phase 2 section says so.

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

---
---

# Phase 2 — search & browse UI

The library is now usable on its own: search, browse, read, annotate, and fix
the taxonomy by hand. No LLM feature was added and no migration was needed —
migration 0001 already had every table and the FTS triggers were live from day
one, so `GET /api/search` worked against papers ingested in Phase 1 with zero
backfill.

## 1. What was added

```
backend/src/mouseion/
  api/
    search.py       GET /api/search  ← the response shape Phase 3 reuses
    topics.py       tree / create / rename+reparent / split / merge / delete
    notes.py        notes CRUD, nested under the paper
    ui.py           page shells (public) + /ui/* HTMX fragments (protected)
    papers.py       + PATCH, + /pdf, + paper↔topic add/remove
  services/
    search.py       FTS5 query builder, one parametrised query, snippet safety
    notes.py        notes rows
    taxonomy.py     + descendants/ancestors, counted tree, rename,
                    reparent (cycle-safe), merge, paper↔topic links
frontend/
  templates/        base, library, paper, taxonomy + 9 partials
  static/app.css    palette, components, and a no-CDN fallback
  static/app.js     token, toasts, ingest polling, PDF blob, drag-to-merge
backend/tests/      + test_search.py, test_taxonomy_edit.py, test_api_phase2.py
```

`frontend/index.html` (the Phase 1 list view) is gone. `jinja2` is the only new
runtime dependency.

## 2. HTMX, not React+Vite — and why

Phase 1 left **zero** frontend tooling: no node, no bundler, no `package.json`,
and `docker-compose.yml` mounts `./frontend` read-only into a Python-only
image. React+Vite would have meant a node build stage, a `dist/` artifact and a
second dependency universe for a single-user app. HTMX cost one pure-Python
package and reuses the service layer the JSON API already needed.

It also made the "reusable result card" a *server* component
(`templates/partials/paper_card.html`) that Phase 3's QA views include
directly, instead of a React component that pipes written in Python cannot
touch.

The parts HTMX does not cover — debounced search is native, but autosave,
toasts, drag-to-merge and the PDF viewer are not — are ~200 lines of vanilla JS
in `static/app.js`, with no build step.

**The cost, stated plainly:** Tailwind comes from the Play CDN
(`cdn.tailwindcss.com`), which prints a console warning and needs the network.
`static/app.css` carries the palette and a fallback for the essentials so an
offline box still gets a legible library, but **vendoring a built Tailwind CSS
is a Phase 5 hardening item.**

## 3. Decisions you would otherwise have to reverse-engineer

**The search response shape is frozen.** `SearchHit` is `{paper, score,
snippet}` — a wrapper, not extra fields on `PaperOut` — precisely so Phase 3
can hand the identical structure to "papers consulted" with `score` carrying
retrieval confidence instead of BM25. `score` is `-bm25(...)`, so **higher is
better**; it is comparable within one result set, not across queries.

**Every term in a user's query is quoted.** `build_fts_query` turns `AND`,
`NEAR`, `-`, `*`, `^`, `:` and unbalanced quotes into literals, so typing into
the search box can never produce an `OperationalError`. The last *bare* term
gets a `*` for search-as-you-type; a `"quoted phrase"` never does, because
quoting is an explicit request for that wording.

**Snippets are escaped before they are marked.** `snippet()` is asked for the
C0 sentinels `\x02`/`\x03`, not `<mark>`; `services/search.py::highlight` HTML-
escapes the whole string and only then swaps them. The surrounding text is
arbitrary PDF content, so this is the difference between a highlight and an
injection. The JSON API returns the escaped form too — the field is documented
as safe HTML.

**The topic scope CTE is depth-bounded** (`MAX_TOPIC_DEPTH = 32`). The API
refuses to create a cycle, but a recursive CTE over one does not terminate, and
the query does not take an application-level invariant on trust. The Python
tree walks in `taxonomy.py` are cycle-safe for the same reason.

**Descendant counts de-duplicate.** A paper tagged with both "Transformers" and
its parent "Architectures" counts once under "Architectures". That is why
`build_topic_tree` rolls up sets of paper ids in Python rather than summing
child counts in SQL.

**Merging a parent into its own child needed a specific fix.** `topics.parent_id`
is `ON DELETE SET NULL`, so deleting the source would orphan its children after
they had just been re-parented onto the target. `merge_topics` splices the
target into the source's place *first*. There is a regression test named after
this.

**Splitting is explicit paper reassignment, not text inference.** The taxonomy
editor creates a new peer topic under the same parent and moves only the
directly tagged papers the owner selects. Descendant topics stay under the
source. This keeps taxonomy correction deterministic and avoids another LLM
interpretation step. Deleting a topic removes its direct paper links but never
deletes papers; its children are promoted to its parent so a whole subtree
cannot disappear accidentally.

**Status transitions are unrestricted.** All twelve moves between the four
statuses are allowed, including backwards — re-reading a paper is a normal
thing to do. The validation is the Pydantic literal (422 on anything else) plus
the paper existing. This is a deliberate non-implementation of a state machine,
not an oversight; `test_every_status_transition_is_allowed` pins it.

**Notes are NOT in the FTS index**, answering the open question Phase 1 left. A
note is the reader's own words about a paper; indexing it would let the note
outrank the paper for the very terms the reader chose. Adding it later is a new
FTS column plus triggers in a new migration —
`test_notes_are_not_indexed_for_search` will fail loudly first.

**Page shells are public, fragments are not.** `/`, `/papers/{id}` and
`/taxonomy` render without touching the database — `/papers/{id}` deliberately
does not look the paper up, so being public cannot even confirm which ids
exist. `auth.py` grew `PUBLIC_PATTERNS` (a regex list) for `/papers/\d+`;
matching by prefix would have exposed anything a later phase mounts under it.
This is why `/?topic_id=N` does not resolve the topic *name* server-side —
app.js fills the filter chip's label from the authenticated topic tree instead.

**The PDF viewer fetches, it does not link.** `/api/papers/{id}/pdf` is behind
the bearer token and an `<iframe>` cannot send a header, so `app.js::loadPdf`
fetches with the token and points the frame at a `blob:` URL (revoked on
`pagehide`). Loading is on click, because a 64 MB paper should not be pulled on
every detail-page view.

**Paper addition is a fault-isolated client-side batch.** The add dialog accepts
multiple PDFs plus newline-separated URLs in the same submission. The browser
starts one existing `POST /api/papers` request per item, so every paper retains
its own resumable job, progress toast, duplicate handling, and error. A bad item
does not prevent the rest from being queued; identical pasted URL lines are
collapsed before submission.

**Topic filtering is wired by delegation, not `onclick`.** Jinja's `tojson`
escapes `<`, `>`, `&` and `'` but **not** `"`, so
`onclick="…setTopic(1, {{ name|tojson }})"` produced an unparseable attribute
for every topic name. Names now travel in `data-topic-name` (autoescaped) and
`app.js` listens on `document` for `[data-topic-filter]`. Worth remembering
before writing another inline handler that takes user data.

**Blank query params mean "unset".** An HTML form submits every control it
holds, so an untouched filter arrives as `?status=&year_from=`. `BlankableInt`
/ `BlankableStatus` in `api/models.py` coerce `""` to `None`. Two traps here:
FastAPI **silently drops the `BeforeValidator`** when the parameter is spelled
`x: T = Query(...)` instead of `x: Annotated[T, Query(...)]` — use the
Annotated form — and this bites `Literal` types specifically.

## 4. What was actually verified, and how

Run against a seeded 300-paper library (real SQLite, real HTTP, real browser).

| Acceptance item | Status |
| --- | --- |
| Searching "quantum" highlights snippets in <100 ms on a few hundred papers | ✅ 20-26 ms end-to-end for `/api/search`, 19-38 ms for the rendered fragment |
| BM25 puts title matches above passing full-text mentions | ✅ top 5 for "quantum" were all title hits out of 226 matches |
| Clicking a mid-tree topic shows descendant papers | ✅ "Architectures" → 119 papers, 18 of the first 25 tagged only with a grandchild |
| Counts include descendants, de-duplicated | ✅ ML = 205, not the 249 its children sum to |
| Full reading flow persists a refresh | ✅ status → `understood`, note → markdown, topic added; all three survived reload |
| Merge relinks, tree updates, counts correct | ✅ Surface Codes (32) → Stabiliser Codes (37) = 63, i.e. 26 relinked + 6 already-both; parent total unchanged at 89 |
| Usable at ~390 px | ✅ `scrollWidth == clientWidth == 390` on library, detail and upload dialog; sidebar is an overlay drawer that closes on selection |
| Markdown preview | ✅ headings/lists/bold/code render; note text is escaped into the textarea |
| PDF viewer | ✅ authenticated fetch → `blob:` iframe |
| Ingest toasts | ✅ "sending…" immediately, then a red toast carrying the server's detail; a completed job turns green and refreshes both the list and the tree |
| `make test` green | ✅ 189 passed (was 92 after Phase 1) |

**Two bugs were found by running it, not by the tests**, and both are now
covered: the `tojson` attribute break, and blank form values 422-ing the whole
library view on first paint.

**Not verified here: `make up`.** Still no Docker on this machine (unchanged
from Phase 1). A live ingest was not run end-to-end either — there is no Redis
here, so `POST /api/papers` returns 503 at the enqueue step. The *client* side
of that flow was exercised against a real 503 and against a hand-seeded
completed job row.

## 5. Known gaps

1. **Tailwind is a CDN dependency** (see §2). Vendor a built stylesheet in
   Phase 5.
2. **No optimistic UI.** Every mutation is a server round trip that swaps a
   fragment. At single-user scale on a LAN this is imperceptible; over a slow
   phone connection the note autosave receipt will lag.
3. **The topic picker on the detail page is a flat `<select>`** of every topic.
   Past a few hundred topics that becomes unusable and wants a typeahead.
4. **Pagination is offset-based.** Fine at this size; a keyset cursor would be
   the fix if the library ever gets large.
5. **`/ui/*` returns HTML for errors as JSON `{"detail": ...}`** (FastAPI's
   default handler). `app.js` parses it out of the `htmx:responseError` event,
   so the user sees a toast, but the fragment itself does not re-render.
6. **`POST /api/topics` was not in the Phase 2 build list.** It exists because
   the taxonomy page cannot reorganise a hierarchy it has no way to add a node
   to. It is the only endpoint here that was not asked for.
7. Everything still open from Phase 1 §4 (PageIndex opt-in, `EMBEDDING_DIM`
   baked into migration 0001, worker runs as root, no backups).

## 6. Where Phase 3 hooks in — exact files

Phase 3 is "QA: paper-level vectors + two-stage retrieval + Open WebUI pipes".

**The result card.** `frontend/templates/partials/paper_card.html` exports a
Jinja macro:

```jinja
{% from "partials/paper_card.html" import paper_card %}
{{ paper_card(hit, show_score=true) }}
```

`hit` is `{paper, score, snippet}` — the `SearchHit` shape — regardless of
whether it came from BM25 or from vector retrieval. `show_score=true` surfaces
the number, which is what retrieval debugging wants. Render "papers consulted"
with this and a citation looks exactly like a search result, because it is one.
`status_badge(status)` is exported from the same file.

**The search endpoint is the retrieval-debugging surface.** `GET /api/search`
already returns ids + scores; `services/search.py::search_papers` takes a
`SearchQuery` dataclass. A hybrid ranker should produce `SearchHit`s and reuse
`api/search.py::to_hits` (which batches the topic lookup — do not reintroduce
N+1 there).

**The two disabled buttons.** `frontend/templates/partials/paper_detail.html`,
in the block commented "Phase 3 / Phase 4 seams". Each carries:

```html
data-openwebui-url="{{ openwebui_base_url }}"
data-chat-prefix="[paper:{{ paper['id'] }}]"   {# or [test:{id}] #}
```

`openwebui_base_url` comes from `Settings.openwebui_base_url`
(`OPENWEBUI_BASE_URL`, in `.env.example`). Phase 3 removes `disabled` and turns
the click into a deep link that opens a chat pre-filled with the prefix; the
pipe parses `[paper:{id}]` off the front of the message to know which paper the
conversation is about. `test_paper_fragment_shows_the_phase_3_and_4_seams`
pins the attributes so a rename cannot silently break the pipes.

**Adding a page.** A new UI surface is: a shell route in `api/ui.py` (public,
data-free — and add it to `auth.py::PUBLIC_PATHS` or `PUBLIC_PATTERNS`), a
template extending `base.html`, and `/ui/*` fragments that call services. Give
each fragment container `hx-trigger="load, mouseion:auth from:body"` so it
re-fetches once the user supplies a token.

**Client hooks already in `app.js`.** `Mouseion.api(path, options)` (bearer
token attached), `Mouseion.toast(msg, kind, opts)`, `Mouseion.watchJob(jobId,
label)`, `Mouseion.setTopic(id, name)`, `Mouseion.renderMarkdown(src)` — the
last is a small, escape-first Markdown subset, and is what a QA answer should
render through rather than adding a CDN parser.

**Do not** add a second HTTP client, a second templates environment, or a
second way to talk to the API. `services/llm.py`, `api/ui.py::get_templates`,
and `Mouseion.api` are the single instances of each.

---

# Phase 3 — grounded QA

Mouseion now answers grounded questions over the collection, a topic subtree,
or one paper. Open WebUI remains a stock chat surface: its two Pipe Functions
only manage scope and relay the backend's SSE stream. The paper detail page has
both an Open WebUI handoff and an inline no-context-switch question box.

## 1. What was added

```
backend/migrations/versions/0002_qa_log.py
backend/src/mouseion/
  api/qa.py                 collection + paper SSE; title-resolution helper
  services/qa.py            hybrid retrieval, grounding, budgets, citations/log
  services/llm.py           OpenRouter streaming + aggregate token telemetry
  services/prompts.py       tree-navigation + grounded synthesis prompts
pipes/
  library_qa.py             📚 Paper Library wrapper
  single_paper.py           📄 Single Paper wrapper
  common.py                 hot-reloaded tag/lock/SSE transport implementation
frontend/
  templates/partials/paper_detail.html   enabled handoff + inline QA form
  static/app.js                         authenticated SSE rendering
```

Every QA setting is environment-backed and listed in `.env.example`:
`QA_CANDIDATE_K` (8), `QA_MAX_NAVIGATIONS` (4),
`QA_SECTIONS_PER_PAPER` (3), and hard tree/per-paper/total context plus history
count/character budgets.

## 2. Retrieval and grounding decisions

**Stage 1 is true hybrid retrieval.** The question is embedded by the same
cached local `Embedder` ingest uses. vec0 produces paper ids ordered by
distance; FTS5 independently preserves literal/acronym/author matches. The two
rank lists are merged with reciprocal-rank fusion (`k=60`) so their unrelated
score scales never get mixed directly.

**Topic scopes are controlled taxonomy subtrees.** `[topic:Cryptography]` and
numeric ids resolve through the existing case-insensitive taxonomy service.
Because migration 0001's vec0 table has no topic metadata column, a scoped
query asks vec0 for the full distance ordering, then filters it through the
cycle-safe descendant id set. That is deliberate: filtering a global top 8
would miss an in-scope paper ranked ninth overall.

**Stage 2 is a bounded MODEL_QA operation.** `tree_indexer.navigate` retains
its tree/question/limit seam but is now async and asks the configured QA model
for validated stored node ids. At most `QA_MAX_NAVIGATIONS` candidate papers
reach this step. Bad/invented ids are dropped; an empty valid selection falls
back to the old lexical ranker. PDFs are re-extracted only for the selected
page ranges; missing PDF/tree data falls back to stored full text/abstract.

**The budget removes papers before mutilating evidence.** Each paper is
bounded first. If the total is still too large, the lowest RRF score is evicted
until it fits. Only when one paper alone exceeds the hard total cap is that
paper's final section trimmed. Tests pin the weakest-first behavior.

**Grounding is enforced twice.** `qa/v1` tells the synthesizer that excerpts
are data, prior chat is not evidence, citations must be exact
`[PaperTitle §Section]`, missing answers must say “The answer is not in your
library,” and disagreements must remain disagreements. After streaming, a
deterministic citation check flags every cited title that was absent from the
actual context; warnings ship in the final SSE metadata and `qa_log`.

## 3. API, pipes, and UI contracts

- `POST /api/qa/collection`: `question` or a messages-only conversation plus
  optional `topic` (id or exact name). Streams `metadata`, `token`, `done`, and
  readable `error` SSE events.
- `POST /api/qa/paper/{id}`: same conversation contract, hard-scoped to one
  paper.
- `POST /api/qa/paper/resolve`: backend-owned fuzzy title resolution for the
  thin single-paper pipe. A clear hit must exceed both an absolute and a
  runner-up margin; otherwise the pipe lists choices.
- Initial/final metadata contains consulted paper ids, exact titles, RRF
  scores, exact section labels, and a nested frozen `SearchHit` card payload
  built through `api/search.py::to_hits` (one paper query plus one batched topic
  query). Final metadata also carries citation warnings and the `qa_log` id.
- Open WebUI function ids are `paper_library` and `single_paper`. The latter
  finds the first explicit `[paper:N]` anywhere in retained user messages, so
  the lock survives follow-ups. If there is no tag, the first user message is
  resolved once deterministically on every request from retained history.
- The detail button uses Open WebUI's `model` and `q` URL parameters. The
  inline form stays on the paper page, calls the same paper endpoint through
  `Mouseion.api`, and renders tokens with the existing escape-first Markdown
  renderer.

Installation and Valve configuration are exact in `pipes/README.md`. The
compose bind mount is `/app/backend/data/mouseion-pipes`; installed wrappers
reload `common.py` on every request, so transport edits are live without an
image rebuild.

## 4. Cost visibility

Migration `0002` adds `qa_log`. One completed/failed stream records scope,
paper/topic, configured/returned model, aggregate prompt/completion/total
tokens across navigation retries and synthesis, end-to-end latency,
candidate/navigation counts, consulted-section JSON, citation warnings, and a
readable error. Streaming requests ask OpenRouter for the final usage chunk;
providers that omit it still record the model, call, and latency with zero
token fields instead of fabricating counts.

## 5. Verification and known limits

- Baseline before edits: **197 passed**.
- Required focused coverage includes RRF, real vec0 topic restriction,
  weakest-first context eviction, MODEL_QA tree selection, citation post-check,
  all specified pipe tag cases, conversation lock persistence, title
  resolution, multi-paper metadata, empty-library abstention, SSE, prompt
  golden, and `qa_log` usage.
- The complete suite is run with a workspace-local pytest temp directory on
  this Windows sandbox; AppData temp is not writable here. Final result:
  **222 passed, 1 third-party Starlette/httpx deprecation warning**.
- Docker Desktop's CLI is not on this shell's `PATH`, but its explicit binary
  path was used for a live compose smoke test. API, worker, Redis, and Open
  WebUI are healthy; the API image contains the retry path, the mounted pipe
  contains the tag-only lock response, and the served JavaScript contains no
  built-in paper question.
- Semantic retrieval intentionally has no universal distance cutoff: embedding
  distance calibration varies by the configured local model. For a weak
  shortlist, the synthesis prompt must abstain based on the excerpts. An empty
  shortlist is short-circuited to the exact abstention sentence without paying
  for synthesis.

### Runtime hardening (2026-08-23)

- The paper-detail Open WebUI handoff now sends only `[paper:{id}]`. Open WebUI
  auto-submits its `q` URL parameter, so the Single Paper pipe acknowledges the
  lock without an LLM call and lets the owner write the first actual question.
- The shared OpenRouter client retries transient `httpx` transport failures up
  to `LLM_MAX_ATTEMPTS`, discarding its owned connection pool between attempts.
  Streaming retries are allowed only before the first answer token, preventing
  duplicate partial answers when a connection fails mid-stream.
- Tag-only scope messages are removed from forwarded chat history after their
  lock is captured, so follow-ups never send an empty `QAMessage` to the API.

## 6. Phase 4 hook points

**Do not duplicate paper grounding.** Call
`services/qa.py::gather_grounding_material_for_paper(conn, paper_id, question,
client=..., navigate_tree=...)`. Its `PaperGrounding` return carries the exact
title, both stored summaries, and `SectionGrounding` values with section text
and optional page bounds. Use `navigate_tree=False` when Phase 4 needs a cheap
summary/full-text rubric seed before a question exists.

**Tree access remains isolated.** Phase 4 should continue through
`tree_indexer.load_tree` / async `navigate`; never parse PageIndex JSON or
import PageIndex directly.

**Sessions already have a home.** `test_sessions` from migration 0001 stores
`paper_id`, transcript, rubric, score, and gaps. Add a service/repository around
that table; do not put examiner state in a pipe or add a parallel store.

**Model and prompt seams are unchanged.** Add grading/examiner prompts only in
`services/prompts.py`, route calls through `LLMTask.GRADING` / the one
`LLMClient`, and use the configured structured-output validation retry. The
still-disabled `[test:{id}]` detail-button data contract is reserved for the
Phase 4 examiner Pipe Function.

---

# Phase 4 — test mode

Mouseion now runs a structured explain-it-back exam for one paper. The backend
owns a persisted EXPLAIN → PROBE → VERDICT state machine; Open WebUI is still a
stock chat/voice surface, and the new pipe only resolves scope, carries a
session lock, relays examiner turns, and formats the final structured verdict.

## 1. What was added

```
backend/migrations/versions/0003_test_session_state.py
backend/src/mouseion/
  api/test_mode.py             start / reload / history / streaming turn
  services/examiner.py         state machine, grounding, grading, persistence
  services/prompts.py          + probe planning and verdict prompts
  services/schemas.py          + strict probe/rubric/verdict contracts
pipes/
  test_me.py                   🎓 Test me Open WebUI wrapper
  common.py                    + shared test tags/session lock/verdict renderer
frontend/
  templates/partials/understanding.html
  templates/partials/paper_detail.html   enabled test handoff + panel loader
  templates/partials/paper_card.html     latest-score status ring
  static/app.js                         test deep link + PDF page jump
backend/tests/test_examiner.py
backend/tests/fixtures/examiner_prompts.golden.txt
```

Three environment-backed controls were added and are all in `.env.example`:
`TEST_MAX_TURNS` (8), `TEST_SESSION_TTL_HOURS` (72), and
`TEST_UNDERSTOOD_THRESHOLD` (4).

## 2. Examiner and grounding decisions

**The state machine is explicit and stored.** Migration `0003` adds `phase`,
`turn_count`, `state_json`, `updated_at`, `expires_at`, and `completed_at` to
the Phase 1 `test_sessions` table. `state_json` contains only the bounded probe
plan, current probe index, and one-pushback flag. Transcript, rubric, overall
score, and gaps continue to use the columns reserved in migration `0001`.

**EXPLAIN is deterministic.** Starting a test validates that the paper and a
usable stored tree exist, creates the row immediately, and returns the fixed
opening request to explain the core idea, problem, and approach. No model call
is spent before the owner starts explaining.

**PROBE reuses Phase 3 paper grounding.** The initial explanation is passed to
`qa.py::gather_grounding_material_for_paper`; its async tree navigation selects
method/results/limitations evidence. Probe planning is a separate structured
`MODEL_QA` call. The Pydantic contract requires 2–3 probes, collective coverage
of methodology/results/limitations, and at least one marked explanation gap.

**Section grounding is enforced inside the retry loop.** `LLMClient.complete_json`
now accepts a validation context. Probe, misconception, and reread section
labels must exactly match the actual tree-section excerpts selected for that
call. An invented label is a Pydantic validation failure, so the normal
configured retry receives the bad reply plus its validation error. Pipes never
validate or repair model content.

**Vagueness gets one pushback, never an answer.** A short/explicitly uncertain
probe response receives one concrete request to answer the same question and
name the mechanism or evidence. The response identifies the section but does
not reveal its content. A second vague response advances to the next probe.

**VERDICT is isolated from probing.** Early “I’m done”, the configured user-turn
cap, normal probe exhaustion, and lazy session expiry all enter the same final
structured call. The validated result has three 1–5 rubric dimensions,
misconceptions with contradicting real sections, reread section refs, and a
1–5 overall score plus one-line summary. `rubric_json`, `score`, `gaps_json`,
and the final transcript are written before a stream exposes the result.

**Expiry is lazy and resumable.** There is no scheduler: an overdue active
session is finalized on its next `GET /api/test/{session_id}` or turn. Every
active turn refreshes the expiry. If a mobile stream drops after the update,
reloading the session id replays the persisted phase, transcript, current
question, or verdict.

## 3. API, pipe, and UI contracts

- `POST /api/test/{paper_id}/start` creates a session and returns its id plus
  opening question and initial snapshot.
- `POST /api/test/{session_id}/turn` emits named SSE `metadata`, `token`,
  optional structured `verdict`, `done`, and readable `error` events. Its final
  snapshot omits transcript to keep turn payloads short.
- `GET /api/test/{session_id}` is the replay/resume contract.
- `GET /api/test/paper/{paper_id}/sessions` returns history for non-HTML clients.
- The Open WebUI Function id is `test_me`. It accepts `[test:N]` (the reserved
  detail-button contract), `[paper:N]`, or a fuzzy title resolved through the
  same Phase 3 backend endpoint as Single Paper.
- A hidden `mouseion-test-session:N` marker in retained assistant content keeps
  the pipe stateless while preserving the backend session across follow-ups.
- Open WebUI's built-in call mode works without audio code. Optional ElevenLabs
  TTS is documented only as an Open WebUI setting; Mouseion does not integrate
  it directly.
- The detail page shows latest score/date, three rubric mini-bars,
  misconception count, expandable transcript/history, and reread targets.
  Targets with a tree page call the authenticated blob PDF viewer with
  `#page=N`; targets without page metadata remain plain section names.
- Library cards batch-fetch the latest completed session per visible paper and
  add a subtle score-colored ring around the existing reading-status chip.
- A latest score at or above `TEST_UNDERSTOOD_THRESHOLD` offers a one-click
  status change. It is server-rechecked and never automatic.

## 4. Verification and known limits

- Untouched Phase 3 baseline: **222 passed**.
- Phase 4 focused coverage includes normal transitions, two real-section
  probes, gap targeting, early done, max turns, expiry, one-time vague
  pushback, section-reference validation retry, persisted wrong-answer
  misconception, shared tag/session parsing, empty/many misconception Markdown,
  SSE verdicts, reload/history, migration columns, card rings, PDF page links,
  opt-in status, and suppression of Open WebUI title/tag helper tasks.
- Prompt changes were regenerated with `python -m tests.regenerate_golden` and
  the new examiner golden was inspected. Existing ingest and QA goldens did
  not change.
- Complete local suite: **240 passed**, with only the existing third-party
  Starlette/httpx deprecation and unwritable pytest-cache warnings.
- Test-mode LLM usage is not added to `qa_log`; that table deliberately logs QA
  requests, while persistent examiner outcomes live in `test_sessions`.
- Expiry is request-driven rather than a background sweep. An abandoned
  session that is never read again remains active in storage but cannot affect
  latest-score UI, which only considers completed rows.

### Live acceptance (2026-08-24)

Run through the installed **🎓 Test me** Function in the stock Open WebUI
container against real ingested paper 14, *Deep-TEMPEST: Using Deep Learning to
Eavesdrop on HDMI from its Unintended Electromagnetic Emanations*:

- Open WebUI sent the fixed explanation prompt, then three grounded probes: the
  actual emanation/capture mechanism, measured CER/robustness, and documented
  countermeasures. The first and third explicitly challenged claims skipped or
  contradicted by the deliberately wrong explanation.
- The final Markdown contained a 1/5 overall score, all three rubric rows, five
  misconceptions, and real refs including `§3 Unintended Electromagnetic
  Emanations of HDMI`, `§7.1 Robustness`, and `§7.2 Countermeasures`.
- The persisted verdict appeared immediately on the authenticated paper detail
  page with date, mini-bars, five-misconception count, session history, and
  reread links to PDF pages 3, 8, and 9. Clicking the first produced an
  authenticated `blob:` viewer URL ending in `#page=3`.
- The library card exposed the latest-score ring through the accessible title
  “Latest understanding score: 1/5”; reading status remained `to_read`.
- Open WebUI's visible **Voice mode** and **Voice Input** controls were present
  for the Test-me model; no Mouseion audio code or ElevenLabs call was needed.
- The run exposed that Open WebUI may call the selected model for auxiliary
  title/tag tasks. `common.py::is_auxiliary_request` now drops those bodies
  before they can create or advance a session, with a regression test.

## 5. Phase 5 hook points

**Phone resume.** Persist a returned session id alongside any phone test route;
`GET /api/test/{session_id}` is sufficient to restore the conversation. Do not
cache examiner phase or probe position in a service worker—the database row is
the authority.

**PWA capture.** The share target should keep using `POST /api/papers` and hand
the resulting job id to `Mouseion.watchJob`. Test mode requires a completed
paper plus stored tree, so capture should link to the paper after ingest rather
than trying to start an exam inside the share event.

**Mobile PDF reread.** `Mouseion.jumpToPdf(paperId, page)` is the stable browser
hook for reread links. A Phase 5 viewer replacement must preserve bearer-token
fetching, blob lifetime cleanup, and page-number navigation.

**Offline and retry semantics.** A dropped examiner stream may be followed by
a session reload before resubmitting an answer. If Phase 5 adds client-generated
turn ids for stronger retry idempotency, add a migration and enforce them in
`services/examiner.py`; do not deduplicate conversational text heuristically.

**Deployment hardening remains unchanged.** Vendor frontend assets, run
services non-root, add backups for the SQLite/PDF/tree volume, and keep all
three Open WebUI pipes as thin authenticated transports.

---

# Phase 5 — phone capture and production

Mouseion is now a complete five-phase system. Phase 5 closes the phone-capture
loop and adds the operational surfaces needed for a long-lived private
deployment without changing the Phase 1–4 domain boundaries.

## 1. Complete system map

```text
phone/desktop PWA ── HTTPS on the tailnet ──> FastAPI + Jinja/HTMX
       │                                          │
       │ share URL/text/PDF                       ├─ SQLite + FTS5 + sqlite-vec
       │ QR device pairing                        ├─ content-addressed PDFs
       │ QA/test deep links                       └─ stored paper trees
       │
       └──────────────── Open WebUI ── thin pipes ─┘
                                                   │
FastAPI ── arq ── Redis ── worker ── OpenRouter + local embeddings
                                                   │
nightly maintenance ── VACUUM INTO + PDF/tree snapshot ── off-site copy
```

The development stack remains in `docker-compose.yml`. The production stack is
`docker-compose.prod.yml`: Redis, one-shot migrations, API, worker, optional
Open WebUI, and an on-demand maintenance container. Mouseion data, Redis state,
and Open WebUI state use named volumes; completed snapshots bind-mount to the
host so restic/rsync and restore procedures do not depend on Docker internals.

## 2. Phone capture and authentication

- `manifest.webmanifest` registers Mouseion as a `multipart/form-data` Web
  Share Target for title, text, URL, and one PDF. `sw.js` captures the browser's
  POST, stages the values/file in IndexedDB, and redirects to the minimal
  `/share` confirmation page. Normal shell resources are cache-first; GET API
  requests are network-first; mutating API calls are never cached.
- `sharing.py` extracts an arXiv link from arbitrary share-sheet prose before
  falling back to the explicit URL. `/share` submits the exact existing
  `POST /api/papers` contract and delegates progress/retry/deep-link behavior to
  the extended `Mouseion.watchJob` callback seam.
- The desktop admin page creates a short-lived random pairing token and a QR
  data URI. Migration `0004` stores only its SHA-256 hash. The narrowly public
  `/api/pairing/consume` transaction marks a valid row used before returning
  the API token once; `/pair` removes the query string before making that call
  and then uses the one existing browser token setter.
- Shells remain public and data-free. `/ui/admin`, all library fragments, all
  jobs, and the share extraction/ingest routes require bearer auth.

## 3. Operations and durability

- `observability.py` installs JSON logging in the API and worker. Incoming
  `X-Request-ID` is validated or replaced, returned on the response, placed in
  context, and persisted on `ingest_jobs`/`qa_log` by migration `0004`. URLs are
  logged without query strings so pairing tokens are not copied into logs.
- `/health/full` checks a rollback-only SQLite write plus `quick_check`, FTS
  source/index counts, an actual local embedding and configured dimension,
  PageIndex availability, free disk, and OpenRouter through the one existing
  `LLMClient`. The authenticated admin fragment displays those checks with
  queue counts, failed-job retries, recent request ids, and daily/model QA
  token totals.
- The ingest route uses a modest, process-local sliding-window limit per client
  after auth and before multipart parsing. It is intentionally defense in
  depth, not a distributed quota; Tailscale and the bearer token remain the
  security boundary.
- `maintenance.py backup` checkpoints WAL, creates a consistent database with
  `VACUUM INTO`, copies PDFs/trees, writes a count manifest, atomically publishes
  the dated directory, and retains the configured number of days. Dry-run does
  no writes. `restore-drill` restores the latest snapshot to scratch, opens it
  through the real DB layer, and runs the local full-health checks without
  touching live data.
- `make reembed` selects only missing/stale rows by stored model name;
  `make reindex-fts` rebuilds FTS from the source tables. Restore and off-site
  procedures are in `BACKUPS.md`; Tailscale Serve, Open WebUI, and the first-run
  production checklist are in `DEPLOYMENT.md`.
- Tailwind 3.4.17 is compiled and checked in, HTMX 2.0.4 is vendored, and the
  production image runs as UID/GID 10001. Development alone overrides that
  user for compatibility with the historical host `./data` bind mount. No
  browser CDN or Node runtime is needed after checkout.

## 4. Verification at handoff

- Pre-change Phase 4 baseline: **240 passed**.
- Phase 5 tests cover messy arXiv shares, authenticated extraction, expiring
  single-use pairing, public-shell boundaries, manifest/service-worker assets,
  request-id persistence, rate limiting, stale-FTS health failure, backup
  dry-run and real snapshot/restore, FTS rebuild, and stale-only re-embedding.
- Complete local suite: **258 passed**. `git diff --check`, Python compilation,
  JavaScript syntax checks, manifest/YAML parsing, and environment-variable
  inventory checks are part of the final validation pass.
- The same **258 tests** pass from an isolated source copy with no `.git`,
  `.env`, `data/`, or virtual environment, importing only that copied source.
- At 390×844 browser emulation, the library and share page have no horizontal
  overflow. A fresh browser origin followed a one-time QR URL, removed the
  token query, loaded authenticated fragments without token entry, and a
  second consume returned HTTP 410.
- The Web Share Target contract and both URL/PDF ingest branches are exercised
  automatically. A real Android/iOS share-sheet install, a live OpenRouter
  ingest, and a clean-machine Docker production boot still require an HTTPS
  host with Docker, credentials, and a supported browser; those facilities
  were not available in the local verification environment.

## 5. Known limitations

- Browser support for Web Share Target is platform-dependent (best on
  Chromium/Android); unsupported browsers still have the normal upload dialog.
- Offline capture stages one most-recent share locally, but ingest itself is
  deliberately online-only and must reach the API, Redis worker, models, and
  source URL. There is no offline mutation queue.
- Pairing grants the single shared API token. There is no device list or
  per-device revocation; rotate `API_TOKEN` to revoke every paired browser.
- In-memory ingest rate limits are per API process and reset on restart. The
  supplied production topology intentionally runs one API replica.
- Full health loads the embedding model and calls OpenRouter, so it can be
  slower than the lightweight liveness endpoint. Container healthchecks use
  the lightweight endpoint; administrators invoke the full check explicitly.
- Local snapshots are not off-site backups until `scripts/backup.sh` uploads
  them with restic or an operator copies completed directories with rsync.

## 6. Ideas for later

- Per-device credentials with a paired-device/revocation screen.
- A durable multi-item offline capture queue with background-sync where the
  browser supports it.
- Backup encryption-key rotation checks and scheduled restore-drill reporting.
- Optional Prometheus/OpenTelemetry export built from the existing JSON request
  ids and cost summaries.
- A mobile-native PDF reader that preserves `Mouseion.jumpToPdf` authenticated
  blob and page-fragment semantics.
