"""
LoanApplicationAggregate — consistency boundary for loan lifecycle.

Reconstructed by replaying events from the loan-{application_id} stream.
All business rules are enforced here before new events are appended.
"""

from __future__ import annotations

from src.models.events import (
    ApplicationState,
    DomainError,
    StoredEvent,
    VALID_TRANSITIONS,
)

# Importing EventStore type for type hints only (avoid circular)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.event_store import EventStore


class LoanApplicationAggregate:
    """
    Aggregate root for a single loan application.

    State machine:
        Submitted -> AwaitingAnalysis -> AnalysisComplete ->
        ComplianceReview -> PendingDecision ->
        ApprovedPendingHuman / DeclinedPendingHuman ->
        FinalApproved / FinalDeclined
    """

    def __init__(self, application_id: str):
        self.application_id = application_id
        self.version: int = 0
        self.state: ApplicationState | None = None
        self.applicant_id: str | None = None
        self.requested_amount: float | None = None
        self.loan_purpose: str | None = None
        self.approved_amount: float | None = None
        self.risk_tier: str | None = None
        self.fraud_score: float | None = None
        self.confidence_score: float | None = None
        self.decision: str | None = None
        self.final_decision: str | None = None
        self.human_reviewer_id: str | None = None
        self.contributing_agent_sessions: list[str] = []
        self.credit_analysis_completed: bool = False
        self.fraud_screening_completed: bool = False
        self.compliance_checks_required: list[str] = []
        self.compliance_checks_passed: list[str] = []
        self.compliance_checks_failed: list[str] = []
        self.has_human_review_override: bool = False

    @classmethod
    async def load(
        cls, store: EventStore, application_id: str
    ) -> LoanApplicationAggregate:
        """Reconstruct aggregate state by replaying the event stream."""
        events = await store.load_stream(f"loan-{application_id}")
        agg = cls(application_id=application_id)
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

    def _on_ApplicationSubmitted(self, event: StoredEvent) -> None:
        self.state = ApplicationState.SUBMITTED
        self.applicant_id = event.payload["applicant_id"]
        self.requested_amount = event.payload["requested_amount_usd"]
        self.loan_purpose = event.payload.get("loan_purpose")

    def _on_CreditAnalysisRequested(self, event: StoredEvent) -> None:
        self.state = ApplicationState.AWAITING_ANALYSIS

    def _on_CreditAnalysisCompleted(self, event: StoredEvent) -> None:
        self.credit_analysis_completed = True
        self.risk_tier = event.payload.get("risk_tier")
        self.confidence_score = event.payload.get("confidence_score")

    def _on_FraudScreeningCompleted(self, event: StoredEvent) -> None:
        self.fraud_screening_completed = True
        self.fraud_score = event.payload.get("fraud_score")
        self.state = ApplicationState.ANALYSIS_COMPLETE

    def _on_ComplianceCheckRequested(self, event: StoredEvent) -> None:
        self.state = ApplicationState.COMPLIANCE_REVIEW
        self.compliance_checks_required = event.payload.get("checks_required", [])

    def _on_ComplianceRulePassed(self, event: StoredEvent) -> None:
        rule_id = event.payload.get("rule_id")
        if rule_id and rule_id not in self.compliance_checks_passed:
            self.compliance_checks_passed.append(rule_id)

    def _on_ComplianceRuleFailed(self, event: StoredEvent) -> None:
        rule_id = event.payload.get("rule_id")
        if rule_id and rule_id not in self.compliance_checks_failed:
            self.compliance_checks_failed.append(rule_id)

    def _on_DecisionGenerated(self, event: StoredEvent) -> None:
        self.state = ApplicationState.PENDING_DECISION
        self.decision = event.payload.get("recommendation")
        self.confidence_score = event.payload.get("confidence_score")
        self.contributing_agent_sessions = event.payload.get(
            "contributing_agent_sessions", []
        )

    def _on_HumanReviewRequested(self, event: StoredEvent) -> None:
        pass  # State stays at PENDING_DECISION until review completes

    def _on_HumanReviewCompleted(self, event: StoredEvent) -> None:
        self.human_reviewer_id = event.payload.get("reviewer_id")
        self.has_human_review_override = event.payload.get("override", False)
        final = event.payload.get("final_decision", "").upper()
        if final in ("APPROVE", "APPROVED"):
            self.state = ApplicationState.APPROVED_PENDING_HUMAN
        elif final in ("DECLINE", "DECLINED"):
            self.state = ApplicationState.DECLINED_PENDING_HUMAN

    def _on_ApplicationApproved(self, event: StoredEvent) -> None:
        self.state = ApplicationState.FINAL_APPROVED
        self.approved_amount = event.payload.get("approved_amount_usd")
        self.final_decision = "APPROVED"

    def _on_ApplicationDeclined(self, event: StoredEvent) -> None:
        self.state = ApplicationState.FINAL_DECLINED
        self.final_decision = "DECLINED"

    # ------------------------------------------------------------------
    # Business Rule Assertions
    # ------------------------------------------------------------------

    def assert_valid_transition(self, target: ApplicationState) -> None:
        """Rule 1: Application state machine — only valid transitions allowed."""
        if self.state is None:
            if target != ApplicationState.SUBMITTED:
                raise DomainError(
                    f"New application must start in SUBMITTED state, not {target}",
                    rule="state_machine",
                )
            return
        allowed = VALID_TRANSITIONS.get(self.state, set())
        if target not in allowed:
            raise DomainError(
                f"Invalid state transition: {self.state.value} -> {target.value}. "
                f"Allowed: {[s.value for s in allowed]}",
                rule="state_machine",
            )

    def assert_awaiting_credit_analysis(self) -> None:
        """Verify the application is in a state that accepts credit analysis."""
        if self.state not in (
            ApplicationState.AWAITING_ANALYSIS,
            ApplicationState.SUBMITTED,
        ):
            raise DomainError(
                f"Cannot accept credit analysis in state {self.state}",
                rule="awaiting_analysis",
            )

    def assert_no_duplicate_credit_analysis(self) -> None:
        """Rule 3: Model version locking — no duplicate credit analysis
        unless superseded by HumanReviewOverride."""
        if self.credit_analysis_completed and not self.has_human_review_override:
            raise DomainError(
                "CreditAnalysisCompleted already recorded for this application. "
                "A new analysis requires a HumanReviewOverride first.",
                rule="model_version_locking",
            )

    def assert_confidence_floor(self, confidence_score: float) -> str:
        """Rule 4: Confidence floor — score < 0.6 forces REFER."""
        if confidence_score < 0.6:
            return "REFER"
        return ""  # No override needed

    def assert_compliance_complete(self) -> None:
        """Rule 5: Compliance dependency — cannot approve without all checks passed."""
        if self.compliance_checks_required:
            missing = set(self.compliance_checks_required) - set(
                self.compliance_checks_passed
            )
            failed = set(self.compliance_checks_failed)
            if missing:
                raise DomainError(
                    f"Cannot approve: compliance checks not completed: {missing}",
                    rule="compliance_dependency",
                )
            if failed:
                raise DomainError(
                    f"Cannot approve: compliance checks failed: {failed}",
                    rule="compliance_dependency",
                )

    def assert_valid_orchestrator_decision(
        self,
        recommendation: str,
        confidence_score: float,
        contributing_sessions: list[str],
    ) -> str:
        """
        Rule 4 + Rule 6: Validate orchestrator decision.
        Returns the (possibly overridden) recommendation.
        """
        # Rule 4: Confidence floor enforcement
        if confidence_score < 0.6 and recommendation == "APPROVE":
            recommendation = "REFER"

        return recommendation

    def assert_causal_chain(
        self,
        contributing_sessions: list[str],
        valid_session_ids: set[str],
    ) -> None:
        """Rule 6: Causal chain enforcement — all referenced sessions must exist
        and contain a decision event for this application."""
        invalid = set(contributing_sessions) - valid_session_ids
        if invalid:
            raise DomainError(
                f"Contributing sessions not found or did not process "
                f"this application: {invalid}",
                rule="causal_chain",
            )
