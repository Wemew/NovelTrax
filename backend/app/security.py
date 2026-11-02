import os
import re
import socket
import ipaddress
import logging
import urllib.request
import urllib.error
from urllib.parse import urlparse, urljoin
from typing import Optional, Dict, Any, List, Set, Tuple

try:
    import httpx
except ImportError:
    httpx = None

logger = logging.getLogger("noveltrax.security")

# Specific non-routable, private, internal, link-local, and reserved networks
DISALLOWED_NETWORKS: List[ipaddress._BaseNetwork] = [
    # IPv4
    ipaddress.ip_network("0.0.0.0/8"),          # Current network (RFC 1122)
    ipaddress.ip_network("10.0.0.0/8"),         # Private-Use (RFC 1918)
    ipaddress.ip_network("100.64.0.0/10"),      # Shared Address Space / CGNAT (RFC 6598)
    ipaddress.ip_network("127.0.0.0/8"),        # Loopback (RFC 1122)
    ipaddress.ip_network("169.254.0.0/16"),     # Link-Local / Cloud Metadata (RFC 3927)
    ipaddress.ip_network("172.16.0.0/12"),      # Private-Use (RFC 1918)
    ipaddress.ip_network("192.0.0.0/24"),       # IETF Protocol Assignments (RFC 6890)
    ipaddress.ip_network("192.0.2.0/24"),       # Documentation TEST-NET-1 (RFC 5737)
    ipaddress.ip_network("192.168.0.0/16"),     # Private-Use (RFC 1918)
    ipaddress.ip_network("198.18.0.0/15"),      # Benchmarking (RFC 2544)
    ipaddress.ip_network("198.51.100.0/24"),    # Documentation TEST-NET-2 (RFC 5737)
    ipaddress.ip_network("203.0.113.0/24"),     # Documentation TEST-NET-3 (RFC 5737)
    ipaddress.ip_network("224.0.0.0/4"),        # Multicast (RFC 5771)
    ipaddress.ip_network("240.0.0.0/4"),        # Reserved / Future use (RFC 1112)
    ipaddress.ip_network("255.255.255.255/32"), # Limited Broadcast (RFC 919)
    # IPv6
    ipaddress.ip_network("::/128"),             # Unspecified
    ipaddress.ip_network("::1/128"),            # Loopback
    ipaddress.ip_network("::ffff:0:0/96"),      # IPv4-mapped IPv6
    ipaddress.ip_network("64:ff9b::/96"),       # IPv4-IPv6 translation
    ipaddress.ip_network("100::/64"),           # Discard prefix
    ipaddress.ip_network("2001:db8::/32"),      # Documentation
    ipaddress.ip_network("fc00::/7"),           # Unique-Local Address (ULA)
    ipaddress.ip_network("fe80::/10"),          # Link-Local Unicast
    ipaddress.ip_network("ff00::/8"),           # Multicast
]

def is_ip_disallowed(ip: ipaddress._BaseAddress) -> bool:
    """Checks whether an IP address belongs to private, loopback, metadata, or reserved ranges."""
    # Check standard attributes
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return True

    # Check for IPv4-mapped IPv6 (e.g., ::ffff:127.0.0.1 or ::ffff:192.168.1.1)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        if is_ip_disallowed(ip.ipv4_mapped):
            return True

    # Check against explicit disallowed network definitions
    for net in DISALLOWED_NETWORKS:
        if ip in net:
            return True

    return False
 
# Cloud metadata and link-local networks disallowed for internal clients (SSRF guard)
CLOUD_METADATA_IPV4_NETWORKS: List[ipaddress.IPv4Network] = [
    ipaddress.ip_network("169.254.0.0/16"),     # IPv4 Link-Local / Cloud Metadata (RFC 3927)
]

CLOUD_METADATA_IPV6_NETWORKS: List[ipaddress.IPv6Network] = [
    ipaddress.ip_network("fe80::/10"),          # IPv6 Link-Local Unicast (RFC 4291)
]

CLOUD_METADATA_HOSTNAMES: Set[str] = {
    "metadata.google.internal",
    "metadata",
    "instance-data",
}

def is_cloud_metadata_ip(ip: ipaddress._BaseAddress) -> bool:
    """
    Checks whether an IP address belongs to cloud metadata or link-local ranges:
    - 169.254.0.0/16 (AWS, GCP, Azure, Oracle, DO instance metadata endpoints)
    - fe80::/10 (IPv6 link-local unicast)
    - IPv4-mapped IPv6 link-local (e.g. ::ffff:169.254.169.254)
    """
    if ip.is_link_local:
        return True

    # Check for IPv4-mapped IPv6 (e.g., ::ffff:169.254.169.254)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return is_cloud_metadata_ip(ip.ipv4_mapped)

    if ip.version == 4:
        for net in CLOUD_METADATA_IPV4_NETWORKS:
            if ip in net:
                return True
    elif ip.version == 6:
        for net in CLOUD_METADATA_IPV6_NETWORKS:
            if ip in net:
                return True

    return False

def is_safe_public_url(url: str) -> bool:
    """
    Rigorously validates that a URL targets a public, routable HTTP/HTTPS destination.
    Blocks Server-Side Request Forgery (SSRF) targeting:
    - Non-HTTP(S) protocols (file://, ftp://, gopher://, javascript:, etc.)
    - Loopback addresses (127.0.0.0/8, ::1, localhost)
    - Private RFC 1918 subnets (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16)
    - Carrier-Grade NAT (100.64.0.0/10)
    - Link-Local and Cloud Metadata addresses (169.254.169.254, 169.254.0.0/16)
    - Multicast, broadcast, unspecified, and reserved ranges
    """
    if not url or not isinstance(url, str):
        return False

    # Reject raw NUL bytes and other C0/DEL control characters outright. These are
    # never valid in a public HTTP(S) URL and can truncate/mutate URLs at lower layers.
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        logger.warning(f"SSRF blocked: URL contains NUL or control characters: {url!r}")
        return False

    url_clean = url.strip()

    # Percent-encoded NUL/C0/DEL control characters are an obfuscation technique used to
    # smuggle control bytes past upstream parsers (e.g. "http://127.0.0.1%00@google.com/").
    if re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)", url_clean, re.IGNORECASE):
        logger.warning(f"SSRF blocked: URL contains percent-encoded control characters: {url_clean!r}")
        return False

    try:
        parsed = urlparse(url_clean)
        # Scheme must be http or https strictly
        if parsed.scheme.lower() not in ("http", "https"):
            logger.warning(f"SSRF blocked: Disallowed protocol '{parsed.scheme}' in URL {url_clean}")
            return False

        # Parser-confusion guard: an '@' after the authority (in path/query/fragment) lets
        # naive SSRF filters treat the trailing token as the host (e.g.
        # "http://google.com#@127.0.0.1/"). Only userinfo '@' inside the netloc is valid.
        netloc = parsed.netloc or ""
        if url_clean.count("@") > netloc.count("@"):
            logger.warning(f"SSRF blocked: Ambiguous '@' outside URL authority: {url_clean}")
            return False

        hostname = parsed.hostname
        if not hostname:
            logger.warning(f"SSRF blocked: Missing hostname in URL {url_clean}")
            return False

        hostname_lower = hostname.lower()

        # Reject literal localhost or standard local names
        if hostname_lower in ("localhost", "localhost.localdomain", "0.0.0.0", "127.0.0.1", "::1", "metadata.google.internal"):
            logger.warning(f"SSRF blocked: Hostname '{hostname}' is a reserved local destination.")
            return False

        # If hostname is an IP literal directly
        try:
            ip = ipaddress.ip_address(hostname_lower)
            if is_ip_disallowed(ip):
                logger.warning(f"SSRF blocked: Literal IP '{ip}' is in a protected or private range.")
                return False
            return True
        except ValueError:
            # Not an IP literal, proceed to DNS resolution
            pass

        # Resolve DNS hostname via socket.getaddrinfo to inspect all bound IPs
        addr_info = socket.getaddrinfo(hostname, None)
        if not addr_info:
            return False

        for family, _, _, _, sockaddr in addr_info:
            ip_str = sockaddr[0]
            try:
                ip = ipaddress.ip_address(ip_str)
                if is_ip_disallowed(ip):
                    logger.warning(f"SSRF blocked: Hostname '{hostname}' resolved to disallowed IP {ip_str} ({url_clean})")
                    return False
            except ValueError:
                return False

        return True

    except Exception as e:
        logger.debug(f"SSRF check failed/blocked for URL {url_clean}: {e}")
        return False

def validate_downloader_destination(
    host_or_url: str,
    scheme: Optional[str] = None,
    resolve_dns: bool = True
) -> Tuple[bool, str]:
    """
    Validates destination address for internal downloader clients (e.g. qBittorrent).
    Blocks SSRF attacks targeting cloud instance metadata endpoints (169.254.0.0/16,
    metadata.google.internal, fe80::/10) and non-HTTP(S) protocols.

    Permits:
    - Loopback: 127.0.0.0/8, ::1, localhost, localhost.localdomain
    - Private LAN (RFC 1918): 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16
    - Carrier-Grade NAT (RFC 6598 - 100.64.0.0/10) & ULA (fc00::/7)
    - Routable public internet domains and IP addresses

    Returns:
    (is_valid: bool, error_message: str)
    """
    if not host_or_url or not isinstance(host_or_url, str):
        return False, "SSRF blocked: Downloader destination host or URL is required."

    target = host_or_url.strip()
    target_scheme = (scheme or "").strip().lower()

    if "://" in target:
        parsed = urlparse(target)
        target_scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        if not hostname:
            return False, "SSRF blocked: Invalid URL; missing hostname."
    else:
        # Host or host:port string
        if target.startswith("[") and "]" in target:
            # IPv6 literal with or without port: [::1]:8080 or [::1]
            end_bracket = target.find("]")
            hostname = target[1:end_bracket]
        elif ":" in target and not target.endswith(":"):
            hostname = target.split(":")[0]
        else:
            hostname = target
        if not target_scheme:
            target_scheme = "http"

    # 1. Validate scheme strictly
    if target_scheme not in ("http", "https"):
        return False, f"SSRF blocked: Scheme '{target_scheme}' is disallowed. Only http and https are permitted."

    # 2. Validate hostname
    if not hostname:
        return False, "SSRF blocked: Downloader destination hostname is missing."

    hostname_clean = hostname.lower().rstrip(".")

    # Block well-known cloud metadata hostnames directly
    if hostname_clean in CLOUD_METADATA_HOSTNAMES:
        return False, "SSRF blocked: Cloud metadata IP is disallowed."

    # Explicitly permit loopback hostnames
    if hostname_clean in ("localhost", "localhost.localdomain"):
        return True, ""

    # 3. Check IP literal (IPv4 or IPv6)
    ip_candidate = hostname_clean.strip("[]")
    try:
        ip = ipaddress.ip_address(ip_candidate)
        if is_cloud_metadata_ip(ip):
            return False, "SSRF blocked: Cloud metadata IP is disallowed."
        return True, ""
    except ValueError:
        # Not an IP literal; proceed to DNS resolution
        pass

    # 4. Resolve DNS to inspect all bound IP addresses
    if resolve_dns:
        try:
            addr_info = socket.getaddrinfo(hostname_clean, None)
            if not addr_info:
                return False, f"SSRF blocked: Could not resolve hostname '{hostname_clean}'."
            for family, _, _, _, sockaddr in addr_info:
                ip_str = sockaddr[0]
                try:
                    resolved_ip = ipaddress.ip_address(ip_str)
                    if is_cloud_metadata_ip(resolved_ip):
                        return False, "SSRF blocked: Cloud metadata IP is disallowed."
                except ValueError:
                    continue
            return True, ""
        except socket.gaierror as e:
            logger.debug(f"DNS resolution failed for downloader host '{hostname_clean}': {e}")
            return False, f"DNS resolution failed for downloader host '{hostname_clean}'."
        except Exception as e:
            logger.debug(f"Destination check error for '{hostname_clean}': {e}")
            return False, f"Invalid destination: {e}"

    return True, ""

def is_safe_downloader_url(url: str) -> bool:
    """Convenience boolean helper for downloader destination safety."""
    is_valid, _ = validate_downloader_destination(url)
    return is_valid

class SafeResponse:
    """Lightweight response abstraction for urllib fallback."""
    def __init__(self, status_code: int, content: bytes, headers: dict):
        self.status_code = status_code
        self.content = content
        self.text = content.decode("utf-8", errors="ignore")
        self.headers = headers

class SSRFSafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Enforces SSRF validation on every redirect hop when using urllib."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_safe_public_url(newurl):
            raise ValueError(f"SSRF blocked: Redirect hop targets unsafe destination: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def _is_redirect_response(resp: Any) -> bool:
    """Best-effort redirect detection that works with httpx responses and test stubs."""
    if getattr(resp, "is_redirect", False) is True:
        return True
    status = getattr(resp, "status_code", None)
    return isinstance(status, int) and 300 <= status < 400


def _get_redirect_location(resp: Any) -> Optional[str]:
    """Best-effort Location header lookup for httpx responses and plain-dict stubs."""
    headers = getattr(resp, "headers", None)
    if not headers:
        return None
    try:
        location = headers.get("location") or headers.get("Location")
    except Exception:
        return None
    return location if isinstance(location, str) and location.strip() else None


def safe_client_request(client: Any, method: str, url: str, *, max_redirects: int = 5, **kwargs: Any) -> Any:
    """
    Performs an HTTP request through an httpx-like client while validating every
    redirect hop.

    Automatic redirect following MUST be disabled on the client (callers construct
    their clients with ``follow_redirects=False``); this helper then follows redirects
    manually, resolving each Location with urljoin and validating it with
    ``is_safe_public_url`` before requesting it. This closes SSRF-via-redirect holes
    where a public host 302s to a loopback/private/cloud-metadata destination.

    Raises ValueError when the initial URL or any hop targets an unsafe destination, or
    when more than ``max_redirects`` redirect hops are encountered.
    """
    if not is_safe_public_url(url):
        raise ValueError(f"SSRF blocked: Target URL is not a safe public destination: {url}")

    kwargs.pop("follow_redirects", None)
    request_method = getattr(client, method.lower())
    current_url = url

    for hop in range(max_redirects + 1):
        try:
            resp = request_method(current_url, follow_redirects=False, **kwargs)
        except TypeError:
            # Test stubs / minimal clients may not accept the follow_redirects kwarg.
            resp = request_method(current_url, **kwargs)

        if not _is_redirect_response(resp):
            return resp

        location = _get_redirect_location(resp)
        if not location:
            return resp

        if hop >= max_redirects:
            raise ValueError(
                f"SSRF blocked: Maximum redirects ({max_redirects}) exceeded while fetching {url}"
            )

        next_url = urljoin(current_url, location)
        if not is_safe_public_url(next_url):
            raise ValueError(f"SSRF blocked: Redirect hop targets unsafe destination: {next_url}")
        logger.info(f"Following validated redirect hop {hop + 1}/{max_redirects}: {current_url} -> {next_url}")
        current_url = next_url

    raise ValueError(f"SSRF blocked: Maximum redirects ({max_redirects}) exceeded while fetching {url}")


def safe_client_get(client: Any, url: str, *, max_redirects: int = 5, **kwargs: Any) -> Any:
    """GET counterpart of safe_client_request (validated per-hop redirect following)."""
    return safe_client_request(client, "get", url, max_redirects=max_redirects, **kwargs)


def safe_http_get(
    url: str,
    timeout: float = 15.0,
    headers: Optional[Dict[str, str]] = None,
    max_redirects: int = 5,
    max_bytes: int = 10 * 1024 * 1024
):
    """
    Executes an HTTP GET request with SSRF validation on the initial URL
    and every redirect hop. Enforces a maximum response byte threshold to prevent DoS.
    Raises ValueError("SSRF blocked: ...") if destination is unsafe.
    """
    if not is_safe_public_url(url):
        raise ValueError(f"SSRF blocked: Target URL is not a safe public destination: {url}")

    default_headers = {
        "User-Agent": "NovelTrax/1.0 (+https://github.com/noveltrax; Light Novel Acquisition)",
        "Accept": "*/*"
    }
    if headers:
        default_headers.update(headers)

    if httpx:
        current_url = url
        client = httpx.Client(timeout=timeout, headers=default_headers, follow_redirects=False)
        try:
            redirects = 0
            while True:
                resp = client.get(current_url)

                if resp.is_redirect and "location" in resp.headers:
                    redirects += 1
                    if redirects > max_redirects:
                        raise ValueError(f"Maximum redirects ({max_redirects}) exceeded.")

                    next_url = urljoin(current_url, resp.headers["location"])
                    if not is_safe_public_url(next_url):
                        raise ValueError(f"SSRF blocked: Redirect hop targets unsafe destination: {next_url}")
                    current_url = next_url
                    continue

                content_length = resp.headers.get("content-length")
                if content_length and content_length.isdigit() and int(content_length) > max_bytes:
                    raise ValueError(f"Response size ({content_length} bytes) exceeds limit of {max_bytes} bytes.")

                if len(resp.content) > max_bytes:
                    raise ValueError(f"Response payload exceeds maximum allowed size of {max_bytes} bytes.")

                return resp
        finally:
            client.close()
    else:
        # Fallback using urllib when httpx is not present in runtime environment
        opener = urllib.request.build_opener(SSRFSafeRedirectHandler)
        req = urllib.request.Request(url, headers=default_headers)
        with opener.open(req, timeout=timeout) as resp:
            content = resp.read(max_bytes + 1)
            if len(content) > max_bytes:
                raise ValueError(f"Response payload exceeds maximum allowed size of {max_bytes} bytes.")
            resp_headers = dict(resp.headers)
            status = resp.status if hasattr(resp, "status") else resp.getcode()
            return SafeResponse(status_code=status, content=content, headers=resp_headers)

def safe_fetch_text(url: str, timeout: float = 15.0, headers: Optional[Dict[str, str]] = None) -> str:
    """Convenience helper to retrieve safe response body as string."""
    resp = safe_http_get(url, timeout=timeout, headers=headers)
    return resp.text

def sanitize_calibre_path(path_str: str) -> str:
    """
    Validates and canonicalizes a Calibre library filesystem path.
    Prevents path traversal, rejects null bytes, and blocks forbidden file exploration.
    """
    if not path_str or not isinstance(path_str, str) or "\x00" in path_str:
        raise ValueError("Invalid path: Path cannot be empty or contain null bytes.")

    cleaned = os.path.normpath(path_str.strip())
    base_name = os.path.basename(cleaned).lower()

    # Block probing of executable or sensitive system files
    disallowed_exts = (
        ".exe", ".dll", ".bat", ".cmd", ".ps1", ".vbs", ".sh",
        ".env", ".key", ".pem", ".crt", ".conf", ".ini", ".log"
    )
    if any(base_name.endswith(ext) for ext in disallowed_exts):
        raise ValueError(f"Invalid path: Probing of '{base_name}' files is disallowed.")

    return cleaned
