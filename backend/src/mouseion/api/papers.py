"""Ingest and read endpoints.

`POST /api/papers` accepts either a multipart PDF upload or a JSON body with a
URL, and returns immediately with a job id (CLAUDE.md: ingest is asynchronous).
"""

from __future__ import annotations

import logging
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from pydantic import ValidationError

from mouseion.api.models import (
    IngestAccepted,
    IngestUrlIn,
    PaperListOut,
    PaperOut,
    PaperPatchIn,
    PaperTopicIn,
)
from mouseion.config import Settings, get_settings
from mouseion.db import get_db
from mouseion.services import papers as papers_repo
from mouseion.services import queue
from mouseion.services.arxiv import normalize_arxiv_url
from mouseion.services.hashing import sha256_bytes
from mouseion.services.jobs import JobKind, JobState, create_job, set_state
from mouseion.services.jobs import attach as attach_job
from mouseion.services.pdfs import NotAPdfError, pdf_path_for, store_pdf
from mouseion.services.rate_limit import ingest_limiter
from mouseion.services.taxonomy import (
    TopicNotFoundError,
    add_paper_topic,
    remove_paper_topic,
    topics_for_paper,
    topics_for_papers,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["papers"])

_READ_CHUNK = 1024 * 1024


async def _enqueue(conn: sqlite3.Connection, job_id: str) -> None:
    """Hand the job to the worker; a queue outage is reported, not swallowed."""
    try:
        await queue.enqueue_ingest(job_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("failed to enqueue job %s", job_id)
        set_state(conn, job_id, JobState.FAILED, error=f"could not enqueue: {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ingest queue is unavailable",
        ) from exc


def _duplicate_response(
    conn: sqlite3.Connection, kind: JobKind, payload: dict, paper_id: int, sha256: str | None
) -> IngestAccepted:
    """Record a job row even for duplicates so every submit has a job to poll."""
    job_id = create_job(conn, kind, payload)
    attach_job(conn, job_id, paper_id=paper_id, sha256=sha256, duplicate=True)
    set_state(conn, job_id, JobState.DONE)
    return IngestAccepted(job_id=job_id, duplicate=True, paper_id=paper_id)


async def _ingest_upload(
    request: Request, conn: sqlite3.Connection, settings: Settings
) -> IngestAccepted:
    form = await request.form()
    upload = form.get("file")
    url_field = form.get("url")

    if upload is None or isinstance(upload, str):
        # A multipart submit carrying only a url field is still a URL ingest —
        # the Phase 1 upload form posts one form with both inputs.
        if isinstance(url_field, str) and url_field.strip():
            return await _ingest_url_value(url_field.strip(), conn, settings)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "provide a PDF file or a url")

    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(_READ_CHUNK):
        total += len(chunk)
        if total > settings.max_upload_bytes:
            # Literal 413: the Starlette constant for it was renamed, and this
            # spelling works across both versions.
            raise HTTPException(413, f"file exceeds MAX_UPLOAD_MB={settings.max_upload_mb}")
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "uploaded file is empty")

    # Hash early (build step 3): dedup before touching the disk or the queue.
    digest = sha256_bytes(data)
    payload = {"filename": getattr(upload, "filename", None), "sha256": digest}

    existing = papers_repo.find_by_sha256(conn, digest)
    if existing is not None:
        return _duplicate_response(
            conn, JobKind.UPLOAD, payload, int(existing["id"]), digest
        )

    try:
        store_pdf(data, settings.pdf_dir, digest)
    except NotAPdfError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{exc}. Upload a PDF, or paste an arXiv URL instead.",
        ) from exc

    job_id = create_job(conn, JobKind.UPLOAD, payload)
    attach_job(conn, job_id, sha256=digest)
    await _enqueue(conn, job_id)
    return IngestAccepted(job_id=job_id, duplicate=False)


async def _ingest_url_value(
    url: str, conn: sqlite3.Connection, settings: Settings
) -> IngestAccepted:
    try:
        payload_model = IngestUrlIn(url=url)
    except ValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.errors()[0]["msg"]) from exc

    # Cheap pre-check: for arXiv we know the canonical URL before downloading,
    # so a re-submit short-circuits without spending a fetch. The authoritative
    # hash-based dedup still happens in the worker.
    ref = normalize_arxiv_url(payload_model.url)
    canonical = ref.abs_url if ref else payload_model.url
    existing = papers_repo.find_by_source_url(conn, canonical)
    if existing is not None:
        return _duplicate_response(
            conn,
            JobKind.URL,
            {"url": payload_model.url},
            int(existing["id"]),
            existing["sha256"],
        )

    job_id = create_job(conn, JobKind.URL, {"url": payload_model.url})
    await _enqueue(conn, job_id)
    return IngestAccepted(job_id=job_id, duplicate=False)


@router.post("/papers", response_model=IngestAccepted, status_code=status.HTTP_202_ACCEPTED)
async def ingest_paper(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> IngestAccepted:
    """Ingest a PDF upload (multipart) or a URL (JSON `{"url": ...}`)."""
    client_key = request.client.host if request.client else "unknown"
    retry_after = ingest_limiter.check(
        client_key,
        limit=settings.ingest_rate_limit_count,
        window_seconds=settings.ingest_rate_limit_window_seconds,
    )
    if retry_after:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "ingest rate limit exceeded; try again shortly",
            headers={"Retry-After": str(retry_after)},
        )
    content_type = (request.headers.get("content-type") or "").lower()

    if content_type.startswith("multipart/form-data"):
        return await _ingest_upload(request, conn, settings)

    if content_type.startswith("application/json"):
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "body is not valid JSON") from exc
        if not isinstance(body, dict) or not body.get("url"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, 'expected {"url": "..."}')
        return await _ingest_url_value(str(body["url"]), conn, settings)

    raise HTTPException(
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        'send multipart/form-data with a "file" part, or application/json with {"url": ...}',
    )


@router.get("/papers", response_model=PaperListOut)
def list_papers(
    conn: sqlite3.Connection = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> PaperListOut:
    rows = papers_repo.list_papers(conn, limit=limit, offset=offset)
    topics = topics_for_papers(conn, [row["id"] for row in rows])
    return PaperListOut(
        items=[PaperOut.from_row(row, topics.get(row["id"], [])) for row in rows],
        total=papers_repo.count_papers(conn),
        limit=limit,
        offset=offset,
    )


def _require_paper(conn: sqlite3.Connection, paper_id: int) -> sqlite3.Row:
    row = papers_repo.get_paper(conn, paper_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"paper {paper_id} not found")
    return row


def _paper_out(conn: sqlite3.Connection, row: sqlite3.Row) -> PaperOut:
    paper_id = int(row["id"])
    paper = PaperOut.from_row(row, topics_for_paper(conn, paper_id))
    text = papers_repo.get_full_text(conn, paper_id)
    if text is not None:
        paper.n_pages = text["n_pages"]
    return paper


@router.get("/papers/{paper_id}", response_model=PaperOut)
def get_paper(
    paper_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> PaperOut:
    return _paper_out(conn, _require_paper(conn, paper_id))


@router.patch("/papers/{paper_id}", response_model=PaperOut)
def patch_paper(
    paper_id: int,
    body: PaperPatchIn,
    conn: sqlite3.Connection = Depends(get_db),
) -> PaperOut:
    """Reading-workflow update. Only `status` is writable.

    Every transition among the four statuses is allowed — including going
    backwards, which is what re-reading a paper actually looks like. The
    validation is that the value is one of the four (Pydantic rejects anything
    else with a 422 before this body runs) and that the paper exists; there is
    deliberately no state machine on top of that, because the ordering exists
    for display, not as a workflow to enforce on a single user.
    """
    _require_paper(conn, paper_id)
    papers_repo.update_paper(conn, paper_id, status=body.status)
    return _paper_out(conn, _require_paper(conn, paper_id))


@router.get("/papers/{paper_id}/pdf")
def get_paper_pdf(
    paper_id: int,
    conn: sqlite3.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    """The stored PDF, for the detail page's viewer.

    Behind the bearer token like every other /api route, which is why the
    viewer fetches it with the token and renders a blob URL rather than
    pointing an <iframe> straight at this path — a browser cannot attach a
    header to an iframe's own request.
    """
    row = _require_paper(conn, paper_id)
    path = pdf_path_for(settings.pdf_dir, row["sha256"])
    if not path.is_file():
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no stored PDF for paper {paper_id} (ingested from a URL that was never saved?)",
        )
    return FileResponse(
        path,
        media_type="application/pdf",
        # inline, so the viewer displays it instead of the browser downloading.
        headers={"Content-Disposition": f'inline; filename="paper-{paper_id}.pdf"'},
    )


# ------------------------------------------------------------- paper ↔ topics
@router.post("/papers/{paper_id}/topics", response_model=PaperOut)
def add_topic_to_paper(
    paper_id: int,
    body: PaperTopicIn,
    conn: sqlite3.Connection = Depends(get_db),
) -> PaperOut:
    """Tag a paper with an existing topic (CLAUDE.md: never free-form tags)."""
    row = _require_paper(conn, paper_id)
    try:
        add_paper_topic(conn, paper_id, body.topic_id)
    except TopicNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return _paper_out(conn, row)


@router.delete("/papers/{paper_id}/topics/{topic_id}", response_model=PaperOut)
def remove_topic_from_paper(
    paper_id: int,
    topic_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> PaperOut:
    row = _require_paper(conn, paper_id)
    # Removing a tag the paper does not have is a no-op, not an error: the UI
    # sends this from a chip that may already be gone in another tab.
    remove_paper_topic(conn, paper_id, topic_id)
    return _paper_out(conn, row)
