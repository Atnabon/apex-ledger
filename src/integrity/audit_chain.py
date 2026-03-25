"""
Cryptographic Audit Chain — SHA-256 hash chain over event streams.

Each AuditIntegrityCheckRun records a hash of all preceding events
plus the previous integrity hash, forming a blockchain-style chain.
Any post-hoc modification of events breaks the chain.
"""
from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.event_store import EventStore
from src.models.events import AuditIntegrityCheckRun, StoredEvent


@dataclass
class IntegrityCheckResult:
    entity_type: str
    entity_id: str
    events_verified: int
    chain_valid: bool
    tamper_detected: bool
    integrity_hash: str
    previous_hash: str | None
    details: list[str] = field(default_factory=list)


def hash_event(event: StoredEvent) -> str:
    """Produce a deterministic SHA-256 hash of an event's payload."""
    canonical = json.dumps(event.payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def compute_chain_hash(event_hashes: list[str], previous_hash: str | None) -> str:
    """Compute the chain hash: sha256(previous_hash + concatenated event hashes)."""
    combined = (previous_hash or "GENESIS") + "".join(event_hashes)
    return hashlib.sha256(combined.encode()).hexdigest()


async def run_integrity_check(
    store: EventStore,
    entity_type: str,
    entity_id: str,
) -> IntegrityCheckResult:
    """
    Run a cryptographic integrity check on an entity's event stream.

    1. Load all events for the entity's primary stream
    2. Load the last AuditIntegrityCheckRun event (if any)
    3. Hash the payloads of all events since the last check
    4. Verify hash chain
    5. Append new AuditIntegrityCheckRun event
    6. Return result
    """
    # Determine the primary stream for this entity
    stream_prefix_map = {
        "loan": "loan",
        "agent": "agent",
        "compliance": "compliance",
        "fraud": "fraud",
        "credit": "credit",
        "docpkg": "docpkg",
    }
    prefix = stream_prefix_map.get(entity_type, entity_type)
    primary_stream_id = f"{prefix}-{entity_id}"

    # Load all events from the primary stream
    events = await store.load_stream(primary_stream_id)

    if not events:
        return IntegrityCheckResult(
            entity_type=entity_type,
            entity_id=entity_id,
            events_verified=0,
            chain_valid=True,
            tamper_detected=False,
            integrity_hash="GENESIS",
            previous_hash=None,
            details=["No events found in stream"],
        )

    # Load the audit stream to find previous integrity check
    audit_stream_id = f"audit-{entity_type}-{entity_id}"
    audit_events = await store.load_stream(audit_stream_id)

    previous_hash: str | None = None
    last_verified_position = 0

    if audit_events:
        last_check = audit_events[-1]
        previous_hash = last_check.payload.get("integrity_hash")
        last_verified_position = last_check.payload.get("events_verified_count", 0)

    # Hash all events since last check
    events_to_verify = [e for e in events if e.stream_position > last_verified_position]

    if not events_to_verify:
        events_to_verify = events  # Verify all if no prior check

    event_hashes = [hash_event(e) for e in events_to_verify]
    new_hash = compute_chain_hash(event_hashes, previous_hash)

    # Verify chain continuity
    chain_valid = True
    tamper_detected = False
    details = []

    if previous_hash and audit_events:
        # Re-verify previously checked events to detect tampering
        previously_checked = [e for e in events if e.stream_position <= last_verified_position]
        if previously_checked:
            prev_hashes = [hash_event(e) for e in previously_checked]
            # Find the hash before the previous check
            prior_prior_hash = None
            if len(audit_events) >= 2:
                prior_prior_hash = audit_events[-2].payload.get("integrity_hash")
            recomputed = compute_chain_hash(prev_hashes, prior_prior_hash)
            if recomputed != previous_hash:
                chain_valid = False
                tamper_detected = True
                details.append(f"TAMPER DETECTED: Recomputed hash {recomputed[:16]}... != stored {previous_hash[:16]}...")

    details.append(f"Verified {len(events_to_verify)} events from position {last_verified_position + 1}")
    details.append(f"New integrity hash: {new_hash[:16]}...")

    # Append the integrity check event to the audit stream
    check_event = AuditIntegrityCheckRun.create(
        entity_id=entity_id,
        check_timestamp=datetime.utcnow().isoformat(),
        events_verified_count=len(events),
        integrity_hash=new_hash,
        previous_hash=previous_hash,
    )

    audit_version = await store.stream_version(audit_stream_id)
    await store.append(
        stream_id=audit_stream_id,
        events=[check_event],
        expected_version=audit_version,
    )

    return IntegrityCheckResult(
        entity_type=entity_type,
        entity_id=entity_id,
        events_verified=len(events),
        chain_valid=chain_valid,
        tamper_detected=tamper_detected,
        integrity_hash=new_hash,
        previous_hash=previous_hash,
        details=details,
    )
