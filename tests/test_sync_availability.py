from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import catalog  # noqa: E402
import sync_availability  # noqa: E402


class AvailabilityDependencyTests(unittest.TestCase):
    def test_reddit_network_failure_does_not_count_as_successful_omission(self):
        value = copy.deepcopy(catalog.read_json(ROOT / "data" / "catalog.json"))
        source_id = next(
            key for key, source in value["sources"].items()
            if source.get("platform") == "reddit"
        )
        sync_availability.ensure_automation(value, "2026-08-11T12:00:00Z")

        with mock.patch.object(sync_availability, "reddit_token", return_value="test"), \
             mock.patch.object(
                 sync_availability, "request_json", side_effect=RuntimeError("network outage")
             ):
            self.assertEqual(
                (0, 0),
                sync_availability.sync_reddit(
                    value, [source_id], "2026-08-11T12:01:00Z", 2
                ),
            )
        availability = value["sources"][source_id]["availability"]
        self.assertEqual(0, availability["consecutive_successful_missing"])

        empty = ({"data": {"children": []}}, 200)
        with mock.patch.object(sync_availability, "reddit_token", return_value="test"), \
             mock.patch.object(sync_availability, "request_json", return_value=empty):
            self.assertEqual(
                (0, 0),
                sync_availability.sync_reddit(
                    value, [source_id], "2026-08-11T12:02:00Z", 2
                ),
            )
            self.assertNotEqual(
                "deleted", value["sources"][source_id]["availability"]["state"]
            )
            self.assertEqual(
                1,
                value["sources"][source_id]["availability"][
                    "consecutive_successful_missing"
                ],
            )
            self.assertEqual(
                (0, 1),
                sync_availability.sync_reddit(
                    value, [source_id], "2026-08-11T12:03:00Z", 2
                ),
            )
        self.assertEqual("deleted", value["sources"][source_id]["availability"]["state"])
        self.assertEqual(
            2,
            value["sources"][source_id]["availability"][
                "consecutive_successful_missing"
            ],
        )

    def test_deleted_prompt_comment_is_redacted_without_removing_item(self):
        value = catalog.read_json(ROOT / "data" / "catalog.json")
        value = copy.deepcopy(value)
        item = next(iter(value["items"].values()))
        root_id = item["canonical_source_id"]
        root = value["sources"][root_id]
        root_title = "ROOT-TITLE-MUST-STAY"
        root_text = "ROOT-TEXT-MUST-STAY"
        root_note = "ROOT-ATTRIBUTION-NOTE-MUST-STAY"
        item["display"]["title"]["text"] = root_title
        root["text"]["value"] = root_text
        item["attribution"]["original_video_creators"][0]["note"] = root_note
        actor_id = root["posted_by_actor_id"]
        comment_id = "x:9999999999999999999"
        comment_url = "https://x.com/example/status/9999999999999999999"
        at = "2026-08-11T12:00:00Z"
        value["sources"][comment_id] = {
            "id": comment_id, "platform": "x", "native_id": "9999999999999999999",
            "kind": "comment", "url": comment_url, "parent_source_id": root_id,
            "posted_at": at, "posted_date": "2026-08-11",
            "posted_by_actor_id": actor_id, "community": None,
            "text": {"status": "available", "value": "COMMENT-SECRET", "language": "en"},
            "metrics": {"observed_at": at, "views": None, "likes": 1, "reposts": 0, "comments": None},
            "media_observations": [],
            "availability": {
                "state": "available", "checked_at": at, "last_available_at": at,
                "first_unavailable_at": None, "confirmed_at": None,
                "consecutive_failures": 0, "consecutive_successful_missing": 0,
                "http_status": 200, "evidence_ids": [],
            },
            "fetch": {"adapter": "test", "observed_at": at, "raw_sha256": None},
        }
        comment_evidence = "ev_comment_redaction_test"
        value["evidence"][comment_evidence] = {
            "id": comment_evidence, "kind": "source_comment", "url": comment_url,
            "source_id": comment_id, "observed_at": at, "excerpt": "COMMENT-SECRET",
            "captured_by_actor_id": "act_repository_opensource-works",
            "visibility": "public", "integrity_sha256": None,
        }
        item["prompt"] = {
            "status": "verbatim", "text": "COMMENT-SECRET", "language": "en",
            "source_id": comment_id, "source_url": comment_url,
            "capture_method": "comment_text", "is_verbatim": True,
            "evidence_ids": [comment_evidence],
        }
        item["annotations"] = [{
            "kind": "community_comment", "source_id": comment_id,
            "author_actor_id": actor_id, "text": "COMMENT-SECRET",
            "evidence_ids": [comment_evidence],
        }]

        sync_availability.ensure_automation(value, at)
        github_before = sync_availability.build_github_attachment_manifest(value, at)
        r2_before = sync_availability.build_r2_manifest(value, at)
        sync_availability.redact_and_retire(value, comment_id, "deleted", at, "test deletion")

        self.assertEqual("approved", item["curation"]["status"])
        self.assertEqual("removed", item["prompt"]["status"])
        self.assertIsNone(item["prompt"]["text"])
        self.assertEqual([], item["annotations"])
        self.assertEqual("deleted", value["sources"][comment_id]["availability"]["state"])
        self.assertNotIn("COMMENT-SECRET", json.dumps(value))
        self.assertEqual(root_title, item["display"]["title"]["text"])
        self.assertEqual(root_text, root["text"]["value"])
        self.assertEqual(root_note, item["attribution"]["original_video_creators"][0]["note"])
        posts = catalog.export_posts(value)
        post = next(post for post in posts if post["item_id"] == item["id"])
        self.assertIsNone(post["prompt"])
        self.assertEqual([], post["annotations"])
        self.assertEqual(github_before, sync_availability.build_github_attachment_manifest(value, at))
        self.assertEqual(r2_before, sync_availability.build_r2_manifest(value, at))
        self.assertEqual([], catalog.validate_catalog(value))

    def test_root_unavailability_redacts_all_content_and_synchronizes_outputs(self):
        value = copy.deepcopy(catalog.read_json(ROOT / "data" / "catalog.json"))
        item = next(iter(value["items"].values()))
        source_id = item["canonical_source_id"]
        source = value["sources"][source_id]
        candidate = value["candidates"][source_id]
        at = "2026-08-11T13:00:00Z"
        secret = "ROOT-SECRET-" + ("S" * 3109)
        permission_id = "ev_permission_root_redaction_test"
        actor_id = "act_repository_opensource-works"

        source["text"] = {"status": "available", "value": secret, "language": "en"}
        item["display"]["title"]["text"] = secret
        item["prompt"] = {
            "status": "verbatim", "text": secret, "language": "en",
            "source_id": source_id, "source_url": source["url"],
            "capture_method": "post_text", "is_verbatim": True,
            "evidence_ids": [permission_id],
        }
        item["annotations"] = [{
            "kind": "editorial_note", "source_id": None,
            "author_actor_id": actor_id, "text": secret, "created_at": at,
            "evidence_ids": [permission_id],
        }]
        item["attribution"]["original_video_creators"][0]["note"] = secret
        item["attribution"]["prompt_authors"][0]["note"] = secret
        candidate["review"]["note"] = secret
        candidate["review"].setdefault("history", []).append({"note": secret})
        value["evidence"][permission_id] = {
            "id": permission_id, "kind": "permission", "url": source["url"],
            "source_id": source_id, "observed_at": at, "excerpt": secret,
            "captured_by_actor_id": actor_id, "visibility": "public",
            "integrity_sha256": None,
            "rights_assertion": {
                "asset_item_ids": [item["id"]],
                "grantor_actor_ids": [actor_id],
                "granted_scopes": ["download", "mirror_r2", "mirror_github"],
            },
        }
        item["rights"]["video_republication"] = {
            "status": "granted", "license_spdx": None,
            "granted_scopes": ["download", "mirror_r2", "mirror_github"],
            "grantor_actor_ids": [actor_id], "granted_at": at,
            "expires_at": None, "evidence_ids": [permission_id],
        }
        media = item["media"][0]
        r2_url = "https://media.example.test/root-redaction.mp4"
        github_url = "https://github.com/user-attachments/assets/12345678-1234-1234-1234-123456789abc"
        media["delivery"]["mode"] = "authorized_mirror"
        media["delivery"]["mirrors"] = [
            {
                "mirror_id": "mir_r2_root_redaction", "provider": "r2",
                "artifact": "video", "url": r2_url, "bytes": 10,
                "sha256": "a" * 64, "uploaded_at": at, "last_checked_at": at,
                "state": "active", "permission_evidence_ids": [permission_id],
            },
            {
                "mirror_id": "mir_github_root_redaction", "provider": "github_attachment",
                "artifact": "video", "url": github_url, "bytes": 10,
                "sha256": "b" * 64, "uploaded_at": at, "verified_at": at,
                "last_checked_at": at, "state": "active",
                "permission_evidence_ids": [permission_id],
            },
        ]
        sync_availability.ensure_automation(value, at)
        self.assertEqual(1, len(sync_availability.build_r2_manifest(value, at)["mirrors"]))
        self.assertEqual(1, len(sync_availability.build_github_attachment_manifest(value, at)["attachments"]))

        sync_availability.redact_and_retire(
            value, source_id, "private", at, secret,
        )
        catalog.refresh_retirement_manifest(value, {"entries": []}, at)

        serialized = json.dumps(value)
        self.assertNotIn(secret, serialized)
        self.assertNotIn(r2_url, serialized)
        self.assertNotIn(github_url, serialized)
        self.assertIsNone(source["text"]["value"])
        self.assertNotEqual(secret, item["display"]["title"]["text"])
        self.assertIsNone(item["prompt"]["text"])
        self.assertEqual([], item["annotations"])
        self.assertIsNone(item["attribution"]["original_video_creators"][0]["note"])
        self.assertIsNone(candidate["review"]["note"])
        self.assertTrue(all(entry.get("note") is None for entry in candidate["review"]["history"]))
        self.assertNotIn(item["id"], {post["item_id"] for post in catalog.export_posts(value)})
        self.assertEqual({}, sync_availability.build_r2_manifest(value, at)["mirrors"])
        self.assertEqual({}, sync_availability.build_github_attachment_manifest(value, at)["attachments"])
        self.assertEqual([], catalog.validate_catalog(value))


if __name__ == "__main__":
    unittest.main()
