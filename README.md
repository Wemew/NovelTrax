# NovelPulse 📚

**NovelPulse** is a lightweight, automated novel release monitor and wishlist manager tailored for Calibre library owners. It periodically monitors novel release websites (with out-of-the-box support for **J-Novels EPUB** releases at `https://jnovels.com/?s=epub`, **NovelUpdates**, and generic **RSS/HTML scrapers**), matches newly detected releases against your Calibre library, and manages an active release wishlist.

Runs seamlessly in **Docker**, mounts your Calibre library network share safely in **read-only zero-lock mode**, and features a modern, responsive web dashboard.

---

## ✨ Key Features

1. **Auto-Populates Series from Calibre**
   - Reads your Calibre library's `metadata.db` file.
   - Automatically imports all your series, total book counts, authors, and highest volume numbers owned.
   - Monitor status can be toggled on/off per series.

2. **Automatic Request Resolution**
   - When a monitored novel site publishes a new volume (e.g. *DanMachi Vol 19* or *Dungeon Crawler Carl Book 7*), NovelPulse detects that your Calibre library is missing that volume and automatically marks it as **`REQUESTED`**.
   - As soon as you add the book to your Calibre library, NovelPulse **automatically removes the request**, updates the book's status to **`IN_CALIBRE`**, and logs the resolution in the audit log.

3. **Zero-Lock Calibre Database Access**
   - **Never blocks Calibre writes**: Uses SQLite's `mode=ro&immutable=1` connection protocol.
   - SQLite bypasses all file-locking operations on SMB, CIFS, NFS, and local filesystems. Host Calibre, Calibre-Web, and network clients can write freely without locking conflicts or `database is locked` errors.

4. **Multi-Feed & Scraper Support**
   - **J-Novels EPUB Scraper**: Pre-configured for `https://jnovels.com/?s=epub` with WordPress RSS fallback.
   - **NovelUpdates / Light Novel Trackers**: Pre-built support for LN release listings.
   - **Standard RSS / Atom Feeds**: Ingests feeds from publishers, blogs, and release trackers.
   - **Custom HTML Scraper**: Point NovelPulse at any website and provide CSS selectors.
   - **Live Test Mode**: Test any feed extraction in real time directly from the web dashboard.

5. **Modern Web Dashboard**
   - Responsive, dark-mode dashboard built with Tailwind CSS and Lucide icons.
   - Real-time stats, filterable wishlist table, series manager, feed tester, and audit logs.
   - One-click manual triggers: "Sync Calibre Now" and "Check Feeds Now".

6. **Notification Webhooks**
   - Optional Discord embed cards and generic webhook / Apprise notifications when new releases are detected or acquired.

---

## 🚀 Quick Start with Docker

### 1. Configure `docker-compose.yml`

```yaml
version: '3.8'

services:
  novelpulse:
    build:
      context: .
      dockerfile: Dockerfile
    image: novelpulse:latest
    container_name: novelpulse
    restart: unless-stopped
    ports:
      - "8080:8080"
    environment:
      - PORT=8080
      - CALIBRE_DIR=/calibre
      - DATA_DIR=/app/data
      - TZ=UTC
    volumes:
      # 1. Mount your Calibre library folder or network share (READ-ONLY)
      - /path/to/your/calibre/library:/calibre:ro

      # 2. Mount local directory for persistent database, settings, and logs
      - ./data:/app/data
```

> **Note on Network Shares (SMB / CIFS / NFS):**  
> If your Calibre library lives on a NAS or Windows share (e.g., `\\192.168.1.100\calibre`), mount the share on your Docker host (e.g., at `/mnt/calibre`) and pass `/mnt/calibre:/calibre:ro` into the container.

### 2. Start the Container

```bash
docker compose up -d --build
```

Access the dashboard at:
👉 **`http://localhost:8080`** (or your server's IP).

---

## 🧭 Dashboard Walkthrough

| Tab | Purpose |
| --- | --- |
| **Overview** | Quick summary cards (Tracked Series, Wanted Requests, Acquired Books, Active Feeds), Calibre status banner, pending requests cards with direct download links, and recent activity logs. |
| **Series** | Displays all novel series auto-populated from Calibre. Shows total volumes owned, latest index, and toggle switches to enable/disable monitoring. |
| **Releases & Requests** | Filterable catalog of all detected releases. Filter by `Requested (Wanted)`, `In Calibre (Acquired)`, `Ignored`, or `All`. |
| **Feeds & Scrapers** | Manage configured feeds (J-Novels, RSS, NovelUpdates, Custom HTML Scraper). Includes live extraction preview ("Test Scraper") and per-feed check triggers. |
| **Logs** | Chronological audit trail of all background checks, sync runs, and auto-resolutions. |
| **Settings** | Calibre container path, sync/check intervals, connection diagnostics, and Discord notification webhooks. |

---

## 🛠️ How Series & Volume Matching Works

1. **Smart Title & Volume Extraction**:
   Novel releases frequently contain varying formats such as:
   - `DanMachi Volume 19 Epub` &rarr; Series: `DanMachi`, Vol: `19.0`
   - `Overlord Volume 16 [PDF+EPUB]` &rarr; Series: `Overlord`, Vol: `16.0`
   - `Classroom of the Elite: Year 2 Volume 12.5 Epub` &rarr; Series: `Classroom of the Elite: Year 2`, Vol: `12.5`
   - `Dungeon Crawler Carl: Book 7 - This Inevitable Ruin` &rarr; Series: `Dungeon Crawler Carl`, Vol: `7.0`, Subtitle: `This Inevitable Ruin`
   - Suffixes like `[EPUB]`, `[Light Novel]`, `Download`, and `PDF` are automatically cleansed.

2. **Matching Against Calibre**:
   - The scanner indexes your Calibre library by `(series_id, volume)` and normalized `clean_title`.
   - If the detected volume is already in Calibre &rarr; marked **`IN_CALIBRE`**.
   - If the series is monitored and the volume is missing &rarr; marked **`REQUESTED`**.
   - If you later add that volume into Calibre &rarr; the status flips to **`IN_CALIBRE`** and it is automatically cleared from your wanted list!

---

## 🧪 Testing Locally

To run the automated end-to-end integration test (which spins up a realistic mock Calibre SQLite database, tests zero-lock scanning, checks J-Novels simulated feeds, and tests auto-resolution):

```bash
python tests/test_full_workflow.py
```

---

## ⚙️ Configuration & Environment Variables

| Variable | Default | Description |
| --- | --- | --- |
| `PORT` | `8080` | Port for the web dashboard |
| `CALIBRE_DIR` | `/calibre` | Directory containing Calibre's `metadata.db` |
| `DATA_DIR` | `/app/data` | Directory where NovelPulse stores its database and logs |
| `TZ` | `UTC` | Container timezone |

All intervals (e.g. check feed every 60 min, sync Calibre every 15 min) and Discord webhooks can also be configured directly inside the **Settings** tab in the web UI.
