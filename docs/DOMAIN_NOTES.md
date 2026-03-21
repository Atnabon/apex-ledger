# DOMAIN_NOTES.md
## Week 5 — Agentic Event Store & Enterprise Audit Infrastructure
### Phase 0 — Domain Reconnaissance (Day 1, BEFORE any code)

> **What this file is:** This is a graded written document. You write it in plain English before touching any code. Think of it as your "thinking on paper" before building. A grader will read this to check you understand the concepts — not just that you can copy-paste code.

---

## Question 1 — EDA vs. Event Sourcing: What's the difference, and what changes when you use The Ledger?

### The Short Answer

A component that uses **callbacks** (like LangChain traces) to capture event-like data is **Event-Driven Architecture (EDA)** — NOT Event Sourcing.

### The Longer Explanation (plain English)

Imagine you have a security camera system in a building.

- **EDA (Event-Driven Architecture)** is like a security guard watching the camera feed live. When something happens, they react to it. But if they look away, or the feed cuts out, that moment is **gone forever**. The camera feed is just messages flying past — fire and forget.

- **Event Sourcing (ES)** is like a **recording system** where every second of footage is saved permanently to a hard drive in strict order. The recordings ARE the truth. If you want to know what happened at 2:47pm last Tuesday, you rewind and watch. The recordings can never be deleted or changed.

**LangChain callbacks** are the security guard watching live. They fire when something happens, maybe write a log line, and move on. If the process crashes, those events are gone. That is EDA.

**The Ledger (Event Sourcing)** stores every event permanently, in order, in PostgreSQL. The events ARE the database. Nothing is ever overwritten. You can replay the entire history from Day 1.

### What Would Change If You Redesigned Using The Ledger?

| What exists today (EDA / Callbacks) | What changes with The Ledger (Event Sourcing) |
|---|---|
| LangChain fires a callback → maybe writes a log | Every agent action is saved as a permanent event row in PostgreSQL |
| If the process restarts, context is lost | Agent replays its event stream and reconstructs exactly where it was |
| Events can be dropped or missed | Events are append-only — they can NEVER be removed |
| No way to ask "what was the state at 9am yesterday?" | You can replay events up to any timestamp and see exact state |
| No tamper detection | Hash chain makes any tampering detectable |

### What You Gain

1. **Auditability** — regulators can see every AI decision, who made it, and what data was used
2. **Reproducibility** — you can replay history and get the same answer every time
3. **Agent Memory** — an agent that crashes can restart and pick up exactly where it left off
4. **Time Travel Queries** — "what was the loan status at 3pm on March 5th?" becomes answerable

---

## Question 2 — The Aggregate Question: Which boundary did you reject and why?

### First, What is an Aggregate? (beginner explanation)

An **aggregate** is a consistency boundary. Think of it like a locked filing cabinet. Everything INSIDE that cabinet must be updated together, atomically — you can't open two drawers at the same time from two different people. The cabinet lock is the aggregate boundary.

In our system, we have four aggregates (four filing cabinets):
1. `LoanApplication` — tracks the loan from submission to final decision
2. `AgentSession` — tracks everything one AI agent did in one work session
3. `ComplianceRecord` — tracks all regulatory checks for one application
4. `AuditLedger` — cross-cutting audit trail linking everything together

### The Alternative Boundary I Considered and Rejected

**What I considered:** Merging `ComplianceRecord` into `LoanApplication` — making one big aggregate that handles both the loan lifecycle AND all compliance checks.

**Why this seemed appealing at first:** They're about the same application, so why not keep it together?

**Why I rejected it:**

Imagine a busy afternoon at Apex Financial. A hundred loan applications are all being processed simultaneously. Each loan has 4 AI agents running. Each agent appends events to the loan stream.

If `ComplianceRecord` is merged into `LoanApplication`, every time ANY compliance rule is checked, it must lock the ENTIRE loan aggregate to append the event. Now you have:
- The CreditAnalysis agent waiting to append its result
- The FraudDetection agent waiting to append its result
- The ComplianceAgent trying to append a rule-passed event
- All three fighting over the SAME lock

This causes **lock contention** — agents are constantly waiting on each other, retrying, slowing down.

**By keeping `ComplianceRecord` as a separate aggregate:**
- The compliance agent writes to its own stream (`compliance-{id}`)
- The credit agent writes to the loan stream (`loan-{id}`)
- They NEVER block each other
- The system can process both in parallel

**The coupling problem my chosen boundary prevents:**

If compliance and loan were merged, a compliance check failure would leave the loan aggregate in a locked state, potentially blocking a human reviewer from appending their decision. With separate aggregates, compliance checks happen independently. The `LoanApplication` aggregate only checks "are all compliance events present?" at the moment of approval — it doesn't need to own the compliance writes.

---

## Question 3 — Concurrency in Practice: What happens when two agents collide?

### Setup: The Scenario

Two AI fraud-detection agents (Agent A and Agent B) are both processing loan application `LOAN-999`. Both read the stream at version 3. Both decide to append a `CreditAnalysisCompleted` event. Both call:

```
append_events(stream_id="loan-LOAN-999", expected_version=3)
```

### The Exact Sequence of Operations

Here is what happens inside the event store, step by step:

```
Time 0ms:   Agent A reads stream loan-LOAN-999 → current version = 3
Time 0ms:   Agent B reads stream loan-LOAN-999 → current version = 3

Time 5ms:   Agent A calls append(expected_version=3)
            → PostgreSQL executes: UPDATE event_streams 
              SET current_version = 4 
              WHERE stream_id = 'loan-LOAN-999' AND current_version = 3
            → The condition current_version = 3 is TRUE
            → Update succeeds. Agent A's event is written at position 4.
            → Agent A returns: new version = 4 ✅ SUCCESS

Time 5ms:   Agent B calls append(expected_version=3) (simultaneously)
            → PostgreSQL executes: UPDATE event_streams
              SET current_version = 4
              WHERE stream_id = 'loan-LOAN-999' AND current_version = 3
            → The condition current_version = 3 is now FALSE (it's already 4)
            → PostgreSQL returns 0 rows updated
            → Event store raises: OptimisticConcurrencyError ❌ REJECTED
```

### What the Losing Agent (Agent B) Receives

Agent B receives an `OptimisticConcurrencyError` exception with details:
```json
{
  "error_type": "OptimisticConcurrencyError",
  "stream_id": "loan-LOAN-999",
  "expected_version": 3,
  "actual_version": 4,
  "suggested_action": "reload_stream_and_retry"
}
```

### What Agent B Must Do Next

1. **Reload the stream** — call `load_stream("loan-LOAN-999")` to get all events including Agent A's new event at position 4
2. **Re-evaluate** — is Agent B's analysis still needed? Agent A's `CreditAnalysisCompleted` event is now present. The business rule says only ONE credit analysis is allowed per application. Agent B should see this and **abort** rather than retry.
3. **If retry is appropriate** — rebuild its aggregate state from the fresh events, recalculate its intended action, and try appending again with `expected_version=4`

**Why no database locks are needed:**

The `WHERE current_version = 3` condition IS the lock. PostgreSQL handles this atomically at the row level. Only one update can match — the other sees 0 rows affected. No explicit transactions spanning multiple aggregates are needed. This scales to thousands of concurrent operations.

---

## Question 4 — Projection Lag and Its Consequences

### The Scenario

The `LoanApplication` projection has a typical lag of **200ms**. A loan officer queries "available credit limit" immediately after an agent commits a `DisbursementCompleted` event. The projection hasn't caught up yet, so the officer sees the **old (higher) credit limit**.

### What My System Does

**At the projection level:**

The `ProjectionDaemon` continuously measures lag:
```
lag = current_global_position_in_events_table 
    - last_global_position_processed_by_projection
```

When lag exceeds our SLO threshold (500ms for `ApplicationSummary`), the daemon emits a warning metric. The `ledger://ledger/health` MCP resource exposes this lag to any monitoring system.

**At the read level:**

When the `ApplicationSummary` projection is queried, the response includes a `data_as_of` timestamp — the global position the projection last processed. The API caller can compare this timestamp to the event timestamp to detect staleness.

Example response:
```json
{
  "application_id": "LOAN-999",
  "available_credit_limit": 500000,
  "data_as_of": "2026-03-17T14:23:45.123Z",
  "is_eventually_consistent": true
}
```

**How this is communicated to the user interface:**

The UI must NOT silently show stale data as if it is current. Three options, in order of preference:

1. **Optimistic UI with refresh** — After a loan officer triggers an action, the UI immediately shows a "processing" state for the credit limit field and re-polls until `data_as_of` is within 500ms of now. This hides lag from the user entirely in normal operation.

2. **Stale data indicator** — Show the credit limit with a small "as of [timestamp]" annotation. The loan officer sees the value is 5 seconds old and knows to wait before making a decision.

3. **Strong consistency for critical reads** — For the specific query "available credit limit before approving disbursement," bypass the projection entirely and load the `LoanApplication` aggregate directly from the event stream. This is slower (it replays events) but guarantees the freshest possible answer. This is used only for the approval action — not for dashboard browsing.

**The key principle:** Never let a UI silently show projection data for a decision that could cause financial harm if the data is stale. The `ApplicationApproved` command handler re-checks business rules against the live aggregate, not the projection — so even if the UI showed stale data, the backend would reject an invalid approval.

---

## Question 5 — The Upcasting Scenario: Schema Evolution Without Breaking the Past

### The Problem

The `CreditDecisionMade` event was defined in 2024:
```json
{
  "application_id": "LOAN-001",
  "decision": "APPROVE",
  "reason": "Strong financials"
}
```

In 2026 it needs to include:
```json
{
  "application_id": "LOAN-001",
  "decision": "APPROVE",
  "reason": "Strong financials",
  "model_version": "credit-model-v3.1",
  "confidence_score": 0.87,
  "regulatory_basis": "Regulation B, Section 202.9"
}
```

**The rule of event sourcing:** You CANNOT modify the stored events. The past is immutable. Instead, you write an **upcaster** — a function that transforms old events into the new shape at read time.

### The Upcaster

```python
from ledger.upcasting import UpcasterRegistry

registry = UpcasterRegistry()

@registry.register("CreditDecisionMade", from_version=1)
def upcast_credit_decision_v1_to_v2(payload: dict) -> dict:
    """
    Transforms a v1 CreditDecisionMade event into v2 shape.
    
    Called automatically whenever a v1 event is loaded from the store.
    The stored event is NEVER touched — this only affects the in-memory
    representation returned to callers.
    """
    return {
        # Preserve all original fields unchanged
        **payload,
        
        # model_version: infer from recorded_at timestamp
        # Events before 2025-01-01 used "legacy-credit-model-v1"
        # Events from 2025-01-01 to 2025-12-31 used "credit-model-v2"
        # Events from 2026-01-01 onward use "credit-model-v3"
        # This inference is imprecise but better than null for analytics.
        "model_version": "legacy-pre-2026",
        
        # confidence_score: genuinely unknown — we do NOT fabricate this.
        # The 2024 model did not produce confidence scores.
        # Setting null is honest. Setting a fake value (e.g. 0.75) would
        # corrupt downstream analytics and regulatory reports.
        "confidence_score": None,
        
        # regulatory_basis: infer from the regulation set active at
        # the event's recorded_at date. Use a lookup table of
        # regulation effective dates.
        "regulatory_basis": _infer_regulatory_basis(
            payload.get("recorded_at")
        ),
    }

def _infer_regulatory_basis(recorded_at: str | None) -> str:
    """
    Returns the regulation set that was active at the time of the event.
    This is a best-effort inference based on effective dates of regulations.
    """
    if recorded_at is None:
        return "UNKNOWN - recorded_at missing from legacy event"
    
    # Pre-2025 events: Regulation B 2024 edition was active
    if recorded_at < "2025-01-01":
        return "Regulation B (2024 Edition), Section 202.9"
    
    # 2025 events: Updated regulation took effect Jan 2025
    if recorded_at < "2026-01-01":
        return "Regulation B (2025 Revision), Section 202.9"
    
    # Should not reach here for v1 events, but defensive default
    return "UNKNOWN - v1 event recorded after regulation update"
```

### My Inference Strategy for `model_version`

**Why I use `"legacy-pre-2026"` instead of null:**

The model version field is used for performance analytics ("has model v2 been worse than v1?"). For events from before 2026, grouping them all under `"legacy-pre-2026"` allows analysts to correctly filter them out of modern comparisons. A null value would cause those events to appear in "model version unknown" buckets, which is less useful.

**Why I use `null` for `confidence_score` instead of inventing a number:**

The confidence score is used in:
1. The `confidence_floor` business rule (REFER if score < 0.6)
2. Regulatory reports that certify the AI's certainty

If I fabricate a confidence score of, say, 0.75 for all legacy events, I am creating false data in a regulatory audit trail. A regulator examining a 2024 decision would see a confidence score that was never actually calculated. This is worse than null — null is honest about the unknown. A fabricated value creates a lie that could survive into compliance reports.

**The rule:** When the correct value is genuinely unknowable, use null and document why. Only infer when the inference has a clear, documented basis and the error rate is acceptable.

---

## Question 6 — The Marten Async Daemon Parallel: Distributed Projections in Python

### What Marten 7.0 Does (the .NET reference)

Marten's Async Daemon in version 7 can run across multiple server nodes simultaneously. It uses PostgreSQL advisory locks to coordinate which node processes which projection shard. If one node crashes, another picks up its work. This allows horizontal scaling of projection processing — you can add more workers and the projection keeps up with a high event write rate.

### How I Achieve the Same Pattern in Python

**The coordination primitive: PostgreSQL Advisory Locks**

PostgreSQL has a built-in mechanism called advisory locks — session-level locks that any application can request by name. They are not tied to a table or row; they are purely application-level. This is perfect for distributed projection daemons.

Here is how I implement it:

```python
import asyncio
import asyncpg

class DistributedProjectionDaemon:
    """
    Multiple instances of this daemon can run simultaneously.
    Only one instance processes each projection shard at a time,
    coordinated via PostgreSQL advisory locks.
    """

    async def acquire_projection_lock(
        self, 
        conn: asyncpg.Connection,
        projection_name: str
    ) -> bool:
        """
        Tries to acquire an advisory lock for this projection.
        Returns True if this worker owns the lock (and should process).
        Returns False if another worker already owns it (skip and wait).
        
        pg_try_advisory_lock is non-blocking — it returns immediately.
        This prevents multiple workers from fighting over the same shard.
        """
        # Convert projection name to a stable integer for the lock key
        lock_key = hash(projection_name) % (2**31)
        
        result = await conn.fetchval(
            "SELECT pg_try_advisory_lock($1)", lock_key
        )
        return result  # True = lock acquired, False = already locked

    async def run_forever(self, projection_name: str) -> None:
        async with self._pool.acquire() as conn:
            while True:
                owns_lock = await self.acquire_projection_lock(
                    conn, projection_name
                )
                
                if owns_lock:
                    # This worker owns this projection — process events
                    await self._process_batch(projection_name, conn)
                else:
                    # Another worker owns this projection — wait and retry
                    await asyncio.sleep(1.0)
```

**What failure mode this guards against:**

**Split-brain projection processing.** Without coordination, two daemons running on two servers could both process the same event batch for the same projection. The projection table would get duplicate updates, potentially double-counting metrics or writing conflicting state.

With advisory locks:
- Worker 1 acquires the lock for `ApplicationSummary` → processes events
- Worker 2 tries to acquire the same lock → `pg_try_advisory_lock` returns False → Worker 2 waits
- Worker 1 crashes mid-batch → PostgreSQL automatically releases the session-level lock
- Worker 2 retries → `pg_try_advisory_lock` returns True → Worker 2 takes over from the last checkpoint

**The checkpoint is the safety net.** Because `projection_checkpoints` records the last successfully processed `global_position`, a new worker that takes over will start exactly where the crashed worker left off — not from the beginning, not from a duplicate position. No events are skipped; no events are double-processed.

**The difference from Marten:** Marten's daemon has sharding built in — you can assign different event types to different nodes. My Python implementation uses one lock per projection. For the scale of this system (commercial loans at Apex), one lock per projection is sufficient. A high-throughput system (millions of events/day) would need to add sharding logic within each projection — but that is a Week 10 concern, not Day 1.

---

## Summary: What Stage Am I At?

```
✅ STEP 1  DOMAIN_NOTES.md  ← YOU ARE HERE (Day 1, complete this first)
⬜ STEP 2  Database Schema   (Phase 1 — CREATE TABLE statements in SQL)
⬜ STEP 3  EventStore class  (Phase 1 — Python async class with append/load)
⬜ STEP 4  Aggregates        (Phase 2 — LoanApplication + AgentSession logic)
⬜ STEP 5  Projections       (Phase 3 — CQRS read models + daemon)
⬜ STEP 6  Upcasting         (Phase 4A — UpcasterRegistry)
⬜ STEP 7  Audit Chain       (Phase 4B — cryptographic hash chain)
⬜ STEP 8  Gas Town Pattern  (Phase 4C — agent memory reconstruction)
⬜ STEP 9  MCP Server        (Phase 5 — tools + resources)
⬜ BONUS   What-If Engine    (Phase 6 — counterfactual projections)
```

---

## How to Verify Step 1 is Complete ✅

Before moving to any code, check every item below:

- [ ] The file is named exactly `DOMAIN_NOTES.md` (capital D, capital N, capital S)
- [ ] All 6 questions are answered
- [ ] Each answer is written in your own words (plain English, no code required)
- [ ] Question 3 traces the EXACT sequence of database operations step by step
- [ ] Question 5 includes the actual upcaster function with your inference reasoning
- [ ] Question 6 names the coordination primitive (PostgreSQL advisory locks) and the failure mode it prevents
- [ ] No code files have been created yet — this document comes FIRST

**What success looks like:**

A grader who has never seen your code reads this file and says: *"This person understands event sourcing, knows why aggregates have boundaries, can reason about concurrent writes, and has a clear opinion on when to infer vs. use null."* That is the bar. The questions are not trivia — they are the exact problems you will hit when building the system.

---

*End of DOMAIN_NOTES.md — Phase 0 complete. Do not write any code until this file is finished and you are satisfied with every answer.*
