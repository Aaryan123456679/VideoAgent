"""Video Agent — agentic text-to-video pipeline.

The module set is fixed at the ten LLDs in ``docs/LLD/`` plus ``config``. A twelfth
sub-package may not appear without a documentation change; ``tests/unit/test_package_layout.py``
enforces that.
"""

__version__ = "0.1.0"

MODULES: frozenset[str] = frozenset(
    {
        "api",
        "harness",
        "gateway",
        "graph",
        "planning",
        "providers",
        "qc",
        "assembly",
        "persistence",
        "observability",
        "config",
    }
)

__all__ = ["MODULES", "__version__"]
