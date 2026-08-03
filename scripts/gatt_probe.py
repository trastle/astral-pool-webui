#!/usr/bin/env python3
"""Debugging helper - NOT part of the deployed gateway app.

Connect to the chlorinator and enumerate its GATT services/characteristics.
Use this after ble_scan.py finds your device, if you want to confirm a real
GATT connection works before involving the access code at all.

Doesn't need the Bluetooth access code - just confirms we can establish a
real GATT connection (not just see advertisements) and that the service/
characteristic UUIDs match what pychlorinator expects.

Run from the project root:
    source venv/bin/activate
    python3 scripts/gatt_probe.py
"""
import asyncio
import sys
from pathlib import Path

from bleak import BleakClient, BleakScanner

# gateway/ holds the actual app code (including config.py); add it to the
# path so this standalone script can share the same .env-backed config
# rather than duplicating that loading logic.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gateway"))
from config import DEVICE_NAME


async def main():
    print(f"Looking for '{DEVICE_NAME}'...")
    device = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=15)
    if device is None:
        print("Not found - is it still advertising?")
        return

    print(f"Found {device.address}, connecting...")
    async with BleakClient(device) as client:
        print(f"Connected: {client.is_connected}\n")
        for service in client.services:
            print(f"Service {service.uuid}")
            for char in service.characteristics:
                props = ",".join(char.properties)
                print(f"  Characteristic {char.uuid}  [{props}]")


if __name__ == "__main__":
    asyncio.run(main())
