# Mouseion

A personal Mouseion — self-hosted paper library with LLM tagging, semantic search, and an AI examiner that tests whether you actually understood what you read.

Upload a PDF or paste an arXiv URL from desktop or phone; the library ingests,
tags, summarizes and indexes it, then lets you browse, search, ask questions
about it, and be examined on it.

- **[CLAUDE.md](CLAUDE.md)** — the system overview. Authoritative.
- **[PROJECT_NOTES.md](PROJECT_NOTES.md)** — what is built so far, known gaps,
  and where the next phase hooks in.

## Status

Phases 1-4 of 5 are done. Ingest works end to end, and the library supports
full-text and semantic search, descendant-aware topic browsing, grounded QA
over one paper or the collection, and persisted examiner sessions with rubric
scores, misconceptions, and sections to reread. Open WebUI provides the QA and
voice-ready test-mode chat surfaces. Phase 5 will focus on phone/PWA capture and
deployment hardening.

## Quickstart

```bash
cp .env.example .env    # set API_TOKEN and OPENROUTER_API_KEY
make up                 # library at http://localhost:8000/
make webui              # optional QA/test chat at http://localhost:3000/
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
