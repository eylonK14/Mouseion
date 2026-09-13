# Mouseion

Mouseion is a self-hosted research-paper library that turns PDFs and arXiv
links into a searchable, topic-organized collection. It can answer grounded
questions about one paper or the whole library, then test whether you can
explain a paper in your own words.

Each installation is designed for one owner. Your papers, metadata, notes,
search index, and test history stay on infrastructure you control.

## Features

- PDF upload and arXiv ingestion with content-hash deduplication
- Resumable background jobs for extraction, indexing, tagging, and summaries
- Controlled hierarchical topics rather than uncontrolled free-form tags
- Full-text search, semantic retrieval, filters, reading states, and notes
- Authenticated in-browser PDF reading
- Grounded questions over one paper, a topic, or the complete collection
- Explain-it-back examiner sessions with rubric scores, misconceptions, and
  exact sections to reread
- Optional Open WebUI chat and voice interface through three thin Pipe
  Functions
- Local sentence-transformers embeddings; all LLM requests route through
  OpenRouter

## How it fits together

```text
Browser ──> FastAPI + Jinja/HTMX ──> SQLite + PDFs + paper trees
                    │
                    ├──> Redis ──> ingest worker
                    │                 ├──> local embedding model
                    │                 └──> OpenRouter
                    │
Open WebUI ──> thin Pipe Functions ───┘
```

SQLite is the system of record. PDFs are stored by SHA-256 hash, embeddings are
one local vector per paper, and Redis is used only for the resumable ingest
queue.

## Requirements

- Git
- Docker Engine or Docker Desktop
- Docker Compose v2 (`docker compose version`)
- An [OpenRouter API key](https://openrouter.ai/keys)
- At least 8 GB RAM and several GB of free disk

A CPU is sufficient for the default MiniLM embedding model. The first build and
first model download can take several minutes; later starts reuse Docker's
cached layers and the persistent Hugging Face cache volume.

## Quickstart

Clone the repository:

```bash
git clone https://github.com/eylonK14/Mouseion.git
cd Mouseion
```

Create the environment file.

Linux, macOS, or WSL:

```bash
cp .env.example .env
```

Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Edit `.env` and replace these two values:

```dotenv
API_TOKEN=<a long random token>
OPENROUTER_API_KEY=<your OpenRouter API key>
```

You can generate the API token without installing Python locally:

```bash
docker run --rm python:3.12-alpine python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Start Mouseion. GNU Make is not required:

```bash
docker compose up -d --build
docker compose ps
```

The migration container runs automatically before the API and worker start.
When `api`, `worker`, and `redis` are running, open
[http://localhost:8000](http://localhost:8000). Enter the `API_TOKEN` from
`.env`, click **Add paper**, and upload a PDF or paste an arXiv URL such as:

```text
https://arxiv.org/abs/1706.03762
```

Ingest runs asynchronously. The UI shows its progress and refreshes the
library when the paper is ready.

## Add Open WebUI

Start the optional chat interface:

```bash
docker compose --profile webui up -d open-webui
```

Open [http://localhost:3000](http://localhost:3000), then install and configure
the **Paper Library**, **Single Paper**, and **Test me** functions by following
[pipes/README.md](pipes/README.md). Retrieval, prompts, LLM calls, grading, and
session state remain in the Mouseion backend; the functions are transport
adapters only.

## Useful commands

```bash
docker compose ps
docker compose logs -f api worker
docker compose restart api worker
docker compose down
docker compose run --rm migrate
docker compose run --rm --no-deps --entrypoint pytest api -q
```

If GNU Make is installed, the shorter equivalents are available:

```bash
make up
make webui
make logs
make test
make down
```

`docker compose down` preserves the library in `data/` and keeps the Open
WebUI and model-cache volumes. Do not run `make clean` or
`docker compose down -v` unless you intentionally want to remove Docker
volumes.

## Main pages

| Address | Purpose |
| --- | --- |
| `/` | Search, browse, filter, and add papers |
| `/papers/{id}` | Metadata, summaries, topics, notes, status, tests, and PDF |
| `/taxonomy` | Create, rename, move, merge, split, and delete topics |
| `/health` | Lightweight service health |
| `/docs` | Authenticated OpenAPI documentation |

## Configuration

Every supported environment variable and its default is documented in
[.env.example](.env.example). The most commonly changed values are:

| Variable | Purpose |
| --- | --- |
| `API_TOKEN` | Bearer token protecting library data and API routes |
| `OPENROUTER_API_KEY` | Credential used for every LLM request |
| `MODEL_INGEST` | Ingest-time metadata, topic, and summary model |
| `MODEL_QA` | Grounded QA and examiner model |
| `EMBEDDING_MODEL` | Local sentence-transformers model |
| `TREE_INDEXER` | `auto`, `heuristic`, or required `pageindex` mode |
| `API_PORT` | Host port for Mouseion; default `8000` |
| `WEBUI_PORT` | Host port for Open WebUI; default `3000` |

The public HTML shells contain no library data. All `/api/*` and `/ui/*` data
routes require the bearer token, which the browser stores locally and attaches
to requests. The development Compose stack should not be exposed directly to
the public internet.

## Troubleshooting

- **Compose says `.env` is missing:** copy `.env.example` to `.env` and set
  `API_TOKEN` plus `OPENROUTER_API_KEY`.
- **The UI reports 401:** enter the exact `API_TOKEN` from `.env`. Recreate the
  API container after changing it: `docker compose up -d --force-recreate api`.
- **Ingest appears slow on the first paper:** inspect
  `docker compose logs -f worker`. The worker may be downloading the local
  embedding model, and a long paper may take several minutes to process.
- **Port 8000 or 3000 is occupied:** change `API_PORT` or `WEBUI_PORT` in
  `.env`, then recreate the affected service.
- **Open WebUI models do not appear:** confirm the three functions are saved
  and enabled, and verify their backend URL and token using
  [pipes/README.md](pipes/README.md).

For architectural constraints and implementation details, see
[CLAUDE.md](CLAUDE.md) and [PROJECT_NOTES.md](PROJECT_NOTES.md).
