from __future__ import annotations

import os
import sys
import unittest
import urllib.parse
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import catalog  # noqa: E402
import discover  # noqa: E402


class DiscoveryWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matrix = catalog.read_json(ROOT / "config" / "query-matrix.json")
        cls.config = catalog.read_json(ROOT / "config" / "collection.json")

    def test_fixed_historical_and_separate_ongoing_boundaries(self):
        historical = discover.discovery_window(
            self.matrix, "historical", "2026-08-20T00:00:00Z",
        )
        self.assertEqual("2026-07-29T00:00:00Z", historical["from"])
        self.assertEqual("2026-08-11T12:30:00Z", historical["through_exclusive"])
        self.assertTrue(discover.in_discovery_window("2026-08-11T12:29:59Z", historical))
        self.assertFalse(discover.in_discovery_window("2026-08-11T12:30:00Z", historical))
        ongoing = discover.discovery_window(
            self.matrix, "ongoing", "2026-08-18T12:30:00Z",
        )
        self.assertEqual("2026-08-11T12:30:00Z", ongoing["from"])
        self.assertEqual("2026-08-18T12:29:45Z", ongoing["through_exclusive"])
        self.assertEqual("2026-08-18T12:30:00Z", ongoing["requested_through_exclusive"])

    def test_x_api_and_client_filter_share_the_exact_bounds(self):
        value = {"actors": {}, "sources": {}, "candidates": {}}
        window = discover.discovery_window(self.matrix, "historical", "2026-08-20T00:00:00Z")
        payload = {
            "data": [
                {"id": "2001", "text": "MiniMax H3", "created_at": "2026-08-11T12:29:59Z",
                 "author_id": "u1"},
                {"id": "2002", "text": "MiniMax H3", "created_at": "2026-08-11T12:30:00Z",
                 "author_id": "u1"},
            ],
            "includes": {"users": [{"id": "u1", "username": "tester"}]}, "meta": {},
        }
        seen = []

        def request(url, **_kwargs):
            seen.append(url)
            return payload, 200

        with mock.patch.dict(os.environ, {"X_BEARER_TOKEN": "test"}), mock.patch.object(
            discover, "request_json", side_effect=request
        ):
            count, errors, stats = discover.discover_x(
                value, [{"id": "q", "query": "MiniMax H3"}], self.matrix, self.config,
                window=window, max_pages=1, run_id="run", observed_at="2026-08-20T00:00:00Z",
            )
        params = urllib.parse.parse_qs(urllib.parse.urlparse(seen[0]).query)
        self.assertEqual([window["from"]], params["start_time"])
        self.assertEqual([window["through_exclusive"]], params["end_time"])
        self.assertEqual((1, [], 1), (count, errors, stats["filtered_outside_window"]))

    def test_reddit_client_side_filter(self):
        value = {"actors": {}, "sources": {}, "candidates": {}}
        window = discover.discovery_window(self.matrix, "ongoing", "2026-08-12T12:30:00Z")
        epoch = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        payload = {"data": {"children": [
            {"data": {"id": "old", "name": "t3_old", "permalink": "/comments/old",
                      "author": "tester", "created_utc": epoch("2026-08-11T12:29:59Z"),
                      "title": "MiniMax H3", "is_video": True}},
            {"data": {"id": "new", "name": "t3_new", "permalink": "/comments/new",
                      "author": "tester", "created_utc": epoch("2026-08-11T12:30:00Z"),
                      "title": "MiniMax H3", "is_video": True}},
        ], "after": None}}
        with mock.patch.object(discover, "reddit_token", return_value="token"), mock.patch.object(
            discover, "request_json", return_value=(payload, 200)
        ):
            count, errors, stats = discover.discover_reddit(
                value, [{"id": "q", "query": "MiniMax H3"}], self.matrix, self.config,
                window=window, max_pages=1, run_id="run", observed_at="2026-08-12T12:30:00Z",
            )
        self.assertEqual((1, [], 1), (count, errors, stats["filtered_outside_window"]))


if __name__ == "__main__":
    unittest.main()
