#!/usr/bin/env python3
"""Hydrate known X/Reddit source IDs through official APIs.

Hydration updates observations and review checks but never includes a candidate
or claims creative ownership. Optional comment capture keeps only replies that
may contain a prompt, credit, source, or workflow annotation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from catalog import (  # noqa: E402
    actor_id, evidence_id, export_posts, read_json, refresh_retirement_manifest,
    utc_now, validate_catalog, write_json,
)
from authorized_manifests import (  # noqa: E402
    build_github_attachment_manifest, build_r2_manifest,
)
from discover import model_matches, person, reddit_token, request_json  # noqa: E402
from sync_availability import (  # noqa: E402
    classify_x_error, ensure_automation, mark_available, mark_transient,
    redact_and_retire,
)

PROMPT_MARKER = re.compile(
    r"(?:here(?:'s| is) (?:the )?(?:exact )?prompt|(?:video|image|exact|full|base)?\s*prompt"
    r"|提示词|提示詞|プロンプト|프롬프트|indicaciones)\s*[👇⬇️]*\s*[:：-]?",
    re.I,
)
THREAD_MARKER = re.compile(
    r"\bprompts?\s+(?:in|is in|below|in the)\s+(?:the\s+)?(?:comments?|thread|repl(?:y|ies))\b"
    r"|提示词.{0,12}(?:评论|回复|楼下)|プロンプト.{0,12}(?:返信|コメント)",
    re.I,
)
COMMENT_SIGNAL = re.compile(
    r"prompt|workflow|credit|source|made by|created by|original|提示词|提示詞|工作流|原作者|出处"
    r"|プロンプト|作者|프롬프트|indicaciones",
    re.I,
)
VOLATILE_CACHE_VERSION = "volatile-media-cache-v1"


def initialize_volatile_cache(catalog: dict, at: str, existing: dict | None = None) -> dict:
    """Return the private, gitignored cache used for raw text and media locators."""
    cache = existing if isinstance(existing, dict) else {}
    if cache and cache.get("schema_version") != VOLATILE_CACHE_VERSION:
        raise ValueError(f"volatile cache schema must be {VOLATILE_CACHE_VERSION}")
    collection_id = (catalog.get("collection") or {}).get("id")
    if cache.get("collection_id") not in {None, collection_id}:
        raise ValueError("volatile cache belongs to a different collection")
    cache.setdefault("schema_version", VOLATILE_CACHE_VERSION)
    cache.setdefault("collection_id", collection_id)
    cache.setdefault("generated_at", at)
    cache["updated_at"] = at
    cache.setdefault("observations", {})
    cache.setdefault("source_texts", {})
    cache.setdefault("prompts", {})
    for key in ("observations", "source_texts", "prompts"):
        if not isinstance(cache[key], dict):
            raise ValueError(f"volatile cache {key} must be an object")
    return cache


def volatile_cache_path(value: Path) -> Path:
    """Reject tracked in-repository cache locations."""
    resolved = value.expanduser().resolve()
    try:
        relative = resolved.relative_to(ROOT.resolve())
    except ValueError:
        return resolved
    if not relative.parts or relative.parts[0] != ".cache":
        raise ValueError("an in-repository volatile cache must be under .cache/")
    return resolved


def write_volatile_cache(path: Path, cache: dict) -> None:
    """Write the private cache with owner-only permissions."""
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        inside_repository_cache = (
            path.resolve().relative_to((ROOT / ".cache").resolve()) is not None
        )
    except ValueError:
        inside_repository_cache = False
    if not parent_existed or inside_repository_cache:
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        os.fchmod(descriptor, 0o600)
        payload = (json.dumps(cache, ensure_ascii=False, indent=2) + "\n").encode()
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _cache_text(cache: dict | None, source_id: str, source_url: str,
                text: str, at: str, language=None) -> None:
    if cache is None:
        return
    cache["source_texts"][source_id] = {
        "source_id": source_id, "source_url": source_url, "text": text,
        "language": language, "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "length": len(text), "observed_at": at,
    }


def _cache_media(cache: dict | None, source_id: str, observations: list[dict]) -> None:
    if cache is None:
        return
    prefix = source_id + "/"
    for key in [key for key in cache["observations"] if key.startswith(prefix)]:
        del cache["observations"][key]
    for observation in observations:
        source_media_id = observation.get("source_media_id")
        if not source_media_id:
            continue
        cache["observations"][f"{source_id}/{source_media_id}"] = {
            "source_id": source_id,
            "source_media_id": source_media_id,
            "direct_url": observation.get("direct_url"),
            "thumbnail_url": observation.get("thumbnail_url"),
            "variants": observation.get("variants") or [],
            "observed_at": observation.get("observed_at"),
        }


def _withheld_text(text: str, language=None) -> dict:
    return {
        "status": "withheld_pending_review", "value": None, "language": language,
        "sha256": hashlib.sha256(text.encode()).hexdigest(), "length": len(text),
    }


def _sanitized_media(observations: list[dict]) -> list[dict]:
    output = []
    for observation in observations:
        safe = dict(observation)
        safe.update({"direct_url": None, "thumbnail_url": None, "variants": []})
        output.append(safe)
    return output


def scrub_volatile_catalog(catalog: dict, cache: dict | None, at: str, *,
                           discard: bool = False) -> None:
    """Move pre-existing raw source text/media locators out of the public catalog."""
    volatile_found = any(
        isinstance((source.get("text") or {}).get("value"), str)
        and bool((source.get("text") or {}).get("value"))
        or any(
            observation.get("direct_url") or observation.get("thumbnail_url")
            or observation.get("variants")
            for observation in source.get("media_observations") or []
        )
        for source in (catalog.get("sources") or {}).values()
    ) or any(
        "text" in (candidate.get("prompt_observation") or {})
        for candidate in (catalog.get("candidates") or {}).values()
    )
    if volatile_found and cache is None and not discard:
        raise ValueError(
            "catalog contains volatile raw content; pass --volatile-cache to migrate it "
            "or --discard-volatile to delete it explicitly"
        )
    for source_id, source in (catalog.get("sources") or {}).items():
        text_record = source.get("text") or {}
        raw_text = text_record.get("value")
        if isinstance(raw_text, str) and raw_text:
            _cache_text(
                cache, source_id, source.get("url"), raw_text, at,
                text_record.get("language"),
            )
            source["text"] = _withheld_text(raw_text, text_record.get("language"))
        observations = source.get("media_observations") or []
        if any(
            observation.get("direct_url") or observation.get("thumbnail_url")
            or observation.get("variants")
            for observation in observations
        ):
            _cache_media(cache, source_id, observations)
            source["media_observations"] = _sanitized_media(observations)
    for candidate_source_id, candidate in (catalog.get("candidates") or {}).items():
        observation = candidate.get("prompt_observation") or {}
        if "text" not in observation:
            continue
        raw_prompt = observation.pop("text")
        if isinstance(raw_prompt, str) and raw_prompt:
            prompt_source_id = observation.get("source_id") or candidate_source_id
            cache_key = _prompt_cache_key(candidate_source_id, prompt_source_id)
            digest = hashlib.sha256(raw_prompt.encode()).hexdigest()
            observation.update({
                "candidate_source_id": candidate_source_id,
                "cache_key": cache_key,
                "text_sha256": digest,
                "text_length": len(raw_prompt),
            })
            if cache is not None:
                cache["prompts"][cache_key] = {
                    "candidate_source_id": candidate_source_id,
                    "source_id": prompt_source_id,
                    "source_url": observation.get("source_url"),
                    "status": observation.get("status"),
                    "text": raw_prompt,
                    "language": observation.get("language"),
                    "capture_method": observation.get("capture_method"),
                    "is_verbatim": observation.get("is_verbatim"),
                    "sha256": digest,
                    "length": len(raw_prompt),
                    "evidence_ids": list(observation.get("evidence_ids") or []),
                    "observed_at": observation.get("observed_at") or at,
                }
        else:
            observation.update({
                "candidate_source_id": candidate_source_id,
                "cache_key": None, "text_sha256": None, "text_length": None,
            })


def _prompt_cache_key(candidate_source_id: str, prompt_source_id: str) -> str:
    return f"{candidate_source_id}/{prompt_source_id}"


def _forget_candidate_prompt(cache: dict | None, candidate: dict | None) -> None:
    observation = (candidate or {}).get("prompt_observation") or {}
    cache_key = observation.get("cache_key")
    if cache is not None and cache_key:
        cache["prompts"].pop(cache_key, None)


def _record_prompt_observation(
    catalog: dict,
    candidate_source_id: str,
    prompt_source_id: str,
    at: str,
    *,
    status: str,
    text: str | None,
    capture_method: str,
    volatile_cache: dict | None,
) -> None:
    """Record review metadata publicly and the verbatim payload privately."""
    candidate = (catalog.get("candidates") or {}).get(candidate_source_id)
    if not candidate or (candidate.get("review") or {}).get("state") != "pending":
        return
    prompt_source = catalog["sources"][prompt_source_id]
    evidence_kind = "source_comment" if prompt_source.get("kind") == "comment" else "source_post"
    evidence_ids = [evidence(catalog, prompt_source_id, evidence_kind, at)]
    _forget_candidate_prompt(volatile_cache, candidate)
    cache_key = (
        _prompt_cache_key(candidate_source_id, prompt_source_id)
        if status == "verbatim" else None
    )
    digest = hashlib.sha256(text.encode()).hexdigest() if text is not None else None
    candidate["prompt_observation"] = {
        "review_state": "pending", "status": status,
        "candidate_source_id": candidate_source_id,
        "source_id": prompt_source_id, "source_url": prompt_source["url"],
        "capture_method": capture_method, "is_verbatim": status == "verbatim",
        "text_sha256": digest, "text_length": len(text) if text is not None else None,
        "language": None, "cache_key": cache_key, "evidence_ids": evidence_ids,
        "observed_at": at,
        "observed_by_actor_id": "act_automation_availability-sync",
    }
    if volatile_cache is not None and text is not None and cache_key:
        volatile_cache["prompts"][cache_key] = {
            "candidate_source_id": candidate_source_id,
            "source_id": prompt_source_id, "source_url": prompt_source["url"],
            "status": status, "text": text, "language": None,
            "capture_method": capture_method, "is_verbatim": True,
            "sha256": digest, "length": len(text), "evidence_ids": evidence_ids,
            "observed_at": at,
        }


def prompt_from_text(text: str) -> str | None:
    best = None
    for match in PROMPT_MARKER.finditer(text or ""):
        tail = (text[match.end():] or "").strip()
        tail = re.sub(r"\s*https://t\.co/\w+\s*$", "", tail).strip()
        if len(tail) >= 80 and (best is None or len(tail) > len(best)):
            best = tail
    return best


def formats_from_x(media: dict) -> list[dict]:
    output = []
    for variant in media.get("variants") or []:
        if variant.get("content_type") != "video/mp4":
            continue
        dimensions = re.search(r"/(\d+)x(\d+)/", variant.get("url") or "")
        output.append({
            "url": variant.get("url"),
            "width": int(dimensions.group(1)) if dimensions else media.get("width"),
            "height": int(dimensions.group(2)) if dimensions else media.get("height"),
            "bitrate": variant.get("bit_rate"), "container": "mp4", "codec": None,
        })
    return output


def x_media_observations(tweet: dict, media_map: dict, at: str) -> list[dict]:
    output = []
    for position, key in enumerate((tweet.get("attachments") or {}).get("media_keys") or []):
        media = media_map.get(key) or {}
        if media.get("type") not in {"video", "animated_gif"}:
            continue
        variants = formats_from_x(media)
        direct = max(variants, key=lambda value: value.get("bitrate") or 0)["url"] if variants else media.get("url")
        output.append({
            "source_media_id": f"x-media:{key}", "kind": "video", "position": position,
            "platform_media_id": key, "direct_url": direct,
            "thumbnail_url": media.get("preview_image_url"),
            "width": media.get("width"), "height": media.get("height"),
            "duration_ms": media.get("duration_ms"), "variants": variants,
            "observed_at": at, "volatile": True,
        })
    return output


def evidence(catalog: dict, source_id: str, kind: str, at: str) -> str:
    key = evidence_id("comment" if kind == "source_comment" else "source", source_id)
    source = catalog["sources"][source_id]
    catalog["evidence"][key] = {
        "id": key, "kind": kind, "url": source["url"], "source_id": source_id,
        "observed_at": at, "excerpt": None,
        "captured_by_actor_id": "act_automation_availability-sync",
        "visibility": "public", "integrity_sha256": None,
    }
    return key


def update_actor(catalog: dict, source: dict, incoming: dict) -> str:
    current_id = source.get("posted_by_actor_id")
    current = catalog["actors"].get(current_id)
    # Preserve stable IDs already referenced by included items. Replace only
    # the generic unknown actor used by discovery/import stubs.
    if current and current.get("kind") != "unknown":
        current.update({key: value for key, value in incoming.items() if value is not None and key != "id"})
        return current_id
    catalog["actors"][incoming["id"]] = incoming
    source["posted_by_actor_id"] = incoming["id"]
    return incoming["id"]


def refresh_item_posted_by(catalog: dict, source_id: str, actor_key: str) -> None:
    for item in catalog["items"].values():
        if source_id not in item.get("source_ids", []):
            continue
        roles = (item.get("attribution") or {}).get("posted_by") or []
        for role in roles:
            if role.get("source_id") == source_id:
                role["actor_id"] = actor_key


def source_needs_mirror_locator(catalog: dict, source_id: str) -> bool:
    """Keep rights-cleared, not-yet-mirrored sources hydratable in pending-only runs."""
    for item in (catalog.get("items") or {}).values():
        if (item.get("curation") or {}).get("status") != "approved":
            continue
        rights = ((item.get("rights") or {}).get("video_republication") or {})
        if rights.get("status") not in {"granted", "public_license"}:
            continue
        scopes = set(rights.get("granted_scopes") or [])
        required_providers = set()
        if {"download", "mirror_r2"} <= scopes:
            required_providers.add("r2")
        if {"download", "mirror_github"} <= scopes:
            required_providers.add("github_attachment")
        if not required_providers:
            continue
        for media in item.get("media") or []:
            if media.get("kind") != "video" or media.get("source_id") != source_id:
                continue
            active = {
                mirror.get("provider")
                for mirror in ((media.get("delivery") or {}).get("mirrors") or [])
                if mirror.get("state") == "active" and mirror.get("artifact") == "video"
            }
            if required_providers - active:
                return True
    return False


def maybe_capture_root_prompt(catalog: dict, source_id: str, at: str, *,
                              text: str, volatile_cache: dict | None = None) -> None:
    """Save only a review proposal; hydration never changes an approved item."""
    source = catalog["sources"][source_id]
    found = prompt_from_text(text)
    if found:
        _record_prompt_observation(
            catalog, source_id, source_id, at, status="verbatim", text=found,
            capture_method="post_text", volatile_cache=volatile_cache,
        )
        return
    elif THREAD_MARKER.search(text):
        _record_prompt_observation(
            catalog, source_id, source_id, at,
            status="referenced_not_captured", text=None,
            capture_method="none", volatile_cache=volatile_cache,
        )
        return
    candidate = (catalog.get("candidates") or {}).get(source_id)
    if candidate and (candidate.get("review") or {}).get("state") == "pending":
        _forget_candidate_prompt(volatile_cache, candidate)
        candidate.pop("prompt_observation", None)


def store_comment(catalog: dict, *, platform: str, native_id: str, url: str,
                  parent_source_id: str, text: str, actor: dict, at: str,
                  posted_at: str | None, metrics: dict | None = None,
                  volatile_cache: dict | None = None) -> str:
    source_id = f"{platform}:{native_id}"
    catalog["actors"][actor["id"]] = {**catalog["actors"].get(actor["id"], {}), **actor}
    catalog["sources"][source_id] = {
        "id": source_id, "platform": platform, "native_id": native_id,
        "kind": "comment", "url": url, "parent_source_id": parent_source_id,
        "posted_at": posted_at, "posted_date": posted_at[:10] if posted_at else None,
        "posted_by_actor_id": actor["id"],
        "text": _withheld_text(text),
        "metrics": {"observed_at": at, "views": None, "likes": (metrics or {}).get("likes"),
                    "reposts": (metrics or {}).get("reposts"), "comments": None},
        "media_observations": [],
        "availability": {
            "state": "available", "checked_at": at, "last_available_at": at,
            "first_unavailable_at": None, "confirmed_at": None,
            "consecutive_failures": 0, "consecutive_successful_missing": 0,
            "http_status": 200, "evidence_ids": [],
        },
        "fetch": {
            "adapter": f"{platform}-official-api", "observed_at": at,
            "raw_sha256": hashlib.sha256(text.encode()).hexdigest(),
        },
    }
    _cache_text(volatile_cache, source_id, url, text, at)
    ev = evidence(catalog, source_id, "source_comment", at)
    catalog["sources"][source_id]["availability"]["evidence_ids"] = [ev]
    return source_id


def maybe_capture_comment_prompt(catalog: dict, root_source_id: str, comment_source_id: str,
                                 *, text: str, at: str,
                                 volatile_cache: dict | None = None) -> None:
    comment = catalog["sources"][comment_source_id]
    found = prompt_from_text(text)
    if not found and len(text) >= 120 and "prompt" in text.casefold():
        found = text
    if not found:
        return
    root_actor = catalog["sources"][root_source_id].get("posted_by_actor_id")
    if comment.get("posted_by_actor_id") != root_actor:
        return
    _record_prompt_observation(
        catalog, root_source_id, comment_source_id, at,
        status="verbatim", text=found, capture_method="comment_text",
        volatile_cache=volatile_cache,
    )


def hydrate_x(catalog: dict, source_ids: list[str], config: dict, at: str,
              *, with_comments: bool, x_search_mode: str, comment_pages: int,
              volatile_cache: dict | None = None) -> tuple[int, list[str]]:
    bearer = os.environ.get("X_BEARER_TOKEN")
    if not bearer:
        raise RuntimeError("X hydration needs X_BEARER_TOKEN")
    hydrated, errors = 0, []
    for start in range(0, len(source_ids), 100):
        batch = source_ids[start:start + 100]
        ids = [catalog["sources"][key]["native_id"] for key in batch]
        params = {
            "ids": ",".join(ids),
            "tweet.fields": "id,text,note_tweet,created_at,author_id,attachments,public_metrics",
            "expansions": "author_id,attachments.media_keys",
            "user.fields": "id,name,username,profile_image_url",
            "media.fields": "media_key,type,url,preview_image_url,width,height,duration_ms,variants",
        }
        url = "https://api.x.com/2/tweets?" + urllib.parse.urlencode(params)
        try:
            payload, _ = request_json(url, headers={"Authorization": f"Bearer {bearer}"})
        except RuntimeError as exc:
            for source_id in batch:
                mark_transient(catalog, source_id, at, str(exc), getattr(exc, "http_status", None))
            errors.append(str(exc))
            continue
        users = {u["id"]: u for u in (payload.get("includes") or {}).get("users", [])}
        media = {m["media_key"]: m for m in (payload.get("includes") or {}).get("media", [])}
        by_id = {tweet["id"]: tweet for tweet in payload.get("data") or []}
        api_errors = {str(error.get("resource_id")): error for error in payload.get("errors") or []}
        for source_id in batch:
            source = catalog["sources"][source_id]
            tweet = by_id.get(source["native_id"])
            if not tweet:
                state, reason = classify_x_error(api_errors.get(source["native_id"], {}))
                if state:
                    redact_and_retire(catalog, source_id, state, at, reason)
                else:
                    mark_transient(catalog, source_id, at, reason)
                continue
            user = users.get(tweet.get("author_id"), {})
            handle = user.get("username")
            incoming = person(
                "x", handle, at, uid=user.get("id"), name=user.get("name"),
                profile_url=f"https://x.com/{handle}" if handle else None,
                avatar_url=user.get("profile_image_url"),
            )
            actor_key = update_actor(catalog, source, incoming)
            refresh_item_posted_by(catalog, source_id, actor_key)
            if handle:
                source["url"] = f"https://x.com/{handle}/status/{tweet['id']}"
            text = (tweet.get("note_tweet") or {}).get("text") or tweet.get("text") or ""
            metrics = tweet.get("public_metrics") or {}
            observations = x_media_observations(tweet, media, at)
            _cache_text(volatile_cache, source_id, source["url"], text, at)
            _cache_media(volatile_cache, source_id, observations)
            source.update({
                "posted_at": tweet.get("created_at"),
                "posted_date": (tweet.get("created_at") or "")[:10] or source.get("posted_date"),
                "text": _withheld_text(text),
                "media_observations": _sanitized_media(observations),
                "fetch": {
                    "adapter": "x-official-api", "observed_at": at,
                    "raw_sha256": hashlib.sha256(text.encode()).hexdigest(),
                },
            })
            mark_available(catalog, source_id, at, metrics={
                "views": metrics.get("impression_count"), "likes": metrics.get("like_count"),
                "reposts": metrics.get("retweet_count"), "comments": metrics.get("reply_count"),
            })
            ev = evidence(catalog, source_id, "source_post", at)
            source["availability"]["evidence_ids"].append(ev)
            candidate = catalog["candidates"].get(source_id)
            if candidate:
                candidate["checks"].update({
                    "public_visibility": "pass",
                    "target_model": "pass" if model_matches(text, config) else "fail",
                    "has_video": "pass" if source["media_observations"] else "fail",
                    "role_separation": "pass", "delivery_policy": "pass",
                })
            maybe_capture_root_prompt(
                catalog, source_id, at, text=text, volatile_cache=volatile_cache,
            )
            hydrated += 1
            if with_comments:
                try:
                    hydrate_x_comments(
                        catalog, source_id, bearer, at, mode=x_search_mode,
                        max_pages=comment_pages, volatile_cache=volatile_cache,
                    )
                except RuntimeError as exc:
                    errors.append(f"{source_id} comments: {exc}")
    return hydrated, errors


def hydrate_x_comments(catalog: dict, root_source_id: str, bearer: str, at: str,
                       *, mode: str, max_pages: int,
                       volatile_cache: dict | None = None) -> None:
    root = catalog["sources"][root_source_id]
    endpoint = "all" if mode == "all" else "recent"
    token = None
    for _ in range(max_pages):
        params = {
            "query": f"conversation_id:{root['native_id']}", "max_results": "100",
            "tweet.fields": "id,text,note_tweet,created_at,author_id,public_metrics,in_reply_to_user_id",
            "expansions": "author_id", "user.fields": "id,name,username,profile_image_url",
        }
        if token:
            params["next_token"] = token
        url = f"https://api.x.com/2/tweets/search/{endpoint}?" + urllib.parse.urlencode(params)
        payload, _ = request_json(url, headers={"Authorization": f"Bearer {bearer}"})
        users = {u["id"]: u for u in (payload.get("includes") or {}).get("users", [])}
        for tweet in payload.get("data") or []:
            if tweet["id"] == root["native_id"]:
                continue
            text = (tweet.get("note_tweet") or {}).get("text") or tweet.get("text") or ""
            user = users.get(tweet.get("author_id"), {})
            same_poster = user.get("username") and user.get("username") == (
                catalog["actors"].get(root["posted_by_actor_id"], {}).get("handle")
            )
            if not (COMMENT_SIGNAL.search(text) or (same_poster and len(text) >= 80)):
                continue
            handle = user.get("username")
            actor = person(
                "x", handle, at, uid=user.get("id"), name=user.get("name"),
                profile_url=f"https://x.com/{handle}" if handle else None,
                avatar_url=user.get("profile_image_url"),
            )
            metrics = tweet.get("public_metrics") or {}
            comment_id = store_comment(
                catalog, platform="x", native_id=tweet["id"],
                url=f"https://x.com/{handle or 'i'}/status/{tweet['id']}",
                parent_source_id=root_source_id, text=text, actor=actor, at=at,
                posted_at=tweet.get("created_at"),
                metrics={"likes": metrics.get("like_count"), "reposts": metrics.get("retweet_count")},
                volatile_cache=volatile_cache,
            )
            maybe_capture_comment_prompt(
                catalog, root_source_id, comment_id, text=text, at=at,
                volatile_cache=volatile_cache,
            )
        token = (payload.get("meta") or {}).get("next_token")
        if not token:
            break


def reddit_video(post: dict, at: str) -> list[dict]:
    media = (post.get("secure_media") or post.get("media") or {}).get("reddit_video") or {}
    if not media:
        return []
    fallback = media.get("fallback_url")
    return [{
        "source_media_id": f"reddit-media:{post.get('id')}:0", "kind": "video", "position": 0,
        "platform_media_id": post.get("id"), "direct_url": fallback,
        "thumbnail_url": post.get("thumbnail") if str(post.get("thumbnail", "")).startswith("http") else None,
        "width": media.get("width"), "height": media.get("height"),
        "duration_ms": (media.get("duration") or 0) * 1000,
        "variants": [{
            "url": fallback, "width": media.get("width"), "height": media.get("height"),
            "bitrate": None, "container": "mp4", "codec": None,
        }] if fallback else [],
        "observed_at": at, "volatile": True,
    }]


def walk_reddit_comments(children, limit: int):
    count = 0
    stack = list(reversed(children or []))
    while stack and count < limit:
        child = stack.pop()
        if child.get("kind") != "t1":
            continue
        data = child.get("data") or {}
        yield data
        count += 1
        replies = data.get("replies")
        if isinstance(replies, dict):
            nested = ((replies.get("data") or {}).get("children") or [])
            stack.extend(reversed(nested))


def hydrate_reddit(catalog: dict, source_ids: list[str], config: dict, at: str,
                   *, with_comments: bool, comment_limit: int,
                   volatile_cache: dict | None = None) -> tuple[int, list[str]]:
    token = reddit_token()
    hydrated, errors = 0, []
    headers = {"Authorization": f"Bearer {token}"}
    for source_id in source_ids:
        source = catalog["sources"][source_id]
        url = "https://oauth.reddit.com/api/info.json?" + urllib.parse.urlencode({
            "id": source["native_id"], "raw_json": "1"
        })
        try:
            payload, _ = request_json(url, headers=headers)
        except RuntimeError as exc:
            mark_transient(catalog, source_id, at, str(exc), getattr(exc, "http_status", None))
            errors.append(f"{source_id}: {exc}")
            continue
        children = (payload.get("data") or {}).get("children") or []
        if not children:
            mark_transient(catalog, source_id, at, "Reddit API omitted source; awaiting confirmation", 200)
            continue
        post = children[0].get("data") or {}
        if post.get("removed_by_category"):
            redact_and_retire(catalog, source_id, "deleted", at,
                              f"Reddit removed_by_category={post['removed_by_category']}")
            continue
        handle = post.get("author") if post.get("author") not in {None, "[deleted]"} else None
        incoming = person(
            "reddit", handle, at, uid=post.get("author_fullname"), name=handle,
            profile_url=f"https://www.reddit.com/user/{handle}/" if handle else None,
        )
        actor_key = update_actor(catalog, source, incoming)
        refresh_item_posted_by(catalog, source_id, actor_key)
        text = "\n\n".join(value for value in [post.get("title"), post.get("selftext")] if value)
        created = post.get("created_utc")
        posted_at = datetime.fromtimestamp(created, timezone.utc).isoformat().replace("+00:00", "Z") if created else None
        source_url = "https://www.reddit.com" + (post.get("permalink") or "")
        observations = reddit_video(post, at)
        _cache_text(volatile_cache, source_id, source_url, text, at)
        _cache_media(volatile_cache, source_id, observations)
        source.update({
            "url": source_url,
            "community": post.get("subreddit_name_prefixed"), "title": post.get("title"),
            "posted_at": posted_at, "posted_date": posted_at[:10] if posted_at else None,
            "text": _withheld_text(text),
            "media_observations": _sanitized_media(observations),
            "fetch": {
                "adapter": "reddit-official-api", "observed_at": at,
                "raw_sha256": hashlib.sha256(text.encode()).hexdigest(),
            },
        })
        mark_available(catalog, source_id, at, metrics={
            "views": None, "likes": post.get("ups"), "reposts": None,
            "comments": post.get("num_comments"),
        })
        ev = evidence(catalog, source_id, "source_post", at)
        source["availability"]["evidence_ids"].append(ev)
        candidate = catalog["candidates"].get(source_id)
        if candidate:
            candidate["checks"].update({
                "public_visibility": "pass",
                "target_model": "pass" if model_matches(text, config) else "fail",
                "has_video": "pass" if source["media_observations"] else "fail",
                "role_separation": "pass", "delivery_policy": "pass",
            })
        maybe_capture_root_prompt(
            catalog, source_id, at, text=text, volatile_cache=volatile_cache,
        )
        hydrated += 1
        if with_comments:
            comments_url = f"https://oauth.reddit.com/comments/{post['id']}.json?" + urllib.parse.urlencode({
                "raw_json": "1", "limit": str(min(comment_limit, 500)), "depth": "8"
            })
            try:
                listings, _ = request_json(comments_url, headers=headers)
            except RuntimeError as exc:
                errors.append(f"{source_id} comments: {exc}")
                continue
            comment_children = (((listings[1] if len(listings) > 1 else {}).get("data") or {}).get("children") or [])
            root_handle = catalog["actors"].get(source["posted_by_actor_id"], {}).get("handle")
            for comment in walk_reddit_comments(comment_children, comment_limit):
                body = comment.get("body") or ""
                same_poster = comment.get("author") == root_handle
                if not body or not (COMMENT_SIGNAL.search(body) or (same_poster and len(body) >= 80)):
                    continue
                comment_handle = comment.get("author") if comment.get("author") not in {None, "[deleted]"} else None
                actor = person(
                    "reddit", comment_handle, at, uid=comment.get("author_fullname"),
                    name=comment_handle,
                    profile_url=f"https://www.reddit.com/user/{comment_handle}/" if comment_handle else None,
                )
                created = comment.get("created_utc")
                comment_at = datetime.fromtimestamp(created, timezone.utc).isoformat().replace("+00:00", "Z") if created else None
                native = comment.get("name") or f"t1_{comment.get('id')}"
                permalink = comment.get("permalink") or f"/comments/{post['id']}/_/{comment.get('id')}/"
                comment_id = store_comment(
                    catalog, platform="reddit", native_id=native,
                    url="https://www.reddit.com" + permalink,
                    parent_source_id=source_id, text=body, actor=actor, at=at,
                    posted_at=comment_at, metrics={"likes": comment.get("ups")},
                    volatile_cache=volatile_cache,
                )
                maybe_capture_comment_prompt(
                    catalog, source_id, comment_id, text=body, at=at,
                    volatile_cache=volatile_cache,
                )
    return hydrated, errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=["x", "reddit", "all"], default="all")
    parser.add_argument("--with-comments", action="store_true")
    parser.add_argument("--x-comment-mode", choices=["recent", "all"], default="recent")
    parser.add_argument("--comment-pages", type=int, default=3)
    parser.add_argument("--comment-limit", type=int, default=200)
    parser.add_argument("--pending-only", action="store_true")
    parser.add_argument(
        "--sanitize-only", action="store_true",
        help="migrate raw catalog values to the private cache without API calls",
    )
    parser.add_argument(
        "--discard-volatile", action="store_true",
        help="explicitly delete raw values instead of preserving them in a private cache",
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--volatile-cache", type=Path,
        help="gitignored private cache for raw text and volatile media locators",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    catalog = read_json(ROOT / "data" / "catalog.json")
    config = read_json(ROOT / "config" / "collection.json")
    at = utc_now()
    ensure_automation(catalog, at)
    cache_path = None
    volatile_cache = None
    if args.volatile_cache:
        try:
            cache_path = volatile_cache_path(args.volatile_cache)
            volatile_cache = initialize_volatile_cache(
                catalog, at, read_json(cache_path, {}),
            )
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
    try:
        scrub_volatile_catalog(
            catalog, volatile_cache, at, discard=args.discard_volatile,
        )
    except ValueError as exc:
        parser.error(str(exc))
    selected = [args.platform] if args.platform != "all" else ["x", "reddit"]
    total, all_errors = 0, []
    for platform in ([] if args.sanitize_only else selected):
        source_ids = [
            source_id for source_id, source in catalog["sources"].items()
            if source.get("platform") == platform and source.get("kind") == "post"
            and (source.get("availability") or {}).get("state") not in {"deleted", "private", "suspended"}
            and (
                not args.pending_only
                or (catalog.get("candidates", {}).get(source_id, {}).get("review") or {}).get("state") == "pending"
                or source_needs_mirror_locator(catalog, source_id)
            )
        ]
        if args.limit:
            source_ids = source_ids[:args.limit]
        if not source_ids:
            continue
        if volatile_cache is None and not args.discard_volatile:
            parser.error(
                "hydration would observe raw text/media; pass --volatile-cache or "
                "--discard-volatile explicitly"
            )
        if platform == "x":
            count, errors = hydrate_x(
                catalog, source_ids, config, at, with_comments=args.with_comments,
                x_search_mode=args.x_comment_mode, comment_pages=args.comment_pages,
                volatile_cache=volatile_cache,
            )
        else:
            count, errors = hydrate_reddit(
                catalog, source_ids, config, at, with_comments=args.with_comments,
                comment_limit=args.comment_limit, volatile_cache=volatile_cache,
            )
        total += count
        all_errors.extend(errors)
        print(f"{platform}: hydrated {count}/{len(source_ids)} sources")
    catalog["updated_at"] = at
    retirement_path = ROOT / "data" / "media-retirement.json"
    current_retirement = read_json(retirement_path, {})
    retirement = refresh_retirement_manifest(
        catalog, current_retirement, at,
        note="Hydration confirmed a source removal; remote mirror cleanup requires confirmation.",
    )
    errors = validate_catalog(catalog)
    if errors:
        raise SystemExit("hydration produced invalid catalog:\n" + "\n".join(f"- {e}" for e in errors))
    if not args.dry_run:
        if cache_path is not None:
            write_volatile_cache(cache_path, volatile_cache)
        write_json(ROOT / "data" / "catalog.json", catalog)
        write_json(ROOT / "data" / "posts.json", export_posts(catalog))
        write_json(
            ROOT / "data" / "github-attachments.json",
            build_github_attachment_manifest(catalog, at),
        )
        write_json(ROOT / "data" / "r2-mirrors.json", build_r2_manifest(catalog, at))
        if retirement != current_retirement:
            write_json(retirement_path, retirement)
    print(f"hydrated {total} sources; {len(all_errors)} non-fatal request errors")
    if args.dry_run:
        print("dry run: no files changed")
    if all_errors:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
