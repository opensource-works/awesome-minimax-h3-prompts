#!/usr/bin/env python3
"""Synchronize source availability and remove confirmed takedowns in one run.

Only authoritative deleted/private/suspended signals retire an item immediately.
Timeouts, rate limits, and 5xx responses preserve last-known-public output while
recording a transient error. Reddit omissions require two successful empty
checks by default because that endpoint does not explain every missing ID.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from catalog import (  # noqa: E402
    export_posts, read_json, refresh_retirement_manifest, utc_now,
    validate_catalog, write_json,
)
from authorized_manifests import (  # noqa: E402
    build_github_attachment_manifest, build_r2_manifest,
)

USER_AGENT = "opensource-works-prompt-index/2.0 (+https://github.com/opensource-works)"
AUTOMATION_ID = "act_automation_availability-sync"


def request_json(url: str, *, headers=None, data=None):
    request = urllib.request.Request(
        url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})}
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.loads(response.read().decode()), response.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:500]
        error = RuntimeError(f"HTTP {exc.code}: {body}")
        error.http_status = exc.code
        raise error from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        error = RuntimeError(f"network error: {exc}")
        error.http_status = None
        raise error from exc


def reddit_token() -> str:
    if os.environ.get("REDDIT_ACCESS_TOKEN"):
        return os.environ["REDDIT_ACCESS_TOKEN"]
    client = os.environ.get("REDDIT_CLIENT_ID")
    secret = os.environ.get("REDDIT_CLIENT_SECRET")
    if not client or not secret:
        raise RuntimeError("Reddit sync needs REDDIT_ACCESS_TOKEN or REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET")
    basic = base64.b64encode(f"{client}:{secret}".encode()).decode()
    payload = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    result, _ = request_json(
        "https://www.reddit.com/api/v1/access_token",
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        data=payload,
    )
    if not result.get("access_token"):
        raise RuntimeError("Reddit OAuth response did not include an access token")
    return result["access_token"]


def chunks(values: list[str], size: int):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def ensure_automation(catalog: dict, at: str) -> None:
    catalog["actors"].setdefault(AUTOMATION_ID, {
        "id": AUTOMATION_ID, "kind": "automation", "platform": "repository",
        "platform_user_id": None, "handle": "availability-sync",
        "display_name": "Availability sync automation", "profile_url": None,
        "avatar_url": None, "aliases": [], "observed_at": at,
    })


def add_evidence(catalog: dict, source_id: str, at: str, state: str, note: str | None = None) -> str:
    stamp = at.replace("-", "").replace(":", "").replace("T", "_").replace("Z", "")
    key = f"ev_availability_{source_id.replace(':', '_')}_{stamp}"
    source = catalog["sources"][source_id]
    catalog["evidence"][key] = {
        "id": key, "kind": "availability_check", "url": source["url"],
        "source_id": source_id, "observed_at": at, "excerpt": note,
        "captured_by_actor_id": AUTOMATION_ID, "visibility": "public",
        "integrity_sha256": None, "result": state,
    }
    return key


def mark_available(catalog: dict, source_id: str, at: str, *, metrics=None) -> None:
    source = catalog["sources"][source_id]
    evidence = add_evidence(catalog, source_id, at, "available")
    source["availability"].update({
        "state": "available", "checked_at": at, "last_available_at": at,
        "first_unavailable_at": None, "confirmed_at": None,
        "consecutive_failures": 0, "consecutive_successful_missing": 0,
        "http_status": 200,
        "evidence_ids": (source["availability"].get("evidence_ids") or []) + [evidence],
    })
    if metrics:
        source["metrics"] = {**source.get("metrics", {}), **metrics, "observed_at": at}


def mark_transient(catalog: dict, source_id: str, at: str, message: str, status=None) -> None:
    source = catalog["sources"][source_id]
    evidence = add_evidence(catalog, source_id, at, "transient_error", message)
    availability = source["availability"]
    availability.update({
        "state": "transient_error" if availability.get("last_available_at") else "unknown",
        "checked_at": at, "http_status": status,
        "consecutive_failures": availability.get("consecutive_failures", 0) + 1,
        # A transport/API failure interrupts the sequence of successful
        # responses that omitted a Reddit ID.  It must never help satisfy the
        # deletion-confirmation threshold.
        "consecutive_successful_missing": 0,
        "evidence_ids": (availability.get("evidence_ids") or []) + [evidence],
    })


def mark_reddit_missing(catalog: dict, source_id: str, at: str) -> int:
    """Record one successful Reddit response that omitted a requested ID."""
    source = catalog["sources"][source_id]
    availability = source["availability"]
    missing = availability.get("consecutive_successful_missing", 0) + 1
    evidence = add_evidence(
        catalog, source_id, at, "transient_error",
        "Reddit API omitted the ID; awaiting confirmation",
    )
    availability.update({
        "state": "transient_error" if availability.get("last_available_at") else "unknown",
        "checked_at": at, "http_status": 200,
        "consecutive_failures": availability.get("consecutive_failures", 0) + 1,
        "consecutive_successful_missing": missing,
        "evidence_ids": (availability.get("evidence_ids") or []) + [evidence],
    })
    return missing


def _nested_evidence_ids(value) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "evidence_ids" or key.endswith("_evidence_ids"):
                if isinstance(child, list):
                    found.update(part for part in child if isinstance(part, str))
            else:
                found.update(_nested_evidence_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_nested_evidence_ids(child))
    return found


def _clear_notes(value) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "note":
                value[key] = None
            else:
                _clear_notes(child)
    elif isinstance(value, list):
        for child in value:
            _clear_notes(child)


def _redact_source_content(source: dict, state: str) -> None:
    language = (source.get("text") or {}).get("language")
    source["text"] = {
        "status": "deleted" if state == "deleted" else "redacted",
        "value": None,
        "language": language,
    }
    for media in source.get("media_observations") or []:
        media["direct_url"] = None
        media["thumbnail_url"] = None
        media["variants"] = []


def _remove_prompt(item: dict, evidence_id: str) -> set[str]:
    prompt = item.get("prompt") or {}
    evidence_ids = _nested_evidence_ids(prompt)
    prompt.update({
        "status": "removed", "text": None, "source_id": None,
        "source_url": None, "capture_method": "none",
        "is_verbatim": False, "evidence_ids": [evidence_id],
    })
    prompt_rights = ((item.get("rights") or {}).get("prompt_republication") or {})
    prompt_rights.update({
        "status": "revoked", "license_spdx": None, "granted_scopes": [],
        "grantor_actor_ids": [], "granted_at": None, "expires_at": None,
        "evidence_ids": [evidence_id],
    })
    for role in ((item.get("attribution") or {}).get("prompt_authors") or []):
        role.update({"actor_id": None, "status": "unknown", "evidence_ids": [], "note": None})
    return evidence_ids


def _redact_root_item(catalog: dict, item: dict, state: str, at: str,
                      evidence_id: str, reason_code: str) -> tuple[set[str], set[str]]:
    """Apply takedown-grade redaction to one canonical root item."""
    prompt = item.get("prompt") or {}
    annotations = item.get("annotations") or []
    content_evidence_ids = (
        _nested_evidence_ids(prompt)
        | _nested_evidence_ids(annotations)
        | _nested_evidence_ids((item.get("display") or {}).get("title") or {})
        | _nested_evidence_ids(item.get("attribution") or {})
    )
    related_source_ids = set(item.get("source_ids") or [])
    if prompt.get("source_id"):
        related_source_ids.add(prompt["source_id"])
    related_source_ids.update(
        annotation.get("source_id") for annotation in annotations
        if isinstance(annotation, dict) and annotation.get("source_id")
    )

    title = item.setdefault("display", {}).setdefault("title", {})
    title.update({
        "text": f"Removed item {item.get('id')}", "provenance": "redacted",
        "editor_actor_id": AUTOMATION_ID, "edited_at": at,
        "evidence_ids": [evidence_id],
    })
    content_evidence_ids.update(_remove_prompt(item, evidence_id))
    item["annotations"] = []
    _clear_notes(item.get("attribution") or {})
    _clear_notes(item.get("provenance") or {})
    for rights in (item.get("rights") or {}).values():
        rights.update({
            "status": "revoked", "license_spdx": None, "granted_scopes": [],
            "grantor_actor_ids": [], "granted_at": None, "expires_at": None,
            "evidence_ids": [evidence_id],
        })
    item["curation"] = {
        "status": "removed", "reviewer_actor_id": AUTOMATION_ID,
        "reviewed_at": at, "reason_codes": [reason_code],
        "evidence_ids": [evidence_id],
    }
    for media in item.get("media") or []:
        delivery = media.get("delivery") or {}
        for mirror in delivery.get("mirrors") or []:
            if mirror.get("state") not in {"deleted", "pending_delete"}:
                mirror.update({
                    "state": "pending_delete", "pending_delete_at": at,
                    "pending_delete_reason_codes": [reason_code],
                    "pending_delete_evidence_ids": [evidence_id],
                })
        if not any(mirror.get("state") == "active" for mirror in delivery.get("mirrors") or []):
            delivery["mode"] = "source_link"
    return content_evidence_ids, related_source_ids


def redact_and_retire(catalog: dict, source_id: str, state: str, at: str, reason: str,
                      *, successful_missing_count: int = 0) -> None:
    source = catalog["sources"][source_id]
    evidence = add_evidence(catalog, source_id, at, state, reason)
    availability = source["availability"]
    availability.update({
        "state": state, "checked_at": at,
        "first_unavailable_at": availability.get("first_unavailable_at") or at,
        "confirmed_at": at, "consecutive_failures": availability.get("consecutive_failures", 0) + 1,
        "consecutive_successful_missing": successful_missing_count,
        "http_status": 404 if state == "deleted" else 403,
        "evidence_ids": (availability.get("evidence_ids") or []) + [evidence],
    })
    _redact_source_content(source, state)

    candidate = catalog.get("candidates", {}).get(source_id)
    reason_code = "source_deleted" if state == "deleted" else "source_private"
    if candidate:
        review = candidate["review"]
        review.update({
            "state": "removed", "reason_codes": [reason_code],
            "note": None, "reviewer_actor_id": AUTOMATION_ID,
            "reviewed_at": at, "evidence_ids": [evidence],
        })
        _clear_notes(review.get("history") or [])
        review.setdefault("history", []).append({
            "state": "removed", "at": at, "actor_id": AUTOMATION_ID,
            "reason_codes": [reason_code], "evidence_ids": [evidence], "note": None,
        })
        candidate.pop("prompt_observation", None)

    content_evidence_ids: set[str] = set()
    related_source_ids: set[str] = {source_id}
    for item in catalog.get("items", {}).values():
        if item.get("canonical_source_id") == source_id:
            item_evidence, item_sources = _redact_root_item(
                catalog, item, state, at, evidence, reason_code,
            )
            content_evidence_ids.update(item_evidence)
            related_source_ids.update(item_sources)
            continue

        removed_annotations = [
            annotation for annotation in item.get("annotations") or []
            if annotation.get("source_id") == source_id
        ]
        content_evidence_ids.update(_nested_evidence_ids(removed_annotations))
        item["annotations"] = [
            annotation for annotation in item.get("annotations") or []
            if annotation.get("source_id") != source_id
        ]
        prompt = item.get("prompt") or {}
        if prompt.get("source_id") == source_id:
            content_evidence_ids.update(_remove_prompt(item, evidence))

    for related_source_id in related_source_ids:
        related_source = (catalog.get("sources") or {}).get(related_source_id)
        if related_source:
            _redact_source_content(related_source, state)
        related_candidate = (catalog.get("candidates") or {}).get(related_source_id) or {}
        related_candidate.pop("prompt_observation", None)
        _clear_notes((related_candidate.get("review") or {}).get("history") or [])
        if "note" in (related_candidate.get("review") or {}):
            related_candidate["review"]["note"] = None

    for evidence_record in (catalog.get("evidence") or {}).values():
        if evidence_record.get("id") in content_evidence_ids or (
            evidence_record.get("source_id") in related_source_ids
        ):
            if "excerpt" in evidence_record:
                evidence_record["excerpt"] = None


def classify_x_error(error: dict) -> tuple[str | None, str]:
    title = (error.get("title") or "").casefold()
    detail = (error.get("detail") or "").casefold()
    status = error.get("status")
    text = title + " " + detail
    if status == 404 or "not found" in text or "does not exist" in text:
        return "deleted", error.get("detail") or error.get("title") or "X returned not found"
    if "protected" in text or "private" in text:
        return "private", error.get("detail") or "X account/post is private"
    if "suspend" in text:
        return "suspended", error.get("detail") or "X account is suspended"
    return None, error.get("detail") or error.get("title") or "Unclassified X API error"


def sync_x(catalog: dict, source_ids: list[str], at: str) -> tuple[int, int]:
    bearer = os.environ.get("X_BEARER_TOKEN")
    if not bearer:
        raise RuntimeError("X sync needs X_BEARER_TOKEN")
    available = unavailable = 0
    for batch in chunks(source_ids, 100):
        ids = [catalog["sources"][sid]["native_id"] for sid in batch]
        params = {
            "ids": ",".join(ids),
            "tweet.fields": "id,public_metrics",
        }
        url = "https://api.x.com/2/tweets?" + urllib.parse.urlencode(params)
        try:
            payload, _ = request_json(url, headers={"Authorization": f"Bearer {bearer}"})
        except RuntimeError as exc:
            for source_id in batch:
                mark_transient(catalog, source_id, at, str(exc), getattr(exc, "http_status", None))
            continue
        by_native = {tweet["id"]: tweet for tweet in payload.get("data") or []}
        errors = {str(error.get("resource_id")): error for error in payload.get("errors") or []}
        for source_id in batch:
            native = catalog["sources"][source_id]["native_id"]
            if native in by_native:
                metrics = by_native[native].get("public_metrics") or {}
                mark_available(catalog, source_id, at, metrics={
                    "views": metrics.get("impression_count"),
                    "likes": metrics.get("like_count"),
                    "reposts": metrics.get("retweet_count"),
                    "comments": metrics.get("reply_count"),
                })
                available += 1
                continue
            state, reason = classify_x_error(errors.get(native, {}))
            if state:
                redact_and_retire(catalog, source_id, state, at, reason)
                unavailable += 1
            else:
                mark_transient(catalog, source_id, at, reason)
    return available, unavailable


def sync_reddit(catalog: dict, source_ids: list[str], at: str,
                confirm_missing: int) -> tuple[int, int]:
    token = reddit_token()
    available = unavailable = 0
    for batch in chunks(source_ids, 100):
        ids = [catalog["sources"][sid]["native_id"] for sid in batch]
        url = "https://oauth.reddit.com/api/info.json?" + urllib.parse.urlencode({
            "id": ",".join(ids), "raw_json": "1"
        })
        try:
            payload, _ = request_json(url, headers={"Authorization": f"Bearer {token}"})
        except RuntimeError as exc:
            for source_id in batch:
                mark_transient(catalog, source_id, at, str(exc), getattr(exc, "http_status", None))
            continue
        children = (payload.get("data") or {}).get("children") or []
        by_native = {(child.get("data") or {}).get("name"): child.get("data") or {} for child in children}
        for source_id in batch:
            source = catalog["sources"][source_id]
            post = by_native.get(source["native_id"])
            if post:
                removed = post.get("removed_by_category")
                if source.get("kind") == "comment" and post.get("body") in {"[deleted]", "[removed]"}:
                    removed = post.get("body")
                if removed:
                    redact_and_retire(catalog, source_id, "deleted", at, f"Reddit removed_by_category={removed}")
                    unavailable += 1
                else:
                    mark_available(catalog, source_id, at, metrics={
                        "views": None, "likes": post.get("ups"),
                        "reposts": None, "comments": post.get("num_comments"),
                    })
                    available += 1
                continue
            missing = (
                (source.get("availability") or {}).get("consecutive_successful_missing", 0)
                + 1
            )
            if missing >= confirm_missing:
                redact_and_retire(catalog, source_id, "deleted", at,
                                  f"Reddit API omitted the ID in {missing} consecutive successful checks",
                                  successful_missing_count=missing)
                unavailable += 1
            else:
                mark_reddit_missing(catalog, source_id, at)
    return available, unavailable


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=["x", "reddit", "all"], default="all")
    parser.add_argument("--confirm-reddit-missing", type=int, default=2)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.confirm_reddit_missing < 2:
        raise SystemExit("--confirm-reddit-missing must be at least 2")
    catalog = read_json(ROOT / "data" / "catalog.json")
    at = utc_now()
    ensure_automation(catalog, at)
    selected = [args.platform] if args.platform != "all" else ["x", "reddit"]
    summary = {}
    for platform in selected:
        source_ids = [
            key for key, source in catalog["sources"].items()
            if source.get("platform") == platform and source.get("kind") in {"post", "comment"}
            and (source.get("availability") or {}).get("state") not in {"deleted", "private", "suspended"}
        ]
        if args.limit:
            source_ids = source_ids[:args.limit]
        if not source_ids:
            summary[platform] = (0, 0)
            continue
        if platform == "x":
            summary[platform] = sync_x(catalog, source_ids, at)
        else:
            summary[platform] = sync_reddit(catalog, source_ids, at, args.confirm_reddit_missing)
    catalog["updated_at"] = at
    errors = validate_catalog(catalog)
    if errors:
        raise SystemExit("availability sync produced invalid catalog:\n" + "\n".join(f"- {e}" for e in errors))
    if not args.dry_run:
        retirement_path = ROOT / "data" / "media-retirement.json"
        current_retirement = read_json(retirement_path, {})
        retirement = refresh_retirement_manifest(
            catalog, current_retirement, at,
            note="Source availability or rights state retired the mirror; remote cleanup requires confirmation.",
        )
        write_json(ROOT / "data" / "catalog.json", catalog)
        write_json(ROOT / "data" / "posts.json", export_posts(catalog))
        write_json(
            ROOT / "data" / "github-attachments.json",
            build_github_attachment_manifest(catalog, at),
        )
        write_json(ROOT / "data" / "r2-mirrors.json", build_r2_manifest(catalog, at))
        if retirement != current_retirement:
            write_json(retirement_path, retirement)
    for platform, (present, removed) in summary.items():
        print(f"{platform}: {present} available, {removed} confirmed unavailable")
    if args.dry_run:
        print("dry run: no files changed")


if __name__ == "__main__":
    main()
