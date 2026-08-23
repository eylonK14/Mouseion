"""Structured-output validation and the retry path."""

from __future__ import annotations

import json

import httpx
import pytest

from mouseion.services.llm import (
    LLMClient,
    LLMError,
    LLMTask,
    LLMValidationError,
    strict_json_schema,
)
from mouseion.services.schemas import IngestExtraction
from tests.conftest import extraction_payload, make_llm_client

VALID = extraction_payload(topics=[{"existing_topic_id": 1}])


async def call(client: LLMClient) -> IngestExtraction:
    return await client.complete_json(
        task=LLMTask.INGEST,
        system="system",
        user="user",
        output_model=IngestExtraction,
    )


def test_strict_json_schema_requires_all_fields_and_forbids_extras() -> None:
    schema = strict_json_schema(IngestExtraction)

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])

    # Nested models are tightened too, or providers reject the whole schema.
    metadata = schema["$defs"]["IngestMetadata"]
    assert metadata["additionalProperties"] is False
    assert set(metadata["required"]) == set(metadata["properties"])


def test_model_routing_is_per_task(settings) -> None:
    client = LLMClient(settings, client=httpx.AsyncClient())
    assert client.model_for(LLMTask.INGEST) == "test/ingest-model"
    assert client.model_for(LLMTask.QA) == "test/qa-model"
    # Grading is a frontier-model task too (CLAUDE.md).
    assert client.model_for(LLMTask.GRADING) == "test/qa-model"


async def test_complete_json_returns_validated_model(settings) -> None:
    requests: list[dict] = []
    client = make_llm_client([VALID], requests)

    result = await call(client)

    assert result.summary_short == "It computes attention over inputs."
    assert result.topics[0].existing_topic_id == 1
    assert len(requests) == 1

    # The request asks for strict structured output, not free-form text.
    response_format = requests[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert requests[0]["model"] == "test/ingest-model"


async def test_retries_once_on_validation_failure_and_feeds_back_the_error(settings) -> None:
    requests: list[dict] = []
    # First reply is missing summary_long, which Pydantic rejects.
    broken = {"metadata": {}, "topics": [], "summary_short": "Too little."}
    client = make_llm_client([broken, VALID], requests)

    result = await call(client)

    assert result.summary_long.startswith("The paper introduces")
    assert len(requests) == 2

    # The retry carries the model's own bad reply plus the validation error.
    messages = requests[1]["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert json.loads(messages[2]["content"]) == broken
    assert "did not validate" in messages[3]["content"]
    assert "summary_long" in messages[3]["content"]


async def test_gives_up_after_max_attempts(settings) -> None:
    requests: list[dict] = []
    broken = {"metadata": {}, "topics": [], "summary_short": "x"}
    client = make_llm_client([broken, broken, broken], requests)

    with pytest.raises(LLMValidationError, match="did not validate after 2 attempts"):
        await call(client)

    # Exactly two attempts: one call plus one retry (LLM_MAX_ATTEMPTS default).
    assert len(requests) == 2


async def test_rejects_a_topic_that_is_both_pick_and_proposal(settings) -> None:
    both = extraction_payload(
        topics=[{"existing_topic_id": 3, "new_topic_name": "Transformers", "parent": 1}]
    )
    client = make_llm_client([both, both])

    with pytest.raises(LLMValidationError):
        await call(client)


async def test_accepts_a_proposal_after_a_rejected_pick_and_proposal(settings) -> None:
    both = extraction_payload(
        topics=[{"existing_topic_id": 3, "new_topic_name": "Transformers", "parent": 1}]
    )
    fixed = extraction_payload(topics=[{"new_topic_name": "Transformers", "parent": 1}])
    client = make_llm_client([both, fixed])

    result = await call(client)

    assert result.topics[0].is_proposal
    assert result.topics[0].new_topic_name == "Transformers"


async def test_http_error_is_surfaced_not_retried(settings) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, text="rate limited")

    client = LLMClient(
        settings,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://openrouter.test/api/v1"
        ),
    )

    with pytest.raises(LLMError, match="429"):
        await call(client)
    assert calls["n"] == 1


async def test_transport_error_is_retried_before_nonstream_response(settings) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("temporary connection failure", request=request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "recovered"}}]},
        )

    client = LLMClient(
        settings,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://openrouter.test/api/v1"
        ),
    )

    answer = await client.complete_text(task=LLMTask.QA, system="system", user="user")

    assert answer == "recovered"
    assert calls["n"] == 2


async def test_empty_content_is_an_error(settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    client = LLMClient(
        settings,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://openrouter.test/api/v1"
        ),
    )

    with pytest.raises(LLMError, match="no content"):
        await call(client)


async def test_stream_text_yields_tokens_and_captures_openrouter_usage(settings) -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=(
                'data: {"model":"provider/qa","choices":[{"delta":{"content":"hello "}}]}\n\n'
                'data: {"model":"provider/qa","choices":[{"delta":{"content":"world"}}]}\n\n'
                'data: {"model":"provider/qa","choices":[],"usage":'
                '{"prompt_tokens":7,"completion_tokens":2,"total_tokens":9}}\n\n'
                "data: [DONE]\n\n"
            ),
        )

    client = LLMClient(
        settings,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://openrouter.test/api/v1"
        ),
    )
    stream = client.stream_text(
        task=LLMTask.QA,
        messages=[{"role": "user", "content": "say hello"}],
    )

    assert "".join([token async for token in stream]) == "hello world"
    assert requests[0]["stream"] is True
    assert requests[0]["stream_options"] == {"include_usage": True}
    assert stream.metrics.model == "provider/qa"
    assert (
        stream.metrics.prompt_tokens,
        stream.metrics.completion_tokens,
        stream.metrics.total_tokens,
    ) == (7, 2, 9)


async def test_stream_retries_transport_error_before_first_token(settings) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("temporary connection failure", request=request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text='data: {"choices":[{"delta":{"content":"recovered"}}]}\n\n',
        )

    client = LLMClient(
        settings,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://openrouter.test/api/v1"
        ),
    )
    stream = client.stream_text(
        task=LLMTask.QA,
        messages=[{"role": "user", "content": "say hello"}],
    )

    assert "".join([token async for token in stream]) == "recovered"
    assert calls["n"] == 2


async def test_stream_does_not_retry_after_first_token(settings) -> None:
    calls = {"n": 0}

    class BrokenAfterToken(httpx.AsyncByteStream):
        async def __aiter__(self):  # noqa: ANN202
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            raise httpx.ReadError("connection lost during answer")

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=BrokenAfterToken(),
        )

    client = LLMClient(
        settings,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://openrouter.test/api/v1"
        ),
    )
    stream = client.stream_text(
        task=LLMTask.QA,
        messages=[{"role": "user", "content": "say hello"}],
    )
    tokens: list[str] = []

    with pytest.raises(LLMError, match="connection lost during answer"):
        async for token in stream:
            tokens.append(token)

    assert tokens == ["partial"]
    assert calls["n"] == 1
