#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

import websockets

# Allow running directly from examples/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from csms.defaults import sample_from_schema  # noqa: E402
from csms.schema_registry import SchemaRegistry  # noqa: E402


SUBPROTOCOL = {"1.6": "ocpp1.6", "2.0.1": "ocpp2.0.1", "2.1": "ocpp2.1"}


def boot_payload(version: str) -> dict:
    if version == "1.6":
        return {"chargePointVendor": "OCPP-Lab", "chargePointModel": "Simulator", "firmwareVersion": "0.1"}
    return {
        "reason": "PowerUp",
        "chargingStation": {"vendorName": "OCPP-Lab", "model": "Simulator", "firmwareVersion": "0.1"},
    }


def status_payload(version: str) -> dict:
    if version == "1.6":
        return {"connectorId": 1, "errorCode": "NoError", "status": "Available"}
    return {"timestamp": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat().replace("+00:00", "Z"), "connectorStatus": "Available", "evseId": 1, "connectorId": 1}


async def call(ws, action: str, payload: dict) -> dict:
    uid = uuid.uuid4().hex
    await ws.send(json.dumps([2, uid, action, payload], separators=(",", ":")))
    while True:
        frame = json.loads(await ws.recv())
        if frame[0] == 2:
            # Main receive loop handles these after boot; during startup answer empty.
            await ws.send(json.dumps([3, frame[1], {}]))
            continue
        if frame[1] != uid:
            continue
        if frame[0] == 3:
            return frame[2]
        raise RuntimeError(f"CALLERROR: {frame}")


async def run(args) -> None:
    schemas = SchemaRegistry(Path(__file__).resolve().parents[1] / "schemas")
    uri = f"{args.url.rstrip('/')}/{args.identity}"
    async with websockets.connect(uri, subprotocols=[SUBPROTOCOL[args.version]], ping_interval=20) as ws:
        print(f"Connected {args.identity} with OCPP {args.version} -> {uri}")
        print("BootNotification =>", await call(ws, "BootNotification", boot_payload(args.version)))
        print("StatusNotification =>", await call(ws, "StatusNotification", status_payload(args.version)))
        print("Ready. Send commands from the dashboard.")
        async for raw in ws:
            frame = json.loads(raw)
            print("RX", json.dumps(frame, indent=2))
            if not isinstance(frame, list) or frame[0] != 2:
                continue
            _, uid, action, payload = frame
            try:
                schemas.validate(args.version, action, payload)
                response_schema = schemas.get(args.version, action, response=True)
                response = sample_from_schema(response_schema)
                # Common command semantics for a useful simulator.
                if "status" in response and isinstance(response["status"], str):
                    enums = response_schema.get("properties", {}).get("status", {}).get("enum", [])
                    if "Accepted" in enums:
                        response["status"] = "Accepted"
                schemas.validate(args.version, action, response, response=True)
                await ws.send(json.dumps([3, uid, response], separators=(",", ":")))
            except Exception as exc:
                await ws.send(json.dumps([4, uid, "InternalError", str(exc), {}], separators=(",", ":")))


def main() -> None:
    p = argparse.ArgumentParser(description="Small OCPP charger simulator for OCPP Test CSMS")
    p.add_argument("--url", default="ws://localhost:9000/ocpp")
    p.add_argument("--identity", default="TEST-001")
    p.add_argument("--version", choices=SUBPROTOCOL, default="1.6")
    args = p.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
