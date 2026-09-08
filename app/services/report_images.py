"""Private Supabase Storage helpers for report evidence images."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Dict

from app.core.config import settings
from app.db.supabase import require_client, run_db

_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
_SIGNED_UPLOAD_URL_TTL_SECONDS = 7200


def owner_prefix(device_id: str) -> str:
    return hashlib.sha256(device_id.encode("utf-8")).hexdigest()


def is_owned_path(device_id: str, path: str) -> bool:
    return path.startswith(f"reports/{owner_prefix(device_id)}/")


async def create_upload_url(device_id: str, content_type: str) -> Dict[str, Any]:
    extension = _EXTENSIONS[content_type]
    path = f"reports/{owner_prefix(device_id)}/{uuid.uuid4().hex}.{extension}"
    client = require_client()

    def _create() -> Dict[str, Any]:
        result = client.storage.from_(settings.report_image_bucket).create_signed_upload_url(path)
        if not isinstance(result, dict):
            raise RuntimeError("Supabase returned an invalid upload URL")
        upload_url = result.get("signedUrl") or result.get("signed_url") or result.get("url")
        if not isinstance(upload_url, str) or not upload_url.startswith("https://"):
            raise RuntimeError("Supabase returned no secure upload URL")
        return {
            "upload_url": upload_url,
            "storage_path": path,
            "expires_in": _SIGNED_UPLOAD_URL_TTL_SECONDS,
        }

    return await run_db("reports.image_upload_url", _create)


async def signed_read_url(path: str) -> str:
    client = require_client()

    def _create() -> str:
        result = client.storage.from_(settings.report_image_bucket).create_signed_url(path, settings.report_image_url_ttl_seconds)
        if not isinstance(result, dict):
            raise RuntimeError("Supabase returned an invalid image URL")
        url = result.get("signedUrl") or result.get("signed_url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise RuntimeError("Supabase returned no secure image URL")
        return url

    return await run_db("reports.image_read_url", _create)


async def delete_image(path: str) -> None:
    """Delete a private evidence object after its report is removed."""
    client = require_client()

    def _delete() -> Any:
        result = client.storage.from_(settings.report_image_bucket).remove([path])
        if isinstance(result, dict) and result.get("error"):
            raise RuntimeError("Supabase could not delete the report image")
        return result

    await run_db("reports.image_delete", _delete)
