# Mouseion

A personal Mouseion — self-hosted paper library with LLM tagging, semantic search, and an AI examiner that tests whether you actually understood what you read.

Upload a PDF or paste an arXiv URL from desktop or phone; the library ingests,
tags, summarizes and indexes it, then lets you browse, search, ask questions
about it, and be examined on it.

- **[CLAUDE.md](CLAUDE.md)** — the system overview. Authoritative.
- **[PHASE1_NOTES.md](PHASE1_NOTES.md)** — what is built so far, known gaps, and
  where the next phase hooks in.

## Status

Phase 1 of 5 (foundation: schema, ingest pipeline, bare list view). Ingest works
end to end; search, QA and test mode are Phases 2-4.

## Quickstart

```bash
cp .env.example .env    # set API_TOKEN and OPENROUTER_API_KEY
make up                 # http://localhost:8000/
```

```bash
make test
```

Every variable the stack reads is documented in
[.env.example](.env.example). The API requires a bearer token on all `/api/*`
routes; `/health` and the static UI shell are public.
