import asyncio
from unittest.mock import AsyncMock, call, patch

import pytest
from pychlorinator.chlorinator_parsers import ChlorinatorActions

from app import COMMAND_RETRY_ATTEMPTS, handle_mqtt_command


@pytest.fixture(autouse=True)
def no_sleep_no_refresh(monkeypatch):
    """handle_mqtt_command sleeps COMMAND_SETTLE_DELAY_SECONDS then calls
    refresh_now() after a successful write, and COMMAND_RETRY_DELAY_SECONDS
    between failed attempts - none of that is relevant to testing the write
    logic itself. Zeroing the named constants (rather than patching
    asyncio.sleep itself, which would patch the real stdlib function
    globally since app.asyncio is the same module object) keeps the real
    sleep/event-loop machinery untouched. Tests that specifically want to
    verify the retry delay override COMMAND_RETRY_DELAY_SECONDS back with
    their own monkeypatch call."""
    monkeypatch.setattr("app.COMMAND_SETTLE_DELAY_SECONDS", 0)
    monkeypatch.setattr("app.COMMAND_RETRY_DELAY_SECONDS", 0)
    with patch("app.refresh_now", new=AsyncMock()) as fake_refresh:
        yield fake_refresh


def test_action_command_calls_write_action_with_correct_enum():
    fake_device = object()
    with (
        patch("app.BleakScanner.find_device_by_name", new=AsyncMock(return_value=fake_device)),
        patch("app.ChlorinatorAPI.async_write_action", new=AsyncMock()) as fake_write,
    ):
        asyncio.run(handle_mqtt_command("action", {"action": 2}))

    fake_write.assert_called_once_with(ChlorinatorActions.Auto)


def test_action_command_passes_through_extra_kwargs():
    """e.g. {"action": 11, "period_minutes": 30} for DisableAcidDosingForPeriod."""
    fake_device = object()
    with (
        patch("app.BleakScanner.find_device_by_name", new=AsyncMock(return_value=fake_device)),
        patch("app.ChlorinatorAPI.async_write_action", new=AsyncMock()) as fake_write,
    ):
        asyncio.run(handle_mqtt_command("action", {"action": 11, "period_minutes": 30}))

    fake_write.assert_called_once_with(
        ChlorinatorActions.DisableAcidDosingForPeriod, period_minutes=30
    )


def test_setup_command_calls_write_setup_with_kwargs():
    fake_device = object()
    with (
        patch("app.BleakScanner.find_device_by_name", new=AsyncMock(return_value=fake_device)),
        patch("app.ChlorinatorAPI.async_write_setup", new=AsyncMock()) as fake_write,
    ):
        asyncio.run(handle_mqtt_command("setup", {"ph_control_setpoint": 7.4}))

    fake_write.assert_called_once_with(ph_control_setpoint=7.4)


def test_refresh_runs_after_a_successful_write(no_sleep_no_refresh):
    fake_device = object()
    with (
        patch("app.BleakScanner.find_device_by_name", new=AsyncMock(return_value=fake_device)),
        patch("app.ChlorinatorAPI.async_write_action", new=AsyncMock()),
    ):
        asyncio.run(handle_mqtt_command("action", {"action": 1}))

    no_sleep_no_refresh.assert_called_once()


def test_command_gives_up_after_retries_when_device_never_found(no_sleep_no_refresh):
    with (
        patch("app.BleakScanner.find_device_by_name", new=AsyncMock(return_value=None)) as fake_scan,
        patch("app.ChlorinatorAPI.async_write_action", new=AsyncMock()) as fake_write,
    ):
        asyncio.run(handle_mqtt_command("action", {"action": 2}))

    assert fake_scan.call_count == COMMAND_RETRY_ATTEMPTS
    fake_write.assert_not_called()
    no_sleep_no_refresh.assert_not_called()  # no refresh if the write never happened


def test_write_failure_is_retried_then_swallowed_and_skips_refresh(no_sleep_no_refresh):
    fake_device = object()
    with (
        patch("app.BleakScanner.find_device_by_name", new=AsyncMock(return_value=fake_device)),
        patch(
            "app.ChlorinatorAPI.async_write_action",
            new=AsyncMock(side_effect=RuntimeError("BLE error")),
        ) as fake_write,
    ):
        asyncio.run(handle_mqtt_command("action", {"action": 2}))  # must not raise

    assert fake_write.call_count == COMMAND_RETRY_ATTEMPTS
    no_sleep_no_refresh.assert_not_called()


def test_write_succeeds_on_a_later_retry_attempt(no_sleep_no_refresh):
    """The real-world case this was built for: the first attempt(s) fail
    with a BLE error, a later one succeeds - the command should still go
    through and refresh, not be treated as a final failure."""
    fake_device = object()
    with (
        patch("app.BleakScanner.find_device_by_name", new=AsyncMock(return_value=fake_device)),
        patch(
            "app.ChlorinatorAPI.async_write_action",
            new=AsyncMock(side_effect=[RuntimeError("BLE error"), None]),
        ) as fake_write,
    ):
        asyncio.run(handle_mqtt_command("action", {"action": 2}))

    assert fake_write.call_count == 2
    no_sleep_no_refresh.assert_called_once()


def test_retry_waits_between_failed_attempts(monkeypatch):
    """Restores the real delay (the autouse fixture zeroes it for every
    other test) to confirm the gap between attempts is what's configured,
    and that there's no trailing wait after the final attempt."""
    monkeypatch.setattr("app.COMMAND_RETRY_DELAY_SECONDS", 15)
    fake_device = object()
    with (
        patch("app.BleakScanner.find_device_by_name", new=AsyncMock(return_value=fake_device)),
        patch("app.ChlorinatorAPI.async_write_action", new=AsyncMock(side_effect=RuntimeError("BLE error"))),
        patch("app.asyncio.sleep", new=AsyncMock()) as fake_sleep,
    ):
        asyncio.run(handle_mqtt_command("action", {"action": 2}))

    assert fake_sleep.call_args_list == [call(15)] * (COMMAND_RETRY_ATTEMPTS - 1)


def test_unknown_action_value_is_swallowed(no_sleep_no_refresh):
    """999 isn't a valid ChlorinatorActions value - ChlorinatorActions(999)
    raises ValueError, which must be handled, not crash the app."""
    fake_device = object()
    with patch("app.BleakScanner.find_device_by_name", new=AsyncMock(return_value=fake_device)):
        asyncio.run(handle_mqtt_command("action", {"action": 999}))

    no_sleep_no_refresh.assert_not_called()


def test_unknown_command_kind_is_ignored(no_sleep_no_refresh):
    fake_device = object()
    with patch("app.BleakScanner.find_device_by_name", new=AsyncMock(return_value=fake_device)):
        asyncio.run(handle_mqtt_command("bogus", {}))

    no_sleep_no_refresh.assert_not_called()
