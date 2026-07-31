# charts.d collector: derived "solar not generating" gauge.
#
# not_generating = 1 when it is daytime AND an inverter is not producing
# when it should be. Two fault modes:
#   - DC > solar_dc_threshold (sun on panels) but AC power == 0  (not converting)
#   - DC < solar_dc_dead      (inverter off/dead) but AC == 0    (should be on)
# "Daytime" is computed locally from sun elevation (no producer is_daylight
# flag -- that was unreliable and has been removed). Pure-python, no deps.
#
# Config (netdata/charts.d/solar.conf):
#   solar_lat / solar_lon     -- site coordinates (default: Annapolis, MD)
#   solar_dc_threshold        -- DC volts above which panels should produce (300)
#   solar_dc_dead             -- DC volts below which inverter is "off" (10)
#
# Install: copy to /usr/libexec/netdata/charts.d/solar.chart.sh (charts.d.plugin
# only scans its own stock dir for .chart.sh files) and set `solar=yes` (NOT
# `force`, which only works for built-in stock modules) in charts.d.conf.
#
# Loaded by charts.d.plugin (no shebang needed). All symbols are solar_*.

solar_update_every=10
solar_priority=95000

solar_lat="${solar_lat:-38.9784}"        # Annapolis, MD
solar_lon="${solar_lon:--76.4921}"
solar_dc_threshold="${solar_dc_threshold:-300}"
solar_dc_dead="${solar_dc_dead:-10}"

solar_not_generating=

solar_get() {
  local vals
  vals=$(python3 - "$solar_lat" "$solar_lon" "$solar_dc_threshold" "$solar_dc_dead" <<'PY' 2>/dev/null
import math, json, sys, urllib.request
from datetime import datetime, timezone

lat = float(sys.argv[1]); lon = float(sys.argv[2])
dc_thr = float(sys.argv[3]); dc_dead = float(sys.argv[4])

def sun_elevation(t):
    doy = t.timetuple().tm_yday
    decl = 23.45 * math.sin(math.radians(360 * (284 + doy) / 365))
    B = math.radians(360 * (doy - 81) / 365)
    eot = 9.87*math.sin(2*B) - 7.53*math.cos(B) - 1.5*math.sin(B)
    solar_noon = 720 - 4*lon + eot
    now_min = t.hour*60 + t.minute + t.second/60
    ha = math.radians((now_min - solar_noon) / 4.0)
    lr = math.radians(lat); dr = math.radians(decl)
    return math.degrees(math.asin(
        math.sin(lr)*math.sin(dr) + math.cos(lr)*math.cos(dr)*math.cos(ha)))

try:
    with urllib.request.urlopen("http://solar-pi:8080/", timeout=8) as r:
        d = json.load(r)
except Exception:
    print("ERR"); sys.exit()

daytime = sun_elevation(datetime.now(timezone.utc).replace(tzinfo=None)) > 3.0
ng = 0
if daytime:
    for inv in d.get("inverters", []) or []:
        try:
            dc = float(inv.get("avg_dc_voltage") or 0)
            ac = float(inv.get("avg_ac_power") or 0)
        except (TypeError, ValueError):
            continue
        if ac <= 0 and (dc > dc_thr or dc < dc_dead):
            ng = 1
print(ng)
PY
)
  [ "$vals" = "ERR" ] && return 1
  read -r solar_not_generating <<< "$vals"
  return 0
}

solar_check() {
  solar_get || { error "solar: cannot fetch from http://solar-pi:8080/"; return 1; }
  return 0
}

solar_create() {
  cat <<EOF
CHART solar.not_generating '' "Solar not generating during the day" "state" solar solar.not_generating line $solar_priority $solar_update_every '' '' 'solar'
DIMENSION not_generating '' absolute 1 1
EOF
  return 0
}

solar_update() {
  solar_get || return 1
  cat <<EOF
BEGIN solar.not_generating $1
SET not_generating = $solar_not_generating
END
EOF
  return 0
}
