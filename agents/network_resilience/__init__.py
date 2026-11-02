"""Agent 2 package exports."""

from importlib import import_module

from .action_service import ActionService, SyntheticVerificationService
from .recommendation_engine import RecommendationEngine
from .risk_analyzer import RiskAnalyzer, map_score_to_risk_level
from .service_impact import ServiceImpactAnalyzer

__all__ = [
    "ActionService",
    "NetworkResilienceAgent",
    "RecommendationEngine",
    "RiskAnalyzer",
    "ServiceImpactAnalyzer",
    "SyntheticVerificationService",
    "map_score_to_risk_level",
]


def __getattr__(name: str) -> object:
    if name == "NetworkResilienceAgent":
        return import_module("agents.network_resilience.agent").NetworkResilienceAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
