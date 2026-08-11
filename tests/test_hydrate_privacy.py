from __future__ import annotations

import copy
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import catalog  # noqa: E402
import hydrate  # noqa: E402
import prepare_uploads  # noqa: E402
import review  # noqa: E402


class HydrationPrivacyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = catalog.read_json(ROOT / "data" / "catalog.json")
        cls.at = "2026-08-11T12:00:00Z"
        cls.reviewer = "act_repository_opensource-works"
        cls.review_evidence = ["ev_editorial_legacy_migration_20260811"]

    def pending(self, value):
        return next(key for key, row in value["candidates"].items()
                    if row["review"]["state"] == "pending")

    def test_prompt_is_private_until_explicit_human_acceptance(self):
        value = copy.deepcopy(self.base)
        hydrate.ensure_automation(value, self.at)
        source_id = self.pending(value)
        sentinel = "PRIVATE-PROMPT-MINIMAX-90817"
        raw = "Prompt: " + " ".join([sentinel] * 8)
        source = value["sources"][source_id]
        source["text"] = {"status": "available", "value": raw, "language": "en"}
        source["media_observations"] = [{
            "source_media_id": "x-media:privacy", "kind": "video", "position": 0,
            "direct_url": "https://video.twimg.com/ext_tw_video/privacy.mp4",
            "thumbnail_url": "https://pbs.twimg.com/ext_tw_video_thumb/privacy.jpg",
            "variants": [], "observed_at": self.at,
        }]
        cache = hydrate.initialize_volatile_cache(value, self.at)
        hydrate.scrub_volatile_catalog(value, cache, self.at)
        hydrate.maybe_capture_root_prompt(
            value, source_id, self.at, text=raw, volatile_cache=cache,
        )
        observation = value["candidates"][source_id]["prompt_observation"]
        self.assertNotIn("text", observation)
        self.assertIsNone(source["text"]["value"])
        self.assertIsNone(source["media_observations"][0]["direct_url"])
        self.assertNotIn(sentinel, json.dumps(value, ensure_ascii=False))
        with self.assertRaises(review.ReviewError):
            review.include_candidate(
                value, source_id, reasons=["meets_scope"],
                evidence_ids=self.review_evidence, actor=self.reviewer, at=self.at,
                note="must decide", requested_item_id=None, title="Reviewed title",
            )
        item_id = review.include_candidate(
            value, source_id, reasons=["meets_scope"],
            evidence_ids=self.review_evidence, actor=self.reviewer, at=self.at,
            note="accept", requested_item_id=None, prompt_decision="accept",
            prompt_payload=cache["prompts"][observation["cache_key"]],
            title="Reviewed title",
        )
        item = value["items"][item_id]
        self.assertIn(sentinel, item["prompt"]["text"])
        self.assertEqual("unknown", item["attribution"]["prompt_authors"][0]["status"])
        self.assertEqual("unknown", item["rights"]["prompt_republication"]["status"])

    def test_comment_raw_text_is_never_canonical(self):
        value = copy.deepcopy(self.base)
        hydrate.ensure_automation(value, self.at)
        root_id = self.pending(value)
        root = value["sources"][root_id]
        actor = copy.deepcopy(value["actors"][root["posted_by_actor_id"]])
        text = "Prompt: " + "private-comment " * 12
        cache = hydrate.initialize_volatile_cache(value, self.at)
        comment_id = hydrate.store_comment(
            value, platform=root["platform"], native_id="private-comment",
            url=("https://x.com/example/status/private-comment" if root["platform"] == "x"
                 else "https://www.reddit.com/comments/example/_/private-comment/"),
            parent_source_id=root_id, text=text, actor=actor, at=self.at,
            posted_at=self.at, volatile_cache=cache,
        )
        hydrate.maybe_capture_comment_prompt(
            value, root_id, comment_id, text=text, at=self.at, volatile_cache=cache,
        )
        self.assertIsNone(value["sources"][comment_id]["text"]["value"])
        self.assertNotIn("text", value["candidates"][root_id]["prompt_observation"])

    def test_fail_closed_and_owner_only_cache_without_parent_chmod(self):
        value = copy.deepcopy(self.base)
        value["sources"][self.pending(value)]["text"] = {
            "status": "available", "value": "private", "language": None,
        }
        with self.assertRaises(ValueError):
            hydrate.scrub_volatile_catalog(value, None, self.at)
        cache = hydrate.initialize_volatile_cache(value, self.at)
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "shared"
            parent.mkdir()
            parent.chmod(0o755)
            path = parent / "cache.json"
            hydrate.write_volatile_cache(path, cache)
            self.assertEqual(0o755, stat.S_IMODE(parent.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            cache["prompts"]["decided"] = {"text": "private"}
            hydrate.write_volatile_cache(path, cache)
            review.consume_prompt_payload(value, path, "decided")
            self.assertNotIn("decided", catalog.read_json(path)["prompts"])

    def test_weekly_workflow_cache_and_comments_contract(self):
        workflow = (ROOT / ".github" / "workflows" / "refresh.yml").read_text()
        self.assertGreaterEqual(workflow.count("--volatile-cache .cache/media-locators.json"), 3)
        self.assertGreaterEqual(workflow.count("--with-comments"), 2)

    def test_upload_cache_rejects_non_platform_media_hosts(self):
        with self.assertRaises(ValueError):
            prepare_uploads.overlay_volatile_observation(
                {"id": "reddit:t3_1", "platform": "reddit"},
                {"source_media_id": "reddit-media:1:0", "direct_url": None, "variants": []},
                {"reddit:t3_1/reddit-media:1:0": {
                    "source_id": "reddit:t3_1", "source_media_id": "reddit-media:1:0",
                    "direct_url": "https://example.com/private.mp4", "variants": [],
                }},
            )

    def test_pending_only_selection_also_refreshes_rights_cleared_mirror_sources(self):
        value = copy.deepcopy(self.base)
        item = next(iter(value["items"].values()))
        source_id = item["canonical_source_id"]
        item["curation"]["status"] = "approved"
        item["rights"]["video_republication"].update({
            "status": "granted", "granted_scopes": ["download", "mirror_r2"],
        })
        for media in item.get("media") or []:
            media["source_id"] = source_id
            media["delivery"]["mirrors"] = []
        self.assertTrue(hydrate.source_needs_mirror_locator(value, source_id))

    def test_reject_remains_possible_without_ephemeral_cache(self):
        value = copy.deepcopy(self.base)
        hydrate.ensure_automation(value, self.at)
        source_id = self.pending(value)
        cache = hydrate.initialize_volatile_cache(value, self.at)
        hydrate.maybe_capture_root_prompt(
            value, source_id, self.at, text="Prompt: " + "reject-me " * 15,
            volatile_cache=cache,
        )
        item_id = review.include_candidate(
            value, source_id, reasons=["meets_scope"],
            evidence_ids=self.review_evidence, actor=self.reviewer, at=self.at,
            note="reject", requested_item_id=None, prompt_decision="reject",
            prompt_payload=None, title="Reviewed title",
        )
        self.assertNotIn("prompt_observation", value["candidates"][source_id])
        self.assertEqual("unavailable", value["items"][item_id]["prompt"]["status"])


if __name__ == "__main__":
    unittest.main()
