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
  field, for your own dashboards/alerting. Also includes MQTT connection
  health (`chlorinator_mqtt_enabled`/`_connected`/`_disconnect_count`/
  `_last_connected_timestamp_seconds`) - useful for alerting if the bridge
  is enabled but not actually connected.
- **MQTT** - publishes full state as JSON on `chlorinator/<name>/state`
  every poll; optionally accepts commands on `chlorinator/<name>/action` and
  `chlorinator/<name>/setup` (see below) to actually control the device.
  Reconnects automatically (with backoff) after a network blip, including
  the very first connection attempt at startup, and re-subscribes to the
  action/setup topics on every reconnect so command handling doesn't
  silently stop working after a drop.
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
cp gateway/.secrets.yaml.example gateway/.secrets.yaml
# edit gateway/.secrets.yaml: set your real access_code
bash provision.sh
```

`provision.sh` sets up Bluetooth, creates a dedicated unprivileged
`chlorinator-gateway` system account, copies the project into that
account's home directory (e.g. `/home/chlorinator-gateway/astral-pool-webui`
- leaving your original checkout alone), and installs/starts it as a
systemd service running as that account. It's safe to re-run any time,
including after a `git pull`, to redeploy code changes - it copies the
update over and restarts the service.

Running it as its own account (rather than whichever user happens to run
the script) means a bug or compromised dependency in the BLE/MQTT/web
stack can't touch anything outside that account's own home directory. One
consequence: your regular login won't automatically be able to read its
logs or files (everything logs to the systemd journal - see `journalctl -u
chlorinator-gateway` - there's no separate log file). To grant another
admin account read-only access for troubleshooting, without giving it
broad sudo:

```bash
sudo usermod -aG systemd-journal <your-user>   # read logs without sudo
sudo usermod -aG chlorinator-gateway <your-user>
sudo chmod 750 /home/chlorinator-gateway        # let that group traverse/read
```

`gateway/.secrets.yaml` stays owner-only (`chmod 600`, no group bit) either
way, so this doesn't expose the access code or any MQTT credentials - only
code, non-secret config, and logs become readable. Group membership is
picked up on next login, not retroactively for an already-open session.

To try the app out directly first, without provisioning anything (this
venv is just for trying it out - `provision.sh` manages its own,
separately, under the service account's home directory):

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 gateway/app.py
# dashboard at http://<host>:8080/
```

(If you want to hand-install the systemd unit instead of using
`provision.sh`, see [`systemd/chlorinator-gateway.service`](systemd/chlorinator-gateway.service)
for a reference copy - adjust `User`/paths to match your setup first.)

To deploy from a separate dev machine over SSH instead of working directly
on the Pi:

```bash
./deploy.sh pi@your-pi-hostname ~/.ssh/your_key
```

### Configuration

Configuration is layered via [Dynaconf](https://www.dynaconf.com/), grouped
into `chlorinator` (the physical device/connection), `web` (the dashboard
server), and `mqtt` (the optional bridge):

1. [`gateway/settings.yaml`](gateway/settings.yaml) - committed, non-secret
   defaults.
2. `gateway/.secrets.yaml` - gitignored (copy it from
   [`gateway/.secrets.yaml.example`](gateway/.secrets.yaml.example)). Despite
   the name, this is really "any per-install override, not just secrets" -
   put your access code here, but also anything else specific to one
   install (e.g. `mqtt.host`) that you don't want to set via an environment
   variable. `provision.sh` copies it along with the rest of the project
   into the service account's home directory and preserves it across
   redeploys - a value set only in a hand-edited systemd unit or shell
   session will *not* survive the next `provision.sh` run, since that
   regenerates the unit from scratch.
3. Environment variables - override anything from either file above, e.g.
   for containerized/systemd deployments where you'd rather not keep a
   secrets file on disk at all.

Environment variables use a `GATEWAY_` prefix, with a double underscore
between the section and the key:

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

Leave `mqtt.host` unset to run without MQTT entirely - the app works
standalone (dashboard + metrics) with no broker configured at all.

### Restricting which networks can reach the dashboard

Every request (dashboard, `/help`, `/metrics`) is checked against
`web.allowed_cidrs` - anything from outside those networks gets `403
Forbidden`. This defaults to private/loopback ranges only (`10.0.0.0/8`,
`172.16.0.0/12`, `192.168.0.0/16`, `127.0.0.0/8`, `::1/128`), so
accidentally exposing this port to the internet (a misconfigured router, a
stray port-forward, UPnP, etc.) doesn't hand the dashboard to anyone who
finds it.

**This does not include VPN/mesh-network ranges** like Tailscale's
(`100.64.0.0/10` for IPv4, `fc00::/7` for its IPv6 range) even though
those aren't publicly routable either - if you access this over a VPN,
add its range yourself, e.g. in `gateway/.secrets.yaml`:

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

(List values can't be set with a plain environment variable string - if
you'd rather use `GATEWAY_WEB__ALLOWED_CIDRS`, Dynaconf needs it as JSON:
`GATEWAY_WEB__ALLOWED_CIDRS='@json ["10.0.0.0/8", "100.64.0.0/10"]'`.)

### Command topics (optional)

If `mqtt.host` is set, the bridge subscribes to:

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
