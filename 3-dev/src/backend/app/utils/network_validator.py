"""Network address validation to prevent SSRF attacks.

Defence layers:
1. Block well-known private/loopback hostnames (localhost, *.local, etc.)
2. Block literal private/loopback IPs (IPv4 + IPv6)
3. Resolve hostnames via DNS and block if *any* resolved address is private
"""

import ipaddress
import socket
from urllib.parse import urlparse

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    # 100.64.0.0/10 (CGNAT) intentionally NOT blocked — used by Tailscale/ZeroTier
    ipaddress.ip_network("192.0.0.0/24"),      # IETF protocol assignments
    ipaddress.ip_network("198.18.0.0/15"),     # Benchmark testing
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::ffff:127.0.0.0/104"),
]

_BLOCKED_HOSTNAME_SUFFIXES = (
    "localhost",
    ".local",
    ".internal",
    ".localdomain",
    ".home.arpa",
    ".intranet",
    ".corp",
    ".lan",
)

_ALLOWED_PORT_RANGE = (1, 65535)


def _is_ip_blocked(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(addr in net for net in _BLOCKED_NETWORKS)


def _is_hostname_blocked(hostname: str) -> bool:
    """Block well-known private/loopback hostnames without DNS."""
    h = hostname.lower().rstrip(".")
    if h == "localhost" or h.endswith(".localhost"):
        return True
    return any(h.endswith(suffix) for suffix in _BLOCKED_HOSTNAME_SUFFIXES)


def is_private_ip(host: str) -> bool:
    """Check if a host string is or resolves to a private/internal address.

    Handles literal IPs, localhost, internal domain names, and performs
    DNS resolution to catch hostnames that resolve to private ranges.
    """
    if _is_hostname_blocked(host):
        return True

    try:
        addr = ipaddress.ip_address(host)
        return _is_ip_blocked(addr)
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for family, _type, _proto, _canon, sockaddr in infos:
            ip_str = sockaddr[0]
            try:
                addr = ipaddress.ip_address(ip_str)
                if _is_ip_blocked(addr):
                    return True
            except ValueError:
                continue
    except socket.gaierror:
        return True

    return False


async def is_private_ip_async(host: str) -> bool:
    """Async wrapper — runs DNS resolution in a thread to avoid blocking."""
    import asyncio
    return await asyncio.to_thread(is_private_ip, host)


def validate_server_address(host: str, port: int) -> str | None:
    """Validate server host/port for SSRF. Returns error message or None if valid."""
    if is_private_ip(host):
        return f"Registration of private/loopback IP addresses is not allowed: {host}"

    if not _ALLOWED_PORT_RANGE[0] <= port <= _ALLOWED_PORT_RANGE[1]:
        return f"Port {port} is out of allowed range"

    return None


async def validate_server_address_async(host: str, port: int) -> str | None:
    """Async version of validate_server_address — uses async DNS check."""
    if await is_private_ip_async(host):
        return f"Registration of private/loopback IP addresses is not allowed: {host}"

    if not _ALLOWED_PORT_RANGE[0] <= port <= _ALLOWED_PORT_RANGE[1]:
        return f"Port {port} is out of allowed range"

    return None


def validate_callback_url(url: str) -> str | None:
    """Validate callback URL for SSRF. Returns error message or None if valid."""
    try:
        parsed = urlparse(url)
    except Exception:
        return "Invalid callback URL format"

    if parsed.scheme not in ("http", "https"):
        return f"Callback URL scheme must be http or https, got: {parsed.scheme}"

    hostname = parsed.hostname
    if not hostname:
        return "Callback URL has no hostname"

    if is_private_ip(hostname):
        return f"Callback URL must not point to private/internal addresses: {hostname}"

    return None


async def validate_callback_url_async(url: str) -> str | None:
    """Async version of validate_callback_url — uses async DNS check."""
    try:
        parsed = urlparse(url)
    except Exception:
        return "Invalid callback URL format"

    if parsed.scheme not in ("http", "https"):
        return f"Callback URL scheme must be http or https, got: {parsed.scheme}"

    hostname = parsed.hostname
    if not hostname:
        return "Callback URL has no hostname"

    if await is_private_ip_async(hostname):
        return f"Callback URL must not point to private/internal addresses: {hostname}"

    return None
