"""Process entrypoint — startup preflight.

Runs every refuse-to-start assertion *before* anything is bound or connected, so a failing
precondition costs a non-zero exit and no listening socket. The HTTP server itself is attached
here by the FastAPI application shell task, after this preflight has passed.

The order is deliberate. The media toolchain pin (assembly.md S7/S8) reads ``os.environ``
directly and runs first, because it is the one assertion that must hold even when
configuration is unreadable. Configuration is validated second, so a missing variable is
reported as a list of names rather than as a ``KeyError`` raised by whichever module happened
to read it first. The alias table is validated last, because it is the only one of the three a
running process could survive without noticing: an absent alias would otherwise be discovered
mid-job, after that job had already been paid for.

Structured logging does not exist yet (it is a later task), so this module writes to stderr
directly. It must stay the only place in the tree that does.
"""

from __future__ import annotations

import sys

from pydantic import ValidationError

from video_agent.assembly.media_toolchain import assert_media_toolchain
from video_agent.config.aliases import get_alias_table
from video_agent.config.settings import get_settings

EXIT_OK = 0
EXIT_PRECONDITION_FAILED = 1


def preflight() -> None:
    """Run every startup assertion. Raises on the first failure."""
    assert_media_toolchain()
    get_settings()
    get_alias_table()


def main() -> int:
    """Return a process exit code. Never raises."""
    try:
        preflight()
    except (RuntimeError, ValidationError) as exc:
        sys.stderr.write(f"startup preflight failed: {exc}\n")
        return EXIT_PRECONDITION_FAILED
    sys.stderr.write("startup preflight passed\n")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
