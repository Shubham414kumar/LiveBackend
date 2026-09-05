"""Superseded. This file is intentionally empty of tests.

Written against ``server.py``, which no longer exists. The name is a development
artefact — "round 3" of a manual test-writing pass — and the contents mixed
reverse-geocoding, emergency contacts and the SOS flow into one file with no
shared setup.

Two of those need real coverage and did not have it:

* **Geocoding** is rate-policy sensitive. Nominatim permits 1 request per second
  and bans the deployment IP for exceeding it, so ``app/core/http.py`` paces
  requests per host. The replacement asserts the pacing and the identifying
  User-Agent, both of which are the difference between a working deployment and a
  blocked one.
* **SOS** was mocked end to end in the mobile app — it displayed a success state
  without sending anything. A person pressing that button believes help is coming.
  The replacement asserts the real request is made and that a failure surfaces as
  a failure.

Replacement coverage: ``test_geo.py`` and ``test_sos.py``.

This file is a tombstone only because the environment this rewrite was performed
in cannot delete files. Remove it:

    git rm backend/tests/test_round3_geo_sos.py
"""
