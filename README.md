# NovelTrax 📚

**NovelTrax** is a lightweight, automated novel release monitor and wishlist manager tailored for Calibre library owners. It periodically monitors novel release websites (with out-of-the-box support for **J-Novels EPUB** releases at `https://jnovels.com/?s=epub`, **NovelUpdates**, **Nyaa category 3_1** English-translated light novels, and generic **RSS/Atom or CSS-selector HTML scrapers**), matches newly detected releases against your Calibre library, and manages an active release wishlist. Optional qBittorrent integration lets you dispatch wanted releases to a downloader and monitor the queue from the dashboard.

This README documents the feature set as implemented in the current codebase. It covers the original release monitoring and wishlist workflow, plus the Calibre omnibus matching, Nyaa provider, prioritized release groups, one-click ignore/restore, qBittorrent downloader, metadata caching and UI improvements added since the previous revision. Features are only described here if they are present in `backend/app`; the security posture is documented separately in [`SECURITY_AUDIT.md`](SECURITY_AUDIT.md), and the feature evaluation that produced these additions is in [`FEATURE_RECOMMENDATIONS.md`](FEATURE_RECOMMENDATIONS.md).

Runs seamlessly in **Docker**, mounts your Calibre library network share safely in **read-only zero-lock mode**, and features a modern, responsive web dashboard.

---

## ✨ Key Features

1. **Auto-Populates Series from Calibre (with Omnibus Awareness)**
   - Reads your Calibre library's `metadata.db` file.
   - Automatically imports all your series, total book counts, authors, and owned volume numbers.
   - Parses **omnibus and bundle titles** inside Calibre — formats such as `Overlord Vol 1-3`, `Mushoku Tensei v01-02`, `Title [1-4]`, `Title Vol 1, 2, and 3` and bare ranges like `86 - 1-4` expand into complete owned-volume sets, so volumes contained in a bundle are never requested again.
   - Owned volumes are stored per series as `owned_volumes_json` and refreshed on every sync.
   - Monitor status can be toggled on/off per series.

2. **Automatic Request Resolution**
   - When a monitored novel site publishes a new volume (e.g. *DanMachi Vol 19* or *Dungeon Crawler Carl Book 7*), NovelTrax detects that your Calibre library is missing that volume and automatically marks it as **`REQUESTED`**.
   - As soon as you add the book to your Calibre library, NovelTrax **automatically removes the request**, updates the book's status to **`IN_CALIBRE`**, and logs the resolution in the audit log.

3. **Zero-Lock Calibre Database Access**
   - **Never blocks Calibre writes**: Uses SQLite's `mode=ro&immutable=1` connection protocol.
   - SQLite bypasses all file-locking operations on SMB, CIFS, NFS, and local filesystems. Host Calibre, Calibre-Web, and network clients can write freely without locking conflicts or `database is locked` errors.

4. **Multi-Feed & Scraper Support**
   - **J-Novels EPUB Scraper**: Pre-configured for `https://jnovels.com/?s=epub` with WordPress RSS fallback and Master Index ingestion.
   - **Nyaa Light Novels**: Zero-authentication crawler for Nyaa category `3_1` (Literature - English-translated), with RSS polling, paginated HTML crawling, magnet/`.torrent` link capture, seeder/leecher/size metadata and targeted per-series search. A `Nyaa Light Novels (3_1)` feed is seeded automatically on first start, and **Nyaa** is selectable as a preset when adding a new feed (it fills the category `3_1` RSS URL automatically).
   - **NovelUpdates / Light Novel Trackers**: Pre-built support for LN release listings.
   - **Standard RSS / Atom Feeds**: Ingests feeds from publishers, blogs, and release trackers.
   - **Custom HTML Scraper**: Point NovelTrax at any website and provide CSS selectors.
   - **Live Test Mode**: Test any feed extraction in real time directly from the web dashboard.

4b. **Light-Novels Only: Manga & Audiobook Separation**
   - Nyaa's Literature category is heavily polluted (roughly 75% of the live `3_1` feed is manga volumes, chapter compilations and dumps), so category filtering alone is not enough. NovelTrax classifies every release by media type.
   - **Manga is never stored.** Detection uses the release-title convention first — manga rips carry parenthesised scanlation metadata (`… v01-03 (2025-2026) (Digital) (1r0n, Anon)`, `(Digital-Compilation)`, `as v01-02`, `Chapter 222`, `[JPEG-XL]`), while light novels carry bracketed publisher/format tags (`… v01-04 [Seven Seas] [Stick]`, `… v01-17 [J-Novel Club] [CleanBookGuy]`, `[EPUB]`).
   - **Ambiguous titles are verified against the torrent itself.** The `.torrent` metainfo is fetched (~40 KB, cached per release, rate-limited, bounded per scan) and its file list decides: `.epub`/`.azw3`/`.mobi` = novel, `.cbz`/`.cbr`/images = manga, `.m4b`/`.mp3` = audiobook. A release whose format cannot be determined is kept as *unverified* rather than silently dropped, and retried on a later scan.
   - **Audiobooks get their own section** at `/audiobooks`. They are stored (tagged `[Audiobook]`, `[M4B]`, `[Seven Seas Siren]`, or detected from audio files) but never appear in Releases, Discovery or the wishlist.
   - Existing rows are re-typed once on startup — audiobook rows move to the Audiobooks section and manga rows are hidden from the tracking views. **Nothing is deleted**, rows already classified (including Calibre matches) are left alone, and the pass is skipped entirely when `filter_non_novels` is `false`.

5. **Prioritized Release Groups**
   - Maintains a `prioritized_groups` list of trusted release groups and publishers, managed from the **Prioritized** tab.
   - Seeded with `Yen Press`, `J-Novel Club`, `Seven Seas`, `Stick` and `One Peace Books`, and editable at any time (add, note, remove).
   - Release-group names are extracted from bracketed and hyphenated release titles, with format/edition buzzwords (`[EPUB]`, `[Digital]`, `[1080p]`, `Blu-Ray`, `Web-DL`, audio tags and similar) excluded so they are not mistaken for groups.
   - Prioritized releases carry a VIP badge, and the **Wanted / Missing First** sort places prioritized wanted series ahead of other wanted series, so official digital rips surface first when you are choosing what to grab.

6. **One-Click Ignore / Restore**
   - Any release can be marked **`IGNORED`** with one click, removing it from the wanted view without blacklisting the whole series.
   - Ignored releases remain visible under the **Ignored** filter pill and can be restored to **`REQUESTED`** with one click.
   - Ignore/restore actions are recorded in the audit log.

6b. **Chapter / Volume Disambiguation**
   - A number is only treated as a **volume** when the title carries an explicit volume keyword (`Vol`, `Vol.`, `Volume`, `Vols`, `V.`, `Book`, `Part`) or a multi-volume range (`Vols 1-3`, `1-4`).
   - A bare trailing number (`Dandadan 250`) or an explicit chapter marker (`Chapter 179`, `Ch. 179`, `c374`) is treated as a **chapter** and is dropped at ingestion, so chapter feeds never pollute Releases, Discovery or the wishlist.
   - Existing chapter rows stored by earlier versions (status `NEW` or `REQUESTED`) are purged once on startup; rows matched to your Calibre library (`IN_CALIBRE`) and your own ignore/blacklist decisions are never touched.

7. **qBittorrent Downloader & Selective Volume Downloading**
   - Optional in-app **qBittorrent Web API v2** client (compatible with qBittorrent, Flood, TorBox and Real-Debrid qBit emulators), configurable entirely from the **Downloads** tab.
   - Every release row and series-card release exposes a one-click **Download** action that dispatches the release's magnet or `.torrent` URL to qBittorrent with the configured category, tag and save path. Rows without a torrent/magnet URL show a disabled button with an explanatory tooltip.
   - The action is backed by `POST /api/releases/{release_id}/download`, which resolves the download URL from the release record and sends it to the configured downloader.
   - **Selective volume downloading**: when a sent release is a multi-file bundle torrent, NovelTrax reads the torrent file list and sets `filePrio=0` ("Do Not Download") on files whose volumes are all already owned in Calibre, downloading only the missing volumes. The same filter can be re-applied to a queued torrent through `POST /api/downloads/{torrent_hash}/selective`.
   - Omnibus/range filenames and comma/`and` volume lists (`Vol 1, 2, and 3`) are parsed before selection; a file is deselected only when *all* of its volumes are owned, so a partially missing bundle is never skipped.
   - Single-file archives cannot be split at the BitTorrent protocol layer and are downloaded whole as a safe fallback; unparseable book files are always kept.
   - The **Downloads** tab shows downloader connection status, live queue progress and speed, and pause/resume/delete controls.

8. **Targeted Series Backfill**
   - The per-series **Backfill** action runs an on-demand background search for one specific series across Nyaa instead of re-scanning every feed.
   - `POST /api/series/{id}/backfill` and `POST /api/series/backfill` accept the series identity and a page ceiling; polite delays and a stop check keep the crawl rate-limited.
   - Global backfill remains available for a full historical sweep.

9. **Modern Dashboard with Search, Sorting and Lazy Loading**
   - Responsive, dark-mode dashboard built with Tailwind CSS and Lucide icons.
   - Releases can be viewed as **grouped series cards** or a **flat release table**, filtered by `Requested (Wanted)`, `In Calibre (Acquired)`, `Ignored` or `All`.
   - Sort options include release date, title, series name and wanted-first ordering, with different option sets for the grouped and flat views.
   - Instant client-side search filters on title/series as you type, with server-side search applied when a query is submitted and carried through pagination.
   - Progressive lazy loading (`/api/releases/cards`, `/api/releases/flat_rows`) with load-more buttons and reusable Jinja partials; the Prioritized view additionally auto-loads on scroll.
   - A global top progress bar and button spinners give instant feedback for navigation and long-running actions.
   - One-click manual triggers: **"Sync Calibre"** / **"Sync From Calibre Now"** and **"Check Feeds Now"**.

10. **Metadata Cache & Rate Limiting**
    - Kitsu and AniList metadata (canonical title, synopsis, cover art) is cached persistently in the `novel_metadata_cache` SQLite table, so repeated page views and re-scans do not re-query external APIs.
    - Thread-safe sliding-window rate limiters pace AniList (90 requests / 60 s) and Kitsu (30 requests / 60 s), honor `X-RateLimit-Remaining` and `Retry-After`, and fail soft when a provider is unavailable.

11. **Timezone Support**
    - `backend/app/timezone_util.py` converts UTC timestamps to a configured timezone with automatic daylight-saving transitions.
    - Registered as the `localtime` and `localdate` Jinja2 filters and resolved from the `app_timezone` setting or the `TZ` environment variable, defaulting to `Europe/Copenhagen` when neither is valid.
    - Conversion falls back gracefully on missing, partial or malformed timestamps instead of raising.

12. **Direct Series Website Links & Hubs**
    - Automatically maintains a direct link to each novel's series hub on the source website (e.g. `https://jnovels.com/?s=DanMachi` or category/tag page).
    - Pending request cards on the Overview page provide **"Release"** (opens the specific book post) and **"Series Hub"** (opens all releases for that series) buttons; release rows link to their feed post and series page.
    - Series links are editable in the UI so you can point them to exact custom category/tag URLs if desired.

13. **New Series Discovery & Editable Blacklist Panel**
    - **Discovery Hub**: Shows newly detected Volume 1 and unnumbered/standalone releases that aren't yet in your Calibre library, with search, sort and pagination.
    - **1-Click Track**: Instantly converts a discovered series into an actively monitored series and adds it to your wishlist.
    - **1-Click Blacklist**: Permanently stops future volume releases from generating alerts or requests, and automatically purges existing requests for that series.
    - **Editable Blacklist**: Full management table where you can view, proactively add series names, add notes/reasons, or restore/unblock series at any time.

14. **Notification Webhooks**
    - Optional Discord embed cards and generic webhook / Apprise notifications when new releases are detected or acquired, with in-app test buttons for each target.

15. **Maintenance & Cleanup Tools**
    - Settings-tab tools to clear stored descriptions, purge unreviewed Discovery series, clear the cached cover-image store, and purge PDF releases.
    - NovelTrax strictly tracks EPUB releases: PDF releases are rejected at ingestion and can be purged from existing data.

---

## 🚀 Quick Start with Docker

### 1. Configure `docker-compose.yml`

```yaml
version: '3.8'

services:
  noveltrax:
    build:
      context: .
      dockerfile: Dockerfile
    image: noveltrax:latest
    container_name: noveltrax
    restart: unless-stopped
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    ports:
      - "8080:8080"
    environment:
      - PORT=8080
      - CALIBRE_DIR=/calibre
      - DATA_DIR=/app/data
      - TZ=UTC
      # Optional: default save path used when sending torrents to qBittorrent
      # - DOWNLOAD_DIR=/downloads
    volumes:
      # 1. Mount your Calibre library folder or network share (READ-ONLY)
      - /path/to/your/calibre/library:/calibre:ro

      # 2. Mount local directory for persistent database, settings, and logs
      - ./data:/app/data
```

> **Note on Network Shares (SMB / CIFS / NFS):**  
> If your Calibre library lives on a NAS or Windows share (e.g., `\\192.168.1.100\calibre`), mount the share on your Docker host (e.g. at `/mnt/calibre`) and pass `/mnt/calibre:/calibre:ro` into the container.

### 2. Start the Container

```bash
docker compose up -d --build
```

Access the dashboard at:
👉 **`http://localhost:8080`** (or your server's IP).

---

### 🐬 TrueNAS SCALE Stack Setup (UID / GID 568)

If deploying to **TrueNAS SCALE** (Electric Eel, Dragonfish, or via Portainer / Dockge):
1. TrueNAS executes container apps under the unprivileged `apps` user (`UID 568`, `GID 568`).
2. You can build and deploy directly from GitHub without cloning first by pasting the following into your TrueNAS **Custom App** compose editor (the repository also ships `docker-compose.truenas.yml`):

```yaml
services:
  noveltrax:
    build:
      context: https://github.com/Wemew/NovelTrax.git#main
      dockerfile: Dockerfile
    image: noveltrax:latest
    container_name: noveltrax
    restart: unless-stopped
    user: "568:568"
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    ports:
      - "8080:8080"
    environment:
      - PORT=8080
      - CALIBRE_DIR=/calibre
      - DATA_DIR=/app/data
      - TZ=Etc/UTC
    volumes:
      # Replace with your Calibre dataset host path:
      - /mnt/tank/media/calibre:/calibre:ro
      # Replace with your app data dataset host path:
      - /mnt/tank/apps/noveltrax/data:/app/data
```

---

## 🧭 Dashboard Walkthrough

| Tab | Purpose |
| --- | --- |
| **Overview** | Quick summary cards (Tracked Series, Wanted Requests, Acquired Books, Active Feeds), Calibre status banner, pending request cards with **Release** and **Series Hub** links, and recent activity logs. |
| **Series** | Displays all novel series auto-populated from Calibre. Shows owned volumes (including omnibus-expanded volumes), latest index, and toggle switches to enable/disable monitoring. |
| **Releases & Requests** | Searchable, sortable catalog of all detected releases in grouped or flat view. Filter by `Requested (Wanted)`, `In Calibre (Acquired)`, `Ignored`, or `All`; ignore/restore a release, dispatch it to qBittorrent with one click, run a per-series backfill, or open its release/hub links. |
| **Prioritized** | Releases from trusted release groups, with VIP badges, grouped/flat view switcher, group management (add/remove/note) and auto-loading on scroll. |
| **Downloads** | qBittorrent connection status and in-app configuration (custom URL, host, port, username, password, HTTPS toggle, category, save path), live torrent queue with progress/speed, and pause/resume/delete controls. |
| **Discovery** | New series discovery panel (Volume 1 and unnumbered/standalone releases) with 1-click **Track** or **Blacklist** buttons, search/sort, plus an editable **Blacklist Management** table. |
| **Feeds & Scrapers** | Manage configured feeds (the seeded J-Novels and Nyaa Light Novels feeds, plus RSS, NovelUpdates and Custom HTML Scraper entries; Nyaa is also available as a preset in the Add-Feed dropdown). Includes live extraction preview ("Test Scraper") and per-feed check triggers. |
| **Logs** | Chronological audit trail of all background checks, sync runs, downloader actions, ignore/restore actions and auto-resolutions. |
| **Settings** | Calibre container path, sync/check intervals, backfill limits and delays, official-translation-only toggle, Discord/generic webhooks, and maintenance tools. The qBittorrent settings live in the **Downloads** tab. |

---

## 🛠️ How Series & Volume Matching Works

1. **Smart Title & Volume Extraction**:
   Novel releases frequently contain varying formats such as:
   - `DanMachi Volume 19 Epub` &rarr; Series: `DanMachi`, Vol: `19.0`
   - `Overlord Volume 16 [PDF+EPUB]` &rarr; Series: `Overlord`, Vol: `16.0`
   - `Classroom of the Elite: Year 2 Volume 12.5 Epub` &rarr; Series: `Classroom of the Elite: Year 2`, Vol: `12.5`
   - `Dungeon Crawler Carl: Book 7 - This Inevitable Ruin` &rarr; Series: `Dungeon Crawler Carl`, Vol: `7.0`, Subtitle: `This Inevitable Ruin`
   - `DanMachi Vol 1-3 [EPUB]` &rarr; bundle spanning volumes `{1.0, 2.0, 3.0}`
   - Suffixes like `[EPUB]`, `[Light Novel]`, `Download`, and `PDF` are automatically cleansed.

2. **Omnibus Expansion Against Calibre**:
   - Calibre book titles are parsed for bundled ranges and lists (`Vol 1-3`, `v01-02`, `[1-4]`, `Vol 1, 2, and 3`) and expanded into every owned volume they contain.
   - `series_tracking.owned_volumes_json` therefore reflects what you actually own, even when Calibre stores an omnibus as a single book with `series_index = 1.0`.
   - A request is only created when the detected volume is absent from that owned set.

3. **Matching Against Calibre**:
   - The scanner indexes your Calibre library by `(series_id, volume)` and normalized `clean_title`.
   - If the detected volume is already in Calibre &rarr; marked **`IN_CALIBRE`**.
   - If the series is monitored and the volume is missing &rarr; marked **`REQUESTED`**.
   - If you later add that volume into Calibre &rarr; the status flips to **`IN_CALIBRE`** and it is automatically cleared from your wanted list!

4. **Downloading What Is Actually Missing**:
   - Sending a bundle release to qBittorrent (`POST /api/releases/{release_id}/download`) inspects the torrent's file list and deselects files whose volumes are all already owned in Calibre.
   - Example: a `DanMachi Vol 1-18` pack when Calibre owns volumes 1-15 downloads only files for volumes 16-18.
   - Monolithic single-file archives are downloaded whole (the BitTorrent protocol cannot deselect inside one file), and unparseable book files are kept rather than risk missing a volume.

5. **Consolidated Detection Rules** (shared with MangaTrax — see `AI-Workspace/RELEASE_DETECTION_SPEC.md`):
   - **Chapter-range → volume mapping**: `Reading Our Way … 001-009 as v01-02 + 010-012 (Digital-Compilation) (Oak)` resolves to series `Reading Our Way to a Happy New World`, volumes `[1, 2]`, chapter data `[[1,9],[10,12]]`, group `Oak`. Chapter numbers never become volumes, and the whole clause is removed from the Calibre matching key.
   - **Context-guarded chapter discard**: an explicit `Ch.`/`c` marker is always a chapter; a *bare* trailing number is treated as a chapter only when the base series is already known (Calibre library or tracked series). Otherwise the release is kept — with the number stored as chapter data, not as a volume — so the first instalment of a series you have never seen is no longer lost. Toggle with the `discard_unmarked_chapters` setting; drops are recorded as `CHAPTER_DISCARDED` activity events.
   - **Group sets instead of one group**: a joint release `(1r0n, Ushi)`, `(1r0n & Ushi)` or `[1r0n] [Ushi]` stores both groups (`release_groups_json`), keeps the primary for display, and prioritized-group matching is a set-membership test — configuring either group is enough to prioritise the release.
   - **Publishers are not groups**: `(Square Enix)`, `(K-MANGA)`, `[Yen Press]` are recorded in the separate `publisher` field and never occupy the release-group slot; a fallback prefers the last valid bracketed candidate.
   - **Revision markers**: `(f)`, `(f2)`, `(F)` parse to `revision` / `is_rerelease` (never a group); a higher revision supersedes on re-ingest, making re-releases visible as upgrade candidates.
   - **Status precedence**: automated feed re-polls can no longer downgrade a deliberate status — `REQUESTED` / `IN_CALIBRE` are never replaced by a classification, and `IGNORED` / `BLACKLISTED` can only be lifted by an explicit action.
   - **Stable identity key**: the stored `series_name` is the matching key and is never rewritten to a metadata-provider title; canonical English is used for display only. A non-destructive startup repair re-keys rows saved under a provider alias back to the tracked name and skips (never merges) ambiguous aliases.

---

## 🧪 Testing Locally

The automated suites run under the Calibre bundled Python interpreter, which provides Calibre's native SQLite semantics. The primary integration suites are:

```powershell
# Baseline end-to-end workflow suite (mock Calibre DB, zero-lock scanning,
# J-Novels feed simulation, matching and request auto-resolution)
& "C:\Program Files\Calibre2\calibre-debug.exe" -e tests\test_full_workflow.py

# Comprehensive end-to-end suite (Tiers 1-4 security, features, boundaries and workflows)
& "C:\Program Files\Calibre2\calibre-debug.exe" -e tests\test_e2e_suite.py
```

Additional targeted suites exist in `tests/` for security, downloader, Nyaa, omnibus/timezone, Tier 5 adversarial coverage, and UI wiring (including a stdlib-only static guard that balances Jinja blocks and checks template wiring, since the Calibre interpreter has no Jinja2); `TEST_READY.md` describes the end-to-end suite layout and commands.

### Whole-suite runner (assert-preserving)

Calibre's interpreter runs with `sys.flags.optimize == 2`, which compiles bare `assert` statements away — a suite can print `ALL TESTS PASSED` while checking nothing. `tests/run_tests.py` exists so that can never be relied on:

```powershell
# Every suite, recompiled at optimize=0 so assertions are live
& "C:\Program Files\Calibre2\calibre-debug.exe" -e tests\run_tests.py

# Substring filters / verbose output (options go after calibre-debug's own --)
& "C:\Program Files\Calibre2\calibre-debug.exe" -e tests\run_tests.py -- chapter media
& "C:\Program Files\Calibre2\calibre-debug.exe" -e tests\run_tests.py -- --verbose
```

It prints the interpreter's optimisation level on every run, recompiles each suite with `optimize=0`, counts both compiled and actually-executed assertions, isolates suites from each other, reports failing `file:line`/statement with a full traceback, and exits non-zero on failure. The project's own suites contain no bare asserts (enforced by `tests/test_assert_hygiene.py`); the runner is what proves that, and would catch the next bare assert that slips in.

---

## ⚙️ Configuration & Environment Variables

### Container environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `PORT` | `8080` | Port for the web dashboard |
| `CALIBRE_DIR` | `/calibre` | Directory containing Calibre's `metadata.db` |
| `DATA_DIR` | `/app/data` | Directory where NovelTrax stores its database, cached covers and logs |
| `TZ` | `UTC` (compose) | Container timezone; used by the timezone utility when no `app_timezone` setting is present. Defaults to `Europe/Copenhagen` if unset or invalid |
| `DOWNLOAD_DIR` | `/downloads` | Default qBittorrent save path seeded into settings |
| `QBIT_URL` | `http://localhost:8080` | Default qBittorrent Web API base URL seeded into settings |
| `QBIT_HOST` | `localhost` | Default qBittorrent host seeded into settings |
| `QBIT_PORT` | `8080` | Default qBittorrent Web API port seeded into settings |
| `QBIT_USERNAME` | `admin` | Default qBittorrent Web API username seeded into settings |
| `QBIT_PASSWORD` | empty | Default qBittorrent Web API password seeded into settings (stored in the application database) |
| `QBIT_USE_SSL` | `false` | Default HTTPS toggle for the qBittorrent connection |
| `QBIT_CATEGORY` | `novels` | Default qBittorrent category |

These qBittorrent environment variables only seed the database on first start; the values are editable afterwards in the **Downloads** tab.

### In-app settings (Settings tab)

| Setting | Description |
| --- | --- |
| Calibre library path in container | Path containing `metadata.db`; has a "Test Connection" button |
| Calibre sync interval | How often to re-scan Calibre and auto-clear acquired requests |
| Feed check interval | How often to poll J-Novels, Nyaa and other configured feeds |
| Gentle backfill max pages | Page ceiling for historical backfill (`0` means unlimited, stopping at end of feed) |
| Backfill request delay | Polite delay between backfill network requests |
| Track official translations only | When enabled, unofficial web-novel/fan-translation releases are excluded from wanted coverage |
| Discord webhook URL | Optional rich-embed notifications, with a live test button |
| Generic webhook / Apprise endpoint | Optional JSON event notifications (Gotify, Ntfy, Home Assistant, Apprise), with a live test button |
| Maintenance tools | Clear descriptions, clear discovered series, clear image cache, purge PDF releases |

### qBittorrent downloader settings (Downloads tab)

The downloader is entirely optional and is configured from the **Downloads** tab, not the Settings tab. It is enabled by default but degrades gracefully when unreachable: pages render with a "Disconnected / Needs In-App Setup" status and no download is attempted.

| Setting | Default | Description |
| --- | --- | --- |
| `qbit_url` | `http://localhost:8080` | Custom Web API base URL. When set it is used as the connection address; clear it to connect via the host/port fields below |
| `qbit_host` | `localhost` | qBittorrent hostname or IP, used when no custom base URL is configured |
| `qbit_port` | `8080` | qBittorrent Web API port, used when no custom base URL is configured |
| `qbit_username` | `admin` | Web API username |
| `qbit_password` | empty | Web API password (stored in the application SQLite settings database) |
| `qbit_use_ssl` | `false` | Use HTTPS when talking to qBittorrent |
| `qbit_enabled` | `true` | Master toggle for the downloader |
| `qbit_category` | `novels` | qBittorrent category assigned to added torrents (also filters the displayed queue) |
| `qbit_tags` | `noveltrax` | Tag applied to torrents added by NovelTrax |
| `qbit_savepath` | `/downloads` | Save path passed to qBittorrent; falls back to `DOWNLOAD_DIR` then `/downloads` |

A "Test Connection" action reports the qBittorrent version/status for the currently saved settings. Saving the configuration validates the destination and re-tests the connection; the downloader host may be loopback or a private LAN address, but cloud-metadata and non-HTTP(S) targets are rejected.

### Prioritized release groups

Trusted groups are managed from the **Prioritized** tab rather than an environment variable:

- Add a group by name with an optional note; remove it at any time.
- The seeded defaults are `Yen Press`, `J-Novel Club`, `Seven Seas`, `Stick` and `One Peace Books`.
- Matching is case-insensitive and trims whitespace; adding an existing group updates its note instead of duplicating it.
- Releases whose extracted group matches the list receive the VIP badge, and the **Wanted / Missing First** sort ranks prioritized wanted series ahead of other wanted series.

All intervals, webhook targets, maintenance tools, downloader credentials and prioritized groups can be configured directly inside the web UI.

---

## 🔒 Security Note

NovelTrax is designed as a single-user, self-hosted application with **no built-in authentication layer**. Run it behind an authenticating reverse proxy (Authelia, Authentik, Cloudflare Access) or restrict the port to your trusted LAN/VPN, and keep the dashboard off the public Internet. Outbound targets for the RSS/Atom, Nyaa, webhook, downloader, HTML-scraper and metadata paths are validated against SSRF and local-file-inclusion attempts — including every HTTP redirect hop — and the dashboard sends CSP, Permissions-Policy and related hardening headers. See [`SECURITY_AUDIT.md`](SECURITY_AUDIT.md) for the full findings, remediations and accepted residual risks (including the `urllib` fallback and template-rendering coverage notes).
