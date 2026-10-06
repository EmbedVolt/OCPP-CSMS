import tempfile
import unittest
from pathlib import Path

from csms.app import _dashboard_metrics
from csms.db import Database


class StationPortalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.sqlite3")
        self.db.station_connected("CP-1", "1.6", "2026-10-05T08:00:00Z", "127.0.0.1")

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def test_station_settings_round_trip(self):
        saved = self.db.set_station_settings("CP-1", {
            "friendly_name": "Bench charger",
            "reference": "LAB-01",
            "debug_logging": True,
            "latitude": "51.0",
            "longitude": "5.0",
        }, "2026-10-05T08:01:00Z")
        self.assertEqual("Bench charger", saved["friendly_name"])
        self.assertEqual("LAB-01", saved["reference"])
        self.assertTrue(saved["debug_logging"])
        self.assertEqual(51.0, saved["latitude"])

    def test_dashboard_metrics_extract_status_and_meter_data(self):
        self.db.add_message(
            ts="2026-10-05T08:02:00Z", station_id="CP-1", protocol="1.6",
            direction="Station → CSMS", message_type="CALL", unique_id="1",
            action="StatusNotification", payload={"connectorId": 1, "status": "Charging", "errorCode": "NoError"},
        )
        self.db.add_message(
            ts="2026-10-05T08:03:00Z", station_id="CP-1", protocol="1.6",
            direction="Station → CSMS", message_type="CALL", unique_id="2",
            action="MeterValues", payload={"connectorId": 1, "meterValue": [{
                "timestamp": "2026-10-05T08:03:00Z",
                "sampledValue": [
                    {"value": "16", "measurand": "Current.Import", "unit": "A"},
                    {"value": "3.68", "measurand": "Power.Active.Import", "unit": "kW"},
                    {"value": "1250", "measurand": "Energy.Active.Import.Register", "unit": "Wh"},
                ],
            }]},
        )
        metrics = _dashboard_metrics(self.db, "CP-1")
        self.assertEqual({"Charging": 1}, metrics["statusCounts"])
        self.assertEqual("Charging", metrics["statusHistory"][0]["status"])
        self.assertEqual(16.0, metrics["meterSeries"][0]["current_a"])
        self.assertEqual(3.68, metrics["meterSeries"][0]["power_kw"])
        self.assertEqual(1.25, metrics["meterSeries"][0]["energy_kwh"])


if __name__ == "__main__":
    unittest.main()
