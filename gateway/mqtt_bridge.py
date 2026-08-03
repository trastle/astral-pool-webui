"""Publishes chlorinator state to, and receives commands from, MQTT.

Doesn't publish its own Home Assistant MQTT Discovery config - pair this
with an MQTT-aware fork of astralpool_chlorinator (e.g.
https://github.com/trastle/astralpool_chlorinator, `mqtt` branch), which
provides a richer entity set (select/number/button, not just sensors)
reading from the same chlorinator/<name>/state topic this module
publishes to.

Subscribes to chlorinator/<name>/action ({"action": <int>[, extra
kwargs]}, matching pychlorinator's ChlorinatorActions enum) and
chlorinator/<name>/setup (kwargs for async_write_setup, e.g.
{"ph_control_setpoint": 7.4}) - the exact topics/payload shapes that fork's
mqtt_client.py publishes to. This module only does the MQTT transport; the
actual pychlorinator write calls happen in app.py's handle_mqtt_command(),
which this bridge invokes via a registered handler.

paho-mqtt's on_message/on_connect/on_disconnect callbacks all run on its
own background network-loop thread, not the asyncio event loop the rest
of the app runs on - commands are handed off via
asyncio.run_coroutine_threadsafe() using the loop captured when connect()
runs (from the FastAPI lifespan startup, so it's always the real running
loop). Updating self.connected/self.disconnect_count/self.last_connected_at
from that thread needs no extra locking, but not because those ops are
individually atomic under the GIL - self.disconnect_count += 1 is a
read-modify-write, which the GIL does NOT make atomic against a
concurrent writer. It's safe here because paho only ever runs one
background thread, so there's exactly one writer for these attributes,
ever; a second writer (e.g. handling reconnect logic on a different
thread) would need real locking, not this reasoning.

Connection resilience: connect() uses connect_async() + loop_start()
rather than a blocking connect() call, so paho's own background thread
handles retrying with backoff - including the *first* connection attempt
(retry_first_connection=True is hardcoded into paho's loop_start()) - not
just reconnects after a connection that was once established. Without
this, a transient network blip at startup would raise out of a bare
connect() call, and nothing would ever retry it.

Re-subscribing on every (re)connect, not just the first one: subscribe()
is only called from the on_connect callback, which paho invokes on every
successful CONNACK - including automatic reconnects. Subscribing once in
connect() instead would look fine initially, but a broker with clean
sessions (the default) forgets a client's subscriptions across a drop -
so after any reconnect, publish_state() would keep working (it doesn't
need a subscription) while action/setup commands silently stopped
arriving, with nothing in the logs to explain why.

If MQTT_HOST isn't configured, the bridge is inert (every method is a no-op)
so the rest of the app keeps working standalone with no broker present.
"""
import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

import paho.mqtt.client as mqtt

from config import MQTT_BASE_TOPIC, MQTT_HOST, MQTT_PASSWORD, MQTT_PORT, MQTT_USERNAME
from quirks import decode_pool_volume

log = logging.getLogger("mqtt_bridge")

# handler(kind: "action" | "setup", payload: dict) -> None (async)
CommandHandler = Callable[[str, dict], Awaitable[None]]


def build_state_payload(data: dict) -> dict:
    """Flatten/decode the pychlorinator state dict into a plain JSON-safe
    dict, matching what the astralpool_chlorinator fork's coordinator.py
    expects to reconstruct (see parse_mqtt_state() there). mode/pump_speed/
    chlorine_control_status/default_manual_on_speed are published as raw
    enum ints (they're IntEnum-like in pychlorinator, reconstructed via
    EnumClass(value) on the fork side); ph_control_type/chlorine_control_type/
    acid_dosing_inhibit_status are published by name (plain Enum, not
    IntEnum, reconstructed via EnumClass[name] instead)."""
    return {
        "mode": data["mode"].value,
        "pump_speed": data["pump_speed"].value,
        "active_timer": data["active_timer"],
        "info_message": str(data["info_message"]),
        "ph_measurement": data["ph_measurement"],
        "chlorine_control_status": data["chlorine_control_status"].value,
        "chemistry_values_current": bool(data["chemistry_values_current"]),
        "chemistry_values_valid": bool(data["chemistry_values_valid"]),
        "time_hours": data["time_hours"],
        "time_minutes": data["time_minutes"],
        "time_seconds": data["time_seconds"],
        "spa_selection": bool(data["spa_selection"]),
        "pump_is_priming": bool(data["pump_is_priming"]),
        "pump_is_operating": bool(data["pump_is_operating"]),
        "cell_is_operating": bool(data["cell_is_operating"]),
        "sanitising_until_next_timer_tomorrow": bool(data["sanitising_until_next_timer_tomorrow"]),
        "ph_control_setpoint": data["ph_control_setpoint"],
        "chlorine_control_setpoint": data["chlorine_control_setpoint"],
        "ph_control_type": str(data["ph_control_type"]),
        "chlorine_control_type": str(data["chlorine_control_type"]),
        "default_manual_on_speed": data["default_manual_on_speed"].value,
        "minimum_orp_setpoint": data["minimum_orp_setpoint"],
        "maximum_orp_setpoint": data["maximum_orp_setpoint"],
        "cell_reversal_count": data["cell_reversal_count"],
        "acid_dosing_inhibit_status": str(data["acid_dosing_inhibit_status"]),
        "acid_dosing_inhibit_time_remaining": data["acid_dosing_inhibit_time_remaining"],
        "pool_volume_litres": decode_pool_volume(data["pool_volume"]),
        "spa_volume_litres": data["spa_volume"],
        "cell_running_time_days": round(data["cell_running_time"].total_seconds() / 86400, 1),
        "low_salt_cell_running_time_days": round(data["low_salt_cell_running_time"].total_seconds() / 86400, 1),
        "previous_days_cell_load": data["previous_days_cell_load"],
        "highest_ph_measured": data["highest_ph_measured"],
        "lowest_ph_measured": data["lowest_ph_measured"],
        "highest_orp_measured": data["highest_orp_measured"],
        "lowest_orp_measured": data["lowest_orp_measured"],
    }


class MqttBridge:
    """Publishes chlorinator state on every poll, and delivers action/setup
    commands to a registered handler. No discovery config - the
    astralpool_chlorinator fork's own config flow handles device/entity
    registration in Home Assistant.

    Tolerant by design: a missing/unreachable broker never crashes the app;
    connection loss (initial or mid-session) is retried automatically by
    paho in the background, and is reflected in self.connected /
    self.disconnect_count / self.last_connected_at for app.py's /metrics
    to expose - see the module docstring for why.
    """

    def __init__(self) -> None:
        self.enabled = bool(MQTT_HOST)
        self.connected = False
        self.disconnect_count = 0
        self.last_connected_at: float | None = None
        self._client: mqtt.Client | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._command_handler: CommandHandler | None = None
        self._action_topic = f"{MQTT_BASE_TOPIC}/action"
        self._setup_topic = f"{MQTT_BASE_TOPIC}/setup"
        if not self.enabled:
            log.info("MQTT_HOST not set - MQTT bridge disabled")
            return

        self._client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        if MQTT_USERNAME:
            self._client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
        self._client.on_message = self._on_message
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect

    def set_command_handler(self, handler: CommandHandler) -> None:
        """Register the async function to call when an action/setup command
        arrives. Must be set before connect() to receive commands."""
        self._command_handler = handler

    def connect(self) -> None:
        if not self.enabled:
            return
        try:
            self._loop = asyncio.get_running_loop()
            self._client.connect_async(MQTT_HOST, MQTT_PORT)
            self._client.loop_start()
            log.info("Connecting to MQTT broker %s:%s in the background...", MQTT_HOST, MQTT_PORT)
        except Exception as exc:  # noqa: BLE001 - never let MQTT take the app down
            log.warning("MQTT connect failed (will keep running without it): %s", exc)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        """Runs on paho's background thread, on every successful CONNACK -
        the first connection AND every automatic reconnect after a drop.
        Subscribing here (not just once in connect()) is what makes
        command handling survive a reconnect - see the module docstring."""
        if reason_code.is_failure:
            log.warning("MQTT connect failed: %s", reason_code)
            return
        self.connected = True
        self.last_connected_at = time.time()
        client.subscribe([(self._action_topic, 0), (self._setup_topic, 0)])
        log.info("Connected to MQTT broker %s:%s, subscribed to %s and %s", MQTT_HOST, MQTT_PORT, self._action_topic, self._setup_topic)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None) -> None:
        """Runs on paho's background thread. Covers both an unexpected drop
        and our own disconnect() below - paho retries the former on its
        own; either way this keeps self.connected accurate for /metrics."""
        self.connected = False
        self.disconnect_count += 1
        log.warning("Disconnected from MQTT broker: %s", reason_code)

    def _on_message(self, client, userdata, msg) -> None:
        """Runs on paho's background network thread - hand off to the
        asyncio loop rather than doing any real work here."""
        if self._command_handler is None or self._loop is None:
            return
        try:
            payload = json.loads(msg.payload)
        except (json.JSONDecodeError, TypeError) as exc:
            log.warning("Bad command payload on %s: %r (%s)", msg.topic, msg.payload, exc)
            return
        kind = "action" if msg.topic.endswith("/action") else "setup"
        asyncio.run_coroutine_threadsafe(self._command_handler(kind, payload), self._loop)

    def publish_state(self, data: dict) -> None:
        if not self.enabled:
            return
        try:
            self._client.publish(f"{MQTT_BASE_TOPIC}/state", json.dumps(build_state_payload(data)))
        except Exception as exc:  # noqa: BLE001
            log.warning("MQTT publish failed: %s", exc)

    def disconnect(self) -> None:
        if not self.enabled or self._client is None:
            return
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception as exc:  # noqa: BLE001
            log.warning("MQTT disconnect failed: %s", exc)
