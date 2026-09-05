"""Superseded. This file is intentionally empty of tests.

Written against ``server.py``, which no longer exists.

The behaviour worth testing here is the one the legacy code got wrong rather than
the one it tested. ``fetch_gdacs`` and ``fetch_pandemics`` used to wrap their
whole body in ``except Exception: return []``, so an outage at a provider was
indistinguishable from "there are no active floods anywhere on Earth" — and the
app rendered the second interpretation. ``disasters.aggregate`` now returns
``(events, sources_failed)`` and every route surfaces ``partial``.

So the replacement, ``test_disasters.py``, asserts the failure modes first: one
source down still serves the other three *and* names the failure; all four down
is still a 200 with an empty feed and four names in ``sources_failed``; a
malformed payload from one provider does not take the response with it. The
happy-path parsing of each provider's payload shape is asserted alongside it.
``test_favorites.py`` already covers the same aggregate through
``/api/favorites/alerts``.

This file is a tombstone only because the environment this rewrite was performed
in cannot delete files. Remove it:

    git rm backend/tests/test_disasters_api.py
"""
