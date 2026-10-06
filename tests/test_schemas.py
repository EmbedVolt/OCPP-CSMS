import unittest
from pathlib import Path

from csms.defaults import response_for, sample_from_schema
from csms.schema_registry import SchemaRegistry, VERSIONS


ROOT = Path(__file__).resolve().parents[1]


class SchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = SchemaRegistry(ROOT / "schemas")

    def test_all_versions_have_actions(self):
        for version in VERSIONS:
            self.assertGreater(len(self.registry.actions(version)), 20)

    def test_generated_request_templates_are_valid(self):
        failures = []
        for version in VERSIONS:
            for item in self.registry.actions(version):
                try:
                    schema = self.registry.get(version, item.action)
                    payload = sample_from_schema(schema)
                    self.registry.validate(version, item.action, payload)
                except Exception as exc:
                    failures.append(f"{version} {item.action}: {exc}")
        self.assertEqual([], failures, "\n".join(failures))

    def test_generated_responses_are_valid(self):
        failures = []
        for version in VERSIONS:
            for item in self.registry.actions(version):
                if not self.registry.has(version, item.action, response=True):
                    continue
                try:
                    schema = self.registry.get(version, item.action, response=True)
                    payload = response_for(version, item.action, schema)
                    self.registry.validate(version, item.action, payload, response=True)
                except Exception as exc:
                    failures.append(f"{version} {item.action}: {exc}")
        self.assertEqual([], failures, "\n".join(failures))


if __name__ == "__main__":
    unittest.main()
