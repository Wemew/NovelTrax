"""
tests/test_challenger_adversarial_ip.py
Empirical Challenger Test Suite for Milestone 1 Iteration 2.

Focus:
1. Verification of assertion enforcement under Calibre -O2 optimization (check() vs bare assert).
2. Rigorous empirical stress-testing of is_safe_public_url() with IP literals and early return.
3. Boundary condition testing of IPv4 and IPv6 private/public subnet edges.
4. Adversarial obfuscated IP inputs (octal, hex, dword, IPv4-mapped IPv6).
5. Zero-DNS verification for valid IP literals (ensures offline resilience and performance).
"""

import sys
import os
import socket
from unittest.mock import patch, MagicMock

# Setup imports
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.security import is_safe_public_url, is_ip_disallowed, DISALLOWED_NETWORKS


def check(condition: bool, message: str = "") -> None:
    """Enforced assertion check that cannot be stripped under -O2."""
    if not condition:
        raise AssertionError(message or "Assertion check failed")


def test_calibre_o2_optimization_behavior():
    print("\n--- [CHALLENGER 1] Calibre -O2 Compiler & Assertion Behavior ---")
    print(f"  sys.flags.optimize: {sys.flags.optimize}")
    print(f"  __debug__: {__debug__}")

    # Under -O2, __debug__ must be False and optimize must be 2
    check(sys.flags.optimize == 2, f"Expected sys.flags.optimize == 2, got {sys.flags.optimize}")
    check(__debug__ is False, f"Expected __debug__ is False under Calibre, got {__debug__}")

    # Empirically demonstrate that check() raises AssertionError
    caught = False
    try:
        check(False, "Empirical invalidation test")
    except AssertionError as e:
        caught = True
        check("Empirical invalidation test" in str(e), "Error message corrupted")

    check(caught is True, "check() failed to raise AssertionError under -O2!")
    print("  ✓ Confirmed check() is strictly enforced and cannot be stripped by -O2 compiler.")


def test_ip_literals_zero_dns_early_return():
    print("\n--- [CHALLENGER 2] Zero-DNS Early Return for Safe IP Literals ---")
    
    def forbidden_dns_call(*args, **kwargs):
        raise AssertionError(f"Unexpected DNS resolution called for host: {args[0]}")

    safe_ip_urls = [
        # Standard public IPv4
        "http://1.1.1.1/",
        "http://1.0.0.1:8080/path",
        "https://8.8.8.8/",
        "https://8.8.4.4:443/dns-query",
        "http://93.184.216.34/news.atom",
        "http://151.101.1.140/",
        "http://208.67.222.222/",
        # Public IPv6 literals (bracketed)
        "http://[2606:4700:4700::1111]/",
        "https://[2001:4860:4860::8888]:443/",
        "http://[2606:4700:4700::aaaa]/feed.xml",
        "http://[2400:cb00:2048:1::c629:d7a2]/",
        # With credentials and query parameters
        "http://user:pass@1.1.1.1:80/feed?token=xyz#section",
        "https://reader:key@[2606:4700:4700::1111]:443/v1/update"
    ]

    with patch("socket.getaddrinfo", side_effect=forbidden_dns_call):
        for url in safe_ip_urls:
            is_safe = is_safe_public_url(url)
            check(is_safe is True, f"Safe IP literal unexpectedly blocked: {url}")
            print(f"  ✓ Verified safe IP literal bypasses DNS and returns True: {url}")


def test_ip_subnet_boundary_conditions():
    print("\n--- [CHALLENGER 3] Subnet Boundary Stress Testing (Edges & Neighbors) ---")

    # Pairs of (adjacent safe IP, disallowed boundary IP)
    test_boundaries = [
        # RFC 1918 10.0.0.0/8 boundary
        ("http://9.255.255.255/", True, "Pre-10.0.0.0 boundary (Safe)"),
        ("http://10.0.0.0/", False, "10.0.0.0 network address (Disallowed)"),
        ("http://10.0.0.1/", False, "10.0.0.1 first host (Disallowed)"),
        ("http://10.255.255.254/", False, "10.255.255.254 last host (Disallowed)"),
        ("http://10.255.255.255/", False, "10.255.255.255 broadcast (Disallowed)"),
        ("http://11.0.0.0/", True, "Post-10.255.255.255 boundary (Safe)"),

        # RFC 1918 172.16.0.0/12 boundary
        ("http://172.15.255.255/", True, "Pre-172.16.0.0 boundary (Safe)"),
        ("http://172.16.0.0/", False, "172.16.0.0 network address (Disallowed)"),
        ("http://172.31.255.255/", False, "172.31.255.255 broadcast (Disallowed)"),
        ("http://172.32.0.0/", True, "Post-172.31.255.255 boundary (Safe)"),

        # RFC 1918 192.168.0.0/16 boundary
        ("http://192.167.255.255/", True, "Pre-192.168.0.0 boundary (Safe)"),
        ("http://192.168.0.0/", False, "192.168.0.0 network address (Disallowed)"),
        ("http://192.168.255.255/", False, "192.168.255.255 broadcast (Disallowed)"),
        ("http://192.169.0.0/", True, "Post-192.168.255.255 boundary (Safe)"),

        # Carrier-Grade NAT 100.64.0.0/10 boundary
        ("http://100.63.255.255/", True, "Pre-100.64.0.0 boundary (Safe)"),
        ("http://100.64.0.0/", False, "100.64.0.0 network address (Disallowed)"),
        ("http://100.127.255.255/", False, "100.127.255.255 broadcast (Disallowed)"),
        ("http://100.128.0.0/", True, "Post-100.127.255.255 boundary (Safe)"),

        # Cloud Metadata & Link-Local 169.254.0.0/16 boundary
        ("http://169.253.255.255/", True, "Pre-169.254.0.0 boundary (Safe)"),
        ("http://169.254.0.0/", False, "169.254.0.0 network address (Disallowed)"),
        ("http://169.254.169.254/", False, "169.254.169.254 cloud metadata (Disallowed)"),
        ("http://169.254.255.255/", False, "169.254.255.255 broadcast (Disallowed)"),
        ("http://169.255.0.0/", True, "Post-169.254.255.255 boundary (Safe)"),

        # Multicast 224.0.0.0/4 & Reserved 240.0.0.0/4
        ("http://223.255.255.255/", True, "Pre-224.0.0.0 boundary (Safe)"),
        ("http://224.0.0.1/", False, "224.0.0.1 multicast (Disallowed)"),
        ("http://239.255.255.255/", False, "239.255.255.255 multicast end (Disallowed)"),
        ("http://240.0.0.1/", False, "240.0.0.1 reserved range (Disallowed)"),
        ("http://255.255.255.255/", False, "255.255.255.255 limited broadcast (Disallowed)"),

        # Loopback 127.0.0.0/8
        ("http://126.255.255.255/", True, "Pre-127.0.0.0 boundary (Safe)"),
        ("http://127.0.0.0/", False, "127.0.0.0 network address (Disallowed)"),
        ("http://127.0.0.1/", False, "127.0.0.1 loopback (Disallowed)"),
        ("http://127.255.255.255/", False, "127.255.255.255 loopback end (Disallowed)"),
        ("http://128.0.0.0/", True, "Post-127.255.255.255 boundary (Safe)"),
    ]

    for url, expected_safe, description in test_boundaries:
        result = is_safe_public_url(url)
        check(result == expected_safe, f"Boundary failure for {description} ({url}): expected {expected_safe}, got {result}")
        status_text = "ALLOWED" if result else "BLOCKED"
        print(f"  ✓ {status_text:7} [{description}]: {url}")


def test_adversarial_ipv6_variations():
    print("\n--- [CHALLENGER 4] Adversarial IPv6 Variations & Mappings ---")

    ipv6_adversarial = [
        # Loopback representations
        ("http://[::1]/", False, "IPv6 loopback"),
        ("http://[0:0:0:0:0:0:0:1]/", False, "IPv6 uncompressed loopback"),
        ("http://[0000:0000:0000:0000:0000:0000:0000:0001]/", False, "IPv6 full zero-padded loopback"),
        ("http://[::]/", False, "IPv6 unspecified address"),
        ("http://[0:0:0:0:0:0:0:0]/", False, "IPv6 uncompressed unspecified"),

        # Unique-Local Addresses (fc00::/7)
        ("http://[fc00::1]/", False, "IPv6 ULA prefix fc00"),
        ("http://[fd12:3456:789a::1]/", False, "IPv6 ULA prefix fd12"),
        ("http://[fdff:ffff:ffff:ffff:ffff:ffff:ffff:ffff]/", False, "IPv6 ULA upper limit"),

        # Link-Local Unicast (fe80::/10)
        ("http://[fe80::1]/", False, "IPv6 link-local prefix"),
        ("http://[febf:ffff:ffff:ffff:ffff:ffff:ffff:ffff]/", False, "IPv6 link-local upper limit"),

        # Multicast (ff00::/8)
        ("http://[ff02::1]/", False, "IPv6 multicast all-nodes"),
        ("http://[ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff]/", False, "IPv6 multicast upper limit"),

        # IPv4-mapped IPv6 (::ffff:0:0/96)
        ("http://[::ffff:127.0.0.1]/", False, "IPv4-mapped IPv6 loopback (dotted)"),
        ("http://[::ffff:7f00:1]/", False, "IPv4-mapped IPv6 loopback (hex)"),
        ("http://[::ffff:10.0.0.1]/", False, "IPv4-mapped IPv6 private 10.0.0.1"),
        ("http://[::ffff:192.168.1.1]/", False, "IPv4-mapped IPv6 private 192.168.1.1"),
        ("http://[::ffff:169.254.169.254]/", False, "IPv4-mapped IPv6 metadata"),
        ("http://[::ffff:1.1.1.1]/", False, "IPv4-mapped IPv6 in disallowed /96 network"),

        # NAT64 translation prefix (64:ff9b::/96)
        ("http://[64:ff9b::127.0.0.1]/", False, "NAT64 prefix with loopback"),
        ("http://[64:ff9b::192.168.1.1]/", False, "NAT64 prefix with private IP"),

        # Documentation (2001:db8::/32)
        ("http://[2001:db8::1]/", False, "IPv6 documentation prefix"),

        # Valid routable public IPv6
        ("http://[2606:4700:4700::1111]/", True, "Public Cloudflare IPv6 DNS"),
        ("http://[2001:4860:4860::8888]/", True, "Public Google IPv6 DNS"),
    ]

    for url, expected_safe, description in ipv6_adversarial:
        result = is_safe_public_url(url)
        check(result == expected_safe, f"IPv6 failure for {description} ({url}): expected {expected_safe}, got {result}")
        status_text = "ALLOWED" if result else "BLOCKED"
        print(f"  ✓ {status_text:7} [{description}]: {url}")


def test_obfuscated_and_nonstandard_ip_representations():
    print("\n--- [CHALLENGER 5] Obfuscated / Non-Standard IP Representations ---")

    # In Python 3.8+, ipaddress.ip_address() strictly disallows leading zeros (octal) and non-standard formats.
    # Therefore, these fall through to DNS resolution (socket.getaddrinfo).
    # On Windows, socket.getaddrinfo resolves octal/dword/hex representations to the underlying IPv4 address.
    # We verify that is_safe_public_url() blocks them either via DNS resolution check or via fail-closed behavior.

    obfuscated_ips = [
        # Octal representations of 127.0.0.1
        ("http://0177.0.0.1/", False, "Octal 0177.0.0.1"),
        ("http://0177.0000.0000.0001/", False, "Octal zero-padded"),
        # Decimal / Dword representation of 127.0.0.1 (2130706433)
        ("http://2130706433/", False, "Dword integer 2130706433"),
        # Hex representation of 127.0.0.1
        ("http://0x7f000001/", False, "Hex 0x7f000001"),
        ("http://0x7f.0.0.1/", False, "Dotted hex 0x7f.0.0.1"),
        # Decimal representation of cloud metadata 169.254.169.254 (2852039166)
        ("http://2852039166/", False, "Dword integer cloud metadata"),
        ("http://0xa9fea9fe/", False, "Hex cloud metadata"),
    ]

    for url, expected_safe, description in obfuscated_ips:
        result = is_safe_public_url(url)
        check(result == expected_safe, f"Obfuscated IP check failed for {description} ({url}): expected {expected_safe}, got {result}")
        print(f"  ✓ BLOCKED [{description}]: {url}")


def run_all_challenger_tests():
    print("======================================================================")
    print("RUNNING CHALLENGER ADVERSARIAL IP & ASSERTION TEST HARNESS (M1 IT2)")
    print("======================================================================")
    test_calibre_o2_optimization_behavior()
    test_ip_literals_zero_dns_early_return()
    test_ip_subnet_boundary_conditions()
    test_adversarial_ipv6_variations()
    test_obfuscated_and_nonstandard_ip_representations()
    print("\n======================================================================")
    print("ALL EMPIRICAL CHALLENGER STRESS TESTS PASSED (100% SUCCESS)!")
    print("======================================================================")


if __name__ == "__main__":
    run_all_challenger_tests()
