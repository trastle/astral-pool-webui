#!/usr/bin/env python3
"""Web UI + Prometheus exporter + MQTT bridge for the pool chlorinator.

Polls the EQ25 over BLE on a timer using pychlorinator's async_gatherdata()
and serves:
  GET /         human-readable HTML snapshot of the last reading
  GET /metrics  Prometheus text-exposition metrics

Also publishes state to MQTT, and executes real pychlorinator action/setup
writes against the device when commands arrive on chlorinator/<name>/action
or .../setup - see handle_mqtt_command() below.

Every HTTP route is gated by restrict_to_allowed_networks() below, which
403s any request whose source address isn't in config.ALLOWED_NETWORKS
(private/loopback ranges by default - see settings.yaml's web.allowed_cidrs).

The dashboard has no templating engine (no auto-escaping), so anywhere a
device-sourced value (not one of our own fixed strings/CSS classes) gets
embedded in the rendered HTML, it goes through html.escape() first -
defense-in-depth against a rogue/spoofed BLE device or a parser
regression, not because pychlorinator is expected to misbehave in normal
operation.

Run directly (venv activated) or via the systemd/chlorinator-gateway.service
unit.
"""
import asyncio
import contextlib
import html
import ipaddress
import logging
import re
import time
from contextlib import asynccontextmanager

# Configure logging before importing anything that might log at module
# import time (e.g. MqttBridge() below, constructed as a module-level
# singleton) - logging.basicConfig() used to only run inside main(), which
# is *after* the whole module (including that constructor) had already
# finished importing, so its startup log line was silently dropped by
# Python's default root logger level.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

import uvicorn
from bleak import BleakScanner
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest
from pychlorinator.chlorinator import ChlorinatorAPI
from pychlorinator.chlorinator_parsers import ChlorinatorActions

from config import ACCESS_CODE, ALLOWED_NETWORKS, DEVICE_NAME, HTTP_PORT, POLL_INTERVAL_SECONDS
from mqtt_bridge import MqttBridge
from quirks import decode_pool_volume

log = logging.getLogger("chlorinator_exporter")

# How long to wait after a write before reading state back, to give the
# device time to apply the change.
COMMAND_SETTLE_DELAY_SECONDS = 2

# Plain numeric fields from the pychlorinator state dict, copied straight
# into a Gauge of the same rough shape.
NUMERIC_FIELDS = {
    "chlorinator_ph": "ph_measurement",
    "chlorinator_ph_setpoint": "ph_control_setpoint",
    "chlorinator_chlorine_setpoint": "chlorine_control_setpoint",
    "chlorinator_spa_volume_litres": "spa_volume",
    "chlorinator_highest_ph_measured": "highest_ph_measured",
    "chlorinator_lowest_ph_measured": "lowest_ph_measured",
    "chlorinator_highest_orp_measured": "highest_orp_measured",
    "chlorinator_lowest_orp_measured": "lowest_orp_measured",
    "chlorinator_cell_reversal_count": "cell_reversal_count",
    "chlorinator_acid_dosing_inhibit_seconds_remaining": "acid_dosing_inhibit_time_remaining",
}

# Boolean fields, exposed as a 0/1 Gauge.
BOOL_FIELDS = {
    "chlorinator_pump_priming": "pump_is_priming",
    "chlorinator_pump_operating": "pump_is_operating",
    "chlorinator_cell_operating": "cell_is_operating",
    "chlorinator_chemistry_values_current": "chemistry_values_current",
    "chlorinator_chemistry_values_valid": "chemistry_values_valid",
    "chlorinator_spa_selected": "spa_selection",
    "chlorinator_ai_mode_enabled": "ai_mode_enabled",
    "chlorinator_threespeed_pump_enabled": "threespeed_pump_enabled",
    "chlorinator_dosing_capable": "dosing_capable_unit",
}

# timedelta fields, exposed in seconds.
TIMEDELTA_FIELDS = {
    "chlorinator_cell_running_time_seconds": "cell_running_time",
    "chlorinator_low_salt_cell_running_time_seconds": "low_salt_cell_running_time",
}

# Keys to leave out of the HTML dump (not useful / not simply printable).
HTML_SKIP_KEYS = {"pump_timers", "_reserved"}

registry = CollectorRegistry()
numeric_gauges = {
    name: Gauge(name, f"chlorinator field '{key}'", registry=registry)
    for name, key in NUMERIC_FIELDS.items()
}
bool_gauges = {
    name: Gauge(name, f"chlorinator field '{key}' (1=true, 0=false)", registry=registry)
    for name, key in BOOL_FIELDS.items()
}
timedelta_gauges = {
    name: Gauge(name, f"chlorinator field '{key}' in seconds", registry=registry)
    for name, key in TIMEDELTA_FIELDS.items()
}
g_pool_volume = Gauge(
    "chlorinator_pool_volume_litres",
    "Pool volume in litres (pychlorinator returns this undecoded; decoded here)",
    registry=registry,
)
g_info = Gauge(
    "chlorinator_info",
    "Chlorinator categorical/string state, exposed as labels (value always 1)",
    [
        "mode",
        "pump_speed",
        "chlorine_control_status",
        "info_message",
        "acid_dosing_inhibit_status",
        "ph_control_type",
        "chlorine_control_type",
        "volume_units",
    ],
    registry=registry,
)
g_scrape_success = Gauge(
    "chlorinator_scrape_success", "1 if the last poll succeeded, else 0", registry=registry
)
g_last_scrape_timestamp = Gauge(
    "chlorinator_last_scrape_timestamp_seconds",
    "Unix timestamp of the last poll attempt",
    registry=registry,
)
g_last_success_timestamp = Gauge(
    "chlorinator_last_success_timestamp_seconds",
    "Unix timestamp of the last successful poll",
    registry=registry,
)
g_mqtt_enabled = Gauge(
    "chlorinator_mqtt_enabled",
    "1 if MQTT is configured (mqtt.host set) at all, else 0",
    registry=registry,
)
g_mqtt_connected = Gauge(
    "chlorinator_mqtt_connected",
    "1 if currently connected to the MQTT broker, else 0 (only meaningful when enabled=1)",
    registry=registry,
)
g_mqtt_disconnect_count = Gauge(
    "chlorinator_mqtt_disconnect_count",
    "Number of MQTT disconnects (including our own on shutdown) since this process started",
    registry=registry,
)
g_mqtt_last_connected_timestamp = Gauge(
    "chlorinator_mqtt_last_connected_timestamp_seconds",
    "Unix timestamp MQTT last (re)connected, or 0 if never",
    registry=registry,
)

state_lock = asyncio.Lock()
# Shared between the periodic poll and MQTT command handling below, so a
# write and a poll can never open simultaneous BLE connections to the
# device - Bleak/BLE only reliably supports one connection at a time.
ble_lock = asyncio.Lock()
latest_state: dict = {"data": None, "error": None, "updated_at": None}
mqtt_bridge = MqttBridge()


def update_metrics(data: dict) -> None:
    for name, key in NUMERIC_FIELDS.items():
        numeric_gauges[name].set(float(data[key]))
    for name, key in BOOL_FIELDS.items():
        bool_gauges[name].set(1 if data[key] else 0)
    for name, key in TIMEDELTA_FIELDS.items():
        timedelta_gauges[name].set(data[key].total_seconds())
    g_pool_volume.set(decode_pool_volume(data["pool_volume"]))
    g_info.labels(
        mode=str(data["mode"]),
        pump_speed=str(data["pump_speed"]),
        chlorine_control_status=str(data["chlorine_control_status"]),
        info_message=str(data["info_message"]),
        acid_dosing_inhibit_status=str(data["acid_dosing_inhibit_status"]),
        ph_control_type=str(data["ph_control_type"]),
        chlorine_control_type=str(data["chlorine_control_type"]),
        volume_units=str(data["volume_units"]),
    ).set(1)


async def poll_once() -> dict:
    device = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=15)
    if device is None:
        raise RuntimeError(f"'{DEVICE_NAME}' not found in BLE scan")
    api = ChlorinatorAPI(ble_device=device, access_code=ACCESS_CODE)
    return await api.async_gatherdata()


async def refresh_now() -> None:
    """Poll once and update every downstream sink (metrics, MQTT state,
    dashboard cache). Shared by the periodic loop and by
    handle_mqtt_command() (to reflect a write immediately rather than
    waiting up to POLL_INTERVAL_SECONDS for it to show up)."""
    g_last_scrape_timestamp.set(time.time())
    try:
        async with ble_lock:
            data = await poll_once()
        update_metrics(data)
        mqtt_bridge.publish_state(data)
        async with state_lock:
            latest_state["data"] = data
            latest_state["error"] = None
            latest_state["updated_at"] = time.time()
        g_scrape_success.set(1)
        g_last_success_timestamp.set(time.time())
        log.info("Poll OK: mode=%s speed=%s ph=%s", data["mode"], data["pump_speed"], data["ph_measurement"])
    except Exception as exc:  # noqa: BLE001 - want to survive and retry
        log.warning("Poll failed: %s", exc)
        async with state_lock:
            latest_state["error"] = str(exc)
            latest_state["updated_at"] = time.time()
        g_scrape_success.set(0)


async def poll_loop() -> None:
    while True:
        await refresh_now()
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def handle_mqtt_command(kind: str, payload: dict) -> None:
    """Execute a real pychlorinator write against the device.

    kind is "action" (payload: {"action": <int>[, extra kwargs like
    period_minutes]}, matching pychlorinator's ChlorinatorActions enum) or
    "setup" (payload: kwargs for async_write_setup, e.g.
    {"ph_control_setpoint": 7.4}) - the exact shapes the
    astralpool_chlorinator fork's mqtt_client.py publishes.
    """
    async with ble_lock:
        try:
            device = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=15)
            if device is None:
                log.warning("MQTT %s command received but device not found - ignoring", kind)
                return
            api = ChlorinatorAPI(ble_device=device, access_code=ACCESS_CODE)
            if kind == "action":
                action = ChlorinatorActions(payload["action"])
                kwargs = {k: v for k, v in payload.items() if k != "action"}
                log.info("Executing MQTT action: %s %s", action, kwargs)
                await api.async_write_action(action, **kwargs)
            elif kind == "setup":
                log.info("Executing MQTT setup write: %s", payload)
                await api.async_write_setup(**payload)
            else:
                log.warning("Unknown MQTT command kind: %s", kind)
                return
        except Exception as exc:  # noqa: BLE001 - never let a bad command crash the app
            log.warning("MQTT %s command failed: %s", kind, exc)
            return
    # Give the device a moment to apply the change before reading it back,
    # then refresh outside the lock (refresh_now() takes it itself). A
    # module-level constant (not a bare asyncio.sleep(2) call) so tests can
    # zero it out without patching the real asyncio.sleep globally.
    await asyncio.sleep(COMMAND_SETTLE_DELAY_SECONDS)
    await refresh_now()


mqtt_bridge.set_command_handler(handle_mqtt_command)


PAGE_STYLE = """
:root {
  color-scheme: light dark;
  --ok: #1b7f3a; --ok-bg: #e6f6ea;
  --warn: #8a5a00; --warn-bg: #fff3d6;
  --alert: #b3251e; --alert-bg: #fde8e7;
  --neutral: #555; --neutral-bg: #eee;
  --card-bg: #fff; --page-bg: #f4f5f7; --border: #e0e2e6; --muted: #6b7280; --text: #1a1c1e;
}
@media (prefers-color-scheme: dark) {
  :root {
    --ok-bg: #123a20; --warn-bg: #3a2d05; --alert-bg: #3d1513; --neutral-bg: #2a2c30;
    --card-bg: #1e2024; --page-bg: #121316; --border: #33353a; --muted: #9aa0aa; --text: #e8e9eb;
    --ok: #4fd07a; --warn: #f0b429; --alert: #ff6b63; --neutral: #cfd2d8;
  }
}
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: var(--page-bg); color: var(--text); margin: 0; padding: 24px;
}
.wrap { max-width: 880px; margin: 0 auto; }
h1 { font-size: 1.4rem; margin: 0 0 4px; }
.subtitle { color: var(--muted); font-size: 0.9rem; margin: 0 0 20px; }
.banner { padding: 12px 16px; border-radius: 8px; margin-bottom: 20px; font-weight: 500; }
.banner.alert { background: var(--alert-bg); color: var(--alert); }
.banner.warn { background: var(--warn-bg); color: var(--warn); }
.cards {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px; margin-bottom: 20px;
}
.card {
  background: var(--card-bg); border: 1px solid var(--border); border-left: 3px solid var(--border);
  border-radius: 8px; padding: 12px 14px;
}
.card.ok { border-left-color: var(--ok); }
.card.warn { border-left-color: var(--warn); }
.card.alert { border-left-color: var(--alert); }
.card .label { font-size: 0.72rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
.card .value { font-size: 1.15rem; font-weight: 600; margin-top: 5px; line-height: 1.3; }
.card .hint { font-size: 0.78rem; color: var(--muted); margin-top: 3px; }
.badge {
  display: inline-block; padding: 1px 9px; border-radius: 999px; font-size: 0.8rem; font-weight: 600;
}
.badge.ok { background: var(--ok-bg); color: var(--ok); }
.badge.warn { background: var(--warn-bg); color: var(--warn); }
.badge.alert { background: var(--alert-bg); color: var(--alert); }
.badge.neutral { background: var(--neutral-bg); color: var(--neutral); }
.panel {
  background: var(--card-bg); border: 1px solid var(--border); border-radius: 10px;
  padding: 14px 18px 16px; margin-bottom: 16px;
}
.panel-header { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
h2 { font-size: 0.92rem; color: var(--muted); margin: 0; font-weight: 600; text-transform: uppercase; letter-spacing: 0.03em; }
details.panel { padding: 12px 18px; }
summary { cursor: pointer; color: var(--muted); font-size: 0.85rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.03em; }
details[open] summary { margin-bottom: 8px; }
table { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 0.85rem; }
th { padding: 4px 6px; text-align: left; border-bottom: 1px solid var(--border); font-size: 0.7rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.03em; font-weight: 600; }
td { padding: 5px 6px; border-bottom: 1px solid var(--border); }
tr:last-child td { border-bottom: none; }
td:first-child { color: var(--muted); white-space: nowrap; }
footer { color: var(--muted); font-size: 0.8rem; margin-top: 8px; text-align: center; }
footer a { color: inherit; }
.note { font-size: 0.78rem; color: var(--muted); margin: 10px 0 0; }
"""


def badge(text: str, level: str) -> str:
    return f"<span class='badge {level}'>{text}</span>"


def humanize(value) -> str:
    """'InhibitedIndefinitely' -> 'Inhibited Indefinitely', 'NoMessage' -> 'No Message'."""
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(value))
    return text.replace("Orp", "ORP")


def ph_level(ph: float) -> str:
    if 7.2 <= ph <= 7.8:
        return "ok"
    if 6.8 <= ph <= 8.2:
        return "warn"
    return "alert"


def chlorine_level(status: str) -> str:
    if status == "Ok":
        return "ok"
    if status in ("Low", "High"):
        return "warn"
    return "alert"


def mode_level(mode: str) -> str:
    return {"Auto": "ok", "ManualOn": "warn", "Off": "alert"}.get(mode, "neutral")


def dosing_level(status: str) -> str:
    return {"NotInhibited": "ok", "InhibitedForAPeriod": "warn"}.get(status, "alert")


def fmt_timedelta_days(td) -> str:
    days = td.days + td.seconds / 86400
    return f"{days:.0f} days"


def fmt_time_of_day(td) -> str:
    """timedelta representing seconds-since-midnight -> 'HH:MM'."""
    total_minutes = int(td.total_seconds() // 60)
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def panel(title: str, table_html: str, *, header_extra: str = "", note: str = "") -> str:
    header = f"<div class='panel-header'><h2>{title}</h2>{header_extra}</div>" if header_extra else f"<h2>{title}</h2>"
    note_html = f"<p class='note'>{note}</p>" if note else ""
    return f"<section class='panel'>{header}{table_html}{note_html}</section>"


def render_cards(data: dict) -> str:
    pump_hint = "priming" if data["pump_is_priming"] else ("running" if data["pump_is_operating"] else "stopped")
    dosing_status = str(data["acid_dosing_inhibit_status"])
    dosing_hint = None
    if dosing_status == "InhibitedForAPeriod" and data["acid_dosing_inhibit_time_remaining"]:
        dosing_hint = f"{data['acid_dosing_inhibit_time_remaining']}s remaining"

    cards = [
        ("Mode", html.escape(humanize(data["mode"])), mode_level(str(data["mode"])), None),
        ("Pump", html.escape(humanize(data["pump_speed"])), "neutral", pump_hint),
        ("pH", data["ph_measurement"], ph_level(float(data["ph_measurement"])), f"target {data['ph_control_setpoint']}"),
        ("Chlorine", html.escape(humanize(data["chlorine_control_status"])), chlorine_level(str(data["chlorine_control_status"])), f"target {data['chlorine_control_setpoint']}mV"),
        ("Salt cell", "Operating" if data["cell_is_operating"] else "Idle", "ok" if data["cell_is_operating"] else "neutral", f"{data['cell_reversal_count']} lifetime reversals"),
        ("Acid dosing", html.escape(humanize(dosing_status)), dosing_level(dosing_status), dosing_hint),
    ]
    card_html = "".join(
        f"<div class='card {level}'><div class='label'>{label}</div>"
        f"<div class='value'>{value}</div>"
        + (f"<div class='hint'>{hint}</div>" if hint else "")
        + "</div>"
        for label, value, level, hint in cards
    )
    return f"<div class='cards'>{card_html}</div>"


def render_chemistry(data: dict) -> str:
    rows = (
        f"<tr><td>pH control</td><td>{html.escape(humanize(data['ph_control_type']))} &middot; range seen {data['lowest_ph_measured']}–{data['highest_ph_measured']}</td></tr>"
        f"<tr><td>Chlorine control</td><td>{html.escape(humanize(data['chlorine_control_type']))} &middot; range seen {data['lowest_orp_measured']}–{data['highest_orp_measured']} mV</td></tr>"
    )
    note = (
        "The chlorinator only exposes a Low/OK/High-style status and a setpoint over "
        "BLE, not a live ORP millivolt reading &mdash; see the Chlorine card above for "
        "the current status."
    )
    return panel("Water chemistry", f"<table>{rows}</table>", note=note)


def render_schedule(pump_timers) -> str:
    rows = []
    total_hours = 0.0
    for i, timer in enumerate(pump_timers, start=1):
        duration_hours = (timer.stop_time - timer.start_time).total_seconds() / 3600
        if timer.enabled:
            total_hours += duration_hours
        state = badge("on", "ok") if timer.enabled else badge("off", "neutral")
        window = f"{fmt_time_of_day(timer.start_time)} – {fmt_time_of_day(timer.stop_time)}"
        rows.append(
            f"<tr><td>Timer {i}</td><td>{state}</td><td>{window}</td>"
            f"<td>{duration_hours:.1f}h</td><td>{html.escape(timer.speed_level.name)}</td></tr>"
        )
    table = (
        "<table><tr><th>Timer</th><th>State</th><th>Window</th><th>Duration</th><th>Speed</th></tr>"
        + "".join(rows)
        + "</table>"
    )
    header_extra = badge(f"{total_hours:.1f}h/day", "neutral")
    return panel("Pump schedule", table, header_extra=header_extra)


def render_pool_info(data: dict) -> str:
    rows = (
        f"<tr><td>Pool / spa volume</td><td>{decode_pool_volume(data['pool_volume'])} L / {data['spa_volume']} L</td></tr>"
        f"<tr><td>Cell size</td><td>{data['cell_size']} g/h</td></tr>"
        f"<tr><td>Cell running time</td><td>{fmt_timedelta_days(data['cell_running_time'])}</td></tr>"
        f"<tr><td>Low salt running time</td><td>{fmt_timedelta_days(data['low_salt_cell_running_time'])}</td></tr>"
    )
    return panel("Pool info", f"<table>{rows}</table>")


def render_raw_dump(data: dict) -> str:
    # Escaped generically (not just the fields known to be enum-derived
    # strings) since this dumps every field pychlorinator returns,
    # including ones that could change shape across versions.
    rows = "".join(
        f"<tr><td>{html.escape(str(k))}</td><td>{html.escape(str(v))}</td></tr>"
        for k, v in sorted(data.items())
        if k not in HTML_SKIP_KEYS
    )
    return f"<details class='panel'><summary>Raw field dump</summary><table>{rows}</table></details>"


def render_dashboard(data: dict | None, error: str | None, updated_at: float | None) -> str:
    age_seconds = (time.time() - updated_at) if updated_at else None
    age_text = f"{age_seconds:.0f}s ago" if age_seconds is not None else "never"
    stale = age_seconds is not None and age_seconds > POLL_INTERVAL_SECONDS * 2

    banner = ""
    if error:
        banner = f"<div class='banner alert'>Last poll failed ({age_text}): {html.escape(error)}</div>"
    elif stale:
        banner = f"<div class='banner warn'>No successful poll in {age_text} - data below may be out of date.</div>"
    elif data and data.get("info_message") not in (None, "NoMessage"):
        banner = f"<div class='banner warn'>Chlorinator message: {html.escape(humanize(data['info_message']))}</div>"

    if data is None:
        body = "<p>Waiting for first poll...</p>"
    else:
        body = (
            render_cards(data)
            + render_chemistry(data)
            + render_schedule(data["pump_timers"])
            + render_pool_info(data)
            + render_raw_dump(data)
        )

    return (
        "<!doctype html><html><head><title>Pool Chlorinator</title>"
        "<meta http-equiv='refresh' content='30'>"
        f"<style>{PAGE_STYLE}</style></head><body><div class='wrap'>"
        f"<h1>Pool Chlorinator</h1>"
        f"<p class='subtitle'>{html.escape(DEVICE_NAME)} &middot; last updated {age_text}</p>"
        f"{banner}{body}"
        f"<footer>Auto-refreshes every 30s &middot; <a href='/help'>Help</a> &middot; "
        f"<a href='/metrics'>Prometheus metrics</a></footer>"
        "</div></body></html>"
    )


# AstralPool's own resources - link out rather than reproducing their manual.
ASTRALPOOL_SUPPORT_URL = "https://www.astralpool.com.au/eq-support"
ASTRALPOOL_VIDEOS_URL = "https://www.astralpool.com.au/eqc-support"
ASTRALPOOL_MANUAL_URL = (
    "https://astralpools-au-2.s3.ap-southeast-2.amazonaws.com/Products/"
    "Eq_Chlorinator/H0717000_REVC%20eQ%20Manual.pdf"
)


def render_help() -> str:
    resources = panel(
        "Official AstralPool resources",
        "<table>"
        f"<tr><td>Product support page</td><td><a href='{ASTRALPOOL_SUPPORT_URL}' target='_blank' rel='noopener'>astralpool.com.au/eq-support</a></td></tr>"
        f"<tr><td>How-to videos (probe cleaning/calibration, installation, etc.)</td><td><a href='{ASTRALPOOL_VIDEOS_URL}' target='_blank' rel='noopener'>astralpool.com.au/eqc-support</a></td></tr>"
        f"<tr><td>Full installation &amp; operation manual</td><td><a href='{ASTRALPOOL_MANUAL_URL}' target='_blank' rel='noopener'>Download PDF (H0717000_REVC)</a></td></tr>"
        "</table>",
    )

    glossary_rows = "".join(
        f"<tr><td>{term}</td><td>{desc}</td></tr>"
        for term, desc in [
            ("Mode", "Whether the chlorinator is following its schedule (Auto), forced on/off by hand (Manual), or switched Off entirely."),
            ("Pump", "Current pump speed, and whether it's priming, running, or stopped."),
            ("pH", "Current pH reading versus the target you've configured."),
            ("Chlorine (ORP)", "Whether sanitiser level is tracking below, at, or above target. See the note below - this is a status, not a live millivolt reading."),
            ("Salt cell", "Whether the salt cell is actively producing chlorine right now, plus its lifetime polarity-reversal count (routine self-cleaning, not an error count)."),
            ("Acid dosing", "Whether automatic pH correction is currently active, paused for a set period, or switched off indefinitely."),
            ("Water chemistry", "Automatic vs. manual control mode, and the highest/lowest pH and ORP readings seen since the chlorinator's stats were last reset."),
            ("Pump schedule", "The chlorinator's own built-in daily timers - up to 4, each with an on/off state, time window, duration, and pump speed."),
        ]
    )
    glossary = panel("What the dashboard shows", f"<table>{glossary_rows}</table>")

    messages_rows = "".join(
        f"<tr><td>{code}</td><td>{desc}</td></tr>"
        for code, desc in [
            ("No Message", "Normal operation, nothing to report."),
            ("ORP Probe Clean Calibrate", "The unit thinks its ORP probe needs cleaning and recalibrating."),
        ]
    )
    fault_rows = "".join(
        f"<tr><td>{code}</td><td>{desc}</td></tr>"
        for code, desc in [
            ("1", "No communication with the pH/ORP probe."),
            ("3", "Probe detected but its reading can't be read."),
            ("17", "Probe readings are too inconsistent/variable to trust."),
            ("18", "Reading is above the expected range."),
            ("19", "Reading is below the expected range."),
        ]
    )
    messages = panel(
        "Status messages &amp; fault codes",
        f"<table>{messages_rows}</table>"
        f"<p style='margin:14px 0 4px'><strong>pH/ORP fault codes</strong> (shown as \"FAULT X\" on the unit's own screen):</p>"
        f"<table>{fault_rows}</table>",
        note=(
            "Paraphrased from AstralPool's manual for quick reference - see the full "
            "manual above for complete troubleshooting steps and maintenance procedures."
        ),
    )

    body = resources + glossary + messages

    return (
        "<!doctype html><html><head><title>Help &middot; Pool Chlorinator</title>"
        f"<style>{PAGE_STYLE}</style></head><body><div class='wrap'>"
        "<h1>Help</h1>"
        f"<p class='subtitle'>{html.escape(DEVICE_NAME)} &middot; <a href='/'>&larr; back to dashboard</a></p>"
        f"{body}"
        "<footer><a href='/'>&larr; Dashboard</a></footer>"
        "</div></body></html>"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    mqtt_bridge.connect()
    task = asyncio.create_task(poll_loop())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    mqtt_bridge.disconnect()


def _client_allowed(
    host: str | None,
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    """Is this client's source address within one of the configured
    allowed networks? Used to keep the dashboard/metrics off the public
    internet if this port ever gets accidentally exposed - see
    config.ALLOWED_NETWORKS / settings.yaml's web.allowed_cidrs."""
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    # Dual-stack listeners can hand back an IPv4 address wrapped as
    # ::ffff:a.b.c.d - unwrap it so it still matches a plain IPv4 network.
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr in network for network in networks)


app = FastAPI(lifespan=lifespan, title="Pool Chlorinator")


@app.middleware("http")
async def restrict_to_allowed_networks(request: Request, call_next):
    client_host = request.client.host if request.client else None
    if not _client_allowed(client_host, ALLOWED_NETWORKS):
        log.warning("Rejected request from disallowed address: %s", client_host)
        return PlainTextResponse("Forbidden", status_code=403)
    return await call_next(request)


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    async with state_lock:
        data = latest_state["data"]
        error = latest_state["error"]
        updated_at = latest_state["updated_at"]

    return render_dashboard(data, error, updated_at)


@app.get("/help", response_class=HTMLResponse)
async def help_page() -> str:
    return render_help()


@app.get("/metrics")
async def metrics() -> Response:
    # Read live off mqtt_bridge rather than only updating at poll time -
    # a connect/disconnect can happen at any point between polls, and this
    # is a cheap plain-attribute read, not I/O.
    g_mqtt_enabled.set(1 if mqtt_bridge.enabled else 0)
    g_mqtt_connected.set(1 if mqtt_bridge.connected else 0)
    g_mqtt_disconnect_count.set(mqtt_bridge.disconnect_count)
    g_mqtt_last_connected_timestamp.set(mqtt_bridge.last_connected_at or 0)
    return Response(content=generate_latest(registry), media_type=CONTENT_TYPE_LATEST)


def main() -> None:
    uvicorn.run(app, host="0.0.0.0", port=HTTP_PORT, log_level="info")


if __name__ == "__main__":
    main()
