# astral-pool-webui

Talks to an AstralPool Viron eQuilibrium (eQ) salt-water chlorinator over
Bluetooth Low Energy and exposes it as a web dashboard, Prometheus
metrics, and an MQTT bridge.

BLE has short range, so this is meant to run on a small always-on
machine (a Raspberry Pi works well) near the pool equipment, making the
chlorinator reachable over your normal network instead of BLE.

Built on [pychlorinator](https://github.com/pbutterworth/pychlorinator),
which reverse-engineered the eQ's BLE protocol.

Pairs with an MQTT-aware fork of
[astralpool_chlorinator](https://github.com/pbutterworth/astralpool_chlorinator)
- e.g. [trastle/astralpool_chlorinator](https://github.com/trastle/astralpool_chlorinator)
(`mqtt` branch) - for full Home Assistant entities (select/number/button,
not just sensors). If your HA host is already within BLE range of the
chlorinator, you may not need this project at all.

## Features

- **Dashboard** (`/`) - mode, pump, pH, chlorine, salt cell, acid dosing,
  pool info, and the chlorinator's own pump schedule.
- **Prometheus metrics** (`/metrics`) - every device field, plus MQTT
  connection health (`chlorinator_mqtt_*`).
- **MQTT** - publishes state to `chlorinator/<name>/state`; optionally
  accepts commands on `.../action` and `.../setup` (see below).
  Reconnects automatically after a network blip.
- **Help page** (`/help`) - links to AstralPool's manual/support pages
  and a glossary of dashboard terms.
- Read-only until something writes to the action/setup MQTT topics.

## Requirements

- A host with Bluetooth, in BLE range of the chlorinator (a Raspberry Pi
  3/4/5 works well; any Linux with BlueZ should).
- Python 3.11+.
- The chlorinator's Bluetooth access code - the same one the "Chlorinator
  Go" app uses to pair (check the app's settings, or the device manual).
- (Optional) An MQTT broker, e.g. the Mosquitto add-on for Home Assistant.

## Project layout

- [`gateway/`](gateway/) - the app itself. Deploy/run this as a service.
- [`scripts/`](scripts/) - standalone BLE debugging helpers, not part of
  the deployed app - see [Diagnostic scripts](#diagnostic-scripts).
- [`tests/`](tests/) - tests for `gateway/`.

## Setup

```bash
git clone <this-repo-url>
cd astral-pool-webui
cp gateway/.secrets.yaml.example gateway/.secrets.yaml
# edit gateway/.secrets.yaml: set your real access_code
bash provision.sh
```

`provision.sh` creates a dedicated `chlorinator-gateway` system account,
copies the project into its home directory, and installs/starts it as a
systemd service running as that account. Safe to re-run any time
(including after a `git pull`) to redeploy.

Running as its own account means a bug or bad dependency in the
BLE/MQTT/web stack can't touch anything outside that account's home. One
side effect: your regular login can't read its logs/files by default
(everything logs to `journalctl -u chlorinator-gateway` - there's no
separate log file). To grant another account read-only access:

```bash
sudo usermod -aG systemd-journal <your-user>   # read logs without sudo
sudo usermod -aG chlorinator-gateway <your-user>
sudo chmod 750 /home/chlorinator-gateway
```

`gateway/.secrets.yaml` stays owner-only regardless, so this never
exposes the access code or MQTT credentials. (New group membership needs
a fresh login to take effect.)

To try the app out without provisioning anything:

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python3 gateway/app.py
# dashboard at http://<host>:8080/
```

To deploy from a separate dev machine over SSH:
`./deploy.sh pi@your-pi-hostname ~/.ssh/your_key`

For a fully manual systemd install instead of `provision.sh`, see
[`systemd/chlorinator-gateway.service`](systemd/chlorinator-gateway.service).

### Configuration

Three layers, via [Dynaconf](https://www.dynaconf.com/) - each overrides
the one before it:

1. [`gateway/settings.yaml`](gateway/settings.yaml) - committed defaults.
2. `gateway/.secrets.yaml` - gitignored (copy from
   [`gateway/.secrets.yaml.example`](gateway/.secrets.yaml.example)).
   Despite the name, this is for any per-install override, not just
   secrets - it's the one file `provision.sh` preserves across redeploys.
3. Environment variables - prefix `GATEWAY_`, double underscore between
   section and key, e.g. `GATEWAY_MQTT__HOST`.

| Variable | Overrides | Default |
|---|---|---|
| `GATEWAY_CHLORINATOR__DEVICE_NAME` | `chlorinator.device_name` | `POOL01` |
| `GATEWAY_CHLORINATOR__ACCESS_CODE` | `chlorinator.access_code` | *(required)* |
| `GATEWAY_CHLORINATOR__POLL_INTERVAL_SECONDS` | `chlorinator.poll_interval_seconds` | `60` |
| `GATEWAY_WEB__HTTP_PORT` | `web.http_port` | `8080` |
| `GATEWAY_WEB__ALLOWED_CIDRS` | `web.allowed_cidrs` | private/loopback ranges - see below |
| `GATEWAY_MQTT__HOST` | `mqtt.host` | *(unset)* |
| `GATEWAY_MQTT__PORT` | `mqtt.port` | `1883` |
| `GATEWAY_MQTT__USERNAME` / `GATEWAY_MQTT__PASSWORD` | `mqtt.username` / `mqtt.password` | *(unset)* |
| `GATEWAY_MQTT__BASE_TOPIC` | `mqtt.base_topic` | `chlorinator/<device name, lowercased>` |
| `GATEWAY_MQTT__LAST_KNOWN_GOOD_CACHE_FILE` | `mqtt.last_known_good_cache_file` | `last_known_good_readings.json` |

Leave `mqtt.host` unset to run without MQTT - dashboard and metrics
still work.

### Restricting network access

Only requests from `web.allowed_cidrs` reach the dashboard/metrics -
everything else gets `403`. Defaults to private/loopback ranges
(`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `127.0.0.0/8`,
`::1/128`), so accidentally exposing this port doesn't expose it to the
internet.

VPN ranges (e.g. Tailscale's `100.64.0.0/10`) aren't included by
default - add them yourself in `gateway/.secrets.yaml`:

```yaml
web:
  allowed_cidrs:
    - 10.0.0.0/8
    - 172.16.0.0/12
    - 192.168.0.0/16
    - 127.0.0.0/8
    - ::1/128
    - 100.64.0.0/10   # e.g. Tailscale
```

To override via environment variable instead, Dynaconf needs a list as
JSON: `GATEWAY_WEB__ALLOWED_CIDRS='@json ["10.0.0.0/8"]'`.

### Command topics (optional)

If `mqtt.host` is set, the bridge also subscribes to:

- `chlorinator/<name>/action` - `{"action": <int>[, ...kwargs]}`,
  matching pychlorinator's `ChlorinatorActions` enum.
- `chlorinator/<name>/setup` - kwargs for setpoint changes, e.g.
  `{"ph_control_setpoint": 7.4}`.

A message on either triggers a real BLE write, then an immediate
re-poll so the change shows up right away.

## Diagnostic scripts

`scripts/` holds standalone debugging helpers, not part of the deployed
app. Useful roughly in this order when setting up a new device:

- `scripts/ble_scan.py` - find nearby BLE devices that look like an eQ
  chlorinator.
- `scripts/gatt_probe.py` - confirm a real GATT connection works (no
  access code needed).
- `scripts/read_state.py` - full read handshake, prints current state
  once (access code required, no settings touched).

Run from the project root, with the venv from [Setup](#setup) activated:
`source venv/bin/activate && python3 scripts/read_state.py`

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

## License

MIT - see [LICENSE](LICENSE).
