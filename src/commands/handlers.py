"""
Command handlers — the write side of CQRS.

Every command handler follows the pattern:
    1. Reconstruct current aggregate state from event history
    2. Validate — all business rules checked BEFORE any state change
    3. Determine new events — pure logic, no I/O
    4. Append atomically — optimistic concurrency enforced by store
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from src.aggregates.agent_session import AgentSessionAggregate
from src.aggregates.loan_application import LoanApplicationAggregate
from src.event_store import EventStore
from src.models.events import (
    ApplicationSubmitted,
    CreditAnalysisCompleted,
    CreditAnalysisRequested,
    ComplianceCheckRequested,
    DecisionGenerated,
    FraudScreeningCompleted,
    HumanReviewCompleted,
    HumanReviewRequested,
    ApplicationApproved,
    ApplicationDeclined,
    AgentSessionStarted,
    AgentContextLoaded,
    DomainError,
)


# =============================================================================
# Command Data Classes
# =============================================================================

@dataclass
class SubmitApplicationCommand:
    application_id: str
    applicant_id: str
    requested_amount_usd: float
    loan_purpose: str
    submission_channel: str = "api"
    correlation_id: str | None = None


@dataclass
class CreditAnalysisCompletedCommand:
    application_id: str
    agent_id: str
    session_id: str
    model_version: str
    confidence_score: float
    risk_tier: str
    recommended_limit_usd: float
    duration_ms: int
    input_data: dict[str, Any] | None = None
    correlation_id: str | None = None
    causation_id: str | None = None


@dataclass
class FraudScreeningCompletedCommand:
    application_id: str
    agent_id: str
    fraud_score: float
    anomaly_flags: list[str]
    screening_model_version: str
    input_data: dict[str, Any] | None = None
    correlation_id: str | None = None
    causation_id: str | None = None


@dataclass
class GenerateDecisionCommand:
    application_id: str
    orchestrator_agent_id: str
    recommendation: str
    confidence_score: float
    contributing_agent_sessions: list[str]
    decision_basis_summary: str
    model_versions: dict[str, str]
    correlation_id: str | None = None
    causation_id: str | None = None


@dataclass
class HumanReviewCompletedCommand:
    application_id: str
    reviewer_id: str
    override: bool
    final_decision: str
    override_reason: str | None = None
    correlation_id: str | None = None


@dataclass
class StartAgentSessionCommand:
    agent_id: str
    session_id: str
    agent_type: str
    model_version: str
    context_source: str
    context_token_count: int
    correlation_id: str | None = None


# =============================================================================
# Utility
# =============================================================================

def hash_inputs(data: dict[str, Any] | None) -> str:
    if data is None:
        return "none"
    serialized = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode()).hexdigest()[:16]


# =============================================================================
# Command Handlers
# =============================================================================

async def handle_submit_application(
    cmd: SubmitApplicationCommand,
    store: EventStore,
) -> int:
    """
    Handle a new loan application submission.
    Creates the loan-{application_id} stream.
    Returns the new stream version.
    """
    # Check that the stream does not already exist
    existing_version = await store.stream_version(f"loan-{cmd.application_id}")
    if existing_version != -1:
        raise DomainError(
            f"Application {cmd.application_id} already exists",
            rule="duplicate_application",
        )

    event = ApplicationSubmitted.create(
        application_id=cmd.application_id,
        applicant_id=cmd.applicant_id,
        requested_amount_usd=cmd.requested_amount_usd,
        loan_purpose=cmd.loan_purpose,
        submission_channel=cmd.submission_channel,
    )

    return await store.append(
        stream_id=f"loan-{cmd.application_id}",
        events=[event],
        expected_version=-1,
        correlation_id=cmd.correlation_id,
    )


async def handle_credit_analysis_completed(
    cmd: CreditAnalysisCompletedCommand,
    store: EventStore,
) -> int:
    """
    Record the completion of a credit analysis by an AI agent.
    Validates against both LoanApplication and AgentSession aggregates.
    """
    # 1. Reconstruct aggregate states
    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    agent = await AgentSessionAggregate.load(store, cmd.agent_id, cmd.session_id)

    # 2. Validate business rules
    app.assert_awaiting_credit_analysis()
    app.assert_no_duplicate_credit_analysis()
    agent.assert_session_started()
    agent.assert_context_loaded()
    agent.assert_model_version_current(cmd.model_version)

    # 3. Determine new events
    new_events = [
        CreditAnalysisCompleted.create(
            application_id=cmd.application_id,
            agent_id=cmd.agent_id,
            session_id=cmd.session_id,
            model_version=cmd.model_version,
            confidence_score=cmd.confidence_score,
            risk_tier=cmd.risk_tier,
            recommended_limit_usd=cmd.recommended_limit_usd,
            analysis_duration_ms=cmd.duration_ms,
            input_data_hash=hash_inputs(cmd.input_data),
        ),
    ]

    # 4. Append atomically
    return await store.append(
        stream_id=f"loan-{cmd.application_id}",
        events=new_events,
        expected_version=app.version,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
    )


async def handle_fraud_screening_completed(
    cmd: FraudScreeningCompletedCommand,
    store: EventStore,
) -> int:
    """Record the completion of fraud screening."""
    app = await LoanApplicationAggregate.load(store, cmd.application_id)

    if not app.credit_analysis_completed:
        raise DomainError(
            "Credit analysis must complete before fraud screening",
            rule="analysis_ordering",
        )

    event = FraudScreeningCompleted.create(
        application_id=cmd.application_id,
        agent_id=cmd.agent_id,
        fraud_score=cmd.fraud_score,
        anomaly_flags=cmd.anomaly_flags,
        screening_model_version=cmd.screening_model_version,
        input_data_hash=hash_inputs(cmd.input_data),
    )

    return await store.append(
        stream_id=f"loan-{cmd.application_id}",
        events=[event],
        expected_version=app.version,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
    )


async def handle_generate_decision(
    cmd: GenerateDecisionCommand,
    store: EventStore,
) -> int:
    """Generate a decision for the loan application."""
    app = await LoanApplicationAggregate.load(store, cmd.application_id)

    # Validate: both credit and fraud analysis must be complete
    if not app.credit_analysis_completed:
        raise DomainError("Credit analysis required before decision", rule="analysis_required")
    if not app.fraud_screening_completed:
        raise DomainError("Fraud screening required before decision", rule="analysis_required")

    # Rule 4: Confidence floor enforcement
    recommendation = app.assert_valid_orchestrator_decision(
        recommendation=cmd.recommendation,
        confidence_score=cmd.confidence_score,
        contributing_sessions=cmd.contributing_agent_sessions,
    )

    event = DecisionGenerated.create(
        application_id=cmd.application_id,
        orchestrator_agent_id=cmd.orchestrator_agent_id,
        recommendation=recommendation,
        confidence_score=cmd.confidence_score,
        contributing_agent_sessions=cmd.contributing_agent_sessions,
        decision_basis_summary=cmd.decision_basis_summary,
        model_versions=cmd.model_versions,
    )

    return await store.append(
        stream_id=f"loan-{cmd.application_id}",
        events=[event],
        expected_version=app.version,
        correlation_id=cmd.correlation_id,
        causation_id=cmd.causation_id,
    )


async def handle_human_review_completed(
    cmd: HumanReviewCompletedCommand,
    store: EventStore,
) -> int:
    """Record the completion of a human review."""
    app = await LoanApplicationAggregate.load(store, cmd.application_id)

    if cmd.override and not cmd.override_reason:
        raise DomainError(
            "Override reason is required when overriding an AI decision",
            rule="override_reason_required",
        )

    event = HumanReviewCompleted.create(
        application_id=cmd.application_id,
        reviewer_id=cmd.reviewer_id,
        override=cmd.override,
        final_decision=cmd.final_decision,
        override_reason=cmd.override_reason,
    )

    return await store.append(
        stream_id=f"loan-{cmd.application_id}",
        events=[event],
        expected_version=app.version,
        correlation_id=cmd.correlation_id,
    )


async def handle_start_agent_session(
    cmd: StartAgentSessionCommand,
    store: EventStore,
) -> int:
    """
    Start an agent session — Gas Town pattern.
    This must be called before any agent work.
    Creates the agent-{agent_id}-{session_id} stream.
    """
    stream_id = f"agent-{cmd.agent_id}-{cmd.session_id}"

    events = [
        AgentSessionStarted.create(
            agent_id=cmd.agent_id,
            session_id=cmd.session_id,
            agent_type=cmd.agent_type,
            model_version=cmd.model_version,
            context_source=cmd.context_source,
            context_token_count=cmd.context_token_count,
        ),
        AgentContextLoaded.create(
            agent_id=cmd.agent_id,
            session_id=cmd.session_id,
            context_source=cmd.context_source,
            event_replay_from_position=0,
            context_token_count=cmd.context_token_count,
            model_version=cmd.model_version,
        ),
    ]

    return await store.append(
        stream_id=stream_id,
        events=events,
        expected_version=-1,
        correlation_id=cmd.correlation_id,
    )
