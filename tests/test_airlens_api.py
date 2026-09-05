"""Superseded. This file is intentionally empty of tests.

Written against ``server.py``, which no longer exists. Beyond that, the legacy
assertions here encoded a contract that was deliberately changed:

* ``GET /api/aqi/here`` is now deprecated-but-served — it answers, and it carries
  a ``Deprecation`` header, because a shipped mobile client still calls it. A test
  that asserts only the status code cannot tell the two apart, so the replacement
  asserts the header.
* the WAQI token is never returned to a client. Map tiles are proxied through
  this API precisely so the token stays server-side, and that is the single most
  important thing to assert about these routes.

Replacement coverage: ``test_aqi.py`` for the readings and
``/api/aqi/location-intel``, plus the secret-leakage sweep already in
``test_security.py``, which asserts that no configured secret value appears in
any response body or header.

This file is a tombstone only because the environment this rewrite was performed
in cannot delete files. Remove it:

    git rm backend/tests/test_airlens_api.py
"""
