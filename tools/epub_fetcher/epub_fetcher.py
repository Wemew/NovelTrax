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

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

#: Buttons/links that advance the interstitial chain, highest priority first.
CLICK_LABELS: Tuple[str, ...] = (
    "get link", "getlink", "download now", "download", "continue", "proceed",
    "next", "click here", "verify you are human", "i am human",
)
#: Anchors/URL suffixes that mean "this is already the file".
FILE_SUFFIXES: Tuple[str, ...] = (".epub", ".pdf", ".cbz", ".zip", ".mobi", ".azw3")
TURNSTILE_RESPONSE_SELECTOR = "input[name='cf-turnstile-response']"
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
        return bool(page.evaluate(
            """
            () => {
                const labels = ['Consent', 'Accept all', 'Accept', 'I agree'];
                for (const button of document.querySelectorAll('button')) {
                    if (labels.includes((button.innerText || '').trim())) {
                        button.click();
                        return true;
                    }
                }
                return false;
            }
            """
        ))
    except Exception:
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
    clicked = False
    while time.monotonic() < deadline:
        token = _turnstile_token(page)
        if token:
            return token
        if not clicked:
            clicked = True
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
    """
    try:
        return bool(page.evaluate(
            """
            (args) => {
                const input = document.querySelector(args.selector);
                if (!input) return false;
                if (args.token) input.value = args.token;
                if (!input.value) return false;
                const form = input.closest('form');
                if (!form) return false;
                const button = form.querySelector('button[type="submit"], input[type="submit"], button');
                if (button) { button.click(); return true; }
                if (form.requestSubmit) { form.requestSubmit(); return true; }
                form.submit();
                return true;
            }
            """,
            {"selector": TURNSTILE_RESPONSE_SELECTOR, "token": token},
        ))
    except Exception:
        return False


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


def _click_label(page, label: str) -> bool:
    pattern = re.compile(r"^\s*" + re.escape(label) + r"\s*$", re.IGNORECASE)
    for role in ("button", "link"):
        try:
            locator = page.get_by_role(role, name=pattern).first
            if locator.count() and locator.is_visible() and locator.is_enabled():
                locator.click(timeout=2000)
                return True
        except Exception:
            continue
    return False


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


def _save_download(download, out_dir: str) -> FetchResult:
    filename = safe_filename(download.suggested_filename or "novel.epub")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, filename)
    stem, suffix = os.path.splitext(path)
    counter = 2
    while os.path.exists(path):
        path = f"{stem} ({counter}){suffix}"
        counter += 1
    download.save_as(path)
    return FetchResult(path=path, filename=os.path.basename(path))


def _download_via_request(context, url: str, out_dir: str) -> FetchResult:
    response = context.request.get(url, timeout=60000)
    if not response.ok:
        raise FetchError(f"File request failed with HTTP {response.status}")
    body = response.body()
    if not body:
        raise FetchError("File response was empty")
    header = response.headers.get("content-disposition", "")
    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", header, re.IGNORECASE)
    suggested = match.group(1) if match else os.path.basename(urllib.parse.urlsplit(url).path)
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
    user_agent: str = DEFAULT_USER_AGENT,
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
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        downloads: List[Any] = []
        clicked_keys = set()
        cleared_urls = set()
        manual_attempts = set()
        turnstile_submissions = set()
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
            ctx = browser.new_context(
                accept_downloads=True,
                user_agent=ua,
                viewport={"width": 1366, "height": 900},
            )
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
                if downloads:
                    try:
                        result = _save_download(downloads[0], out_dir)
                        result.steps = steps
                        return result
                    except Exception:
                        downloads.pop(0)
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
                        consent_dismissed = _dismiss_consent(page)
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

                challenge_markers = is_challenge_text(body_text) or "challenges.cloudflare.com" in page.content()
                if challenge_markers and current_url not in cleared_urls:
                    if not consent_dismissed:
                        consent_dismissed = _dismiss_consent(page)
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
                                solver.request_get(
                                    current_url,
                                    cookies=browser_cookies,
                                    session=session_id,
                                    include_tabs=False,
                                )
                                solution = solver.request_get(current_url, session=session_id)
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

                labels = candidate_click_labels(_visible_controls(page))
                clicked = False
                for label in labels:
                    key = (page.url, label)
                    if key in clicked_keys:
                        continue
                    try:
                        with page.expect_download(timeout=2500) as download_info:
                            if not _click_label(page, label):
                                continue
                            clicked = True
                            clicked_keys.add(key)
                        download = download_info.value
                        if not downloads:
                            downloads.append(download)
                        steps.append(f"click:{label}")
                        break
                    except PlaywrightTimeoutError:
                        # No download from this click: the page advanced instead.
                        if clicked:
                            clicked_keys.add(key)
                            steps.append(f"click:{label}")
                            break
                    except Exception:
                        continue
                if not clicked:
                    time.sleep(1.0)
        finally:
            try:
                context.close()
            finally:
                browser.close()

    raise FetchError(f"No file download appeared within {int(timeout)}s")


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
