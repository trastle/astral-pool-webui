import asyncio
import re
from unittest.mock import MagicMock, patch

from prometheus_client import generate_latest

from app import metrics, registry, update_metrics


def get_metric_value(text: str, name: str) -> float:
    match = re.search(rf"^{re.escape(name)} (.+)$", text, re.MULTILINE)
    assert match, f"metric {name!r} not found in:\n{text}"
    return float(match.group(1))


def metrics_text(data: dict) -> str:
    update_metrics(data)
    return generate_latest(registry).decode()


def test_numeric_fields_and_setpoints(sample_data):
    text = metrics_text(sample_data)
    assert get_metric_value(text, "chlorinator_ph") == 8.8
    assert get_metric_value(text, "chlorinator_ph_setpoint") == 7.4
    assert get_metric_value(text, "chlorinator_chlorine_setpoint") == 315.0


def test_pool_volume_is_decoded_not_raw_bytes(sample_data):
    text = metrics_text(sample_data)
    assert get_metric_value(text, "chlorinator_pool_volume_litres") == 38000.0


def test_boolean_fields_become_zero_or_one(sample_data):
    text = metrics_text(sample_data)
    assert get_metric_value(text, "chlorinator_pump_operating") == 1.0
    assert get_metric_value(text, "chlorinator_cell_operating") == 0.0


def test_timedelta_fields_become_seconds(sample_data):
    text = metrics_text(sample_data)
    assert get_metric_value(text, "chlorinator_cell_running_time_seconds") == 238 * 86400
    assert get_metric_value(
        text, "chlorinator_low_salt_cell_running_time_seconds"
    ) == (33 * 86400 + 12 * 3600)


def test_categorical_state_exposed_as_info_labels(sample_data):
    text = metrics_text(sample_data)
    assert 'mode="Auto"' in text
    assert 'chlorine_control_status="Low"' in text
    assert 'acid_dosing_inhibit_status="InhibitedIndefinitely"' in text


def route_metrics_text(fake_bridge) -> str:
    """Unlike update_metrics()'s gauges, the MQTT gauges are set live in
    the /metrics route itself (not just once per poll) - see app.py."""
    with patch("app.mqtt_bridge", fake_bridge):
        response = asyncio.run(metrics())
    return response.body.decode()


def test_mqtt_metrics_reflect_disabled_bridge():
    fake_bridge = MagicMock(enabled=False, connected=False, disconnect_count=0, last_connected_at=None)
    text = route_metrics_text(fake_bridge)
    assert get_metric_value(text, "chlorinator_mqtt_enabled") == 0.0
    assert get_metric_value(text, "chlorinator_mqtt_connected") == 0.0
    assert get_metric_value(text, "chlorinator_mqtt_last_connected_timestamp_seconds") == 0.0


def test_mqtt_metrics_reflect_connected_bridge():
    fake_bridge = MagicMock(enabled=True, connected=True, disconnect_count=2, last_connected_at=1700000000.0)
    text = route_metrics_text(fake_bridge)
    assert get_metric_value(text, "chlorinator_mqtt_enabled") == 1.0
    assert get_metric_value(text, "chlorinator_mqtt_connected") == 1.0
    assert get_metric_value(text, "chlorinator_mqtt_disconnect_count") == 2.0
    assert get_metric_value(text, "chlorinator_mqtt_last_connected_timestamp_seconds") == 1700000000.0


def test_mqtt_metrics_reflect_enabled_but_currently_disconnected():
    """The gap this whole PR closes: enabled=1 + connected=0 is exactly
    the "should be up but isn't" state an alert would fire on."""
    fake_bridge = MagicMock(enabled=True, connected=False, disconnect_count=1, last_connected_at=1700000000.0)
    text = route_metrics_text(fake_bridge)
    assert get_metric_value(text, "chlorinator_mqtt_enabled") == 1.0
    assert get_metric_value(text, "chlorinator_mqtt_connected") == 0.0
