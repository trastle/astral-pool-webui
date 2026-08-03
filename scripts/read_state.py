#!/usr/bin/env python3
"""Debugging helper - NOT part of the deployed gateway app.

Read-only: connect, authenticate, and print the chlorinator's current
state. Use this to confirm your access code is correct and pychlorinator
can fully parse your device's data, before running the full app.py service.

Uses pychlorinator's async_gatherdata() only - this performs the read-side
auth handshake (proving we know the access code) but does NOT call any
action/setup write methods, so no chlorinator setting or pump command is
touched.

Run from the project root:
    source venv/bin/activate
    python3 scripts/read_state.py
"""
import asyncio
import sys
from pathlib import Path

from bleak import BleakScanner
from pychlorinator.chlorinator import ChlorinatorAPI

# gateway/ holds the actual app code (including config.py); add it to the
# path so this standalone script can share the same .env-backed config
# rather than duplicating that loading logic.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gateway"))
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
