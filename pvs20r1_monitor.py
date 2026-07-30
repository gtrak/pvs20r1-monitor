#!/usr/bin/env python3
"""
pvs20r1-monitor — Prometheus-compatible exporter for SunPower SMS-PVS20R1 gateways.

Runs on a Raspberry Pi bridging two networks:
  - WiFi (wlan0): main home network (default route)
  - Ethernet (eth0): PVS20R1 LAN2 port (172.27.153.254/24, no gateway)

Serves Prometheus metrics on :8080/metrics for your existing stack to scrape.
Zero external dependencies — Python stdlib only.

Setup:  sudo bash setup.sh
"""

import argparse
import json
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Thread
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from logging.handlers import RotatingFileHandler

# ── Configuration ────────────────────────────────────────────────────────────

PVS_IP = "172.27.153.1"
POLL_INTERVAL = 300        # seconds between polls
LISTEN_ADDR = "0.0.0.0"
LISTEN_PORT = 8080
LOG_DIR = Path(__file__).resolve().parent / "logs"
DAYLIGHT_HOURS = (6, 19)   # hour range considered daylight


# ── Logging ──────────────────────────────────────────────────────────────────

LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger("pvs20r1")
logger.setLevel(logging.INFO)

console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))

file_handler = RotatingFileHandler(
    LOG_DIR / "monitor.log", maxBytes=5 * 1024 * 1024, backupCount=3
)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)-7s %(message)s"
))

logger.addHandler(console)
logger.addHandler(file_handler)


# ── HTML Parser ─────────────────────────────────────────────────────────────

class HTMLInfoExtractor:
    """Extract <td class='info'> cell values from PVS20R1 HTML pages."""

    def __init__(self, html: str):
        self.html = html
        pattern = r"<td\s+class=['\"]info['\"]>(.*?)</td>"
        raw = re.findall(pattern, html, re.DOTALL)
        self.values: list[str] = []
        for cell in raw:
            clean = re.sub(r"<[^>]+>", " ", cell)
            clean = clean.replace("&nbsp;", "").replace("\xa0", "").strip()
            # collapse whitespace
            clean = re.sub(r"\s+", " ", clean)
            self.values.append(clean)

    def get_by_index(self, idx: int) -> str | None:
        if 0 <= idx < len(self.values):
            return self.values[idx]
        return None

    @property
    def all_values(self) -> list[str]:
        return list(self.values)


def parse_float(text: str | None) -> float | None:
    """Extract a float from a string like '1.23 kW' or '0.00 kW'."""
    if not text:
        return None
    m = re.search(r"[-]?([\d.]+)", text.replace(",", ""))
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def extract_serials(html: str) -> list[str]:
    """Extract inverter serial numbers from URL parameters in HTML."""
    serials = re.findall(r"SerialNumber=([A-Z0-9]{8,})", html, re.IGNORECASE)
    # Deduplicate preserving order
    seen = set()
    result = []
    for s in serials:
        if s not in seen:
            seen.add(s)
            result.append(s)
    return result


# ── HTTP Fetcher ─────────────────────────────────────────────────────────────

def fetch(url: str, timeout: int = 10) -> str:
    """Fetch URL, return decoded text."""
    req = Request(url)
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ── Data Collector ───────────────────────────────────────────────────────────

class CollectorState:
    """Holds the latest poll results, updated in background thread."""

    def __init__(self):
        self.gateway_up = 0
        self.total_ac_power_w: float | None = None
        self.inverter_count = 0
        self.scrape_duration_s = 0.0
        self.scrape_success = 0
        self.scrape_failures = 0
        self.start_time = time.time()
        self.inverters: list[dict] = []
        self.last_poll_time: float = 0
        self.is_daylight = False
        self.system_state = 0  # 0=unknown, 1=offline, 2=partial, 3=producing
        self.errors: list[str] = []

    def uptime(self) -> float:
        return time.time() - self.start_time


state = CollectorState()


def collect_once():
    """
    Single poll cycle: fetch gateway, extract inverters, populate state.
    """
    global state
    start = time.time()
    now = datetime.now()
    state.errors.clear()

    try:
        # Fetch main page
        html = fetch(f"http://{PVS_IP}/")
        state.gateway_up = 1
        extractor = HTMLInfoExtractor(html)

        logger.info("Main page: %d info fields extracted", len(extractor.all_values))

        # Get inverter serials
        serials = extract_serials(html)
        logger.info("Found %d inverter(s): %s", len(serials), serials)

        # If no serials from main page, try DeviceList
        if not serials:
            try:
                list_html = fetch(f"http://{PVS_IP}/cgi-bin/dl_cgi?Command=DeviceList")
                serials = extract_serials(list_html)
                if not serials:
                    # Fallback: look for alphanumeric serial patterns
                    serials = re.findall(r"\b([A-Z]{1,3}\d{7,11})\b", html, re.IGNORECASE)
                    serials = list(dict.fromkeys(serials))
            except Exception as e:
                state.errors.append(f"DeviceList fetch failed: {e}")

        # Determine daylight
        is_daylight = DAYLIGHT_HOURS[0] <= now.hour < DAYLIGHT_HOURS[1]
        state.is_daylight = is_daylight

        inverters = []
        total_power = 0.0
        producing_count = 0

        for serial in serials:
            try:
                inv_html = fetch(
                    f"http://{PVS_IP}/cgi-bin/dl_cgi?Command=DeviceDetails&SerialNumber={serial}"
                )
                inv_ext = HTMLInfoExtractor(inv_html)
                vals = inv_ext.all_values

                # Field mapping (from Node-RED prior art):
                # [0]=Name, [1]=Total Lifetime Energy, [2]=Last Refresh,
                # [3]=Avg AC Power, [4]=Model, [5]=Avg AC Voltage,
                # [6]=Serial Number, [7]=Avg AC Current, [8]=Software Version,
                # [9]=Avg DC Voltage, [10]=Avg DC Current,
                # [11]=Avg Heat Sink Temp, [12]=Avg AC Frequency

                inv = {
                    "serial": serial,
                    "name": vals[0] if len(vals) > 0 else "",
                    "model": vals[4] if len(vals) > 4 else "",
                    "avg_ac_power": parse_float(vals[3]) if len(vals) > 3 else None,
                    "avg_ac_voltage": parse_float(vals[5]) if len(vals) > 5 else None,
                    "avg_ac_current": parse_float(vals[7]) if len(vals) > 7 else None,
                    "avg_dc_voltage": parse_float(vals[9]) if len(vals) > 9 else None,
                    "avg_dc_current": parse_float(vals[10]) if len(vals) > 10 else None,
                    "avg_heatsink_temp": parse_float(vals[11]) if len(vals) > 11 else None,
                    "lifetime_energy_kwh": parse_float(vals[1]) if len(vals) > 1 else None,
                    "last_refresh": vals[2] if len(vals) > 2 else None,
                    "software_version": vals[8] if len(vals) > 8 else None,
                }

                # Power is reported in kW -> convert to W
                power_w = inv["avg_ac_power"] * 1000 if inv["avg_ac_power"] else 0
                inv["power_w"] = power_w
                total_power += power_w

                # Inverter state: 0=unknown, 1=offline, 2=idle, 3=producing
                if power_w > 10:  # > 10W = definitely producing
                    inv["state"] = 3
                    producing_count += 1
                elif inv["avg_ac_voltage"] is not None and inv["avg_ac_voltage"] > 0:
                    # Has voltage but no power — could be idle or degraded
                    inv["state"] = 2
                else:
                    inv["state"] = 1

                # Calculate data age
                if inv["last_refresh"]:
                    lr_match = re.search(
                        r"(\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}:\d{2})",
                        inv["last_refresh"]
                    )
                    if lr_match:
                        try:
                            lr_dt = datetime.strptime(lr_match.group(1), "%m/%d/%Y %H:%M:%S")
                            age = (now - lr_dt).total_seconds()
                            inv["data_age_s"] = max(0, age)
                        except ValueError:
                            inv["data_age_s"] = None
                    else:
                        inv["data_age_s"] = None
                else:
                    inv["data_age_s"] = None

                inverters.append(inv)
                logger.info(
                    "Inverter %s: %.1fW AC, %.1fV AC, %.1fC heatsink, state=%d",
                    serial, power_w, inv["avg_ac_voltage"] or 0,
                    inv["avg_heatsink_temp"] or 0, inv["state"]
                )

            except Exception as e:
                state.errors.append(f"Inverter {serial}: {e}")
                logger.error("Inverter %s error: %s", serial, e)

        state.inverters = inverters
        state.total_ac_power_w = round(total_power, 1)
        state.inverter_count = len(inverters)
        state.scrape_success += 1

        # System state: 0=unknown, 1=offline, 2=partial, 3=producing
        if is_daylight and len(inverters) > 0:
            if producing_count == len(inverters):
                state.system_state = 3  # all producing
            elif producing_count > 0:
                state.system_state = 2  # some producing, some not
            else:
                state.system_state = 1  # none producing during daylight
        elif not is_daylight:
            state.system_state = 0  # nighttime — unknown
        else:
            state.system_state = 0  # no inverters found

        logger.info(
            "System state=%d (daylight=%s, %d/%d producing, %.1fW total)",
            state.system_state, is_daylight, producing_count, len(inverters), total_power
        )

    except Exception as e:
        state.gateway_up = 0
        state.scrape_failures += 1
        state.errors.append(f"Poll failed: {e}")
        logger.error("Poll failed: %s", e)

    state.scrape_duration_s = time.time() - start
    state.last_poll_time = time.time()


def poll_loop():
    """Background thread: polls every POLL_INTERVAL seconds."""
    collect_once()  # initial poll immediately
    while True:
        try:
            time.sleep(POLL_INTERVAL)
            collect_once()
        except Exception as e:
            logger.error("Poll loop error: %s", e)


# ── Prometheus Metrics Formatter ─────────────────────────────────────────────

def format_metrics() -> str:
    """Generate Prometheus text-format metrics from current state."""
    lines = []

    def metric(name: str, value: float | int, labels: dict | None = None,
               metric_type: str = "gauge", help_text: str = ""):
        if help_text:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {metric_type}")
        lbl = ""
        if labels:
            lbl = "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}"
        lines.append(f"{name}{lbl} {value}")

    # Gateway and system state
    metric("pvs_gateway_up", state.gateway_up,
           help_text="1 if gateway reachable, 0 otherwise")
    metric("pvs_system_state", state.system_state,
           help_text="0=unknown, 1=offline (no power during day), "
                      "2=partial (some inverters down), 3=producing")
    metric("pvs_is_daylight", 1 if state.is_daylight else 0,
           help_text="1 if current time is within daylight hours")

    # Per-inverter metrics
    for inv in state.inverters:
        lbl = {"serial": inv["serial"]}

        metric("pvs_inverter_state", inv.get("state", 0), lbl,
               help_text="0=unknown, 1=offline, 2=idle, 3=producing")

        if inv.get("avg_ac_power") is not None:
            metric("pvs_inverter_ac_power_w", inv["power_w"], lbl,
                   help_text="Instantaneous AC power output (watts)")
        if inv.get("avg_ac_voltage") is not None:
            metric("pvs_inverter_ac_voltage_v", inv["avg_ac_voltage"], lbl,
                   help_text="AC voltage (volts)")
        if inv.get("avg_ac_current") is not None:
            metric("pvs_inverter_ac_current_a", inv["avg_ac_current"], lbl,
                   help_text="AC current (amps)")
        if inv.get("avg_dc_voltage") is not None:
            metric("pvs_inverter_dc_voltage_v", inv["avg_dc_voltage"], lbl,
                   help_text="DC voltage (volts)")
        if inv.get("avg_dc_current") is not None:
            metric("pvs_inverter_dc_current_a", inv["avg_dc_current"], lbl,
                   help_text="DC current (amps)")
        if inv.get("avg_heatsink_temp") is not None:
            metric("pvs_inverter_heatsink_temp_c", inv["avg_heatsink_temp"], lbl,
                   help_text="Heat sink temperature (C)")
        if inv.get("lifetime_energy_kwh") is not None:
            metric("pvs_inverter_lifetime_energy_kwh", inv["lifetime_energy_kwh"], lbl,
                   help_text="Total lifetime energy produced (kWh)")
        if inv.get("data_age_s") is not None:
            metric("pvs_inverter_data_age_seconds", inv["data_age_s"], lbl,
                   help_text="Seconds since last refresh timestamp")

    # Aggregates
    metric("pvs_inverter_count", state.inverter_count,
           help_text="Number of inverters found")
    if state.total_ac_power_w is not None:
        metric("pvs_total_ac_power_w", state.total_ac_power_w,
               help_text="Sum of all inverter AC power (watts)")
    metric("pvs_scrape_duration_seconds", round(state.scrape_duration_s, 3),
           help_text="Last poll duration (seconds)")
    metric("pvs_scrape_success", state.scrape_success,
           metric_type="counter", help_text="Total successful poll cycles")
    metric("pvs_scrape_failures", state.scrape_failures,
           metric_type="counter", help_text="Total failed poll cycles")
    metric("pvs_uptime_seconds", state.uptime(),
           metric_type="counter", help_text="Server uptime (seconds)")

    return "\n".join(lines) + "\n"


# ── HTTP Server ──────────────────────────────────────────────────────────────

class MetricsHandler(BaseHTTPRequestHandler):
    """Serve Prometheus metrics on /metrics, health on /."""

    def log_message(self, fmt, *args):
        # Suppress default logging (too noisy for Prometheus scrapes)
        pass

    def do_GET(self):
        if self.path == "/metrics":
            body = format_metrics().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/":
            # Health check + quick status JSON
            status = {
                "gateway_up": bool(state.gateway_up),
                "system_state": state.system_state,
                "is_daylight": state.is_daylight,
                "total_ac_power_w": state.total_ac_power_w,
                "inverter_count": state.inverter_count,
                "inverters": state.inverters,
                "last_poll": datetime.fromtimestamp(state.last_poll_time).isoformat(),
                "errors": state.errors,
                "scrape_success": state.scrape_success,
                "scrape_failures": state.scrape_failures,
            }
            body = json.dumps(status, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/debug":
            # Dump raw data for troubleshooting
            debug = {
                "state": {
                    "gateway_up": state.gateway_up,
                    "system_state": state.system_state,
                    "is_daylight": state.is_daylight,
                    "inverter_count": state.inverter_count,
                    "total_ac_power_w": state.total_ac_power_w,
                },
                "inverters": state.inverters,
                "errors": state.errors,
            }
            body = json.dumps(debug, indent=2, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Not found. Try /metrics or /\n")


def run_server():
    """Start the HTTP metrics server."""
    server = HTTPServer((LISTEN_ADDR, LISTEN_PORT), MetricsHandler)
    logger.info("Serving on http://%s:%d/metrics", LISTEN_ADDR, LISTEN_PORT)

    # Graceful shutdown on SIGTERM/SIGINT
    def shutdown(signum, frame):
        logger.info("Shutting down...")
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    server.serve_forever()


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    global POLL_INTERVAL
    parser = argparse.ArgumentParser(description="PVS20R1 Prometheus Monitor")
    parser.add_argument("--daemon", action="store_true", help="Fork to background")
    parser.add_argument("--poll-interval", type=int, default=POLL_INTERVAL,
                        help=f"Seconds between polls (default: {POLL_INTERVAL})")
    args = parser.parse_args()

    POLL_INTERVAL = args.poll_interval

    logger.info("PVS20R1 monitor starting (poll interval: %ds)", POLL_INTERVAL)

    if args.daemon:
        pid = os.fork()
        if pid > 0:
            # Parent — print PID and exit
            with open(LOG_DIR / "monitor.pid", "w") as f:
                f.write(str(pid))
            print(f"Started as PID {pid}")
            sys.exit(0)

    # Start background poll thread
    poll_thread = Thread(target=poll_loop, daemon=True)
    poll_thread.start()
    logger.info("Poll thread started")

    # Run HTTP server in main thread
    run_server()


if __name__ == "__main__":
    main()