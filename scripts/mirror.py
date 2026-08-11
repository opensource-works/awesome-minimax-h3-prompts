#!/usr/bin/env python3
"""Mirror only media with explicit, verified republication rights.

No download occurs until the catalog rights gate proves download and mirror_r2
scopes. Animated previews additionally require derive_preview. Catalog
delivery records are written only after successful uploads.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from catalog import (  # noqa: E402
    export_posts, mirror_is_authorized, read_json, refresh_retirement_manifest,
    utc_now, validate_catalog, write_json,
)
from authorized_manifests import build_r2_manifest  # noqa: E402

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
MAX_HEIGHT = 1080
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
UPLOAD_JOURNAL_VERSION = "r2-upload-recovery-v1"
VOLATILE_CACHE_VERSION = "volatile-media-cache-v1"
X_VIDEO_HOSTS = frozenset({"video.twimg.com"})
X_THUMBNAIL_HOSTS = frozenset({"pbs.twimg.com", "video.twimg.com"})
REDDIT_VIDEO_HOSTS = frozenset({"v.redd.it", "packaged-media.redd.it"})
REDDIT_THUMBNAIL_HOSTS = frozenset({
    "preview.redd.it", "external-preview.redd.it", "i.redd.it",
})


def safe_key(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", value)


def content_key(base: str, artifact: str, digest: str) -> str:
    """Use immutable content-addressed keys so retiring v1 cannot delete v2."""
    extension = "mp4" if artifact == "video" else "webp"
    return f"{base}-{artifact}-{digest[:20]}.{extension}"


def r2_mirror_id(item_key: str, media_id: str, artifact: str, digest: str) -> str:
    return (
        f"mir_r2_{safe_key(item_key)}_{safe_key(media_id)}_"
        f"{safe_key(artifact)}_{digest[:20]}"
    )


def pick_variant(observation: dict) -> tuple[str | None, int | None, int | None]:
    choices = []
    for variant in observation.get("variants") or []:
        if variant.get("container") != "mp4" or not variant.get("url"):
            continue
        width = variant.get("width") or observation.get("width") or 0
        height = variant.get("height") or observation.get("height") or 0
        choices.append((width, height, variant["url"]))
    if choices:
        choices.sort(key=lambda row: row[0] * row[1])
        bounded = [row for row in choices if row[1] <= MAX_HEIGHT] or choices[:1]
        return bounded[-1][2], bounded[-1][0] or None, bounded[-1][1] or None
    return observation.get("direct_url"), observation.get("width"), observation.get("height")


def prospective(provider: str, artifact: str, evidence_ids: list[str]) -> dict:
    return {
        "provider": provider, "artifact": artifact,
        "permission_evidence_ids": evidence_ids,
    }


def exact_media_observation(source: dict, source_media_id: str | None) -> dict | None:
    """Resolve the declared media identity without falling back to another asset."""
    if not source_media_id:
        return None
    return next(
        (
            observation
            for observation in source.get("media_observations") or []
            if observation.get("source_media_id") == source_media_id
        ),
        None,
    )


def _locator_is_allowed(url: str, hosts: frozenset[str]) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname in hosts
        and port in {None, 443}
        and not parsed.username
        and not parsed.password
    )


def _source_hosts(source: dict) -> tuple[frozenset[str], frozenset[str]]:
    if source.get("platform") == "reddit":
        return REDDIT_VIDEO_HOSTS, REDDIT_THUMBNAIL_HOSTS
    return X_VIDEO_HOSTS, X_THUMBNAIL_HOSTS


def load_volatile_cache(path: Path | None, catalog: dict) -> dict[str, dict]:
    if path is None or not path.exists():
        return {}
    if path.stat().st_mode & 0o077:
        raise SystemExit(f"{path}: volatile cache must have mode 0600 or stricter")
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get("schema_version") != VOLATILE_CACHE_VERSION:
        raise SystemExit(f"{path}: unsupported volatile media cache schema")
    if value.get("collection_id") != (catalog.get("collection") or {}).get("id"):
        raise SystemExit(f"{path}: volatile cache belongs to a different collection")
    observations = value.get("observations")
    if not isinstance(observations, dict) or any(
        not isinstance(key, str) or not isinstance(record, dict)
        for key, record in observations.items()
    ):
        raise SystemExit(f"{path}: observations must be an object")
    for key, record in observations.items():
        source_id = record.get("source_id")
        source_media_id = record.get("source_media_id")
        if key != f"{source_id}/{source_media_id}":
            raise SystemExit(f"{path}: observation key/identity mismatch for {key}")
        source = (catalog.get("sources") or {}).get(source_id)
        if not source or not exact_media_observation(source, source_media_id):
            raise SystemExit(f"{path}: observation identity does not resolve for {key}")
        video_hosts, thumbnail_hosts = _source_hosts(source)
        for field, hosts in (
            ("direct_url", video_hosts), ("thumbnail_url", thumbnail_hosts),
        ):
            url = record.get(field)
            if url is not None and not _locator_is_allowed(url, hosts):
                raise SystemExit(f"{path}: unsafe {field} for {key}")
        variants = record.get("variants") or []
        if not isinstance(variants, list) or any(
            not isinstance(variant, dict)
            or not _locator_is_allowed(variant.get("url"), video_hosts)
            for variant in variants
        ):
            raise SystemExit(f"{path}: unsafe variants for {key}")
    return observations


def observation_with_cache(
    source_id: str,
    source: dict,
    source_media_id: str | None,
    volatile_cache: dict[str, dict],
) -> dict | None:
    observation = exact_media_observation(source, source_media_id)
    if not observation or not source_media_id:
        return None
    output = dict(observation)
    cached = volatile_cache.get(f"{source_id}/{source_media_id}") or {}
    for field in ("direct_url", "thumbnail_url", "variants"):
        if field in cached:
            output[field] = cached[field]
    return output


def eligible_jobs(catalog: dict, volatile_cache: dict[str, dict] | None = None) -> list[dict]:
    volatile_cache = volatile_cache or {}
    jobs = []
    for item_key, item in catalog.get("items", {}).items():
        if (item.get("curation") or {}).get("status") != "approved":
            continue
        canonical_source_id = item.get("canonical_source_id")
        canonical_source = catalog.get("sources", {}).get(canonical_source_id) or {}
        if (canonical_source.get("availability") or {}).get("state") not in {
            "available", "transient_error",
        }:
            continue
        rights = (item.get("rights") or {}).get("video_republication") or {}
        evidence_ids = rights.get("evidence_ids") or []
        for media in item.get("media") or []:
            if media.get("kind") != "video":
                continue
            source_id = media.get("source_id") or canonical_source_id
            source = catalog.get("sources", {}).get(source_id) or {}
            if (source.get("availability") or {}).get("state") not in {
                "available", "transient_error",
            }:
                continue
            delivery = media.get("delivery") or {}
            active = delivery.get("mirrors") or []
            has_video = any(
                m.get("provider") == "r2" and m.get("artifact") == "video"
                and m.get("state") == "active" and mirror_is_authorized(item, m, catalog)
                for m in active
            )
            has_preview = any(
                m.get("provider") == "r2" and m.get("artifact") == "animated_preview"
                and m.get("state") == "active" and mirror_is_authorized(item, m, catalog)
                for m in active
            )
            may_video = mirror_is_authorized(item, prospective("r2", "video", evidence_ids), catalog)
            may_preview = mirror_is_authorized(
                item, prospective("r2", "animated_preview", evidence_ids), catalog
            )
            if may_video and (not has_video or (may_preview and not has_preview)):
                observation = observation_with_cache(
                    source_id,
                    source,
                    media.get("source_media_id"),
                    volatile_cache,
                )
                if not observation:
                    continue
                url, width, height = pick_variant(observation)
                video_hosts, _thumbnail_hosts = _source_hosts(source)
                if url and not _locator_is_allowed(url, video_hosts):
                    continue
                jobs.append({
                    "item_key": item_key, "item": item, "source": source, "media": media,
                    "observation": observation,
                    "url": url, "width": width, "height": height,
                    "allowed_hosts": video_hosts,
                    "video_needed": not has_video, "preview_needed": may_preview and not has_preview,
                    "evidence_ids": evidence_ids,
                })
    return jobs


def download(
    url: str, path: Path, allowed_hosts: frozenset[str]
) -> tuple[int, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "opensource-works-prompt-index/2.0"})
    digest = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(request, timeout=600) as response, path.open("wb") as output:
        if not _locator_is_allowed(response.geturl(), allowed_hosts):
            raise RuntimeError("download redirected outside the approved platform video hosts")
        content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if not content_type.startswith("video/"):
            raise RuntimeError(f"unexpected media content type {content_type or 'missing'}")
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError as exc:
                raise RuntimeError("invalid Content-Length") from exc
            if declared_size > MAX_DOWNLOAD_BYTES:
                raise RuntimeError(f"download exceeds {MAX_DOWNLOAD_BYTES} byte limit")
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            if size + len(chunk) > MAX_DOWNLOAD_BYTES:
                raise RuntimeError(f"download exceeds {MAX_DOWNLOAD_BYTES} byte limit")
            output.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    if size < 10_000:
        raise RuntimeError(f"download too small ({size} bytes)")
    return size, digest.hexdigest()


def preview(source: Path, destination: Path, duration_ms: int | None) -> tuple[int, str]:
    start = "2" if (duration_ms or 0) > 8000 else "0"
    command = [
        FFMPEG, "-y", "-v", "error", "-ss", start, "-t", "3", "-i", str(source),
        "-vf", "fps=10,scale=480:-2:flags=lanczos", "-an", "-c:v", "libwebp_anim",
        "-lossless", "0", "-q:v", "62", "-compression_level", "5", "-loop", "0",
        str(destination),
    ]
    result = subprocess.run(command, capture_output=True)
    if result.returncode:
        raise RuntimeError("ffmpeg: " + result.stderr.decode(errors="replace")[-500:])
    data = destination.read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()


def replace_active(delivery: dict, record: dict) -> None:
    for existing in delivery.setdefault("mirrors", []):
        if (existing.get("provider"), existing.get("artifact")) == (
            record["provider"], record["artifact"]
        ) and existing.get("state") == "active":
            if (
                existing.get("mirror_id") == record.get("mirror_id")
                and existing.get("url") == record.get("url")
            ):
                existing.update(record)
                delivery["mode"] = "authorized_mirror"
                return
            existing["state"] = "pending_delete"
    delivery["mirrors"].append(record)
    delivery["mode"] = "authorized_mirror"


active_manifest = build_r2_manifest


def write_upload_journal(
    path: Path,
    *,
    at: str,
    bucket: str,
    attempts: list[dict],
    status: str,
    errors: list[str] | None = None,
) -> None:
    """Persist enough identity to recover an interrupted upload transaction."""
    write_json(path, {
        "schema_version": UPLOAD_JOURNAL_VERSION,
        "started_at": at,
        "updated_at": utc_now(),
        "status": status,
        "bucket": bucket,
        "objects": attempts,
        "errors": errors or [],
    })


def rollback_uploads(
    r2_client,
    bucket: str,
    attempts: list[dict],
    on_change=None,
) -> list[str]:
    """Confirm every attempted key absent, updating the recovery journal."""
    errors: list[str] = []
    for attempt in reversed(attempts):
        key = attempt["key"]
        try:
            if attempt.get("existed_before"):
                attempt["state"] = "preserved_preexisting"
                attempt["cleanup_error"] = None
                if on_change:
                    on_change()
                continue
            existed = r2_client.head(bucket, key)
            if existed:
                r2_client.delete(bucket, key)
            if r2_client.head(bucket, key):
                raise RuntimeError("object still exists after rollback DELETE")
            attempt["state"] = "rolled_back" if existed else "confirmed_absent"
            attempt["cleanup_error"] = None
        except Exception as exc:
            attempt["state"] = "cleanup_failed"
            attempt["cleanup_error"] = str(exc)
            errors.append(f"{key}: {exc}")
        if on_change:
            on_change()
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--volatile-cache", type=Path,
        help="gitignored volatile-media cache produced by hydrate.py",
    )
    args = parser.parse_args()
    catalog = read_json(ROOT / "data" / "catalog.json")
    config = read_json(ROOT / "config" / "collection.json")
    initial_errors = validate_catalog(catalog)
    if initial_errors:
        raise SystemExit(
            "refusing media operations on an invalid catalog:\n"
            + "\n".join(f"- {error}" for error in initial_errors)
        )
    jobs = eligible_jobs(catalog, load_volatile_cache(args.volatile_cache, catalog))
    if args.limit:
        jobs = jobs[:args.limit]
    print(f"{len(jobs)} media objects pass the verified R2 rights gate")
    if args.dry_run:
        for job in jobs:
            print(f"- {job['item_key']} / {job['media']['media_id']}")
        return
    journal_path = ROOT / "data" / "r2-upload-recovery.json"
    if journal_path.exists():
        raise SystemExit(
            "data/r2-upload-recovery.json records an unresolved prior upload; "
            "reconcile its R2 keys before starting another mirror run"
        )
    if not jobs:
        write_json(
            ROOT / "data" / "r2-mirrors.json",
            build_r2_manifest(catalog, catalog.get("updated_at") or utc_now()),
        )
        return
    import r2

    bucket = config["r2"]["bucket"]
    public = config["r2"]["public_base"].rstrip("/")
    at = utc_now()
    failures = []
    upload_attempts: list[dict] = []

    def persist_journal(status: str = "in_progress", errors: list[str] | None = None) -> None:
        write_upload_journal(
            journal_path,
            at=at,
            bucket=bucket,
            attempts=upload_attempts,
            status=status,
            errors=errors,
        )

    for job in jobs:
        if not job["url"]:
            failures.append(f"{job['item_key']}: source has no downloadable observation")
            continue
        base = f"v2/{safe_key(job['item_key'])}/{safe_key(job['media']['media_id'])}"
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / "source.mp4"
            try:
                size, digest = download(job["url"], local, job["allowed_hosts"])
                delivery = job["media"]["delivery"]
                if job["video_needed"]:
                    key = content_key(base, "video", digest)
                    url = public + "/" + urllib.parse.quote(key)
                    existed_before = r2.head(bucket, key)
                    upload_attempts.append({
                        "item_id": job["item_key"],
                        "media_id": job["media"]["media_id"],
                        "source_id": job["media"].get("source_id")
                        or job["item"].get("canonical_source_id"),
                        "artifact": "video",
                        "key": key,
                        "sha256": digest,
                        "existed_before": existed_before,
                        "state": "attempting_upload",
                    })
                    persist_journal()
                    r2.put(bucket, key, local.read_bytes(), "video/mp4")
                    upload_attempts[-1]["state"] = "uploaded"
                    persist_journal()
                    record = {
                        "mirror_id": r2_mirror_id(
                            job["item_key"], job["media"]["media_id"], "video", digest
                        ),
                        "provider": "r2", "artifact": "video", "url": url,
                        "bytes": size, "sha256": digest, "width": job["width"],
                        "height": job["height"], "uploaded_at": at, "state": "active",
                        "permission_evidence_ids": job["evidence_ids"], "last_checked_at": at,
                    }
                    if not mirror_is_authorized(job["item"], record, catalog):
                        raise RuntimeError("rights changed while preparing the video mirror")
                    replace_active(delivery, record)
                if job["preview_needed"]:
                    loop_path = Path(directory) / "preview.webp"
                    observation = job.get("observation") or {}
                    loop_size, loop_hash = preview(local, loop_path, observation.get("duration_ms"))
                    key = content_key(base, "animated_preview", loop_hash)
                    url = public + "/" + urllib.parse.quote(key)
                    existed_before = r2.head(bucket, key)
                    upload_attempts.append({
                        "item_id": job["item_key"],
                        "media_id": job["media"]["media_id"],
                        "source_id": job["media"].get("source_id")
                        or job["item"].get("canonical_source_id"),
                        "artifact": "animated_preview",
                        "key": key,
                        "sha256": loop_hash,
                        "existed_before": existed_before,
                        "state": "attempting_upload",
                    })
                    persist_journal()
                    r2.put(bucket, key, loop_path.read_bytes(), "image/webp")
                    upload_attempts[-1]["state"] = "uploaded"
                    persist_journal()
                    record = {
                        "mirror_id": r2_mirror_id(
                            job["item_key"], job["media"]["media_id"], "animated_preview", loop_hash
                        ),
                        "provider": "r2", "artifact": "animated_preview", "url": url,
                        "bytes": loop_size, "sha256": loop_hash, "width": 480,
                        "height": None, "uploaded_at": at, "state": "active",
                        "permission_evidence_ids": job["evidence_ids"], "last_checked_at": at,
                    }
                    if not mirror_is_authorized(job["item"], record, catalog):
                        raise RuntimeError("rights changed while preparing the preview mirror")
                    replace_active(delivery, record)
                print(f"mirrored {job['item_key']} ({size / 1_000_000:.1f} MB)")
            except Exception as exc:
                failures.append(f"{job['item_key']}: {exc}")
                break
    if failures:
        rollback_errors = rollback_uploads(
            r2,
            bucket,
            upload_attempts,
            on_change=lambda: persist_journal("rolling_back", failures),
        )
        if rollback_errors:
            persist_journal("manual_cleanup_required", failures + rollback_errors)
            failures.extend(
                f"rollback failed; recovery journal retained: {error}"
                for error in rollback_errors
            )
        else:
            journal_path.unlink(missing_ok=True)
        print(f"{len(failures)} mirror failures:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        raise SystemExit(2)
    catalog["updated_at"] = at
    retirement_path = ROOT / "data" / "media-retirement.json"
    current_retirement = read_json(retirement_path, {})
    retirement = refresh_retirement_manifest(
        catalog, current_retirement, at,
        note="An authorized mirror was replaced; the superseded object requires confirmed cleanup.",
    )
    errors = validate_catalog(catalog)
    if errors:
        failures = [f"catalog validation: {error}" for error in errors]
        rollback_errors = rollback_uploads(
            r2,
            bucket,
            upload_attempts,
            on_change=lambda: persist_journal("rolling_back", failures),
        )
        if rollback_errors:
            persist_journal("manual_cleanup_required", failures + rollback_errors)
        else:
            journal_path.unlink(missing_ok=True)
        raise SystemExit(
            "mirror run produced invalid catalog; uploaded objects were rolled back"
            + (" or recorded for manual cleanup" if rollback_errors else "")
            + ":\n"
            + "\n".join(f"- {error}" for error in errors + rollback_errors)
        )
    write_json(ROOT / "data" / "catalog.json", catalog)
    write_json(ROOT / "data" / "posts.json", export_posts(catalog))
    write_json(ROOT / "data" / "r2-mirrors.json", build_r2_manifest(catalog, at))
    if retirement != current_retirement:
        write_json(retirement_path, retirement)
    journal_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
