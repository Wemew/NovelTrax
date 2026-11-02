import os
import zoneinfo
from datetime import datetime, timezone
from typing import Optional, Any

def get_app_timezone(tz_name: Optional[str] = None, custom_tz: Optional[str] = None) -> zoneinfo.ZoneInfo:
    """
    Resolves target timezone:
    1. tz_name or custom_tz parameter (e.g. from app_settings or query)
    2. TZ environment variable (e.g. set in docker-compose)
    3. Default 'Europe/Copenhagen' (automatic Danish CET/CEST DST)
    4. Host system local timezone
    """
    tz_str = (tz_name or custom_tz or os.environ.get("TZ", "")).strip()
    if tz_str:
        try:
            return zoneinfo.ZoneInfo(tz_str)
        except Exception:
            pass
    try:
        return zoneinfo.ZoneInfo("Europe/Copenhagen")
    except Exception:
        fallback = datetime.now().astimezone().tzinfo
        if isinstance(fallback, zoneinfo.ZoneInfo):
            return fallback
        # In case tzinfo is not ZoneInfo
        return zoneinfo.ZoneInfo("UTC")

def format_localtime(
    value: Any,
    tz_name: Optional[str] = None,
    fmt: str = "%Y-%m-%d %H:%M:%S",
    custom_tz: Optional[str] = None
) -> str:
    """
    Converts a UTC SQLite timestamp or datetime object to the target timezone
    (with automatic daylight saving transitions) and formats it.
    """
    if not value:
        return ""
    target_tz = get_app_timezone(tz_name=tz_name, custom_tz=custom_tz)
    try:
        if isinstance(value, datetime):
            dt = value
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(target_tz).strftime(fmt)

        val_str = str(value).strip().replace("T", " ")
        if val_str.endswith("Z"):
            val_str = val_str[:-1].strip()

        # Date only: YYYY-MM-DD
        if len(val_str) == 10 and "-" in val_str:
            return val_str

        # YYYY-MM-DD HH:MM
        if len(val_str) == 16:
            val_str += ":00"
        elif len(val_str) > 19:
            val_str = val_str[:19]

        dt = datetime.strptime(val_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone(target_tz).strftime(fmt)
    except Exception:
        return str(value)[:19]

def format_localdate(
    value: Any,
    tz_name: Optional[str] = None,
    fmt: str = "%Y-%m-%d",
    custom_tz: Optional[str] = None
) -> str:
    """Converts a UTC SQLite timestamp or datetime object to local date (YYYY-MM-DD)."""
    return format_localtime(value, tz_name=tz_name, fmt=fmt, custom_tz=custom_tz)
