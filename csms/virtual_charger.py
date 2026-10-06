from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from aiohttp import ClientSession, ClientWebSocketResponse, WSMsgType

from .config import Config
from .defaults import sample_from_schema, utc_now
from .schema_registry import SchemaRegistry

CALL = 2
CALLRESULT = 3
CALLERROR = 4
SUBPROTOCOL = {"1.6": "ocpp1.6", "2.0.1": "ocpp2.0.1", "2.1": "ocpp2.1"}


@dataclass
class VirtualCharger:
    identity: str
    version: str
    ws: ClientWebSocketResponse
    session: ClientSession
    task: asyncio.Task | None = None
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    config16: dict[str, dict[str, Any]] = field(default_factory=dict)
    variables: dict[str, str] = field(default_factory=dict)


class VirtualChargerManager:
    """Small charger emulator controlled from the web UI.

    It exists only for local/lab testing. It connects to the same CSMS over OCPP,
    so dashboard tests exercise the actual websocket path rather than bypassing it.
    """

    def __init__(self, config: Config, schemas: SchemaRegistry):
        self.config = config
        self.schemas = schemas
        self.chargers: dict[str, VirtualCharger] = {}

    def list(self) -> list[dict[str, Any]]:
        return [
            {"identity": c.identity, "version": c.version, "connected": not c.ws.closed}
            for c in sorted(self.chargers.values(), key=lambda x: x.identity)
        ]

    async def start(self, identity: str, version: str) -> dict[str, Any]:
        identity = identity.strip()
        if not identity or len(identity) > 100:
            raise ValueError("Invalid virtual charger identity")
        if version not in SUBPROTOCOL:
            raise ValueError("Unsupported OCPP version")
        if identity in self.chargers:
            await self.stop(identity)

        client = ClientSession()
        url = f"ws://127.0.0.1:{self.config.port}/ocpp/{identity}"
        try:
            ws = await client.ws_connect(url, protocols=[SUBPROTOCOL[version]], heartbeat=20)
        except Exception:
            await client.close()
            raise

        charger = VirtualCharger(
            identity=identity,
            version=version,
            ws=ws,
            session=client,
            config16=self._default_config_16(),
            variables=self._default_variables(),
        )
        self.chargers[identity] = charger
        charger.task = asyncio.create_task(self._receive_loop(charger), name=f"virtual-charger-{identity}")

        try:
            boot = await self.send_call(identity, "BootNotification", self._boot_payload(version), timeout=5)
            await self.send_call(identity, "StatusNotification", self._status_payload(version), timeout=5)
        except Exception:
            await self.stop(identity)
            raise
        return {"identity": identity, "version": version, "boot": boot}

    async def stop(self, identity: str) -> bool:
        charger = self.chargers.pop(identity, None)
        if not charger:
            return False
        if not charger.ws.closed:
            await charger.ws.close()
        if charger.task and not charger.task.done():
            charger.task.cancel()
            try:
                await charger.task
            except asyncio.CancelledError:
                pass
        for fut in charger.pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("Virtual charger stopped"))
        await charger.session.close()
        return True

    async def close(self) -> None:
        for identity in list(self.chargers):
            await self.stop(identity)

    async def send_call(self, identity: str, action: str, payload: dict[str, Any], timeout: int = 10) -> dict[str, Any]:
        charger = self.chargers.get(identity)
        if not charger or charger.ws.closed:
            raise LookupError("Virtual charger is not running")
        self.schemas.validate(charger.version, action, payload)
        uid = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        charger.pending[uid] = future
        await charger.ws.send_str(json.dumps([CALL, uid, action, payload], separators=(",", ":")))
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            charger.pending.pop(uid, None)
            raise TimeoutError(f"No CSMS response to {action} within {timeout}s")
        return result

    async def _receive_loop(self, charger: VirtualCharger) -> None:
        try:
            async for msg in charger.ws:
                if msg.type != WSMsgType.TEXT:
                    if msg.type in {WSMsgType.ERROR, WSMsgType.CLOSED, WSMsgType.CLOSE}:
                        break
                    continue
                try:
                    frame = json.loads(msg.data)
                    if not isinstance(frame, list) or not frame:
                        continue
                    if frame[0] == CALL:
                        await self._handle_csms_call(charger, frame)
                    elif frame[0] == CALLRESULT and len(frame) == 3:
                        fut = charger.pending.pop(str(frame[1]), None)
                        if fut and not fut.done():
                            fut.set_result({"type": "CALLRESULT", "uniqueId": frame[1], "payload": frame[2]})
                    elif frame[0] == CALLERROR and len(frame) == 5:
                        fut = charger.pending.pop(str(frame[1]), None)
                        if fut and not fut.done():
                            fut.set_result({
                                "type": "CALLERROR",
                                "uniqueId": frame[1],
                                "error": {"code": frame[2], "description": frame[3], "details": frame[4]},
                            })
                except Exception:
                    continue
        finally:
            if self.chargers.get(charger.identity) is charger:
                self.chargers.pop(charger.identity, None)
            for fut in charger.pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("Virtual charger disconnected"))
            if not charger.session.closed:
                await charger.session.close()

    async def _handle_csms_call(self, charger: VirtualCharger, frame: list[Any]) -> None:
        if len(frame) != 4:
            return
        _, uid, action, payload = frame
        try:
            self.schemas.validate(charger.version, action, payload)
            response = self._response_for_command(charger, action, payload)
            self.schemas.validate(charger.version, action, response, response=True)
            await charger.ws.send_str(json.dumps([CALLRESULT, uid, response], separators=(",", ":")))
        except Exception as exc:
            await charger.ws.send_str(json.dumps([CALLERROR, uid, "InternalError", str(exc)[:255], {}], separators=(",", ":")))

    def _response_for_command(self, charger: VirtualCharger, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        version = charger.version
        if version == "1.6" and action == "GetConfiguration":
            keys = payload.get("key") or list(charger.config16)
            known = []
            unknown = []
            for key in keys:
                item = charger.config16.get(key)
                if item is None:
                    unknown.append(key)
                else:
                    known.append({"key": key, "readonly": bool(item["readonly"]), "value": str(item["value"])})
            out: dict[str, Any] = {}
            if known:
                out["configurationKey"] = known
            if unknown:
                out["unknownKey"] = unknown
            return out

        if version == "1.6" and action == "ChangeConfiguration":
            key, value = str(payload["key"]), str(payload["value"])
            existing = charger.config16.get(key)
            if existing and existing.get("readonly"):
                return {"status": "Rejected"}
            if existing is None:
                return {"status": "NotSupported"}
            existing["value"] = value
            return {"status": "Accepted"}

        if version in {"2.0.1", "2.1"} and action == "GetVariables":
            results = []
            for item in payload.get("getVariableData", []):
                component = item.get("component", {})
                variable = item.get("variable", {})
                attr_type = item.get("attributeType", "Actual")
                key = self._variable_key(component, variable, attr_type)
                result = {
                    "attributeStatus": "Accepted" if key in charger.variables else "UnknownVariable",
                    "attributeType": attr_type,
                    "component": component,
                    "variable": variable,
                }
                if key in charger.variables:
                    result["attributeValue"] = charger.variables[key]
                results.append(result)
            return {"getVariableResult": results}

        if version in {"2.0.1", "2.1"} and action == "SetVariables":
            results = []
            for item in payload.get("setVariableData", []):
                component = item.get("component", {})
                variable = item.get("variable", {})
                attr_type = item.get("attributeType", "Actual")
                key = self._variable_key(component, variable, attr_type)
                charger.variables[key] = str(item.get("attributeValue", ""))
                results.append({
                    "attributeStatus": "Accepted",
                    "attributeType": attr_type,
                    "component": component,
                    "variable": variable,
                })
            return {"setVariableResult": results}

        schema = self.schemas.get(version, action, response=True)
        response = sample_from_schema(schema)
        if not isinstance(response, dict):
            response = {}
        self._prefer_success_status(response, schema)
        return response

    @staticmethod
    def _prefer_success_status(payload: dict[str, Any], schema: dict[str, Any]) -> None:
        # Generated schema samples are already valid. This only chooses a positive
        # status when an action exposes the common top-level `status` enum.
        props = schema.get("properties", {})
        status_schema = props.get("status", {})
        enums = status_schema.get("enum", []) if isinstance(status_schema, dict) else []
        for preferred in ("Accepted", "Success", "Scheduled", "Unlocked", "RebootRequired"):
            if preferred in enums:
                payload["status"] = preferred
                break

    @staticmethod
    def _variable_key(component: dict[str, Any], variable: dict[str, Any], attribute_type: str) -> str:
        return "|".join([
            str(component.get("name", "")), str(component.get("instance", "")),
            str(variable.get("name", "")), str(variable.get("instance", "")), str(attribute_type),
        ])

    @staticmethod
    def _default_config_16() -> dict[str, dict[str, Any]]:
        return {
            "HeartbeatInterval": {"value": "30", "readonly": False},
            "MeterValueSampleInterval": {"value": "60", "readonly": False},
            "ClockAlignedDataInterval": {"value": "0", "readonly": False},
            "ConnectionTimeOut": {"value": "60", "readonly": False},
            "LocalAuthorizeOffline": {"value": "true", "readonly": False},
            "LocalPreAuthorize": {"value": "false", "readonly": False},
            "StopTransactionOnEVSideDisconnect": {"value": "true", "readonly": False},
            "UnlockConnectorOnEVSideDisconnect": {"value": "false", "readonly": False},
            "NumberOfConnectors": {"value": "1", "readonly": True},
        }

    @classmethod
    def _default_variables(cls) -> dict[str, str]:
        values: dict[str, str] = {}
        common = [
            ("OCPPCommCtrlr", "HeartbeatInterval", "30"),
            ("SampledDataCtrlr", "TxUpdatedInterval", "60"),
            ("SampledDataCtrlr", "AlignedDataInterval", "0"),
            ("AuthCtrlr", "Enabled", "true"),
        ]
        for component, variable, value in common:
            values[cls._variable_key({"name": component}, {"name": variable}, "Actual")] = value
        return values

    @staticmethod
    def _boot_payload(version: str) -> dict[str, Any]:
        if version == "1.6":
            return {"chargePointVendor": "OCPP-Lab", "chargePointModel": "Web Virtual Charger", "firmwareVersion": "1.0"}
        return {
            "reason": "PowerUp",
            "chargingStation": {"vendorName": "OCPP-Lab", "model": "Web Virtual Charger", "firmwareVersion": "1.0"},
        }

    @staticmethod
    def _status_payload(version: str) -> dict[str, Any]:
        if version == "1.6":
            return {"connectorId": 1, "errorCode": "NoError", "status": "Available"}
        return {"timestamp": utc_now(), "connectorStatus": "Available", "evseId": 1, "connectorId": 1}
