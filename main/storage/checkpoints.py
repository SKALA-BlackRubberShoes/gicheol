"""Async SQLite checkpoint lifetime and fail-closed resume validation."""
from __future__ import annotations

from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from typing import Mapping

from pydantic import BaseModel

from main.graph.supervisor_state import StateIntegrityError, SupervisorState, validate_state
from main.graph.supervisor_workflow import validate_resume_versions


@asynccontextmanager
async def open_checkpointer(path: str | Path):
    """Open a durable LangGraph saver, including its async connection lifetime."""
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from main.agents.supervisor import contracts
    database = Path(path).expanduser().absolute()
    if database.is_symlink():
        raise StateIntegrityError("checkpoint database cannot be a symlink")
    database.parent.mkdir(parents=True, exist_ok=True)
    allowed = [(contracts.__name__, name) for name, value in vars(contracts).items()
               if isinstance(value, type) and value.__module__ == contracts.__name__
               and issubclass(value, (BaseModel, Enum))]
    serializer = JsonPlusSerializer(pickle_fallback=False,
                                    allowed_json_modules=allowed,
                                    allowed_msgpack_modules=allowed)
    async with aiosqlite.connect(str(database)) as connection:
        saver = AsyncSqliteSaver(connection, serde=serializer)
        await saver.setup()
        # WAL recovers complete transactions on reopen; FULL fsync prevents
        # acknowledged checkpoints from living only in an unflushed page cache.
        await saver.conn.execute("PRAGMA synchronous=FULL")
        await saver.conn.execute("PRAGMA busy_timeout=30000")
        yield saver


async def load_resume_state(checkpointer, thread_id: str,
                            current_versions: Mapping[str, str]) -> SupervisorState:
    snapshot = await checkpointer.aget_tuple({"configurable": {"thread_id": thread_id}})
    if snapshot is None:
        raise StateIntegrityError("no persisted checkpoint exists for this thread")
    channels = snapshot.checkpoint["channel_values"]
    # LangGraph has internal routing channels alongside the bounded State.
    state = {key: value for key, value in channels.items() if key in SupervisorState.__annotations__}
    state = validate_state(state)
    if state["thread_id"] != thread_id:
        raise StateIntegrityError("checkpoint thread identity differs from persisted State")
    validate_resume_versions(state, current_versions)
    return state


sqlite_checkpointer = open_checkpointer
