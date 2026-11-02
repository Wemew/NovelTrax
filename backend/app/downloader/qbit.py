"""
In-App client for qBittorrent v2 Web API for NovelTrax.
Compatible with qBittorrent v4.x, v5.x, Flood, Torbox, and qBit emulators.
Implements connection testing, queue management, torrent controls,
and selective volume prioritization for light novel bundles.
"""

import os
import re
import json
import socket
import logging
import base64
import urllib.request
import urllib.parse
import urllib.error
import http.cookiejar
from typing import List, Dict, Any, Optional, Union, Set, Iterable, Tuple

logger = logging.getLogger("noveltrax.qbit")

try:
    from app.security import validate_downloader_destination, is_cloud_metadata_ip
except ImportError:
    try:
        from backend.app.security import validate_downloader_destination, is_cloud_metadata_ip
    except ImportError:
        def validate_downloader_destination(h, scheme="http", resolve_dns=True):
            clean = (h or "").lower().strip()
            if "169.254" in clean or "metadata.google.internal" in clean:
                return False, "SSRF blocked: Cloud metadata IP is disallowed."
            return True, ""
        def is_cloud_metadata_ip(ip):
            return "169.254" in str(ip)

# Known light novel archive and document extensions
NOVEL_EXTENSIONS = (
    ".epub", ".azw3", ".mobi", ".kfx", ".pdf",
    ".cbz", ".cbr", ".zip", ".rar", ".7z"
)

# Ancillary / clutter / non-book files to skip in selective downloads
ANCILLARY_EXTENSIONS = (
    ".txt", ".nfo", ".url", ".html", ".htm",
    ".jpg", ".jpeg", ".png", ".webp", ".sfv",
    ".lnk", ".md", ".torrent", ".xml", ".ini"
)


class ConnectionTestResult(dict):
    """
    Dictionary representing connection test result that evaluates cleanly
    both as a boolean (success status) and as a structured dictionary.
    """
    @property
    def success(self) -> bool:
        return bool(self.get("success", False))

    def __bool__(self) -> bool:
        return bool(self.get("success", False))

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, bool):
            return bool(self) == other
        return super().__eq__(other)


class SelectiveDownloadResult(dict):
    """
    Dictionary representing selective volume download result that evaluates
    cleanly as an integer (count of skipped files) when cast via int().
    """
    @property
    def skipped_files(self) -> int:
        return int(self.get("skipped_files", 0))

    def __int__(self) -> int:
        return int(self.get("skipped_files", 0))

    def __index__(self) -> int:
        return int(self.get("skipped_files", 0))

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, int):
            return int(self) == other
        return super().__eq__(other)


def parse_torrent_filename_volumes(
    filename: str,
    series_name: Optional[str] = None
) -> Tuple[bool, Set[float]]:
    """
    Parses a torrent file path or name to extract volume numbers.
    Returns:
        (is_ancillary, volumes_set)
    - If file is clutter/ancillary (e.g. cover.jpg, series.nfo, samples, subtitles), returns (True, set()).
    - If novel file contains single or omnibus volumes, returns (False, {vols}).
    - If novel file contains no volume numbers, returns (False, set()).

    Handles:
    - Leading release group tags: '[Yen Press] Mob Psycho 100 - Vol 03.epub' -> (False, {3.0})
    - Numbered series with tags: '[Group] 86 - Vol 01.epub' -> (False, {1.0})
    - Numbered series without series_name: '86 - Vol 01.epub' -> (False, {1.0})
    - Numbered titles with ranges: 'Catch-22 - Vol 1-3.epub' -> (False, {1.0, 2.0, 3.0})
    - Conjunctions/lists: 'Mushoku Tensei Vol 1 & 2.epub' -> (False, {1.0, 2.0})
    - Fractional ranges: 'Series Vol 1.5-3.epub' -> (False, {1.5, 2.0, 3.0}) (no integer 1.0)
    - Oversized omnibus: 'Guin Saga Vol 1-120.epub' -> (False, set()) (safe keep)
    - Inverted ranges: 'Series Vol 03-01.epub' -> (False, {1.0, 2.0, 3.0})
    """
    normalized_path = filename.replace("\\", "/")
    base_name = os.path.basename(normalized_path)
    name_no_ext, ext = os.path.splitext(base_name)
    ext_lower = ext.lower()

    # Check for ancillary/junk file extensions
    if ext_lower in ANCILLARY_EXTENSIONS:
        return True, set()

    # Supported light novel formats
    if ext_lower not in NOVEL_EXTENSIONS:
        return True, set()

    def clean_target_text(text: str) -> str:
        s = text
        # 1. Continuously strip all leading release groups / format tags
        # Handles [Yen Press], [J-Novel Club], [Group], (Digital), [1080p], etc.
        while True:
            stripped = re.sub(r"^(?:\[[^\]]*\]|\([^\)]*\))\s*", "", s).strip()
            if stripped == s:
                break
            s = stripped

        # 2. Case-insensitively strip series_name from the beginning
        if series_name:
            s = re.sub(r"^" + re.escape(series_name), "", s, flags=re.IGNORECASE).strip(" -–—:,|._")
        return s

    cleaned = clean_target_text(name_no_ext)

    # Search targets: cleaned name, immediate parent folder, and full relative path
    search_targets = [cleaned]
    path_parts = [p for p in normalized_path.split("/")[:-1] if p]
    if path_parts:
        search_targets.append(clean_target_text(path_parts[-1]))
        search_targets.append(clean_target_text(os.path.splitext(normalized_path)[0]))

    for t_cleaned in search_targets:
        if not t_cleaned:
            continue

        # 1. Bracketed omnibus ranges: [1-4], [Vol 1-3], (v01-04), [0-2]
        m_bracket = re.search(
            r"[\[\(]\s*(?:(?P<p1>\b(?:volumes?|volume|vols?|vol|books?|book|bk)\b\.?|\bv\.?)\s*)?(?P<start>\d+(?:\.\d+)?)"
            r"\s*[-–—~to/]+\s*"
            r"(?:(?P<p2>\b(?:volumes?|volume|vols?|vol|books?|book|bk)\b\.?|\bv\.?)\s*)?(?P<end>\d+(?:\.\d+)?)\s*[\]\)]",
            t_cleaned,
            re.IGNORECASE
        )
        if m_bracket:
            p1 = m_bracket.group("p1")
            p2 = m_bracket.group("p2")
            if not p1 and p2:
                # Syntactic guard: title delimiter preceding volume tag inside brackets (e.g. '[100 - Vol 1]')
                pass
            else:
                try:
                    s_val = float(m_bracket.group("start"))
                    e_val = float(m_bracket.group("end"))
                    if s_val > e_val:
                        if not p1:
                            # Logic guard: reversed ranges require explicit volume prefix
                            pass
                        else:
                            s_val, e_val = e_val, s_val
                    if s_val <= e_val and 0 <= s_val <= 2000 and 0 <= e_val <= 2000:
                        if (e_val - s_val) <= 100:
                            vols: Set[float] = set()
                            curr = int(s_val)
                            while curr <= int(e_val):
                                if curr >= s_val:
                                    vols.add(float(curr))
                                curr += 1
                            if s_val != int(s_val):
                                vols.add(round(s_val, 2))
                            if e_val != int(e_val):
                                vols.add(round(e_val, 2))
                            return False, vols
                        else:
                            # Oversized range (>100 volumes): safely return empty set to KEEP file
                            return False, set()
                except ValueError:
                    pass

        # 2. Standard range: Vol 1-3, v01-02, Volume 1 to 4, 01-03, Vol 1-2.5, bare 1-4
        # Proven Calibre dual-branch regex ensuring p1 is required if prefix exists,
        # or range is purely unadorned digits. Lookahead prevents greedy false matching on titles like '86 - 1-4'.
        m_range = re.search(
            r"(?i)(?:"
            r"(?P<p1>\b(?:volumes?|volume|vols?|vol|books?|book|bk)\b\.?|\bv\.?)\s*(?P<start1>\d+(?:\.\d+)?)\s*[-–—~to/]+\s*(?:(?P<p2>\b(?:volumes?|volume|vols?|vol|books?|book|bk)\b\.?|\bv\.?)\s*)?(?P<end1>\d+(?:\.\d+)?)\b"
            r"|"
            r"(?<![-–—/])\b(?P<start2>\d+(?:\.\d+)?)\s*[-–—~to/]+\s*(?P<end2>\d+(?:\.\d+)?)\b(?!\s*[-–—~to/]+\s*\d)"
            r")",
            t_cleaned
        )
        if m_range:
            try:
                p1 = m_range.group("p1")
                p2 = m_range.group("p2")
                s_val = float(m_range.group("start1") or m_range.group("start2"))
                e_val = float(m_range.group("end1") or m_range.group("end2"))
                if s_val > e_val:
                    # Logic guard: reversed ranges (e.g. Vol 3-1) ONLY allowed when p1 explicitly matched
                    if not p1:
                        pass
                    else:
                        s_val, e_val = e_val, s_val
                if s_val <= e_val and 0 <= s_val <= 2000 and 0 <= e_val <= 2000:
                    if (e_val - s_val) <= 100:
                        vols = set()
                        curr = int(s_val)
                        while curr <= int(e_val):
                            if curr >= s_val:
                                vols.add(float(curr))
                            curr += 1
                        if s_val != int(s_val):
                            vols.add(round(s_val, 2))
                        if e_val != int(e_val):
                            vols.add(round(e_val, 2))
                        return False, vols
                    else:
                        # Oversized range (>100 volumes): safely return empty set to KEEP file
                        return False, set()
            except ValueError:
                pass

        # 3. Multi-volume list/conjunction pattern: Vol 1 & 2, v1+v2, Vol 1, 2, 3, Book 1 and 2, Oxford comma: Vol 1, 2, and 3
        m_list = re.search(
            r"(?i)(?:(?:\b(?:volumes?|volume|vols?|vol|books?|book|bk)\b\.?|\bv\.?)\s*)"
            r"(?P<list>\d+(?:\.\d+)?(?:\s*(?:,\s*(?:and\b|[+&])|[,+&]|\band\b)\s*(?:(?:\b(?:volumes?|volume|vols?|vol|books?|book|bk)\b\.?|\bv\.?)\s*)?\d+(?:\.\d+)?)+)",
            t_cleaned
        )
        if m_list:
            raw_nums = re.findall(r"\d+(?:\.\d+)?", m_list.group("list"))
            vols_list: Set[float] = set()
            for n in raw_nums:
                val = round(float(n), 2)
                if 0 <= val <= 2000:
                    vols_list.add(val)
            if vols_list:
                return False, vols_list

        # 4. Check explicit volume keyword: Vol. 01, Volume 15, v08, Vol 4.5, Book 3
        m_single = re.search(
            r"(?i)(?:^|[\s_\[\(-])(?:(?:\b(?:volumes?|volume|vols?|vol|books?|book|bk|parts?|pt)\b\.?|\bv\.?))\s*#?\s*([0-9]+(?:\.[0-9]+)?)(?:[\s_\]\).-]|$)",
            t_cleaned
        )
        if m_single:
            try:
                v_val = float(m_single.group(1))
                if 0 <= v_val <= 2000:
                    return False, {round(v_val, 2)}
            except ValueError:
                pass

        # 5. Check bare number after hyphen/delimiter: Series - 02.epub
        m_bare = re.search(
            r"(?i)(?:^|\s+[-–—_]\s+)(\d{1,3}(?:\.\d+)?)(?:\s*\[|\s*\(|\s+[-–—]|\s*$)",
            t_cleaned
        )
        if m_bare:
            try:
                b_val = float(m_bare.group(1))
                # Guard against common release years like 1999-2029
                if 0 <= b_val <= 500 and not (1900 <= b_val <= 2099):
                    return False, {round(b_val, 2)}
            except ValueError:
                pass

    # No volume number could be determined safely
    return False, set()


class QBitClient:
    """
    qBittorrent Web API v2 Client.
    Supports both parameter-based initialization (host, port, use_ssl)
    and base_url string initialization.
    """
    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        username: Optional[str] = "admin",
        password: Optional[str] = "adminadmin",
        use_ssl: Optional[bool] = None,
        base_url: Optional[str] = None,
        enabled: bool = True,
        category: str = "novels",
        tags: str = "noveltrax",
        savepath: Optional[str] = None
    ):
        self.enabled = bool(enabled)
        self.username = username or ""
        self.password = password or ""
        self.category = category or "novels"
        self.tags = tags or "noveltrax"
        self.savepath = savepath or "/downloads"

        # Resolve host, port, use_ssl, and base_url
        if base_url:
            raw_url = base_url.strip().rstrip("/")
            if "://" not in raw_url:
                raw_url = "http://" + raw_url
            self.base_url = raw_url
            parsed = urllib.parse.urlparse(self.base_url)
            self.use_ssl = (parsed.scheme.lower() == "https")
            self.host = parsed.hostname or "localhost"
            self.port = parsed.port or (443 if self.use_ssl else 80)
        else:
            self.host = host or "localhost"
            self.use_ssl = bool(use_ssl)
            scheme = "https" if self.use_ssl else "http"
            self.port = int(port) if port is not None else (443 if self.use_ssl else 8080)
            self.base_url = f"{scheme}://{self.host}:{self.port}"

        # CookieJar and HTTP Opener for session management
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))
        self.authenticated = False

        # Brief caching of connection test results
        self._last_test_res: Optional[ConnectionTestResult] = None
        self._last_test_time: float = 0.0

    def validate_destination(self) -> Tuple[bool, str]:
        """
        Validates destination scheme, host, and resolved IP addresses against
        cloud metadata endpoints (169.254.0.0/16, metadata.google.internal, fe80::/10)
        and disallowed protocols.
        Permits loopback (127.0.0.1, localhost, ::1) and RFC 1918 private subnets.
        Returns: (is_valid: bool, error_message: str)
        """
        scheme = "https" if self.use_ssl else "http"
        return validate_downloader_destination(self.base_url or self.host, scheme=scheme)

    def login(self, timeout: float = 3.0) -> bool:
        """Authenticates with qBittorrent Web API v2 via /api/v2/auth/login."""
        if not self.enabled:
            logger.info("Downloader is disabled in settings. Skipping login.")
            return False

        is_safe, error_msg = self.validate_destination()
        if not is_safe:
            logger.warning(f"qBittorrent login blocked: {error_msg}")
            self.authenticated = False
            return False

        login_url = f"{self.base_url}/api/v2/auth/login"
        post_data = urllib.parse.urlencode({
            "username": self.username,
            "password": self.password
        }).encode("utf-8")

        req = urllib.request.Request(
            login_url,
            data=post_data,
            headers={
                "User-Agent": "NovelTrax/1.0",
                "Content-Type": "application/x-www-form-urlencoded"
            }
        )

        try:
            with self.opener.open(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", errors="replace")
                has_cookie = any(c.name == "SID" for c in self.cj)
                if resp.status == 200 and ("Ok." in text or has_cookie):
                    self.authenticated = True
                    return True
                else:
                    logger.warning(f"qBittorrent login failed: {text.strip()}")
                    self.authenticated = False
                    return False
        except urllib.error.HTTPError as e:
            if e.code == 403:
                logger.warning(f"qBittorrent authentication failed (HTTP 403 Forbidden): {e.reason}")
            else:
                logger.warning(f"qBittorrent login HTTP {e.code}: {e.reason}")
            self.authenticated = False
            return False
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            logger.error(f"Network error connecting to qBittorrent at {login_url}: {e}")
            self.authenticated = False
            return False
        except Exception as e:
            logger.error(f"Unexpected error connecting to qBittorrent at {login_url}: {e}")
            self.authenticated = False
            return False

    def _ensure_auth(self) -> bool:
        """Verifies session is authenticated or logs in."""
        if not self.authenticated:
            return self.login()
        return True

    def test_connection(self, timeout: float = 3.0, force: bool = False, max_age: float = 8.0) -> ConnectionTestResult:
        """Verifies connectivity and returns downloader app version and state, caching results briefly."""
        import time
        now = time.time()
        if not force and self._last_test_res and (now - self._last_test_time < max_age):
            return self._last_test_res

        if not self.enabled:
            res = ConnectionTestResult({
                "success": False,
                "version": "",
                "message": "Downloader is currently disabled in NovelTrax settings."
            })
            self._last_test_res = res
            self._last_test_time = now
            return res

        if not self.base_url:
            res = ConnectionTestResult({
                "success": False,
                "version": "",
                "message": "Downloader URL is not configured."
            })
            self._last_test_res = res
            self._last_test_time = now
            return res

        # SSRF Guard against cloud metadata endpoints & disallowed protocols
        is_safe, error_msg = self.validate_destination()
        if not is_safe:
            msg = error_msg or "SSRF blocked: Cloud metadata IP is disallowed."
            if "cloud metadata" not in msg.lower() and "scheme" not in msg.lower():
                msg = f"SSRF blocked: Cloud metadata IP is disallowed. ({msg})"
            res = ConnectionTestResult({
                "success": False,
                "version": "",
                "message": msg
            })
            self._last_test_res = res
            self._last_test_time = now
            return res

        try:
            if not self.login(timeout=timeout):
                res = ConnectionTestResult({
                    "success": False,
                    "version": "",
                    "message": f"Authentication failed connecting to {self.base_url}. Verify username and password."
                })
                self._last_test_res = res
                self._last_test_time = now
                return res

            req = urllib.request.Request(
                f"{self.base_url}/api/v2/app/version",
                headers={"User-Agent": "NovelTrax/1.0"}
            )
            with self.opener.open(req, timeout=timeout) as resp:
                if resp.status == 200:
                    version = resp.read().decode("utf-8", errors="replace").strip()
                    res = ConnectionTestResult({
                        "success": True,
                        "version": version,
                        "message": f"Connected to qBittorrent {version} at {self.base_url}"
                    })
                else:
                    res = ConnectionTestResult({
                        "success": False,
                        "version": "",
                        "message": f"HTTP {resp.status} from downloader"
                    })
                self._last_test_res = res
                self._last_test_time = now
                return res
        except urllib.error.HTTPError as e:
            res = ConnectionTestResult({"success": False, "version": "", "message": f"HTTP {e.code}: {e.reason}"})
            self._last_test_res = res
            self._last_test_time = now
            return res
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            res = ConnectionTestResult({"success": False, "version": "", "message": f"Connection error: {e}"})
            self._last_test_res = res
            self._last_test_time = now
            return res
        except Exception as e:
            res = ConnectionTestResult({"success": False, "version": "", "message": f"Connection error: {e}"})
            self._last_test_res = res
            self._last_test_time = now
            return res

    def add_torrent(
        self,
        url_or_magnet: str,
        savepath: Optional[str] = None,
        category: Optional[str] = None,
        tags: Optional[str] = None,
        paused: bool = False
    ) -> bool:
        """Sends a magnet link or .torrent URL to qBittorrent."""
        if not self.enabled:
            logger.info("Downloader is disabled in settings. Skipping add_torrent.")
            return False

        is_safe, error_msg = self.validate_destination()
        if not is_safe:
            logger.warning(f"qBittorrent add_torrent blocked: {error_msg}")
            return False

        if not self._ensure_auth():
            logger.warning("Downloader authentication not available; cannot add torrent.")
            return False

        add_url = f"{self.base_url}/api/v2/torrents/add"
        form_fields = {
            "urls": url_or_magnet,
            "category": category or self.category or "novels",
            "tags": tags or self.tags or "noveltrax",
            "paused": "true" if paused else "false"
        }
        sp = savepath or self.savepath
        if sp:
            form_fields["savepath"] = sp

        post_data = urllib.parse.urlencode(form_fields).encode("utf-8")
        req = urllib.request.Request(
            add_url,
            data=post_data,
            headers={
                "User-Agent": "NovelTrax/1.0",
                "Content-Type": "application/x-www-form-urlencoded"
            }
        )

        try:
            with self.opener.open(req, timeout=12.0) as resp:
                text = resp.read().decode("utf-8", errors="replace")
                if resp.status == 200 and "Fails" not in text:
                    logger.info(f"Successfully added torrent to downloader: {url_or_magnet[:60]}...")
                    return True
                logger.warning(f"Failed to add torrent to downloader: {text}")
                return False
        except Exception as e:
            logger.error(f"Error calling downloader add_torrent: {e}")
            return False

    def add_torrent_url(self, url: str, category: Optional[str] = None, savepath: Optional[str] = None) -> bool:
        """Alias adhering to PROJECT.md interface contract."""
        return self.add_torrent(url_or_magnet=url, category=category or self.category, savepath=savepath)

    def get_torrents(self, category: Optional[str] = None, filter_status: str = "all") -> List[Dict[str, Any]]:
        """Retrieves list of active torrents from qBittorrent."""
        if not self.enabled or not self._ensure_auth():
            return []

        is_safe, _ = self.validate_destination()
        if not is_safe:
            return []

        cat = category if category is not None else self.category
        params = {"filter": filter_status}
        if cat:
            params["category"] = cat

        list_url = f"{self.base_url}/api/v2/torrents/info?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            list_url,
            headers={"User-Agent": "NovelTrax/1.0", "Accept": "application/json"}
        )

        try:
            with self.opener.open(req, timeout=10.0) as resp:
                if resp.status == 200:
                    data = resp.read().decode("utf-8", errors="replace")
                    return json.loads(data)
                return []
        except urllib.error.HTTPError as e:
            if e.code == 403:
                self.authenticated = False
            logger.error(f"HTTP error fetching torrent list: {e}")
            return []
        except Exception as e:
            logger.error(f"Error fetching torrent list: {e}")
            return []

    def pause_torrents(self, hashes: Union[str, List[str], Set[str], Tuple[str, ...]]) -> bool:
        """Pauses one or more torrents by hash."""
        if not self.enabled or not self._ensure_auth():
            return False
        return self._post_hashes("/api/v2/torrents/pause", hashes)

    def pause_torrent(self, torrent_hash: str) -> bool:
        """Singular alias for pause_torrents."""
        return self.pause_torrents(torrent_hash)

    def resume_torrents(self, hashes: Union[str, List[str], Set[str], Tuple[str, ...]]) -> bool:
        """Resumes one or more torrents by hash."""
        if not self.enabled or not self._ensure_auth():
            return False
        return self._post_hashes("/api/v2/torrents/resume", hashes)

    def resume_torrent(self, torrent_hash: str) -> bool:
        """Singular alias for resume_torrents."""
        return self.resume_torrents(torrent_hash)

    def delete_torrents(self, hashes: Union[str, List[str], Set[str], Tuple[str, ...]], delete_files: bool = False) -> bool:
        """Deletes one or more torrents from queue, optionally deleting downloaded data files."""
        if not self.enabled or not self._ensure_auth():
            return False
        serialized_hashes = self._serialize_hashes(hashes)
        data = {
            "hashes": serialized_hashes,
            "deleteFiles": "true" if delete_files else "false"
        }
        return self._post_data("/api/v2/torrents/delete", data)

    def delete_torrent(self, torrent_hash: str, delete_files: bool = False) -> bool:
        """Singular alias for delete_torrents."""
        return self.delete_torrents(torrent_hash, delete_files=delete_files)

    @staticmethod
    def extract_torrent_hash(url_or_magnet: str) -> Optional[str]:
        """
        Extracts normalized 40-character hex hash from:
        - Magnet URIs with urn:btih: (40-hex or 32-Base32)
        - Bare 40-character hex strings
        - Bare 32-character Base32 strings (decoded to 40-hex)
        - HTTP/HTTPS URLs with embedded 40-hex hashes (e.g. .torrent filenames)
        """
        if not url_or_magnet or not isinstance(url_or_magnet, str):
            return None

        s = url_or_magnet.strip()
        if not s:
            return None

        # 1. Bare 40-character hex string
        if re.fullmatch(r"[0-9a-fA-F]{40}", s):
            return s.lower()

        # 2. Bare 32-character Base32 string
        if re.fullmatch(r"[2-7a-zA-Z]{32}", s):
            try:
                raw_bytes = base64.b32decode(s.upper())
                if len(raw_bytes) == 20:
                    return raw_bytes.hex().lower()
            except Exception:
                pass

        # 3. Magnet link with urn:btih:
        m = re.search(r"urn:btih:([^&?#\s]+)", s, re.IGNORECASE)
        if m:
            raw_hash = m.group(1).strip()
            if len(raw_hash) == 40 and re.fullmatch(r"[0-9a-fA-F]{40}", raw_hash):
                return raw_hash.lower()
            if len(raw_hash) == 32 and re.fullmatch(r"[2-7a-zA-Z]{32}", raw_hash):
                try:
                    raw_bytes = base64.b32decode(raw_hash.upper())
                    if len(raw_bytes) == 20:
                        return raw_bytes.hex().lower()
                except Exception:
                    pass
            return None

        # 4. Embedded 40-character hex in URL path / parameters (e.g. /<hash>.torrent)
        m_url = re.search(r"(?:/|=|\b)([0-9a-fA-F]{40})(?:\.torrent|\b|$)", s, re.IGNORECASE)
        if m_url:
            return m_url.group(1).lower()

        return None

    def get_torrent_files(self, torrent_hash: str) -> List[Dict[str, Any]]:
        """Queries qBittorrent for the list of files in a specific torrent."""
        if not self.enabled or not self._ensure_auth():
            return []

        is_safe, _ = self.validate_destination()
        if not is_safe:
            return []

        url = f"{self.base_url}/api/v2/torrents/files?hash={urllib.parse.quote(torrent_hash)}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "NovelTrax/1.0", "Accept": "application/json"}
        )
        try:
            with self.opener.open(req, timeout=10.0) as resp:
                if resp.status == 200:
                    data = resp.read().decode("utf-8", errors="replace")
                    return json.loads(data)
                return []
        except Exception as e:
            logger.debug(f"Could not retrieve files for torrent {torrent_hash}: {e}")
            return []

    def set_file_priorities(self, torrent_hash: str, file_indices: List[int], priority: int = 0) -> bool:
        """
        Sets download priority for a list of file indices in a torrent.
        priority=0 sets 'Do not download' (deselects file in downloader).
        priority=1 sets Normal priority.
        """
        if not self.enabled or not self._ensure_auth() or not file_indices:
            return False

        id_str = "|".join(str(i) for i in file_indices)
        data = {
            "hash": torrent_hash,
            "id": id_str,
            "priority": str(priority)
        }
        return self._post_data("/api/v2/torrents/filePrio", data)

    def apply_selective_volume_download(
        self,
        torrent_hash: str,
        owned_volumes: Optional[Iterable[float]] = None,
        wanted_volumes: Optional[Iterable[float]] = None,
        series_name: Optional[str] = None
    ) -> SelectiveDownloadResult:
        """
        Examines files in a multi-file light novel torrent and sets priority=0 (Do Not Download)
        for any volumes that the user already owns in Calibre (or not in wanted_volumes).
        
        Rule: An omnibus or single-volume file is deselected IF AND ONLY IF ALL contained
        volumes are already owned. If any volume inside a file is missing/wanted, it is KEPT.
        
        Returns SelectiveDownloadResult (dict subclass supporting int() coercion).
        """
        if not self.enabled:
            return SelectiveDownloadResult({
                "status": "disabled",
                "skipped_files": 0,
                "kept_files": 0,
                "message": "Downloader is disabled in settings."
            })

        files = self.get_torrent_files(torrent_hash)
        if not files:
            # Metadata might still be resolving via DHT
            return SelectiveDownloadResult({
                "status": "pending_metadata",
                "skipped_files": 0,
                "kept_files": 0,
                "message": "Metadata not loaded yet. Will re-attempt during active polling."
            })

        novel_files = [
            f for f in files
            if any(f.get("name", "").lower().endswith(ext) for ext in NOVEL_EXTENSIONS)
        ]

        # Single monolithic file cannot be split at BitTorrent protocol layer
        if len(novel_files) <= 1:
            return SelectiveDownloadResult({
                "status": "single_archive",
                "skipped_files": 0,
                "kept_files": len(files),
                "message": "Torrent contains a single archive; full archive will be downloaded."
            })

        # Build comparison sets
        owned_set: Optional[Set[float]] = {round(float(v), 2) for v in owned_volumes} if owned_volumes is not None else None
        wanted_set: Optional[Set[float]] = {round(float(v), 2) for v in wanted_volumes} if wanted_volumes is not None else None

        skip_indices: List[int] = []
        keep_indices: List[int] = []
        skipped_vols: Set[float] = set()
        kept_vols: Set[float] = set()

        for idx, f in enumerate(files):
            file_idx = f.get("id", f.get("index", idx))
            fname = f.get("name", "")
            is_ancillary, vols = parse_torrent_filename_volumes(fname, series_name=series_name)

            if is_ancillary:
                skip_indices.append(file_idx)
                continue

            if vols:
                # Evaluation rule:
                # If owned_set given: file is skipped ONLY if ALL contained volumes are owned.
                # If wanted_set given: file is kept if ANY contained volume is wanted.
                should_skip = False
                if owned_set is not None:
                    should_skip = vols.issubset(owned_set)
                elif wanted_set is not None:
                    should_skip = vols.isdisjoint(wanted_set)

                if should_skip:
                    skip_indices.append(file_idx)
                    skipped_vols.update(vols)
                else:
                    keep_indices.append(file_idx)
                    kept_vols.update(vols)
            else:
                # Safety fallback: unparsed book files are always kept
                keep_indices.append(file_idx)

        # Apply priority changes in qBittorrent if any files to skip
        if skip_indices:
            self.set_file_priorities(torrent_hash, skip_indices, priority=0)

        # Determine outcome status
        if len(keep_indices) == 0:
            status = "all_owned"
            msg = f"All {len(files)} files in torrent are already owned; all deselected."
        elif len(skip_indices) == 0:
            status = "all_needed"
            msg = "All files in torrent are needed; downloading all."
        else:
            status = "filtered"
            msg = (f"Selective download applied: skipped {len(skip_indices)} files "
                   f"(Vols: {sorted(list(skipped_vols))}); "
                   f"downloading {len(keep_indices)} files (Vols: {sorted(list(kept_vols))}).")

        logger.info(f"Torrent {torrent_hash[:10]}: {msg}")
        return SelectiveDownloadResult({
            "status": status,
            "skipped_files": len(skip_indices),
            "skipped_indices": skip_indices,
            "skipped_volumes": sorted(list(skipped_vols)),
            "kept_files": len(keep_indices),
            "kept_indices": keep_indices,
            "kept_volumes": sorted(list(kept_vols)),
            "total_files": len(files),
            "message": msg
        })

    def _serialize_hashes(self, hashes: Union[str, List[str], Set[str], Tuple[str, ...]]) -> str:
        """Serializes single or multiple torrent hashes into pipe-separated string."""
        if isinstance(hashes, (list, tuple, set)):
            return "|".join(str(h).strip() for h in hashes if str(h).strip())
        return str(hashes).strip() if hashes else ""

    def _post_hashes(self, path: str, hashes: Union[str, List[str], Set[str], Tuple[str, ...]]) -> bool:
        serialized = self._serialize_hashes(hashes)
        return self._post_data(path, {"hashes": serialized})

    def _post_data(self, path: str, form_dict: Dict[str, str]) -> bool:
        is_safe, error_msg = self.validate_destination()
        if not is_safe:
            logger.warning(f"qBittorrent request blocked: {error_msg}")
            return False

        post_data = urllib.parse.urlencode(form_dict).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=post_data,
            headers={
                "User-Agent": "NovelTrax/1.0",
                "Content-Type": "application/x-www-form-urlencoded"
            }
        )
        try:
            with self.opener.open(req, timeout=10.0) as resp:
                return resp.status == 200
        except urllib.error.HTTPError as e:
            if e.code == 403:
                self.authenticated = False
            logger.warning(f"Error calling {path}: HTTP {e.code} {e.reason}")
            return False
        except Exception as e:
            logger.warning(f"Error calling {path}: {e}")
            return False
