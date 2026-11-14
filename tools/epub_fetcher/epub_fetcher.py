"""Resolve a link-hider/shortener chain and download the final EPUB.

Flow (see README): the source anchor points at a shortener that may interpose
Cloudflare Turnstile, a countdown page and a "Get Link" button before the real
file. FlareSolverr solves the Cloudflare/Turnstile gate; Playwright drives the
remaining pages and captures the download.

This module is intentionally dependency-light at import time: Playwright is
imported only inside :func:`fetch`, so the helper tests run without a browser.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("epub_fetcher")

#: Buttons/links that advance the interstitial chain, highest priority first.
CLICK_LABELS: Tuple[str, ...] = (
    "get link", "getlink", "download now", "download", "click here to continue",
    "continue", "proceed", "next", "click here", "verify you are human",
    "i am human",
)
#: Anchors/URL suffixes that mean "this is already the file".
FILE_SUFFIXES: Tuple[str, ...] = (".epub", ".pdf", ".cbz", ".zip", ".mobi", ".azw3")
TURNSTILE_RESPONSE_SELECTOR = "input[name='cf-turnstile-response']"
EXECUTABLE_SUFFIXES = (".exe", ".msi", ".dmg", ".pkg", ".apk", ".bat", ".cmd", ".sh")
TRUSTED_DOWNLOAD_HOSTS = (
    "docs.google.com",
    "drive.google.com",
    "drive.usercontent.google.com",
    "pixeldrain.com",
    "mediafire.com",
    "dropbox.com",
    "dropboxusercontent.com",
)
#: Text that indicates a Cloudflare/Turnstile gate rather than the file page.
CHALLENGE_MARKERS: Tuple[str, ...] = (
    "just a moment", "verify you are human", "checking your browser",
    "enable javascript and cookies", "cf-chl", "turnstile",
)

_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


class FetchError(RuntimeError):
    """The chain could not be resolved to a downloadable file."""


@dataclass
class FetchResult:
    path: str
    filename: str
    final_url: str = ""
    steps: List[str] = field(default_factory=list)


def safe_filename(value: Any, fallback: str = "novel.epub") -> str:
    """Basename-only, filesystem-safe name with a meaningful file suffix."""
    name = os.path.basename(str(value or "").strip().replace("\\", "/"))
    name = _UNSAFE_FILENAME.sub("_", name).strip(" ._")
    if not name:
        return fallback
    if not name.lower().endswith(FILE_SUFFIXES):
        name += os.path.splitext(fallback)[1] or ".epub"
    return name[:180]


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    """Read a non-negative integer env override, falling back safely."""
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return default if default >= minimum else 0
    try:
        value = int(raw)
    except ValueError:
        return default if default >= minimum else 0
    return value if value >= minimum else 0


def is_challenge_text(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in CHALLENGE_MARKERS)


def candidate_click_labels(texts: Iterable[str]) -> List[str]:
    """Filter visible control labels to the ones worth clicking, in priority order."""
    lowered = [str(text or "").strip().lower() for text in texts]
    ordered: List[str] = []
    for label in CLICK_LABELS:
        for text in lowered:
            if text == label and text not in ordered:
                ordered.append(text)
    return ordered


def looks_like_file_url(url: str) -> bool:
    path = urllib.parse.urlsplit(str(url or "")).path.lower()
    return path.endswith(FILE_SUFFIXES)


class FlareSolverrClient:
    """Minimal FlareSolverr v1 client (`request.get` + sessions are enough)."""

    def __init__(self, base_url: str, timeout: float = 120.0, max_timeout_ms: int = 90000):
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.timeout = float(timeout)
        self.max_timeout_ms = int(max_timeout_ms)
        # Give post-solve JS a moment to swap the challenge for the real page.
        self.wait_in_seconds = _env_int("EPUB_FETCHER_FLARESOLVERR_WAIT", 2, minimum=0)
        # Site-specific Tab count to focus a standalone Turnstile widget.
        self.tabs_till_verify = _env_int("EPUB_FETCHER_FLARESOLVERR_TABS", 0, minimum=1)

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    def _post(self, body: Dict[str, Any]) -> Dict[str, Any]:
        if not self.enabled:
            raise FetchError("FlareSolverr is not configured")
        payload = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/v1",
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise FetchError(f"FlareSolverr request failed: {exc}") from exc
        if str(data.get("status") or "").lower() != "ok":
            raise FetchError(f"FlareSolverr rejected the request: {data.get('message') or data.get('status')}")
        return data

    def create_session(self) -> str:
        data = self._post({"cmd": "sessions.create", "maxTimeout": self.max_timeout_ms})
        return str(data.get("session") or "")

    def destroy_session(self, session: str) -> None:
        if not session:
            return
        try:
            self._post({"cmd": "sessions.destroy", "session": session})
        except Exception:
            pass

    def request_get(
        self,
        url: str,
        cookies: Optional[List[Dict[str, Any]]] = None,
        *,
        session: str = "",
        include_tabs: bool = True,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "cmd": "request.get",
            "url": str(url),
            "maxTimeout": self.max_timeout_ms,
        }
        if self.wait_in_seconds > 0:
            body["waitInSeconds"] = self.wait_in_seconds
        if include_tabs and self.tabs_till_verify > 0:
            body["tabs_till_verify"] = self.tabs_till_verify
        if session:
            body["session"] = session
        browser_cookies = [
            {"name": str(cookie["name"]), "value": str(cookie.get("value") or "")}
            for cookie in (cookies or [])
            if isinstance(cookie, dict) and cookie.get("name")
        ]
        if browser_cookies:
            # The shortener gates its form behind a cookie it sets on the
            # intermediate host (e.g. kecapku ``tp``). A fresh FlareSolverr
            # browser will otherwise load the unscoped homepage and never see
            # the Turnstile widget.
            body["cookies"] = browser_cookies
        data = self._post(body)
        solution = data.get("solution") or {}
        if not isinstance(solution, dict) or not solution.get("url"):
            raise FetchError("FlareSolverr returned no solution URL")
        return solution

    @staticmethod
    def cookies_for_context(solution: Dict[str, Any]) -> List[Dict[str, Any]]:
        cookies: List[Dict[str, Any]] = []
        for item in solution.get("cookies") or []:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            cookie = {
                "name": str(item.get("name")),
                "value": str(item.get("value") or ""),
                "path": str(item.get("path") or "/"),
            }
            if item.get("domain"):
                cookie["domain"] = str(item["domain"])
            if item.get("expires"):
                try:
                    cookie["expires"] = float(item["expires"])
                except (TypeError, ValueError):
                    pass
            cookie["secure"] = bool(item.get("secure"))
            cookie["httpOnly"] = bool(item.get("httpOnly"))
            cookies.append(cookie)
        return cookies


def _challenge_visible(page) -> bool:
    """Check the live page again after a manual Turnstile click.

    A click returning successfully only means the element was clicked, not that
    the challenge cleared. Treat unreadable pages as still challenged so the
    FlareSolverr fallback remains reachable.
    """
    try:
        if is_challenge_text(page.inner_text("body")[:8000]):
            return True
        return "challenges.cloudflare.com" in page.content()
    except Exception:
        return True


def _dismiss_consent(page) -> bool:
    """Click a Funding Choices-style consent button via JS.

    The consent modal traps keyboard focus and sits above the Turnstile widget,
    so both the checkbox click and FlareSolverr's Tab/Space solver fail until it
    is dismissed. Accepting the consent yields cookies that are forwarded to
    FlareSolverr so it sees the same gate page.
    """
    try:
        clicked = bool(page.evaluate(
            """
            () => {
                const labels = ['Consent', 'Accept all', 'Accept', 'I agree', 'Got it!', 'Got it'];
                for (const button of document.querySelectorAll('button')) {
                    const rect = button.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    if (labels.includes((button.innerText || '').trim())) {
                        button.click();
                        return true;
                    }
                }
                return false;
            }
            """
        ))
        if clicked:
            logger.info("Consent overlay dismissed")
        return clicked
    except Exception:
        return False


def _dismiss_consent_soon(page, timeout: float = 6.0) -> bool:
    """Poll briefly for the consent overlay; it often renders after the form."""
    deadline = time.monotonic() + max(0.5, float(timeout))
    while time.monotonic() < deadline:
        if _dismiss_consent(page):
            return True
        time.sleep(0.5)
    return False


def _has_turnstile_form(page) -> bool:
    """Detect a Turnstile-gated form (e.g. the kecapku/shortener hop)."""
    try:
        return bool(page.query_selector(TURNSTILE_RESPONSE_SELECTOR)) or bool(
            page.query_selector("form .cf-turnstile")
        )
    except Exception:
        return False


def _turnstile_token(page) -> str:
    try:
        if not page.query_selector(TURNSTILE_RESPONSE_SELECTOR):
            return ""
        return str(page.eval_on_selector(TURNSTILE_RESPONSE_SELECTOR, "el => el.value") or "")
    except Exception:
        return ""


def _wait_for_turnstile_token(page, timeout: float = 30.0) -> str:
    """Wait for the widget to issue a token; never treat a click as clearance."""
    deadline = time.monotonic() + max(1.0, float(timeout))
    next_widget_click = 0.0
    next_checkbox_click = 0.0
    while time.monotonic() < deadline:
        token = _turnstile_token(page)
        logger.debug("Turnstile wait url=%s token_len=%d", page.url, len(token or ""))
        if token:
            return token
        # Funding Choices can render well after the form; keep trying while
        # waiting instead of blocking the widget behind its modal.
        _dismiss_consent(page)
        now = time.monotonic()
        if now >= next_widget_click:
            next_widget_click = now + 3.0
            try:
                widget = page.locator(".cf-turnstile").first
                if widget.count() and widget.is_visible():
                    box = widget.bounding_box()
                    if box and box.get("width") and box.get("height"):
                        widget.click(
                            position={"x": box["width"] / 2, "y": box["height"] / 2},
                            timeout=3000,
                        )
                        logger.debug("Turnstile widget clicked url=%s", page.url)
            except Exception as exc:
                logger.debug("Turnstile widget click failed: %s", exc)
        if now >= next_checkbox_click:
            next_checkbox_click = now + 3.0
            try:
                for frame in page.frames:
                    if "challenges.cloudflare.com" in (frame.url or ""):
                        checkbox = frame.get_by_role("checkbox").first
                        if checkbox.count() and checkbox.is_visible():
                            checkbox.click(timeout=3000)
                            break
            except Exception:
                pass
        time.sleep(1.0)
    return _turnstile_token(page)


def _submit_turnstile_form(page, token: str = "") -> bool:
    """Fill/submit the form only after a token exists.

    Returns True when a form submission was initiated. The caller must wait for
    navigation and inspect the page again; this helper never guesses a link.
    The submit click is done with a Playwright locator, not inside evaluate(),
    because navigation would destroy the JS context and mask a successful POST.
    """
    try:
        page.eval_on_selector(
            TURNSTILE_RESPONSE_SELECTOR,
            "(el, token) => { if (token) el.value = token; }",
            token,
        )
        if not _turnstile_token(page):
            return False
        # Submit from a zero-delay task with the native prototype method.
        # Clicking the visible button is intercepted by the ad overlay and can
        # open a downloadgood.com popup; requestSubmit() ran no navigation here.
        page.eval_on_selector(
            TURNSTILE_RESPONSE_SELECTOR,
            """
            (el) => setTimeout(() => {
                const form = el.closest('form');
                if (!form) return;
                const button = form.querySelector('button[type="submit"], input[type="submit"]');
                if (button && form.requestSubmit) {
                    form.requestSubmit(button);
                    return;
                }
                if (button && button.name) {
                    const hidden = document.createElement('input');
                    hidden.type = 'hidden';
                    hidden.name = button.name;
                    hidden.value = button.value || '';
                    form.appendChild(hidden);
                }
                HTMLFormElement.prototype.submit.call(form);
            }, 0);
            """,
        )
        return True
    except Exception:
        return False


def _countdown_percent(text: str):
    """Return the shortener's scan percentage, or None on a normal page."""
    lowered = str(text or "").lower()
    if "link scan" not in lowered and "scaning" not in lowered:
        return None
    values = [int(v) for v in re.findall(r"(\d{1,3})\s*%", text or "")]
    return max(values) if values else 0


def _visible_controls(page) -> List[str]:
    texts: List[str] = []
    try:
        for selector in ("button", "a", "[role=button]", "input[type=submit]"):
            for element in page.query_selector_all(selector):
                try:
                    if element.is_visible():
                        text = str(element.inner_text() or "").strip()
                        if text:
                            texts.append(text)
                except Exception:
                    continue
    except Exception:
        return []
    return texts


def _click_label(page, label: str, occurrence: int = 0) -> bool:
    pattern = re.compile(r"^\s*" + re.escape(label) + r"\s*$", re.IGNORECASE)
    for role in ("button", "link"):
        try:
            locator = page.get_by_role(role, name=pattern)
            if occurrence < locator.count():
                candidate = locator.nth(occurrence)
                if candidate.is_visible() and candidate.is_enabled():
                    candidate.click(timeout=2000)
                    return True
        except Exception:
            continue
    # Shortener countdown controls are sometimes plain spans/divs with an
    # onclick handler rather than semantic buttons or links.
    if occurrence == 0:
        try:
            locator = page.get_by_text(label, exact=True).first
            if locator.count() and locator.is_visible() and locator.is_enabled():
                locator.click(timeout=2000)
                return True
        except Exception:
            pass
    return False


def _download_link_candidates(page) -> List[str]:
    """Return Get-Link/Download anchors that point at a known file host."""
    try:
        anchors = page.eval_on_selector_all(
            "a[href]",
            """
            els => els.map(e => ({
                text: (e.innerText || '').trim().toLowerCase(),
                href: e.href,
                cls: String(e.className || '').toLowerCase(),
            }))
            """,
        )
    except Exception:
        return []
    hrefs: List[str] = []
    for item in anchors or []:
        text = str(item.get("text") or "")
        href = str(item.get("href") or "")
        classes = str(item.get("cls") or "")
        if not href.lower().startswith(("http://", "https://")):
            continue
        if not (
            text.startswith("get link")
            or text.startswith("download")
            or "get-link" in classes
        ):
            continue
        host = urllib.parse.urlsplit(href).hostname or ""
        if any(host == allowed or host.endswith("." + allowed) for allowed in TRUSTED_DOWNLOAD_HOSTS):
            if href not in hrefs:
                hrefs.append(href)
        elif looks_like_file_url(href):
            if href not in hrefs:
                hrefs.append(href)
    return hrefs


def _find_file_href(page) -> str:
    try:
        hrefs = page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => e.href).filter(Boolean)",
        )
    except Exception:
        return ""
    for href in hrefs or []:
        if looks_like_file_url(str(href)):
            return str(href)
    return ""


def _is_book_bytes(data: bytes) -> bool:
    """Accept only ZIP-backed books (EPUB/CBZ) or PDFs, never installers."""
    return data.startswith((b"PK", b"%PDF"))


def _save_download(download, out_dir: str) -> FetchResult:
    suggested = str(download.suggested_filename or "")
    if os.path.splitext(suggested)[1].lower() in EXECUTABLE_SUFFIXES:
        raise FetchError(f"Downloaded file {suggested!r} is an executable, not an EPUB")
    filename = safe_filename(suggested or "novel.epub")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, filename)
    stem, suffix = os.path.splitext(path)
    counter = 2
    while os.path.exists(path):
        path = f"{stem} ({counter}){suffix}"
        counter += 1
    download.save_as(path)
    try:
        with open(path, "rb") as handle:
            magic = handle.read(8)
        if not _is_book_bytes(magic):
            raise FetchError(
                f"Downloaded file {os.path.basename(path)!r} is not an EPUB/PDF archive"
            )
    except FetchError:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return FetchResult(path=path, filename=os.path.basename(path))


def _download_via_request(context, url: str, out_dir: str) -> FetchResult:
    response = context.request.get(url, timeout=60000)
    if not response.ok:
        raise FetchError(f"File request failed with HTTP {response.status}")
    body = response.body()
    if not body:
        raise FetchError("File response was empty")
    if not _is_book_bytes(body):
        raise FetchError("File response is not an EPUB/PDF archive")
    header = response.headers.get("content-disposition", "")
    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", header, re.IGNORECASE)
    suggested = match.group(1) if match else os.path.basename(urllib.parse.urlsplit(url).path)
    if os.path.splitext(suggested)[1].lower() in EXECUTABLE_SUFFIXES:
        raise FetchError(f"File response {suggested!r} is an executable, not an EPUB")
    filename = safe_filename(suggested or "novel.epub")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, filename)
    stem, suffix = os.path.splitext(path)
    counter = 2
    while os.path.exists(path):
        path = f"{stem} ({counter}){suffix}"
        counter += 1
    with open(path, "wb") as handle:
        handle.write(body)
    return FetchResult(path=path, filename=os.path.basename(path))


def fetch(
    url: str,
    out_dir: str,
    *,
    flaresolverr_url: str = "",
    timeout: float = 150.0,
    headless: bool = True,
    user_agent: str = "",
    channel: str = "",
) -> FetchResult:
    """Resolve *url* through its shortener chain and save the final file.

    Raises :class:`FetchError` when Playwright is unavailable, FlareSolverr is
    required but not configured, or no download appears before *timeout*.
    """
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on deployment image
        raise FetchError(
            "Playwright is not installed; run this tool from the bundled Docker image "
            "or `pip install playwright && playwright install chromium`."
        ) from exc

    solver = FlareSolverrClient(flaresolverr_url)
    deadline = time.monotonic() + max(10.0, float(timeout))
    turnstile_wait = float(_env_int("EPUB_FETCHER_TURNSTILE_WAIT", 30, minimum=1))
    steps: List[str] = []
    target_url = str(url or "").strip()
    if not target_url.lower().startswith(("http://", "https://")):
        raise FetchError("url must be an absolute http(s) URL")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=headless,
            channel=channel or None,
            ignore_default_args=["--enable-automation"],
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        downloads: List[Any] = []
        clicked_counts: Dict[Tuple[str, str], int] = {}
        cleared_urls = set()
        manual_attempts = set()
        turnstile_submissions = set()
        delay_submissions = set()
        tried_download_links = set()
        consent_dismissed = False
        pages: List[Any] = []

        def attach(page) -> None:
            pages.append(page)

            def on_download(download) -> None:
                # Save only once, after the loop; Chromium can emit several
                # download events for one navigation to an attachment.
                downloads.append(download)

            page.on("download", on_download)

        def make_context(ua: str):
            options = {
                "accept_downloads": True,
                "viewport": {"width": 1366, "height": 900},
            }
            if ua:
                options["user_agent"] = ua
            ctx = browser.new_context(**options)
            ctx.on("page", attach)
            return ctx

        context_ua = user_agent
        context = make_context(context_ua)
        page = context.new_page()
        attach(page)
        solved = 0

        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
            while time.monotonic() < deadline:
                # A download can be emitted while its page closes; process it
                # before deciding that no live page remains.
                if downloads:
                    try:
                        result = _save_download(downloads[0], out_dir)
                        result.steps = steps
                        return result
                    except FetchError as exc:
                        # Fake download buttons often emit installers/HTML. Drop
                        # them and keep traversing; the real archive may follow.
                        steps.append(f"rejected-download:{str(exc)[:120]}")
                        downloads.pop(0)
                    except Exception:
                        downloads.pop(0)
                live_pages = [p for p in pages if not p.is_closed()]
                if page.is_closed():
                    if not live_pages:
                        # The POST can close the old tab just before the new
                        # destination tab is announced; give the context a
                        # short grace window instead of failing instantly.
                        for _ in range(10):
                            time.sleep(0.3)
                            live_pages = [p for p in pages if not p.is_closed()]
                            if live_pages:
                                break
                    if not live_pages:
                        if downloads:
                            continue
                        raise FetchError("Browser page closed before a download appeared")
                    page = live_pages[-1]
                    steps.append(f"page-switch:{page.url}")
                for candidate in list(pages):
                    try:
                        if candidate.is_closed():
                            continue
                        if candidate.url and looks_like_file_url(candidate.url):
                            try:
                                result = _download_via_request(context, candidate.url, out_dir)
                                result.steps = steps
                                return result
                            except FetchError:
                                pass
                    except Exception:
                        continue

                try:
                    page.wait_for_load_state("domcontentloaded", timeout=5000)
                except PlaywrightTimeoutError:
                    pass
                except Exception:
                    time.sleep(0.5)
                    continue

                body_text = ""
                try:
                    body_text = page.inner_text("body")[:8000]
                except Exception:
                    body_text = ""
                current_url = ""
                try:
                    current_url = page.url
                except Exception:
                    current_url = ""

                # Some link hiders gate a POST form behind a standalone
                # Turnstile. The destination is only valid after the widget
                # issues a token, so wait for it before submitting the form.
                if _has_turnstile_form(page) and current_url not in turnstile_submissions:
                    if not consent_dismissed:
                        consent_dismissed = _dismiss_consent_soon(page)
                        if consent_dismissed:
                            time.sleep(1.0)
                    wait_budget = min(turnstile_wait, max(1.0, deadline - time.monotonic()))
                    steps.append("turnstile-form-wait")
                    token = _wait_for_turnstile_token(page, wait_budget)
                    if token and _submit_turnstile_form(page, token):
                        turnstile_submissions.add(current_url)
                        cleared_urls.add(current_url)
                        steps.append("turnstile-form-submit")
                        time.sleep(2.0)
                        continue
                    steps.append("turnstile-token-missing")

                try:
                    page_html = page.content()
                except Exception:
                    page_html = ""
                challenge_markers = is_challenge_text(body_text) or "challenges.cloudflare.com" in page_html
                if challenge_markers and current_url not in cleared_urls:
                    if not consent_dismissed:
                        consent_dismissed = _dismiss_consent_soon(page)
                        if consent_dismissed:
                            time.sleep(1.0)
                    # A plain Turnstile widget often clears with a real checkbox
                    # click, but a click alone is not proof of clearance. Try it
                    # once per URL; if the challenge remains, fall through to
                    # FlareSolverr instead of permanently skipping the gate.
                    clicked_manual = False
                    if current_url not in manual_attempts:
                        for label in ("verify you are human", "i am human"):
                            if _click_label(page, label):
                                clicked_manual = True
                                break
                        if not clicked_manual:
                            try:
                                for frame in page.frames:
                                    if "challenges.cloudflare.com" in (frame.url or ""):
                                        checkbox = frame.get_by_role("checkbox").first
                                        if checkbox.count() and checkbox.is_visible():
                                            checkbox.click(timeout=3000)
                                            clicked_manual = True
                                            break
                            except Exception:
                                clicked_manual = False
                    if clicked_manual:
                        manual_attempts.add(current_url)
                        steps.append("turnstile-click")
                        time.sleep(2.0)
                        try:
                            current_url = page.url or current_url
                        except Exception:
                            pass
                        if not _challenge_visible(page):
                            cleared_urls.add(current_url)
                            continue
                        steps.append("turnstile-unconfirmed")
                    if not solver.enabled:
                        raise FetchError(
                            "Cloudflare/Turnstile challenge detected; configure a FlareSolverr URL"
                            f" (flow: {' > '.join(steps[-8:])})"
                        )
                    steps.append(f"flare-solve:{current_url}")
                    browser_cookies: List[Dict[str, Any]] = []
                    try:
                        browser_cookies = context.cookies([current_url])
                    except Exception:
                        browser_cookies = []
                    form_present = _has_turnstile_form(page)
                    session_id = ""
                    try:
                        if form_present and solver.tabs_till_verify > 0:
                            try:
                                session_id = solver.create_session()
                            except FetchError:
                                session_id = ""
                            if session_id:
                                # FlareSolverr applies request cookies *after*
                                # resolving Turnstile. Warm the session first so
                                # the tab solve sees the cookie-gated form, then
                                # solve in that same browser session.
                                steps.append("flare-session-warm")
                                try:
                                    solver.request_get(
                                        current_url,
                                        cookies=browser_cookies,
                                        session=session_id,
                                        include_tabs=False,
                                    )
                                    solution = solver.request_get(current_url, session=session_id)
                                except FetchError:
                                    # A stale/unsupported session must not leave
                                    # the fetch without the single-request path.
                                    steps.append("flare-session-fallback")
                                    solution = solver.request_get(
                                        current_url, cookies=browser_cookies
                                    )
                            else:
                                solution = solver.request_get(current_url, cookies=browser_cookies)
                        else:
                            solution = solver.request_get(current_url, cookies=browser_cookies)
                    finally:
                        solver.destroy_session(session_id)
                    cookies = FlareSolverrClient.cookies_for_context(solution)
                    solution_url = str(solution.get("url") or current_url)
                    solution_ua = str(solution.get("userAgent") or context_ua)
                    solution_token = str(solution.get("turnstile_token") or "")
                    steps.append(f"flare-token:{len(solution_token)}")

                    # A Turnstile form needs the token from the solver; submit it
                    # explicitly because the site's auto-submit hook may target a
                    # missing element id.
                    if solution_token and _has_turnstile_form(page):
                        if cookies:
                            try:
                                context.add_cookies(cookies)
                            except Exception:
                                pass
                        if _submit_turnstile_form(page, solution_token):
                            cleared_urls.add(current_url)
                            turnstile_submissions.add(current_url)
                            steps.append("flare-turnstile-form-submit")
                            time.sleep(2.0)
                            continue
                        steps.append("turnstile-token-injection-failed")
                    if _has_turnstile_form(page) and not solution_token and solution_url == current_url:
                        raise FetchError(
                            "Turnstile form did not issue a token; the source may be an ad or expired shortener"
                            f" (flow: {' > '.join(steps[-8:])})"
                        )

                    # The challenge frame/text can outlive a successful solve.
                    # Mark both URLs so the cleared page reaches its controls.
                    cleared_urls.add(current_url)
                    cleared_urls.add(solution_url)

                    # cf_clearance is UA-bound: rebuild the context with the UA
                    # that FlareSolverr used instead of only copying cookies.
                    if solution_ua and solution_ua != context_ua:
                        old_context = context
                        old_context_ua = context_ua
                        try:
                            context = make_context(solution_ua)
                            context_ua = solution_ua
                            page = context.new_page()
                            attach(page)
                        except Exception:
                            context = old_context
                            context_ua = old_context_ua
                        else:
                            try:
                                old_context.close()
                            except Exception:
                                pass
                    if solution.get("userAgent") and context_ua != str(solution["userAgent"]):
                        try:
                            context.set_extra_http_headers({"User-Agent": solution_ua})
                        except Exception:
                            pass
                    if cookies:
                        try:
                            context.add_cookies(cookies)
                        except Exception:
                            pass
                    solved += 1
                    if solved > 4:
                        raise FetchError("Cloudflare challenge did not clear after FlareSolverr")
                    try:
                        page.goto(solution_url, wait_until="domcontentloaded", timeout=45000)
                    except PlaywrightTimeoutError:
                        pass
                    time.sleep(1.0)
                    continue

                for direct_href in _download_link_candidates(page):
                    if direct_href in tried_download_links:
                        continue
                    tried_download_links.add(direct_href)
                    steps.append(f"direct-download:{urllib.parse.urlsplit(direct_href).hostname}")
                    try:
                        result = _download_via_request(context, direct_href, out_dir)
                        result.steps = steps
                        return result
                    except FetchError as exc:
                        steps.append(f"direct-rejected:{str(exc)[:100]}")

                href = _find_file_href(page)
                if href:
                    steps.append(f"file-link:{href}")
                    try:
                        result = _download_via_request(context, href, out_dir)
                        result.steps = steps
                        return result
                    except FetchError:
                        try:
                            with page.expect_download(timeout=15000) as download_info:
                                page.goto(href, wait_until="domcontentloaded")
                            download = download_info.value
                            result = _save_download(download, out_dir)
                            result.steps = steps
                            return result
                        except Exception:
                            pass

                # Cookie banners ("Got it!") can cover the Get Link control.
                if _dismiss_consent(page):
                    time.sleep(0.5)
                    continue
                labels = candidate_click_labels(_visible_controls(page))
                countdown_pct = _countdown_percent(body_text)
                if (
                    countdown_pct is not None
                    and countdown_pct >= 100
                    and current_url not in delay_submissions
                    and page.query_selector("input[name='delay_token']")
                ):
                    try:
                        page.eval_on_selector(
                            "input[name='delay_token']",
                            """
                            (el) => setTimeout(() => {
                                const form = el.closest('form');
                                const button = form && form.querySelector('button[type="submit"]');
                                if (form) form.requestSubmit(button);
                            }, 0);
                            """,
                        )
                        delay_submissions.add(current_url)
                        steps.append("countdown-submit")
                        time.sleep(2.0)
                        continue
                    except Exception:
                        pass
                logger.debug(
                    "loop url=%s closed=%s labels=%s pct=%s pages=%s body=%r",
                    current_url, page.is_closed(), labels, countdown_pct,
                    [(p.url, p.is_closed()) for p in pages],
                    body_text[:250],
                )
                clicked = False
                for label in labels:
                    if label == "continue" and countdown_pct is not None and countdown_pct < 100:
                        wait_step = f"countdown-wait:{countdown_pct}%"
                        if not steps or steps[-1] != wait_step:
                            steps.append(wait_step)
                        break
                    key = (page.url, label)
                    occurrence = clicked_counts.get(key, 0)
                    try:
                        with page.expect_download(timeout=2500) as download_info:
                            if not _click_label(page, label, occurrence):
                                continue
                            clicked = True
                            clicked_counts[key] = occurrence + 1
                        download = download_info.value
                        if not downloads:
                            downloads.append(download)
                        steps.append(f"click:{label}#{occurrence}")
                        break
                    except PlaywrightTimeoutError:
                        # No download from this click: the page advanced instead.
                        if clicked:
                            clicked_counts[key] = occurrence + 1
                            steps.append(f"click:{label}#{occurrence}")
                            break
                    except Exception:
                        continue
                if not clicked:
                    time.sleep(1.0)
        except FetchError:
            raise
        except Exception as exc:
            raise FetchError(
                f"Browser flow failed: {type(exc).__name__}: {exc}"
                f" (flow: {' > '.join(steps[-8:])})"
            ) from exc
        finally:
            try:
                context.close()
            finally:
                browser.close()

    raise FetchError(
        f"No file download appeared within {int(timeout)}s"
        f" (flow: {' > '.join(steps[-8:])})"
    )


def fetch_to_json(url: str, out_dir: str, flaresolverr_url: str = "", timeout: float = 150.0) -> Dict[str, Any]:
    result = fetch(url, out_dir, flaresolverr_url=flaresolverr_url, timeout=timeout)
    with open(result.path, "rb") as handle:
        content = handle.read()
    return {
        "status": "success",
        "filename": result.filename,
        "bytes": len(content),
        "content_b64": base64.b64encode(content).decode("ascii"),
        "final_url": result.final_url,
        "steps": result.steps,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve a shortener/hider chain and fetch the EPUB")
    parser.add_argument("url", help="source link (shortener/hider URL)")
    parser.add_argument("--out", default=os.getenv("EPUB_FETCHER_OUTPUT_DIR", os.getcwd()))
    parser.add_argument("--flaresolverr", default=os.getenv("EPUB_FETCHER_FLARESOLVERR_URL", ""))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("EPUB_FETCHER_TIMEOUT", "150")))
    parser.add_argument("--show", action="store_true", help="run a visible browser")
    args = parser.parse_args(argv)
    try:
        result = fetch(
            args.url, args.out, flaresolverr_url=args.flaresolverr,
            timeout=args.timeout, headless=not args.show,
        )
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"status": "success", "path": result.path, "filename": result.filename, "steps": result.steps}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
