"""Shared config for all gateway scripts.

Loaded via Dynaconf from settings.yaml (committed, non-secret defaults)
merged with .secrets.yaml (gitignored - see .secrets.yaml.example), both
resolved relative to this file rather than the current working directory -
otherwise this silently finds nothing (or the wrong files) depending on
where a script/test happens to be run from.

Every value can be overridden with a GATEWAY_-prefixed environment
variable, e.g. GATEWAY_CHLORINATOR__ACCESS_CODE, GATEWAY_WEB__HTTP_PORT,
GATEWAY_MQTT__HOST (double underscore for nesting - settings.yaml groups
keys under chlorinator/web/mqtt).
"""
import ipaddress
from pathlib import Path

from dynaconf import Dynaconf

settings = Dynaconf(
    envvar_prefix="GATEWAY",
    settings_files=["settings.yaml", ".secrets.yaml"],
    root_path=Path(__file__).parent,
)


def parse_allowed_networks(cidrs) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Turn the configured web.allowed_cidrs list into ipaddress network
    objects, failing loudly on a typo rather than silently matching
    nothing (or, worse, matching more than intended)."""
    try:
        return [ipaddress.ip_network(cidr, strict=False) for cidr in cidrs]
    except ValueError as exc:
        raise SystemExit(f"web.allowed_cidrs contains an invalid entry: {exc}") from exc


def _str_or_none(value) -> str | None:
    """Dynaconf auto-casts numeric-looking YAML/env values to int/float -
    force back to str for fields pychlorinator/paho-mqtt need as text (a
    purely-numeric access code would otherwise become an int, and
    `bytes(int, "utf_8")` blows up)."""
    return None if value is None else str(value)


DEVICE_NAME = _str_or_none(settings.get("chlorinator.device_name", "POOL01"))
ACCESS_CODE = _str_or_none(settings.get("chlorinator.access_code"))
POLL_INTERVAL_SECONDS = int(settings.get("chlorinator.poll_interval_seconds", 60))
HTTP_PORT = int(settings.get("web.http_port", 8080))
ALLOWED_NETWORKS = parse_allowed_networks(
    settings.get(
        "web.allowed_cidrs",
        ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128"],
    )
)

if not ACCESS_CODE:
    raise SystemExit(
        "chlorinator.access_code is not set - copy gateway/.secrets.yaml.example to "
        "gateway/.secrets.yaml and fill it in, or set GATEWAY_CHLORINATOR__ACCESS_CODE."
    )

# MQTT is optional - unlike ACCESS_CODE, there's no SystemExit if unset.
# The app should keep working standalone (dashboard/metrics) with no broker
# configured; the MQTT bridge just stays disabled until MQTT_HOST is set.
MQTT_HOST = _str_or_none(settings.get("mqtt.host"))
MQTT_PORT = int(settings.get("mqtt.port", 1883))
MQTT_USERNAME = _str_or_none(settings.get("mqtt.username"))
MQTT_PASSWORD = _str_or_none(settings.get("mqtt.password"))
# Defaults to chlorinator/<device name, lowercased>.
MQTT_BASE_TOPIC = _str_or_none(settings.get("mqtt.base_topic")) or f"chlorinator/{DEVICE_NAME.lower()}"
