"""
Network Scanner Plugin — Discovers local network devices with auto-refresh.

Uses nmap -sn (ping scan, no root required) to find devices, then enriches
each device with hostname via DNS reverse + NetBIOS (nmblookup) + mDNS (avahi).
Auto-refreshes every 60 seconds. Supports manual scan trigger.
"""

import re
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from plugins.base import DashboardPlugin


# ── MAC Vendor DB ──────────────────────────────────────────────────────────
MAC_VENDORS: dict[str, str] = {
    "30:83:98": "Espressif", "24:0a:c4": "Espressif", "24:62:ab": "Espressif",
    "a4:cf:12": "Espressif", "b4:e6:2d": "Espressif", "7c:9e:bd": "Espressif",
    "ac:15:a2": "TP-Link", "50:c7:bf": "TP-Link", "60:32:b1": "TP-Link",
    "9c:a6:15": "TP-Link",
    "60:7d:09": "Lenovo", "f8:b0:27": "Lenovo", "3c:97:26": "Lenovo",
    "a4:17:31": "Intel", "f8:da:0c": "Intel", "70:66:55": "Intel",
    "98:a8:29": "Samsung", "a0:cb:fd": "Samsung", "b0:be:76": "Samsung",
    "ac:23:3d": "Apple", "a4:83:e7": "Apple", "3c:22:fb": "Apple",
    "82:1a:c3": "Realme/Oppo", "ce:89:91": "Realme/Oppo",
    "14:ca:56": "Gateway", "6c:c8:40": "Gateway",
    "ac:a7:04": "Wireless",
    # More vendors
    "b8:27:eb": "Raspberry Pi", "dc:a6:32": "Raspberry Pi", "e4:5f:01": "Raspberry Pi",
    "00:1a:79": "Dell", "00:e0:4c": "Realtek",
    "f0:4a:5d": "LG", "a8:23:fe": "LG",
    "00:50:56": "VMware", "00:0c:29": "VMware", "00:05:69": "VMware",
    "52:54:00": "KVM/QEMU",
}

# ── Known device aliases (IP → friendly name) ─────────────────────────────
DEVICE_ALIASES: dict[str, str] = {}


def _lookup_vendor(mac: str) -> str:
    """Look up vendor from first 3 octets of MAC."""
    if not mac or mac == "N/A":
        return ""
    prefix = mac.lower()[:8]
    return MAC_VENDORS.get(prefix, "")


def _detect_device_type(mac: str, hostname: str = "", vendor: str = "") -> str:
    """Detect device type from MAC vendor, hostname, and known patterns."""
    h = (hostname or "").lower()
    v = vendor.lower()
    m = mac.lower() if mac else ""

    if "espressif" in v or m.startswith("30:83:98"):
        return "IoT"
    if "raspberry" in v:
        return "IoT"
    if "tp-link" in v or "gateway" in v:
        return "ROUTER"
    if m.startswith("14:ca:56") or m.startswith("6c:c8:40"):
        return "ROUTER"
    if "samsung" in v or "apple" in v or "realme" in v or "oppo" in v:
        return "PHONE"
    if "lenovo" in v or "dell" in v:
        return "PC"
    if any(x in h for x in ["iphone", "galaxy", "phone", "pixel", "redmi", "oppo"]):
        return "PHONE"
    if any(x in h for x in ["desktop", "laptop", "pc-", "windows", "macbook", "linux"]):
        return "PC"
    if "linuxmint" in h or h.endswith("server") or h == "gateway":
        return "SERVER"
    if "_gateway" in h:
        return "ROUTER"
    return "UNKNOWN"


# ── Hostname resolution (multi-method) ────────────────────────────────────

def _resolve_dns(ip: str) -> str | None:
    """DNS reverse lookup."""
    try:
        name, _, _ = socket.gethostbyaddr(ip)
        name = name.split('.')[0]
        if name and not name.replace('.', '').isdigit():
            return name
    except (socket.herror, socket.gaierror, OSError):
        pass
    return None


def _resolve_netbios(ip: str) -> str | None:
    """NetBIOS name lookup via nmblookup (finds Windows/Linux names)."""
    try:
        result = subprocess.run(
            ["nmblookup", "-A", ip],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                # Match lines like: "\tHOSTNAME       <00> -         B <ACTIVE>"
                match = re.match(r'\s+(\S+)\s+<00>\s+-\s+\w\s+<ACTIVE>', line)
                if match:
                    name = match.group(1)
                    # Skip workgroup names (usually all caps)
                    if name == "WORKGROUP" or name == "MSHOME":
                        continue
                    return name
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _resolve_mdns(ip: str) -> str | None:
    """mDNS hostname via avahi-resolve."""
    try:
        result = subprocess.run(
            ["avahi-resolve", "-a", ip],
            capture_output=True, text=True, timeout=2
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split()
            if len(parts) >= 2:
                name = parts[1].split('.')[0]
                if name and not name.replace('.', '').isdigit():
                    return name
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _resolve_all(ip: str) -> str:
    """Try all hostname resolution methods, return best result."""
    # Check aliases first
    if ip in DEVICE_ALIASES:
        return DEVICE_ALIASES[ip]

    # Try in order: nmap already gave us some, then NetBIOS (best for Windows), then mDNS, then DNS
    for resolver in [_resolve_netbios, _resolve_mdns, _resolve_dns]:
        name = resolver(ip)
        if name:
            return name
    return ""


# ── Plugin Class ───────────────────────────────────────────────────────────

class NetworkScannerPlugin(DashboardPlugin):
    @property
    def name(self) -> str:
        return "network-scanner"

    @property
    def display_name(self) -> str:
        return "Network Scanner"

    @property
    def tab_id(self) -> str:
        return "network-scanner"

    def tab_html(self) -> str:
        return "<!-- Network Scanner embedded in Operations page -->"

    def scripts_html(self) -> str:
        return ""

    def styles_html(self) -> str:
        return ""

    def register_routes(self, app: Any) -> None:
        @app.get("/api/network-scanner/devices")
        async def get_devices():
            try:
                force = False  # use cache
                devices = _scan_network(force=force)
                return {"devices": devices}
            except Exception as e:
                return {"devices": [], "error": str(e)}

        @app.post("/api/network-scanner/scan")
        async def force_scan():
            """Force a fresh scan ignoring cache."""
            try:
                devices = _scan_network(force=True)
                return {"devices": devices}
            except Exception as e:
                return {"devices": [], "error": str(e)}

    def on_load(self) -> None:
        pass


# ── Scanner Logic ─────────────────────────────────────────────────────────

_last_scan: float = 0
_scan_results: list[dict] = []
_local_ip: str = ""
_subnet: str = ""


def _get_local_ip() -> str:
    global _local_ip
    if _local_ip:
        return _local_ip
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        _local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        _local_ip = "192.168.1.108"
    return _local_ip


def _get_subnet() -> str:
    global _subnet
    if _subnet:
        return _subnet
    ip = _get_local_ip()
    prefix = ".".join(ip.split(".")[:3])
    _subnet = f"{prefix}.0/24"
    return _subnet


def _ip_to_mac(ip: str) -> str | None:
    """Look up MAC from ARP table."""
    try:
        with open("/proc/net/arp") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) >= 4 and parts[0] == ip:
                    mac = parts[3]
                    if mac != "00:00:00:00:00:00":
                        return mac
    except Exception:
        pass
    return None


def _get_iface() -> str:
    """Get the primary network interface name."""
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                parts = line.strip().split()
                if len(parts) >= 8 and parts[1] == "00000000":  # default route
                    return parts[0]
    except Exception:
        pass
    return "wlp7s0"


def _enrich_device(ip: str, nmap_hostname: str | None, local_ip: str) -> dict | None:
    """Enrich a single device with all available info."""
    if ip == local_ip:
        # Try to get own MAC from sysfs
        try:
            with open(f"/sys/class/net/{_get_iface()}/address") as f:
                mac = f.read().strip()
        except Exception:
            mac = _ip_to_mac(ip) or "N/A"
        return {
            "ip": ip, "mac": mac,
            "hostname": socket.gethostname(),
            "vendor": _lookup_vendor(mac) or "This Server", "type": "SERVER", "online": True,
        }

    mac = _ip_to_mac(ip) or "N/A"
    vendor = _lookup_vendor(mac) if mac != "N/A" else ""

    # Filter out bogus nmap hostnames (IP-as-hostname, "_gateway")
    hostname = ""
    if nmap_hostname:
        # Skip if hostname is just the IP address
        if nmap_hostname != ip and not nmap_hostname.replace(".", "").isdigit():
            if not nmap_hostname.startswith("_"):
                hostname = nmap_hostname

    if not hostname:
        hostname = _resolve_all(ip)

    # If still no hostname, generate a friendly name from vendor + type
    if not hostname:
        dev_type = _detect_device_type(mac, hostname, vendor)
        if vendor:
            hostname = vendor
        else:
            hostname = ip

    dev_type = _detect_device_type(mac, hostname, vendor)

    return {
        "ip": ip, "mac": mac, "hostname": hostname,
        "vendor": vendor, "type": dev_type, "online": True,
    }


def _scan_network(force: bool = False) -> list[dict[str, Any]]:
    """
    Scan local network. Uses cache (60s) unless force=True.
    Strategy: nmap -sn (no root) → /proc/net/arp fallback.
    Then enriches all devices with hostnames in parallel.
    """
    global _last_scan, _scan_results

    if not force and _scan_results and time.time() - _last_scan < 60:
        return _scan_results

    _last_scan = time.time()
    local_ip = _get_local_ip()
    subnet = _get_subnet()

    # Collect IPs from nmap
    raw_devices: list[tuple[str, str | None]] = []  # (ip, nmap_hostname)

    try:
        result = subprocess.run(
            ["nmap", "-sn", subnet],
            capture_output=True, text=True, timeout=20
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.startswith("Nmap scan report for "):
                    rest = line[len("Nmap scan report for "):]
                    m = re.match(r'(.+)\s+\((\d+\.\d+\.\d+\.\d+)\)', rest)
                    if m:
                        raw_devices.append((m.group(2), m.group(1).strip()))
                    else:
                        raw_devices.append((rest.strip(), None))
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback to ARP table
    if not raw_devices:
        try:
            with open("/proc/net/arp") as f:
                for line in f.readlines()[1:]:
                    parts = line.split()
                    if len(parts) >= 6:
                        ip, mac = parts[0], parts[3]
                        if mac != "00:00:00:00:00:00":
                            raw_devices.append((ip, None))
        except Exception:
            pass

    # Enrich all devices in parallel (NetBIOS/mDNS are slow per-IP)
    devices: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_enrich_device, ip, nhost, local_ip): ip
            for ip, nhost in raw_devices
        }
        for future in as_completed(futures, timeout=30):
            try:
                dev = future.result(timeout=5)
                if dev:
                    devices.append(dev)
            except Exception:
                pass

    # Sort: SERVER first, then ROUTER, PC, PHONE, IOT, UNKNOWN
    type_order = {"SERVER": 0, "ROUTER": 1, "PC": 2, "PHONE": 3, "IoT": 4, "UNKNOWN": 5}
    devices.sort(key=lambda d: (type_order.get(d.get("type", "UNKNOWN"), 9),
                                d.get("hostname", "") or d.get("ip", "")))

    _scan_results = devices
    return devices
