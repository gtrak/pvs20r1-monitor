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

if [ "$RESTART" -eq 1 ]; then
  echo "Restarting netdata..."
  systemctl restart netdata
fi

echo "Done. Alarms: solar_fault, solar_not_generating, solar_no_data"
echo "Discord: set DISCORD_WEBHOOK_URL / DEFAULT_RECIPIENT_DISCORD in"
echo "         /etc/netdata/health_alarm_notify.conf (alarms ship with to: discord)."
