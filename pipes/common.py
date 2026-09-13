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


ScopeKind = Literal["paper", "topic", "test"]
_TEST_SESSION_RE = re.compile(r"<!--\s*mouseion-test-session:(\d+)\s*-->")


def parse_scope_tag(text: str, kind: ScopeKind) -> ParsedTag:
    """Parse one leading scope tag without accepting lookalike malformed tags."""
    source = text or ""
    valid = re.match(rf"^\s*\[{kind}:([^\]\r\n]+)\]\s*", source, re.IGNORECASE)
    if valid:
        raw = valid.group(1).strip()
        rest = source[valid.end() :].strip()
        if kind in {"paper", "test"}:
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
    raw_messages: Sequence[dict[str, Any]], kind: ScopeKind | Sequence[ScopeKind]
) -> tuple[list[dict[str, str]], ParsedTag | None, str | None]:
    """Strip scope tags, retaining the first valid lock across the transcript."""
    kinds = (kind,) if isinstance(kind, str) else tuple(kind)
    cleaned: list[dict[str, str]] = []
    lock: ParsedTag | None = None
    malformed: str | None = None
    for message in raw_messages:
        role = str(message.get("role", ""))
        if role not in {"user", "assistant"}:
            continue
        text = _text_content(message)
        parsed: ParsedTag | None = None
        if role == "user":
            for candidate_kind in kinds:
                candidate = parse_scope_tag(text, candidate_kind)
                if candidate.status != "absent":
                    parsed = candidate
                    break
        if parsed and parsed.status == "present":
            if lock is None:
                lock = parsed
            text = parsed.text
        elif parsed and parsed.status == "malformed" and lock is None:
            malformed = (
                "Malformed test scope tag. Use [test:12] or [paper:12]."
                if "test" in kinds
                else (
                    "Malformed paper scope tag. Use [paper:12]."
                    if "paper" in kinds
                    else "Malformed topic scope tag. Use [topic:id-or-name]."
                )
            )
        text = text.strip()
        # A tag-only first message exists solely to establish the pipe lock.
        # Once its tag is stripped it must not become an empty QAMessage: the
        # backend correctly rejects empty conversation content with HTTP 422.
        if not text:
            continue
        cleaned.append({"role": role, "content": text})
    return cleaned, lock, malformed


def test_session_lock(raw_messages: Sequence[dict[str, Any]]) -> int | None:
    """Recover the backend session id hidden in an earlier assistant turn."""
    for message in reversed(raw_messages):
        if str(message.get("role", "")) != "assistant":
            continue
        match = _TEST_SESSION_RE.search(_text_content(message))
        if match and int(match.group(1)) > 0:
            return int(match.group(1))
    return None


def test_session_marker(session_id: int) -> str:
    return f"<!-- mouseion-test-session:{session_id} -->"


def is_auxiliary_request(body: dict[str, Any]) -> bool:
    """Open WebUI may invoke the selected model for title/tag helper tasks.

    Those calls are not user turns and must never create or advance a persisted
    examiner session. Open WebUI versions have placed the marker at either the
    top level or under metadata, so accept both without depending on its enum.
    """
    metadata = body.get("metadata")
    nested = metadata.get("task") if isinstance(metadata, dict) else None
    return bool(body.get("task") or nested)


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


async def _post_json(
    *, api_base_url: str, token: str, path: str, payload: dict[str, Any] | None = None
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(
                f"{api_base_url.rstrip('/')}{path}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=payload,
            )
        if response.status_code >= 400:
            return None, await _read_error(response)
        return response.json(), None
    except (httpx.HTTPError, ValueError) as exc:
        return None, f"Could not reach Mouseion at {api_base_url}: {exc}"


def format_test_verdict(verdict: dict[str, Any]) -> str:
    """Render the structured backend verdict as readable Open WebUI Markdown."""
    overall = verdict.get("overall") or {}
    rubric = verdict.get("rubric") or {}
    lines = [
        f"## Verdict — {overall.get('score', '?')}/5",
        "",
        str(overall.get("summary") or "Exam completed."),
        "",
        "| Rubric | Score | Justification |",
        "| --- | ---: | --- |",
    ]
    labels = (
        ("Problem understanding", "problem_understanding"),
        ("Method understanding", "method_understanding"),
        ("Results & limitations", "results_and_limitations"),
    )
    for label, key in labels:
        dimension = rubric.get(key) or {}
        justification = str(dimension.get("justification") or "").replace("|", "\\|")
        lines.append(f"| {label} | {dimension.get('score', '?')}/5 | {justification} |")

    lines += ["", "### Misconceptions"]
    misconceptions = list(verdict.get("misconceptions") or [])
    if misconceptions:
        for item in misconceptions:
            lines += [
                f"- **You said:** {item.get('what_user_said', '')}",
                f"  **Paper:** {item.get('what_paper_says', '')} "
                f"(§{item.get('section', '')})",
            ]
    else:
        lines.append("- No specific misconceptions were identified.")

    lines += ["", "### Sections to reread"]
    reread = list(verdict.get("reread") or [])
    lines += [f"- §{section}" for section in reread] or ["- None assigned."]
    return "\n".join(lines)


async def _relay_test_turn(
    *, api_base_url: str, token: str, session_id: int, answer: str
) -> AsyncIterator[str]:
    url = f"{api_base_url.rstrip('/')}/api/test/{session_id}/turn"
    verdict: dict[str, Any] | None = None
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST", url, headers=_headers(token), json={"answer": answer}
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
                    try:
                        data = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    if event == "token":
                        text = str(data.get("text", ""))
                        if text:
                            yield text
                    elif event == "verdict":
                        verdict = data
                    elif event == "done" and verdict is None:
                        verdict = data.get("verdict")
                    elif event == "error":
                        yield f"Mouseion examiner error: {data.get('detail', 'unknown error')}"
                        return
        if verdict is not None:
            yield format_test_verdict(verdict)
        yield "\n\n" + test_session_marker(session_id)
    except httpx.HTTPError as exc:
        yield f"Could not reach Mouseion at {api_base_url}: {exc}"


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


async def test_response(
    body: dict[str, Any], *, api_base_url: str, token: str
) -> AsyncIterator[str]:
    """Drive one persisted Mouseion test session from stateless pipe calls."""
    if is_auxiliary_request(body):
        return
    if not token.strip():
        yield "Test me is not configured: set MOUSEION_API_TOKEN in this pipe's Valves."
        return
    raw_messages = body.get("messages") or []
    messages, scope, malformed = clean_messages(raw_messages, ("test", "paper"))
    if malformed:
        yield malformed
        return
    session_id = test_session_lock(raw_messages)

    if session_id is None:
        locked_title: str | None = None
        if scope is not None:
            paper_id = int(scope.value)
        else:
            seed = first_user_text(messages)
            if not seed:
                yield "Name a paper to be tested on, or start with [test:ID]."
                return
            locked, matches, error = await resolve_paper(
                api_base_url=api_base_url, token=token, text=seed
            )
            if error:
                yield error
                return
            if locked is None:
                if not matches:
                    yield "I could not match that message to a paper title. Start with [test:ID]."
                    return
                lines = ["I found several possible papers. Reply with `[test:ID]`:"]
                lines += [f"- `{match['id']}` — {match['title']}" for match in matches]
                yield "\n".join(lines)
                return
            paper_id = int(locked["id"])
            locked_title = str(locked["title"])

        started, error = await _post_json(
            api_base_url=api_base_url,
            token=token,
            path=f"/api/test/{paper_id}/start",
        )
        if error or started is None:
            yield error or "Mouseion did not return a test session."
            return
        session_id = int(started["id"])
        prefix = (
            f"Testing **{locked_title}** (paper {paper_id}).\n\n"
            if locked_title
            else ""
        )
        yield prefix + str(started["opening_question"])
        yield "\n\n" + test_session_marker(session_id)
        return

    answer, _ = split_latest_question(messages)
    if not answer:
        yield "Continue with your explanation, or say “I’m done” for the verdict."
        yield "\n\n" + test_session_marker(session_id)
        return
    async for token_text in _relay_test_turn(
        api_base_url=api_base_url,
        token=token,
        session_id=session_id,
        answer=answer,
    ):
        yield token_text
