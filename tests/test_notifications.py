"""Superseded. This file is intentionally empty of tests.

Written against ``server.py`` with a mocked Supabase client that ignored its
filters, so nothing here could observe the two properties that matter for push
registration:

* the token table is keyed on ``device_id``, not on the token. An Expo token
  rotates on reinstall and on some OS updates; keying on the token accumulates
  dead rows that fail delivery forever, while keying on the device replaces the
  old token in place. Proving that needs a fake that actually applies
  ``on_conflict``, which is why ``tests/fakes.py`` implements upsert semantics.
* the token's *shape* is validated on the way in. An invalid token fails silently
  at send time — the worst possible failure mode for an alerting system — so
  ``PushTokenRegister`` rejects anything that is not
  ``ExponentPushToken[...]``/``ExpoPushToken[...]``, and that rejection is a
  422 the client can act on.

Replacement coverage: ``test_notifications_api.py``. Cross-device isolation for
both ``POST`` and ``DELETE /api/notifications/register`` is already asserted in
``test_security.py``.

This file is a tombstone only because the environment this rewrite was performed
in cannot delete files. Remove it:

    git rm backend/tests/test_notifications.py
"""
