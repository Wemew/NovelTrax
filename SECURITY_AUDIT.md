# Application and Network Security Audit & Hardening Report

**Project**: NovelTrax (Calibre Light Novel Acquisition & Wishlist Manager)  
**Date**: November 1, 2026 (revision 3: November 2, 2026)  
**Auditor**: Teamwork Security Implementer & QA Subagent  
**Scope**: Application code (`backend/app`), REST endpoints, frontend templates, dependency footprint, and network communications  
**Standard**: OWASP Top 10 / CWE Standard Risk Assessment  

This report records the application and network security assessment of NovelTrax required by Requirement R3. Revision 1 covered the baseline web, feed and Calibre integrations and the ten findings SEC-01 to SEC-10. Revision 2 preserved those findings and added the hardening performed while delivering milestones 2-5: the downloader and Nyaa provider surfaces, the expanded SSRF guard, template escaping and header verification, and the null-byte/control-character URL rejection found by the Milestone 5 ground-truth test run. Revision 3 adds the final post-review batch: SSRF-via-redirect hardening across every scraper and metadata transport, two URL parser-confusion input-validation fixes, the Calibre immutable-read retry, and the previously latent flat-release-view template defect, together with updated residual-risk and coverage-gap records.

---

## 1. Executive Summary

A comprehensive application and network security assessment was conducted on NovelTrax in accordance with Requirement R3 of the project specification. The review identified 10 baseline security vulnerabilities and architectural concerns spanning Server-Side Request Forgery (SSRF), Local File Inclusion (LFI), DOM-based Cross-Site Scripting (XSS), information disclosure oracles, path traversal, and missing security headers.

All identified vulnerabilities with high and medium severities were directly remediated in the codebase and are exercised by the dedicated security, adversarial and end-to-end test suites. In addition, the dependency footprint was reduced from 9 packages to 7 without altering core application architecture or public interfaces. Final suite counts are recorded in section 7 by the project lead because the test suite was still being hardened when this revision was written.

Revision 2 adds eight further findings (SEC-11 to SEC-18) raised or re-verified while implementing milestones 2-5. The most significant are the hardened SSRF guard (IPv6-mapped IPv4, CGNAT, cloud metadata and fail-closed DNS handling), SSRF validation for the newly introduced qBittorrent destination settings, and the Nyaa provider's feed ingestion routing through the safe HTTP client. The remaining items re-verify M1 controls against the M2-M5 code and record the newly found null-byte/control-character URL rejection. After revision 2 the tracked set was 18 findings.

Revision 3 records five post-review findings (SEC-19 to SEC-23) raised while completing the Milestone 5 ground-truth and UI-wiring work. SEC-19 is a security fix: automatic HTTP redirect following in the scraper transports allowed a validated public URL to redirect into a private or cloud-metadata destination, and a shared redirect-validating helper now closes that gap across the J-Novels, NovelUpdates, custom HTML and metadata transports. SEC-20 and SEC-21 are URL parser-confusion input-validation defects (percent-encoded NUL and userinfo `@`) that could obfuscate the SSRF filter; both now fail closed. SEC-22 and SEC-23 are release-quality defects rather than security vulnerabilities: a Calibre immutable-read torn-snapshot failure that could abort a scan while Calibre was writing, and an unbalanced Jinja block that made the flat release view (`/releases?view=flat` and `GET /api/releases/flat_rows`) unrenderable. With these additions the tracked set is 23 findings; all security findings have an implemented remediation, and the two quality defects are recorded for traceability.

---

## 2. Security Findings Matrix

| Finding ID | Vulnerability | Severity | CWE | Impacted Component | Remediation Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **SEC-01** | Server-Side Request Forgery (SSRF) | **High** | CWE-918 | `app/notifier/webhook.py`<br>`app/main.py` (`/api/feeds/test`)<br>`app/providers/` | **FIXED** (Centralized `app.security.is_safe_public_url` blocking private IPs, loopback, CGNAT, and metadata) |
| **SEC-02** | Local File Inclusion (LFI) via Feedparser | **High** | CWE-73 | `app/providers/rss.py`<br>`app/providers/jnovels.py` | **FIXED** (Pre-fetch content via `safe_http_get` before in-memory `feedparser.parse`) |
| **SEC-03** | Webhook Diagnostic Response Leakage Oracle | **Medium** | CWE-209 | `app/notifier/webhook.py` (`test_webhook`) | **FIXED** (Removed raw response body echoes `resp.text[:200]`, sanitizing diagnostic output) |
| **SEC-04** | DOM-Based Cross-Site Scripting (XSS) | **High** | CWE-79 | `templates/discovery.html`<br>`templates/feeds.html` | **FIXED** (Replaced unescaped `.outerHTML` and template literals with safe DOM element creation, `textContent`, `escapeHtml`, and `sanitizeLink`) |
| **SEC-05** | Missing CSP & Permissions-Policy Headers | **Medium** | CWE-693 | `app/main.py` (`add_security_headers`) | **FIXED** (Injected strict `Content-Security-Policy` and `Permissions-Policy` in HTTP middleware) |
| **SEC-06** | Calibre Path Traversal & Probing Oracle | **Medium** | CWE-22 | `app/main.py` (`/api/calibre/test`) | **FIXED** (Path canonicalization, null-byte rejection, and forbidden file extension filtering in `sanitize_calibre_path`) |
| **SEC-07** | Broken Access Control / Unauthenticated Endpoints | **Medium** | CWE-306 | `app/main.py` | **DOCUMENTED** (Local container network architecture; access control token blueprint documented) |
| **SEC-08** | Uncontrolled Resource Consumption (DoS) | **Low** | CWE-400 | `app/security.py` (`safe_http_get`) | **FIXED** (Enforced 10 MB payload limits and redirect ceilings) |
| **SEC-09** | Information Disclosure in Error Handlers | **Low** | CWE-209 | `app/notifier/webhook.py` | **FIXED** (Sanitized error strings in webhook diagnostics) |
| **SEC-10** | Missing Subresource Integrity (SRI) on CDNs | **Low** | CWE-353 | `templates/base.html` | **MITIGATED** (Strict CSP `script-src` restricts script execution to trusted CDN origins `cdn.tailwindcss.com` and `unpkg.com`) |
| **SEC-11** | SSRF guard bypass classes (IPv6-mapped IPv4, CGNAT, cloud metadata, DNS failures) | **High** | CWE-918 | `app/security.py`<br>`app/providers/` | **FIXED** (Expanded network/address checks, IPv4-mapped recursion, explicit metadata hostnames, fail-closed DNS) |
| **SEC-12** | qBittorrent destination SSRF / protocol abuse | **High** | CWE-918 | `app/downloader/qbit.py`<br>`app/main.py` (`/api/downloader/configure`) | **FIXED** (`validate_downloader_destination` blocks metadata endpoints and non-HTTP(S) schemes while permitting LAN/loopback downloader hosts) |
| **SEC-13** | Feed ingestion LFI on the Nyaa / J-Novels RSS surfaces | **High** | CWE-73 | `app/providers/nyaa.py`, `rss.py`, `jnovels.py` | **FIXED** (All `feedparser.parse` calls operate on bodies fetched by `safe_http_get`) |
| **SEC-14** | Webhook diagnostic sanitization verification | **Medium** | CWE-209 | `app/notifier/webhook.py` | **FIXED** (Status-code-only diagnostics; response bodies never reflected) |
| **SEC-15** | DOM XSS escaping verification in discovery and feed testing | **High** | CWE-79 | `templates/discovery.html`<br>`templates/feeds.html` | **FIXED** (`textContent`/`createElement` sinks; `escapeHtml` and `sanitizeLink` on every interpolated field) |
| **SEC-16** | CSP / Permissions-Policy coverage verification | **Medium** | CWE-693 | `app/main.py` (`add_security_headers`) | **FIXED** (CSP and Permissions-Policy present on all responses; `unsafe-inline` accepted for inline templates and documented) |
| **SEC-17** | Calibre path-traversal validation verification | **Medium** | CWE-22 | `app/main.py` (`/api/calibre/test`) | **FIXED** (`sanitize_calibre_path` rejects null bytes, normalizes, blocks sensitive extensions, returns HTTP 400) |
| **SEC-18** | Null-byte / control-character URL acceptance | **Low** | CWE-20 | `app/security.py` (`is_safe_public_url`) | **FIXED** (NUL and C0 control characters rejected before URL parsing; defense-in-depth input validation, not an SSRF bypass) |
| **SEC-19** | SSRF via HTTP redirect hop | **High** | CWE-918 | `app/security.py` (`safe_client_get` / `safe_client_request`)<br>`app/providers/` | **FIXED** (Initial URL and every `urljoin`-resolved `Location` hop validated; `follow_redirects=False` plus a 5-hop cap on all scraper/metadata transports) |
| **SEC-20** | Percent-encoded NUL / control-character URL obfuscation | **Medium** | CWE-20 | `app/security.py` (`is_safe_public_url`) | **FIXED** (Percent-encoded C0/DEL escapes rejected before URL parsing, fail closed) |
| **SEC-21** | Userinfo `@` parser-confusion URL acceptance | **Medium** | CWE-20 | `app/security.py` (`is_safe_public_url`) | **FIXED** (Ambiguous `@` outside the URL authority rejected, fail closed) |
| **SEC-22** | Calibre immutable-read torn snapshot (`database disk image is malformed`) | **Medium** (quality/reliability) | CWE-362 | `app/calibre/scanner.py` (`_run_read`) | **FIXED** (Single retry with plain `mode=ro` on `sqlite3.DatabaseError`, then fail closed with a logged warning; immutable fast path remains the default) |
| **SEC-23** | Flat release view `TemplateSyntaxError` (unbalanced Jinja block) | **High** (release quality) | — (template integrity defect) | `templates/partials/flat_release_row_chunk.html`<br>`GET /api/releases/flat_rows` | **FIXED** (Missing `{% endif %}` restored; static Jinja block/delimiter balance guard added in `tests/test_ui_wiring.py`) |

---

## 3. Detailed Findings, Attack Vectors & Remediations (Baseline, SEC-01 to SEC-10)

### SEC-01: Server-Side Request Forgery (SSRF) in Outbound Clients
- **Severity**: High | **CVSS 3.1**: 8.6 (`AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:N/A:N`) | **CWE-918**
- **Description**: 
  Prior to remediation, outbound HTTP requests initiated by the webhook notification service, feed test endpoints (`/api/feeds/test`), and scraper providers did not restrict destinations. An attacker could supply internal or loopback IP addresses (e.g. `127.0.0.1`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), Carrier-Grade NAT addresses (`100.64.0.0/10`), or cloud instance metadata endpoints (`http://169.254.169.254/latest/meta-data/`) to probe network infrastructure or extract instance tokens.
- **Remediation**:
  Created `backend/app/security.py` featuring `is_safe_public_url(url: str) -> bool`.
  1. Validates scheme: Only `http` and `https` protocols are allowed.
  2. Resolves destination hostname to all candidate IP addresses using `socket.getaddrinfo`.
  3. Rejects loopback (`127.0.0.0/8`, `::1`), RFC 1918 private subnets, Carrier-Grade NAT (`100.64.0.0/10`), Link-Local / Cloud Metadata (`169.254.0.0/16`, `169.254.169.254`), multicast, and reserved addresses.
  4. Enforced in `app/notifier/webhook.py`, `app/main.py` (`/api/feeds/test`, `/api/feeds`), and `app/providers/rss.py`.

### SEC-02: Local File Inclusion (LFI) via Feedparser
- **Severity**: High | **CVSS 3.1**: 8.2 (`AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N`) | **CWE-73**
- **Description**:
  `feedparser.parse(url)` natively accepts `file:///` URIs. Supplying a URI such as `file:///etc/passwd` or `file:///C:/Windows/System32/drivers/etc/hosts` in `RSSFeedProvider` or J-Novels RSS fallback allowed reading local files directly from the host filesystem during feed polling.
- **Remediation**:
  Updated `backend/app/providers/rss.py` and `backend/app/providers/jnovels.py` to decouple network transport from XML parsing. Feed content is pre-fetched using `safe_http_get()` (strictly validating HTTP/HTTPS schemes and public IP addresses), and only the validated in-memory XML string is passed to `feedparser.parse(resp.text)`. All attempts to supply `file:///` URIs are blocked before parser invocation.

### SEC-03: Webhook Diagnostic Response Leakage Oracle
- **Severity**: Medium | **CVSS 3.1**: 6.5 (`AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:N`) | **CWE-209**
- **Description**:
  `NotificationService.test_webhook()` in `backend/app/notifier/webhook.py` previously captured the first 200 characters of the HTTP response body (`resp.text[:200]`) upon receiving a non-2xx status code and reflected it in the JSON response to the caller. This transformed the diagnostic feature into an exfiltration oracle capable of reading internal service banners.
- **Remediation**:
  Removed response body reflection in `test_webhook()`. Error responses now return only the HTTP status code (e.g. `Discord returned HTTP 404`) and generic connection error summaries without echoing payload bodies.

### SEC-04: DOM-Based Cross-Site Scripting (XSS) in Dynamic Views
- **Severity**: High | **CVSS 3.1**: 7.5 (`AV:N/AC:L/PR:N/UI:R/S:C/C:H/I:L/A:N`) | **CWE-79**
- **Description**:
  In `backend/app/templates/discovery.html` (lines 515-516), synopsis text from external feeds was interpolated into `.outerHTML` with inadequate quote-only replacement. In `backend/app/templates/feeds.html` (lines 400-416), title, series name, and URLs from feed extractions were directly concatenated into `content.innerHTML`. Malicious web feeds or scraped sites containing `<script>` or `javascript:` links could execute arbitrary scripts in the client's browser session.
- **Remediation**:
  1. In `discovery.html`: Replaced `outerHTML` template strings with safe DOM element creation (`document.createElement('div')`), setting `.textContent = data.synopsis.trim()` and `.title = data.synopsis`.
  2. In `feeds.html`: Implemented `escapeHtml(str)` using temporary element text nodes and `sanitizeLink(url)` validating that URL schemes are strictly `http:` or `https:`. All dynamic variables are escaped prior to HTML rendering.

### SEC-05: Security Misconfiguration - Missing CSP and Permissions-Policy
- **Severity**: Medium | **CVSS 3.1**: 5.3 (`AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:L/A:N`) | **CWE-693**
- **Description**:
  The application middleware configured `X-Content-Type-Options`, `X-Frame-Options`, `X-XSS-Protection`, and `Referrer-Policy`, but omitted `Content-Security-Policy` (CSP) and `Permissions-Policy`.
- **Remediation**:
  Added the following headers to `add_security_headers` middleware in `backend/app/main.py`:
  - `Content-Security-Policy`: Restricts scripts to `'self'`, `'unsafe-inline'`, `https://cdn.tailwindcss.com`, and `https://unpkg.com`; restricts connections to `'self'`; frame-ancestors to `'self'`.
  - `Permissions-Policy`: `camera=(), microphone=(), geolocation=()`.

### SEC-06: Calibre Path Traversal & Filesystem Probing
- **Severity**: Medium | **CVSS 3.1**: 5.3 (`AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:N`) | **CWE-22**
- **Description**:
  The endpoint `/api/calibre/test?path=...` accepted arbitrary paths, allowing users to probe whether specific paths exist on disk and whether they contain SQLite headers.
- **Remediation**:
  Implemented `sanitize_calibre_path(path_str: str) -> str` in `backend/app/security.py`. Rejects null bytes (`\x00`), normalizes path separators, and explicitly disallows probing of executable or sensitive system file extensions (`.exe`, `.dll`, `.env`, `.key`, `.pem`, `.crt`, `.conf`, `.ini`, `.log`). Applied to `/api/calibre/test`.

### SEC-07: Broken Access Control & Unauthenticated APIs
- **Severity**: Medium | **CVSS 3.1**: 5.0 (`AV:A/AC:L/PR:N/UI:N/S:U/C:L/I:L/A:N`) | **CWE-306**
- **Description**:
  NovelTrax endpoints operate without user authentication, intended for single-user home networks and local container orchestration (Docker / TrueNAS SCALE). Exposing the container port directly to untrusted networks or the public Internet would allow unauthorized configuration changes.
- **Remediation / Recommendation**:
  For deployments requiring access outside local LAN, deploy behind an authenticating reverse proxy (e.g. Authelia, Authentik, Cloudflare Access) or configure an optional Bearer API token via environment variable `API_KEY`. The token is a documented blueprint, not an implemented authentication layer; see section 8.

### SEC-08: Uncontrolled Resource Consumption (DoS)
- **Severity**: Low | **CVSS 3.1**: 4.3 (`AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:L`) | **CWE-400**
- **Description**:
  Outbound HTTP requests without response byte ceilings could result in memory exhaustion if a remote server returned gigabytes of data.
- **Remediation**:
  `safe_http_get()` enforces a strict 10 MB maximum payload ceiling and a maximum redirect hop limit (5 hops), terminating downloads that exceed limits.

---

## 4. Milestones 2-5 Findings and Remediation Verification (SEC-11 to SEC-18)

Milestones 2-5 added Calibre omnibus parsing, timezone handling, persistent metadata caching, the qBittorrent downloader, the Nyaa provider, prioritized release groups and one-click ignore. Each addition was re-reviewed against the baseline controls, and the M5 adversarial/ground-truth run surfaced one new input-validation defect. The items below are ordered by their first security-relevant milestone.

### SEC-11: SSRF Guard Hardening (IPv6-Mapped IPv4, CGNAT, Cloud Metadata, DNS Failures)
- **Severity**: High | **CVSS 3.1**: 8.6 (`AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:N/A:N`) | **CWE-918** | extends SEC-01
- **Attack scenario**: A hostname (or literal address) that resolves to an IPv4-mapped IPv6 address, a CGNAT range, link-local metadata address, or whose DNS lookup fails could previously be treated as an ordinary public destination, allowing an outbound fetch to pivot into the local network or a cloud metadata service.
- **Remediation as implemented** (`backend/app/security.py`):
  1. `DISALLOWED_NETWORKS` now covers `0.0.0.0/8`, RFC 1918, CGNAT (`100.64.0.0/10`), loopback, link-local (`169.254.0.0/16`), documentation/test-nets, multicast, reserved, plus IPv6 unspecified, loopback, IPv4-mapped (`::ffff:0:0/96`), NAT64 (`64:ff9b::/96`), discard (`100::/64`), documentation, ULA (`fc00::/7`), link-local (`fe80::/10`) and multicast.
  2. `is_ip_disallowed()` recurses through `IPv6Address.ipv4_mapped`, so `::ffff:127.0.0.1`, `::ffff:169.254.169.254` and hex-encoded variants are blocked even though the wrapper address is IPv6.
  3. Explicit `metadata.google.internal` and loopback/`localhost` aliases are rejected before DNS (the downloader validator additionally blocks the `metadata` and `instance-data` hostnames per SEC-12).
  4. Every address returned by `socket.getaddrinfo()` is inspected, and DNS failure (`socket.gaierror`) or an empty result fails closed (returns `False`) instead of falling through to the fetch.
  5. `safe_http_get()` re-validates every redirect hop with `is_safe_public_url()` (httpx path) and the urllib fallback uses `SSRFSafeRedirectHandler.redirect_request()`, closing the redirect-based bypass.
  6. The guard is enforced on webhooks, `/api/feeds/test`, the RSS provider, the J-Novels RSS fallback and the Nyaa provider, and (from the M5 post-review batch, SEC-19) on every redirect hop of the J-Novels, NovelUpdates, custom HTML and metadata transports through `safe_client_get` / `safe_client_request`; `validate_downloader_destination()` is a deliberately separate policy for LAN downloader hosts (SEC-12).
- **Proving test suite**: `tests/test_challenger_adversarial_ip.py` (boundary vectors for CGNAT, link-local, IPv4-mapped IPv6, decimal/octal/hex forms), `tests/test_adversarial_m1.py`, `tests/test_adversarial_m1_it2.py`, `tests/test_tier5_adversarial_core.py` (T5_SEC_01, T5_SEC_02, T5_SEC_07 mapped-address and CGNAT cases), and `tests/test_e2e_suite.py` (T2_F06).

### SEC-12: qBittorrent Destination SSRF and Protocol Abuse
- **Severity**: High | **CVSS 3.1**: 8.1 (`AV:N/AC:L/PR:L/UI:N/S:C/C:H/I:N/A:N`) | **CWE-918** | new surface introduced in M3
- **Attack scenario**: The downloader host, port or custom Web API URL is saved in `app_settings` and then used by the server to log in and issue API calls. Pointing it at `http://169.254.169.254:8080`, `metadata.google.internal` or a non-HTTP scheme would turn the qBittorrent integration into a server-side probe of cloud metadata or unsupported protocols.
- **Remediation as implemented**:
  1. `validate_downloader_destination(host_or_url, scheme, resolve_dns)` in `backend/app/security.py` validates the scheme (only `http`/`https`), rejects cloud-metadata hostnames and addresses (`169.254.0.0/16`, `fe80::/10`, IPv4-mapped equivalents, `metadata.google.internal`) on every resolved address, and fails closed on unresolvable hosts.
  2. It deliberately permits loopback, RFC 1918, CGNAT and ULA destinations, because a qBittorrent instance normally runs on `localhost` or the LAN; this exception is constrained to non-metadata hosts.
  3. `POST /api/downloader/configure` calls the validator before persisting settings and returns HTTP 400 with the rejection reason. `QBitClient.validate_destination()` runs the same policy before `login()`, so a previously stored or indirectly supplied destination still cannot be contacted.
  4. A DNS-rebinding window remains inherent to validate-then-connect flows; it is recorded as residual risk in section 8.
- **Proving test suite**: `tests/test_downloader.py` (permits loopback/RFC 1918, blocks metadata), `tests/test_adversarial_deep.py`, `tests/test_challenger_m3_it2_stress.py`, `tests/test_challenger_invalidation_verification.py` (including mocked DNS rebinding to `169.254.169.254`), and `tests/test_tier5_adversarial_core.py` (T5 downloader destination cases).

### SEC-13: Feed Ingestion LFI Hardening on the Nyaa and J-Novels RSS Surfaces
- **Severity**: High | **CVSS 3.1**: 8.2 (`AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N`) | **CWE-73** | extends SEC-02 to M4 surfaces
- **Attack scenario**: `feedparser.parse(raw_url)` treats `file:///` URIs as readable local resources. The M4 Nyaa provider and the J-Novels RSS fallback both parse RSS/Atom, so a configured or user-supplied feed URL of `file:///C:/Windows/System32/drivers/etc/hosts` would return host file contents as feed titles.
- **Remediation as implemented**:
  1. `RSSFeedProvider.fetch_releases()` performs `safe_http_get(self.url)` and then `feedparser.parse(resp.text)` on the in-memory body.
  2. The J-Novels RSS fallback (`jnovels.py`) does the same via `safe_http_get(rss_url)`.
  3. `NyaaNovelProvider._fetch_rss()` uses `safe_http_get()` followed by in-memory parsing, and its HTML crawl path also uses `safe_http_get()`.
  4. Because `safe_http_get()` enforces the scheme allow-list and public-IP checks before any network or file access, `file://`, `ftp://`, `gopher://` and non-HTTP pseudo-schemes never reach the parser.
- **Proving test suite**: `tests/test_security_audit.py` (LFI prevention in `RSSFeedProvider`, `file://` rejection), `tests/test_e2e_suite.py` (T1_F07, T2_F07), `tests/test_adversarial_m1.py` (redirect and `file://` vectors), and `tests/test_tier5_adversarial_db_providers.py` (RSS XXE entity and external-entity neutralisation).

### SEC-14: Webhook Diagnostic Sanitization Verification
- **Severity**: Medium | **CVSS 3.1**: 6.5 (`AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:N`) | **CWE-209** | verification of SEC-03 after M2-M5
- **Attack scenario**: Returning the remote response body from `/api/settings/test-webhook` lets a caller use the server as a read primitive against internal services; an attacker who can reach the endpoint can point it at an internal URL and read the first bytes of the response.
- **Remediation as implemented**:
  1. `NotificationService.test_webhook()` returns only the HTTP status code (e.g. `Discord returned HTTP 404`) or a generic connection-error summary; `resp.text` is no longer embedded in any result field.
  2. Both send paths and the test path call `is_safe_public_url()` before dispatching, and blocked destinations return a generic SSRF protection message.
  3. The generic webhook sender and Discord sender both verify the configured URL before posting, so a stale unsafe setting cannot be exercised through the notification path.
- **Proving test suite**: `tests/test_security_audit.py` (webhook SSRF and body-sanitization checks), `tests/test_e2e_suite.py` (T2_F08), and `tests/test_tier5_adversarial_core.py`.

### SEC-15: DOM XSS Escaping in Discovery and Feed Testing Templates
- **Severity**: High | **CVSS 3.1**: 7.5 (`AV:N/AC:L/PR:N/UI:R/S:C/C:H/I:L/A:N`) | **CWE-79** | verification of SEC-04 after M2-M5
- **Attack scenario**: A malicious or compromised third-party feed (J-Novels, NovelUpdates, Nyaa or a user-added RSS source) supplies a synopsis or title containing `<img onerror=...>` or a `javascript:` link, which executes in the dashboard origin when the user opens Discovery or clicks "Test Scraper".
- **Remediation as implemented**:
  1. `discovery.html` builds the synopsis node with `document.createElement()` and assigns `.textContent = data.synopsis.trim()`; the title tooltip is assigned as a DOM property, not interpolated into markup. Cover URLs pass through `encodeURIComponent()`.
  2. `feeds.html` defines `escapeHtml(str)` using a temporary element's `textContent` and applies it to every interpolated field (`item.title`, `item.volume`, `item.series_name`, status messages and errors). `sanitizeLink(url)` accepts only `http:`/`https:` before the link is rendered.
  3. The CSP in SEC-16 is an independent second layer, so a missed escape cannot load external script.
- **Proving test suite**: `tests/test_security_audit.py` (asserts `escapeHtml`/`sanitizeLink` and safe `textContent` sinks), `tests/test_e2e_suite.py` (T1_F09 script-tag title case), `tests/test_adversarial_m1.py`, and `tests/test_tier5_adversarial_core.py`.

### SEC-16: CSP and Permissions-Policy Coverage Verification
- **Severity**: Medium | **CVSS 3.1**: 5.3 (`AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:L/A:N`) | **CWE-693** | verification of SEC-05 after M2-M5
- **Attack scenario**: Without a CSP, an injected inline script or external CDN script executes with full access to the dashboard session; without Permissions-Policy, a future injection can request camera, microphone or geolocation access.
- **Remediation as implemented** (`backend/app/main.py`, `add_security_headers` middleware, applied to every response):
  - `Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://unpkg.com; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; font-src 'self' data:; connect-src 'self'; frame-ancestors 'self';`
  - `Permissions-Policy: camera=(), microphone=(), geolocation=()`
  - `X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN`, `X-XSS-Protection: 1; mode=block`, `Referrer-Policy: strict-origin-when-cross-origin`.
  - `'unsafe-inline'` remains in `script-src`/`style-src` because the Jinja2 templates use inline event handlers and styles; this is accepted and recorded as residual risk in section 8.
- **Proving test suite**: `tests/test_e2e_suite.py` (T1_F10 CSP and Permissions-Policy assertions), `tests/test_challenger_m2_it2_regression.py` (live response header check), and `tests/test_tier5_adversarial_core.py`.

### SEC-17: Calibre Path-Traversal Validation Verification
- **Severity**: Medium | **CVSS 3.1**: 5.3 (`AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:N`) | **CWE-22** | verification of SEC-06 after M2-M5
- **Attack scenario**: `/api/calibre/test?path=` reports whether a path exists and whether it looks like a SQLite database, letting a caller enumerate the filesystem layout of the container or host mount.
- **Remediation as implemented**:
  1. `/api/calibre/test` calls `sanitize_calibre_path()` before instantiating `CalibreScanner`; rejection returns HTTP 400 with `available: false` and no filesystem information.
  2. `sanitize_calibre_path()` rejects empty strings and embedded null bytes, normalizes with `os.path.normpath()`, and blocks probing of executable/credential/log extensions (`.exe`, `.dll`, `.bat`, `.cmd`, `.ps1`, `.vbs`, `.sh`, `.env`, `.key`, `.pem`, `.crt`, `.conf`, `.ini`, `.log`).
  3. The Calibre reader still opens the database read-only with `mode=ro&immutable=1`, so even a permitted path cannot write to the library.
- **Proving test suite**: `tests/test_security_audit.py`, `tests/test_adversarial_m1.py` (null-byte, traversal and extension vectors), `tests/test_e2e_suite.py` (T1_F11), and `tests/test_tier5_adversarial_core.py` (T5_SEC_20).

### SEC-18: Null-Byte and Control-Character URL Rejection
- **Severity**: Low | **CWE-20** (input validation / URL canonicalization) | newly found during M5
- **Attack scenario**: A URL containing a NUL byte or other C0 control character (for example `http://example.com/feed\x00.xml`) can truncate or confuse downstream URL parsing, logging or filesystem calls in a way that differs from the URL the validator inspected. In this build the host still resolved and the URL passed validation before the rejection was added, so the defect is an input-validation/canonicalization gap and **not** an SSRF bypass: no concrete truncation or address-confusion path was demonstrated in this codebase, which is why the severity is Low rather than Medium.
- **Remediation as implemented**: `is_safe_public_url()` rejects any URL containing NUL (`0x00`) or C0 control characters (`0x01`-`0x1F`, `0x7F`) before scheme/hostname parsing and DNS resolution, returning `False` with a logged rejection reason. This is defense-in-depth, aligned with CWE-20; it does not replace the address-based SSRF checks in SEC-11.
- **How it was found**: This is a direct product of the Milestone 5 ground-truth run. The previous suite used bare `assert` statements that Calibre's `-O2` bytecode compilation stripped, so failing checks silently vanished; after the assertions were converted to unstrippable guards, the case at `tests/test_e2e_suite.py` (`T2_F07_2`) failed and exposed the gap. It is exactly the class of defect the stripped-assert suite could never have caught.
- **Proving test suite**: `tests/test_e2e_suite.py` (`T2_F07_2` null-byte URL rejection) and the SSRF input-hardening cases in `tests/test_tier5_adversarial_core.py`.

---

## 5. Post-Review Findings and Fixes (M5 Final Batch, SEC-19 to SEC-23)

These findings were raised after revision 2 was written, during the final ground-truth, adversarial and UI-wiring pass. SEC-19 to SEC-21 are security fixes; SEC-22 and SEC-23 are release-quality defects recorded here for traceability and test attribution.

### SEC-19: SSRF via HTTP Redirect Hop
- **Severity**: High | **CVSS 3.1**: 8.6 (`AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:N/A:N`) | **CWE-918** | post-review security fix
- **Attack scenario**: A feed or scraper URL that passes `is_safe_public_url()` can sit on a public host that responds `302`/`307` with a `Location` pointing at `http://169.254.169.254/`, `http://127.0.0.1:8080/` or a RFC 1918 address; because clients used `follow_redirects=True`, only the initial URL was validated and the internal hop was fetched blindly.
- **Remediation as implemented** (`backend/app/security.py`):
  1. Added `safe_client_get(client, url, max_redirects=5)` and `safe_client_request(client, method, url, max_redirects=5)`. Both validate the initial URL with `is_safe_public_url()`, request with `follow_redirects=False`, resolve each `Location` with `urljoin()`, validate the resolved destination before requesting it, and abort after 5 hops with a `ValueError("SSRF blocked: ...")`.
  2. All scraper transports now construct their clients with `follow_redirects=False` and call the helper: `jnovels.py` (search, series-page, master-index and HTML fallback fetches), `novelupdates.py`, and `html_scraper.py`.
  3. The metadata provider was migrated in the same batch: `metadata.py` uses `safe_client_get` for the Kitsu GET and `safe_client_request(client, "post", ...)` for the AniList GraphQL POST, so redirect hops are validated on the metadata paths as well.
  4. An unsafe hop raises before the request is sent, so the private/metadata destination is never contacted.
- **Proving test suite**: `tests/test_security_audit.py` — `test_safe_client_get_follows_validated_public_redirects` (a public → public → public chain is followed and parsed), `test_safe_client_get_blocks_unsafe_redirect_hops` (public → `169.254.169.254`, `127.0.0.1` and `192.168.1.1` refused and the target URL is absent from the client's request log; 5-hop cap enforced), and `test_jnovels_provider_redirect_ssrf_hardening` (provider-level master-index test: public chain still parses, metadata/loopback redirects yield zero results and are never requested). The residual `urllib` fallback branches are recorded as RR-05 in section 8.

### SEC-20: Percent-Encoded NUL / Control-Character URL Obfuscation
- **Severity**: Medium | **CWE-20** (input validation / URL canonicalization) | post-review security fix
- **Attack scenario**: A URL such as `http://127.0.0.1%00@google.com/` embeds a percent-encoded NUL in the userinfo. Different URL parsers may decode the escape at different layers, so a downstream client could interpret a different authority than the validator inspected — an SSRF-filter obfuscation. Before the fix the case was accepted by `is_safe_public_url()` whenever the apparent hostname (here `google.com`) resolved; in offline gate runs it only appeared blocked because DNS resolution failed first, which masked the gap.
- **Remediation as implemented**: `is_safe_public_url()` now rejects any URL containing a percent-encoded C0 or DEL escape (`%(?:0[0-9a-f]|1[0-9a-f]|7f)`, case-insensitive) before scheme/hostname parsing, logging the rejection and returning `False`. This complements the raw-control-character check in SEC-18, which only matched literal bytes.
- **Proving test suite**: `tests/test_adversarial_m1.py` (obfuscated payload list including `http://127.0.0.1%00@google.com/`) and `tests/test_adversarial_m1_it2.py` (exhaustive SSRF IP/vector matrix, including malformed URLs and null-byte inputs).

### SEC-21: Userinfo `@` Parser-Confusion URL Acceptance
- **Severity**: Medium | **CWE-20** (input validation / URL canonicalization) | post-review security fix
- **Attack scenario**: `http://google.com#@127.0.0.1/` places `@127.0.0.1` after the authority, where a parser-confused filter (or a naive downstream client) can treat the trailing token as the host while `urlparse` reports `google.com`. The validator therefore inspected a different destination than the one an alternate parser could reach.
- **Remediation as implemented**: `is_safe_public_url()` now rejects any URL where an `@` occurs outside the parsed `netloc` (`url_clean.count("@") > netloc.count("@")`), i.e. `@` is only accepted as userinfo inside the authority. Legitimate credential-bearing URLs such as `http://user:pass@127.0.0.1/` remain blocked by the address checks, and a compatible harness case `http://127.0.0.1@1.1.1.1/` (userinfo before a public host) still evaluates as permitted.
- **Proving test suite**: `tests/test_adversarial_m1.py` (obfuscation payload list including `http://google.com#@127.0.0.1/`) and `tests/test_adversarial_m1_it2.py` (userinfo-confusion and malformed-URL vectors).

### SEC-22: Calibre Immutable-Read Torn Snapshot (Release Quality)
- **Severity**: Medium (quality/reliability, not a scoreable security vulnerability) | **CWE-362** (concurrent execution using a shared resource) | post-review quality fix
- **Attack scenario / failure mode**: `mode=ro&immutable=1` tells SQLite that `metadata.db` cannot change. While Calibre is mid-commit, an immutable reader can observe a torn snapshot and raise `sqlite3.DatabaseError: database disk image is malformed`, aborting the sync, library summary or matcher scan. This was reproduced standalone against a live writer, so it is a real defect rather than a flake.
- **Remediation as implemented** (`backend/app/calibre/scanner.py`):
  1. Read helpers now route through `_run_read(query, params, label)` instead of opening and executing ad hoc.
  2. The immutable fast path is unchanged and remains the default: `_get_connection(immutable=True)` still uses `mode=ro&immutable=1`.
  3. On `sqlite3.DatabaseError` from the immutable connection, `_run_read` logs a warning and retries the query exactly once with `mode=ro` (no `immutable`). If the retry also fails, the error propagates and the existing callers fail closed (summary reports `available: false` with the error, series/book listings return an empty list and log).
  4. Trade-off recorded explicitly: the retry participates in normal SQLite read locking and adds a small read cost, but it only runs on the exceptional inconsistent-snapshot path; the zero-lock fast path and the `immutable=1` guarantee for normal reads are preserved.
- **Proving / verification method**: reproduced standalone with a concurrent writer; the existing zero-lock and concurrency suites (`tests/test_challenger_m2_regression.py`, `tests/test_tier5_adversarial_core.py`) exercise concurrent scanner reads. The retry behavior itself is verified by the standalone reproduction because no committed test drives a malformed immutable snapshot deterministically.

### SEC-23: Flat Release View `TemplateSyntaxError` (Release Quality)
- **Severity**: High (release quality — the whole flat view was unrenderable) | **CWE**: — (template integrity defect, not a security issue) | post-review quality fix
- **Defect**: `backend/app/templates/partials/flat_release_row_chunk.html` contained 11 `{% if %}` blocks against 10 `{% endif %}` blocks: a prioritized-badge `{% if %}` block had been nested inside `{% if r.author %}` without its closing tag during the Milestone 4 template edits. This was present at commit `30c7797`. As a result, rendering `/releases?view=flat` or calling `GET /api/releases/flat_rows` raised `jinja2.exceptions.TemplateSyntaxError` — the flat view could not render at all.
- **Why it survived the gates**: the test suites asserted on template source strings rather than rendering them, and no test rendered Jinja because `jinja2` is **not installed in the Calibre embedded interpreter** used to execute the gates. Template rendering is therefore not verifiable in that environment — a real coverage gap, recorded as RR-09 in section 8.
- **Remediation as implemented**: the missing `{% endif %}` was restored (block tags now balance at 12/12) and a static guard was added: `tests/test_ui_wiring.py` verifies (a) balanced `{% if %}`/`{% endif %}`, `{% for %}`/`{% endfor %}` and `{% block %}`/`{% endblock %}` counts and `{%`/`%}` and `{{`/`}}` delimiter counts in every edited template, (b) the release Download button wiring in both release partials, and (c) the Nyaa Add-Feed preset. Runtime rendering still requires an environment with Jinja2 available (Docker or a development virtualenv), as noted in RR-09.
- **Proving test suite**: `tests/test_ui_wiring.py`.

---

## 6. Codebase Simplification & Dependency Minimization

In compliance with Requirement R4:
1. **Dependency Footprint**:
   - `python-multipart>=0.0.9`: **Removed**. The application uses JSON payloads for API endpoints and query parameters for forms; multipart form parsing was unused.
   - `pydantic>=2.6.4`: **Removed from top-level manifest**. Pydantic is already transitively managed by FastAPI, and NovelTrax defines no custom Pydantic models.
   - Net result: Top-level dependencies reduced from **9 to 7** (22% reduction). `backend/requirements.txt` now lists `fastapi`, `uvicorn[standard]`, `jinja2`, `httpx`, `beautifulsoup4`, `feedparser` and `apscheduler`.
2. **Dead Code Elimination**:
   - `backend/app/main.py`: Removed unused `Form` import.
   - `backend/app/main.py`: Consolidated 8 inline `import asyncio` occurrences into a single module-level import.
   - `backend/app/providers/metadata.py`: Removed redundant duplicate `return None` statement.
   - `backend/app/providers/jnovels.py`: Corrected 12-space indentation defect at line 428 to 8 spaces, restoring the link parsing loop in `fetch_master_index()`.
3. **Additions kept dependency-free**: The M3 downloader (`app/downloader/qbit.py`) uses only the standard library (`urllib`, `http.cookiejar`, `json`), and the M2 timezone work uses the standard-library `zoneinfo` module, so neither milestone added a package to `requirements.txt`.

---

## 7. Verification & Test Attestation

All remediations were independently executed and verified under the Calibre 7.x Python 3.11 environment (`calibre-debug.exe`), plus the project's Docker/Python runtime where third-party packages are present:

1. **Dedicated Security Audit Test Suite (`tests/test_security_audit.py`)**:
   - Command: `& "C:\Program Files\Calibre2\calibre-debug.exe" -e tests\test_security_audit.py`
   - Coverage: SSRF disallowed-URL vectors across loopback, RFC 1918, CGNAT, link-local/cloud metadata, IPv6 forms and non-HTTP schemes; `safe_http_get` SSRF blocking; LFI prevention in `RSSFeedProvider`; webhook SSRF and body sanitization; Calibre path traversal and file sanitization; frontend DOM XSS sanitization checks; dependency minimization; J-Novels master-index fix; the null-byte/control-character URL case; and the three redirect-SSRF regression tests for `safe_client_get` (validated public chain, refused unsafe hops with hop cap, and the provider-level J-Novels master-index redirect case).
2. **Adversarial IP and Guard Suites (`tests/test_challenger_adversarial_ip.py`, `tests/test_adversarial_m1.py`, `tests/test_adversarial_m1_it2.py`, `tests/test_adversarial_deep.py`, `tests/test_challenger_invalidation_verification.py`)**:
   - Coverage: range boundaries (CGNAT, link-local, IPv4-mapped IPv6, decimal/octal/hex literals), redirect-hop blocking, downloader destination policy, DNS-rebinding simulation, fail-closed DNS handling, and the exhaustive SSRF input matrix including percent-encoded NUL obfuscation and userinfo `@` parser-confusion vectors.
3. **End-to-End Suites (`tests/test_e2e_suite.py`, `tests/test_full_workflow.py`)**:
   - Coverage: security features T1_F06/F07/F08/F09/F10/F11 and T2_F06/F07/F08, plus the ground-truth workflow stages. These are the suites whose assertions were converted to unstrippable guards during M5; the conversion and final run are owned by the test track.
4. **Milestone security suites** (`tests/test_downloader.py`, `tests/test_nyaa.py`, `tests/test_tier5_adversarial_core.py`, `tests/test_tier5_adversarial_db_providers.py`):
   - Coverage: qBittorrent destination SSRF, Nyaa feed safety, Tier 5 adversarial security, database/provider edge cases.
5. **UI wiring and template-integrity guard (`tests/test_ui_wiring.py`)**:
   - Stdlib-only static guard (no Jinja2 required) that proves the release Download button is wired in both release partials, the global `downloadRelease()` helper calls the documented endpoint contract, the Nyaa Add-Feed preset exists, and every edited template has balanced Jinja block tags and delimiters. This is the regression guard for SEC-23; runtime rendering is not executable in the Calibre interpreter (see RR-09).

6. **Final attestation run (Lead-executed, all 44 suites)**:
   - Every suite in `tests/` was executed through Calibre's interpreter with assertions live. Result: **44 of 44 exit 0, zero `[FAIL]`/`[ERROR]` markers**.
   - Primary suites: `test_e2e_suite.py` 255/255; `test_full_workflow.py` all 17 stages; `test_tier5_adversarial_core.py` 85/85; `test_tier5_adversarial_db_providers.py` 280/280; `test_nyaa.py` 107/107; `test_downloader.py` 184/184; `test_omnibus_timezone.py` 30/30; `test_security_audit.py` 100%; `test_challenger_m4_it2_empiric.py` 82/82.
   - Integrity precondition: `tests/test_assert_hygiene.py` reports **0 bare asserts across the 45 `.py` files under `tests/`** (44 test modules plus `mock_calibre.py`), which is what makes the counts above meaningful rather than vacuous.

---

## 8. Residual Risk & Accepted Risk

NovelTrax is a single-user, self-hosted home-lab application. The following risks are accepted or explicitly mitigated by deployment practice rather than code; they are recorded so the operator can make an informed exposure decision.

| ID | Accepted / residual risk | Why it is accepted | Operator mitigation |
| :--- | :--- | :--- | :--- |
| RR-01 | **No authentication layer** (SEC-07) | Adding mandatory auth would break deployments already protected by an authenticating reverse proxy (Authelia, Authentik, Cloudflare Access) and change the public interface. | Bind the port to `127.0.0.1` behind a reverse proxy, or restrict the container to the trusted LAN/VPN. The documented `API_KEY` blueprint is not implemented in this revision. |
| RR-02 | **Network-trust assumption** | The app assumes the dashboard and the configured feeds/downloader are reachable only by trusted users on a trusted network; anyone who can reach the dashboard can change settings, including webhook and downloader credentials. | Keep the published port LAN-only; the shipped `docker-compose.yml` drops all Linux capabilities and sets `no-new-privileges`, but that does not replace network segmentation. |
| RR-03 | **Downloader credentials stored in SQLite** | The qBittorrent username/password are stored as plaintext values in `app_settings` because the application intentionally has no secrets vault or key management dependency. | Use a dedicated qBittorrent account with a limited category/save path, protect the `DATA_DIR` volume with filesystem permissions, and never expose the dashboard publicly. |
| RR-04 | **DNS-rebinding window** | `is_safe_public_url()` and `validate_downloader_destination()` validate the resolved address before connecting; a hostile resolver could return a public address for validation and a private address for the connection. | Outbound access is limited to the destinations the user configures; do not add untrusted feed or downloader URLs. Full immunity requires pinning the validated IP at connection time. |
| RR-05 | **`urllib` fallback branches in `jnovels.py` still follow redirects** | The four `urllib.request.urlopen` fallbacks in `JNovelsProvider` are only reachable when `httpx` is absent. `httpx` is a hard requirement in `backend/requirements.txt`, so the branches are a dead path in production; routing them through the safe client would also break suites that deliberately patch `urllib.request.urlopen` to exercise the fallback. The httpx paths (J-Novels, NovelUpdates, custom HTML scraper, metadata provider) all validate every redirect hop (SEC-19). | Keep `httpx` installed and do not remove it from `backend/requirements.txt`; the fallback then never runs. |
| RR-06 | **CSP allows `'unsafe-inline'`** (SEC-16) | The Jinja2 templates use inline event handlers and inline styles; removing `'unsafe-inline'` would require nonce/hash refactoring across every template. | The escaping controls in SEC-15 are the primary XSS defense; CSP is defense-in-depth. Revisit if the templates are refactored to external scripts. |
| RR-07 | **Downloader is optional and trust-boundary-adjacent** | When configured, NovelTrax holds a session against a local qBittorrent instance and can add/remove torrents. When unconfigured, all downloader endpoints degrade gracefully and no connection is attempted. | Leave the downloader disabled if it is not used; restrict the qBittorrent Web API to the same host/network as NovelTrax. |
| RR-08 | **`safe_http_get()` 10 MB / 5-redirect ceilings** (SEC-08) | Legitimate feeds are far below the limits; a very large or redirect-heavy feed fails closed rather than exhausting memory. | No action required. |
| RR-09 | **Jinja template rendering is not verifiable in the Calibre interpreter** | The gate suites run under Calibre's embedded Python, which has neither `jinja2` nor `fastapi` installed, so templates can only be asserted as source text, not rendered. This coverage gap is why the SEC-23 unbalanced-block defect survived the M1-M4 gates (the flat view could not be rendered in that environment at all). | The new `tests/test_ui_wiring.py` static guard balances Jinja block tags/delimiters and checks the wiring contracts; full runtime rendering must be verified in Docker or a development virtualenv before release. |
| RR-10 | **`is_safe_public_url()` rejects more than it strictly must** | The guard rejects any `@` outside the authority component and any percent-encoded control byte (`%00`-`%1f`, `%7f`) anywhere in the URL, which also rejects a few exotic but harmless legitimate URLs. This is deliberate fail-closed behaviour: the rejected forms are exactly the ones used for userinfo/parser-confusion and truncation attacks. | None needed. If a legitimate feed URL is refused, percent-encode or restructure it rather than weakening the guard. |

---

## 9. Change Log

### Revision 2 (November 2, 2026)

- Re-verified SEC-01 to SEC-06 against the M2-M5 code and documented the expanded guard, sanitization and escaping behavior.
- Added SEC-11 (SSRF guard hardening), SEC-12 (qBittorrent destination SSRF), SEC-13 (feed LFI on the Nyaa/J-Novels RSS surfaces), SEC-14 (webhook sanitization verification), SEC-15 (DOM XSS verification), SEC-16 (CSP/Permissions-Policy verification), SEC-17 (Calibre path-traversal verification) and SEC-18 (null-byte/control-character URL rejection).
- Added the residual and accepted-risk section, including the then-unguarded HTML-scraper transport paths and the plaintext downloader credentials.
- Removed hard-coded test-step counts from the attestation section so the report does not go stale while the suite is repaired; the final suite counts are inserted by the project lead.

### Revision 3 (November 2, 2026)

- Added SEC-19 (SSRF via HTTP redirect hop): shared `safe_client_get` / `safe_client_request` now validate every `urljoin`-resolved redirect hop with `follow_redirects=False` and a 5-hop cap across the J-Novels, NovelUpdates, custom HTML and metadata transports.
- Added SEC-20 (percent-encoded NUL/control-character URL obfuscation) and SEC-21 (userinfo `@` parser confusion); both now fail closed in `is_safe_public_url()`.
- Added SEC-22 (Calibre `mode=ro&immutable=1` torn snapshot): `_run_read` retries once with plain `mode=ro` on `sqlite3.DatabaseError` and then fails closed, preserving the zero-lock immutable fast path for normal reads.
- Added SEC-23 (flat release view `TemplateSyntaxError`): restored the missing `{% endif %}` and added the static Jinja integrity/wiring guard (`tests/test_ui_wiring.py`).
- Updated residual risk: RR-05 now records the `urllib` fallback redirect behavior (dead path while `httpx` is installed) instead of the now-fixed HTML-scraper gap; added RR-09 for the Jinja-rendering coverage gap in the Calibre interpreter.
- Expanded the verification section with the redirect-SSRF, adversarial input-matrix and UI-wiring suites; the tracked findings total is 23.
