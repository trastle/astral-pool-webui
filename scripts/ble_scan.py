#!/usr/bin/env python3
"""Debugging helper - NOT part of the deployed gateway app.

Scan for nearby BLE devices and flag anything that looks like the EQ25.
Use this first when setting up a new device, to confirm it's advertising
and visible before troubleshooting anything else.

Run once the Pi is near the pool equipment, from the project root:
    source venv/bin/activate
    python3 scripts/ble_scan.py
"""
import asyncio

from bleak import BleakScanner

ASTRALPOOL_SERVICE_UUID = "45000001-98b7-4e29-a03f-160174643001"


async def main():
    print("Scanning for 15 seconds...\n")
    devices = await BleakScanner.discover(timeout=15, return_adv=True)

    if not devices:
        print("No BLE devices found at all - check Bluetooth is powered on.")
        return

    for address, (device, adv) in devices.items():
        is_chlorinator = ASTRALPOOL_SERVICE_UUID.lower() in [
            u.lower() for u in (adv.service_uuids or [])
        ]
        flag = "  <-- Astralpool chlorinator!" if is_chlorinator else ""
        name = device.name or "(no name)"
        print(f"{address}  RSSI={adv.rssi:>5}  {name}{flag}")


if __name__ == "__main__":
    asyncio.run(main())
