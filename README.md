# Mouseion

A personal Mouseion — self-hosted paper library with LLM tagging, semantic search, and an AI examiner that tests whether you actually understood what you read.

Upload a PDF or paste an arXiv URL from desktop or phone; the library ingests,
tags, summarizes and indexes it, then lets you browse, search, ask questions
about it, and be examined on it.

- **[CLAUDE.md](CLAUDE.md)** — the system overview. Authoritative.
- **[PROJECT_NOTES.md](PROJECT_NOTES.md)** — what is built so far, known gaps,
  and where the next phase hooks in.

## Status

Phases 1-2 of 5 are done. Ingest works end to end, and the library is a usable
tool on its own: full-text search with highlighted snippets, a topic tree that
filters by descendants, a paper detail page with reading status and markdown
  notes, and a taxonomy editor with create / rename / re-parent / split / merge /
  delete. QA (Phase 3) and
test mode (Phase 4) are not wired yet.

## Quickstart

```bash
cp .env.example .env    # set API_TOKEN and OPENROUTER_API_KEY
make up                 # library at http://localhost:8000/
```

```bash
make test
```

| Where | What |
| --- | --- |
| `/` | library: search, filters, topic sidebar, upload |
| `/papers/{id}` | one paper: metadata, summaries, topics, status, notes, PDF |
| `/taxonomy` | rename / re-parent / merge topics |
| `/docs` | OpenAPI (needs the bearer token) |

Every variable the stack reads is documented in
[.env.example](.env.example). The API requires a bearer token on all `/api/*`
and `/ui/*` routes; `/health` and the (data-free) page shells are public — the
UI holds the token in localStorage and attaches it to every request.
