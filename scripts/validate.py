#!/usr/bin/env python3
"""Validate the canonical catalog and every lossy/public projection.

The catalog helpers intentionally keep validation free of third-party
dependencies.  This command adds repository-level checks that need access to
``scripts/urls.txt`` and the committed compatibility artifacts:

    python3 scripts/validate.py
    python3 scripts/validate.py --json

It exits non-zero when any check fails.  The functions are kept small and
side-effect free so tests and CI can exercise each policy gate directly.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from catalog import (  # noqa: E402
    export_posts,
    item_is_public,
    mirror_needs_cleanup,
    mirror_is_authorized,
    platform_from_url,
    public_catalog,
    source_identity,
    validate_catalog,
)
from authorized_manifests import (  # noqa: E402
    build_r2_manifest, validate_github_attachment_manifest,
)


def load_json(path: Path):
    return json.loads(path.read_text())


def _prefix(prefix: str, errors: list[str]) -> list[str]:
    return [f"{prefix}: {error}" for error in errors]


def _is_rfc3339(value) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _all_evidence_references(value, path=""):
    """Yield (path, evidence_id) pairs from any nested ``*_evidence_ids``."""
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            if key == "evidence_ids" or key.endswith("_evidence_ids"):
                for evidence_id in child or []:
                    yield child_path, evidence_id
            else:
                yield from _all_evidence_references(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _all_evidence_references(child, f"{path}[{index}]")


def _nested_reference_ids(value, singular: str, plural: str) -> set[str]:
    """Collect ID references recursively for public-graph closure checks."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key == singular or key.endswith("_" + singular):
                if isinstance(child, str) and child:
                    found.add(child)
            elif key == plural or key.endswith("_" + plural):
                if isinstance(child, list):
                    found.update(part for part in child if isinstance(part, str) and part)
            found.update(_nested_reference_ids(child, singular, plural))
    elif isinstance(value, list):
        for child in value:
            found.update(_nested_reference_ids(child, singular, plural))
    return found


def validate_schema_contract(catalog: dict, schema: dict) -> list[str]:
    """Apply the root-level contract without requiring ``jsonschema``."""
    errors = []
    if schema.get("type") != "object":
        errors.append("schema root must describe an object")
        return errors
    for key in schema.get("required") or []:
        if key not in catalog:
            errors.append(f"missing required root key {key}")
    properties = schema.get("properties") or {}
    if schema.get("additionalProperties") is False:
        for key in catalog:
            if key not in properties:
                errors.append(f"unexpected root key {key}")
    for key, definition in properties.items():
        if key not in catalog:
            continue
        if "const" in definition and catalog[key] != definition["const"]:
            errors.append(f"{key} must equal {definition['const']!r}")
        expected_type = definition.get("type")
        if expected_type == "object" and not isinstance(catalog[key], dict):
            errors.append(f"{key} must be an object")
        elif expected_type == "array" and not isinstance(catalog[key], list):
            errors.append(f"{key} must be an array")
        elif expected_type == "string" and not isinstance(catalog[key], str):
            errors.append(f"{key} must be a string")
    if not _is_rfc3339(catalog.get("updated_at")):
        errors.append("updated_at must be RFC3339")
    return errors


def validate_collection_config(catalog: dict, config: dict) -> list[str]:
    errors = []
    collection = catalog.get("collection") or {}
    for key in (
        "id", "repo_url", "model_scope", "historical_window",
        "query_matrix_version",
    ):
        if collection.get(key) != config.get(key):
            errors.append(f"collection.{key} differs from config/collection.json")
    return errors


def validate_canonical(catalog: dict, schema: dict | None = None,
                       config: dict | None = None) -> list[str]:
    """Validate graph integrity in addition to ``catalog.validate_catalog``."""
    errors = list(validate_catalog(catalog))
    if schema is not None:
        errors.extend(_prefix("schema", validate_schema_contract(catalog, schema)))
    if config is not None:
        errors.extend(_prefix("config", validate_collection_config(catalog, config)))

    actors = catalog.get("actors") or {}
    sources = catalog.get("sources") or {}
    evidence = catalog.get("evidence") or {}
    items = catalog.get("items") or {}
    candidates = catalog.get("candidates") or {}
    runs = catalog.get("discovery_runs") or {}

    for actor_id, actor in actors.items():
        if actor.get("id") != actor_id:
            errors.append(f"actors.{actor_id}.id must equal object key")
        for field in ("profile_url", "avatar_url"):
            value = actor.get(field)
            if value and urlparse(value).scheme != "https":
                errors.append(f"actors.{actor_id}.{field} must be HTTPS")

    source_media_ids = {}
    for source_id, source in sources.items():
        tag = f"sources.{source_id}"
        if source.get("parent_source_id") and source["parent_source_id"] not in sources:
            errors.append(f"{tag}.parent_source_id does not resolve")
        if source.get("posted_at") is not None and not _is_rfc3339(source["posted_at"]):
            errors.append(f"{tag}.posted_at must be RFC3339 or null")
        if not _is_rfc3339((source.get("metrics") or {}).get("observed_at")):
            errors.append(f"{tag}.metrics.observed_at must be RFC3339")
        if not _is_rfc3339((source.get("fetch") or {}).get("observed_at")):
            errors.append(f"{tag}.fetch.observed_at must be RFC3339")
        try:
            if platform_from_url(source.get("url", "")) != source.get("platform"):
                errors.append(f"{tag}.url platform mismatch")
            if source.get("kind") == "post":
                _, native_id, parsed_id = source_identity(source["url"])
                if parsed_id != source_id or native_id != source.get("native_id"):
                    errors.append(f"{tag}.url does not identify this source")
        except (KeyError, ValueError):
            errors.append(f"{tag}.url is not a valid source URL")
        positions = set()
        for observation in source.get("media_observations") or []:
            media_id = observation.get("source_media_id")
            if not media_id:
                errors.append(f"{tag} media observation is missing source_media_id")
            elif media_id in source_media_ids:
                errors.append(
                    f"{tag} reuses source_media_id {media_id} from "
                    f"{source_media_ids[media_id]}"
                )
            else:
                source_media_ids[media_id] = source_id
            position = observation.get("position")
            if position in positions:
                errors.append(f"{tag} has duplicate media position {position}")
            positions.add(position)
            if observation.get("direct_url") is not None:
                errors.append(f"{tag}.{media_id}.direct_url must not be persisted")
            if observation.get("thumbnail_url") is not None:
                errors.append(f"{tag}.{media_id}.thumbnail_url must not be persisted")
            if observation.get("variants"):
                errors.append(f"{tag}.{media_id}.variants must not be persisted")

    for evidence_id, record in evidence.items():
        tag = f"evidence.{evidence_id}"
        if record.get("id") != evidence_id:
            errors.append(f"{tag}.id must equal object key")
        if not str(record.get("url", "")).startswith("https://"):
            errors.append(f"{tag}.url must be HTTPS")
        if not _is_rfc3339(record.get("observed_at")):
            errors.append(f"{tag}.observed_at must be RFC3339")

    media_ids = set()
    for item_id, item in items.items():
        tag = f"items.{item_id}"
        source_ids = item.get("source_ids") or []
        canonical_id = item.get("canonical_source_id")
        posted_by = {
            role.get("source_id"): role.get("actor_id")
            for role in (item.get("attribution") or {}).get("posted_by") or []
        }
        for source_id in source_ids:
            if posted_by.get(source_id) != (sources.get(source_id) or {}).get("posted_by_actor_id"):
                errors.append(f"{tag}.attribution.posted_by must cover {source_id} exactly")
        for path, evidence_id in _all_evidence_references(item, tag):
            if evidence_id not in evidence:
                errors.append(f"{path} contains missing evidence {evidence_id}")
        for media in item.get("media") or []:
            media_id = media.get("media_id")
            if not media_id:
                errors.append(f"{tag} media is missing media_id")
            elif media_id in media_ids:
                errors.append(f"{tag} reuses media_id {media_id}")
            else:
                media_ids.add(media_id)
            media_source_id = media.get("source_id")
            if media_source_id not in source_ids:
                errors.append(f"{tag}.{media_id} source_id is not in item source_ids")
                continue
            observation_ids = {
                value.get("source_media_id")
                for value in (sources.get(media_source_id) or {}).get("media_observations") or []
            }
            if media.get("source_media_id") not in observation_ids:
                errors.append(f"{tag}.{media_id} source_media_id does not resolve")
            delivery = media.get("delivery") or {}
            expected_link = (sources.get(media_source_id) or {}).get("url")
            if delivery.get("link_url") != expected_link:
                errors.append(f"{tag}.{media_id}.delivery.link_url must be its source URL")
        candidate = candidates.get(canonical_id) or {}
        review = candidate.get("review") or {}
        if (item.get("curation") or {}).get("status") == "approved":
            if review.get("state") != "included" or review.get("item_id") != item_id:
                errors.append(f"{tag} approved item needs an included canonical candidate")

    for source_id, candidate in candidates.items():
        tag = f"candidates.{source_id}"
        if candidate.get("source_id") != source_id:
            errors.append(f"{tag}.source_id must equal object key")
        discoveries = candidate.get("discoveries") or []
        if not discoveries:
            errors.append(f"{tag}.discoveries must not be empty")
        for discovery in discoveries:
            run_id = discovery.get("run_id")
            run = runs.get(run_id)
            if not run:
                errors.append(f"{tag} discovery run {run_id} does not resolve")
            elif discovery.get("query_id") not in (run.get("query_ids") or []):
                errors.append(f"{tag} discovery query is not declared by {run_id}")
        review = candidate.get("review") or {}
        if review.get("state") == "included":
            if review.get("reviewer_actor_id") not in actors:
                errors.append(f"{tag} included decision needs reviewer")
            if not _is_rfc3339(review.get("reviewed_at")):
                errors.append(f"{tag} included decision needs reviewed_at")
        for path, evidence_id in _all_evidence_references(candidate, tag):
            if evidence_id not in evidence:
                errors.append(f"{path} contains missing evidence {evidence_id}")

    for run_id, run in runs.items():
        if run.get("id") != run_id:
            errors.append(f"discovery_runs.{run_id}.id must equal object key")
        if run.get("status") not in {"running", "complete", "partial", "failed"}:
            errors.append(f"discovery_runs.{run_id}.status is invalid")
        if not _is_rfc3339(run.get("started_at")):
            errors.append(f"discovery_runs.{run_id}.started_at must be RFC3339")
    return errors


def _first_difference(left, right, path="$") -> str | None:
    if type(left) is not type(right):
        return f"{path}: {type(left).__name__} != {type(right).__name__}"
    if isinstance(left, dict):
        if left.keys() != right.keys():
            missing = sorted(set(left) - set(right))
            extra = sorted(set(right) - set(left))
            return f"{path}: keys differ (missing={missing}, extra={extra})"
        for key in left:
            result = _first_difference(left[key], right[key], f"{path}.{key}")
            if result:
                return result
    elif isinstance(left, list):
        if len(left) != len(right):
            return f"{path}: lengths differ ({len(left)} != {len(right)})"
        for index, (left_value, right_value) in enumerate(zip(left, right)):
            result = _first_difference(left_value, right_value, f"{path}[{index}]")
            if result:
                return result
    elif left != right:
        return f"{path}: {left!r} != {right!r}"
    return None


def validate_v1_projection(catalog: dict, committed_posts: list) -> list[str]:
    expected = export_posts(catalog)
    difference = _first_difference(expected, committed_posts)
    if difference:
        return [f"data/posts.json is stale or hand-edited: {difference}"]
    errors = []
    for index, post in enumerate(committed_posts):
        video = post.get("video") or {}
        if video.get("formats"):
            errors.append(f"posts[{index}].video.formats must be stripped")
        if video.get("source_url") is not None:
            errors.append(f"posts[{index}].video.source_url must be stripped")
    return errors


def read_candidate_urls(path: Path) -> list[tuple[int, str]]:
    output = []
    for line_number, raw in enumerate(path.read_text().splitlines(), 1):
        value = raw.strip()
        if value and not value.startswith("#"):
            output.append((line_number, value))
    return output


def validate_candidate_coverage(catalog: dict, candidate_urls: list[tuple[int, str]]) -> list[str]:
    errors = []
    sources = catalog.get("sources") or {}
    candidates = catalog.get("candidates") or {}
    seen = {}
    for line_number, url in candidate_urls:
        try:
            _, _, source_id = source_identity(url)
        except ValueError as exc:
            errors.append(f"scripts/urls.txt:{line_number}: {exc}")
            continue
        if source_id in seen:
            errors.append(
                f"scripts/urls.txt:{line_number}: duplicate source {source_id} "
                f"(first at line {seen[source_id]})"
            )
        seen[source_id] = line_number
        if source_id not in sources:
            errors.append(f"scripts/urls.txt:{line_number}: {source_id} missing from sources")
        if source_id not in candidates:
            errors.append(f"scripts/urls.txt:{line_number}: {source_id} missing from candidates")
    return errors


def validate_rights_and_mirrors(catalog: dict) -> list[str]:
    errors = []
    evidence = catalog.get("evidence") or {}
    actors = catalog.get("actors") or {}
    sha256 = re.compile(r"^[0-9a-f]{64}$")
    for item_id, item in (catalog.get("items") or {}).items():
        rights = ((item.get("rights") or {}).get("video_republication") or {})
        for actor_id in rights.get("grantor_actor_ids") or []:
            if actor_id not in actors:
                errors.append(f"items.{item_id} rights grantor {actor_id} does not resolve")
        for evidence_id in rights.get("evidence_ids") or []:
            if evidence_id not in evidence:
                errors.append(f"items.{item_id} rights evidence {evidence_id} does not resolve")
        for media in item.get("media") or []:
            tag = f"items.{item_id}.{media.get('media_id', 'unknown')}"
            delivery = media.get("delivery") or {}
            active = []
            for mirror in delivery.get("mirrors") or []:
                if mirror.get("state") != "active":
                    continue
                active.append(mirror)
                if not mirror_is_authorized(item, mirror, catalog):
                    errors.append(f"{tag} has unauthorized active mirror {mirror.get('mirror_id')}")
                    continue
                if not str(mirror.get("url", "")).startswith("https://"):
                    errors.append(f"{tag} active mirror URL must be HTTPS")
                if not isinstance(mirror.get("bytes"), int) or mirror["bytes"] <= 0:
                    errors.append(f"{tag} active mirror needs a positive byte count")
                if not sha256.fullmatch(str(mirror.get("sha256") or "")):
                    errors.append(f"{tag} active mirror needs a SHA-256 digest")
                if not _is_rfc3339(mirror.get("uploaded_at")):
                    errors.append(f"{tag} active mirror needs uploaded_at")
            mode = delivery.get("mode")
            if mode == "authorized_mirror":
                if not any(m.get("artifact") == "video" for m in active):
                    errors.append(f"{tag} authorized_mirror mode needs an active video mirror")
            elif active:
                errors.append(f"{tag} {mode} mode cannot expose active mirrors")
            if mode == "official_embed":
                embed = delivery.get("official_embed") or {}
                if embed.get("provider") not in {"x", "reddit"}:
                    errors.append(f"{tag} official embed provider is invalid")
                if embed.get("source_id") not in (item.get("source_ids") or []):
                    errors.append(f"{tag} official embed source does not resolve")
    return errors


def validate_public_projection(catalog: dict) -> list[str]:
    before = copy.deepcopy(catalog)
    public = public_catalog(catalog)
    errors = []
    if catalog != before:
        errors.append("public_catalog mutated the canonical catalog")
    errors.extend(_prefix("public graph", validate_catalog(public)))
    items = public.get("items") or {}
    sources = public.get("sources") or {}
    for item_id, item in items.items():
        if not item_is_public(item, catalog):
            errors.append(f"public items contains non-public {item_id}")
        for media in item.get("media") or []:
            for mirror in (media.get("delivery") or {}).get("mirrors") or []:
                if mirror.get("state") != "active":
                    errors.append(f"public {item_id} contains non-active mirror")
                elif not mirror_is_authorized(item, mirror, catalog):
                    errors.append(f"public {item_id} contains unauthorized mirror")
    graph_roots = {
        "collection": public.get("collection") or {},
        "items": items,
        "candidates": public.get("candidates") or {},
        "sources": sources,
        "evidence": public.get("evidence") or {},
    }
    for label, singular, plural, present in (
        ("source", "source_id", "source_ids", set(sources)),
        ("actor", "actor_id", "actor_ids", set(public.get("actors") or {})),
        ("evidence", "evidence_id", "evidence_ids", set(public.get("evidence") or {})),
    ):
        referenced = _nested_reference_ids(graph_roots, singular, plural)
        missing = sorted(referenced - present)
        extra = sorted(present - referenced)
        if missing:
            errors.append(f"public {label} references are missing: {missing[:5]}")
        if extra:
            errors.append(f"public {label} records are unreachable: {extra[:5]}")
    for source_id, source in sources.items():
        for observation in source.get("media_observations") or []:
            if observation.get("direct_url") is not None:
                errors.append(f"public {source_id} retains direct_url")
            if observation.get("thumbnail_url") is not None:
                errors.append(f"public {source_id} retains thumbnail_url")
            if observation.get("variants"):
                errors.append(f"public {source_id} retains volatile variants")
    for evidence_id, record in (public.get("evidence") or {}).items():
        if record.get("visibility") != "public":
            errors.append(f"public evidence contains private {evidence_id}")
    for source_id, candidate in (public.get("candidates") or {}).items():
        if source_id not in sources:
            errors.append(f"public candidates contains missing source {source_id}")
        if (candidate.get("review") or {}).get("state") != "included":
            errors.append(f"public candidates contains non-included {source_id}")
    return errors


def validate_legacy_media_retirement(root: Path, catalog: dict) -> list[str]:
    """Ensure quarantined legacy URLs cannot leak through old manifests."""
    errors = []
    for relative in ("data/mirror.json", "data/attachments.json"):
        value = load_json(root / relative)
        if value != {}:
            errors.append(f"{relative} must be an empty object after retirement")
    retirement_path = root / "data/media-retirement.json"
    if not retirement_path.exists():
        return errors + ["data/media-retirement.json is missing"]
    retirement = load_json(retirement_path)
    retired = {entry.get("mirror_id"): entry for entry in retirement.get("entries") or []}
    for item in (catalog.get("items") or {}).values():
        for media in item.get("media") or []:
            for mirror in (media.get("delivery") or {}).get("mirrors") or []:
                if not mirror_needs_cleanup(mirror):
                    continue
                mirror_id = mirror.get("mirror_id")
                record = retired.get(mirror_id)
                if not record:
                    errors.append(f"quarantined mirror {mirror_id} is absent from retirement manifest")
                elif record.get("state") not in {"pending_delete", "deleted"}:
                    errors.append(f"retirement record {mirror_id} has invalid state")
                if record and "url" in record:
                    errors.append(f"retirement record {mirror_id} must not retain the raw URL")
                if mirror.get("url") is not None:
                    errors.append(f"retired mirror {mirror_id} must not retain the raw URL")
                former_digest = mirror.get("former_url_sha256")
                if not re.fullmatch(r"[0-9a-f]{64}", str(former_digest or "")):
                    errors.append(f"retired mirror {mirror_id} needs former_url_sha256")
                elif record and record.get("url_sha256") != former_digest:
                    errors.append(
                        f"retired mirror {mirror_id} hash differs from retirement manifest"
                    )
    return errors


def validate_r2_manifest(root: Path, catalog: dict) -> list[str]:
    """Require the namespaced R2 manifest to exactly project the catalog."""
    path = root / "data/r2-mirrors.json"
    if not path.exists():
        return ["data/r2-mirrors.json is missing"]
    manifest = load_json(path)
    if not isinstance(manifest, dict):
        return ["data/r2-mirrors.json must be an object"]
    errors = []
    generated_at = manifest.get("generated_at")
    if not _is_rfc3339(generated_at):
        errors.append("data/r2-mirrors.json generated_at must be RFC3339")
    expected = build_r2_manifest(catalog, generated_at)
    difference = _first_difference(expected, manifest)
    if difference:
        errors.append(
            "data/r2-mirrors.json is stale, malformed, or hand-edited: "
            + difference
        )
    return errors


def validate_no_upload_recovery(root: Path) -> list[str]:
    path = root / "data/r2-upload-recovery.json"
    if path.exists():
        return [
            "data/r2-upload-recovery.json requires manual R2 reconciliation "
            "before validation can pass"
        ]
    return []


def validate_github_manifest(root: Path, catalog: dict) -> list[str]:
    """Require GitHub attachment URLs to be an exact canonical projection."""
    path = root / "data/github-attachments.json"
    if not path.exists():
        return ["data/github-attachments.json is missing"]
    return validate_github_attachment_manifest(catalog, load_json(path))


def validate_repository(root: Path = ROOT) -> dict[str, list[str]]:
    catalog = load_json(root / "data/catalog.json")
    schema = load_json(root / "schema/catalog-v2.schema.json")
    config = load_json(root / "config/collection.json")
    posts = load_json(root / "data/posts.json")
    urls = read_candidate_urls(root / "scripts/urls.txt")
    return {
        "canonical": validate_canonical(catalog, schema, config),
        "v1_projection": validate_v1_projection(catalog, posts),
        "candidate_coverage": validate_candidate_coverage(catalog, urls),
        "rights_and_mirrors": validate_rights_and_mirrors(catalog),
        "public_projection": validate_public_projection(catalog),
        "legacy_media_retirement": validate_legacy_media_retirement(root, catalog),
        "r2_manifest": validate_r2_manifest(root, catalog),
        "r2_upload_recovery": validate_no_upload_recovery(root),
        "github_attachment_manifest": validate_github_manifest(root, catalog),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit a machine-readable report")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root")
    args = parser.parse_args(argv)
    report = validate_repository(args.root.resolve())
    failed = sum(len(errors) for errors in report.values())
    if args.json:
        print(json.dumps({"ok": failed == 0, "checks": report}, indent=2))
    else:
        for name, errors in report.items():
            if not errors:
                print(f"OK   {name}")
                continue
            print(f"FAIL {name} ({len(errors)})")
            for error in errors:
                print(f"  - {error}")
        print(f"\n{'validation passed' if not failed else f'validation failed with {failed} error(s)'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
