#!/usr/bin/env python3
"""Bind every verbatim prompt/segment to its exact prompt-source evidence hash."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from catalog import read_json, validate_catalog, write_json  # noqa: E402


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def backfill(catalog: dict) -> int:
    changed = 0
    evidence = catalog.get("evidence") or {}
    for item_id, item in (catalog.get("items") or {}).items():
        prompt = item.get("prompt") or {}
        if prompt.get("status") != "verbatim" or prompt.get("is_verbatim") is not True:
            continue
        segments = prompt.get("segments") or []
        values = segments or [{
            "text": prompt.get("text"),
            "evidence_ids": prompt.get("evidence_ids") or [],
        }]
        for value in values:
            text = value.get("text")
            if not isinstance(text, str) or not text:
                raise ValueError(f"{item_id}: verbatim prompt segment has no text")
            expected = digest(text)
            prompt_evidence = [
                evidence.get(evidence_id) or {}
                for evidence_id in value.get("evidence_ids") or []
                if (evidence.get(evidence_id) or {}).get("kind") == "prompt_source"
            ]
            if not prompt_evidence:
                raise ValueError(f"{item_id}: verbatim prompt segment lacks prompt_source evidence")
            for record in prompt_evidence:
                current = record.get("integrity_sha256")
                if current not in {None, expected}:
                    raise ValueError(
                        f"{item_id}: prompt evidence {record.get('id')} has a conflicting digest"
                    )
                if current != expected or record.get("integrity_subject") != "prompt_text":
                    record["integrity_sha256"] = expected
                    record["integrity_subject"] = "prompt_text"
                    changed += 1
    errors = validate_catalog(catalog)
    if errors:
        raise ValueError("backfilled catalog is invalid:\n" + "\n".join(errors))
    return changed


def main() -> None:
    path = ROOT / "data" / "catalog.json"
    catalog = read_json(path)
    count = backfill(catalog)
    write_json(path, catalog)
    print(f"bound {count} prompt evidence record(s) to exact prompt text")


if __name__ == "__main__":
    main()
