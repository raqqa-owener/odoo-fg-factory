from __future__ import annotations

import os
import tempfile
import unittest


_TEMP_ROOT = tempfile.mkdtemp(prefix="odoo-fg-factory-test-")
os.environ.update(
    {
        "ARTIFACT_ROOT": os.path.join(_TEMP_ROOT, "artifacts"),
        "STORAGE_ROOT": os.path.join(_TEMP_ROOT, "storage"),
        "GENERATED_ADDONS_ROOT": os.path.join(_TEMP_ROOT, "generated-addons"),
        "CUSTOM_ADDONS_ROOT": os.path.join(_TEMP_ROOT, "custom-addons"),
        "NEO4J_APPLY_ENABLED": "false",
    }
)

from app.main import app, health  # noqa: E402


class HealthTest(unittest.TestCase):
    def test_health_is_safe_by_default(self) -> None:
        payload = health()

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["service"], "odoo-fg-factory-api")
        self.assertFalse(payload["neo4j_apply_enabled"])

    def test_routes_are_registered(self) -> None:
        paths = {route.path for route in app.routes}

        self.assertIn("/health", paths)
        self.assertIn("/p1/import-pack", paths)
        self.assertIn("/p5/internal-design/import", paths)


if __name__ == "__main__":
    unittest.main()
