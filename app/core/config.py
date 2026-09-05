"""Application settings.

Single source of truth for configuration. Everything comes from the
environment (12-factor), is validated once at import time, and is exposed
through :func:`get_settings`.

Production safety rails live in :meth:`Settings._validate_production` — the
process refuses to boot with an insecure configuration rather than starting
up and quietly serving traffic with, say, a wildcard CORS policy.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import List, Literal, Optional, Tuple

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent

Environment = Literal["development", "test", "staging", "production"]

# The severity vocabulary, canonically capitalised. Duplicated from the CHECK
# constraint on `push_tokens.min_severity` rather than imported from
# `app.services.alert_match`, because `app.core` must not depend on
# `app.services` — settings are constructed at import time, before any service
# exists. The ranking logic stays in one place; only the spelling is repeated,
# and the field validator below is what keeps the two from drifting silently.
SEVERITY_LEVELS: Tuple[str, ...] = ("Low", "Minor", "Moderate", "Severe", "Extreme")


def _split_csv(raw: str) -> List[str]:
    """Parse a comma-separated env var into a clean list."""
    return [part.strip() for part in raw.split(",") if part.strip()]


class ConfigurationError(RuntimeError):
    """Raised when the process is configured in a way that must not boot."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        # Lets tests construct Settings(environment="test", ...) by field name
        # while the environment still populates via the uppercase aliases.
        populate_by_name=True,
    )

    # ---------- Runtime ----------
    environment: Environment = Field(default="development", alias="ENVIRONMENT")
    service_name: str = Field(default="sentinelai-api", alias="SERVICE_NAME")
    release: str = Field(default="dev", alias="RELEASE")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_format: Literal["json", "console"] = Field(default="json", alias="LOG_FORMAT")

    # ---------- Datastore ----------
    supabase_url: str = Field(default="", alias="SUPABASE_URL")
    supabase_key: SecretStr = Field(default=SecretStr(""), alias="SUPABASE_KEY")

    # ---------- Cache / rate-limit backend ----------
    # Optional. Without it the process falls back to per-worker in-memory
    # structures, which is correct but does not coordinate across workers.
    redis_url: Optional[str] = Field(default=None, alias="REDIS_URL")

    # ---------- Upstream credentials ----------
    waqi_token: SecretStr = Field(default=SecretStr(""), alias="WAQI_TOKEN")
    gemini_api_key: Optional[SecretStr] = Field(default=None, alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-2.5-flash", alias="GEMINI_MODEL")

    # Nominatim and Overpass both require a contactable User-Agent by policy.
    # Sending a generic UA is grounds for an IP ban.
    contact_email: str = Field(default="", alias="CONTACT_EMAIL")
    public_app_url: str = Field(default="https://sentinelai.app", alias="PUBLIC_APP_URL")

    # ---------- HTTP surface ----------
    allowed_origins_raw: str = Field(default="*", alias="ALLOWED_ORIGINS")
    trusted_hosts_raw: str = Field(default="*", alias="TRUSTED_HOSTS")
    enable_docs: Optional[bool] = Field(default=None, alias="ENABLE_DOCS")
    enable_hsts: Optional[bool] = Field(default=None, alias="ENABLE_HSTS")
    root_path: str = Field(default="", alias="ROOT_PATH")

    # ---------- Admin auth ----------
    admin_jwt_secret: SecretStr = Field(default=SecretStr(""), alias="ADMIN_JWT_SECRET")
    admin_username: str = Field(default="", alias="ADMIN_USERNAME")
    # bcrypt hash, never a plaintext password.
    admin_password_hash: SecretStr = Field(default=SecretStr(""), alias="ADMIN_PASSWORD_HASH")
    admin_token_ttl_minutes: int = Field(default=60, ge=5, le=1440, alias="ADMIN_TOKEN_TTL_MINUTES")

    # ---------- Outbound HTTP ----------
    http_timeout_seconds: float = Field(default=12.0, gt=0, le=60, alias="HTTP_TIMEOUT_SECONDS")
    http_max_connections: int = Field(default=100, ge=10, alias="HTTP_MAX_CONNECTIONS")
    http_max_keepalive: int = Field(default=20, ge=5, alias="HTTP_MAX_KEEPALIVE")
    http_max_retries: int = Field(default=2, ge=0, le=5, alias="HTTP_MAX_RETRIES")

    # ---------- Cache TTLs (seconds) ----------
    # Tuned to each upstream's update cadence and politeness policy.
    cache_ttl_aqi: int = Field(default=300, ge=0, alias="CACHE_TTL_AQI")
    cache_ttl_disasters: int = Field(default=600, ge=0, alias="CACHE_TTL_DISASTERS")
    cache_ttl_weather: int = Field(default=600, ge=0, alias="CACHE_TTL_WEATHER")
    cache_ttl_geocode: int = Field(default=86400, ge=0, alias="CACHE_TTL_GEOCODE")
    cache_ttl_hospitals: int = Field(default=86400, ge=0, alias="CACHE_TTL_HOSPITALS")
    cache_ttl_ai: int = Field(default=1800, ge=0, alias="CACHE_TTL_AI")
    cache_max_entries: int = Field(default=5000, ge=100, alias="CACHE_MAX_ENTRIES")

    # ---------- Rate limits (requests per window) ----------
    rate_limit_enabled: bool = Field(default=True, alias="RATE_LIMIT_ENABLED")
    rate_limit_read: int = Field(default=120, ge=1, alias="RATE_LIMIT_READ")
    rate_limit_write: int = Field(default=20, ge=1, alias="RATE_LIMIT_WRITE")
    rate_limit_ai: int = Field(default=10, ge=1, alias="RATE_LIMIT_AI")
    rate_limit_auth: int = Field(default=5, ge=1, alias="RATE_LIMIT_AUTH")
    rate_limit_window_seconds: int = Field(default=60, ge=1, alias="RATE_LIMIT_WINDOW_SECONDS")

    # Number of reverse-proxy hops we trust for client-IP extraction. 0 means
    # "trust nothing, use the socket peer" — the correct default when the app
    # is exposed directly, since X-Forwarded-For is otherwise spoofable.
    trusted_proxy_hops: int = Field(default=0, ge=0, le=5, alias="TRUSTED_PROXY_HOPS")

    # ---------- Push alerts ----------
    # Off by default, and deliberately so. Enabling this starts sending real
    # notifications to real phones from whatever data the configured environment
    # happens to hold; a staging database restored from production would page
    # every user. Sending is therefore something a deployment opts into.
    push_enabled: bool = Field(default=False, alias="PUSH_ENABLED")

    # Optional. Expo accepts unauthenticated sends for most projects; an access
    # token is required once "enhanced security for push notifications" is on for
    # the project, and is a good idea regardless — it stops anyone who extracts a
    # push token from the app bundle sending notifications as us.
    expo_access_token: Optional[SecretStr] = Field(default=None, alias="EXPO_ACCESS_TOKEN")

    # How often a dispatch pass runs. 15 minutes is a compromise: USGS publishes
    # within a few minutes of a quake, so this is late enough to matter for a
    # distant event and early enough for the ones people can act on, while
    # keeping the upstream fan-out to four requests per pass.
    push_dispatch_interval_seconds: int = Field(
        default=900, ge=60, le=21600, alias="PUSH_DISPATCH_INTERVAL_SECONDS"
    )

    # How far back a pass looks for events. Longer than the interval on purpose:
    # a pass that crashed, a deploy, or an upstream outage must not create a
    # permanent gap. Re-examining an event is free — `sent_alerts` makes the
    # second look a no-op.
    push_lookback_hours: int = Field(default=24, ge=1, le=168, alias="PUSH_LOOKBACK_HOURS")

    # Applied to devices that have not chosen a threshold. Matches the column
    # default in `0002_alert_delivery.sql`.
    push_default_min_severity: str = Field(default="Moderate", alias="PUSH_DEFAULT_MIN_SEVERITY")

    # Severity that overrides quiet hours. A user asleep at 3 a.m. still wants to
    # know about a tsunami; that is the difference between a notification setting
    # and a safety product. In config rather than in code so the line can be
    # moved without a deploy.
    push_quiet_hours_breakthrough: str = Field(
        default="Severe", alias="PUSH_QUIET_HOURS_BREAKTHROUGH"
    )

    # Attempts per (device, event) before it is abandoned. Three, because the
    # failures worth retrying are transient; a fourth attempt at a token Expo has
    # retired is just noise in the logs.
    push_max_attempts: int = Field(default=3, ge=1, le=10, alias="PUSH_MAX_ATTEMPTS")

    # Devices per page of the dispatch walk. Expo's own limit is 100
    # notifications per request, so this keeps a page's worth of sends close to
    # one upstream request.
    push_batch_size: int = Field(default=100, ge=1, le=500, alias="PUSH_BATCH_SIZE")

    # The volume cap, and the single most important number here. A favourite with
    # a wide radius during an active earthquake sequence can match dozens of
    # events that each individually deserve a notification. Delivered as dozens
    # of notifications, the user mutes the app — and then hears nothing about the
    # one that mattered.
    push_max_per_device_per_pass: int = Field(
        default=3, ge=1, le=20, alias="PUSH_MAX_PER_DEVICE_PER_PASS"
    )

    # Retention for the delivery ledger. Matches the default argument of
    # `prune_old_data(retain_days)`.
    sent_alerts_retention_days: int = Field(
        default=30, ge=1, le=365, alias="SENT_ALERTS_RETENTION_DAYS"
    )

    # ---------- Derived ----------
    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_test(self) -> bool:
        return self.environment == "test"

    @property
    def allowed_origins(self) -> List[str]:
        return _split_csv(self.allowed_origins_raw)

    @property
    def trusted_hosts(self) -> List[str]:
        return _split_csv(self.trusted_hosts_raw)

    @property
    def allow_credentials(self) -> bool:
        """Credentialed CORS is incompatible with a wildcard origin.

        The spec forbids ``Access-Control-Allow-Origin: *`` together with
        ``Access-Control-Allow-Credentials: true``; browsers reject it. Rather
        than shipping a config that silently breaks, we only enable
        credentials once origins are explicitly enumerated.
        """
        return "*" not in self.allowed_origins

    @property
    def docs_enabled(self) -> bool:
        if self.enable_docs is not None:
            return self.enable_docs
        return not self.is_production

    @property
    def hsts_enabled(self) -> bool:
        if self.enable_hsts is not None:
            return self.enable_hsts
        return self.is_production

    @property
    def has_supabase(self) -> bool:
        return bool(self.supabase_url and self.supabase_key.get_secret_value())

    @property
    def has_gemini(self) -> bool:
        return bool(self.gemini_api_key and self.gemini_api_key.get_secret_value())

    @property
    def has_admin_auth(self) -> bool:
        return bool(
            self.admin_username
            and self.admin_password_hash.get_secret_value()
            and self.admin_jwt_secret.get_secret_value()
        )

    @property
    def user_agent(self) -> str:
        """Policy-compliant User-Agent for Nominatim / Overpass / OSM."""
        contact = self.contact_email or self.public_app_url
        return f"SentinelAI/{self.release} ({contact})"

    @property
    def has_expo_access_token(self) -> bool:
        return bool(self.expo_access_token and self.expo_access_token.get_secret_value())

    # ---------- Validation ----------
    @field_validator("push_default_min_severity", "push_quiet_hours_breakthrough")
    @classmethod
    def _canonical_severity(cls, value: str) -> str:
        """Accept any casing, store the canonical spelling, reject the unknown.

        Case-insensitive because ``PUSH_DEFAULT_MIN_SEVERITY=moderate`` is an
        obvious thing to write and refusing to boot over capitalisation would be
        theatre. Unknown values are a different matter: ``min_severity`` is
        written into a column with a CHECK constraint, so a typo that reached the
        database would surface as a 503 on an unrelated request instead of a
        configuration error at startup.
        """
        cleaned = value.strip()
        for level in SEVERITY_LEVELS:
            if cleaned.lower() == level.lower():
                return level
        allowed = ", ".join(SEVERITY_LEVELS)
        raise ValueError(f"must be one of: {allowed}")

    @model_validator(mode="after")
    def _validate_production(self) -> Settings:
        if not self.is_production:
            return self

        problems: List[str] = []

        if "*" in self.allowed_origins:
            problems.append(
                "ALLOWED_ORIGINS must enumerate explicit origins in production; '*' is not allowed."
            )
        if "*" in self.trusted_hosts:
            problems.append(
                "TRUSTED_HOSTS must enumerate explicit hostnames in production; "
                "'*' permits Host-header injection."
            )
        if not self.has_supabase:
            problems.append("SUPABASE_URL and SUPABASE_KEY are required in production.")
        if not self.waqi_token.get_secret_value():
            problems.append("WAQI_TOKEN is required in production; AQI endpoints depend on it.")
        if not self.contact_email:
            problems.append(
                "CONTACT_EMAIL is required in production: Nominatim and Overpass "
                "usage policies require a contactable User-Agent."
            )
        secret = self.admin_jwt_secret.get_secret_value()
        if secret and len(secret) < 32:
            problems.append("ADMIN_JWT_SECRET must be at least 32 characters.")
        if self.docs_enabled:
            problems.append(
                "ENABLE_DOCS must be false in production, or unset to use the secure default."
            )
        if self.push_enabled and not self.has_supabase:
            problems.append(
                "PUSH_ENABLED requires Supabase: push tokens, alert preferences and the "
                "delivery ledger all live in the database, and without it the dispatcher "
                "would run every pass and send nothing."
            )

        if problems:
            bullets = "\n".join(f"  - {p}" for p in problems)
            raise ConfigurationError(
                "Refusing to start: invalid production configuration.\n" + bullets
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    try:
        return Settings()  # type: ignore[call-arg]
    except ConfigurationError as exc:
        # Emitted before logging is configured, so write straight to stderr.
        print(f"\n{exc}\n", file=sys.stderr)  # noqa: T201
        raise


settings = get_settings()
