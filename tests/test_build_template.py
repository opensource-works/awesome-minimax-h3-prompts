from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build as builder  # noqa: E402
from catalog import export_posts, public_catalog  # noqa: E402


class ReadmeLegacyTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads((ROOT / "data/catalog.json").read_text())
        cls.config = json.loads((ROOT / "config/collection.json").read_text())

    def fixture(self, *, prompt_status="verbatim", evidence_visibility="public",
                maintainer_attested=False):
        catalog = copy.deepcopy(self.catalog)
        item = next(iter(catalog["items"].values()))
        source = catalog["sources"][item["canonical_source_id"]]
        grantor = "act_repository_opensource-works"
        evidence_id = "ev_readme_template_permission"
        attachment = (
            "https://github.com/user-attachments/assets/"
            "12345678-1234-1234-1234-123456789abc"
        )
        catalog["evidence"][evidence_id] = {
            "id": evidence_id,
            "kind": "permission",
            "url": "https://example.com/public-permission",
            "source_id": source["id"],
            "observed_at": "2026-08-11T00:00:00Z",
            "captured_by_actor_id": grantor,
            "visibility": evidence_visibility,
            "rights_assertion": {
                "asset_item_ids": [item["id"]],
                "grantor_actor_ids": [grantor],
                "granted_scopes": ["download", "mirror_github"],
            },
        }
        evidence_ids = [] if maintainer_attested else [evidence_id]
        item["rights"]["video_republication"] = {
            "status": "granted",
            "license_spdx": None,
            "granted_scopes": ["download", "mirror_github"],
            "grantor_actor_ids": [grantor],
            "granted_at": "2026-08-11T00:00:00Z",
            "expires_at": None,
            "evidence_ids": evidence_ids,
            "grant_verification": "maintainer_attestation" if maintainer_attested else "public_evidence",
        }
        media = item["media"][0]
        media["delivery"]["mode"] = "authorized_mirror"
        media["delivery"]["mirrors"] = [{
            "mirror_id": "mir_readme_template_github",
            "provider": "github_attachment",
            "artifact": "video",
            "url": attachment,
            "bytes": 1234,
            "sha256": "a" * 64,
            "width": 1280,
            "height": 720,
            "uploaded_at": "2026-08-11T00:00:00Z",
            "verified_at": "2026-08-11T00:00:00Z",
            "last_checked_at": "2026-08-11T00:00:00Z",
            "state": "active",
            "permission_evidence_ids": evidence_ids,
        }]

        prompt_text = "EXACT FULL PROMPT FROM THE SOURCE"
        item["prompt"] = {
            "status": prompt_status,
            "text": None if prompt_status == "referenced_not_captured" else prompt_text,
            "language": "en",
            "source_id": source["id"],
            "source_url": source["url"],
            "capture_method": "none" if prompt_status == "referenced_not_captured" else "post_text",
            "is_verbatim": prompt_status == "verbatim",
            "evidence_ids": [evidence_id],
        }

        public = public_catalog(catalog)
        post = next(
            value for value in export_posts(public)
            if value["item_id"] == item["id"]
        )
        post = builder.enrich_posts([post], public)[0]
        return catalog, post, attachment, prompt_text

    def test_authorized_attachment_uses_legacy_readme_shape(self):
        catalog, post, attachment, prompt_text = self.fixture()
        rendered = builder.readme(catalog, [post], self.config)

        self.assertIn(f"#### [{post['title']}]({post['url']})", rendered)
        self.assertIn(f"\n{attachment}\n", rendered)
        self.assertIn("- **Video credit / source:**", rendered)
        self.assertIn("source account; original creator not inferred", rendered)
        self.assertIn("- **Prompt credit / source:**", rendered)
        self.assertIn(f"- **Original post:** [X]({post['url']})", rendered)
        self.assertIn("<details><summary><b>Prompt</b></summary>", rendered)
        self.assertIn(prompt_text, rendered)

        rendered_zh = builder.readme(catalog, [post], self.config, zh=True)
        self.assertIn(attachment, rendered_zh)
        self.assertIn("**视频署名 / 来源（Video credit）：**", rendered_zh)
        self.assertIn("**提示词署名 / 来源（Prompt credit）：**", rendered_zh)
        self.assertIn("**原帖（Original post）：**", rendered_zh)

    def test_incomplete_prompt_is_not_reproduced_or_inferred(self):
        catalog, post, _attachment, prompt_text = self.fixture(prompt_status="partial")
        self.assertEqual(prompt_text, post["prompt"], "fixture proves partial text reached the view")

        rendered = builder.readme(catalog, [post], self.config)
        self.assertNotIn(prompt_text, rendered)
        self.assertNotIn("<details><summary><b>Prompt</b></summary>", rendered)
        self.assertIn(
            "The full prompt was not captured, so no prompt text is reproduced or inferred.",
            rendered,
        )

    def test_private_or_missing_permission_record_cannot_enable_playback(self):
        catalog, post, attachment, _prompt_text = self.fixture(
            evidence_visibility="attested_private"
        )
        self.assertIsNone((post.get("video") or {}).get("attachment"))

        # Even a caller-injected legacy URL must not bypass the template gate.
        post["video"]["attachment"] = attachment
        post["video"]["media_mode"] = "authorized_mirror"
        rendered = builder.readme(catalog, [post], self.config)
        self.assertNotIn(attachment, rendered)
        self.assertIn("Source link only", rendered)

    def test_maintainer_attestation_enables_playback_without_public_proof(self):
        catalog, post, attachment, _prompt_text = self.fixture(maintainer_attested=True)
        rendered = builder.readme(catalog, [post], self.config)
        self.assertIn(f"\n{attachment}\n", rendered)

    def test_missing_integrity_metadata_cannot_enable_playback(self):
        catalog, post, attachment, _prompt_text = self.fixture(maintainer_attested=True)
        catalog["items"][post["item_id"]]["media"][0]["delivery"]["mirrors"][0]["sha256"] = None
        rendered = builder.readme(catalog, [post], self.config)
        self.assertNotIn(attachment, rendered)
        self.assertIn("Source link only", rendered)


if __name__ == "__main__":
    unittest.main()
