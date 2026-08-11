#!/usr/bin/env python3
"""Ingest explicit filename-to-GitHub-asset mappings into catalog v2.

This command never guesses from upload order and never activates a mirror.  A
new mapping remains quarantined until verify_uploads.py downloads and matches
every staged asset byte-for-byte.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from catalog import (  # noqa: E402
    item_is_public,
    mirror_is_authorized,
    read_json,
    utc_now,
    validate_catalog,
    write_json,
)

INDEX_VERSION = "github-attachments-index-v2"
ASSET_BODY = (
    r"https://github\.com/user-attachments/assets/"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
ASSET_RE = re.compile(ASSET_BODY)
MARKDOWN_RE = re.compile(r"\[([^\]\r\n]+\.mp4)\]\((" + ASSET_BODY + r")\)", re.IGNORECASE)
EXPLICIT_RE = re.compile(
    r"^\s*([A-Za-z0-9._-]+\.mp4)\s*(?:=|:)\s*(" + ASSET_BODY + r")\s*$",
    re.IGNORECASE | re.MULTILINE,
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def mirror_id_for(item_id: str, media_id: str) -> str:
    digest = hashlib.sha256(f"{item_id}\0{media_id}".encode()).hexdigest()[:24]
    return f"mir_github_{digest}"


def parse_mappings(text: str) -> tuple[dict[str, str], list[str]]:
    pairs = list(MARKDOWN_RE.findall(text)) + list(EXPLICIT_RE.findall(text))
    mappings: dict[str, str] = {}
    errors: list[str] = []
    url_owner: dict[str, str] = {}
    for raw_filename, url in pairs:
        filename = Path(raw_filename.strip()).name
        if filename != raw_filename.strip():
            errors.append(f"filename must not contain a path: {raw_filename}")
            continue
        old_url = mappings.get(filename)
        if old_url and old_url != url:
            errors.append(f"{filename} is mapped to more than one asset URL")
            continue
        old_filename = url_owner.get(url)
        if old_filename and old_filename != filename:
            errors.append(f"one asset URL is mapped to both {old_filename} and {filename}")
            continue
        mappings[filename] = url
        url_owner[url] = filename

    all_urls = set(ASSET_RE.findall(text))
    unmapped_urls = sorted(all_urls - set(mappings.values()))
    if unmapped_urls:
        errors.append(
            f"{len(unmapped_urls)} bare asset URL(s) lack a filename; use `filename.mp4 = URL`"
        )
    if not mappings:
        errors.append("no explicit filename-to-asset mappings found")
    return mappings, errors


def validate_index(index: dict) -> list[str]:
    errors: list[str] = []
    if index.get("schema_version") != INDEX_VERSION:
        errors.append(f"index schema_version must be {INDEX_VERSION}")
    if index.get("identity") != "item_id/media_id":
        errors.append("index identity must be item_id/media_id")
    entries = index.get("entries")
    if not isinstance(entries, dict) or not entries:
        return errors + ["index entries must be a non-empty object keyed by filename"]
    identities: set[tuple[str, str]] = set()
    for filename, entry in entries.items():
        tag = f"entries.{filename}"
        if not isinstance(entry, dict):
            errors.append(f"{tag} must be an object")
            continue
        if entry.get("filename") != filename or Path(filename).name != filename:
            errors.append(f"{tag} filename/key mismatch")
        identity = (entry.get("item_id"), entry.get("media_id"))
        if not all(isinstance(value, str) and value for value in identity):
            errors.append(f"{tag} needs item_id and media_id")
        elif identity in identities:
            errors.append(f"{tag} duplicates item/media identity")
        else:
            identities.add(identity)
        if not isinstance(entry.get("bytes"), int) or entry["bytes"] <= 0:
            errors.append(f"{tag}.bytes must be positive actual bytes")
        if not SHA256_RE.fullmatch(str(entry.get("sha256") or "")):
            errors.append(f"{tag}.sha256 must be an actual SHA-256 digest")
        evidence_ids = entry.get("permission_evidence_ids")
        if not isinstance(evidence_ids, list) or not evidence_ids:
            errors.append(f"{tag} needs permission_evidence_ids")
    return errors


def find_media(item: dict, media_id: str) -> dict | None:
    return next((media for media in item.get("media") or [] if media.get("media_id") == media_id), None)


def validate_entry(entry: dict, catalog: dict) -> tuple[dict | None, dict | None, list[str]]:
    item_id = entry.get("item_id")
    media_id = entry.get("media_id")
    tag = f"{item_id}/{media_id}"
    errors: list[str] = []
    item = (catalog.get("items") or {}).get(item_id)
    if not item:
        return None, None, [f"{tag}: item does not exist"]
    media = find_media(item, media_id)
    if not media or media.get("kind") != "video":
        return item, None, [f"{tag}: video media does not exist"]
    if entry.get("source_id") != media.get("source_id"):
        errors.append(f"{tag}: source_id changed since staging")
    if entry.get("source_media_id") != media.get("source_media_id"):
        errors.append(f"{tag}: source_media_id changed since staging")
    if not item_is_public(item, catalog):
        errors.append(f"{tag}: item is no longer approved/included/available")
    evidence_ids = entry.get("permission_evidence_ids") or []
    probe = {
        "provider": "github_attachment",
        "artifact": "video",
        "permission_evidence_ids": evidence_ids,
    }
    if not mirror_is_authorized(item, probe, catalog):
        errors.append(f"{tag}: current rights do not authorize download + mirror_github with this evidence")
    return item, media, errors


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pasted", type=Path, help="text copied from the GitHub upload editor")
    parser.add_argument("--index", type=Path, required=True, help="index.json made by prepare_uploads.py")
    parser.add_argument("--catalog", type=Path, default=ROOT / "data" / "catalog.json")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    index = read_json(args.index)
    index_errors = validate_index(index or {})
    if index_errors:
        for error in index_errors:
            print(f"index error: {error}", file=sys.stderr)
        return 2
    catalog = read_json(args.catalog)
    catalog_errors = validate_catalog(catalog or {})
    if catalog_errors:
        for error in catalog_errors:
            print(f"catalog error: {error}", file=sys.stderr)
        return 2
    if index.get("catalog_schema_version") != catalog.get("schema_version"):
        print("index/catalog schema versions differ", file=sys.stderr)
        return 2
    if index.get("collection_id") != (catalog.get("collection") or {}).get("id"):
        print("index belongs to a different collection", file=sys.stderr)
        return 2

    mappings, mapping_errors = parse_mappings(args.pasted.read_text())
    entries = index["entries"]
    unknown = sorted(set(mappings) - set(entries))
    mapping_errors.extend(f"filename is not present in this index: {name}" for name in unknown)

    plans = []
    for filename, url in sorted(mappings.items()):
        if filename not in entries:
            continue
        entry = entries[filename]
        item, media, entry_errors = validate_entry(entry, catalog)
        mapping_errors.extend(entry_errors)
        if item and media and not entry_errors:
            plans.append((filename, url, entry, item, media))
    if mapping_errors:
        for error in mapping_errors:
            print(f"mapping error: {error}", file=sys.stderr)
        return 2

    ingested_at = utc_now()
    changed = 0
    for filename, url, entry, item, media in plans:
        mirror_id = mirror_id_for(entry["item_id"], entry["media_id"])
        mirrors = (media.setdefault("delivery", {})).setdefault("mirrors", [])
        existing = next((mirror for mirror in mirrors if mirror.get("mirror_id") == mirror_id), None)
        record = {
            "mirror_id": mirror_id,
            "provider": "github_attachment",
            "artifact": "video",
            "url": url,
            "bytes": entry["bytes"],
            "sha256": entry["sha256"],
            "width": entry.get("width"),
            "height": entry.get("height"),
            "uploaded_at": ingested_at,
            "state": "quarantined",
            "permission_evidence_ids": list(entry["permission_evidence_ids"]),
            "last_checked_at": None,
            "ingested_at": ingested_at,
            "staged_filename": filename,
        }
        if existing and existing.get("state") == "active":
            immutable = ("provider", "artifact", "url", "bytes", "sha256", "staged_filename")
            if any(existing.get(key) != record.get(key) for key in immutable):
                print(f"mapping error: refusing to replace active mirror {mirror_id}", file=sys.stderr)
                return 2
            continue
        if existing:
            existing.clear()
            existing.update(record)
        else:
            mirrors.append(record)
        changed += 1

    catalog["updated_at"] = ingested_at
    final_errors = validate_catalog(catalog)
    if final_errors:
        for error in final_errors:
            print(f"catalog error after ingest: {error}", file=sys.stderr)
        return 2
    write_json(args.catalog, catalog)

    missing = sorted(set(entries) - set(mappings))
    print(f"{len(mappings)}/{len(entries)} filename mappings ingested; {changed} quarantined mirror(s) updated")
    if missing:
        print(f"{len(missing)} staged file(s) still lack an explicit mapping")
    else:
        print("all mappings are quarantined; run verify_uploads.py before they can become public")
    return 0


def main() -> None:
    raise SystemExit(run(parse_args()))


if __name__ == "__main__":
    main()
