"""Choose a durable PostgreSQL checkpointer in the API and memory in tests."""

from __future__ import annotations

import os
from contextlib import ExitStack
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import Engine


def enter_investigation_checkpointer(engine: Engine | None, stack: ExitStack) -> Any:
    """Enter the PostgreSQL checkpointer context, or use memory for local tests.

    The PostgreSQL saver owns its support-table schema and initializes it with
    setup(). Application-owned tables continue to use Alembic migrations.
    """
    dialect_name = getattr(getattr(engine, "dialect", None), "name", None)
    if engine is None or dialect_name != "postgresql":
        return InMemorySaver()

    # Checkpoints contain only our typed dict/list/scalar graph state. Keep
    # LangGraph's MessagePack deserialization in strict mode for stored data.
    os.environ["LANGGRAPH_STRICT_MSGPACK"] = "true"

    from langgraph.checkpoint.postgres import PostgresSaver

    connection_url = engine.url.set(drivername="postgresql").render_as_string(
        hide_password=False
    )
    checkpointer = stack.enter_context(PostgresSaver.from_conn_string(connection_url))
    checkpointer.setup()
    return checkpointer
