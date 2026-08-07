"""Constants shared by more than one test module.

One definition, imported by every check that uses it. These lists previously existed twice —
once in `tests/contract/test_env_example_contract.py` and once in
`tests/unit/test_dev_stack.py` — and the two drifted: the compose copy was missing
`_PASSWORD`, which was exactly the suffix needed to catch the one literal credential in
`docker-compose.dev.yml`. A duplicated deny-list is a deny-list that will be narrowed by
accident.

Not a test module: the filename does not match `python_files`, so pytest does not collect it.
"""

from __future__ import annotations

SECRET_SUFFIXES: tuple[str, ...] = (
    "_KEY",
    "_KEY_ID",
    "_SECRET",
    "_TOKEN",
    "_PASSWORD",
)
"""Variable-name suffixes that mark a value as a credential."""

BANNED_SECRET_PREFIXES: tuple[str, ...] = ("mhk_live_", "sk-", "pk-", "AKIA")
"""Value prefixes that mean a real credential has been committed."""
