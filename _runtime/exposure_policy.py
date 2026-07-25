#!/usr/bin/env python3
"""Fail-closed network binding policy for the unauthenticated preview runtime."""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping


NETWORK_ACK_ENV = "MERGED_NETWORK_EXPOSURE_ACK"
NETWORK_ACK_VALUE = "I_UNDERSTAND_THIS_RUNTIME_HAS_NO_AUTH"


def is_loopback_host(host: str) -> bool:
    normalized = str(host).strip().lower().strip("[]")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def validate_bind_host(host: str, environment: Mapping[str, str]) -> None:
    """Require an explicit acknowledgement before any non-loopback bind."""
    if is_loopback_host(host):
        return
    if environment.get(NETWORK_ACK_ENV) == NETWORK_ACK_VALUE:
        return
    raise RuntimeError(
        f"Refusing non-loopback MERGED_HOST={host!r}: this preview has no authentication. "
        f"Use loopback, or set {NETWORK_ACK_ENV}={NETWORK_ACK_VALUE} only on an isolated trusted network."
    )
