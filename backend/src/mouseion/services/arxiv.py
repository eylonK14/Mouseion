"""arXiv URL normalization and Atom API metadata.

CLAUDE.md: "arXiv URLs: rewrite /abs/→/pdf/, fetch metadata from the arXiv API
instead of LLM-extracting it." Metadata that comes from here is authoritative
and is never overwritten by the ingest LLM call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from xml.etree import ElementTree

import httpx

ATOM_NS = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"

_HOSTS = {"arxiv.org", "www.arxiv.org", "export.arxiv.org", "browse.arxiv.org"}

# Post-2007 identifiers (2301.00234v2) and the pre-2007 archive/number form
# (math.GT/0309136, cs/0112017). Versions are preserved everywhere: asking the
# API for v2 and downloading v1 would silently mismatch.
_NEW_ID = r"\d{4}\.\d{4,5}(?:v\d+)?"
_OLD_ID = r"[a-z][a-z\-]*(?:\.[A-Za-z]{2})?/\d{7}(?:v\d+)?"

_URL_RE = re.compile(
    rf"^(?:https?://)?(?:{'|'.join(re.escape(h) for h in _HOSTS)})"
    rf"/(?:abs|pdf|ps|format|html)/({_NEW_ID}|{_OLD_ID})(?:\.pdf)?/?(?:[?#].*)?$",
    re.IGNORECASE,
)
_BARE_RE = re.compile(rf"^(?:arxiv:)?({_NEW_ID}|{_OLD_ID})$", re.IGNORECASE)
_VERSION_SUFFIX = re.compile(r"v\d+$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ArxivRef:
    """A normalized arXiv identifier plus the canonical URLs for it."""

    arxiv_id: str  # may carry a version suffix, e.g. "2301.00234v2"

    @property
    def id_without_version(self) -> str:
        return _VERSION_SUFFIX.sub("", self.arxiv_id)

    @property
    def abs_url(self) -> str:
        return f"https://arxiv.org/abs/{self.arxiv_id}"

    @property
    def pdf_url(self) -> str:
        return f"https://arxiv.org/pdf/{self.arxiv_id}.pdf"


@dataclass(slots=True)
class ArxivMetadata:
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    abstract: str | None = None
    venue: str | None = None
    doi: str | None = None


def normalize_arxiv_url(value: str) -> ArxivRef | None:
    """Return an `ArxivRef` for any arXiv abs/pdf/html URL or bare id.

    Returns None for anything that is not arXiv, which is the caller's signal to
    fall back to a direct PDF download.
    """
    candidate = (value or "").strip()
    if not candidate:
        return None
    for pattern in (_URL_RE, _BARE_RE):
        match = pattern.match(candidate)
        if match:
            arxiv_id = match.group(1)
            # Old-style ids keep their lowercase archive but the subject class
            # is case-sensitive (math.GT), so only the new form is safe to fold.
            if re.fullmatch(_NEW_ID, arxiv_id, re.IGNORECASE):
                arxiv_id = arxiv_id.lower()
            return ArxivRef(arxiv_id=arxiv_id)
    return None


def _clean(text: str | None) -> str | None:
    """arXiv wraps text at ~80 columns; collapse it back into one line."""
    if text is None:
        return None
    collapsed = re.sub(r"\s+", " ", text).strip()
    return collapsed or None


def parse_atom(xml_text: str) -> ArxivMetadata:
    """Parse one entry out of an arXiv Atom response.

    Raises ValueError when the feed has no entry, or carries arXiv's "Error"
    entry (which is what a bad id returns — HTTP 200 with an error body).
    """
    root = ElementTree.fromstring(xml_text)
    entry = root.find(f"{ATOM_NS}entry")
    if entry is None:
        raise ValueError("arXiv response contained no entry")

    title = _clean(entry.findtext(f"{ATOM_NS}title"))
    if title == "Error":
        detail = _clean(entry.findtext(f"{ATOM_NS}summary")) or "unknown error"
        raise ValueError(f"arXiv API error: {detail}")

    authors = [
        name
        for author in entry.findall(f"{ATOM_NS}author")
        if (name := _clean(author.findtext(f"{ATOM_NS}name")))
    ]

    year: int | None = None
    published = entry.findtext(f"{ATOM_NS}published")
    if published and len(published) >= 4 and published[:4].isdigit():
        year = int(published[:4])

    return ArxivMetadata(
        title=title,
        authors=authors,
        year=year,
        abstract=_clean(entry.findtext(f"{ATOM_NS}summary")),
        venue=_clean(entry.findtext(f"{ARXIV_NS}journal_ref")),
        doi=_clean(entry.findtext(f"{ARXIV_NS}doi")),
    )


async def fetch_metadata(client: httpx.AsyncClient, ref: ArxivRef, api_base: str) -> ArxivMetadata:
    response = await client.get(api_base, params={"id_list": ref.arxiv_id, "max_results": 1})
    response.raise_for_status()
    return parse_atom(response.text)
