"""A module that builds a Redis key by hand, so the static check has something to catch.

`S0.6.1` acceptance 3 bans a string literal beginning with any registered key prefix outside
`video_agent.persistence.keys`. A check that has only ever been run against a clean tree is a
check that might be scanning nothing — the scanner could be looking for the wrong prefix, or
skipping every file, and "no violations" would read exactly the same.

Not collected by pytest: `_fixtures` is in `norecursedirs` and this file is never imported by
the application. It exists to be parsed.
"""

from __future__ import annotations

from uuid import UUID


def failure_signature_key(job_id: UUID) -> str:
    """The violation the check must find: the pattern spelled out rather than rendered."""
    return f"sig:{job_id}"


def progress_key(job_id: UUID) -> str:
    """A second one, under a different prefix, so a scanner that stops at the first is caught."""
    return "progress:" + str(job_id)
