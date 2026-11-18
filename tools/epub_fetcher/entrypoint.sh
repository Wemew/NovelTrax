#!/bin/sh
set -u

# Match server.py's truthy parsing instead of only accepting lowercase "true".
case "$(printf '%s' "${EPUB_FETCHER_HEADLESS:-false}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on)
        echo "Starting uvicorn in headless mode"
        exec uvicorn server:app --host 0.0.0.0 --port 8010
        ;;
esac

DISPLAY_NUM=99
X_LOCK="/tmp/.X${DISPLAY_NUM}-lock"
X_SOCKET="/tmp/.X11-unix/X${DISPLAY_NUM}"

mkdir -p /tmp/.X11-unix

# A container restart keeps /tmp, so a previous Xvfb can leave a lock and a
# socket behind.  Xvfb then exits ("server is already active") while the stale
# socket makes the readiness check below pass, and every Chromium launch fails
# with "Missing X server or $DISPLAY".  Remove the artifacts only when the PID
# recorded in the lock is gone (or the lock is not an Xvfb lock).
if [ -e "$X_LOCK" ]; then
    LOCK_PID="$(tr -cd '0-9' < "$X_LOCK" 2>/dev/null || true)"
    if [ -z "$LOCK_PID" ] || ! kill -0 "$LOCK_PID" 2>/dev/null \
        || [ "$(cat "/proc/$LOCK_PID/comm" 2>/dev/null || true)" != "Xvfb" ]; then
        echo "Removing stale X display $DISPLAY_NUM lock/socket" >&2
        rm -f "$X_LOCK" "$X_SOCKET"
    fi
elif [ -S "$X_SOCKET" ]; then
    # Socket without a lock cannot belong to a healthy Xvfb.
    rm -f "$X_SOCKET"
fi

export DISPLAY=":$DISPLAY_NUM"
Xvfb ":$DISPLAY_NUM" -screen 0 1366x900x24 -nolisten tcp 2>&1 &
XVFB_PID=$!
trap 'kill "$XVFB_PID" 2>/dev/null || true' EXIT INT TERM

READY=0
for _ in $(seq 1 50); do
    # Check the process first: a stale socket must not mask an Xvfb that died
    # (for example because another server owns the display).
    if ! kill -0 "$XVFB_PID" 2>/dev/null; then
        break
    fi
    if [ -S "$X_SOCKET" ]; then
        READY=1
        break
    fi
    sleep 0.1
done

if [ "$READY" -eq 1 ] && command -v xdpyinfo >/dev/null 2>&1; then
    xdpyinfo -display ":$DISPLAY_NUM" >/dev/null 2>&1 || READY=0
fi

if [ "$READY" -ne 1 ] || ! kill -0 "$XVFB_PID" 2>/dev/null; then
    echo "Xvfb did not start; falling back to headless mode" >&2
    kill "$XVFB_PID" 2>/dev/null || true
    wait "$XVFB_PID" 2>/dev/null || true
    export EPUB_FETCHER_HEADLESS=true
    exec uvicorn server:app --host 0.0.0.0 --port 8010
fi

echo "Starting uvicorn with DISPLAY=$DISPLAY"
exec uvicorn server:app --host 0.0.0.0 --port 8010
