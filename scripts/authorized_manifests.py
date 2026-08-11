#!/usr/bin/env python3
"""Rebuild or check authorized-media manifests from the canonical catalog.

The manifests are disposable projections. Revocation, takedown, availability
changes, or any catalog edit can only remove an entry by changing
``data/catalog.json`` and regenerating this file; the retired v1
``data/attachments.json`` is never read or written here.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from catalog import (  # noqa: E402
    item_is_public,
    mirror_is_authorized,
    public_catalog,
    read_json,
    utc_now,
    validate_catalog,
    write_json,
)
MANIFEST_VERSION = "github-attachments-manifest-v2"
R2_MANIFEST_VERSION = "r2-mirrors-manifest-v2"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ASSET_RE = re.compile(
    r"https://github\.com/user-attachments/assets/"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def build_r2_manifest(catalog: dict, generated_at: str | None = None) -> dict:
    """Project only current, public, rights-gated R2 objects."""
    catalog = public_catalog(catalog)
    mirrors: dict[str, dict] = {}
    for item_id in sorted(catalog.get("items") or {}):
        item = catalog["items"][item_id]
        if not item_is_public(item, catalog):
            continue
        for media in sorted(
            item.get("media") or [], key=lambda value: value.get("media_id") or ""
        ):
            media_id = media.get("media_id")
            for record in sorted(
                (media.get("delivery") or {}).get("mirrors") or [],
                key=lambda value: value.get("mirror_id") or "",
            ):
                if record.get("provider") != "r2" or record.get("state") != "active":
                    continue
                if not mirror_is_authorized(item, record, catalog):
                    continue
                mirror_id = record.get("mirror_id")
                if not media_id or not mirror_id:
                    continue
                namespace = f"{item_id}/{media_id}/{mirror_id}"
                mirrors[namespace] = {
                    "item_id": item_id,
                    "media_id": media_id,
                    "mirror_id": mirror_id,
                    "source_id": media.get("source_id"),
                    "artifact": record.get("artifact"),
                    "url": record.get("url"),
                    "bytes": record.get("bytes"),
                    "sha256": record.get("sha256"),
                    "permission_evidence_ids": list(
                        record.get("permission_evidence_ids") or []
                    ),
                    "uploaded_at": record.get("uploaded_at"),
                    "last_checked_at": record.get("last_checked_at"),
                }
    return {
        "schema_version": R2_MANIFEST_VERSION,
        "catalog_schema_version": catalog.get("schema_version"),
        "generated_at": generated_at or catalog.get("updated_at") or utc_now(),
        "namespace": "item_id/media_id/mirror_id",
        "mirrors": mirrors,
    }


def _is_rfc3339(value) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def mirror_id_for(item_id: str, media_id: str) -> str:
    digest = hashlib.sha256(f"{item_id}\0{media_id}".encode()).hexdigest()[:24]
    return f"mir_github_{digest}"


def build_github_attachment_manifest(catalog: dict, generated_at: str | None = None) -> dict:
    """Project only current, public, authorized and integrity-checked assets."""
    catalog = public_catalog(catalog)
    attachments: dict[str, dict] = {}
    for item_id in sorted(catalog.get("items") or {}):
        item = catalog["items"][item_id]
        if not item_is_public(item, catalog):
            continue
        for media in sorted(item.get("media") or [], key=lambda value: value.get("media_id") or ""):
            media_id = media.get("media_id")
            for mirror in sorted(
                (media.get("delivery") or {}).get("mirrors") or [],
                key=lambda value: value.get("mirror_id") or "",
            ):
                if mirror.get("state") != "active":
                    continue
                if mirror.get("provider") != "github_attachment" or mirror.get("artifact") != "video":
                    continue
                if not mirror_is_authorized(item, mirror, catalog):
                    continue
                if not ASSET_RE.fullmatch(str(mirror.get("url") or "")):
                    continue
                if not isinstance(mirror.get("bytes"), int) or mirror["bytes"] <= 0:
                    continue
                if not SHA256_RE.fullmatch(str(mirror.get("sha256") or "")):
                    continue
                mirror_id = mirror.get("mirror_id")
                if not media_id or not mirror_id:
                    continue
                namespace = f"{item_id}/{media_id}/{mirror_id}"
                attachments[namespace] = {
                    "item_id": item_id,
                    "media_id": media_id,
                    "mirror_id": mirror_id,
                    "source_id": media.get("source_id"),
                    "url": mirror["url"],
                    "bytes": mirror["bytes"],
                    "sha256": mirror["sha256"],
                    "permission_evidence_ids": list(mirror.get("permission_evidence_ids") or []),
                    "verified_at": mirror.get("verified_at") or mirror.get("last_checked_at"),
                }
    return {
        "schema_version": MANIFEST_VERSION,
        "catalog_schema_version": catalog.get("schema_version"),
        "generated_at": generated_at or catalog.get("updated_at") or utc_now(),
        "namespace": "item_id/media_id/mirror_id",
        "attachments": attachments,
    }


def validate_github_attachment_manifest(catalog: dict, manifest: dict | None) -> list[str]:
    """Require the committed manifest to be the exact canonical projection."""
    if not isinstance(manifest, dict):
        return ["data/github-attachments.json must be an object"]
    generated_at = manifest.get("generated_at")
    errors = []
    if not _is_rfc3339(generated_at):
        errors.append("data/github-attachments.json generated_at must be RFC3339")
        return errors
    expected = build_github_attachment_manifest(catalog, generated_at)
    if manifest != expected:
        errors.append(
            "data/github-attachments.json is stale, malformed, or not the exact "
            "active-authorized catalog projection"
        )
    return errors


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT / "data" / "catalog.json")
    parser.add_argument("--manifest", type=Path, default=ROOT / "data" / "github-attachments.json")
    parser.add_argument("--r2-manifest", type=Path, default=ROOT / "data" / "r2-mirrors.json")
    parser.add_argument("--check", action="store_true", help="fail instead of writing when stale")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    catalog = read_json(args.catalog)
    errors = validate_catalog(catalog or {})
    if errors:
        for error in errors:
            print(f"catalog error: {error}", file=sys.stderr)
        return 2
    generated_at = catalog.get("updated_at") or utc_now()
    expected = build_github_attachment_manifest(catalog, generated_at)
    expected_r2 = build_r2_manifest(catalog, generated_at)
    current = read_json(args.manifest)
    current_r2 = read_json(args.r2_manifest)
    if args.check:
        errors = validate_github_attachment_manifest(catalog, current)
        if not isinstance(current_r2, dict) or not _is_rfc3339(current_r2.get("generated_at")):
            errors.append("data/r2-mirrors.json is missing or malformed")
        elif current_r2 != build_r2_manifest(catalog, current_r2["generated_at"]):
            errors.append("data/r2-mirrors.json is stale or not the exact public projection")
        if errors:
            for error in errors:
                print(error, file=sys.stderr)
            return 1
        print(
            f"OK: {len(current['attachments'])} GitHub attachment(s), "
            f"{len(current_r2['mirrors'])} R2 mirror(s)"
        )
        return 0
    if current != expected:
        write_json(args.manifest, expected)
        print(f"wrote {len(expected['attachments'])} authorized GitHub attachment(s) to {args.manifest}")
    else:
        print(f"unchanged: {len(expected['attachments'])} authorized GitHub attachment(s)")
    if current_r2 != expected_r2:
        write_json(args.r2_manifest, expected_r2)
        print(f"wrote {len(expected_r2['mirrors'])} authorized R2 mirror(s) to {args.r2_manifest}")
    else:
        print(f"unchanged: {len(expected_r2['mirrors'])} authorized R2 mirror(s)")
    return 0


def main() -> None:
    raise SystemExit(run(parse_args()))


if __name__ == "__main__":
    main()
