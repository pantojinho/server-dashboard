#!/usr/bin/env python3
"""
Hermes Server Dashboard — Retro terminal-style monitoring web dashboard.
Backend: FastAPI serving system metrics + Hermes status via JSON API.
"""

import json
import os
import re
import base64
import platform
import secrets
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psutil
import uvicorn
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

# Metrics TSDB
import metrics_tsdb

# Plugin system
import plugins as plugin_system

# ── Configuration ────────────────────────────────────────────────────
DASHBOARD_DIR = Path(__file__).parent
STATIC_DIR = DASHBOARD_DIR / "static"


def _load_config() -> dict:
    """Load config.yaml, falling back to config.example.yaml defaults."""
    cfg_path = DASHBOARD_DIR / "config.yaml"
    example_path = DASHBOARD_DIR / "config.example.yaml"
    path = cfg_path if cfg_path.exists() else example_path
    if path.exists():
        try:
            with open(path) as f:
                return yaml.safe_load(f) or {}
        except Exception:
            pass
    return {}


CFG = _load_config()
_hermes_home_config = CFG.get("hermes", {}).get(
    "home",
    CFG.get("hermes_home", str(Path.home() / ".hermes")),
)
HERMES_HOME = Path(os.environ.get("HERMES_HOME", _hermes_home_config)).expanduser()
HISTORY_LEN = CFG.get("metrics", {}).get("history_length", 60)


def _normalize_service_name(name: str) -> str:
    """Return a systemd unit name without duplicating the .service suffix."""
    unit = str(name or "").strip()
    if not unit:
        return unit
    return unit if unit.endswith(".service") else f"{unit}.service"


def _service_key(name: str) -> str:
    """Return a stable short key for service lookups and API payloads."""
    unit = _normalize_service_name(name)
    return unit[:-8] if unit.endswith(".service") else unit


def _configured_service_scope(service_name: str, default: str = "system") -> str:
    """Find a configured service scope by short name or full systemd unit."""
    wanted_unit = _normalize_service_name(service_name)
    wanted_key = _service_key(service_name)
    for svc in CFG.get("services", []):
        configured_name = svc.get("name", "")
        configured_unit = _normalize_service_name(configured_name)
        if configured_unit == wanted_unit or _service_key(configured_name) == wanted_key:
            return svc.get("scope", default)
    return default


def _run_systemctl_is_active(service_name: str, scope: str, timeout: int = 3) -> subprocess.CompletedProcess:
    cmd = ["systemctl"]
    env = None
    if scope == "user":
        cmd.append("--user")
        env = _user_env()
    cmd.extend(["is-active", _normalize_service_name(service_name)])
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)

# Hermes gateway service name (configurable for non-standard setups)
HERMES_GATEWAY_SERVICE = _normalize_service_name(os.environ.get(
    "HERMES_GATEWAY_SERVICE",
    CFG.get("hermes", {}).get(
        "gateway_service",
        CFG.get("gateway_service", "hermes-gateway.service"),
    ),
))
HERMES_GATEWAY_SCOPE = os.environ.get(
    "HERMES_GATEWAY_SCOPE",
    CFG.get("hermes", {}).get(
        "gateway_scope",
        CFG.get("gateway_scope", _configured_service_scope(HERMES_GATEWAY_SERVICE, "user")),
    ),
)

# Sudo configuration — password from env var, NEVER hardcoded
SUDO_MODE = CFG.get("sudo_mode", "none")
SUDO_PASSWORD = os.environ.get("SUDO_PASSWORD", "")

# Optional Basic Auth. Disabled unless a password is configured.
AUTH_CFG = CFG.get("auth", {})
DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", AUTH_CFG.get("username", "admin"))
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", AUTH_CFG.get("password", ""))

# Build allowed services from config
ALLOWED_RESTART_SERVICES = {}
for svc in CFG.get("services", []):
    if svc.get("allow_restart", False):
        name = _service_key(svc["name"])
        scope = svc.get("scope", "system")
        ALLOWED_RESTART_SERVICES[name] = (scope, _normalize_service_name(svc["name"]))


def _cfg_server(key: str, default: Any = None) -> Any:
    return CFG.get("server", {}).get(key, default)


SERVER_HOST = _cfg_server("host", "0.0.0.0")
SERVER_PORT = _cfg_server("port", 18791)
SERVER_LOG_LEVEL = _cfg_server("log_level", "warning")


app = FastAPI(
    title="Hermes Server Dashboard",
    description=(
        "Retro terminal-style monitoring web dashboard for Linux servers, "
        "Hermes Agent instances, and trading bots. Provides real-time system "
        "metrics, service management, time-series charts, and a plugin system "
        "for custom integrations."
    ),
    version="2.0.0",
    contact={
        "name": "Hermes Server Dashboard",
        "url": "https://github.com/pantojinho/hermes-server-dashboard",
    },
    license_info={"name": "MIT"},
    openapi_tags=[
        {
            "name": "System",
            "description": "Hardware and OS-level metrics — CPU, memory, disk, network, temperatures, Docker containers, and network connectivity.",
        },
        {
            "name": "Services",
            "description": "Systemd service monitoring, restart operations, and journalctl log retrieval.",
        },
        {
            "name": "Hermes",
            "description": "Hermes Agent status — model, provider, platforms, cron jobs, sessions, kanban tasks, and configuration management.",
        },
        {
            "name": "Metrics",
            "description": "Time-series data (TSDB) — historical chart series, collector status, and aggregated dashboard payloads.",
        },
        {
            "name": "Plugins",
            "description": "Plugin discovery and tab content retrieval for dashboard extensions.",
        },
    ],
)


def _auth_required_response() -> Response:
    return Response(
        "Authentication required",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="Hermes Server Dashboard"'},
    )


@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    """Enable Basic Auth when DASHBOARD_PASSWORD or auth.password is configured."""
    if not DASHBOARD_PASSWORD:
        return await call_next(request)

    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return _auth_required_response()

    try:
        decoded = base64.b64decode(header.removeprefix("Basic ").strip()).decode("utf-8")
        username, password = decoded.split(":", 1)
    except Exception:
        return _auth_required_response()

    user_ok = secrets.compare_digest(username, str(DASHBOARD_USERNAME))
    pass_ok = secrets.compare_digest(password, str(DASHBOARD_PASSWORD))
    if not (user_ok and pass_ok):
        return _auth_required_response()

    return await call_next(request)


# CORS from config
_cors_cfg = CFG.get("cors", {})
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_cfg.get("allow_origins", ["*"]),
    allow_credentials=True,
    allow_methods=_cors_cfg.get("allow_methods", ["*"]),
    allow_headers=_cors_cfg.get("allow_headers", ["*"]),
)

# Load plugins and register their routes
_loaded_plugins = plugin_system.load_plugins()
for _p in _loaded_plugins:
    try:
        _p.register_routes(app)
    except Exception as e:
        import logging
        logging.getLogger("plugins").error(f"Plugin {_p.name} route registration failed: {e}")

# TTL Cache for expensive endpoints
_api_cache = {}
def _cached(key, ttl, fn, *args, **kwargs):
    now = time.time()
    entry = _api_cache.get(key)
    if entry and now - entry[0] < ttl:
        return entry[1]
    result = fn(*args, **kwargs)
    _api_cache[key] = (now, result)
    return result

# Rolling history for sparklines
temp_history: list[float] = []
cpu_history: list[float] = []
net_rx_history: list[float] = []
net_tx_history: list[float] = []
last_net = psutil.net_io_counters()
last_net_time = time.time()


def _get_loadavg() -> tuple[float, float, float]:
    try:
        return os.getloadavg()
    except (AttributeError, OSError):
        return (0.0, 0.0, 0.0)


def _system_name() -> tuple[str, str, str]:
    uname = getattr(os, "uname", None)
    if uname:
        u = uname()
        return u.nodename, u.sysname, u.release
    return platform.node(), platform.system(), platform.release()


# ── System metric collectors ──────────────────────────────────────────

def get_temperatures() -> dict:
    """Parse 'sensors' output for temperatures and fan."""
    temps = {}
    fan = {}
    try:
        output = subprocess.check_output(["sensors", "-j"], text=True, timeout=5)
        data = json.loads(output)
        for chip, readings in data.items():
            for key, val in readings.items():
                if key.endswith("_input"):
                    name = key.replace("_input", "").replace("_", " ").title()
                    if "temp" in key.lower():
                        temps[name] = round(val, 1)
                    elif "fan" in key.lower():
                        fan[name] = round(val)
    except Exception:
        try:
            output = subprocess.check_output(["sensors"], text=True, timeout=5)
            for line in output.splitlines():
                if ":" in line and ("°C" in line or "RPM" in line):
                    parts = line.split(":")
                    name = parts[0].strip()
                    val_part = parts[1].strip()
                    if "°C" in val_part:
                        match = re.search(r"([+-]?\d+\.?\d*)°C", val_part)
                        if match:
                            temps[name] = float(match.group(1))
                    elif "RPM" in val_part:
                        match = re.search(r"(\d+)\s*RPM", val_part)
                        if match:
                            fan[name] = int(match.group(1))
        except Exception:
            pass
    return {"temps": temps, "fan": fan}


def get_system_metrics() -> dict:
    """Collect all system metrics."""
    global last_net, last_net_time

    cpu_percent = psutil.cpu_percent(interval=None)  # Non-blocking since first call was at startup
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage("/")
    uptime_seconds = time.time() - psutil.boot_time()

    # Network I/O
    net = psutil.net_io_counters()
    now = time.time()
    dt = now - last_net_time if last_net_time > 0 else 1
    rx_rate = (net.bytes_recv - last_net.bytes_recv) / dt if dt > 0 else 0
    tx_rate = (net.bytes_sent - last_net.bytes_sent) / dt if dt > 0 else 0
    last_net = net
    last_net_time = now

    # Temperature
    temp_data = get_temperatures()
    cpu_temp = temp_data["temps"].get("Package Id 0") or temp_data["temps"].get("Cpu") or temp_data["temps"].get("Tctl") or 0
    if isinstance(cpu_temp, (int, float)) and cpu_temp > 0:
        temp_history.append(cpu_temp)
        if len(temp_history) > HISTORY_LEN:
            temp_history.pop(0)

    cpu_history.append(cpu_percent)
    if len(cpu_history) > HISTORY_LEN:
        cpu_history.pop(0)

    net_rx_history.append(rx_rate)
    if len(net_rx_history) > HISTORY_LEN:
        net_rx_history.pop(0)
    net_tx_history.append(tx_rate)
    if len(net_tx_history) > HISTORY_LEN:
        net_tx_history.pop(0)

    # Format uptime
    hours, remainder = divmod(int(uptime_seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 24:
        days, hours = divmod(hours, 24)
        uptime_str = f"{days}d {hours}h {minutes}m"
    elif hours > 0:
        uptime_str = f"{hours}h {minutes}m"
    else:
        uptime_str = f"{minutes}m {seconds}s"

    load1, load5, load15 = _get_loadavg()

    hostname, os_name, os_release = _system_name()

    return {
        "cpu": {
            "percent": cpu_percent,
            "freq": psutil.cpu_freq()._asdict() if psutil.cpu_freq() else {"current": 0},
            "count_logical": psutil.cpu_count(),
            "count_physical": psutil.cpu_count(logical=False),
            "model": _get_cpu_model(),
        },
        "memory": {
            "total": mem.total,
            "used": mem.used,
            "available": mem.available,
            "percent": mem.percent,
            "cached": getattr(mem, "cached", 0),
        },
        "swap": {
            "total": swap.total,
            "used": swap.used,
            "percent": swap.percent,
        },
        "disk": {
            "total": disk.total,
            "used": disk.used,
            "free": disk.free,
            "percent": disk.percent,
            "mount": "/",
        },
        "uptime": uptime_str,
        "uptime_seconds": uptime_seconds,
        "load": [load1, load5, load15],
        "temps": temp_data,
        "cpu_temp": cpu_temp,
        "network": {
            "rx_rate": rx_rate,
            "tx_rate": tx_rate,
            "rx_total": net.bytes_recv,
            "tx_total": net.bytes_sent,
        },
        "hostname": hostname,
        "os": f"{os_name} {os_release}",
        "timestamp": datetime.now().isoformat(),
    }


def _get_cpu_model() -> str:
    """Read CPU model from /proc/cpuinfo."""
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if "model name" in line.lower():
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return "Unknown"


def get_neofetch() -> dict:
    """Collect neofetch-style system information."""
    info = {}
    hostname, os_name, os_release = _system_name()

    # User and hostname
    info["user"] = os.environ.get("USER", os.environ.get("LOGNAME", "user"))
    info["hostname"] = hostname

    # OS
    try:
        with open("/etc/os-release") as f:
            osr = {}
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    osr[k] = v.strip('"')
        info["os"] = osr.get("PRETTY_NAME", osr.get("NAME", "Linux"))
        info["os_id"] = osr.get("ID", "linux")
    except Exception:
        info["os"] = os_name
        info["os_id"] = os_name.lower()

    # Kernel
    info["kernel"] = os_release

    # Host / Device model
    try:
        info["host"] = Path("/sys/devices/virtual/dmi/id/product_name").read_text().strip()
        vendor = Path("/sys/devices/virtual/dmi/id/sys_vendor").read_text().strip()
        if vendor:
            info["host"] = f"{vendor} {info['host']}"
    except Exception:
        info["host"] = "Unknown"

    # Uptime
    uptime_s = time.time() - psutil.boot_time()
    days = int(uptime_s // 86400)
    hours = int((uptime_s % 86400) // 3600)
    minutes = int((uptime_s % 3600) // 60)
    if days > 0:
        info["uptime"] = f"{days}d {hours}h {minutes}m"
    else:
        info["uptime"] = f"{hours}h {minutes}m"

    # Shell
    info["shell"] = os.path.basename(os.environ.get("SHELL", "/bin/bash"))

    # Terminal
    info["terminal"] = os.environ.get("TERM", "unknown")

    # CPU
    info["cpu"] = _get_cpu_model()
    info["cpu_cores"] = psutil.cpu_count(logical=False)
    info["cpu_threads"] = psutil.cpu_count()

    # CPU freq
    freq = psutil.cpu_freq()
    info["cpu_freq"] = round(freq.current, 1) if freq else 0

    # GPU
    try:
        gpu_out = subprocess.check_output(
            ["lspci"], text=True, timeout=5
        )
        for line in gpu_out.splitlines():
            if "VGA" in line or "3D" in line or "Display" in line:
                info["gpu"] = line.split(":", 1)[1].strip()
                # Shorten common patterns
                info["gpu"] = re.sub(r"\[.*?\]\s*", "", info["gpu"])
                break
        else:
            info["gpu"] = "Unknown"
    except Exception:
        info["gpu"] = "Unknown"

    # Memory
    mem = psutil.virtual_memory()
    info["memory"] = f"{_format_bytes(mem.used)} / {_format_bytes(mem.total)} ({mem.percent}%)"

    # Disk (all mounted)
    disks = []
    for part in psutil.disk_partitions():
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disks.append({
                "mount": part.mountpoint,
                "total": _format_bytes(usage.total),
                "used": _format_bytes(usage.used),
                "percent": usage.percent,
            })
        except Exception:
            pass
    info["disks"] = disks

    # Network IPs
    ips = []
    for iface, addrs in psutil.net_if_addrs().items():
        for addr in addrs:
            if addr.family == 2:  # IPv4
                ip = addr.address
                if ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("172."):
                    ips.append({"iface": iface, "ip": ip})
    info["ips"] = ips

    # Packages
    try:
        result = subprocess.run(["dpkg", "-l"], capture_output=True, text=True, timeout=10)
        info["packages"] = sum(1 for l in result.stdout.splitlines() if l.startswith("ii"))
    except Exception:
        info["packages"] = 0

    # Resolution (try xrandr via display)
    info["resolution"] = "N/A"

    # Username@hostname (formatted string)
    info["user_host"] = f"{info['user']}@{info['hostname']}"

    return info


def get_docker() -> list:
    """Get Docker container status."""
    containers = []
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format",
             "{{.Names}}|{{.Status}}|{{.Ports}}|{{.Image}}|{{.State}}"],
            capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.strip().splitlines():
            if not line.strip():
                continue
            parts = line.split("|")
            if len(parts) >= 5:
                name, status, ports, image, state = parts[0], parts[1], parts[2], parts[3], parts[4]
                containers.append({
                    "name": name,
                    "status": status,
                    "ports": ports,
                    "image": image,
                    "state": state,
                    "running": state == "running",
                })
    except Exception:
        pass
    return containers


def get_connections() -> dict:
    """Check external connectivity."""
    connections = {}

    # Internet
    try:
        start = time.time()
        subprocess.run(
            ["curl", "-s", "--max-time", "3", "https://1.1.1.1", "-o", "/dev/null"],
            capture_output=True, timeout=5
        )
        latency = round((time.time() - start) * 1000)
        connections["internet"] = {"reachable": True, "latency_ms": latency, "endpoint": "https://1.1.1.1"}
    except Exception:
        connections["internet"] = {"reachable": False, "latency_ms": 0, "endpoint": "https://1.1.1.1"}

    # DNS
    try:
        start = time.time()
        subprocess.run(
            ["nslookup", "google.com"],
            capture_output=True, timeout=5
        )
        latency = round((time.time() - start) * 1000)
        connections["dns"] = {"reachable": True, "latency_ms": latency, "endpoint": "google.com"}
    except Exception:
        connections["dns"] = {"reachable": False, "latency_ms": 0, "endpoint": "google.com"}

    # Cloudflare Tunnel
    try:
        result = subprocess.run(["pgrep", "-f", "cloudflared"], capture_output=True, text=True, timeout=3)
        connections["cloudflare"] = {"running": bool(result.stdout.strip()), "process": "cloudflared"}
    except Exception:
        connections["cloudflare"] = {"running": False, "process": "cloudflared"}

    # Hermes Gateway (systemd check, no HTTP port)
    try:
        result = _run_systemctl_is_active(HERMES_GATEWAY_SERVICE, HERMES_GATEWAY_SCOPE)
        connections["hermes_gateway"] = {
            "reachable": result.stdout.strip() == "active",
            "scope": HERMES_GATEWAY_SCOPE,
            "service": HERMES_GATEWAY_SERVICE,
            "port": f"{HERMES_GATEWAY_SCOPE}-svc",
        }
    except Exception:
        connections["hermes_gateway"] = {
            "reachable": False,
            "scope": HERMES_GATEWAY_SCOPE,
            "service": HERMES_GATEWAY_SERVICE,
            "port": f"{HERMES_GATEWAY_SCOPE}-svc",
        }

    # Binance Bot
    bot_api_url = (CFG.get("integrations", {}).get("bot_api_url") or "").rstrip("/")
    if not bot_api_url:
        connections["binance_bot"] = {"enabled": False, "reachable": None, "url": ""}
    else:
        try:
            result = subprocess.run(
                ["curl", "-s", "--max-time", "2", bot_api_url + "/state"],
                capture_output=True, timeout=4
            )
            connections["binance_bot"] = {"enabled": True, "reachable": result.returncode == 0, "url": bot_api_url}
        except Exception:
            connections["binance_bot"] = {"enabled": True, "reachable": False, "url": bot_api_url}

    # Dashboard (self)
    connections["dashboard"] = {"reachable": True, "port": SERVER_PORT}

    return connections


def get_services() -> list[dict]:
    """Get status of services — driven entirely by config.yaml."""
    services = []
    cfg_services = CFG.get("services", [])
    if not cfg_services:
        # Fallback if config is empty
        cfg_services = [
            {"name": "hermes-gateway", "scope": "user", "display_name": "Hermes Gateway"},
            {"name": "server-dashboard", "scope": "system", "display_name": "Dashboard"},
        ]

    for svc in cfg_services:
        name = _service_key(svc["name"])
        svc_name = _normalize_service_name(svc["name"])
        scope = svc.get("scope", "system")
        display = svc.get("display_name", name)
        allow_restart = svc.get("allow_restart", False)
        try:
            if scope == "user":
                env = _user_env()
                result = subprocess.run(
                    ["systemctl", "--user", "show", svc_name, "--property=ActiveState,SubState,MainPID,PID,MemoryCurrent,CPUUsageNSec,ActiveEnterTimestamp"],
                    capture_output=True, text=True, timeout=5, env=env
                )
            else:
                result = subprocess.run(
                    ["systemctl", "show", svc_name, "--property=ActiveState,SubState,MainPID,PID,MemoryCurrent,CPUUsageNSec,ActiveEnterTimestamp"],
                    capture_output=True, text=True, timeout=5
                )
            props = {}
            for line in result.stdout.strip().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    props[k] = v

            active = props.get("ActiveState", "unknown") == "active"
            pid = props.get("MainPID") or props.get("PID", "")
            pid_val = pid if pid and pid != "0" else None
            mem_bytes = int(props.get("MemoryCurrent", "0") or "0") if active else 0

            # Get real CPU% via psutil if PID available
            cpu_pct = 0.0
            if pid_val and active:
                try:
                    proc = psutil.Process(int(pid_val))
                    cpu_pct = proc.cpu_percent(interval=None)
                except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
                    pass

            # Compute uptime from ActiveEnterTimestamp
            uptime_str = ""
            uptime_seconds = 0
            active_enter = props.get("ActiveEnterTimestamp", "")
            if active_enter and active:
                try:
                    # systemd timestamp format: "Sat 2026-05-09 21:35:16 -03"
                    # Need to normalize the timezone part
                    from datetime import datetime as _dt
                    ts_str = active_enter.strip()
                    # systemd often outputs TZ like "-03" instead of "-03:00"
                    # Fix: replace trailing " +/-HH" with proper offset
                    tz_match = re.search(r'\s+([+-])(\d{2})$', ts_str)
                    if tz_match:
                        ts_str = ts_str[:tz_match.start()] + ' ' + tz_match.group(1) + tz_match.group(2) + '00'
                    for fmt in (
                        "%a %Y-%m-%d %H:%M:%S %z",
                        "%a %Y-%m-%d %H:%M:%S",
                        "%Y-%m-%d %H:%M:%S %z",
                        "%Y-%m-%d %H:%M:%S",
                    ):
                        try:
                            enter_dt = _dt.strptime(ts_str, fmt)
                            uptime_seconds = time.time() - enter_dt.timestamp()
                            break
                        except ValueError:
                            continue
                    else:
                        # Last resort: strip TZ and parse
                        try:
                            clean = re.sub(r'\s+[+-]\d{2,4}$', '', ts_str)
                            enter_dt = _dt.strptime(clean.strip(), "%a %Y-%m-%d %H:%M:%S")
                            uptime_seconds = time.time() - enter_dt.timestamp()
                        except Exception:
                            pass

                    if uptime_seconds > 0:
                        uptime_str = _format_duration(uptime_seconds)
                except Exception:
                    pass

            services.append({
                "name": display,
                "service": name,
                "scope": scope,
                "active": active,
                "pid": pid_val,
                "memory": _format_bytes(mem_bytes) if mem_bytes > 0 else "—",
                "cpu": f"{cpu_pct:.1f}%" if cpu_pct > 0.1 else "0.0%",
                "allow_restart": allow_restart,
                "uptime": uptime_str,
                "uptime_seconds": round(uptime_seconds),
            })
        except Exception:
            services.append({
                "name": display,
                "service": name,
                "scope": scope,
                "active": False,
                "pid": None,
                "memory": "—",
                "cpu": "—",
                "allow_restart": allow_restart,
                "uptime": "",
                "uptime_seconds": 0,
            })

    return services


def _format_duration(seconds: float) -> str:
    """Format duration in seconds to human-readable string."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m"
    d, h = divmod(h, 24)
    return f"{d}d {h}h {m}m"


def get_hermes_status() -> dict:
    """Get Hermes agent status info."""
    status = {
        "gateway_running": False,
        "gateway_service": HERMES_GATEWAY_SERVICE,
        "gateway_scope": HERMES_GATEWAY_SCOPE,
        "model": "unknown",
        "platforms": {},
        "cron_jobs": [],
        "config": {},
    }

    # Check if gateway is running
    try:
        result = _run_systemctl_is_active(HERMES_GATEWAY_SERVICE, HERMES_GATEWAY_SCOPE)
        status["gateway_running"] = result.stdout.strip() == "active"
    except Exception:
        try:
            result = subprocess.run(
                ["pgrep", "-f", "hermes.*gateway"],
                capture_output=True, text=True, timeout=3
            )
            status["gateway_running"] = bool(result.stdout.strip())
        except Exception:
            pass

    # Read Hermes config
    config_path = HERMES_HOME / "config.yaml"
    if config_path.exists():
        try:
            import yaml
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            _model = cfg.get("model", "unknown")
            if isinstance(_model, dict):
                status["model"] = _model.get("default", "unknown")
                status["provider"] = _model.get("provider", "unknown")
            else:
                status["model"] = str(_model)
                status["provider"] = cfg.get("provider", "unknown")
            status["api_mode"] = cfg.get("api_mode", "chat_completions")
            _approvals = cfg.get("approvals", {})
            if isinstance(_approvals, dict):
                status["approvals"] = "on" if _approvals.get("mode") else "off"
            else:
                status["approvals"] = str(_approvals)
        except Exception:
            pass

    # Check platforms
    platforms = {}
    if status["gateway_running"]:
        platforms["telegram"] = "connected"

    # Check .env for platform indicators
    env_path = HERMES_HOME / ".env"
    if env_path.exists():
        try:
            env_content = env_path.read_text()
            if "TELEGRAM" in env_content:
                platforms.setdefault("telegram", "configured")
            if "DISCORD" in env_content:
                platforms.setdefault("discord", "configured")
            if "WHATSAPP" in env_content:
                platforms.setdefault("whatsapp", "disabled")
        except Exception:
            pass

    status["platforms"] = platforms

    # Read cron jobs
    cron_file = HERMES_HOME / "cron" / "jobs.json"
    if cron_file.exists():
        try:
            with open(cron_file) as f:
                data = json.load(f)
            for j in data.get("jobs", []):
                last_status = j.get("last_status", "")
                last_error = j.get("last_error", "")
                status["cron_jobs"].append({
                    "id": j.get("id", ""),
                    "name": j.get("name") or j.get("id", ""),
                    "schedule": j.get("schedule_display", j.get("schedule", {}).get("display", "?")),
                    "enabled": j.get("enabled", False),
                    "state": j.get("state", "unknown"),
                    "last_run": j.get("last_run_at", ""),
                    "next_run": j.get("next_run_at", ""),
                    "last_status": last_status,
                    "last_error": last_error if last_error and last_error != "none" else None,
                    "completed": j.get("repeat", {}).get("completed", 0),
                })
        except Exception:
            pass

    # Read active sessions
    status["active_sessions"] = []
    state_db = HERMES_HOME / "state.db"
    if state_db.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(str(state_db))
            rows = conn.execute("""
                SELECT id, title, started_at, ended_at, end_reason,
                       message_count, source, api_call_count
                FROM sessions
                ORDER BY COALESCE(ended_at, started_at) DESC
                LIMIT 15
            """).fetchall()
            for row in rows:
                sid, title, started, ended, reason, msgs, source, calls = row
                if source == "cron":
                    continue
                title_display = title or sid
                if len(title_display) > 60:
                    title_display = title_display[:57] + "..."
                status["active_sessions"].append({
                    "id": sid,
                    "title": title_display,
                    "messages": msgs or 0,
                    "started": _ts_to_iso(started) if started else None,
                    "ended": _ts_to_iso(ended) if ended else None,
                    "active": reason not in ("complete", "error", "cron_complete"),
                    "reason": reason,
                    "api_calls": calls or 0,
                })
            conn.close()
        except Exception:
            pass

    return status


def _ts_to_iso(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).isoformat()
    except Exception:
        return ""


def _format_bytes(n: int) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}P"


def _format_rate(bps: float) -> str:
    if bps < 1024:
        return f"{bps:.0f} B/s"
    elif bps < 1024 * 1024:
        return f"{bps/1024:.1f} KB/s"
    else:
        return f"{bps/(1024*1024):.1f} MB/s"


def _sparkline(data: list[float]) -> str:
    if not data:
        return ""
    bars = "▁▂▃▄▅▆▇█"
    min_v = min(data)
    max_v = max(data)
    rng = max_v - min_v if max_v != min_v else 1
    return "".join(
        bars[min(int((v - min_v) / rng * (len(bars) - 1)), len(bars) - 1)]
        for v in data
    )


def _user_env() -> dict:
    """Get env with user D-Bus context for systemctl --user."""
    env = os.environ.copy()
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return env
    uid = str(getuid())
    runtime_dir = f"/run/user/{uid}"
    env["XDG_RUNTIME_DIR"] = runtime_dir
    env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime_dir}/bus"
    return env


# ── API Routes ────────────────────────────────────────────────────────

@app.get(
    "/",
    tags=["System"],
    summary="Dashboard UI",
    description="Serves the single-page retro terminal dashboard HTML.",
    response_class=HTMLResponse,
    responses={200: {"description": "HTML dashboard page"}},
)
async def index():
    html_path = STATIC_DIR / "index.html"
    if html_path.exists():
        return FileResponse(html_path)
    return HTMLResponse("<h1>Dashboard not built yet</h1>")

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    ico_path = STATIC_DIR / "favicon.ico"
    if ico_path.exists():
        return FileResponse(ico_path, media_type="image/x-icon")
    return Response(status_code=404)


# Serve static assets (logo, images, etc.)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get(
    "/api/metrics",
    tags=["System"],
    summary="All system metrics",
    description=(
        "Returns a comprehensive snapshot of system health: CPU usage and frequency, "
        "memory/swap/disk utilization, temperatures, network I/O rates, uptime, load averages, "
        "sparkline histories, plus cached service/hermes/docker/connection data. "
        "Results are TTL-cached (3 s base, 10–30 s for sub-queries) to avoid overloading the host."
    ),
    responses={
        200: {
            "description": "Full system metrics snapshot",
            "content": {
                "application/json": {
                    "example": {
                        "cpu": {"percent": 12.5, "freq": {"current": 3200}, "count_logical": 8, "count_physical": 4, "model": "Intel Core i7-8550U"},
                        "memory": {"total": 16777216000, "used": 6442450000, "available": 10334766000, "percent": 38.4, "cached": 2100000000},
                        "swap": {"total": 4294967296, "used": 0, "percent": 0.0},
                        "disk": {"total": 500107862016, "used": 245366878208, "free": 254740983808, "percent": 49.1, "mount": "/"},
                        "uptime": "12d 5h 30m",
                        "uptime_seconds": 1056600,
                        "load": [0.75, 0.82, 0.91],
                        "cpu_temp": 52.0,
                        "network": {"rx_rate": 125000, "tx_rate": 42000, "rx_total": 10737418240, "tx_total": 5368709120},
                        "hostname": "your-hostname",
                        "os": "Linux 6.17.0-23-generic",
                        "timestamp": "2026-05-09T21:30:00.000000",
                        "sparklines": {"cpu": "▃▄▃▂▅▃▂▁▃▄", "temp": "▃▃▄▃▃▃▂▃▄▃", "net_rx": "▁▂▁▁▂▃▁▁▁▂", "net_tx": "▁▁▁▁▁▂▁▁▁▁"},
                    }
                }
            },
        }
    },
)
async def metrics():
    m = _cached("metrics_base", 3, get_system_metrics)
    return {
        **m,
        "sparklines": {
            "cpu": _sparkline(cpu_history),
            "temp": _sparkline(temp_history),
            "net_rx": _sparkline(net_rx_history),
            "net_tx": _sparkline(net_tx_history),
        },
        "services": _cached("services", 10, get_services),
        "hermes": _cached("hermes", 15, get_hermes_status),
        "neofetch": _cached("neofetch", 30, get_neofetch),
        "docker": _cached("docker", 10, get_docker),
        "connections": _cached("connections", 15, get_connections),
    }


KANBAN_DB = str(HERMES_HOME / "kanban.db")

@app.get(
    "/api/kanban",
    tags=["Hermes"],
    summary="Active kanban tasks",
    description=(
        "Queries the local Hermes kanban SQLite database for in-progress and triage tasks, "
        "plus status counts for all non-archived tasks. Used by the HERMES panel in the dashboard."
    ),
    responses={
        200: {
            "description": "Kanban task list and status counts",
            "content": {
                "application/json": {
                    "example": {
                        "tasks": [
                            {"id": "t_abc123", "title": "Polish API docs", "status": "in_progress", "priority": 2, "assignee": "default"}
                        ],
                        "counts": {"todo": 3, "in_progress": 1, "done": 42, "triage": 0},
                    }
                }
            },
        }
    },
)
async def kanban_tasks():
    """Return in-progress kanban tasks from the local DB."""
    try:
        conn = sqlite3.connect(KANBAN_DB)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        # In-progress + triage tasks
        c.execute("""
            SELECT id, title, status, priority, assignee, created_at, started_at,
                   workspace_kind, body, skills, current_step_key, workflow_template_id
            FROM tasks
            WHERE status IN ('in_progress', 'triage', 'running')
            ORDER BY
              CASE status WHEN 'in_progress' THEN 0 WHEN 'triage' THEN 1 END,
              priority DESC, created_at DESC
            LIMIT 15
        """)
        in_progress = [dict(r) for r in c.fetchall()]
        # Quick counts for all statuses
        c.execute("SELECT status, COUNT(*) as cnt FROM tasks WHERE status NOT IN ('archived') GROUP BY status")
        counts = {r['status']: r['cnt'] for r in c.fetchall()}
        conn.close()
        return {"tasks": in_progress, "counts": counts}
    except Exception as e:
        return {"tasks": [], "counts": {}, "error": str(e)}


@app.get(
    "/api/neofetch",
    tags=["System"],
    summary="System information",
    description="Returns ASCII-styled system info (OS, kernel, uptime, packages, shell, resolution, DE, WM, theme, icons, terminal, CPU, GPU, Memory). TTL-cached (30s).",
    responses={
        200: {
            "description": "Neofetch ASCII system information",
            "content": {
                "application/json": {
                    "example": {
                        "os": "Linux Mint 22 Wilma",
                        "kernel": "6.17.0-23-generic",
                        "uptime": "12 days, 5 hours, 30 minutes",
                        "packages": "3421 (apt)",
                        "shell": "zsh 5.9",
                        "resolution": "1920x1080",
                        "de": "Cinnamon 6.2",
                        "terminal": "kitty",
                        "cpu": "Intel Core i7-8550U (8) @ 4.00GHz",
                        "memory": "15724MiB"
                    }
                }
            },
        }
    },
)
async def neofetch():
    return _cached("neofetch", 30, get_neofetch)


@app.get(
    "/api/docker",
    tags=["System"],
    summary="Docker containers",
    description="Lists all Docker containers with status, image, ports, and uptime. Returns empty list if Docker is not installed or not running. TTL-cached (10s).",
    responses={
        200: {
            "description": "Docker container list",
            "content": {
                "application/json": {
                    "example": {
                        "containers": [
                            {
                                "id": "abc123def456",
                                "name": "nginx-proxy",
                                "status": "running",
                                "image": "nginx:latest",
                                "ports": "80:80, 443:443",
                                "uptime": "5d 3h"
                            }
                        ]
                    }
                }
            },
        }
    },
)
async def docker_info():
    return _cached("docker", 10, get_docker)


@app.get(
    "/api/connections",
    tags=["System"],
    summary="Network connectivity checks",
    description="Performs connectivity tests to external services (Google DNS, Cloudflare, configured URLs) and reports latency, status, and DNS resolution. TTL-cached (15s).",
    responses={
        200: {
            "description": "Connectivity test results",
            "content": {
                "application/json": {
                    "example": {
                        "google_dns": {"host": "8.8.8.8", "status": "ok", "latency_ms": 12.3},
                        "cloudflare": {"host": "1.1.1.1", "status": "ok", "latency_ms": 15.7},
                        "gateway": {"host": "192.168.1.1", "status": "ok", "latency_ms": 2.1},
                        "timestamp": "2026-05-09T21:30:00.000000"
                    }
                }
            },
        }
    },
)
async def connections():
    return _cached("connections", 15, get_connections)


@app.get(
    "/api/system",
    tags=["System"],
    summary="Base system metrics",
    description="Returns CPU, memory, swap, disk, temperatures, network, uptime, load, hostname, and OS info without cached service/hermes data. TTL-cached (30s).",
    responses={
        200: {
            "description": "System metrics snapshot",
            "content": {
                "application/json": {
                    "example": {
                        "cpu": {"percent": 12.5, "freq": {"current": 3200}, "count_logical": 8, "count_physical": 4, "model": "Intel Core i7-8550U"},
                        "memory": {"total": 16777216000, "used": 6442450000, "available": 10334766000, "percent": 38.4, "cached": 2100000000},
                        "swap": {"total": 4294967296, "used": 0, "percent": 0.0},
                        "disk": {"total": 500107862016, "used": 245366878208, "free": 254740983808, "percent": 49.1, "mount": "/"},
                        "uptime": "12d 5h 30m",
                        "uptime_seconds": 1056600,
                        "load": [0.75, 0.82, 0.91],
                        "cpu_temp": 52.0,
                        "network": {"rx_rate": 125000, "tx_rate": 42000, "rx_total": 10737418240, "tx_total": 5368709120},
                        "hostname": "your-hostname",
                        "os": "Linux 6.17.0-23-generic"
                    }
                }
            },
        }
    },
)
async def system_metrics():
    return _cached("system_metrics", 30, get_system_metrics)


@app.get(
    "/api/features",
    tags=["Config"],
    summary="Feature flags from config",
    description="Returns which features are enabled based on config.yaml. Frontend uses this to show/hide sections dynamically.",
)
async def get_features():
    integrations = CFG.get("integrations", {})
    services = [_service_key(s["name"]) for s in CFG.get("services", [])]
    return {
        "has_bot": bool(integrations.get("bot_api_url")),
        "has_bitsy": bool(integrations.get("bitsy_url")),
        "has_bot_service": "binance-bot" in services,
        "has_network_scanner": True,  # plugin-based, always available
        "services": services,
    }


@app.get(
    "/api/temps",
    tags=["System"],
    summary="Hardware temperatures",
    description="Returns CPU, GPU, and other hardware temperatures and fan speeds from lm-sensors. TTL-cached (5s).",
    responses={
        200: {
            "description": "Temperature readings",
            "content": {
                "application/json": {
                    "example": {
                        "temps": {
                            "Package id 0": 52.0,
                            "Core 0": 48.0,
                            "Core 1": 50.0
                        },
                        "fan": {
                            "fan1": 1200,
                            "cpu_fan": 1500
                        }
                    }
                }
            },
        }
    },
)
async def temps():
    return _cached("temps", 5, get_temperatures)


@app.get(
    "/api/services",
    tags=["Services"],
    summary="Monitored services status",
    description="Returns status (running, failed, inactive) and uptime for all services configured in config.yaml. TTL-cached (10s).",
    responses={
        200: {
            "description": "Service status list",
            "content": {
                "application/json": {
                    "example": {
                        "services": [
                            {"name": "hermes-gateway", "status": "running", "uptime": "12d 5h 30m", "enabled": True, "allow_restart": True},
                            {"name": "binance-bot", "status": "running", "uptime": "5d 2h 15m", "enabled": True, "allow_restart": True},
                            {"name": "watchdog", "status": "inactive", "enabled": False, "allow_restart": False}
                        ]
                    }
                }
            },
        }
    },
)
async def services():
    return _cached("services", 10, get_services)

# Build a lookup for service_key → (scope, service_name) for log fetching
def _build_service_log_lookup():
    """Build service key → (scope, full_service_name) map from config."""
    lookup = {}
    for svc in CFG.get("services", []):
        name = _service_key(svc["name"])
        scope = svc.get("scope", "system")
        svc_name = _normalize_service_name(svc["name"])
        # Allow lookup by short name (without .service)
        lookup[name] = (scope, svc_name)
        # Also allow with .service suffix
        if name != svc_name:
            lookup[svc_name] = (scope, svc_name)
    return lookup


@app.get(
    "/api/services/logs/{service_key}",
    tags=["Services"],
    summary="Service logs",
    description="Fetch tail logs from journalctl for a specific configured service. Parses log lines into structured format with timestamps, messages, and log levels (error/warn/info). Supports both user and system services.",
    responses={
        200: {
            "description": "Service log entries",
            "content": {
                "application/json": {
                    "example": {
                        "service": "hermes-gateway",
                        "lines": [
                            {"ts": "14:20:30", "msg": "Starting worker for task t_abc123", "level": "info"},
                            {"ts": "14:20:32", "msg": "Task completed successfully", "level": "info"},
                            {"ts": "14:20:35", "msg": "Connection timeout to API", "level": "error"}
                        ],
                        "count": 3
                    }
                }
            },
        },
        400: {"description": "Service not configured"},
        500: {"description": "Failed to fetch logs"},
    },
)
async def service_logs(service_key: str, lines: int = 20):
    """Fetch tail logs for a specific configured service."""
    lookup = _build_service_log_lookup()
    if service_key not in lookup:
        return JSONResponse({"error": f"Service '{service_key}' not configured"}, status_code=400)

    scope, svc_name = lookup[service_key]
    try:
        if scope == "user":
            env = _user_env()
            # Use JSON output to filter by unit
            result = subprocess.run(
                ["journalctl", "--user", "--no-pager", "-n", str(min(lines * 5, 500)), "--output=json"],
                capture_output=True, text=True, timeout=8, env=env
            )
            filtered = []
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    import json as _json
                    entry = _json.loads(line)
                    unit = entry.get("_SYSTEMD_USER_UNIT", "")
                    if unit == svc_name:
                        ts = entry.get("__REALTIME_TIMESTAMP", "")
                        msg = entry.get("MESSAGE", "")
                        priority = entry.get("PRIORITY", "6")
                        if ts:
                            try:
                                from datetime import datetime as _dt, timezone as _tz
                                ts_str = _dt.fromtimestamp(int(ts) / 1_000_000, tz=_tz.utc).strftime("%H:%M:%S")
                            except Exception:
                                ts_str = ""
                        else:
                            ts_str = ""
                        # Priority: 0=emerg, 1=alert, 2=crit, 3=err, 4=warn, 5=notice, 6=info, 7=debug
                        level = "error" if int(priority) <= 3 else "warn" if int(priority) == 4 else "info"
                        filtered.append({"ts": ts_str, "msg": msg[:200], "level": level})
                except Exception:
                    pass
            return {"service": service_key, "lines": filtered[-lines:], "count": len(filtered)}

        else:
            # System service
            cmd = ["journalctl", "-u", svc_name, "--no-pager", "-n", str(lines), "--output=short-iso"]
            if SUDO_MODE == "sudo_password" and SUDO_PASSWORD:
                result = subprocess.run(
                    ["sudo", "-S", *cmd],
                    input=SUDO_PASSWORD + "\n", capture_output=True, text=True, timeout=8
                )
            elif SUDO_MODE == "sudo_nopasswd":
                result = subprocess.run(
                    ["sudo", "-n", *cmd],
                    capture_output=True, text=True, timeout=8
                )
            else:
                result = subprocess.run(
                    cmd,
                    capture_output=True, text=True, timeout=8
                )
            parsed = []
            for line in result.stdout.splitlines():
                if not line.strip():
                    continue
                # Parse short-iso: "2026-05-09T14:20:30-03:00 HOSTNAME ..."
                parts = None
                m = re.match(r"^(\d{4}-\d{2}-\d{2}T(\d{2}:\d{2}:\d{2}))[^\s]*\s+\S+\s+(.*)", line)
                if m:
                    ts = m.group(2)
                    msg = m.group(3)[:200]
                else:
                    ts = ""
                    msg = line[:200]
                level = "error" if any(k in line.lower() for k in ["error", "critical", "fatal", "exception", "traceback"]) else \
                        "warn" if "warn" in line.lower() else "info"
                parsed.append({"ts": ts, "msg": msg, "level": level})
            return {"service": service_key, "lines": parsed[-lines:], "count": len(parsed)}

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get(
    "/api/hermes",
    tags=["Hermes"],
    summary="Hermes agent status",
    description="Returns Hermes Agent configuration, current model, provider, platform connections (Telegram, Discord), active sessions, recent runs, and cron job status. Reads from ~/.hermes/ config and kanban SQLite DB. TTL-cached (15s).",
    responses={
        200: {
            "description": "Hermes agent status",
            "content": {
                "application/json": {
                    "example": {
                        "model": "glm-5.1",
                        "provider": "zai",
                        "platforms": {
                            "telegram": {"connected": True, "chat_id": "6436594324"},
                            "discord": {"connected": False}
                        },
                        "sessions": {
                            "active": 2,
                            "recent": [{"id": "session_abc123", "model": "glm-5.1", "started_at": "2026-05-09T14:20:00"}]
                        },
                        "cron_jobs": {
                            "enabled": 3,
                            "last_run": "2026-05-09T14:15:00"
                        },
                        "config_path": "/home/pantojinho/.hermes/config.yaml"
                    }
                }
            },
        }
    },
)
async def hermes_status():
    return _cached("hermes", 15, get_hermes_status)


@app.post(
    "/api/service/{name}/restart",
    tags=["Services"],
    summary="Restart a service",
    description="Restart a configured systemd service. The service must be in config.yaml with allow_restart: True. Supports user services (systemctl --user) and system services (requires sudo_mode configuration).",
    responses={
        200: {
            "description": "Service restarted successfully",
            "content": {
                "application/json": {
                    "example": {"success": True, "message": "hermes-gateway restarted"}
                }
            },
        },
        400: {
            "description": "Service not allowed or sudo not configured",
            "content": {
                "application/json": {
                    "example": {"success": False, "message": "Service 'nginx' not allowed"}
                }
            },
        },
        500: {
            "description": "Restart failed",
            "content": {
                "application/json": {
                    "example": {"success": False, "message": "Job failed. See systemctl status for details."}
                }
            },
        },
    },
)
async def restart_service(name: str):
    if name not in ALLOWED_RESTART_SERVICES:
        return JSONResponse({"success": False, "message": f"Service '{name}' not allowed"}, status_code=400)

    scope, service_name = ALLOWED_RESTART_SERVICES[name]
    try:
        if scope == "user":
            env = _user_env()
            result = subprocess.run(
                ["systemctl", "--user", "restart", service_name],
                capture_output=True, text=True, timeout=15, env=env
            )
        elif SUDO_MODE == "sudo_password" and SUDO_PASSWORD:
            result = subprocess.run(
                ["sudo", "-S", "systemctl", "restart", service_name],
                input=SUDO_PASSWORD + "\n", capture_output=True, text=True, timeout=15
            )
        elif SUDO_MODE == "sudo_nopasswd":
            result = subprocess.run(
                ["sudo", "-n", "systemctl", "restart", service_name],
                capture_output=True, text=True, timeout=15
            )
        else:
            return JSONResponse(
                {"success": False, "message": f"Restart not configured for system services. Set sudo_mode in config.yaml."},
                status_code=400
            )

        if result.returncode == 0:
            return {"success": True, "message": f"{name} restarted"}
        else:
            return JSONResponse(
                {"success": False, "message": result.stderr.strip() or f"Failed to restart {name}"},
                status_code=500
            )
    except Exception as e:
        return JSONResponse(
            {"success": False, "message": str(e)},
            status_code=500
        )


@app.get(
    "/bot-api/{path:path}",
    tags=["Hermes"],
    summary="Proxy to trading bot API",
    description="Reverse proxy to the trading bot API configured in config.yaml (bot_api_url). Forwards all GET requests and returns JSON or text responses. Used by dashboard to fetch bot metrics, trades, and P&L without CORS issues. TTL-cached (4s).",
    responses={
        200: {
            "description": "Bot API response (JSON or text)",
            "content": {
                "application/json": {
                    "example": {
                        "status": "trading",
                        "regime": "ALTA",
                        "daily_pnl": 0.0234,
                        "equity": 1.0234,
                        "trades_today": 5,
                        "last_trade": {"symbol": "BTCUSDT", "side": "BUY", "price": 67500.50}
                    }
                }
            },
        },
        503: {"description": "Bot API unavailable"},
    },
)
async def proxy_bot(path: str, request: Request):
    """Proxy to Binance bot API."""
    import requests as req_lib
    bot_url = CFG.get("integrations", {}).get("bot_api_url", "http://localhost:18790/api")
    url = f"{bot_url}/{path}"
    try:
        resp = _cached("bot_" + path, 4, lambda u=url: req_lib.get(u, timeout=5))
        try:
            return JSONResponse(resp.json(), status_code=resp.status_code)
        except Exception:
            return resp.text
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=503)


# ── Hermes Model & Ops Endpoints ─────────────────────────────────────

@app.get(
    "/api/hermes/models",
    tags=["Hermes"],
    summary="Available AI models",
    description="Lists available AI models from Hermes config.yaml. Returns current model, provider, and a list of available models (custom + common suggestions). Used by dashboard model switcher.",
    responses={
        200: {
            "description": "Available models",
            "content": {
                "application/json": {
                    "example": {
                        "current": "glm-5.1",
                        "provider": "zai",
                        "models": [
                            "glm-5.1",
                            "glm-5",
                            "gemma4-e4b-oblit",
                            "gemma4-nothink",
                            "kimi-k2-2.6",
                            "gemini-2.5-pro",
                            "gemini-2.5-flash",
                            "deepseek-r1",
                            "claude-sonnet-4-20250514"
                        ]
                    }
                }
            },
        }
    },
)
async def hermes_models():
    """Lista modelos disponíveis lendo do config.yaml."""
    import yaml
    config_path = HERMES_HOME / "config.yaml"
    models = []
    try:
        with open(config_path) as f:
            c = yaml.safe_load(f) or {}
        current = ""
        if isinstance(c.get('model'), dict):
            current = c['model'].get('default', '')
        elif c.get('model'):
            current = str(c['model'])
        provider = ""
        if isinstance(c.get('model'), dict):
            provider = c['model'].get('provider', 'zai')
        else:
            provider = c.get('provider', 'zai')
        # Custom models from config
        if 'models' in c and isinstance(c['models'], dict):
            for name in c['models']:
                models.append(name)
        elif 'models' in c and isinstance(c['models'], list):
            models.extend(c['models'])
        # Common models as suggestions
        suggested = ['glm-5.1', 'glm-5', 'gemma4-e4b-oblit', 'gemma4-nothink', 'kimi-k2-2.6',
                     'gemini-2.5-pro', 'gemini-2.5-flash', 'deepseek-r1', 'claude-sonnet-4-20250514']
        for m in suggested:
            if m not in models:
                models.append(m)
        return {"current": current, "provider": provider, "models": models}
    except Exception as e:
        return {"current": "unknown", "provider": "zai", "models": ["glm-5.1"], "error": str(e)}


@app.post(
    "/api/hermes/model",
    tags=["Hermes"],
    summary="Switch AI model",
    description="Updates the default AI model in ~/.hermes/config.yaml and restarts the Hermes gateway service to apply changes. Accepts JSON body with 'model' and optional 'provider' fields.",
    responses={
        200: {
            "description": "Model switched successfully",
            "content": {
                "application/json": {
                    "example": {"success": True, "model": "glm-5.1", "provider": "zai"}
                }
            },
        },
        500: {"description": "Failed to update config or restart gateway"},
    },
)
async def set_hermes_model(request: Request):
    """Troca modelo no config.yaml e reinicia o gateway."""
    import yaml
    body = await request.json()
    model = body.get("model")
    provider = body.get("provider")
    config_path = HERMES_HOME / "config.yaml"
    try:
        with open(config_path) as f:
            c = yaml.safe_load(f) or {}
        if 'model' not in c or not isinstance(c['model'], dict):
            c['model'] = {}
        if model:
            c['model']['default'] = model
        if provider:
            c['model']['provider'] = provider
        with open(config_path, 'w') as f:
            yaml.dump(c, f, default_flow_style=False)
        # Restart gateway to apply
        if HERMES_GATEWAY_SCOPE == "user":
            result = subprocess.run(
                ["systemctl", "--user", "restart", HERMES_GATEWAY_SERVICE],
                capture_output=True, text=True, timeout=30, env=_user_env()
            )
        elif SUDO_MODE == "sudo_password" and SUDO_PASSWORD:
            result = subprocess.run(
                ["sudo", "-S", "systemctl", "restart", HERMES_GATEWAY_SERVICE],
                input=SUDO_PASSWORD + "\n", capture_output=True, text=True, timeout=30
            )
        elif SUDO_MODE == "sudo_nopasswd":
            result = subprocess.run(
                ["sudo", "-n", "systemctl", "restart", HERMES_GATEWAY_SERVICE],
                capture_output=True, text=True, timeout=30
            )
        else:
            result = subprocess.run(
                ["systemctl", "restart", HERMES_GATEWAY_SERVICE],
                capture_output=True, text=True, timeout=30
            )
        if result.returncode != 0:
            return JSONResponse({
                "success": False,
                "error": result.stderr.strip() or "Model saved, but gateway restart failed",
            }, status_code=500)
        # Invalidate hermes cache
        if "hermes" in _api_cache:
            del _api_cache["hermes"]
        return {"success": True, "model": model or c['model'].get('default'),
                "provider": provider or c['model'].get('provider')}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.post(
    "/api/backup",
    tags=["Hermes"],
    summary="Create config backup",
    description="Creates a timestamped backup of essential Hermes config files (config.yaml, MEMORY.md, USER.md, PERSONA.md, skills/) and Binance bot config files. Automatically cleans up old backups (max_copies setting, default 10).",
    responses={
        200: {
            "description": "Backup created successfully",
            "content": {
                "application/json": {
                    "example": {"success": True, "path": "/home/pantojinho/backups/hermes_20260509_143025", "timestamp": "20260509_143025"}
                }
            },
        },
        500: {"description": "Failed to create backup"},
    },
)
async def backup_config():
    """Faz backup das configs essenciais do Hermes e bot."""
    import shutil
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = Path(os.path.expanduser(CFG.get("backup", {}).get("directory", "~/backups")))
    backup_dir = backup_root / f"hermes_{ts}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    # Backup only essential hermes files (not cache/sessions/audio)
    hermes_dir = backup_dir / "hermes"
    hermes_dir.mkdir(exist_ok=True)
    essential_files = ["config.yaml", "MEMORY.md", "USER.md", "PERSONA.md"]
    for fname in essential_files:
        src = HERMES_HOME / fname
        if src.exists():
            shutil.copy2(src, hermes_dir / fname)
    # Backup skills dir (just SKILL.md files)
    skills_src = HERMES_HOME / "skills"
    if skills_src.exists():
        skills_dst = hermes_dir / "skills"
        shutil.copytree(skills_src, skills_dst, dirs_exist_ok=True)
    # Backup binance-bot configs
    bot_dir = Path.home() / "binance-bot"
    if bot_dir.exists():
        for f in bot_dir.glob("*.yaml"):
            shutil.copy2(f, backup_dir / f.name)
        for f in bot_dir.glob("*.yml"):
            shutil.copy2(f, backup_dir / f.name)
    # Keep only last N backups
    max_backups = CFG.get("backup", {}).get("max_copies", 10)
    if backup_root.exists():
        all_backups = sorted(backup_root.iterdir())
        if len(all_backups) > max_backups:
            for old in all_backups[:-max_backups]:
                shutil.rmtree(old, ignore_errors=True)
    return {"success": True, "path": str(backup_dir), "timestamp": ts}


# ── Metrics / Graficos API ────────────────────────────────────────

@app.get(
    "/api/logs/{service}",
    tags=["Services"],
    summary="Service logs (fast reader)",
    description="Fast journalctl log reader for dashboard tabs. Supports predefined services: hermes (gateway), bot (binance-bot), dashboard (server-dashboard), watchdog. Returns structured log lines with timestamps and messages.",
    responses={
        200: {
            "description": "Log entries",
            "content": {
                "application/json": {
                    "example": {
                        "service": "hermes",
                        "lines": [
                            {"ts": "14:20:30", "msg": "Starting worker for task t_abc123", "level": "info"},
                            {"ts": "14:20:32", "msg": "Task completed successfully", "level": "info"}
                        ],
                        "count": 2
                    }
                }
            },
        },
        400: {"description": "Invalid service name"},
        500: {"description": "Failed to fetch logs"},
    },
)
async def get_logs(service: str, lines: int = 80):
    """Fast journalctl log reader for the dashboard."""
    allowed = {
        "hermes": (HERMES_GATEWAY_SCOPE, HERMES_GATEWAY_SERVICE),
        "bot": (_configured_service_scope("binance-bot", "system"), "binance-bot.service"),
        "dashboard": (_configured_service_scope("server-dashboard", "system"), "server-dashboard.service"),
        "watchdog": (None, None),
    }
    if service not in allowed:
        return JSONResponse({"error": f"Unknown service: {service}"}, status_code=400)

    try:
        scope, svc_name = allowed[service]

        if scope == "user":
            env = _user_env()
            # Use JSON output + filter by _SYSTEMD_USER_UNIT for user services
            # Filter lines that belong to this service
            # Use JSON to get _SYSTEMD_USER_UNIT, then match
            result_json = subprocess.run(
                ["journalctl", "--user", "--no-pager", "-n", str(min(lines * 5, 1000)), "--output=json"],
                capture_output=True, text=True, timeout=8, env=env
            )
            import json as _json
            filtered = []
            for line in result_json.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = _json.loads(line)
                    unit = entry.get("_SYSTEMD_USER_UNIT", "")
                    if unit == svc_name:
                        # Reconstruct short-iso format
                        ts = entry.get("__REALTIME_TIMESTAMP", "")
                        host = entry.get("_HOSTNAME", "")
                        msg = entry.get("MESSAGE", "")
                        if ts:
                            try:
                                from datetime import datetime as _dt, timezone as _tz
                                ts_str = _dt.fromtimestamp(int(ts)/1_000_000, tz=_tz.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
                            except Exception:
                                ts_str = ts[:19]
                        else:
                            ts_str = ""
                        filtered.append(f"{ts_str} {host} {msg}")
                except:
                    pass
            return {"service": service, "lines": filtered[-lines:], "count": len(filtered)}

        elif scope == "system":
            cmd = ["journalctl", "-u", svc_name, "--no-pager", "-n", str(lines), "--output=short-iso"]
            if SUDO_MODE == "sudo_password" and SUDO_PASSWORD:
                result = subprocess.run(
                    ["sudo", "-S", *cmd],
                    input=SUDO_PASSWORD + "\n", capture_output=True, text=True, timeout=8
                )
            elif SUDO_MODE == "sudo_nopasswd":
                result = subprocess.run(
                    ["sudo", "-n", *cmd],
                    capture_output=True, text=True, timeout=8
                )
            else:
                # Try without sudo (user may have access)
                result = subprocess.run(
                    cmd,
                    capture_output=True, text=True, timeout=8
                )
            lines_list = [l for l in result.stdout.splitlines() if l.strip()]
            return {"service": service, "lines": lines_list[-lines:], "count": len(lines_list)}

        else:  # watchdog
            cmd = ["journalctl", "--no-pager", "-n", str(lines * 5), "--output=short-iso"]
            if SUDO_MODE == "sudo_password" and SUDO_PASSWORD:
                result = subprocess.run(
                    ["sudo", "-S", *cmd],
                    input=SUDO_PASSWORD + "\n", capture_output=True, text=True, timeout=8
                )
            elif SUDO_MODE == "sudo_nopasswd":
                result = subprocess.run(
                    ["sudo", "-n", *cmd],
                    capture_output=True, text=True, timeout=8
                )
            else:
                result = subprocess.run(
                    cmd,
                    capture_output=True, text=True, timeout=8
                )
            lines_list = [l for l in result.stdout.splitlines() if l.strip() and "watchdog" in l.lower()]
            return {"service": service, "lines": lines_list[-lines:], "count": len(lines_list)}

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get(
    "/api/graficos/series",
    tags=["Metrics"],
    summary="Time-series data for a metric",
    description="Query time-series data from SQLite TSDB for a specific metric over a duration. Returns [timestamp_ms, value] pairs suitable for Chart.js. Supports optional tag filtering.",
    responses={
        200: {
            "description": "Time-series data points",
            "content": {
                "application/json": {
                    "example": {
                        "metric": "system_cpu_percent",
                        "duration": 3600,
                        "data": [[1715289000000, 12.5], [1715289060000, 13.2], [1715289120000, 11.8]],
                        "points": 3
                    }
                }
            },
        },
        500: {"description": "Failed to query TSDB"},
    },
)
async def graficos_series(
    metric: str = "system_cpu_percent",
    duration: int = 3600,
    tags: str = None,
):
    """
    Return time-series data for a given metric.
    
    Args:
        metric: metric name (e.g. 'system_cpu_percent', 'bitsy_hashrate_khs')
        duration: lookback in seconds (3600=1h, 21600=6h, 86400=24h, 604800=7d, 2592000=30d)
        tags: optional JSON filter for tags
    """
    try:
        conn = metrics_tsdb.get_db()
        tag_filter = json.loads(tags) if tags else None
        rows = metrics_tsdb.query_range(conn, metric, duration, tag_filter)
        conn.close()
        # Convert to [timestamp_ms, value] pairs for Chart.js
        data = [[int(ts * 1000), round(val, 3)] for ts, val in rows]
        return {"metric": metric, "duration": duration, "data": data, "points": len(data)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get(
    "/api/graficos/series_tagged",
    tags=["Metrics"],
    summary="Time-series data grouped by tag",
    description="Query time-series data grouped by a tag value (e.g., CPU cores, GPU devices). Returns multiple series where keys are tag values and values are [timestamp_ms, value] pairs.",
    responses={
        200: {
            "description": "Grouped time-series data",
            "content": {
                "application/json": {
                    "example": {
                        "metric": "system_cpu_core_percent",
                        "duration": 3600,
                        "series": {
                            "0": [[1715289000000, 12.5], [1715289060000, 13.2]],
                            "1": [[1715289000000, 11.0], [1715289060000, 12.1]],
                            "2": [[1715289000000, 10.5], [1715289060000, 11.8]]
                        }
                    }
                }
            },
        },
        500: {"description": "Failed to query TSDB"},
    },
)
async def graficos_series_tagged(
    metric: str = "system_cpu_core_percent",
    duration: int = 3600,
    tag_key: str = "core",
):
    """Return time-series data grouped by a tag value."""
    try:
        conn = metrics_tsdb.get_db()
        grouped = metrics_tsdb.query_tags_range(conn, metric, duration, tag_key)
        conn.close()
        result = {}
        for tag_val, rows in grouped.items():
            result[tag_val] = [[int(ts * 1000), round(val, 3)] for ts, val in rows]
        return {"metric": metric, "duration": duration, "series": result}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get(
    "/api/graficos/dashboard",
    tags=["Metrics"],
    summary="All chart data for dashboard",
    description="Returns all time-series data needed for the Graficos tab in a single request. Includes system metrics (CPU, memory, disk, etc.), BitsyMiner metrics, bot metrics, and TSDB status. Optimized for one-call loading of all charts.",
    responses={
        200: {
            "description": "Complete dashboard chart data",
            "content": {
                "application/json": {
                    "example": {
                        "duration": 3600,
                        "generated_at": "2026-05-09T14:30:00.000000",
                        "system": {
                            "cpu": [[1715289000000, 12.5], [1715289060000, 13.2]],
                            "memory": [[1715289000000, 38.4], [1715289060000, 39.1]],
                            "disk": [[1715289000000, 49.1], [1715289060000, 49.2]]
                        },
                        "bitsy": {
                            "hashrate": [[1715289000000, 777], [1715289060000, 780]],
                            "shares": [[1715289000000, 123], [1715289060000, 124]]
                        },
                        "bot": {
                            "pnl": [[1715289000000, 0.0234], [1715289060000, 0.0251]],
                            "equity": [[1715289000000, 1.0234], [1715289060000, 1.0251]]
                        },
                        "status": {
                            "db_size_mb": 12.5,
                            "collector_running": True,
                            "points_total": 125000
                        }
                    }
                }
            },
        },
        500: {"description": "Failed to query TSDB"},
    },
)
async def graficos_dashboard(duration: int = 3600):
    """
    Return all metrics needed for Graficos tab in one request.
    """
    try:
        conn = metrics_tsdb.get_db()
        now = time.time()

        # Helper
        def q(metric_name):
            rows = metrics_tsdb.query_range(conn, metric_name, duration)
            return [[int(ts * 1000), round(val, 3)] for ts, val in rows]

        # System metrics
        result = {
            "duration": duration,
            "generated_at": datetime.now().isoformat(),
            "system": {
                "cpu": q("system_cpu_percent"),
                "memory": q("system_memory_percent"),
                "disk": q("system_disk_percent"),
                "swap": q("system_swap_percent"),
                "cpu_temp": q("system_cpu_temp_celsius"),
                "net_rx": q("system_net_bytes_recv"),
                "net_tx": q("system_net_bytes_sent"),
                "load_1m": q("system_load_1m"),
            },
            "bitsy": {
                "hashrate": q("bitsy_hashrate_khs"),
                "shares": q("bitsy_shares"),
                "best_difficulty": q("bitsy_best_difficulty"),
                "block_height": q("bitsy_block_height"),
                "temp": q("bitsy_temp_celsius"),
            },
            "bot": {
                "pnl": q("bot_daily_pnl"),
                "equity": q("bot_equity"),
                "btc_price": q("bot_btc_price"),
                "rsi": q("bot_rsi"),
                "trades": q("bot_trades_count"),
            },
            "status": {
                "db_size_mb": round(metrics_tsdb.DB_PATH.stat().st_size / 1024 / 1024, 2) if metrics_tsdb.DB_PATH.exists() else 0,
                "collector_running": metrics_tsdb._running,
                "points_total": conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0],
            }
        }

        # Compute net rates if we have net data
        net_rx = result["system"]["net_rx"]
        net_tx = result["system"]["net_tx"]
        if len(net_rx) >= 2:
            # Convert cumulative bytes to rate in KB/s
            rx_rate = []
            tx_rate = []
            for i in range(1, len(net_rx)):
                dt = (net_rx[i][0] - net_rx[i-1][0]) / 1000  # ms to seconds
                if dt > 0:
                    rx_rate.append([net_rx[i][0], round((net_rx[i][1] - net_rx[i-1][1]) / 1024 / dt, 2)])
                    tx_rate.append([net_tx[i][0], round((net_tx[i][1] - net_tx[i-1][1]) / 1024 / dt, 2)])
            result["system"]["net_rx_rate"] = rx_rate
            result["system"]["net_tx_rate"] = tx_rate

        conn.close()
        return result
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get(
    "/api/graficos/status",
    tags=["Metrics"],
    summary="TSDB collector status",
    description="Returns the status of the time-series database collector: running state, database path and size, total data points, oldest/newest timestamps, list of collected metrics, and scrape interval.",
    responses={
        200: {
            "description": "TSDB status",
            "content": {
                "application/json": {
                    "example": {
                        "collector_running": True,
                        "db_path": "/home/pantojinho/server-dashboard/metrics.db",
                        "db_size_mb": 12.5,
                        "total_points": 125000,
                        "oldest": "2026-05-08T14:30:00",
                        "newest": "2026-05-09T14:30:00",
                        "metrics": [
                            "system_cpu_percent",
                            "system_memory_percent",
                            "system_disk_percent",
                            "bitsy_hashrate_khs",
                            "bot_daily_pnl"
                        ],
                        "scrape_interval": 30
                    }
                }
            },
        },
        500: {"description": "Failed to query TSDB status"},
    },
)
async def graficos_status():
    """Return TSDB collector status."""
    try:
        conn = metrics_tsdb.get_db()
        total = conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]
        oldest = conn.execute("SELECT MIN(ts) FROM metrics").fetchone()[0]
        newest = conn.execute("SELECT MAX(ts) FROM metrics").fetchone()[0]
        metrics_list = conn.execute("SELECT DISTINCT metric FROM metrics ORDER BY metric").fetchall()
        conn.close()

        return {
            "collector_running": metrics_tsdb._running,
            "db_path": str(metrics_tsdb.DB_PATH),
            "db_size_mb": round(metrics_tsdb.DB_PATH.stat().st_size / 1024 / 1024, 2) if metrics_tsdb.DB_PATH.exists() else 0,
            "total_points": total,
            "oldest": datetime.fromtimestamp(oldest).isoformat() if oldest else None,
            "newest": datetime.fromtimestamp(newest).isoformat() if newest else None,
            "metrics": [m[0] for m in metrics_list],
            "scrape_interval": metrics_tsdb.SCRAPE_INTERVAL,
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Plugin API ────────────────────────────────────────────────────────

@app.get(
    "/api/plugins",
    tags=["Plugins"],
    summary="List loaded plugins",
    description="Returns metadata for all loaded dashboard plugins: name, display name, tab ID, whether they have a tab UI, routes, and metric collector.",
    responses={
        200: {
            "description": "Plugin list",
            "content": {
                "application/json": {
                    "example": {
                        "plugins": [
                            {
                                "name": "my-plugin",
                                "display_name": "MY PLUGIN",
                                "tab_id": "my-plugin-tab",
                                "has_tab": True,
                                "has_routes": True,
                                "has_collector": False
                            }
                        ]
                    }
                }
            },
        }
    },
)
async def list_plugins():
    """List all loaded plugins with their metadata."""
    return {
        "plugins": [
            {
                "name": p.name,
                "display_name": p.display_name,
                "tab_id": p.tab_id,
                "has_tab": bool(p.tab_html()),
                "has_routes": True,
                "has_collector": bool(p.collect_metrics()),
            }
            for p in _loaded_plugins
        ],
        "count": len(_loaded_plugins),
    }


@app.get(
    "/api/plugins/{name}/tab",
    tags=["Plugins"],
    summary="Plugin tab HTML",
    description="Returns the HTML content for a specific plugin's tab, along with any custom JavaScript and CSS styles. Used by dashboard to load plugin UI dynamically.",
    responses={
        200: {
            "description": "Plugin tab HTML",
            "content": {
                "application/json": {
                    "example": {
                        "name": "my-plugin",
                        "html": "<div class='panel'>Hello from plugin!</div>",
                        "scripts": "<script>console.log('Plugin loaded');</script>",
                        "styles": "<style>.plugin-panel { background: red; }</style>"
                    }
                }
            },
        },
        404: {"description": "Plugin not found or has no tab"},
    },
)
async def plugin_tab(name: str):
    """Return a specific plugin's tab HTML content."""
    for p in _loaded_plugins:
        if p.name == name:
            html = p.tab_html()
            if html:
                return {"name": name, "html": html, "scripts": p.scripts_html(), "styles": p.styles_html()}
            return JSONResponse({"error": f"Plugin '{name}' has no tab"}, status_code=404)
    return JSONResponse({"error": f"Plugin '{name}' not found"}, status_code=404)



# ── Settings API (in-browser config editor) ────────────────────────

SETTINGS_BACKUP_DIR = DASHBOARD_DIR / "backups"


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge override into base recursively. Returns new dict."""
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


# Schema definition for the settings form — defines which config fields are editable
# and their types, labels, groups, validation rules.
SETTINGS_SCHEMA = [
    {
        "group": "Server",
        "icon": "🖥️",
        "fields": [
            {"key": "server.host", "label": "Host", "type": "text", "placeholder": "0.0.0.0"},
            {"key": "server.port", "label": "Port", "type": "number", "min": 1, "max": 65535, "placeholder": "18791"},
            {"key": "server.log_level", "label": "Log Level", "type": "select", "options": ["debug", "info", "warning", "error", "critical"]},
        ],
    },
    {
        "group": "Metrics",
        "icon": "📊",
        "fields": [
            {"key": "metrics.scrape_interval", "label": "Scrape Interval (s)", "type": "number", "min": 5, "max": 600},
            {"key": "metrics.retention_days", "label": "Retention Days", "type": "number", "min": 1, "max": 365},
            {"key": "metrics.history_length", "label": "History Length", "type": "number", "min": 10, "max": 500},
        ],
    },
    {
        "group": "Alerts",
        "icon": "🚨",
        "fields": [
            {"key": "alerts.cpu_temp_celsius", "label": "CPU Temp Alert (°C)", "type": "number", "min": 40, "max": 110},
            {"key": "alerts.cpu_percent", "label": "CPU Usage Alert (%)", "type": "number", "min": 50, "max": 100},
            {"key": "alerts.disk_percent", "label": "Disk Usage Alert (%)", "type": "number", "min": 50, "max": 100},
        ],
    },
    {
        "group": "Integrations",
        "icon": "🔗",
        "fields": [
            {"key": "integrations.bot_api_url", "label": "Bot API URL", "type": "text", "placeholder": "http://localhost:18790/api"},
            {"key": "integrations.bitsy_url", "label": "BitsyMiner URL", "type": "text", "placeholder": "http://192.168.1.132/statusJson"},
        ],
    },
    {
        "group": "Backup",
        "icon": "💾",
        "fields": [
            {"key": "backup.directory", "label": "Backup Directory", "type": "text", "placeholder": "~/backups"},
            {"key": "backup.max_copies", "label": "Max Backup Copies", "type": "number", "min": 1, "max": 100},
        ],
    },
    {
        "group": "Sudo",
        "icon": "🔑",
        "fields": [
            {"key": "sudo_mode", "label": "Sudo Mode", "type": "select", "options": ["none", "sudo_password", "sudo_nopasswd"]},
        ],
    },
    {
        "group": "Hermes",
        "icon": "🤖",
        "fields": [
            {"key": "hermes.home", "label": "Hermes Home", "type": "text", "placeholder": "~/.hermes"},
            {"key": "hermes.gateway_service", "label": "Gateway Service", "type": "text", "placeholder": "hermes-gateway.service"},
            {"key": "hermes.gateway_scope", "label": "Gateway Scope", "type": "select", "options": ["user", "system"]},
            {"key": "integrations.telegram.enabled", "label": "Telegram Alerts", "type": "select", "options": ["true", "false"]},
        ],
    },
]


def _get_nested(d: dict, key_path: str, default=None):
    """Get a nested dict value from dot-separated key path."""
    keys = key_path.split(".")
    val = d
    for k in keys:
        if isinstance(val, dict):
            val = val.get(k, default)
        else:
            return default
    return val


def _set_nested(d: dict, key_path: str, value):
    """Set a nested dict value using dot-separated key path."""
    keys = key_path.split(".")
    current = d
    for k in keys[:-1]:
        if k not in current or not isinstance(current[k], dict):
            current[k] = {}
        current = current[k]
    current[keys[-1]] = value


@app.get(
    "/api/settings",
    tags=["System"],
    summary="Get dashboard settings",
    description="Returns current config.yaml values and the editable schema for the in-browser settings form. Values are extracted from the config and matched to schema fields.",
    responses={
        200: {
            "description": "Settings values and schema",
            "content": {
                "application/json": {
                    "example": {
                        "values": {
                            "server.port": 18791,
                            "server.host": "0.0.0.0",
                            "sudo_mode": "sudo_nopasswd"
                        },
                        "schema": [
                            {
                                "title": "Server",
                                "fields": [
                                    {"key": "server.port", "type": "number", "label": "Port", "default": 18791},
                                    {"key": "server.host", "type": "text", "label": "Host", "default": "0.0.0.0"}
                                ]
                            }
                        ]
                    }
                }
            },
        },
        500: {"description": "Failed to read config"},
    },
)
async def get_settings():
    """Return current config values and the editable schema."""
    cfg_path = DASHBOARD_DIR / "config.yaml"
    try:
        if cfg_path.exists():
            with open(cfg_path) as f:
                current = yaml.safe_load(f) or {}
        else:
            current = {}
        # Build values from schema
        values = {}
        for group in SETTINGS_SCHEMA:
            for field in group["fields"]:
                key = field["key"]
                val = _get_nested(current, key, "")
                # Convert booleans to strings for select fields
                if field.get("type") == "select" and isinstance(val, bool):
                    val = "true" if val else "false"
                values[key] = val
        return {"values": values, "schema": SETTINGS_SCHEMA}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post(
    "/api/settings",
    tags=["System"],
    summary="Save dashboard settings",
    description="Updates config.yaml with new settings values, validates against schema, creates automatic backup, and marks dashboard for restart. Accepts JSON body with 'values' object containing nested key-value pairs.",
    responses={
        200: {
            "description": "Settings saved successfully",
            "content": {
                "application/json": {
                    "example": {
                        "success": True,
                        "message": "Settings saved successfully",
                        "backup": "config.yaml.backup_20260509_143025",
                        "restart_required": True
                    }
                }
            },
        },
        400: {"description": "Validation error"},
        500: {"description": "Failed to save config"},
    },
)
async def save_settings(request: Request):
    """Save config with backup, validation, and optional restart."""
    import shutil
    body = await request.json()
    new_values = body.get("values", {})

    cfg_path = DASHBOARD_DIR / "config.yaml"

    # 1. Load current config
    try:
        if cfg_path.exists():
            with open(cfg_path) as f:
                current = yaml.safe_load(f) or {}
        else:
            current = {}
    except Exception as e:
        return JSONResponse({"success": False, "error": f"Failed to read config: {e}"}, status_code=500)

    # 2. Validate new values
    errors = []
    for group in SETTINGS_SCHEMA:
        for field in group["fields"]:
            key = field["key"]
            if key not in new_values:
                continue
            val = new_values[key]
            ftype = field.get("type", "text")

            if ftype == "number":
                try:
                    val = float(val) if val != "" else 0
                    if val != int(val):
                        pass  # allow floats
                    val_num = val
                    if "min" in field and val_num < field["min"]:
                        errors.append(f"{field['label']}: value {val} is below minimum {field['min']}")
                    if "max" in field and val_num > field["max"]:
                        errors.append(f"{field['label']}: value {val} exceeds maximum {field['max']}")
                except (ValueError, TypeError):
                    errors.append(f"{field['label']}: '{val}' is not a valid number")

            if ftype == "select":
                # Normalize booleans to string "true"/"false"
                if isinstance(val, bool):
                    val = "true" if val else "false"
                    new_values[key] = val
                str_val = str(val).lower() if isinstance(val, bool) else str(val)
                if str_val not in field.get("options", []):
                    errors.append(f"{field['label']}: '{val}' is not a valid option")

            if ftype == "text" and "key" in field:
                # Sanitize — no newlines, reasonable length
                val = str(val).strip()[:200]
                new_values[key] = val

    if errors:
        return JSONResponse({"success": False, "errors": errors}, status_code=400)

    # 3. Create backup
    SETTINGS_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = SETTINGS_BACKUP_DIR / f"config_{ts}.yaml"
    if cfg_path.exists():
        shutil.copy2(cfg_path, backup_path)
    # Keep only last 20 config backups
    backups = sorted(SETTINGS_BACKUP_DIR.glob("config_*.yaml"))
    if len(backups) > 20:
        for old in backups[:-20]:
            old.unlink(missing_ok=True)

    # 4. Apply new values to config
    for key, val in new_values.items():
        # Convert number types properly
        for group in SETTINGS_SCHEMA:
            for field in group["fields"]:
                if field["key"] == key and field.get("type") == "number":
                    try:
                        val = float(val)
                        if val == int(val):
                            val = int(val)
                    except (ValueError, TypeError):
                        pass
                    break
        _set_nested(current, key, val)

    # 5. Write config
    try:
        with open(cfg_path, "w") as f:
            yaml.dump(current, f, default_flow_style=False, allow_unicode=True)
    except Exception as e:
        # Restore backup on write failure
        if backup_path.exists():
            shutil.copy2(backup_path, cfg_path)
        return JSONResponse({"success": False, "error": f"Write failed, backup restored: {e}"}, status_code=500)

    # 6. Invalidate caches
    global CFG
    CFG = _load_config()

    return {
        "success": True,
        "message": "Settings saved successfully",
        "backup": str(backup_path.name),
        "restart_required": True,
    }


@app.post(
    "/api/settings/restart-dashboard",
    tags=["System"],
    summary="Restart dashboard service",
    description="Restarts the server-dashboard systemd service. Tries multiple methods in order: sudo with password (if configured), passwordless sudo, then direct systemctl. Used after config changes.",
    responses={
        200: {
            "description": "Restart initiated",
            "content": {
                "application/json": {
                    "example": {"success": True, "message": "Dashboard restarting..."}
                }
            },
        },
        500: {
            "description": "Restart failed",
            "content": {
                "application/json": {
                    "example": {"success": False, "message": "Failed to restart service"}
                }
            },
        },
    },
)
async def restart_dashboard_service():
    """Restart the server-dashboard systemd service."""
    try:
        # Try multiple methods in order of preference
        for method in [
            # Method 1: sudo with password (if configured)
            (SUDO_MODE == "sudo_password" and SUDO_PASSWORD,
             ["sudo", "-S", "systemctl", "restart", "server-dashboard.service"]),
            # Method 2: passwordless sudo
            (True, ["sudo", "-n", "systemctl", "restart", "server-dashboard.service"]),
            # Method 3: direct systemctl (no sudo)
            (True, ["systemctl", "restart", "server-dashboard.service"]),
        ]:
            should_try, cmd = method
            if not should_try:
                continue
            stdin = SUDO_PASSWORD + "\n" if cmd[:2] == ["sudo", "-S"] else None
            result = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=15)
            if result.returncode == 0:
                return {"success": True, "message": "Dashboard restarting..."}
            # If method 2 failed, try next
        return JSONResponse(
            {"success": False, "message": result.stderr.strip() or "Restart failed"},
            status_code=500
        )
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)



# ── Health endpoint ────────────────────────────────────────────────────

def get_health_state(system: dict, connections: dict) -> str:
    """Determine health state based on metrics and connections."""
    # Critical conditions (RED)
    if connections.get("internet", {}).get("reachable", True) is False:
        return "red"
    if connections.get("hermes_gateway", {}).get("reachable", True) is False:
        return "red"
    bot_connection = connections.get("binance_bot", {})
    if bot_connection.get("enabled", True) and bot_connection.get("reachable", True) is False:
        return "red"
    
    # Check disk critical (>= 95%)
    if system.get("disk", {}).get("percent", 100) >= 95:
        return "red"
    
    # Warning conditions (YELLOW)
    cpu_warn = system.get("cpu", {}).get("percent", 100) >= 70
    ram_warn = system.get("memory", {}).get("percent", 100) >= 80
    disk_warn = system.get("disk", {}).get("percent", 100) >= 85
    
    # Any warning metric = YELLOW (unless critical already)
    if cpu_warn or ram_warn or disk_warn:
        return "yellow"
    
    # All good = GREEN
    return "green"


@app.get(
    "/api/health",
    tags=["System"],
    summary="Health status",
    description="Returns aggregated health state based on system metrics and connectivity checks. Returns 'green', 'yellow', or 'red' state with full system and connection data. Used for health badges and monitoring.",
    responses={
        200: {
            "description": "Health status",
            "content": {
                "application/json": {
                    "example": {
                        "state": "green",
                        "system": {
                            "cpu": {"percent": 12.5},
                            "memory": {"percent": 38.4},
                            "disk": {"percent": 49.1}
                        },
                        "connections": {
                            "google_dns": {"status": "ok"},
                            "cloudflare": {"status": "ok"},
                            "internet": {"reachable": True}
                        }
                    }
                }
            },
        }
    },
)
async def health():
    """Get aggregated health state from system metrics and connections."""
    system = _cached("health_system", 30, get_system_metrics)
    connections = _cached("health_connections", 15, get_connections)
    state = get_health_state(system, connections)
    
    return {
        "state": state,
        "system": system,
        "connections": connections,
    }


# ── Main ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"  Hermes Server Dashboard starting on http://{SERVER_HOST}:{SERVER_PORT}")
    print(f"  Plugins loaded: {len(_loaded_plugins)}")
    print("  Starting metrics TSDB collector...")
    metrics_tsdb.start_collector()
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level=SERVER_LOG_LEVEL)
