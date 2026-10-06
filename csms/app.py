from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aiohttp import web

from .config import Config
from .db import Database
from .defaults import sample_from_schema, utc_now
from .ocpp_server import OcppHub
from .schema_registry import SchemaRegistry, VERSIONS
from .test_catalog import catalog_for
from .test_runner import TestRunner, run_schema_self_test
from .virtual_charger import VirtualChargerManager


def json_error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


def create_app(config: Config | None = None) -> web.Application:
    config = config or Config()
    db = Database(config.db_path)
    schemas = SchemaRegistry(config.schema_root)
    hub = OcppHub(config, db, schemas)
    virtual = VirtualChargerManager(config, schemas)
    test_runner = TestRunner(hub, db, schemas, virtual)

    app = web.Application(client_max_size=2 * 1024 * 1024)
    app["config"] = config
    app["db"] = db
    app["schemas"] = schemas
    app["hub"] = hub
    app["virtual"] = virtual
    app["test_runner"] = test_runner
    app.on_cleanup.append(_cleanup)

    app.router.add_get("/ocpp/{identity}", hub.handle_websocket)
    app.router.add_get("/api/health", api_health)
    app.router.add_get("/api/stations", api_stations)
    app.router.add_get("/api/stations/{identity}", api_station)
    app.router.add_post("/api/stations/{identity}/command", api_command)
    app.router.add_post("/api/stations/{identity}/disconnect", api_disconnect)
    app.router.add_get("/api/messages", api_messages)
    app.router.add_get("/api/transactions", api_transactions)
    app.router.add_get("/api/protocols", api_protocols)
    app.router.add_get("/api/actions/{version}", api_actions)
    app.router.add_get("/api/template/{version}/{action}", api_template)
    app.router.add_get("/api/schema/{version}/{action}", api_schema)
    app.router.add_get("/api/response-rules", api_response_rules)
    app.router.add_put("/api/response-rules/{version}/{action}", api_set_response_rule)
    app.router.add_delete("/api/response-rules/{version}/{action}", api_delete_response_rule)
    app.router.add_post("/api/stations/{identity}/configuration/read", api_configuration_read)
    app.router.add_post("/api/stations/{identity}/configuration/write", api_configuration_write)
    app.router.add_get("/api/stations/{identity}/dashboard", api_station_dashboard)
    app.router.add_get("/api/stations/{identity}/settings", api_station_settings)
    app.router.add_put("/api/stations/{identity}/settings", api_update_station_settings)
    app.router.add_post("/api/stations/{identity}/communication/clear", api_clear_communication)
    app.router.add_post("/api/stations/{identity}/maintenance", api_station_maintenance)
    app.router.add_get("/api/test-cases/{version}", api_test_cases)
    app.router.add_post("/api/stations/{identity}/tests/run", api_run_tests)
    app.router.add_get("/api/test-runs", api_test_runs)
    app.router.add_get("/api/test-runs/{run_id}", api_test_run)
    app.router.add_post("/api/self-tests/schemas", api_schema_self_tests)
    app.router.add_get("/api/virtual-chargers", api_virtual_chargers)
    app.router.add_post("/api/virtual-chargers", api_start_virtual_charger)
    app.router.add_delete("/api/virtual-chargers/{identity}", api_stop_virtual_charger)
    app.router.add_post("/api/virtual-chargers/{identity}/call", api_virtual_charger_call)

    static_root = config.static_root
    app.router.add_get("/", lambda _: web.FileResponse(static_root / "index.html"))
    app.router.add_get("/stations/{identity}", lambda _: web.FileResponse(static_root / "station_detail.html"))
    app.router.add_static("/static/", path=static_root, show_index=False)
    return app


async def api_health(request: web.Request) -> web.Response:
    hub: OcppHub = request.app["hub"]
    config: Config = request.app["config"]
    return web.json_response({
        "ok": True,
        "connectedStations": hub.connected_count(),
        "maxStations": config.max_stations,
        "versions": list(VERSIONS),
    })


async def api_stations(request: web.Request) -> web.Response:
    return web.json_response(request.app["db"].list_stations())


async def api_station(request: web.Request) -> web.Response:
    station = request.app["db"].get_station(request.match_info["identity"])
    return web.json_response(station) if station else json_error("Station not found", 404)


async def api_messages(request: web.Request) -> web.Response:
    try:
        limit = int(request.query.get("limit", "200"))
    except ValueError:
        limit = 200
    data = request.app["db"].list_messages(request.query.get("station_id"), limit, request.query.get("action"))
    return web.json_response(data)


async def api_transactions(request: web.Request) -> web.Response:
    try:
        limit = int(request.query.get("limit", "200"))
    except ValueError:
        limit = 200
    return web.json_response(request.app["db"].list_transactions(limit))


async def api_protocols(request: web.Request) -> web.Response:
    schemas: SchemaRegistry = request.app["schemas"]
    return web.json_response({version: len(schemas.actions(version)) for version in VERSIONS})


async def api_actions(request: web.Request) -> web.Response:
    version = request.match_info["version"]
    if version not in VERSIONS:
        return json_error("Unsupported version", 404)
    actions = request.app["schemas"].actions(version)
    return web.json_response([{"action": a.action, "direction": a.direction} for a in actions])


async def api_template(request: web.Request) -> web.Response:
    version, action = request.match_info["version"], request.match_info["action"]
    response = request.query.get("type") == "response"
    try:
        schema = request.app["schemas"].get(version, action, response=response)
        return web.json_response(sample_from_schema(schema))
    except KeyError as exc:
        return json_error(str(exc), 404)


async def api_schema(request: web.Request) -> web.Response:
    version, action = request.match_info["version"], request.match_info["action"]
    response = request.query.get("type") == "response"
    try:
        return web.json_response(request.app["schemas"].get(version, action, response=response))
    except KeyError as exc:
        return json_error(str(exc), 404)


async def api_command(request: web.Request) -> web.Response:
    station_id = request.match_info["identity"]
    try:
        body: dict[str, Any] = await request.json()
    except (json.JSONDecodeError, ValueError):
        return json_error("Request body must be JSON")
    action = body.get("action")
    payload = body.get("payload", {})
    timeout = body.get("timeout")
    validate = body.get("validate", True) is not False
    if not isinstance(action, str) or not action:
        return json_error("action is required")
    if not isinstance(payload, dict):
        return json_error("payload must be an object")
    try:
        result = await request.app["hub"].send_command(station_id, action, payload, timeout, validate=validate)
        return web.json_response(result)
    except LookupError as exc:
        return json_error(str(exc), 409)
    except KeyError as exc:
        return json_error(str(exc), 404)
    except ValueError as exc:
        return json_error(str(exc), 422)
    except TimeoutError as exc:
        return json_error(str(exc), 504)


async def api_disconnect(request: web.Request) -> web.Response:
    ok = await request.app["hub"].disconnect(request.match_info["identity"])
    return web.json_response({"disconnected": ok})



async def api_response_rules(request: web.Request) -> web.Response:
    return web.json_response(request.app["db"].list_response_rules())


async def api_set_response_rule(request: web.Request) -> web.Response:
    version, action = request.match_info["version"], request.match_info["action"]
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return json_error("Request body must be a JSON object")
    if not isinstance(payload, dict):
        return json_error("Response payload must be an object")
    try:
        request.app["schemas"].validate(version, action, payload, response=True)
    except KeyError as exc:
        return json_error(str(exc), 404)
    except ValueError as exc:
        return json_error(str(exc), 422)
    request.app["db"].set_response_rule(version, action, payload, utc_now())
    return web.json_response({"saved": True, "protocol": version, "action": action, "payload": payload})


async def api_delete_response_rule(request: web.Request) -> web.Response:
    version, action = request.match_info["version"], request.match_info["action"]
    deleted = request.app["db"].delete_response_rule(version, action)
    return web.json_response({"deleted": deleted})



async def _cleanup(app: web.Application) -> None:
    await app["virtual"].close()


def _station_or_error(request: web.Request) -> tuple[dict[str, Any] | None, web.Response | None]:
    station = request.app["db"].get_station(request.match_info["identity"])
    if not station:
        return None, json_error("Station not found", 404)
    if not station.get("connected"):
        return None, json_error("Station is not connected", 409)
    return station, None


async def api_configuration_read(request: web.Request) -> web.Response:
    station, error = _station_or_error(request)
    if error:
        return error
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        body = {}
    body = body if isinstance(body, dict) else {}
    version = station["protocol"]
    try:
        if version == "1.6":
            keys = body.get("keys")
            payload = {"key": keys} if isinstance(keys, list) and keys else {}
            result = await request.app["hub"].send_command(station["identity"], "GetConfiguration", payload)
        else:
            items = body.get("items") or [
                {"component": {"name": "OCPPCommCtrlr"}, "variable": {"name": "HeartbeatInterval"}, "attributeType": "Actual"},
                {"component": {"name": "SampledDataCtrlr"}, "variable": {"name": "TxUpdatedInterval"}, "attributeType": "Actual"},
            ]
            result = await request.app["hub"].send_command(station["identity"], "GetVariables", {"getVariableData": items})
        return web.json_response(result)
    except LookupError as exc:
        return json_error(str(exc), 409)
    except (KeyError, ValueError) as exc:
        return json_error(str(exc), 422)
    except TimeoutError as exc:
        return json_error(str(exc), 504)


async def api_configuration_write(request: web.Request) -> web.Response:
    station, error = _station_or_error(request)
    if error:
        return error
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return json_error("Request body must be JSON")
    if not isinstance(body, dict):
        return json_error("Request body must be an object")
    version = station["protocol"]
    try:
        if version == "1.6":
            key, value = body.get("key"), body.get("value")
            if not isinstance(key, str) or not key:
                return json_error("key is required")
            result = await request.app["hub"].send_command(
                station["identity"], "ChangeConfiguration", {"key": key, "value": str(value if value is not None else "")}
            )
        else:
            items = body.get("items")
            if not isinstance(items, list) or not items:
                return json_error("items is required")
            set_data = []
            for item in items:
                if not isinstance(item, dict):
                    return json_error("Every item must be an object")
                component = item.get("component")
                variable = item.get("variable")
                if isinstance(component, str):
                    component = {"name": component}
                if isinstance(variable, str):
                    variable = {"name": variable}
                set_data.append({
                    "component": component or {},
                    "variable": variable or {},
                    "attributeType": item.get("attributeType", "Actual"),
                    "attributeValue": str(item.get("attributeValue", item.get("value", ""))),
                })
            result = await request.app["hub"].send_command(
                station["identity"], "SetVariables", {"setVariableData": set_data}
            )
        return web.json_response(result)
    except LookupError as exc:
        return json_error(str(exc), 409)
    except (KeyError, ValueError) as exc:
        return json_error(str(exc), 422)
    except TimeoutError as exc:
        return json_error(str(exc), 504)


def _float_value(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalized_sample(sample: dict[str, Any]) -> tuple[str, float | None, str]:
    measurand = str(sample.get("measurand") or "Energy.Active.Import.Register")
    value = _float_value(sample.get("value"))
    unit = sample.get("unit")
    if not unit and isinstance(sample.get("unitOfMeasure"), dict):
        unit = sample["unitOfMeasure"].get("unit")
        multiplier = sample["unitOfMeasure"].get("multiplier", 0)
        try:
            if value is not None:
                value *= 10 ** int(multiplier)
        except (TypeError, ValueError):
            pass
    return measurand, value, str(unit or "")


def _dashboard_metrics(db: Database, identity: str) -> dict[str, Any]:
    messages = db.list_messages(identity, limit=1200)
    samples: list[dict[str, Any]] = []
    statuses: dict[str, int] = {}
    status_history: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    watched = {
        "BootNotification", "StatusNotification", "FirmwareStatusNotification", "DiagnosticsStatusNotification",
        "LogStatusNotification", "SecurityEventNotification", "NotifyEvent", "StartTransaction", "StopTransaction",
        "TransactionEvent",
    }
    for msg in reversed(messages):
        action = msg.get("action")
        payload = msg.get("payload") if isinstance(msg.get("payload"), dict) else {}
        if msg.get("direction") == "Station → CSMS" and msg.get("message_type") == "CALL":
            if action == "StatusNotification":
                status = payload.get("status") or payload.get("connectorStatus") or "Unknown"
                statuses[str(status)] = statuses.get(str(status), 0) + 1
                status_history.append({"ts": payload.get("timestamp") or msg.get("ts"), "status": str(status)})
            if action in watched:
                events.append({"ts": msg.get("ts"), "action": action, "payload": payload})
            if action in {"MeterValues", "TransactionEvent"}:
                for meter in payload.get("meterValue") or []:
                    currents: list[float] = []
                    power_w = 0.0
                    power_seen = False
                    energy_wh: float | None = None
                    for sample in meter.get("sampledValue") or []:
                        if not isinstance(sample, dict):
                            continue
                        measurand, value, unit = _normalized_sample(sample)
                        if value is None:
                            continue
                        low = measurand.lower()
                        unit_low = unit.lower()
                        if "current" in low:
                            if unit_low == "ma":
                                value /= 1000.0
                            currents.append(value)
                        elif "power.active.import" in low or low.startswith("power.active"):
                            if unit_low == "kw":
                                value *= 1000.0
                            power_w += value
                            power_seen = True
                        elif "energy.active.import.register" in low or "energy.active.import.interval" in low:
                            if unit_low == "kwh":
                                value *= 1000.0
                            energy_wh = value
                    if currents or power_seen or energy_wh is not None:
                        samples.append({
                            "ts": meter.get("timestamp") or payload.get("timestamp") or msg.get("ts"),
                            "current_a": round(sum(abs(x) for x in currents) / len(currents), 3) if currents else None,
                            "power_kw": round(power_w / 1000.0, 4) if power_seen else None,
                            "energy_kwh": round(energy_wh / 1000.0, 4) if energy_wh is not None else None,
                        })
    tx = [t for t in db.list_transactions(2000) if t.get("station_id") == identity]
    delivered = 0.0
    for item in tx:
        start, stop = _float_value(item.get("meter_start")), _float_value(item.get("meter_stop"))
        if start is not None and stop is not None and stop >= start:
            # Most OCPP meter registers are Wh. This is a lab estimate, not billing data.
            delivered += (stop - start) / 1000.0
    return {
        "meterSeries": samples[-300:],
        "statusCounts": statuses,
        "statusHistory": status_history[-500:],
        "recentEvents": list(reversed(events[-80:])),
        "transactionCount": len(tx),
        "activeTransactions": sum(1 for t in tx if t.get("status") == "Active"),
        "deliveredEnergyKWh": round(delivered, 4),
    }


async def api_station_dashboard(request: web.Request) -> web.Response:
    identity = request.match_info["identity"]
    if not request.app["db"].get_station(identity):
        return json_error("Station not found", 404)
    return web.json_response(_dashboard_metrics(request.app["db"], identity))


async def api_station_settings(request: web.Request) -> web.Response:
    identity = request.match_info["identity"]
    if not request.app["db"].get_station(identity):
        return json_error("Station not found", 404)
    return web.json_response(request.app["db"].get_station_settings(identity))


async def api_update_station_settings(request: web.Request) -> web.Response:
    identity = request.match_info["identity"]
    if not request.app["db"].get_station(identity):
        return json_error("Station not found", 404)
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return json_error("Request body must be JSON")
    if not isinstance(body, dict):
        return json_error("Request body must be an object")
    try:
        result = request.app["db"].set_station_settings(identity, body, utc_now())
    except (TypeError, ValueError) as exc:
        return json_error(str(exc), 422)
    return web.json_response(result)


async def api_clear_communication(request: web.Request) -> web.Response:
    identity = request.match_info["identity"]
    if not request.app["db"].get_station(identity):
        return json_error("Station not found", 404)
    deleted = request.app["db"].clear_messages(identity)
    return web.json_response({"cleared": deleted})


async def api_station_maintenance(request: web.Request) -> web.Response:
    identity = request.match_info["identity"]
    if not request.app["db"].get_station(identity):
        return json_error("Station not found", 404)
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        body = {}
    body = body if isinstance(body, dict) else {}
    ended = 0
    cleared = 0
    disconnected = False
    if body.get("endActiveTransactions") is True:
        ended = request.app["db"].end_all_active_transactions(identity, utc_now())
    if body.get("clearCommunicationLog") is True:
        cleared = request.app["db"].clear_messages(identity)
    if body.get("disconnect") is True:
        disconnected = await request.app["hub"].disconnect(identity)
    return web.json_response({"endedTransactions": ended, "clearedMessages": cleared, "disconnected": disconnected})


async def api_test_cases(request: web.Request) -> web.Response:
    version = request.match_info["version"]
    if version not in VERSIONS:
        return json_error("Unsupported version", 404)
    return web.json_response(catalog_for(request.app["schemas"], version))


async def api_run_tests(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        body = {}
    if not isinstance(body, dict):
        return json_error("Request body must be an object")
    actions = body.get("actions")
    if actions is not None and not isinstance(actions, list):
        return json_error("actions must be an array")
    try:
        result = await request.app["test_runner"].run(
            request.match_info["identity"],
            actions=actions,
            include_risky=body.get("includeRisky") is True,
            overrides=body.get("overrides") if isinstance(body.get("overrides"), dict) else None,
            timeout=max(1, min(int(body.get("timeout", 8)), 120)),
        )
        return web.json_response(result)
    except LookupError as exc:
        return json_error(str(exc), 409)
    except KeyError as exc:
        return json_error(str(exc), 404)
    except (ValueError, TypeError) as exc:
        return json_error(str(exc), 422)


async def api_test_runs(request: web.Request) -> web.Response:
    return web.json_response(request.app["db"].list_test_runs())


async def api_test_run(request: web.Request) -> web.Response:
    try:
        run_id = int(request.match_info["run_id"])
    except ValueError:
        return json_error("Invalid run id")
    data = request.app["db"].get_test_run(run_id)
    return web.json_response(data) if data else json_error("Test run not found", 404)


async def api_schema_self_tests(request: web.Request) -> web.Response:
    return web.json_response(run_schema_self_test(request.app["schemas"]))


async def api_virtual_chargers(request: web.Request) -> web.Response:
    return web.json_response(request.app["virtual"].list())


async def api_start_virtual_charger(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return json_error("Request body must be JSON")
    if not isinstance(body, dict):
        return json_error("Request body must be an object")
    try:
        result = await request.app["virtual"].start(str(body.get("identity", "VIRTUAL-001")), str(body.get("version", "1.6")))
        return web.json_response(result)
    except ValueError as exc:
        return json_error(str(exc), 422)
    except Exception as exc:
        return json_error(f"Could not start virtual charger: {exc}", 500)


async def api_stop_virtual_charger(request: web.Request) -> web.Response:
    stopped = await request.app["virtual"].stop(request.match_info["identity"] )
    return web.json_response({"stopped": stopped})


async def api_virtual_charger_call(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return json_error("Request body must be JSON")
    if not isinstance(body, dict) or not isinstance(body.get("action"), str) or not isinstance(body.get("payload", {}), dict):
        return json_error("action and payload are required")
    try:
        result = await request.app["virtual"].send_call(
            request.match_info["identity"], body["action"], body.get("payload", {}), int(body.get("timeout", 10))
        )
        return web.json_response(result)
    except LookupError as exc:
        return json_error(str(exc), 409)
    except (KeyError, ValueError) as exc:
        return json_error(str(exc), 422)
    except TimeoutError as exc:
        return json_error(str(exc), 504)

def main() -> None:
    config = Config()
    app = create_app(config)
    web.run_app(app, host=config.host, port=config.port, print=lambda x: print(x))


if __name__ == "__main__":
    main()
