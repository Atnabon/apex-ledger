"""
MCP Server — The Ledger's interface for AI agents and enterprise systems.

Tools (Commands) write events; Resources (Queries) read from projections.
This is structural CQRS — the MCP specification naturally implements
the read/write separation.
"""
from __future__ import annotations
import asyncio
import os
from fastmcp import FastMCP

mcp = FastMCP("apex-ledger", instructions="The Ledger — Agentic Event Store & Enterprise Audit Infrastructure")

# Import tools and resources to register them
from src.mcp.tools import register_tools
from src.mcp.resources import register_resources

_store = None
_pool = None
_daemon = None

async def get_store():
    global _store, _pool
    if _store is None:
        import asyncpg
        from src.event_store import EventStore, _init_connection
        dsn = os.environ.get("DATABASE_URL", "postgresql://localhost/apex_ledger")
        _pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10, init=_init_connection)
        _store = EventStore(_pool)
    return _store

async def get_pool():
    global _pool
    if _pool is None:
        await get_store()
    return _pool

register_tools(mcp)
register_resources(mcp)

def main():
    mcp.run()

if __name__ == "__main__":
    main()
