"""
Double-Decision Concurrency Test

Two AI agents simultaneously attempt to append a CreditAnalysisCompleted event
to the same loan application stream. Both read the stream at version N and pass
expected_version=N to their append call.

Assertions:
    (a) Total events appended to the stream after both attempts = N+1 (not N+2)
    (b) The winning task's event has the correct stream_position
    (c) The losing task's OptimisticConcurrencyError is raised, not silently swallowed
"""

from __future__ import annotations

import asyncio
import os

import pytest
import asyncpg

from src.event_store import EventStore, _init_connection
from src.models.events import (
    ApplicationSubmitted,
    CreditAnalysisRequested,
    CreditAnalysisCompleted,
    OptimisticConcurrencyError,
)


DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://localhost/apex_ledger_test"
)

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "schema.sql")


@pytest.fixture
async def pool():
    """Create a fresh test database pool and apply schema."""
    p = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10, init=_init_connection)

    # Read and apply schema
    with open(SCHEMA_PATH) as f:
        schema_sql = f.read()

    async with p.acquire() as conn:
        # Clean slate for each test
        await conn.execute("DROP TABLE IF EXISTS outbox CASCADE")
        await conn.execute("DROP TABLE IF EXISTS snapshots CASCADE")
        await conn.execute("DROP TABLE IF EXISTS projection_checkpoints CASCADE")
        await conn.execute("DROP TABLE IF EXISTS events CASCADE")
        await conn.execute("DROP TABLE IF EXISTS event_streams CASCADE")
        await conn.execute(schema_sql)

    yield p
    await p.close()


@pytest.fixture
def store(pool) -> EventStore:
    return EventStore(pool)


async def _setup_loan_stream(store: EventStore, app_id: str) -> int:
    """Create a loan stream with 3 events to set up the test scenario."""
    # Event 1: ApplicationSubmitted
    event1 = ApplicationSubmitted.create(
        application_id=app_id,
        applicant_id="applicant-001",
        requested_amount_usd=500_000.0,
        loan_purpose="expansion",
    )
    await store.append(
        stream_id=f"loan-{app_id}",
        events=[event1],
        expected_version=-1,
    )

    # Event 2: CreditAnalysisRequested
    event2 = CreditAnalysisRequested.create(
        application_id=app_id,
        assigned_agent_id="agent-credit-001",
    )
    await store.append(
        stream_id=f"loan-{app_id}",
        events=[event2],
        expected_version=1,
    )

    # Event 3: Another preparatory event (e.g., a second request)
    event3 = CreditAnalysisRequested.create(
        application_id=app_id,
        assigned_agent_id="agent-credit-002",
    )
    await store.append(
        stream_id=f"loan-{app_id}",
        events=[event3],
        expected_version=2,
    )

    return 3  # current version


@pytest.mark.asyncio
async def test_double_decision_concurrency(store: EventStore):
    """
    The most critical test in Phase 1.

    Two agents simultaneously attempt to append CreditAnalysisCompleted
    to the same loan stream, both with expected_version=3.
    Exactly one must succeed. The other must receive OptimisticConcurrencyError.
    """
    app_id = "TEST-CONC-001"
    current_version = await _setup_loan_stream(store, app_id)
    assert current_version == 3

    # Both agents read the stream at version 3
    stream_id = f"loan-{app_id}"

    # Agent A's event
    event_a = CreditAnalysisCompleted.create(
        application_id=app_id,
        agent_id="agent-credit-A",
        session_id="session-A",
        model_version="v2.3",
        confidence_score=0.85,
        risk_tier="MEDIUM",
        recommended_limit_usd=400_000.0,
        analysis_duration_ms=1500,
        input_data_hash="hash-a",
    )

    # Agent B's event
    event_b = CreditAnalysisCompleted.create(
        application_id=app_id,
        agent_id="agent-credit-B",
        session_id="session-B",
        model_version="v2.3",
        confidence_score=0.78,
        risk_tier="LOW",
        recommended_limit_usd=350_000.0,
        analysis_duration_ms=1200,
        input_data_hash="hash-b",
    )

    results: dict[str, str] = {}  # agent -> "success" | "conflict"
    occ_errors: list[OptimisticConcurrencyError] = []

    async def agent_append(agent_name: str, event):
        try:
            new_version = await store.append(
                stream_id=stream_id,
                events=[event],
                expected_version=3,  # Both read version 3
            )
            results[agent_name] = "success"
            return new_version
        except OptimisticConcurrencyError as e:
            results[agent_name] = "conflict"
            occ_errors.append(e)
            return None

    # Launch both concurrently
    task_a = asyncio.create_task(agent_append("Agent_A", event_a))
    task_b = asyncio.create_task(agent_append("Agent_B", event_b))
    await asyncio.gather(task_a, task_b)

    # -------------------------------------------------------------------------
    # Assertions
    # -------------------------------------------------------------------------

    # (a) Total events in the stream = 4, not 5
    final_events = await store.load_stream(stream_id)
    assert len(final_events) == 4, (
        f"Expected 4 events in stream, got {len(final_events)}. "
        "Both agents' writes should not both succeed."
    )

    # (b) The winning task's event has stream_position=4
    last_event = final_events[-1]
    assert last_event.stream_position == 4, (
        f"Expected winning event at position 4, got {last_event.stream_position}"
    )
    assert last_event.event_type == "CreditAnalysisCompleted"

    # (c) Exactly one succeeded, exactly one got OptimisticConcurrencyError
    success_count = sum(1 for v in results.values() if v == "success")
    conflict_count = sum(1 for v in results.values() if v == "conflict")

    assert success_count == 1, f"Expected exactly 1 success, got {success_count}"
    assert conflict_count == 1, f"Expected exactly 1 conflict, got {conflict_count}"
    assert len(occ_errors) == 1, "OptimisticConcurrencyError was not raised"

    # Verify the error contains useful information
    err = occ_errors[0]
    assert err.stream_id == stream_id
    assert err.expected_version == 3

    # Stream version should now be 4
    final_version = await store.stream_version(stream_id)
    assert final_version == 4

    print("\n--- Double-Decision Concurrency Test PASSED ---")
    print(f"  Results: {results}")
    print(f"  Final stream version: {final_version}")
    print(f"  Winning event position: {last_event.stream_position}")
    print(f"  OCCError details: {err.to_dict()}")


@pytest.mark.asyncio
async def test_new_stream_concurrency(store: EventStore):
    """
    Two agents try to create the same stream simultaneously.
    Only one should succeed.
    """
    stream_id = "loan-CONC-002"

    event_a = ApplicationSubmitted.create(
        application_id="CONC-002",
        applicant_id="applicant-A",
        requested_amount_usd=100_000.0,
        loan_purpose="working capital",
    )
    event_b = ApplicationSubmitted.create(
        application_id="CONC-002",
        applicant_id="applicant-B",
        requested_amount_usd=200_000.0,
        loan_purpose="equipment",
    )

    results = []

    async def try_create(event):
        try:
            v = await store.append(
                stream_id=stream_id, events=[event], expected_version=-1
            )
            results.append(("success", v))
        except OptimisticConcurrencyError as e:
            results.append(("conflict", e))

    await asyncio.gather(try_create(event_a), try_create(event_b))

    successes = [r for r in results if r[0] == "success"]
    conflicts = [r for r in results if r[0] == "conflict"]

    assert len(successes) == 1, f"Expected 1 success, got {len(successes)}"
    assert len(conflicts) == 1, f"Expected 1 conflict, got {len(conflicts)}"

    # Stream should have exactly 1 event
    events = await store.load_stream(stream_id)
    assert len(events) == 1

    print("\n--- New Stream Concurrency Test PASSED ---")


@pytest.mark.asyncio
async def test_sequential_appends(store: EventStore):
    """Basic test: sequential appends with correct versioning."""
    app_id = "SEQ-001"
    stream_id = f"loan-{app_id}"

    # First event — new stream
    e1 = ApplicationSubmitted.create(
        application_id=app_id,
        applicant_id="applicant-001",
        requested_amount_usd=300_000.0,
        loan_purpose="bridge loan",
    )
    v1 = await store.append(stream_id=stream_id, events=[e1], expected_version=-1)
    assert v1 == 1

    # Second event — append to existing
    e2 = CreditAnalysisRequested.create(
        application_id=app_id,
        assigned_agent_id="agent-001",
    )
    v2 = await store.append(stream_id=stream_id, events=[e2], expected_version=1)
    assert v2 == 2

    # Verify stream contents
    events = await store.load_stream(stream_id)
    assert len(events) == 2
    assert events[0].event_type == "ApplicationSubmitted"
    assert events[1].event_type == "CreditAnalysisRequested"
    assert events[0].stream_position == 1
    assert events[1].stream_position == 2

    print("\n--- Sequential Appends Test PASSED ---")


@pytest.mark.asyncio
async def test_wrong_expected_version(store: EventStore):
    """Appending with wrong expected_version must raise."""
    stream_id = "loan-WRONG-VER"

    e1 = ApplicationSubmitted.create(
        application_id="WRONG-VER",
        applicant_id="applicant-001",
        requested_amount_usd=100_000.0,
        loan_purpose="test",
    )
    await store.append(stream_id=stream_id, events=[e1], expected_version=-1)

    e2 = CreditAnalysisRequested.create(
        application_id="WRONG-VER",
        assigned_agent_id="agent-001",
    )

    with pytest.raises(OptimisticConcurrencyError) as exc_info:
        await store.append(stream_id=stream_id, events=[e2], expected_version=0)

    assert exc_info.value.expected_version == 0
    assert exc_info.value.actual_version == 1
    assert exc_info.value.stream_id == stream_id

    print("\n--- Wrong Expected Version Test PASSED ---")
