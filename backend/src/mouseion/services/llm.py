"""The one OpenRouter client.

CLAUDE.md: ALL LLM calls go through OpenRouter, with per-task model routing
(`MODEL_INGEST` for tagging/summaries, `MODEL_QA` for QA and grading). Phases
3-4 add entries to `LLMTask` and call `complete_json`/`complete_text` — they do
not add HTTP clients.

Structured output is validated with Pydantic and retried exactly once on
validation failure, with the error fed back to the model.
"""

from __future__ import annotations

import json
import logging
from enum import Enum
from functools import lru_cache
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from mouseion.config import Settings, get_settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMTask(str, Enum):
    """What the call is for. This — not the caller — decides the model."""

    INGEST = "ingest"  # tagging + summaries + metadata (cheap/fast model)
    QA = "qa"  # Phase 3
    GRADING = "grading"  # Phase 4


class LLMError(RuntimeError):
    """Any failure talking to OpenRouter."""


class LLMValidationError(LLMError):
    """The model kept returning JSON that does not satisfy the schema."""


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic's schema, tightened for provider strict modes.

    Strict structured output requires every object to list all its properties in
    `required` and to forbid extras. Optional fields stay expressible because
    Pydantic already renders `X | None` as an anyOf with null.
    """
    schema = model.model_json_schema()

    def tighten(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["required"] = list(node["properties"].keys())
                node["additionalProperties"] = False
            for value in node.values():
                tighten(value)
        elif isinstance(node, list):
            for item in node:
                tighten(item)

    tighten(schema)
    return schema


class LLMClient:
    """Async OpenRouter client. One per process; inject `client` in tests."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._owns_client = client is None

    # -- wiring -----------------------------------------------------------
    def model_for(self, task: LLMTask) -> str:
        if task is LLMTask.INGEST:
            return self.settings.model_ingest
        return self.settings.model_qa

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            if not self.settings.openrouter_api_key:
                raise LLMError("OPENROUTER_API_KEY is not set")
            self._client = httpx.AsyncClient(
                base_url=self.settings.openrouter_base_url,
                timeout=httpx.Timeout(self.settings.llm_timeout_seconds),
                headers={
                    "Authorization": f"Bearer {self.settings.openrouter_api_key}",
                    # OpenRouter attribution headers; harmless if unset.
                    "HTTP-Referer": self.settings.openrouter_app_url,
                    "X-Title": self.settings.openrouter_app_title,
                },
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # -- calls ------------------------------------------------------------
    async def _post(self, payload: dict[str, Any]) -> str:
        try:
            response = await self.client.post("/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise LLMError(f"OpenRouter request failed: {exc}") from exc
        if response.status_code >= 400:
            raise LLMError(
                f"OpenRouter returned {response.status_code}: {response.text[:500]}"
            )
        try:
            body = response.json()
            message = body["choices"][0]["message"]
        except (json.JSONDecodeError, KeyError, IndexError) as exc:
            raise LLMError(f"unexpected OpenRouter response shape: {exc}") from exc
        content = message.get("content")
        if not content:
            raise LLMError(f"OpenRouter returned no content (message={message!r})")
        return content

    async def complete_text(self, *, task: LLMTask, system: str, user: str) -> str:
        return await self._post(
            {
                "model": self.model_for(task),
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0,
            }
        )

    async def complete_json(
        self,
        *,
        task: LLMTask,
        system: str,
        user: str,
        output_model: type[T],
        max_attempts: int | None = None,
    ) -> T:
        """Call the model and validate its JSON into `output_model`.

        Retries `max_attempts - 1` times (default 1 retry, per CLAUDE.md),
        appending the failed reply and the validation error so the model can
        correct itself rather than resample blindly.
        """
        attempts = max_attempts or self.settings.llm_max_attempts
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        payload = {
            "model": self.model_for(task),
            "messages": messages,
            "temperature": 0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": output_model.__name__,
                    "strict": True,
                    "schema": strict_json_schema(output_model),
                },
            },
        }

        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            content = await self._post(payload)
            try:
                return output_model.model_validate_json(content)
            except ValidationError as exc:
                last_error = exc
                log.warning(
                    "LLM structured output failed validation (attempt %s/%s): %s",
                    attempt,
                    attempts,
                    exc,
                )
                if attempt < attempts:
                    messages.append({"role": "assistant", "content": content})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "That JSON did not validate. Fix these problems and reply "
                                "with the corrected JSON object only:\n"
                                f"{exc}"
                            ),
                        }
                    )

        raise LLMValidationError(
            f"{output_model.__name__} did not validate after {attempts} attempts: {last_error}"
        )


@lru_cache(maxsize=1)
def get_llm_client() -> LLMClient:
    """Process-wide client (the worker reuses one connection pool)."""
    return LLMClient()
