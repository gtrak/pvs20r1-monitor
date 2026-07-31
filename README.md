# pvs20r1-monitor

Prometheus-compatible metrics exporter for SunPower **SMS-PVS20R1** solar gateways.

Runs on a Raspberry Pi bridging two networks:
- **WiFi** → your home network (where Prometheus/Grafana live)
- **Ethernet** → PVS20R1 LAN2 port (direct to gateway)

Zero external dependencies — Python stdlib only.

## Architecture

```
┌──────────────┐  172.27.153.1/24   ┌───────────────┐  :8080/metrics   ┌────────────┐
│   PVS20R1    │◄─── ethernet ──────►│  Raspberry Pi  │◄─────────────────│ Prometheus │
│   Gateway    │  LAN2 (eth0)        │  (WiFi + eth0) │                  │ / Grafana  │
└──────────────┘                     └───────────────┘                   └────────────┘
                                             ▲
                                    your WiFi network
```

## Endpoints

| Path | Format | Description |
|---|---|---|
| `/metrics` | Prometheus text | Gauges and counters for your scraper |
| `/` | JSON | Quick health check + latest data |
| `/debug` | JSON | Full state dump (troubleshooting) |

## Metrics

### System-level

| Name | Type | Description |
|---|---|---|
| `pvs_gateway_up` | gauge | 1 if reachable, 0 otherwise |
| `pvs_system_state` | gauge | 0=unknown, 1=offline, 2=partial, 3=producing |
| `pvs_inverter_count` | gauge | Number of inverters found |
| `pvs_total_ac_power_watts` | gauge | Sum of all inverter AC power (watts) |
| `pvs_scrape_duration_seconds` | gauge | Last poll duration |
| `pvs_scrape_success` | counter | Successful poll cycles |
| `pvs_scrape_failures` | counter | Failed poll cycles |
| `pvs_uptime_seconds` | counter | Server uptime |

### Per-inverter (label: `serial`)

| Name | Type | Description |
|---|---|---|
| `pvs_inverter_state` | gauge | 0=unknown, 1=offline, 2=idle, 3=producing |
| `pvs_inverter_ac_power_watts` | gauge | AC power output (watts) |
| `pvs_inverter_ac_voltage_volts` | gauge | AC voltage (volts) |
| `pvs_inverter_ac_current_amps` | gauge | AC current (amps) |
| `pvs_inverter_dc_voltage_volts` | gauge | DC voltage (volts) |
| `pvs_inverter_dc_current_amps` | gauge | DC current (amps) |
| `pvs_inverter_heatsink_temp_celsius` | gauge | Heat sink temperature (celsius) |
| `pvs_inverter_lifetime_energy_kwh` | gauge | Total lifetime energy produced (kWh) |


### State model

System state tracks whether panels are producing vs offline. The state enum captures the actual operating condition — so a brief outage shows as `state=1` for its duration, then returns to `state=3` when production resumes.

**System states:**
- `0` — unknown (no data)
- `1` — offline (all inverters at zero power)
- `2` — partial (some inverters producing, others not)
- `3` — producing (all inverters above threshold)

**Per-inverter states:**
- `0` — unknown (couldn't fetch data)
- `1` — offline (no power, no voltage)
- `2` — idle (has voltage but < 10W output — degraded or just starting)
- `3` — producing (> 10W AC output)

## Setup

### 1. Hardware

Connect a Raspberry Pi with:
- **Ethernet cable** → PVS20R1 **LAN2** port (not LAN1)
- **WiFi** → your home network (already configured)

### 2. Deploy

```bash
# Copy project to Pi, then:
cd ~/pvs20r1-monitor
sudo bash setup.sh
```

`setup.sh` does:
- Configures `eth0` with static IP `172.27.153.254/24` (no gateway — WiFi is the default route)
- Persists the config across reboots via `dhcpcd`
- Installs the `pvs20r1-monitor` systemd service
- Runs the monitor in foreground for a test (Ctrl+C to stop)

### 3. Production run

```bash
sudo systemctl restart pvs20r1-monitor
sudo systemctl status pvs20r1-monitor
sudo journalctl -u pvs20r1-monitor -f
```

### 4. Point Prometheus at it

```yaml
scrape_configs:
  - job_name: 'pvs20r1-solar'
    static_configs:
      - targets: ['<pi-ip>:8080']
```

### 5. Grafana alerts

```promql
# Gateway down
pvs_gateway_up == 0

# System offline
pvs_system_state == 1

# Partial outage (some inverters down)
pvs_system_state == 2

# Inverter offline
pvs_inverter_state == 1

# Total power below threshold
pvs_total_ac_power_watts < 100
```

## Netdata integration

The `netdata/` directory contains a complete alerting integration for Netdata:

- `go.d/prometheus.conf` — scrapes `http://solar-pi:8080/metrics` and
  auto-creates one chart per `pvs_*` metric. Metric names use full-unit
  suffixes (`_volts`, `_amps`, `_watts`, `_celsius`) so Netdata's
  auto-derived unit labels are human-readable.
- `charts.d/solar.chart.sh` + `charts.d/solar.conf` — a small bash/python
  collector that publishes a derived `solar.not_generating` gauge. It gates on
  sun elevation (computed locally from your lat/lon, no `is_daylight` flag
  needed) and panel DC voltage, so the alarm distinguishes "daytime fault"
  from "night" — the one case the raw metrics alone can't cover (an inverter
  fully off during the day reads DC~0V, indistinguishable from night).
- `health.d/solar.conf` — three alarms: `solar_fault` (gateway unreachable),
  `solar_not_generating` (daytime + not producing, 30 min sustained), and
  `solar_no_data` (exporter stopped sending data).

Metric names use full-unit words rather than single-letter suffixes
(`_amps` not `_a`) per the [Prometheus naming guidance](https://prometheus.io/docs/practices/naming/),
so any Prometheus consumer (Grafana, Prometheus, Netdata) derives readable
units deterministically instead of guessing.

### Netdata install

The `netdata/` files are **templates** with placeholder tokens
(`__SOLAR_HOST__`, `__SOLAR_PORT__`, `__SOLAR_JOB__`, `__SOLAR_LAT__`,
`__SOLAR_LON__`); no host is hardcoded. `netdata/install.sh` renders them
and installs everything (it also keeps the go.d job name consistent across
the scrape config and the health alarms).

```bash
# Defaults: host=solar-pi  port=8080  job=solar_pi  lat/lon=Annapolis, MD
sudo ./netdata/install.sh

# Custom host + site coordinates:
sudo ./netdata/install.sh --host 192.168.1.50 --lat 40.7128 --lon -74.0060

# Render only, don't restart netdata (e.g. to review first):
sudo ./netdata/install.sh --no-restart
```

`install.sh` copies the charts.d module into `/usr/libexec/netdata/charts.d/`
(charts.d.plugin only scans its stock dir for `*.chart.sh`), enables the
module in `charts.d.conf` (custom modules need `solar=yes`, **not** `=force`),
and restarts netdata. See `./netdata/install.sh --help` for all options.

Discord notifications: set `DISCORD_WEBHOOK_URL` and `DEFAULT_RECIPIENT_DISCORD`
in `/etc/netdata/health_alarm_notify.conf`; the alarms ship with `to: discord`.

## Troubleshooting

```bash
# Quick status
curl http://<pi-ip>:8080/

# Debug — see full state
curl http://<pi-ip>:8080/debug

# Logs
tail -f ~/pvs20r1-monitor/logs/monitor.log

# Service status
systemctl status pvs20r1-monitor
journalctl -u pvs20r1-monitor --since "1 hour ago"
```

## Prior Art

Built from these projects:

- **[Dukat-Gul/SMS-PVS20R1](https://github.com/Dukat-Gul/SMS-PVS20R1)** — Node-RED flow that polls `http://172.27.153.1/cgi-bin/dl_cgi?Command=DeviceDetails&SerialNumber=XXX` and uploads to PVOutput.org. This project replaces Node-RED with a standalone Prometheus exporter. Key insight: the PVS20R1 serves HTML (not JSON), so you parse `<td class="info">` cells from the response.

- **[blog.gruby.com — Monitoring a SunPower Solar System](https://blog.gruby.com/2020/04/28/monitoring-a-sunpower-solar-system.html)** — Describes the LAN2 port setup (172.27.153.1/24, LAN1 runs DHCP), HTML scraping approach, and why local monitoring matters (SunPower's cloud only showed aggregate, not per-inverter). Also documents using a Pi as an HTTP proxy between WiFi and the PVS gateway.

- **[smcneece/ha-esunpower](https://github.com/smcneece/ha-esunpower)** — Home Assistant integration for SunPower PVS5/PVS6 gateways. Local API, no cloud required. Demonstrates the varserver API and direct PVS communication that informed this project's approach.