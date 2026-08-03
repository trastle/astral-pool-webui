# astral-pool-webui

A small Python service that talks to an AstralPool Viron eQuilibrium (eQ)
salt-water chlorinator over Bluetooth Low Energy, and exposes it as:

- a read-only web dashboard (auto-refreshing, mobile friendly)
- a Prometheus `/metrics` endpoint
- an MQTT bridge, publishing state and accepting commands

It exists mainly to solve one problem: the chlorinator only speaks BLE, and
BLE has a short range. Run this on a small always-on machine (a Raspberry Pi
works well) placed near the pool equipment, and it becomes reachable from
anywhere on your network over HTTP/MQTT instead.

Built on [pychlorinator](https://github.com/pbutterworth/pychlorinator), the
Python library that reverse-engineered the eQ's BLE protocol.

## Why not just use Home Assistant's Bluetooth integration directly?

You can, if your Home Assistant host is within BLE range of the chlorinator.
If it isn't (pool equipment is often at the opposite end of a property from
where HA runs), this project bridges the gap: it runs *at* the pool
equipment and re-publishes over MQTT, which travels over your regular
network instead of BLE.

Pairs well with an MQTT-aware fork of
[astralpool_chlorinator](https://github.com/pbutterworth/astralpool_chlorinator)
(the Home Assistant custom integration for this device) - e.g.
[trastle/astralpool_chlorinator](https://github.com/trastle/astralpool_chlorinator),
`mqtt` branch - which reads the same `chlorinator/<name>/state` topic this
project publishes to and gives you full HA entities (select/number/button,
not just sensors).

## Features

- **Dashboard** (`/`) - mode, pump, pH, chlorine, salt cell, and acid dosing
  at a glance, plus pool info and the chlorinator's own built-in pump
  schedule.
- **Prometheus metrics** (`/metrics`) - every numeric/boolean/categorical
  field, for your own dashboards/alerting.
- **MQTT** - publishes full state as JSON on `chlorinator/<name>/state`
  every poll; optionally accepts commands on `chlorinator/<name>/action` and
  `chlorinator/<name>/setup` (see below) to actually control the device.
- **Help page** (`/help`) - links to AstralPool's own manual/support pages,
  plus a glossary of what each dashboard field means.
- Read-only by default. Command handling only does anything once you've
  pointed something (like the HA fork above) at the action/setup topics.

## Requirements

- A host with Bluetooth, within BLE range of the chlorinator (a Raspberry Pi
  3/4/5 running Raspberry Pi OS or Debian works well; anything running Linux
  with BlueZ should work).
- Python 3.11+.
- The chlorinator's **Bluetooth access code** - the same code the
  "Chlorinator Go" phone app uses to pair. Check the app's settings, or the
  device's own screen/manual.
- (Optional) An MQTT broker - e.g. the Mosquitto add-on if you run Home
  Assistant OS/Supervised.

## Project layout

- [`gateway/`](gateway/) - the actual app (`app.py`, plus `config.py`,
  `mqtt_bridge.py`, `quirks.py`). This is what you deploy/run as a service.
- [`scripts/`](scripts/) - standalone helper scripts for debugging your BLE
  setup. Not part of the deployed app - see their docstrings and the
  [Diagnostic scripts](#diagnostic-scripts) section below.
- [`tests/`](tests/) - tests for `gateway/`.

## Setup

```bash
git clone <this-repo-url>
cd astral-pool-webui
cp gateway/.env.example gateway/.env
# edit gateway/.env: set CHLORINATOR_ACCESS_CODE at minimum
bash provision.sh
```

`provision.sh` sets up Bluetooth, creates a venv, and installs dependencies.
It's safe to re-run any time.

Run it directly to try it out:

```bash
source venv/bin/activate
python3 gateway/app.py
# dashboard at http://<host>:8080/
```

Or install it as a systemd service so it survives reboots - see
[`systemd/chlorinator-gateway.service`](systemd/chlorinator-gateway.service)
(edit the `User`/paths for your setup first).

To deploy from a separate dev machine over SSH instead of working directly
on the Pi:

```bash
./deploy.sh pi@your-pi-hostname ~/.ssh/your_key
```

### Configuration

All configuration is via environment variables (or `gateway/.env` - see
[`gateway/.env.example`](gateway/.env.example)):

| Variable | Default | Notes |
|---|---|---|
| `CHLORINATOR_DEVICE_NAME` | `POOL01` | BLE advertised name of your chlorinator |
| `CHLORINATOR_ACCESS_CODE` | *(required)* | Bluetooth access code from the phone app |
| `POLL_INTERVAL_SECONDS` | `60` | How often to poll over BLE |
| `HTTP_PORT` | `8080` | Web UI / metrics port |
| `MQTT_HOST` | *(unset)* | Leave unset to run without MQTT |
| `MQTT_PORT` | `1883` | |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | *(unset)* | |
| `MQTT_BASE_TOPIC` | `chlorinator/<device name, lowercased>` | |

The app works standalone (dashboard + metrics) with no MQTT broker
configured at all - MQTT is purely additive.

### Command topics (optional)

If `MQTT_HOST` is set, the bridge subscribes to:

- `chlorinator/<name>/action` - JSON `{"action": <int>[, ...kwargs]}`,
  matching pychlorinator's `ChlorinatorActions` enum (e.g. turning the pump
  on/off, setting speed).
- `chlorinator/<name>/setup` - JSON kwargs for setpoint-style changes (e.g.
  `{"ph_control_setpoint": 7.4}`).

A message on either topic triggers a real write to the device over BLE,
followed by an immediate re-poll so the new state shows up right away
instead of waiting for the next scheduled poll.

## Diagnostic scripts

`scripts/` holds standalone helper scripts for debugging your setup - they
are **not** part of the deployed gateway app. Useful roughly in this order
when first getting a device talking to this project, or debugging a
connection issue:

- `scripts/ble_scan.py` - scans for nearby BLE devices and flags anything
  that looks like an eQ chlorinator.
- `scripts/gatt_probe.py` - connects and enumerates GATT
  services/characteristics (no access code needed - just confirms a real
  connection is possible).
- `scripts/read_state.py` - does the full read-side handshake (access code
  required) and prints the current state once, without touching any
  settings.

Run any of them from the project root (venv activated):

```bash
source venv/bin/activate
python3 scripts/read_state.py
```

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

## License

MIT - see [LICENSE](LICENSE).
