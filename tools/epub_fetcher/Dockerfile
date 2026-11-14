# Playwright's image ships Chromium and its OS dependencies.
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

# Headful Chromium under Xvfb: Cloudflare's managed Turnstile refuses to issue
# a token to headless Chromium, but passes in a real display context.
RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb xauth \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY epub_fetcher.py server.py entrypoint.sh ./
RUN chmod +x /app/entrypoint.sh
ENV EPUB_FETCHER_OUTPUT_DIR=/tmp/epub_fetcher \
    EPUB_FETCHER_TIMEOUT=150 \
    EPUB_FETCHER_HEADLESS=false \
    EPUB_FETCHER_FLARESOLVERR_TABS=1 \
    EPUB_FETCHER_MAX_CONCURRENCY=1
EXPOSE 8010
CMD ["/app/entrypoint.sh"]
