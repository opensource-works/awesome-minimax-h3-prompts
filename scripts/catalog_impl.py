#!/usr/bin/env python3
"""Canonical catalog-v2 helpers shared by collection scripts.

``data/catalog.json`` is authoritative.  The old post array remains a generated
compatibility export for one release cycle; curation and rights must never be
written back into that lossy projection.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


SCHEMA_VERSION = "2.0.0"
PLATFORMS = {"x", "reddit"}
AVAILABILITY = {
    "available", "deleted", "private", "suspended", "geo_restricted",
    "age_restricted", "unknown", "transient_error",
}
CANDIDATE_STATES = {"pending", "included", "excluded", "removed"}
CURATION_STATES = {"pending", "approved", "changes_requested", "removed"}
ATTRIBUTION_STATES = {"confirmed", "claimed", "inferred", "unknown", "disputed"}
PROMPT_STATES = {
    "verbatim", "partial", "referenced_not_captured", "not_provided",
    "unavailable", "removed",
}
RIGHTS_STATES = {
    "unknown", "not_requested", "requested", "granted", "denied",
    "revoked", "public_license",
}
RIGHTS_EVIDENCE_KINDS = {
    "granted": "permission",
    "public_license": "public_license",
}
DELIVERY_MODES = {"source_link", "official_embed", "authorized_mirror"}
MIRROR_STATES = {"active", "quarantined", "pending_delete", "deleted"}
REASON_CODES = {
    "meets_scope", "not_target_model", "no_video", "duplicate_media",
    "repost_without_credit", "original_source_unverified",
    "outside_time_window", "unfetchable_transient", "source_deleted",
    "source_private", "rights_denied", "permission_revoked",
    "creator_takedown", "unsafe_or_illegal", "legacy_drop_reason_unknown",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: str | Path, default=None):
    path = Path(path)
    if not path.exists():
        return copy.deepcopy(default)
    return json.loads(path.read_text())


def write_json(path: str | Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def mirror_needs_cleanup(mirror: dict) -> bool:
    """Distinguish retired mirrors from authorized uploads awaiting verification."""
    state = mirror.get("state")
    return state == "pending_delete" or (
        state == "quarantined" and not mirror.get("staged_filename")
    )


def refresh_retirement_manifest(catalog: dict, existing: dict | None, at: str,
                                note: str | None = None) -> dict:
    """Add newly retired mirrors to the URL-redacted remote-cleanup ledger."""
    output = copy.deepcopy(existing or {
        "schema_version": "1.0.0", "generated_at": at,
        "policy": "Retired mirrors are removed from public projections and queued for confirmed cleanup.",
        "entries": [],
    })
    output.setdefault("schema_version", "1.0.0")
    output.setdefault("generated_at", at)
    output.setdefault(
        "policy", "Retired mirrors are removed from public projections and queued for confirmed cleanup."
    )
    entries = output.setdefault("entries", [])
    indexed = {entry.get("mirror_id"): entry for entry in entries}
    changed = False
    for item_key, item in (catalog.get("items") or {}).items():
        for media in item.get("media") or []:
            for mirror in (media.get("delivery") or {}).get("mirrors") or []:
                if not mirror_needs_cleanup(mirror) and mirror.get("state") != "deleted":
                    continue
                mirror_id = mirror.get("mirror_id")
                if not mirror_id:
                    continue
                desired_state = "deleted" if mirror.get("state") == "deleted" else "pending_delete"
                url = mirror.get("url")
                record = indexed.get(mirror_id)
                raw_url_digest = hashlib.sha256(url.encode()).hexdigest() if url else None
                url_digest = (
                    (record or {}).get("url_sha256")
                    or mirror.get("former_url_sha256")
                    or raw_url_digest
                )
                if url_digest and mirror.get("former_url_sha256") != url_digest:
                    mirror["former_url_sha256"] = url_digest
                    changed = True
                if mirror.get("url") is not None:
                    mirror["url"] = None
                    changed = True
                if record is None:
                    record = {
                        "mirror_id": mirror_id,
                        "provider": mirror.get("provider"),
                        "artifact": mirror.get("artifact"),
                        "url_sha256": url_digest,
                        "state": desired_state,
                        "note": note or "Removed from public projections; remote cleanup requires confirmation.",
                        "source_id": media.get("source_id"),
                        "item_id": item_key,
                        "media_id": media.get("media_id"),
                        "queued_at": at,
                    }
                    entries.append(record)
                    indexed[mirror_id] = record
                    changed = True
                elif record.get("state") != desired_state or (
                    url_digest and not record.get("url_sha256")
                ):
                    record["state"] = desired_state
                    if url_digest:
                        record["url_sha256"] = url_digest
                    changed = True
    before_order = [entry.get("mirror_id") for entry in entries]
    entries.sort(key=lambda entry: entry.get("mirror_id") or "")
    changed = changed or before_order != [entry.get("mirror_id") for entry in entries]
    if changed:
        output["updated_at"] = at
    return output


def platform_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    if host in {"x.com", "twitter.com", "mobile.twitter.com"}:
        return "x"
    if host == "redd.it" or host == "reddit.com" or host.endswith(".reddit.com"):
        return "reddit"
    raise ValueError(f"unsupported source host: {host or url}")


def source_identity(url: str) -> tuple[str, str, str]:
    """Return platform, native ID and stable catalog source ID."""
    platform = platform_from_url(url)
    if platform == "x":
        match = re.search(r"/(?:status|statuses)/(\d+)", url)
        if not match:
            raise ValueError(f"not an X status URL: {url}")
        native = match.group(1)
        return platform, native, f"x:{native}"
    match = re.search(r"/(?:comments|gallery)/(\w+)", url)
    if not match:
        match = re.search(r"redd\.it/(\w+)", url)
    if not match:
        raise ValueError(f"not a Reddit submission URL: {url}")
    native = f"t3_{match.group(1)}"
    return platform, native, f"reddit:{native}"


def actor_id(platform: str, *, platform_user_id=None, handle=None) -> str:
    token = str(platform_user_id or handle or "unknown").lower()
    safe = re.sub(r"[^a-z0-9_.-]+", "-", token).strip("-") or "unknown"
    if len(safe) > 60:
        safe = hashlib.sha256(token.encode()).hexdigest()[:20]
    return f"act_{platform}_{safe}"


def item_id(source_id: str, position: int = 0) -> str:
    return "itm_" + source_id.replace(":", "_") + f"_{position}"


def evidence_id(kind: str, source_id: str) -> str:
    safe = source_id.replace(":", "_")
    return f"ev_{kind}_{safe}"


def media_observation(source: dict, source_media_id: str | None) -> dict | None:
    media = source.get("media_observations") or []
    if source_media_id:
        for observation in media:
            if observation.get("source_media_id") == source_media_id:
                return observation
        return None
    return media[0] if media else None


def _parse_rfc3339(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _valid_expiry(rights: dict, now: str | None = None) -> bool:
    expiry = rights.get("expires_at")
    if not expiry:
        return True
    parsed_expiry = _parse_rfc3339(expiry)
    parsed_now = _parse_rfc3339(now or utc_now())
    return bool(parsed_expiry and parsed_now and parsed_expiry > parsed_now)


def _string_set(value) -> set[str]:
    return (
        {part for part in value if isinstance(part, str) and part}
        if isinstance(value, (list, tuple, set)) else set()
    )


def rights_evidence_applies(item: dict, rights: dict, record: dict,
                            required_scopes=None) -> bool:
    """Return whether one evidence record supports this exact rights claim.

    A generic public source is never permission. Positive rights evidence must
    use a semantic evidence kind and bind the grantor, catalog item, scopes,
    and (for a public license) the exact SPDX identifier in ``rights_assertion``.
    """
    status = rights.get("status")
    expected_kind = RIGHTS_EVIDENCE_KINDS.get(status)
    if not expected_kind or record.get("kind") != expected_kind:
        return False
    assertion = record.get("rights_assertion")
    if not isinstance(assertion, dict):
        return False
    item_key = item.get("id")
    if not item_key or item_key not in _string_set(assertion.get("asset_item_ids")):
        return False
    grantors = _string_set(rights.get("grantor_actor_ids"))
    asserted_grantors = _string_set(assertion.get("grantor_actor_ids"))
    if not grantors or not grantors <= asserted_grantors:
        return False
    scopes = _string_set(required_scopes if required_scopes is not None
                         else rights.get("granted_scopes"))
    if not scopes or not scopes <= _string_set(assertion.get("granted_scopes")):
        return False
    if status == "public_license" and (
        not rights.get("license_spdx")
        or assertion.get("license_spdx") != rights.get("license_spdx")
    ):
        return False
    return True


def mirror_is_authorized(item: dict, mirror: dict, catalog: dict, now: str | None = None) -> bool:
    rights = (item.get("rights") or {}).get("video_republication") or {}
    status = rights.get("status")
    if status not in {"granted", "public_license"}:
        return False
    if status == "granted" and not (
        rights.get("grantor_actor_ids") and rights.get("granted_at")
    ):
        return False
    if status == "public_license" and not rights.get("license_spdx"):
        return False
    if not _valid_expiry(rights, now):
        return False
    evidence = rights.get("evidence_ids") or []
    if not evidence or any(e not in catalog.get("evidence", {}) for e in evidence):
        return False
    scopes = _string_set(rights.get("granted_scopes"))
    needed = {"download"}
    provider = mirror.get("provider")
    if provider == "r2":
        needed.add("mirror_r2")
    elif provider == "github_attachment":
        needed.add("mirror_github")
    else:
        return False
    if mirror.get("artifact") == "animated_preview":
        needed.add("derive_preview")
    permission_evidence = _string_set(mirror.get("permission_evidence_ids"))
    evidence_records = catalog.get("evidence", {})
    return (
        needed <= scopes
        and bool(permission_evidence)
        and permission_evidence <= set(evidence)
        and all(
            rights_evidence_applies(item, rights, evidence_records.get(evidence_id) or {}, scopes)
            for evidence_id in permission_evidence
        )
    )


def item_is_public(item: dict, catalog: dict) -> bool:
    if (item.get("curation") or {}).get("status") != "approved":
        return False
    source_id = item.get("canonical_source_id")
    source = catalog.get("sources", {}).get(source_id) or {}
    availability = source.get("availability") or {}
    state = availability.get("state")
    # A timeout/rate-limit is not a deletion signal. Keep the last known
    # public source visible until an authoritative unavailable state is
    # confirmed; imported never-checked sources still remain private.
    if state != "available" and not (
        state in {"unknown", "transient_error"}
        and availability.get("last_available_at")
        and not availability.get("confirmed_at")
    ):
        return False
    candidate = catalog.get("candidates", {}).get(source_id) or {}
    review = candidate.get("review") or {}
    return review.get("state") == "included" and review.get("item_id") in {None, item.get("id")}


def _actor_public(actor: dict | None) -> dict:
    actor = actor or {}
    return {
        "name": actor.get("display_name") or actor.get("handle") or "Unknown poster",
        "handle": actor.get("handle") or "unknown",
        "url": actor.get("profile_url"),
        "avatar": actor.get("avatar_url"),
    }


def _role_public(role: dict | None, actors: dict) -> dict:
    role = role or {"status": "unknown", "actor_id": None}
    actor = actors.get(role.get("actor_id")) if role.get("actor_id") else None
    return {"status": role.get("status", "unknown"), "person": _actor_public(actor) if actor else None}


def export_posts(catalog: dict) -> list[dict]:
    """Generate the legacy-shaped public array without volatile media URLs."""
    actors = catalog.get("actors", {})
    output = []
    for key, item in catalog.get("items", {}).items():
        if not item_is_public(item, catalog):
            continue
        source = catalog["sources"][item["canonical_source_id"]]
        poster = _actor_public(actors.get(source.get("posted_by_actor_id")))
        media = (item.get("media") or [{}])[0]
        observation_source_id = media.get("source_id") or item["canonical_source_id"]
        observation_source = (catalog.get("sources") or {}).get(observation_source_id) or {}
        observation = media_observation(observation_source, media.get("source_media_id")) or {}
        mirrors = [
            m for m in ((media.get("delivery") or {}).get("mirrors") or [])
            if m.get("state") == "active" and mirror_is_authorized(item, m, catalog)
        ]
        video_mirror = next((m for m in mirrors if m.get("artifact") == "video" and m.get("provider") == "r2"), None)
        preview = next((m for m in mirrors if m.get("artifact") == "animated_preview"), None)
        attachment = next((m for m in mirrors if m.get("artifact") == "video" and m.get("provider") == "github_attachment"), None)
        prompt = item.get("prompt") or {}
        prompt_text = prompt.get("text") if prompt.get("status") in {"verbatim", "partial"} else None
        prompt_rights = ((item.get("rights") or {}).get("prompt_republication") or {})
        if prompt_rights.get("status") in {"denied", "revoked"} or not _valid_expiry(prompt_rights):
            prompt_text = None
        stats = source.get("metrics") or {}
        display = item.get("display") or {}
        category = (display.get("category") or {}).get("label") or (display.get("category") or {}).get("id") or "Showcase"
        title = (display.get("title") or {}).get("text") or "Untitled"
        original = ((item.get("attribution") or {}).get("original_video_creators") or [{}])[0]
        prompt_author = ((item.get("attribution") or {}).get("prompt_authors") or [{}])[0]
        output.append({
            "id": source["native_id"].removeprefix("t3_").removeprefix("t1_"),
            "entry_id": source["id"],
            "item_id": key,
            "schema_version": SCHEMA_VERSION,
            "platform": source["platform"],
            "url": source["url"],
            "title": title,
            "text": (source.get("text") or {}).get("value") or "",
            "prompt": prompt_text,
            "prompt_in_thread": prompt.get("status") == "referenced_not_captured",
            "prompt_source_url": prompt.get("source_url"),
            "model": " ".join(filter(None, [item.get("model", {}).get("family"), item.get("model", {}).get("version")])),
            "category": category,
            "date": source.get("posted_date") or "",
            "author": poster,
            "roles": {
                "poster": poster,
                "original_video_creator": _role_public(original, actors),
                "prompt_author": _role_public(prompt_author, actors),
            },
            "annotations": copy.deepcopy(item.get("annotations") or []),
            "video": {
                "url": video_mirror.get("url") if video_mirror else None,
                "thumbnail": preview.get("url") if preview else None,
                "width": observation.get("width"),
                "height": observation.get("height"),
                "duration": (observation.get("duration_ms") or 0) / 1000,
                "formats": [],
                "attachment": attachment.get("url") if attachment else None,
                "source_url": None,
                "media_mode": "authorized_mirror" if mirrors else (media.get("delivery") or {}).get("mode", "source_link"),
            },
            "stats": {
                "views": stats.get("views"), "likes": stats.get("likes"),
                "retweets": stats.get("reposts"), "comments": stats.get("comments"),
            },
            "rights": copy.deepcopy(item.get("rights") or {}),
            "review": copy.deepcopy(item.get("curation") or {}),
        })
    output.sort(key=lambda p: (-(p["stats"].get("views") or 0), p["entry_id"]))
    return output


def _reference_ids(value, singular: str, plural: str) -> set[str]:
    """Collect graph references such as source_id/source_ids recursively."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key == singular or key.endswith("_" + singular):
                if isinstance(child, str) and child:
                    found.add(child)
            elif key == plural or key.endswith("_" + plural):
                if isinstance(child, list):
                    found.update(part for part in child if isinstance(part, str) and part)
            found.update(_reference_ids(child, singular, plural))
    elif isinstance(value, list):
        for child in value:
            found.update(_reference_ids(child, singular, plural))
    return found


def _filter_evidence_refs(value, allowed: set[str]) -> None:
    """Remove non-public evidence references from an already-copied value."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "evidence_ids" or key.endswith("_evidence_ids"):
                if isinstance(child, list):
                    value[key] = [part for part in child if part in allowed]
            else:
                _filter_evidence_refs(child, allowed)
    elif isinstance(value, list):
        for child in value:
            _filter_evidence_refs(child, allowed)


def _downgrade_unverifiable_public_claims(item: dict) -> None:
    """Do not export a positive claim after its non-public evidence is stripped."""
    model = item.get("model") or {}
    if model.get("verification") != "unverified" and not model.get("evidence_ids"):
        model["verification"] = "unverified"
    attribution = item.get("attribution") or {}
    for role_name in ("original_video_creators", "prompt_authors"):
        for role in attribution.get(role_name) or []:
            if role.get("status") != "unknown" and not role.get("evidence_ids"):
                role.update({"status": "unknown", "actor_id": None, "evidence_ids": []})
    prompt = item.get("prompt") or {}
    prompt_rights = ((item.get("rights") or {}).get("prompt_republication") or {})
    if prompt_rights.get("status") in {"denied", "revoked"} or not _valid_expiry(prompt_rights):
        prompt.update({
            "status": "removed", "text": None, "source_url": None,
            "capture_method": "none", "is_verbatim": False,
        })
    if prompt.get("status") in {"verbatim", "partial"} and not prompt.get("evidence_ids"):
        prompt.update({
            "status": "referenced_not_captured", "text": None,
            "capture_method": "none", "is_verbatim": False,
        })
    for rights in (item.get("rights") or {}).values():
        if rights.get("status") in {"granted", "public_license"} and not _valid_expiry(rights):
            rights.update({
                "status": "revoked", "license_spdx": None, "granted_scopes": [],
                "grantor_actor_ids": [], "granted_at": None,
            })
        if rights.get("status") in {"granted", "public_license"} and not rights.get("evidence_ids"):
            rights.update({
                "status": "unknown", "license_spdx": None, "granted_scopes": [],
                "grantor_actor_ids": [], "granted_at": None, "expires_at": None,
            })
    item["annotations"] = [
        annotation for annotation in item.get("annotations") or []
        if annotation.get("kind") != "editorial_note" or annotation.get("evidence_ids")
    ]


def public_catalog(catalog: dict) -> dict:
    """Return a closed public graph without candidates or unsafe media."""
    out = copy.deepcopy(catalog)
    public_items = {k: v for k, v in out.get("items", {}).items() if item_is_public(v, catalog)}
    public_candidates = {
        key: value for key, value in out.get("candidates", {}).items()
        if key in {item.get("canonical_source_id") for item in public_items.values()}
        and (value.get("review") or {}).get("state") == "included"
    }

    source_ids = _reference_ids(public_items, "source_id", "source_ids") | set(public_candidates)
    evidence_ids = (
        _reference_ids(out.get("collection") or {}, "evidence_id", "evidence_ids")
        | _reference_ids(public_items, "evidence_id", "evidence_ids")
        | _reference_ids(public_candidates, "evidence_id", "evidence_ids")
    )
    public_evidence_ids: set[str] = set()

    # Sources and evidence point to each other. Walk to a fixed point so model
    # release sources, prompt comments, availability checks, and their parents
    # remain independently verifiable without retaining unrelated candidates.
    changed = True
    while changed:
        before = (len(source_ids), len(evidence_ids), len(public_evidence_ids))
        for source_id in list(source_ids):
            source = out.get("sources", {}).get(source_id) or {}
            parent = source.get("parent_source_id")
            if parent:
                source_ids.add(parent)
            evidence_ids.update(_reference_ids(source, "evidence_id", "evidence_ids"))
        for evidence_key in list(evidence_ids):
            record = out.get("evidence", {}).get(evidence_key) or {}
            if record.get("visibility") != "public":
                continue
            public_evidence_ids.add(evidence_key)
            if record.get("source_id"):
                source_ids.add(record["source_id"])
            evidence_ids.update(_reference_ids(record, "evidence_id", "evidence_ids"))
        changed = before != (len(source_ids), len(evidence_ids), len(public_evidence_ids))

    _filter_evidence_refs(out["collection"], public_evidence_ids)
    _filter_evidence_refs(public_items, public_evidence_ids)
    _filter_evidence_refs(public_candidates, public_evidence_ids)
    redacted_prompt_source_ids: set[str] = set()
    for item in public_items.values():
        _downgrade_unverifiable_public_claims(item)
        prompt_rights = ((item.get("rights") or {}).get("prompt_republication") or {})
        if prompt_rights.get("status") in {"denied", "revoked"}:
            prompt_source_id = (item.get("prompt") or {}).get("source_id")
            if prompt_source_id:
                redacted_prompt_source_ids.add(prompt_source_id)
        for media in item.get("media") or []:
            delivery = media.get("delivery") or {}
            delivery["mirrors"] = [
                mirror for mirror in delivery.get("mirrors") or []
                if mirror.get("state") == "active"
                and mirror_is_authorized(item, mirror, out)
                and set(mirror.get("permission_evidence_ids") or []) <= public_evidence_ids
            ]
            if not delivery["mirrors"] and delivery.get("mode") == "authorized_mirror":
                delivery["mode"] = "source_link"

    public_sources = {
        key: value for key, value in out.get("sources", {}).items() if key in source_ids
    }
    _filter_evidence_refs(public_sources, public_evidence_ids)
    for source in public_sources.values():
        if source.get("id") in redacted_prompt_source_ids:
            language = (source.get("text") or {}).get("language")
            source["text"] = {"status": "redacted", "value": None, "language": language}
        for observation in source.get("media_observations") or []:
            observation["direct_url"] = None
            observation["thumbnail_url"] = None
            observation["variants"] = []

    public_evidence = {
        key: value for key, value in out.get("evidence", {}).items()
        if key in public_evidence_ids
    }
    _filter_evidence_refs(public_evidence, public_evidence_ids)
    actor_ids = (
        _reference_ids(out.get("collection") or {}, "actor_id", "actor_ids")
        | _reference_ids(public_items, "actor_id", "actor_ids")
        | _reference_ids(public_candidates, "actor_id", "actor_ids")
        | _reference_ids(public_sources, "actor_id", "actor_ids")
        | _reference_ids(public_evidence, "actor_id", "actor_ids")
    )

    out["items"] = public_items
    out["sources"] = public_sources
    out["actors"] = {k: v for k, v in out.get("actors", {}).items() if k in actor_ids}
    out["evidence"] = public_evidence
    out["candidates"] = public_candidates
    return out


def _need(errors: list[str], condition: bool, message: str) -> None:
    if not condition:
        errors.append(message)


def validate_catalog(catalog: dict) -> list[str]:
    errors: list[str] = []
    _need(errors, catalog.get("schema_version") == SCHEMA_VERSION, f"schema_version must be {SCHEMA_VERSION}")
    actors = catalog.get("actors") or {}
    sources = catalog.get("sources") or {}
    evidence = catalog.get("evidence") or {}
    items = catalog.get("items") or {}
    candidates = catalog.get("candidates") or {}

    for index, model in enumerate((catalog.get("collection") or {}).get("model_scope") or []):
        for ref in model.get("evidence_ids") or []:
            _need(errors, ref in evidence, f"collection.model_scope[{index}] evidence {ref} does not resolve")

    for source_id, source in sources.items():
        tag = f"sources.{source_id}"
        _need(errors, source.get("id") == source_id, f"{tag}.id must equal object key")
        platform = source.get("platform")
        _need(errors, platform in PLATFORMS, f"{tag}.platform is invalid")
        native = source.get("native_id")
        expected = f"{platform}:{native}" if platform and native else None
        _need(errors, source_id == expected, f"{tag} is not a canonical source id")
        _need(errors, str(source.get("url", "")).startswith("https://"), f"{tag}.url must be HTTPS")
        try:
            _need(errors, platform_from_url(source.get("url", "")) == platform, f"{tag}.url platform mismatch")
        except ValueError:
            errors.append(f"{tag}.url is not an X/Reddit URL")
        _need(errors, source.get("posted_by_actor_id") in actors, f"{tag}.posted_by_actor_id does not resolve")
        availability = source.get("availability") or {}
        _need(errors, availability.get("state") in AVAILABILITY, f"{tag}.availability.state is invalid")
        _need(errors, bool(availability.get("checked_at")), f"{tag}.availability.checked_at is required")

    for evidence_key, record in evidence.items():
        tag = f"evidence.{evidence_key}"
        _need(errors, record.get("id") == evidence_key, f"{tag}.id must equal object key")
        if record.get("source_id"):
            _need(errors, record["source_id"] in sources, f"{tag}.source_id does not resolve")
        if record.get("captured_by_actor_id"):
            _need(errors, record["captured_by_actor_id"] in actors, f"{tag}.captured_by_actor_id does not resolve")
        if record.get("kind") in set(RIGHTS_EVIDENCE_KINDS.values()):
            assertion = record.get("rights_assertion")
            _need(errors, isinstance(assertion, dict), f"{tag}.rights_assertion is required")
            assertion = assertion if isinstance(assertion, dict) else {}
            asset_item_ids = assertion.get("asset_item_ids") or []
            grantor_actor_ids = assertion.get("grantor_actor_ids") or []
            granted_scopes = assertion.get("granted_scopes") or []
            _need(errors, isinstance(asset_item_ids, list), f"{tag}.rights_assertion.asset_item_ids must be a list")
            _need(errors, isinstance(grantor_actor_ids, list), f"{tag}.rights_assertion.grantor_actor_ids must be a list")
            _need(errors, isinstance(granted_scopes, list), f"{tag}.rights_assertion.granted_scopes must be a list")
            asset_item_ids = asset_item_ids if isinstance(asset_item_ids, list) else []
            grantor_actor_ids = grantor_actor_ids if isinstance(grantor_actor_ids, list) else []
            granted_scopes = granted_scopes if isinstance(granted_scopes, list) else []
            _need(errors, bool(asset_item_ids), f"{tag}.rights_assertion needs asset_item_ids")
            _need(errors, all(value in items for value in asset_item_ids), f"{tag}.rights_assertion asset does not resolve")
            _need(errors, bool(grantor_actor_ids), f"{tag}.rights_assertion needs grantor_actor_ids")
            _need(errors, all(value in actors for value in grantor_actor_ids), f"{tag}.rights_assertion grantor does not resolve")
            _need(errors, bool(granted_scopes) and all(isinstance(value, str) and value for value in granted_scopes),
                  f"{tag}.rights_assertion needs granted_scopes")
            if record.get("kind") == "public_license":
                _need(errors, bool(assertion.get("license_spdx")), f"{tag}.rights_assertion needs license_spdx")

    for key, item in items.items():
        tag = f"items.{key}"
        _need(errors, item.get("id") == key, f"{tag}.id must equal object key")
        source_ids = item.get("source_ids") or []
        canonical = item.get("canonical_source_id")
        _need(errors, canonical in source_ids, f"{tag}.canonical_source_id must be in source_ids")
        for source_id in source_ids:
            _need(errors, source_id in sources, f"{tag}.source_ids contains missing {source_id}")
        curation = item.get("curation") or {}
        _need(errors, curation.get("status") in CURATION_STATES, f"{tag}.curation.status is invalid")
        if curation.get("status") in {"approved", "removed"}:
            _need(errors, curation.get("reviewer_actor_id") in actors, f"{tag}.curation reviewer is required")
            _need(errors, bool(curation.get("reviewed_at")), f"{tag}.curation reviewed_at is required")
        model = item.get("model") or {}
        if model.get("verification") != "unverified":
            _need(errors, bool(model.get("evidence_ids")), f"{tag}.model claim needs evidence")
        attribution = item.get("attribution") or {}
        posted_by = attribution.get("posted_by") or []
        _need(errors, bool(posted_by), f"{tag}.attribution.posted_by is required")
        for post_role in posted_by:
            _need(errors, post_role.get("source_id") in source_ids, f"{tag}.posted_by source is missing")
            _need(errors, post_role.get("actor_id") in actors, f"{tag}.posted_by actor is missing")
        for role_name in ("original_video_creators", "prompt_authors"):
            for role in attribution.get(role_name) or []:
                state = role.get("status")
                _need(errors, state in ATTRIBUTION_STATES, f"{tag}.{role_name} status is invalid")
                if role.get("actor_id"):
                    _need(errors, role["actor_id"] in actors, f"{tag}.{role_name} actor is missing")
                if state != "unknown":
                    _need(errors, bool(role.get("evidence_ids")), f"{tag}.{role_name} claim needs evidence")
        prompt = item.get("prompt") or {}
        pstatus = prompt.get("status")
        _need(errors, pstatus in PROMPT_STATES, f"{tag}.prompt.status is invalid")
        if pstatus in {"verbatim", "partial"}:
            _need(errors, bool(prompt.get("text")), f"{tag}.prompt text is required")
            _need(errors, prompt.get("source_id") in sources, f"{tag}.prompt source is missing")
            _need(errors, bool(prompt.get("source_url")), f"{tag}.prompt source_url is required")
            _need(errors, bool(prompt.get("evidence_ids")), f"{tag}.prompt evidence is required")
        if pstatus == "referenced_not_captured":
            _need(errors, prompt.get("text") is None, f"{tag}.referenced prompt text must be null")
        for annotation in item.get("annotations") or []:
            if annotation.get("kind") == "community_comment":
                sid = annotation.get("source_id")
                _need(errors, sid in sources and (sources.get(sid) or {}).get("kind") == "comment", f"{tag} community annotation needs comment source")
                _need(errors, annotation.get("author_actor_id") == (sources.get(sid) or {}).get("posted_by_actor_id"), f"{tag} community annotation author mismatch")
            elif annotation.get("kind") == "editorial_note":
                _need(errors, annotation.get("author_actor_id") in actors, f"{tag} editorial annotation needs author")
                _need(errors, bool(annotation.get("evidence_ids")), f"{tag} editorial annotation needs review evidence")
            else:
                errors.append(f"{tag} annotation kind is invalid")
        for right_name, rights in (item.get("rights") or {}).items():
            right_status = rights.get("status")
            _need(errors, right_status in RIGHTS_STATES, f"{tag}.rights.{right_name}.status is invalid")
            if right_status in {"granted", "public_license"}:
                _need(errors, bool(rights.get("evidence_ids")), f"{tag}.rights.{right_name} needs evidence")
                _need(errors, bool(rights.get("grantor_actor_ids")), f"{tag}.rights.{right_name} needs a grantor")
                _need(errors, bool(rights.get("granted_scopes")), f"{tag}.rights.{right_name} needs granted_scopes")
                semantic_evidence = [
                    evidence.get(evidence_id) or {}
                    for evidence_id in rights.get("evidence_ids") or []
                ]
                _need(
                    errors,
                    any(rights_evidence_applies(item, rights, record) for record in semantic_evidence),
                    f"{tag}.rights.{right_name} needs semantically bound permission evidence",
                )
            if right_status == "granted":
                _need(errors, bool(rights.get("grantor_actor_ids")), f"{tag}.rights.{right_name} needs a grantor")
                _need(errors, bool(rights.get("granted_at")), f"{tag}.rights.{right_name} needs granted_at")
                if rights.get("granted_at"):
                    _need(errors, _parse_rfc3339(rights["granted_at"]) is not None, f"{tag}.rights.{right_name}.granted_at is invalid")
            if right_status == "public_license":
                _need(errors, bool(rights.get("license_spdx")), f"{tag}.rights.{right_name} needs license_spdx")
            if rights.get("expires_at"):
                _need(errors, _parse_rfc3339(rights["expires_at"]) is not None, f"{tag}.rights.{right_name}.expires_at is invalid")
        for media in item.get("media") or []:
            delivery = media.get("delivery") or {}
            _need(errors, delivery.get("mode") in DELIVERY_MODES, f"{tag} delivery mode is invalid")
            for mirror in delivery.get("mirrors") or []:
                _need(errors, mirror.get("state") in MIRROR_STATES, f"{tag} mirror state is invalid")
                if mirror.get("state") == "active":
                    _need(errors, mirror_is_authorized(item, mirror, catalog), f"{tag} has unauthorized active mirror")

    for source_id, candidate in candidates.items():
        tag = f"candidates.{source_id}"
        _need(errors, source_id in sources, f"{tag} source does not resolve")
        review = candidate.get("review") or {}
        state = review.get("state")
        _need(errors, state in CANDIDATE_STATES, f"{tag}.review.state is invalid")
        for code in review.get("reason_codes") or []:
            _need(errors, code in REASON_CODES, f"{tag} has invalid reason code {code}")
        if state == "included":
            item_key = review.get("item_id")
            _need(errors, item_key in items, f"{tag} included candidate needs item")
            _need(errors, (items.get(item_key, {}).get("curation") or {}).get("status") == "approved", f"{tag} included item must be approved")
        if state in {"excluded", "removed"}:
            _need(errors, bool(review.get("reason_codes")), f"{tag} decision needs reason")
            _need(errors, review.get("reviewer_actor_id") in actors, f"{tag} decision needs reviewer")
            _need(errors, bool(review.get("reviewed_at")), f"{tag} decision needs reviewed_at")
        if "duplicate_media" in (review.get("reason_codes") or []):
            _need(errors, review.get("duplicate_of_item_id") in items, f"{tag} duplicate target is missing")
    return errors
