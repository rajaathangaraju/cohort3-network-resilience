"""Audit and evidence recorder for every stage of Agent 2 execution."""

from datetime import UTC, datetime

from common.tracing import generate_prefixed_id
from contracts.action_result import EvidenceRecord

SENSITIVE_KEYWORDS = ("password", "secret", "token", "credential", "access_key", "session_token")


def _scrub_summary(summary: str) -> str:
    lowered = summary.lower()
    for word in SENSITIVE_KEYWORDS:
        if word in lowered:
            return "[REDACTED_SENSITIVE_CONTENT]"
    return summary


class AuditRecorder:
    """Captures ordered, immutable synthetic evidence records across workflow stages."""

    def __init__(self) -> None:
        self._records: list[EvidenceRecord] = []

    def record_stage(
        self,
        *,
        run_id: str,
        trace_id: str,
        event_type: str,
        stage: str,
        status: str,
        source: str,
        summary: str,
        synthetic: bool = True,
    ) -> EvidenceRecord:
        entry = EvidenceRecord(
            evidence_id=generate_prefixed_id("EVD"),
            run_id=run_id,
            trace_id=trace_id,
            event_type=event_type,
            stage=stage,
            status=status,
            recorded_at=datetime.now(UTC),
            source=source,
            summary=_scrub_summary(summary),
            synthetic=synthetic,
        )
        self._records.append(entry)
        return entry

    def record(self, event: EvidenceRecord) -> None:
        self._records.append(event)

    def get_records(self) -> list[EvidenceRecord]:
        return list(self._records)
