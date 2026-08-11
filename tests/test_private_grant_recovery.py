from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import activate_verified_attachments as recovery  # noqa: E402
import import_verified_prompt_threads as prompt_import  # noqa: E402


class PrivateGrantRecoverySecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads((ROOT / "data/catalog.json").read_text())
        cls.lock = json.loads((ROOT / "data/verified-prompt-imports.json").read_text())
        collection_id = cls.catalog["collection"]["id"]
        cls.repo_key = "minimax" if "minimax" in collection_id else "seedance"

    def test_existing_attestation_must_match_scope_identity_time_and_no_expiry(self):
        rights = {
            "status": "granted",
            "grant_verification": "maintainer_attestation",
            "evidence_ids": [],
            "grantor_actor_ids": ["act_grantor"],
            "granted_scopes": ["download", "mirror_github"],
            "granted_at": "2026-08-11T16:52:51Z",
            "expires_at": None,
        }
        self.assertTrue(recovery.matches_existing_attestation(
            rights,
            "act_grantor",
            {"download", "mirror_github"},
            "2026-08-11T16:52:51Z",
        ))
        for field, value in (
            ("expires_at", "2026-08-11T16:52:52Z"),
            ("evidence_ids", ["ev_other"]),
            ("grantor_actor_ids", ["act_other"]),
            ("granted_scopes", ["download", "mirror_r2"]),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(rights)
                changed[field] = value
                self.assertFalse(recovery.matches_existing_attestation(
                    changed,
                    "act_grantor",
                    {"download", "mirror_github"},
                    "2026-08-11T16:52:51Z",
                ))

    def test_verification_lock_rejects_unreviewed_reply(self):
        fake_payload = [{
            "repo": self.repo_key,
            "item_id": next(iter(self.catalog["items"])),
            "root_id": "1",
            "root_url": "https://x.com/fake/status/1",
            "prompt_replies": [{
                "reply_id": "2",
                "url": "https://x.com/fake/status/2",
                "author": {"handle": "fake"},
                "created_at": "Mon Feb 09 15:11:43 +0000 2026",
                "replying_to": {"status": "1"},
                "text": "FABRICATED",
            }],
        }]
        with self.assertRaisesRegex(ValueError, "reviewed public-source lock"):
            prompt_import.validate_verification_lock(
                self.lock,
                fake_payload,
                self.lock["reviewed_payload_sha256"],
                self.repo_key,
            )

    def test_prompt_import_timestamps_require_rfc3339_offsets(self):
        self.assertTrue(prompt_import.is_rfc3339("2026-08-11T16:52:51Z"))
        self.assertTrue(prompt_import.is_rfc3339("2026-08-12T00:52:51+08:00"))
        self.assertFalse(prompt_import.is_rfc3339("not-a-time"))
        self.assertFalse(prompt_import.is_rfc3339("2026-08-11T16:52:51"))

    def test_review_lock_entries_match_published_prompt_evidence(self):
        relevant = {
            reply_id: record
            for reply_id, record in self.lock["entries"].items()
            if record["repo"] == self.repo_key
        }
        self.assertTrue(relevant)
        for reply_id, record in relevant.items():
            with self.subTest(reply_id=reply_id):
                source_id = f"x:{reply_id}"
                source = self.catalog["sources"][source_id]
                evidence = self.catalog["evidence"][f"ev_prompt_x_{reply_id}"]
                self.assertEqual(record["reply_url"].lower(), source["url"].lower())
                self.assertEqual(source_id, evidence["source_id"])
                self.assertEqual("prompt_text", evidence["integrity_subject"])
                self.assertEqual(record["text_sha256"], evidence["integrity_sha256"])

    def test_removed_or_revoked_prompt_target_is_not_importable(self):
        item = copy.deepcopy(next(iter(self.catalog["items"].values())))
        grantor = item["rights"]["video_republication"]["grantor_actor_ids"][0]
        item["prompt"]["status"] = "removed"
        item["rights"]["prompt_republication"].update({
            "status": "revoked",
            "grant_verification": "maintainer_attestation",
        })
        errors = prompt_import.prompt_target_errors(
            self.catalog,
            item,
            grantor,
            "2026-08-11T16:52:51Z",
            "test",
        )
        self.assertTrue(any("removed prompt" in error for error in errors))
        self.assertTrue(any("rights cannot be overwritten" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
