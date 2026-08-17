# pipes/ — Open WebUI pipes

Stub. Nothing here in Phase 1.

Per `CLAUDE.md`, Open WebUI runs as a **stock container** and is a chat surface
only; everything it can do is reached through custom Python *pipes* that live in
this directory and call the Mouseion backend over HTTP.

Planned contents:

| Phase | File | Purpose |
| ----- | ---- | ------- |
| 3 | `library_qa.py` | QA over one paper or the whole collection. Calls `POST /api/qa` (paper-level vectors + two-stage retrieval: vector shortlist → PageIndex tree navigation). |
| 4 | `examiner.py` | Test mode. Drives the explain-it-back transcript, then calls the grading endpoint for a rubric score + gap list. |

Conventions for pipes added later:

- A pipe is a thin transport shim. **No prompt logic and no LLM calls belong
  here** — the backend owns orchestration and model routing (`MODEL_QA` via
  `services/llm.py`), so the two surfaces can never drift.
- Auth: send `Authorization: Bearer $API_TOKEN`, same token as every other
  client.
- Reach the backend at `http://api:8000` on the compose network (the
  `open-webui` service is defined under the `webui` profile in
  `docker-compose.yml`).
