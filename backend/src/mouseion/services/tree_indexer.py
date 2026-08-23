"""Document tree index — our interface, PageIndex behind it.

This module is a seam, and that is its whole point. Phase 3's QA will call
`load_tree(...)` and `navigate(tree, question)` from here and will never import
`pageindex`, so swapping the tree builder (or dropping PageIndex entirely) is a
change to this file alone.

Two implementations ship in Phase 1:

* `PageIndexTreeIndexer` — the open-source PageIndex library, with its LLM
  client pointed at OpenRouter (CLAUDE.md: ALL LLM calls go through OpenRouter,
  "including PageIndex's internal calls").
* `HeuristicTreeIndexer` — PDF bookmarks, else detected headings, else page
  blocks. No LLM calls, always available; this is what runs in tests and on a
  box with no API key.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from mouseion.config import Settings, get_settings
from mouseion.services.llm import LLMClient, LLMMetrics, LLMTask, get_llm_client
from mouseion.services.pdfs import ExtractedText, extract_outline
from mouseion.services.prompts import build_tree_navigation_prompt, extract_section_headers

log = logging.getLogger(__name__)

PAGES_PER_FALLBACK_NODE = 5


# --------------------------------------------------------------------------
# Our types. Nothing outside this module should know PageIndex's shapes.
# --------------------------------------------------------------------------
@dataclass(slots=True)
class TreeNode:
    node_id: str
    title: str
    level: int = 0
    start_page: int | None = None
    end_page: int | None = None
    summary: str | None = None
    children: list[TreeNode] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "title": self.title,
            "level": self.level,
            "start_page": self.start_page,
            "end_page": self.end_page,
            "summary": self.summary,
            "children": [c.to_dict() for c in self.children],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TreeNode:
        return cls(
            node_id=str(data.get("node_id", "")),
            title=str(data.get("title", "")),
            level=int(data.get("level", 0) or 0),
            start_page=data.get("start_page"),
            end_page=data.get("end_page"),
            summary=data.get("summary"),
            children=[cls.from_dict(c) for c in data.get("children", [])],
        )


@dataclass(slots=True)
class TreeDocument:
    sha256: str
    indexer: str
    nodes: list[TreeNode] = field(default_factory=list)
    model: str | None = None
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    def iter_nodes(self) -> Iterator[TreeNode]:
        def walk(nodes: Sequence[TreeNode]) -> Iterator[TreeNode]:
            for node in nodes:
                yield node
                yield from walk(node.children)

        return walk(self.nodes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "indexer": self.indexer,
            "model": self.model,
            "created_at": self.created_at,
            "nodes": [n.to_dict() for n in self.nodes],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TreeDocument:
        return cls(
            sha256=str(data.get("sha256", "")),
            indexer=str(data.get("indexer", "unknown")),
            model=data.get("model"),
            created_at=str(data.get("created_at", "")),
            nodes=[TreeNode.from_dict(n) for n in data.get("nodes", [])],
        )


@runtime_checkable
class TreeIndexer(Protocol):
    """What Phase 3 depends on. Implementations must be idempotent."""

    name: str

    async def build_tree(
        self, pdf_path: Path, sha256: str, extracted: ExtractedText | None = None
    ) -> TreeDocument: ...


# --------------------------------------------------------------------------
# Heuristic implementation
# --------------------------------------------------------------------------
class HeuristicTreeIndexer:
    """Structure without an LLM: bookmarks → headings → fixed page blocks."""

    name = "heuristic"

    async def build_tree(
        self, pdf_path: Path, sha256: str, extracted: ExtractedText | None = None
    ) -> TreeDocument:
        nodes = await asyncio.to_thread(self._build, pdf_path, extracted)
        return TreeDocument(sha256=sha256, indexer=self.name, nodes=nodes)

    def _build(self, pdf_path: Path, extracted: ExtractedText | None) -> list[TreeNode]:
        outline = extract_outline(pdf_path)
        if outline:
            return self._from_outline(outline)
        if extracted and extracted.page_texts:
            headings = self._from_headings(extracted)
            if headings:
                return headings
            return self._from_pages(extracted)
        return []

    def _from_outline(self, outline: Sequence[Any]) -> list[TreeNode]:
        roots: list[TreeNode] = []
        stack: list[TreeNode] = []
        for index, entry in enumerate(outline):
            node = TreeNode(
                node_id=f"n{index}",
                title=entry.title,
                level=max(0, entry.level - 1),
                start_page=entry.page,
            )
            while stack and stack[-1].level >= node.level:
                stack.pop()
            if stack:
                stack[-1].children.append(node)
            else:
                roots.append(node)
            stack.append(node)
        _close_page_ranges(roots)
        return roots

    def _from_headings(self, extracted: ExtractedText) -> list[TreeNode]:
        nodes: list[TreeNode] = []
        for page_number, page_text in enumerate(extracted.page_texts, start=1):
            for heading in extract_section_headers(page_text):
                nodes.append(
                    TreeNode(
                        node_id=f"h{len(nodes)}",
                        title=heading,
                        level=0,
                        start_page=page_number,
                    )
                )
        _close_page_ranges(nodes)
        return nodes

    def _from_pages(self, extracted: ExtractedText) -> list[TreeNode]:
        nodes: list[TreeNode] = []
        for start in range(0, extracted.n_pages, PAGES_PER_FALLBACK_NODE):
            end = min(start + PAGES_PER_FALLBACK_NODE, extracted.n_pages)
            nodes.append(
                TreeNode(
                    node_id=f"p{start + 1}-{end}",
                    title=f"Pages {start + 1}–{end}",
                    start_page=start + 1,
                    end_page=end,
                    summary=(extracted.page_texts[start][:300] or None),
                )
            )
        return nodes


def _close_page_ranges(nodes: Sequence[TreeNode]) -> None:
    """Give each node an end_page: the page before its next sibling starts."""
    flat = sorted(
        (n for n in _flatten(nodes) if n.start_page is not None),
        key=lambda n: n.start_page or 0,
    )
    for current, following in zip(flat, flat[1:]):
        if current.end_page is None:
            current.end_page = max(current.start_page or 0, (following.start_page or 1) - 1)


def _flatten(nodes: Sequence[TreeNode]) -> Iterator[TreeNode]:
    for node in nodes:
        yield node
        yield from _flatten(node.children)


# --------------------------------------------------------------------------
# PageIndex implementation
# --------------------------------------------------------------------------
def pageindex_available() -> bool:
    try:
        import pageindex  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


class PageIndexUnavailableError(RuntimeError):
    pass


class PageIndexTreeIndexer:
    """Adapter over the open-source PageIndex tree builder.

    OpenRouter routing: PageIndex builds its client with `openai.OpenAI()`,
    which reads OPENAI_API_KEY/OPENAI_BASE_URL from the environment, so pointing
    it at OpenRouter is a matter of exporting those two names. The model is
    passed as `openai/<MODEL_INGEST>` because PageIndex sends any model id
    containing a slash down its litellm path, which would ignore the base URL
    and try to reach the provider directly.
    """

    name = "pageindex"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def build_tree(
        self, pdf_path: Path, sha256: str, extracted: ExtractedText | None = None
    ) -> TreeDocument:
        if not pageindex_available():
            raise PageIndexUnavailableError(
                "pageindex is not installed — pip install -r requirements-pageindex.txt, "
                "or set TREE_INDEXER=heuristic"
            )
        if not self.settings.openrouter_api_key:
            raise PageIndexUnavailableError("OPENROUTER_API_KEY is required by PageIndex")

        raw = await asyncio.to_thread(self._run, pdf_path)
        model = f"openai/{self.settings.model_ingest}"
        return TreeDocument(
            sha256=sha256,
            indexer=self.name,
            model=model,
            nodes=_convert_pageindex_nodes(_structure_of(raw)),
        )

    def _run(self, pdf_path: Path) -> Any:
        import pageindex

        # Scoped to this process; the worker never calls OpenAI directly.
        os.environ["OPENAI_API_KEY"] = self.settings.openrouter_api_key
        os.environ["OPENAI_BASE_URL"] = self.settings.openrouter_base_url

        model = f"openai/{self.settings.model_ingest}"
        wanted: dict[str, Any] = {
            "model": model,
            "summary_model": model,
            "mode": self.settings.pageindex_mode,
        }

        fn = pageindex.page_index
        try:
            accepted = set(inspect.signature(fn).parameters)
            kwargs = {k: v for k, v in wanted.items() if k in accepted}
        except (TypeError, ValueError):  # unintrospectable wrapper
            kwargs = wanted

        try:
            return fn(str(pdf_path), **kwargs)
        except TypeError as exc:
            # Signature drifted between PageIndex releases; the positional call
            # still works and the model comes from config.yaml/env.
            log.warning("pageindex kwargs %s rejected (%s); retrying bare", sorted(kwargs), exc)
            return fn(str(pdf_path))


def _structure_of(raw: Any) -> list[dict[str, Any]]:
    """PageIndex returns either a bare list of nodes or a dict wrapping one."""
    if isinstance(raw, list):
        return [n for n in raw if isinstance(n, dict)]
    if isinstance(raw, dict):
        for key in ("structure", "nodes", "tree", "result"):
            value = raw.get(key)
            if isinstance(value, list):
                return [n for n in value if isinstance(n, dict)]
    log.warning("unrecognized PageIndex result of type %s", type(raw).__name__)
    return []


def _convert_pageindex_nodes(nodes: Sequence[dict[str, Any]], level: int = 0) -> list[TreeNode]:
    """Map PageIndex's node dicts onto ours, tolerating key drift."""
    converted: list[TreeNode] = []
    for index, node in enumerate(nodes):
        children = node.get("nodes") or node.get("children") or []
        converted.append(
            TreeNode(
                node_id=str(node.get("node_id") or node.get("id") or f"{level}-{index}"),
                title=str(node.get("title") or node.get("name") or "").strip(),
                level=level,
                start_page=_as_page(node, "start_index", "start_page", "page"),
                end_page=_as_page(node, "end_index", "end_page"),
                summary=(node.get("summary") or node.get("text") or None),
                children=_convert_pageindex_nodes(
                    [c for c in children if isinstance(c, dict)], level + 1
                ),
            )
        )
    return converted


def _as_page(node: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = node.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


# --------------------------------------------------------------------------
# Selection, persistence, navigation
# --------------------------------------------------------------------------
def get_tree_indexer(settings: Settings | None = None) -> TreeIndexer:
    settings = settings or get_settings()
    choice = settings.tree_indexer

    if choice == "heuristic":
        return HeuristicTreeIndexer()
    if choice == "pageindex":
        return PageIndexTreeIndexer(settings)

    # auto: PageIndex when it can actually run, heuristic otherwise.
    if pageindex_available() and settings.openrouter_api_key:
        return PageIndexTreeIndexer(settings)
    log.info("tree indexer: falling back to heuristic (pageindex unavailable or no API key)")
    return HeuristicTreeIndexer()


def tree_path(sha256: str, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return settings.tree_dir / f"{sha256}.json"


def save_tree(tree: TreeDocument, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    path = tree_path(tree.sha256, settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(tree.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def load_tree_by_sha(sha256: str, settings: Settings | None = None) -> TreeDocument | None:
    path = tree_path(sha256, settings)
    if not path.exists():
        return None
    try:
        return TreeDocument.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("tree %s unreadable: %s", path, exc)
        return None


def load_tree(
    paper_id: int, conn: sqlite3.Connection, settings: Settings | None = None
) -> TreeDocument | None:
    """Phase 3 entry point: the tree for a paper, or None if it has none."""
    row = conn.execute("SELECT sha256 FROM papers WHERE id = ?", (paper_id,)).fetchone()
    if row is None:
        return None
    return load_tree_by_sha(row["sha256"], settings)


class TreeNavigationSelection(BaseModel):
    node_ids: list[str] = Field(default_factory=list, max_length=10)


async def navigate(
    tree: TreeDocument,
    question: str,
    limit: int = 5,
    *,
    client: LLMClient | None = None,
    metrics: LLMMetrics | None = None,
    text_budget: int | None = None,
) -> list[TreeNode]:
    """Use MODEL_QA to select relevant nodes from a stored tree.

    The first three parameters preserve the Phase 1 seam. Phase 3 makes the
    operation asynchronous because tree descent is now a budgeted LLM call.
    Invalid ids are ignored; if a model selects nothing, the old lexical ranker
    remains a deterministic fallback rather than returning an empty paper.
    """
    nodes = list(tree.iter_nodes())
    if not nodes or not question.strip() or limit <= 0:
        return []

    settings = get_settings()
    prompt = build_tree_navigation_prompt(
        question=question,
        nodes=[(node.node_id, node.title, node.summary) for node in nodes],
        text_budget=text_budget or settings.qa_tree_budget_chars,
    )
    selection = await (client or get_llm_client()).complete_json(
        task=LLMTask.QA,
        system=prompt.system,
        user=prompt.user,
        output_model=TreeNavigationSelection,
        metrics=metrics,
    )
    by_id = {node.node_id: node for node in nodes}
    selected: list[TreeNode] = []
    seen: set[str] = set()
    for node_id in selection.node_ids:
        if node_id in by_id and node_id not in seen:
            selected.append(by_id[node_id])
            seen.add(node_id)
        if len(selected) >= limit:
            break
    return selected or _navigate_lexically(tree, question, limit)


def _navigate_lexically(tree: TreeDocument, question: str, limit: int) -> list[TreeNode]:
    """Phase 1's ranker, retained only as a no-selection fallback."""
    terms = {t for t in _tokenize(question) if len(t) > 2}
    if not terms:
        return []

    scored: list[tuple[float, TreeNode]] = []
    for node in tree.iter_nodes():
        haystack = _tokenize(f"{node.title} {node.summary or ''}")
        if not haystack:
            continue
        overlap = terms & set(haystack)
        if overlap:
            # Title hits count double: a section called "Ablations" is a better
            # answer to "what ablations?" than a body mentioning the word once.
            title_hits = len(terms & set(_tokenize(node.title)))
            scored.append((len(overlap) + title_hits, node))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [node for _, node in scored[:limit]]


def _tokenize(text: str) -> list[str]:
    return [t for t in "".join(c.lower() if c.isalnum() else " " for c in text).split() if t]
