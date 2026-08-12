from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import validate as validator  # noqa: E402
import build as builder  # noqa: E402
import authorized_manifests  # noqa: E402
import sync_rights_expiry  # noqa: E402
from catalog import (  # noqa: E402
    export_posts, mirror_needs_cleanup, public_catalog, refresh_retirement_manifest,
)


class RepositoryValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads((ROOT / "data/catalog.json").read_text())
        cls.schema = json.loads((ROOT / "schema/catalog-v2.schema.json").read_text())
        cls.config = json.loads((ROOT / "config/collection.json").read_text())
        cls.posts = json.loads((ROOT / "data/posts.json").read_text())

    def test_repository_passes_every_validation_gate(self):
        report = validator.validate_repository(ROOT)
        self.assertEqual(
            {name: errors for name, errors in report.items() if errors},
            {},
        )

    def test_canonical_graph_and_schema_are_valid(self):
        self.assertEqual(
            validator.validate_canonical(self.catalog, self.schema, self.config),
            [],
        )

    def test_v1_projection_is_exactly_generated(self):
        self.assertEqual(export_posts(self.catalog), self.posts)
        self.assertEqual(validator.validate_v1_projection(self.catalog, self.posts), [])

    def test_v1_projection_drift_is_reported_with_a_path(self):
        posts = copy.deepcopy(self.posts)
        posts[0]["title"] += " changed"
        errors = validator.validate_v1_projection(self.catalog, posts)
        self.assertEqual(len(errors), 1)
        self.assertIn("$[0].title", errors[0])

    def test_export_uses_media_source_and_strict_media_identity_for_dimensions(self):
        value = copy.deepcopy(self.catalog)
        item = next(iter(value["items"].values()))
        canonical = value["sources"][item["canonical_source_id"]]
        canonical_url = canonical["url"]
        secondary_id = "x:9999999999999999999"
        secondary = copy.deepcopy(canonical)
        secondary.update({
            "id": secondary_id,
            "native_id": "9999999999999999999",
            "url": "https://x.com/example/status/9999999999999999999",
            "media_observations": [
                {"source_media_id": "wrong", "width": 1, "height": 2, "duration_ms": 3000},
                {"source_media_id": "wanted", "width": 1920, "height": 1080, "duration_ms": 4500},
            ],
        })
        value["sources"][secondary_id] = secondary
        item["source_ids"].append(secondary_id)
        item["media"][0]["source_id"] = secondary_id
        item["media"][0]["source_media_id"] = "wanted"

        post = next(value for value in export_posts(value) if value["item_id"] == item["id"])
        self.assertEqual(canonical_url, post["url"], "post identity must remain canonical")
        self.assertEqual(1920, post["video"]["width"])
        self.assertEqual(4.5, post["video"]["duration"])

        item["media"][0]["source_media_id"] = "missing"
        post = next(value for value in export_posts(value) if value["item_id"] == item["id"])
        self.assertIsNone(post["video"]["width"])
        self.assertEqual(0.0, post["video"]["duration"])

    def test_every_url_is_a_source_and_candidate(self):
        urls = validator.read_candidate_urls(ROOT / "scripts/urls.txt")
        self.assertEqual(validator.validate_candidate_coverage(self.catalog, urls), [])
        self.assertEqual(len(urls), len({url for _, url in urls}))

    def test_silently_dropped_legacy_urls_have_per_item_evidence(self):
        for candidate in self.catalog["candidates"].values():
            review = candidate.get("review") or {}
            if "legacy_drop_reason_unknown" not in (review.get("reason_codes") or []):
                continue
            self.assertTrue(review.get("evidence_ids"))
            self.assertTrue(all(value in self.catalog["evidence"] for value in review["evidence_ids"]))

    def test_missing_candidate_url_is_rejected(self):
        url = "https://x.com/example/status/9999999999999999999"
        errors = validator.validate_candidate_coverage(self.catalog, [(1, url)])
        self.assertTrue(any("missing from sources" in error for error in errors))
        self.assertTrue(any("missing from candidates" in error for error in errors))

    def test_unknown_rights_cannot_activate_a_legacy_mirror(self):
        catalog = copy.deepcopy(self.catalog)
        item = next(iter(catalog["items"].values()))
        delivery = item["media"][0]["delivery"]
        delivery["mode"] = "authorized_mirror"
        delivery["mirrors"][0]["state"] = "active"
        errors = validator.validate_rights_and_mirrors(catalog)
        self.assertTrue(any("unauthorized active mirror" in error for error in errors))

    def test_retirement_queue_excludes_uploads_awaiting_verification(self):
        self.assertFalse(mirror_needs_cleanup({
            "state": "quarantined", "staged_filename": "authorized-upload.mp4",
        }))
        self.assertTrue(mirror_needs_cleanup({"state": "quarantined"}))
        self.assertTrue(mirror_needs_cleanup({"state": "pending_delete"}))
        catalog = copy.deepcopy(self.catalog)
        item = next(iter(catalog["items"].values()))
        media = item["media"][0]
        media["delivery"]["mirrors"].append({
            "mirror_id": "mir_retirement_test", "provider": "r2", "artifact": "video",
            "url": "https://example.com/retired.mp4", "state": "pending_delete",
        })
        retirement = refresh_retirement_manifest(catalog, {"entries": []}, "2026-08-11T12:00:00Z")
        record = next(value for value in retirement["entries"] if value["mirror_id"] == "mir_retirement_test")
        self.assertNotIn("url", record)
        self.assertEqual(64, len(record["url_sha256"]))
        retired_mirror = media["delivery"]["mirrors"][-1]
        self.assertIsNone(retired_mirror["url"])
        self.assertEqual(record["url_sha256"], retired_mirror["former_url_sha256"])

    def test_evidenced_rights_can_activate_a_complete_mirror(self):
        catalog, item, mirror = self._authorized_r2_catalog()
        self.assertEqual(validator.validate_rights_and_mirrors(catalog), [])
        public = public_catalog(catalog)
        public_item = public["items"][item["id"]]
        public_mirrors = public_item["media"][0]["delivery"]["mirrors"]
        self.assertEqual([value["mirror_id"] for value in public_mirrors], [mirror["mirror_id"]])

    def test_direct_grant_requires_grantor_and_timestamp(self):
        catalog, item, mirror = self._authorized_r2_catalog()
        rights = item["rights"]["video_republication"]
        rights["grantor_actor_ids"] = []
        rights["granted_at"] = None
        self.assertFalse(validator.mirror_is_authorized(item, mirror, catalog))
        errors = validator.validate_canonical(catalog, self.schema, self.config)
        self.assertTrue(any("needs a grantor" in error for error in errors))
        self.assertTrue(any("needs granted_at" in error for error in errors))

    def test_rights_expiry_compares_instants_not_timestamp_strings(self):
        catalog, item, mirror = self._authorized_r2_catalog()
        item["rights"]["video_republication"]["expires_at"] = "2026-08-11T13:00:00+01:00"
        self.assertTrue(validator.mirror_is_authorized(
            item, mirror, catalog, now="2026-08-11T11:30:00Z"
        ))
        self.assertFalse(validator.mirror_is_authorized(
            item, mirror, catalog, now="2026-08-11T12:30:00Z"
        ))

    def test_expiry_sync_retires_media_and_suppresses_prompt_text(self):
        value, item, mirror = self._authorized_r2_catalog()
        evidence_id = mirror["permission_evidence_ids"][0]
        item["rights"]["video_republication"]["expires_at"] = "2026-08-11T12:00:00Z"
        item["rights"]["prompt_republication"] = {
            "status": "granted", "license_spdx": None,
            "granted_scopes": ["reproduce_prompt"],
            "grantor_actor_ids": ["act_repository_opensource-works"],
            "granted_at": "2026-08-11T00:00:00Z",
            "expires_at": "2026-08-11T12:00:00Z",
            "evidence_ids": [evidence_id],
        }
        value["evidence"][evidence_id]["rights_assertion"]["granted_scopes"].append(
            "reproduce_prompt"
        )
        secret = "EXPIRED-PROMPT-MUST-NOT-PUBLISH"
        source = value["sources"][item["canonical_source_id"]]
        source["text"]["value"] = f"prefix {secret} suffix"
        item["prompt"] = {
            "status": "verbatim", "text": secret, "language": "en",
            "source_id": source["id"], "source_url": source["url"],
            "capture_method": "post_text", "is_verbatim": True,
            "evidence_ids": [evidence_id],
        }

        counts = sync_rights_expiry.retire_expired_rights(
            value, "2026-08-11T12:00:01Z"
        )
        self.assertEqual(2, counts["rights"])
        self.assertEqual(1, counts["prompts"])
        self.assertGreaterEqual(counts["mirrors"], 1)
        self.assertEqual("pending_delete", mirror["state"])
        self.assertEqual("revoked", item["rights"]["prompt_republication"]["status"])
        self.assertIsNone(source["text"]["value"])
        self.assertIsNone(next(
            post for post in export_posts(value) if post["item_id"] == item["id"]
        )["prompt"])
        self.assertNotIn(secret, json.dumps(value))
        self.assertNotIn(secret, json.dumps(public_catalog(value)))
        self.assertEqual([], validator.validate_catalog(value))

    def test_active_mirror_requires_integrity_metadata(self):
        catalog, _, mirror = self._authorized_r2_catalog()
        mirror["sha256"] = None
        mirror["bytes"] = None
        mirror["uploaded_at"] = None
        errors = validator.validate_rights_and_mirrors(catalog)
        self.assertTrue(any("positive byte count" in error for error in errors))
        self.assertTrue(any("SHA-256" in error for error in errors))
        self.assertTrue(any("uploaded_at" in error for error in errors))

    def test_public_projection_strips_volatile_and_quarantined_media(self):
        original = copy.deepcopy(self.catalog)
        public = public_catalog(self.catalog)
        self.assertEqual(self.catalog, original, "projection must not mutate canonical data")
        for source in public["sources"].values():
            for observation in source.get("media_observations") or []:
                self.assertIsNone(observation.get("direct_url"))
                self.assertIsNone(observation.get("thumbnail_url"))
                self.assertEqual(observation.get("variants"), [])
        for item in public["items"].values():
            for media in item.get("media") or []:
                self.assertEqual(media["delivery"].get("mirrors"), [])
        self.assertEqual(validator.validate_public_projection(self.catalog), [])

    def test_public_projection_excludes_pending_candidates_and_private_evidence(self):
        catalog = copy.deepcopy(self.catalog)
        item = next(iter(catalog["items"].values()))
        private_id = "ev_private_validation_test"
        catalog["evidence"][private_id] = {
            "id": private_id,
            "kind": "permission",
            "url": "https://example.com/private-attestation",
            "source_id": None,
            "observed_at": "2026-08-11T00:00:00Z",
            "captured_by_actor_id": "act_repository_opensource-works",
            "visibility": "attested_private",
        }
        item["rights"]["prompt_republication"]["evidence_ids"] = [private_id]
        public = public_catalog(catalog)
        self.assertNotIn(private_id, public["evidence"])
        self.assertNotIn(private_id, json.dumps(public))
        self.assertEqual(validator.validate_public_projection(catalog), [])
        self.assertTrue(
            all(
                candidate["review"]["state"] == "included"
                for candidate in public["candidates"].values()
            )
        )

    def test_public_render_views_are_derived_only_from_the_public_graph(self):
        catalog = copy.deepcopy(self.catalog)
        item = next(iter(catalog["items"].values()))
        private_id = "ev_private_editorial_render_test"
        secret = "PRIVATE-EDITORIAL-NOTE-MUST-NOT-RENDER"
        catalog["evidence"][private_id] = {
            "id": private_id,
            "kind": "editorial_note",
            "url": "https://example.com/private-editorial-note",
            "source_id": None,
            "observed_at": "2026-08-11T00:00:00Z",
            "captured_by_actor_id": "act_repository_opensource-works",
            "visibility": "attested_private",
        }
        item.setdefault("annotations", []).append({
            "kind": "editorial_note",
            "source_id": None,
            "author_actor_id": "act_repository_opensource-works",
            "text": secret,
            "created_at": "2026-08-11T00:00:00Z",
            "evidence_ids": [private_id],
        })

        public = public_catalog(catalog)
        public_posts = export_posts(public)
        rendered_posts = builder.enrich_posts(public_posts, public)
        payload = json.dumps({"catalog": public, "posts": rendered_posts})
        self.assertNotIn(private_id, payload)
        self.assertNotIn(secret, payload)

    def test_missing_nested_evidence_is_rejected(self):
        catalog = copy.deepcopy(self.catalog)
        item = next(iter(catalog["items"].values()))
        item["display"]["title"]["evidence_ids"] = ["ev_missing"]
        errors = validator.validate_canonical(catalog, self.schema, self.config)
        self.assertTrue(any("missing evidence ev_missing" in error for error in errors))

    def _authorized_r2_catalog(self):
        catalog = copy.deepcopy(self.catalog)
        item = next(iter(catalog["items"].values()))
        evidence_id = "ev_permission_validation_test"
        catalog["evidence"][evidence_id] = {
            "id": evidence_id,
            "kind": "permission",
            "url": "https://example.com/public-permission",
            "source_id": item["canonical_source_id"],
            "observed_at": "2026-08-11T00:00:00Z",
            "captured_by_actor_id": "act_repository_opensource-works",
            "visibility": "public",
            "rights_assertion": {
                "asset_item_ids": [item["id"]],
                "grantor_actor_ids": ["act_repository_opensource-works"],
                "granted_scopes": ["download", "mirror_r2"],
            },
        }
        item["rights"]["video_republication"] = {
            "status": "granted",
            "license_spdx": None,
            "granted_scopes": ["download", "mirror_r2"],
            "grantor_actor_ids": ["act_repository_opensource-works"],
            "granted_at": "2026-08-11T00:00:00Z",
            "expires_at": None,
            "evidence_ids": [evidence_id],
        }
        delivery = item["media"][0]["delivery"]
        delivery["mode"] = "authorized_mirror"
        mirror = delivery["mirrors"][0]
        mirror.update({
            "provider": "r2",
            "artifact": "video",
            "url": "https://media.example.test/authorized.mp4",
            "state": "active",
            "bytes": 123456,
            "sha256": "a" * 64,
            "uploaded_at": "2026-08-11T00:00:00Z",
            "permission_evidence_ids": [evidence_id],
        })
        return catalog, item, mirror

    def test_generic_or_mismatched_evidence_cannot_authorize_a_mirror(self):
        catalog, item, mirror = self._authorized_r2_catalog()
        evidence_id = mirror["permission_evidence_ids"][0]
        mutations = {
            "generic evidence kind": lambda record: record.update(kind="model_release"),
            "wrong asset": lambda record: record["rights_assertion"].update(asset_item_ids=["itm_other"]),
            "wrong grantor": lambda record: record["rights_assertion"].update(grantor_actor_ids=["act_other"]),
            "missing scope": lambda record: record["rights_assertion"].update(granted_scopes=["download"]),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                changed = copy.deepcopy(catalog)
                changed_item = changed["items"][item["id"]]
                changed_mirror = changed_item["media"][0]["delivery"]["mirrors"][0]
                mutate(changed["evidence"][evidence_id])
                self.assertFalse(validator.mirror_is_authorized(changed_item, changed_mirror, changed))

    def test_public_license_evidence_requires_matching_spdx_and_asset_binding(self):
        value, item, mirror = self._authorized_r2_catalog()
        evidence_id = mirror["permission_evidence_ids"][0]
        rights = item["rights"]["video_republication"]
        rights.update({"status": "public_license", "license_spdx": "CC-BY-4.0"})
        record = value["evidence"][evidence_id]
        record.update({"kind": "public_license"})
        record["rights_assertion"]["license_spdx"] = "CC-BY-4.0"
        self.assertTrue(validator.mirror_is_authorized(item, mirror, value))
        record["rights_assertion"]["license_spdx"] = "CC0-1.0"
        self.assertFalse(validator.mirror_is_authorized(item, mirror, value))

    def test_github_manifest_is_revocation_and_removal_safe(self):
        catalog, item, mirror = self._authorized_r2_catalog()
        evidence_id = mirror["permission_evidence_ids"][0]
        r2_manifest = authorized_manifests.build_r2_manifest(
            catalog, "2026-08-11T00:00:00Z"
        )
        self.assertEqual(1, len(r2_manifest["mirrors"]))
        r2_revoked = copy.deepcopy(catalog)
        r2_revoked["items"][item["id"]]["rights"]["video_republication"]["status"] = "revoked"
        self.assertEqual({}, authorized_manifests.build_r2_manifest(
            r2_revoked, "2026-08-11T00:00:00Z"
        )["mirrors"])

        rights = item["rights"]["video_republication"]
        rights["granted_scopes"] = ["download", "mirror_github"]
        catalog["evidence"][evidence_id]["rights_assertion"]["granted_scopes"] = [
            "download", "mirror_github",
        ]
        mirror.update({
            "mirror_id": authorized_manifests.mirror_id_for(item["id"], item["media"][0]["media_id"]),
            "provider": "github_attachment",
            "url": "https://github.com/user-attachments/assets/12345678-1234-1234-1234-123456789abc",
            "verified_at": "2026-08-11T00:00:00Z",
        })
        manifest = authorized_manifests.build_github_attachment_manifest(
            catalog, "2026-08-11T00:00:00Z"
        )
        self.assertEqual(1, len(manifest["attachments"]))

        revoked = copy.deepcopy(catalog)
        revoked["items"][item["id"]]["rights"]["video_republication"]["status"] = "revoked"
        self.assertTrue(authorized_manifests.validate_github_attachment_manifest(revoked, manifest))
        self.assertEqual({}, authorized_manifests.build_github_attachment_manifest(
            revoked, "2026-08-11T00:00:00Z"
        )["attachments"])

        removed = copy.deepcopy(catalog)
        removed["items"][item["id"]]["curation"]["status"] = "removed"
        self.assertTrue(authorized_manifests.validate_github_attachment_manifest(removed, manifest))
        self.assertEqual({}, authorized_manifests.build_github_attachment_manifest(
            removed, "2026-08-11T00:00:00Z"
        )["attachments"])

        private = copy.deepcopy(catalog)
        private["evidence"][evidence_id]["visibility"] = "attested_private"
        self.assertEqual({}, authorized_manifests.build_github_attachment_manifest(
            private, "2026-08-11T00:00:00Z"
        )["attachments"])
        self.assertEqual({}, authorized_manifests.build_r2_manifest(
            private, "2026-08-11T00:00:00Z"
        )["mirrors"])


if __name__ == "__main__":
    unittest.main()
