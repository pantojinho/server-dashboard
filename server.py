#!/usr/bin/env python3
"""
Hermes Server Dashboard — Retro terminal-style monitoring web dashboard.
Backend: FastAPI serving system metrics + Hermes status via JSON API.
"""

import json
import os
import re
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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
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
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
HISTORY_LEN = CFG.get("metrics", {}).get("history_length", 60)

# Hermes gateway service name (configurable for non-standard setups)
HERMES_GATEWAY_SERVICE = os.environ.get(
    "HERMES_GATEWAY_SERVICE",
    CFG.get("hermes", {}).get("gateway_service", "hermes-gateway.service"),
)

# Sudo configuration — password from env var, NEVER hardcoded
SUDO_MODE = CFG.get("sudo_mode", "none")
SUDO_PASSWORD = os.environ.get("SUDO_PASSWORD", "")

# Build allowed services from config
ALLOWED_RESTART_SERVICES = {}
for svc in CFG.get("services", []):
    if svc.get("allow_restart", False):
        name = svc["name"]
        scope = svc.get("scope", "system")
        ALLOWED_RESTART_SERVICES[name] = (scope, f"{name}.service")


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

    load1, load5, load15 = os.getloadavg()

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
        "hostname": os.uname().nodename,
        "os": f"{os.uname().sysname} {os.uname().release}",
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

    # User and hostname
    info["user"] = os.environ.get("USER", os.environ.get("LOGNAME", "user"))
    info["hostname"] = os.uname().nodename

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
        info["os"] = "Linux"
        info["os_id"] = "linux"

    # Kernel
    info["kernel"] = os.uname().release

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
        env = _user_env()
        result = subprocess.run(
            ["systemctl", "--user", "is-active", HERMES_GATEWAY_SERVICE],
            capture_output=True, text=True, timeout=3, env=env
        )
        connections["hermes_gateway"] = {"reachable": result.stdout.strip() == "active", "port": "user-svc"}
    except Exception:
        connections["hermes_gateway"] = {"reachable": False, "port": "user-svc"}

    # Binance Bot
    bot_api_url = CFG.get("integrations", {}).get("bot_api_url", "http://localhost:18790/api")
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", "2", bot_api_url + "/state"],
            capture_output=True, timeout=4
        )
        connections["binance_bot"] = {"reachable": result.returncode == 0, "url": bot_api_url}
    except Exception:
        connections["binance_bot"] = {"reachable": False, "url": bot_api_url}

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
        name = svc["name"]
        svc_name = f"{name}.service" if not name.endswith(".service") else name
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
                    # systemd timestamp format: "Day YYYY-MM-DD HH:MM:SS TZ"
                    # Use monotonic fallback: parse the timestamp
                    # Try strptime for common format
                    from datetime import datetime as _dt
                    ts_str = active_enter.strip()
                    # systemd format: "Fri 2026-05-09 14:35:22 -03"
                    # Try multiple formats
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
                        # Last resort: try direct parse
                        try:
                            import dateutil.parser
                            enter_dt = dateutil.parser.parse(ts_str)
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
        "model": "unknown",
        "platforms": {},
        "cron_jobs": [],
        "config": {},
    }

    # Check if gateway is running
    try:
        env = _user_env()
        result = subprocess.run(
            ["systemctl", "--user", "is-active", HERMES_GATEWAY_SERVICE],
            capture_output=True, text=True, timeout=3, env=env
        )
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
    try:
        env = _user_env()
        result = subprocess.run(
            ["systemctl", "--user", "is-active", HERMES_GATEWAY_SERVICE],
            capture_output=True, text=True, timeout=3, env=env
        )
        if result.stdout.strip() == "active":
            platforms["telegram"] = "connected"
    except Exception:
        try:
            result = subprocess.run(["pgrep", "-f", "hermes.*gateway"], capture_output=True, text=True, timeout=3)
            if result.stdout.strip():
                platforms["telegram"] = "connected"
        except Exception:
            pass

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
    uid = str(os.getuid())
    env = os.environ.copy()
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
                        "hostname": "linuxmint",
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

@app.get("/api/kanban")
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
            WHERE status IN ('in_progress', 'triage')
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


@app.get("/api/neofetch")
async def neofetch():
    return _cached("neofetch", 30, get_neofetch)


@app.get("/api/docker")
async def docker_info():
    return _cached("docker", 10, get_docker)


@app.get("/api/connections")
async def connections():
    return _cached("connections", 15, get_connections)


@app.get("/api/temps")
async def temps():
    return _cached("temps", 5, get_temperatures)


@app.get("/api/services")
async def services():
    return _cached("services", 10, get_services)


@app.get("/api/hermes")
async def hermes_status():
    return _cached("hermes", 15, get_hermes_status)


@app.post("/api/service/{name}/restart")
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
                ["bash", "-c", f"echo '{SUDO_PASSWORD}' | sudo -S systemctl restart {service_name}"],
                capture_output=True, text=True, timeout=15
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


@app.get("/bot-api/{path:path}")
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

@app.get("/api/hermes/models")
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


@app.post("/api/hermes/model")
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
        env = _user_env()
        subprocess.run(["systemctl", "--user", "restart", HERMES_GATEWAY_SERVICE.replace(".service", "")],
                       capture_output=True, timeout=30, env=env)
        # Invalidate hermes cache
        if "hermes" in _api_cache:
            del _api_cache["hermes"]
        return {"success": True, "model": model or c['model'].get('default'),
                "provider": provider or c['model'].get('provider')}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.post("/api/backup")
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

@app.get("/api/logs/{service}")
async def get_logs(service: str, lines: int = 80):
    """Fast journalctl log reader for the dashboard."""
    allowed = {
        "hermes": ("user", HERMES_GATEWAY_SERVICE),
        "bot": ("system", "binance-bot.service"),
        "dashboard": ("system", "server-dashboard.service"),
        "watchdog": (None, None),
    }
    if service not in allowed:
        return JSONResponse({"error": f"Unknown service: {service}"}, status_code=400)

    try:
        scope, svc_name = allowed[service]

        if scope == "user":
            env = _user_env()
            # Use JSON output + filter by _SYSTEMD_USER_UNIT for user services
            cmd = f"journalctl --user --no-pager -n {min(lines*5, 1000)} --output=short-iso"
            result = subprocess.run(
                ["bash", "-c", cmd],
                capture_output=True, text=True, timeout=8, env=env
            )
            # Filter lines that belong to this service
            # Use JSON to get _SYSTEMD_USER_UNIT, then match
            cmd_json = f"journalctl --user --no-pager -n {min(lines*5, 1000)} --output=json"
            result_json = subprocess.run(
                ["bash", "-c", cmd_json],
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
            cmd = f"journalctl -u {svc_name} --no-pager -n {lines} --output=short-iso"
            if SUDO_MODE == "sudo_password" and SUDO_PASSWORD:
                result = subprocess.run(
                    ["bash", "-c", f"echo '{SUDO_PASSWORD}' | sudo -S {cmd}"],
                    capture_output=True, text=True, timeout=8
                )
            elif SUDO_MODE == "sudo_nopasswd":
                result = subprocess.run(
                    ["bash", "-c", f"sudo -n {cmd}"],
                    capture_output=True, text=True, timeout=8
                )
            else:
                # Try without sudo (user may have access)
                result = subprocess.run(
                    ["bash", "-c", cmd],
                    capture_output=True, text=True, timeout=8
                )
            lines_list = [l for l in result.stdout.splitlines() if l.strip()]
            return {"service": service, "lines": lines_list[-lines:], "count": len(lines_list)}

        else:  # watchdog
            cmd = f"journalctl --no-pager -n {lines*5} --output=short-iso | grep -iE 'watchdog' | tail -n {lines}"
            if SUDO_MODE == "sudo_password" and SUDO_PASSWORD:
                result = subprocess.run(
                    ["bash", "-c", f"echo '{SUDO_PASSWORD}' | sudo -S {cmd}"],
                    capture_output=True, text=True, timeout=8
                )
            elif SUDO_MODE == "sudo_nopasswd":
                result = subprocess.run(
                    ["bash", "-c", f"sudo -n {cmd}"],
                    capture_output=True, text=True, timeout=8
                )
            else:
                result = subprocess.run(
                    ["bash", "-c", cmd],
                    capture_output=True, text=True, timeout=8
                )
            lines_list = [l for l in result.stdout.splitlines() if l.strip()]
            return {"service": service, "lines": lines_list[-lines:], "count": len(lines_list)}

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/graficos/series")
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


@app.get("/api/graficos/series_tagged")
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


@app.get("/api/graficos/dashboard")
async def graficos_dashboard(duration: int = 3600):
    """
    Return all metrics needed for the Graficos tab in one request.
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


@app.get("/api/graficos/status")
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

@app.get("/api/plugins")
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


@app.get("/api/plugins/{name}/tab")
async def plugin_tab(name: str):
    """Return a specific plugin's tab HTML content."""
    for p in _loaded_plugins:
        if p.name == name:
            html = p.tab_html()
            if html:
                return {"name": name, "html": html, "scripts": p.scripts_html(), "styles": p.styles_html()}
            return JSONResponse({"error": f"Plugin '{name}' has no tab"}, status_code=404)
    return JSONResponse({"error": f"Plugin '{name}' not found"}, status_code=404)


# ── Main ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"  Hermes Server Dashboard starting on http://{SERVER_HOST}:{SERVER_PORT}")
    print(f"  Plugins loaded: {len(_loaded_plugins)}")
    print("  Starting metrics TSDB collector...")
    metrics_tsdb.start_collector()
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level=SERVER_LOG_LEVEL)
