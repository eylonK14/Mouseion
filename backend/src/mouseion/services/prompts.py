"""Prompt construction for the combined ingest call.

Everything here is deterministic — same taxonomy plus same paper produces the
same bytes — because tests/test_prompts.py pins the output against a golden
file. Change the wording and the golden file must change with it, on purpose.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from mouseion.services.taxonomy import TopicRow

PROMPT_VERSION = "ingest/v1"

EMPTY_TAXONOMY = "(empty — the library has no topics yet, so every topic must be a new proposal)"

SYSTEM_PROMPT = f"""\
You are the librarian of a single researcher's personal paper library.
Prompt version: {PROMPT_VERSION}

You read one paper and return ONE JSON object with: any requested bibliographic
metadata, the topics it belongs to, and two summaries.

TOPIC RULES — these are the rules of the library, not suggestions:
1. You are shown the CURRENT TAXONOMY as an indented tree. Every line is
   `- [id] Name`.
2. For each topic slot, do exactly ONE of:
   a. PICK an existing topic: set `existing_topic_id` to its id and leave
      `new_topic_name` null.
   b. PROPOSE a new topic: set `new_topic_name`, and set `parent` to the id of
      the existing topic it belongs under (or null if it is genuinely a new
      root-level area).
3. NEVER invent free-form tags, keywords, or phrases outside this structure.
4. NEVER propose a name that is a near-duplicate of an existing topic — that
   includes different casing, singular/plural, punctuation, abbreviations, and
   word order ("LLMs" vs "Large Language Models", "Transformer" vs
   "Transformers"). If an existing topic means the same thing, PICK it.
5. Prefer picking. Only propose when no existing topic honestly fits.
6. Assign 1 to 6 topics: the subject areas a person would browse to find this
   paper, not a description of every technique it mentions.
7. Never use an id that does not appear in the tree above.

SUMMARY RULES:
- `summary_short`: exactly one sentence stating what the paper does.
- `summary_long`: one paragraph covering the idea, why it matters, and what the
  paper discusses.
- Write plainly and concretely. No marketing language, no "this paper".

METADATA RULES:
- Only fill fields listed as missing. Fields already known are authoritative;
  do not restate, "correct", or contradict them.
- If a missing field is not determinable from the text, return null for it.
- `authors` is a list of full names in the order they appear on the paper.

Return only the JSON object.
"""

# Headings we keep when a paper is too long to send whole: numbered sections
# ("3 Method", "4.2 Ablations"), and the standard unnumbered ones.
_NUMBERED_HEADING = re.compile(r"^\s{0,4}(\d{1,2}(?:\.\d{1,2}){0,2})\.?\s+([A-Z][^\n]{2,80})$")
_NAMED_HEADING = re.compile(
    r"^\s{0,4}((?:abstract|introduction|background|related work|method(?:s|ology)?|"
    r"approach|model|architecture|experiments?|results?|evaluation|ablations?|"
    r"discussion|limitations|conclusions?|future work|references|appendix)"
    r"[^\n]{0,60})$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class IngestPrompt:
    system: str
    user: str


def render_taxonomy_tree(topics: Sequence[TopicRow]) -> str:
    """Render the taxonomy as the indented `- [id] Name` tree the prompt promises.

    Children are sorted by name so the rendering is stable across runs; orphans
    (a parent that was deleted) are rendered at the root rather than vanishing.
    """
    if not topics:
        return EMPTY_TAXONOMY

    by_parent: dict[int | None, list[TopicRow]] = {}
    ids = {t.id for t in topics}
    for topic in topics:
        parent = topic.parent_id if topic.parent_id in ids else None
        by_parent.setdefault(parent, []).append(topic)
    for children in by_parent.values():
        children.sort(key=lambda t: (t.name.lower(), t.id))

    lines: list[str] = []

    def walk(parent_id: int | None, depth: int) -> None:
        for topic in by_parent.get(parent_id, []):
            lines.append(f"{'  ' * depth}- [{topic.id}] {topic.name}")
            walk(topic.id, depth + 1)

    walk(None, 0)
    return "\n".join(lines)


def extract_section_headers(text: str) -> list[str]:
    headers: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or len(line) > 90:
            continue
        match = _NUMBERED_HEADING.match(line) or _NAMED_HEADING.match(line)
        if not match:
            continue
        key = line.lower()
        if key not in seen:
            seen.add(key)
            headers.append(line)
    return headers


def bound_paper_text(text: str, budget: int) -> str:
    """Bound the paper text sent to the model.

    Short papers go whole. Long ones become head + the section headers from the
    remainder, so the model still sees the shape of what it is summarizing
    instead of being cut off mid-page.
    """
    text = (text or "").strip()
    if len(text) <= budget:
        return text

    head_budget = max(1, int(budget * 0.8))
    head = text[:head_budget].rstrip()
    headers = extract_section_headers(text[head_budget:])

    parts = [head, "", "[... middle of the paper omitted ...]"]
    if headers:
        parts += ["", "SECTION HEADINGS FROM THE OMITTED PART:"]
        parts += [f"- {h}" for h in headers]
    return "\n".join(parts)


def _render_known(known: dict[str, object]) -> str:
    present = {k: v for k, v in known.items() if v not in (None, "", [], {})}
    if not present:
        return "(nothing known yet — supply every metadata field)"
    lines = []
    for key in ("title", "authors", "year", "venue"):
        if key in present:
            value = present[key]
            rendered = ", ".join(str(v) for v in value) if isinstance(value, list) else value
            lines.append(f"- {key}: {rendered}")
    return "\n".join(lines)


def build_ingest_prompt(
    *,
    paper_text: str,
    topics: Sequence[TopicRow],
    known_metadata: dict[str, object] | None = None,
    missing_metadata: Sequence[str] = (),
    text_budget: int = 24_000,
    source_note: str | None = None,
) -> IngestPrompt:
    """Assemble the single combined ingest call (metadata + topics + summaries)."""
    known = known_metadata or {}
    missing = list(missing_metadata)

    sections = [
        "## CURRENT TAXONOMY",
        "",
        render_taxonomy_tree(topics),
        "",
        "## METADATA ALREADY KNOWN (authoritative — do not contradict)",
        "",
        _render_known(known),
        "",
        "## METADATA YOU MUST SUPPLY",
        "",
        (
            "\n".join(f"- {name}" for name in missing)
            if missing
            else "(none — every field is already known; return null for all metadata fields)"
        ),
    ]

    if source_note:
        sections += ["", "## SOURCE", "", source_note]

    sections += [
        "",
        "## PAPER TEXT",
        "",
        bound_paper_text(paper_text, text_budget),
    ]

    return IngestPrompt(system=SYSTEM_PROMPT, user="\n".join(sections))
