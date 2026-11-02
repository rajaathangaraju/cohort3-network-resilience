# Data Sources

All data in the current implementation is **SYNTHETIC / MOCK DATA** used for development and integration testing. Nothing in this repository represents real telecom, customer, production network, or live weather-provider data.

## 1. Agent 1 Weather & Region Inventory (`MockInventoryAdapter`)

- `Region-A`: `["NE-101", "NE-102", "NE-103"]`
- `Demo-Region-A`: `["NE-101", "NE-102"]`
- `Region-B`: `["NE-201", "NE-202"]`
- `Region-C`: `["NE-301", "NE-302", "NE-303", "NE-304"]`

## 2. Agent 2 Synthetic Element Enrichment Catalog (`MockNetworkInventoryAdapter`)

Accessed through the `NetworkInventoryPort` protocol in `integrations/inventory_api.py`:

- `NE-101`: `type=CELL_SITE`, `region=Demo-Region-A`, `criticality=CRITICAL`, `services=["MOBILE_DATA", "VOICE"]`, `redundancy_available=true`, `operational_status=ACTIVE`, `synthetic=true`
- `NE-102`: `type=ROUTER`, `region=Demo-Region-A`, `criticality=HIGH`, `services=["IP_TRANSIT", "ENTERPRISE_CONNECTIVITY"]`, `redundancy_available=true`, `operational_status=ACTIVE`, `synthetic=true`
- `NE-103`: `type=FIBER_AGGREGATION`, `region=Demo-Region-A`, `criticality=MEDIUM`, `services=["BACKHAUL"]`, `redundancy_available=true`, `operational_status=ACTIVE`, `synthetic=true`
- `NE-201`: `type=EDGE_SWITCH`, `region=Region-B`, `criticality=LOW`, `services=["INTERNAL_TELEMETRY"]`, `redundancy_available=true`, `operational_status=ACTIVE`, `synthetic=true`
- `NE-202`: `type=MICROWAVE_LINK`, `region=Region-B`, `criticality=MEDIUM`, `services=["REGIONAL_VOICE"]`, `redundancy_available=true`, `operational_status=ACTIVE`, `synthetic=true`
- `NE-301`: `type=CORE_ROUTER`, `region=Region-C`, `criticality=HIGH`, `services=["ENTERPRISE_VPN", "SIP_TRUNKING"]`, `redundancy_available=false`, `operational_status=ACTIVE`, `synthetic=true`

Unresolved element IDs are never silently dropped; they are returned in `unresolved_network_elements` and recorded in audit evidence.

## 3. Controlled Mock Remediation & Verification

- `MockRemediationAdapter` simulates `START_ENHANCED_MONITORING`, `SIMULATE_TRAFFIC_REROUTE`, and `SIMULATE_NETWORK_ELEMENT_PROTECTION` with `synthetic=true`.
- `SyntheticVerificationService` returns synthetic verification outcomes (`NOT_STARTED`, `PENDING`, `PASSED`, `FAILED`, `BLOCKED`) with `synthetic=true`.
