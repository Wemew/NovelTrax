"""Pure, stdlib-only derivation of a release's download state.

The download engine (qBittorrent or TorBox) persists its bookkeeping on the
``releases`` row.  Templates must not re-implement the derivation rules: every
release-dict producer in ``app.database`` attaches the result of
:func:`release_download_state` as ``item["download_state"]`` and the UI/API read
that single object.

Contract (TorBox port §3.3) — derivation order, first match wins:

=====================================  ==============  ===============
condition                              state           label
=====================================  ==============  ===============
``download_status == 'FAILED'`` or      ``failed``      ``Failed``
``status == 'FAILED'``
``status == 'IN_CALIBRE'``              ``in_calibre``  ``In Calibre``
``download_status == 'COMPLETED'``      ``downloaded``  ``Downloaded``
``download_status == 'DOWNLOADING'``    ``downloading`` ``Downloading``
otherwise                              ``not_downloaded`` ``Not downloaded``
=====================================  ==============  ===============

No database, network, third-party imports or logging: this module is imported
by the database layer itself and runs in the bundled stdlib-only interpreter.
"""

import json
from typing import Any, Dict


# States from which the "Download" action may be offered again.
DOWNLOADABLE_STATES = frozenset(("failed", "not_downloaded"))
# States that mean "the release is terminal; do not download again".
TERMINAL_STATES = frozenset(("in_calibre",))


def _coerce_progress(raw: Any) -> Dict[str, Any]:
    """Return ``torbox_progress_json`` as a dict (never raises)."""
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    # JSON may store NaN/inf via a hand-edited row; never surface those to the UI.
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return number


def _coerce_int(value: Any, default: int = 0) -> int:
    return int(_coerce_float(value, float(default)))


def release_download_state(release: Dict[str, Any]) -> Dict[str, Any]:
    """Derive the single source of truth for a release download-state chip.

    Returns ``{release_id, state, label, progress_pct, speed_bps, error,
    can_download, can_retry, terminal, engine}``.  Pure: the input mapping is
    never mutated.
    """
    item = release if isinstance(release, dict) else {}
    progress = _coerce_progress(item.get("torbox_progress_json"))

    download_status = str(item.get("download_status") or "").strip().upper()
    release_status = str(item.get("status") or "").strip().upper()

    if download_status == "FAILED" or release_status == "FAILED":
        state, label = "failed", "Failed"
    elif release_status == "IN_CALIBRE":
        state, label = "in_calibre", "In Calibre"
    elif download_status == "COMPLETED":
        state, label = "downloaded", "Downloaded"
    elif download_status == "DOWNLOADING":
        state, label = "downloading", "Downloading"
    else:
        state, label = "not_downloaded", "Not downloaded"

    progress_pct = _coerce_float(progress.get("progress_pct"), 0.0)
    progress_pct = max(0.0, min(100.0, progress_pct))
    speed_bps = max(0, _coerce_int(progress.get("speed_bps"), 0))
    error = str(item.get("download_error") or "").strip()

    release_id = item.get("id", item.get("release_id"))
    engine = str(item.get("downloader") or "").strip().lower()

    return {
        "release_id": release_id,
        "state": state,
        "label": label,
        "progress_pct": progress_pct,
        "speed_bps": speed_bps,
        "error": error,
        "can_download": state in DOWNLOADABLE_STATES,
        "can_retry": state == "failed",
        "terminal": state in TERMINAL_STATES,
        "engine": engine,
    }
