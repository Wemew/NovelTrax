# EPUB fetcher

Resolves the link-hider chain behind a release's **Source** link and downloads
the final EPUB. NovelTrax calls it over HTTP; it can also be used standalone.

## Chain

1. `POST /fetch` with the source URL (a shortener such as `vplink.cc`, then
   `zshort.link`/`charexempire`/turnstile gate).
2. If the page is a Cloudflare/Turnstile challenge, FlareSolverr
   (`request.get`) returns the cleared URL and cookies; Playwright adopts them.
3. Standalone Turnstile forms are handled explicitly: dismiss the consent
   overlay, then warm a FlareSolverr **session** with the page cookies before
   asking it to solve the widget. This is required because FlareSolverr applies
   request cookies only *after* its Turnstile routine; without the warm-up it
   would solve the cookie-less homepage and never see the form. Once a real
   `cf-turnstile-response` token exists, the form is submitted explicitly. A
   browser click is never treated as proof that the widget passed.
4. Playwright follows countdown pages, clicks visible `Get Link`/`Download`/
   `Continue` controls, and captures the browser download (also handles a
   direct `*.epub` anchor).
5. The file is returned with its original filename. If no token/download is
   produced, the service returns a clear 502 instead of waiting forever.

Ads/popups are ignored: only the tab that emits a download is used, and each
clicks is bounded by a timeout so a non-navigating button cannot stall the
loop.

## Run

```bash
docker build -t noveltrax-epub-fetcher tools/epub_fetcher
docker run --rm -p 127.0.0.1:8010:8010 \
  -e EPUB_FETCHER_FLARESOLVERR_URL=http://flaresolverr:8191 \
  noveltrax-epub-fetcher
```

`docker-compose.yml` service:

```yaml
epub-fetcher:
  build: ./tools/epub_fetcher
  environment:
    EPUB_FETCHER_FLARESOLVERR_URL: http://flaresolverr:8191
    # Required for sites whose standalone Turnstile only passes via keyboard focus.
    EPUB_FETCHER_FLARESOLVERR_TABS: "1"
    # Post-solve wait so the challenged page can swap in its download controls.
    EPUB_FETCHER_FLARESOLVERR_WAIT: "2"
    # Wait for the page's Turnstile form to issue a token before FlareSolverr.
    EPUB_FETCHER_TURNSTILE_WAIT: "30"
  expose: ["8010"]
```

FlareSolverr is still required for Cloudflare/Turnstile; without it a challenge
fails with a clear error instead of hanging. A solved URL is remembered, so a
residual Turnstile iframe or "verify you are human" banner no longer re-triggers
the gate before the page's Get Link/Continue controls are clicked. The context
is rebuilt with FlareSolverr's User-Agent because `cf_clearance` is UA-bound.

Standalone CLI (same image):

```bash
docker run --rm noveltrax-epub-fetcher \
  python epub_fetcher.py "https://vplink.cc/..." --out /tmp/books
```

Add `--show` for a visible browser only when running outside the container.

## NovelTrax settings

Set **Settings → Downloader → EPUB Fetcher URL** to `http://epub-fetcher:8010`
(or `http://localhost:8010` for a local run). The per-release **Fetch EPUB**
button downloads into `downloads_dir` and records the release as
`COMPLETED` (engine `http`) with the file path in `download_files_json`.

## Security

The service fetches whatever URL it is given and returns the bytes; bind it to
the internal Docker network or loopback only, never expose port 8010 publicly.

## Honest limits

- Playwright drives a real Chromium; selector/label heuristics can break when a
  shortener changes its UI. `/health` reports whether the browser is available,
  and failures return the last flow steps in `X-Fetch-Steps`.
- No CAPTCHA billing service is used: interactive captchas (not Turnstile) still
  block the flow by design.
- A Turnstile widget can refuse to issue a token (expired/blocked shortener);
  NovelTrax then records the release as `FAILED` with the returned error.
- The file is fetched over the browser/HTTP client, so the fetcher must be able
  to reach the CDN host; it does not bypass DRM or paywalls.
