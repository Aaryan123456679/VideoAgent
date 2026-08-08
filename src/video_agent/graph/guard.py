"""The harness veto every router applies first. `graph.md` §3.1.

`_guard` is the only bridge between the graph's `JobState` and the harness's pure `decide()` —
the graph module owns topology, the harness owns whether a job keeps running, and `decide()`
itself imports no domain module (`harness.md` §7). `JobHarness` is what does the importing on
the graph's behalf: it is a per-job facade a worker mutates as cancel requests, fatal errors and
repeated failure signatures arrive, and its `decide` method is exactly the `deps.harness.decide`
the LLD's pseudocode calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from video_agent.graph.state import JobState
from video_agent.harness.cancel import CancelRequest
from video_agent.harness.decide import EvaluatorState, FatalError, LoopState, NoProgress, decide
from video_agent.harness.outcomes import Decision, Verdict
from video_agent.persistence.enums import ShotStatus

__all__ = ["JobHarness", "guard"]


@dataclass
class JobHarness:
    """The live cancel/error/no-progress facts for one job's run.

    A worker sets these fields as it observes them (a cancel API call, a caught exception, a
    repeated failure digest) and `decide()` reads them fresh on every `_guard` call — this is
    the object `graph.md`'s `deps.harness` names.
    """

    job_id: UUID
    shots_required: int
    cancel: CancelRequest | None = None
    error: FatalError | None = None
    no_progress: NoProgress | None = None
    preserved: tuple[str, ...] = field(default_factory=tuple)
    force_repair_shots: set[int] = field(default_factory=set)
    """Shot indices manually flagged for repair via `persistence.keys.shot_repair_signal_key`,
    standing in for a QC verdict `qc.md`'s real scoring (E3, not wired) would eventually send.
    `qc_shot_node` consumes an entry the moment it acts on it; `decide()` never reads this —
    it is graph-local, not a harness termination fact."""

    async def decide(self, state: JobState, node: str, *, now: datetime) -> Decision:
        """Build this superstep's `LoopState` from live facts plus `state`, and apply the rules.

        `node` is accepted to match `graph.md`'s call shape even though `decide()` itself never
        branches on which node called it — every rule applies uniformly, everywhere.
        """
        del node
        evaluator = EvaluatorState(
            shots_required=self.shots_required,
            shots_accepted=sum(1 for shot in state.shots if shot.status is ShotStatus.ACCEPTED),
            assemble_complete=state.final_video_artifact_id is not None,
            deliver_complete=state.manifest is not None,
            manifest_entries=len(state.manifest.entries) if state.manifest is not None else 0,
        )
        loop_state = LoopState(
            job_id=self.job_id,
            ledger=state.budget,
            evaluator=evaluator,
            cancel=self.cancel,
            error=self.error,
            no_progress=self.no_progress,
            preserved=self.preserved,
        )
        return decide(loop_state, now=now)


async def guard(state: JobState, node: str, *, harness: JobHarness, now: datetime) -> str | None:
    """`graph.md` §3.1: every router calls this first. `None` means keep going.

    On a terminal decision this mutates `state` in place — `outcome`/`degraded`/
    `terminal_reason_code` are "control, written by the harness" per `graph.md` §2, and this is
    the one place that writes them — and returns `"finalize"`, the only next node a terminal
    decision may name.
    """
    decision = await harness.decide(state, node, now=now)
    if decision.verdict is Verdict.CONTINUE:
        return None
    state.outcome = decision.outcome
    state.degraded = decision.degraded
    state.terminal_reason_code = decision.reason_code.value if decision.reason_code else None
    return "finalize"
