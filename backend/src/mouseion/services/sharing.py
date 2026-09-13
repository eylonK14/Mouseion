"""Normalize the inconsistent URL/text payloads emitted by phone share sheets."""

from __future__ import annotations

import re

from mouseion.services.arxiv import normalize_arxiv_url

_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_ARXIV_ID = re.compile(
    r"(?<![\w./-])(?:arxiv\s*:\s*)?"
    r"((?:\d{4}\.\d{4,5}|[a-z][a-z-]*(?:\.[A-Za-z]{2})?/\d{7})(?:v\d+)?)",
    re.IGNORECASE,
)
_TRAILING = ".,;:!?)]}>\"'"


def extract_shared_url(*, shared_url: str = "", text: str = "", title: str = "") -> str:
    """Return an arXiv URL found anywhere, otherwise the first shared HTTP URL.

    Share sheets commonly put prose, a title, and a URL into one ``text`` field.
    arXiv is intentionally preferred because normalizing it preserves Mouseion's
    API-metadata and deduplication behavior.
    """
    combined = "\n".join(part for part in (shared_url, text, title) if part)
    urls = [match.group(0).rstrip(_TRAILING) for match in _URL.finditer(combined)]
    for candidate in urls:
        if ref := normalize_arxiv_url(candidate):
            return ref.abs_url
    if match := _ARXIV_ID.search(combined):
        if ref := normalize_arxiv_url(match.group(1)):
            return ref.abs_url
    if shared_url.strip().lower().startswith(("http://", "https://")):
        return shared_url.strip().rstrip(_TRAILING)
    if urls:
        return urls[0]
    raise ValueError("No paper URL was found in the shared content")
