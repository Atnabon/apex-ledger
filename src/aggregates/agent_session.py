"""
AgentSessionAggregate — consistency boundary for AI agent sessions.

Reconstructed by replaying events from the agent-{agent_id}-{session_id} stream.
Enforces the Gas Town pattern: every session must start with AgentSessionStarted
and have AgentContextLoaded before any decision events can be appended.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.models.events import DomainError, StoredEvent

if TYPE_CHECKING:
    from src.event_store import EventStore


class AgentSessionAggregate:
    """
    Aggregate root for a single agent session.

    Gas Town Pattern:
        AgentSessionStarted must be the first event.
        AgentContextLoaded must appear before any decision event.
        On crash recovery, replay the session stream to reconstruct context.
    """

    def __init__(self, agent_id: str, session_id: str):
        self.agent_id = agent_id
        self.session_id = session_id
        self.version: int = 0
        self.started: bool = False
        self.context_loaded: bool = False
        self.completed: bool = False
        self.failed: bool = False
        self.model_version: str | None = None
        self.agent_type: str | None = None
        self.context_source: str | None = None
        self.context_token_count: int = 0
        self.nodes_executed: list[str] = []
        self.total_llm_calls: int = 0
        self.total_tokens_used: int = 0
        self.total_cost_usd: float = 0.0
        self.last_successful_node: str | None = None
        self.events: list[StoredEvent] = []

    @property
    def stream_id(self) -> str:
        return f"agent-{self.agent_id}-{self.session_id}"

    @classmethod
    async def load(
        cls, store: EventStore, agent_id: str, session_id: str
    ) -> AgentSessionAggregate:
        """Reconstruct aggregate state by replaying the session event stream."""
        stream_id = f"agent-{agent_id}-{session_id}"
        events = await store.load_stream(stream_id)
        agg = cls(agent_id=agent_id, session_id=session_id)
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
        self.events.append(event)

    def _on_AgentSessionStarted(self, event: StoredEvent) -> None:
        self.started = True
        self.agent_type = event.payload.get("agent_type")
        self.model_version = event.payload.get("model_version")
        self.context_source = event.payload.get("context_source")
        self.context_token_count = event.payload.get("context_token_count", 0)

    def _on_AgentContextLoaded(self, event: StoredEvent) -> None:
        self.context_loaded = True
        self.model_version = event.payload.get("model_version", self.model_version)
        self.context_token_count = event.payload.get(
            "context_token_count", self.context_token_count
        )

    def _on_AgentNodeExecuted(self, event: StoredEvent) -> None:
        node_name = event.payload.get("node_name", "unknown")
        self.nodes_executed.append(node_name)
        self.last_successful_node = node_name
        if event.payload.get("llm_called"):
            self.total_llm_calls += 1
            self.total_tokens_used += (
                event.payload.get("llm_tokens_input", 0) or 0
            ) + (event.payload.get("llm_tokens_output", 0) or 0)
            self.total_cost_usd += event.payload.get("llm_cost_usd", 0.0) or 0.0

    def _on_AgentSessionCompleted(self, event: StoredEvent) -> None:
        self.completed = True

    def _on_AgentSessionFailed(self, event: StoredEvent) -> None:
        self.failed = True
        self.last_successful_node = event.payload.get("last_successful_node")

    # ------------------------------------------------------------------
    # Business Rule Assertions (Gas Town Pattern)
    # ------------------------------------------------------------------

    def assert_session_started(self) -> None:
        """Gas Town Rule: Session must be started before any work."""
        if not self.started:
            raise DomainError(
                "AgentSessionStarted must be the first event in a session. "
                "No work can be performed before the session is started.",
                rule="gas_town_session_start",
            )

    def assert_context_loaded(self) -> None:
        """Gas Town Rule: Context must be loaded before any decision event."""
        if not self.context_loaded:
            raise DomainError(
                "AgentContextLoaded must appear before any decision event. "
                "An agent cannot make decisions without declaring its context source.",
                rule="gas_town_context_loaded",
            )

    def assert_not_completed(self) -> None:
        """Cannot append events to a completed or failed session."""
        if self.completed:
            raise DomainError(
                "Session is already completed. Cannot append new events.",
                rule="session_lifecycle",
            )
        if self.failed:
            raise DomainError(
                "Session has failed. Start a recovery session instead.",
                rule="session_lifecycle",
            )

    def assert_model_version_current(self, model_version: str) -> None:
        """Verify the model version matches the session's declared version."""
        if self.model_version and model_version != self.model_version:
            raise DomainError(
                f"Model version mismatch: session declared '{self.model_version}', "
                f"but operation uses '{model_version}'.",
                rule="model_version_consistency",
            )
