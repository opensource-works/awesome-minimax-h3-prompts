from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import prepare_uploads  # noqa: E402
import verify_uploads  # noqa: E402
from authorized_manifests import (  # noqa: E402
    build_github_attachment_manifest, build_r2_manifest, mirror_id_for,
)
from catalog import export_posts, validate_catalog  # noqa: E402


class StagingDownloadSafetyTests(unittest.TestCase):
    class Response:
        def __init__(self, final_url: str, content_type: str, data: bytes = b"video"):
            self.final_url = final_url
            self.headers = {
                "Content-Type": content_type,
                "Content-Length": str(len(data)),
            }
            self.data = data
            self.offset = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return self.final_url

        def read(self, size: int):
            chunk = self.data[self.offset:self.offset + size]
            self.offset += len(chunk)
            return chunk

    def test_download_rejects_redirect_host_and_non_video_content(self):
        cases = [
            self.Response("https://evil.example/video.mp4", "video/mp4"),
            self.Response("https://video.twimg.com/video.mp4", "text/html"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "asset.mp4"
            for response in cases:
                with self.subTest(response=response.final_url):
                    with mock.patch.object(
                        prepare_uploads, "urlopen", return_value=response
                    ):
                        with self.assertRaises((RuntimeError, ValueError)):
                            prepare_uploads.download_file(
                                "https://video.twimg.com/source.mp4",
                                destination,
                                1024,
                                10,
                                "x",
                            )
                    self.assertFalse(destination.exists())

    def test_download_enforces_limit_and_accepts_safe_video(self):
        oversized = self.Response(
            "https://video.twimg.com/video.mp4", "video/mp4", b"12345"
        )
        oversized.headers["Content-Length"] = "2048"
        safe = self.Response(
            "https://video.twimg.com/video.mp4", "video/mp4; charset=binary", b"safe"
        )
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "asset.mp4"
            with mock.patch.object(prepare_uploads, "urlopen", return_value=oversized):
                with self.assertRaises(prepare_uploads.TooLargeError):
                    prepare_uploads.download_file(
                        "https://video.twimg.com/source.mp4",
                        destination,
                        1024,
                        10,
                        "x",
                    )
            with mock.patch.object(prepare_uploads, "urlopen", return_value=safe):
                byte_count, digest = prepare_uploads.download_file(
                    "https://video.twimg.com/source.mp4",
                    destination,
                    1024,
                    10,
                    "x",
                )
            self.assertEqual(4, byte_count)
            self.assertEqual(hashlib.sha256(b"safe").hexdigest(), digest)
            self.assertEqual(b"safe", destination.read_bytes())


class VerificationProjectionTests(unittest.TestCase):
    def test_success_refreshes_posts_and_both_authorized_manifests(self):
        catalog = json.loads((ROOT / "data/catalog.json").read_text())
        item = next(iter(catalog["items"].values()))
        media = item["media"][0]
        evidence_id = "ev_github_verification_projection_test"
        actor_id = "act_repository_opensource-works"
        catalog["evidence"][evidence_id] = {
            "id": evidence_id,
            "kind": "permission",
            "url": "https://example.com/public-github-permission",
            "source_id": item["canonical_source_id"],
            "observed_at": "2026-08-11T00:00:00Z",
            "captured_by_actor_id": actor_id,
            "visibility": "public",
            "rights_assertion": {
                "asset_item_ids": [item["id"]],
                "grantor_actor_ids": [actor_id],
                "granted_scopes": ["download", "mirror_github"],
            },
        }
        item["rights"]["video_republication"] = {
            "status": "granted",
            "license_spdx": None,
            "granted_scopes": ["download", "mirror_github"],
            "grantor_actor_ids": [actor_id],
            "granted_at": "2026-08-11T00:00:00Z",
            "expires_at": None,
            "evidence_ids": [evidence_id],
        }
        filename = "ghatt--projection-test.mp4"
        payload = b"verified github attachment"
        digest = hashlib.sha256(payload).hexdigest()
        mirror_id = mirror_id_for(item["id"], media["media_id"])
        media["delivery"]["mirrors"].append({
            "mirror_id": mirror_id,
            "provider": "github_attachment",
            "artifact": "video",
            "url": "https://github.com/user-attachments/assets/12345678-1234-1234-1234-123456789abc",
            "state": "quarantined",
            "bytes": len(payload),
            "sha256": digest,
            "staged_filename": filename,
            "permission_evidence_ids": [evidence_id],
            "uploaded_at": "2026-08-11T00:00:00Z",
        })
        entry = {
            "filename": filename,
            "item_id": item["id"],
            "media_id": media["media_id"],
            "source_id": media["source_id"],
            "source_media_id": media["source_media_id"],
            "bytes": len(payload),
            "sha256": digest,
            "permission_evidence_ids": [evidence_id],
        }
        index = {
            "schema_version": "github-attachments-index-v2",
            "catalog_schema_version": catalog["schema_version"],
            "collection_id": catalog["collection"]["id"],
            "generated_at": "2026-08-11T00:00:00Z",
            "identity": "item_id/media_id",
            "entries": {filename: entry},
        }
        self.assertEqual([], validate_catalog(catalog))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {
                "index": root / "index.json",
                "catalog": root / "catalog.json",
                "posts": root / "posts.json",
                "manifest": root / "github-attachments.json",
                "r2_manifest": root / "r2-mirrors.json",
            }
            paths["index"].write_text(json.dumps(index))
            paths["catalog"].write_text(json.dumps(catalog))
            args = SimpleNamespace(
                **paths,
                jobs=1,
                timeout=10,
            )
            with mock.patch.object(
                verify_uploads, "utc_now", return_value="2026-08-11T12:00:00Z"
            ):
                result = verify_uploads.run(
                    args, fetcher=lambda _url, _timeout: (len(payload), digest)
                )
            self.assertEqual(0, result)

            updated = json.loads(paths["catalog"].read_text())
            posts = json.loads(paths["posts"].read_text())
            github_manifest = json.loads(paths["manifest"].read_text())
            r2_manifest = json.loads(paths["r2_manifest"].read_text())
            self.assertEqual([], validate_catalog(updated))
            self.assertEqual(export_posts(updated), posts)
            self.assertEqual(
                build_github_attachment_manifest(updated, github_manifest["generated_at"]),
                github_manifest,
            )
            self.assertEqual(
                build_r2_manifest(updated, r2_manifest["generated_at"]),
                r2_manifest,
            )


if __name__ == "__main__":
    unittest.main()
