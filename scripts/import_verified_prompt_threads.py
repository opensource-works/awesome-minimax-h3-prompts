#!/usr/bin/env python3
"""Import author-posted prompt replies that were independently verified."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from catalog import read_json, validate_catalog, write_json  # noqa: E402


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def is_rfc3339(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def read_digest_bound_payload(path: Path, expected_sha256: str) -> list[dict]:
    if len(expected_sha256) != 64 or any(c not in "0123456789abcdef" for c in expected_sha256):
        raise ValueError("payload SHA-256 is invalid")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("prompt payload SHA-256 does not match --payload-sha256")
    value = json.loads(raw)
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError("prompt payload must be a JSON array of objects")
    return value


def read_digest_bound_grantors(path: Path, expected_sha256: str) -> dict[str, str]:
    if len(expected_sha256) != 64 or any(c not in "0123456789abcdef" for c in expected_sha256):
        raise ValueError("grantor-map SHA-256 is invalid")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("grantor map SHA-256 does not match --grantor-map-sha256")
    value = json.loads(raw)
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(actor_id, str)
        for key, actor_id in value.items()
    ):
        raise ValueError("grantor map must be a JSON object of item IDs to actor IDs")
    return value


def validate_verification_lock(
    lock: dict,
    payload: list[dict],
    payload_sha256: str,
    repo_key: str,
) -> None:
    if lock.get("schema_version") != "verified-prompt-imports-v1":
        raise ValueError("prompt verification lock schema is invalid")
    if lock.get("reviewed_payload_sha256") != payload_sha256:
        raise ValueError("prompt payload is not the reviewed payload in the verification lock")
    expected = {
        reply_id: record
        for reply_id, record in (lock.get("entries") or {}).items()
        if record.get("repo") == repo_key
    }
    observed: dict[str, dict] = {}
    for thread in payload:
        if thread.get("repo") != repo_key:
            continue
        for reply in thread.get("prompt_replies") or []:
            reply_id = str(reply.get("reply_id"))
            observed[reply_id] = {
                "repo": repo_key,
                "item_id": thread.get("item_id"),
                "root_id": thread.get("root_id"),
                "root_url": thread.get("root_url"),
                "reply_url": reply.get("url"),
                "author_handle": (reply.get("author") or {}).get("handle"),
                "created_at": reply.get("created_at"),
                "parent_id": str((reply.get("replying_to") or {}).get("status") or ""),
                "text_sha256": sha256_text(str(reply.get("text") or "")),
            }
    if observed != expected:
        raise ValueError("prompt payload differs from the reviewed public-source lock")


def rfc3339(value: str) -> str:
    parsed = parsedate_to_datetime(value).astimezone(timezone.utc)
    return parsed.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def prompt_value(replies: list[dict], evidence_ids: list[str]) -> dict:
    segments = []
    for reply, evidence_id in zip(replies, evidence_ids):
        source_id = f"x:{reply['reply_id']}"
        segments.append({
            "text": reply["text"],
            "source_id": source_id,
            "source_url": reply["url"],
            "is_verbatim": True,
            "evidence_ids": [evidence_id],
        })
    return {
        "status": "verbatim",
        "text": "\n\n---\n\n".join(segment["text"] for segment in segments),
        "language": None,
        "source_id": segments[0]["source_id"],
        "source_url": segments[0]["source_url"],
        "source_ids": [segment["source_id"] for segment in segments],
        "source_urls": [segment["source_url"] for segment in segments],
        "segments": segments,
        "capture_method": "verified_reply_text",
        "is_verbatim": True,
        "evidence_ids": evidence_ids,
    }


def prompt_rights(grantor: str, granted_at: str) -> dict:
    return {
        "status": "granted",
        "license_spdx": None,
        "granted_scopes": ["reproduce_prompt"],
        "grantor_actor_ids": [grantor],
        "granted_at": granted_at,
        "expires_at": None,
        "evidence_ids": [],
        "grant_verification": "maintainer_attestation",
    }


def set_prompt_attribution(item: dict) -> None:
    item["attribution"]["prompt_authors"] = [{
        "actor_id": None,
        "status": "unknown",
        "evidence_ids": [],
        "note": "The linked account is credited as the prompt source; authorship is not inferred.",
    }]


def prompt_target_errors(
    catalog: dict,
    item: dict,
    grantor: str | None,
    granted_at: str,
    tag: str,
) -> list[str]:
    errors: list[str] = []
    if (item.get("curation") or {}).get("status") != "approved":
        errors.append(f"{tag}: item is not approved")
    source = (catalog.get("sources") or {}).get(item.get("canonical_source_id")) or {}
    if ((source.get("availability") or {}).get("state")) in {
        "deleted", "private", "suspended",
    }:
        errors.append(f"{tag}: source availability forbids prompt import")
    prompt = item.get("prompt") or {}
    if prompt.get("status") == "removed":
        errors.append(f"{tag}: removed prompt cannot be restored by import")
    rights = (item.get("rights") or {}).get("prompt_republication") or {}
    status = rights.get("status")
    if status in {"denied", "revoked", "public_license"}:
        errors.append(f"{tag}: existing prompt rights cannot be overwritten")
    elif status == "granted" and not (
        rights.get("grant_verification") == "maintainer_attestation"
        and not (rights.get("evidence_ids") or [])
        and rights.get("grantor_actor_ids") == [grantor]
        and set(rights.get("granted_scopes") or []) == {"reproduce_prompt"}
        and rights.get("granted_at") == granted_at
        and rights.get("expires_at") is None
    ):
        errors.append(f"{tag}: existing prompt grant differs from this private confirmation")
    return errors


def import_threads(
    catalog: dict,
    payload: list[dict],
    repo_key: str,
    captured_at: str,
    granted_at: str,
    grantors: dict[str, str],
) -> tuple[dict, int, int]:
    threads = [value for value in payload if value.get("repo") == repo_key]
    errors: list[str] = []
    if not is_rfc3339(captured_at):
        errors.append("captured_at must be RFC3339")
    if not is_rfc3339(granted_at):
        errors.append("granted_at must be RFC3339")
    if not threads:
        errors.append(f"no verified prompt threads found for repo key {repo_key}")
    actors = catalog.get("actors") or {}
    sources = catalog.get("sources") or {}
    evidence = catalog.get("evidence") or {}
    items = catalog.get("items") or {}

    prepared: list[tuple[dict, dict, list[dict], str, str]] = []
    seen_reply_ids: set[str] = set()
    for thread in threads:
        item_id = thread.get("item_id")
        item = items.get(item_id)
        replies = thread.get("prompt_replies") or []
        tag = f"thread.{thread.get('root_id')}"
        if not item:
            errors.append(f"{tag}: item_id does not resolve")
            continue
        root_source = sources.get(item.get("canonical_source_id")) or {}
        if root_source.get("native_id") != thread.get("root_id"):
            errors.append(f"{tag}: root_id differs from the item's canonical source")
        if str(thread.get("root_url") or "").lower() != str(root_source.get("url") or "").lower():
            errors.append(f"{tag}: root_url differs from the item's canonical source")
        source_actor_id = root_source.get("posted_by_actor_id")
        actor = actors.get(source_actor_id) or {}
        rights_grantor = grantors.get(item_id)
        if rights_grantor not in actors:
            errors.append(f"{tag}: private grantor map actor cannot be resolved")
        errors.extend(prompt_target_errors(catalog, item, rights_grantor, granted_at, tag))
        if not replies:
            errors.append(f"{tag}: prompt_replies is empty")
            continue
        reply_ids = {str(reply.get("reply_id")) for reply in replies}
        listed_reply_ids = {str(value) for value in thread.get("prompt_reply_ids") or []}
        if reply_ids != listed_reply_ids or len(reply_ids) != len(replies):
            errors.append(f"{tag}: prompt_reply_ids is incomplete or duplicated")
        incoming_text = "\n\n---\n\n".join(
            str(reply.get("text") or "") for reply in replies
        )
        existing_text = (item.get("prompt") or {}).get("text")
        if existing_text is not None and existing_text != incoming_text:
            errors.append(f"{tag}: import would overwrite different prompt text")
        for reply in replies:
            reply_id = str(reply.get("reply_id"))
            if reply_id in seen_reply_ids:
                errors.append(f"{tag}.{reply_id}: reply appears in more than one thread")
            seen_reply_ids.add(reply_id)
            if not reply_id.isdigit():
                errors.append(f"{tag}.{reply_id}: reply ID must be numeric")
            expected_url = f"https://x.com/{reply.get('author', {}).get('handle')}/status/{reply_id}"
            if reply.get("url", "").lower() != expected_url.lower():
                errors.append(f"{tag}.{reply_id}: URL does not match author and reply ID")
            raw_text = (reply.get("raw_text") or {}).get("text")
            if not reply.get("text") or reply.get("text") != raw_text:
                errors.append(f"{tag}.{reply_id}: text is missing or differs from raw_text")
            if str(reply.get("author", {}).get("handle", "")).lower() != str(actor.get("handle", "")).lower():
                errors.append(f"{tag}.{reply_id}: reply author differs from the source account")
            if str((reply.get("replying_to") or {}).get("screen_name", "")).lower() != str(actor.get("handle", "")).lower():
                errors.append(f"{tag}.{reply_id}: reply parent account differs from the source account")
            parent_id = str((reply.get("replying_to") or {}).get("status") or "")
            if parent_id != thread.get("root_id") and parent_id not in reply_ids:
                parent_url = str((reply.get("replying_to") or {}).get("url") or "")
                if not parent_url.lower().startswith(
                    f"https://x.com/{str(actor.get('handle') or '').lower()}/status/"
                ):
                    errors.append(f"{tag}.{reply_id}: reply chain leaves the source account's thread")
            try:
                rfc3339(reply.get("created_at"))
            except (TypeError, ValueError):
                errors.append(f"{tag}.{reply_id}: created_at is invalid")
            reply_item = items.get(f"itm_x_{reply_id}_0")
            if reply_item and reply_item is not item:
                reply_grantor = grantors.get(reply_item["id"])
                if reply_grantor not in actors:
                    errors.append(
                        f"{tag}.{reply_id}: private grantor map actor cannot be resolved"
                    )
                errors.extend(prompt_target_errors(
                    catalog,
                    reply_item,
                    reply_grantor,
                    granted_at,
                    f"{tag}.{reply_id}.indexed_item",
                ))
                reply_existing_text = (reply_item.get("prompt") or {}).get("text")
                if reply_existing_text is not None and reply_existing_text != reply.get("text"):
                    errors.append(
                        f"{tag}.{reply_id}: import would overwrite different indexed-item prompt"
                    )
        prepared.append((thread, item, replies, source_actor_id, rights_grantor))

    if errors:
        raise ValueError("\n".join(errors))

    reply_count = 0
    updated_item_ids: set[str] = set()
    for thread, item, replies, source_actor_id, rights_grantor in prepared:
        prompt_evidence_ids: list[str] = []
        for reply in replies:
            reply_id = str(reply["reply_id"])
            source_id = f"x:{reply_id}"
            source_evidence_id = f"ev_source_x_{reply_id}"
            prompt_evidence_id = f"ev_prompt_x_{reply_id}"
            parent_id = str((reply.get("replying_to") or {}).get("status"))
            parent_source_id = f"x:{parent_id}"
            if parent_id != thread["root_id"] and parent_id not in {
                str(value["reply_id"]) for value in replies
            } and parent_source_id not in sources:
                # Some numbered threads have a non-prompt step between the
                # root and the prompt. Keep the captured graph closed while
                # retaining the exact direct prompt URL in the segment.
                parent_source_id = item["canonical_source_id"]
            posted_at = rfc3339(reply["created_at"])
            text_sha = sha256_text(reply["text"])
            existing = sources.get(source_id)
            if existing:
                if existing.get("posted_by_actor_id") != source_actor_id:
                    raise ValueError(f"existing source {source_id} has a different author")
                if (existing.get("text") or {}).get("sha256") not in {None, text_sha}:
                    raise ValueError(f"existing source {source_id} has different text")
                existing["parent_source_id"] = parent_source_id
            else:
                sources[source_id] = {
                    "id": source_id,
                    "platform": "x",
                    "native_id": reply_id,
                    "kind": "comment",
                    "url": reply["url"],
                    "parent_source_id": parent_source_id,
                    "posted_at": posted_at,
                    "posted_date": posted_at[:10],
                    "posted_by_actor_id": source_actor_id,
                    "text": {
                        "status": "withheld_pending_review",
                        "value": None,
                        "language": None,
                        "sha256": text_sha,
                        "length": len(reply["text"]),
                    },
                    "metrics": {
                        "observed_at": captured_at,
                        "views": None,
                        "likes": None,
                        "reposts": None,
                        "comments": None,
                    },
                    "media_observations": [],
                    "availability": {
                        "state": "available",
                        "checked_at": captured_at,
                        "last_available_at": captured_at,
                        "first_unavailable_at": None,
                        "confirmed_at": None,
                        "consecutive_failures": 0,
                        "http_status": 200,
                        "evidence_ids": [source_evidence_id],
                    },
                    "fetch": {
                        "adapter": "verified-fxtwitter-v2",
                        "observed_at": captured_at,
                        "raw_sha256": text_sha,
                    },
                }
            evidence.setdefault(source_evidence_id, {
                "id": source_evidence_id,
                "kind": "source_snapshot",
                "url": reply["url"],
                "source_id": source_id,
                "observed_at": captured_at,
                "excerpt": None,
                "captured_by_actor_id": "act_repository_opensource-works",
                "visibility": "public",
                "integrity_sha256": text_sha,
            })
            evidence[prompt_evidence_id] = {
                "id": prompt_evidence_id,
                "kind": "prompt_source",
                "url": reply["url"],
                "source_id": source_id,
                "observed_at": captured_at,
                "excerpt": None,
                "captured_by_actor_id": "act_repository_opensource-works",
                "visibility": "public",
                "integrity_sha256": text_sha,
                "integrity_subject": "prompt_text",
            }
            prompt_evidence_ids.append(prompt_evidence_id)
            if source_id not in item["source_ids"]:
                item["source_ids"].append(source_id)
                item["attribution"]["posted_by"].append({
                    "source_id": source_id,
                    "actor_id": source_actor_id,
                })
            reply_count += 1

        item["prompt"] = prompt_value(replies, prompt_evidence_ids)
        item["rights"]["prompt_republication"] = prompt_rights(rights_grantor, granted_at)
        set_prompt_attribution(item)
        updated_item_ids.add(item["id"])

        # A prompt reply may already be indexed as its own video entry. Give
        # that entry the same exact prompt instead of leaving it empty.
        for reply, evidence_id in zip(replies, prompt_evidence_ids):
            reply_item = items.get(f"itm_x_{reply['reply_id']}_0")
            if not reply_item or reply_item is item:
                continue
            one_reply = [reply]
            reply_item["prompt"] = prompt_value(one_reply, [evidence_id])
            reply_grantor = grantors.get(reply_item["id"])
            if reply_grantor not in actors:
                raise ValueError(
                    f"{reply_item['id']}: private grantor map actor cannot be resolved"
                )
            reply_item["rights"]["prompt_republication"] = prompt_rights(
                reply_grantor, granted_at
            )
            set_prompt_attribution(reply_item)
            updated_item_ids.add(reply_item["id"])

    catalog["updated_at"] = max(str(catalog.get("updated_at") or ""), captured_at)
    validation_errors = validate_catalog(catalog)
    if validation_errors:
        raise ValueError("imported catalog is invalid:\n" + "\n".join(validation_errors))
    return catalog, len(updated_item_ids), reply_count


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("payload", type=Path)
    parser.add_argument("--payload-sha256", required=True)
    parser.add_argument(
        "--verification-lock",
        type=Path,
        default=ROOT / "data" / "verified-prompt-imports.json",
    )
    parser.add_argument("--grantor-map", type=Path, required=True)
    parser.add_argument("--grantor-map-sha256", required=True)
    parser.add_argument("--repo-key", choices=("seedance", "minimax"), required=True)
    parser.add_argument("--catalog", type=Path, default=ROOT / "data/catalog.json")
    parser.add_argument("--captured-at", required=True)
    parser.add_argument("--granted-at", required=True)
    parser.add_argument("--confirm-maintainer-attestation", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    if not args.confirm_maintainer_attestation:
        print("refusing prompt import without --confirm-maintainer-attestation", file=sys.stderr)
        return 2
    try:
        payload = read_digest_bound_payload(args.payload, args.payload_sha256)
        validate_verification_lock(
            read_json(args.verification_lock),
            payload,
            args.payload_sha256,
            args.repo_key,
        )
        catalog, item_count, reply_count = import_threads(
            read_json(args.catalog),
            payload,
            args.repo_key,
            args.captured_at,
            args.granted_at,
            read_digest_bound_grantors(args.grantor_map, args.grantor_map_sha256),
        )
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    if args.dry_run:
        print(f"OK: {reply_count} verified reply/replies update {item_count} item(s)")
        return 0
    write_json(args.catalog, catalog)
    print(f"imported {reply_count} verified prompt reply/replies into {item_count} item(s)")
    return 0


def main() -> None:
    raise SystemExit(run(parse_args()))


if __name__ == "__main__":
    main()
