# Mouseion Open WebUI pipes

These are Open WebUI **Pipe Functions**. They are deliberately thin: tag and
conversation-scope handling happens here, while retrieval, prompts, OpenRouter
calls, grounding checks, and cost logging stay in the Mouseion backend.

## Install into the compose Open WebUI container

1. Configure `API_TOKEN`, `OPENROUTER_API_KEY`, and `MODEL_QA` in `.env`, then
   migrate and start the API plus Open WebUI:

   ```bash
   docker compose run --rm migrate
   docker compose --profile webui up -d api open-webui
   ```

2. Confirm the source mount exists inside the stock container:

   ```bash
   docker compose exec open-webui sh -lc \
     'ls -l /app/backend/data/mouseion-pipes'
   ```

   `docker-compose.yml` mounts this repository's `pipes/` directory read-only
   at `/app/backend/data/mouseion-pipes`.

3. Open `http://localhost:${WEBUI_PORT:-3000}`. Go to **Admin Panel →
   Functions → + Create Function**. Create and enable these two functions:

   | Function id | Source file | Model shown in the picker |
   | --- | --- | --- |
   | `paper_library` | `pipes/library_qa.py` | 📚 Paper Library |
   | `single_paper` | `pipes/single_paper.py` | 📄 Single Paper |

   Paste each file's complete source into the editor. The ids above are
   important: the Mouseion detail-page deep link selects `single_paper` via
   Open WebUI's `?model=single_paper&q=...` URL parameters.

4. Open the gear icon for each Function and set its Valves:

   - `MOUSEION_API_BASE_URL`: `http://api:8000`
   - `MOUSEION_API_TOKEN`: the exact value of Mouseion's `API_TOKEN`

   The token is stored in Open WebUI's Function configuration and forwarded as
   a bearer token. Do not paste it into either Python source file.

5. Select **📚 Paper Library** and send a question. Optional collection scope
   is a leading `[topic:id-or-name]` tag. Select **📄 Single Paper** and start
   with `[paper:12] Your question`; without a tag, the first message is matched
   against paper titles and the pipe either locks one clear match or asks you
   to choose from the best candidates.

## Development / hot reload

The installed wrappers are intentionally tiny. On every request they reload
`common.py` from the bind mount, so edits to tag parsing, locking, error
handling, or SSE relay code are live on the next message—no image rebuild or
container restart. If you change a wrapper's frontmatter, Valve schema, or
model name, paste that wrapper into its Function editor once more and save it;
Open WebUI stores Function wrappers in its database.

Useful checks:

```bash
docker compose exec open-webui python -m py_compile \
  /app/backend/data/mouseion-pipes/common.py
docker compose logs -f api open-webui
```

## Transport contract

- Collection: `POST http://api:8000/api/qa/collection`
- Paper: `POST http://api:8000/api/qa/paper/{id}`
- Title resolution: `POST http://api:8000/api/qa/paper/resolve`
- The backend emits named SSE events: `metadata`, `token`, `done`, and `error`.
  Pipes yield only token/error text to Open WebUI; metadata remains available
  to direct clients such as Mouseion's inline quick-question box.
