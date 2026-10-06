import unittest
from pathlib import Path

from csms.schema_registry import SchemaRegistry, VERSIONS
from csms.test_catalog import catalog_for

ROOT = Path(__file__).resolve().parents[1]


class LabFeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = SchemaRegistry(ROOT / "schemas")

    def test_catalog_covers_every_request_action(self):
        for version in VERSIONS:
            actions = {x.action for x in self.registry.actions(version)}
            catalog = {x["action"] for x in catalog_for(self.registry, version)}
            self.assertEqual(actions, catalog)

    def test_generated_live_test_payloads_validate(self):
        failures = []
        for version in VERSIONS:
            for item in catalog_for(self.registry, version):
                try:
                    self.registry.validate(version, item["action"], item["payload"])
                except Exception as exc:
                    failures.append(f"{version} {item['action']}: {exc}")
        self.assertEqual([], failures, "\n".join(failures))

    def test_expected_action_counts(self):
        self.assertEqual(39, len(catalog_for(self.registry, "1.6")))
        self.assertEqual(64, len(catalog_for(self.registry, "2.0.1")))
        self.assertEqual(90, len(catalog_for(self.registry, "2.1")))


if __name__ == "__main__":
    unittest.main()
