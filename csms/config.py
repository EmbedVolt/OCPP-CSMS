from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True)
class Config:
    host: str = os.getenv("CSMS_HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", os.getenv("CSMS_PORT", "9001")))
    max_stations: int = int(os.getenv("CSMS_MAX_STATIONS", "10"))
    heartbeat_interval: int = int(os.getenv("CSMS_HEARTBEAT_INTERVAL", "30"))
    command_timeout: int = int(os.getenv("CSMS_COMMAND_TIMEOUT", "30"))
    db_path: Path = Path(os.getenv("CSMS_DB_PATH", "data/csms.sqlite3"))
    schema_root: Path = Path(os.getenv("CSMS_SCHEMA_ROOT", "schemas"))
    static_root: Path = Path(os.getenv("CSMS_STATIC_ROOT", "static"))


SUPPORTED_SUBPROTOCOLS = {
    "ocpp1.6": "1.6",
    "ocpp2.0.1": "2.0.1",
    "ocpp2.1": "2.1",
}
