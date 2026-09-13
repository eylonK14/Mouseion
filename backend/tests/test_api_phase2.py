"""Phase 2 HTTP surface: search, topics, reading workflow, notes, and the UI.

The service layer is tested directly elsewhere; this file is about the wiring —
status codes, auth, response shapes, and the fact that the HTMX fragments and
the JSON API answer from the same data.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from mouseion.services.papers import get_or_create_by_sha256, set_full_text, update_paper
from mouseion.services.taxonomy import get_or_create_topic

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def client(data_dir, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from mouseion.api import ui
    from mouseion.main import create_app
    from mouseion.services import queue

    async def fake_enqueue(job_id: str, *, attempt: int = 0) -> None:
        return None

    monkeypatch.setattr(queue, "enqueue_ingest", fake_enqueue)
    ui.reset_templates()
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def paper(conn: sqlite3.Connection) -> int:
    paper_id, _ = get_or_create_by_sha256(conn, "a" * 64)
    update_paper(
        conn,
        paper_id,
        title="Quantum Error Correction",
        authors="Ada Lovelace; Alan Turing",
        year=2019,
        abstract="Surface codes protect logical qubits.",
        summary_short="Codes that protect qubits.",
        summary_long="A longer account of the same thing.",
    )
    set_full_text(conn, paper_id, "Introduction. Quantum computers are noisy.", 1)
    return paper_id


# ------------------------------------------------------------------- search
def test_search_returns_hits_with_scores_and_snippets(
    client: TestClient, paper: int
) -> None:
    body = client.get("/api/search?q=quantum", headers=AUTH).json()

    assert body["total"] == 1
    hit = body["items"][0]
    # The shape Phase 3 reuses for "papers consulted".
    assert set(hit) == {"paper", "score", "snippet"}
    assert hit["paper"]["id"] == paper
    assert hit["paper"]["authors"] == ["Ada Lovelace", "Alan Turing"]
    assert hit["score"] > 0
    assert "<mark>" in hit["snippet"]
    # Relevance is chosen automatically when there is query text.
    assert body["query"]["sort"] == "relevance"


def test_search_echoes_the_effective_query(client: TestClient, paper: int) -> None:
    body = client.get("/api/search?sort=relevance", headers=AUTH).json()
    # Relevance is meaningless while browsing, so it falls back rather than 400.
    assert body["query"]["sort"] == "added_at"
    assert body["items"][0]["score"] is None
    assert body["items"][0]["snippet"] is None


def test_search_rejects_an_unknown_status(client: TestClient) -> None:
    assert client.get("/api/search?status=finished", headers=AUTH).status_code == 422


@pytest.mark.parametrize(
    "path",
    [
        "/api/search?q=&topic_id=&status=&year_from=&year_to=&sort=",
        "/ui/results?q=&topic_id=&status=&year_from=&year_to=&sort=",
        "/ui/topics?topic_id=",
        "/?topic_id=",
    ],
)
def test_blank_filter_values_mean_unset(client: TestClient, paper: int, path: str) -> None:
    """Regression: an HTML form submits every control it holds, so an untouched
    filter arrives as `status=&year_from=`. Rejecting that 422s the library
    view on first paint. (FastAPI also drops the coercing validator when the
    param is spelled `= Query(...)` instead of `Annotated[...]` — this covers
    both halves.)"""
    response = client.get(path, headers=AUTH)
    assert response.status_code == 200, response.text


def test_search_requires_the_token(client: TestClient) -> None:
    assert client.get("/api/search?q=quantum").status_code == 401


# ------------------------------------------------------------------- topics
def test_topic_tree_counts_descendants(
    client: TestClient, conn: sqlite3.Connection, paper: int
) -> None:
    root, _ = get_or_create_topic(conn, "Physics")
    child, _ = get_or_create_topic(conn, "Quantum Information", root.id)
    conn.execute("INSERT INTO paper_topics (paper_id, topic_id) VALUES (?, ?)", (paper, child.id))

    body = client.get("/api/topics/tree", headers=AUTH).json()

    assert body["total_topics"] == 2
    physics = body["items"][0]
    assert physics["name"] == "Physics"
    assert physics["paper_count"] == 0
    assert physics["total_count"] == 1
    assert physics["children"][0]["name"] == "Quantum Information"


def test_patch_topic_renames_and_reparents(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    a, _ = get_or_create_topic(conn, "Alpha")
    b, _ = get_or_create_topic(conn, "Beta")

    renamed = client.patch(f"/api/topics/{b.id}", headers=AUTH, json={"name": "Beta Prime"})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Beta Prime"

    moved = client.patch(f"/api/topics/{b.id}", headers=AUTH, json={"parent_id": a.id})
    assert moved.json()["parent_id"] == a.id

    # parent_id is tri-state: omitted leaves it alone...
    kept = client.patch(f"/api/topics/{b.id}", headers=AUTH, json={"name": "Beta"})
    assert kept.json()["parent_id"] == a.id
    # ...explicit null promotes it to a root.
    promoted = client.patch(f"/api/topics/{b.id}", headers=AUTH, json={"parent_id": None})
    assert promoted.json()["parent_id"] is None


def test_patch_topic_refuses_a_cycle(client: TestClient, conn: sqlite3.Connection) -> None:
    parent, _ = get_or_create_topic(conn, "Parent")
    child, _ = get_or_create_topic(conn, "Child", parent.id)

    response = client.patch(f"/api/topics/{parent.id}", headers=AUTH, json={"parent_id": child.id})
    assert response.status_code == 422
    assert "cycle" in response.json()["detail"]


def test_patch_topic_name_conflict_is_409(client: TestClient, conn: sqlite3.Connection) -> None:
    get_or_create_topic(conn, "Optics")
    other, _ = get_or_create_topic(conn, "Acoustics")
    response = client.patch(f"/api/topics/{other.id}", headers=AUTH, json={"name": "optics"})
    assert response.status_code == 409


def test_merge_relinks_and_updates_the_tree(
    client: TestClient, conn: sqlite3.Connection, paper: int
) -> None:
    source, _ = get_or_create_topic(conn, "Transformers")
    target, _ = get_or_create_topic(conn, "Attention Mechanisms")
    conn.execute("INSERT INTO paper_topics (paper_id, topic_id) VALUES (?, ?)", (paper, source.id))

    response = client.post(
        f"/api/topics/{source.id}/merge", headers=AUTH, json={"into_topic_id": target.id}
    )
    assert response.status_code == 200
    assert response.json()["papers_relinked"] == 1

    # The paper moved across, the source is gone, and the tree agrees.
    detail = client.get(f"/api/papers/{paper}", headers=AUTH).json()
    assert [t["id"] for t in detail["topics"]] == [target.id]

    tree = client.get("/api/topics/tree", headers=AUTH).json()
    assert tree["total_topics"] == 1
    assert tree["items"][0]["total_count"] == 1

    # And it is reachable by filtering on the surviving topic.
    found = client.get(f"/api/search?topic_id={target.id}", headers=AUTH).json()
    assert [hit["paper"]["id"] for hit in found["items"]] == [paper]


def test_merge_into_itself_is_rejected(client: TestClient, conn: sqlite3.Connection) -> None:
    topic, _ = get_or_create_topic(conn, "Solo")
    response = client.post(
        f"/api/topics/{topic.id}/merge", headers=AUTH, json={"into_topic_id": topic.id}
    )
    assert response.status_code == 400


def test_create_topic(client: TestClient) -> None:
    created = client.post("/api/topics", headers=AUTH, json={"name": "Optics"})
    assert created.status_code == 201
    parent_id = created.json()["id"]

    child = client.post(
        "/api/topics", headers=AUTH, json={"name": "Lenses", "parent_id": parent_id}
    )
    assert child.json()["parent_id"] == parent_id

    assert client.post(
        "/api/topics", headers=AUTH, json={"name": "X", "parent_id": 9999}
    ).status_code == 404


# --------------------------------------------------------- reading workflow
@pytest.mark.parametrize("new_status", ["to_read", "reading", "read", "understood"])
def test_every_status_transition_is_allowed(
    client: TestClient, paper: int, new_status: str
) -> None:
    """Including going backwards — re-reading a paper is a normal thing to do,
    so the four statuses are a label, not a state machine."""
    client.patch(f"/api/papers/{paper}", headers=AUTH, json={"status": "understood"})

    response = client.patch(f"/api/papers/{paper}", headers=AUTH, json={"status": new_status})
    assert response.status_code == 200
    assert response.json()["status"] == new_status
    assert client.get(f"/api/papers/{paper}", headers=AUTH).json()["status"] == new_status


@pytest.mark.parametrize("bad", ["finished", "", None, "READ", 3])
def test_invalid_statuses_are_rejected(client: TestClient, paper: int, bad: object) -> None:
    response = client.patch(f"/api/papers/{paper}", headers=AUTH, json={"status": bad})
    assert response.status_code == 422
    # The stored value is untouched.
    assert client.get(f"/api/papers/{paper}", headers=AUTH).json()["status"] == "to_read"


def test_patch_unknown_paper_is_404(client: TestClient) -> None:
    assert (
        client.patch("/api/papers/9999", headers=AUTH, json={"status": "read"}).status_code == 404
    )


def test_status_filter_uses_the_new_value(client: TestClient, paper: int) -> None:
    client.patch(f"/api/papers/{paper}", headers=AUTH, json={"status": "reading"})
    assert client.get("/api/search?status=reading", headers=AUTH).json()["total"] == 1
    assert client.get("/api/search?status=to_read", headers=AUTH).json()["total"] == 0


# ------------------------------------------------------------- paper topics
def test_add_and_remove_a_topic(client: TestClient, conn: sqlite3.Connection, paper: int) -> None:
    topic, _ = get_or_create_topic(conn, "Quantum Information")

    added = client.post(f"/api/papers/{paper}/topics", headers=AUTH, json={"topic_id": topic.id})
    assert [t["id"] for t in added.json()["topics"]] == [topic.id]

    # Adding twice is idempotent, not a 409.
    again = client.post(f"/api/papers/{paper}/topics", headers=AUTH, json={"topic_id": topic.id})
    assert len(again.json()["topics"]) == 1

    removed = client.delete(f"/api/papers/{paper}/topics/{topic.id}", headers=AUTH)
    assert removed.json()["topics"] == []
    # Removing again is a no-op: the UI may send it from a stale chip.
    assert client.delete(f"/api/papers/{paper}/topics/{topic.id}", headers=AUTH).status_code == 200


def test_cannot_tag_with_a_topic_that_does_not_exist(client: TestClient, paper: int) -> None:
    """CLAUDE.md: topics are chosen from the taxonomy, never invented here."""
    response = client.post(f"/api/papers/{paper}/topics", headers=AUTH, json={"topic_id": 9999})
    assert response.status_code == 404


# -------------------------------------------------------------------- notes
def test_notes_crud_round_trip(client: TestClient, paper: int) -> None:
    assert client.get(f"/api/papers/{paper}/notes", headers=AUTH).json()["items"] == []

    created = client.post(f"/api/papers/{paper}/notes", headers=AUTH, json={"content": ""})
    assert created.status_code == 201
    note_id = created.json()["id"]

    saved = client.put(
        f"/api/papers/{paper}/notes/{note_id}",
        headers=AUTH,
        json={"content": "## Key idea\nStabiliser codes."},
    )
    assert saved.json()["content"] == "## Key idea\nStabiliser codes."

    # Persisted, not just echoed.
    listed = client.get(f"/api/papers/{paper}/notes", headers=AUTH).json()["items"]
    assert [n["content"] for n in listed] == ["## Key idea\nStabiliser codes."]

    assert client.delete(f"/api/papers/{paper}/notes/{note_id}", headers=AUTH).status_code == 204
    assert client.get(f"/api/papers/{paper}/notes", headers=AUTH).json()["items"] == []


def test_notes_are_scoped_to_their_paper(
    client: TestClient, conn: sqlite3.Connection, paper: int
) -> None:
    other, _ = get_or_create_by_sha256(conn, "b" * 64)
    note_id = client.post(f"/api/papers/{paper}/notes", headers=AUTH, json={}).json()["id"]

    assert client.get(f"/api/papers/{other}/notes/{note_id}", headers=AUTH).status_code == 404
    assert (
        client.put(
            f"/api/papers/{other}/notes/{note_id}", headers=AUTH, json={"content": "x"}
        ).status_code
        == 404
    )


def test_notes_on_an_unknown_paper_are_404(client: TestClient) -> None:
    assert client.get("/api/papers/9999/notes", headers=AUTH).status_code == 404
    assert client.post("/api/papers/9999/notes", headers=AUTH, json={}).status_code == 404


def test_notes_are_not_indexed_for_search(client: TestClient, paper: int) -> None:
    """Deliberate: a note about a paper must not outrank the paper itself."""
    note_id = client.post(f"/api/papers/{paper}/notes", headers=AUTH, json={}).json()["id"]
    client.put(
        f"/api/papers/{paper}/notes/{note_id}",
        headers=AUTH,
        json={"content": "zzyzx is a word I made up"},
    )
    assert client.get("/api/search?q=zzyzx", headers=AUTH).json()["total"] == 0


# ---------------------------------------------------------------------- pdf
def test_pdf_route_requires_auth_and_404s_without_a_stored_file(
    client: TestClient, paper: int
) -> None:
    assert client.get(f"/api/papers/{paper}/pdf").status_code == 401
    assert client.get(f"/api/papers/{paper}/pdf", headers=AUTH).status_code == 404


def test_pdf_route_serves_the_stored_bytes(
    client: TestClient, conn: sqlite3.Connection, fixture_pdf_bytes: bytes, settings
) -> None:
    from mouseion.services.hashing import sha256_bytes
    from mouseion.services.pdfs import store_pdf

    digest = sha256_bytes(fixture_pdf_bytes)
    store_pdf(fixture_pdf_bytes, settings.pdf_dir, digest)
    paper_id, _ = get_or_create_by_sha256(conn, digest)

    response = client.get(f"/api/papers/{paper_id}/pdf", headers=AUTH)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content == fixture_pdf_bytes


# ----------------------------------------------------------------------- ui
def test_the_templates_directory_was_found(settings) -> None:
    """Fails loudly rather than letting every UI test 404.

    `create_app` skips the UI router when templates/ is missing, so a wrong
    FRONTEND_DIR would otherwise look like a pile of routing bugs. See
    conftest._frontend_dir — the checkout and the container disagree about
    where frontend/ lives.
    """
    assert settings.templates_dir.is_dir(), f"no templates at {settings.templates_dir}"
    assert (settings.templates_dir / "base.html").is_file()
    assert settings.static_dir.is_dir()


def test_shells_are_public_and_carry_no_data(client: TestClient, paper: int) -> None:
    for path in ("/", f"/papers/{paper}", "/taxonomy"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "text/html" in response.headers["content-type"]
        # The whole point of the public shell: it is markup, not library data.
        assert "Quantum Error Correction" not in response.text


def test_upload_dialog_accepts_multiple_pdfs_and_newline_separated_urls(
    client: TestClient,
) -> None:
    shell = client.get("/").text
    assert 'id="upload-file"' in shell
    assert 'accept="application/pdf" multiple' in shell
    assert 'id="upload-url"' in shell
    assert "<textarea" in shell
    assert "One URL per line" in shell
    assert "Ingest all" in shell


def test_shell_for_a_missing_paper_still_renders(client: TestClient) -> None:
    """It must not confirm which ids exist — the 404 comes from the
    authenticated fragment instead."""
    assert client.get("/papers/9999").status_code == 200
    assert client.get("/ui/papers/9999", headers=AUTH).status_code == 404


def test_ui_fragments_require_the_token(client: TestClient, paper: int) -> None:
    for path in ("/ui/results", "/ui/topics", f"/ui/papers/{paper}", "/ui/taxonomy"):
        assert client.get(path).status_code == 401, path


def test_results_fragment_renders_cards_with_highlights(
    client: TestClient, paper: int
) -> None:
    response = client.get("/ui/results?q=quantum", headers=AUTH)
    assert response.status_code == 200
    assert "<mark>" in response.text
    assert f'href="/papers/{paper}"' in response.text
    assert "Quantum Error Correction" in response.text


def test_results_fragment_and_json_api_agree(
    client: TestClient, conn: sqlite3.Connection, paper: int
) -> None:
    topic, _ = get_or_create_topic(conn, "Physics")
    child, _ = get_or_create_topic(conn, "Quantum Information", topic.id)
    conn.execute("INSERT INTO paper_topics (paper_id, topic_id) VALUES (?, ?)", (paper, child.id))

    fragment = client.get(f"/ui/results?topic_id={topic.id}", headers=AUTH)
    payload = client.get(f"/api/search?topic_id={topic.id}", headers=AUTH).json()

    assert payload["total"] == 1
    assert f'data-paper-id="{paper}"' in fragment.text


def test_status_fragment_persists_the_change(client: TestClient, paper: int) -> None:
    response = client.post(f"/ui/papers/{paper}/status", headers=AUTH, data={"status": "reading"})
    assert response.status_code == 200
    assert "saved" in response.text
    assert client.get(f"/api/papers/{paper}", headers=AUTH).json()["status"] == "reading"


def test_note_fragments_round_trip(client: TestClient, paper: int) -> None:
    created = client.post(f"/ui/papers/{paper}/notes", headers=AUTH)
    assert created.status_code == 200
    note_id = client.get(f"/api/papers/{paper}/notes", headers=AUTH).json()["items"][0]["id"]

    saved = client.put(
        f"/ui/papers/{paper}/notes/{note_id}", headers=AUTH, data={"content": "hello"}
    )
    assert "Saved" in saved.text
    assert client.get(f"/api/papers/{paper}/notes", headers=AUTH).json()["items"][0][
        "content"
    ] == "hello"

    # Note content is escaped into the textarea, never interpolated as markup.
    client.put(
        f"/ui/papers/{paper}/notes/{note_id}", headers=AUTH, data={"content": "<script>x</script>"}
    )
    fragment = client.get(f"/ui/papers/{paper}/notes", headers=AUTH)
    assert "<script>x</script>" not in fragment.text
    assert "&lt;script&gt;" in fragment.text


def test_taxonomy_fragment_merges(client: TestClient, conn: sqlite3.Connection, paper: int) -> None:
    source, _ = get_or_create_topic(conn, "Transformers")
    target, _ = get_or_create_topic(conn, "Attention Mechanisms")
    conn.execute("INSERT INTO paper_topics (paper_id, topic_id) VALUES (?, ?)", (paper, source.id))

    response = client.post(
        f"/ui/taxonomy/topics/{source.id}/merge", headers=AUTH, data={"into_topic_id": target.id}
    )
    assert response.status_code == 200
    assert "1 paper(s) relinked" in response.text
    assert "Transformers" not in response.text


def test_taxonomy_fragment_reports_a_cycle(client: TestClient, conn: sqlite3.Connection) -> None:
    parent, _ = get_or_create_topic(conn, "Parent")
    child, _ = get_or_create_topic(conn, "Child", parent.id)
    response = client.post(
        f"/ui/taxonomy/topics/{parent.id}/reparent", headers=AUTH, data={"parent_id": child.id}
    )
    assert response.status_code == 422


def test_taxonomy_can_split_and_delete_topics(
    client: TestClient, conn: sqlite3.Connection, paper: int
) -> None:
    parent, _ = get_or_create_topic(conn, "Machine Learning")
    source, _ = get_or_create_topic(conn, "Architectures", parent.id)
    child, _ = get_or_create_topic(conn, "Transformers", source.id)
    conn.execute("INSERT INTO paper_topics (paper_id, topic_id) VALUES (?, ?)", (paper, source.id))

    split = client.post(
        f"/ui/taxonomy/topics/{source.id}/split",
        headers=AUTH,
        data={"name": "Graph Architectures", "paper_ids": str(paper)},
    )
    assert split.status_code == 200
    assert "Split 1 paper(s)" in split.text
    created = conn.execute(
        "SELECT id FROM topics WHERE name = 'Graph Architectures'"
    ).fetchone()["id"]
    assert conn.execute(
        "SELECT 1 FROM paper_topics WHERE paper_id = ? AND topic_id = ?", (paper, created)
    ).fetchone()

    deleted = client.delete(f"/ui/taxonomy/topics/{source.id}", headers=AUTH)
    assert deleted.status_code == 200
    assert "Deleted &#39;Architectures&#39;" in deleted.text
    assert conn.execute("SELECT id FROM topics WHERE id = ?", (source.id,)).fetchone() is None
    assert conn.execute("SELECT parent_id FROM topics WHERE id = ?", (child.id,)).fetchone()[
        "parent_id"
    ] == parent.id


def test_taxonomy_json_split_and_delete(
    client: TestClient, conn: sqlite3.Connection, paper: int
) -> None:
    source, _ = get_or_create_topic(conn, "Broad")
    conn.execute("INSERT INTO paper_topics (paper_id, topic_id) VALUES (?, ?)", (paper, source.id))
    response = client.post(
        f"/api/topics/{source.id}/split",
        headers=AUTH,
        json={"name": "Narrow", "paper_ids": [paper]},
    )
    assert response.status_code == 200
    assert response.json()["papers_moved"] == 1

    response = client.delete(f"/api/topics/{source.id}", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["deleted"]["name"] == "Broad"


def test_paper_fragment_shows_the_phase_3_and_4_seams(client: TestClient, paper: int) -> None:
    """Both Open WebUI handoffs preserve their scope-tag contracts."""
    text = client.get(f"/ui/papers/{paper}", headers=AUTH).text
    assert f'data-chat-prefix="[paper:{paper}]"' in text
    assert f'data-chat-prefix="[test:{paper}]"' in text
    assert 'data-openwebui-url="http://localhost:3000"' in text
    assert "data-open-paper-chat" in text
    assert "data-open-test-chat" in text
    assert "data-quick-qa-form" in text
