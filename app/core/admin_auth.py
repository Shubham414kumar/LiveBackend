"""Admin authentication.

The admin dashboard previously shipped with a hardcoded "DEV MODE — No Auth"
badge and called a ``POST /api/admin/login`` endpoint that did not exist. This
module is that endpoint's backing logic.

Design notes:

* Credentials are a username plus a **bcrypt hash** in the environment. No
  plaintext password ever lives in config, and no admin table is required.
* Tokens are short-lived HS256 JWTs with ``iat``/``exp``/``jti`` and an
  explicit audience, verified on every admin request.
* Failed logins are compared in constant time and rate limited under the
  ``auth`` bucket to make credential stuffing impractical.
"""

from __future__ import annotations

import hmac
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, Optional

import bcrypt
import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings
from app.core.errors import AuthError, ConfigurationMissingError
from app.core.logging import get_logger

logger = get_logger(__name__)

ALGORITHM = "HS256"
AUDIENCE = "sentinelai-admin"
ISSUER = "sentinelai-api"

# auto_error=False so a missing header produces our problem+json shape rather
# than FastAPI's default body.
_bearer = HTTPBearer(auto_error=False, description="Admin JWT from /api/admin/login")


def hash_password(plaintext: str) -> str:
    """Generate a bcrypt hash. Used by the `make admin-hash` helper script."""
    return bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_credentials(username: str, password: str) -> bool:
    """Check admin credentials in constant time with respect to the username."""
    if not settings.has_admin_auth:
        return False

    expected_user = settings.admin_username
    stored_hash = settings.admin_password_hash.get_secret_value()

    user_ok = hmac.compare_digest(username.encode("utf-8"), expected_user.encode("utf-8"))

    # Always run bcrypt, even when the username is wrong, so response timing
    # doesn't reveal whether the username exists.
    try:
        password_ok = bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("utf-8"))
    except (ValueError, TypeError):
        logger.error("ADMIN_PASSWORD_HASH is not a valid bcrypt hash; admin login cannot succeed.")
        password_ok = False

    return user_ok and password_ok


def create_access_token(subject: str) -> Dict[str, Any]:
    if not settings.admin_jwt_secret.get_secret_value():
        raise ConfigurationMissingError(
            "Admin authentication is not configured on this deployment."
        )

    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=settings.admin_token_ttl_minutes)
    payload = {
        "sub": subject,
        "role": "admin",
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": uuid.uuid4().hex,
        "aud": AUDIENCE,
        "iss": ISSUER,
    }
    token = jwt.encode(payload, settings.admin_jwt_secret.get_secret_value(), algorithm=ALGORITHM)
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": settings.admin_token_ttl_minutes * 60,
        "expires_at": expires_at.isoformat(),
    }


def decode_token(token: str) -> Dict[str, Any]:
    secret = settings.admin_jwt_secret.get_secret_value()
    if not secret:
        raise ConfigurationMissingError(
            "Admin authentication is not configured on this deployment."
        )
    try:
        return jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.ExpiredSignatureError:
        raise AuthError("Session expired. Please sign in again.") from None
    except jwt.InvalidTokenError:
        raise AuthError("Invalid authentication token.") from None


async def require_admin(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> Dict[str, Any]:
    """Dependency guarding every ``/api/admin/*`` route."""
    if not settings.has_admin_auth:
        raise ConfigurationMissingError(
            "Admin authentication is not configured. Set ADMIN_USERNAME, "
            "ADMIN_PASSWORD_HASH and ADMIN_JWT_SECRET to enable the dashboard."
        )
    if credentials is None or not credentials.credentials:
        raise AuthError("Missing bearer token.")
    if credentials.scheme.lower() != "bearer":
        raise AuthError("Authorization scheme must be Bearer.")

    claims = decode_token(credentials.credentials)
    if claims.get("role") != "admin":
        raise AuthError("Token is not an admin token.")

    request.state.admin_subject = claims.get("sub")
    return claims


__all__ = [
    "create_access_token",
    "decode_token",
    "hash_password",
    "require_admin",
    "verify_credentials",
]
