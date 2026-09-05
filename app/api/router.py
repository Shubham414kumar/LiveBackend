"""API router aggregation.

Every router is included here exactly once, under the ``/api`` prefix.

Why aggregate in one file: the previous version declared all 33 routes inline in
a single 1,119-line module. Nothing was actually registered twice, but a
collision would have been invisible — FastAPI resolves two handlers on the same
method and path silently, by matching whichever was registered first, with no
warning. Keeping the include list in one short file makes a duplicate prefix
obvious on sight.

Route ordering note: within a router, literal paths must be declared before
parameterised ones at the same depth (``/reports/categories`` before
``/reports/{report_id}``, ``/favorites/alerts`` before ``/favorites/{id}``),
otherwise the literal is swallowed as a path parameter. That ordering is
maintained inside each module.

Compatibility: every path the previous ``server.py`` exposed is still served
here. Two are marked deprecated rather than removed (``/aqi/here``,
``/weather/clouds/tile-url``) and one changed its key for security reasons
(``DELETE /favorites/{uid}`` took a shared, guessable WAQI station id; it now
takes the caller's own favourite id).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import (
    admin,
    ai,
    aqi,
    disasters,
    favorites,
    geo,
    health,
    notifications,
    reports,
    weather,
)

api_router = APIRouter(prefix="/api")

# Health first: it must stay reachable and dependency-free even if a feature
# router below fails to configure.
api_router.include_router(health.router)
api_router.include_router(aqi.router)
api_router.include_router(weather.router)
api_router.include_router(disasters.router)
api_router.include_router(geo.router)
api_router.include_router(ai.router)
api_router.include_router(reports.router)
api_router.include_router(favorites.router)
api_router.include_router(notifications.router)
api_router.include_router(admin.router)

__all__ = ["api_router"]
