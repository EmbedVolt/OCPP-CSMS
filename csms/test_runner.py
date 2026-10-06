from __future__ import annotations

import time
from typing import Any

from .db import Database
from .defaults import utc_now
from .ocpp_server import OcppHub
from .schema_registry import SchemaRegistry
from .test_catalog import catalog_for, response_looks_successful
from .virtual_charger import VirtualChargerManager


class TestRunner:
    def __init__(self, hub: OcppHub, db: Database, schemas: SchemaRegistry, virtual: VirtualChargerManager):
        self.hub = hub
        self.db = db
        self.schemas = schemas
        self.virtual = virtual

    async def run(self, station_id: str, actions: list[str] | None = None, *, include_risky: bool = False,
                  overrides: dict[str, Any] | None = None, timeout: int = 8) -> dict[str, Any]:
        station = self.db.get_station(station_id)
        if not station or not station.get("connected"):
            raise LookupError("Station is not connected")
        version = station["protocol"]
        catalog = catalog_for(self.schemas, version)
        by_action = {item["action"]: item for item in catalog}
        selected = actions or [item["action"] for item in catalog]
        unknown = [a for a in selected if a not in by_action]
        if unknown:
            raise KeyError(f"Unknown action(s): {', '.join(unknown)}")

        run_id = self.db.create_test_run(station_id, version, utc_now())
        results: list[dict[str, Any]] = []
        counts = {"PASS": 0, "FAIL": 0, "SKIPPED": 0}
        overrides = overrides or {}

        for action in selected:
            item = by_action[action]
            direction = item["direction"]
            payload = overrides.get(action, item["payload"])
            if item["risky"] and not include_risky:
                result = self._result(action, direction, "SKIPPED", 0, "State-changing test excluded. Enable risky tests to run it.", payload, None)
            else:
                result = await self._run_one(station_id, version, action, direction, payload, timeout)
            results.append(result)
            counts[result["status"]] += 1
            self.db.add_test_result(
                run_id, action, direction, result["status"], result["durationMs"], result["detail"],
                result.get("request"), result.get("response"),
            )

        self.db.finish_test_run(run_id, utc_now(), counts["PASS"], counts["FAIL"], counts["SKIPPED"])
        return {
            "runId": run_id,
            "stationId": station_id,
            "protocol": version,
            "passed": counts["PASS"],
            "failed": counts["FAIL"],
            "skipped": counts["SKIPPED"],
            "results": results,
        }

    async def _run_one(self, station_id: str, version: str, action: str, direction: str,
                       payload: dict[str, Any], timeout: int) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            self.schemas.validate(version, action, payload)
            if direction == "Station → CSMS":
                if station_id not in self.virtual.chargers:
                    return self._result(
                        action, direction, "SKIPPED", self._ms(started),
                        "Inbound action needs a virtual charger or a physical charger event; CSMS cannot force this message directly.",
                        payload, None,
                    )
                response = await self.virtual.send_call(station_id, action, payload, timeout=timeout)
            else:
                response = await self.hub.send_command(station_id, action, payload, timeout=timeout, validate=True)

            if response.get("type") == "CALLERROR":
                return self._result(action, direction, "FAIL", self._ms(started),
                                    f"CALLERROR: {response.get('error', {}).get('code', 'Unknown')}", payload, response)
            if response.get("responseValidationError"):
                return self._result(action, direction, "FAIL", self._ms(started),
                                    f"Invalid response schema: {response['responseValidationError']}", payload, response)

            response_payload = response.get("payload", {})
            try:
                self.schemas.validate(version, action, response_payload, response=True)
            except (KeyError, ValueError) as exc:
                return self._result(action, direction, "FAIL", self._ms(started),
                                    f"Response schema validation failed: {exc}", payload, response)

            ok, detail = response_looks_successful(response_payload)
            return self._result(action, direction, "PASS" if ok else "FAIL", self._ms(started), detail, payload, response)
        except TimeoutError as exc:
            return self._result(action, direction, "FAIL", self._ms(started), str(exc), payload, None)
        except Exception as exc:
            return self._result(action, direction, "FAIL", self._ms(started), str(exc), payload, None)

    @staticmethod
    def _ms(started: float) -> int:
        return max(0, int((time.perf_counter() - started) * 1000))

    @staticmethod
    def _result(action: str, direction: str, status: str, duration_ms: int, detail: str,
                request: Any, response: Any) -> dict[str, Any]:
        return {
            "action": action,
            "direction": direction,
            "status": status,
            "durationMs": duration_ms,
            "detail": detail,
            "request": request,
            "response": response,
        }


def run_schema_self_test(schemas: SchemaRegistry) -> dict[str, Any]:
    from .defaults import response_for, sample_from_schema
    from .schema_registry import VERSIONS

    results = []
    passed = failed = 0
    for version in VERSIONS:
        for item in schemas.actions(version):
            action = item.action
            errors = []
            try:
                request_payload = sample_from_schema(schemas.get(version, action))
                schemas.validate(version, action, request_payload)
            except Exception as exc:
                errors.append(f"request: {exc}")
            if schemas.has(version, action, response=True):
                try:
                    response_payload = response_for(version, action, schemas.get(version, action, response=True))
                    schemas.validate(version, action, response_payload, response=True)
                except Exception as exc:
                    errors.append(f"response: {exc}")
            status = "PASS" if not errors else "FAIL"
            passed += status == "PASS"
            failed += status == "FAIL"
            results.append({"protocol": version, "action": action, "status": status, "detail": "; ".join(errors) or "Request/response templates valid"})
    return {"passed": passed, "failed": failed, "total": passed + failed, "results": results}
