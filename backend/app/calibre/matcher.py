from typing import List, Dict, Any, Optional, Tuple
from app.parser import (
    normalize_text,
    clean_feed_title,
    match_series_in_calibre,
    string_similarity
)

class CalibreMatcher:
    """
    Matches feed releases against Calibre books and series.
    Provides fast lookup caches to process hundreds of releases in milliseconds.
    """
    def __init__(self, calibre_series: List[Dict[str, Any]], calibre_books: List[Dict[str, Any]]):
        self.calibre_series = calibre_series
        self.calibre_books = calibre_books

        # Pre-build lookup index for quick book matching:
        # 1. By series_id + rounded volume index: (series_id, round(vol, 2)) -> book
        self.series_vol_index: Dict[Tuple[int, float], Dict[str, Any]] = {}
        # 2. By normalized series name + volume index
        self.series_name_vol_index: Dict[Tuple[str, float], Dict[str, Any]] = {}
        # 3. By normalized clean title
        self.clean_title_index: Dict[str, Dict[str, Any]] = {}

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
                if ser_name:
                    norm_ser_name = normalize_text(ser_name)
                    self.series_name_vol_index[(norm_ser_name, vol_key)] = b

    def check_release(self, release: Dict[str, Any], auto_request_monitored: bool = True) -> Dict[str, Any]:
        """
        Determines if a release is already in Calibre or needs to be requested.
        Returns dict with:
        {
            "status": "IN_CALIBRE" | "REQUESTED" | "IGNORED" | "NEW",
            "matched_book_id": Optional[int],
            "matched_series": Optional[Dict],
            "reason": str
        }
        """
        series_name = release.get("series_name")
        volume = release.get("volume")
        title = release.get("title", "")
        clean_title = clean_feed_title(title)

        # 1. Check title match directly
        norm_title = normalize_text(clean_title)
        if norm_title in self.clean_title_index:
            matched_book = self.clean_title_index[norm_title]
            return {
                "status": "IN_CALIBRE",
                "matched_book_id": matched_book["book_id"],
                "matched_series": None,
                "reason": f"Title matched Calibre book #{matched_book['book_id']} ('{matched_book['title']}')"
            }

        # 2. Check series + volume match
        matched_series = None
        if series_name:
            matched_series = match_series_in_calibre(series_name, self.calibre_series)

        volumes = release.get("volumes") or ([volume] if volume is not None else [])
        is_bundle = release.get("is_bundle", False) or len(volumes) > 1

        if matched_series:
            ser_id = matched_series["series_id"]

            if is_bundle and len(volumes) > 1:
                owned_vols = [v for v in volumes if (ser_id, round(float(v), 2)) in self.series_vol_index]
                missing_vols = [v for v in volumes if v not in owned_vols]

                if not missing_vols:
                    return {
                        "status": "IN_CALIBRE",
                        "matched_book_id": None,
                        "matched_series": matched_series,
                        "missing_volumes": [],
                        "reason": f"All {len(volumes)} volumes in bundle for '{matched_series['series_name']}' are already in Calibre"
                    }
                else:
                    if auto_request_monitored:
                        return {
                            "status": "REQUESTED",
                            "matched_book_id": None,
                            "matched_series": matched_series,
                            "missing_volumes": missing_vols,
                            "reason": f"Bundle has {len(missing_vols)} missing volume(s) ({missing_vols}) for tracked series '{matched_series['series_name']}'"
                        }
            elif volume is not None:
                vol_key = round(float(volume), 2)
                # Check if volume exists in Calibre for this series
                if (ser_id, vol_key) in self.series_vol_index:
                    matched_book = self.series_vol_index[(ser_id, vol_key)]
                    return {
                        "status": "IN_CALIBRE",
                        "matched_book_id": matched_book["book_id"],
                        "matched_series": matched_series,
                        "missing_volumes": [],
                        "reason": f"Series '{matched_series['series_name']}' Vol {volume} already present in Calibre (Book #{matched_book['book_id']})"
                    }
                
                # Series matched, but this volume is NOT in Calibre!
                if auto_request_monitored:
                    return {
                        "status": "REQUESTED",
                        "matched_book_id": None,
                        "matched_series": matched_series,
                        "missing_volumes": [volume],
                        "reason": f"New volume {volume} for tracked series '{matched_series['series_name']}' (Latest owned: Vol {matched_series['latest_index']})"
                    }

        # 3. Series matched, but volume wasn't specified:
        if matched_series:
            # Let's check fuzzy title against books in that series
            for b in self.calibre_books:
                if b.get("series_id") == matched_series["series_id"]:
                    if string_similarity(b["title"], title) > 0.85:
                        return {
                            "status": "IN_CALIBRE",
                            "matched_book_id": b["book_id"],
                            "matched_series": matched_series,
                            "reason": f"Fuzzy title match in series '{matched_series['series_name']}'"
                        }
            if auto_request_monitored:
                return {
                    "status": "REQUESTED",
                    "matched_book_id": None,
                    "matched_series": matched_series,
                    "reason": f"New release detected for tracked series '{matched_series['series_name']}'"
                }

        # 4. Neither series nor title matched
        return {
            "status": "NEW",
            "matched_book_id": None,
            "matched_series": None,
            "reason": "Not in Calibre library and does not match any currently tracked series"
        }

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
                    "volume": req.get("volume"),
                    "matched_book_id": result["matched_book_id"],
                    "reason": result["reason"]
                })
        return acquired
