"""Shared config for all gateway scripts.

Loaded via Dynaconf from settings.yaml (committed, non-secret defaults)
merged with .secrets.yaml (gitignored - see .secrets.yaml.example), both
resolved relative to this file rather than the current working directory -
otherwise this silently finds nothing (or the wrong files) depending on
where a script/test happens to be run from.

Every value can be overridden with a CHLORINATOR_-prefixed environment
variable, e.g. CHLORINATOR_ACCESS_CODE, CHLORINATOR_HTTP_PORT, or (for the
nested mqtt.* keys) CHLORINATOR_MQTT__HOST.
"""
from pathlib import Path

from dynaconf import Dynaconf

settings = Dynaconf(
    envvar_prefix="CHLORINATOR",
    settings_files=["settings.yaml", ".secrets.yaml"],
    root_path=Path(__file__).parent,
)


def _str_or_none(value) -> str | None:
    """Dynaconf auto-casts numeric-looking YAML/env values to int/float -
    force back to str for fields pychlorinator/paho-mqtt need as text (a
    purely-numeric access code would otherwise become an int, and
    `bytes(int, "utf_8")` blows up)."""
    return None if value is None else str(value)


DEVICE_NAME = _str_or_none(settings.get("device_name", "POOL01"))
ACCESS_CODE = _str_or_none(settings.get("access_code"))
POLL_INTERVAL_SECONDS = int(settings.get("poll_interval_seconds", 60))
HTTP_PORT = int(settings.get("http_port", 8080))

if not ACCESS_CODE:
    raise SystemExit(
        "access_code is not set - copy gateway/.secrets.yaml.example to "
        "gateway/.secrets.yaml and fill it in, or set CHLORINATOR_ACCESS_CODE."
    )

# MQTT is optional - unlike ACCESS_CODE, there's no SystemExit if unset.
# The app should keep working standalone (dashboard/metrics) with no broker
# configured; the MQTT bridge just stays disabled until MQTT_HOST is set.
MQTT_HOST = _str_or_none(settings.get("mqtt.host"))
MQTT_PORT = int(settings.get("mqtt.port", 1883))
MQTT_USERNAME = _str_or_none(settings.get("mqtt.username"))
MQTT_PASSWORD = _str_or_none(settings.get("mqtt.password"))
# Defaults to the same "chlorinator/<name>" scheme used by
# github.com/hwmaier/chlorinator-gateway and its ESP32 successor, so this
# bridge is drop-in compatible with their topic layout and HA entities.
MQTT_BASE_TOPIC = _str_or_none(settings.get("mqtt.base_topic")) or f"chlorinator/{DEVICE_NAME.lower()}"
