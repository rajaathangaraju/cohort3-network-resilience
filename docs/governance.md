# Governance, Approval, and Audit Model

Governance logic is isolated in `governance/` and never embedded inside Agent 2's risk, impact, or recommendation calculations.

## Governance Decisions (`GovernanceDecision`)

1. **`ALLOW`**
   - Issued only for non-state-changing monitoring recommendations (`state_changing=False` and `human_approval_required=False`).
   - Approval status is `NOT_REQUIRED`, and controlled mock execution may proceed immediately.

2. **`PAUSE`**
   - Issued whenever a recommendation is policy-eligible but `state_changing=True` or `human_approval_required=True`.
   - Initial approval status is `PENDING`, execution status is `BLOCKED`, and verification status is `NOT_STARTED`.
   - **Critical Invariant**: A state-changing recommendation can never transition directly from `ALLOW` to `EXECUTED`. It must pass through `PAUSE -> Human Approval (APPROVED) -> Controlled Action (EXECUTED)`.

3. **`DENY`**
   - Issued when any hard-stop rule in `governance/deny_rules.py` matches (e.g., unsupported action, empty targets, incomplete/unresolved inventory on automated or state-changing actions, or explicit policy override).
   - Approval status is `NOT_REQUIRED`, execution is `BLOCKED`, verification is `BLOCKED`, and denial reasons + rule IDs are recorded in audit evidence.

## Human Approval Transitions (`ApprovalWorkflow`)

- `PENDING`: Execution remains `BLOCKED`; verification is `NOT_STARTED`.
- `APPROVED`: Requires an explicit synthetic `approver_reference` (e.g., `demo-approver`); permits controlled mock remediation and synthetic verification.
- `REJECTED` / `EXPIRED`: Execution remains `BLOCKED` and verification is `BLOCKED`.

## Evidence & Audit (`AuditRecorder`)

Every Agent 2 run records 10 ordered `EvidenceRecord` items with `run_id`, `trace_id`, `evidence_id`, `event_type`, `stage`, `status`, `recorded_at`, `source`, `summary`, and `synthetic=true`:
1. `input_received`
2. `input_validation`
3. `inventory_enrichment`
4. `service_impact_analysis`
5. `risk_assessment`
6. `recommendation`
7. `governance_decision`
8. `human_approval`
9. `action_execution`
10. `verification`
