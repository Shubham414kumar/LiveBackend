"""Superseded. This file is intentionally empty of tests.

Written against ``server.py``, which no longer exists.

"Edge cases" as a file name is the problem as much as the contents: an edge case
belongs next to the behaviour it qualifies, where a maintainer changing that
behaviour will actually read it. Collecting them here meant the oversized-body
check sat several hundred lines away from anything about request handling, and the
malformed-JSON check sat away from every route that accepts a body.

They have been redistributed:

* request-level limits and hostile input — oversized bodies, an unparseable
  ``Content-Length``, host-header injection, log injection, CORS drift, secret
  leakage — are in ``test_security.py``, several of them driving the raw ASGI app
  because ``httpx`` computes a correct ``Content-Length`` and cannot send a bad
  one;
* per-route invalid input is a parametrised table inside that route's own file
  (see ``test_invalid_payloads_are_rejected`` in ``test_favorites.py`` and
  ``test_community_reports.py``);
* the degraded-dependency cases — Supabase unreachable, a provider down, a
  feature unconfigured — are a titled section at the foot of each route's file,
  because "what this endpoint does when its dependency is gone" is part of the
  endpoint's contract, not a curiosity.

This file is a tombstone only because the environment this rewrite was performed
in cannot delete files. Remove it:

    git rm backend/tests/test_edge_cases.py
"""
