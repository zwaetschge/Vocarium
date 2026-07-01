"""Shared outbound URL validation helpers.

These checks are used for user-configurable server-side HTTP targets. They
intentionally block loopback, link-local, private, reserved, and other
non-global addresses unless an operator explicitly allowlists them.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse


class URLValidationError(ValueError):
    """Raised when a user-supplied outbound URL is unsafe."""


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _csv(name: str) -> set[str]:
    return {
        item.strip().lower()
        for item in (os.environ.get(name, "") or "").split(",")
        if item.strip()
    }


def _host_allowed(host: str, allowed_hosts_env: str) -> bool:
    allowed = _csv(allowed_hosts_env)
    if not allowed:
        return False
    host_l = host.lower()
    return "*" in allowed or host_l in allowed


def _raise_for_non_global_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, field: str) -> None:
    if not ip.is_global:
        raise URLValidationError(
            f"{field} must not target private, loopback, link-local, or reserved addresses"
        )


def normalize_http_base_url(
    raw_url: str,
    *,
    field: str = "base_url",
    allow_private_env: str = "ALLOW_PRIVATE_OUTBOUND_URLS",
    allowed_hosts_env: str = "ALLOWED_PRIVATE_OUTBOUND_HOSTS",
    validate_dns_env: str = "VALIDATE_OUTBOUND_URL_DNS",
) -> str:
    """Normalize and validate a user-configurable http(s) base URL."""
    value = (raw_url or "").strip().rstrip("/")
    if not value:
        raise URLValidationError(f"{field} is required")

    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise URLValidationError(f"{field} must be an http(s) URL")

    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise URLValidationError(f"{field} host is required")

    if _env_bool(allow_private_env, False) or _host_allowed(host, allowed_hosts_env):
        return value

    if host == "localhost" or host.endswith(".localhost"):
        raise URLValidationError(f"{field} must not target localhost")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None

    if ip is not None:
        _raise_for_non_global_ip(ip, field)
        return value

    if not _env_bool(validate_dns_env, True):
        return value

    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise URLValidationError(f"{field} host could not be resolved: {exc}") from exc

    resolved = {item[4][0] for item in infos if item and len(item) >= 5 and item[4]}
    if not resolved:
        raise URLValidationError(f"{field} host could not be resolved")

    for address in resolved:
        try:
            resolved_ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise URLValidationError(f"{field} resolved to an invalid address") from exc
        _raise_for_non_global_ip(resolved_ip, field)

    return value
