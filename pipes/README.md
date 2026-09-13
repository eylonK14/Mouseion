# Mouseion Open WebUI pipes

These are Open WebUI **Pipe Functions**. They are deliberately thin: tag and
conversation-scope handling happens here, while retrieval, prompts, OpenRouter
calls, grounding checks, examiner state, grading, and cost logging stay in the
Mouseion backend.

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
   Functions → + Create Function**. Create and enable these three functions:

   | Function id | Source file | Model shown in the picker |
   | --- | --- | --- |
   | `paper_library` | `pipes/library_qa.py` | 📚 Paper Library |
   | `single_paper` | `pipes/single_paper.py` | 📄 Single Paper |
   | `test_me` | `pipes/test_me.py` | 🎓 Test me |

   Paste each file's complete source into the editor. The ids above are
   important: Mouseion's detail-page deep links select `single_paper` or
   `test_me` through Open WebUI's `model` and `q` URL parameters.

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

6. Select **🎓 Test me** and start with `[test:12]` or `[paper:12]`; without a
   tag, mention the paper title and the same backend-owned fuzzy resolver is
   used. The pipe starts a persisted session, relays the opening explanation
   prompt and grounded probes, and renders the final rubric, misconceptions,
   and reread list as Markdown. Its hidden session marker lets Open WebUI's
   retained conversation resume the same backend session after a reconnect.

## Voice mode

Open WebUI's built-in voice/call mode works with **🎓 Test me** as-is. Start a
call while that model is selected and speak each explanation or probe response;
the pipe needs no audio or speech integration. If you want ElevenLabs text to
speech, configure it in Open WebUI's audio settings. Mouseion does not call or
embed ElevenLabs directly.

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
- Start exam: `POST http://api:8000/api/test/{paper_id}/start`
- Advance exam: `POST http://api:8000/api/test/{session_id}/turn`
- Reload exam: `GET http://api:8000/api/test/{session_id}`
- The backend emits named SSE events: `metadata`, `token`, `done`, and `error`.
  Pipes yield only token/error text to Open WebUI; metadata remains available
  to direct clients such as Mouseion's inline quick-question box.
- Examiner turns additionally emit a structured `verdict` event. The Test-me
  pipe formats it; prompts, state transitions, section validation, and grading
  remain in the backend.
