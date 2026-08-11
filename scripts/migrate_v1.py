#!/usr/bin/env python3
"""One-time migration from the legacy post array to catalog v2.

The migration is deliberately conservative: legacy author becomes only the
source poster; creator and prompt-author claims stay unknown. Existing R2 and
GitHub copies are quarantined because no permission evidence was recorded.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from catalog import (  # noqa: E402
    SCHEMA_VERSION, actor_id, evidence_id, export_posts, item_id, read_json,
    source_identity, validate_catalog, write_json,
)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def curator_actor(config: dict) -> dict:
    return {
        "id": "act_repository_opensource-works",
        "kind": "organization",
        "platform": "repository",
        "platform_user_id": None,
        "handle": "opensource-works",
        "display_name": "opensource-works maintainers",
        "profile_url": "https://github.com/opensource-works",
        "avatar_url": None,
        "aliases": [],
        "observed_at": config["migration_reviewed_at"],
    }


def unknown_actor(platform: str, observed_at: str) -> dict:
    key = actor_id(platform)
    return {
        "id": key, "kind": "unknown", "platform": platform,
        "platform_user_id": None, "handle": None,
        "display_name": "Unknown (not hydrated)", "profile_url": None,
        "avatar_url": None, "aliases": [], "observed_at": observed_at,
    }


def legacy_actor(post: dict, platform: str, observed_at: str) -> dict:
    old = post.get("author") or {}
    key = actor_id(platform, handle=old.get("handle"))
    return {
        "id": key, "kind": "person", "platform": platform,
        "platform_user_id": None, "handle": old.get("handle"),
        "display_name": old.get("name"), "profile_url": old.get("url"),
        "avatar_url": old.get("avatar"), "aliases": [], "observed_at": observed_at,
    }


def media_platform_id(url: str | None) -> str | None:
    match = re.search(r"/(?:amplify_video|ext_tw_video|tweet_video)/(\d+)/", url or "")
    return match.group(1) if match else None


def format_observation(record: dict) -> dict:
    url = record.get("url")
    dimensions = re.search(r"/(\d+)x(\d+)/", url or "")
    return {
        "url": url,
        "width": int(dimensions.group(1)) if dimensions else None,
        "height": int(dimensions.group(2)) if dimensions else None,
        "bitrate": record.get("bitrate"), "container": record.get("container"),
        "codec": None,
    }


def rights_record() -> dict:
    return {
        "status": "unknown", "license_spdx": None, "granted_scopes": [],
        "grantor_actor_ids": [], "granted_at": None, "expires_at": None,
        "evidence_ids": [],
    }


def role_unknown() -> dict:
    return {"actor_id": None, "status": "unknown", "evidence_ids": [], "note": None}


def evidence_record(key: str, kind: str, source_id: str | None, url: str, observed_at: str,
                    curator_id: str, excerpt=None) -> dict:
    return {
        "id": key, "kind": kind, "url": url, "source_id": source_id,
        "observed_at": observed_at, "excerpt": excerpt,
        "captured_by_actor_id": curator_id, "visibility": "public",
        "integrity_sha256": None,
    }


def mirror_records(item_key: str, mirror: dict | None,
                   attachment: str | None) -> tuple[list[dict], list[dict]]:
    records, retirement = [], []

    def add(provider: str, artifact: str, url: str):
        number = len(records)
        key = f"mir_legacy_{provider}_{item_key}_{number}"
        records.append({
            "mirror_id": key, "provider": provider, "artifact": artifact,
            "url": url, "bytes": (mirror or {}).get("bytes") if artifact == "video" else None,
            "sha256": None, "width": (mirror or {}).get("width"),
            "height": (mirror or {}).get("height"), "uploaded_at": None,
            "state": "quarantined", "permission_evidence_ids": [],
            "last_checked_at": None,
        })
        retirement.append({
            "mirror_id": key, "provider": provider, "artifact": artifact,
            "url_sha256": sha256_text(url), "state": "pending_delete",
            "note": "Removed from public manifests; remote cleanup still requires confirmation.",
        })

    if mirror and mirror.get("mp4"):
        add("r2", "video", mirror["mp4"])
    if mirror and mirror.get("webp"):
        add("r2", "animated_preview", mirror["webp"])
    if attachment:
        add("github_attachment", "video", attachment)
    return records, retirement


def model_record(label: str, source_evidence: str) -> dict:
    if label.lower().startswith("seedance"):
        suffix = label[len("Seedance"):].strip()
        return {"family": "Seedance", "version": suffix or None,
                "verification": "inferred", "evidence_ids": [source_evidence]}
    return {"family": "MiniMax", "version": "H3",
            "verification": "inferred", "evidence_ids": [source_evidence]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="replace an existing v2 catalog")
    args = parser.parse_args()
    catalog_path = ROOT / "data" / "catalog.json"
    if catalog_path.exists() and not args.force:
        raise SystemExit("data/catalog.json already exists; pass --force to rebuild it from v1")

    config = read_json(ROOT / "config" / "collection.json")
    posts = read_json(ROOT / "data" / "posts.json", [])
    old_mirror = read_json(ROOT / "data" / "mirror.json", {})
    old_attach = read_json(ROOT / "data" / "attachments.json", {})
    overrides = read_json(ROOT / "scripts" / "overrides.json", {})
    observed_at = config["legacy_observed_at"]
    reviewed_at = config["migration_reviewed_at"]
    curator = curator_actor(config)
    audit_evidence = "ev_editorial_legacy_migration_20260811"

    catalog = {
        "$schema": "../schema/catalog-v2.schema.json",
        "schema_version": SCHEMA_VERSION,
        "updated_at": reviewed_at,
        "collection": {
            "id": config["id"], "repo_url": config["repo_url"],
            "model_scope": config["model_scope"],
            "historical_window": config["historical_window"],
            "query_matrix_version": config["query_matrix_version"],
        },
        "discovery_runs": {
            "run_legacy_urls_import_20260811": {
                "id": "run_legacy_urls_import_20260811",
                "started_at": reviewed_at, "ended_at": reviewed_at,
                "platform": "x", "query_matrix_version": "legacy-urls.txt",
                "query_ids": ["legacy.urls.txt"], "status": "complete",
                "result_count": 0, "log_url": None,
            }
        },
        "actors": {curator["id"]: curator},
        "sources": {},
        "evidence": {
            audit_evidence: evidence_record(
                audit_evidence, "editorial_review", None,
                config["repo_url"] + "/blob/main/data/catalog.json",
                reviewed_at, curator["id"],
                "Legacy entries approved for source-link indexing only; creator and rights remain unverified.",
            )
        },
        "items": {},
        "candidates": {},
    }
    for release in config.get("release_evidence", []):
        catalog["evidence"][release["id"]] = evidence_record(
            release["id"], "model_release", release.get("source_id"), release["url"],
            release["observed_at"], curator["id"], release.get("excerpt"),
        )
    retirement = {
        "schema_version": "1.0.0", "generated_at": reviewed_at,
        "policy": "All legacy mirrors lacked permission evidence and were removed from public manifests.",
        "entries": [],
    }
    included_sources = set()

    for post in posts:
        platform, native, source_id = source_identity(post["url"])
        included_sources.add(source_id)
        actor = legacy_actor(post, platform, observed_at)
        catalog["actors"][actor["id"]] = actor
        source_ev = evidence_id("source", source_id)
        catalog["evidence"][source_ev] = evidence_record(
            source_ev, "source_post", source_id, post["url"], observed_at, curator["id"]
        )
        video = post.get("video") or {}
        source_media_id = f"{platform}-media:{native}:0"
        catalog["sources"][source_id] = {
            "id": source_id, "platform": platform, "native_id": native, "kind": "post",
            "url": post["url"], "parent_source_id": None, "posted_at": None,
            "posted_date": post.get("date"), "posted_by_actor_id": actor["id"],
            "text": {"status": "available", "value": post.get("text") or "", "language": None},
            "metrics": {
                "observed_at": observed_at,
                "views": (post.get("stats") or {}).get("views"),
                "likes": (post.get("stats") or {}).get("likes"),
                "reposts": (post.get("stats") or {}).get("retweets"),
                "comments": None,
            },
            "media_observations": [{
                "source_media_id": source_media_id, "kind": "video", "position": 0,
                "platform_media_id": media_platform_id(video.get("url")),
                "direct_url": video.get("url"), "thumbnail_url": video.get("thumbnail"),
                "width": video.get("width"), "height": video.get("height"),
                "duration_ms": round((video.get("duration") or 0) * 1000),
                "variants": [format_observation(value) for value in video.get("formats") or []],
                "observed_at": observed_at, "volatile": True,
            }],
            "availability": {
                "state": "available", "checked_at": observed_at,
                "last_available_at": observed_at, "first_unavailable_at": None,
                "confirmed_at": None, "consecutive_failures": 0,
                "consecutive_successful_missing": 0,
                "http_status": 200, "evidence_ids": [source_ev],
            },
            "fetch": {"adapter": "legacy-fxtwitter", "observed_at": observed_at, "raw_sha256": None},
        }
        key = item_id(source_id)
        media_key = f"med_{key}_0"
        mirrors, retirement_rows = mirror_records(
            key, old_mirror.get(str(post["id"])), old_attach.get(str(post["id"]))
        )
        for row in retirement_rows:
            row.update({"source_id": source_id, "item_id": key, "media_id": media_key})
            retirement["entries"].append(row)
        prompt_evidence = evidence_id("prompt", source_id)
        if post.get("prompt"):
            prompt = {
                "status": "verbatim", "text": post["prompt"], "language": None,
                "source_id": source_id, "source_url": post["url"],
                "capture_method": "post_text", "is_verbatim": True,
                "evidence_ids": [prompt_evidence],
            }
            catalog["evidence"][prompt_evidence] = evidence_record(
                prompt_evidence, "prompt_source", source_id, post["url"], observed_at, curator["id"]
            )
        elif post.get("prompt_in_thread"):
            prompt = {
                "status": "referenced_not_captured", "text": None, "language": None,
                "source_id": source_id, "source_url": post["url"],
                "capture_method": "none", "is_verbatim": False,
                "evidence_ids": [source_ev],
            }
        else:
            prompt = {
                "status": "not_provided", "text": None, "language": None,
                "source_id": None, "source_url": None, "capture_method": "none",
                "is_verbatim": False, "evidence_ids": [],
            }
        category = post.get("category") or "Showcase"
        item = {
            "id": key,
            "display": {
                "title": {
                    "text": post.get("title") or "Untitled",
                    "provenance": "legacy_unattributed" if str(post["id"]) in overrides else "generated",
                    "editor_actor_id": None, "edited_at": None, "evidence_ids": [],
                },
                "category": {
                    "id": re.sub(r"[^a-z0-9]+", "-", category.lower()).strip("-"),
                    "label": category,
                    "provenance": "legacy_unattributed" if str(post["id"]) in overrides else "rule",
                    "rule_version": "legacy", "editor_actor_id": None, "evidence_ids": [],
                },
            },
            "model": model_record(post.get("model") or config["family"], source_ev),
            "canonical_source_id": source_id, "source_ids": [source_id],
            "attribution": {
                "posted_by": [{"source_id": source_id, "actor_id": actor["id"]}],
                "original_video_creators": [role_unknown()],
                "prompt_authors": [role_unknown()],
            },
            "prompt": prompt, "annotations": [],
            "provenance": {"duplicate_cluster_id": None, "fingerprints": [], "repost_chain": []},
            "media": [{
                "media_id": media_key, "source_id": source_id,
                "source_media_id": source_media_id, "kind": "video",
                "delivery": {
                    "mode": "source_link", "link_url": post["url"],
                    "official_embed": None, "mirrors": mirrors,
                },
            }],
            "rights": {
                "video_republication": rights_record(),
                "prompt_republication": rights_record(),
            },
            "curation": {
                "status": "approved", "reviewer_actor_id": curator["id"],
                "reviewed_at": reviewed_at, "evidence_ids": [audit_evidence],
            },
        }
        catalog["items"][key] = item
        catalog["candidates"][source_id] = {
            "source_id": source_id,
            "discoveries": [{
                "run_id": "run_legacy_urls_import_20260811", "query_id": "legacy.urls.txt",
                "found_at": reviewed_at, "rank": None, "submitted_by_actor_id": None,
            }],
            "review": {
                "state": "included", "reason_codes": ["meets_scope"],
                "note": "Approved for source-link indexing; authorship and republication rights are unverified.",
                "item_id": key, "duplicate_of_item_id": None,
                "reviewer_actor_id": curator["id"], "reviewed_at": reviewed_at,
                "evidence_ids": [audit_evidence],
                "history": [{"state": "included", "at": reviewed_at,
                             "actor_id": curator["id"], "reason_codes": ["meets_scope"]}],
            },
            "checks": {
                "public_visibility": "pass", "target_model": "pass", "has_video": "pass",
                "original_source": "unknown", "role_separation": "pass", "delivery_policy": "pass",
            },
        }

    urls = []
    for line in (ROOT / "scripts" / "urls.txt").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    for url in dict.fromkeys(urls):
        try:
            platform, native, source_id = source_identity(url)
        except ValueError as exc:
            print(f"warning: {exc}", file=sys.stderr)
            continue
        if source_id in included_sources:
            continue
        unknown = unknown_actor(platform, reviewed_at)
        catalog["actors"][unknown["id"]] = unknown
        legacy_input_evidence = f"ev_legacy_candidate_{source_id.replace(':', '_')}"
        catalog["evidence"][legacy_input_evidence] = {
            "id": legacy_input_evidence,
            "kind": "legacy_candidate_input",
            "url": url,
            "source_id": source_id,
            "observed_at": reviewed_at,
            "excerpt": (
                "The URL was present in scripts/urls.txt but had no data/posts.json record; "
                "the legacy pipeline did not preserve a drop or exclusion decision."
            ),
            "captured_by_actor_id": curator["id"],
            "visibility": "public",
            "integrity_sha256": sha256_text(url),
        }
        catalog["sources"][source_id] = {
            "id": source_id, "platform": platform, "native_id": native, "kind": "post",
            "url": url, "parent_source_id": None, "posted_at": None, "posted_date": None,
            "posted_by_actor_id": unknown["id"],
            "text": {"status": "unavailable", "value": None, "language": None},
            "metrics": {"observed_at": reviewed_at, "views": None, "likes": None,
                        "reposts": None, "comments": None},
            "media_observations": [],
            "availability": {
                "state": "unknown", "checked_at": reviewed_at, "last_available_at": None,
                "first_unavailable_at": None, "confirmed_at": None,
                "consecutive_failures": 0, "consecutive_successful_missing": 0,
                "http_status": None, "evidence_ids": [],
            },
            "fetch": {"adapter": "legacy-url-only", "observed_at": reviewed_at, "raw_sha256": None},
        }
        catalog["candidates"][source_id] = {
            "source_id": source_id,
            "discoveries": [{
                "run_id": "run_legacy_urls_import_20260811", "query_id": "legacy.urls.txt",
                "found_at": reviewed_at, "rank": None, "submitted_by_actor_id": None,
            }],
            "review": {
                "state": "pending", "reason_codes": ["legacy_drop_reason_unknown"],
                "note": "The legacy pipeline silently dropped this URL; it must be rehydrated and reviewed.",
                "item_id": None, "duplicate_of_item_id": None,
                "reviewer_actor_id": None, "reviewed_at": None,
                "evidence_ids": [legacy_input_evidence], "history": [],
            },
            "checks": {
                "public_visibility": "unknown", "target_model": "unknown", "has_video": "unknown",
                "original_source": "unknown", "role_separation": "unknown", "delivery_policy": "pass",
            },
        }

    catalog["discovery_runs"]["run_legacy_urls_import_20260811"]["result_count"] = len(catalog["candidates"])
    errors = validate_catalog(catalog)
    if errors:
        raise SystemExit("migration produced invalid catalog:\n" + "\n".join(f"- {e}" for e in errors))
    write_json(catalog_path, catalog)
    write_json(ROOT / "data" / "posts.json", export_posts(catalog))
    write_json(ROOT / "data" / "mirror.json", {})
    write_json(ROOT / "data" / "attachments.json", {})
    write_json(ROOT / "data" / "media-retirement.json", retirement)
    print(
        f"migrated {len(posts)} items and {len(catalog['candidates'])} candidates; "
        f"quarantined {len(retirement['entries'])} legacy media artifacts"
    )


if __name__ == "__main__":
    main()
