"""OpenAI-compatible LLM client wrapper (SPEC §8).

Single point of contact for the external LLM server — timeouts, retries, JSON-mode
validation with one retry. Never call the OpenAI client directly elsewhere.
The server is external (oMLX/Ollama/llama.cpp/LM Studio…); only base URLs/models
are configured here.
"""

import json
import time
from contextvars import ContextVar
from typing import Any

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from app.core.config import settings


class LLMError(RuntimeError):
    pass


# Token usage of the most recent call in the CURRENT async task (None when the
# server omitted `usage` — some local servers do). A ContextVar keeps concurrent
# callers (LLM queue worker vs. GUI-triggered calls) isolated without changing
# the chat_json/embed signatures that tests monkeypatch. Read by services/usage.
last_usage: ContextVar[dict[str, Any] | None] = ContextVar("llm_last_usage", default=None)


def _extract_usage(resp: Any) -> dict[str, Any] | None:
    """Normalize the OpenAI `usage` object to a plain dict (None if absent).

    Standard fields: prompt/completion/total tokens. Newer servers add details
    (cached prompt tokens, reasoning tokens) — collected when present.
    """
    u = getattr(resp, "usage", None)
    if u is None:
        return None
    prompt_details = getattr(u, "prompt_tokens_details", None)
    completion_details = getattr(u, "completion_tokens_details", None)
    return {
        "prompt_tokens": getattr(u, "prompt_tokens", None),
        "completion_tokens": getattr(u, "completion_tokens", None),
        "total_tokens": getattr(u, "total_tokens", None),
        "cached_tokens": getattr(prompt_details, "cached_tokens", None)
        if prompt_details
        else None,
        "reasoning_tokens": getattr(completion_details, "reasoning_tokens", None)
        if completion_details
        else None,
    }


def _chat_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        timeout=settings.llm_timeout_s,
        max_retries=1,
    )


def _embed_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=settings.embed_base_url or settings.llm_base_url,
        api_key=settings.llm_api_key,
        timeout=settings.llm_timeout_s,
        max_retries=1,
    )


async def chat_json(
    system: str, user: str, *, model: str | None = None
) -> tuple[dict[str, Any], int]:
    """Chat completion expecting a JSON object. Validates + retries once on
    parse failure (SPEC §8). Returns (parsed_json, latency_ms)."""
    start = time.monotonic()
    last_usage.set(None)
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            resp = await _chat_client().chat.completions.create(
                model=model or settings.llm_model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.2,
            )
            # Usage of the successful attempt (a failed first attempt's tokens
            # are lost — acceptable, retries are rare).
            last_usage.set(_extract_usage(resp))
            content = resp.choices[0].message.content or ""
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise LLMError(f"LLM returned JSON {type(parsed).__name__}, expected object")
            latency_ms = int((time.monotonic() - start) * 1000)
            return parsed, latency_ms
        except (json.JSONDecodeError, LLMError) as exc:
            last_error = exc
            if attempt == 0:
                # one retry, explicitly asking for valid JSON
                messages.append(
                    {"role": "user", "content": "Your previous reply was not valid JSON. "
                     "Reply with ONLY a valid JSON object."}
                )
            continue
        except Exception as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc
    raise LLMError(f"LLM returned invalid JSON after retry: {last_error}")


async def embed(texts: list[str], *, model: str | None = None) -> list[list[float]]:
    """Embeddings for a batch of texts."""
    last_usage.set(None)
    try:
        resp = await _embed_client().embeddings.create(
            model=model or settings.embed_model, input=texts
        )
    except Exception as exc:
        raise LLMError(f"Embedding request failed: {exc}") from exc
    last_usage.set(_extract_usage(resp))
    return [list(d.embedding) for d in resp.data]


async def test_connection() -> dict[str, Any]:
    """Admin 'test connection' probe — cheap chat + embeddings ping."""
    result: dict[str, Any] = {"chat": False, "embeddings": False, "errors": []}
    try:
        await chat_json(
            "You are a health probe. Reply with JSON.",
            'Respond with exactly: {"ok": true}',
        )
        result["chat"] = True
    except LLMError as exc:
        result["errors"].append(str(exc))
    try:
        await embed(["health probe"])
        result["embeddings"] = True
    except LLMError as exc:
        result["errors"].append(str(exc))
    return result
