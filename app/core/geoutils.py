"""Geospatial helpers.

Kept in ``core`` because both the service layer and the data layer need them:
the repositories use :func:`bounding_box` to push a coarse filter into the
database, and the services use :func:`haversine_km` to compute exact distances
on the rows that come back.
"""

from __future__ import annotations

import math
from typing import Tuple

EARTH_RADIUS_KM = 6371.0088

# Approximate, and deliberately so — it is only used to size a pre-filter box,
# which is then refined by an exact haversine pass.
_KM_PER_DEG_LAT = 110.574


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in kilometres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)

    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, a)))


def bounding_box(lat: float, lon: float, radius_km: float) -> Tuple[float, float, float, float]:
    """Return ``(min_lat, max_lat, min_lon, max_lon)`` enclosing the radius.

    The box is intentionally a slight over-approximation: it must never exclude
    a point that is genuinely inside the radius, because the exact filter runs
    afterwards. Longitude degrees shrink with latitude, so the delta is divided
    by ``cos(lat)``; near the poles that blows up, hence the clamp.
    """
    lat_delta = radius_km / _KM_PER_DEG_LAT

    cos_lat = math.cos(math.radians(lat))
    if abs(cos_lat) < 0.01:
        # Within ~0.6 degrees of a pole: every meridian is close, so don't try
        # to constrain longitude at all.
        lon_delta = 180.0
    else:
        lon_delta = radius_km / (_KM_PER_DEG_LAT * cos_lat)

    return (
        max(-90.0, lat - lat_delta),
        min(90.0, lat + lat_delta),
        max(-180.0, lon - abs(lon_delta)),
        min(180.0, lon + abs(lon_delta)),
    )


__all__ = ["EARTH_RADIUS_KM", "bounding_box", "haversine_km"]
