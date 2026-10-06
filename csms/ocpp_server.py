from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from aiohttp import WSMsgType, web

from .config import Config, SUPPORTED_SUBPROTOCOLS
from .db import Database
from .defaults import response_for, utc_now
from .schema_registry import SchemaRegistry

CALL = 2
CALLRESULT = 3
CALLERROR = 4


@dataclass
class PendingCall:
    action: str
    future: asyncio.Future


@dataclass
class StationSession:
    identity: str
    protocol: str
    ws: web.WebSocketResponse
    pending: dict[str, PendingCall] = field(default_factory=dict)


class OcppHub:
    def __init__(self, config: Config, db: Database, schemas: SchemaRegistry):
        self.config = config
        self.db = db
        self.schemas = schemas
        self.sessions: dict[str, StationSession] = {}

    def connected_count(self) -> int:
        return len(self.sessions)

    async def handle_websocket(self, request: web.Request) -> web.StreamResponse:
        identity = request.match_info["identity"].strip()
        if not identity or len(identity) > 100:
            raise web.HTTPBadRequest(text="Invalid station identity")

        offered = [x.strip() for x in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if x.strip()]
        chosen = next((p for p in offered if p in SUPPORTED_SUBPROTOCOLS), None)
        if not chosen:
            return web.json_response(
                {"error": "Unsupported OCPP subprotocol", "supported": list(SUPPORTED_SUBPROTOCOLS)}, status=400
            )
        protocol = SUPPORTED_SUBPROTOCOLS[chosen]

        old = self.sessions.get(identity)
        if not old and len(self.sessions) >= self.config.max_stations:
            return web.json_response({"error": f"Maximum {self.config.max_stations} connected stations reached"}, status=503)
        if old:
            await old.ws.close(code=1000, message=b"Replaced by newer connection")

        ws = web.WebSocketResponse(protocols=[chosen], heartbeat=45, autoping=True, max_msg_size=2 * 1024 * 1024)
        await ws.prepare(request)
        remote = request.remote or "unknown"
        session = StationSession(identity, protocol, ws)
        self.sessions[identity] = session
        self.db.station_connected(identity, protocol, utc_now(), remote)

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._on_text(session, msg.data)
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            if self.sessions.get(identity) is session:
                self.sessions.pop(identity, None)
                self.db.station_disconnected(identity, utc_now())
            for pending in session.pending.values():
                if not pending.future.done():
                    pending.future.set_exception(ConnectionError("Charging station disconnected"))
        return ws

    async def _on_text(self, session: StationSession, raw: str) -> None:
        now = utc_now()
        self.db.touch_station(session.identity, now)
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError as exc:
            await session.ws.close(code=1007, message=f"Invalid JSON: {exc}".encode()[:120])
            return
        if not isinstance(frame, list) or not frame:
            await session.ws.close(code=1007, message=b"OCPP frame must be a JSON array")
            return

        kind = frame[0]
        if kind == CALL:
            await self._handle_call(session, frame)
        elif kind == CALLRESULT:
            await self._handle_call_result(session, frame)
        elif kind == CALLERROR:
            await self._handle_call_error(session, frame)
        else:
            await session.ws.close(code=1007, message=b"Unsupported OCPP message type")

    async def _handle_call(self, session: StationSession, frame: list[Any]) -> None:
        if len(frame) != 4 or not isinstance(frame[1], str) or not isinstance(frame[2], str) or not isinstance(frame[3], dict):
            return await self._send_call_error(session, str(frame[1]) if len(frame) > 1 else "unknown", "FormationViolation", "Malformed CALL frame", {})

        _, unique_id, action, payload = frame
        self.db.add_message(ts=utc_now(), station_id=session.identity, protocol=session.protocol,
                            direction="Station → CSMS", message_type="CALL", unique_id=unique_id,
                            action=action, payload=payload)
        if not self.schemas.has(session.protocol, action):
            return await self._send_call_error(session, unique_id, "NotImplemented", f"Unsupported action: {action}", {})
        try:
            self.schemas.validate(session.protocol, action, payload)
        except ValueError as exc:
            return await self._send_call_error(session, unique_id, "FormationViolation", str(exc), {})

        try:
            txid = self._project_state(session, action, payload)
            schema = self.schemas.get(session.protocol, action, response=True)
            response = self.db.get_response_rule(session.protocol, action)
            if response is None:
                response = response_for(session.protocol, action, schema,
                                        heartbeat_interval=self.config.heartbeat_interval,
                                        transaction_id=txid or 1)
            # Validate our auto-response before putting it on the wire.
            self.schemas.validate(session.protocol, action, response, response=True)
            await session.ws.send_str(json.dumps([CALLRESULT, unique_id, response], separators=(",", ":")))
            self.db.add_message(ts=utc_now(), station_id=session.identity, protocol=session.protocol,
                                direction="CSMS → Station", message_type="CALLRESULT", unique_id=unique_id,
                                action=action, payload=response)
        except Exception as exc:
            await self._send_call_error(session, unique_id, "InternalError", str(exc), {})

    async def _handle_call_result(self, session: StationSession, frame: list[Any]) -> None:
        if len(frame) != 3 or not isinstance(frame[1], str) or not isinstance(frame[2], dict):
            return
        _, unique_id, payload = frame
        pending = session.pending.pop(unique_id, None)
        action = pending.action if pending else None
        self.db.add_message(ts=utc_now(), station_id=session.identity, protocol=session.protocol,
                            direction="Station → CSMS", message_type="CALLRESULT", unique_id=unique_id,
                            action=action, payload=payload)
        if pending and not pending.future.done():
            validation_error = None
            if action:
                try:
                    self.schemas.validate(session.protocol, action, payload, response=True)
                except (KeyError, ValueError) as exc:
                    validation_error = str(exc)
            result = {"type": "CALLRESULT", "uniqueId": unique_id, "action": action, "payload": payload}
            if validation_error:
                result["responseValidationError"] = validation_error
            pending.future.set_result(result)

    async def _handle_call_error(self, session: StationSession, frame: list[Any]) -> None:
        if len(frame) != 5 or not isinstance(frame[1], str):
            return
        _, unique_id, code, description, details = frame
        pending = session.pending.pop(unique_id, None)
        action = pending.action if pending else None
        error = {"code": code, "description": description, "details": details}
        self.db.add_message(ts=utc_now(), station_id=session.identity, protocol=session.protocol,
                            direction="Station → CSMS", message_type="CALLERROR", unique_id=unique_id,
                            action=action, error=error)
        if pending and not pending.future.done():
            pending.future.set_result({"type": "CALLERROR", "uniqueId": unique_id, "action": action, "error": error})

    async def _send_call_error(self, session: StationSession, unique_id: str, code: str, description: str, details: dict[str, Any]) -> None:
        frame = [CALLERROR, unique_id, code, description[:255], details]
        await session.ws.send_str(json.dumps(frame, separators=(",", ":")))
        self.db.add_message(ts=utc_now(), station_id=session.identity, protocol=session.protocol,
                            direction="CSMS → Station", message_type="CALLERROR", unique_id=unique_id,
                            action=None, error={"code": code, "description": description, "details": details})

    async def send_command(self, station_id: str, action: str, payload: dict[str, Any], timeout: int | None = None, validate: bool = True) -> dict[str, Any]:
        session = self.sessions.get(station_id)
        if not session:
            raise LookupError("Station is not connected")
        if not self.schemas.has(session.protocol, action):
            raise KeyError(f"No {session.protocol} schema for {action}")
        if validate:
            self.schemas.validate(session.protocol, action, payload)

        unique_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        session.pending[unique_id] = PendingCall(action, future)
        frame = [CALL, unique_id, action, payload]
        await session.ws.send_str(json.dumps(frame, separators=(",", ":")))
        self.db.add_message(ts=utc_now(), station_id=session.identity, protocol=session.protocol,
                            direction="CSMS → Station", message_type="CALL", unique_id=unique_id,
                            action=action, payload=payload)
        try:
            return await asyncio.wait_for(future, timeout=timeout or self.config.command_timeout)
        except asyncio.TimeoutError:
            session.pending.pop(unique_id, None)
            raise TimeoutError(f"No response to {action} within {timeout or self.config.command_timeout}s")

    async def disconnect(self, station_id: str) -> bool:
        session = self.sessions.get(station_id)
        if not session:
            return False
        await session.ws.close(code=1000, message=b"Disconnected from dashboard")
        return True

    def _project_state(self, session: StationSession, action: str, payload: dict[str, Any]) -> int | None:
        now = utc_now()
        if action == "BootNotification":
            self.db.update_boot(session.identity, payload)
        elif action == "StatusNotification":
            if session.protocol == "1.6":
                key = str(payload.get("connectorId", 0))
                status = payload.get("status", "Unknown")
            else:
                evse = payload.get("evseId", 0)
                conn = payload.get("connectorId", 0)
                key = f"{evse}:{conn}"
                status = payload.get("connectorStatus", "Unknown")
            self.db.update_connector(session.identity, key, status)
        elif session.protocol == "1.6" and action == "StartTransaction":
            txid = self.db.start_transaction(
                session.identity, session.protocol, payload.get("timestamp", now),
                ocpp_transaction_id=None, id_token=payload.get("idTag"), evse_id=None,
                connector_id=payload.get("connectorId"), meter_start=payload.get("meterStart"),
            )
            self.db.execute("UPDATE transactions SET ocpp_transaction_id=? WHERE id=?", (str(txid), txid))
            return txid
        elif session.protocol == "1.6" and action == "StopTransaction":
            self.db.end_transaction(session.identity, payload.get("timestamp", now),
                                    ocpp_transaction_id=str(payload.get("transactionId")) if payload.get("transactionId") is not None else None,
                                    meter_stop=payload.get("meterStop"))
        elif action == "TransactionEvent":
            txinfo = payload.get("transactionInfo", {})
            txid = str(txinfo.get("transactionId", ""))
            evse = payload.get("evse", {}) or {}
            token = payload.get("idToken", {}) or {}
            event_type = payload.get("eventType")
            if event_type == "Started":
                self.db.ensure_transaction(session.identity, session.protocol, txid, payload.get("timestamp", now),
                                           token.get("idToken"), evse.get("id"), evse.get("connectorId"))
            elif event_type == "Ended":
                self.db.end_transaction(session.identity, payload.get("timestamp", now), ocpp_transaction_id=txid)
        elif action == "MeterValues":
            value = self._extract_meter_value(payload)
            txid = payload.get("transactionId")
            self.db.update_meter(session.identity, value, str(txid) if txid is not None else None)
        return None

    @staticmethod
    def _extract_meter_value(payload: dict[str, Any]) -> float | None:
        meter_values = payload.get("meterValue") or []
        for mv in reversed(meter_values):
            for sample in reversed(mv.get("sampledValue") or []):
                raw = sample.get("value")
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    continue
        return None
