"""Process entrypoint — startup preflight.

Runs every refuse-to-start assertion *before* anything is bound or connected, so a failing
precondition costs a non-zero exit and no listening socket. At M0 the only such assertion is
the media toolchain version pin (assembly.md S7/S8); the HTTP server itself is attached here
by the FastAPI application shell task, after this preflight has passed.

Structured logging does not exist yet (it is a later task), so this module writes to stderr
directly. It must stay the only place in the tree that does.
"""

from __future__ import annotations

import sys

from video_agent.assembly.media_toolchain import assert_media_toolchain

EXIT_OK = 0
EXIT_PRECONDITION_FAILED = 1


def preflight() -> None:
    """Run every startup assertion. Raises on the first failure."""
    assert_media_toolchain()


def main() -> int:
    """Return a process exit code. Never raises."""
    try:
        preflight()
    except RuntimeError as exc:
        sys.stderr.write(f"startup preflight failed: {exc}\n")
        return EXIT_PRECONDITION_FAILED
    sys.stderr.write("startup preflight passed\n")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
