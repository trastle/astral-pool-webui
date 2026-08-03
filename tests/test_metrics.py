import re

from prometheus_client import generate_latest

from app import registry, update_metrics


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
