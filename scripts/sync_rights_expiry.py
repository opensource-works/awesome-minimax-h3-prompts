#!/usr/bin/env python3
"""Retire expired grants locally before validation or public generation."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from authorized_manifests import (  # noqa: E402
    build_github_attachment_manifest,
    build_r2_manifest,
)
from catalog import (  # noqa: E402
    read_json,
    refresh_retirement_manifest,
    utc_now,
    validate_catalog,
    write_json,
)


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def retire_expired_rights(catalog: dict, at: str) -> dict[str, int]:
    """Revoke elapsed positive rights and queue affected video mirrors."""
    now = parse_time(at)
    counts = {"rights": 0, "mirrors": 0, "prompts": 0}
    for item in (catalog.get("items") or {}).values():
        rights_map = item.get("rights") or {}
        for right_name, rights in rights_map.items():
            if rights.get("status") not in {"granted", "public_license"}:
                continue
            expires_at = rights.get("expires_at")
            if not expires_at or parse_time(expires_at) > now:
                continue
            rights["status"] = "revoked"
            rights["granted_scopes"] = []
            counts["rights"] += 1
            if right_name == "prompt_republication":
                counts["prompts"] += 1
                prompt = item.get("prompt") or {}
                prompt_text = prompt.get("text")
                prompt_source_id = prompt.get("source_id")
                prompt_evidence_ids = list(prompt.get("evidence_ids") or [])
                prompt.update({
                    "status": "removed", "text": None, "source_url": None,
                    "capture_method": "none", "is_verbatim": False,
                })
                source = (catalog.get("sources") or {}).get(prompt_source_id)
                source_text = (source or {}).get("text") or {}
                if prompt_text and prompt_text in (source_text.get("value") or ""):
                    source["text"] = {
                        "status": "redacted", "value": None,
                        "language": source_text.get("language"),
                    }
                for evidence_id in prompt_evidence_ids:
                    record = (catalog.get("evidence") or {}).get(evidence_id) or {}
                    if "excerpt" in record:
                        record["excerpt"] = None
                continue
            if right_name != "video_republication":
                continue
            for media in item.get("media") or []:
                delivery = media.get("delivery") or {}
                for mirror in delivery.get("mirrors") or []:
                    if mirror.get("state") in {"deleted", "pending_delete"}:
                        continue
                    mirror.update({
                        "state": "pending_delete",
                        "pending_delete_at": at,
                        "pending_delete_reason_codes": ["permission_revoked"],
                        "pending_delete_evidence_ids": list(rights.get("evidence_ids") or []),
                    })
                    counts["mirrors"] += 1
                if not any(value.get("state") == "active" for value in delivery.get("mirrors") or []):
                    delivery["mode"] = "source_link"
    return counts


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT / "data/catalog.json")
    parser.add_argument("--retirement", type=Path, default=ROOT / "data/media-retirement.json")
    parser.add_argument("--github-manifest", type=Path, default=ROOT / "data/github-attachments.json")
    parser.add_argument("--r2-manifest", type=Path, default=ROOT / "data/r2-mirrors.json")
    parser.add_argument("--at", help="RFC3339 evaluation time (defaults to now)")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    at = args.at or utc_now()
    try:
        parse_time(at)
    except (TypeError, ValueError) as exc:
        print(f"invalid --at: {exc}", file=sys.stderr)
        return 2
    catalog = read_json(args.catalog)
    try:
        counts = retire_expired_rights(catalog, at)
    except (TypeError, ValueError) as exc:
        print(f"invalid catalog rights expiry: {exc}", file=sys.stderr)
        return 2
    if not counts["rights"]:
        print("no expired positive rights")
        return 0
    catalog["updated_at"] = at
    retirement = refresh_retirement_manifest(
        catalog, read_json(args.retirement, {}), at,
        note="A recorded republication grant expired; remote cleanup requires confirmation.",
    )
    errors = validate_catalog(catalog)
    if errors:
        for error in errors:
            print(f"catalog error: {error}", file=sys.stderr)
        return 2
    write_json(args.catalog, catalog)
    write_json(args.retirement, retirement)
    write_json(args.github_manifest, build_github_attachment_manifest(catalog, at))
    write_json(args.r2_manifest, build_r2_manifest(catalog, at))
    print(
        f"revoked {counts['rights']} expired right(s), retired {counts['mirrors']} "
        f"mirror(s), suppressed {counts['prompts']} prompt grant(s)"
    )
    return 0


def main() -> None:
    raise SystemExit(run(parse_args()))


if __name__ == "__main__":
    main()
