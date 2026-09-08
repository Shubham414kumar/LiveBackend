"""Safety checks for community-report evidence images."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models.schemas import ReportCreate


def _payload(image_path: str) -> dict[str, object]:
    return {
        "category": "flood",
        "title": "Waterlogged road",
        "lat": 28.6139,
        "lon": 77.209,
        "image_path": image_path,
    }


def test_report_accepts_owned_path_shape() -> None:
    path = "reports/" + "a" * 64 + "/" + "b" * 32 + ".jpg"
    assert ReportCreate(**_payload(path)).image_path == path


@pytest.mark.parametrize(
    "image_path",
    [
        "https://example.com/photo.jpg",
        "reports/not-a-device/photo.jpg",
        "reports/" + "a" * 64 + "/photo.gif",
    ],
)
def test_report_rejects_untrusted_image_paths(image_path: str) -> None:
    with pytest.raises(ValidationError):
        ReportCreate(**_payload(image_path))


def test_image_migration_targets_the_real_reports_table() -> None:
    migration = Path(__file__).resolve().parents[1] / "migrations" / "0003_report_images.sql"
    sql = migration.read_text(encoding="utf-8")
    assert "alter table public.community_reports" in sql
    assert "alter table public.reports" not in sql
