from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft4Validator, Draft6Validator
from jsonschema.exceptions import ValidationError


VERSIONS = ("1.6", "2.0.1", "2.1")

# Direction metadata is for the UI only. The generic console can send any request
# schema deliberately, which is useful for negative/protocol testing.
CSMS_ACTIONS_16 = {
    "CancelReservation", "CertificateSigned", "ChangeAvailability", "ChangeConfiguration",
    "ClearCache", "ClearChargingProfile", "DataTransfer", "DeleteCertificate",
    "ExtendedTriggerMessage", "GetCompositeSchedule", "GetConfiguration", "GetDiagnostics",
    "GetInstalledCertificateIds", "GetLocalListVersion", "GetLog", "InstallCertificate",
    "RemoteStartTransaction", "RemoteStopTransaction", "ReserveNow", "Reset", "SendLocalList",
    "SetChargingProfile", "SignedUpdateFirmware", "TriggerMessage", "UnlockConnector", "UpdateFirmware",
}

CSMS_ACTIONS_201 = {
    "CancelReservation", "CertificateSigned", "ChangeAvailability", "ClearCache",
    "ClearChargingProfile", "ClearDisplayMessage", "ClearVariableMonitoring", "CustomerInformation",
    "DataTransfer", "DeleteCertificate", "GetBaseReport", "GetChargingProfiles", "GetCompositeSchedule",
    "GetDisplayMessages", "GetInstalledCertificateIds", "GetLocalListVersion", "GetLog",
    "GetMonitoringReport", "GetReport", "GetTransactionStatus", "GetVariables", "InstallCertificate",
    "PublishFirmware", "RequestStartTransaction", "RequestStopTransaction", "ReserveNow", "Reset",
    "SendLocalList", "SetChargingProfile", "SetDisplayMessage", "SetMonitoringBase", "SetMonitoringLevel",
    "SetNetworkProfile", "SetVariableMonitoring", "SetVariables", "TriggerMessage", "UnlockConnector",
    "UnpublishFirmware", "UpdateFirmware",
}

CSMS_ACTIONS_21 = CSMS_ACTIONS_201 | {
    "AFRRSignal", "AdjustPeriodicEventStream", "ChangeTransactionTariff", "ClearDERControl",
    "ClearTariffs", "GetCertificateChainStatus", "GetDERControl", "GetPeriodicEventStream", "GetTariffs",
    "OpenPeriodicEventStream", "RequestBatterySwap", "SetDERControl", "SetDefaultTariff",
    "UpdateDynamicSchedule", "UsePriorityCharging",
}


@dataclass(frozen=True)
class SchemaAction:
    action: str
    direction: str


class SchemaRegistry:
    def __init__(self, root: Path):
        self.root = root

    def _path(self, version: str, action: str, response: bool = False) -> Path:
        if version not in VERSIONS:
            raise KeyError(f"Unsupported OCPP version: {version}")
        suffix = "Response" if response else ""
        if version == "1.6":
            return self.root / version / f"{action}{suffix}.json"
        return self.root / version / f"{action}{'Response' if response else 'Request'}.json"

    @lru_cache(maxsize=512)
    def get(self, version: str, action: str, response: bool = False) -> dict[str, Any]:
        path = self._path(version, action, response)
        if not path.exists():
            raise KeyError(f"No {'response' if response else 'request'} schema for {version} {action}")
        return json.loads(path.read_text(encoding="utf-8-sig"))

    def has(self, version: str, action: str, response: bool = False) -> bool:
        try:
            return self._path(version, action, response).exists()
        except KeyError:
            return False

    def validate(self, version: str, action: str, payload: Any, response: bool = False) -> None:
        schema = self.get(version, action, response)
        validator_cls = Draft4Validator if version == "1.6" else Draft6Validator
        validator = validator_cls(schema)
        errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
        if errors:
            error: ValidationError = errors[0]
            path = ".".join(str(x) for x in error.absolute_path) or "$"
            raise ValueError(f"{path}: {error.message}")

    def actions(self, version: str) -> list[SchemaAction]:
        folder = self.root / version
        if version == "1.6":
            names = sorted(p.stem for p in folder.glob("*.json") if not p.stem.endswith("Response"))
        else:
            names = sorted(p.name[:-12] for p in folder.glob("*Request.json"))
        csms = CSMS_ACTIONS_16 if version == "1.6" else CSMS_ACTIONS_201 if version == "2.0.1" else CSMS_ACTIONS_21
        result = []
        for name in names:
            direction = "CSMS → Station" if name in csms else "Station → CSMS"
            if name == "DataTransfer":
                direction = "Bidirectional"
            result.append(SchemaAction(name, direction))
        return result
