"""
Event models for the Apex Ledger event store.

This file is the single source of truth for all event types.
Every agent, test, and projection imports from here.
Never redefine event classes elsewhere.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# =============================================================================
# Custom Exceptions
# =============================================================================

class OptimisticConcurrencyError(Exception):
    """Raised when a stream's actual version does not match expected_version."""

    def __init__(
        self,
        stream_id: str,
        expected_version: int,
        actual_version: int,
    ):
        self.stream_id = stream_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"Concurrency conflict on stream '{stream_id}': "
            f"expected version {expected_version}, actual {actual_version}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_type": "OptimisticConcurrencyError",
            "message": str(self),
            "stream_id": self.stream_id,
            "expected_version": self.expected_version,
            "actual_version": self.actual_version,
            "suggested_action": "reload_stream_and_retry",
        }


class DomainError(Exception):
    """Raised when a business rule is violated in aggregate logic."""

    def __init__(self, message: str, rule: str | None = None):
        self.rule = rule
        super().__init__(message)


class StreamNotFoundError(Exception):
    """Raised when a stream does not exist."""

    def __init__(self, stream_id: str):
        self.stream_id = stream_id
        super().__init__(f"Stream '{stream_id}' not found")


# =============================================================================
# Application State Machine
# =============================================================================

class ApplicationState(str, Enum):
    SUBMITTED = "SUBMITTED"
    AWAITING_ANALYSIS = "AWAITING_ANALYSIS"
    ANALYSIS_COMPLETE = "ANALYSIS_COMPLETE"
    COMPLIANCE_REVIEW = "COMPLIANCE_REVIEW"
    PENDING_DECISION = "PENDING_DECISION"
    APPROVED_PENDING_HUMAN = "APPROVED_PENDING_HUMAN"
    DECLINED_PENDING_HUMAN = "DECLINED_PENDING_HUMAN"
    FINAL_APPROVED = "FINAL_APPROVED"
    FINAL_DECLINED = "FINAL_DECLINED"


# Valid state transitions
VALID_TRANSITIONS: dict[ApplicationState, set[ApplicationState]] = {
    ApplicationState.SUBMITTED: {ApplicationState.AWAITING_ANALYSIS},
    ApplicationState.AWAITING_ANALYSIS: {ApplicationState.ANALYSIS_COMPLETE},
    ApplicationState.ANALYSIS_COMPLETE: {ApplicationState.COMPLIANCE_REVIEW},
    ApplicationState.COMPLIANCE_REVIEW: {ApplicationState.PENDING_DECISION},
    ApplicationState.PENDING_DECISION: {
        ApplicationState.APPROVED_PENDING_HUMAN,
        ApplicationState.DECLINED_PENDING_HUMAN,
    },
    ApplicationState.APPROVED_PENDING_HUMAN: {ApplicationState.FINAL_APPROVED},
    ApplicationState.DECLINED_PENDING_HUMAN: {ApplicationState.FINAL_DECLINED},
    ApplicationState.FINAL_APPROVED: set(),
    ApplicationState.FINAL_DECLINED: set(),
}


# =============================================================================
# Base Event Models
# =============================================================================

class BaseEvent(BaseModel):
    """Base class for all domain events before they are stored."""

    event_type: str
    event_version: int = 1
    payload: dict[str, Any]

    def model_post_init(self, __context: Any) -> None:
        if "event_type" not in self.payload:
            self.payload["event_type"] = self.event_type


class StoredEvent(BaseModel):
    """An event as loaded from the event store (with position metadata)."""

    event_id: uuid.UUID
    stream_id: str
    stream_position: int
    global_position: int
    event_type: str
    event_version: int
    payload: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)
    recorded_at: datetime

    def with_payload(self, new_payload: dict[str, Any], version: int) -> StoredEvent:
        """Return a copy with updated payload and version (used by upcasters)."""
        return self.model_copy(
            update={"payload": new_payload, "event_version": version}
        )


class StreamMetadata(BaseModel):
    """Metadata about an event stream."""

    stream_id: str
    aggregate_type: str
    current_version: int
    created_at: datetime
    archived_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


# =============================================================================
# LoanApplication Aggregate Events
# =============================================================================

class ApplicationSubmitted(BaseEvent):
    event_type: str = "ApplicationSubmitted"

    @classmethod
    def create(
        cls,
        application_id: str,
        applicant_id: str,
        requested_amount_usd: float,
        loan_purpose: str,
        submission_channel: str = "api",
    ) -> ApplicationSubmitted:
        return cls(
            payload={
                "application_id": application_id,
                "applicant_id": applicant_id,
                "requested_amount_usd": requested_amount_usd,
                "loan_purpose": loan_purpose,
                "submission_channel": submission_channel,
                "submitted_at": datetime.utcnow().isoformat(),
            }
        )


class CreditAnalysisRequested(BaseEvent):
    event_type: str = "CreditAnalysisRequested"

    @classmethod
    def create(
        cls,
        application_id: str,
        assigned_agent_id: str,
        priority: str = "normal",
    ) -> CreditAnalysisRequested:
        return cls(
            payload={
                "application_id": application_id,
                "assigned_agent_id": assigned_agent_id,
                "requested_at": datetime.utcnow().isoformat(),
                "priority": priority,
            }
        )


class CreditAnalysisCompleted(BaseEvent):
    event_type: str = "CreditAnalysisCompleted"
    event_version: int = 2

    @classmethod
    def create(
        cls,
        application_id: str,
        agent_id: str,
        session_id: str,
        model_version: str,
        confidence_score: float,
        risk_tier: str,
        recommended_limit_usd: float,
        analysis_duration_ms: int,
        input_data_hash: str,
    ) -> CreditAnalysisCompleted:
        return cls(
            payload={
                "application_id": application_id,
                "agent_id": agent_id,
                "session_id": session_id,
                "model_version": model_version,
                "confidence_score": confidence_score,
                "risk_tier": risk_tier,
                "recommended_limit_usd": recommended_limit_usd,
                "analysis_duration_ms": analysis_duration_ms,
                "input_data_hash": input_data_hash,
            }
        )


class FraudScreeningCompleted(BaseEvent):
    event_type: str = "FraudScreeningCompleted"

    @classmethod
    def create(
        cls,
        application_id: str,
        agent_id: str,
        fraud_score: float,
        anomaly_flags: list[str],
        screening_model_version: str,
        input_data_hash: str,
    ) -> FraudScreeningCompleted:
        return cls(
            payload={
                "application_id": application_id,
                "agent_id": agent_id,
                "fraud_score": fraud_score,
                "anomaly_flags": anomaly_flags,
                "screening_model_version": screening_model_version,
                "input_data_hash": input_data_hash,
            }
        )


class ComplianceCheckRequested(BaseEvent):
    event_type: str = "ComplianceCheckRequested"

    @classmethod
    def create(
        cls,
        application_id: str,
        regulation_set_version: str,
        checks_required: list[str],
    ) -> ComplianceCheckRequested:
        return cls(
            payload={
                "application_id": application_id,
                "regulation_set_version": regulation_set_version,
                "checks_required": checks_required,
            }
        )


class ComplianceRulePassed(BaseEvent):
    event_type: str = "ComplianceRulePassed"

    @classmethod
    def create(
        cls,
        application_id: str,
        rule_id: str,
        rule_version: str,
        evidence_hash: str,
    ) -> ComplianceRulePassed:
        return cls(
            payload={
                "application_id": application_id,
                "rule_id": rule_id,
                "rule_version": rule_version,
                "evaluation_timestamp": datetime.utcnow().isoformat(),
                "evidence_hash": evidence_hash,
            }
        )


class ComplianceRuleFailed(BaseEvent):
    event_type: str = "ComplianceRuleFailed"

    @classmethod
    def create(
        cls,
        application_id: str,
        rule_id: str,
        rule_version: str,
        failure_reason: str,
        remediation_required: bool = False,
    ) -> ComplianceRuleFailed:
        return cls(
            payload={
                "application_id": application_id,
                "rule_id": rule_id,
                "rule_version": rule_version,
                "failure_reason": failure_reason,
                "remediation_required": remediation_required,
            }
        )


class DecisionGenerated(BaseEvent):
    event_type: str = "DecisionGenerated"
    event_version: int = 2

    @classmethod
    def create(
        cls,
        application_id: str,
        orchestrator_agent_id: str,
        recommendation: str,
        confidence_score: float,
        contributing_agent_sessions: list[str],
        decision_basis_summary: str,
        model_versions: dict[str, str],
    ) -> DecisionGenerated:
        return cls(
            payload={
                "application_id": application_id,
                "orchestrator_agent_id": orchestrator_agent_id,
                "recommendation": recommendation,
                "confidence_score": confidence_score,
                "contributing_agent_sessions": contributing_agent_sessions,
                "decision_basis_summary": decision_basis_summary,
                "model_versions": model_versions,
            }
        )


class HumanReviewCompleted(BaseEvent):
    event_type: str = "HumanReviewCompleted"

    @classmethod
    def create(
        cls,
        application_id: str,
        reviewer_id: str,
        override: bool,
        final_decision: str,
        override_reason: str | None = None,
    ) -> HumanReviewCompleted:
        return cls(
            payload={
                "application_id": application_id,
                "reviewer_id": reviewer_id,
                "override": override,
                "final_decision": final_decision,
                "override_reason": override_reason,
            }
        )


class ApplicationApproved(BaseEvent):
    event_type: str = "ApplicationApproved"

    @classmethod
    def create(
        cls,
        application_id: str,
        approved_amount_usd: float,
        interest_rate: float,
        conditions: list[str],
        approved_by: str,
        effective_date: str,
    ) -> ApplicationApproved:
        return cls(
            payload={
                "application_id": application_id,
                "approved_amount_usd": approved_amount_usd,
                "interest_rate": interest_rate,
                "conditions": conditions,
                "approved_by": approved_by,
                "effective_date": effective_date,
            }
        )


class ApplicationDeclined(BaseEvent):
    event_type: str = "ApplicationDeclined"

    @classmethod
    def create(
        cls,
        application_id: str,
        decline_reasons: list[str],
        declined_by: str,
        adverse_action_notice_required: bool = True,
    ) -> ApplicationDeclined:
        return cls(
            payload={
                "application_id": application_id,
                "decline_reasons": decline_reasons,
                "declined_by": declined_by,
                "adverse_action_notice_required": adverse_action_notice_required,
            }
        )


class HumanReviewRequested(BaseEvent):
    event_type: str = "HumanReviewRequested"

    @classmethod
    def create(
        cls,
        application_id: str,
        reason: str,
        assigned_to: str | None = None,
    ) -> HumanReviewRequested:
        return cls(
            payload={
                "application_id": application_id,
                "reason": reason,
                "assigned_to": assigned_to,
                "requested_at": datetime.utcnow().isoformat(),
            }
        )


# =============================================================================
# AgentSession Aggregate Events
# =============================================================================

class AgentSessionStarted(BaseEvent):
    event_type: str = "AgentSessionStarted"

    @classmethod
    def create(
        cls,
        agent_id: str,
        session_id: str,
        agent_type: str,
        model_version: str,
        context_source: str,
        context_token_count: int,
    ) -> AgentSessionStarted:
        return cls(
            payload={
                "agent_id": agent_id,
                "session_id": session_id,
                "agent_type": agent_type,
                "model_version": model_version,
                "context_source": context_source,
                "context_token_count": context_token_count,
                "started_at": datetime.utcnow().isoformat(),
            }
        )


class AgentContextLoaded(BaseEvent):
    event_type: str = "AgentContextLoaded"

    @classmethod
    def create(
        cls,
        agent_id: str,
        session_id: str,
        context_source: str,
        event_replay_from_position: int,
        context_token_count: int,
        model_version: str,
    ) -> AgentContextLoaded:
        return cls(
            payload={
                "agent_id": agent_id,
                "session_id": session_id,
                "context_source": context_source,
                "event_replay_from_position": event_replay_from_position,
                "context_token_count": context_token_count,
                "model_version": model_version,
            }
        )


class AgentSessionCompleted(BaseEvent):
    event_type: str = "AgentSessionCompleted"

    @classmethod
    def create(
        cls,
        agent_id: str,
        session_id: str,
        total_nodes_executed: int,
        total_llm_calls: int,
        total_tokens_used: int,
        total_cost_usd: float,
    ) -> AgentSessionCompleted:
        return cls(
            payload={
                "agent_id": agent_id,
                "session_id": session_id,
                "total_nodes_executed": total_nodes_executed,
                "total_llm_calls": total_llm_calls,
                "total_tokens_used": total_tokens_used,
                "total_cost_usd": total_cost_usd,
                "completed_at": datetime.utcnow().isoformat(),
            }
        )


class AgentSessionFailed(BaseEvent):
    event_type: str = "AgentSessionFailed"

    @classmethod
    def create(
        cls,
        agent_id: str,
        session_id: str,
        error_type: str,
        error_message: str,
        last_successful_node: str | None,
        recoverable: bool,
    ) -> AgentSessionFailed:
        return cls(
            payload={
                "agent_id": agent_id,
                "session_id": session_id,
                "error_type": error_type,
                "error_message": error_message,
                "last_successful_node": last_successful_node,
                "recoverable": recoverable,
            }
        )


class AgentNodeExecuted(BaseEvent):
    event_type: str = "AgentNodeExecuted"

    @classmethod
    def create(
        cls,
        agent_id: str,
        session_id: str,
        node_name: str,
        node_sequence: int,
        input_keys: list[str],
        output_keys: list[str],
        llm_called: bool = False,
        llm_tokens_input: int | None = None,
        llm_tokens_output: int | None = None,
        llm_cost_usd: float | None = None,
        duration_ms: int = 0,
    ) -> AgentNodeExecuted:
        return cls(
            payload={
                "agent_id": agent_id,
                "session_id": session_id,
                "node_name": node_name,
                "node_sequence": node_sequence,
                "input_keys": input_keys,
                "output_keys": output_keys,
                "llm_called": llm_called,
                "llm_tokens_input": llm_tokens_input,
                "llm_tokens_output": llm_tokens_output,
                "llm_cost_usd": llm_cost_usd,
                "duration_ms": duration_ms,
            }
        )


# =============================================================================
# AuditLedger Aggregate Events
# =============================================================================

class AuditIntegrityCheckRun(BaseEvent):
    event_type: str = "AuditIntegrityCheckRun"

    @classmethod
    def create(
        cls,
        entity_id: str,
        check_timestamp: str,
        events_verified_count: int,
        integrity_hash: str,
        previous_hash: str | None,
    ) -> AuditIntegrityCheckRun:
        return cls(
            payload={
                "entity_id": entity_id,
                "check_timestamp": check_timestamp,
                "events_verified_count": events_verified_count,
                "integrity_hash": integrity_hash,
                "previous_hash": previous_hash,
            }
        )


# =============================================================================
# Event Registry — maps event_type string to class
# =============================================================================

EVENT_REGISTRY: dict[str, type[BaseEvent]] = {
    "ApplicationSubmitted": ApplicationSubmitted,
    "CreditAnalysisRequested": CreditAnalysisRequested,
    "CreditAnalysisCompleted": CreditAnalysisCompleted,
    "FraudScreeningCompleted": FraudScreeningCompleted,
    "ComplianceCheckRequested": ComplianceCheckRequested,
    "ComplianceRulePassed": ComplianceRulePassed,
    "ComplianceRuleFailed": ComplianceRuleFailed,
    "DecisionGenerated": DecisionGenerated,
    "HumanReviewRequested": HumanReviewRequested,
    "HumanReviewCompleted": HumanReviewCompleted,
    "ApplicationApproved": ApplicationApproved,
    "ApplicationDeclined": ApplicationDeclined,
    "AgentSessionStarted": AgentSessionStarted,
    "AgentContextLoaded": AgentContextLoaded,
    "AgentSessionCompleted": AgentSessionCompleted,
    "AgentSessionFailed": AgentSessionFailed,
    "AgentNodeExecuted": AgentNodeExecuted,
    "AuditIntegrityCheckRun": AuditIntegrityCheckRun,
}
