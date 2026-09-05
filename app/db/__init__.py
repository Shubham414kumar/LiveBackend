"""Data access layer.

Route handlers never touch the Supabase client directly; they go through the
repositories in :mod:`app.db.repositories`. That indirection is what makes the
device-scoping rule enforceable — every query that reads user-owned data takes
a ``device_id`` argument, so an unscoped read is a visible omission at the call
site rather than an invisible one buried in a query builder chain.
"""

from __future__ import annotations

__all__: list[str] = []
