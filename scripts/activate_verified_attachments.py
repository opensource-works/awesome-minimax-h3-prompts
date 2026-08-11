#!/usr/bin/env python3
"""Activate previously uploaded GitHub videos after private integrity checks.

The command deliberately keeps private permission proof outside
Git.  It records only a maintainer attestation, grant scope, grantor account,
and the verified attachment identity needed for deterministic public builds.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from catalog import read_json, validate_catalog, write_json  # noqa: E402

ASSET_RE = re.compile(
    r"https://github\.com/user-attachments/assets/"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def file_identity(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def is_rfc3339(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def read_digest_bound_json(path: Path, expected_sha256: str, label: str) -> dict:
    if not SHA256_RE.fullmatch(expected_sha256):
        raise ValueError(f"{label} SHA-256 is invalid")
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256:
        raise ValueError(f"{label} SHA-256 does not match its expected digest")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def matches_existing_attestation(
    rights: dict,
    grantor: str | None,
    scopes: set[str],
    granted_at: str,
) -> bool:
    return (
        rights.get("status") == "granted"
        and rights.get("grant_verification") == "maintainer_attestation"
        and not (rights.get("evidence_ids") or [])
        and rights.get("grantor_actor_ids") == [grantor]
        and set(rights.get("granted_scopes") or []) == scopes
        and rights.get("granted_at") == granted_at
        and rights.get("expires_at") is None
    )


def activate_catalog(
    catalog: dict,
    retirement: dict,
    report: dict,
    locators: dict,
    grantors: dict,
    staging: Path,
    granted_at: str,
    *,
    require_all: bool = True,
    attest_prompts: bool = False,
) -> tuple[dict, dict, int]:
    errors: list[str] = []
    mapping = report.get("attachment_mapping") or {}
    summary = report.get("summary") or {}
    if not mapping:
        errors.append("recovery report has no attachment_mapping")
    if summary.get("failed") != 0 or summary.get("preflight_errors") != 0:
        errors.append("recovery report contains failed or preflight-error entries")
    if summary.get("successful") != len(mapping):
        errors.append("recovery report successful count differs from its mapping")
    if report.get("collection_id") != (catalog.get("collection") or {}).get("id"):
        errors.append("recovery report collection_id differs from the catalog")
    entries = report.get("entries") or []
    if not isinstance(entries, list):
        errors.append("recovery report entries must be a list")
        entries = []
    entries_by_item = {
        entry.get("item_id"): entry
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("item_id"), str)
    }
    if len(entries_by_item) != len(entries):
        errors.append("recovery report entries have missing or duplicate item IDs")

    items = catalog.get("items") or {}
    if require_all and set(mapping) != set(items):
        missing = sorted(set(items) - set(mapping))
        extra = sorted(set(mapping) - set(items))
        errors.append(f"report scope differs from catalog items (missing={missing}, extra={extra})")
    if set(grantors) != set(mapping):
        errors.append("private grantor map scope differs from the recovery report")

    checked_at = report.get("generated_at")
    if not is_rfc3339(checked_at):
        errors.append("recovery report generated_at must be RFC3339")
    staging = staging.resolve()
    seen_files: set[str] = set()
    seen_mirrors: set[str] = set()
    retirement_by_mirror = {
        entry.get("mirror_id"): entry
        for entry in retirement.get("entries") or []
        if isinstance(entry, dict)
    }

    for item_id, record in sorted(mapping.items()):
        tag = f"attachment_mapping.{item_id}"
        item = items.get(item_id)
        if not item:
            errors.append(f"{tag}: item does not resolve")
            continue
        grantor = grantors.get(item_id)
        if grantor not in (catalog.get("actors") or {}):
            errors.append(f"{tag}: private grantor map actor cannot be resolved")
        media_id = record.get("media_id")
        mirror_id = record.get("mirror_id")
        filename = record.get("filename")
        if not isinstance(filename, str) or Path(filename).name != filename:
            errors.append(f"{tag}: filename must be one safe basename")
            continue
        if filename in seen_files:
            errors.append(f"{tag}: duplicate filename")
        seen_files.add(filename)
        if not isinstance(mirror_id, str) or mirror_id in seen_mirrors:
            errors.append(f"{tag}: mirror_id is missing or duplicated")
        seen_mirrors.add(mirror_id)

        media = next(
            (value for value in item.get("media") or [] if value.get("media_id") == media_id),
            None,
        )
        if not media:
            errors.append(f"{tag}: media_id does not resolve in the item")
            continue
        mirror = next(
            (
                value
                for value in (media.get("delivery") or {}).get("mirrors") or []
                if value.get("mirror_id") == mirror_id
            ),
            None,
        )
        if not mirror:
            errors.append(f"{tag}: mirror_id does not resolve in the media")
            continue
        if mirror.get("provider") != "github_attachment" or mirror.get("artifact") != "video":
            errors.append(f"{tag}: target must be a GitHub video attachment")
        if (item.get("curation") or {}).get("status") != "approved":
            errors.append(f"{tag}: item is not approved")
        source_record = (
            (catalog.get("sources") or {}).get(item.get("canonical_source_id")) or {}
        )
        source_state = (source_record.get("availability") or {}).get("state")
        if source_state in {"deleted", "private", "suspended"}:
            errors.append(f"{tag}: source availability forbids reactivation")
        current_video_rights = (item.get("rights") or {}).get("video_republication") or {}
        if current_video_rights.get("status") in {
            "denied", "revoked", "public_license",
        }:
            errors.append(f"{tag}: existing video rights forbid recovery activation")
        elif (
            current_video_rights.get("status") == "granted"
            and not matches_existing_attestation(
                current_video_rights,
                grantor,
                {"download", "mirror_github"},
                granted_at,
            )
        ):
            errors.append(f"{tag}: existing video grant differs from this private confirmation")
        current_prompt_rights = (item.get("rights") or {}).get("prompt_republication") or {}
        prompt = item.get("prompt") or {}
        will_attest_prompt = (
            attest_prompts
            and prompt.get("status") == "verbatim"
            and prompt.get("is_verbatim") is True
            and isinstance(prompt.get("text"), str)
            and bool(prompt.get("text"))
        )
        if will_attest_prompt:
            if current_prompt_rights.get("status") in {
                "denied", "revoked", "public_license",
            }:
                errors.append(f"{tag}: existing prompt rights forbid recovery activation")
            elif (
                current_prompt_rights.get("status") == "granted"
                and not matches_existing_attestation(
                    current_prompt_rights,
                    grantor,
                    {"reproduce_prompt"},
                    granted_at,
                )
            ):
                errors.append(
                    f"{tag}: existing prompt grant differs from this private confirmation"
                )

        url = locators.get(mirror_id)
        if not ASSET_RE.fullmatch(str(url or "")):
            errors.append(f"{tag}: private locator is not a GitHub attachment URL")
        url_digest = hashlib.sha256(str(url or "").encode()).hexdigest()
        if mirror.get("former_url_sha256") != url_digest:
            errors.append(f"{tag}: locator does not match the catalog's retired URL digest")
        if mirror.get("state") == "active":
            if (
                mirror.get("url") != url
                or mirror.get("bytes") != record.get("bytes")
                or mirror.get("sha256") != record.get("sha256")
            ):
                errors.append(f"{tag}: active mirror differs from the reviewed recovery record")
        elif mirror.get("state") in {"quarantined", "pending_delete"}:
            retired = retirement_by_mirror.get(mirror_id) or {}
            if (
                retired.get("state") != "pending_delete"
                or retired.get("item_id") != item_id
                or retired.get("media_id") != media_id
                or retired.get("source_id") != media.get("source_id")
                or retired.get("provider") != "github_attachment"
                or retired.get("artifact") != "video"
                or retired.get("url_sha256") != url_digest
            ):
                errors.append(f"{tag}: legacy quarantine does not match the retirement ledger")
        else:
            errors.append(f"{tag}: mirror is not a recoverable legacy quarantine")

        entry = entries_by_item.get(item_id) or {}
        probe = entry.get("probe") or {}
        if (
            entry.get("status") != "success"
            or entry.get("selected_provider") != "github_attachment"
            or entry.get("selected_mirror_id") != mirror_id
            or entry.get("media_id") != media_id
            or entry.get("source_id") != media.get("source_id")
            or entry.get("filename") != filename
            or entry.get("http_status") != 200
            or not str(entry.get("content_type") or "").lower().startswith("video/")
            or probe.get("valid") is not True
        ):
            errors.append(f"{tag}: report does not prove a successful GitHub video download")

        expected_size = record.get("bytes")
        expected_sha = record.get("sha256")
        if not isinstance(expected_size, int) or expected_size <= 0:
            errors.append(f"{tag}: bytes must be a positive integer")
        if not SHA256_RE.fullmatch(str(expected_sha or "")):
            errors.append(f"{tag}: sha256 is invalid")
        if entry.get("bytes") != expected_size or entry.get("sha256") != expected_sha:
            errors.append(f"{tag}: detailed report entry differs from attachment_mapping")
        successful_attempts = [
            attempt for attempt in entry.get("attempts") or []
            if (
                isinstance(attempt, dict)
                and attempt.get("status") == "success"
                and attempt.get("provider") == "github_attachment"
                and attempt.get("mirror_id") == mirror_id
                and attempt.get("locator_sha256") == url_digest
                and attempt.get("bytes") == expected_size
                and attempt.get("sha256") == expected_sha
            )
        ]
        if not successful_attempts:
            errors.append(f"{tag}: no successful attachment attempt binds URL, bytes, and SHA-256")
        file_path = (staging / filename).resolve()
        if file_path.parent != staging or not file_path.is_file():
            errors.append(f"{tag}: verified staging file is missing")
        else:
            actual_size, actual_sha = file_identity(file_path)
            if actual_size != expected_size or actual_sha != expected_sha:
                errors.append(f"{tag}: staging bytes or sha256 differs from the report")

    if errors:
        raise ValueError("\n".join(errors))

    activated: set[str] = set()
    for item_id, record in sorted(mapping.items()):
        item = items[item_id]
        media = next(value for value in item["media"] if value["media_id"] == record["media_id"])
        mirror = next(
            value for value in media["delivery"]["mirrors"]
            if value["mirror_id"] == record["mirror_id"]
        )
        grantor = grantors[item_id]
        item["rights"]["video_republication"] = {
            "status": "granted",
            "license_spdx": None,
            "granted_scopes": ["download", "mirror_github"],
            "grantor_actor_ids": [grantor],
            "granted_at": granted_at,
            "expires_at": None,
            "evidence_ids": [],
            "grant_verification": "maintainer_attestation",
        }
        prompt = item.get("prompt") or {}
        if attest_prompts and (
            prompt.get("status") == "verbatim"
            and prompt.get("is_verbatim") is True
            and isinstance(prompt.get("text"), str)
            and prompt["text"]
        ):
            item["rights"]["prompt_republication"] = {
                "status": "granted",
                "license_spdx": None,
                "granted_scopes": ["reproduce_prompt"],
                "grantor_actor_ids": [grantor],
                "granted_at": granted_at,
                "expires_at": None,
                "evidence_ids": [],
                "grant_verification": "maintainer_attestation",
            }
        mirror.update({
            "url": locators[record["mirror_id"]],
            "bytes": record["bytes"],
            "sha256": record["sha256"],
            "state": "active",
            "permission_evidence_ids": [],
            "verified_at": checked_at,
            "last_checked_at": checked_at,
        })
        media["delivery"]["mode"] = "authorized_mirror"
        activated.add(record["mirror_id"])

    retirement["entries"] = [
        entry for entry in retirement.get("entries") or []
        if entry.get("mirror_id") not in activated
    ]
    retirement["updated_at"] = max(str(retirement.get("updated_at") or ""), checked_at)
    catalog["updated_at"] = max(str(catalog.get("updated_at") or ""), checked_at)
    validation_errors = validate_catalog(catalog)
    if validation_errors:
        raise ValueError("activated catalog is invalid:\n" + "\n".join(validation_errors))
    return catalog, retirement, len(activated)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT / "data/catalog.json")
    parser.add_argument("--retirement", type=Path, default=ROOT / "data/media-retirement.json")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--report-sha256", required=True)
    parser.add_argument("--locator-cache", type=Path, required=True)
    parser.add_argument("--grantor-map", type=Path, required=True)
    parser.add_argument("--grantor-map-sha256", required=True)
    parser.add_argument("--staging", type=Path, required=True)
    parser.add_argument("--granted-at", required=True, help="RFC3339 time recorded for the private grant")
    parser.add_argument(
        "--confirm-maintainer-attestation",
        action="store_true",
        help="confirm the maintainer has separately verified every grant in this report",
    )
    parser.add_argument(
        "--confirm-prompt-republication",
        action="store_true",
        help="separately confirm full-prompt republication for existing verbatim prompts",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    if not args.confirm_maintainer_attestation:
        print("refusing activation without --confirm-maintainer-attestation", file=sys.stderr)
        return 2
    try:
        catalog, retirement, count = activate_catalog(
            read_json(args.catalog),
            read_json(args.retirement),
            read_digest_bound_json(args.report, args.report_sha256, "recovery report"),
            read_json(args.locator_cache),
            read_digest_bound_json(
                args.grantor_map, args.grantor_map_sha256, "private grantor map"
            ),
            args.staging,
            args.granted_at,
            attest_prompts=args.confirm_prompt_republication,
        )
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    if args.dry_run:
        print(f"OK: {count} verified GitHub attachment(s) ready to activate")
        return 0
    write_json(args.catalog, catalog)
    write_json(args.retirement, retirement)
    print(f"activated {count} verified GitHub attachment(s)")
    return 0


def main() -> None:
    raise SystemExit(run(parse_args()))


if __name__ == "__main__":
    main()
