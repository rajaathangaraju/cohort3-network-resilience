"""Local CLI for Agent 1: python -m agents.weather_network_intelligence.main."""

import argparse
import json
import sys

from common.logging import configure_logging

from .agent import WeatherNetworkIntelligenceAgent

DEFAULT_INPUT = {
    "run_id": "WX-0042",
    "trace_id": "TRACE-0042",
    "weather_event": "SEVERE_STORM",
    "severity": "HIGH",
    "affected_region": "Region-A",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-file")
    parser.add_argument("--json")
    args = parser.parse_args()
    configure_logging()
    if args.input_file:
        with open(args.input_file, encoding="utf-8") as source:
            payload = json.load(source)
    elif args.json:
        payload = json.loads(args.json)
    else:
        payload = DEFAULT_INPUT
    event = WeatherNetworkIntelligenceAgent().process(payload)
    print(event.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
