"""
AuditLedgerAggregate — consistency boundary for cross-cutting audit trails.

Reconstructed by replaying events from the audit-{entity_type}-{entity_id} stream.
Maintains an append-only audit trail with a hash chain to guarantee integrity
of the recorded event history.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.models.events import DomainError, StoredEvent

if TYPE_CHECKING:
    from src.event_store import EventStore


class AuditLedgerAggregate:
    """
    Aggregate root for an append-only audit trail with hash-chain integrity.

    Each AuditIntegrityCheckRun records a cryptographic hash over the events
    verified so far. The previous_hash field in each new check must match the
    last recorded hash, forming an unbroken chain.
    """

    def __init__(self, entity_type: str, entity_id: str):
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.version: int = 0
        self.integrity_checks: list[dict] = []
        self.last_integrity_hash: str | None = None
        self.events_verified_count: int = 0

    @property
    def stream_id(self) -> str:
        return f"audit-{self.entity_type}-{self.entity_id}"

    @classmethod
    async def load(
        cls, store: EventStore, entity_type: str, entity_id: str
    ) -> AuditLedgerAggregate:
        """Reconstruct aggregate state by replaying the event stream."""
        stream_id = f"audit-{entity_type}-{entity_id}"
        events = await store.load_stream(stream_id)
        agg = cls(entity_type=entity_type, entity_id=entity_id)
        for event in events:
            agg._apply(event)
        return agg

    # ------------------------------------------------------------------
    # Event Application (state reconstruction)
    # ------------------------------------------------------------------

    def _apply(self, event: StoredEvent) -> None:
        handler = getattr(self, f"_on_{event.event_type}", None)
        if handler:
            handler(event)
        self.version = event.stream_position

    def _on_AuditIntegrityCheckRun(self, event: StoredEvent) -> None:
        integrity_hash = event.payload.get("integrity_hash")
        check_timestamp = event.payload.get("check_timestamp")
        events_count = event.payload.get("events_verified_count", 0)

        self.integrity_checks.append(
            {
                "hash": integrity_hash,
                "timestamp": check_timestamp,
                "events_count": events_count,
            }
        )
        self.last_integrity_hash = integrity_hash
        self.events_verified_count = events_count

    # ------------------------------------------------------------------
    # Business Rule Assertions
    # ------------------------------------------------------------------

    def assert_chain_continuous(self, previous_hash: str | None) -> None:
        """Previous hash in new check must match last recorded hash."""
        if previous_hash != self.last_integrity_hash:
            raise DomainError(
                f"Hash chain broken: expected previous_hash "
                f"'{self.last_integrity_hash}', got '{previous_hash}'. "
                f"The audit trail integrity chain must be continuous.",
                rule="chain_continuous",
            )

    def assert_no_duplicate_check(self, check_timestamp: str) -> None:
        """Cannot run two integrity checks at the same timestamp."""
        existing_timestamps = {
            check["timestamp"] for check in self.integrity_checks
        }
        if check_timestamp in existing_timestamps:
            raise DomainError(
                f"An integrity check has already been recorded at timestamp "
                f"'{check_timestamp}'. Cannot run duplicate checks.",
                rule="no_duplicate_check",
            )
