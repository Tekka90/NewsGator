# SPEC: Dual-Mode Feed Discovery & Verification

> **Status:** Approved Architecture  
> **Applicability:** NewsGator-Apple (Native iOS/macOS) & NewsGator (Python/FastAPI Server Mode)

---

## 1. Core Principles & Philosophy

1. **Zero Hardcoded Translations or City Lists:**  
   No manual translation dictionaries, hardcoded word arrays, or static seed publications. All internationalization and translation utilize system capabilities (`Translation` framework, system `Locale`).
2. **Neutral Identity:**  
   Underlying external directory services are internal implementation details. The user-facing UI and documentation must strictly use neutral terms: **"Catalog Search"** and **"Smart Search (AI)"**. Never display or reference third-party catalog vendor names anywhere.
3. **Hardware-Adaptive & User Choice:**  
   Devices with Apple Intelligence (or servers with active LLM endpoints) support both **Smart Search (AI)** and **Catalog Search**. Users can toggle between them freely. Devices without AI run fast, high-quality **Catalog Search**.
4. **Live 1-by-1 Streaming:**  
   Candidate feeds are resolved, verified, and checked for paywalls one at a time, streaming directly into UI cards with real-time progress and clean pagination ("Show More").

---

## 2. The Two Discovery Modes

```
                        ┌──────────────────────────────────────────────┐
                        │             Feed Discovery Sheet             │
                        │   Mode Switcher: [Catalog Search | Smart AI] │
                        └──────────────────────┬───────────────────────┘
                                               │
                       ┌───────────────────────┴───────────────────────┐
                       ▼                                               ▼
         ┌───────────────────────────┐                   ┌───────────────────────────┐
         │      Catalog Search       │                   │     Smart Search (AI)     │
         │  (Deterministic, No AI)   │                   │ (Apple Intelligence / LLM)│
         └─────────────┬─────────────┘                   └─────────────┬─────────────┘
                       │                                               │
         1. Select Region/Locale from dynamic            1. Freeform location & custom
            system list (e.g. 🇨🇦 Canada - fr_CA)            niche/topic prompts.
         2. Select single-topic categories.              2. LLM prompts for authoritative,
         3. Dynamic on-device translation of                local, and independent media.
            category keywords via Apple                  3. Resolves publisher domains.
            Translation framework.                                     │
         4. Optional city or keywords search.                          │
         5. Query directory API by localized                           │
            topic tag (#sante) or city name.                           │
         6. Rank results by subscribers DESC.                          │
                       │                                               │
                       └───────────────────────┬───────────────────────┘
                                               │
                                               ▼
                               ┌───────────────────────────────┐
                               │ 1-by-1 Streaming Verification │
                               │  - Direct RSS/Atom resolution │
                               │  - Fetch sample headlines     │
                               │  - Assess paywall & fulltext  │
                               │  - Stream card to UI live     │
                               │  - "Show More" pagination     │
                               └───────────────────────────────┘
```

---

## 3. Mode 1: Catalog Search (Deterministic)

### A. Locale & Region Selector UI
- **Dynamic Population:** Built at runtime from `Locale.Region.isoRegions` and `Locale.Language`.
- **Display Format:** `[Flag Emoji] [Localized Country Name] — [Localized Language Name]`  
  *Examples:*
  - 🇫🇷 France — Français (`fr_FR`)
  - 🇨🇦 Canada — Français (`fr_CA`)
  - 🇨🇦 Canada — English (`en_CA`)
  - 🇩🇪 Germany — Deutsch (`de_DE`)
  - 🇪🇸 Spain — Español (`es_ES`)
  - 🇺🇸 United States — English (`en_US`)
  - 🇯🇵 Japan — 日本語 (`ja_JP`)
- **Default:** Automatically initializes to `Locale.current`.
- **Searchable:** Includes a live search filter in the picker sheet/menu to find countries and languages instantly.

### B. Single-Topic Category Model
Compound categories are divided into distinct single topics:
- **Technology** (icon: `laptopcomputer`, key: `tech`)
- **Artificial Intelligence** (icon: `brain`, key: `artificial-intelligence`)
- **General News** (icon: `newspaper`, key: `news`)
- **Politics** (icon: `building.columns`, key: `politics`)
- **Business** (icon: `briefcase`, key: `business`)
- **Finance** (icon: `chart.line.uptrend.xyaxis`, key: `finance`)
- **Science** (icon: `atom`, key: `science`)
- **Health** (icon: `cross.case`, key: `health`)
- **Environment** (icon: `leaf`, key: `environment`)
- **Sports** (icon: `figure.run`, key: `sports`)
- **Culture** (icon: `paintpalette`, key: `culture`)
- **Gaming** (icon: `gamecontroller`, key: `gaming`)
- **Cybersecurity** (icon: `lock.shield`, key: `cybersecurity`)

### C. Zero-Hardcoding Dynamic Translation
To query topics accurately in non-English locales without hardcoded dictionary tables:
- NewsGator utilizes Apple’s on-device **`Translation` framework** (`import Translation` / `TranslationSession`).
- The session translates the English topic key into the selected locale's target language dynamically on-device in under 5ms.
- The resulting term is formatted as a topic tag (e.g., `#sante` for French, `#gesundheit` for German, `#salud` for Spanish).

### D. City & Keyword Search
- If a city or regional keyword is entered (e.g. *Montréal*, *Lyon*, *München*, *Chicago*), the search queries the directory API directly with the selected `locale`.
- **Verified Behavior Across Locales:**
  - `Montreal` + `locale=fr_CA` → *Journal Métro*, *Montréal Campus*, *Montréal Counter-info*
  - `Quebec` + `locale=fr_CA` → *Québec Urbain*, *Québec Science*, *Fil RSS quebec.ca*
  - `Lyon` + `locale=fr_FR` → *Lyon Capitale*, *Lyon Mag*, *Lyon CityCrunch*, *Le Progrès*
  - `München` + `locale=de_DE` → *München SZ.de*, *MünchenBlogger*, *München News*
  - `Chicago` + `locale=en_US` → *Chicago Reader*, *Eater Chicago*, *Chicago Sun-Times*
- Results from multiple selected categories are fetched in parallel, merged, deduplicated by feed host, and sorted by **`subscribers` descending**.

---

## 4. Mode 2: Smart Search (AI-Driven)

### A. Availability
- Displayed when `ModelProvider.resolve()` succeeds on Apple platforms, or when `LLM_BASE_URL` is configured in Server Mode.
- Users who have AI enabled can still switch to **Catalog Search** via a segmented control or menu at the top of the discovery sheet.

### B. Freeform Inputs & LLM Generation
- Allows free-text location entry ("Lyon and Rhone Alps", "Quebec Eastern Townships", "Silicon Valley").
- Allows custom user prompts ("independent investigative journalism", "climate tech and renewable energy").
- Prompt requests structured publisher domains and verified independent outlets.

---

## 5. Unified 1-by-1 Streaming, Verification & Progress UI

Regardless of whether candidate feed links originated from Catalog Search or Smart Search (AI):

1. **Deduplication:** Hostnames already subscribed to or previously discovered in the current session are skipped.
2. **Sequential Verification Loop:**
   - Query Feedsearch / direct HTML `<link rel="alternate">` to confirm the active RSS/Atom URL.
   - Fetch the feed payload; parse the latest 3 sample article headlines.
   - Analyze content length and metadata for paywalls (`free_full`, `free_excerpt`, `paywalled`).
   - Immediately yield the candidate to the UI `AsyncStream` / published array.
3. **Determinate Progress Bar in GUI:**
   - Replaces the old indeterminate spinner with a **determinate linear `ProgressView`**:
     ```swift
     ProgressView(value: Double(currentStep), total: Double(totalCandidates))
         .progressViewStyle(.linear)
         .tint(Color.gatorForest)
     ```
   - **Step-by-Step Status Label:**
     - Displays the active publication name and fraction:  
       `"Verifying 3 of 10: Le Monde (lemonde.fr)…"`
     - Shows current status badges: `"Resolving RSS"`, `"Checking paywall"`, `"Discovered"`.
   - **Live Incremental Card Insertion:**  
     Cards appear in the results list in real-time as each feed passes verification, allowing the user to inspect and select feeds immediately without waiting for the entire batch to finish.
4. **Pagination:** Each batch delivers up to 10 verified feeds. A **"Show More"** button continues down the candidate queue with the exact same incremental progress bar.

---

## 6. Server Mode Architecture & API Contract (`Newsgator/backend`)

When operating in **Server Mode** (FastAPI backend + SvelteKit webapp, or Apple client in Server Mode):

### A. Endpoint Contract (`POST /api/feeds/discover`)
The endpoint accepts:
```json
{
  "location": "Lyon",
  "themes": ["tech", "ai"],
  "query": "optional keywords",
  "mode": "catalog", // "catalog" | "smart" (default: "smart" if LLM configured, else "catalog")
  "locale": "fr_FR",  // ISO locale code (e.g. "fr_FR", "fr_CA", "de_DE")
  "excluded_urls": []
}
```

### B. Execution Flow on Server:
1. **Mode Selection:**
   - If `mode == "smart"` AND `llm_client.is_configured()`:
     - LLM runs Turn 1 query formulation to output authoritative and independent publication domains.
   - If `mode == "catalog"` OR `not llm_client.is_configured()`:
     - Server queries the directory catalog directly using `#topic` or city keyword with the provided `locale`.
     - When translating category topics on server without Apple Intelligence: if LLM is active, LLM translates the topic word; if no LLM is configured, server uses standard ISO language mappings or English tag fallback.
     - Results are ranked strictly by `subscribers` descending.
2. **Feed Verification:**
   - Server probes candidates using Feedsearch API / direct `<link rel="alternate">` discovery.
   - Probes live XML using `feedparser` via `anyio.to_thread`.
   - Parses the latest 3 sample article headlines.
   - Evaluates content length & accessibility (`free_full`, `free_excerpt`, `paywalled`).
3. **Activity Logging:**
   - Emits structured progress events to `ACTIVITY_LOG` and the SSE stream (`/api/activity/stream`).

### C. Server-Side Code Removal:
- **Delete `CURATED_SEEDS` dictionary entirely** in `discovery.py` (no hardcoded seeds).
- **Delete DuckDuckGo web/lite scraping** and regexes in `discovery.py`.

---

## 7. Client Code Removal & Cleanup

### Apple Client (`Newsgator-Apple`):
- **Remove all DuckDuckGo scraping:** Delete `searchDuckDuckGo(...)`, HTML regex parsers, form encoding workarounds, and DDG Lite error models in `FeedDiscoveryActor.swift`.
- **Remove US fallback seeds:** Eliminate fallback blocks that substitute US tech publications on regional search misses.
- **Remove Google News scraping:** Delete redirect decoders and article scraper paths.

### Python Server (`Newsgator/backend`):
- Clean up `discovery.py` to eliminate DDG web scraping fallbacks.
- Update prompts in `prompts.py` to reflect single-topic taxonomy and domain-only generation.
