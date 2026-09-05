"""Deprecated module - split into two focused modules.

The disaster feeds now live in :mod:`app.services.disasters` and the emergency
contact directory in :mod:`app.services.emergency`. They were separated because
this file mixed "fetch live events from four public providers" with "look up a
country's police number in a static table" - two responsibilities with entirely
different failure modes, cache lifetimes and testing needs.

The behavioural change worth knowing about: the fetchers here caught every
exception and returned ``[]``, so an outage at GDACS was indistinguishable from
"there are no active floods anywhere on Earth", and the app rendered the second
interpretation. The replacements raise, and
:func:`app.services.disasters.aggregate` reports which providers failed.

Nothing new should be added here.
"""

from __future__ import annotations

import warnings

from app.services.disasters import (
    CATEGORY_META,
    aggregate,
    earthquakes,
    eonet_events,
    gdacs_events,
    pandemic_hotspots,
)
from app.services.emergency import (
    EMERGENCY_CONTACTS,
    UNIVERSAL_GSM_NUMBER,
    emergency_contacts_for,
)

warnings.warn(
    "backend.disasters is deprecated; use app.services.disasters and "
    "app.services.emergency instead.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "CATEGORY_META",
    "EMERGENCY_CONTACTS",
    "UNIVERSAL_GSM_NUMBER",
    "aggregate",
    "earthquakes",
    "emergency_contacts_for",
    "eonet_events",
    "gdacs_events",
    "pandemic_hotspots",
]
