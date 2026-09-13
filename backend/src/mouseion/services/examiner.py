"""Grounded, resumable explain-it-back examiner state machine.

Pipes and routers only transport state. This module owns every transition,
prompt, grounding decision, guard, structured verdict, and database write.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Sequence

from pydantic import ValidationError

from mouseion.config import Settings, get_settings
from mouseion.services.llm import LLMClient, LLMTask
from mouseion.services.prompts import (
    GroundingExcerpt,
    build_exam_probe_prompt,
    build_exam_verdict_prompt,
)
from mouseion.services.qa import (
    QANotFoundError,
    SectionGrounding,
    gather_grounding_material_for_paper,
)
from mouseion.services.schemas import ExamProbe, ExamProbePlan, ExamVerdict
from mouseion.services.tree_indexer import TreeDocument, load_tree

OPENING_QUESTION = (
    "Explain the paper’s core idea in your own words. What problem does it "
    "address, and how does its approach work?"
)
_DONE_RE = re.compile(
    r"^\s*(?:i(?:'m| am)\s+done|done|that(?:'s| is)\s+all|stop(?:\s+the)?\s+exam)"
    r"[.!\s]*$",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[\w'-]+", re.UNICODE)


class ExamPhase(str, Enum):
    EXPLAIN = "explain"
    PROBE = "probe"
    VERDICT = "verdict"


class ExaminerError(RuntimeError):
    """Readable examiner failure."""


class ExaminerNotFoundError(ExaminerError):
    pass


class ExaminerConflictError(ExaminerError):
    pass


@dataclass(slots=True)
class ExamState:
    probes: list[ExamProbe] = field(default_factory=list)
    probe_index: int = 0
    pushback_used: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "probes": [probe.model_dump(mode="json") for probe in self.probes],
            "probe_index": self.probe_index,
            "pushback_used": self.pushback_used,
        }

    @classmethod
    def from_raw(cls, raw: str | None) -> ExamState:
        try:
            body = json.loads(raw or "{}")
            probes = [ExamProbe.model_validate(item) for item in body.get("probes", [])]
            index = max(0, int(body.get("probe_index", 0)))
            return cls(
                probes=probes,
                probe_index=min(index, len(probes)),
                pushback_used=bool(body.get("pushback_used", False)),
            )
        except (json.JSONDecodeError, TypeError, ValueError, ValidationError):
            return cls()


@dataclass(slots=True)
class ExamSession:
    id: int
    paper_id: int
    phase: ExamPhase
    turn_count: int
    transcript: list[dict[str, object]]
    state: ExamState
    verdict: ExamVerdict | None
    created_at: str
    updated_at: str
    expires_at: str | None
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class AdvanceResult:
    session: ExamSession
    assistant_text: str
    verdict: ExamVerdict | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _json_list(raw: str | None) -> list[dict[str, object]]:
    try:
        body = json.loads(raw or "[]")
        return [item for item in body if isinstance(item, dict)] if isinstance(body, list) else []
    except json.JSONDecodeError:
        return []


def _verdict_from_row(row: sqlite3.Row) -> ExamVerdict | None:
    if row["score"] is None:
        return None
    try:
        rubric = json.loads(row["rubric_json"] or "{}")
        gaps = json.loads(row["gaps_json"] or "{}")
        if not isinstance(gaps, dict):
            gaps = {}
        return ExamVerdict.model_validate(
            {
                "rubric": rubric,
                "misconceptions": gaps.get("misconceptions", []),
                "reread": gaps.get("reread", []),
                "overall": {
                    "score": int(row["score"]),
                    "summary": gaps.get("overall_summary") or "Exam completed.",
                },
            }
        )
    except (json.JSONDecodeError, TypeError, ValueError, ValidationError):
        return None


def _session_from_row(row: sqlite3.Row) -> ExamSession:
    try:
        phase = ExamPhase(str(row["phase"]))
    except ValueError:
        phase = ExamPhase.VERDICT if row["score"] is not None else ExamPhase.EXPLAIN
    return ExamSession(
        id=int(row["id"]),
        paper_id=int(row["paper_id"]),
        phase=phase,
        turn_count=int(row["turn_count"] or 0),
        transcript=_json_list(row["transcript_json"]),
        state=ExamState.from_raw(row["state_json"]),
        verdict=_verdict_from_row(row),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"] or row["created_at"]),
        expires_at=row["expires_at"],
        completed_at=row["completed_at"],
    )


def get_session(conn: sqlite3.Connection, session_id: int) -> ExamSession:
    row = conn.execute("SELECT * FROM test_sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        raise ExaminerNotFoundError(f"test session {session_id} was not found")
    return _session_from_row(row)


def list_sessions(conn: sqlite3.Connection, paper_id: int) -> list[ExamSession]:
    rows = conn.execute(
        "SELECT * FROM test_sessions WHERE paper_id = ? ORDER BY id DESC", (paper_id,)
    ).fetchall()
    return [_session_from_row(row) for row in rows]


def latest_completed_by_papers(
    conn: sqlite3.Connection, paper_ids: Sequence[int]
) -> dict[int, ExamSession]:
    ids = list(dict.fromkeys(int(paper_id) for paper_id in paper_ids))
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT ts.*
        FROM test_sessions ts
        JOIN (
            SELECT paper_id, MAX(id) AS latest_id
            FROM test_sessions
            WHERE score IS NOT NULL AND paper_id IN ({placeholders})
            GROUP BY paper_id
        ) latest ON latest.latest_id = ts.id
        """,
        ids,
    ).fetchall()
    return {int(row["paper_id"]): _session_from_row(row) for row in rows}


def _paper_title(conn: sqlite3.Connection, paper_id: int) -> str:
    row = conn.execute("SELECT title FROM papers WHERE id = ?", (paper_id,)).fetchone()
    if row is None:
        raise ExaminerNotFoundError(f"paper {paper_id} was not found")
    return str(row["title"] or f"Untitled paper {paper_id}")


def _require_tree(conn: sqlite3.Connection, paper_id: int, settings: Settings) -> TreeDocument:
    tree = load_tree(paper_id, conn, settings)
    if tree is None or not any(node.title.strip() for node in tree.iter_nodes()):
        raise ExaminerConflictError(
            "this paper has no usable section tree; rebuild its index before starting a test"
        )
    return tree


def create_session(
    conn: sqlite3.Connection,
    paper_id: int,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> ExamSession:
    settings = settings or get_settings()
    _paper_title(conn, paper_id)
    _require_tree(conn, paper_id, settings)
    current = now or _utc_now()
    timestamp = _iso(current)
    expires_at = _iso(current + timedelta(hours=max(1, settings.test_session_ttl_hours)))
    transcript = [
        {
            "role": "assistant",
            "kind": "opening",
            "content": OPENING_QUESTION,
            "created_at": timestamp,
        }
    ]
    cursor = conn.execute(
        """
        INSERT INTO test_sessions (
            paper_id, transcript_json, rubric_json, score, gaps_json, phase,
            turn_count, state_json, created_at, updated_at, expires_at
        ) VALUES (?, ?, '{}', NULL, '{}', 'explain', 0, '{}', ?, ?, ?)
        """,
        (paper_id, json.dumps(transcript, ensure_ascii=False), timestamp, timestamp, expires_at),
    )
    return get_session(conn, int(cursor.lastrowid))


def current_question(session: ExamSession) -> str | None:
    if session.phase is ExamPhase.VERDICT:
        return None
    for turn in reversed(session.transcript):
        if turn.get("role") == "assistant" and turn.get("kind") in {
            "opening",
            "probe",
            "pushback",
        }:
            return str(turn.get("content") or "") or None
    return OPENING_QUESTION if session.phase is ExamPhase.EXPLAIN else None


def user_is_done(text: str) -> bool:
    return bool(_DONE_RE.fullmatch(text or ""))


def answer_is_vague(text: str) -> bool:
    normalized = " ".join((text or "").casefold().split())
    words = _WORD_RE.findall(normalized)
    vague_phrases = (
        "i don't know",
        "i do not know",
        "not sure",
        "it just works",
        "something like that",
        "some stuff",
    )
    return len(words) < 9 or any(phrase in normalized for phrase in vague_phrases)


def _actual_sections(
    tree: TreeDocument, sections: Sequence[SectionGrounding]
) -> list[SectionGrounding]:
    titles = {node.title.strip() for node in tree.iter_nodes() if node.title.strip()}
    return [section for section in sections if section.section in titles and section.text.strip()]


async def _ground_sections(
    conn: sqlite3.Connection,
    paper_id: int,
    question: str,
    *,
    client: LLMClient,
    settings: Settings,
) -> tuple[str, list[SectionGrounding]]:
    tree = _require_tree(conn, paper_id, settings)
    try:
        material = await gather_grounding_material_for_paper(
            conn,
            paper_id,
            question[:8_000],
            client=client,
            settings=settings,
        )
    except QANotFoundError as exc:
        raise ExaminerNotFoundError(str(exc)) from exc
    sections = _actual_sections(tree, material.sections)
    if not sections:
        raise ExaminerConflictError(
            "the section tree has no readable grounded excerpts for this paper"
        )
    return material.title, sections


def _prompt_excerpts(
    paper_id: int, paper_title: str, sections: Sequence[SectionGrounding]
) -> list[GroundingExcerpt]:
    return [
        GroundingExcerpt(
            paper_id=paper_id,
            paper_title=paper_title,
            section=section.section,
            text=section.text,
        )
        for section in sections
    ]


async def _plan_probes(
    conn: sqlite3.Connection,
    session: ExamSession,
    explanation: str,
    *,
    client: LLMClient,
    settings: Settings,
) -> ExamProbePlan:
    grounding_question = (
        "Find concrete methodology, results, and limitations sections that expose "
        "what this explanation skipped or got wrong:\n" + explanation[:6_000]
    )
    title, sections = await _ground_sections(
        conn, session.paper_id, grounding_question, client=client, settings=settings
    )
    prompt = build_exam_probe_prompt(
        paper_title=title,
        explanation=explanation,
        excerpts=_prompt_excerpts(session.paper_id, title, sections),
    )
    allowed = [section.section for section in sections]
    return await client.complete_json(
        task=LLMTask.GRADING,
        system=prompt.system,
        user=prompt.user,
        output_model=ExamProbePlan,
        validation_context={"allowed_sections": allowed},
    )


def _transcript_query(transcript: Sequence[dict[str, object]]) -> str:
    rendered = []
    for turn in transcript:
        content = str(turn.get("content") or "").strip()
        if content:
            rendered.append(f"{turn.get('role', 'unknown')}: {content}")
    return (
        "Find the paper sections needed to grade problem, methodology, results, "
        "limitations, and any contradictions in this oral-exam transcript:\n"
        + "\n".join(rendered)[-7_000:]
    )


async def _build_verdict(
    conn: sqlite3.Connection,
    session: ExamSession,
    transcript: Sequence[dict[str, object]],
    *,
    client: LLMClient,
    settings: Settings,
) -> ExamVerdict:
    title, sections = await _ground_sections(
        conn,
        session.paper_id,
        _transcript_query(transcript),
        client=client,
        settings=settings,
    )
    prompt = build_exam_verdict_prompt(
        paper_title=title,
        transcript=transcript,
        excerpts=_prompt_excerpts(session.paper_id, title, sections),
    )
    allowed = [section.section for section in sections]
    return await client.complete_json(
        task=LLMTask.GRADING,
        system=prompt.system,
        user=prompt.user,
        output_model=ExamVerdict,
        validation_context={"allowed_sections": allowed},
    )


def _append_turn(
    transcript: list[dict[str, object]],
    *,
    role: str,
    kind: str,
    content: str,
    now: datetime,
    **metadata: object,
) -> None:
    transcript.append(
        {
            "role": role,
            "kind": kind,
            "content": content,
            "created_at": _iso(now),
            **metadata,
        }
    )


def _persist_active(
    conn: sqlite3.Connection,
    session: ExamSession,
    *,
    phase: ExamPhase,
    transcript: list[dict[str, object]],
    state: ExamState,
    turn_count: int,
    now: datetime,
    settings: Settings,
) -> ExamSession:
    conn.execute(
        """
        UPDATE test_sessions
        SET transcript_json = ?, phase = ?, turn_count = ?, state_json = ?,
            updated_at = ?, expires_at = ?
        WHERE id = ?
        """,
        (
            json.dumps(transcript, ensure_ascii=False),
            phase.value,
            turn_count,
            json.dumps(state.to_dict(), ensure_ascii=False),
            _iso(now),
            _iso(now + timedelta(hours=max(1, settings.test_session_ttl_hours))),
            session.id,
        ),
    )
    return get_session(conn, session.id)


async def _finalize(
    conn: sqlite3.Connection,
    session: ExamSession,
    *,
    transcript: list[dict[str, object]],
    turn_count: int,
    client: LLMClient,
    settings: Settings,
    now: datetime,
) -> AdvanceResult:
    verdict = await _build_verdict(
        conn, session, transcript, client=client, settings=settings
    )
    _append_turn(
        transcript,
        role="assistant",
        kind="verdict",
        content=verdict.overall.summary,
        now=now,
    )
    gaps = {
        "misconceptions": [item.model_dump(mode="json") for item in verdict.misconceptions],
        "reread": verdict.reread,
        "overall_summary": verdict.overall.summary,
    }
    conn.execute(
        """
        UPDATE test_sessions
        SET transcript_json = ?, rubric_json = ?, score = ?, gaps_json = ?,
            phase = 'verdict', turn_count = ?, state_json = '{}', updated_at = ?,
            completed_at = ?
        WHERE id = ?
        """,
        (
            json.dumps(transcript, ensure_ascii=False),
            verdict.rubric.model_dump_json(),
            verdict.overall.score,
            json.dumps(gaps, ensure_ascii=False),
            turn_count,
            _iso(now),
            _iso(now),
            session.id,
        ),
    )
    completed = get_session(conn, session.id)
    return AdvanceResult(completed, "", verdict)


def session_is_expired(session: ExamSession, *, now: datetime | None = None) -> bool:
    expires_at = _parse_time(session.expires_at)
    return (
        session.phase is not ExamPhase.VERDICT
        and expires_at is not None
        and (now or _utc_now()) >= expires_at
    )


async def ensure_session_current(
    conn: sqlite3.Connection,
    session_id: int,
    *,
    client: LLMClient,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> ExamSession:
    settings = settings or get_settings()
    current = now or _utc_now()
    session = get_session(conn, session_id)
    if not session_is_expired(session, now=current):
        return session
    result = await _finalize(
        conn,
        session,
        transcript=list(session.transcript),
        turn_count=session.turn_count,
        client=client,
        settings=settings,
        now=current,
    )
    return result.session


async def advance_session(
    conn: sqlite3.Connection,
    session_id: int,
    answer: str,
    *,
    client: LLMClient,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> AdvanceResult:
    """Advance exactly one user turn through EXPLAIN → PROBE → VERDICT."""
    settings = settings or get_settings()
    current = now or _utc_now()
    session = get_session(conn, session_id)
    if session.phase is ExamPhase.VERDICT:
        return AdvanceResult(session, "", session.verdict)
    if session_is_expired(session, now=current):
        return await _finalize(
            conn,
            session,
            transcript=list(session.transcript),
            turn_count=session.turn_count,
            client=client,
            settings=settings,
            now=current,
        )

    response = answer.strip()
    transcript = list(session.transcript)
    _append_turn(
        transcript, role="user", kind="answer", content=response, now=current
    )
    turn_count = session.turn_count + 1
    if user_is_done(response) or turn_count >= max(1, settings.test_max_turns):
        return await _finalize(
            conn,
            session,
            transcript=transcript,
            turn_count=turn_count,
            client=client,
            settings=settings,
            now=current,
        )

    if session.phase is ExamPhase.EXPLAIN:
        plan = await _plan_probes(
            conn, session, response, client=client, settings=settings
        )
        state = ExamState(probes=list(plan.probes))
        probe = state.probes[0]
        _append_turn(
            transcript,
            role="assistant",
            kind="probe",
            content=probe.question,
            now=current,
            section=probe.section,
            focus=probe.focus,
            targets_gap=probe.targets_gap,
        )
        active = _persist_active(
            conn,
            session,
            phase=ExamPhase.PROBE,
            transcript=transcript,
            state=state,
            turn_count=turn_count,
            now=current,
            settings=settings,
        )
        return AdvanceResult(active, probe.question)

    state = session.state
    if not state.probes or state.probe_index >= len(state.probes):
        return await _finalize(
            conn,
            session,
            transcript=transcript,
            turn_count=turn_count,
            client=client,
            settings=settings,
            now=current,
        )
    probe = state.probes[state.probe_index]
    if answer_is_vague(response) and not state.pushback_used:
        state.pushback_used = True
        pushback = (
            "That is still too vague. Be concrete without guessing: answer the "
            f"question about §{probe.section}, including the mechanism or evidence "
            "you think matters. I’ll move on after this response."
        )
        _append_turn(
            transcript,
            role="assistant",
            kind="pushback",
            content=pushback,
            now=current,
            section=probe.section,
        )
        active = _persist_active(
            conn,
            session,
            phase=ExamPhase.PROBE,
            transcript=transcript,
            state=state,
            turn_count=turn_count,
            now=current,
            settings=settings,
        )
        return AdvanceResult(active, pushback)

    state.probe_index += 1
    state.pushback_used = False
    if state.probe_index >= len(state.probes):
        return await _finalize(
            conn,
            session,
            transcript=transcript,
            turn_count=turn_count,
            client=client,
            settings=settings,
            now=current,
        )

    next_probe = state.probes[state.probe_index]
    _append_turn(
        transcript,
        role="assistant",
        kind="probe",
        content=next_probe.question,
        now=current,
        section=next_probe.section,
        focus=next_probe.focus,
        targets_gap=next_probe.targets_gap,
    )
    active = _persist_active(
        conn,
        session,
        phase=ExamPhase.PROBE,
        transcript=transcript,
        state=state,
        turn_count=turn_count,
        now=current,
        settings=settings,
    )
    return AdvanceResult(active, next_probe.question)


def reread_targets(
    conn: sqlite3.Connection, session: ExamSession, *, settings: Settings | None = None
) -> list[dict[str, object]]:
    if session.verdict is None:
        return []
    tree = load_tree(session.paper_id, conn, settings or get_settings())
    nodes = list(tree.iter_nodes()) if tree is not None else []
    result: list[dict[str, object]] = []
    for section in session.verdict.reread:
        node = next((candidate for candidate in nodes if candidate.title == section), None)
        result.append(
            {
                "section": section,
                "start_page": node.start_page if node is not None else None,
                "end_page": node.end_page if node is not None else None,
            }
        )
    return result


def session_payload(
    conn: sqlite3.Connection,
    session: ExamSession,
    *,
    include_transcript: bool = True,
    settings: Settings | None = None,
) -> dict[str, object]:
    return {
        "id": session.id,
        "paper_id": session.paper_id,
        "phase": session.phase.value,
        "turn_count": session.turn_count,
        "opening_question": OPENING_QUESTION,
        "current_question": current_question(session),
        "transcript": session.transcript if include_transcript else [],
        "verdict": session.verdict.model_dump(mode="json") if session.verdict else None,
        "reread_targets": reread_targets(conn, session, settings=settings),
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "expires_at": session.expires_at,
        "completed_at": session.completed_at,
    }
