#!/usr/bin/env python3
"""List retired mirrors and, with explicit confirmation, delete R2 objects.

The command is deliberately read-only by default. GitHub user attachments do
not expose a repository-scoped deletion API, so they are always reported for
manual escalation and are never marked deleted by this tool.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from catalog import (  # noqa: E402
    export_posts, mirror_needs_cleanup, read_json, utc_now, validate_catalog, write_json,
)


def cleanup_jobs(catalog: dict, provider: str, item_id: str | None) -> list[dict]:
    jobs: list[dict] = []
    for current_item_id, item in catalog.get("items", {}).items():
        if item_id and current_item_id != item_id:
            continue
        for media in item.get("media") or []:
            for mirror in (media.get("delivery") or {}).get("mirrors") or []:
                if not mirror_needs_cleanup(mirror):
                    continue
                if provider != "all" and mirror.get("provider") != provider:
                    continue
                jobs.append({
                    "item_id": current_item_id,
                    "media_id": media.get("media_id"),
                    "source_id": media.get("source_id"),
                    "mirror": mirror,
                })
    return sorted(jobs, key=lambda row: (row["item_id"], row["media_id"] or "", row["mirror"].get("mirror_id") or ""))


def r2_key(url: str, public_base: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    base = urllib.parse.urlsplit(public_base.rstrip("/"))
    if parsed.scheme != base.scheme or parsed.netloc != base.netloc:
        raise ValueError("mirror URL is outside config.r2.public_base")
    base_path = base.path.rstrip("/")
    if base_path and not parsed.path.startswith(base_path + "/"):
        raise ValueError("mirror URL is outside the configured R2 path")
    path = parsed.path[len(base_path):].lstrip("/")
    if not path:
        raise ValueError("mirror URL has no R2 object key")
    return urllib.parse.unquote(path)


def validate_r2_preflight(
    jobs: list[dict], retirement: dict, public_base: str, url_map: dict[str, str]
) -> list[str]:
    """Validate the entire selected deletion batch before any R2 request.

    The URL-redacted retirement ledger is the deletion authorization record.
    A missing, duplicate, stale, or mismatched record rejects the whole batch;
    callers must not issue HEAD or DELETE requests when this returns errors.
    """
    records_by_id: dict[str, list[dict]] = {}
    for record in retirement.get("entries") or []:
        mirror_id = record.get("mirror_id")
        if mirror_id:
            records_by_id.setdefault(mirror_id, []).append(record)

    errors: list[str] = []
    selected_ids: set[str] = set()
    for job in jobs:
        mirror = job.get("mirror") or {}
        mirror_id = mirror.get("mirror_id")
        tag = mirror_id or f"{job.get('item_id')}/{job.get('media_id')}"
        if not mirror_id:
            errors.append(f"{tag}: mirror_id is missing")
            continue
        if mirror_id in selected_ids:
            errors.append(f"{tag}: mirror_id is selected more than once")
        selected_ids.add(mirror_id)

        matches = records_by_id.get(mirror_id) or []
        if len(matches) != 1:
            if not matches:
                errors.append(f"{tag}: retirement record is missing")
            else:
                errors.append(f"{tag}: retirement record is duplicated")
            continue
        record = matches[0]
        if record.get("state") != "pending_delete":
            errors.append(f"{tag}: retirement state must be pending_delete")

        expected = {
            "provider": mirror.get("provider"),
            "artifact": mirror.get("artifact"),
            "item_id": job.get("item_id"),
            "media_id": job.get("media_id"),
            "source_id": job.get("source_id"),
        }
        if expected["provider"] != "r2":
            errors.append(f"{tag}: selected mirror provider must be r2")
        for field, value in expected.items():
            if record.get(field) != value:
                errors.append(
                    f"{tag}: retirement {field} does not match the catalog mirror"
                )

        url = url_map.get(mirror_id)
        if not isinstance(url, str) or not url:
            errors.append(f"{tag}: private URL map entry is missing")
            continue
        actual_digest = hashlib.sha256(url.encode()).hexdigest()
        if record.get("url_sha256") != actual_digest:
            errors.append(f"{tag}: retirement url_sha256 does not match the private URL")
        if mirror.get("former_url_sha256") != actual_digest:
            errors.append(f"{tag}: catalog former_url_sha256 does not match the private URL")
        try:
            r2_key(url, public_base)
        except ValueError as exc:
            errors.append(f"{tag}: {exc}")
    return errors


def load_private_url_map(path: Path) -> dict[str, str]:
    """Load an uncommitted mirror_id -> URL map from a private local path."""
    resolved = path.expanduser().resolve()
    try:
        relative = resolved.relative_to(ROOT.resolve())
    except ValueError:
        pass
    else:
        if not relative.parts or relative.parts[0] != ".cache":
            raise SystemExit(
                "a --url-map inside the repository must be under gitignored .cache/"
            )
    if resolved.stat().st_mode & 0o077:
        raise SystemExit("--url-map must have mode 0600 or stricter")
    value = read_json(resolved)
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(url, str)
        for key, url in value.items()
    ):
        raise SystemExit("--url-map must be a JSON object of mirror_id -> URL strings")
    return value


def mark_retirement(retirement: dict, mirror_id: str, at: str, result: str) -> None:
    for entry in retirement.get("entries") or []:
        if entry.get("mirror_id") == mirror_id:
            entry["state"] = "deleted"
            entry["deleted_at"] = at
            entry["delete_result"] = result
            entry["note"] = "Remote R2 object deleted or confirmed absent."


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["all", "r2", "github_attachment"], default="all")
    parser.add_argument("--item", help="limit the plan to one catalog item ID")
    parser.add_argument("--limit", type=int, help="limit the number of listed/deleted mirrors")
    parser.add_argument(
        "--url-map", type=Path,
        help="private mirror_id -> URL JSON map outside the repo or under gitignored .cache/",
    )
    parser.add_argument("--confirm-delete-r2", action="store_true", help="perform irreversible R2 deletion")
    parser.add_argument("--at", help="RFC3339 audit timestamp; defaults to current UTC time")
    args = parser.parse_args()

    if args.confirm_delete_r2 and args.provider != "r2":
        raise SystemExit("--confirm-delete-r2 requires the explicit selector --provider r2")
    if args.confirm_delete_r2 and not args.url_map:
        raise SystemExit("--confirm-delete-r2 requires an explicit private --url-map")

    catalog_path = ROOT / "data" / "catalog.json"
    retirement_path = ROOT / "data" / "media-retirement.json"
    catalog = read_json(catalog_path)
    retirement = read_json(retirement_path)
    initial_errors = validate_catalog(catalog)
    if initial_errors:
        raise SystemExit(
            "refusing cleanup from an invalid catalog:\n"
            + "\n".join(f"- {error}" for error in initial_errors)
        )
    jobs = cleanup_jobs(catalog, args.provider, args.item)
    if args.limit is not None:
        if args.limit < 1:
            raise SystemExit("--limit must be at least 1")
        jobs = jobs[:args.limit]

    counts: dict[str, int] = {}
    for job in jobs:
        provider = job["mirror"].get("provider") or "unknown"
        counts[provider] = counts.get(provider, 0) + 1
        print(f"- {provider}: {job['item_id']} / {job['media_id']} / {job['mirror'].get('mirror_id')}")
    print(f"{len(jobs)} retired mirrors selected: {counts}")

    if not args.confirm_delete_r2:
        if counts.get("github_attachment"):
            print("GitHub user attachments require a maintainer/platform takedown request; no deletion was attempted.")
        print(
            "Dry run only. Use --provider r2 --url-map /private/locators.json "
            "--confirm-delete-r2 to delete selected R2 objects."
        )
        return

    config = read_json(ROOT / "config" / "collection.json")
    bucket = config["r2"]["bucket"]
    public_base = config["r2"]["public_base"]
    url_map = load_private_url_map(args.url_map)
    preflight_errors = validate_r2_preflight(jobs, retirement, public_base, url_map)
    if preflight_errors:
        raise SystemExit(
            "refusing the entire R2 deletion batch; retirement preflight failed:\n"
            + "\n".join(f"- {error}" for error in preflight_errors)
        )

    import r2  # Credentials are required only after the full batch passes preflight.

    at = args.at or utc_now()
    failures: list[str] = []
    deleted = 0
    for job in jobs:
        mirror = job["mirror"]
        mirror_id = mirror.get("mirror_id") or "unknown"
        url = url_map.get(mirror_id)
        try:
            if not url:
                raise ValueError("mirror URL is missing")
            key = r2_key(url, public_base)
            existed = r2.head(bucket, key)
            if existed:
                r2.delete(bucket, key)
                if r2.head(bucket, key):
                    raise RuntimeError("object still exists after DELETE")
            result = "deleted" if existed else "already_missing"
            mirror["state"] = "deleted"
            mirror["deleted_at"] = at
            mirror["delete_result"] = result
            mirror["former_url_sha256"] = hashlib.sha256(url.encode()).hexdigest()
            mirror["url"] = None
            mark_retirement(retirement, mirror_id, at, result)
            deleted += 1
            print(f"{result}: {mirror_id}")
        except Exception as exc:
            failures.append(f"{mirror_id}: {exc}")

    if deleted:
        catalog["updated_at"] = at
        errors = validate_catalog(catalog)
        if errors:
            raise SystemExit("cleanup produced an invalid catalog:\n" + "\n".join(f"- {error}" for error in errors))
        retirement["updated_at"] = at
        write_json(catalog_path, catalog)
        write_json(ROOT / "data" / "posts.json", export_posts(catalog))
        write_json(retirement_path, retirement)
    if failures:
        print(f"{len(failures)} cleanup failures:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
