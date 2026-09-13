# Mouseion

Mouseion is a single-user, self-hosted research-paper library that turns PDFs and arXiv links into a searchable, topic-organized collection. It adds grounded library QA and an explain-it-back examiner, while keeping PDFs, metadata, notes, and model usage on infrastructure you control. Its installable PWA makes saving a paper from a phone share sheet a two-tap action.

## Features

- PDF upload and arXiv capture with content-hash deduplication and resumable background ingestion
- Controlled hierarchical topics, summaries, full-text search, reading states, notes, and authenticated PDF reading
- Hybrid local-vector/FTS retrieval followed by tree-grounded QA with citations and token/cost logs
- Persisted examiner conversations with rubric scores, misconceptions, and exact sections to reread
- Installable dark-mode PWA, Android share target for links/text/PDFs, offline shell, and QR device pairing
- JSON logs with request IDs, ingest throttling, full dependency health, job retries, and an admin dashboard
- Consistent SQLite/PDF/tree snapshots, retention, restore drills, FTS rebuilds, and model-aware re-embedding
- Tailnet-only production topology with unprivileged containers and durable named volumes

## Architecture

Mouseion has three logical services and one durable library volume. Redis is an internal queue broker; a one-shot migration container upgrades SQLite before the API or worker starts.

```text
phone / desktop
       │ HTTPS + bearer token
       ▼
┌───────────────────────────────┐
│ Backend                       │
│ FastAPI/UI ─ Redis ─ worker   │
└──────────┬───────────────┬────┘
           │               │ internal HTTP/SSE
           ▼               ▼
  [Mouseion data volume]  [Open WebUI]
  SQLite + PDFs + trees    three thin Pipe Functions
           ▲
           │ served Jinja/HTMX/CSS/JS
       [Frontend]
```

All LLM calls—including PageIndex calls—go through OpenRouter in the backend. Embeddings run locally with sentence-transformers; Open WebUI contains no retrieval, prompting, grading, or model credentials beyond the Mouseion bearer token used by its pipes.

## Prerequisites

- Linux, macOS, or WSL with Git and GNU Make
- Docker Engine 24+ and Docker Compose v2.20+ (`docker compose version`)
- An [OpenRouter API key](https://openrouter.ai/keys)
- At least 8 GB RAM and 10 GB free disk; 16 GB RAM is comfortable if API, worker, and Open WebUI share one host
- A CPU is sufficient for the default 384-dimensional MiniLM embedding model. Its first download is roughly a few hundred MB and the first image build is slower because it installs CPU PyTorch.

PageIndex is optional. The default `TREE_INDEXER=auto` uses the built-in heuristic tree when the separately pinned PageIndex dependency is absent.

## Quickstart

The following POSIX-shell commands take a fresh machine from clone to a migrated, running stack. The prompt keeps the OpenRouter key out of shell history, and Docker generates a random API token.

```bash
git clone https://github.com/eylonK14/Mouseion.git
cd Mouseion
cp .env.example .env

read -rsp "OpenRouter API key: " MOUSEION_OPENROUTER_KEY && echo
MOUSEION_API_TOKEN="$(docker run --rm python:3.12-alpine python -c 'import secrets; print(secrets.token_urlsafe(32))')"
sed -i.bak "s|^API_TOKEN=.*|API_TOKEN=${MOUSEION_API_TOKEN}|; s|^OPENROUTER_API_KEY=.*|OPENROUTER_API_KEY=${MOUSEION_OPENROUTER_KEY}|" .env
rm .env.bak
unset MOUSEION_OPENROUTER_KEY MOUSEION_API_TOKEN

make up
docker compose ps
make first-paper
```

Open [http://localhost:8000](http://localhost:8000), paste the `API_TOKEN` from `.env` into the one-time browser prompt, and confirm that *Attention Is All You Need* appears. Migrations are automatic: `make up` waits for the migration service before starting the API and worker.

For grounded QA and examiner chat:

```bash
make webui
docker compose ps
```

Open [http://localhost:3000](http://localhost:3000), create the first Open WebUI account, then install and configure the three trusted local functions exactly as described in [pipes/README.md](pipes/README.md). The functions appear as **Paper Library**, **Single Paper**, and **Test me** models.

### Configuration reference

`.env.example` is executable documentation and stays synchronized with the application. Only `API_TOKEN` and `OPENROUTER_API_KEY` must be replaced for a functional stack; production should also set the two public browser URLs.

| Variable | Required | Default / source |
| --- | --- | --- |
| `API_TOKEN` | Yes | Generate with `secrets.token_urlsafe(32)` as in Quickstart. |
| `PAIRING_TOKEN_TTL_MINUTES` | No | `10`; one-time QR lifetime, 1–60. |
| `OPENROUTER_API_KEY` | Yes | Create at [OpenRouter Keys](https://openrouter.ai/keys). |
| `OPENROUTER_BASE_URL` | No | `https://openrouter.ai/api/v1`. |
| `MODEL_INGEST` | No | `anthropic/claude-haiku-4.5`; metadata, topics, summaries, PageIndex. |
| `MODEL_QA` | No | `anthropic/claude-opus-4.5`; QA navigation/synthesis and grading. |
| `OPENROUTER_APP_URL` | No | `http://localhost:8000`; attribution only. |
| `OPENROUTER_APP_TITLE` | No | `Mouseion`; attribution only. |
| `LLM_TIMEOUT_SECONDS` | No | `180`. |
| `LLM_MAX_ATTEMPTS` | No | `2`; validation and safe transport attempts. |
| `QA_CANDIDATE_K` | No | `8`; hybrid shortlist size. |
| `QA_MAX_NAVIGATIONS` | No | `4`; paper trees navigated per question. |
| `QA_SECTIONS_PER_PAPER` | No | `3`. |
| `QA_CONTEXT_BUDGET_CHARS` | No | `48000`. |
| `QA_SECTION_BUDGET_CHARS` | No | `12000`. |
| `QA_TREE_BUDGET_CHARS` | No | `16000`. |
| `QA_HISTORY_MESSAGES` | No | `12`. |
| `QA_HISTORY_BUDGET_CHARS` | No | `12000`. |
| `TEST_MAX_TURNS` | No | `8`. |
| `TEST_SESSION_TTL_HOURS` | No | `72`. |
| `TEST_UNDERSTOOD_THRESHOLD` | No | `4` out of 5; status change is still opt-in. |
| `EMBEDDING_MODEL` | No | `sentence-transformers/all-MiniLM-L6-v2`; local download. |
| `EMBEDDING_DIM` | No | `384`; changing dimension requires a vector-table migration. |
| `TREE_INDEXER` | No | `auto`; alternatives are `heuristic` and `pageindex`. |
| `PAGEINDEX_MODE` | No | `flash`. |
| `DATA_DIR` | No | `/app/data` in Compose. |
| `DB_PATH` | No | `/app/data/library.db` in Compose. |
| `BACKUP_DIR` | No | `/app/backups` in Compose. |
| `BACKUP_RETENTION_DAYS` | No | `30`. |
| `BACKUP_HOST_DIR` | No | `./backups`; host directory mounted into maintenance containers. |
| `REDIS_URL` | No | `redis://redis:6379/0` in Compose. |
| `MAX_UPLOAD_MB` | No | `64`. |
| `INGEST_RATE_LIMIT_COUNT` | No | `20` requests per window/client/API process. |
| `INGEST_RATE_LIMIT_WINDOW_SECONDS` | No | `60`. |
| `HTTP_TIMEOUT_SECONDS` | No | `60`; PDF/arXiv fetches. |
| `ARXIV_API_BASE` | No | `http://export.arxiv.org/api/query`. |
| `INGEST_TEXT_BUDGET_CHARS` | No | `24000`. |
| `API_PORT` | No | `8000` on the host. |
| `WEBUI_PORT` | No | `3000` on the host. |
| `LOG_LEVEL` | No | `INFO`; logs are JSON on stdout. |
| `FRONTEND_DIR` | No | `/app/frontend` in Compose. |
| `PUBLIC_BASE_URL` | Production | Empty locally; set the Tailscale HTTPS origin used in pairing QR codes. |
| `HEALTH_OPENROUTER_TIMEOUT_SECONDS` | No | `5`. |
| `HEALTH_MIN_FREE_DISK_MB` | No | `1024`. |
| `OPENWEBUI_BASE_URL` | Production | `http://localhost:3000`; browser-facing chat URL. |

## Useful commands

```bash
make test            # complete pytest suite in the API image
make logs            # JSON API and worker logs
make backup          # dated consistent snapshot
make restore-drill   # new snapshot, isolated restore, health verification
make reindex-fts     # rebuild FTS5 from source tables
make reembed         # refresh only missing/stale-model embeddings
make health-full     # DB, FTS, embedding, OpenRouter, PageIndex, disk
```

The authenticated admin page is `/admin`. Production/Tailscale setup lives in [DEPLOYMENT.md](DEPLOYMENT.md), backup and restore procedures in [BACKUPS.md](BACKUPS.md), implementation history and limitations in [PROJECT_NOTES.md](PROJECT_NOTES.md), and architecture rules in [CLAUDE.md](CLAUDE.md).

## Troubleshooting

- **`API_TOKEN` or `OPENROUTER_API_KEY` missing:** protected routes return 503 without a token; ingest/QA health fails without an OpenRouter key. Compare `.env` with `.env.example`, then `docker compose up -d --force-recreate api worker`.
- **Port 8000 or 3000 is occupied:** set `API_PORT` or `WEBUI_PORT` in `.env`, recreate the stack, and update browser-facing URLs.
- **Embedding model download stalls:** check `docker compose logs worker`, free disk, and outbound HTTPS. The `hf-cache` volume preserves successful downloads across rebuilds.
- **Pipe models do not appear:** verify the Open WebUI profile is running, all three functions are saved and enabled under **Admin Panel → Functions**, and their Valve URL/token match [pipes/README.md](pipes/README.md).
