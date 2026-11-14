#!/bin/sh
set -u

if [ "${EPUB_FETCHER_HEADLESS:-false}" = "true" ]; then
    echo "Starting uvicorn in headless mode"
    exec uvicorn server:app --host 0.0.0.0 --port 8010
fi

mkdir -p /tmp/.X11-unix
export DISPLAY=:99
Xvfb :99 -screen 0 1366x900x24 -nolisten tcp 2>&1 &
XVFB_PID=$!
trap 'kill "$XVFB_PID" 2>/dev/null || true' EXIT INT TERM

for _ in $(seq 1 50); do
    [ -S /tmp/.X11-unix/X99 ] && break
    sleep 0.1
done

if [ ! -S /tmp/.X11-unix/X99 ]; then
    echo "Xvfb did not start; falling back to headless mode" >&2
    export EPUB_FETCHER_HEADLESS=true
    exec uvicorn server:app --host 0.0.0.0 --port 8010
fi

echo "Starting uvicorn with DISPLAY=$DISPLAY"
exec uvicorn server:app --host 0.0.0.0 --port 8010
