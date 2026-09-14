"""This machine's LAN addresses, for the desktop build.

VelociDrone listens on the PC's LAN address and never on loopback, so when
Splitter runs on the same PC it still needs that address. The address on the
default route is what the game binds in practice; the full list lets a PC with
several adapters (Wi-Fi + Ethernet, a VPN, Hyper-V) pick another one.
"""

from __future__ import annotations

import socket


def default_route_ipv4() -> str | None:
    """The IPv4 address of the interface that carries the default route.

    A UDP socket "connected" to a public address never sends anything; the OS
    just picks the outgoing interface, and ``getsockname`` reveals its address.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1: never routed, never sent to
            ip = str(s.getsockname()[0])
    except OSError:
        return None
    return ip if _usable(ip) else None


def local_ipv4_addresses() -> list[str]:
    """Every usable IPv4 address of this machine, default-route one first."""
    out: list[str] = []
    first = default_route_ipv4()
    if first:
        out.append(first)
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    for info in infos:
        ip = str(info[4][0])
        if _usable(ip) and ip not in out:
            out.append(ip)
    return out


def _usable(ip: str) -> bool:
    return (
        bool(ip) and not ip.startswith("127.") and not ip.startswith("169.254.") and ip != "0.0.0.0"
    )
