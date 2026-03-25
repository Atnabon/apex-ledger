"""
Registered upcasters for the Apex Ledger event store.

Each upcaster transforms an old event payload to its next version.
Inference strategies are documented inline per the DOMAIN_NOTES.md requirements.
"""
from __future__ import annotations
from datetime import datetime
from src.upcasting.registry import registry


# ---------------------------------------------------------------------------
# CreditAnalysisCompleted v1 -> v2
# ---------------------------------------------------------------------------
# v1 had: application_id, agent_id, session_id, risk_tier, recommended_limit_usd,
#          analysis_duration_ms, input_data_hash
# v2 adds: model_version, confidence_score, regulatory_basis

@registry.register("CreditAnalysisCompleted", from_version=1)
def upcast_credit_analysis_v1_to_v2(payload: dict) -> dict:
    recorded_at = payload.get("recorded_at", "2024-01-01T00:00:00Z")
    return {
        **payload,
        # Inference: derive model version from timestamp
        # ~5% error rate during model rollover periods
        "model_version": _infer_model_version(recorded_at),
        # Genuinely unknown — do NOT fabricate
        # Null forces consumers to handle missing case explicitly
        # Fabricating 0.5 would pass confidence floor (Rule 4: < 0.6 -> REFER)
        "confidence_score": None,
        # Inference: regulations active at the time
        # ~2% error rate during regulation transition periods
        "regulatory_basis": _infer_regulatory_basis(recorded_at),
    }


# ---------------------------------------------------------------------------
# DecisionGenerated v1 -> v2
# ---------------------------------------------------------------------------
# v1 had: application_id, orchestrator_agent_id, recommendation, decision_basis_summary
# v2 adds: confidence_score, contributing_agent_sessions, model_versions

@registry.register("DecisionGenerated", from_version=1)
def upcast_decision_v1_to_v2(payload: dict) -> dict:
    return {
        **payload,
        # Genuinely unknown — orchestrator confidence not tracked in v1
        "confidence_score": None,
        # Empty list — session references not tracked in v1
        # Performance note: reconstructing from store would require O(N) lookups
        # per historical event, which is prohibitive for bulk replay
        "contributing_agent_sessions": payload.get("contributing_agent_sessions", []),
        # Empty dict — model versions not tracked in v1
        "model_versions": payload.get("model_versions", {}),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _infer_model_version(recorded_at: str) -> str:
    """Infer model version from the event's recorded_at timestamp."""
    try:
        if isinstance(recorded_at, datetime):
            dt = recorded_at
        else:
            dt = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
        if dt.year < 2025:
            return "legacy-pre-2025"
        elif dt.year == 2025:
            return "v1.0-2025"
        return "v2.0-2026"
    except (ValueError, TypeError):
        return "legacy-unknown"


def _infer_regulatory_basis(recorded_at: str) -> str:
    """Infer which regulation set was active when the event was recorded."""
    try:
        if isinstance(recorded_at, datetime):
            dt = recorded_at
        else:
            dt = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
        if dt.year < 2025:
            return "OCC-2024-Q4"
        elif dt < datetime(2026, 1, 1, tzinfo=dt.tzinfo):
            return "OCC-2025-ANNUAL"
        return "OCC-2026-Q1"
    except (ValueError, TypeError):
        return "OCC-UNKNOWN"
