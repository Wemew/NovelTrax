# Feature Recommendations & Evaluation

This document is the documented evaluation of feature additions required by Requirement R2 of the original project request. It consolidates the MangaTrax comparison and candidate brainstorming produced during the survey phase into a single scored matrix, then divides the candidates into three groups: features that were **implemented** in this project (mapped explicitly to the twenty-four rows of the project feature inventory and the milestone that delivered them), features that are **recommended next** but were not built, and candidates that were **evaluated and deferred or rejected** with the reason. All claims about implemented behavior were re-verified against the code on 2026-11-02; source files and entry points are named so each row can be checked.

---

## 1. Scope, Sources and Scoring Method

### 1.1 Sources

| Source | Used for |
| :--- | :--- |
| `.agents/teamwork/ORIGINAL_REQUEST.md` | Requirement R2: "produce a documented evaluation of recommendations, and implement selected high-priority additions" |
| `.agents/teamwork/explorer_survey_2/handoff.md`, `analysis.md` | MangaTrax comparison, twelve candidate features F01-F12, scoring matrix and edge cases |
| `.agents/teamwork/orchestrator/PROJECT.md` | Authoritative feature inventory rows 1-24 and milestone status |
| `.agents/teamwork/orchestrator/GATE_STATUS.md` | Which milestone passed, on which iteration, and what reviewers rejected |
| `backend/app/**` (verified 2026-11-02) | Actual implementation state of every claimed feature |

### 1.2 Scoring dimensions

Each candidate is scored on the same five dimensions used by the survey-phase evaluation, so the implemented, recommended and rejected tables remain directly comparable:

- **User value** - how much a light novel reader running Calibre + NovelTrax gains from the capability.
- **Implementation complexity** - Low / Low-Medium / Medium / High, based on the code that was actually required (or is estimated for unbuilt items).
- **Architectural fit** - does it stay inside the existing FastAPI + SQLite WAL + Jinja2 + zero-lock Calibre architecture, or does it require new runtime services.
- **Risk** - regression, data-corruption, network-security and maintenance risk.
- **Priority** - for implemented items this records the delivering milestone; for unbuilt items it records the recommendation (High / Medium / Low-Medium / Backlog / Rejected).

The "Rationale" bullets under each table provide the required one-to-two sentence justification per row. Priorities are concrete to NovelTrax (EPUB light novels matched against a Calibre library), not generic wishlist items.

---

## 2. Section A - Implemented in this project

Eleven of the twelve survey-phase candidates (F01-F11) were implemented. F01's qBittorrent client and REST dispatch were delivered in M3, and the release-list Download action that completes the one-click flow landed in the M5 UI-wiring batch. The table below is the single consolidated evaluation matrix for the implemented set; the "Inventory row" column is the exact row number in `PROJECT.md`'s twenty-four-row feature inventory.

| ID | Feature | What it does | User value | Complexity | Architectural fit | Risk | Priority / delivered in | Inventory row |
| :--- | :--- | :--- | :---: | :---: | :---: | :---: | :--- | :---: |
| **F01** | In-app qBittorrent client (`QBitClient`) | Zero-dependency qBittorrent Web API v2 client: login, version/queue status, add torrent or magnet, pause/resume/delete, category `novels`, tag `noveltrax`; release rows/cards expose a one-click Download action backed by `POST /api/releases/{id}/download`. | High | Medium | High | Low | Delivered in **M3** (client + REST); release-list Download action completed in **M5** | 16 |
| **F02** | Downloads and queue dashboard (`/downloads`) | Dedicated tab with connection status, in-app host/port/URL/user/password/category/save-path/SSL configuration, live torrent queue and pause/resume/delete controls. | High | Medium | High | Low | Delivered in **M3** | 17 |
| **F03** | Selective volume downloading | Reads the torrent file list via `/api/v2/torrents/files`, parses volume numbers from filenames (including omnibus ranges and Oxford-comma lists), and sets `filePrio=0` for files whose volumes are all already owned in Calibre. | High | Medium | High | Low | Delivered in **M3** | 18 |
| **F04** | Nyaa light novel provider | Zero-auth provider for Nyaa category `3_1` (Literature - English-translated): RSS polling with HTML-table fallback, paced multi-page crawls, targeted per-series search, magnet/.torrent extraction, seeder/leecher/size metadata; seeded at first start and selectable as a preset in the Add-Feed dropdown. | Very High | Medium | High | Low | Delivered in **M4** (dropdown preset added in **M5**) | 19 |
| **F05** | Calibre omnibus expansion | Parses bundle titles in Calibre (`Overlord Vol 1-3`, `Title v01-02`, `Title [1-4]`, `Vol 1, 2, and 3`) into a complete owned-volume set stored in `series_tracking.owned_volumes_json`, so omnibus contents stop generating false requests. | High | Medium | High | Low | Delivered in **M2** | 12 |
| **F06** | Targeted single-series backfill | `POST /api/series/{id}/backfill` and `POST /api/series/backfill` run an on-demand background search for one series across Nyaa, with polite delays and a configurable page ceiling. | High | Medium | High | Low | Delivered in **M4** | 20 |
| **F07** | Prioritized release groups | `prioritized_groups` table (seeded with Yen Press, J-Novel Club, Seven Seas, Stick, One Peace Books), release-group extraction in the parser, `/prioritized` tab with VIP badges and a priority boost in the wanted-first grouped sort. | Medium-High | Medium | High | Low | Delivered in **M4** | 21 |
| **F08** | One-click ignore / restore | `POST /api/releases/{id}/ignore` and `/unignore`, with an `IGNORED` filter pill on Releases so a single bad release can be dismissed without blacklisting the whole series. | Medium | Low | High | Very Low | Delivered in **M4** | 22 |
| **F09** | Timezone localization | `backend/app/timezone_util.py`: `get_app_timezone`, `format_localtime`, `format_localdate`, DST-aware conversion with `Europe/Copenhagen` fallback, exposed as the `localtime`/`localdate` Jinja2 filters in `main.py`. | Medium | Low | High | Very Low | Delivered in **M2** (utility + filters; template wiring pending - see RN-02) | 13 |
| **F10** | Persistent metadata cache and rate limiting | `novel_metadata_cache` SQLite table for Kitsu/AniList results plus thread-safe sliding-window limiters (AniList 90 req/60 s, Kitsu 30 req/60 s) honoring `X-RateLimit-Remaining` and `Retry-After`. | Medium-High | Medium | High | Low | Delivered in **M2** | 14 |
| **F11** | Global top progress bar | `#page-progress-bar` plus nav/button loading feedback in `base.html`, giving instant visual acknowledgement during slow background-triggered requests. | Medium | Low | High | Very Low | Delivered in **M2** | 15 |

### 2.1 Rationale per implemented row

- **F01** - Without a downloader, NovelTrax could only tell the user a release existed; with `QBitClient` a wanted release can be dispatched to qBittorrent from the release list or through the application's own endpoint instead of a separate client script. The client speaks the public qBittorrent v2 API, so existing qBittorrent, Flood, TorBox and Real-Debrid qBit emulators all work; the M5 UI wiring added the row button and a disabled explanatory fallback when no torrent/magnet URL exists.
- **F02** - A downloader that cannot be observed is worse than none; the `/downloads` tab keeps queue state and credentials in the same UI as the rest of the workflow. In-app configuration also keeps the downloader fully optional for users who only want tracking.
- **F03** - Light novel batches are usually sold as large volume packs, so downloading a whole pack for one missing volume both wastes bandwidth and re-imports books Calibre already owns. Per-file priority keeps the missing volumes and deselects the rest, and deliberately keeps an unparseable file rather than risking data loss.
- **F04** - Nyaa category `3_1` is the broadest public source of English light novel EPUB releases, filling the coverage gap left by J-Novels and NovelUpdates. It requires no account or API key, matching NovelTrax's zero-configuration self-hosted model, and the M5 UI wiring made it selectable as an Add-Feed preset with the category `3_1` RSS URL prefilled (in addition to the auto-seeded default feed).
- **F05** - A Calibre library containing `Overlord Vol 1-3` previously registered only volume 1, so the app kept requesting volumes 2 and 3 forever. Omnibus expansion removes those false positives, which is the single largest matching-accuracy improvement in the project.
- **F06** - Global backfill searches everything the app knows about; a targeted per-series search answers the far more common question "where are the missing volumes of this one series". It runs in the existing background scheduler, so it reuses the established polite-delay and stop-check plumbing.
- **F07** - Official digital retail rips (`Yen Press`, `J-Novel Club`, `Seven Seas`, `Stick`) are consistently better than machine-translated web scrapes, so letting the user mark trusted groups changes which release they pick from a crowded list. The VIP badge and priority sort make that ordering visible without hiding other releases.
- **F08** - Fan-translation noise and misparsed titles otherwise pile up in the wanted list until the user blacklists an entire series. One-click ignore is the minimal intervention that keeps the series monitored while removing the noise, and restore keeps it reversible.
- **F09** - A single-container app displaying raw UTC makes release dates and audit timestamps hard to read, so the utility converts UTC to a chosen zone with automatic DST transitions and falls back safely on malformed input. The templates still render date slices directly; RN-02 wires the registered filters into them end to end.
- **F10** - Kitsu and AniList rate-limit aggressively and NovelTrax re-queries metadata on repeated page views. Persistent SQLite caching removes repeat lookups and the sliding-window limiter prevents throttling during large discovery syncs, without adding a cache server.
- **F11** - Several actions (feed checks, Calibre syncs, backfill) legitimately take seconds; without feedback users click twice. A 2.5 px top bar is a low-risk way to signal progress without touching any business logic.

### 2.2 Explicit traceability to all twenty-four inventory rows

The table below reproduces every row of `PROJECT.md`'s feature inventory. Rows 1-11 are the M1 security, simplification and defect work required by R3/R4 plus R1 baseline verification; rows 12-22 are the R2 feature additions; rows 23-24 are the M5 verification and adversarial-hardening work.

| Inventory row | Feature | Delivered in | Source |
| :---: | :--- | :---: | :--- |
| 1 | Baseline end-to-end workflows (zero-lock scan, title/volume extraction, auto-resolution, wishlist) | M1 | Survey 1 |
| 2 | J-Novels Master Index indentation defect fix in `fetch_master_index` | M1 | Survey 1 |
| 3 | Centralized SSRF guard (`app.security.is_safe_public_url`) | M1 | Survey 3 |
| 4 | Outbound network hardening across webhook, feed testing and scraper providers | M1 | Survey 3 |
| 5 | RSS LFI and SSRF remediation (pre-fetch before `feedparser.parse`) | M1 | Survey 3 |
| 6 | Webhook diagnostic leakage fix | M1 | Survey 3 |
| 7 | DOM XSS sanitization in `discovery.html` and `feeds.html` | M1 | Survey 3 |
| 8 | CSP and Permissions-Policy security headers | M1 | Survey 3 |
| 9 | Calibre path-traversal guard (`/api/calibre/test`) | M1 | Survey 3 |
| 10 | Dependency minimization (`python-multipart`, top-level `pydantic` removal) | M1 | Survey 3 |
| 11 | Dead-code elimination (`Form` import, duplicate `import asyncio`, redundant returns) | M1 | Survey 3 |
| 12 | Calibre omnibus parser (`owned_volumes_json`) | M2 | Survey 2 (F05) |
| 13 | Timezone localization (`timezone_util.py`, Jinja2 filters) | M2 | Survey 2 (F09) |
| 14 | SQLite metadata caching and rate limiting (`novel_metadata_cache`) | M2 | Survey 2 (F10) |
| 15 | UI top progress bar (`#page-progress-bar`) | M2 | Survey 2 (F11) |
| 16 | In-app qBittorrent client (`QBitClient`) | M3 | Survey 2 (F01) |
| 17 | Downloads dashboard UI and REST API | M3 | Survey 2 (F02) |
| 18 | Selective volume downloading (`apply_selective_volume_download`) | M3 | Survey 2 (F03) |
| 19 | Nyaa light novel provider (`NyaaNovelProvider`, category 3_1) | M4 | Survey 2 (F04) |
| 20 | Targeted series backfill API | M4 | Survey 2 (F06) |
| 21 | Prioritized groups management and `/prioritized` view | M4 | Survey 2 (F07) |
| 22 | One-click release ignore and restore | M4 | Survey 2 (F08) |
| 23 | End-to-end regression verification (Tiers 1-4) | M5 | Prompt and survey |
| 24 | Adversarial hardening (Tier 5) | M5 | Project pattern |

Milestone status per `GATE_STATUS.md`: M1 passed on iteration 2, M2 on iteration 4, M3 on iteration 3, M4 on iteration 4, and M5 is the closing verification milestone. The additions extended existing surfaces (new tabs, new endpoints, additive SQLite migrations); no existing public route or template was removed, and the M5 remediation work tightened maintenance-query behavior without changing the feature set.

---

## 3. Section B - Recommended next (high value, not built)

These are the candidates that scored well but were outside the implemented scope. They are ordered by recommended priority and require no change to the core FastAPI + SQLite + Calibre architecture.

| ID | Feature | What it does | User value | Complexity | Architectural fit | Risk | Priority |
| :--- | :--- | :--- | :---: | :---: | :---: | :---: | :--- |
| **RN-01** | Staging directory inspector | Read-only view of completed download files (EPUB archives, sizes, series/volume parse result) in the `/downloads` tab, so the user can confirm what the downloader produced before importing into Calibre. | High | Low-Medium | High | Low | **High** |
| **RN-02** | Timezone end-to-end completion | Expose an `app_timezone` setting in the Settings tab and apply the already-registered `localtime`/`localdate` filters to release, audit-log and series timestamp renderings. | Medium | Low | High | Very Low | **High** |
| **RN-03** | OPDS catalog server (`/opds`) | Publish the tracked library as an OPDS feed so e-reader apps on the same network can browse and download EPUBs directly. | Medium | Medium-High | High | Low | **Medium** |
| **RN-04** | Audiobookshelf / Kavita library sync | Push acquisition events or library state to a self-hosted companion server so reading progress and presence stay in sync across apps. | Medium | High | Medium | Medium | **Medium** |
| **RN-05** | Opt-in automatic download of new wanted releases | A per-feed or global opt-in toggle that dispatches newly `REQUESTED` releases to qBittorrent automatically, with guardrails (explicit opt-in, wanted-volumes-only filtering, per-series interval) instead of unattended acquisition of everything. | Medium | Medium | High | Medium | **Low-Medium** |

### 3.1 Rationale per recommended row

- **RN-01** - The downloader saves completed files to the configured save path, but the dashboard only lists torrents, not the resulting files, which forces the user back to the filesystem to verify a batch. This is the remaining slice of survey candidate F02 and adds no new network surface if implemented read-only.
- **RN-02** - The timezone utility and Jinja2 filters exist and are tested in isolation, but `settings.html` has no timezone field and the templates currently render date slices such as `pub_date[:10]` instead of piping through `localtime`. Wiring the two ends together completes F09 with a small, contained change.
- **RN-03** - OPDS is the standard way e-readers discover a self-hosted library and maps cleanly onto the existing Calibre scanner and cover cache. It was explicitly listed as a future roadmap item in the survey and does not need an external service.
- **RN-04** - Synchronizing with an audiobook or comics server is valuable for users who already run both stacks, but it introduces a second state model and credential lifecycle, so it is deferred until the OPDS/staging work is stable.
- **RN-05** - Every release currently has to be dispatched explicitly, which is deliberate but leaves the acquisition loop manual. An opt-in auto-download would close that loop, but only with guardrails, because an unattended false positive in title/volume matching becomes an unwanted multi-gigabyte download.

---

## 4. Section C - Backlog, evaluated and deferred or rejected

| ID | Feature | What it does | User value | Complexity | Architectural fit | Risk | Priority / reason |
| :--- | :--- | :--- | :---: | :---: | :---: | :---: | :--- |
| **B01** | EPUB OPF metadata injection (survey F12) | Rewrite `<dc:title>`, `<dc:creator>` and `calibre:series` metadata inside completed EPUB containers before Calibre import. | High | High | Medium | Medium | **Deferred / Backlog** - EPUB3 OPF and container complexities (mimetype ordering, manifest/itemref consistency, NCX/NAV and encryption.xml edge cases) make in-place edits risky; recommend a dedicated phase with a fuzzing harness and `ebook-convert`/`calibre-debug` round-trip validation. |
| **B02** | ComicInfo.xml tagging port | Copy MangaTrax's non-destructive `ComicInfo.xml` injector for CBZ archives. | Low for this app | Medium | Low | Medium | **Rejected** - NovelTrax is EPUB-only and deliberately rejects PDF releases; there is no CBZ workflow to tag, and the MangaTrax schema does not map to EPUB OPF. |
| **B03** | Intra-archive selective download | Deselect individual volumes inside a single `.zip`/`.cbz`/monolithic pack file. | Medium | High | Low | High | **Rejected** - BitTorrent file priorities operate at file granularity, not inside an archive; the implemented behavior correctly reports `single_archive` and downloads the whole file rather than attempting unsafe partial extraction. |
| **B04** | Read-only EPUB metadata verifier | Inspect completed EPUBs and report missing or mismatched `calibre:series`/volume metadata before import (no writes). | Medium-High | Medium | High | Low | **Backlog** - a safe precursor to B01; worth doing before any mutation work, but not selected for this cycle. |

### 4.1 Rationale per backlog row

- **B01** - The MangaTrax analogue (`ComicInfo.xml` appended to a ZIP) is simple because CBZ metadata is a sibling file; EPUB metadata lives in the OPF package that must stay consistent with the container manifest and spine. The survey already flagged EPUB metadata injection as the one candidate requiring dedicated fuzzing before implementation.
- **B02** - Porting the tagger would add a code path that can never run on this application's library format, so it is a maintenance liability rather than a feature.
- **B03** - The single-file case is a protocol limitation, not a missing parser: qBittorrent can only mark whole files as "do not download", and the implementation detects this case explicitly instead of silently skipping content.
- **B04** - Verification is the useful half of B01 and carries almost no risk because it never mutates the container; it can ship independently and de-risk the injection work later.

---

## 5. Summary and Requirement R2 Acceptance

| Group | Count | Status |
| :--- | :---: | :--- |
| Implemented survey candidates (F01-F11) | 11 | Delivered across M2-M4 plus M5 UI completion |
| Implemented supporting work items (inventory rows 1-11, 23-24) | 13 | Delivered across M1 and M5 |
| Recommended next (RN-01 - RN-05) | 5 | Not built; prioritized above |
| Backlog / deferred / rejected (B01 - B04) | 4 | Not built; reasons recorded above |

Requirement R2 asked for a documented list and evaluation of recommended feature additions, with selected high-priority additions implemented and verified. This document supplies the evaluation; `README.md` documents the resulting user-facing feature set; `SECURITY_AUDIT.md` records the security hardening performed on the same additions.

## 6. Verification Notes

- Implemented behavior cited here was checked against `backend/app/downloader/qbit.py`, `backend/app/providers/nyaa.py`, `backend/app/providers/registry.py`, `backend/app/calibre/scanner.py`, `backend/app/parser.py`, `backend/app/database.py`, `backend/app/providers/metadata.py`, `backend/app/timezone_util.py`, `backend/app/main.py` and the templates on 2026-11-02.
- Milestone attribution comes from `PROJECT.md` and `GATE_STATUS.md`; the code was not re-scored against the old survey matrix where the implementation changed scope.
- `M2` introduced the Calibre bundle parser, timezone utility and metadata cache; `M3` the downloader and dashboard; `M4` the Nyaa provider, prioritization and ignore workflow. `M5` is the verification milestone and added only the small UI completions (release Download action, Nyaa Add-Feed preset) plus hardening; it did not change the underlying feature set.
- `PROJECT.md` row 4 ("outbound network hardening across webhook, feed testing and scraper providers") is now accurate for the httpx paths: the M5 post-review batch added `safe_client_get`/`safe_client_request`, so J-Novels, NovelUpdates, custom HTML, metadata and Nyaa/RSS transports validate every redirect hop. The remaining `urllib` fallbacks in `jnovels.py` are recorded as RR-05 in `SECURITY_AUDIT.md`.

