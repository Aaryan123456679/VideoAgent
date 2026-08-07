"""The ambient trace context every log line and every error reads from.

`observability.md` §2 is explicit about the mechanism: *`trace_id` is propagated through a
context variable, so no module passes it by hand and no log line can omit it.* The two halves
of that sentence are the same design choice seen from both ends. If `trace_id` were a
parameter it would be threaded through signatures that have no other reason to know about
tracing, and the first function that forgot to forward it would break the join between a log
line and its trace — quietly, and only for the code path nobody exercised.

A `ContextVar` is the right shape because the propagation rule matches the requirement
exactly: it follows `await` boundaries and is copied into tasks spawned from the current
context, which is precisely the scope of one job's work, and it does **not** leak into a
sibling task, which is precisely what would misattribute one job's lines to another's trace.

`bind_trace` guarantees a trace exists for its body. That is deliberate: it means the
synthesise-and-alarm path in the log formatter fires only for code logging genuinely outside
any traced unit of work, which is a real defect worth counting, rather than for the ordinary
case of a caller that did not think about it.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

SYNTHESISED_TRACE_PREFIX = "syn-"
"""Marks an id invented by the formatter because nothing was bound.

Visible in the id itself, not only in a sibling field, so that someone pasting the id into a
trace search and finding nothing learns *why* from the string they already have.
"""

_trace_id: ContextVar[str | None] = ContextVar("va_trace_id", default=None)
_span_id: ContextVar[str | None] = ContextVar("va_span_id", default=None)
_job_id: ContextVar[str | None] = ContextVar("va_job_id", default=None)
_tenant_id: ContextVar[str | None] = ContextVar("va_tenant_id", default=None)
_node: ContextVar[str | None] = ContextVar("va_node", default=None)
_degraded: ContextVar[bool] = ContextVar("va_degraded", default=False)


def new_trace_id() -> str:
    """A fresh trace id for a unit of work that is starting now."""
    return uuid4().hex


def synthesised_trace_id() -> str:
    """A stand-in id for a log line emitted outside any trace.

    Never absent, never silently blank `[observability.md §10]`: a line with no id cannot join
    its trace, and a line with an *empty* id looks like a successful join to every query that
    groups by it.
    """
    return f"{SYNTHESISED_TRACE_PREFIX}{uuid4().hex}"


def is_synthesised(trace_id: str) -> bool:
    """Whether `trace_id` was invented locally rather than issued by a real trace."""
    return trace_id.startswith(SYNTHESISED_TRACE_PREFIX)


def current_trace_id() -> str | None:
    """The bound trace id, or `None` when nothing is bound."""
    return _trace_id.get()


def current_span_id() -> str | None:
    """The bound span id, or `None`."""
    return _span_id.get()


def current_job_id() -> str | None:
    """The bound job id, or `None`."""
    return _job_id.get()


def current_tenant_id() -> str | None:
    """The bound tenant id, or `None`."""
    return _tenant_id.get()


def current_node() -> str | None:
    """The graph node currently executing, or `None`."""
    return _node.get()


def is_degraded() -> bool:
    """Whether the current unit of work is running in a degraded mode."""
    return _degraded.get()


def current_context() -> dict[str, str | bool | None]:
    """Every bound field, in the shape the log schema wants them.

    Returned as a plain dict rather than the `ContextVar`s themselves so the formatter reads a
    consistent snapshot and cannot accidentally mutate the context while serialising it.
    """
    return {
        "trace_id": _trace_id.get(),
        "span_id": _span_id.get(),
        "job_id": _job_id.get(),
        "tenant_id": _tenant_id.get(),
        "node": _node.get(),
        "degraded": _degraded.get(),
    }


@dataclass(frozen=True, slots=True)
class _Binding:
    """One variable and the token that undoes its set, so unbinding cannot guess."""

    variable: ContextVar[Any]
    token: Token[Any]


def _bind(requested: Mapping[ContextVar[Any], Any]) -> list[_Binding]:
    """Set every non-`None` variable and hand back what is needed to undo it.

    `None` means *inherit*, never *clear*: a node entering with only its own name should not
    erase the job and tenant it is running for.
    """
    return [
        _Binding(variable=variable, token=variable.set(value))
        for variable, value in requested.items()
        if value is not None
    ]


def _unbind(bindings: Sequence[_Binding]) -> None:
    """Restore in reverse, using the tokens `ContextVar.set` handed back.

    Tokens rather than remembered values, so nesting unwinds exactly — resetting to a
    remembered value would turn "there was no job bound" into "the job is `None`" on the way
    out, and the two are different states to every consumer that checks for absence.
    """
    for binding in reversed(bindings):
        binding.variable.reset(binding.token)


@contextmanager
def bind_trace(
    trace_id: str | None = None,
    *,
    job_id: str | None = None,
    tenant_id: str | None = None,
) -> Iterator[str]:
    """Bind the trace-level context — one trace is one job — yielding the effective trace id.

    If nothing is bound and no id is given, one is minted, because the whole point of the
    block is that what happens inside it is traced. Split from `bind_span` because the trace
    model splits the same way `[observability.md §2.1]`: a trace is a job and lives for the
    whole run, a span is one node execution and there are dozens per job.
    """
    bindings = _bind(
        {
            _trace_id: trace_id if trace_id is not None else _trace_id.get() or new_trace_id(),
            _job_id: job_id,
            _tenant_id: tenant_id,
        }
    )
    try:
        yield str(_trace_id.get())
    finally:
        _unbind(bindings)


@contextmanager
def bind_span(
    *,
    span_id: str | None = None,
    node: str | None = None,
    degraded: bool | None = None,
) -> Iterator[None]:
    """Bind the span-level context — one span is one node execution.

    No id is minted when none is given: span ids are issued by the tracer, and inventing a
    local one would produce a `span_id` that joins to nothing while looking like it should.
    `T4.1` supplies real ones.
    """
    bindings = _bind({_span_id: span_id, _node: node, _degraded: degraded})
    try:
        yield
    finally:
        _unbind(bindings)


@contextmanager
def clear_trace() -> Iterator[None]:
    """Unbind everything for the duration of the block.

    Exists for the tests that have to observe what happens when a line is logged with no trace
    bound. Application code has no reason to leave a traced scope.
    """
    cleared: dict[ContextVar[Any], Any] = {
        _trace_id: None,
        _span_id: None,
        _job_id: None,
        _tenant_id: None,
        _node: None,
        _degraded: False,
    }
    bindings = [
        _Binding(variable=variable, token=variable.set(value))
        for variable, value in cleared.items()
    ]
    try:
        yield
    finally:
        _unbind(bindings)
