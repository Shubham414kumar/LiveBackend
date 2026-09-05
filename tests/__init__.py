"""Backend test suite.

A real package rather than a bare directory, so ``from tests.fakes import ...``
resolves to *this* ``tests`` package and not to whatever else happens to be
named ``tests`` on ``sys.path``. It also matches the ``[[tool.mypy.overrides]]``
block for ``tests.*`` in ``pyproject.toml``.

Layout:

* ``conftest.py`` — fixtures, plus the test environment that has to be set
  before any ``app.*`` import.
* ``fakes.py`` — the in-memory Supabase double and the outbound-HTTP router.
* ``test_core_*.py`` — unit tests for one module at a time, no HTTP.
* ``test_*_api.py`` / ``test_*.py`` — endpoint tests through ``TestClient``.
"""
