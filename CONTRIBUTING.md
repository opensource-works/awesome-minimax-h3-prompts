# Contributing

Thank you for helping improve this index. We collect public MiniMax H3 examples
from X and Reddit, but discovery is only the start: every candidate is reviewed
by a person before it can appear in the public collection.

## Submit an X or Reddit candidate

The easiest contribution is an
[issue](https://github.com/opensource-works/awesome-minimax-h3-prompts/issues/new)
containing a canonical X status URL or Reddit submission URL. If the prompt is
in a Reddit comment, include that exact comment permalink as supporting
evidence beside the parent submission. One issue may contain a newline-separated
batch. You do not need API credentials.

Please include, when known:

- the model/version and why the example is in scope;
- where the prompt appears: post body, an exact reply/comment permalink, or
  “not provided”;
- the platform poster, claimed original video creator, and claimed prompt
  author as three separate roles, with source links for each claim;
- the earliest/original source if the candidate is a repost or cross-post; and
- any explicit permission or public license. Public posting and attribution do
  not by themselves permit downloading or re-hosting.

Do not upload a copy of someone else's video to the issue. A source link is
enough. Never put API tokens, private messages, personal contact details, or
other non-public evidence in an issue or pull request.

`scripts/urls.txt` is a legacy X-only compatibility input. It is not the
canonical database and cannot represent Reddit, comments, evidence, rights, or
review decisions. New submissions should use an issue; maintainers import them
into `data/catalog.json`.

**中文速览：** 请提交 X/Reddit 原始链接，并分别写明“发帖账号、视频原作者、提示词作者”；三者不能默认视为同一人。提示词若在回复或评论中，请给出该条回复/评论的永久链接。默认只收录来源链接，不下载、不搬运视频。

## What counts as MiniMax H3

In-scope aliases include MiniMax H3, MiniMax-H3, `#MiniMaxH3`, Hailuo H3,
Hailuo 3/3.0/03, and 海螺 H3 when the source context verifies the model. A bare
“Hailuo” is not sufficient: it may refer to earlier Hailuo models. Comparisons,
workflows, and failure cases are welcome when an H3 result is identifiable.

We normally exclude unrelated models, posts without a relevant video or useful
workflow, duplicate media, and uncredited reposts whose original source cannot
be verified. Borderline cases remain pending until evidence is sufficient.

## Maintainer discovery workflow

Discovery uses the official X API v2 and Reddit OAuth API. It stores stable
identifiers, permalinks, discovery provenance, and limited metadata; it does
not publish a candidate or download its media.

Use environment variables or repository secrets and never commit credentials:

```bash
export X_BEARER_TOKEN='...'
export REDDIT_ACCESS_TOKEN='...'
# Or let the script request a Reddit client-credentials token:
export REDDIT_CLIENT_ID='...'
export REDDIT_CLIENT_SECRET='...'

python3 scripts/discover.py --platform all --window ongoing --max-pages 10 --dry-run
python3 scripts/discover.py --platform all --window ongoing --max-pages 10
```

Ongoing X discovery is limited to the official recent-search window. Historical
X search uses `--window historical` and requires the corresponding paid/full-archive API
entitlement:

```bash
python3 scripts/discover.py --platform x --window historical --max-pages 25 --dry-run
```

The historical archive is fixed to `[collection start, 2026-08-11T12:30:00Z)`.
Weekly ongoing runs start at that exact cutoff, use the API's recent window, and
are reported separately with their actual request end and run-observed time.

To import issue submissions or another documented inventory without searching:

```bash
python3 scripts/discover.py \
  --import-file /path/to/candidates.txt \
  --query-id manual.issue-123 \
  --at 2026-08-11T00:00:00Z \
  --dry-run
```

After checking the dry run, repeat without `--dry-run`. The file must contain
one canonical X status or Reddit submission URL per line. The manual importer
does not preserve a standalone Reddit comment ID; supply comment permalinks as
evidence. Hydration may capture the comment if it falls within the configured
API pages/limits and signal filter; otherwise a maintainer must verify it through
the official API and add the separate comment source/evidence explicitly. For a
structured historical inventory, use
`python3 scripts/import_backfill.py /path/to/inventory.json`.

Hydration also uses official APIs. Comment capture is opt-in and retains only
replies that may contain a prompt, credit, source, or workflow signal:

```bash
python3 scripts/hydrate.py --platform all --with-comments --pending-only \
  --volatile-cache .cache/media-locators.json --dry-run
python3 scripts/hydrate.py --platform all --with-comments --pending-only \
  --volatile-cache .cache/media-locators.json
```

A captured comment is a separate source with its own commenter and permalink.
Raw post/comment text and volatile media URLs live only in the gitignored,
owner-only cache; canonical sources retain hashes, lengths, and stable IDs.
Automation may propose a same-poster prompt, but a human must explicitly accept
or reject it. Other commenters never establish prompt authorship by themselves.
Accepted/rejected prompt payloads are consumed from the cache when it is present.
`source_texts` remain only for unresolved attribution/annotation review; the
workflow cache is ephemeral, and local maintainers should delete those entries
or the whole cache when review ends.

## Human review and evidence

There is no automatic inclusion. Review requires an existing reviewer actor,
existing evidence records, explicit reason codes, and an RFC 3339 timestamp:

```bash
python3 scripts/review.py list --state pending --platform reddit
python3 scripts/review.py show 'reddit:t3_example'

python3 scripts/review.py include 'reddit:t3_example' \
  --reason meets_scope \
  --evidence ev_source_reddit_t3_example \
  --actor act_github_reviewer \
  --title 'Human-reviewed public title' \
  --reject-observed-prompt \
  --at 2026-08-11T00:00:00Z
```

The IDs above are illustrative; they must already resolve in the catalog.
If a pending prompt proposal is verbatim, acceptance additionally requires
`--accept-observed-prompt --volatile-cache .cache/media-locators.json`; rejection
uses `--reject-observed-prompt` and remains possible when that ephemeral cache is gone.
Inclusion creates or approves a safe `source_link` item. It deliberately leaves
the original creator, prompt author, and republication permission unknown until
separate evidence supports those claims. Use `exclude` for a candidate that was
never included and `remove` for an included item; run each subcommand with
`--help` for its permitted reason codes and arguments.

Human-authored titles, translations, categories, and notes must identify their
editor, time, method/provenance, and evidence. Never turn a paraphrase or
translation into a “verbatim” prompt, and never copy a comment prompt without
retaining the comment source and commenter attribution.

## Media is a separate, rights-gated step

Source linking is the default. `scripts/mirror.py` downloads and uploads only
when `video_republication` is `granted` or covered by a public license, the
grant is verified by public evidence or a maintainer attestation, and the exact
scopes include `download` plus the destination scope. Animated previews
additionally require `derive_preview`.
This is a structural catalog gate, not a legal judgment: a human must still
verify the substance of the evidence, the grantor's authority, license terms,
asset identity, and intended use.

```bash
python3 scripts/mirror.py --dry-run
python3 scripts/mirror.py
```

R2 needs `R2_ACCOUNT`, `R2_KEY_ID`, and `R2_SECRET`; preview generation also
needs `ffmpeg`. Successful R2 runs regenerate the authorized, namespaced
`data/r2-mirrors.json`; the old `data/mirror.json` remains `{}`. A partial
upload failure rolls back every new object, preserves pre-existing keys, and
retains `data/r2-upload-recovery.json` only when cleanup cannot be confirmed.
GitHub issue attachments are a separate manual process. Do not
run either workflow until the checks in [RIGHTS.md](RIGHTS.md) pass. Retired
media cleanup uses `scripts/purge_media.py`, which is read-only by default and
can delete only R2 objects after `--provider r2`, a private gitignored
`--url-map`, and `--confirm-delete-r2` are explicit; it never deletes GitHub
attachments.

## Validate and build

`data/catalog.json` is authoritative. `data/posts.json`, the READMEs, coverage
reports, and `docs/` are generated or lossy projections and must not be edited
as the source of truth.

```bash
python3 scripts/validate.py
python3 scripts/report_coverage.py --format both
python3 scripts/build.py
```

Before opening a PR, review the diff so that credentials, private evidence,
volatile source-media URLs, and unapproved mirrors are not present.

## Coverage is bounded, not complete

The documented query matrix fixes historical public-search coverage to
`[2026-07-29T00:00:00Z, 2026-08-11T12:30:00Z)`. Ongoing recent-search runs
after that cutoff are recorded separately. The retained report is partial: it
currently represents legacy/backfill query IDs, includes partial
runs, and does not establish that every configured matrix query ran to
completion. Private, deleted, restricted, search-invisible, misspelled, and
API-inaccessible posts are outside the claim. Counts describe retained
candidates and review outcomes, never “all of X and Reddit.”

The shared collection process also has a disclosed historical loss: an early
Seedance search returned 614 unique X status IDs, but the raw identifiers were
not persisted before that endpoint became unavailable. Those IDs are not part
of this H3 catalog or any completeness claim. See [DATA_MODEL.md](DATA_MODEL.md).

## Corrections and removals

Open an
[issue](https://github.com/opensource-works/awesome-minimax-h3-prompts/issues/new)
for incorrect attribution, prompt provenance, model labels, or removal. Creator
and rights-holder requests are handled under [TAKEDOWN.md](TAKEDOWN.md). A
request does not need to wait for the next automated availability check.
