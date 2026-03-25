"""
ApplicationSummary projection — one row per application with current state.

Maintains a denormalized read model in the `application_summary` table,
updated by consuming LoanApplication lifecycle events.
"""
import logging
from datetime import datetime
from typing import Any

import asyncpg

from src.models.events import StoredEvent

logger = logging.getLogger(__name__)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS application_summary (
    application_id TEXT PRIMARY KEY,
    state TEXT,
    applicant_id TEXT,
    requested_amount_usd NUMERIC,
    approved_amount_usd NUMERIC,
    risk_tier TEXT,
    fraud_score NUMERIC,
    compliance_status TEXT,
    decision TEXT,
    agent_sessions_completed TEXT[],
    last_event_type TEXT,
    last_event_at TIMESTAMPTZ,
    human_reviewer_id TEXT,
    final_decision_at TIMESTAMPTZ
);
"""


class ApplicationSummaryProjection:
    name: str = "ApplicationSummary"
    event_types: list[str] = [
        "ApplicationSubmitted",
        "CreditAnalysisRequested",
        "CreditAnalysisCompleted",
        "FraudScreeningCompleted",
        "ComplianceCheckRequested",
        "ComplianceRulePassed",
        "ComplianceRuleFailed",
        "DecisionGenerated",
        "HumanReviewCompleted",
        "ApplicationApproved",
        "ApplicationDeclined",
        "HumanReviewRequested",
    ]

    async def handle(self, event: StoredEvent, conn: asyncpg.Connection) -> None:
        """Route event to the appropriate handler and upsert into application_summary."""
        payload = event.payload
        application_id = payload.get("application_id") or event.stream_id

        handler = getattr(self, f"_handle_{event.event_type}", None)
        if handler is None:
            logger.warning(f"No handler for event type: {event.event_type}")
            return

        updates = handler(payload)
        if not updates:
            return

        # Always update last_event_type and last_event_at
        updates["last_event_type"] = event.event_type
        updates["last_event_at"] = event.recorded_at

        await self._upsert(conn, application_id, updates)

    async def rebuild(self, conn: asyncpg.Connection) -> None:
        """Truncate the projection table so the daemon can replay from position 0."""
        await conn.execute("TRUNCATE TABLE application_summary")
        logger.info("ApplicationSummary projection table truncated for rebuild.")

    # ------------------------------------------------------------------
    # Per-event-type handlers — each returns a dict of column updates
    # ------------------------------------------------------------------

    def _handle_ApplicationSubmitted(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "state": "SUBMITTED",
            "applicant_id": payload.get("applicant_id"),
            "requested_amount_usd": payload.get("requested_amount_usd"),
        }

    def _handle_CreditAnalysisRequested(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "state": "AWAITING_ANALYSIS",
        }

    def _handle_CreditAnalysisCompleted(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "state": "ANALYSIS_COMPLETE",
            "risk_tier": payload.get("risk_tier"),
        }

    def _handle_FraudScreeningCompleted(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "fraud_score": payload.get("fraud_score"),
        }

    def _handle_ComplianceCheckRequested(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "state": "COMPLIANCE_REVIEW",
            "compliance_status": "PENDING",
        }

    def _handle_ComplianceRulePassed(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "compliance_status": "PASSING",
        }

    def _handle_ComplianceRuleFailed(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "compliance_status": "FAILED",
        }

    def _handle_DecisionGenerated(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "state": "PENDING_DECISION",
            "decision": payload.get("recommendation"),
            "agent_sessions_completed": payload.get("contributing_agent_sessions", []),
        }

    def _handle_HumanReviewRequested(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "state": "PENDING_HUMAN_REVIEW",
            "human_reviewer_id": payload.get("assigned_to"),
        }

    def _handle_HumanReviewCompleted(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "human_reviewer_id": payload.get("reviewer_id"),
            "decision": payload.get("final_decision"),
        }

    def _handle_ApplicationApproved(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "state": "FINAL_APPROVED",
            "approved_amount_usd": payload.get("approved_amount_usd"),
            "decision": "APPROVED",
            "final_decision_at": datetime.utcnow(),
        }

    def _handle_ApplicationDeclined(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "state": "FINAL_DECLINED",
            "decision": "DECLINED",
            "final_decision_at": datetime.utcnow(),
        }

    # ------------------------------------------------------------------
    # Upsert helper
    # ------------------------------------------------------------------

    @staticmethod
    async def _upsert(
        conn: asyncpg.Connection,
        application_id: str,
        updates: dict[str, Any],
    ) -> None:
        """Insert or update application_summary row with the given column values."""
        columns = ["application_id"] + list(updates.keys())
        placeholders = ", ".join(f"${i+1}" for i in range(len(columns)))
        col_names = ", ".join(columns)

        # Build ON CONFLICT SET clause (skip application_id)
        set_clauses = ", ".join(
            f"{col} = EXCLUDED.{col}" for col in updates.keys()
        )

        sql = f"""
            INSERT INTO application_summary ({col_names})
            VALUES ({placeholders})
            ON CONFLICT (application_id) DO UPDATE SET {set_clauses}
        """

        values = [application_id] + list(updates.values())
        await conn.execute(sql, *values)
