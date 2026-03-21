# Apex Ledger — Agentic Event Store & Enterprise Audit Infrastructure

An event-sourced system for processing commercial loan applications with multi-agent AI collaboration. Built for the TRP1 FDE Program, Arc 5.

## Overview

Apex Ledger is the immutable memory and governance backbone for multi-agent AI systems. It provides:

- **Append-only event store** with optimistic concurrency control
- **Aggregate-based domain logic** (LoanApplication, AgentSession)
- **CQRS command handlers** with full business rule enforcement
- **Gas Town pattern** for agent crash recovery
- **Cryptographic audit chains** for regulatory compliance

## Prerequisites

- Python 3.12+
- PostgreSQL 15+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Quick Start

### 1. Install Dependencies

```bash
# Using uv (recommended)
uv sync

# Or using pip
pip install -e ".[dev]"
```

### 2. Create Database

```bash
createdb apex_ledger
createdb apex_ledger_test  # for tests
```

### 3. Run Migrations

```bash
psql apex_ledger < db/schema.sql
psql apex_ledger_test < db/schema.sql
```

### 4. Environment Setup

```bash
cp .env.example .env
# Edit .env with your database credentials
```

Required environment variables:
```
DATABASE_URL=postgresql://localhost/apex_ledger
```

### 5. Run Tests

```bash
# Run all tests
pytest tests/ -v

# Run concurrency test specifically
pytest tests/test_concurrency.py -v
```

## Project Structure

```
apex-ledger/
├── db/
│   └── schema.sql                  # PostgreSQL schema (events, streams, outbox, snapshots)
├── src/
│   ├── __init__.py
│   ├── event_store.py              # EventStore async class (append, load_stream, load_all)
│   ├── models/
│   │   └── events.py               # Pydantic event models, exceptions, state machine
│   ├── aggregates/
│   │   ├── loan_application.py     # LoanApplicationAggregate with 6 business rules
│   │   └── agent_session.py        # AgentSessionAggregate with Gas Town enforcement
│   └── commands/
│       └── handlers.py             # Command handlers (submit, credit, fraud, decision, review)
├── tests/
│   └── test_concurrency.py         # Double-decision concurrency test
├── docs/
│   └── DOMAIN_NOTES.md             # Domain analysis (graded deliverable)
├── pyproject.toml
└── README.md
```

## Architecture

The system follows Event Sourcing + CQRS:

- **Write side**: Commands -> Aggregates -> Events -> Event Store
- **Read side**: Event Store -> Projections -> Query APIs (coming in final submission)
- **Concurrency**: Optimistic concurrency control via `expected_version`
- **Auditability**: Every decision is an immutable event with causal chains

## Key Design Decisions

1. **PostgreSQL as event store** — ubiquitous, ACID-compliant, supports LISTEN/NOTIFY
2. **Separate aggregates per concern** — LoanApplication, AgentSession, ComplianceRecord, AuditLedger prevent write contention
3. **Gas Town pattern** — agents write session-start events before any work, enabling crash recovery
4. **Outbox pattern** — events written to outbox in same transaction for guaranteed downstream delivery

## Interim Submission Status

- [x] Phase 1: Event Store Core (schema, EventStore class, OCC)
- [x] Phase 2: Domain Logic (LoanApplication + AgentSession aggregates, command handlers)
- [x] Concurrency Test (double-decision test)
- [ ] Phase 3: Projections & Async Daemon
- [ ] Phase 4: Upcasting & Integrity
- [ ] Phase 5: MCP Server
- [ ] Phase 6: What-If & Regulatory Package
