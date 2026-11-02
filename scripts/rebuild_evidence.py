import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

RUNS = {
    "WX-ALLOW-4D2494BB": (
        "ALLOW",
        "NOT_REQUIRED",
        "EXECUTED",
        False,
        False,
        False,
        "PASSED",
    ),
    "WX-DENY-5614F523": (
        "DENY",
        "NOT_REQUIRED",
        "BLOCKED",
        False,
        False,
        True,
        "BLOCKED",
    ),
    "WX-PAUSE-0C07E286": (
        "PAUSE",
        "APPROVED",
        "EXECUTED",
        True,
        True,
        True,
        "PASSED",
    ),
}
NAMES = ("agent1-response.json", "agent2-response.json", "workflow-summary.json")
ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "evidence/manifest.json"


def require(condition: object, message: str) -> None:
    if not condition:
        raise ValueError(message)


def read(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return data


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--freeze", action="store_true")
    args = parser.parse_args()

    files = [
        Path("evidence") / run / name
        for run in RUNS
        for name in NAMES
    ]
    for relative in files:
        require((ROOT / relative).is_file(), f"Missing: {relative}")

    if args.freeze:
        require(not MANIFEST.exists(), "Manifest exists; refusing to overwrite")
        manifest = {
            "purpose": "Post-run integrity snapshot; not a pre-run declaration",
            "sha256": {
                p.as_posix(): digest(ROOT / p) for p in files
            },
        }
        with MANIFEST.open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, indent=2)
        print("Manifest saved. Preserve this reviewed copy separately.")
        return

    hashes = read(MANIFEST)["sha256"]
    require(
        set(hashes) == {p.as_posix() for p in files},
        "Manifest file list differs from expected evidence",
    )
    for relative in files:
        require(
            digest(ROOT / relative) == hashes[relative.as_posix()],
            f"Integrity failure: {relative}",
        )

    for run, expected in RUNS.items():
        folder = ROOT / "evidence" / run
        a1, a2, summary = [read(folder / name) for name in NAMES]
        trace = a1["trace_id"]
        require(
            isinstance(trace, str) and bool(trace.strip()),
            f"{run}: missing trace ID",
        )
        for document in (a1, a2, summary):
            require(document["run_id"] == run, f"{run}: run ID mismatch")
            require(document["trace_id"] == trace, f"{run}: trace ID mismatch")

        for key in (
            "weather_event",
            "severity",
            "affected_region",
            "affected_area",
            "forecast",
            "affected_network_elements",
        ):
            require(
                a1[key] == a2["input_context"][key],
                f"{run}: Agent 1/2 input mismatch: {key}",
            )

        for key in (
            "risk_assessment",
            "recommendation",
            "governance",
            "approval",
            "execution",
            "verification",
            "ai_reasoning",
        ):
            require(
                summary[key] == a2[key],
                f"{run}: summary differs from Agent 2: {key}",
            )

        execution = a2["execution"]
        recommendation = a2["recommendation"]
        for value in (
            execution["changed_state"],
            execution["synthetic"],
            recommendation["state_changing"],
            recommendation["human_approval_required"],
        ):
            require(type(value) is bool, f"{run}: expected boolean")

        actual = (
            a2["governance"]["decision"],
            a2["approval"]["status"],
            execution["status"],
            execution["changed_state"],
            recommendation["state_changing"],
            recommendation["human_approval_required"],
            a2["verification"]["status"],
        )
        require(actual == expected, f"{run}: unexpected outcome: {actual}")
        require(execution["synthetic"] is True, f"{run}: unexpected action source")

        if actual[0] == "DENY":
            require(
                actual[2] == "BLOCKED" and actual[3] is False,
                f"{run}: DENY did not block action",
            )
        if actual[2] == "EXECUTED" and (actual[4] or actual[5] or actual[3]):
            require(actual[1] == "APPROVED", f"{run}: approval missing")

        print(f"PASS {run}: {actual[0]} / {actual[1]} / {actual[2]}")

    print("PASS: 3/3 recorded workflow outcomes and evidence consistency checks.")
    print(
        "Scope: saved synthetic results; not independent risk recalculation "
        "or proof of live AWS execution."
    )


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SystemExit(f"FAIL: {error}") from error
