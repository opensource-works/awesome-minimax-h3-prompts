#!/usr/bin/env python3
"""Human review CLI for ``data/catalog.json``.

Review decisions are deliberately explicit: every mutation requires reason
codes, existing evidence records, an existing actor, and an RFC3339 timestamp.
Including a candidate records only the human curation decision.  It never
infers an original creator, prompt author, or republication permission.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from catalog import (  # noqa: E402
    CANDIDATE_STATES,
    REASON_CODES,
    item_id,
    read_json,
    refresh_retirement_manifest,
    source_identity,
    validate_catalog,
    write_json,
)
from authorized_manifests import (  # noqa: E402
    build_github_attachment_manifest,
    build_r2_manifest,
)

DEFAULT_CATALOG = ROOT / "data" / "catalog.json"
VOLATILE_CACHE_VERSION = "volatile-media-cache-v1"
CONTENT_REDACTION_REASONS = {
    "creator_takedown", "permission_revoked", "rights_denied",
    "source_deleted", "source_private", "unsafe_or_illegal",
}


class ReviewError(ValueError):
    """A human-readable review command failure."""


def rfc3339(value: str) -> str:
    """Validate and normalize an RFC3339 timestamp to UTC seconds."""
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def resolve_source_id(catalog: dict, value: str) -> str:
    """Resolve a source ID, source URL, or unambiguous platform-native ID."""
    candidates = catalog.get("candidates", {})
    if value in candidates:
        return value
    if value.startswith("https://"):
        try:
            source_id = source_identity(value)[2]
        except ValueError as exc:
            raise ReviewError(str(exc)) from exc
        if source_id in candidates:
            return source_id
    matches = [
        source_id for source_id, source in catalog.get("sources", {}).items()
        if source.get("native_id") == value or str(source.get("native_id", "")).removeprefix("t3_") == value
    ]
    if len(matches) == 1 and matches[0] in candidates:
        return matches[0]
    if len(matches) > 1:
        raise ReviewError(f"ambiguous native ID {value!r}; use the canonical source ID")
    raise ReviewError(f"candidate not found: {value}")


def _review_inputs(catalog: dict, args: argparse.Namespace) -> tuple[list[str], list[str]]:
    reasons = _unique(args.reason)
    evidence_ids = _unique(args.evidence)
    invalid = [reason for reason in reasons if reason not in REASON_CODES]
    if invalid:
        raise ReviewError("invalid reason code(s): " + ", ".join(invalid))
    if args.actor not in catalog.get("actors", {}):
        raise ReviewError(f"actor does not exist in catalog: {args.actor}")
    missing = [evidence_id for evidence_id in evidence_ids if evidence_id not in catalog.get("evidence", {})]
    if missing:
        raise ReviewError("evidence does not exist in catalog: " + ", ".join(missing))
    return reasons, evidence_ids


def _source_title(source: dict) -> str:
    text = ((source.get("text") or {}).get("value") or "").strip()
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first[:140] or f"Untitled source {source.get('id', '')}"


def _reviewed_title(source: dict, title: str | None) -> str:
    raw_source_text = ((source.get("text") or {}).get("value") or "").strip()
    if title is None and not raw_source_text:
        raise ReviewError(
            "source text is withheld; include requires an explicit human-reviewed --title"
        )
    selected = (title or _source_title(source)).strip()
    if not selected:
        raise ReviewError("title must not be empty")
    if len(selected) > 140:
        raise ReviewError("title must be 140 characters or fewer")
    return selected


def _candidate_prompt(catalog: dict, source_id: str, *, decision: str | None,
                      prompt_payload: dict | None) -> dict | None:
    """Consume an explicitly accepted/rejected prompt proposal."""
    candidate = (catalog.get("candidates") or {}).get(source_id) or {}
    observation = candidate.get("prompt_observation")
    if not observation:
        if decision is not None:
            raise ReviewError("candidate has no observed prompt to accept or reject")
        return None
    if decision not in {"accept", "reject"}:
        raise ReviewError(
            "candidate has an observed prompt; pass --accept-observed-prompt "
            "or --reject-observed-prompt"
        )
    if decision == "reject":
        return None
    prompt_source_id = observation.get("source_id")
    source = (catalog.get("sources") or {}).get(prompt_source_id) or {}
    errors = []
    status = observation.get("status")
    if "text" in observation:
        errors.append("public observation must not contain a text field")
    if observation.get("review_state") != "pending":
        errors.append("review_state must be pending")
    if status not in {"verbatim", "referenced_not_captured"}:
        errors.append("status must be verbatim or referenced_not_captured")
    if observation.get("candidate_source_id") != source_id:
        errors.append("candidate source identity changed after hydration")
    if not source or observation.get("source_url") != source.get("url"):
        errors.append("source identity changed after hydration")
    if observation.get("observed_by_actor_id") not in (catalog.get("actors") or {}):
        errors.append("observing actor does not resolve")
    try:
        rfc3339(observation.get("observed_at") or "")
    except argparse.ArgumentTypeError:
        errors.append("observed_at must be RFC3339")

    evidence_ids = observation.get("evidence_ids") or []
    if not evidence_ids:
        errors.append("source evidence is required")
    expected_kind = "source_comment" if source.get("kind") == "comment" else "source_post"
    for evidence_id in evidence_ids:
        record = (catalog.get("evidence") or {}).get(evidence_id) or {}
        if record.get("kind") != expected_kind or record.get("source_id") != prompt_source_id:
            errors.append(f"{evidence_id} is not evidence for the observed prompt source")

    text = None
    if status == "verbatim":
        if observation.get("capture_method") != "post_text" or observation.get("is_verbatim") is not True:
            if not (
                source.get("kind") == "comment"
                and observation.get("capture_method") == "comment_text"
                and observation.get("is_verbatim") is True
            ):
                errors.append("verbatim capture metadata is invalid")
        if not isinstance(prompt_payload, dict):
            errors.append("accepted verbatim prompt requires its volatile-cache payload")
        else:
            text = prompt_payload.get("text")
            digest = hashlib.sha256(text.encode()).hexdigest() if isinstance(text, str) else None
            comparisons = {
                "candidate_source_id": source_id,
                "source_id": prompt_source_id,
                "source_url": source.get("url"),
                "status": status,
                "capture_method": observation.get("capture_method"),
                "is_verbatim": True,
                "sha256": observation.get("text_sha256"),
                "length": observation.get("text_length"),
                "evidence_ids": evidence_ids,
                "observed_at": observation.get("observed_at"),
            }
            for key, expected in comparisons.items():
                if prompt_payload.get(key) != expected:
                    errors.append(f"volatile-cache {key} does not match public metadata")
            if not text:
                errors.append("volatile-cache prompt text is empty")
            if digest != observation.get("text_sha256"):
                errors.append("volatile-cache prompt hash does not match")
            if isinstance(text, str) and len(text) != observation.get("text_length"):
                errors.append("volatile-cache prompt length does not match")
    elif (
        prompt_payload is not None
        or observation.get("cache_key") is not None
        or observation.get("text_sha256") is not None
        or observation.get("text_length") is not None
        or observation.get("capture_method") != "none"
        or observation.get("is_verbatim") is not False
    ):
        errors.append("referenced prompt must not contain captured text")
    if errors:
        raise ReviewError("candidate prompt observation is invalid: " + "; ".join(errors))
    return {
        "status": status, "text": text, "language": observation.get("language"),
        "source_id": prompt_source_id, "source_url": source["url"],
        "capture_method": observation.get("capture_method"),
        "is_verbatim": observation.get("is_verbatim"),
        "evidence_ids": list(evidence_ids),
    }


def load_prompt_payload(catalog: dict, source_id: str, path: Path | None) -> dict | None:
    """Load the exact private payload named by public prompt metadata."""
    observation = ((catalog.get("candidates") or {}).get(source_id) or {}).get("prompt_observation")
    if not observation or observation.get("status") != "verbatim":
        return None
    if path is None:
        raise ReviewError("--accept-observed-prompt requires --volatile-cache for verbatim text")
    resolved = path.expanduser().resolve()
    try:
        relative = resolved.relative_to(ROOT.resolve())
    except ValueError:
        relative = None
    if relative is not None and (not relative.parts or relative.parts[0] != ".cache"):
        raise ReviewError("an in-repository volatile cache must be under .cache/")
    try:
        permissions = stat.S_IMODE(resolved.stat().st_mode)
        if permissions & 0o077:
            raise ReviewError("volatile cache must have mode 0600 (no group/other access)")
        cache = read_json(resolved)
    except OSError as exc:
        raise ReviewError(f"cannot read volatile cache: {exc}") from exc
    except (json.JSONDecodeError, TypeError) as exc:
        raise ReviewError(f"volatile cache is not valid JSON: {exc}") from exc
    if not isinstance(cache, dict) or cache.get("schema_version") != VOLATILE_CACHE_VERSION:
        raise ReviewError(f"volatile cache schema must be {VOLATILE_CACHE_VERSION}")
    if cache.get("collection_id") != (catalog.get("collection") or {}).get("id"):
        raise ReviewError("volatile cache belongs to a different collection")
    cache_key = observation.get("cache_key")
    payload = (cache.get("prompts") or {}).get(cache_key)
    if not isinstance(payload, dict):
        raise ReviewError(f"volatile cache has no prompt payload for {cache_key}")
    return payload


def consume_prompt_payload(catalog: dict, path: Path, cache_key: str) -> None:
    """Atomically remove a decided prompt payload from its owner-only cache."""
    resolved = path.expanduser().resolve()
    try:
        permissions = stat.S_IMODE(resolved.stat().st_mode)
        if permissions & 0o077:
            raise ReviewError("volatile cache must have mode 0600 (no group/other access)")
        cache = read_json(resolved)
    except OSError as exc:
        raise ReviewError(f"cannot read volatile cache: {exc}") from exc
    except (json.JSONDecodeError, TypeError) as exc:
        raise ReviewError(f"volatile cache is not valid JSON: {exc}") from exc
    if not isinstance(cache, dict) or cache.get("schema_version") != VOLATILE_CACHE_VERSION:
        raise ReviewError(f"volatile cache schema must be {VOLATILE_CACHE_VERSION}")
    if cache.get("collection_id") != (catalog.get("collection") or {}).get("id"):
        raise ReviewError("volatile cache belongs to a different collection")
    prompts = cache.get("prompts") or {}
    if not isinstance(prompts, dict) or cache_key not in prompts:
        raise ReviewError(f"volatile cache has no prompt payload for {cache_key}")
    prompts.pop(cache_key)
    cache["prompts"] = prompts
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{resolved.name}.", dir=resolved.parent)
        temporary = Path(name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(cache, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, resolved)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _safe_item(catalog: dict, source_id: str, actor: str, at: str,
               evidence_ids: list[str], reasons: list[str],
               prompt_record: dict | None = None,
               title: str | None = None) -> tuple[str, dict]:
    """Create a source-link-only item with all creator and rights claims unknown."""
    source = catalog["sources"][source_id]
    position = 0
    new_id = item_id(source_id, position)
    while new_id in catalog.get("items", {}):
        position += 1
        new_id = item_id(source_id, position)

    scope = (catalog.get("collection", {}).get("model_scope") or [{}])[0]
    media = []
    for index, observation in enumerate(source.get("media_observations") or []):
        if observation.get("kind") != "video":
            continue
        media.append({
            "media_id": f"med_{new_id}_{index}",
            "source_id": source_id,
            "source_media_id": observation.get("source_media_id"),
            "kind": "video",
            "delivery": {
                "mode": "source_link",
                "link_url": source["url"],
                "official_embed": None,
                "mirrors": [],
            },
        })

    item = {
        "id": new_id,
        "display": {
            "title": {
                "text": _reviewed_title(source, title),
                "provenance": "reviewed",
                "editor_actor_id": actor,
                "edited_at": at,
                "evidence_ids": list(evidence_ids),
            },
            "category": {
                "id": "uncategorized",
                "label": "Uncategorized",
                "provenance": "review_default",
                "rule_version": None,
                "editor_actor_id": None,
                "evidence_ids": [],
            },
        },
        "model": {
            "family": scope.get("family") or catalog.get("collection", {}).get("id"),
            "version": None,
            "verification": "unverified",
            "evidence_ids": [],
        },
        "canonical_source_id": source_id,
        "source_ids": [source_id],
        "attribution": {
            "posted_by": [{"source_id": source_id, "actor_id": source["posted_by_actor_id"]}],
            "original_video_creators": [{
                "actor_id": None, "status": "unknown", "evidence_ids": [], "note": None,
            }],
            "prompt_authors": [{
                "actor_id": None, "status": "unknown", "evidence_ids": [], "note": None,
            }],
        },
        "prompt": prompt_record or {
            "status": "unavailable",
            "text": None,
            "language": None,
            "source_id": None,
            "source_url": None,
            "capture_method": "none",
            "is_verbatim": False,
            "evidence_ids": [],
        },
        "annotations": [],
        "provenance": {"duplicate_cluster_id": None, "fingerprints": [], "repost_chain": []},
        "media": media,
        "rights": {
            "video_republication": {
                "status": "unknown", "license_spdx": None, "granted_scopes": [],
                "grantor_actor_ids": [], "granted_at": None, "expires_at": None,
                "evidence_ids": [],
            },
            "prompt_republication": {
                "status": "unknown", "license_spdx": None, "granted_scopes": [],
                "grantor_actor_ids": [], "granted_at": None, "expires_at": None,
                "evidence_ids": [],
            },
        },
        "curation": {
            "status": "approved",
            "reviewer_actor_id": actor,
            "reviewed_at": at,
            "evidence_ids": evidence_ids,
            "reason_codes": reasons,
        },
    }
    return new_id, item


def _history(review: dict, state: str, reasons: list[str], evidence_ids: list[str],
             actor: str, at: str, note: str | None) -> list[dict]:
    history = list(review.get("history") or [])
    history.append({
        "previous_state": review.get("state"),
        "state": state,
        "at": at,
        "actor_id": actor,
        "reason_codes": reasons,
        "evidence_ids": evidence_ids,
        "note": note,
    })
    return history


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


def redact_sensitive_removal(catalog: dict, item: dict, candidate: dict, *,
                              reasons: list[str], evidence_ids: list[str],
                              actor: str, at: str) -> None:
    """Erase publishable content while retaining a minimal decision trail."""
    prompt = item.get("prompt") or {}
    annotations = item.get("annotations") or []
    content_evidence_ids = (
        _nested_evidence_ids(prompt)
        | _nested_evidence_ids(annotations)
        | _nested_evidence_ids((item.get("display") or {}).get("title") or {})
    )
    source_ids = set(item.get("source_ids") or [])
    if prompt.get("source_id"):
        source_ids.add(prompt["source_id"])
    source_ids.update(
        annotation.get("source_id") for annotation in annotations
        if isinstance(annotation, dict) and annotation.get("source_id")
    )

    for source_id in source_ids:
        source = (catalog.get("sources") or {}).get(source_id)
        if not source:
            continue
        language = (source.get("text") or {}).get("language")
        source["text"] = {"status": "redacted", "value": None, "language": language}
        for observation in source.get("media_observations") or []:
            observation["direct_url"] = None
            observation["thumbnail_url"] = None
            observation["variants"] = []

    for record in (catalog.get("evidence") or {}).values():
        if record.get("id") in content_evidence_ids or (
            record.get("source_id") in source_ids
            and record.get("kind") in {"source_post", "source_comment", "prompt_source"}
        ):
            if "excerpt" in record:
                record["excerpt"] = None

    title = (item.setdefault("display", {})).setdefault("title", {})
    title.update({
        "text": f"Removed item {item.get('id')}",
        "provenance": "redacted",
        "editor_actor_id": actor,
        "edited_at": at,
        "evidence_ids": list(evidence_ids),
    })
    item["prompt"] = {
        "status": "removed", "text": None, "language": None,
        "source_id": None, "source_url": None, "capture_method": "none",
        "is_verbatim": False, "evidence_ids": list(evidence_ids),
    }
    item["annotations"] = []
    for roles in ((item.get("attribution") or {}).values()):
        if isinstance(roles, list):
            for role in roles:
                if isinstance(role, dict) and "note" in role:
                    role["note"] = None

    status = "revoked" if {"creator_takedown", "permission_revoked"} & set(reasons) else "denied"
    for rights in (item.get("rights") or {}).values():
        rights.update({
            "status": status, "license_spdx": None, "granted_scopes": [],
            "grantor_actor_ids": [], "granted_at": None, "expires_at": None,
            "evidence_ids": list(evidence_ids),
        })

    review = candidate.get("review") or {}
    review["note"] = None
    for entry in review.get("history") or []:
        entry["note"] = None


def include_candidate(catalog: dict, source_id: str, *, reasons: list[str], evidence_ids: list[str],
                      actor: str, at: str, note: str | None, requested_item_id: str | None,
                      prompt_decision: str | None = None,
                      prompt_payload: dict | None = None,
                      title: str | None = None) -> str:
    if "meets_scope" not in reasons:
        raise ReviewError("include requires --reason meets_scope")
    candidate = catalog["candidates"][source_id]
    review = candidate.get("review") or {}
    had_prompt_observation = bool(candidate.get("prompt_observation"))
    prompt_record = _candidate_prompt(
        catalog, source_id, decision=prompt_decision, prompt_payload=prompt_payload,
    )
    reviewed_title = _reviewed_title(catalog["sources"][source_id], title)
    selected = requested_item_id or review.get("item_id")
    created = False
    if selected:
        item = catalog.get("items", {}).get(selected)
        if not item:
            raise ReviewError(f"item does not exist: {selected}")
        if item.get("canonical_source_id") != source_id:
            raise ReviewError(f"item {selected} belongs to {item.get('canonical_source_id')}, not {source_id}")
    else:
        existing = [
            key for key, item in catalog.get("items", {}).items()
            if item.get("canonical_source_id") == source_id
        ]
        if len(existing) > 1:
            raise ReviewError("multiple items use this source; pass --item-id explicitly")
        if existing:
            selected = existing[0]
        else:
            selected, item = _safe_item(
                catalog, source_id, actor, at, evidence_ids, reasons, prompt_record, title
            )
            catalog.setdefault("items", {})[selected] = item
            created = True

    item = catalog["items"][selected]
    item.setdefault("display", {})["title"] = {
        "text": reviewed_title, "provenance": "reviewed",
        "editor_actor_id": actor, "edited_at": at,
        "evidence_ids": list(evidence_ids),
    }
    if prompt_record and not created and (item.get("prompt") or {}).get("status") in {
        "not_provided", "referenced_not_captured", "unavailable",
    }:
        item["prompt"] = prompt_record
    # Pending observations are deliberately not retained on an included/public
    # candidate.  The reviewed item and its evidence become the source of truth.
    if had_prompt_observation:
        candidate.pop("prompt_observation", None)
    item["curation"] = {
        **(item.get("curation") or {}),
        "status": "approved",
        "reviewer_actor_id": actor,
        "reviewed_at": at,
        "evidence_ids": evidence_ids,
        "reason_codes": reasons,
    }
    candidate["review"] = {
        **review,
        "state": "included",
        "reason_codes": reasons,
        "note": note,
        "item_id": selected,
        "duplicate_of_item_id": None,
        "reviewer_actor_id": actor,
        "reviewed_at": at,
        "evidence_ids": evidence_ids,
        "history": _history(review, "included", reasons, evidence_ids, actor, at, note),
    }
    return selected


def exclude_candidate(catalog: dict, source_id: str, *, reasons: list[str], evidence_ids: list[str],
                      actor: str, at: str, note: str | None,
                      duplicate_of_item_id: str | None) -> None:
    if reasons == ["meets_scope"] or "meets_scope" in reasons:
        raise ReviewError("exclude cannot use meets_scope as a reason")
    candidate = catalog["candidates"][source_id]
    review = candidate.get("review") or {}
    if review.get("state") == "included":
        raise ReviewError("included candidates must use remove, not exclude")
    if "duplicate_media" in reasons:
        if not duplicate_of_item_id:
            raise ReviewError("duplicate_media requires --duplicate-of-item")
        if duplicate_of_item_id not in catalog.get("items", {}):
            raise ReviewError(f"duplicate target item does not exist: {duplicate_of_item_id}")
    elif duplicate_of_item_id:
        raise ReviewError("--duplicate-of-item is only valid with duplicate_media")
    candidate["review"] = {
        **review,
        "state": "excluded",
        "reason_codes": reasons,
        "note": note,
        "item_id": None,
        "duplicate_of_item_id": duplicate_of_item_id,
        "reviewer_actor_id": actor,
        "reviewed_at": at,
        "evidence_ids": evidence_ids,
        "history": _history(review, "excluded", reasons, evidence_ids, actor, at, note),
    }


def remove_candidate(catalog: dict, source_id: str, *, reasons: list[str], evidence_ids: list[str],
                     actor: str, at: str, note: str | None) -> str:
    if "meets_scope" in reasons:
        raise ReviewError("remove cannot use meets_scope as a reason")
    candidate = catalog["candidates"][source_id]
    review = candidate.get("review") or {}
    if review.get("state") != "included":
        raise ReviewError("remove requires a currently included candidate")
    selected = review.get("item_id")
    item = catalog.get("items", {}).get(selected)
    if not item:
        raise ReviewError("included candidate has no valid item")
    sensitive = bool(CONTENT_REDACTION_REASONS & set(reasons))
    if sensitive:
        redact_sensitive_removal(
            catalog, item, candidate, reasons=reasons, evidence_ids=evidence_ids,
            actor=actor, at=at,
        )
        note = "Content redacted; the reason codes and evidence IDs retain the minimal audit trail."
    item["curation"] = {
        **(item.get("curation") or {}),
        "status": "removed",
        "reviewer_actor_id": actor,
        "reviewed_at": at,
        "evidence_ids": evidence_ids,
        "reason_codes": reasons,
    }
    for media in item.get("media") or []:
        delivery = media.get("delivery") or {}
        for mirror in (delivery.get("mirrors") or []):
            if mirror.get("state") not in {"deleted", "pending_delete"}:
                mirror.update({
                    "state": "pending_delete",
                    "pending_delete_at": at,
                    "pending_delete_by_actor_id": actor,
                    "pending_delete_reason_codes": reasons,
                    "pending_delete_evidence_ids": evidence_ids,
                })
        if not any(mirror.get("state") == "active" for mirror in delivery.get("mirrors") or []):
            delivery["mode"] = "source_link"
    candidate["review"] = {
        **review,
        "state": "removed",
        "reason_codes": reasons,
        "note": note,
        "item_id": selected,
        "reviewer_actor_id": actor,
        "reviewed_at": at,
        "evidence_ids": evidence_ids,
        "history": _history(review, "removed", reasons, evidence_ids, actor, at, note),
    }
    return selected


def list_candidates(catalog: dict, *, state: str | None, platform: str | None,
                    limit: int | None) -> list[dict]:
    rows = []
    for source_id, candidate in sorted(catalog.get("candidates", {}).items()):
        source = catalog.get("sources", {}).get(source_id) or {}
        review = candidate.get("review") or {}
        if state and review.get("state") != state:
            continue
        if platform and source.get("platform") != platform:
            continue
        linked = catalog.get("items", {}).get(review.get("item_id")) or {}
        title = (((linked.get("display") or {}).get("title") or {}).get("text") or _source_title(source))
        rows.append({
            "source_id": source_id,
            "platform": source.get("platform"),
            "state": review.get("state"),
            "availability": (source.get("availability") or {}).get("state"),
            "item_id": review.get("item_id"),
            "reason_codes": review.get("reason_codes") or [],
            "title": title,
            "url": source.get("url"),
        })
    return rows if limit is None else rows[:limit]


def show_candidate(catalog: dict, source_id: str) -> dict:
    candidate = catalog["candidates"][source_id]
    item_key = (candidate.get("review") or {}).get("item_id")
    evidence_ids = (candidate.get("review") or {}).get("evidence_ids") or []
    return {
        "source_id": source_id,
        "candidate": candidate,
        "source": catalog.get("sources", {}).get(source_id),
        "item": catalog.get("items", {}).get(item_key) if item_key else None,
        "reviewer": catalog.get("actors", {}).get((candidate.get("review") or {}).get("reviewer_actor_id")),
        "review_evidence": {
            evidence_id: catalog.get("evidence", {}).get(evidence_id) for evidence_id in evidence_ids
        },
    }


def _add_decision_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("candidate", help="canonical source ID, source URL, or native post ID")
    parser.add_argument("--reason", action="append", required=True, choices=sorted(REASON_CODES),
                        help="review reason code; repeat for multiple reasons")
    parser.add_argument("--evidence", action="append", required=True,
                        help="existing catalog evidence ID; repeat for multiple records")
    parser.add_argument("--actor", required=True, help="existing catalog actor ID")
    parser.add_argument("--at", required=True, type=rfc3339, help="review time as RFC3339")
    parser.add_argument("--note", help="optional human review note")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--retirement", type=Path, default=ROOT / "data" / "media-retirement.json")
    parser.add_argument("--github-manifest", type=Path,
                        default=ROOT / "data" / "github-attachments.json")
    parser.add_argument("--r2-manifest", type=Path,
                        default=ROOT / "data" / "r2-mirrors.json")
    commands = parser.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("list", help="list review candidates")
    listing.add_argument("--state", choices=sorted(CANDIDATE_STATES))
    listing.add_argument("--platform", choices=("x", "reddit"))
    listing.add_argument("--limit", type=int)
    listing.add_argument("--json", action="store_true", dest="as_json")

    showing = commands.add_parser("show", help="show a candidate with source, item and evidence")
    showing.add_argument("candidate")

    including = commands.add_parser("include", help="approve a candidate for source-link indexing")
    _add_decision_args(including)
    including.add_argument("--item-id", help="reuse an existing item for this source")
    including.add_argument(
        "--title", help="human-reviewed public title (required when source text is withheld)",
    )
    prompt_choice = including.add_mutually_exclusive_group()
    prompt_choice.add_argument(
        "--accept-observed-prompt", action="store_const", const="accept",
        dest="prompt_decision", help="accept the pending prompt after cache verification",
    )
    prompt_choice.add_argument(
        "--reject-observed-prompt", action="store_const", const="reject",
        dest="prompt_decision", help="reject and discard the pending prompt proposal",
    )
    including.add_argument(
        "--volatile-cache", type=Path,
        help="owner-only private cache required when accepting verbatim prompt text",
    )

    excluding = commands.add_parser("exclude", help="exclude a candidate that is not currently included")
    _add_decision_args(excluding)
    excluding.add_argument("--duplicate-of-item", help="required with duplicate_media")

    removing = commands.add_parser("remove", help="remove an included candidate and quarantine active mirrors")
    _add_decision_args(removing)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "list" and args.limit is not None and args.limit < 0:
        parser.error("--limit must be zero or greater")
    catalog = read_json(args.catalog)
    if not isinstance(catalog, dict):
        parser.error(f"catalog is not a JSON object: {args.catalog}")

    try:
        if args.command == "list":
            rows = list_candidates(catalog, state=args.state, platform=args.platform, limit=args.limit)
            if args.as_json:
                print(json.dumps(rows, indent=2, ensure_ascii=False))
            else:
                for row in rows:
                    reasons = ",".join(row["reason_codes"]) or "-"
                    print("\t".join(str(row[key] or "-") for key in
                                    ("source_id", "platform", "state", "availability", "item_id"))
                          + f"\t{reasons}\t{row['title']}")
            return 0
        source_id = resolve_source_id(catalog, args.candidate)
        if args.command == "show":
            print(json.dumps(show_candidate(catalog, source_id), indent=2, ensure_ascii=False))
            return 0

        reasons, evidence_ids = _review_inputs(catalog, args)
        result_item = None
        if args.command == "include":
            prompt_observation = (
                (catalog.get("candidates", {}).get(source_id) or {}).get("prompt_observation") or {}
            )
            prompt_cache_key = prompt_observation.get("cache_key")
            cached_prompt_payload = (
                load_prompt_payload(catalog, source_id, args.volatile_cache)
                if prompt_cache_key and (
                    args.prompt_decision == "accept"
                    or (args.prompt_decision == "reject" and args.volatile_cache is not None)
                )
                else None
            )
            result_item = include_candidate(
                catalog, source_id, reasons=reasons, evidence_ids=evidence_ids,
                actor=args.actor, at=args.at, note=args.note, requested_item_id=args.item_id,
                prompt_decision=args.prompt_decision,
                prompt_payload=cached_prompt_payload if args.prompt_decision == "accept" else None,
                title=args.title,
            )
        elif args.command == "exclude":
            exclude_candidate(
                catalog, source_id, reasons=reasons, evidence_ids=evidence_ids,
                actor=args.actor, at=args.at, note=args.note,
                duplicate_of_item_id=args.duplicate_of_item,
            )
        elif args.command == "remove":
            result_item = remove_candidate(
                catalog, source_id, reasons=reasons, evidence_ids=evidence_ids,
                actor=args.actor, at=args.at, note=args.note,
            )
        catalog["updated_at"] = args.at
        current_retirement = None
        retirement = None
        if args.command == "remove":
            current_retirement = read_json(args.retirement, {})
            retirement = refresh_retirement_manifest(
                catalog, current_retirement, args.at,
                note="Human review removed the item; remote mirror cleanup requires confirmation.",
            )
        errors = validate_catalog(catalog)
        if errors:
            raise ReviewError("catalog validation failed:\n  " + "\n  ".join(errors))
        write_json(args.catalog, catalog)
        if (
            args.command == "include" and prompt_cache_key
            and args.prompt_decision in {"accept", "reject"}
            and args.volatile_cache is not None
        ):
            consume_prompt_payload(catalog, args.volatile_cache, prompt_cache_key)
        if args.command == "remove":
            if retirement != current_retirement:
                write_json(args.retirement, retirement)
            write_json(
                args.github_manifest,
                build_github_attachment_manifest(catalog, args.at),
            )
            write_json(args.r2_manifest, build_r2_manifest(catalog, args.at))
        print(json.dumps({
            "source_id": source_id,
            "state": catalog["candidates"][source_id]["review"]["state"],
            "item_id": result_item or catalog["candidates"][source_id]["review"].get("item_id"),
            "reviewed_by": args.actor,
            "reviewed_at": args.at,
            "reason_codes": reasons,
            "evidence_ids": evidence_ids,
        }, indent=2, ensure_ascii=False))
        return 0
    except ReviewError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
