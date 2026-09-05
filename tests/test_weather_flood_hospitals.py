"""Superseded. This file is intentionally empty of tests.

The legacy suite in this file was written against ``server.py`` — a single
module that no longer exists — and it installed ``MagicMock`` objects into
``sys.modules`` for ``supabase`` and ``google``. Both make it worse than no
test at all:

* importing the ``server`` shim raises ``DeprecationWarning``, and
  ``filterwarnings = ["error", ...]`` in ``pyproject.toml`` makes that fatal, so
  every test in the file errored during collection;
* the mocked Supabase client ignored its own filters, so an unscoped query — the
  actual data-isolation bug this rewrite exists to fix — passed its assertions.

Weather, flood-risk and hospital coverage belongs in ``test_weather.py``,
``test_flood.py`` and ``test_places.py``, written against the fakes in
``tests/fakes.py`` (a behavioural in-memory database and an
``httpx.MockTransport`` router) rather than mocks.

This file is a tombstone only because the environment this rewrite was performed
in cannot delete files. Remove it:

    git rm backend/tests/test_weather_flood_hospitals.py
"""
