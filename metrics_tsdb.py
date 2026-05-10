#!/usr/bin/env python3
"""
Lightweight time-series metrics store for Hermes Server Dashboard.
Uses SQLite as embedded TSDB — no external Prometheus needed.

Collectors:
  - System metrics (CPU, RAM, disk, network, temperature)
  - BitsyMiner hashrate (configurable URL)
  - Binance Bot trades & P&L (configurable URL)

Alerts via Telegram for CPU > 80%, disk > 90%, service down.
"""

import json
import logging
import os
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import psutil
import requests

logger = logging.getLogger("metrics_tsdb")

DB_PATH = Path(__file__).parent / "metrics.db"
SCRAPE_INTERVAL = 30  # seconds between metric collections

# Alert thresholds
ALERT_CPU_PCT = 80
ALERT_DISK_PCT = 90
ALERT_COOLDOWN = 300  # 5 min between repeated alerts

# ── Config loading ──────────────────────────────────────────────────
def _load_urls_from_config():
    """Read integration URLs from config.yaml."""
    try:
        import yaml
        cfg_path = Path(__file__).parent / "config.yaml"
        if not cfg_path.exists():
            cfg_path = Path(__file__).parent / "config.example.yaml"
        if cfg_path.exists():
            with open(cfg_path) as f:
                cfg = yaml.safe_load(f) or {}
            integrations = cfg.get("integrations", {})
            return {
                "bitsy": integrations.get("bitsy_url", ""),
                "bot": integrations.get("bot_api_url", "http://localhost:18790/api"),
            }
    except Exception:
        pass
    return {"bitsy": "", "bot": "http://localhost:18790/api"}

_urls = _load_urls_from_config()
BITSY_URL = _urls["bitsy"]
BOT_URL = _urls["bot"] + "/state" if _urls["bot"] else ""

# ── Schema ──────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
    ts REAL NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    tags TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(ts);
CREATE INDEX IF NOT EXISTS idx_metrics_metric ON metrics(metric);
CREATE INDEX IF NOT EXISTS idx_metrics_metric_ts ON metrics(metric, ts);
"""


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _insert(conn: sqlite3.Connection, metric: str, value: float, tags: dict = None):
    """Insert a single metric data point."""
    conn.execute(
        "INSERT INTO metrics (ts, metric, value, tags) VALUES (?, ?, ?, ?)",
        (time.time(), metric, value, json.dumps(tags or {}))
    )


def _insert_batch(conn: sqlite3.Connection, rows: list):
    """Insert multiple metric data points. rows = [(ts, metric, value, tags_json), ...]"""
    conn.executemany(
        "INSERT INTO metrics (ts, metric, value, tags) VALUES (?, ?, ?, ?)",
        rows
    )


# ── Retention ───────────────────────────────────────────────────────

RETENTION_DAYS = 35  # keep ~5 weeks of data

def _cleanup(conn: sqlite3.Connection):
    cutoff = time.time() - RETENTION_DAYS * 86400
    conn.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,))
    conn.commit()


# ── Collectors ──────────────────────────────────────────────────────

def collect_system(conn: sqlite3.Connection) -> dict:
    """Collect system metrics and insert into TSDB."""
    now = time.time()
    rows = []

    # CPU
    cpu_pct = psutil.cpu_percent(interval=None)
    rows.append((now, "system_cpu_percent", cpu_pct, "{}"))

    # Per-core CPU
    for i, core_pct in enumerate(psutil.cpu_percent(interval=None, percpu=True)):
        rows.append((now, "system_cpu_core_percent", core_pct, json.dumps({"core": i})))

    # CPU frequency
    freq = psutil.cpu_freq()
    if freq:
        rows.append((now, "system_cpu_freq_mhz", freq.current, "{}"))

    # Memory
    mem = psutil.virtual_memory()
    rows.append((now, "system_memory_percent", mem.percent, "{}"))
    rows.append((now, "system_memory_used_bytes", mem.used, "{}"))
    rows.append((now, "system_memory_available_bytes", mem.available, "{}"))

    # Swap
    swap = psutil.swap_memory()
    rows.append((now, "system_swap_percent", swap.percent, "{}"))

    # Disk
    disk = psutil.disk_usage("/")
    rows.append((now, "system_disk_percent", disk.percent, "{}"))
    rows.append((now, "system_disk_used_bytes", disk.used, "{}"))
    rows.append((now, "system_disk_free_bytes", disk.free, "{}"))

    # Network I/O rates (calculated from delta)
    net = psutil.net_io_counters()
    rows.append((now, "system_net_bytes_recv", net.bytes_recv, "{}"))
    rows.append((now, "system_net_bytes_sent", net.bytes_sent, "{}"))

    # Temperature
    cpu_temp = 0
    try:
        output = subprocess.check_output(["sensors", "-j"], text=True, timeout=5)
        data = json.loads(output)
        for chip, readings in data.items():
            for key, val in readings.items():
                if isinstance(val, dict):
                    for subkey, subval in val.items():
                        if subkey.endswith("_input") and "temp" in subkey.lower():
                            name = key  # e.g. "Package id 0", "Core 0"
                            if name.lower().replace(" ", "") in ("packageid0", "cpu", "tctl", "tdie"):
                                cpu_temp = subval
                                rows.append((now, "system_cpu_temp_celsius", subval, json.dumps({"sensor": name})))
                            else:
                                rows.append((now, "system_temp_celsius", subval, json.dumps({"sensor": name})))
    except Exception:
        try:
            output = subprocess.check_output(["sensors"], text=True, timeout=5)
            import re
            for line in output.splitlines():
                if "°C" in line and ":" in line:
                    match = re.search(r"([+-]?\d+\.?\d*)°C", line.split(":")[1])
                    if match:
                        val = float(match.group(1))
                        name = line.split(":")[0].strip()
                        if "Package" in name or "Tctl" in name or "Cpu" in name:
                            cpu_temp = val
                        rows.append((now, "system_temp_celsius", val, json.dumps({"sensor": name})))
        except Exception:
            pass

    # Load average
    load1, load5, load15 = os.getloadavg()
    rows.append((now, "system_load_1m", load1, "{}"))
    rows.append((now, "system_load_5m", load5, "{}"))
    rows.append((now, "system_load_15m", load15, "{}"))

    # Uptime
    uptime_s = time.time() - psutil.boot_time()
    rows.append((now, "system_uptime_seconds", uptime_s, "{}"))

    _insert_batch(conn, rows)
    conn.commit()

    return {
        "cpu_pct": cpu_pct,
        "disk_pct": disk.percent,
        "cpu_temp": cpu_temp,
        "mem_pct": mem.percent,
    }


def collect_bitsy(conn: sqlite3.Connection) -> dict:
    """Collect BitsyMiner metrics from statusJson."""
    now = time.time()
    try:
        resp = requests.get(BITSY_URL, timeout=5)
        data = resp.json()
        rows = []

        # Hashrate (hps field, can be string with k/M suffix)
        hashrate = 0
        hps = str(data.get("hps", data.get("hashRate", data.get("hashrate", "0"))))
        try:
            if hps.endswith("k"):
                hashrate = float(hps[:-1]) * 1000
            elif hps.endswith("M"):
                hashrate = float(hps[:-1]) * 1000000
            elif hps.endswith("G"):
                hashrate = float(hps[:-1]) * 1000000000
            else:
                hashrate = float(hps.replace(",", ""))
        except:
            hashrate = 0
        rows.append((now, "bitsy_hashrate_khs", hashrate / 1000 if hashrate > 10000 else hashrate, "{}"))

        # Mining status
        mining = 1 if data.get("mining", False) else 0
        rows.append((now, "bitsy_mining", mining, "{}"))

        # Pool connected
        connected = 1 if data.get("poolConnected", False) else 0
        rows.append((now, "bitsy_pool_connected", connected, "{}"))

        # Pool submissions (shares)
        shares_str = str(data.get("poolSubmissions", data.get("shares", "0")))
        try:
            if shares_str.endswith("k"):
                shares = float(shares_str[:-1]) * 1000
            elif shares_str.endswith("M"):
                shares = float(shares_str[:-1]) * 1000000
            else:
                shares = float(shares_str.replace(",", ""))
        except:
            shares = 0
        rows.append((now, "bitsy_shares", shares, "{}"))

        # Best difficulty
        best_diff = 0
        try:
            best_diff = float(str(data.get("bestDifficulty", "0")).replace(",", ""))
        except:
            pass
        rows.append((now, "bitsy_best_difficulty", best_diff, "{}"))

        # Uptime (in milliseconds)
        uptime_ms = 0
        try:
            uptime_ms = float(str(data.get("uptime", "0")).replace(",", ""))
        except:
            pass
        rows.append((now, "bitsy_uptime_seconds", uptime_ms / 1000, "{}"))

        # Block height
        block_height = 0
        try:
            block_height = float(str(data.get("blockHeight", "0")).replace(",", ""))
        except:
            pass
        rows.append((now, "bitsy_block_height", block_height, "{}"))

        _insert_batch(conn, rows)
        conn.commit()
        return {"hashrate": hashrate, "mining": mining}

    except Exception as e:
        logger.debug(f"BitsyMiner collect failed: {e}")
        return {"hashrate": 0, "mining": 0}


def collect_bot(conn: sqlite3.Connection) -> dict:
    """Collect Binance Bot metrics from local API."""
    now = time.time()
    try:
        resp = requests.get(BOT_URL, timeout=5)
        data = resp.json()
        rows = []

        # P&L
        pnl = data.get("daily_pnl", 0)
        rows.append((now, "bot_daily_pnl", pnl, "{}"))

        # Equity
        equity = data.get("min_operational_quote", 0)
        if isinstance(equity, str):
            equity = float(equity)
        rows.append((now, "bot_equity", equity, "{}"))

        # Consecutive losses
        losses = data.get("consecutive_losses", 0)
        rows.append((now, "bot_consecutive_losses", losses, "{}"))

        # Price
        indicators = data.get("indicators", {})
        price = indicators.get("price", 0)
        if price:
            rows.append((now, "bot_btc_price", price, "{}"))

        # RSI
        rsi = indicators.get("rsi_5m", indicators.get("rsi_14", 0))
        if rsi:
            rows.append((now, "bot_rsi", rsi, "{}"))

        # ADX
        adx = indicators.get("adx_15m", indicators.get("adx_1h", 0))
        if adx:
            rows.append((now, "bot_adx", adx, "{}"))

        # Position value
        pos = data.get("open_position", {})
        if pos and pos.get("side"):
            rows.append((now, "bot_position", 1 if pos["side"] == "long" else -1,
                         json.dumps({"side": pos["side"], "entry": pos.get("entry_price", 0)})))

        # Trades (from decisions)
        decisions = data.get("last_decisions", [])
        executed = [d for d in decisions if d.get("decision") == "EXECUTADO"]
        rows.append((now, "bot_trades_count", len(executed), "{}"))

        _insert_batch(conn, rows)
        conn.commit()
        return {"pnl": pnl, "equity": equity, "price": price}

    except Exception as e:
        logger.debug(f"Bot collect failed: {e}")
        return {"pnl": 0, "equity": 0, "price": 0}


# ── Alerts ──────────────────────────────────────────────────────────

_last_alerts: dict[str, float] = {}


def _send_telegram_alert(message: str, alert_key: str):
    """Send alert via Telegram using Hermes gateway."""
    now = time.time()
    if alert_key in _last_alerts and now - _last_alerts[alert_key] < ALERT_COOLDOWN:
        return

    # Respect config: integrations.telegram.enabled
    try:
        cfg_path = Path(__file__).parent / "config.yaml"
        if cfg_path.exists():
            import yaml as _yaml
            with open(cfg_path) as f:
                _cfg = _yaml.safe_load(f) or {}
            if str(_cfg.get("integrations", {}).get("telegram", {}).get("enabled", "true")).lower() != "true":
                logger.debug(f"Alert skipped (telegram alerts disabled): {alert_key}")
                return
    except Exception:
        pass
    _last_alerts[alert_key] = now

    try:
        # Read Telegram config from Hermes .env
        env_path = Path.home() / ".hermes" / ".env"
        bot_token = None
        chat_id = None
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    bot_token = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("TELEGRAM_CHAT_ID="):
                    chat_id = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("TELEGRAM_HOME_CHANNEL="):
                    chat_id = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("TELEGRAM_ALLOWED_USERS="):
                    if not chat_id:
                        chat_id = line.split("=", 1)[1].strip().strip('"').strip("'")

        if bot_token and chat_id:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": f"⚠️ HERMES ALERT\n{message}",
                "parse_mode": "HTML"
            }
            requests.post(url, json=payload, timeout=10)
            logger.info(f"Alert sent: {alert_key}")
    except Exception as e:
        logger.warning(f"Failed to send Telegram alert: {e}")


def check_alerts(system_data: dict, bitsy_data: dict, bot_data: dict):
    """Check alert thresholds and send Telegram notifications."""
    # CPU alert
    if system_data.get("cpu_pct", 0) > ALERT_CPU_PCT:
        _send_telegram_alert(
            f"🔥 CPU: <b>{system_data['cpu_pct']:.1f}%</b> (threshold: {ALERT_CPU_PCT}%)",
            "cpu_high"
        )

    # Disk alert
    if system_data.get("disk_pct", 0) > ALERT_DISK_PCT:
        _send_telegram_alert(
            f"💾 Disco: <b>{system_data['disk_pct']:.1f}%</b> (threshold: {ALERT_DISK_PCT}%)",
            "disk_high"
        )

    # Temperature alert
    if system_data.get("cpu_temp", 0) > 85:
        _send_telegram_alert(
            f"🌡 CPU Temp: <b>{system_data['cpu_temp']:.1f}°C</b>",
            "temp_high"
        )

    # BitsyMiner down (hashrate = 0) — DISABLED: miner não precisa de monitoramento
    # if bitsy_data.get("hashrate", 0) == 0:
    #     _send_telegram_alert(
    #         "⛏ BitsyMiner: <b>Hashrate = 0</b> — possível queda",
    #         "bitsy_down"
    #     )

    # Bot down
    if bot_data.get("equity", 0) == 0 and bot_data.get("pnl", 0) == 0 and bot_data.get("price", 0) == 0:
        _send_telegram_alert(
            "🤖 Binance Bot: <b>Sem resposta</b> — possível queda",
            "bot_down"
        )


# ── Query helpers ───────────────────────────────────────────────────

def query_range(conn: sqlite3.Connection, metric: str,
                duration_seconds: int = 3600,
                tags: dict = None,
                aggregate: str = "auto") -> list:
    """
    Query a metric over a time range, returning [(timestamp, value), ...].

    Args:
        metric: metric name pattern (supports SQL LIKE with %)
        duration_seconds: how far back to look
        tags: optional tag filter dict
        aggregate: 'auto' (aggregate to ~200 points), 'raw', or 'avg'
    """
    now = time.time()
    start = now - duration_seconds
    cutoff = now

    if "%" in metric:
        query = "SELECT ts, value FROM metrics WHERE metric LIKE ? AND ts >= ? AND ts <= ? ORDER BY ts"
        rows = conn.execute(query, (metric, start, cutoff)).fetchall()
    else:
        if tags:
            query = "SELECT ts, value FROM metrics WHERE metric = ? AND ts >= ? AND ts <= ? ORDER BY ts"
            rows = conn.execute(query, (metric, start, cutoff)).fetchall()
            # Filter by tags in Python
            rows = [(ts, val) for ts, val, in rows]
        else:
            query = "SELECT ts, value FROM metrics WHERE metric = ? AND ts >= ? AND ts <= ? ORDER BY ts"
            rows = conn.execute(query, (metric, start, cutoff)).fetchall()

    if not rows:
        return []

    if aggregate == "raw" or len(rows) <= 200:
        return rows

    # Downsample to ~200 points
    step = max(1, len(rows) // 200)
    result = []
    for i in range(0, len(rows), step):
        chunk = rows[i:i + step]
        avg_ts = sum(r[0] for r in chunk) / len(chunk)
        avg_val = sum(r[1] for r in chunk) / len(chunk)
        result.append((avg_ts, avg_val))
    return result


def query_tags_range(conn: sqlite3.Connection, metric: str,
                     duration_seconds: int = 3600,
                     tag_key: str = None) -> dict:
    """
    Query a metric grouped by a tag value.
    Returns {tag_value: [(timestamp, value), ...]}
    """
    now = time.time()
    start = now - duration_seconds
    query = "SELECT ts, value, tags FROM metrics WHERE metric = ? AND ts >= ? ORDER BY ts"
    rows = conn.execute(query, (metric, start)).fetchall()

    grouped = {}
    for ts, val, tags_json in rows:
        tags = json.loads(tags_json)
        tag_val = tags.get(tag_key, "default") if tag_key else "default"
        grouped.setdefault(tag_val, []).append((ts, val))

    # Downsample each series
    result = {}
    for tag_val, series in grouped.items():
        if len(series) <= 200:
            result[tag_val] = series
        else:
            step = max(1, len(series) // 200)
            downsampled = []
            for i in range(0, len(series), step):
                chunk = series[i:i + step]
                avg_ts = sum(r[0] for r in chunk) / len(chunk)
                avg_val = sum(r[1] for r in chunk) / len(chunk)
                downsampled.append((avg_ts, avg_val))
            result[tag_val] = downsampled

    return result


# ── Background collector thread ─────────────────────────────────────

_running = False
_thread = None


def _collector_loop():
    """Main collection loop running in background thread."""
    global _running
    conn = _get_conn()
    logger.info("Metrics collector started")

    # Cleanup on start
    _cleanup(conn)

    cleanup_counter = 0
    while _running:
        try:
            sys_data = collect_system(conn)
            bitsy_data = collect_bitsy(conn)
            bot_data = collect_bot(conn)
            check_alerts(sys_data, bitsy_data, bot_data)

            # Plugin metric collectors
            try:
                import plugins as _ps
                for _p in _ps.get_plugins():
                    try:
                        _metrics = _p.collect_metrics()
                        if _metrics:
                            now = time.time()
                            for _mname, _mval in _metrics.items():
                                _insert(conn, _mname, float(_mval))
                            conn.commit()
                    except Exception as _e:
                        logger.debug(f"Plugin {_p.name} collector error: {_e}")
            except ImportError:
                pass

            cleanup_counter += 1
            if cleanup_counter >= 120:  # Cleanup every ~1 hour (120 * 30s)
                _cleanup(conn)
                cleanup_counter = 0

        except Exception as e:
            logger.error(f"Collector error: {e}")

        # Sleep in small increments so we can stop quickly
        for _ in range(SCRAPE_INTERVAL):
            if not _running:
                break
            time.sleep(1)

    conn.close()
    logger.info("Metrics collector stopped")


def start_collector():
    """Start the background metrics collector."""
    global _running, _thread
    if _running:
        return
    _running = True
    _thread = threading.Thread(target=_collector_loop, daemon=True, name="metrics-collector")
    _thread.start()
    logger.info("Metrics collector thread spawned")


def stop_collector():
    """Stop the background metrics collector."""
    global _running
    _running = False


def get_db() -> sqlite3.Connection:
    """Get a read connection to the metrics DB."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    return conn
