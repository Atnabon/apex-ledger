"""
ComplianceRecordAggregate — consistency boundary for regulatory compliance checks.

Reconstructed by replaying events from the compliance-{application_id} stream.
Tracks all compliance checks for an application and enforces that mandatory rules
are evaluated before a compliance clearance can be issued.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.models.events import DomainError, StoredEvent

if TYPE_CHECKING:
    from src.event_store import EventStore


class ComplianceRecordAggregate:
    """
    Aggregate root for regulatory compliance checks on a loan application.

    Lifecycle:
        ComplianceCheckInitiated -> ComplianceCheckRequested ->
        (ComplianceRulePassed | ComplianceRuleFailed | ComplianceRuleNoted)* ->
        ComplianceCheckCompleted
    """

    def __init__(self, application_id: str):
        self.application_id = application_id
        self.version: int = 0
        self.checks_required: list[str] = []
        self.checks_passed: list[str] = []
        self.checks_failed: list[str] = []
        self.checks_noted: list[str] = []
        self.regulation_set_version: str | None = None
        self.initiated: bool = False
        self.completed: bool = False
        self.all_passed: bool = False

    @property
    def stream_id(self) -> str:
        return f"compliance-{self.application_id}"

    @classmethod
    async def load(
        cls, store: EventStore, application_id: str
    ) -> ComplianceRecordAggregate:
        """Reconstruct aggregate state by replaying the event stream."""
        stream_id = f"compliance-{application_id}"
        events = await store.load_stream(stream_id)
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

    def _on_ComplianceCheckInitiated(self, event: StoredEvent) -> None:
        self.initiated = True
        self.regulation_set_version = event.payload.get("regulation_set_version")

    def _on_ComplianceCheckRequested(self, event: StoredEvent) -> None:
        self.checks_required = event.payload.get("checks_required", [])
        self.regulation_set_version = event.payload.get(
            "regulation_set_version", self.regulation_set_version
        )

    def _on_ComplianceRulePassed(self, event: StoredEvent) -> None:
        rule_id = event.payload.get("rule_id")
        if rule_id and rule_id not in self.checks_passed:
            self.checks_passed.append(rule_id)

    def _on_ComplianceRuleFailed(self, event: StoredEvent) -> None:
        rule_id = event.payload.get("rule_id")
        if rule_id and rule_id not in self.checks_failed:
            self.checks_failed.append(rule_id)

    def _on_ComplianceRuleNoted(self, event: StoredEvent) -> None:
        rule_id = event.payload.get("rule_id")
        if rule_id and rule_id not in self.checks_noted:
            self.checks_noted.append(rule_id)

    def _on_ComplianceCheckCompleted(self, event: StoredEvent) -> None:
        self.completed = True
        self.all_passed = len(self.checks_failed) == 0

    # ------------------------------------------------------------------
    # Business Rule Assertions
    # ------------------------------------------------------------------

    def assert_initiated(self) -> None:
        """Check must be initiated before rules can be evaluated."""
        if not self.initiated:
            raise DomainError(
                "ComplianceCheckInitiated must be recorded before any rules "
                "can be evaluated. Initiate the compliance check first.",
                rule="compliance_initiated",
            )

    def assert_not_completed(self) -> None:
        """Cannot add rules after check is completed."""
        if self.completed:
            raise DomainError(
                "Compliance check is already completed. "
                "Cannot add or modify rule results after completion.",
                rule="compliance_lifecycle",
            )

    def assert_all_required_checked(self) -> None:
        """All required checks must have a result before completing."""
        evaluated = set(self.checks_passed) | set(self.checks_failed) | set(
            self.checks_noted
        )
        missing = set(self.checks_required) - evaluated
        if missing:
            raise DomainError(
                f"Cannot complete compliance check: the following required "
                f"rules have not been evaluated: {sorted(missing)}",
                rule="all_required_checked",
            )

    def assert_clearance_valid(self) -> None:
        """Cannot issue clearance if any mandatory check failed."""
        if self.checks_failed:
            raise DomainError(
                f"Cannot issue compliance clearance: mandatory checks failed: "
                f"{sorted(self.checks_failed)}",
                rule="clearance_valid",
            )
