"""
UpcasterRegistry — automatic event version migration on load.

When old events are loaded from the store, the registry transparently
applies version migration chains so callers always see the latest schema.
"""
from __future__ import annotations
from typing import Callable, Any
from src.models.events import StoredEvent


class UpcasterRegistry:
    def __init__(self):
        self._upcasters: dict[tuple[str, int], Callable[[dict], dict]] = {}

    def register(self, event_type: str, from_version: int):
        """Decorator. Registers fn as upcaster from event_type@from_version."""
        def decorator(fn: Callable[[dict], dict]) -> Callable[[dict], dict]:
            self._upcasters[(event_type, from_version)] = fn
            return fn
        return decorator

    def upcast(self, event: StoredEvent) -> StoredEvent:
        """Apply all registered upcasters for this event type in version order."""
        current = event
        v = event.event_version
        while (event.event_type, v) in self._upcasters:
            new_payload = self._upcasters[(event.event_type, v)](current.payload)
            current = current.with_payload(new_payload, version=v + 1)
            v += 1
        return current

    def upcast_batch(self, events: list[StoredEvent]) -> list[StoredEvent]:
        """Upcast a list of events."""
        return [self.upcast(e) for e in events]


# Global singleton
registry = UpcasterRegistry()
