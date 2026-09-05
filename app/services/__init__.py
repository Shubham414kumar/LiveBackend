"""Domain services.

Each module here owns one upstream concern and knows nothing about HTTP
routing: no ``Request``, no status codes, no FastAPI imports. Route handlers in
:mod:`app.api.routes` translate between the wire and these functions.

The payoff is that the interesting logic — cache keys, retry policy, partial
failure handling, prompt construction — is testable by calling a function,
without a test client.
"""

from __future__ import annotations

__all__: list[str] = []
