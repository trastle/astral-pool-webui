#!/usr/bin/env python3
"""Read-only: connect, authenticate, and print the chlorinator's current state.

Uses pychlorinator's async_gatherdata() only - this performs the read-side
auth handshake (proving we know the access code) but does NOT call any
action/setup write methods, so no chlorinator setting or pump command is
touched.
"""
import asyncio

from bleak import BleakScanner
from pychlorinator.chlorinator import ChlorinatorAPI

from config import ACCESS_CODE, DEVICE_NAME


async def main():
    print(f"Looking for '{DEVICE_NAME}'...")
    device = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=15)
    if device is None:
        print("Not found - is it still advertising?")
        return

    print(f"Found {device.address}, authenticating and reading state...\n")
    api = ChlorinatorAPI(ble_device=device, access_code=ACCESS_CODE)
    data = await api.async_gatherdata()

    print("Returned data keys:", list(data.keys()))
    print()
    for key, value in data.items():
        print(f"--- {key} ---")
        print(value)
        print()


if __name__ == "__main__":
    asyncio.run(main())
