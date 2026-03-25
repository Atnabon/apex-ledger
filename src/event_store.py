"""
EventStore — async Python interface to the append-only event store.

All writes go through append() with optimistic concurrency control.
All reads go through load_stream() or load_all().
Outbox entries are written in the same transaction as event appends.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import asyncpg


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Set up JSON codec for asyncpg so dicts are auto-serialized to JSONB."""
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )
    await conn.set_type_codec(
        "json",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )

from src.models.events import (
    BaseEvent,
    OptimisticConcurrencyError,
    StoredEvent,
    StreamMetadata,
    StreamNotFoundError,
)
from src.upcasting.registry import registry as _upcaster_registry


class EventStore:
    """Async event store backed by PostgreSQL."""

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    async def append(
        self,
        stream_id: str,
        events: list[BaseEvent],
        expected_version: int,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> int:
        """
        Atomically append events to a stream.

        Args:
            stream_id: Target stream identifier (e.g. "loan-123").
            events: List of domain events to append.
            expected_version: -1 for new streams, or the exact current version.
            correlation_id: Optional correlation ID for tracing.
            causation_id: Optional causation ID for causal chains.

        Returns:
            The new stream version after appending.

        Raises:
            OptimisticConcurrencyError: If the stream version does not match.
        """
        if not events:
            raise ValueError("Cannot append an empty event list")

        async with self._pool.acquire() as conn:
            try:
                async with conn.transaction():
                    # Determine the aggregate_type from the stream_id prefix
                    aggregate_type = self._infer_aggregate_type(stream_id)

                    if expected_version == -1:
                        # New stream — attempt to insert
                        await conn.execute(
                            """
                            INSERT INTO event_streams (stream_id, aggregate_type, current_version)
                            VALUES ($1, $2, 0)
                            """,
                            stream_id,
                            aggregate_type,
                        )
                        current_version = 0
                    else:
                        # Existing stream — verify version with row-level lock
                        row = await conn.fetchrow(
                            """
                            SELECT current_version FROM event_streams
                            WHERE stream_id = $1
                            FOR UPDATE
                            """,
                            stream_id,
                        )
                        if row is None:
                            raise StreamNotFoundError(stream_id)
                        current_version = row["current_version"]
                        if current_version != expected_version:
                            raise OptimisticConcurrencyError(
                                stream_id=stream_id,
                                expected_version=expected_version,
                                actual_version=current_version,
                            )

                    # Build metadata
                    meta: dict[str, Any] = {}
                    if correlation_id:
                        meta["correlation_id"] = correlation_id
                    if causation_id:
                        meta["causation_id"] = causation_id

                    # Append each event
                    new_version = current_version
                    for event in events:
                        new_version += 1
                        event_id = uuid.uuid4()

                        await conn.execute(
                            """
                            INSERT INTO events
                                (event_id, stream_id, stream_position, event_type,
                                 event_version, payload, metadata)
                            VALUES ($1, $2, $3, $4, $5, $6, $7)
                            """,
                            event_id,
                            stream_id,
                            new_version,
                            event.event_type,
                            event.event_version,
                            event.payload,
                            meta,
                        )

                        # Write to outbox in the same transaction
                        await conn.execute(
                            """
                            INSERT INTO outbox (event_id, destination, payload)
                            VALUES ($1, $2, $3)
                            """,
                            event_id,
                            f"stream:{stream_id}",
                            event.payload,
                        )

                    # Update stream version
                    await conn.execute(
                        """
                        UPDATE event_streams
                        SET current_version = $1
                        WHERE stream_id = $2
                        """,
                        new_version,
                        stream_id,
                    )

                    return new_version

            except asyncpg.UniqueViolationError:
                # The UNIQUE constraint on (stream_id, stream_position) or
                # event_streams PK caught a concurrent write.
                # Fetch the actual version outside the failed transaction.
                actual = await self._get_version(conn, stream_id)
                raise OptimisticConcurrencyError(
                    stream_id=stream_id,
                    expected_version=expected_version,
                    actual_version=actual,
                )

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    async def load_stream(
        self,
        stream_id: str,
        from_position: int = 0,
        to_position: int | None = None,
    ) -> list[StoredEvent]:
        """Load events from a single stream in stream-position order."""
        query = """
            SELECT event_id, stream_id, stream_position, global_position,
                   event_type, event_version, payload, metadata, recorded_at
            FROM events
            WHERE stream_id = $1 AND stream_position > $2
        """
        params: list[Any] = [stream_id, from_position]

        if to_position is not None:
            query += " AND stream_position <= $3"
            params.append(to_position)

        query += " ORDER BY stream_position ASC"

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        events = [self._row_to_stored_event(row) for row in rows]
        # Transparently apply upcasting so callers always see the latest schema
        return _upcaster_registry.upcast_batch(events)

    async def load_all(
        self,
        from_global_position: int = 0,
        event_types: list[str] | None = None,
        batch_size: int = 500,
    ) -> AsyncIterator[StoredEvent]:
        """
        Async generator yielding all events from a global position.
        Efficient for projection replay — loads in batches.
        """
        position = from_global_position

        while True:
            if event_types:
                query = """
                    SELECT event_id, stream_id, stream_position, global_position,
                           event_type, event_version, payload, metadata, recorded_at
                    FROM events
                    WHERE global_position > $1 AND event_type = ANY($2)
                    ORDER BY global_position ASC
                    LIMIT $3
                """
                params: list[Any] = [position, event_types, batch_size]
            else:
                query = """
                    SELECT event_id, stream_id, stream_position, global_position,
                           event_type, event_version, payload, metadata, recorded_at
                    FROM events
                    WHERE global_position > $1
                    ORDER BY global_position ASC
                    LIMIT $2
                """
                params = [position, batch_size]

            async with self._pool.acquire() as conn:
                rows = await conn.fetch(query, *params)

            if not rows:
                break

            for row in rows:
                event = self._row_to_stored_event(row)
                # Transparently apply upcasting on the load path
                event = _upcaster_registry.upcast(event)
                position = event.global_position
                yield event

            if len(rows) < batch_size:
                break

    async def stream_version(self, stream_id: str) -> int:
        """Return the current version of a stream, or -1 if it does not exist."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT current_version FROM event_streams WHERE stream_id = $1",
                stream_id,
            )
        if row is None:
            return -1
        return row["current_version"]

    async def archive_stream(self, stream_id: str) -> None:
        """Mark a stream as archived (soft-delete)."""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE event_streams
                SET archived_at = NOW()
                WHERE stream_id = $1 AND archived_at IS NULL
                """,
                stream_id,
            )
            if result == "UPDATE 0":
                raise StreamNotFoundError(stream_id)

    async def get_stream_metadata(self, stream_id: str) -> StreamMetadata:
        """Return metadata for a stream."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT stream_id, aggregate_type, current_version,
                       created_at, archived_at, metadata
                FROM event_streams
                WHERE stream_id = $1
                """,
                stream_id,
            )
        if row is None:
            raise StreamNotFoundError(stream_id)
        return StreamMetadata(
            stream_id=row["stream_id"],
            aggregate_type=row["aggregate_type"],
            current_version=row["current_version"],
            created_at=row["created_at"],
            archived_at=row["archived_at"],
            metadata=row["metadata"],
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _get_version(conn: asyncpg.Connection, stream_id: str) -> int:
        row = await conn.fetchrow(
            "SELECT current_version FROM event_streams WHERE stream_id = $1",
            stream_id,
        )
        if row is None:
            return -1
        return row["current_version"]

    @staticmethod
    def _row_to_stored_event(row: asyncpg.Record) -> StoredEvent:
        payload = row["payload"]
        metadata = row["metadata"]

        return StoredEvent(
            event_id=row["event_id"],
            stream_id=row["stream_id"],
            stream_position=row["stream_position"],
            global_position=row["global_position"],
            event_type=row["event_type"],
            event_version=row["event_version"],
            payload=payload,
            metadata=metadata,
            recorded_at=row["recorded_at"],
        )

    @staticmethod
    def _infer_aggregate_type(stream_id: str) -> str:
        """Infer aggregate type from stream_id prefix."""
        prefixes = {
            "loan-": "LoanApplication",
            "agent-": "AgentSession",
            "compliance-": "ComplianceRecord",
            "audit-": "AuditLedger",
            "credit-": "CreditRecord",
            "fraud-": "FraudScreening",
            "docpkg-": "DocumentPackage",
        }
        for prefix, agg_type in prefixes.items():
            if stream_id.startswith(prefix):
                return agg_type
        return "Unknown"


# ------------------------------------------------------------------
# Factory
# ------------------------------------------------------------------

async def create_event_store(dsn: str) -> EventStore:
    """Create an EventStore with a connection pool."""
    pool = await asyncpg.create_pool(
        dsn, min_size=2, max_size=10, init=_init_connection
    )
    return EventStore(pool)
