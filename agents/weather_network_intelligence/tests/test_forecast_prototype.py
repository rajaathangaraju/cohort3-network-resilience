"""Offline prototype tests; no AWS access or third-party test packages needed."""
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "forecast_prototype.py"
SPEC = importlib.util.spec_from_file_location("forecast_prototype_under_test", MODULE_PATH)
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)


def forecast(rain=1.3, gust=32.4):
    start = datetime(2026, 11, 2)
    return {
        "latitude": 12.97, "longitude": 77.59,
        "hourly_units": {"precipitation": "mm", "wind_gusts_10m": "km/h"},
        "hourly": {
            "time": [(start + timedelta(hours=i)).isoformat() for i in range(24)],
            "precipitation": [rain] * 24, "wind_gusts_10m": [gust] * 24,
        },
    }


class ForecastPrototypeTests(unittest.TestCase):
    def execute(self, mode, provider=None):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            argv = [str(MODULE_PATH), "--mode", mode, "--team-code", "TEAM_TEST",
                    "--output-dir", str(root)]
            with patch.object(sys, "argv", argv), patch.object(
                agent, "urlopen", side_effect=provider or AssertionError("Unexpected network call")
            ) as http, contextlib.redirect_stdout(io.StringIO()):
                code = agent.main()
            folder, = root.iterdir()
            event = json.loads((folder / "agent2_event.json").read_text())
            records = [json.loads(line) for line in (folder / "audit.jsonl").read_text().splitlines()]
            files = {file.name: file.read_text() for file in folder.iterdir()}
            return code, event, records, files, http.call_count

    def test_demo_is_offline_and_keeps_action_disabled(self):
        code, event, records, files, calls = self.execute("demo")
        self.assertEqual((code, calls), (0, 0))
        self.assertEqual(event["elevated_risk_network_elements"], ["NE-101", "NE-102"])
        self.assertEqual([x["risk"] for x in event["element_assessments"]], ["HIGH", "MEDIUM", "LOW"])
        self.assertFalse(event["action_requested"])
        self.assertFalse(event["official_warning"])
        self.assertEqual(event["delivery_status"], "NOT_SENT")
        self.assertEqual(len(records), 4)
        self.assertTrue(all(r["trace_id"] == event["trace_id"] for r in records))
        self.assertTrue(all(r["run_id"] == event["run_id"] for r in records))
        self.assertIn("synthetic_inputs.json", files)

    def test_live_low_uses_point_forecasts_and_saves_evidence(self):
        urls = []

        def provider(url, timeout):
            urls.append(url)
            self.assertEqual(timeout, 30)
            return io.BytesIO(json.dumps(forecast()).encode())

        code, event, _, files, calls = self.execute("live", provider)
        self.assertEqual((code, calls), (0, 3))
        self.assertEqual(len(set(urls)), 3)
        self.assertTrue(all("forecast_hours=24" in url for url in urls))
        self.assertEqual(event["assessment_status"], "NO_ELEVATED_WEATHER_RISK")
        self.assertEqual(event["elevated_risk_network_elements"], [])
        self.assertEqual(event["sources"]["inventory"], "synthetic")
        self.assertEqual(len([n for n in files if n.startswith("forecast-")]), 3)

    def test_request_failure_is_unknown_not_low(self):
        def provider(*args, **kwargs):
            raise TimeoutError("Simulated timeout")

        code, event, _, _, calls = self.execute("live", provider)
        self.assertEqual((code, calls), (2, 3))
        self.assertEqual(event["assessment_status"], "INCOMPLETE")
        self.assertEqual(len(event["unknown_risk_network_elements"]), 3)
        self.assertFalse(event["action_requested"])

    def test_partial_failure_retains_known_elevated_elements(self):
        count = 0

        def provider(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("Simulated outage")
            return io.BytesIO(json.dumps(forecast(rain=12 if count == 1 else 0)).encode())

        code, event, _, _, _ = self.execute("live", provider)
        self.assertEqual(code, 2)
        self.assertEqual(event["elevated_risk_network_elements"], ["NE-101"])
        self.assertEqual(event["unknown_risk_network_elements"], ["NE-102"])
        self.assertEqual(event["assessment_status"], "INCOMPLETE")

    def test_invalid_forecast_does_not_create_low_risk(self):
        data = forecast()
        data["hourly"]["precipitation"][0] = None
        code, event, _, files, _ = self.execute(
            "live", lambda *a, **k: io.BytesIO(json.dumps(data).encode()))
        self.assertEqual(code, 2)
        self.assertTrue(all(x["risk"] == "UNKNOWN" for x in event["element_assessments"]))
        self.assertIn("forecast-1.json", files)

    def test_weather_threshold_boundaries(self):
        for rain, gust, expected in [(0, 0, "LOW"), (4.99, 39.99, "LOW"),
                                     (5, 0, "MEDIUM"), (0, 40, "MEDIUM"),
                                     (10, 0, "HIGH"), (0, 60, "HIGH")]:
            with self.subTest(rain=rain, gust=gust):
                self.assertEqual(agent.classify(rain, gust), expected)

    def test_inventory_rejects_duplicates_invalid_coordinates_and_services(self):
        invalid = [[], [agent.INVENTORY[0], agent.INVENTORY[0]]]
        for value in (100, True, float("nan")):
            invalid.append([dict(agent.INVENTORY[0], latitude=value)])
        invalid.append([dict(agent.INVENTORY[0], associated_service_ids="SVC-1")])
        for items in invalid:
            with self.subTest(items=items), self.assertRaises(ValueError):
                agent.validate_inventory(items)

    def test_forecast_rejects_units_missing_hours_and_duplicate_times(self):
        bad_units = forecast()
        bad_units["hourly_units"]["wind_gusts_10m"] = "mph"
        short = forecast()
        short["hourly"]["time"].pop()
        duplicate = forecast()
        duplicate["hourly"]["time"][1] = duplicate["hourly"]["time"][0]
        for data in (bad_units, short, duplicate):
            with self.assertRaises(ValueError):
                agent.forecast_peaks(data)

    def test_external_inventory_requires_provenance(self):
        with patch.object(sys, "argv", [str(MODULE_PATH), "--mode", "live",
                                       "--inventory", "unread.json"]), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            agent.main()
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
