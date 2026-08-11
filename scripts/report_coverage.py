#!/usr/bin/env python3
"""Report catalog discovery and review coverage as JSON and Markdown."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from catalog import (  # noqa: E402
    ATTRIBUTION_STATES,
    AVAILABILITY,
    CANDIDATE_STATES,
    DELIVERY_MODES,
    MIRROR_STATES,
    PLATFORMS,
    PROMPT_STATES,
    RIGHTS_STATES,
    read_json,
    utc_now,
    validate_catalog,
)

DEFAULT_CATALOG = ROOT / "data" / "catalog.json"
DEFAULT_QUERY_MATRIX = ROOT / "config" / "query-matrix.json"


def _counts(values) -> dict[str, int]:
    return dict(sorted(Counter("missing" if value is None else str(value) for value in values).items()))


def _with_zeros(counts: dict[str, int], known_values) -> dict[str, int]:
    """Keep zero-coverage states visible instead of silently omitting them."""
    return {key: counts.get(key, 0) for key in sorted(set(known_values) | set(counts))}


def _run_window_kind(run: dict) -> str:
    if run.get("window_kind"):
        return run["window_kind"]
    run_id = str(run.get("id") or "")
    return "historical" if "historical" in run_id or "legacy" in run_id else "unclassified"


def build_report(catalog: dict, *, generated_at: str | None = None,
                 query_matrix: dict | None = None) -> dict:
    sources = catalog.get("sources", {})
    candidates = catalog.get("candidates", {})
    items = catalog.get("items", {})
    runs = catalog.get("discovery_runs", {})

    query_candidates: dict[str, set[str]] = defaultdict(set)
    query_events = Counter()
    query_platforms: dict[str, set[str]] = defaultdict(set)
    declared_queries = set()
    configured = {}
    window_candidates: dict[str, set[str]] = defaultdict(set)
    window_events = Counter()
    for query in (query_matrix or {}).get("queries", []):
        declared_queries.add(query["id"])
        query_platforms[query["id"]].add(query["platform"])
        configured[query["id"]] = query
    for run in runs.values():
        declared_queries.update(run.get("query_ids") or [])
    for source_id, candidate in candidates.items():
        platform = (sources.get(source_id) or {}).get("platform") or "unknown"
        for discovery in candidate.get("discoveries") or []:
            run_record = dict(runs.get(discovery.get("run_id")) or {})
            run_record.setdefault("id", discovery.get("run_id"))
            run_kind = _run_window_kind(run_record)
            window_candidates[run_kind].add(source_id)
            window_events[run_kind] += 1
            query_ids = discovery.get("query_ids") or [discovery.get("query_id")]
            for query_id in filter(None, query_ids):
                declared_queries.add(query_id)
                query_candidates[query_id].add(source_id)
                query_events[query_id] += 1
                query_platforms[query_id].add(platform)
    by_query = {
        query_id: {
            "candidate_count": len(query_candidates[query_id]),
            "discovery_events": query_events[query_id],
            "platforms": sorted(query_platforms[query_id]),
            "configured": query_id in configured,
            "purpose": configured.get(query_id, {}).get("purpose"),
            "cadence": configured.get(query_id, {}).get("cadence"),
        }
        for query_id in sorted(declared_queries)
    }

    candidate_states = _with_zeros(
        _counts((candidate.get("review") or {}).get("state") for candidate in candidates.values()),
        CANDIDATE_STATES,
    )
    source_platforms = _with_zeros(_counts(source.get("platform") for source in sources.values()), PLATFORMS)
    candidate_platforms = _with_zeros(
        _counts((sources.get(source_id) or {}).get("platform") for source_id in candidates), PLATFORMS,
    )
    item_platforms = _with_zeros(
        _counts((sources.get(item.get("canonical_source_id")) or {}).get("platform") for item in items.values()),
        PLATFORMS,
    )
    status_counts = _with_zeros(
        _counts((source.get("availability") or {}).get("state") for source in sources.values()), AVAILABILITY,
    )
    prompt_counts = _with_zeros(
        _counts((item.get("prompt") or {}).get("status") for item in items.values()), PROMPT_STATES,
    )

    original_states = []
    prompt_author_states = []
    posted_by_records = 0
    for item in items.values():
        attribution = item.get("attribution") or {}
        posted_by_records += len(attribution.get("posted_by") or [])
        original = attribution.get("original_video_creators") or []
        prompt_authors = attribution.get("prompt_authors") or []
        original_states.extend(role.get("status") for role in original or [{"status": "missing"}])
        prompt_author_states.extend(role.get("status") for role in prompt_authors or [{"status": "missing"}])

    right_names = sorted({name for item in items.values() for name in (item.get("rights") or {})})
    rights = {
        name: _with_zeros(
            _counts(((item.get("rights") or {}).get(name) or {}).get("status") for item in items.values()),
            RIGHTS_STATES,
        )
        for name in right_names
    }
    delivery_modes = []
    mirror_states = []
    quarantined_provider = []
    quarantined_artifact = []
    quarantine_items = set()
    mirrors_total = 0
    for item_id, item in items.items():
        for media in item.get("media") or []:
            delivery = media.get("delivery") or {}
            delivery_modes.append(delivery.get("mode"))
            for mirror in delivery.get("mirrors") or []:
                mirrors_total += 1
                state = mirror.get("state")
                mirror_states.append(state)
                if state in {"quarantined", "pending_delete"}:
                    quarantine_items.add(item_id)
                    quarantined_provider.append(mirror.get("provider"))
                    quarantined_artifact.append(mirror.get("artifact"))

    exclusion_rows = []
    for source_id, candidate in sorted(candidates.items()):
        review = candidate.get("review") or {}
        if review.get("state") not in {"excluded", "removed"}:
            continue
        source = sources.get(source_id) or {}
        exclusion_rows.append({
            "source_id": source_id,
            "platform": source.get("platform"),
            "state": review.get("state"),
            "reason_codes": review.get("reason_codes") or [],
            "note": review.get("note"),
            "reviewer_actor_id": review.get("reviewer_actor_id"),
            "reviewed_at": review.get("reviewed_at"),
            "evidence_ids": review.get("evidence_ids") or [],
            "url": source.get("url"),
        })
    exclusion_reasons = _counts(reason for row in exclusion_rows for reason in row["reason_codes"])
    window_rows = []
    for run_id, run in sorted(runs.items()):
        window = run.get("window") or {}
        window_rows.append({
            "run_id": run_id, "kind": _run_window_kind(run),
            "platform": run.get("platform"), "from": window.get("from"),
            "through_exclusive": window.get("through_exclusive"),
            "run_observed_at": run.get("run_observed_at") or run.get("ended_at"),
            "result_count": run.get("result_count", 0),
            "filtered_outside_window": run.get("filtered_outside_window", 0),
            "missing_timestamp": run.get("missing_timestamp", 0),
            "status": run.get("status"),
        })
    window_kinds = sorted(
        {row["kind"] for row in window_rows} | set(window_candidates) | {"historical", "ongoing"}
    )
    windows_by_kind = {
        kind: {
            "runs": sum(row["kind"] == kind for row in window_rows),
            "result_count": sum(row["result_count"] for row in window_rows if row["kind"] == kind),
            "discovery_events": window_events[kind],
            "candidates": len(window_candidates[kind]),
        }
        for kind in window_kinds
    }

    return {
        "generated_at": generated_at or utc_now(),
        "catalog_updated_at": catalog.get("updated_at"),
        "collection": {
            "id": (catalog.get("collection") or {}).get("id"),
            "query_matrix_version": (catalog.get("collection") or {}).get("query_matrix_version"),
            "configured_queries": len(configured),
        },
        "totals": {
            "discovery_runs": len(runs),
            "queries": len(by_query),
            "sources": len(sources),
            "candidates": len(candidates),
            "items": len(items),
            "evidence": len(catalog.get("evidence", {})),
            "actors": len(catalog.get("actors", {})),
        },
        "queries": {
            "runs_by_status": _counts(run.get("status") for run in runs.values()),
            "runs_by_platform": _with_zeros(
                _counts(run.get("platform") for run in runs.values()), PLATFORMS,
            ),
            "by_query": by_query,
        },
        "discovery_windows": {"by_kind": windows_by_kind, "runs": window_rows},
        "candidates": {"by_state": candidate_states},
        "platforms": {
            "sources": source_platforms,
            "candidates": candidate_platforms,
            "items": item_platforms,
        },
        "status": {"source_availability": status_counts},
        "prompts": {
            "by_status": prompt_counts,
            "with_text": sum(1 for item in items.values() if (item.get("prompt") or {}).get("text")),
            "with_source_url": sum(1 for item in items.values() if (item.get("prompt") or {}).get("source_url")),
        },
        "attribution": {
            "posted_by_records": posted_by_records,
            "original_video_creators_by_status": _with_zeros(_counts(original_states), ATTRIBUTION_STATES),
            "prompt_authors_by_status": _with_zeros(_counts(prompt_author_states), ATTRIBUTION_STATES),
        },
        "rights": {
            "by_scope_and_status": rights,
            "delivery_modes": _with_zeros(_counts(delivery_modes), DELIVERY_MODES),
        },
        "quarantine": {
            "mirrors_total": mirrors_total,
            "mirrors_by_state": _with_zeros(_counts(mirror_states), MIRROR_STATES),
            "quarantined_or_pending_delete": len(quarantined_provider),
            "items_affected": len(quarantine_items),
            "by_provider": _counts(quarantined_provider),
            "by_artifact": _counts(quarantined_artifact),
        },
        "exclusions": {
            "total": len(exclusion_rows),
            "by_reason": exclusion_reasons,
            "records": exclusion_rows,
        },
    }


def _table(headers: list[str], rows: list[list[object]]) -> list[str]:
    def cell(value) -> str:
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(part) for part in value)
        return str(value if value not in (None, "") else "—").replace("|", "\\|").replace("\n", " ")
    return [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
        *("| " + " | ".join(cell(value) for value in row) + " |" for row in rows),
    ]


def _count_rows(mapping: dict[str, int]) -> list[list[object]]:
    return [[key, value] for key, value in mapping.items()] or [["—", 0]]


def render_markdown(report: dict) -> str:
    collection = report["collection"]["id"] or "catalog"
    lines = [
        f"# Coverage report: {collection}", "",
        f"Generated: `{report['generated_at']}`", "",
        f"Catalog updated: `{report['catalog_updated_at'] or 'unknown'}`", "",
        "## Overview", "",
    ]
    lines += _table(["Metric", "Count"], _count_rows(report["totals"]))
    lines += ["", "## Queries", ""]
    query_rows = [
        [query_id, "yes" if values["configured"] else "historical",
         values["candidate_count"], values["discovery_events"], values["platforms"]]
        for query_id, values in report["queries"]["by_query"].items()
    ]
    lines += _table(
        ["Query", "Matrix", "Candidates", "Discovery events", "Platforms"],
        query_rows or [["—", "—", 0, 0, "—"]],
    )
    lines += ["", "## Discovery windows", ""]
    lines += _table(
        ["Kind", "Runs", "Results", "Events", "Candidates"],
        [[kind, values["runs"], values["result_count"], values["discovery_events"],
          values["candidates"]]
         for kind, values in report["discovery_windows"]["by_kind"].items()],
    )
    lines += ["", "Historical and ongoing results are reported separately; window end times are exclusive.", ""]
    lines += _table(
        ["Run", "Kind", "Platform", "From", "Through (exclusive)", "Observed at",
         "Results", "Filtered", "Missing time", "Status"],
        [[row["run_id"], row["kind"], row["platform"], row["from"],
          row["through_exclusive"], row["run_observed_at"], row["result_count"],
          row["filtered_outside_window"], row["missing_timestamp"], row["status"]]
         for row in report["discovery_windows"]["runs"]]
        or [["—", "—", "—", "—", "—", "—", 0, 0, 0, "—"]],
    )
    lines += ["", "## Candidate review states", ""]
    lines += _table(["State", "Count"], _count_rows(report["candidates"]["by_state"]))
    lines += ["", "## Platforms", ""]
    platform_keys = sorted(set().union(*(set(values) for values in report["platforms"].values())))
    lines += _table(
        ["Platform", "Sources", "Candidates", "Items"],
        [[key, report["platforms"]["sources"].get(key, 0),
          report["platforms"]["candidates"].get(key, 0), report["platforms"]["items"].get(key, 0)]
         for key in platform_keys] or [["—", 0, 0, 0]],
    )
    lines += ["", "## Source availability", ""]
    lines += _table(["Status", "Count"], _count_rows(report["status"]["source_availability"]))
    lines += ["", "## Prompts", ""]
    lines += _table(["Prompt status", "Count"], _count_rows(report["prompts"]["by_status"]))
    lines += ["", f"Items with prompt text: **{report['prompts']['with_text']}**", "",
              f"Items with a prompt source URL: **{report['prompts']['with_source_url']}**", ""]
    lines += ["## Attribution", "", "### Original video creators", ""]
    lines += _table(["Status", "Role records"], _count_rows(report["attribution"]["original_video_creators_by_status"]))
    lines += ["", "### Prompt authors", ""]
    lines += _table(["Status", "Role records"], _count_rows(report["attribution"]["prompt_authors_by_status"]))
    lines += ["", "## Rights", ""]
    rights_rows = [
        [scope, status, count]
        for scope, statuses in report["rights"]["by_scope_and_status"].items()
        for status, count in statuses.items()
    ]
    lines += _table(["Scope", "Status", "Count"], rights_rows or [["—", "—", 0]])
    lines += ["", "## Mirror quarantine", ""]
    quarantine = report["quarantine"]
    lines += _table(["Metric", "Count"], [
        ["All mirrors", quarantine["mirrors_total"]],
        ["Quarantined or pending delete", quarantine["quarantined_or_pending_delete"]],
        ["Items affected", quarantine["items_affected"]],
    ])
    lines += ["", "## Exclusions and removals", ""]
    exclusion_rows = [
        [row["source_id"], row["platform"], row["state"], row["reason_codes"],
         row["reviewer_actor_id"], row["reviewed_at"], row["note"]]
        for row in report["exclusions"]["records"]
    ]
    lines += _table(
        ["Source", "Platform", "State", "Reasons", "Reviewer", "Reviewed at", "Note"],
        exclusion_rows or [["—", "—", "—", "—", "—", "—", "No exclusions"]],
    )
    return "\n".join(lines).rstrip() + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--query-matrix", type=Path, default=DEFAULT_QUERY_MATRIX)
    parser.add_argument("--format", choices=("json", "markdown", "both"), default="both")
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--at", help="override generated_at for reproducible output")
    parser.add_argument("--allow-invalid", action="store_true",
                        help="report an invalid catalog instead of failing validation")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    catalog = read_json(args.catalog)
    if not isinstance(catalog, dict):
        raise SystemExit(f"catalog is not a JSON object: {args.catalog}")
    errors = validate_catalog(catalog)
    if errors and not args.allow_invalid:
        raise SystemExit("catalog validation failed:\n  " + "\n  ".join(errors))
    query_matrix = read_json(args.query_matrix, {})
    report = build_report(
        catalog, generated_at=args.at or catalog.get("updated_at"),
        query_matrix=query_matrix,
    )
    if errors:
        report["validation_errors"] = errors
    json_text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    markdown_text = render_markdown(report)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json_text)
    if args.markdown_out:
        args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_out.write_text(markdown_text)
    if args.format in {"json", "both"}:
        sys.stdout.write(json_text)
    if args.format == "both":
        sys.stdout.write("\n---\n\n")
    if args.format in {"markdown", "both"}:
        sys.stdout.write(markdown_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
