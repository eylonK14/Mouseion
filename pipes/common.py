"""Hot-reloaded transport helpers shared by the two Open WebUI pipes."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, AsyncIterator, Literal, Sequence

import httpx


@dataclass(frozen=True, slots=True)
class ParsedTag:
    status: Literal["present", "absent", "malformed"]
    value: str | int | None
    text: str


def parse_scope_tag(text: str, kind: Literal["paper", "topic"]) -> ParsedTag:
    """Parse one leading scope tag without accepting lookalike malformed tags."""
    source = text or ""
    valid = re.match(rf"^\s*\[{kind}:([^\]\r\n]+)\]\s*", source, re.IGNORECASE)
    if valid:
        raw = valid.group(1).strip()
        rest = source[valid.end() :].strip()
        if kind == "paper":
            if not raw.isdigit() or int(raw) <= 0:
                return ParsedTag("malformed", None, source)
            return ParsedTag("present", int(raw), rest)
        if not raw:
            return ParsedTag("malformed", None, source)
        return ParsedTag("present", int(raw) if raw.isdigit() else raw, rest)
    if re.match(rf"^\s*\[{kind}(?::|\])", source, re.IGNORECASE):
        return ParsedTag("malformed", None, source)
    return ParsedTag("absent", None, source.strip())


def _text_content(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(content or "")


def clean_messages(
    raw_messages: Sequence[dict[str, Any]], kind: Literal["paper", "topic"]
) -> tuple[list[dict[str, str]], ParsedTag | None, str | None]:
    """Strip scope tags, retaining the first valid lock across the transcript."""
    cleaned: list[dict[str, str]] = []
    lock: ParsedTag | None = None
    malformed: str | None = None
    for message in raw_messages:
        role = str(message.get("role", ""))
        if role not in {"user", "assistant"}:
            continue
        text = _text_content(message)
        parsed = parse_scope_tag(text, kind) if role == "user" else None
        if parsed and parsed.status == "present":
            if lock is None:
                lock = parsed
            text = parsed.text
        elif parsed and parsed.status == "malformed" and lock is None:
            malformed = (
                "Malformed paper scope tag. Use [paper:12]."
                if kind == "paper"
                else "Malformed topic scope tag. Use [topic:id-or-name]."
            )
        text = text.strip()
        # A tag-only first message exists solely to establish the pipe lock.
        # Once its tag is stripped it must not become an empty QAMessage: the
        # backend correctly rejects empty conversation content with HTTP 422.
        if not text:
            continue
        cleaned.append({"role": role, "content": text})
    return cleaned, lock, malformed


def split_latest_question(messages: Sequence[dict[str, str]]) -> tuple[str, list[dict[str, str]]]:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message["role"] == "user" and message["content"].strip():
            return message["content"].strip(), list(messages[:index])
    return "", []


def first_user_text(messages: Sequence[dict[str, str]]) -> str:
    for message in messages:
        if message["role"] == "user" and message["content"].strip():
            return message["content"].strip()
    return ""


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }


async def _read_error(response: httpx.Response) -> str:
    body = (await response.aread()).decode(errors="replace")
    try:
        detail = json.loads(body).get("detail", body)
    except (json.JSONDecodeError, AttributeError):
        detail = body
    return f"Mouseion returned {response.status_code}: {str(detail)[:400]}"


async def relay_sse(
    *, api_base_url: str, token: str, path: str, payload: dict[str, Any]
) -> AsyncIterator[str]:
    """Translate Mouseion's named SSE events into Open WebUI text chunks."""
    url = f"{api_base_url.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST", url, headers=_headers(token), json=payload
            ) as response:
                if response.status_code >= 400:
                    yield await _read_error(response)
                    return
                event = "message"
                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        event = line[6:].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw:
                        continue
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if event == "token":
                        text = str(data.get("text", ""))
                        if text:
                            yield text
                    elif event == "error":
                        yield f"\n\nMouseion QA error: {data.get('detail', 'unknown error')}"
    except httpx.HTTPError as exc:
        yield f"Could not reach Mouseion at {api_base_url}: {exc}"


async def resolve_paper(
    *, api_base_url: str, token: str, text: str
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str | None]:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{api_base_url.rstrip('/')}/api/qa/paper/resolve",
                headers=_headers(token),
                json={"text": text},
            )
        if response.status_code >= 400:
            return None, [], await _read_error(response)
        body = response.json()
        return body.get("locked"), list(body.get("matches") or []), None
    except (httpx.HTTPError, ValueError) as exc:
        return None, [], f"Could not resolve a paper through Mouseion: {exc}"


async def collection_response(
    body: dict[str, Any], *, api_base_url: str, token: str
) -> AsyncIterator[str]:
    if not token.strip():
        yield "Paper Library is not configured: set MOUSEION_API_TOKEN in this pipe's Valves."
        return
    messages, scope, malformed = clean_messages(body.get("messages") or [], "topic")
    if malformed:
        yield malformed
        return
    question, history = split_latest_question(messages)
    if not question:
        yield "Ask a question about the paper library."
        return
    payload: dict[str, Any] = {"question": question, "messages": history}
    if scope is not None:
        payload["topic"] = scope.value
    async for token_text in relay_sse(
        api_base_url=api_base_url,
        token=token,
        path="/api/qa/collection",
        payload=payload,
    ):
        yield token_text


async def paper_response(
    body: dict[str, Any], *, api_base_url: str, token: str
) -> AsyncIterator[str]:
    if not token.strip():
        yield "Single Paper is not configured: set MOUSEION_API_TOKEN in this pipe's Valves."
        return
    messages, scope, malformed = clean_messages(body.get("messages") or [], "paper")
    if malformed:
        yield malformed
        return

    inferred = False
    locked_title: str | None = None
    if scope is None:
        seed = first_user_text(messages)
        if not seed:
            yield "Ask a question that includes the paper title, or start with [paper:ID]."
            return
        locked, matches, error = await resolve_paper(
            api_base_url=api_base_url, token=token, text=seed
        )
        if error:
            yield error
            return
        if locked is None:
            if not matches:
                yield "I could not match that message to a paper title. Start with [paper:ID]."
                return
            lines = ["I found several possible papers. Reply with `[paper:ID]` and your question:"]
            lines += [f"- `{match['id']}` — {match['title']}" for match in matches]
            yield "\n".join(lines)
            return
        paper_id = int(locked["id"])
        locked_title = str(locked["title"])
        inferred = True
    else:
        paper_id = int(scope.value)

    question, history = split_latest_question(messages)
    if not question:
        yield f"Paper {paper_id} is selected. What would you like to ask?"
        return
    if inferred and sum(1 for message in messages if message["role"] == "user") == 1:
        yield f"Locked onto **{locked_title}** (paper {paper_id}).\n\n"
    async for token_text in relay_sse(
        api_base_url=api_base_url,
        token=token,
        path=f"/api/qa/paper/{paper_id}",
        payload={"question": question, "messages": history},
    ):
        yield token_text
