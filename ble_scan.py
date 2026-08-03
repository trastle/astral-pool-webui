#!/usr/bin/env python3
"""Scan for nearby BLE devices and flag anything that looks like the EQ25.

Run once the Pi is near the pool equipment:
    source ~/gateway/venv/bin/activate
    python3 ~/gateway/ble_scan.py
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
