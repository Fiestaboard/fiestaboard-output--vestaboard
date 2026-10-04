"""The Vestaboard ``discover`` hook: find Vestaboards on the LAN.

``POST /config/board/scan`` and the board settings "Scan network" action
reach it through FiestaBoard's ``discover_devices`` (the output's
``discover`` classmethod). It probes the /24 of the browser's address
(*hint*: FiestaBoard's ``hint_host``, the private IPv4 address the user
opened FiestaBoard at), then the /24 of FiestaBoard's own ``local_ipv4``. In
Docker bridge mode FiestaBoard's own address is a container network, so the
hint is what finds a board there.
"""

import ipaddress
import logging
import socket
import threading
from typing import Any

from src.plugins import local_ipv4

from .transport import LOCAL_API_PORT as _VESTABOARD_LOCAL_API_PORT

logger = logging.getLogger(__name__)

# mDNS service types to browse when looking for Vestaboards
_BROWSE_SERVICE_TYPES = [
    "_vestaboard._tcp.local.",
    "_http._tcp.local.",
]


def _probe_vestaboard_port(ip: str, port: int = _VESTABOARD_LOCAL_API_PORT, timeout: float = 0.5) -> bool:
    """Return True if *ip*:*port* accepts a TCP connection."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect((ip, port))
            return True
    except (TimeoutError, OSError):
        return False


#: The networks a hint may name: RFC 1918 and IPv4 link-local.
_HINT_NETWORKS = tuple(
    ipaddress.IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")
)


def _lan_address(value: Any) -> str | None:
    """*value* when it is a private or link-local IPv4 address, else ``None``."""
    try:
        address = ipaddress.IPv4Address(str(value).strip())
    except ValueError:
        return None
    return str(address) if any(address in network for network in _HINT_NETWORKS) else None


def _probe_prefixes(hint: str | None, local_ip: str | None) -> list[str]:
    """The /24 prefixes (``"192.168.1"``) to probe, in order: the hint's, then this host's."""
    prefixes: list[str] = []
    for address in (_lan_address(hint) if hint else None, local_ip if local_ip != "127.0.0.1" else None):
        if address:
            prefix = ".".join(address.split(".")[:3])
            if prefix not in prefixes:
                prefixes.append(prefix)
    return prefixes


def discover(timeout: float = 4.0, hint: str | None = None) -> list[dict[str, Any]]:
    """Scan the local network for Vestaboard devices.

    Uses two complementary strategies:
    1. **mDNS browse** – listens for ``_vestaboard._tcp`` and ``_http._tcp``
       service advertisements.  Any service whose name contains "vestaboard"
       (case-insensitive) or that has port 7000 is included.
    2. **Subnet port probe** – iterates over the /24 subnet of *hint*, then
       that of this host, and checks whether port 7000 (Vestaboard Local
       API) is open.

    Args:
        timeout: How long (seconds) to wait for mDNS responses and port
            probes.  The mDNS browse phase uses the full *timeout*; the
            port-probe phase uses a 0.5 s connect timeout per host.
        hint: The private IPv4 address the user opened FiestaBoard at, or
            ``None``. Anything else (a hostname, a public address) is ignored.

    Returns:
        A list of dicts, each with at least ``ip`` and ``port`` keys plus
        optional ``hostname`` and ``source`` fields.
    """
    seen_ips: set = set()
    results: list[dict[str, Any]] = []

    # -- Phase 1: mDNS browse ------------------------------------------------
    try:
        import time

        from zeroconf import ServiceBrowser, Zeroconf

        discovered: list[dict[str, Any]] = []
        lock = threading.Lock()

        class _Listener:
            """Collect service info as boards are discovered."""

            def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
                info = zc.get_service_info(type_, name)
                if info is None:
                    return
                addresses = info.parsed_addresses()
                port = info.port
                hostname = (info.server or "").rstrip(".")
                svc_name = name.lower()
                is_vestaboard = "vestaboard" in svc_name or port == _VESTABOARD_LOCAL_API_PORT
                if is_vestaboard:
                    for addr in addresses:
                        with lock:
                            discovered.append(
                                {
                                    "ip": addr,
                                    "port": port,
                                    "hostname": hostname,
                                    "source": "mdns",
                                }
                            )

            def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
                pass

            def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
                pass

        zc = Zeroconf()
        listener = _Listener()
        browsers = [ServiceBrowser(zc, stype, listener) for stype in _BROWSE_SERVICE_TYPES]

        time.sleep(timeout)
        for browser in browsers:
            browser.cancel()
        zc.close()

        for entry in discovered:
            ip = entry["ip"]
            if ip not in seen_ips:
                seen_ips.add(ip)
                results.append(entry)

    except ImportError:
        logger.debug("zeroconf not installed – skipping mDNS browse phase")
    except Exception:
        logger.warning("mDNS browse phase failed", exc_info=True)

    # -- Phase 2: subnet port probe ------------------------------------------
    try:
        from concurrent.futures import ThreadPoolExecutor

        local_ip = local_ipv4()
        own = {local_ip, _lan_address(hint) if hint else None}
        prefixes = _probe_prefixes(hint, local_ip)
        if prefixes:
            probe_results: list[str] = []
            probe_lock = threading.Lock()

            def _check(ip: str) -> None:
                if _probe_vestaboard_port(ip):
                    with probe_lock:
                        probe_results.append(ip)

            candidates = [
                f"{prefix}.{i}"
                for prefix in prefixes
                for i in range(1, 255)
                if f"{prefix}.{i}" not in own and f"{prefix}.{i}" not in seen_ips
            ]

            with ThreadPoolExecutor(max_workers=50) as pool:
                pool.map(_check, candidates)

            for ip in probe_results:
                if ip not in seen_ips:
                    seen_ips.add(ip)
                    results.append(
                        {
                            "ip": ip,
                            "port": _VESTABOARD_LOCAL_API_PORT,
                            "hostname": "",
                            "source": "port_scan",
                        }
                    )
    except Exception:
        logger.warning("Subnet port-probe phase failed", exc_info=True)

    logger.info("Board scan complete: found %d device(s)", len(results))
    return results
