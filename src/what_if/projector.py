"""
What-If Projector — counterfactual event injection.

Replays application history with substituted events to answer
questions like: "What would the decision have been if the credit
analysis had returned risk_tier='HIGH' instead of 'MEDIUM'?"

NEVER writes counterfactual events to the real store.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

from src.event_store import EventStore
from src.models.events import BaseEvent, StoredEvent
from src.aggregates.loan_application import LoanApplicationAggregate


@dataclass
class WhatIfResult:
    application_id: str
    branch_point: str
    real_outcome: dict[str, Any]
    counterfactual_outcome: dict[str, Any]
    divergence_events: list[dict[str, Any]]
    events_replayed: int
    events_skipped: int


async def run_what_if(
    store: EventStore,
    application_id: str,
    branch_at_event_type: str,
    counterfactual_events: list[BaseEvent],
    projections: list | None = None,
) -> WhatIfResult:
    """
    Run a what-if scenario on a loan application.

    1. Load all events for the application stream
    2. Find the branch point (first event of branch_at_event_type)
    3. Build real outcome by replaying all events
    4. Build counterfactual:
       - Events before branch point: real events
       - At branch point: inject counterfactual_events
       - After branch point: include causally INDEPENDENT events, skip DEPENDENT ones
    5. Return comparison
    """
    stream_id = f"loan-{application_id}"
    all_events = await store.load_stream(stream_id)

    if not all_events:
        return WhatIfResult(
            application_id=application_id,
            branch_point=branch_at_event_type,
            real_outcome={"error": "No events found"},
            counterfactual_outcome={"error": "No events found"},
            divergence_events=[],
            events_replayed=0,
            events_skipped=0,
        )

    # Find the branch point
    branch_index = None
    for i, event in enumerate(all_events):
        if event.event_type == branch_at_event_type:
            branch_index = i
            break

    if branch_index is None:
        return WhatIfResult(
            application_id=application_id,
            branch_point=branch_at_event_type,
            real_outcome=_aggregate_to_dict(_replay_events(all_events, application_id)),
            counterfactual_outcome={"error": f"No {branch_at_event_type} event found to branch at"},
            divergence_events=[],
            events_replayed=len(all_events),
            events_skipped=0,
        )

    # Real outcome
    real_agg = _replay_events(all_events, application_id)
    real_outcome = _aggregate_to_dict(real_agg)

    # Get metadata from branched events for causal dependency tracking
    branched_event = all_events[branch_index]
    branched_causation_ids = set()
    # The branched event and all events causally dependent on it
    branched_causation_ids.add(str(branched_event.event_id))

    # Build counterfactual event sequence
    pre_branch = all_events[:branch_index]
    post_branch = all_events[branch_index + 1:]

    # Determine which post-branch events are causally dependent
    dependent_types = _get_causally_dependent_types(branch_at_event_type)

    independent_events = []
    skipped_events = []
    for event in post_branch:
        if event.event_type in dependent_types:
            skipped_events.append(event)
        else:
            # Check causation_id chain
            if event.metadata.get("causation_id") in branched_causation_ids:
                skipped_events.append(event)
                branched_causation_ids.add(str(event.event_id))
            else:
                independent_events.append(event)

    # Create synthetic StoredEvents from counterfactual BaseEvents
    import uuid
    from datetime import datetime
    synthetic_events = []
    for i, ce in enumerate(counterfactual_events):
        se = StoredEvent(
            event_id=uuid.uuid4(),
            stream_id=stream_id,
            stream_position=branch_index + 1 + i,
            global_position=0,  # synthetic
            event_type=ce.event_type,
            event_version=ce.event_version,
            payload=ce.payload,
            metadata={"counterfactual": True},
            recorded_at=datetime.utcnow(),
        )
        synthetic_events.append(se)

    # Replay: pre-branch + counterfactual + independent post-branch
    counterfactual_sequence = pre_branch + synthetic_events + independent_events
    cf_agg = _replay_events(counterfactual_sequence, application_id)
    cf_outcome = _aggregate_to_dict(cf_agg)

    # Compute divergence
    divergence = []
    for key in set(list(real_outcome.keys()) + list(cf_outcome.keys())):
        if real_outcome.get(key) != cf_outcome.get(key):
            divergence.append({
                "field": key,
                "real": real_outcome.get(key),
                "counterfactual": cf_outcome.get(key),
            })

    return WhatIfResult(
        application_id=application_id,
        branch_point=branch_at_event_type,
        real_outcome=real_outcome,
        counterfactual_outcome=cf_outcome,
        divergence_events=divergence,
        events_replayed=len(counterfactual_sequence),
        events_skipped=len(skipped_events),
    )


def _replay_events(events: list[StoredEvent], application_id: str) -> LoanApplicationAggregate:
    """Replay events to build aggregate state."""
    agg = LoanApplicationAggregate(application_id=application_id)
    for event in events:
        agg._apply(event)
    return agg


def _aggregate_to_dict(agg: LoanApplicationAggregate) -> dict[str, Any]:
    """Convert aggregate state to a dict for comparison."""
    return {
        "state": agg.state.value if agg.state else None,
        "risk_tier": agg.risk_tier,
        "fraud_score": agg.fraud_score,
        "confidence_score": agg.confidence_score,
        "decision": agg.decision,
        "final_decision": agg.final_decision,
        "approved_amount": agg.approved_amount,
        "credit_analysis_completed": agg.credit_analysis_completed,
        "fraud_screening_completed": agg.fraud_screening_completed,
        "compliance_checks_passed": agg.compliance_checks_passed,
        "compliance_checks_failed": agg.compliance_checks_failed,
    }


def _get_causally_dependent_types(branch_type: str) -> set[str]:
    """
    Return event types that are causally dependent on the branch type.

    If we change CreditAnalysisCompleted, then DecisionGenerated,
    HumanReviewCompleted, ApplicationApproved, ApplicationDeclined
    are all dependent (they reference the credit analysis result).
    """
    dependency_map = {
        "CreditAnalysisCompleted": {
            "DecisionGenerated",
            "HumanReviewRequested",
            "HumanReviewCompleted",
            "ApplicationApproved",
            "ApplicationDeclined",
        },
        "FraudScreeningCompleted": {
            "DecisionGenerated",
            "HumanReviewRequested",
            "HumanReviewCompleted",
            "ApplicationApproved",
            "ApplicationDeclined",
        },
        "DecisionGenerated": {
            "HumanReviewRequested",
            "HumanReviewCompleted",
            "ApplicationApproved",
            "ApplicationDeclined",
        },
    }
    return dependency_map.get(branch_type, set())
