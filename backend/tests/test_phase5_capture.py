"""Phase 5 phone capture: share normalization, PWA metadata, and pairing."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from mouseion.services.pairing import PairingTokenError, consume_pairing_token, create_pairing_token
from mouseion.services.sharing import extract_shared_url

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def capture_client(data_dir) -> Iterator[TestClient]:  # noqa: ANN001
    from mouseion.main import create_app

    with TestClient(create_app()) as client:
        yield client


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                "text": "Interesting result — read later:\n"
                "https://arxiv.org/abs/1706.03762v7 (shared from Chrome)"
            },
            "https://arxiv.org/abs/1706.03762v7",
        ),
        (
            {
                "text": "Paper https://example.test/landing and PDF "
                "https://export.arxiv.org/pdf/2301.00234.pdf?download=1"
            },
            "https://arxiv.org/abs/2301.00234",
        ),
        ({"title": "Worth reading arXiv: 2203.02155."}, "https://arxiv.org/abs/2203.02155"),
        ({"url": "https://papers.example.test/paper.pdf"}, "https://papers.example.test/paper.pdf"),
    ],
)
def test_extract_shared_url_from_messy_share_text(payload: dict[str, str], expected: str) -> None:
    assert extract_shared_url(
        shared_url=payload.get("url", ""),
        text=payload.get("text", ""),
        title=payload.get("title", ""),
    ) == expected


def test_share_extract_api_is_authenticated(capture_client: TestClient) -> None:
    denied = capture_client.post("/api/share/extract", json={"text": "arXiv:1706.03762"})
    assert denied.status_code == 401
    response = capture_client.post(
        "/api/share/extract",
        headers=AUTH,
        json={"text": "See https://arxiv.org/pdf/1706.03762.pdf)."},
    )
    assert response.status_code == 200
    assert response.json() == {"url": "https://arxiv.org/abs/1706.03762"}


def test_pairing_token_is_single_use(capture_client: TestClient) -> None:
    created = capture_client.post("/api/pairing", headers=AUTH)
    assert created.status_code == 201
    body = created.json()
    assert body["qr_data_url"].startswith("data:image/svg+xml;base64,")
    token = parse_qs(urlparse(body["pair_url"]).query)["token"][0]

    consumed = capture_client.post("/api/pairing/consume", json={"token": token})
    assert consumed.status_code == 200
    assert consumed.json() == {"api_token": "test-token"}
    assert capture_client.post("/api/pairing/consume", json={"token": token}).status_code == 410


def test_pairing_token_expires(conn) -> None:  # noqa: ANN001
    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    pairing = create_pairing_token(conn, ttl_minutes=5, now=now)
    with pytest.raises(PairingTokenError):
        consume_pairing_token(conn, pairing.token, now=now + timedelta(minutes=5, seconds=1))


def test_pwa_shell_assets_are_public_and_manifest_has_both_share_shapes(
    capture_client: TestClient,
) -> None:
    assert capture_client.get("/share").status_code == 200
    assert capture_client.get("/pair").status_code == 200
    manifest_response = capture_client.get("/manifest.webmanifest")
    assert manifest_response.status_code == 200
    manifest = manifest_response.json()
    assert manifest["share_target"]["params"]["url"] == "url"
    assert manifest["share_target"]["params"]["text"] == "text"
    assert manifest["share_target"]["params"]["files"][0]["accept"] == [
        "application/pdf",
        ".pdf",
    ]
    assert {icon["sizes"] for icon in manifest["icons"]} >= {"192x192", "512x512"}
    assert capture_client.get("/static/icon-192.png").headers["content-type"] == "image/png"
    assert capture_client.get("/static/icon-512.png").headers["content-type"] == "image/png"
    worker = capture_client.get("/sw.js")
    assert worker.status_code == 200
    assert worker.headers["service-worker-allowed"] == "/"
    assert 'event.request.method === "POST"' in worker.text
