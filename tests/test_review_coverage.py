from __future__ import annotations

import copy
import io
import json
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import catalog  # noqa: E402
import report_coverage  # noqa: E402
import review  # noqa: E402


class ReviewAndCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = catalog.read_json(ROOT / "data" / "catalog.json")
        cls.actor = "act_repository_opensource-works"
        cls.evidence = ["ev_editorial_legacy_migration_20260811"]
        cls.at = "2026-08-11T12:00:00Z"

    def pending(self, value: dict, offset: int = 0) -> str:
        return [
            source_id for source_id, candidate in value["candidates"].items()
            if candidate["review"]["state"] == "pending"
        ][offset]

    def test_include_never_claims_creator_prompt_author_or_rights(self):
        value = copy.deepcopy(self.base)
        source_id = self.pending(value)
        item_id = review.include_candidate(
            value,
            source_id,
            reasons=["meets_scope"],
            evidence_ids=self.evidence,
            actor=self.actor,
            at=self.at,
            note="test include",
            requested_item_id=None,
            title="Human-reviewed test title",
        )
        item = value["items"][item_id]
        self.assertEqual("unknown", item["attribution"]["original_video_creators"][0]["status"])
        self.assertEqual("unknown", item["attribution"]["prompt_authors"][0]["status"])
        self.assertEqual("unknown", item["rights"]["video_republication"]["status"])
        self.assertEqual("unknown", item["rights"]["prompt_republication"]["status"])
        self.assertTrue(all(media["delivery"]["mode"] == "source_link" for media in item["media"]))
        self.assertEqual([], catalog.validate_catalog(value))

    def test_exclude_and_remove_leave_a_valid_audit_trail(self):
        value = copy.deepcopy(self.base)
        pending = self.pending(value)
        review.exclude_candidate(
            value,
            pending,
            reasons=["not_target_model"],
            evidence_ids=self.evidence,
            actor=self.actor,
            at=self.at,
            note="test exclusion",
            duplicate_of_item_id=None,
        )
        included = next(
            source_id for source_id, candidate in value["candidates"].items()
            if candidate["review"]["state"] == "included"
        )
        review.remove_candidate(
            value,
            included,
            reasons=["creator_takedown"],
            evidence_ids=self.evidence,
            actor=self.actor,
            at=self.at,
            note="test removal",
        )
        self.assertEqual("excluded", value["candidates"][pending]["review"]["state"])
        self.assertEqual("removed", value["candidates"][included]["review"]["state"])
        self.assertEqual([], catalog.validate_catalog(value))

    def test_sensitive_remove_redacts_long_secret_from_repository_state(self):
        value = copy.deepcopy(self.base)
        source_id = next(
            source_id for source_id, candidate in value["candidates"].items()
            if candidate["review"]["state"] == "included"
        )
        candidate = value["candidates"][source_id]
        item = value["items"][candidate["review"]["item_id"]]
        source = value["sources"][item["canonical_source_id"]]
        secret = "S" * 3109
        self.assertEqual(3109, len(secret))
        source["text"] = {"status": "available", "value": secret, "language": "en"}
        item["display"]["title"]["text"] = secret
        item["prompt"] = {
            "status": "verbatim", "text": secret, "language": "en",
            "source_id": source["id"], "source_url": source["url"],
            "capture_method": "post_text", "is_verbatim": True,
            "evidence_ids": self.evidence,
        }
        item["annotations"] = [{
            "kind": "editorial_note", "source_id": None,
            "author_actor_id": self.actor, "text": secret,
            "created_at": self.at, "evidence_ids": self.evidence,
        }]
        item["attribution"]["prompt_authors"][0]["note"] = secret
        candidate["review"]["note"] = secret
        candidate["review"].setdefault("history", []).append({"note": secret})
        value["evidence"][self.evidence[0]]["excerpt"] = secret

        review.remove_candidate(
            value, source_id, reasons=["creator_takedown"],
            evidence_ids=self.evidence, actor=self.actor, at=self.at, note=secret,
        )

        self.assertNotIn(secret, json.dumps(value))
        self.assertIsNone(source["text"]["value"])
        self.assertEqual("removed", item["prompt"]["status"])
        self.assertIsNone(item["prompt"]["text"])
        self.assertEqual([], item["annotations"])
        self.assertEqual("revoked", item["rights"]["video_republication"]["status"])
        self.assertEqual(self.evidence, item["curation"]["evidence_ids"])
        self.assertEqual([], catalog.validate_catalog(value))

    def test_mutations_require_reason_evidence_actor_and_time(self):
        parser = review.build_parser()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["include", self.pending(self.base)])

    def test_coverage_contains_every_required_dimension(self):
        result = report_coverage.build_report(self.base, generated_at=self.at)
        for key in (
            "queries", "candidates", "platforms", "status", "prompts",
            "attribution", "rights", "quarantine", "exclusions", "discovery_windows",
        ):
            self.assertIn(key, result)
        self.assertIn("reddit", result["platforms"]["sources"])
        reddit_sources = sum(
            source.get("platform") == "reddit"
            for source in self.base["sources"].values()
        )
        self.assertGreater(reddit_sources, 0)
        self.assertEqual(reddit_sources, result["platforms"]["sources"]["reddit"])
        markdown = report_coverage.render_markdown(result)
        self.assertIn("## Queries", markdown)
        self.assertIn("## Discovery windows", markdown)
        self.assertIn("## Exclusions and removals", markdown)


if __name__ == "__main__":
    unittest.main()
