import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app import poll_once


def test_poll_once_raises_when_device_not_found():
    with patch("app.BleakScanner.find_device_by_name", new=AsyncMock(return_value=None)):
        with pytest.raises(RuntimeError, match="not found"):
            asyncio.run(poll_once())


def test_poll_once_returns_gathered_data_on_success():
    fake_device = object()
    fake_data = {"mode": "Auto"}
    with (
        patch("app.BleakScanner.find_device_by_name", new=AsyncMock(return_value=fake_device)),
        patch("app.ChlorinatorAPI.async_gatherdata", new=AsyncMock(return_value=fake_data)),
    ):
        result = asyncio.run(poll_once())
    assert result == fake_data
