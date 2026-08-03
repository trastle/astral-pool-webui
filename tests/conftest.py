import os

# Set before anything imports config/app, so tests never depend on (or leak)
# a real gateway/settings.yaml or .secrets.yaml - Dynaconf env var
# overrides win over both, same as a real deployment.
os.environ.setdefault("GATEWAY_CHLORINATOR__ACCESS_CODE", "TESTCODE")
os.environ.setdefault("GATEWAY_CHLORINATOR__DEVICE_NAME", "TESTPOOL")
os.environ.setdefault("GATEWAY_CHLORINATOR__POLL_INTERVAL_SECONDS", "60")
os.environ.setdefault("GATEWAY_WEB__HTTP_PORT", "8080")

import datetime
from types import SimpleNamespace

import pytest


class FakeEnumValue(str):
    """Mimics pychlorinator's enum fields (e.g. ChlorinatorState.mode):
    behaves exactly like its name as a plain string (str(), equality,
    'in' checks - everything existing tests already rely on), while also
    carrying a numeric .value like a real enum member, for code that reads
    that (e.g. the MQTT bridge, which needs the int to match hwmaier's
    gateway's wire format)."""

    def __new__(cls, name, value):
        obj = super().__new__(cls, name)
        obj.value = value
        return obj


def make_timer(start_hms, stop_hms, enabled, speed_name):
    def to_td(hms):
        hours, minutes = hms
        return datetime.timedelta(hours=hours, minutes=minutes)

    return SimpleNamespace(
        start_time=to_td(start_hms),
        stop_time=to_td(stop_hms),
        enabled=enabled,
        speed_level=SimpleNamespace(name=speed_name),
    )


@pytest.fixture
def sample_data():
    """A realistic fake chlorinator state dict - field names/shapes mirror a
    real async_gatherdata() response captured from our actual EQ25."""
    return {
        "mode": FakeEnumValue("Auto", 2),  # 2 = Auto per hwmaier's gateway README
        "pump_speed": FakeEnumValue("Medium", 1),
        "active_timer": 1,
        "info_message": "NoMessage",
        "_reserved": 0,
        "flags": 35,
        "ph_measurement": 8.8,
        "chlorine_control_status": FakeEnumValue("Low", 0),
        "time_hours": 13,
        "time_minutes": 19,
        "time_seconds": 28,
        "chemistry_values_current": True,
        "chemistry_values_valid": True,
        "spa_selection": False,
        "pump_is_priming": False,
        "pump_is_operating": True,
        "cell_is_operating": False,
        "user_settings_has_changed": False,
        "sanitising_until_next_timer_tomorrow": False,
        "default_manual_on_speed": FakeEnumValue("High", 2),
        "ph_control_setpoint": 7.4,
        "chlorine_control_setpoint": 315,
        "is_no_timer_model": False,
        "is_timer_master_present_in_system": False,
        "minimum_manual_acid_setpoint": 0,
        "maximum_manual_acid_setpoint": 10,
        "minimum_manual_chlorine_setpoint": 0,
        "maximum_manual_chlorine_setpoint": 8,
        "minimum_ph_setpoint": 4.0,
        "maximum_ph_setpoint": 10.0,
        "minimum_orp_setpoint": 200,
        "maximum_orp_setpoint": 800,
        "ph_control_type": "Automatic",
        "chlorine_control_type": "Automatic",
        "cell_size": 25,
        "acid_pump_size": 5,
        "filter_pump_size": 0.1,
        "reversal_period": 4,
        "pool_volume": b"p\x94\x00",  # undecoded little-endian bytes -> 38000
        "spa_volume": 3000,
        "threespeed_pump_enabled": True,
        "ai_mode_enabled": True,
        "volume_units": "Litres",
        "lighting_enabled": False,
        "dosing_capable_unit": True,
        "pump_timers": [
            make_timer((10, 0), (14, 0), True, "AI"),
            make_timer((10, 35), (13, 0), False, "Medium"),
            make_timer((13, 5), (15, 30), False, "Medium"),
            make_timer((15, 35), (18, 10), False, "Medium"),
        ],
        "highest_ph_measured": 9.4,
        "lowest_ph_measured": 5.7,
        "highest_orp_measured": 893,
        "lowest_orp_measured": 100,
        "cell_reversal_count": 1365,
        "cell_running_time": datetime.timedelta(days=238),
        "low_salt_cell_running_time": datetime.timedelta(days=33, hours=12),
        "previous_days_cell_load": 0,
        "acid_dosing_inhibit_time_remaining": 0,
        "acid_dosing_inhibit_status": "InhibitedIndefinitely",
    }
