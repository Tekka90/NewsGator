"""Seed a demo database with realistic fake data — for screenshots and demos.

No LLM or network needed: stories, summaries, activity events and usage rows
are written directly. Story images are generated as local SVG files; serve the
``demo_assets/`` directory over HTTP so the GUI can load them::

    cd backend
    DATABASE_URL=sqlite+aiosqlite:///./demo.db python -m app.scripts.seed_demo
    python -m http.server 8899 --directory demo_assets &   # story images
    DATABASE_URL=sqlite+aiosqlite:///./demo.db uvicorn app.main:app

Login: admin / demo1234
"""

from __future__ import annotations

import asyncio
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete

from app.core.config import settings
from app.core.db import Base, get_engine, init_engine
from app.core.security import hash_password
from app.models import (
    ActivityEvent,
    Article,
    Category,
    Feed,
    LLMUsage,
    Story,
    StoryRevision,
    StoryState,
    User,
)

init_engine(settings.database_url)

IMAGE_DIR = Path("demo_assets/img")
IMAGE_BASE = "http://localhost:8899/img"

rng = random.Random(42)
now = datetime.now(UTC)

FEEDS = [
    ("Le Monde", "https://www.lemonde.fr/rss/une.xml"),
    ("BBC News — World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
    ("Hacker News", "https://hnrss.org/frontpage"),
    ("Frandroid", "https://www.frandroid.com/feed"),
    ("The Guardian — World", "https://www.theguardian.com/world/rss"),
]

# Host used in article URLs (what the GUI shows + fetches favicons for)
ARTICLE_HOSTS = [
    "www.lemonde.fr",
    "www.bbc.com",
    "arstechnica.com",
    "news.ycombinator.com",
    "www.frandroid.com",
    "www.theguardian.com",
]

CATEGORIES = ["World", "Tech", "Science", "Economy", "Culture"]

# (title, summary, category, version, image?, read_state,
#  [(feed_idx, lang, hours_ago, article_title)])
ArticleSpec = tuple[int, str, float, str]
StorySpec = tuple[str, str, str, int, bool, str, list[ArticleSpec]]
STORIES: list[StorySpec] = [
    (
        "EU reaches agreement on AI liability rules after marathon negotiation",
        "After 36 hours of talks, EU member states agreed on a liability framework for "
        "AI systems: providers of general-purpose models face strict documentation duties, "
        "and a new Office for AI Incidents will collect mandatory failure reports. "
        "Fines are capped lower than the Parliament wanted; the text now goes to a "
        "plenary vote in October.",
        "World", 4, True, "updated",
        [
            (0, "fr", 30, "IA : Bruxelles s'accorde sur un régime de responsabilité historique"),
            (1, "en", 29, "EU agrees landmark AI liability rules after marathon talks"),
            (5, "en", 27, "AI liability deal reached in Brussels — what changes for developers"),
            (2, "en", 22, "The EU's AI liability framework, explained"),
        ],
    ),
    (
        "Europa Clipper sends back first high-resolution images of Jupiter's icy moon",
        "NASA released the first close-range images from Europa Clipper's flyby, showing "
        "fractured ice plains and possible plume deposits. Mission scientists say the "
        "surface looks younger than models predicted, strengthening the case for active "
        "exchange between the subsurface ocean and the crust.",
        "Science", 2, True, "unread",
        [
            (1, "en", 12, "Europa Clipper beams back first high-res images"),
            (3, "en", 9, "First Europa Clipper close-ups show a surprisingly young surface"),
        ],
    ),
    (
        "Apple opens on-device LLM framework to third-party developers",
        "Apple's new Foundation Models framework lets apps run the on-device ~3B model "
        "with guided generation and tool calling, free of per-token fees. Early benchmarks "
        "show latency competitive with cloud APIs for short tasks; larger requests still "
        "hand off to Private Cloud Compute.",
        "Tech", 3, True, "unread",
        [
            (2, "en", 26, "Apple opens its on-device LLM to every app"),
            (4, "fr", 25, "Apple ouvre son modèle local aux développeurs tiers"),
            (3, "en", 20, "Hands-on with Apple's Foundation Models framework"),
        ],
    ),
    (
        "Central banks signal a coordinated rate path for 2027",
        "Minutes from the Fed, ECB and BoJ all point to a cautious, synchronized easing "
        "cycle next year. Markets read the alignment as an attempt to avoid the currency "
        "whipsaw that followed the desynchronized 2025 cuts.",
        "Economy", 1, False, "read",
        [
            (1, "en", 34, "Fed, ECB and BoJ hint at coordinated 2027 easing"),
            (0, "fr", 33, "Taux : vers une détente coordonnée des banques centrales en 2027"),
        ],
    ),
    (
        "Solid-state battery prototype passes 1,000 fast-charge cycles with 92% capacity retained",
        "A Toyota / Sumitomo prototype cell retained 92% of capacity after 1,000 10-minute "
        "charges at 45°C — the durability bar automakers set for commercialization. "
        "Pilot-line output is now expected in late 2027, a year earlier than previously "
        "guided, though cost per kWh remains the open question.",
        "Tech", 3, True, "unread",
        [
            (2, "en", 15, "Solid-state battery clears the 1,000-cycle bar"),
            (4, "fr", 14, "Batterie solide : un prototype tient 1 000 cycles rapides"),
            (3, "en", 8, "Why the 1,000-cycle milestone matters more than energy density"),
        ],
    ),
    (
        "France places 14 departments on red alert as record heatwave builds",
        "Météo-France issued red alerts across the south-west with 44°C expected locally "
        "on Saturday. Night-time minima above 26°C are the main health concern; cities "
        "are opening cooled public spaces and cancelling outdoor events.",
        "World", 2, True, "unread",
        [
            (0, "fr", 6, "Canicule : 14 départements en vigilance rouge, 44°C attendus"),
            (5, "en", 5, "France braces for record-breaking August heatwave"),
        ],
    ),
    (
        "ROS 4.0 ships after three years, with real-time scheduling in the core",
        "The open-source robot operating system's first major release since 2023 moves "
        "real-time scheduling into the core executor and drops Python 3.10 support. "
        "Maintainers call it the largest breaking release in the project's history.",
        "Tech", 1, False, "read",
        [
            (3, "en", 40, "ROS 4.0 released"),
        ],
    ),
    (
        "Deep-sea mining moratorium gains 12 new signatories ahead of ISA vote",
        "Twelve more countries, including Brazil and Indonesia, joined the call for a "
        "moratorium on deep-sea mining, bringing the bloc to 44 ahead of next month's "
        "International Seabed Authority session. Proponents argue the science on nodule "
        "ecosystems remains decades away from settled.",
        "Science", 2, True, "unread",
        [
            (5, "en", 18, "Deep-sea mining moratorium gains twelve new backers"),
            (1, "en", 16, "The geopolitics of the ocean floor heats up"),
        ],
    ),
    (
        "Streaming prices rise again as the great unbundling quietly reverses",
        "Three major platforms announced simultaneous price hikes this week, and analysts "
        "note the bundles being re-assembled look increasingly like the cable packages "
        "streaming was supposed to kill.",
        "Culture", 1, False, "read",
        [
            (2, "en", 44, "Streaming is becoming cable, one price hike at a time"),
            (4, "fr", 43, "Streaming : nouvelle vague de hausses de prix"),
        ],
    ),
]

PALETTES = [
    ("#0f2027", "#2c5364", "#4ade80"),
    ("#1a2a6c", "#b21f1f", "#fdbb2d"),
    ("#134e5e", "#71b280", "#a3e635"),
    ("#232526", "#414345", "#7dd3fc"),
    ("#3a1c71", "#d76d77", "#ffaf7b"),
    ("#0f3443", "#34e89e", "#d9f99d"),
]


def make_svg(i: int) -> str:
    a, b, c = PALETTES[i % len(PALETTES)]
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="675"
 viewBox="0 0 1200 675">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="{a}"/><stop offset="1" stop-color="{b}"/></linearGradient></defs>
<rect width="1200" height="675" fill="url(#g)"/>
<circle cx="{200 + 90 * i}" cy="{180 + 40 * i}" r="260" fill="{c}" opacity="0.18"/>
<circle cx="{900 - 60 * i}" cy="520" r="180" fill="{c}" opacity="0.12"/>
<rect x="{620 - 30 * i}" y="120" width="420" height="240" rx="24" fill="#ffffff"
 opacity="0.07" transform="rotate({-6 + i * 2} 800 240)"/>
<path d="M0 560 Q 300 {440 + 20 * i} 600 540 T 1200 500 V 675 H 0 Z" fill="#000000" opacity="0.22"/>
<path d="M0 610 Q 300 {520 + 15 * i} 650 590 T 1200 560 V 675 H 0 Z" fill="#000000" opacity="0.28"/>
</svg>"""


def svg_urls() -> list[str]:
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    urls = []
    for i in range(6):
        (IMAGE_DIR / f"s{i}.svg").write_text(make_svg(i))
        urls.append(f"{IMAGE_BASE}/s{i}.svg")
    return urls


async def main() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    from sqlalchemy.ext.asyncio import async_sessionmaker

    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        # idempotent: wipe existing demo content
        models = (
            LLMUsage, ActivityEvent, StoryRevision, StoryState,
            Article, Story, Feed, Category, User,
        )
        for model in models:
            await s.execute(delete(model))

        images = svg_urls()
        admin = User(username="admin", password_hash=hash_password("demo1234"), is_admin=True)
        s.add(admin)
        s.add_all([Category(name=c) for c in CATEGORIES])
        feeds = [Feed(title=t, url=u, last_fetched_at=now - timedelta(minutes=7)) for t, u in FEEDS]
        s.add_all(feeds)
        await s.flush()

        img_i = 0
        stories: list[Story] = []
        for title, summary, cat, version, has_img, state, arts in STORIES:
            pub_base = now - timedelta(hours=max(a[2] for a in arts))
            story_img = images[img_i % len(images)] if has_img else None
            if has_img:
                img_i += 1
            story = Story(
                title=title, summary=summary, category=cat, version=version,
                image_url=story_img, first_seen_at=pub_base,
                last_updated_at=now - timedelta(hours=min(a[2] for a in arts) * 0.4),
            )
            s.add(story)
            await s.flush()
            for v in range(1, version):
                s.add(StoryRevision(
                    story_id=story.id, version=v,
                    summary=summary[: int(len(summary) * (0.55 + 0.15 * v))] + "…",
                    created_at=pub_base + timedelta(hours=3 * v),
                ))
            for feed_idx, lang, hours_ago, atitle in arts:
                take = f"[{feeds[feed_idx].title}] take: {summary[:110]}…"
                s.add(Article(
                    feed_id=feeds[feed_idx].id,
                    guid=f"demo-{story.id}-{feed_idx}",
                    url=f"https://{ARTICLE_HOSTS[feed_idx]}/articles/{abs(hash(atitle)) % 10**8}",
                    title=atitle, language=lang, summary=take,
                    full_text=f"Full text of “{atitle}”.\n\n{summary}\n\n(demo content)",
                    category=cat, story_id=story.id, processing_state="clustered",
                    image_url=story_img, published_at=now - timedelta(hours=hours_ago),
                ))
            if state == "read":
                s.add(StoryState(user_id=admin.id, story_id=story.id, is_read=True,
                                 read_at_version=version, read_at=now - timedelta(hours=2)))
            elif state == "updated":
                s.add(StoryState(user_id=admin.id, story_id=story.id, is_read=True,
                                 read_at_version=version - 1, read_at=now - timedelta(hours=20)))
            stories.append(story)

        # --- activity events (last ~3 hours) ---
        events: list[tuple[str, str, dict[str, object], float]] = [
            ("ingest", "feed_poll_start", {"feed": "Le Monde"}, 3.0),
            ("ingest", "feed_poll", {"feed": "Le Monde", "new_entries": 12}, 2.98),
            ("ingest", "feed_poll", {"feed": "BBC News — World", "new_entries": 8}, 2.95),
            ("fulltext", "fulltext_fetch",
             {"article_id": 4817, "path": "direct", "image_recovered": True}, 2.9),
            ("llm", "summarize_start", {"article_id": 4817}, 2.88),
            ("llm", "summarize_done",
             {"article_id": 4817, "latency_ms": 1830, "model": "qwen3-32b-mlx"}, 2.87),
            ("llm", "embed_done", {"article_id": 4817, "latency_ms": 120}, 2.86),
            ("cluster", "decision",
             {"article_id": 4817, "decision": "attach", "story_id": 1, "similarity": 0.91}, 2.85),
            ("cluster", "story_update", {"story_id": 1, "version": 4, "reason": "new facts"}, 2.84),
            ("ingest", "feed_poll", {"feed": "Ars Technica", "new_entries": 5}, 2.1),
            ("fulltext", "fulltext_fetch", {"article_id": 4820, "path": "direct"}, 2.0),
            ("llm", "summarize_done",
             {"article_id": 4820, "latency_ms": 2140, "model": "qwen3-32b-mlx"}, 1.95),
            ("cluster", "decision",
             {"article_id": 4820, "decision": "new", "similarity": 0.62}, 1.94),
            ("share", "prepare_done", {"story_id": 3, "language": "fr", "latency_ms": 1620}, 1.2),
            ("ingest", "feed_poll", {"feed": "Frandroid", "new_entries": 3}, 0.9),
            ("ingest", "backfill_skipped", {"feed": "Hacker News", "skipped": 4}, 0.8),
            ("retention", "purge_done", {"articles_removed": 87, "stories_removed": 11}, 0.5),
            ("ingest", "feed_poll", {"feed": "The Guardian — World", "new_entries": 9}, 0.2),
        ]
        for component, action, detail, hours_ago in events:
            s.add(ActivityEvent(
                ts=now - timedelta(hours=hours_ago), component=component,
                action=action, detail=json.dumps(detail),
                level="warn" if action == "backfill_skipped" else "info",
            ))

        # --- two weeks of LLM usage ---
        kinds_chat = [("summarize", 26, 1100, 320), ("pairwise", 12, 700, 60),
                      ("merge", 4, 2100, 480), ("headline", 4, 900, 40),
                      ("novelty", 3, 600, 90), ("share_translate", 1, 800, 260)]
        for day in range(14):
            date = now - timedelta(days=13 - day)
            scale = 0.6 + rng.random() * 0.8
            for kind, base_n, ptok, ctok in kinds_chat:
                for _ in range(max(1, int(base_n * scale))):
                    p = int(ptok * (0.7 + rng.random() * 0.6))
                    c = int(ctok * (0.7 + rng.random() * 0.6))
                    est = rng.random() < 0.08
                    s.add(LLMUsage(
                        ts=date - timedelta(hours=rng.random() * 14), kind=kind,
                        endpoint="chat", model="qwen3-32b-mlx",
                        prompt_tokens=None if est else p, completion_tokens=None if est else c,
                        total_tokens=None if est else p + c, estimated=est,
                        latency_ms=int(800 + rng.random() * 1800),
                        feed_id=rng.choice(feeds).id,
                        story_id=rng.choice(stories).id if rng.random() < 0.6 else None,
                    ))
            for _ in range(max(2, int(30 * scale))):
                p = int(420 * (0.7 + rng.random() * 0.6))
                s.add(LLMUsage(
                    ts=date - timedelta(hours=rng.random() * 14), kind="embed",
                    endpoint="embed", model="bge-m3", prompt_tokens=p,
                    total_tokens=p, latency_ms=int(40 + rng.random() * 160),
                    feed_id=rng.choice(feeds).id,
                ))

        await s.commit()
        print("Demo database seeded.")
        print(f"  admin / demo1234 — {len(STORIES)} stories, {len(FEEDS)} feeds")
        print(f"  story images written to {IMAGE_DIR}/ — serve with:")
        print("  python -m http.server 8899 --directory demo_assets")


if __name__ == "__main__":
    asyncio.run(main())
