"""What `build_graph` closes every node and router over. `graph.md` §3's `deps` parameter.

A plain dataclass rather than a bag of globals: every name a node needs to reach the outside
world — the LLM gateway, the database, the harness veto, the clock — is declared once here, so
a node missing one is a constructor error, not a `NameError` three nodes downstream.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.ext.asyncio import AsyncEngine

from video_agent.graph.guard import JobHarness

if TYPE_CHECKING:  # pragma: no cover - typing only
    from video_agent.gateway.gateway import Gateway

__all__ = ["GraphDeps"]


@dataclass(frozen=True, slots=True)
class GraphDeps:
    """Everything a compiled job graph needs beyond the `JobState` it is handed.

    `checkpointer` is the langgraph-level saver passed to `.compile()` — it drives the
    framework's own resume bookkeeping. It is **not** the source of truth for the job: per
    `graph.md` §4 `[D-23]`, that is the `checkpoint` table row each node writes itself, in the
    same transaction as its domain writes. v1 wires an in-memory saver here (`build.py`) because
    a fully spec-compliant Postgres-backed `BaseCheckpointSaver` is the resume/regeneration
    machinery `graph.md`'s own status header defers to E3 — building it early would be building
    E3 ahead of the E1/E2 slice this session is scoped to. This is a documented v1 gap, not a
    silent one.
    """

    engine: AsyncEngine
    gateway: Gateway
    checkpointer: BaseCheckpointSaver[Any]
    harness: JobHarness
    now: Callable[[], datetime]
