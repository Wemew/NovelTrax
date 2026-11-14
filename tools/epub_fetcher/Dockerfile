# Playwright's image ships Chromium and its OS dependencies.
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY epub_fetcher.py server.py ./
ENV EPUB_FETCHER_OUTPUT_DIR=/tmp/epub_fetcher \
    EPUB_FETCHER_TIMEOUT=150
EXPOSE 8010
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8010"]
