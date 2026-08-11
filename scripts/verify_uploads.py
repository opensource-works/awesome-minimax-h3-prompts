#!/usr/bin/env python3
"""Verify every staged GitHub asset exactly, then activate catalog mirrors."""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import copy
import hashlib
import sys
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from catalog import (  # noqa: E402
    export_posts,
    item_is_public,
    mirror_is_authorized,
    read_json,
    utc_now,
    validate_catalog,
    write_json,
)
from authorized_manifests import (  # noqa: E402
    ASSET_RE,
    SHA256_RE,
    build_github_attachment_manifest,
    build_r2_manifest,
    mirror_id_for,
)

INDEX_VERSION = "github-attachments-index-v2"


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
        if not isinstance(entry.get("permission_evidence_ids"), list) or not entry["permission_evidence_ids"]:
            errors.append(f"{tag} needs permission_evidence_ids")
    return errors


def find_media(item: dict, media_id: str) -> dict | None:
    return next((media for media in item.get("media") or [] if media.get("media_id") == media_id), None)


def find_expected_mirror(catalog: dict, entry: dict) -> tuple[dict | None, dict | None, dict | None, list[str]]:
    item_id = entry.get("item_id")
    media_id = entry.get("media_id")
    tag = f"{item_id}/{media_id}"
    errors: list[str] = []
    item = (catalog.get("items") or {}).get(item_id)
    if not item:
        return None, None, None, [f"{tag}: item is missing"]
    media = find_media(item, media_id)
    if not media or media.get("kind") != "video":
        return item, None, None, [f"{tag}: video media is missing"]
    mirror_id = mirror_id_for(item_id, media_id)
    mirror = next(
        (value for value in ((media.get("delivery") or {}).get("mirrors") or [])
         if value.get("mirror_id") == mirror_id),
        None,
    )
    if not mirror:
        return item, media, None, [f"{tag}: mapped quarantined mirror is missing"]
    if mirror.get("provider") != "github_attachment" or mirror.get("artifact") != "video":
        errors.append(f"{tag}: mirror provider/artifact is invalid")
    if mirror.get("state") not in {"quarantined", "active"}:
        errors.append(f"{tag}: mirror state is {mirror.get('state')!r}, not quarantined/active")
    if mirror.get("staged_filename") != entry.get("filename"):
        errors.append(f"{tag}: staged filename does not match")
    if mirror.get("bytes") != entry.get("bytes"):
        errors.append(f"{tag}: catalog/index byte counts differ")
    if mirror.get("sha256") != entry.get("sha256"):
        errors.append(f"{tag}: catalog/index SHA-256 digests differ")
    if set(mirror.get("permission_evidence_ids") or []) != set(entry.get("permission_evidence_ids") or []):
        errors.append(f"{tag}: permission evidence changed since staging")
    if not ASSET_RE.fullmatch(str(mirror.get("url") or "")):
        errors.append(f"{tag}: URL is not a GitHub user-attachments asset")
    if entry.get("source_id") != media.get("source_id"):
        errors.append(f"{tag}: source_id changed since staging")
    if entry.get("source_media_id") != media.get("source_media_id"):
        errors.append(f"{tag}: source_media_id changed since staging")
    if not item_is_public(item, catalog):
        errors.append(f"{tag}: item is no longer approved/included/available")
    if not mirror_is_authorized(item, mirror, catalog):
        errors.append(f"{tag}: current rights/evidence no longer authorize this mirror")
    return item, media, mirror, errors


def fetch_digest(url: str, timeout: int) -> tuple[int, str]:
    """Perform a full GET and hash every byte; HEAD/range checks are insufficient."""
    request = Request(url, headers={"User-Agent": "opensource-works-upload-verifier/2"})
    digest = hashlib.sha256()
    total = 0
    with urlopen(request, timeout=timeout) as response:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
    return total, digest.hexdigest()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, required=True, help="index.json made by prepare_uploads.py")
    parser.add_argument("--catalog", type=Path, default=ROOT / "data" / "catalog.json")
    parser.add_argument(
        "--manifest", type=Path, default=ROOT / "data" / "github-attachments.json",
        help="generated, namespaced manifest (the retired data/attachments.json is never used)",
    )
    parser.add_argument(
        "--r2-manifest", type=Path, default=ROOT / "data" / "r2-mirrors.json",
        help="generated namespaced R2 manifest",
    )
    parser.add_argument(
        "--posts", type=Path, default=ROOT / "data" / "posts.json",
        help="generated compatibility post projection",
    )
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=600)
    return parser.parse_args(argv)


def run(args: argparse.Namespace, fetcher=fetch_digest) -> int:
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
    if args.jobs <= 0:
        print("jobs must be positive", file=sys.stderr)
        return 2
    if index.get("catalog_schema_version") != catalog.get("schema_version"):
        print("index/catalog schema versions differ", file=sys.stderr)
        return 2
    if index.get("collection_id") != (catalog.get("collection") or {}).get("id"):
        print("index belongs to a different collection", file=sys.stderr)
        return 2

    entries = index["entries"]
    checks: dict[str, tuple[dict, dict]] = {}
    failures: dict[str, str] = {}
    for filename, entry in sorted(entries.items()):
        _item, _media, mirror, errors = find_expected_mirror(catalog, entry)
        if errors:
            failures[filename] = "; ".join(errors)
        elif mirror:
            checks[filename] = (entry, mirror)

    results: dict[str, tuple[int, str]] = {}
    with futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
        submitted = {
            executor.submit(fetcher, mirror["url"], args.timeout): filename
            for filename, (_entry, mirror) in checks.items()
        }
        for future in futures.as_completed(submitted):
            filename = submitted[future]
            try:
                actual_bytes, actual_sha256 = future.result()
                results[filename] = (actual_bytes, actual_sha256)
                entry = entries[filename]
                mismatch = []
                if actual_bytes != entry["bytes"]:
                    mismatch.append(f"bytes expected {entry['bytes']}, got {actual_bytes}")
                if actual_sha256 != entry["sha256"]:
                    mismatch.append(f"sha256 expected {entry['sha256']}, got {actual_sha256}")
                if mismatch:
                    failures[filename] = "; ".join(mismatch)
            except Exception as exc:
                failures[filename] = f"download failed: {exc}"

    verified = len(entries) - len(failures)
    print(f"{verified}/{len(entries)} GitHub assets match exact bytes and SHA-256")
    if failures:
        for filename in sorted(failures):
            print(f"MISMATCH {filename}: {failures[filename]}", file=sys.stderr)
        print("no mirror was activated and no manifest was written", file=sys.stderr)
        return 1

    verified_at = utc_now()
    updated = copy.deepcopy(catalog)
    for filename, entry in sorted(entries.items()):
        item = updated["items"][entry["item_id"]]
        media = find_media(item, entry["media_id"])
        mirror_id = mirror_id_for(entry["item_id"], entry["media_id"])
        mirror = next(
            value for value in media["delivery"]["mirrors"]
            if value.get("mirror_id") == mirror_id
        )
        actual_bytes, actual_sha256 = results[filename]
        mirror["bytes"] = actual_bytes
        mirror["sha256"] = actual_sha256
        mirror["state"] = "active"
        mirror["last_checked_at"] = verified_at
        mirror["verified_at"] = verified_at
        mirror["uploaded_at"] = mirror.get("uploaded_at") or verified_at
        media["delivery"]["mode"] = "authorized_mirror"
    updated["updated_at"] = verified_at

    final_errors = validate_catalog(updated)
    if final_errors:
        for error in final_errors:
            print(f"catalog error after verification: {error}", file=sys.stderr)
        return 2
    posts = export_posts(updated)
    manifest = build_github_attachment_manifest(updated, verified_at)
    r2_manifest = build_r2_manifest(updated, verified_at)
    write_json(args.catalog, updated)
    write_json(args.posts, posts)
    write_json(args.manifest, manifest)
    write_json(args.r2_manifest, r2_manifest)
    print(
        f"activated {len(entries)} mirror(s); wrote {len(manifest['attachments'])} "
        f"authorized entries and refreshed posts plus both media manifests"
    )
    return 0


def main() -> None:
    raise SystemExit(run(parse_args()))


if __name__ == "__main__":
    main()
