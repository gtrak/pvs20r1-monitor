#!/bin/bash
# netdata/install.sh — render the templated Netdata integration and install it.
#
# The config files in this directory use placeholder tokens:
#   __SOLAR_HOST__  __SOLAR_PORT__  __SOLAR_JOB__  __SOLAR_LAT__  __SOLAR_LON__
# This script renders them (with the options below) and copies the result to
# the system Netdata paths, then restarts Netdata. No host is hardcoded in
# the committed templates.
#
# Usage:
#   sudo ./install.sh [--host HOST] [--port PORT] [--job JOB]
#                     [--lat LAT] [--lon LON] [--restart|--no-restart]
#
# Defaults:
#   host=solar-pi  port=8080  job=solar_pi
#   lat=38.9784   lon=-76.4921   (Annapolis, MD)
#
# Example (custom host + coordinates):
#   sudo ./install.sh --host 192.168.1.50 --lat 40.7128 --lon -74.0060

set -euo pipefail

HOST="solar-pi"
PORT="8080"
JOB="solar_pi"
LAT="38.9784"
LON="-76.4921"
RESTART=1

while [ $# -gt 0 ]; do
  case "$1" in
    --host)    HOST="$2"; shift 2;;
    --port)    PORT="$2"; shift 2;;
    --job)     JOB="$2"; shift 2;;
    --lat)     LAT="$2"; shift 2;;
    --lon)     LON="$2"; shift 2;;
    --no-restart) RESTART=0; shift;;
    --restart) RESTART=1; shift;;
    -h|--help) sed -n '2,22p' "$0"; exit 0;;
    *) echo "unknown argument: $1" >&2; exit 1;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: run as root (sudo)." >&2
  exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

render() {
  sed -e "s|__SOLAR_HOST__|$HOST|g" \
      -e "s|__SOLAR_PORT__|$PORT|g" \
      -e "s|__SOLAR_JOB__|$JOB|g" \
      -e "s|__SOLAR_LAT__|$LAT|g" \
      -e "s|__SOLAR_LON__|$LON|g" "$1"
}

echo "Rendering Netdata config: host=$HOST port=$PORT job=$JOB lat=$LAT lon=$LON"

mkdir -p "$TMP/go.d" "$TMP/health.d" "$TMP/charts.d"
render "$HERE/go.d/prometheus.conf"   > "$TMP/go.d/prometheus.conf"
render "$HERE/health.d/solar.conf"   > "$TMP/health.d/solar.conf"
render "$HERE/charts.d/solar.conf"   > "$TMP/charts.d/solar.conf"
cp "$HERE/charts.d/solar.chart.sh"   "$TMP/charts.d/solar.chart.sh"

echo "Installing to /etc/netdata (and the charts.d stock dir)..."
install -Dm 0644 "$TMP/go.d/prometheus.conf"    /etc/netdata/go.d/prometheus.conf
install -Dm 0644 "$TMP/health.d/solar.conf"    /etc/netdata/health.d/solar.conf
install -Dm 0644 "$TMP/charts.d/solar.conf"    /etc/netdata/charts.d/solar.conf
# charts.d.plugin only scans its stock dir for *.chart.sh:
install -Dm 0755 "$TMP/charts.d/solar.chart.sh" /usr/libexec/netdata/charts.d/solar.chart.sh

# Enable the charts.d module (custom modules need =yes, NOT =force).
# Idempotent: only add the two lines if absent.
touch /etc/netdata/charts.d.conf
grep -qs '^enable_all_charts="yes"' /etc/netdata/charts.d.conf || \
  echo 'enable_all_charts="yes"' >> /etc/netdata/charts.d.conf
grep -qs '^solar=yes' /etc/netdata/charts.d.conf || \
  echo 'solar=yes' >> /etc/netdata/charts.d.conf

# Pre-flight: warn if the exporter isn't up. go.d's prometheus job does NOT
# auto-recover from a startup-time scrape failure (it gets quarantined), so
# if netdata is (re)started while the exporter is down, NO solar charts will
# appear until netdata is restarted again AFTER the exporter comes up.
# This is the exact failure mode where install.sh "succeeds" but the
# integration is silently dead.
EXPORTER_URL="http://$HOST:$PORT/metrics"
EXPORTER_UP=0
if command -v curl >/dev/null 2>&1; then
  if curl -fsS --max-time 5 "$EXPORTER_URL" >/dev/null 2>&1; then
    EXPORTER_UP=1
  fi
else
  echo "NOTE: curl not found; skipping exporter pre-flight check." >&2
fi
if [ "$EXPORTER_UP" -eq 1 ]; then
  echo "Pre-flight: exporter $EXPORTER_URL is reachable."
else
  echo "WARNING: exporter $EXPORTER_URL is not reachable." >&2
  echo "         The pvs20r1-monitor exporter must be running on '$HOST' (port $PORT)" >&2
  echo "         before Netdata can collect solar data. Configs are still installed." >&2
  echo "         If you restart netdata now, go.d will quarantine the solar_pi job and" >&2
  echo "         produce NO charts until you restart netdata again AFTER the exporter" >&2
  echo "         is up." >&2
fi

if [ "$RESTART" -eq 1 ]; then
  echo "Restarting netdata..."
  systemctl restart netdata
  if [ "$EXPORTER_UP" -ne 1 ]; then
    echo "WARNING: netdata restarted while exporter was down -- solar charts will be" >&2
    echo "         missing. Bring the exporter up, then: sudo systemctl restart netdata" >&2
  fi
fi

echo "Done. Alarms: solar_fault, solar_not_generating, solar_no_data"
echo "Discord: set DISCORD_WEBHOOK_URL / DEFAULT_RECIPIENT_DISCORD in"
echo "         /etc/netdata/health_alarm_notify.conf (alarms ship with to: discord)."
