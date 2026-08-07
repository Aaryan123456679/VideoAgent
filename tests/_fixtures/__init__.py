"""Fixture modules that are deliberately *not* collected by a default pytest run.

`norecursedirs` in `pyproject.toml` keeps this directory out of discovery; tests that need
one of these modules pass its path to pytest explicitly in a subprocess.
"""
