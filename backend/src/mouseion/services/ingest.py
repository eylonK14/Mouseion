"""The ingest pipeline.

    download → extract → index (tree) → tag (one combined LLM call) → embed

Two properties every step must keep:

* **Idempotent.** Running a step twice produces the same state as running it
  once — content-addressed writes, upserts, INSERT OR IGNORE.
* **Resumable.** Completed steps are recorded in the job's `progress_json`, so
  a job that failed at `embed` re-runs only `embed`, and in particular does not
  pay for the tagging LLM call a second time.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from mouseion.config import Settings, get_settings
from mouseion.db import session
from mouseion.services import papers as papers_repo
from mouseion.services.arxiv import ArxivMetadata, fetch_metadata, normalize_arxiv_url
from mouseion.services.embeddings import build_embedding_source, get_embedder, store_embedding
from mouseion.services.hashing import sha256_bytes
from mouseion.services.jobs import JobState, Step
from mouseion.services.jobs import (
    attach,
    get_progress,
    is_step_done,
    mark_step_done,
    payload_of,
    set_state,
)
from mouseion.services.llm import LLMClient, LLMTask, get_llm_client
from mouseion.services.pdfs import (
    ExtractedText,
    NotAPdfError,
    extract_text,
    looks_like_pdf,
    pdf_path_for,
    store_pdf,
)
from mouseion.services.prompts import build_ingest_prompt
from mouseion.services.schemas import IngestExtraction
from mouseion.services.taxonomy import apply_topic_assignments, list_topics, topics_for_paper
from mouseion.services.tree_indexer import TreeIndexer, get_tree_indexer, save_tree

log = logging.getLogger(__name__)

METADATA_FIELDS = ("title", "authors", "year", "venue", "abstract")


class IngestError(RuntimeError):
    """A step failed in a way the user needs to read."""


@dataclass(slots=True)
class IngestOutcome:
    job_id: str
    paper_id: int | None = None
    sha256: str | None = None
    duplicate: bool = False
    state: str = JobState.DONE.value
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------
async def download_pdf(client: httpx.AsyncClient, url: str, max_bytes: int) -> bytes:
    """Fetch a URL and insist it is a PDF.

    The size cap is enforced while streaming so a hostile or mistaken URL cannot
    fill the disk before we notice.
    """
    try:
        async with client.stream("GET", url, follow_redirects=True) as response:
            if response.status_code >= 400:
                raise IngestError(f"download failed: {url} returned HTTP {response.status_code}")
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise IngestError(
                        f"download aborted: {url} exceeds the {max_bytes // (1024 * 1024)}MB limit"
                    )
                chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise IngestError(f"download failed: {url} ({exc})") from exc

    data = b"".join(chunks)
    if not data:
        raise IngestError(f"download failed: {url} returned an empty body")
    if not looks_like_pdf(data):
        raise IngestError(
            f"the URL did not return a PDF: {url}. Paywalled or HTML pages cannot be "
            "ingested — download the PDF yourself and upload the file instead."
        )
    return data


async def _resolve_source(
    client: httpx.AsyncClient,
    url: str,
    settings: Settings,
) -> tuple[bytes, str, ArxivMetadata | None]:
    """Turn a user-supplied URL into (pdf bytes, canonical source url, metadata).

    arXiv is special-cased per CLAUDE.md: /abs/ is rewritten to /pdf/ and the
    bibliographic metadata comes from the arXiv API rather than from the LLM.
    """
    ref = normalize_arxiv_url(url)
    if ref is None:
        return await download_pdf(client, url, settings.max_upload_bytes), url, None

    metadata: ArxivMetadata | None = None
    try:
        metadata = await fetch_metadata(client, ref, settings.arxiv_api_base)
    except (httpx.HTTPError, ValueError) as exc:
        # A metadata miss is recoverable: the LLM step will read the PDF.
        log.warning("arXiv metadata lookup failed for %s: %s", ref.arxiv_id, exc)

    data = await download_pdf(client, ref.pdf_url, settings.max_upload_bytes)
    return data, ref.abs_url, metadata


# ---------------------------------------------------------------------------
# the pipeline
# ---------------------------------------------------------------------------
async def run_ingest(
    job_id: str,
    *,
    conn: sqlite3.Connection | None = None,
    settings: Settings | None = None,
    http_client: httpx.AsyncClient | None = None,
    llm: LLMClient | None = None,
    indexer: TreeIndexer | None = None,
) -> IngestOutcome:
    """Run (or resume) one ingest job.

    Every collaborator is injectable — that is how the integration test drives
    the whole pipeline with a canned LLM response and no network.
    """
    settings = settings or get_settings()
    if conn is not None:
        return await _run(job_id, conn, settings, http_client, llm, indexer)
    with session() as owned:
        return await _run(job_id, owned, settings, http_client, llm, indexer)


async def _run(
    job_id: str,
    conn: sqlite3.Connection,
    settings: Settings,
    http_client: httpx.AsyncClient | None,
    llm: LLMClient | None,
    indexer: TreeIndexer | None,
) -> IngestOutcome:
    from mouseion.services.jobs import bump_attempts, get_job

    job = get_job(conn, job_id)
    if job is None:
        raise IngestError(f"unknown job {job_id}")

    outcome = IngestOutcome(job_id=job_id)
    bump_attempts(conn, job_id)
    payload = payload_of(job)

    owns_http = http_client is None
    client = http_client or httpx.AsyncClient(
        timeout=httpx.Timeout(settings.http_timeout_seconds),
        headers={"User-Agent": "Mouseion/0.1 (personal paper library)"},
    )

    try:
        artifacts = get_progress(conn, job_id)["artifacts"]

        # -- 1. download / locate the bytes -------------------------------
        sha256, source_url, arxiv_meta = await _step_download(
            conn, job_id, job, payload, settings, client, artifacts
        )
        outcome.sha256 = sha256

        # Duplicate short-circuit for URL jobs: uploads are caught in the API
        # before a job even exists, but a URL's hash is only knowable here.
        existing = papers_repo.find_by_sha256(conn, sha256)
        if existing is not None and not is_step_done(conn, job_id, Step.EXTRACT):
            outcome.paper_id = int(existing["id"])
            outcome.duplicate = True
            attach(conn, job_id, paper_id=outcome.paper_id, sha256=sha256, duplicate=True)
            set_state(conn, job_id, JobState.DONE)
            log.info("job %s: duplicate of paper %s", job_id, outcome.paper_id)
            return outcome

        paper_id, _ = papers_repo.get_or_create_by_sha256(conn, sha256, source_url=source_url)
        outcome.paper_id = paper_id
        attach(conn, job_id, paper_id=paper_id, sha256=sha256, duplicate=False)

        # arXiv metadata is authoritative and written before the LLM ever runs.
        if arxiv_meta is not None:
            papers_repo.update_paper(
                conn,
                paper_id,
                title=arxiv_meta.title,
                authors=papers_repo.join_authors(arxiv_meta.authors),
                year=arxiv_meta.year,
                abstract=arxiv_meta.abstract,
                venue=arxiv_meta.venue,
            )

        pdf_path = pdf_path_for(settings.pdf_dir, sha256)

        # -- 2. extract ----------------------------------------------------
        extracted = await _step_extract(conn, job_id, paper_id, pdf_path, settings)

        # -- 3. tree index --------------------------------------------------
        await _step_index(conn, job_id, sha256, pdf_path, extracted, settings, indexer, outcome)

        # -- 4. combined LLM call: metadata + topics + summaries -----------
        await _step_tag(conn, job_id, paper_id, extracted, settings, llm, outcome)

        # -- 5. embedding ---------------------------------------------------
        _step_embed(conn, job_id, paper_id, settings, outcome)

        set_state(conn, job_id, JobState.DONE)
        return outcome

    except Exception as exc:  # noqa: BLE001 - the job row is the error channel
        message = f"{type(exc).__name__}: {exc}"
        log.exception("job %s failed", job_id)
        set_state(conn, job_id, JobState.FAILED, error=message)
        outcome.state = JobState.FAILED.value
        raise
    finally:
        if owns_http:
            await client.aclose()


async def _step_download(
    conn: sqlite3.Connection,
    job_id: str,
    job: sqlite3.Row,
    payload: dict[str, Any],
    settings: Settings,
    client: httpx.AsyncClient,
    artifacts: dict[str, Any],
) -> tuple[str, str | None, ArxivMetadata | None]:
    if is_step_done(conn, job_id, Step.DOWNLOAD):
        # Resume: the bytes are already on disk under their hash.
        sha = artifacts.get("sha256") or job["sha256"]
        if sha and pdf_path_for(settings.pdf_dir, sha).exists():
            return str(sha), artifacts.get("source_url"), None
        log.warning("job %s: download marked done but file is missing, redoing", job_id)

    set_state(conn, job_id, JobState.DOWNLOADING)

    if job["kind"] == "upload":
        sha = payload.get("sha256")
        if not sha:
            raise IngestError("upload job has no sha256 in its payload")
        path = pdf_path_for(settings.pdf_dir, sha)
        if not path.exists():
            raise IngestError(f"uploaded file is missing from the store: {path.name}")
        source_url = payload.get("source_url")
        arxiv_meta = None
    else:
        url = payload.get("url")
        if not url:
            raise IngestError("url job has no url in its payload")
        data, source_url, arxiv_meta = await _resolve_source(client, url, settings)
        try:
            _, sha = store_pdf(data, settings.pdf_dir, sha256_bytes(data))
        except NotAPdfError as exc:
            raise IngestError(str(exc)) from exc

    attach(conn, job_id, sha256=sha)
    mark_step_done(conn, job_id, Step.DOWNLOAD, sha256=sha, source_url=source_url)
    return str(sha), source_url, arxiv_meta


async def _step_extract(
    conn: sqlite3.Connection,
    job_id: str,
    paper_id: int,
    pdf_path: Path,
    settings: Settings,
) -> ExtractedText:
    set_state(conn, job_id, JobState.EXTRACTING)
    # Text is re-extracted on resume rather than cached in the job: it is cheap,
    # deterministic, and the later steps need it in memory anyway.
    extracted = extract_text(pdf_path)
    papers_repo.set_full_text(conn, paper_id, extracted.full_text, extracted.n_pages)
    mark_step_done(conn, job_id, Step.EXTRACT, n_pages=extracted.n_pages)
    return extracted


async def _step_index(
    conn: sqlite3.Connection,
    job_id: str,
    sha256: str,
    pdf_path: Path,
    extracted: ExtractedText,
    settings: Settings,
    indexer: TreeIndexer | None,
    outcome: IngestOutcome,
) -> None:
    if is_step_done(conn, job_id, Step.INDEX):
        return
    set_state(conn, job_id, JobState.INDEXING)
    tree_indexer = indexer or get_tree_indexer(settings)
    try:
        tree = await tree_indexer.build_tree(pdf_path, sha256, extracted)
        path = save_tree(tree, settings)
        mark_step_done(
            conn, job_id, Step.INDEX, tree_path=str(path), tree_indexer=tree_indexer.name
        )
    except Exception as exc:  # noqa: BLE001
        # Non-fatal by design: a paper without a tree is still worth having in
        # the library, and Phase 3 can rebuild trees for papers that lack one.
        warning = f"tree index skipped ({type(exc).__name__}: {exc})"
        log.warning("job %s: %s", job_id, warning)
        outcome.warnings.append(warning)
        mark_step_done(conn, job_id, Step.INDEX, tree_error=str(exc))


async def _step_tag(
    conn: sqlite3.Connection,
    job_id: str,
    paper_id: int,
    extracted: ExtractedText,
    settings: Settings,
    llm: LLMClient | None,
    outcome: IngestOutcome,
) -> None:
    if is_step_done(conn, job_id, Step.TAG):
        return
    set_state(conn, job_id, JobState.TAGGING)

    paper = papers_repo.get_paper(conn, paper_id)
    if paper is None:
        raise IngestError(f"paper {paper_id} vanished mid-ingest")

    known: dict[str, Any] = {}
    missing: list[str] = []
    for column in METADATA_FIELDS:
        value = paper[column]
        if column == "authors":
            value = papers_repo.split_authors(value)
        if value:
            known[column] = value
        else:
            missing.append(column)

    prompt = build_ingest_prompt(
        paper_text=extracted.full_text,
        topics=list_topics(conn),
        known_metadata=known,
        missing_metadata=missing,
        text_budget=settings.ingest_text_budget_chars,
        source_note=(
            "Metadata for this paper was not available from any API; read it off "
            "the first pages of the text below."
            if missing
            else None
        ),
    )

    client = llm or get_llm_client()
    extraction = await client.complete_json(
        task=LLMTask.INGEST,
        system=prompt.system,
        user=prompt.user,
        output_model=IngestExtraction,
    )

    # Only fill what was missing — never overwrite arXiv-sourced fields.
    updates = {
        name: getattr(extraction.metadata, name) for name in missing if name != "authors"
    }
    if "authors" in missing:
        updates["authors"] = papers_repo.join_authors(extraction.metadata.authors)
    updates["summary_short"] = extraction.summary_short
    updates["summary_long"] = extraction.summary_long
    papers_repo.update_paper(conn, paper_id, **updates)

    applied = apply_topic_assignments(conn, paper_id, extraction.topics)
    if applied.dropped:
        outcome.warnings.extend(applied.dropped)
    log.info(
        "job %s: topics reused=%s created=%s dropped=%s",
        job_id,
        [t.name for t in applied.reused],
        applied.created_names,
        applied.dropped,
    )

    mark_step_done(
        conn,
        job_id,
        Step.TAG,
        topic_ids=applied.topic_ids,
        created_topics=applied.created_names,
    )


def _step_embed(
    conn: sqlite3.Connection,
    job_id: str,
    paper_id: int,
    settings: Settings,
    outcome: IngestOutcome,
) -> None:
    if is_step_done(conn, job_id, Step.EMBED):
        return
    set_state(conn, job_id, JobState.EMBEDDING)

    paper = papers_repo.get_paper(conn, paper_id)
    if paper is None:
        raise IngestError(f"paper {paper_id} vanished mid-ingest")

    source = build_embedding_source(
        title=paper["title"],
        abstract=paper["abstract"],
        topic_names=[t.name for t in topics_for_paper(conn, paper_id)],
        summary_long=paper["summary_long"],
    )
    if not source:
        outcome.warnings.append("embedding skipped: nothing to embed")
        mark_step_done(conn, job_id, Step.EMBED, embedded=False)
        return

    embedder = get_embedder()
    vector = embedder.encode(source.text)
    stored = store_embedding(conn, paper_id, vector, model=embedder.model_name)
    if not stored:
        outcome.warnings.append("embedding skipped: sqlite-vec unavailable")
    mark_step_done(conn, job_id, Step.EMBED, embedded=stored, embedding_model=embedder.model_name)
