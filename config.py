"""Shared config for all gateway scripts, loaded from .env (see .env.example)."""
import os
from pathlib import Path

from dotenv import load_dotenv

# Load relative to this file, not the current working directory - otherwise
# this silently finds nothing (or the wrong .env) depending on where a
# script/test happens to be run from.
load_dotenv(Path(__file__).parent / ".env")

DEVICE_NAME = os.environ.get("CHLORINATOR_DEVICE_NAME", "POOL01")
ACCESS_CODE = os.environ.get("CHLORINATOR_ACCESS_CODE")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))

if not ACCESS_CODE:
    raise SystemExit(
        "CHLORINATOR_ACCESS_CODE is not set - copy .env.example to .env "
        "and fill it in."
    )

# MQTT is optional - unlike ACCESS_CODE, there's no SystemExit if unset.
# The app should keep working standalone (dashboard/metrics) with no broker
# configured; the MQTT bridge just stays disabled until MQTT_HOST is set.
MQTT_HOST = os.environ.get("MQTT_HOST")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USERNAME = os.environ.get("MQTT_USERNAME")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD")
# Defaults to the same "chlorinator/<name>" scheme used by
# github.com/hwmaier/chlorinator-gateway and its ESP32 successor, so this
# bridge is drop-in compatible with their topic layout and HA entities.
MQTT_BASE_TOPIC = os.environ.get("MQTT_BASE_TOPIC", f"chlorinator/{DEVICE_NAME.lower()}")
