# Catalog v2 data model

`data/catalog.json` is the authoritative record. `data/posts.json`, the
READMEs, `docs/`, `data/r2-mirrors.json`, and coverage reports are generated or
lossy public projections. The retired `data/mirror.json` and
`data/attachments.json` must stay `{}`. Decisions and rights must never be
written only to a projection.

## Entity map

| Entity | Purpose |
| --- | --- |
| `discovery_runs` | API/manual run ID, historical/ongoing window and exact exclusive bounds, platform, query IDs, runtime, filtered counts, status, result count, and optional log/errors |
| `actors` | A person, organization, unknown identity, reviewer, or automation identity observed on a platform |
| `sources` | One canonical X post/reply or Reddit post/comment, including stable native ID, permalink, poster, hashed text/media metadata, fetch data, and availability; raw text/locators stay in a private cache |
| `evidence` | A source-backed observation supporting discovery, attribution, review, rights, release scope, or availability |
| `candidates` | A discovered source and its human-review lifecycle: `pending`, `included`, `excluded`, or `removed` |
| `items` | A curated creative example that may cluster a canonical source with comments, cross-posts, or repost evidence |

Stable source IDs are `x:<status-id>` and `reddit:<fullname>` (for example,
`reddit:t3_abcd`). Comments are independent sources with a parent source; they
are not flattened into the parent post.

## Attribution roles

An item separates these relations:

| Relation | Meaning | Must not be inferred from |
| --- | --- | --- |
| `posted_by` | Account that published a specific source | Ownership of the file or work |
| `original_video_creators` | Credited creator(s) of the video | Poster identity, watermark alone, or possession |
| `prompt_authors` | Credited author(s) of the prompt | Video creator or parent-post author |
| reviewer/editor actor | Person/automation that curated or annotated | Any creative authorship role |

Creator and prompt-author claims carry an attribution status (`confirmed`,
`claimed`, `inferred`, `unknown`, or `disputed`) and evidence. “Unknown” is a
valid result; it is safer than silently collapsing roles.

## Prompts, comments, and annotations

A prompt record states status, text, language, source ID/URL, capture method,
whether it is verbatim, and evidence IDs.

- A prompt copied exactly from the post body points to the post and uses
  `capture_method: post_text`.
- A prompt copied from a reply/comment points to that comment source, uses
  `capture_method: comment_text`, and attributes the comment to its commenter.
  Automatic promotion requires the comment actor to match the root-post actor;
  other commenters' claims remain evidence pending human review.
- A referenced-but-unavailable prompt records that state without invented text.
- A translation, summary, OCR repair, category, or title is editorial output.
  Its provenance identifies an editor or rule, time/version, and evidence; it
  is never presented as a verbatim source prompt.

Annotations should preserve what was observed and what was added by the
project. A source excerpt may support a claim, but the evidence record should
remain short and traceable rather than copying an entire third-party post.

## Source, item, and duplicate relationships

One source can be a candidate before it becomes an item. One item can reference
multiple sources when comments, cross-posts, or repost chains concern the same
creative work. `canonical_source_id` selects the primary source;
`duplicate_cluster_id`, media fingerprints, and `repost_chain` record why other
sources were joined or excluded. Deduplication never changes the platform
poster into the original creator.

## Delivery and rights

Each media record has a delivery mode:

- `source_link` (default) — link to the canonical platform source;
- `official_embed` — use a platform-supported embed where policy allows; or
- `authorized_mirror` — serve a recorded mirror only after a rights gate.

`video_republication` and `prompt_republication` are separate. Each records a
status, license, scopes, grantors, grant/expiry times, and evidence. An active
R2 or GitHub mirror is public only when evidence exists and the video grant
covers `download` plus `mirror_r2` or `mirror_github`; previews also require
`derive_preview`. See [RIGHTS.md](RIGHTS.md).

Prompt capture and prompt republication are also separate. The public index may
display `verbatim`/`partial` text from a public source with exact provenance,
prompt-author status, and `prompt_republication.status: unknown`. That state is
not permission and does not relicense the prompt under MIT. Text is suppressed
when rights are `denied` or `revoked`, and remains subject to takedown.

Mirror lifecycle states are `active`, `quarantined`, `pending_delete`, and
`deleted`. Retired `quarantined`/`pending_delete` records are excluded from
public projections and their raw URLs are immediately stripped from the public
catalog, leaving hashes and lifecycle metadata. Private operational locators
are not a deletion claim; only `deleted` after a remote check means verified deletion.

## Review invariants

Discovery never includes a candidate. Every include/exclude/remove mutation
requires a human-review reason, existing evidence IDs, an existing actor, and a
timestamp. Safe inclusion starts source-link-only with creator, prompt author,
and republication rights unknown. Later attribution or rights claims require
their own evidence; curation approval is not permission.

Removal for `creator_takedown`, `permission_revoked`, `rights_denied`, source
unavailability, or safety reasons is content-redacting: source text, prompt
text, annotations, content excerpts, and display titles are removed from the
repository-publishable record. The decision keeps only reason codes, reviewer,
time, and evidence IDs as the minimal audit trail. Both authorized-media
manifests are regenerated so a formerly active URL cannot survive the decision.

Source availability is distinct from curation. Authoritative deletion/privacy
signals for a root post redact source text, remove prompt text, retire the item,
and move mirrors toward deletion. Transient API errors retain a last-known-public
item but record the failed check. The availability synchronizer checks
both `kind: post` and captured `kind: comment` sources. For Reddit, deletion
from an omitted ID requires consecutive successful API responses; a network or
API failure resets that omission streak and cannot contribute to confirmation.

## Coverage semantics and known loss

Coverage reports measure the documented query matrix, time window, retained
discovery runs, and review state. They do not claim exhaustive coverage of X or
Reddit. Private, deleted, restricted, search-invisible, misspelled, and
API-inaccessible material is outside the measurable population.

For this collection the fixed historical window begins 2026-07-29 and ends at
the exclusive observed cutoff 2026-08-11T12:30:00Z. Ongoing recent-search runs
start after that cutoff and are reported separately. The retained report contains legacy and
backfill query IDs, including partial runs; it does not prove full execution of
the current query matrix. In a separate early Seedance historical search, 614
unique X status IDs were returned but not persisted before the endpoint became
unavailable. That number is disclosed because the projects share collection
methodology; it is not MiniMax H3 data or a coverage count for either repository.

## Legacy migration

Legacy v1 records were migrated with limited provenance. Mirrors without
permission evidence were removed from public manifests and quarantined. The
2026-08-11 retirement ledger has 48 entries, all `pending_delete`; it is a work
queue for verified remote cleanup, not proof that the underlying R2 or GitHub
objects were erased or that a live URL is absent from maintenance data.
