"""llm_budget chunking/sampling, llmtrace failure paths and share translation seam."""

import re
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import settings
from app.services import activity, llm_budget, llm_client, llmtrace, share

# --- llm_budget ---------------------------------------------------------------


def test_fits_is_inclusive() -> None:
    assert llm_budget.fits("abc", 3) and not llm_budget.fits("abcd", 3)


def test_split_paragraphs_packs_whole_paragraphs_and_drops_blanks() -> None:
    text = "aaa\n\n\n\nbbb\n\n   \n\nccccc"
    assert llm_budget.split_paragraphs(text, 8) == ["aaa\n\nbbb", "ccccc"]
    assert llm_budget.split_paragraphs("", 10) == []
    assert llm_budget.split_paragraphs("abc", 0) == ["a", "b", "c"]  # limit floors at 1


def test_split_paragraphs_cuts_long_paragraph_on_sentences_then_words_then_chars() -> None:
    sentences = "One two three. Four five six. Seven eight nine."
    chunks = llm_budget.split_paragraphs(sentences, 20)
    assert chunks == ["One two three.", "Four five six.", "Seven eight nine."]

    words = llm_budget.split_paragraphs("alpha beta gamma delta epsilon", 12)
    assert all(len(c) <= 12 for c in words)
    assert " ".join(" ".join(words).split()) == "alpha beta gamma delta epsilon"

    cjk = "字" * 25
    pieces = llm_budget.split_paragraphs(cjk, 10)
    assert [len(p) for p in pieces] == [10, 10, 5] and "".join(pieces) == cjk


def test_split_long_sentence_flushes_pending_text_first() -> None:
    long_sentence = "x" * 30
    text = f"Short one. {long_sentence}. Tail."
    chunks = llm_budget.split_paragraphs(text, 15)
    assert chunks[0] == "Short one."
    assert all(len(c) <= 15 for c in chunks)
    assert re.sub(r"\s+", "", "".join(chunks)) == re.sub(r"\s+", "", text)


def test_split_words_flushes_current_before_forced_cut() -> None:
    out = llm_budget._split_words("hi " + "y" * 12, 5)
    assert out == ["hi", "yyyyy", "yyyyy", "yy"]


def test_sample_evenly_keeps_ends_and_order() -> None:
    items = list(range(10))
    assert llm_budget.sample_evenly(items, 20) == items
    assert llm_budget.sample_evenly(items, 3) == [0, 4, 9]
    picked = llm_budget.sample_evenly(items, 4)
    assert picked[0] == 0 and picked[-1] == 9 and picked == sorted(picked)
    assert llm_budget.sample_evenly(items, 1) == [5]
    assert llm_budget.sample_evenly([], 0) == []


def test_sample_text_spreads_over_whole_text() -> None:
    paragraphs = [f"para{i} " + "w" * 40 for i in range(20)]
    text = "\n\n".join(paragraphs)
    out = llm_budget.sample_text(text, 300)
    assert len(out) <= 300
    assert out.startswith("para0 ") and "para19" in out  # not just the beginning
    assert llm_budget.sample_text("short", 100) == "short"


def test_sample_text_single_blob_and_tiny_budget() -> None:
    blob = "word " * 200
    out = llm_budget.sample_text(blob, 120)
    assert 0 < len(out) <= 120
    one_para = "a" * 500
    sampled = llm_budget.sample_text(one_para, 50)
    assert 0 < len(sampled) <= 50 and set(sampled) <= {"a", "\n"}


# --- llmtrace -----------------------------------------------------------------


@pytest.fixture()
def trace_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_trace_enabled", True)
    llmtrace.clear()
    yield
    llmtrace.clear()


def test_disabled_tracing_returns_no_record(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_trace_enabled", False)
    assert llmtrace.begin_chat("m", "s", "u") is None
    assert llmtrace.begin_embed("m", ["t"]) is None


def test_closing_a_missing_record_is_noop() -> None:
    llmtrace.complete(None, "x", latency_ms=1, usage=None)
    llmtrace.fail(None, "boom")


def test_chat_record_lifecycle_and_truncation(trace_on, monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_trace_max_chars", 100)
    with llmtrace.context("summarize", label="Title", article_id=7):
        rec = llmtrace.begin_chat("model-x", "s" * 250, "short user")
    assert rec is not None
    assert rec["kind"] == "summarize" and rec["article_id"] == 7 and rec["status"] == "running"
    assert rec["request"]["system_chars"] == 250 and len(rec["request"]["system"]) == 100
    assert rec["request"]["truncated"] is True

    llmtrace.complete(rec, {"a": "é"}, latency_ms=42, usage={"total_tokens": 3}, attempts=2)
    assert rec["status"] == "done" and rec["response"] == '{"a": "é"}'
    assert rec["latency_ms"] == 42 and rec["attempts"] == 2
    assert llmtrace.recent(1)[0]["id"] == rec["id"]

    unannotated = llmtrace.begin_chat("m", "s", "u")
    assert unannotated is not None and unannotated["kind"] == "other"
    llmtrace.fail(unannotated, "e" * 900, latency_ms=5)
    assert unannotated["status"] == "error" and len(unannotated["error"]) == 500
    assert [r["id"] for r in llmtrace.recent()] == [unannotated["id"], rec["id"]]
    assert len(llmtrace.recent(0)) == 0


def test_embed_record_and_context_is_restored(trace_on) -> None:
    assert llmtrace._current.get() is None
    with llmtrace.context("embed_story"):
        assert llmtrace._current.get()["kind"] == "embed_story"
        rec = llmtrace.begin_embed("e-model", ["abc", "de"])
    assert llmtrace._current.get() is None
    assert rec is not None and rec["endpoint"] == "embed"
    assert llmtrace.begin_embed("e", []) is not None  # empty batch is fine


def test_tracing_never_raises(trace_on, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_payload: dict[str, Any]) -> None:
        raise RuntimeError("sse down")

    monkeypatch.setattr(activity, "broadcast", boom)
    assert llmtrace.begin_chat("m", "s", "u") is None
    assert llmtrace.begin_embed("m", ["t"]) is None
    rec: dict[str, Any] = {"status": "running"}
    llmtrace.complete(rec, "ok", latency_ms=1, usage=None)  # swallowed
    llmtrace.fail(rec, "bad")
    assert rec["status"] == "error"
    unserialisable = {"status": "running"}
    llmtrace.complete(unserialisable, {"x": object()}, latency_ms=1, usage=None)
    assert unserialisable["status"] == "running"


# --- share --------------------------------------------------------------------


def _art(i: int, published: Any = None, title: str = "") -> Any:
    return SimpleNamespace(id=i, url=f"https://s/{i}", title=title, published_at=published)


async def test_prepare_share_requires_articles() -> None:
    with pytest.raises(share.ShareError, match="no articles"):
        await share.prepare_share(SimpleNamespace(title="t", summary="s"), [], None)  # type: ignore[arg-type]


async def test_translate_seam_calls_chat_json_with_traced_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def fake_chat_json(system: str, user: str) -> tuple[dict[str, Any], int]:
        seen["ctx"] = llmtrace._current.get()
        seen["prompt"] = (system, user)
        return {"title": "T2", "summary": "S2"}, 99

    monkeypatch.setattr(llm_client, "chat_json", fake_chat_json)
    data, latency = await share._translate("Headline", "Body", "French")
    assert (data, latency) == ({"title": "T2", "summary": "S2"}, 99)
    assert seen["ctx"]["kind"] == "share_translate" and "French" in seen["ctx"]["label"]
    assert "Headline" in seen["prompt"][1] and "French" in "".join(seen["prompt"])
    assert llmtrace._current.get() is None
