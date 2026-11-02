"""Application exception hierarchy."""


class ApplicationError(Exception):
    """Base domain application exception."""


class IntegrationError(ApplicationError):
    """Base integration/adapter exception."""


class GovernanceError(ApplicationError):
    """Base governance policy and approval exception."""


class InvalidWeatherEventError(ApplicationError):
    """Raised when a weather event is missing or unsupported."""


class UnsupportedSeverityError(ApplicationError):
    """Raised when a weather or risk severity level is invalid."""


class UnknownRegionError(ApplicationError):
    """Raised when an affected region is missing or unrecognized."""


class EmptyNetworkElementResultError(ApplicationError):
    """Raised when no network elements are available or enriched."""


class InventoryLookupError(IntegrationError):
    """Raised when an inventory adapter lookup fails."""


class InvalidNetworkRiskEventError(ApplicationError):
    """Raised when Agent 2 receives an invalid NetworkRiskEvent contract."""


class InvalidBoundingBoxError(InvalidNetworkRiskEventError):
    """Raised when affected_area geographic bounds are invalid (south >= north or west >= east)."""


class InvalidTimestampError(InvalidNetworkRiskEventError):
    """Raised when created_at is missing timezone awareness or cannot be parsed."""


class InvalidDataSourceError(InvalidNetworkRiskEventError):
    """Raised when data_source is not in synthetic, mock, or external."""


class RegionMismatchError(ApplicationError):
    """Raised when an enriched network element belongs to a different region than the event."""


class RemediationExecutionError(IntegrationError):
    """Raised when a controlled mock remediation execution fails."""
