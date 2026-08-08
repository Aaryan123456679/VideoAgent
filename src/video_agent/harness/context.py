"""`NodeContext` — the only way a node sees anything. `harness.md` §3.1.

Four rules live here:

1. **Nodes are pure with respect to input.** A node reads its `NodeContext` and nothing else —
   no node opens a DB session or reads Redis directly. This module is what a node is handed;
   how the harness assembles it (the DB reads, the Redis reads) belongs to the graph/worker
   layer, not here.
2. **The bible is verified, not trusted.** `for_node()` calls `verify_bible()` on every load, so
   a mutated row raises `VA-BIBLE-002` before any node sees a bible that disagrees with its own
   hash.
3. **Untrusted content is quarantined at the gateway**, not here: `gateway.rendering.render()`
   is what escapes instruction-shaped content into a delimited block and is what
   `gateway.LiteLLMGateway._record_quarantine` reports as `VA-SEC-001`. `NodeContext` carries no
   raw prompt text, so there is nothing left for this module to quarantine a second time.
4. **Tool grants are per node and enforced, not advisory.** `require_tool()` raises rather than
   no-ops on an ungranted call, because a silent no-op turns a programming error into a shot
   that quietly never rendered.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from video_agent.gateway.models import ArtifactRef
from video_agent.harness.budget import BudgetView
from video_agent.harness.errors import UngrantedToolError, UnknownToolError
from video_agent.harness.grants import GRANTS
from video_agent.planning.bible import verify_bible
from video_agent.planning.models import Beat, ContinuityBible
from video_agent.qc.models import QCFinding

__all__ = ["NodeContext"]


class NodeContext(BaseModel):
    """Everything one node's one call is allowed to know, and the tools it may use.

    `tools` is derived from `GRANTS[node]` by `for_node()`, never supplied by a caller —
    a node that could construct its own grant set could grant itself anything.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    job_id: UUID
    node: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    bible: ContinuityBible | None = None
    beat: Beat | None = None
    chained_frame_ref: ArtifactRef | None = None
    prior_findings: tuple[QCFinding, ...] = ()
    budget_remaining: BudgetView
    tools: frozenset[str]

    @classmethod
    def for_node(
        cls,
        *,
        job_id: UUID,
        node: str,
        trace_id: str,
        budget_remaining: BudgetView,
        bible: ContinuityBible | None = None,
        beat: Beat | None = None,
        chained_frame_ref: ArtifactRef | None = None,
        prior_findings: tuple[QCFinding, ...] = (),
    ) -> NodeContext:
        """Assemble a context for `node`, verifying the bible and resolving its tool grant.

        `node` not appearing in `GRANTS` is a configuration error (a graph node with no grant
        table entry), and is distinguished from *granted but empty* — `select_next_shot` and
        `finalize` are legitimately grant-less.
        """
        if node not in GRANTS:
            message = f"node {node!r} has no entry in the tool grant table"
            raise UnknownToolError(message)
        if bible is not None:
            verify_bible(bible)
        return cls(
            job_id=job_id,
            node=node,
            trace_id=trace_id,
            bible=bible,
            beat=beat,
            chained_frame_ref=chained_frame_ref,
            prior_findings=prior_findings,
            budget_remaining=budget_remaining,
            tools=GRANTS[node],
        )

    def require_tool(self, tool: str) -> None:
        """Raise unless `tool` is granted to this node. `harness.md` §3.1 rule 4."""
        if tool not in self.tools:
            message = f"node {self.node!r} called {tool!r}, which it was not granted"
            raise UngrantedToolError(message)
