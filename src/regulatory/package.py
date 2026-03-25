"""
Regulatory Examination Package Generator.

Produces a self-contained JSON examination package containing:
- Complete event stream, in order, with full payloads
- Projection states at examination_date
- Audit chain integrity verification result
- Human-readable narrative of application lifecycle
- Model versions, confidence scores, and input data hashes
"""
from __future__ import annotations
import json
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any

from src.event_store import EventStore
from src.integrity.audit_chain import run_integrity_check, hash_event


@dataclass
class RegulatoryPackage:
    application_id: str
    examination_date: str
    generated_at: str
    event_stream: list[dict[str, Any]]
    integrity_verification: dict[str, Any]
    agent_metadata: list[dict[str, Any]]
    lifecycle_narrative: str
    total_events: int

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)


async def generate_regulatory_package(
    store: EventStore,
    application_id: str,
    examination_date: str | None = None,
) -> RegulatoryPackage:
    """
    Generate a complete, self-contained examination package.

    The package can be verified against the database independently —
    a regulator should not need to trust the system to validate accuracy.
    """
    if examination_date is None:
        examination_date = datetime.utcnow().isoformat()

    # 1. Load complete event stream
    stream_id = f"loan-{application_id}"
    events = await store.load_stream(stream_id)

    event_stream = []
    for e in events:
        event_stream.append({
            "event_id": str(e.event_id),
            "stream_position": e.stream_position,
            "global_position": e.global_position,
            "event_type": e.event_type,
            "event_version": e.event_version,
            "payload": e.payload,
            "metadata": e.metadata,
            "recorded_at": str(e.recorded_at),
            "payload_hash": hash_event(e),
        })

    # 2. Run integrity verification
    try:
        integrity_result = await run_integrity_check(store, "loan", application_id)
        integrity_verification = {
            "chain_valid": integrity_result.chain_valid,
            "tamper_detected": integrity_result.tamper_detected,
            "events_verified": integrity_result.events_verified,
            "integrity_hash": integrity_result.integrity_hash,
            "details": integrity_result.details,
        }
    except Exception as e:
        integrity_verification = {
            "chain_valid": False,
            "tamper_detected": False,
            "events_verified": 0,
            "error": str(e),
        }

    # 3. Collect agent metadata
    agent_metadata = []
    agent_session_ids = set()
    for e in events:
        if e.event_type == "CreditAnalysisCompleted":
            agent_metadata.append({
                "agent_type": "CreditAnalysis",
                "agent_id": e.payload.get("agent_id"),
                "session_id": e.payload.get("session_id"),
                "model_version": e.payload.get("model_version"),
                "confidence_score": e.payload.get("confidence_score"),
                "input_data_hash": e.payload.get("input_data_hash"),
            })
            if e.payload.get("session_id"):
                agent_session_ids.add((e.payload["agent_id"], e.payload["session_id"]))
        elif e.event_type == "FraudScreeningCompleted":
            agent_metadata.append({
                "agent_type": "FraudDetection",
                "agent_id": e.payload.get("agent_id"),
                "model_version": e.payload.get("screening_model_version"),
                "fraud_score": e.payload.get("fraud_score"),
                "input_data_hash": e.payload.get("input_data_hash"),
            })
        elif e.event_type == "DecisionGenerated":
            agent_metadata.append({
                "agent_type": "DecisionOrchestrator",
                "agent_id": e.payload.get("orchestrator_agent_id"),
                "model_versions": e.payload.get("model_versions"),
                "confidence_score": e.payload.get("confidence_score"),
                "recommendation": e.payload.get("recommendation"),
            })

    # Load agent session details
    for agent_id, session_id in agent_session_ids:
        try:
            session_events = await store.load_stream(f"agent-{agent_id}-{session_id}")
            if session_events:
                started = session_events[0]
                completed = [e for e in session_events if e.event_type == "AgentSessionCompleted"]
                agent_metadata.append({
                    "agent_type": started.payload.get("agent_type", "unknown"),
                    "session_stream": f"agent-{agent_id}-{session_id}",
                    "total_events": len(session_events),
                    "model_version": started.payload.get("model_version"),
                    "completed": len(completed) > 0,
                    "total_nodes": len([e for e in session_events if e.event_type == "AgentNodeExecuted"]),
                })
        except Exception:
            pass

    # 4. Generate lifecycle narrative
    narrative = _generate_narrative(events, application_id)

    return RegulatoryPackage(
        application_id=application_id,
        examination_date=examination_date,
        generated_at=datetime.utcnow().isoformat(),
        event_stream=event_stream,
        integrity_verification=integrity_verification,
        agent_metadata=agent_metadata,
        lifecycle_narrative=narrative,
        total_events=len(events),
    )


def _generate_narrative(events: list, application_id: str) -> str:
    """Generate a human-readable narrative of the application lifecycle."""
    lines = [f"Application {application_id} Lifecycle Narrative", "=" * 50, ""]

    for e in events:
        et = e.event_type
        p = e.payload
        ts = str(e.recorded_at)[:19]

        if et == "ApplicationSubmitted":
            lines.append(f"[{ts}] Application submitted by applicant {p.get('applicant_id')} "
                        f"requesting ${p.get('requested_amount_usd', 0):,.2f} for {p.get('loan_purpose', 'unspecified purpose')}.")
        elif et == "CreditAnalysisRequested":
            lines.append(f"[{ts}] Credit analysis requested, assigned to agent {p.get('assigned_agent_id')}.")
        elif et == "CreditAnalysisCompleted":
            lines.append(f"[{ts}] Credit analysis completed by agent {p.get('agent_id')} "
                        f"(model {p.get('model_version')}): risk tier {p.get('risk_tier')}, "
                        f"confidence {p.get('confidence_score')}, recommended limit ${p.get('recommended_limit_usd', 0):,.2f}.")
        elif et == "FraudScreeningCompleted":
            flags = p.get('anomaly_flags', [])
            lines.append(f"[{ts}] Fraud screening completed: score {p.get('fraud_score')}, "
                        f"{len(flags)} anomaly flag(s) detected{': ' + ', '.join(flags) if flags else ''}.")
        elif et == "ComplianceCheckRequested":
            lines.append(f"[{ts}] Compliance check requested under regulation set {p.get('regulation_set_version')}.")
        elif et == "ComplianceRulePassed":
            lines.append(f"[{ts}] Compliance rule {p.get('rule_id')} (v{p.get('rule_version')}): PASSED.")
        elif et == "ComplianceRuleFailed":
            lines.append(f"[{ts}] Compliance rule {p.get('rule_id')} (v{p.get('rule_version')}): FAILED — {p.get('failure_reason')}.")
        elif et == "DecisionGenerated":
            lines.append(f"[{ts}] Decision generated by orchestrator {p.get('orchestrator_agent_id')}: "
                        f"{p.get('recommendation')} (confidence {p.get('confidence_score')}). "
                        f"Basis: {p.get('decision_basis_summary', 'N/A')}.")
        elif et == "HumanReviewRequested":
            lines.append(f"[{ts}] Human review requested: {p.get('reason')}.")
        elif et == "HumanReviewCompleted":
            override = " (OVERRIDE)" if p.get('override') else ""
            lines.append(f"[{ts}] Human review completed by {p.get('reviewer_id')}: "
                        f"{p.get('final_decision')}{override}.")
        elif et == "ApplicationApproved":
            lines.append(f"[{ts}] APPLICATION APPROVED for ${p.get('approved_amount_usd', 0):,.2f} "
                        f"at {p.get('interest_rate', 0):.1%} by {p.get('approved_by')}.")
        elif et == "ApplicationDeclined":
            lines.append(f"[{ts}] APPLICATION DECLINED by {p.get('declined_by')}. "
                        f"Reasons: {', '.join(p.get('decline_reasons', []))}.")
        else:
            lines.append(f"[{ts}] {et}: {json.dumps(p, default=str)[:100]}...")

    lines.append("")
    lines.append(f"Total events in lifecycle: {len(events)}")
    return "\n".join(lines)
