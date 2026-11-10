import logging
from typing import List, Dict, Any, Optional, Tuple
from app.parser import (
    normalize_text,
    clean_feed_title,
    match_series_in_calibre,
    string_similarity
)

logger = logging.getLogger("noveltrax.calibre.matcher")


class CalibreMatcher:
    """
    Matches feed releases against Calibre books and series.
    Provides fast lookup caches to process hundreds of releases in milliseconds.
    """

    def __init__(
        self,
        calibre_series: List[Dict[str, Any]],
        calibre_books: List[Dict[str, Any]],
        series_aliases: Optional[Dict[str, str]] = None,
    ):
        self.calibre_series = calibre_series
        self.calibre_books = calibre_books
        # Optional normalized-alias -> canonical (tracked/Calibre) name map. Built
        # by the scheduler from alias overrides + AniList titles; resolved before
        # any title/series matching so a release stored under a display title can
        # still match the stable Calibre name.
        self.series_aliases = series_aliases or {}

        # One reusable series index for the whole library; rebuilding it per
        # release would re-normalize every Calibre name on every feed item.
        self.series_index = None
        try:
            from app.series_identity import CalibreSeriesIndex
            self.series_index = CalibreSeriesIndex(calibre_series)
        except Exception as exc:  # pragma: no cover - identity module always ships
            logger.debug("CalibreSeriesIndex unavailable; series matching degrades: %s", exc)

        # Pre-build lookup index for quick book matching:
        # 1. By series_id + rounded volume index: (series_id, round(vol, 2)) -> book
        self.series_vol_index: Dict[Tuple[int, float], Dict[str, Any]] = {}
        # 2. By normalized series name + volume index
        self.series_name_vol_index: Dict[Tuple[str, float], Dict[str, Any]] = {}
        # 3. By normalized clean title
        self.clean_title_index: Dict[str, Dict[str, Any]] = {}
        # 4. First / lowest indexed book per series: series_id -> book
        self.series_first_book: Dict[int, Dict[str, Any]] = {}

        for b in calibre_books:
            norm_title = normalize_text(clean_feed_title(b.get("title") or ""))
            if norm_title:
                self.clean_title_index[norm_title] = b

            ser_id = b.get("series_id")
            ser_name = b.get("series_name")
            vol = b.get("series_index")

            if vol is not None:
                vol_key = round(float(vol), 2)
                if ser_id:
                    self.series_vol_index[(ser_id, vol_key)] = b
                    if ser_id not in self.series_first_book or vol_key < self.series_first_book[ser_id].get("series_index", 9999):
                        self.series_first_book[ser_id] = b
                if ser_name:
                    norm_ser_name = normalize_text(ser_name)
                    self.series_name_vol_index[(norm_ser_name, vol_key)] = b

    def _resolve_series_alias(self, series_name: Optional[str]) -> Optional[str]:
        """Maps a stored series spelling to its canonical tracked/Calibre name."""
        if not series_name or not self.series_aliases:
            return None
        return self.series_aliases.get(normalize_text(series_name))

    @staticmethod
    def _canonical_series_name(series: Dict[str, Any]) -> str:
        return str(series.get("series_name") or series.get("name") or "").strip()

    @staticmethod
    def _with_series_identity(
        result: Dict[str, Any],
        matched_series: Optional[Dict[str, Any]],
        resolved_series_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Adds the canonical-series keys the scheduler relies on.

        ``matched_series`` is copied (never mutated) so the Calibre scanner
        snapshot shared by the matcher stays read-only. Existing callers that
        only read ``status``/``matched_book_id``/``matched_series`` are
        unaffected because every legacy key keeps its old value.
        """
        if matched_series is not None:
            series_copy = dict(matched_series)
            canonical = CalibreMatcher._canonical_series_name(series_copy)
            if canonical:
                series_copy["series_name"] = canonical
            result["matched_series"] = series_copy
            result["matched_series_name"] = canonical or None
            result["canonical_series_name"] = canonical or None
        else:
            result["matched_series_name"] = None
            result["canonical_series_name"] = None
        if resolved_series_name:
            result["resolved_series_name"] = resolved_series_name
        return result

    def check_release(
        self,
        release: Dict[str, Any],
        auto_request_monitored: bool = True,
        alt_series_names: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Determines if a release is already in Calibre or needs to be requested.
        Handles Volume 1 releases (which typically lack volume numbers on J-Novels).
        Returns dict with:
        {
            "status": "IN_CALIBRE" | "REQUESTED" | "IGNORED" | "NEW",
            "matched_book_id": Optional[int],
            "matched_series": Optional[Dict],
            "effective_volume": float,
            "missing_volumes": List[float],
            "reason": str,
            # additive series-identity keys when a series matched:
            "matched_series_name": Optional[str],
            "canonical_series_name": Optional[str],
        }
        """
        series_name = release.get("series_name")
        volume = release.get("volume")
        title = release.get("title", "")
        clean_title = clean_feed_title(title)

        # Check official translation policy: If release is tagged as unofficial, ignore it from tracking
        is_official = release.get("is_official")
        if is_official is not None and int(is_official) == 0:
            return {
                "status": "IGNORED",
                "matched_book_id": None,
                "matched_series": None,
                "effective_volume": float(volume) if volume is not None else 1.0,
                "missing_volumes": [],
                "reason": "Unofficial or web novel fan translation ignored"
            }

        # 1. Check title match directly
        norm_title = normalize_text(clean_title)
        if norm_title in self.clean_title_index:
            matched_book = self.clean_title_index[norm_title]
            return {
                "status": "IN_CALIBRE",
                "matched_book_id": matched_book["book_id"],
                "matched_series": None,
                "effective_volume": volume or matched_book.get("series_index", 1.0),
                "missing_volumes": [],
                "reason": f"Title matched Calibre book #{matched_book['book_id']} ('{matched_book['title']}')"
            }

        # 2. Check series + volume match. An alias override (tracked spelling or
        # AniList title) resolves to the canonical name before matching.
        matched_series = None
        resolved_series_name = series_name
        if series_name:
            alias_target = self._resolve_series_alias(series_name)
            if alias_target:
                resolved_series_name = alias_target
            matched_series = match_series_in_calibre(
                resolved_series_name,
                self.calibre_series,
                alt_names=alt_series_names,
                index=self.series_index,
            )

        # On J-Novels, Volume 1 releases usually do not have the volume moniker in the title
        # (e.g. "Overlord Epub", "Solo Leveling Epub").
        # If unnumbered, treat effective_volume as 1.0 for series tracking against Calibre.
        effective_volume = float(volume) if volume is not None else 1.0
        is_explicit_vol = (volume is not None)

        volumes = release.get("volumes") or [effective_volume]
        is_bundle = release.get("is_bundle", False) or len(volumes) > 1

        if matched_series:
            matched_series = self._with_series_identity({}, matched_series, resolved_series_name)["matched_series"]
            ser_id = matched_series["series_id"]
            canonical_name = self._canonical_series_name(matched_series)

            if is_bundle and len(volumes) > 1:
                owned_vols = [v for v in volumes if (ser_id, round(float(v), 2)) in self.series_vol_index]
                missing_vols = [v for v in volumes if v not in owned_vols]

                if not missing_vols:
                    return self._with_series_identity({
                        "status": "IN_CALIBRE",
                        "matched_book_id": None,
                        "matched_series": matched_series,
                        "effective_volume": effective_volume,
                        "missing_volumes": [],
                        "reason": f"All {len(volumes)} volumes in bundle for '{matched_series['series_name']}' are already in Calibre"
                    }, matched_series, resolved_series_name)
                else:
                    if auto_request_monitored:
                        return self._with_series_identity({
                            "status": "REQUESTED",
                            "matched_book_id": None,
                            "matched_series": matched_series,
                            "effective_volume": effective_volume,
                            "missing_volumes": missing_vols,
                            "reason": f"Bundle has {len(missing_vols)} missing volume(s) ({missing_vols}) for tracked series '{matched_series['series_name']}'"
                        }, matched_series, resolved_series_name)

            # Check single volume
            vol_key = round(effective_volume, 2)
            vol_match = self.series_vol_index.get((ser_id, vol_key))

            # If unnumbered release (Vol 1) and (ser_id, 1.0) wasn't found directly,
            # check if Calibre has Volume 0.0 or if lowest indexed book in this series is <= 1.0
            if not vol_match and not is_explicit_vol:
                vol_match = self.series_vol_index.get((ser_id, 0.0))
                if not vol_match and ser_id in self.series_first_book:
                    first_b = self.series_first_book[ser_id]
                    if round(first_b.get("series_index", 1.0), 2) <= 1.0:
                        vol_match = first_b

            if vol_match:
                return self._with_series_identity({
                    "status": "IN_CALIBRE",
                    "matched_book_id": vol_match["book_id"],
                    "matched_series": matched_series,
                    "effective_volume": effective_volume,
                    "missing_volumes": [],
                    "reason": f"Series '{matched_series['series_name']}' Vol {effective_volume:.0f} already present in Calibre (Book #{vol_match['book_id']})"
                }, matched_series, resolved_series_name)

            # Only check fuzzy title matching within this series if volume was NOT explicitly specified
            # (to prevent Volume 1 from falsely fuzzy-matching Volume 19 due to common series title prefix)
            if not is_explicit_vol:
                for b in self.calibre_books:
                    if b.get("series_id") == ser_id:
                        b_vol = b.get("series_index")
                        if b_vol is None or round(float(b_vol), 2) <= 1.0:
                            if string_similarity(b["title"], clean_title) >= 0.85:
                                return self._with_series_identity({
                                    "status": "IN_CALIBRE",
                                    "matched_book_id": b["book_id"],
                                    "matched_series": matched_series,
                                    "effective_volume": effective_volume,
                                    "missing_volumes": [],
                                    "reason": f"Fuzzy title match in series '{matched_series['series_name']}'"
                                }, matched_series, resolved_series_name)

            # Volume is not in Calibre!
            if auto_request_monitored:
                return self._with_series_identity({
                    "status": "REQUESTED",
                    "matched_book_id": None,
                    "matched_series": matched_series,
                    "effective_volume": effective_volume,
                    "missing_volumes": [effective_volume],
                    "reason": f"New volume {effective_volume:.0f} for tracked series '{canonical_name or matched_series['series_name']}' (Latest owned: Vol {matched_series['latest_index']})"
                }, matched_series, resolved_series_name)

        # 3. Check standalone book fuzzy or substring title match across library
        for b in self.calibre_books:
            if not b.get("series_id"):
                b_clean = clean_feed_title(b.get("title") or "")
                if b_clean:
                    if string_similarity(b_clean, clean_title) >= 0.85 or (len(clean_title) >= 8 and (clean_title.lower() in b_clean.lower() or b_clean.lower() in clean_title.lower())):
                        return self._with_series_identity({
                            "status": "IN_CALIBRE",
                            "matched_book_id": b["book_id"],
                            "matched_series": None,
                            "effective_volume": volume or b.get("series_index", 1.0),
                            "missing_volumes": [],
                            "reason": f"Title match with Calibre book #{b['book_id']} ('{b['title']}')"
                        }, None, resolved_series_name)

        # 4. Neither series nor title matched
        return self._with_series_identity({
            "status": "NEW",
            "matched_book_id": None,
            "matched_series": None,
            "effective_volume": effective_volume,
            "missing_volumes": [effective_volume],
            "reason": "Not in Calibre library and does not match any currently tracked series"
        }, None, resolved_series_name)

    def resolve_pending_requests(self, pending_requests: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Takes a list of currently REQUESTED releases and checks if any
        have now appeared in Calibre.
        Returns list of newly acquired releases.
        """
        acquired = []
        for req in pending_requests:
            result = self.check_release(req, auto_request_monitored=True)
            if result["status"] == "IN_CALIBRE":
                acquired.append({
                    "release_id": req["id"],
                    "title": req["title"],
                    "series_name": req.get("series_name"),
                    "volume": req.get("volume") or result.get("effective_volume") or 1.0,
                    "matched_book_id": result["matched_book_id"],
                    "reason": result["reason"]
                })
        return acquired
