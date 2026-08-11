#!/usr/bin/env python3
"""Stage rights-cleared catalog-v2 videos for manual GitHub upload.

The staged filename is derived only from the stable item/media identities.  The
index records the bytes and SHA-256 actually written to disk; rankings, views,
and iteration order never participate in identity.
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import hashlib
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from catalog import (  # noqa: E402
    item_is_public,
    media_observation,
    mirror_is_authorized,
    read_json,
    utc_now,
    validate_catalog,
    write_json,
)

INDEX_VERSION = "github-attachments-index-v2"
DEFAULT_OUTDIR = ROOT.parent / "minimax-h3-github-uploads"
DEFAULT_LIMIT_MB = int(os.environ.get("UPLOAD_LIMIT_MB", "90"))
VOLATILE_CACHE_VERSION = "volatile-media-cache-v1"
VIDEO_MEDIA_HOSTS = {
    "x": {"video.twimg.com"},
    "reddit": {"v.redd.it", "packaged-media.redd.it"},
}
THUMBNAIL_MEDIA_HOSTS = {
    "x": {"pbs.twimg.com", "video.twimg.com"},
    "reddit": {"preview.redd.it", "external-preview.redd.it", "i.redd.it"},
}


class TooLargeError(RuntimeError):
    """The candidate exceeded the configured attachment limit."""


def _validated_media_url(
    url: str | None, platform: str, artifact: str = "video"
) -> str | None:
    if url is None:
        return None
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid media URL port: {url}") from exc
    if (
        parsed.scheme != "https" or not parsed.hostname
        or parsed.username is not None or parsed.password is not None
        or port not in {None, 443}
        or parsed.hostname.casefold() not in (
            VIDEO_MEDIA_HOSTS if artifact == "video" else THUMBNAIL_MEDIA_HOSTS
        ).get(platform, set())
    ):
        raise ValueError(f"media URL is not an allowed {platform} official-media HTTPS URL")
    return url


def load_volatile_cache(path: Path | None, catalog: dict) -> dict[str, dict]:
    if path is None:
        return {}
    resolved = path.expanduser().resolve()
    permissions = stat.S_IMODE(resolved.stat().st_mode)
    if permissions & 0o077:
        raise ValueError("volatile cache must have mode 0600 (no group/other access)")
    cache = read_json(resolved)
    if not isinstance(cache, dict) or cache.get("schema_version") != VOLATILE_CACHE_VERSION:
        raise ValueError(f"volatile cache schema must be {VOLATILE_CACHE_VERSION}")
    if cache.get("collection_id") != (catalog.get("collection") or {}).get("id"):
        raise ValueError("volatile cache belongs to a different collection")
    observations = cache.get("observations") or {}
    if not isinstance(observations, dict):
        raise ValueError("volatile cache observations must be an object")
    return observations


def overlay_volatile_observation(source: dict, observation: dict,
                                 volatile_cache: dict[str, dict]) -> dict:
    """Overlay only an exact, platform-host-validated private locator record."""
    source_id = source.get("id")
    source_media_id = observation.get("source_media_id")
    cached = volatile_cache.get(f"{source_id}/{source_media_id}")
    if not cached:
        return dict(observation)
    if not isinstance(cached, dict):
        raise ValueError("volatile media cache entry must be an object")
    if cached.get("source_id") != source_id:
        raise ValueError("volatile media cache source_id mismatch")
    if cached.get("source_media_id") != source_media_id:
        raise ValueError("volatile media cache source_media_id mismatch")
    platform = source.get("platform")
    variants = cached.get("variants") or []
    if not isinstance(variants, list) or any(not isinstance(value, dict) for value in variants):
        raise ValueError("volatile media variants must be an array of objects")
    safe_variants = []
    for variant in variants:
        safe = dict(variant)
        safe["url"] = _validated_media_url(safe.get("url"), platform, "video")
        safe_variants.append(safe)
    output = dict(observation)
    output.update({
        "direct_url": _validated_media_url(cached.get("direct_url"), platform, "video"),
        "thumbnail_url": _validated_media_url(
            cached.get("thumbnail_url"), platform, "thumbnail"
        ),
        "variants": safe_variants,
    })
    return output


def safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-") or "id"
    return cleaned[:90]


def stable_filename(item_id: str, media_id: str) -> str:
    identity = f"{item_id}\0{media_id}"
    suffix = hashlib.sha256(identity.encode()).hexdigest()[:12]
    return f"ghatt--{safe_component(item_id)}--{safe_component(media_id)}--{suffix}.mp4"


def prospective_mirror(evidence_ids: list[str]) -> dict:
    return {
        "provider": "github_attachment",
        "artifact": "video",
        "permission_evidence_ids": evidence_ids,
    }


def candidate_variants(observation: dict, max_height: int) -> list[dict]:
    candidates: list[dict] = []
    seen: set[str] = set()

    def add(value: dict) -> None:
        url = value.get("url")
        if not url or url in seen:
            return
        container = str(value.get("container") or "").lower()
        path = urlparse(url).path.lower()
        if container and container != "mp4":
            return
        if not container and not path.endswith(".mp4"):
            return
        seen.add(url)
        candidates.append({
            "url": url,
            "width": int(value.get("width") or 0),
            "height": int(value.get("height") or 0),
            "bitrate": int(value.get("bitrate") or 0),
        })

    for variant in observation.get("variants") or []:
        add(variant)
    if observation.get("direct_url"):
        add({
            "url": observation["direct_url"],
            "width": observation.get("width"),
            "height": observation.get("height"),
            "bitrate": 0,
            "container": observation.get("container"),
        })

    within_limit = [v for v in candidates if not v["height"] or v["height"] <= max_height]
    pool = within_limit or sorted(
        candidates,
        key=lambda v: ((v["width"] or 10**9) * (v["height"] or 10**9), v["bitrate"]),
    )[:1]
    return sorted(
        pool,
        key=lambda v: (v["width"] * v["height"], v["bitrate"], v["url"]),
        reverse=True,
    )


def download_file(
    url: str, destination: Path, limit: int, timeout: int, platform: str
) -> tuple[int, str]:
    """Download one complete candidate atomically and return actual bytes/hash."""
    _validated_media_url(url, platform, "video")
    request = Request(url, headers={"User-Agent": "opensource-works-upload-preparer/2"})
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{destination.name}.", suffix=".part",
            dir=destination.parent, delete=False,
        ) as output:
            temporary = Path(output.name)
            with urlopen(request, timeout=timeout) as response:
                _validated_media_url(response.geturl(), platform, "video")
                content_type = (
                    response.headers.get("Content-Type") or ""
                ).split(";", 1)[0].strip().lower()
                if not content_type.startswith("video/"):
                    raise RuntimeError(
                        f"unexpected media content type {content_type or 'missing'}"
                    )
                declared = response.headers.get("Content-Length")
                if declared:
                    try:
                        declared_size = int(declared)
                    except ValueError as exc:
                        raise RuntimeError("invalid Content-Length") from exc
                    if declared_size > limit:
                        raise TooLargeError(f"declared size {declared_size} exceeds {limit}")
                digest = hashlib.sha256()
                total = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise TooLargeError(f"download exceeded {limit} bytes")
                    digest.update(chunk)
                    output.write(chunk)
        if total <= 0:
            raise RuntimeError("download was empty")
        os.replace(temporary, destination)
        temporary = None
        return total, digest.hexdigest()
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def eligible_jobs(catalog: dict, volatile_cache: dict[str, dict] | None = None) -> tuple[list[dict], list[str]]:
    volatile_cache = volatile_cache or {}
    jobs: list[dict] = []
    errors: list[str] = []
    filenames: set[str] = set()
    for item_id in sorted(catalog.get("items") or {}):
        item = catalog["items"][item_id]
        if not item_is_public(item, catalog):
            continue
        rights = ((item.get("rights") or {}).get("video_republication") or {})
        evidence_ids = list(rights.get("evidence_ids") or [])
        if not mirror_is_authorized(item, prospective_mirror(evidence_ids), catalog):
            continue
        for media in sorted(item.get("media") or [], key=lambda value: value.get("media_id") or ""):
            if media.get("kind") != "video":
                continue
            delivery = media.get("delivery") or {}
            if any(
                mirror.get("state") == "active"
                and mirror.get("provider") == "github_attachment"
                and mirror.get("artifact") == "video"
                and mirror_is_authorized(item, mirror, catalog)
                for mirror in delivery.get("mirrors") or []
            ):
                continue
            media_id = media.get("media_id")
            source_id = media.get("source_id")
            source_media_id = media.get("source_media_id")
            source = (catalog.get("sources") or {}).get(source_id)
            observation = media_observation(source or {}, source_media_id)
            if not media_id or not source_id or not source_media_id or not source:
                errors.append(f"{item_id}: video media has incomplete stable identity")
                continue
            if not observation or observation.get("source_media_id") != source_media_id:
                errors.append(f"{item_id}/{media_id}: exact source media observation is missing")
                continue
            try:
                observation = overlay_volatile_observation(source, observation, volatile_cache)
            except ValueError as exc:
                errors.append(f"{item_id}/{media_id}: {exc}")
                continue
            filename = stable_filename(item_id, media_id)
            if filename in filenames:
                errors.append(f"stable filename collision: {filename}")
                continue
            filenames.add(filename)
            jobs.append({
                "item_id": item_id,
                "media_id": media_id,
                "source_id": source_id,
                "source_media_id": source_media_id,
                "source_url": source.get("url"),
                "platform": source.get("platform"),
                "filename": filename,
                "permission_evidence_ids": evidence_ids,
                "rights_status": rights.get("status"),
                "observation": observation,
            })
    return jobs, errors


def stage_one(job: dict, outdir: Path, limit: int, max_height: int, timeout: int) -> dict:
    variants = candidate_variants(job["observation"], max_height)
    if not variants:
        raise RuntimeError("no downloadable MP4 variant")
    failures = []
    destination = outdir / job["filename"]
    for variant in variants:
        try:
            byte_count, sha256 = download_file(
                variant["url"], destination, limit, timeout, job["platform"]
            )
            return {
                "filename": job["filename"],
                "item_id": job["item_id"],
                "media_id": job["media_id"],
                "source_id": job["source_id"],
                "source_media_id": job["source_media_id"],
                "source_url": job["source_url"],
                "download_url": variant["url"],
                "content_type": "video/mp4",
                "width": variant["width"] or None,
                "height": variant["height"] or None,
                "bytes": byte_count,
                "sha256": sha256,
                "rights_status": job["rights_status"],
                "permission_evidence_ids": job["permission_evidence_ids"],
            }
        except Exception as exc:  # try the next published encode
            failures.append(f"{variant['width']}x{variant['height']}: {exc}")
    raise RuntimeError("; ".join(failures))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("outdir", nargs="?", type=Path, default=DEFAULT_OUTDIR)
    parser.add_argument("--catalog", type=Path, default=ROOT / "data" / "catalog.json")
    parser.add_argument(
        "--volatile-cache", type=Path,
        help="owner-only cache produced by hydrate.py for exact media locators",
    )
    parser.add_argument("--limit-mb", type=int, default=DEFAULT_LIMIT_MB)
    parser.add_argument("--max-height", type=int, default=1080)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=600)
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    catalog = read_json(args.catalog)
    errors = validate_catalog(catalog or {})
    if errors:
        for error in errors:
            print(f"catalog error: {error}", file=sys.stderr)
        return 2
    if args.limit_mb <= 0 or args.max_height <= 0 or args.jobs <= 0:
        print("limit, max height, and jobs must be positive", file=sys.stderr)
        return 2

    args.outdir.mkdir(parents=True, exist_ok=True)
    try:
        volatile_cache = load_volatile_cache(args.volatile_cache, catalog)
    except (OSError, ValueError) as exc:
        print(f"volatile cache error: {exc}", file=sys.stderr)
        return 2
    jobs, identity_errors = eligible_jobs(catalog, volatile_cache)
    for error in identity_errors:
        print(f"stage error: {error}", file=sys.stderr)

    limit = args.limit_mb * 1024 * 1024
    staged: dict[str, dict] = {}
    failures = list(identity_errors)
    with futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
        submitted = {
            executor.submit(stage_one, job, args.outdir, limit, args.max_height, args.timeout): job
            for job in jobs
        }
        for future in futures.as_completed(submitted):
            job = submitted[future]
            try:
                entry = future.result()
                staged[entry["filename"]] = entry
                print(f"staged {entry['filename']} ({entry['bytes']} bytes, {entry['sha256'][:12]}…)")
            except Exception as exc:
                message = f"{job['item_id']}/{job['media_id']}: {exc}"
                failures.append(message)
                print(f"stage error: {message}", file=sys.stderr)

    generated_at = utc_now()
    index = {
        "schema_version": INDEX_VERSION,
        "catalog_schema_version": catalog.get("schema_version"),
        "collection_id": (catalog.get("collection") or {}).get("id"),
        "generated_at": generated_at,
        "attachment_limit_bytes": limit,
        "identity": "item_id/media_id",
        "entries": {key: staged[key] for key in sorted(staged)},
    }
    write_json(args.outdir / "index.json", index)
    print(f"{len(staged)}/{len(jobs)} authorized videos staged in {args.outdir}")
    if not jobs and not identity_errors:
        print("nothing to stage: no approved item currently has download + mirror_github permission")
    return 1 if failures else 0


def main() -> None:
    raise SystemExit(run(parse_args()))


if __name__ == "__main__":
    main()
