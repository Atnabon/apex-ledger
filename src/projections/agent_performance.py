"""
AgentPerformanceLedger projection — metrics per agent model version.

Maintains a denormalized read model in the `agent_performance` table,
keyed by (agent_id, model_version). Tracks throughput, accuracy,
confidence, duration, and human override rates.
"""
import logging
from typing import Any

import asyncpg

from src.models.events import StoredEvent

logger = logging.getLogger(__name__)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS agent_performance (
    agent_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    analyses_completed BIGINT NOT NULL DEFAULT 0,
    decisions_generated BIGINT NOT NULL DEFAULT 0,
    fraud_screenings_completed BIGINT NOT NULL DEFAULT 0,
    sessions_started BIGINT NOT NULL DEFAULT 0,
    sessions_completed BIGINT NOT NULL DEFAULT 0,
    total_confidence_score NUMERIC NOT NULL DEFAULT 0,
    confidence_score_count BIGINT NOT NULL DEFAULT 0,
    total_duration_ms BIGINT NOT NULL DEFAULT 0,
    duration_count BIGINT NOT NULL DEFAULT 0,
    approve_count BIGINT NOT NULL DEFAULT 0,
    decline_count BIGINT NOT NULL DEFAULT 0,
    refer_count BIGINT NOT NULL DEFAULT 0,
    human_override_count BIGINT NOT NULL DEFAULT 0,
    human_review_count BIGINT NOT NULL DEFAULT 0,
    first_seen_at TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ,
    PRIMARY KEY (agent_id, model_version)
);
"""


class AgentPerformanceLedgerProjection:
    name: str = "AgentPerformanceLedger"
    event_types: list[str] = [
        "AgentSessionStarted",
        "AgentSessionCompleted",
        "CreditAnalysisCompleted",
        "FraudScreeningCompleted",
        "DecisionGenerated",
        "HumanReviewCompleted",
    ]

    async def handle(self, event: StoredEvent, conn: asyncpg.Connection) -> None:
        """Route event to the appropriate handler."""
        handler = getattr(self, f"_handle_{event.event_type}", None)
        if handler is None:
            logger.warning(f"No handler for event type: {event.event_type}")
            return
        await handler(event, conn)

    async def rebuild(self, conn: asyncpg.Connection) -> None:
        """Truncate the projection table so the daemon can replay from position 0."""
        await conn.execute("TRUNCATE TABLE agent_performance")
        logger.info("AgentPerformanceLedger projection table truncated for rebuild.")

    # ------------------------------------------------------------------
    # Per-event-type handlers
    # ------------------------------------------------------------------

    async def _handle_AgentSessionStarted(
        self, event: StoredEvent, conn: asyncpg.Connection
    ) -> None:
        payload = event.payload
        agent_id = payload["agent_id"]
        model_version = payload.get("model_version", "unknown")
        await self._ensure_row(conn, agent_id, model_version, event.recorded_at)
        await conn.execute(
            """UPDATE agent_performance
               SET sessions_started = sessions_started + 1,
                   last_seen_at = $3
               WHERE agent_id = $1 AND model_version = $2""",
            agent_id, model_version, event.recorded_at,
        )

    async def _handle_AgentSessionCompleted(
        self, event: StoredEvent, conn: asyncpg.Connection
    ) -> None:
        payload = event.payload
        agent_id = payload["agent_id"]
        # AgentSessionCompleted does not carry model_version directly;
        # fall back to 'unknown' — in practice, the row was already created
        # by AgentSessionStarted with the correct model_version.
        await conn.execute(
            """UPDATE agent_performance
               SET sessions_completed = sessions_completed + 1,
                   last_seen_at = $2
               WHERE agent_id = $1""",
            agent_id, event.recorded_at,
        )

    async def _handle_CreditAnalysisCompleted(
        self, event: StoredEvent, conn: asyncpg.Connection
    ) -> None:
        payload = event.payload
        agent_id = payload["agent_id"]
        model_version = payload.get("model_version", "unknown")
        confidence = payload.get("confidence_score", 0)
        duration_ms = payload.get("analysis_duration_ms", 0)
        await self._ensure_row(conn, agent_id, model_version, event.recorded_at)
        await conn.execute(
            """UPDATE agent_performance
               SET analyses_completed = analyses_completed + 1,
                   total_confidence_score = total_confidence_score + $3,
                   confidence_score_count = confidence_score_count + 1,
                   total_duration_ms = total_duration_ms + $4,
                   duration_count = duration_count + 1,
                   last_seen_at = $5
               WHERE agent_id = $1 AND model_version = $2""",
            agent_id, model_version, confidence, duration_ms, event.recorded_at,
        )

    async def _handle_FraudScreeningCompleted(
        self, event: StoredEvent, conn: asyncpg.Connection
    ) -> None:
        payload = event.payload
        agent_id = payload["agent_id"]
        model_version = payload.get("screening_model_version", "unknown")
        await self._ensure_row(conn, agent_id, model_version, event.recorded_at)
        await conn.execute(
            """UPDATE agent_performance
               SET fraud_screenings_completed = fraud_screenings_completed + 1,
                   last_seen_at = $3
               WHERE agent_id = $1 AND model_version = $2""",
            agent_id, model_version, event.recorded_at,
        )

    async def _handle_DecisionGenerated(
        self, event: StoredEvent, conn: asyncpg.Connection
    ) -> None:
        payload = event.payload
        agent_id = payload.get("orchestrator_agent_id", "unknown")
        # DecisionGenerated carries model_versions dict; use first value or 'unknown'
        model_versions = payload.get("model_versions", {})
        model_version = next(iter(model_versions.values()), "unknown") if model_versions else "unknown"
        confidence = payload.get("confidence_score", 0)
        recommendation = payload.get("recommendation", "").upper()

        await self._ensure_row(conn, agent_id, model_version, event.recorded_at)

        approve_inc = 1 if recommendation == "APPROVE" else 0
        decline_inc = 1 if recommendation == "DECLINE" else 0
        refer_inc = 1 if recommendation == "REFER" else 0

        await conn.execute(
            """UPDATE agent_performance
               SET decisions_generated = decisions_generated + 1,
                   total_confidence_score = total_confidence_score + $3,
                   confidence_score_count = confidence_score_count + 1,
                   approve_count = approve_count + $4,
                   decline_count = decline_count + $5,
                   refer_count = refer_count + $6,
                   last_seen_at = $7
               WHERE agent_id = $1 AND model_version = $2""",
            agent_id, model_version, confidence,
            approve_inc, decline_inc, refer_inc,
            event.recorded_at,
        )

    async def _handle_HumanReviewCompleted(
        self, event: StoredEvent, conn: asyncpg.Connection
    ) -> None:
        """
        HumanReviewCompleted does not carry agent_id/model_version directly.
        We attribute the override to the reviewer and track override counts.
        This event increments human_review_count for all agent rows that
        participated in the application (simplified: we skip agent lookup
        and only track aggregate override stats on a synthetic key).
        """
        payload = event.payload
        override = payload.get("override", False)
        reviewer_id = payload.get("reviewer_id", "human")
        # Use a synthetic agent key for human review tracking
        agent_id = reviewer_id
        model_version = "human"

        await self._ensure_row(conn, agent_id, model_version, event.recorded_at)

        override_inc = 1 if override else 0
        await conn.execute(
            """UPDATE agent_performance
               SET human_review_count = human_review_count + 1,
                   human_override_count = human_override_count + $3,
                   last_seen_at = $4
               WHERE agent_id = $1 AND model_version = $2""",
            agent_id, model_version, override_inc, event.recorded_at,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _ensure_row(
        conn: asyncpg.Connection,
        agent_id: str,
        model_version: str,
        seen_at: Any,
    ) -> None:
        """Insert a row if it does not exist yet."""
        await conn.execute(
            """INSERT INTO agent_performance (agent_id, model_version, first_seen_at, last_seen_at)
               VALUES ($1, $2, $3, $3)
               ON CONFLICT (agent_id, model_version) DO NOTHING""",
            agent_id, model_version, seen_at,
        )

    # ------------------------------------------------------------------
    # Query helpers (can be called from API layer)
    # ------------------------------------------------------------------

    @staticmethod
    async def get_avg_confidence(
        conn: asyncpg.Connection, agent_id: str, model_version: str
    ) -> float | None:
        row = await conn.fetchrow(
            """SELECT total_confidence_score, confidence_score_count
               FROM agent_performance
               WHERE agent_id = $1 AND model_version = $2""",
            agent_id, model_version,
        )
        if not row or row["confidence_score_count"] == 0:
            return None
        return float(row["total_confidence_score"]) / row["confidence_score_count"]

    @staticmethod
    async def get_avg_duration_ms(
        conn: asyncpg.Connection, agent_id: str, model_version: str
    ) -> float | None:
        row = await conn.fetchrow(
            """SELECT total_duration_ms, duration_count
               FROM agent_performance
               WHERE agent_id = $1 AND model_version = $2""",
            agent_id, model_version,
        )
        if not row or row["duration_count"] == 0:
            return None
        return float(row["total_duration_ms"]) / row["duration_count"]

    @staticmethod
    async def get_human_override_rate(
        conn: asyncpg.Connection, agent_id: str, model_version: str
    ) -> float | None:
        row = await conn.fetchrow(
            """SELECT human_override_count, human_review_count
               FROM agent_performance
               WHERE agent_id = $1 AND model_version = $2""",
            agent_id, model_version,
        )
        if not row or row["human_review_count"] == 0:
            return None
        return float(row["human_override_count"]) / row["human_review_count"]
