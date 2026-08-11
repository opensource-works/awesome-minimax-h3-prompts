#!/usr/bin/env python3
"""Import the retained 2026-08-11 historical research inventory as candidates.

Research confidence and creator-language notes are leads, not attribution
claims. Every non-excluded URL remains pending until official API hydration and
human review. Known removed/reference-only pages receive auditable decisions.
"""
from __future__ import annotations

import argparse
import copy
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from catalog import export_posts, read_json, source_identity, validate_catalog, write_json  # noqa: E402
from discover import new_candidate, person, source_stub  # noqa: E402

AT = "2026-08-11T00:00:00Z"
CURATOR = "act_repository_opensource-works"


def selected_groups(collection: str) -> tuple[list[str], str]:
    if collection == "awesome-seedance-prompts":
        return ["seedance_x_missing", "seedance_reddit_candidates"], "Seedance"
    return ["minimax_h3_x_missing", "minimax_h3_reddit_candidates"], "MiniMax H3"


def rows_for_import(inventory: dict, groups: list[str], model_label: str) -> tuple[dict[str, list[dict]], list[dict]]:
    """Accept both the original research bundle and the committed filtered artifact."""
    if isinstance(inventory.get("groups"), dict):
        selected = {group: list(inventory["groups"].get(group, [])) for group in groups}
        excluded = [
            row for row in inventory["groups"].get("excluded_removed_or_reference_only", [])
            if (row.get("model") or "").startswith(model_label)
        ]
        return selected, excluded
    selected = {group: [] for group in groups}
    excluded = []
    for row in inventory.get("records") or []:
        if row.get("reason"):
            excluded.append(row)
            continue
        platform = source_identity(row["url"])[0]
        group = next(value for value in groups if ("_x_" in value) == (platform == "x"))
        selected[group].append(row)
    return selected, excluded


def source_and_actor(url: str):
    platform, native, source_id = source_identity(url)
    handle = None
    if platform == "x":
        match = re.search(r"x\.com/([^/]+)/status/", url, re.I)
        handle = match.group(1) if match and match.group(1) not in {"i", "web"} else None
    actor = person(
        platform, handle, AT,
        profile_url=f"https://x.com/{handle}" if platform == "x" and handle else None,
    )
    source = source_stub(
        source_id, platform, native, url, actor["id"], AT,
        adapter="historical-web-research", available="unknown",
    )
    return platform, source_id, source, actor


def research_evidence(catalog: dict, source_id: str, row: dict, *, decision=False) -> str:
    key = ("ev_backfill_decision_" if decision else "ev_backfill_research_") + source_id.replace(":", "_")
    pieces = []
    for field in ("creator_evidence", "confidence", "prompt", "rights_risk", "reason"):
        if row.get(field):
            pieces.append(f"{field}={row[field]}")
    catalog["evidence"][key] = {
        "id": key,
        "kind": "editorial_review" if decision else "discovery_research",
        "url": row["url"], "source_id": source_id, "observed_at": AT,
        "excerpt": "; ".join(pieces) or None,
        "captured_by_actor_id": CURATOR, "visibility": "public",
        "integrity_sha256": None,
    }
    return key


def add_discovery(catalog: dict, candidate: dict, platform: str, source_id: str,
                  query_id: str, rank: int) -> None:
    run_id = f"run_historical_backfill_{platform}_20260811"
    record = {
        "run_id": run_id, "query_id": query_id, "found_at": AT,
        "rank": rank, "submitted_by_actor_id": None,
    }
    if not any(d.get("run_id") == run_id and d.get("query_id") == query_id
               for d in candidate["discoveries"]):
        candidate["discoveries"].append(record)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("inventory", type=Path)
    args = parser.parse_args()
    inventory = read_json(args.inventory)
    config = read_json(ROOT / "config" / "collection.json")
    if inventory.get("collection") and inventory["collection"] != config["id"]:
        raise SystemExit("backfill inventory belongs to a different collection")
    catalog = read_json(ROOT / "data" / "catalog.json")
    groups, model_label = selected_groups(config["id"])
    grouped_rows, excluded_rows = rows_for_import(inventory, groups, model_label)
    retained = []
    counts = {"pending": 0, "excluded": 0, "removed": 0}
    platform_counts = {"x": 0, "reddit": 0}

    for group in groups:
        for rank, row in enumerate(grouped_rows[group], 1):
            platform, source_id, source, actor = source_and_actor(row["url"])
            catalog["actors"][actor["id"]] = actor
            catalog["sources"].setdefault(source_id, source)
            candidate = catalog["candidates"].setdefault(source_id, new_candidate(source_id))
            add_discovery(catalog, candidate, platform, source_id, f"backfill.{group}", rank)
            ev = research_evidence(catalog, source_id, row)
            candidate["research"] = {
                "inventory": "data/backfill-2026-08-11.json",
                "model": row.get("model"), "prompt_location": row.get("prompt"),
                "prompt_evidence_url": row.get("prompt_evidence_url"),
                "creator_evidence_lead": row.get("creator_evidence"),
                "confidence": row.get("confidence"), "rights_risk": row.get("rights_risk"),
                "dedupe_group": row.get("dedupe_group"), "evidence_ids": [ev],
                "note": "Research lead only; does not establish creator identity or republication rights.",
            }
            candidate["checks"].update({
                "target_model": "pass", "has_video": "pass" if row.get("video") else "unknown",
                "original_source": "unknown", "role_separation": "pass",
                "delivery_policy": "pass",
            })
            retained.append(copy.deepcopy(row))
            counts["pending"] += 1
            platform_counts[platform] += 1

    for rank, row in enumerate(excluded_rows, 1):
        platform, source_id, source, actor = source_and_actor(row["url"])
        is_removed = row.get("reason") == "source_removed"
        if is_removed:
            source["availability"].update({
                "state": "deleted", "first_unavailable_at": AT,
                "confirmed_at": AT, "consecutive_failures": 1,
                "consecutive_successful_missing": 0, "http_status": 404,
            })
        catalog["actors"][actor["id"]] = actor
        catalog["sources"].setdefault(source_id, source)
        candidate = catalog["candidates"].setdefault(source_id, new_candidate(source_id))
        add_discovery(
            catalog, candidate, platform, source_id,
            "backfill.removed" if is_removed else "backfill.reference-only", rank,
        )
        ev = research_evidence(catalog, source_id, row, decision=True)
        state = "removed" if is_removed else "excluded"
        reason_code = "source_deleted" if is_removed else "no_video"
        candidate["review"].update({
            "state": state, "reason_codes": [reason_code],
            "note": row.get("reason"), "item_id": None,
            "duplicate_of_item_id": None, "reviewer_actor_id": CURATOR,
            "reviewed_at": AT, "evidence_ids": [ev],
            "history": [{
                "state": state, "at": AT, "actor_id": CURATOR,
                "reason_codes": [reason_code], "evidence_ids": [ev],
            }],
        })
        candidate["checks"].update({
            "public_visibility": "fail" if is_removed else "unknown",
            "target_model": "pass", "has_video": "fail",
            "original_source": "unknown", "role_separation": "pass",
            "delivery_policy": "pass",
        })
        retained.append(copy.deepcopy(row))
        counts[state] += 1
        platform_counts[platform] += 1

    for platform in ("x", "reddit"):
        query_ids = sorted({
            discovery["query_id"]
            for candidate in catalog["candidates"].values()
            for discovery in candidate.get("discoveries") or []
            if discovery.get("run_id") == f"run_historical_backfill_{platform}_20260811"
        })
        catalog["discovery_runs"][f"run_historical_backfill_{platform}_20260811"] = {
            "id": f"run_historical_backfill_{platform}_20260811",
            "started_at": AT, "ended_at": AT, "platform": platform,
            "query_matrix_version": "historical-research-2026-08-11",
            "query_ids": query_ids, "status": "partial",
            "result_count": platform_counts[platform],
            "log_url": "data/backfill-2026-08-11.json",
            "limitations": [
                inventory["coverage"].get("x_search_result_note") if platform == "x"
                else inventory["coverage"].get("reddit_note")
            ],
        }
    catalog["updated_at"] = AT
    errors = validate_catalog(catalog)
    if errors:
        raise SystemExit("backfill import produced invalid catalog:\n" + "\n".join(f"- {e}" for e in errors))

    filtered_inventory = {
        "schema_version": inventory["schema_version"], "generated_at": inventory["generated_at"],
        "collection": config["id"], "scope": inventory["scope"],
        "coverage": inventory["coverage"], "release_sources": inventory["release_sources"],
        "query_matrix": inventory["query_matrix"],
        "counts": {
            "retained_urls": len(retained), **counts,
            "platform_x": platform_counts["x"], "platform_reddit": platform_counts["reddit"],
        },
        "dedupe_groups": [
            group for group in inventory.get("dedupe_groups", [])
            if any(member in {row["url"] for row in retained} for member in group["member_urls"])
        ],
        "records": retained,
    }
    write_json(ROOT / "data" / "catalog.json", catalog)
    write_json(ROOT / "data" / "posts.json", export_posts(catalog))
    write_json(ROOT / "data" / "backfill-2026-08-11.json", filtered_inventory)
    print(
        f"imported {counts['pending']} pending, {counts['excluded']} excluded and "
        f"{counts['removed']} removed historical URLs"
    )


if __name__ == "__main__":
    main()
