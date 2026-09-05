"""Request and response schemas.

Validation lives here, not in route handlers, so a malformed request is
rejected by the framework with a consistent 422 before any handler code or
upstream call runs.

Latitude and longitude are constrained with ``ge``/``le`` on the field rather
than by hand-rolled ``validate_lat``/``validate_lon`` calls inside handlers —
the previous approach meant every new endpoint had to remember to call them,
and several didn't.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Dict, List, Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.security import sanitize_string

# ---------------------------------------------------------------------------
# Shared coordinate types
# ---------------------------------------------------------------------------
Latitude = Annotated[float, Field(ge=-90, le=90, description="Degrees north, -90 to 90")]
Longitude = Annotated[float, Field(ge=-180, le=180, description="Degrees east, -180 to 180")]

ReportCategory = Literal[
    "flood",
    "fire",
    "accident",
    "road_block",
    "pollution",
    "water_logging",
    "disease_cluster",
    "other",
]

# Capitalised to stay wire-compatible with the existing mobile client.
Severity = Literal["Low", "Moderate", "Severe", "Extreme"]

ReportStatus = Literal["visible", "hidden", "removed"]


class StrictModel(BaseModel):
    """Rejects unknown fields.

    A silently-ignored typo in a client payload is a bug that surfaces days
    later as missing data; better to fail the request immediately.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------------------
# Air quality
# ---------------------------------------------------------------------------
class AqiCategory(BaseModel):
    label: str
    color: str
    level: int = Field(ge=0, le=6)
    advice: str


class AqiCity(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None


class AqiReading(BaseModel):
    uid: Optional[int] = None
    aqi: Optional[int] = None
    dominant_pollutant: Optional[str] = None
    city: AqiCity = Field(default_factory=AqiCity)
    time: Optional[str] = None
    iaqi: Dict[str, Optional[float]] = Field(default_factory=dict)
    attributions: List[Dict[str, Any]] = Field(default_factory=list)
    forecast: Dict[str, Any] = Field(default_factory=dict)
    category: AqiCategory


class AqiStation(BaseModel):
    uid: Optional[int] = None
    name: Optional[str] = None
    country: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    time: Optional[str] = None
    aqi: Optional[int] = None
    category: AqiCategory


class AqiSearchResponse(BaseModel):
    results: List[AqiStation]


class AqiBoundsResponse(BaseModel):
    stations: List[AqiStation]
    count: int


class NearbyStation(BaseModel):
    name: Optional[str] = None
    aqi: int


class AqiRanking(BaseModel):
    rank: Optional[int] = None
    total: int = 0
    nearby_stations: List[NearbyStation] = Field(default_factory=list)


class LocationIntel(BaseModel):
    """Dashboard payload: three upstreams in one round trip.

    ``partial`` and ``unavailable`` exist so the client can distinguish "the sky
    is clear" from "we could not find out", which the previous version could
    not — it returned ``null`` for a failed sub-request and the UI rendered that
    as a reassuring green state.
    """

    aqi: Optional[AqiReading] = None
    weather: Optional[CurrentWeather] = None
    ranking: AqiRanking = Field(default_factory=AqiRanking)
    partial: bool = False
    unavailable: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------
class CurrentWeather(BaseModel):
    time: Optional[str] = None
    temperature_2m: Optional[float] = None
    relative_humidity_2m: Optional[float] = None
    apparent_temperature: Optional[float] = None
    precipitation: Optional[float] = None
    rain: Optional[float] = None
    showers: Optional[float] = None
    snowfall: Optional[float] = None
    weather_code: Optional[int] = None
    surface_pressure: Optional[float] = None
    wind_speed_10m: Optional[float] = None
    wind_direction_10m: Optional[float] = None
    uv_index: Optional[float] = None


class WeatherResponse(BaseModel):
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    timezone: Optional[str] = None
    elevation: Optional[float] = None
    current: CurrentWeather = Field(default_factory=CurrentWeather)


class TileUrlResponse(BaseModel):
    tile_url: str
    attribution: str
    # Present when the upstream exposes a discrete frame time, so the client can
    # tell whether it is looking at live data or a stale frame.
    frame_time: Optional[datetime] = None
    # Highest zoom the upstream actually publishes. The client needs this to stop
    # requesting tiles that will always 404 and to switch to upscaling instead.
    max_zoom: Optional[int] = Field(default=None, ge=0, le=22)


class DailyRain(BaseModel):
    date: str
    rain_mm: float


class FloodRisk(BaseModel):
    flood_probability: int = Field(ge=0, le=100)
    risk_level: Literal["Very Low", "Low", "Moderate", "High", "Unknown"]
    total_rain_7d_mm: float
    daily_forecast: List[DailyRain] = Field(default_factory=list)
    nearby_flood_events: int = 0
    advice: str
    # False when rainfall or event data could not be retrieved, so the client
    # must not present the score as authoritative.
    complete: bool = True


# ---------------------------------------------------------------------------
# Disasters
# ---------------------------------------------------------------------------
class DisasterEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: Optional[str] = None
    source: Optional[str] = None
    category: Optional[str] = None
    title: Optional[str] = None
    time: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    severity: Optional[str] = None
    url: Optional[str] = None


class DisasterFeed(BaseModel):
    events: List[DisasterEvent]
    count: int
    # Which providers failed, so a partial feed is never mistaken for a quiet day.
    sources_failed: List[str] = Field(default_factory=list)
    partial: bool = False


class EmergencyContacts(BaseModel):
    model_config = ConfigDict(extra="allow")

    iso2: Optional[str] = None
    country: Optional[str] = None
    police: Optional[str] = None
    ambulance: Optional[str] = None
    fire: Optional[str] = None
    general: Optional[str] = None


# ---------------------------------------------------------------------------
# Geocoding / places
# ---------------------------------------------------------------------------
class ReverseGeocode(BaseModel):
    lat: float
    lon: float
    iso2: Optional[str] = None
    country: Optional[str] = None
    state: Optional[str] = None
    city: Optional[str] = None
    display_name: Optional[str] = None
    contacts: Dict[str, Any] = Field(default_factory=dict)
    # True when the geocoder failed and only the coordinates are real.
    resolved: bool = True


class Hospital(BaseModel):
    name: str
    lat: float
    lon: float
    distance_km: Optional[float] = None
    phone: Optional[str] = None
    emergency: Optional[str] = None
    website: Optional[str] = None


class HospitalsResponse(BaseModel):
    hospitals: List[Hospital]
    count: int
    radius_km: float
    attribution: str = "© OpenStreetMap contributors"


# ---------------------------------------------------------------------------
# Favourites
# ---------------------------------------------------------------------------
class FavoriteCreate(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    lat: Latitude
    lon: Longitude
    station_uid: Optional[str] = Field(default=None, max_length=200)
    alert_radius_km: float = Field(default=100.0, gt=0, le=20000)

    @field_validator("name")
    @classmethod
    def _clean_name(cls, v: str) -> str:
        cleaned = sanitize_string(v, 200)
        if not cleaned:
            raise ValueError("Name cannot be empty after sanitisation")
        return cleaned


class Favorite(BaseModel):
    id: str
    name: str
    lat: float
    lon: float
    station_uid: Optional[str] = None
    alert_radius_km: float = 100.0
    created_at: Optional[str] = None


class FavoritesResponse(BaseModel):
    favorites: List[Favorite]
    count: int
    limit: int


class FavoriteAlerts(BaseModel):
    favorite: Favorite
    alert_count: int
    alerts: List[DisasterEvent]


class FavoriteAlertsResponse(BaseModel):
    favorites: List[FavoriteAlerts]
    partial: bool = False
    sources_failed: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Community reports
# ---------------------------------------------------------------------------
class ReportCreate(StrictModel):
    category: ReportCategory
    title: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
    lat: Latitude
    lon: Longitude
    severity: Severity = "Moderate"

    @field_validator("title")
    @classmethod
    def _clean_title(cls, v: str) -> str:
        cleaned = sanitize_string(v, 200)
        if not cleaned:
            raise ValueError("Title cannot be empty after sanitisation")
        return cleaned

    @field_validator("description")
    @classmethod
    def _clean_description(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return sanitize_string(v, 2000) or None


class Report(BaseModel):
    id: str
    category: str
    title: str
    description: Optional[str] = None
    lat: float
    lon: float
    severity: str
    status: str
    upvotes: int = 0
    created_at: Optional[str] = None
    distance_km: Optional[float] = None
    # Set for the calling device only; never exposes another device's identity.
    is_mine: bool = False
    has_voted: bool = False


class ReportsResponse(BaseModel):
    reports: List[Report]
    count: int
    radius_km: float


class ReportCreated(BaseModel):
    id: str
    status: str
    created_at: Optional[str] = None


class VoteResponse(BaseModel):
    report_id: str
    upvotes: int
    accepted: bool
    detail: str


class CategoryMeta(BaseModel):
    label: str
    icon: str
    color: str


class ReportCategoriesResponse(BaseModel):
    categories: Dict[str, CategoryMeta]
    severities: List[str]


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
class PushTokenRegister(StrictModel):
    token: str = Field(min_length=1, max_length=500)
    platform: Optional[Literal["ios", "android", "web"]] = None
    lat: Optional[Latitude] = None
    lon: Optional[Longitude] = None

    @field_validator("token")
    @classmethod
    def _expo_token_shape(cls, v: str) -> str:
        # Expo push tokens look like ExponentPushToken[xxxxxxxx] or
        # ExpoPushToken[...]. Rejecting anything else keeps junk out of the
        # table, which matters because a bad token silently fails forever.
        if not (v.startswith(("ExponentPushToken[", "ExpoPushToken[")) and v.endswith("]")):
            raise ValueError("Token must be an Expo push token, e.g. ExponentPushToken[...]")
        return v


class SimpleStatus(BaseModel):
    status: str
    detail: Optional[str] = None


# The push threshold accepts one more value than :data:`Severity` does: "Minor",
# which USGS emits and which a report author is never offered. A user who can see
# a Minor event on the map must be able to set a floor that includes it.
#
# Third spelling of this vocabulary, after the CHECK constraint on
# ``push_tokens.min_severity`` and ``SEVERITY_LEVELS`` in ``app.core.config``.
# Written out rather than derived because a ``Literal`` cannot be built from a
# runtime tuple, and OpenAPI clients are generated from these names. The drift is
# pinned by a test asserting the two agree, not by runtime code.
PushSeverity = Literal["Low", "Minor", "Moderate", "Severe", "Extreme"]

#: Longest plausible IANA zone name, with room to spare
#: ("America/Argentina/ComodRivadavia" is 32).
MAX_TIMEZONE_LENGTH = 64


class AlertPreferencesUpdate(StrictModel):
    """A partial update. Every field omitted means "leave this as it is".

    Sent by the app's notification settings screen, which does not hold the whole
    state and must not have to: a screen that PUTs its idea of every preference
    will happily overwrite a change made on another screen a second earlier.

    Quiet hours are the exception to "omitted means unchanged" — the pair can also
    be *cleared*, by sending both ends as ``null``. Absent and null are told apart
    with ``model_fields_set``, so the two intents stay distinguishable on the wire
    without inventing a sentinel value.
    """

    alerts_enabled: Optional[bool] = None
    min_severity: Optional[PushSeverity] = None
    quiet_hours_start: Optional[int] = Field(default=None, ge=0, le=23)
    quiet_hours_end: Optional[int] = Field(default=None, ge=0, le=23)
    timezone: Optional[str] = Field(default=None, max_length=MAX_TIMEZONE_LENGTH)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, v: Optional[str]) -> Optional[str]:
        # Resolved here so an unknown zone is a 422 the user can act on. Stored
        # unchecked it would instead make the dispatcher log an unresolvable
        # timezone on every pass and deliver *through* the quiet window, which is
        # the safe fallback but not what the user asked for.
        if v is None:
            return None
        name = v.strip()
        if not name:
            raise ValueError("Timezone cannot be empty")
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"Unknown timezone {name!r}. Use an IANA name such as Asia/Kolkata."
            ) from exc
        return name

    @property
    def clears_quiet_hours(self) -> bool:
        """True when both ends were sent explicitly as ``null``."""
        sent = self.model_fields_set
        return (
            "quiet_hours_start" in sent
            and "quiet_hours_end" in sent
            and self.quiet_hours_start is None
            and self.quiet_hours_end is None
        )

    @model_validator(mode="after")
    def _quiet_hours_are_paired(self) -> AlertPreferencesUpdate:
        # Mirrors the `push_tokens_quiet_hours_paired` CHECK. Caught here so a
        # half-specified window is a 422 naming the missing field rather than a
        # 503 from a constraint violation the client cannot interpret.
        start, end = self.quiet_hours_start, self.quiet_hours_end
        if (start is None) != (end is None):
            raise ValueError(
                "quiet_hours_start and quiet_hours_end must be sent together; "
                "send both as null to clear the quiet window"
            )
        return self


class AlertPreferences(BaseModel):
    """This device's delivery preferences, and the server policy around them.

    Deliberately does not echo the push token. Nothing here needs it — the client
    already has it — and a response body is one of the easiest things in a system
    to end up in a proxy log.
    """

    #: False when the device has never registered a push token. The preference
    #: values below are then the server defaults rather than anything stored.
    registered: bool
    alerts_enabled: bool
    min_severity: str
    quiet_hours_start: Optional[int] = None
    quiet_hours_end: Optional[int] = None
    timezone: Optional[str] = None
    #: Severity that is delivered even inside the quiet window, so the settings
    #: screen can say which alerts will still come through instead of implying
    #: that quiet hours silence everything.
    quiet_hours_breakthrough: str
    #: Whether this deployment sends at all. False means preferences are stored
    #: and honoured but nothing is dispatched.
    delivery_enabled: bool


# ---------------------------------------------------------------------------
# AI
# ---------------------------------------------------------------------------
class AiImpactRequest(StrictModel):
    category: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=3000)
    country: Optional[str] = Field(default=None, max_length=100)
    severity: Optional[str] = Field(default=None, max_length=50)


class AiImpactResponse(BaseModel):
    precautions: List[str]
    reach: str
    economic_impact: str
    # False when the model was unreachable and this is the static fallback, so
    # the UI can label it instead of passing guidance off as analysis.
    ai_generated: bool = True


class AiHealthRequest(StrictModel):
    temperature: float = Field(ge=-100, le=70)
    humidity: float = Field(ge=0, le=100)
    uv_index: float = Field(ge=0, le=20)
    aqi: Optional[int] = Field(default=None, ge=0, le=1000)
    age: Optional[int] = Field(default=None, ge=0, le=150)
    pre_existing_conditions: Optional[str] = Field(default=None, max_length=500)


class AiHealthResponse(BaseModel):
    risk_level: str
    advice: str
    clothing: Optional[str] = None
    hydration: Optional[str] = None
    outdoor_activity: Optional[str] = None
    ai_generated: bool = True
    disclaimer: str = (
        "General wellbeing guidance generated from environmental data. "
        "Not medical advice. Consult a healthcare professional for medical concerns."
    )


class AiChatRequest(StrictModel):
    message: str = Field(min_length=1, max_length=1000)
    location_context: Optional[str] = Field(default=None, max_length=2000)


class AiChatResponse(BaseModel):
    reply: str
    ai_generated: bool = True


class DiseaseRisk(BaseModel):
    name: str
    risk: str
    confidence: Optional[float] = Field(default=None, ge=0, le=1)
    reason: Optional[str] = None


class DiseaseRiskResponse(BaseModel):
    diseases: List[DiseaseRisk] = Field(default_factory=list)
    general_advice: Optional[str] = None
    ai_generated: bool = True
    note: str = (
        "AI-generated risk estimates based on environmental conditions. "
        "Not a medical diagnosis, prediction, or substitute for public health guidance."
    )


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------
class AdminLoginRequest(StrictModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class AdminLoginResponse(BaseModel):
    access_token: str
    token_type: str
    expires_in: int
    expires_at: str


class AdminReportsResponse(BaseModel):
    reports: List[Report]
    total: int
    limit: int
    offset: int


class AdminModerateRequest(StrictModel):
    status: ReportStatus
    reason: Optional[str] = Field(default=None, max_length=500)


class AdminStats(BaseModel):
    reports: Dict[str, int]
    push_tokens: int
    cache_backend: str
    rate_limit_backend: str


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    service: str
    release: str
    environment: str
    features: Dict[str, bool]
    dependencies: Dict[str, str]


# Resolves the forward reference in LocationIntel.
LocationIntel.model_rebuild()
