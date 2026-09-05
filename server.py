"""Deprecated entrypoint - kept so existing commands and imports keep working.

The application now lives in the :mod:`app` package and is built by
:func:`app.main.create_app`. This module used to be a 1,119-line monolith that
held configuration, HTTP clients, caching, rate limiting, all 33 routes and
every Supabase query in one file — including a shutdown handler that called
``close()`` on a name from another scope, so teardown raised ``NameError`` on
every restart and connections were never released.

Nothing new should be added here.

Canonical entrypoint::

    uvicorn app.main:app --host 0.0.0.0 --port 8000

This shim re-exports ``app`` so ``uvicorn server:app`` and ``import server``
keep resolving during the migration. It can be deleted once no tooling
references it.
"""

from __future__ import annotations

import warnings

from app.main import app, create_app

warnings.warn(
    "backend.server is deprecated; import the application from app.main "
    "instead (uvicorn app.main:app).",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["app", "create_app"]
