"""Cohort 3 Agent 1 forecast prototype (standard library; repository targets Python 3.13+).

Separate CLI: does not replace agent.py/main.py or the shared NetworkRiskEvent.
Its agent1-handoff-0.1 output is a DRAFT, not accepted by the shared Pydantic
contract. See README_PROTOTYPE.md before integrating with Agent 2.

Run locally or in CloudShell:
    python forecast_prototype.py --mode demo
    python forecast_prototype.py --mode live --team-code YOUR_TEAM_CODE
    python forecast_prototype.py --mode live --inventory inventory.json --inventory-source synthetic

Inventory: JSON list of objects with ne_id, latitude, longitude, and optional
element_type, criticality, associated_service_ids. External inventories require
--inventory-source with an accurate provenance label. Default inventory is synthetic.

Outputs: a new directory under ./runs containing inventory.json, assessment.json,
agent2_event.json, audit.jsonl, and weather inputs. Nothing is sent to Agent 2.
Do not commit generated runs or credentials to SVN. Commit this source file.

Live mode calls Open-Meteo once per network element. Free endpoint use is subject
to https://open-meteo.com/en/terms (non-commercial use and attribution).
Forecast data is not an official weather alert. Demo thresholds are unvalidated.
This file does not implement an LLM, AgentCore hosting, governance, remediation,
CloudWatch, or agent-to-agent transport. It consolidates the CloudShell prototype;
it is not a downloaded copy of the CloudShell files or their historic evidence.
"""

import argparse
import json
import math
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

INVENTORY = [
    {"ne_id": "NE-101", "latitude": 12.97, "longitude": 77.59,
     "element_type": "CELL_SITE", "criticality": "HIGH",
     "associated_service_ids": ["SVC-DEMO-001", "SVC-DEMO-002"]},
    {"ne_id": "NE-102", "latitude": 13.05, "longitude": 77.65,
     "element_type": "BACKHAUL_LINK", "criticality": "MEDIUM",
     "associated_service_ids": ["SVC-DEMO-002"]},
    {"ne_id": "NE-103", "latitude": 13.50, "longitude": 78.10,
     "element_type": "CELL_SITE", "criticality": "LOW",
     "associated_service_ids": ["SVC-DEMO-003"]},
]
DEMO_PEAKS = {"NE-101": (12.0, 65.0), "NE-102": (6.0, 45.0),
              "NE-103": (1.0, 20.0)}
RULE = {
    "id": "demo-weather-risk-v1", "validated": False,
    "high": {"hourly_precipitation_mm": 10, "gust_kmh": 60},
    "medium": {"hourly_precipitation_mm": 5, "gust_kmh": 40},
    "logic": "Either threshold in any returned forecast hour",
    "meaning": "Weather exposure screening; not service impact",
}


def now():
    return datetime.now(UTC).isoformat()


def save(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")


def valid_number(value, low, high):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and low <= value <= high)


def validate_inventory(items):
    if not isinstance(items, list) or not items:
        raise ValueError("Inventory must be a non-empty JSON list")
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Each inventory entry must be an object")
        identifier = item.get("ne_id")
        if not isinstance(identifier, str) or not identifier.strip() or identifier in seen:
            raise ValueError("ne_id must be non-empty and unique")
        seen.add(identifier)
        for key, limit in (("latitude", 90), ("longitude", 180)):
            if not valid_number(item.get(key), -limit, limit):
                raise ValueError(f"Invalid {key} for {identifier}")
        services = item.get("associated_service_ids", [])
        if not isinstance(services, list) or any(
                not isinstance(s, str) or not s.strip() for s in services):
            raise ValueError(f"Invalid associated_service_ids for {identifier}")
        for key in ("element_type", "criticality"):
            if key in item and (not isinstance(item[key], str) or not item[key].strip()):
                raise ValueError(f"Invalid {key} for {identifier}")


def classify(rain, gust):
    for name in ("high", "medium"):
        threshold = RULE[name]
        if rain >= threshold["hourly_precipitation_mm"] or gust >= threshold["gust_kmh"]:
            return name.upper()
    return "LOW"


def forecast_peaks(data):
    hourly, units = data["hourly"], data["hourly_units"]
    if units["precipitation"] != "mm" or units["wind_gusts_10m"] != "km/h":
        raise ValueError("Unexpected forecast units")
    times = hourly["time"]
    if len(times) != 24:
        raise ValueError("Expected 24 hourly forecasts")
    parsed = [datetime.fromisoformat(t) for t in times]
    if any((b - a).total_seconds() != 3600 for a, b in zip(parsed, parsed[1:])):
        raise ValueError("Forecast times are not consecutive hours")
    for key in ("precipitation", "wind_gusts_10m"):
        values = hourly[key]
        if len(values) != 24 or any(not valid_number(v, 0, float("inf")) for v in values):
            raise ValueError(f"Invalid or missing {key}")
    return max(hourly["precipitation"]), max(hourly["wind_gusts_10m"])


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("demo", "live"), default="demo")
    parser.add_argument("--team-code", default="TEAM_PENDING")
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--inventory-source")
    parser.add_argument("--output-dir", type=Path, default=Path("runs"))
    args = parser.parse_args()
    if not args.team_code.strip():
        parser.error("Team code must not be empty")
    if args.inventory and not (args.inventory_source or "").strip():
        parser.error("External inventory requires --inventory-source")
    if args.mode == "demo" and args.inventory:
        parser.error("The built-in demo uses only the built-in synthetic inventory")
    try:
        items = json.loads(args.inventory.read_text(encoding="utf-8")) if args.inventory else INVENTORY
        validate_inventory(items)
    except (OSError, ValueError) as error:
        parser.error(str(error))

    run_id = ("WX-DEMO-" if args.mode == "demo" else "WX-") + uuid.uuid4().hex[:12]
    trace_id = uuid.uuid4().hex
    folder = args.output_dir / run_id
    folder.mkdir(parents=True, exist_ok=False)
    save(folder / "inventory.json", items)

    def audit(step, **details):
        record = dict(timestamp=now(), team_code=args.team_code, run_id=run_id,
                      trace_id=trace_id, step=step, details=details)
        with (folder / "audit.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, allow_nan=False) + "\n")

    results = []
    for index, ne in enumerate(items):
        item = {"network_element": ne, "risk": "UNKNOWN"}
        if args.mode == "demo":
            rain, gust = DEMO_PEAKS[ne["ne_id"]]
            item["input_type"] = "synthetic_forecast_peaks"
        else:
            params = dict(latitude=ne["latitude"], longitude=ne["longitude"],
                          hourly="precipitation,wind_gusts_10m", wind_speed_unit="kmh",
                          timezone="UTC", forecast_hours=24)
            url = "https://api.open-meteo.com/v1/forecast?" + urlencode(params)
            item["request_url"] = url
            try:
                with urlopen(url, timeout=30) as response:
                    raw = response.read()
                filename = f"forecast-{index + 1}.json"
                (folder / filename).write_bytes(raw)
                item.update(raw_forecast_file=filename, retrieved_at=now())
                data = json.loads(raw)
                rain, gust = forecast_peaks(data)
                item.update(forecast_start_utc=data["hourly"]["time"][0],
                            forecast_last_hour_utc=data["hourly"]["time"][-1],
                            provider_grid_coordinates={"latitude": data["latitude"],
                                                       "longitude": data["longitude"]})
            except Exception as error:
                item["error"] = str(error)
        if "error" not in item:
            item.update(risk=classify(rain, gust), max_hourly_precipitation_mm=rain,
                        max_wind_gust_kmh=gust)
        results.append(item)
        audit("ELEMENT_WEATHER_ASSESSED", ne_id=ne["ne_id"], risk=item["risk"],
              mode=args.mode, error=item.get("error"))
        print(f"{ne['ne_id']}: {item['risk']}")

    if args.mode == "demo":
        save(folder / "synthetic_inputs.json", {
            key: {"rain_mm": pair[0], "gust_kmh": pair[1]} for key, pair in DEMO_PEAKS.items()})
    elevated = [x["network_element"]["ne_id"] for x in results if x["risk"] in {"HIGH", "MEDIUM"}]
    unknown = [x["network_element"]["ne_id"] for x in results if x["risk"] == "UNKNOWN"]
    status = "INCOMPLETE" if unknown else (
        "ELEVATED_WEATHER_RISK" if elevated else "NO_ELEVATED_WEATHER_RISK")
    sources = {
        "weather": "synthetic demo scenario; not a live forecast" if args.mode == "demo"
                   else "Open-Meteo live model forecast",
        "inventory": args.inventory_source if args.inventory else "synthetic",
        "attribution": "Team-generated synthetic test data" if args.mode == "demo"
                       else "Weather data by Open-Meteo (CC BY 4.0)",
    }
    common = dict(team_code=args.team_code, run_id=run_id, trace_id=trace_id,
                  created_at=now(), official_warning=False, delivery_status="NOT_SENT")
    save(folder / "assessment.json", dict(
        common, schema_version="weather-assessment-0.1", weather_source=sources["weather"],
        inventory_source=sources["inventory"], attribution=sources["attribution"],
        rule=RULE, elements=results, assessment_status="INCOMPLETE" if unknown else "COMPLETE"))
    event = dict(
        common, schema_version="agent1-handoff-0.1", event_type="WEATHER_NETWORK_ASSESSMENT",
        producer="weather-network-agent", consumer="network-resilience-agent",
        assessment_status=status, sources=sources, risk_rule=RULE,
        elevated_risk_network_elements=elevated, unknown_risk_network_elements=unknown,
        element_assessments=results, action_requested=False,
        evidence={"scope": "local_run_directory", "assessment_file": "assessment.json",
                  "audit_file": "audit.jsonl"})
    save(folder / "agent2_event.json", event)
    audit("AGENT2_HANDOFF_PREPARED", assessment_status=status, delivery_status="NOT_SENT")
    print(f"Status: {status}\nSaved: {folder / 'agent2_event.json'}\nDelivery: NOT_SENT")
    return 2 if unknown else 0


if __name__ == "__main__":
    raise SystemExit(main())
