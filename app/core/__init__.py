"""Cross-cutting infrastructure: config, logging, caching, HTTP, security.

Nothing in this package may import from ``app.api`` or ``app.services``. The
dependency arrow points one way only, which is what keeps these modules
unit-testable in isolation.

Submodules are imported explicitly by consumers rather than re-exported here,
so importing ``app.core`` stays cheap and free of side effects.
"""

from __future__ import annotations

__all__: list[str] = []
