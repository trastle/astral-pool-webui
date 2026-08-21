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
import os
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable

import paho.mqtt.client as mqtt
from pychlorinator.chlorinator_parsers import ChlorineControlStatuses

from config import (
    LAST_KNOWN_GOOD_CACHE_FILE,
    MQTT_BASE_TOPIC,
    MQTT_HOST,
    MQTT_PASSWORD,
    MQTT_PORT,
    MQTT_USERNAME,
)
from quirks import decode_pool_volume, format_time_of_day

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
        # Fixed 4 timer slots (NUMBER_OF_PUMP_TIMERS_SUPPORTED in
        # pychlorinator), read-only for now - speed_level follows the same
        # raw-int convention as pump_speed/default_manual_on_speed above
        # (same SpeedLevels enum), reconstructed via SpeedLevels(value) on
        # the fork side.
        "pump_timers": [
            {
                "start_time": format_time_of_day(timer.start_time),
                "stop_time": format_time_of_day(timer.stop_time),
                "enabled": bool(timer.enabled),
                "speed_level": timer.speed_level.value,
            }
            for timer in data["pump_timers"]
        ],
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

    Also holds the last known-good pH/ORP reading, republished in place of
    the device's own values while it flags chemistry_values_valid=False -
    see publish_state() for why. Persisted to disk (LAST_KNOWN_GOOD_CACHE_FILE,
    configurable via settings.yaml/config.py) on every change, not just
    kept in memory, so a gateway restart landing while chemistry is
    already invalid still has a real value to fall back to, rather than
    passing the device's raw garbage reading straight through with nothing
    to compare it against.
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
        # Last ph_measurement/chlorine_control_status published while the
        # device itself reported chemistry_values_valid=True - see
        # publish_state() for why these get substituted back in. Restored
        # from disk below so this survives a restart, not just held in
        # memory for this process's lifetime.
        self._last_valid_ph_measurement: float | None = None
        self._last_valid_chlorine_control_status: int | None = None
        self._load_cache_from_disk()
        if not self.enabled:
            log.info("MQTT_HOST not set - MQTT bridge disabled")
            return

        self._client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        if MQTT_USERNAME:
            self._client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
        self._client.on_message = self._on_message
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect

    def _load_cache_from_disk(self) -> None:
        """Restores the last known-good pH/chlorine reading at startup, if
        a cache file from a previous run exists. Tolerant of a
        missing/corrupt file (fresh install, first-ever run, or a write
        that got interrupted before the atomic rename in
        _save_cache_to_disk) - just falls back to "nothing cached yet",
        same as before this existed, rather than crashing the app over a
        cache that only ever exists to make things more reliable."""
        try:
            cached = json.loads(LAST_KNOWN_GOOD_CACHE_FILE.read_text())
            ph = cached["ph_measurement"]
            chlorine_status = cached["chlorine_control_status"]
            # Validate before trusting it - resolve_chemistry_reading()
            # reconstructs a real ChlorineControlStatuses member from this
            # int on every invalid-chemistry poll; if a stale/hand-edited/
            # version-mismatched cache file held an int that isn't a real
            # member, that reconstruction would raise deep inside a poll
            # cycle instead of here, at load time, where a bad cache is
            # already expected and handled the same as any other corrupt
            # file - "nothing cached yet" - rather than a real fault.
            ChlorineControlStatuses(chlorine_status)
            self._last_valid_ph_measurement = ph
            self._last_valid_chlorine_control_status = chlorine_status
            log.info(
                "Restored last known-good reading from %s (last changed %s)",
                LAST_KNOWN_GOOD_CACHE_FILE, cached.get("last_changed"),
            )
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError) as exc:
            log.warning("Ignoring unreadable last known-good cache at %s: %s", LAST_KNOWN_GOOD_CACHE_FILE, exc)

    def _save_cache_to_disk(self) -> None:
        """Writes the current last known-good reading to disk (temp file +
        os.replace(), so an interrupted write - e.g. a crash or power loss
        mid-write - never leaves a corrupt cache file behind for the next
        _load_cache_from_disk() to trip over). Only called from
        publish_state() when the cached value actually changes, not on
        every poll, since it exists specifically to survive events like
        the process being killed, not to track every intermediate value."""
        payload = {
            "ph_measurement": self._last_valid_ph_measurement,
            "chlorine_control_status": self._last_valid_chlorine_control_status,
            "last_changed": datetime.now(timezone.utc).isoformat(),
        }
        tmp_path = LAST_KNOWN_GOOD_CACHE_FILE.with_suffix(".tmp")
        try:
            tmp_path.write_text(json.dumps(payload))
            os.replace(tmp_path, LAST_KNOWN_GOOD_CACHE_FILE)
        except OSError as exc:
            log.warning("Failed to persist last known-good cache to %s: %s", LAST_KNOWN_GOOD_CACHE_FILE, exc)

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

    def resolve_chemistry_reading(self, data: dict) -> tuple[float, ChlorineControlStatuses]:
        """Same hold-last-known-good decision publish_state() applies to
        the MQTT payload, exposed for other consumers of the raw poll dict
        - namely app.py's own dashboard and /metrics, which otherwise read
        data["ph_measurement"]/["chlorine_control_status"] directly and
        show the device's raw garbage reading during an invalid window
        even though MQTT/Home Assistant no longer do.

        Returns real pychlorinator types (a ChlorineControlStatuses member,
        reconstructed from the cached int), not the flattened forms
        publish_state() builds for MQTT, so callers that pass this
        straight back into data (as app.py does) keep working with
        whatever they already expect from a fresh poll - str(status) still
        gives the plain name, etc."""
        if data["chemistry_values_valid"] or self._last_valid_ph_measurement is None:
            return data["ph_measurement"], data["chlorine_control_status"]
        return self._last_valid_ph_measurement, ChlorineControlStatuses(self._last_valid_chlorine_control_status)

    def publish_state(self, data: dict) -> None:
        # The cache tracking/persistence below must run regardless of
        # self.enabled - resolve_chemistry_reading() (used by app.py's web
        # dashboard/metrics, independent of MQTT) depends on the same
        # self._last_valid_* state this method maintains. Bailing out here
        # early (as this used to) left standalone/no-MQTT deployments -
        # a documented, supported mode, see the module docstring - with a
        # cache that never advances past whatever _load_cache_from_disk()
        # restored at startup, silently defeating the whole feature for
        # that mode. Only the actual MQTT publish is gated on self.enabled.
        #
        # Building the payload and updating the cache both need their own
        # try/except (distinct from the publish try/except below) - before
        # the hold-last-known-good feature existed, build_state_payload()
        # ran inside the same try as the publish call, so a malformed/
        # unexpected `data` shape (e.g. a pychlorinator field's type
        # changing) was caught and logged right here. Leaving this
        # uncaught would instead propagate into app.py's refresh_now(),
        # turning an isolated "couldn't build/publish state" warning into
        # a full "Poll failed" that also skips update_metrics() and marks
        # the whole poll as failed, even though the BLE read itself
        # succeeded.
        try:
            payload = build_state_payload(data)
            if payload["chemistry_values_valid"]:
                changed = (
                    payload["ph_measurement"] != self._last_valid_ph_measurement
                    or payload["chlorine_control_status"] != self._last_valid_chlorine_control_status
                )
                self._last_valid_ph_measurement = payload["ph_measurement"]
                self._last_valid_chlorine_control_status = payload["chlorine_control_status"]
                if changed:
                    self._save_cache_to_disk()
            elif self._last_valid_ph_measurement is not None:
                # The device flags its own pH/ORP readings as not-yet-trustworthy
                # via chemistry_values_valid (seen for ~8h after an overnight
                # power cycle, while the pump ran a priming cycle) but keeps
                # returning a raw reading regardless - observed: a flat,
                # physically-impossible pH of 0.0 for that whole window, which
                # got faithfully recorded as real history downstream. Hold the
                # last known-good values instead of relaying junk. Everything
                # else in the payload - including chemistry_values_valid/current
                # themselves - still publishes live every poll, so the "not
                # valid yet" state remains visible downstream, just without
                # clobbering the readings with garbage. If we've never seen a
                # valid reading at all (fresh start), there's nothing better to
                # substitute, so the raw value passes through as-is.
                payload["ph_measurement"] = self._last_valid_ph_measurement
                payload["chlorine_control_status"] = self._last_valid_chlorine_control_status
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to build chlorinator state payload: %s", exc)
            return

        if not self.enabled:
            return
        try:
            self._client.publish(f"{MQTT_BASE_TOPIC}/state", json.dumps(payload))
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
