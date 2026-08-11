#!/usr/bin/env python3
"""Discover public X and Reddit candidates without publishing or downloading media.

X uses the official v2 search API (recent or full archive). Reddit uses OAuth
and the documented search endpoint. Only stable identifiers, permalinks,
discovery provenance, and non-content metadata are persisted at this stage.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from catalog import (  # noqa: E402
    actor_id, read_json, source_identity, utc_now, validate_catalog, write_json,
)

USER_AGENT = "opensource-works-prompt-index/2.0 (+https://github.com/opensource-works)"


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def discovery_window(matrix: dict, kind: str, observed_at: str) -> dict:
    """Return the fixed historical or separately claimed ongoing request window."""
    coverage = matrix["coverage"]
    cutoff = coverage.get("observed_through") or coverage.get("through")
    if kind == "historical":
        return {
            "kind": "historical", "from": coverage["from"],
            "through_exclusive": cutoff, "requested_from": coverage["from"],
            "requested_through_exclusive": cutoff, "x_endpoint": "all",
        }
    if kind != "ongoing":
        raise ValueError("window kind must be historical or ongoing")
    run_observed_at = _timestamp(observed_at)
    cutoff_at = _timestamp(cutoff)
    lookback = int(coverage.get("ongoing_recent_lookback_days", 7))
    end = max(cutoff_at, run_observed_at - timedelta(seconds=15))
    actual_from = max(cutoff_at, end - timedelta(days=lookback))
    if run_observed_at < cutoff_at:
        end = cutoff_at
        actual_from = cutoff_at
    return {
        "kind": "ongoing", "from": _rfc3339(actual_from),
        "through_exclusive": _rfc3339(end), "requested_from": cutoff,
        "requested_through_exclusive": observed_at, "x_endpoint": "recent",
    }


def in_discovery_window(posted_at: str | None, window: dict) -> bool:
    if not posted_at:
        return False
    value = _timestamp(posted_at)
    return _timestamp(window["from"]) <= value < _timestamp(window["through_exclusive"])


def request_json(url: str, *, headers=None, data=None, attempts: int = 3):
    headers = {"User-Agent": USER_AGENT, **(headers or {})}
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers=headers, data=data)
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.loads(response.read().decode()), response.status
        except urllib.error.HTTPError as exc:
            if exc.code == 429 or 500 <= exc.code < 600:
                if attempt + 1 < attempts:
                    retry = min(int(exc.headers.get("Retry-After", "2") or 2), 30)
                    time.sleep(retry)
                    continue
            body = exc.read().decode(errors="replace")[:500]
            raise RuntimeError(f"HTTP {exc.code} from {url}: {body}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt + 1 < attempts:
                time.sleep(min(2 ** attempt, 8))
                continue
            raise RuntimeError(f"network error from {url}: {exc}") from exc
    raise AssertionError("unreachable")


def reddit_token() -> str:
    if os.environ.get("REDDIT_ACCESS_TOKEN"):
        return os.environ["REDDIT_ACCESS_TOKEN"]
    client = os.environ.get("REDDIT_CLIENT_ID")
    secret = os.environ.get("REDDIT_CLIENT_SECRET")
    if not client or not secret:
        raise RuntimeError("Reddit discovery needs REDDIT_ACCESS_TOKEN or REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET")
    basic = base64.b64encode(f"{client}:{secret}".encode()).decode()
    payload = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    data, _ = request_json(
        "https://www.reddit.com/api/v1/access_token",
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        data=payload,
    )
    if not data.get("access_token"):
        raise RuntimeError("Reddit OAuth response did not include an access token")
    return data["access_token"]


def person(platform: str, handle: str | None, observed_at: str, *, uid=None,
           name=None, profile_url=None, avatar_url=None) -> dict:
    key = actor_id(platform, platform_user_id=uid, handle=handle)
    return {
        "id": key, "kind": "person" if handle else "unknown", "platform": platform,
        "platform_user_id": str(uid) if uid else None, "handle": handle,
        "display_name": name or handle, "profile_url": profile_url,
        "avatar_url": avatar_url, "aliases": [], "observed_at": observed_at,
    }


def source_stub(source_id: str, platform: str, native_id: str, url: str,
                actor_key: str, observed_at: str, *, posted_at=None,
                posted_date=None, community=None, adapter="manual",
                available="unknown") -> dict:
    return {
        "id": source_id, "platform": platform, "native_id": native_id,
        "kind": "post", "url": url, "parent_source_id": None,
        "posted_at": posted_at, "posted_date": posted_date,
        "posted_by_actor_id": actor_key, "community": community,
        "text": {"status": "unavailable", "value": None, "language": None},
        "metrics": {"observed_at": observed_at, "views": None, "likes": None,
                    "reposts": None, "comments": None},
        "media_observations": [],
        "availability": {
            "state": available, "checked_at": observed_at,
            "last_available_at": observed_at if available == "available" else None,
            "first_unavailable_at": None, "confirmed_at": None,
            "consecutive_failures": 0, "consecutive_successful_missing": 0,
            "http_status": 200 if available == "available" else None,
            "evidence_ids": [],
        },
        "fetch": {"adapter": adapter, "observed_at": observed_at, "raw_sha256": None},
    }


def new_candidate(source_id: str) -> dict:
    return {
        "source_id": source_id, "discoveries": [],
        "review": {
            "state": "pending", "reason_codes": [], "note": None,
            "item_id": None, "duplicate_of_item_id": None,
            "reviewer_actor_id": None, "reviewed_at": None,
            "evidence_ids": [], "history": [],
        },
        "checks": {
            "public_visibility": "unknown", "target_model": "unknown",
            "has_video": "unknown", "original_source": "unknown",
            "role_separation": "unknown", "delivery_policy": "pass",
        },
    }


def record(catalog: dict, source: dict, actor: dict, *, run_id: str,
           query_id: str, rank: int | None, found_at: str,
           has_video: bool | None, model_match: bool | None) -> None:
    catalog["actors"][actor["id"]] = {**catalog["actors"].get(actor["id"], {}), **actor}
    existing_source = catalog["sources"].get(source["id"])
    if not existing_source or (existing_source.get("fetch") or {}).get("adapter") in {
        "legacy-url-only", "manual-import", "x-search", "reddit-search"
    }:
        catalog["sources"][source["id"]] = {**(existing_source or {}), **source}
    candidate = catalog["candidates"].setdefault(source["id"], new_candidate(source["id"]))
    discovery = {
        "run_id": run_id, "query_id": query_id, "found_at": found_at,
        "rank": rank, "submitted_by_actor_id": None,
    }
    if not any(d.get("run_id") == run_id and d.get("query_id") == query_id
               for d in candidate["discoveries"]):
        candidate["discoveries"].append(discovery)
    if has_video is not None:
        candidate["checks"]["has_video"] = "pass" if has_video else "fail"
    if model_match is not None:
        candidate["checks"]["target_model"] = "pass" if model_match else "fail"
    candidate["checks"]["public_visibility"] = "pass"


def model_matches(text: str, config: dict) -> bool:
    value = text.casefold()
    aliases = [alias for model in config["model_scope"] for alias in model.get("aliases", [])]
    if config["id"] == "awesome-minimax-h3-prompts" and "hailuo 2.3" in value:
        return False
    return any(alias.casefold() in value for alias in aliases)


def discover_x(catalog: dict, queries: list[dict], matrix: dict, config: dict,
               *, window: dict, max_pages: int, run_id: str,
               observed_at: str) -> tuple[int, list[str], dict]:
    bearer = os.environ.get("X_BEARER_TOKEN")
    if not bearer:
        raise RuntimeError("X discovery needs X_BEARER_TOKEN")
    endpoint = window["x_endpoint"]
    total, errors = 0, []
    stats = {"filtered_outside_window": 0, "missing_timestamp": 0}
    for query in queries:
        token = None
        for page in range(max_pages):
            params = {
                "query": query["query"], "max_results": "100",
                "tweet.fields": "id,text,created_at,author_id,attachments",
                "expansions": "author_id,attachments.media_keys",
                "user.fields": "id,name,username,profile_image_url",
                "media.fields": "media_key,type",
            }
            params.update({"start_time": window["from"], "end_time": window["through_exclusive"]})
            if token:
                params["next_token"] = token
            url = f"https://api.x.com/2/tweets/search/{endpoint}?" + urllib.parse.urlencode(params)
            try:
                payload, _ = request_json(url, headers={"Authorization": f"Bearer {bearer}"})
            except RuntimeError as exc:
                errors.append(f"{query['id']}: {exc}")
                break
            users = {u["id"]: u for u in (payload.get("includes") or {}).get("users", [])}
            media = {m["media_key"]: m for m in (payload.get("includes") or {}).get("media", [])}
            for rank, tweet in enumerate(payload.get("data") or [], 1 + page * 100):
                user = users.get(tweet.get("author_id"), {})
                handle = user.get("username")
                source_url = f"https://x.com/{handle}/status/{tweet['id']}" if handle else f"https://x.com/i/web/status/{tweet['id']}"
                _, native, source_id = source_identity(source_url)
                actor = person(
                    "x", handle, observed_at, uid=user.get("id"), name=user.get("name"),
                    profile_url=f"https://x.com/{handle}" if handle else None,
                    avatar_url=user.get("profile_image_url"),
                )
                attached = (tweet.get("attachments") or {}).get("media_keys") or []
                has_video = any((media.get(key) or {}).get("type") in {"video", "animated_gif"} for key in attached)
                posted_at = tweet.get("created_at")
                if not posted_at:
                    stats["missing_timestamp"] += 1
                    continue
                if not in_discovery_window(posted_at, window):
                    stats["filtered_outside_window"] += 1
                    continue
                source = source_stub(
                    source_id, "x", native, source_url, actor["id"], observed_at,
                    posted_at=posted_at, posted_date=posted_at[:10] if posted_at else None,
                    adapter="x-search", available="available",
                )
                record(catalog, source, actor, run_id=run_id, query_id=query["id"],
                       rank=rank, found_at=observed_at, has_video=has_video,
                       model_match=model_matches(tweet.get("text") or "", config))
                total += 1
            token = (payload.get("meta") or {}).get("next_token")
            if not token:
                break
    return total, errors, stats


def discover_reddit(catalog: dict, queries: list[dict], matrix: dict, config: dict,
                    *, window: dict, max_pages: int, run_id: str,
                    observed_at: str) -> tuple[int, list[str], dict]:
    token = reddit_token()
    total, errors = 0, []
    stats = {"filtered_outside_window": 0, "missing_timestamp": 0}
    for query in queries:
        communities = query.get("subreddits") or [None]
        for community in communities:
            after = None
            for page in range(max_pages):
                params = {"q": query["query"], "sort": "new", "t": "all", "limit": "100", "raw_json": "1"}
                if community:
                    params["restrict_sr"] = "true"
                if after:
                    params["after"] = after
                prefix = f"/r/{community}" if community else ""
                url = f"https://oauth.reddit.com{prefix}/search.json?" + urllib.parse.urlencode(params)
                try:
                    payload, _ = request_json(url, headers={"Authorization": f"Bearer {token}"})
                except RuntimeError as exc:
                    suffix = f" r/{community}" if community else ""
                    errors.append(f"{query['id']}{suffix}: {exc}")
                    break
                listing = payload.get("data") or {}
                for rank, child in enumerate(listing.get("children") or [], 1 + page * 100):
                    post = child.get("data") or {}
                    native = post.get("name") or f"t3_{post.get('id')}"
                    permalink = post.get("permalink") or f"/comments/{post.get('id')}"
                    source_url = "https://www.reddit.com" + permalink
                    source_id = f"reddit:{native}"
                    handle = post.get("author") if post.get("author") not in {None, "[deleted]"} else None
                    actor = person(
                        "reddit", handle, observed_at, uid=post.get("author_fullname"),
                        name=handle, profile_url=f"https://www.reddit.com/user/{handle}/" if handle else None,
                    )
                    created = post.get("created_utc")
                    posted_at = datetime.fromtimestamp(created, timezone.utc).isoformat().replace("+00:00", "Z") if created else None
                    if not posted_at:
                        stats["missing_timestamp"] += 1
                        continue
                    if not in_discovery_window(posted_at, window):
                        stats["filtered_outside_window"] += 1
                        continue
                    source = source_stub(
                        source_id, "reddit", native, source_url, actor["id"], observed_at,
                        posted_at=posted_at, posted_date=posted_at[:10] if posted_at else None,
                        community=post.get("subreddit_name_prefixed"), adapter="reddit-search",
                        available="available",
                    )
                    media = post.get("media") or {}
                    has_video = bool(post.get("is_video") or media.get("reddit_video") or post.get("post_hint") == "hosted:video")
                    search_text = " ".join(filter(None, [post.get("title"), post.get("selftext")]))
                    record(catalog, source, actor, run_id=run_id, query_id=query["id"],
                           rank=rank, found_at=observed_at, has_video=has_video,
                           model_match=model_matches(search_text, config))
                    total += 1
                after = listing.get("after")
                if not after:
                    break
    return total, errors, stats


def import_urls(catalog: dict, path: Path, *, run_id: str,
                query_id: str, observed_at: str) -> tuple[int, list[str], set[str]]:
    total, errors, platforms = 0, [], set()
    for rank, line in enumerate(path.read_text().splitlines(), 1):
        url = line.strip()
        if not url or url.startswith("#"):
            continue
        try:
            platform, native, source_id = source_identity(url)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        platforms.add(platform)
        actor = person(platform, None, observed_at)
        source = source_stub(source_id, platform, native, url, actor["id"], observed_at,
                             adapter="manual-import", available="unknown")
        record(catalog, source, actor, run_id=run_id, query_id=query_id,
               rank=rank, found_at=observed_at, has_video=None, model_match=None)
        total += 1
    return total, errors, platforms


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=["x", "reddit", "all"], default="all")
    parser.add_argument(
        "--window", choices=["historical", "ongoing"],
        help="fixed historical backfill or separately reported ongoing increment",
    )
    parser.add_argument("--mode", choices=["recent", "all"], help=argparse.SUPPRESS)
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--import-file", type=Path, help="import newline-separated X/Reddit URLs instead of APIs")
    parser.add_argument("--query-id", default="manual.import")
    parser.add_argument("--at", help="fixed RFC3339 discovery time for reproducible backfills")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.max_pages < 1:
        raise SystemExit("--max-pages must be positive")

    config = read_json(ROOT / "config" / "collection.json")
    matrix = read_json(ROOT / "config" / "query-matrix.json")
    catalog = read_json(ROOT / "data" / "catalog.json")
    observed_at = args.at or utc_now()
    window_kind = args.window or ({"all": "historical", "recent": "ongoing"}.get(args.mode)) or "ongoing"
    try:
        window = discovery_window(matrix, window_kind, observed_at)
    except (KeyError, ValueError) as exc:
        parser.error(str(exc))
    stamp = observed_at.replace("-", "").replace(":", "").replace("T", "_").replace("Z", "")
    all_errors, total = [], 0

    if args.import_file:
        run_id = f"run_manual_{stamp}"
        count, errors, platforms = import_urls(
            catalog, args.import_file, run_id=run_id, query_id=args.query_id, observed_at=observed_at
        )
        catalog["discovery_runs"][run_id] = {
            "id": run_id, "started_at": observed_at, "ended_at": observed_at if args.at else utc_now(),
            "platform": next(iter(platforms)) if len(platforms) == 1 else "mixed",
            "query_matrix_version": matrix["version"], "query_ids": [args.query_id],
            "status": "partial" if errors else "complete", "result_count": count,
            "log_url": None, "errors": errors,
        }
        total += count
        all_errors.extend(errors)
    else:
        selected = [args.platform] if args.platform != "all" else ["x", "reddit"]
        for platform in selected:
            queries = [q for q in matrix["queries"] if q["platform"] == platform]
            run_id = f"run_{window_kind}_{platform}_{stamp}"
            started = utc_now()
            try:
                if platform == "x":
                    count, errors, window_stats = discover_x(
                        catalog, queries, matrix, config, window=window,
                        max_pages=args.max_pages, run_id=run_id, observed_at=observed_at,
                    )
                else:
                    count, errors, window_stats = discover_reddit(
                        catalog, queries, matrix, config, window=window,
                        max_pages=args.max_pages,
                        run_id=run_id, observed_at=observed_at,
                    )
            except RuntimeError as exc:
                count, errors = 0, [str(exc)]
                window_stats = {"filtered_outside_window": 0, "missing_timestamp": 0}
            catalog["discovery_runs"][run_id] = {
                "id": run_id, "started_at": started, "ended_at": utc_now(),
                "platform": platform, "query_matrix_version": matrix["version"],
                "query_ids": [q["id"] for q in queries],
                "status": "partial" if errors and count else ("failed" if errors else "complete"),
                "result_count": count, "log_url": None, "errors": errors,
                "window_kind": window_kind,
                "window": {key: value for key, value in window.items() if key != "x_endpoint"},
                "run_observed_at": observed_at,
                **window_stats,
            }
            total += count
            all_errors.extend(errors)
    catalog["updated_at"] = observed_at if args.at else utc_now()
    problems = validate_catalog(catalog)
    if problems:
        raise SystemExit("discovery produced invalid catalog:\n" + "\n".join(f"- {p}" for p in problems))
    if not args.dry_run:
        write_json(ROOT / "data" / "catalog.json", catalog)
    print(f"observed {total} results; catalog now has {len(catalog['candidates'])} candidates")
    if args.dry_run:
        print("dry run: data/catalog.json was not changed")
    if all_errors:
        print(f"{len(all_errors)} query/import errors:", file=sys.stderr)
        for error in all_errors:
            print(f"- {error}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
