"""Local CLI entry point for Agent 2:

python -m agents.network_resilience.main --input samples/network_risk_event.json
"""

import argparse
import json
import sys
from typing import Any

from pydantic import ValidationError

from common.exceptions import ApplicationError
from common.logging import configure_logging
from contracts.governance_decision import ApprovalStatus

from .agent import NetworkResilienceAgent

DEFAULT_AGENT1_PAYLOAD: dict[str, Any] = {
    "schema_version": "0.1",
    "team_code": "TEAM_PENDING",
    "run_id": "WX-46329ae2b74b",
    "trace_id": "7a4106b675e4419f87b9b84d30a67ca4",
    "created_at": "2026-11-02T00:26:32.732111+00:00",
    "data_source": "synthetic",
    "weather_event": "SEVERE_STORM",
    "severity": "HIGH",
    "affected_region": "Demo-Region-A",
    "forecast": "Synthetic storm scenario for integration testing.",
    "affected_area": {
        "south": 12.8,
        "north": 13.2,
        "west": 77.4,
        "east": 77.8,
    },
    "affected_network_elements": [
        "NE-101",
        "NE-102",
    ],
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run Agent 2 (Network Resilience & Governed Action Agent) locally."
    )
    parser.add_argument(
        "--input",
        "--input-file",
        dest="input_file",
        help="Path to Agent 1 NetworkRiskEvent JSON file.",
    )
    parser.add_argument(
        "--json",
        dest="raw_json",
        help="Inline JSON string representing Agent 1 NetworkRiskEvent.",
    )
    parser.add_argument(
        "--approval-decision",
        dest="approval_decision",
        choices=["PENDING", "APPROVED", "REJECTED", "EXPIRED"],
        default=None,
        help="Optional simulated human approval decision for PAUSE workflows.",
    )
    parser.add_argument(
        "--approver",
        dest="approver_reference",
        default="demo-approver",
        help="Synthetic approver reference when simulating approval.",
    )
    args = parser.parse_args(argv)
    configure_logging()

    try:
        if args.input_file:
            with open(args.input_file, encoding="utf-8-sig") as handle:
                payload: dict[str, Any] = json.load(handle)
        elif args.raw_json:
            payload = json.loads(args.raw_json)
        else:
            payload = dict(DEFAULT_AGENT1_PAYLOAD)

        approval_decision = (
            ApprovalStatus(args.approval_decision) if args.approval_decision is not None else None
        )

        result = NetworkResilienceAgent().process(
            payload,
            simulated_approval_decision=approval_decision,
            approver_reference=args.approver_reference,
        )
        print(result.model_dump_json(indent=2))
        return 0
    except (
        ApplicationError,
        ValidationError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
    ) as exc:
        error_payload = {
            "status": "error",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
        print(json.dumps(error_payload, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
