"""Video detection (VIDEO_ARTICLES_PLAN §4): shared Python/Swift vectors + edge cases."""

import json
from pathlib import Path
from typing import Any

import pytest

from app.services import video_detect as vd

FIXTURES = Path(__file__).parent / "fixtures"
VECTORS = json.loads((FIXTURES / "video_detect_vectors.json").read_text(encoding="utf-8"))
BASE = "https://example.com/a/b"


def _page(name: str) -> str:
    return (FIXTURES / "video_pages" / name).read_text(encoding="utf-8")


def _expected(hint: vd.VideoHint | None) -> dict[str, Any] | None:
    if hint is None:
        return None
    return {
        "url": hint.url,
        "provider": hint.provider,
        "embed_url": hint.embed_url,
        "youtube_id": hint.youtube_id,
        "duration_s": hint.duration_s,
        "description": hint.description,
    }


def test_vectors_are_well_formed() -> None:
    assert VECTORS["main_content_max_chars"] == vd.VIDEO_MAIN_CONTENT_MAX_CHARS == 1500
    assert VECTORS["min_body_paragraph_chars"] == vd.MIN_BODY_PARAGRAPH_CHARS
    files = {p.name for p in (FIXTURES / "video_pages").glob("*.html")}
    assert files == {c["file"] for c in VECTORS["cases"]}


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c["name"])
def test_detection_vectors(case: dict[str, Any]) -> None:
    hint = vd.detect_video(_page(case["file"]), case["base_url"], case["body_chars"])
    assert _expected(hint) == case["expect"]


@pytest.mark.parametrize("case", VECTORS["duration"], ids=lambda c: repr(c["input"]))
def test_duration_vectors(case: dict[str, Any]) -> None:
    assert vd.parse_iso_duration(case["input"]) == case["seconds"]


def test_duration_rejects_non_strings() -> None:
    assert vd.parse_iso_duration(52) is None
    assert vd.parse_iso_duration(None) is None


# --- the France 24 page of the plan (https://f24.my/CEi8) ---------------------------------


def test_france24_page_is_flagged_with_youtube_fields() -> None:
    hint = vd.detect_video(_page("france24_cei8.html"), "https://f24.my/CEi8", 120)
    assert hint is not None
    assert hint.provider == vd.PROVIDER_YOUTUBE
    assert hint.youtube_id == "dQw4w9WgXcQ"
    assert hint.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert hint.embed_url == "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"
    assert hint.duration_s == 52
    assert hint.description and hint.description.startswith("Dozens of aid trucks")
    assert vd.video_fields(hint) == {
        "media_kind": "video",
        "video_url": hint.url,
        "video_embed_url": hint.embed_url,
        "video_provider": "youtube",
        "video_id": "dQw4w9WgXcQ",
        "video_duration": 52,
    }


def test_france24_page_stays_flagged_whatever_the_body_length() -> None:
    # the page *is* a video (JSON-LD VideoObject): D10 does not look at the text length
    assert vd.detect_video(_page("france24_cei8.html"), "", 50_000) is not None


# --- main-content decision (D6/D10) --------------------------------------------------------


def test_threshold_boundary_is_exclusive() -> None:
    page = _page("text_article_small_embed.html")
    limit = vd.VIDEO_MAIN_CONTENT_MAX_CHARS
    assert vd.detect_video(page, BASE, limit - 1) is not None
    assert vd.detect_video(page, BASE, limit) is None


def test_small_embed_in_long_text_article_is_not_flagged() -> None:
    assert vd.detect_video(_page("text_article_small_embed.html"), BASE, 6000) is None


def test_video_before_first_body_paragraph_is_main_content_even_with_long_body() -> None:
    assert vd.detect_video(_page("video_before_short_paragraph.html"), BASE, 6000) is not None
    assert vd.detect_video(_page("video_after_body.html"), BASE, 6000) is None


def test_candidate_signals() -> None:
    candidate = vd.find_video(_page("og_type_video.html"), BASE)
    assert candidate is not None and candidate.page_is_video and not candidate.precedes_body
    nested = vd.find_video(_page("jsonld_article_video.html"), BASE)
    assert nested is not None and not nested.page_is_video
    assert vd.is_main_content(nested, 10) and not vd.is_main_content(nested, 1500)
    assert vd.find_video(_page("no_video.html"), BASE) is None


# --- signals and robustness ---------------------------------------------------------------


def test_json_ld_wins_over_page_content_signals() -> None:
    hint = vd.detect_video(_page("multiple_videos.html"), BASE, 0)
    assert hint is not None and hint.url == "https://cdn.example.com/main.mp4"
    assert hint.duration_s == 30


def test_page_level_video_object_outranks_nested_article_video() -> None:
    html = (
        '<script type="application/ld+json">{"@type":"NewsArticle",'
        '"video":{"@type":"VideoObject","contentUrl":"https://a.example/nested.mp4"}}</script>'
        '<script type="application/ld+json">{"@type":"VideoObject",'
        '"contentUrl":"https://a.example/page.mp4"}</script>'
    )
    hint = vd.detect_video(html, "", 0)
    assert hint is not None and hint.url == "https://a.example/page.mp4"


def test_nested_video_list_takes_the_first_usable_object() -> None:
    hint = vd.detect_video(_page("jsonld_article_video.html"), BASE, 0)
    assert hint is not None
    assert (hint.provider, hint.url, hint.duration_s) == (
        "embed", "https://player.vimeo.com/video/123456", 45,
    )


def test_malformed_json_ld_is_ignored_and_content_signal_used() -> None:
    hint = vd.detect_video(_page("malformed_jsonld.html"), BASE, 0)
    assert hint is not None and hint.url == "https://player.vimeo.com/video/555"


def test_relative_urls_are_resolved_against_the_page_url() -> None:
    graph = vd.detect_video(_page("jsonld_graph.html"), "https://news.example.org/x/y", 0)
    assert graph is not None
    assert graph.url == "https://news.example.org/videos/graph-clip.mp4"
    assert (graph.provider, graph.duration_s) == ("file", 3723)
    native = vd.detect_video(_page("native_video_mp4.html"), BASE, 0)
    assert native is not None
    assert native.url == "https://example.com/media/launch.mp4?token=a&b=2"


def test_relative_url_without_base_is_dropped() -> None:
    assert vd.detect_video(_page("jsonld_graph.html"), "", 0) is None


def test_protocol_relative_lazy_iframe_gets_https() -> None:
    hint = vd.detect_video(_page("dailymotion_iframe.html"), "", 0)
    assert hint is not None
    assert (hint.provider, hint.url) == ("embed", "https://www.dailymotion.com/embed/video/x8abcd1")
    assert hint.embed_url == hint.url


def test_unsafe_schemes_comments_and_scripts_never_produce_a_hint() -> None:
    assert vd.detect_video(_page("unsafe_urls.html"), BASE, 0) is None


def test_youtube_nocookie_iframe_is_canonicalised() -> None:
    hint = vd.detect_video(_page("youtube_nocookie_iframe.html"), BASE, 0)
    assert hint is not None
    assert hint.url == "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
    assert hint.youtube_id == "aqz-KE-bpKQ"


def test_video_element_falls_back_to_source_children_and_skips_blob() -> None:
    hint = vd.detect_video(_page("multiple_content_videos.html"), BASE, 0)
    assert hint is not None and hint.url == "https://cdn.example.com/second.mov"
    assert vd.detect_video(_page("native_video_src.html"), BASE, 0).provider == "file"  # type: ignore[union-attr]


def test_mime_hints_make_a_direct_file_without_extension() -> None:
    og = vd.detect_video(_page("og_video.html"), BASE, 0)
    assert og is not None and og.provider == "file"
    assert og.embed_url == "https://player.example.com/embed/42"
    stream = vd.detect_video(_page("twitter_stream.html"), BASE, 0)
    assert stream is not None and stream.url.endswith("stream.bin") and stream.provider == "file"
    hls = vd.detect_video(_page("og_type_video.html"), BASE, 0)
    assert hls is not None and hls.url.endswith("master.m3u8")


def test_non_media_url_without_embed_host_is_an_embed_player() -> None:
    html = '<meta name="twitter:player" content="https://player.example.com/x">'
    hint = vd.detect_video(html, "", 0)
    assert hint is not None and hint.provider == "embed"
    assert hint.embed_url == "https://player.example.com/x"
    page_only = (
        '<script type="application/ld+json">'
        '{"@type":"VideoObject","contentUrl":"https://example.com/watch/1"}</script>'
    )
    other = vd.detect_video(page_only, "", 0)
    assert other is not None and other.provider == "embed" and other.embed_url is None


def test_pages_without_a_video_and_consent_walls_return_none() -> None:
    assert vd.detect_video(_page("no_video.html"), BASE, 0) is None
    assert vd.detect_video(_page("consent_wall.html"), BASE, 0) is None
    assert vd.detect_video("", BASE, 0) is None


def test_detection_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(html: str, base_url: str = "") -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(vd, "find_video", boom)
    assert vd.detect_video("<video src='https://a/x.mp4'>", "", 0) is None
    assert vd.detect_video(None, "", 0) is None  # type: ignore[arg-type]


def test_deeply_nested_json_ld_is_bounded() -> None:
    nested = '{"video":' * 400 + "{}" + "}" * 400
    html = f'<script type="application/ld+json">{nested}</script>'
    assert vd.detect_video(html, "", 0) is None


def test_native_youtube_links_share_the_same_column_values() -> None:
    fields = vd.youtube_video_fields("https://youtu.be/dQw4w9WgXcQ?t=5")
    assert fields == {
        "media_kind": "video",
        "video_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "video_embed_url": "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
        "video_provider": "youtube",
        "video_id": "dQw4w9WgXcQ",
        "video_duration": None,
    }
    assert vd.youtube_video_fields("https://example.com/a") == {}
    assert vd.youtube_video_fields("") == {}
    assert vd.video_fields(None) == {}
