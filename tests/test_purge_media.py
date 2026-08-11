from __future__ import annotations

import hashlib
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import purge_media  # noqa: E402


PUBLIC_BASE = "https://media.example.test/catalog"


def example_job(suffix: str = "one") -> dict:
    url = example_url(suffix)
    return {
        "item_id": f"itm_{suffix}",
        "media_id": f"med_{suffix}",
        "source_id": f"x:{suffix}",
        "mirror": {
            "mirror_id": f"mir_{suffix}",
            "provider": "r2",
            "artifact": "video",
            "state": "pending_delete",
            "url": None,
            "former_url_sha256": hashlib.sha256(url.encode()).hexdigest(),
        },
    }


def example_url(suffix: str = "one") -> str:
    return f"{PUBLIC_BASE}/v2/item/media-{suffix}.mp4"


def retirement_record(job: dict) -> dict:
    mirror = job["mirror"]
    return {
        "mirror_id": mirror["mirror_id"],
        "provider": mirror["provider"],
        "artifact": mirror["artifact"],
        "item_id": job["item_id"],
        "media_id": job["media_id"],
        "source_id": job["source_id"],
        "state": "pending_delete",
        "url_sha256": mirror["former_url_sha256"],
    }


class R2DeletionPreflightTests(unittest.TestCase):
    def test_exact_pending_retirement_record_passes(self):
        job = example_job()
        retirement = {"entries": [retirement_record(job)]}
        self.assertEqual(
            purge_media.validate_r2_preflight(
                [job], retirement, PUBLIC_BASE, {"mir_one": example_url()}
            ), []
        )

    def test_missing_and_stale_url_records_are_rejected(self):
        job = example_job()
        missing = purge_media.validate_r2_preflight(
            [job], {"entries": []}, PUBLIC_BASE, {"mir_one": example_url()}
        )
        self.assertTrue(any("record is missing" in error for error in missing))

        record = retirement_record(job)
        record["url_sha256"] = "0" * 64
        stale = purge_media.validate_r2_preflight(
            [job], {"entries": [record]}, PUBLIC_BASE,
            {"mir_one": example_url()},
        )
        self.assertTrue(any("url_sha256" in error for error in stale))

    def test_every_catalog_identity_field_must_match(self):
        job = example_job()
        for field in ("provider", "artifact", "item_id", "media_id", "source_id"):
            with self.subTest(field=field):
                record = retirement_record(job)
                record[field] = "mismatch"
                errors = purge_media.validate_r2_preflight(
                    [job], {"entries": [record]}, PUBLIC_BASE,
                    {"mir_one": example_url()},
                )
                self.assertTrue(any(field in error for error in errors))

    def test_one_bad_job_aborts_batch_before_any_remote_request(self):
        first = example_job("first")
        second = example_job("second")
        retirement = {
            "entries": [retirement_record(first), retirement_record(second)]
        }
        retirement["entries"][1]["url_sha256"] = "f" * 64
        config = {"r2": {"bucket": "test", "public_base": PUBLIC_BASE}}
        url_map = {
            "mir_first": example_url("first"),
            "mir_second": example_url("second"),
        }
        fake_r2 = types.SimpleNamespace(
            head=mock.Mock(return_value=True), delete=mock.Mock()
        )

        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    "purge_media.py", "--provider", "r2",
                    "--url-map", "/private/locators.json", "--confirm-delete-r2",
                ],
            ),
            mock.patch.object(
                purge_media, "read_json", side_effect=[{}, retirement, config]
            ),
            mock.patch.object(purge_media, "validate_catalog", return_value=[]),
            mock.patch.object(
                purge_media, "cleanup_jobs", return_value=[first, second]
            ),
            mock.patch.object(purge_media, "load_private_url_map", return_value=url_map),
            mock.patch.dict(sys.modules, {"r2": fake_r2}),
        ):
            with self.assertRaisesRegex(SystemExit, "entire R2 deletion batch"):
                purge_media.main()

        fake_r2.head.assert_not_called()
        fake_r2.delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
