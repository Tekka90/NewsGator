"""OpenAI-compatible LLM client wrapper (SPEC §8).

Single point of contact for the external LLM server — timeouts, retries, JSON-mode
validation with one retry. Never call the OpenAI client directly elsewhere.
The server is external (oMLX/Ollama/llama.cpp/LM Studio…); only base URLs/models
are configured here.
"""

import json
import time
from typing import Any

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from app.core.config import settings


class LLMError(RuntimeError):
    pass


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
    try:
        resp = await _embed_client().embeddings.create(
            model=model or settings.embed_model, input=texts
        )
    except Exception as exc:
        raise LLMError(f"Embedding request failed: {exc}") from exc
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
