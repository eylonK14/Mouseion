"""Phase 4 examiner state machine, grounding, persistence, API, and pipes."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mouseion.services.examiner import (
    ExamPhase,
    OPENING_QUESTION,
    advance_session,
    answer_is_vague,
    create_session,
    ensure_session_current,
    get_session,
    latest_completed_by_papers,
    session_payload,
    user_is_done,
)
from mouseion.services.llm import get_llm_client
from mouseion.services.papers import get_or_create_by_sha256, set_full_text, update_paper
from mouseion.services.tree_indexer import TreeDocument, TreeNode, save_tree
from tests.conftest import make_llm_client

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def exam_client(data_dir) -> Iterator[TestClient]:  # noqa: ANN001
    from mouseion.api import ui
    from mouseion.main import create_app

    ui.reset_templates()
    app = create_app()
    fake = make_llm_client([])
    app.dependency_overrides[get_llm_client] = lambda: fake
    with TestClient(app) as test_client:
        yield test_client


def _make_exam_paper(conn: sqlite3.Connection, settings) -> int:  # noqa: ANN001
    paper_id, _ = get_or_create_by_sha256(conn, "e" * 64)
    update_paper(
        conn,
        paper_id,
        title="Grounded Systems",
        abstract="A careful systems experiment.",
        summary_short="A system trades memory for throughput.",
        summary_long=(
            "The method caches intermediate state, improves throughput by twenty percent, "
            "and is limited to one synthetic benchmark."
        ),
    )
    set_full_text(
        conn,
        paper_id,
        (
            "Method\nThe system caches intermediate state to trade memory for throughput.\n\n"
            "Results\nThroughput improves by twenty percent on the reported workload.\n\n"
            "Limitations\nEvaluation uses one synthetic benchmark and no production traffic."
        ),
        3,
    )
    save_tree(
        TreeDocument(
            sha256="e" * 64,
            indexer="test",
            nodes=[
                TreeNode("method", "Method", start_page=1, end_page=1),
                TreeNode("results", "Results", start_page=2, end_page=2),
                TreeNode("limits", "Limitations", start_page=3, end_page=3),
            ],
        ),
        settings,
    )
    return paper_id


def _probe_plan() -> dict:
    return {
        "probes": [
            {
                "question": "What mechanism creates the memory-throughput tradeoff?",
                "section": "Method",
                "focus": "methodology",
                "targets_gap": True,
                "gap": "The explanation skipped the caching mechanism.",
            },
            {
                "question": "How strong is the result, and what limits its generality?",
                "section": "Limitations",
                "focus": "results_and_limitations",
                "targets_gap": False,
                "gap": "Check whether the evaluation scope is understood.",
            },
        ]
    }


def _verdict(*, misconceptions: list[dict] | None = None) -> dict:
    return {
        "rubric": {
            "problem_understanding": {"score": 4, "justification": "The goal was clear."},
            "method_understanding": {"score": 3, "justification": "The cache was named late."},
            "results_and_limitations": {
                "score": 2,
                "justification": "The benchmark limitation was initially missed.",
            },
        },
        "misconceptions": misconceptions or [],
        "reread": ["Limitations"],
        "overall": {"score": 3, "summary": "Good core intuition; revisit evaluation scope."},
    }


def _insert_completed(
    conn: sqlite3.Connection, paper_id: int, verdict: dict, *, created_at: str = "2026-08-24T10:00:00Z"
) -> int:
    gaps = {
        "misconceptions": verdict["misconceptions"],
        "reread": verdict["reread"],
        "overall_summary": verdict["overall"]["summary"],
    }
    cursor = conn.execute(
        """
        INSERT INTO test_sessions (
            paper_id, transcript_json, rubric_json, score, gaps_json, phase,
            turn_count, state_json, created_at, updated_at, expires_at, completed_at
        ) VALUES (?, ?, ?, ?, ?, 'verdict', 3, '{}', ?, ?, ?, ?)
        """,
        (
            paper_id,
            json.dumps(
                [
                    {"role": "assistant", "kind": "opening", "content": OPENING_QUESTION, "created_at": created_at},
                    {"role": "user", "kind": "answer", "content": "My explanation.", "created_at": created_at},
                    {"role": "assistant", "kind": "verdict", "content": verdict["overall"]["summary"], "created_at": created_at},
                ]
            ),
            json.dumps(verdict["rubric"]),
            verdict["overall"]["score"],
            json.dumps(gaps),
            created_at,
            created_at,
            created_at,
            created_at,
        ),
    )
    return int(cursor.lastrowid)


def _full_client(*, misconceptions: list[dict] | None = None, recorder=None):  # noqa: ANN001
    return make_llm_client(
        [
            {"node_ids": ["method", "results", "limits"]},
            _probe_plan(),
            {"node_ids": ["method", "results", "limits"]},
            _verdict(misconceptions=misconceptions),
        ],
        recorder,
    )


async def test_state_machine_runs_explain_two_grounded_probes_then_verdict(
    conn: sqlite3.Connection, settings
) -> None:
    paper_id = _make_exam_paper(conn, settings)
    session = create_session(conn, paper_id, settings=settings)
    assert session.phase is ExamPhase.EXPLAIN
    assert session_payload(conn, session)["opening_question"] == OPENING_QUESTION

    client = _full_client()
    first = await advance_session(
        conn,
        session.id,
        "It improves throughput, but I am not sure how and I did not discuss evaluation.",
        client=client,
        settings=settings,
    )
    assert first.session.phase is ExamPhase.PROBE
    assert first.assistant_text == _probe_plan()["probes"][0]["question"]
    first_probe = first.session.transcript[-1]
    assert first_probe["section"] == "Method"
    assert first_probe["targets_gap"] is True

    second = await advance_session(
        conn,
        session.id,
        "It caches intermediate state, spending additional memory to increase throughput.",
        client=client,
        settings=settings,
    )
    assert second.assistant_text == _probe_plan()["probes"][1]["question"]
    assert second.session.transcript[-1]["section"] == "Limitations"

    final = await advance_session(
        conn,
        session.id,
        "The gain is twenty percent, but only on one synthetic benchmark, so it may not generalize.",
        client=client,
        settings=settings,
    )
    assert final.session.phase is ExamPhase.VERDICT
    assert final.verdict is not None
    assert final.verdict.overall.score == 3
    stored = conn.execute("SELECT * FROM test_sessions WHERE id = ?", (session.id,)).fetchone()
    assert stored["score"] == 3
    assert "results_and_limitations" in stored["rubric_json"]
    assert "Limitations" in stored["gaps_json"]


@pytest.mark.parametrize("answer", ["I'm done", "done", "That is all.", "stop the exam"])
def test_done_phrases_are_explicit(answer: str) -> None:
    assert user_is_done(answer)
    assert not user_is_done("The method is done in two stages")


async def test_early_done_goes_directly_to_verdict(
    conn: sqlite3.Connection, settings
) -> None:
    paper_id = _make_exam_paper(conn, settings)
    session = create_session(conn, paper_id, settings=settings)
    client = make_llm_client(
        [{"node_ids": ["method", "limits"]}, _verdict()]
    )

    result = await advance_session(
        conn, session.id, "I'm done", client=client, settings=settings
    )

    assert result.session.phase is ExamPhase.VERDICT
    assert result.session.turn_count == 1
    assert not any(turn.get("kind") == "probe" for turn in result.session.transcript)


async def test_turn_cap_auto_finalizes(
    conn: sqlite3.Connection, settings
) -> None:
    limited = settings.model_copy(update={"test_max_turns": 1})
    paper_id = _make_exam_paper(conn, limited)
    session = create_session(conn, paper_id, settings=limited)
    client = make_llm_client(
        [{"node_ids": ["method", "limits"]}, _verdict()]
    )

    result = await advance_session(
        conn,
        session.id,
        "The system is a cache that improves throughput.",
        client=client,
        settings=limited,
    )

    assert result.session.phase is ExamPhase.VERDICT
    assert result.session.turn_count == 1


async def test_expired_session_auto_finalizes_on_reload(
    conn: sqlite3.Connection, settings
) -> None:
    base = datetime(2026, 8, 24, tzinfo=timezone.utc)
    short = settings.model_copy(update={"test_session_ttl_hours": 1})
    paper_id = _make_exam_paper(conn, short)
    session = create_session(conn, paper_id, settings=short, now=base)
    client = make_llm_client(
        [{"node_ids": ["method", "limits"]}, _verdict()]
    )

    reloaded = await ensure_session_current(
        conn,
        session.id,
        client=client,
        settings=short,
        now=base + timedelta(hours=2),
    )

    assert reloaded.phase is ExamPhase.VERDICT
    assert reloaded.completed_at is not None


async def test_vague_probe_answer_is_pushed_once_before_moving_on(
    conn: sqlite3.Connection, settings
) -> None:
    paper_id = _make_exam_paper(conn, settings)
    session = create_session(conn, paper_id, settings=settings)
    client = _full_client()
    await advance_session(
        conn,
        session.id,
        "It improves throughput but skips the mechanism and benchmark details.",
        client=client,
        settings=settings,
    )

    pushed = await advance_session(
        conn, session.id, "Not sure.", client=client, settings=settings
    )
    assert pushed.session.state.probe_index == 0
    assert pushed.session.state.pushback_used is True
    assert pushed.session.transcript[-1]["kind"] == "pushback"

    moved = await advance_session(
        conn, session.id, "Still unsure.", client=client, settings=settings
    )
    assert moved.session.state.probe_index == 1
    assert moved.session.transcript[-1]["kind"] == "probe"
    assert answer_is_vague("Still unsure.")


async def test_verdict_validation_retries_invented_section_and_persists_misconception(
    conn: sqlite3.Connection, settings
) -> None:
    paper_id = _make_exam_paper(conn, settings)
    session = create_session(conn, paper_id, settings=settings)
    wrong = [
        {
            "what_user_said": "The paper proves production reliability.",
            "what_paper_says": "It evaluates only one synthetic benchmark.",
            "section": "Limitations",
        }
    ]
    broken = _verdict(misconceptions=[dict(wrong[0])])
    broken["misconceptions"][0]["section"] = "Invented Appendix"
    requests: list[dict] = []
    client = make_llm_client(
        [
            {"node_ids": ["method", "limits"]},
            broken,
            _verdict(misconceptions=wrong),
        ],
        requests,
    )

    result = await advance_session(
        conn,
        session.id,
        "The paper proves production reliability. I'm otherwise done.",
        client=client,
        settings=settings.model_copy(update={"test_max_turns": 1}),
    )

    assert result.verdict is not None
    assert result.verdict.misconceptions[0].section == "Limitations"
    assert len(requests) == 3
    assert "Invented Appendix" in requests[-1]["messages"][-2]["content"]
    assert latest_completed_by_papers(conn, [paper_id])[paper_id].verdict is not None


def _repository_asset(*parts: str) -> Path:
    """Locate a repository asset in a checkout or the Compose API container."""
    backend_root = Path(__file__).resolve().parents[1]
    for root in (backend_root.parent, backend_root):
        candidate = root.joinpath(*parts)
        if candidate.exists():
            return candidate
    return backend_root.parent.joinpath(*parts)


def _load_pipe_common():  # noqa: ANN202
    path = _repository_asset("pipes", "common.py")
    spec = importlib.util.spec_from_file_location("mouseion_test_exam_pipe_common", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_shared_pipe_util_parses_test_and_paper_tags_and_session_marker() -> None:
    common = _load_pipe_common()
    assert common.parse_scope_tag("[test:12]", "test").value == 12
    messages, lock, malformed = common.clean_messages(
        [{"role": "user", "content": "[paper:7]"}], ("test", "paper")
    )
    assert messages == []
    assert lock.value == 7
    assert malformed is None
    marker = common.test_session_marker(31)
    assert common.test_session_lock([{"role": "assistant", "content": marker}]) == 31


def test_pipe_verdict_renders_empty_and_many_misconceptions() -> None:
    common = _load_pipe_common()
    empty = common.format_test_verdict(_verdict())
    assert "No specific misconceptions" in empty
    many = common.format_test_verdict(
        _verdict(
            misconceptions=[
                {
                    "what_user_said": "It was production traffic.",
                    "what_paper_says": "It was synthetic.",
                    "section": "Limitations",
                },
                {
                    "what_user_said": "There was no memory cost.",
                    "what_paper_says": "Caching consumes memory.",
                    "section": "Method",
                },
            ]
        )
    )
    assert many.count("**You said:**") == 2
    assert "§Limitations" in many and "§Method" in many


async def test_test_pipe_ignores_openwebui_auxiliary_tasks() -> None:
    common = _load_pipe_common()
    assert common.is_auxiliary_request({"task": "title_generation"})
    assert common.is_auxiliary_request({"metadata": {"task": "tags_generation"}})
    chunks = [
        chunk
        async for chunk in common.test_response(
            {
                "task": "title_generation",
                "messages": [{"role": "user", "content": "[test:14]"}],
            },
            api_base_url="http://must-not-be-called.invalid",
            token="configured",
        )
    ]
    assert chunks == []


def test_test_mode_api_start_reload_auth_and_ui(
    exam_client: TestClient,
    conn: sqlite3.Connection,
    settings,
) -> None:
    paper_id = _make_exam_paper(conn, settings)
    assert exam_client.post(f"/api/test/{paper_id}/start").status_code == 401
    started = exam_client.post(f"/api/test/{paper_id}/start", headers=AUTH)
    assert started.status_code == 200
    body = started.json()
    assert body["opening_question"] == OPENING_QUESTION
    assert exam_client.get(f"/api/test/{body['id']}", headers=AUTH).status_code == 200
    fragment = exam_client.get(f"/ui/papers/{paper_id}", headers=AUTH).text
    assert "data-open-test-chat" in fragment
    assert f'data-chat-prefix="[test:{paper_id}]"' in fragment
    assert "/understanding" in fragment


def test_test_session_migration_has_explicit_resumable_state(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(test_sessions)").fetchall()
    }
    assert {
        "phase",
        "turn_count",
        "state_json",
        "updated_at",
        "expires_at",
        "completed_at",
    } <= columns


def test_understanding_panel_many_gaps_card_ring_and_opt_in_status(
    exam_client: TestClient,
    conn: sqlite3.Connection,
    settings,
) -> None:
    paper_id = _make_exam_paper(conn, settings)
    verdict = _verdict(
        misconceptions=[
            {
                "what_user_said": "It used production traffic.",
                "what_paper_says": "It used a synthetic benchmark.",
                "section": "Limitations",
            },
            {
                "what_user_said": "Caching had no cost.",
                "what_paper_says": "Caching spends memory.",
                "section": "Method",
            },
        ]
    )
    verdict["overall"]["score"] = 4
    _insert_completed(conn, paper_id, verdict)

    panel = exam_client.get(
        f"/ui/papers/{paper_id}/understanding", headers=AUTH
    )
    assert panel.status_code == 200
    assert "4/5" in panel.text
    assert "2 misconceptions" in panel.text
    assert 'data-pdf-page="3"' in panel.text
    assert "Session history (1)" in panel.text
    assert "Mark understood" in panel.text

    cards = exam_client.get("/ui/results", headers=AUTH)
    assert f'data-paper-id="{paper_id}"' in cards.text
    assert "understanding-score-4" in cards.text

    accepted = exam_client.post(
        f"/ui/papers/{paper_id}/understanding/accept", headers=AUTH
    )
    assert accepted.status_code == 200
    assert 'hx-swap-oob="outerHTML:#status-control"' in accepted.text
    assert conn.execute("SELECT status FROM papers WHERE id = ?", (paper_id,)).fetchone()[
        "status"
    ] == "understood"


def test_turn_endpoint_streams_questions_and_structured_verdict(
    data_dir,
    conn: sqlite3.Connection,
    settings,
) -> None:
    from mouseion.api import ui
    from mouseion.main import create_app

    paper_id = _make_exam_paper(conn, settings)
    fake = _full_client()
    ui.reset_templates()
    app = create_app()
    app.dependency_overrides[get_llm_client] = lambda: fake
    with TestClient(app) as api_client:
        started = api_client.post(f"/api/test/{paper_id}/start", headers=AUTH).json()
        first = api_client.post(
            f"/api/test/{started['id']}/turn",
            headers=AUTH,
            json={"answer": "It improves throughput but I skipped the mechanism and limits."},
        )
        assert first.status_code == 200
        assert "event: token" in first.text
        assert "memory-throughput" in first.text

        second = api_client.post(
            f"/api/test/{started['id']}/turn",
            headers=AUTH,
            json={
                "answer": "Caching intermediate state deliberately spends additional memory to improve throughput."
            },
        )
        assert "generality" in second.text

        final = api_client.post(
            f"/api/test/{started['id']}/turn",
            headers=AUTH,
            json={"answer": "The gain is twenty percent on only one synthetic benchmark."},
        )
        assert "event: verdict" in final.text
        assert '"results_and_limitations"' in final.text
        assert '"transcript": []' in final.text
        history = api_client.get(
            f"/api/test/paper/{paper_id}/sessions", headers=AUTH
        ).json()
        assert history["items"][0]["phase"] == "verdict"
