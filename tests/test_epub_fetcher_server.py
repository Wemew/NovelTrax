"""EPUB fetcher sidecar: environment failures are retryable 503s, not 500s."""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BASE = os.path.dirname(_HERE)
_TOOL = os.path.join(_BASE, "tools", "epub_fetcher")
for _p in (_TOOL, _BASE, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ["EPUB_FETCHER_HEADLESS"] = "false"
os.environ.pop("DISPLAY", None)

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402
from epub_fetcher import FetchError  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    client = TestClient(server.app)

    health_response = client.get("/health")
    health = health_response.json()
    check(health_response.status_code == 503,
          f"dead display returns 503 from /health, got {health_response.status_code}")
    check(health["display_ready"] is False,
          "headful mode without DISPLAY reports display_ready=false")
    check(health["status"] == "degraded",
          "missing display degrades health instead of claiming ok")

    import types
    fake_playwright = types.ModuleType("playwright")
    previous_playwright = sys.modules.get("playwright")
    previous_headless = server.HEADLESS
    sys.modules["playwright"] = fake_playwright
    server.HEADLESS = True
    try:
        healthy_response = client.get("/health")
        check(healthy_response.status_code == 200,
              f"healthy fetcher returns 200, got {healthy_response.status_code}")
        check(healthy_response.json()["status"] == "ok", healthy_response.text)
    finally:
        server.HEADLESS = previous_headless
        if previous_playwright is None:
            sys.modules.pop("playwright", None)
        else:
            sys.modules["playwright"] = previous_playwright

    def boom(*_args, **_kwargs):
        raise RuntimeError("BrowserType.launch: Missing X server or $DISPLAY")

    original = server.fetch
    try:
        server.fetch = boom
        response = client.post("/fetch", json={"url": "https://example.test/book"})
        check(response.status_code == 503,
              f"browser/display crash returns 503, got {response.status_code}")
        check("Missing X server" in response.json().get("detail", ""),
              "503 detail preserves the environment failure")

        def flow_error(*_args, **_kwargs):
            raise FetchError("No download control found")

        server.fetch = flow_error
        response = client.post("/fetch", json={"url": "https://example.test/book"})
        check(response.status_code == 502,
              f"source/flow failure stays 502, got {response.status_code}")
    finally:
        server.fetch = original
    print("epub fetcher server PASSED: display health, 503 environment, 502 flow")


if __name__ == "__main__":
    main()
