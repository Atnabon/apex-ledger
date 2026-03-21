# DOMAIN_NOTES.md — Apex Ledger

## 1. EDA vs. ES Distinction

**Scenario**: A component uses LangChain callbacks to capture event-like data (traces, tool calls, token usage). Is this EDA or ES?

**Answer**: This is **Event-Driven Architecture (EDA)**, not Event Sourcing (ES).

LangChain callbacks produce events-as-messages: they flow through handlers, may be dropped, can be duplicated, and are not the source of truth for system state. The callback data is a **side-channel** — the actual state of the system (the agent's decisions, the application's status) exists elsewhere, typically in memory or a CRUD database. If the callback handler crashes, the data is lost. If you replay the callbacks, you do not reconstruct the system's state — you reconstruct a log of observations.

**If redesigned using The Ledger**, the architecture changes fundamentally:

1. **Events become the database**: Instead of callbacks writing to a log, every LangChain node execution writes an `AgentNodeExecuted` event to an append-only stream. The event IS the state, not an observation of the state.
2. **Durability guarantee**: Events cannot be dropped. The `events` table enforces `UNIQUE (stream_id, stream_position)` — once written, the event is permanent and ordered.
3. **State reconstruction**: The agent's complete execution history can be replayed to reconstruct its state at any point. This enables crash recovery (Gas Town pattern) — something impossible with EDA callbacks.
4. **What you gain**: Auditability by construction (the regulator queries the event stream, not a separate log); reproducibility (replay events to reconstruct any past state); crash recovery (restart agent, replay its session stream, continue from last checkpoint).

**The key distinction**: EDA events can be lost and the system continues. ES events cannot be lost — they ARE the system.

---

## 2. The Aggregate Question

**Chosen boundaries**: Four aggregates — `LoanApplication`, `AgentSession`, `ComplianceRecord`, `AuditLedger`.

**Alternative considered and rejected**: Merging `ComplianceRecord` into `LoanApplication` as a single aggregate.

**Why rejected**: The coupling problem is **write contention under concurrent agents**.

In the Apex scenario, the `ComplianceAgent` writes compliance rule results (`ComplianceRulePassed`, `ComplianceRuleFailed`) while the `DecisionOrchestratorAgent` may simultaneously be reading or writing to the loan application stream (`DecisionGenerated`). If compliance events lived in the `loan-{id}` stream:

- Both agents would need to pass `expected_version` against the same stream
- A compliance rule write would conflict with a decision write — even though they are logically independent operations by different actors
- At 100 concurrent applications with 4 agents each, this contention means constant `OptimisticConcurrencyError` retries on what should be non-conflicting writes
- The retry budget would be consumed by false conflicts rather than genuine business rule violations

With separate aggregates: the `ComplianceAgent` writes to `compliance-{id}` and the `DecisionOrchestrator` writes to `loan-{id}`. They never contend. The `LoanApplicationAggregate` reads the compliance stream when it needs to check compliance status (Rule 5: compliance dependency), but this is a read — not a concurrent write.

**The general principle**: If two different actors write to an entity at different times for different reasons, they should be separate aggregates. Merging them creates artificial coupling that manifests as contention under load.

---

## 3. Concurrency in Practice

**Scenario**: Two AI agents simultaneously process the same loan application. Both call `append_events` with `expected_version=3`.

**Exact sequence of operations**:

1. **Agent A** reads `loan-APP-001` stream. `current_version = 3`.
2. **Agent B** reads `loan-APP-001` stream. `current_version = 3`.
3. **Agent A** calls `store.append(stream_id="loan-APP-001", events=[CreditAnalysisCompleted(...)], expected_version=3)`.
   - Inside the transaction: the store checks `event_streams.current_version` for `loan-APP-001`.
   - `current_version == 3 == expected_version` → check passes.
   - Event inserted at `stream_position=4`.
   - `event_streams.current_version` updated from 3 to 4 via `UPDATE ... WHERE current_version = 3`.
   - Transaction commits. **Agent A succeeds. New version = 4.**
4. **Agent B** calls `store.append(stream_id="loan-APP-001", events=[CreditAnalysisCompleted(...)], expected_version=3)`.
   - Inside the transaction: the store checks `event_streams.current_version`.
   - `current_version == 4 != 3 (expected_version)` → check fails.
   - **`OptimisticConcurrencyError` raised**: `{stream_id: "loan-APP-001", expected_version: 3, actual_version: 4, suggested_action: "reload_stream_and_retry"}`.
   - Transaction rolls back. No events written.

**What the losing agent receives**: An `OptimisticConcurrencyError` with the stream ID, the expected version it passed (3), and the actual current version (4).

**What it must do next**:
1. Reload the stream: `events = await store.load_stream("loan-APP-001")`
2. Reconstruct the aggregate: the new state now includes Agent A's `CreditAnalysisCompleted` event
3. Re-evaluate whether its own analysis is still relevant (Rule 3: model version locking may now reject it)
4. If still valid, retry with `expected_version=4`

The retry is not automatic — the agent must re-evaluate its business logic because the aggregate state has changed.

---

## 4. Projection Lag and Its Consequences

**Scenario**: `ApplicationSummary` projection has typical lag of 200ms. A loan officer queries "available credit limit" immediately after an agent commits a disbursement event. They see the old limit.

**What the system does**:

1. The event is committed to the `events` table (source of truth) immediately.
2. The `ProjectionDaemon` polls for new events every 100ms. The projection will be updated within ~200ms.
3. The loan officer's query hits the `ApplicationSummary` projection table, which is stale by up to 200ms.

**Response strategy**:

- **Expose projection lag as a metric**: Every query response includes `projection_lag_ms` — the time between the latest event in the store and the latest event the projection has processed. The UI displays this: "Data as of 200ms ago".
- **For critical reads, offer a strong-consistency path**: The MCP resource `ledger://applications/{id}/audit-trail` loads directly from the event stream (justified exception). If the loan officer needs the authoritative current state, they query the audit trail, not the projection.
- **UI communication**: Show a subtle indicator — "Refreshing..." — when the projection is known to be behind. Auto-refresh after the SLO window (500ms for ApplicationSummary).

**The architectural principle**: Eventual consistency is acceptable for read models. The source of truth (the event stream) is always strongly consistent. The projection is an optimization, not the authority.

---

## 5. The Upcasting Scenario

**Original (v1)**: `CreditDecisionMade { application_id, decision, reason }`

**New (v2)**: `CreditDecisionMade { application_id, decision, reason, model_version, confidence_score, regulatory_basis }`

**Upcaster implementation**:

```python
@registry.register("CreditDecisionMade", from_version=1)
def upcast_credit_decision_v1_to_v2(payload: dict) -> dict:
    recorded_at = payload.get("recorded_at", "2024-01-01T00:00:00Z")
    return {
        **payload,
        # Inference: derive from the timestamp — pre-2025 = legacy model
        "model_version": _infer_model_version(recorded_at),
        # Genuinely unknown — do NOT fabricate
        "confidence_score": None,
        # Inference: regulations active at the time of the decision
        "regulatory_basis": _infer_regulatory_basis(recorded_at),
    }

def _infer_model_version(recorded_at: str) -> str:
    """Infer model version from the event's recorded_at timestamp."""
    from datetime import datetime
    dt = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
    if dt.year < 2025:
        return "legacy-pre-2025"
    elif dt.year == 2025:
        return "v1.0-2025"
    return "v2.0-2026"

def _infer_regulatory_basis(recorded_at: str) -> str:
    """Infer which regulation set was active when the event was recorded."""
    from datetime import datetime
    dt = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
    if dt.year < 2025:
        return "OCC-2024-Q4"
    elif dt < datetime(2026, 1, 1, tzinfo=dt.tzinfo):
        return "OCC-2025-ANNUAL"
    return "OCC-2026-Q1"
```

**Inference strategy for `model_version`**: Use `recorded_at` timestamp to determine which model was deployed at the time. This is an approximation — the actual model version used is genuinely lost. The inference has ~5% error rate during model rollover periods (a new model may have been deployed mid-day, but we only know the date). The downstream consequence of an incorrect model version inference is that performance analytics (AgentPerformanceLedger projection) may misattribute ~5% of decisions. This is acceptable for analytics but NOT for regulatory reporting — hence we document it.

**Inference strategy for `confidence_score`**: **Null, not fabricated.** We have zero information about what confidence score the legacy system would have produced. Fabricating a value (e.g., 0.5 as "neutral") is worse than null because:
- It would pass the confidence floor check (Rule 4: < 0.6 → REFER), potentially misrepresenting legacy decisions as "reviewed"
- Any downstream analysis using confidence_score would treat fabricated values as real data
- A null forces the consumer to handle the missing case explicitly — which is the correct behavior

**Inference strategy for `regulatory_basis`**: Inferred from timestamp + known regulation schedule. Error rate ~2% (regulation updates happen on known dates, but early-adoption periods create ambiguity). Acceptable for audit trail context; the regulation version in the original event would have been authoritative if captured.

---

## 6. The Marten Async Daemon Parallel

**Marten 7.0** introduced distributed projection execution across multiple nodes using advisory locks and a shard assignment protocol. Each projection shard is assigned to exactly one node at a time. If a node dies, its shards are reassigned.

**Python equivalent using PostgreSQL advisory locks**:

```python
class DistributedProjectionDaemon:
    async def claim_shard(self, shard_id: int) -> bool:
        """Try to acquire a PostgreSQL advisory lock for this shard."""
        async with self._pool.acquire() as conn:
            acquired = await conn.fetchval(
                "SELECT pg_try_advisory_lock($1)", shard_id
            )
            return acquired

    async def release_shard(self, shard_id: int) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "SELECT pg_advisory_unlock($1)", shard_id
            )
```

**Coordination primitive**: PostgreSQL `pg_advisory_lock` — session-scoped advisory locks. Each projection is assigned a numeric lock ID. A daemon instance tries to acquire the lock before processing. If the lock is held by another instance, it skips that projection and tries the next.

**Failure mode guarded against**: **Split-brain projection processing** — two daemon instances processing the same projection simultaneously would produce duplicate or out-of-order updates. The advisory lock guarantees mutual exclusion. When a daemon instance crashes, its PostgreSQL connection terminates, which automatically releases session-scoped advisory locks. The surviving instance detects the released lock on its next polling cycle and takes over — typically within one poll interval (100ms).

**Limitation vs. Marten**: Marten's Async Daemon uses a more sophisticated shard assignment protocol with health checks and graceful handoff. Our advisory lock approach has a gap window of up to one poll interval where no instance is processing a shard after a crash. For the Apex Ledger's throughput requirements (~1,000 applications/hour), this gap is acceptable.
