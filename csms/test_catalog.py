from __future__ import annotations

from typing import Any

from .defaults import sample_from_schema
from .schema_registry import SchemaRegistry

# Actions that can materially change charger state or interrupt charging.
RISKY_ACTIONS = {
    "Reset", "UnlockConnector", "RemoteStartTransaction", "RemoteStopTransaction",
    "RequestStartTransaction", "RequestStopTransaction", "UpdateFirmware", "SignedUpdateFirmware",
    "PublishFirmware", "UnpublishFirmware", "InstallCertificate", "DeleteCertificate",
    "CertificateSigned", "SetNetworkProfile", "ChangeAvailability", "ChangeConfiguration",
    "SetVariables", "SetChargingProfile", "ClearChargingProfile", "ReserveNow", "CancelReservation",
    "SendLocalList", "ClearCache", "SetDisplayMessage", "ClearDisplayMessage", "SetMonitoringBase",
    "SetMonitoringLevel", "SetVariableMonitoring", "ClearVariableMonitoring", "SetDERControl",
    "ClearDERControl", "SetDefaultTariff", "ClearTariffs", "ChangeTransactionTariff",
    "UpdateDynamicSchedule", "UsePriorityCharging", "RequestBatterySwap",
}

READ_ONLY_PREFIXES = ("Get",)


def category_for(action: str) -> str:
    if action.startswith("Get"):
        return "Read / Query"
    if action in {"Reset", "UnlockConnector", "ChangeAvailability"}:
        return "Station control"
    if "Transaction" in action or action in {"ReserveNow", "CancelReservation"}:
        return "Transactions"
    if "ChargingProfile" in action or "Schedule" in action or "Tariff" in action:
        return "Smart charging"
    if "Firmware" in action or "Log" in action or "Diagnostics" in action:
        return "Firmware / diagnostics"
    if "Certificate" in action or "NetworkProfile" in action:
        return "Security / network"
    if "Variable" in action or "Configuration" in action or "Monitoring" in action:
        return "Configuration"
    if "Display" in action:
        return "Display"
    if action == "DataTransfer":
        return "Vendor extension"
    return "Protocol"


def catalog_for(schemas: SchemaRegistry, version: str) -> list[dict[str, Any]]:
    out = []
    for item in schemas.actions(version):
        request_schema = schemas.get(version, item.action)
        payload = sample_from_schema(request_schema)
        # Use meaningful device-model defaults instead of schema placeholder names.
        # This makes the generated live test useful on both the built-in virtual
        # charger and typical OCPP 2.x implementations.
        if version == "1.6" and item.action == "ChangeConfiguration":
            payload = {"key": "HeartbeatInterval", "value": "30"}
        elif version in {"2.0.1", "2.1"} and item.action == "GetVariables":
            payload = {"getVariableData": [{
                "component": {"name": "OCPPCommCtrlr"},
                "variable": {"name": "HeartbeatInterval"},
                "attributeType": "Actual",
            }]}
        elif version in {"2.0.1", "2.1"} and item.action == "SetVariables":
            payload = {"setVariableData": [{
                "component": {"name": "OCPPCommCtrlr"},
                "variable": {"name": "HeartbeatInterval"},
                "attributeType": "Actual",
                "attributeValue": "30",
            }]}
        out.append({
            "action": item.action,
            "direction": item.direction,
            "category": category_for(item.action),
            "risky": item.action in RISKY_ACTIONS,
            "defaultSelected": item.direction == "CSMS → Station" and item.action.startswith(READ_ONLY_PREFIXES),
            "payload": payload,
        })
    return out


def response_looks_successful(payload: Any) -> tuple[bool, str]:
    """Best-effort semantic result for live lab tests.

    A valid CALLRESULT still can represent a business-level rejection. We inspect
    common OCPP status fields while leaving action-specific interpretation visible
    to the user in the raw response.
    """
    negative = {
        "Rejected", "Failed", "Failure", "NotSupported", "Unknown", "UnknownVariable",
        "UnknownComponent", "NotFound", "NotAtThisTime", "NotImplemented", "Invalid",
    }
    if not isinstance(payload, dict):
        return True, "CALLRESULT received"

    found: list[tuple[str, str]] = []

    def walk(value: Any, path: str = "$") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}"
                if key in {"status", "attributeStatus", "certificateStatus"} and isinstance(child, str):
                    found.append((child_path, child))
                walk(child, child_path)
        elif isinstance(value, list):
            for i, child in enumerate(value):
                walk(child, f"{path}[{i}]")

    walk(payload)
    for path, value in found:
        if value in negative:
            return False, f"{path}={value}"
    return True, "CALLRESULT received" if not found else ", ".join(f"{p}={v}" for p, v in found[:4])
