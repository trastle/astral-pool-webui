#!/usr/bin/env python3
"""Connect to the chlorinator and enumerate its GATT services/characteristics.

Doesn't need the Bluetooth access code - just confirms we can establish a
real GATT connection (not just see advertisements) and that the service/
characteristic UUIDs match what pychlorinator expects.
"""
import asyncio

from bleak import BleakClient, BleakScanner

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
