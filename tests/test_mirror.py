from __future__ import annotations

import copy
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import mirror  # noqa: E402


def multi_source_catalog(source_media_id: str = "alt-media-2") -> dict:
    evidence_id = "ev_permission"
    item = {
        "id": "itm_test",
        "canonical_source_id": "x:canonical",
        "curation": {"status": "approved"},
        "rights": {"video_republication": {
            "status": "granted",
            "license_spdx": None,
            "granted_scopes": ["download", "mirror_r2"],
            "grantor_actor_ids": ["act_grantor"],
            "granted_at": "2026-08-11T00:00:00Z",
            "expires_at": None,
            "evidence_ids": [evidence_id],
        }},
        "media": [{
            "media_id": "med_test",
            "kind": "video",
            "source_id": "x:alternate",
            "source_media_id": source_media_id,
            "delivery": {"mode": "source_link", "mirrors": []},
        }],
    }
    return {
        "schema_version": "2.0.0",
        "collection": {"id": "test-collection"},
        "evidence": {evidence_id: {
            "id": evidence_id,
            "kind": "permission",
            "rights_assertion": {
                "asset_item_ids": ["itm_test"],
                "grantor_actor_ids": ["act_grantor"],
                "granted_scopes": ["download", "mirror_r2"],
                "license_spdx": None,
            },
        }},
        "sources": {
            "x:canonical": {
                "platform": "x",
                "availability": {"state": "available"},
                "media_observations": [{
                    "source_media_id": "canonical-media",
                    "direct_url": "https://video.twimg.com/canonical.mp4",
                    "variants": [],
                }],
            },
            "x:alternate": {
                "platform": "x",
                "availability": {"state": "available"},
                "media_observations": [
                    {
                        "source_media_id": "alt-media-1",
                        "direct_url": "https://video.twimg.com/wrong-first.mp4",
                        "variants": [],
                    },
                    {
                        "source_media_id": "alt-media-2",
                        "direct_url": "https://video.twimg.com/correct.mp4",
                        "variants": [],
                    },
                ],
            },
        },
        "items": {item["id"]: item},
    }


class ImmutableMirrorIdentityTests(unittest.TestCase):
    def test_volatile_cache_rejects_wrong_identity_collection_and_hosts(self):
        catalog = multi_source_catalog()
        base = {
            "schema_version": "volatile-media-cache-v1",
            "collection_id": "test-collection",
            "observations": {
                "x:alternate/alt-media-2": {
                    "source_id": "x:alternate",
                    "source_media_id": "alt-media-2",
                    "direct_url": "https://video.twimg.com/correct.mp4",
                    "thumbnail_url": "https://pbs.twimg.com/thumb.jpg",
                    "variants": [{
                        "container": "mp4",
                        "url": "https://video.twimg.com/correct-720.mp4",
                    }],
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            path.write_text(json.dumps(base))
            path.chmod(0o600)
            loaded = mirror.load_volatile_cache(path, catalog)
            self.assertIn("x:alternate/alt-media-2", loaded)

            bad_values = []
            wrong_collection = copy.deepcopy(base)
            wrong_collection["collection_id"] = "other"
            bad_values.append(wrong_collection)
            wrong_identity = copy.deepcopy(base)
            wrong_identity["observations"]["x:alternate/alt-media-2"]["source_media_id"] = "alt-media-1"
            bad_values.append(wrong_identity)
            evil_host = copy.deepcopy(base)
            evil_host["observations"]["x:alternate/alt-media-2"]["direct_url"] = "https://evil.example/video.mp4"
            bad_values.append(evil_host)
            insecure = copy.deepcopy(base)
            insecure["observations"]["x:alternate/alt-media-2"]["variants"][0]["url"] = "http://video.twimg.com/video.mp4"
            bad_values.append(insecure)
            for value in bad_values:
                with self.subTest(value=value):
                    path.write_text(json.dumps(value))
                    with self.assertRaises(SystemExit):
                        mirror.load_volatile_cache(path, catalog)

    def test_download_rejects_non_video_redirects_and_oversized_responses(self):
        class Response:
            def __init__(self, url, headers):
                self.url = url
                self.headers = headers

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def geturl(self):
                return self.url

            def read(self, _size):
                return b""

        cases = [
            Response(
                "https://video.twimg.com/video.mp4",
                {"Content-Type": "text/html", "Content-Length": "10"},
            ),
            Response(
                "https://evil.example/video.mp4",
                {"Content-Type": "video/mp4", "Content-Length": "10"},
            ),
            Response(
                "https://video.twimg.com/video.mp4",
                {
                    "Content-Type": "video/mp4",
                    "Content-Length": str(mirror.MAX_DOWNLOAD_BYTES + 1),
                },
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "video.mp4"
            for response in cases:
                with self.subTest(url=response.url, headers=response.headers):
                    with mock.patch.object(
                        mirror.urllib.request, "urlopen", return_value=response
                    ):
                        with self.assertRaises(RuntimeError):
                            mirror.download(
                                "https://video.twimg.com/video.mp4",
                                destination,
                                mirror.X_VIDEO_HOSTS,
                            )

    def test_multisource_item_uses_declared_source_and_exact_media_identity(self):
        jobs = mirror.eligible_jobs(multi_source_catalog())
        self.assertEqual(1, len(jobs))
        self.assertEqual("https://video.twimg.com/correct.mp4", jobs[0]["url"])
        self.assertEqual("x:alternate", jobs[0]["media"]["source_id"])

    def test_unknown_media_identity_never_falls_back_to_first_observation(self):
        self.assertEqual([], mirror.eligible_jobs(multi_source_catalog("missing-media")))

    def test_authorized_job_can_use_exact_gitignored_cache_overlay(self):
        catalog = multi_source_catalog()
        for source in catalog["sources"].values():
            for observation in source["media_observations"]:
                observation["direct_url"] = None
                observation["thumbnail_url"] = None
                observation["variants"] = []
        cache = {
            "x:alternate/alt-media-1": {
                "direct_url": "https://video.twimg.com/wrong-cache.mp4",
                "variants": [],
            },
            "x:alternate/alt-media-2": {
                "direct_url": "https://video.twimg.com/cached-correct.mp4",
                "variants": [],
            },
        }
        jobs = mirror.eligible_jobs(catalog, cache)
        self.assertEqual(1, len(jobs))
        self.assertEqual("https://video.twimg.com/cached-correct.mp4", jobs[0]["url"])
        self.assertIsNone(
            catalog["sources"]["x:alternate"]["media_observations"][1]["direct_url"]
        )

    def test_r2_manifest_is_namespaced_and_never_uses_legacy_shape(self):
        catalog = json.loads((ROOT / "data/catalog.json").read_text())
        manifest = mirror.active_manifest(catalog, "2026-08-11T00:00:00Z")
        self.assertEqual("r2-mirrors-manifest-v2", manifest["schema_version"])
        self.assertEqual("item_id/media_id/mirror_id", manifest["namespace"])
        self.assertIn("mirrors", manifest)
        self.assertNotIn("attachments", manifest)

    def test_r2_replacements_use_distinct_content_addressed_keys_and_ids(self):
        first = "a" * 64
        second = "b" * 64
        base = "v2/item/media"
        self.assertNotEqual(
            mirror.content_key(base, "video", first),
            mirror.content_key(base, "video", second),
        )
        self.assertNotEqual(
            mirror.r2_mirror_id("item", "media", "video", first),
            mirror.r2_mirror_id("item", "media", "video", second),
        )
        self.assertTrue(mirror.content_key(base, "video", first).endswith(".mp4"))
        self.assertTrue(mirror.content_key(base, "animated_preview", first).endswith(".webp"))

    def test_identical_content_refresh_does_not_queue_the_live_key_for_deletion(self):
        existing = {
            "mirror_id": "mir_same", "provider": "r2", "artifact": "video",
            "url": "https://example.com/same.mp4", "state": "active",
            "permission_evidence_ids": ["ev_old"],
        }
        delivery = {"mode": "authorized_mirror", "mirrors": [existing]}
        mirror.replace_active(delivery, {
            **existing, "permission_evidence_ids": ["ev_new"], "last_checked_at": "2026-08-11T12:00:00Z",
        })
        self.assertEqual(1, len(delivery["mirrors"]))
        self.assertEqual("active", existing["state"])
        self.assertEqual(["ev_new"], existing["permission_evidence_ids"])

    def test_partial_batch_failure_rolls_back_every_attempted_object(self):
        catalog = multi_source_catalog()
        item = catalog["items"]["itm_test"]
        source = catalog["sources"]["x:alternate"]
        jobs = []
        for suffix in ("first", "second"):
            media = {
                "media_id": f"med_{suffix}",
                "source_id": "x:alternate",
                "source_media_id": "alt-media-2",
                "delivery": {"mode": "source_link", "mirrors": []},
            }
            jobs.append({
                "item_key": f"itm_{suffix}",
                "item": item,
                "source": source,
                "media": media,
                "url": f"https://video.example/{suffix}.mp4",
                "width": 1280,
                "height": 720,
                "video_needed": True,
                "preview_needed": False,
                "evidence_ids": ["ev_permission"],
                "allowed_hosts": mirror.X_VIDEO_HOSTS,
            })

        class PartialR2:
            def __init__(self):
                self.objects = set()
                self.put_count = 0

            def put(self, _bucket, key, _data, _content_type):
                self.put_count += 1
                if self.put_count == 2:
                    raise RuntimeError("simulated second upload failure")
                self.objects.add(key)

            def head(self, _bucket, key):
                return key in self.objects

            def delete(self, _bucket, key):
                self.objects.discard(key)

        fake_r2 = PartialR2()

        def fake_download(url, path, _allowed_hosts):
            path.write_bytes(b"x" * 10_000)
            digest = ("a" if "first" in url else "b") * 64
            return 10_000, digest

        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            with (
                mock.patch.object(sys, "argv", ["mirror.py"]),
                mock.patch.object(mirror, "ROOT", temporary_root),
                mock.patch.object(
                    mirror,
                    "read_json",
                    side_effect=[
                        catalog,
                        {"r2": {"bucket": "test", "public_base": "https://media.example"}},
                    ],
                ),
                mock.patch.object(mirror, "validate_catalog", return_value=[]),
                mock.patch.object(mirror, "eligible_jobs", return_value=jobs),
                mock.patch.object(mirror, "download", side_effect=fake_download),
                mock.patch.dict(sys.modules, {"r2": fake_r2}),
            ):
                with self.assertRaisesRegex(SystemExit, "2"):
                    mirror.main()

            self.assertEqual(2, fake_r2.put_count)
            self.assertEqual(set(), fake_r2.objects)
            self.assertFalse(
                (temporary_root / "data/r2-upload-recovery.json").exists()
            )

    def test_rollback_never_deletes_an_object_that_predated_the_put(self):
        class ExistingR2:
            def __init__(self):
                self.objects = {"existing", "new"}
                self.deleted = []

            def head(self, _bucket, key):
                return key in self.objects

            def delete(self, _bucket, key):
                self.deleted.append(key)
                self.objects.discard(key)

        client = ExistingR2()
        attempts = [
            {"key": "existing", "existed_before": True, "state": "uploaded"},
            {"key": "new", "existed_before": False, "state": "uploaded"},
        ]
        self.assertEqual([], mirror.rollback_uploads(client, "bucket", attempts))
        self.assertEqual({"existing"}, client.objects)
        self.assertEqual(["new"], client.deleted)
        self.assertEqual("preserved_preexisting", attempts[0]["state"])




if __name__ == "__main__":
    unittest.main()
