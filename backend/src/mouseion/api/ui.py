"""The server-rendered UI: page shells plus the HTMX fragments they load.

Two kinds of route live here, and the split matters for auth:

* **Shells** (`/`, `/papers/{id}`, `/taxonomy`) are public, because a browser
  cannot attach an `Authorization` header to a top-level navigation. They
  contain no library data — only markup and the HTMX attributes that go and
  fetch it.
* **Fragments and actions** (`/ui/*`) are behind the bearer token like every
  `/api/*` route. HTMX attaches it from localStorage via the
  `htmx:configRequest` hook in static/app.js.

Fragments call exactly the same service functions as the JSON API, so the two
surfaces cannot disagree about what a filter means. The action routes take
form-encoded bodies (what HTMX posts) and answer with the fragment that should
replace itself, which is why they are not just aliases for the JSON routes.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from mouseion.api.models import BlankableInt, BlankableStatus, PaperStatus
from mouseion.api.search import build_query, to_hits
from mouseion.config import Settings, get_settings
from mouseion.db import get_db
from mouseion.services import admin as admin_service
from mouseion.services import notes as notes_repo
from mouseion.services import papers as papers_repo
from mouseion.services.examiner import (
    latest_completed_by_papers,
    list_sessions as list_test_sessions,
    session_payload as test_session_payload,
)
from mouseion.services.search import SORT_KEYS, search_papers
from mouseion.services.health import run_full_health
from mouseion.services.taxonomy import (
    TopicError,
    TopicNotFoundError,
    add_paper_topic,
    build_topic_tree,
    delete_topic,
    get_or_create_topic,
    get_topic,
    list_topics,
    papers_by_topic,
    merge_topics,
    remove_paper_topic,
    rename_topic,
    reparent_topic,
    split_topic,
    topics_for_paper,
)

router = APIRouter(tags=["ui"])

# See api/topics.py — Starlette's constant for 422 was renamed, this spelling
# works on both sides of that change.
_UNPROCESSABLE = 422

# Reading workflow, in display order. The tuple is (value, label, chip classes);
# templates never hard-code a status string, so adding one is a one-line change
# here plus the CHECK constraint in a migration.
STATUSES: tuple[tuple[str, str, str], ...] = (
    ("to_read", "To read", "bg-neutral-800 text-neutral-300 ring-neutral-700"),
    ("reading", "Reading", "bg-amber-500/10 text-amber-300 ring-amber-500/30"),
    ("read", "Read", "bg-sky-500/10 text-sky-300 ring-sky-500/30"),
    ("understood", "Understood", "bg-emerald-500/10 text-emerald-300 ring-emerald-500/30"),
)
STATUS_LABELS = {value: label for value, label, _ in STATUSES}
STATUS_CLASSES = {value: classes for value, _, classes in STATUSES}

_templates: Jinja2Templates | None = None


def get_templates() -> Jinja2Templates:
    """Built lazily so tests can point FRONTEND_DIR somewhere else per-test."""
    global _templates
    if _templates is None:
        settings = get_settings()
        _templates = Jinja2Templates(directory=str(settings.templates_dir))
        _templates.env.globals.update(
            STATUSES=STATUSES,
            STATUS_LABELS=STATUS_LABELS,
            STATUS_CLASSES=STATUS_CLASSES,
            SORT_KEYS=SORT_KEYS,
        )
    return _templates


def reset_templates() -> None:
    """Drop the cached environment (tests, and app factory re-creation)."""
    global _templates
    _templates = None


def render(request: Request, template: str, context: dict[str, Any] | None = None) -> HTMLResponse:
    settings = get_settings()
    return get_templates().TemplateResponse(
        request,
        template,
        {
            "openwebui_base_url": settings.openwebui_base_url,
            **(context or {}),
        },
    )


def _require_paper(conn: sqlite3.Connection, paper_id: int) -> sqlite3.Row:
    row = papers_repo.get_paper(conn, paper_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"paper {paper_id} not found")
    return row


# ---------------------------------------------------------------------------
# shells — public, data-free
# ---------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def library_shell(
    request: Request, topic_id: BlankableInt = None
) -> HTMLResponse:
    """`/?topic_id=N` pre-selects a topic — where a topic chip clicked on a
    page without its own filter form lands.

    The id is echoed into a hidden field and never resolved to a name here:
    the shell is public, and a name is library data. The label on the filter
    chip is filled in by app.js once the (authenticated) topic tree arrives.
    """
    return render(request, "library.html", {"topic_id": topic_id})


@router.get("/papers/{paper_id}", response_class=HTMLResponse, include_in_schema=False)
def paper_shell(request: Request, paper_id: int) -> HTMLResponse:
    # Deliberately does not look the paper up: the shell is public, so it must
    # not be able to confirm whether a given id exists. The fragment it loads
    # is authenticated and returns the 404.
    return render(request, "paper.html", {"paper_id": paper_id})


@router.get("/taxonomy", response_class=HTMLResponse, include_in_schema=False)
def taxonomy_shell(request: Request) -> HTMLResponse:
    return render(request, "taxonomy.html")


@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
def admin_shell(request: Request) -> HTMLResponse:
    return render(request, "admin.html")


@router.api_route(
    "/share", methods=["GET", "POST"], response_class=HTMLResponse, include_in_schema=False
)
def share_shell(request: Request) -> HTMLResponse:
    # Installed PWAs have their share POST intercepted and staged by sw.js.
    # Keeping a public POST fallback gives a useful explanation when a browser
    # invokes the target before the service worker has taken control.
    return render(request, "share.html", {"worker_missed": request.method == "POST"})


@router.get("/pair", response_class=HTMLResponse, include_in_schema=False)
def pair_shell(request: Request) -> HTMLResponse:
    # The opaque nonce stays in location.search and is consumed by pair.js; it
    # is never interpolated into HTML or logged by application code.
    return render(request, "pair.html")


@router.get("/sw.js", include_in_schema=False)
def service_worker(settings: Settings = Depends(get_settings)) -> FileResponse:
    return FileResponse(
        settings.static_dir / "sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@router.get("/manifest.webmanifest", include_in_schema=False)
def web_manifest(settings: Settings = Depends(get_settings)) -> FileResponse:
    return FileResponse(
        settings.static_dir / "manifest.webmanifest",
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/offline", include_in_schema=False)
def offline_page(settings: Settings = Depends(get_settings)) -> FileResponse:
    return FileResponse(settings.static_dir / "offline.html", media_type="text/html")


# ---------------------------------------------------------------------------
# fragments — authenticated
# ---------------------------------------------------------------------------
ui = APIRouter(prefix="/ui", tags=["ui"], include_in_schema=False)


@ui.get("/admin", response_class=HTMLResponse)
async def admin_fragment(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    health = await run_full_health(conn=conn, settings=settings)
    return render(
        request,
        "partials/admin.html",
        {
            "health": health,
            "queue_counts": admin_service.queue_counts(conn),
            "failed_jobs": admin_service.failed_jobs(conn),
            "recent_jobs": admin_service.recent_jobs(conn),
            "qa_costs": admin_service.qa_costs(conn),
        },
    )


@ui.get("/results", response_class=HTMLResponse)
def results_fragment(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    # See api/search.py on why these are Annotated rather than `= Query(...)`.
    q: str | None = None,
    topic_id: BlankableInt = None,
    status_filter: Annotated[BlankableStatus, Query(alias="status")] = None,
    year_from: BlankableInt = None,
    year_to: BlankableInt = None,
    sort: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> HTMLResponse:
    """Result cards + pagination. The library view's only data request."""
    query = build_query(q, topic_id, status_filter, year_from, year_to, sort, limit, offset)
    result = search_papers(conn, query)
    hits = to_hits(conn, result)
    return render(
        request,
        "partials/results.html",
        {
            "hits": hits,
            "understanding_by_paper": latest_completed_by_papers(
                conn, [hit.paper.id for hit in hits]
            ),
            "total": result.total,
            "query": query,
            "topic": get_topic(conn, topic_id) if topic_id is not None else None,
        },
    )


@ui.get("/topics", response_class=HTMLResponse)
def topics_fragment(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    topic_id: BlankableInt = None,
) -> HTMLResponse:
    """The sidebar tree, with the current topic highlighted."""
    return render(
        request,
        "partials/topic_tree.html",
        {
            "nodes": build_topic_tree(conn),
            "selected_id": topic_id,
            "total_papers": papers_repo.count_papers(conn),
        },
    )


@ui.get("/papers/{paper_id}", response_class=HTMLResponse)
def paper_fragment(
    request: Request,
    paper_id: int,
    conn: sqlite3.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    row = _require_paper(conn, paper_id)
    text = papers_repo.get_full_text(conn, paper_id)
    return render(
        request,
        "partials/paper_detail.html",
        {
            "paper": row,
            "authors": papers_repo.split_authors(row["authors"]),
            # Topics and notes load as their own fragments so an edit to either
            # re-renders just that panel.
            "n_pages": text["n_pages"] if text else None,
            "openwebui_base_url": settings.openwebui_base_url,
        },
    )


def _understanding(
    request: Request,
    conn: sqlite3.Connection,
    paper_id: int,
    *,
    settings: Settings,
    status_updated: bool = False,
) -> HTMLResponse:
    paper = _require_paper(conn, paper_id)
    sessions = list_test_sessions(conn, paper_id)
    payloads = [test_session_payload(conn, session, settings=settings) for session in sessions]
    latest = next((item for item in payloads if item["verdict"] is not None), None)
    return render(
        request,
        "partials/understanding.html",
        {
            "paper": paper,
            "sessions": payloads,
            "latest": latest,
            "understood_threshold": settings.test_understood_threshold,
            "status_updated": status_updated,
        },
    )


@ui.get("/papers/{paper_id}/understanding", response_class=HTMLResponse)
def understanding_fragment(
    request: Request,
    paper_id: int,
    conn: sqlite3.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    return _understanding(request, conn, paper_id, settings=settings)


@ui.post("/papers/{paper_id}/understanding/accept", response_class=HTMLResponse)
def understanding_accept(
    request: Request,
    paper_id: int,
    conn: sqlite3.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    _require_paper(conn, paper_id)
    latest = latest_completed_by_papers(conn, [paper_id]).get(paper_id)
    if (
        latest is None
        or latest.verdict is None
        or latest.verdict.overall.score < settings.test_understood_threshold
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"latest test score is below the understood threshold ({settings.test_understood_threshold})",
        )
    papers_repo.update_paper(conn, paper_id, status="understood")
    return _understanding(
        request, conn, paper_id, settings=settings, status_updated=True
    )


def _paper_topics(request: Request, conn: sqlite3.Connection, paper_id: int) -> HTMLResponse:
    assigned = topics_for_paper(conn, paper_id)
    assigned_ids = {topic.id for topic in assigned}
    return render(
        request,
        "partials/paper_topics.html",
        {
            "paper_id": paper_id,
            "topics": assigned,
            # Only topics that exist and are not already on the paper — the
            # picker is how CLAUDE.md's "never free-form tags" rule is enforced
            # in the UI.
            "available": [t for t in list_topics(conn) if t.id not in assigned_ids],
        },
    )


@ui.get("/papers/{paper_id}/topics", response_class=HTMLResponse)
def paper_topics_fragment(
    request: Request, paper_id: int, conn: sqlite3.Connection = Depends(get_db)
) -> HTMLResponse:
    _require_paper(conn, paper_id)
    return _paper_topics(request, conn, paper_id)


@ui.post("/papers/{paper_id}/topics", response_class=HTMLResponse)
def paper_topic_add(
    request: Request,
    paper_id: int,
    topic_id: int = Form(...),
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    _require_paper(conn, paper_id)
    try:
        add_paper_topic(conn, paper_id, topic_id)
    except TopicNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return _paper_topics(request, conn, paper_id)


@ui.delete("/papers/{paper_id}/topics/{topic_id}", response_class=HTMLResponse)
def paper_topic_remove(
    request: Request,
    paper_id: int,
    topic_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    _require_paper(conn, paper_id)
    remove_paper_topic(conn, paper_id, topic_id)
    return _paper_topics(request, conn, paper_id)


@ui.post("/papers/{paper_id}/status", response_class=HTMLResponse)
def paper_status_set(
    request: Request,
    paper_id: int,
    status_value: PaperStatus = Form(..., alias="status"),
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    _require_paper(conn, paper_id)
    papers_repo.update_paper(conn, paper_id, status=status_value)
    return render(
        request,
        "partials/status_control.html",
        {"paper_id": paper_id, "status": status_value, "saved": True},
    )


# ------------------------------------------------------------------- notes
def _notes(request: Request, conn: sqlite3.Connection, paper_id: int) -> HTMLResponse:
    return render(
        request,
        "partials/notes.html",
        {"paper_id": paper_id, "notes": notes_repo.list_notes(conn, paper_id)},
    )


@ui.get("/papers/{paper_id}/notes", response_class=HTMLResponse)
def notes_fragment(
    request: Request, paper_id: int, conn: sqlite3.Connection = Depends(get_db)
) -> HTMLResponse:
    _require_paper(conn, paper_id)
    return _notes(request, conn, paper_id)


@ui.post("/papers/{paper_id}/notes", response_class=HTMLResponse)
def note_create(
    request: Request, paper_id: int, conn: sqlite3.Connection = Depends(get_db)
) -> HTMLResponse:
    _require_paper(conn, paper_id)
    notes_repo.create_note(conn, paper_id, "")
    return _notes(request, conn, paper_id)


@ui.put("/papers/{paper_id}/notes/{note_id}", response_class=HTMLResponse)
def note_save(
    request: Request,
    paper_id: int,
    note_id: int,
    content: str = Form(default=""),
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    """Autosave target. Answers with a timestamp, not the whole editor —
    replacing a textarea the user is typing into would eat the caret."""
    row = notes_repo.update_note(conn, paper_id, note_id, content)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"note {note_id} not found")
    return render(request, "partials/note_saved.html", {"note": row})


@ui.delete("/papers/{paper_id}/notes/{note_id}", response_class=HTMLResponse)
def note_delete(
    request: Request, paper_id: int, note_id: int, conn: sqlite3.Connection = Depends(get_db)
) -> HTMLResponse:
    notes_repo.delete_note(conn, paper_id, note_id)
    return _notes(request, conn, paper_id)


# ---------------------------------------------------------------- taxonomy
def _taxonomy(
    request: Request, conn: sqlite3.Connection, message: str | None = None
) -> HTMLResponse:
    return render(
        request,
        "partials/taxonomy_tree.html",
        {
            "nodes": build_topic_tree(conn),
            "topics": list_topics(conn),
            "topic_papers": papers_by_topic(conn),
            "message": message,
        },
    )


@ui.get("/taxonomy", response_class=HTMLResponse)
def taxonomy_fragment(
    request: Request, conn: sqlite3.Connection = Depends(get_db)
) -> HTMLResponse:
    return _taxonomy(request, conn)


@ui.post("/taxonomy/topics", response_class=HTMLResponse)
def taxonomy_create(
    request: Request,
    name: str = Form(...),
    parent_id: str = Form(default=""),
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    parent = int(parent_id) if parent_id.strip() else None
    try:
        topic, created = get_or_create_topic(conn, name, parent)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    note = f"Created {topic.name!r}." if created else f"{topic.name!r} already existed."
    return _taxonomy(request, conn, note)


@ui.post("/taxonomy/topics/{topic_id}/rename", response_class=HTMLResponse)
def taxonomy_rename(
    request: Request,
    topic_id: int,
    name: str = Form(...),
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    try:
        topic = rename_topic(conn, topic_id, name)
    except TopicError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return _taxonomy(request, conn, f"Renamed to {topic.name!r}.")


@ui.post("/taxonomy/topics/{topic_id}/reparent", response_class=HTMLResponse)
def taxonomy_reparent(
    request: Request,
    topic_id: int,
    parent_id: str = Form(default=""),
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    parent = int(parent_id) if parent_id.strip() else None
    try:
        reparent_topic(conn, topic_id, parent)
    except TopicError as exc:
        raise HTTPException(_UNPROCESSABLE, str(exc)) from exc
    return _taxonomy(request, conn, "Moved.")


@ui.post("/taxonomy/topics/{topic_id}/merge", response_class=HTMLResponse)
def taxonomy_merge(
    request: Request,
    topic_id: int,
    into_topic_id: int = Form(...),
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    try:
        result = merge_topics(conn, topic_id, into_topic_id)
    except TopicError as exc:
        raise HTTPException(_UNPROCESSABLE, str(exc)) from exc
    return _taxonomy(
        request,
        conn,
        f"Merged into {result.target.name!r} — {result.papers_relinked} paper(s) relinked, "
        f"{result.children_moved} child topic(s) moved.",
    )


@ui.post("/taxonomy/topics/{topic_id}/split", response_class=HTMLResponse)
def taxonomy_split(
    request: Request,
    topic_id: int,
    name: str = Form(...),
    paper_ids: list[int] = Form(default=[]),
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    try:
        result = split_topic(conn, topic_id, name, paper_ids)
    except TopicError as exc:
        raise HTTPException(_UNPROCESSABLE, str(exc)) from exc
    return _taxonomy(
        request,
        conn,
        f"Split {result.papers_moved} paper(s) into {result.created.name!r}.",
    )


@ui.delete("/taxonomy/topics/{topic_id}", response_class=HTMLResponse)
def taxonomy_delete(
    request: Request,
    topic_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> HTMLResponse:
    try:
        result = delete_topic(conn, topic_id)
    except TopicError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return _taxonomy(
        request,
        conn,
        f"Deleted {result.topic.name!r} — {result.paper_links_removed} paper link(s) removed, "
        f"{result.children_promoted} child topic(s) promoted.",
    )


router.include_router(ui)
