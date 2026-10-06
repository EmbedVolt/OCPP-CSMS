from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _resolve(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if not ref:
        return schema
    if not ref.startswith("#/definitions/"):
        return schema
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def sample_from_schema(schema: dict[str, Any], root: dict[str, Any] | None = None, field_name: str = "") -> Any:
    root = root or schema
    schema = _resolve(schema, root)

    if "const" in schema:
        return schema["const"]
    if "default" in schema:
        return schema["default"]
    if schema.get("enum"):
        enums = schema["enum"]
        for preferred in ("Accepted", "Available", "Idle", "Valid", "Success"):
            if preferred in enums:
                return preferred
        return enums[0]
    if "oneOf" in schema:
        return sample_from_schema(schema["oneOf"][0], root, field_name)
    if "anyOf" in schema:
        return sample_from_schema(schema["anyOf"][0], root, field_name)

    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        schema_type = next((t for t in schema_type if t != "null"), "null")

    lname = field_name.lower()
    if schema.get("format") == "date-time" or lname in {"currenttime", "timestamp"} or lname.endswith("timestamp"):
        return utc_now()
    if schema_type == "object" or "properties" in schema:
        props = schema.get("properties", {})
        required = schema.get("required", [])
        return {name: sample_from_schema(props[name], root, name) for name in required if name in props}
    if schema_type == "array":
        min_items = int(schema.get("minItems", 0))
        if min_items <= 0:
            return []
        return [sample_from_schema(schema.get("items", {}), root, field_name) for _ in range(min_items)]
    if schema_type == "integer":
        if "interval" in lname:
            return 30
        if "id" in lname:
            return 1
        return max(0, int(schema.get("minimum", 0)))
    if schema_type == "number":
        return float(max(0, schema.get("minimum", 0)))
    if schema_type == "boolean":
        return False
    if schema_type == "string":
        if "url" in lname or schema.get("format") == "uri":
            return "https://example.invalid/"
        if "certificate" in lname:
            return "TEST"
        min_length = int(schema.get("minLength", 1))
        return "x" * max(1, min_length)
    if schema_type == "null":
        return None
    return {}


def response_for(version: str, action: str, schema: dict[str, Any], *, heartbeat_interval: int = 30, transaction_id: int = 1) -> dict[str, Any]:
    payload = sample_from_schema(schema)
    if not isinstance(payload, dict):
        payload = {}

    if action in {"BootNotification", "Heartbeat"}:
        payload["currentTime"] = utc_now()
    if action == "BootNotification":
        payload["interval"] = heartbeat_interval
        payload["status"] = "Accepted"
    if action == "Authorize":
        if version == "1.6":
            payload.setdefault("idTagInfo", {})["status"] = "Accepted"
        else:
            payload.setdefault("idTokenInfo", {})["status"] = "Accepted"
    if version == "1.6" and action == "StartTransaction":
        payload["transactionId"] = transaction_id
        payload.setdefault("idTagInfo", {})["status"] = "Accepted"
    return payload
