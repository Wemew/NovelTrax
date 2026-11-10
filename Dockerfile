FROM python:3.11-slim

# Prevent Python from writing .pyc files and enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8080
ENV CALIBRE_DIR=/calibre
ENV DATA_DIR=/app/data
# Local folder the TorBox debrid engine fetches finished novel files into.
ENV DOWNLOAD_DIR=/downloads

WORKDIR /app

# Install curl and ca-certificates for secure HTTPS feeds
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create non-root unprivileged user (UID/GID 1000) for defense-in-depth
RUN groupadd -g 1000 appuser && \
    useradd -u 1000 -g appuser -s /bin/sh -m appuser

# Copy and install dependencies
COPY backend/requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY backend/ /app/

# Create mount points and ensure permissions for any unprivileged UID (e.g. 1000 or TrueNAS 568)
# /downloads is the local fetch target for the TorBox engine (DOWNLOAD_DIR); it must be
# writable by the app user, and a mounted volume must keep those permissions.
RUN mkdir -p /calibre /app/data /downloads && \
    chown -R appuser:appuser /app /calibre /downloads && \
    chmod -R 755 /app && \
    chmod 777 /app/data /calibre /downloads

USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/ || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
