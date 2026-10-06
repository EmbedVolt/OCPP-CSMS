from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS stations (
                identity TEXT PRIMARY KEY,
                protocol TEXT NOT NULL,
                connected INTEGER NOT NULL DEFAULT 0,
                connected_at TEXT,
                disconnected_at TEXT,
                last_seen TEXT,
                remote_addr TEXT,
                vendor TEXT,
                model TEXT,
                firmware TEXT,
                boot_reason TEXT,
                connectors_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                station_id TEXT NOT NULL,
                protocol TEXT NOT NULL,
                direction TEXT NOT NULL,
                message_type TEXT NOT NULL,
                unique_id TEXT,
                action TEXT,
                payload_json TEXT,
                error_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_messages_station_id ON messages(station_id, id DESC);
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                station_id TEXT NOT NULL,
                protocol TEXT NOT NULL,
                ocpp_transaction_id TEXT,
                status TEXT NOT NULL,
                started_at TEXT,
                ended_at TEXT,
                id_token TEXT,
                evse_id INTEGER,
                connector_id INTEGER,
                meter_start REAL,
                meter_stop REAL,
                last_meter REAL
            );
            CREATE INDEX IF NOT EXISTS idx_transactions_station ON transactions(station_id, id DESC);
            CREATE TABLE IF NOT EXISTS response_rules (
                protocol TEXT NOT NULL,
                action TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(protocol, action)
            );
            CREATE TABLE IF NOT EXISTS test_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                station_id TEXT NOT NULL,
                protocol TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                passed INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                skipped INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS test_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                direction TEXT NOT NULL,
                status TEXT NOT NULL,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                detail TEXT,
                request_json TEXT,
                response_json TEXT,
                FOREIGN KEY(run_id) REFERENCES test_runs(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_test_results_run ON test_results(run_id, id);
            CREATE TABLE IF NOT EXISTS station_settings (
                identity TEXT PRIMARY KEY,
                friendly_name TEXT,
                reference TEXT,
                physical_id TEXT,
                security_profile TEXT,
                allowed_ip TEXT,
                notes TEXT,
                debug_logging INTEGER NOT NULL DEFAULT 0,
                location_name TEXT,
                latitude REAL,
                longitude REAL,
                remote_access_url TEXT,
                remote_access_enabled INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT
            );
            """
        )
        self.conn.commit()

    def execute(self, sql: str, args: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, args)
        self.conn.commit()
        return cur

    def station_connected(self, identity: str, protocol: str, now: str, remote_addr: str) -> None:
        self.execute(
            """
            INSERT INTO stations(identity, protocol, connected, connected_at, last_seen, remote_addr)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(identity) DO UPDATE SET protocol=excluded.protocol, connected=1,
              connected_at=excluded.connected_at, last_seen=excluded.last_seen, remote_addr=excluded.remote_addr
            """,
            (identity, protocol, 1, now, now, remote_addr),
        )

    def station_disconnected(self, identity: str, now: str) -> None:
        self.execute("UPDATE stations SET connected=0, disconnected_at=?, last_seen=? WHERE identity=?", (now, now, identity))

    def touch_station(self, identity: str, now: str) -> None:
        self.execute("UPDATE stations SET last_seen=? WHERE identity=?", (now, identity))

    def update_boot(self, identity: str, payload: dict[str, Any]) -> None:
        station = payload.get("chargingStation", {}) if "chargingStation" in payload else payload
        vendor = station.get("vendorName") or payload.get("chargePointVendor")
        model = station.get("model") or payload.get("chargePointModel")
        firmware = station.get("firmwareVersion") or payload.get("firmwareVersion")
        reason = payload.get("reason")
        self.execute(
            "UPDATE stations SET vendor=?, model=?, firmware=?, boot_reason=? WHERE identity=?",
            (vendor, model, firmware, reason, identity),
        )

    def update_connector(self, identity: str, key: str, status: str) -> None:
        row = self.conn.execute("SELECT connectors_json FROM stations WHERE identity=?", (identity,)).fetchone()
        values = json.loads(row[0] or "{}") if row else {}
        values[str(key)] = status
        self.execute("UPDATE stations SET connectors_json=? WHERE identity=?", (json.dumps(values), identity))

    def add_message(self, *, ts: str, station_id: str, protocol: str, direction: str, message_type: str,
                    unique_id: str | None, action: str | None, payload: Any = None, error: Any = None) -> int:
        cur = self.execute(
            """INSERT INTO messages(ts,station_id,protocol,direction,message_type,unique_id,action,payload_json,error_json)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (ts, station_id, protocol, direction, message_type, unique_id, action,
             json.dumps(payload, separators=(",", ":"), ensure_ascii=False) if payload is not None else None,
             json.dumps(error, separators=(",", ":"), ensure_ascii=False) if error is not None else None),
        )
        return int(cur.lastrowid)

    def list_stations(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM stations ORDER BY connected DESC, identity").fetchall()
        return [self._station_row(r) for r in rows]

    def get_station(self, identity: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM stations WHERE identity=?", (identity,)).fetchone()
        return self._station_row(row) if row else None

    @staticmethod
    def _station_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["connected"] = bool(data["connected"])
        data["connectors"] = json.loads(data.pop("connectors_json") or "{}")
        return data

    def list_messages(self, station_id: str | None = None, limit: int = 200, action: str | None = None) -> list[dict[str, Any]]:
        where, args = [], []
        if station_id:
            where.append("station_id=?")
            args.append(station_id)
        if action:
            where.append("action=?")
            args.append(action)
        sql = "SELECT * FROM messages" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
        args.append(min(max(limit, 1), 2000))
        rows = self.conn.execute(sql, tuple(args)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json")) if d.get("payload_json") else None
            d["error"] = json.loads(d.pop("error_json")) if d.get("error_json") else None
            out.append(d)
        return out

    def start_transaction(self, station_id: str, protocol: str, started_at: str, *, ocpp_transaction_id: str | None,
                          id_token: str | None, evse_id: int | None, connector_id: int | None,
                          meter_start: float | None = None) -> int:
        cur = self.execute(
            """INSERT INTO transactions(station_id,protocol,ocpp_transaction_id,status,started_at,id_token,evse_id,connector_id,meter_start,last_meter)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (station_id, protocol, ocpp_transaction_id, "Active", started_at, id_token, evse_id, connector_id, meter_start, meter_start),
        )
        return int(cur.lastrowid)

    def ensure_transaction(self, station_id: str, protocol: str, ocpp_transaction_id: str, started_at: str,
                           id_token: str | None, evse_id: int | None, connector_id: int | None) -> int:
        row = self.conn.execute(
            "SELECT id FROM transactions WHERE station_id=? AND ocpp_transaction_id=? AND status='Active' ORDER BY id DESC LIMIT 1",
            (station_id, ocpp_transaction_id),
        ).fetchone()
        if row:
            return int(row[0])
        return self.start_transaction(station_id, protocol, started_at, ocpp_transaction_id=ocpp_transaction_id,
                                      id_token=id_token, evse_id=evse_id, connector_id=connector_id)

    def end_transaction(self, station_id: str, ended_at: str, *, ocpp_transaction_id: str | None = None,
                        meter_stop: float | None = None) -> None:
        if ocpp_transaction_id:
            self.execute(
                """UPDATE transactions SET status='Ended', ended_at=?, meter_stop=?, last_meter=COALESCE(?,last_meter)
                   WHERE id=(SELECT id FROM transactions WHERE station_id=? AND ocpp_transaction_id=? AND status='Active' ORDER BY id DESC LIMIT 1)""",
                (ended_at, meter_stop, meter_stop, station_id, ocpp_transaction_id),
            )
        else:
            self.execute(
                """UPDATE transactions SET status='Ended', ended_at=?, meter_stop=?, last_meter=COALESCE(?,last_meter)
                   WHERE id=(SELECT id FROM transactions WHERE station_id=? AND status='Active' ORDER BY id DESC LIMIT 1)""",
                (ended_at, meter_stop, meter_stop, station_id),
            )

    def update_meter(self, station_id: str, value: float | None, ocpp_transaction_id: str | None = None) -> None:
        if value is None:
            return
        if ocpp_transaction_id:
            self.execute(
                "UPDATE transactions SET last_meter=? WHERE id=(SELECT id FROM transactions WHERE station_id=? AND ocpp_transaction_id=? AND status='Active' ORDER BY id DESC LIMIT 1)",
                (value, station_id, ocpp_transaction_id),
            )
        else:
            self.execute(
                "UPDATE transactions SET last_meter=? WHERE id=(SELECT id FROM transactions WHERE station_id=? AND status='Active' ORDER BY id DESC LIMIT 1)",
                (value, station_id),
            )


    def get_response_rule(self, protocol: str, action: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT payload_json FROM response_rules WHERE protocol=? AND action=?", (protocol, action)).fetchone()
        return json.loads(row[0]) if row else None

    def set_response_rule(self, protocol: str, action: str, payload: dict[str, Any], updated_at: str) -> None:
        self.execute(
            """INSERT INTO response_rules(protocol,action,payload_json,updated_at) VALUES(?,?,?,?)
               ON CONFLICT(protocol,action) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
            (protocol, action, json.dumps(payload, separators=(",", ":"), ensure_ascii=False), updated_at),
        )

    def delete_response_rule(self, protocol: str, action: str) -> bool:
        cur = self.execute("DELETE FROM response_rules WHERE protocol=? AND action=?", (protocol, action))
        return cur.rowcount > 0

    def list_response_rules(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT protocol,action,payload_json,updated_at FROM response_rules ORDER BY protocol,action").fetchall()
        return [{"protocol": r[0], "action": r[1], "payload": json.loads(r[2]), "updated_at": r[3]} for r in rows]

    def list_transactions(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM transactions ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 2000),)).fetchall()
        return [dict(r) for r in rows]
    def create_test_run(self, station_id: str, protocol: str, started_at: str) -> int:
        cur = self.execute(
            "INSERT INTO test_runs(station_id,protocol,started_at) VALUES(?,?,?)",
            (station_id, protocol, started_at),
        )
        return int(cur.lastrowid)

    def add_test_result(self, run_id: int, action: str, direction: str, status: str, duration_ms: int,
                        detail: str, request_payload: Any = None, response_payload: Any = None) -> int:
        cur = self.execute(
            """INSERT INTO test_results(run_id,action,direction,status,duration_ms,detail,request_json,response_json)
               VALUES(?,?,?,?,?,?,?,?)""",
            (run_id, action, direction, status, duration_ms, detail,
             json.dumps(request_payload, ensure_ascii=False) if request_payload is not None else None,
             json.dumps(response_payload, ensure_ascii=False) if response_payload is not None else None),
        )
        return int(cur.lastrowid)

    def finish_test_run(self, run_id: int, finished_at: str, passed: int, failed: int, skipped: int) -> None:
        self.execute(
            "UPDATE test_runs SET finished_at=?,passed=?,failed=?,skipped=? WHERE id=?",
            (finished_at, passed, failed, skipped, run_id),
        )

    def list_test_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM test_runs ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 200),)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_test_run(self, run_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM test_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        run = dict(row)
        results = self.conn.execute("SELECT * FROM test_results WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        converted = []
        for r in results:
            d = dict(r)
            d["request"] = json.loads(d.pop("request_json")) if d.get("request_json") else None
            d["response"] = json.loads(d.pop("response_json")) if d.get("response_json") else None
            converted.append(d)
        run["results"] = converted
        return run
    def get_station_settings(self, identity: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM station_settings WHERE identity=?", (identity,)).fetchone()
        if not row:
            return {
                "identity": identity, "friendly_name": "", "reference": "", "physical_id": "",
                "security_profile": "None", "allowed_ip": "", "notes": "", "debug_logging": False,
                "location_name": "", "latitude": None, "longitude": None, "remote_access_url": "",
                "remote_access_enabled": False, "updated_at": None,
            }
        data = dict(row)
        data["debug_logging"] = bool(data["debug_logging"])
        data["remote_access_enabled"] = bool(data["remote_access_enabled"])
        return data

    def set_station_settings(self, identity: str, data: dict[str, Any], updated_at: str) -> dict[str, Any]:
        current = self.get_station_settings(identity)
        allowed = {
            "friendly_name", "reference", "physical_id", "security_profile", "allowed_ip", "notes",
            "debug_logging", "location_name", "latitude", "longitude", "remote_access_url", "remote_access_enabled",
        }
        merged = {k: data.get(k, current.get(k)) for k in allowed}
        def _float_or_none(v: Any) -> float | None:
            if v in (None, ""):
                return None
            return float(v)
        lat = _float_or_none(merged["latitude"])
        lon = _float_or_none(merged["longitude"])
        self.execute(
            """INSERT INTO station_settings(
                identity,friendly_name,reference,physical_id,security_profile,allowed_ip,notes,debug_logging,
                location_name,latitude,longitude,remote_access_url,remote_access_enabled,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(identity) DO UPDATE SET
                friendly_name=excluded.friendly_name, reference=excluded.reference, physical_id=excluded.physical_id,
                security_profile=excluded.security_profile, allowed_ip=excluded.allowed_ip, notes=excluded.notes,
                debug_logging=excluded.debug_logging, location_name=excluded.location_name, latitude=excluded.latitude,
                longitude=excluded.longitude, remote_access_url=excluded.remote_access_url,
                remote_access_enabled=excluded.remote_access_enabled, updated_at=excluded.updated_at""",
            (
                identity, str(merged["friendly_name"] or "")[:100], str(merged["reference"] or "")[:100],
                str(merged["physical_id"] or "")[:100], str(merged["security_profile"] or "None")[:80],
                str(merged["allowed_ip"] or "")[:100], str(merged["notes"] or "")[:4000],
                1 if merged["debug_logging"] else 0, str(merged["location_name"] or "")[:255], lat, lon,
                str(merged["remote_access_url"] or "")[:500], 1 if merged["remote_access_enabled"] else 0, updated_at,
            ),
        )
        return self.get_station_settings(identity)

    def clear_messages(self, identity: str) -> int:
        cur = self.execute("DELETE FROM messages WHERE station_id=?", (identity,))
        return cur.rowcount

    def end_all_active_transactions(self, identity: str, ended_at: str) -> int:
        cur = self.execute(
            "UPDATE transactions SET status='Ended', ended_at=? WHERE station_id=? AND status='Active'",
            (ended_at, identity),
        )
        return cur.rowcount

