# Corrections, consent, and takedown

Creators, prompt authors, platform posters, rights-holders, and affected people
may request correction, source unlinking, text removal, mirror removal, or a
complete catalog takedown.

## How to request action

Open a
[new issue](https://github.com/opensource-works/awesome-minimax-h3-prompts/issues/new)
with “Takedown” or “Attribution correction” in the title. Include only what is
needed:

- the catalog item/source URL and the X or Reddit permalink;
- your relationship to the work or affected person;
- the requested action (correct attribution, remove prompt text, unlink source,
  delete R2 copy, pursue GitHub attachment deletion, or remove everything);
- supporting public evidence, if available; and
- a safe way to follow up if your GitHub account cannot receive replies.

Do not post government IDs, private messages, addresses, phone numbers, API
tokens, or sensitive personal evidence in a public issue. State that private
verification is needed and use a maintainer contact method listed on the
organization/repository. If no private channel is available, file a minimal
issue asking a maintainer to establish one.

**中文：** 请在 issue 中提供原帖链接、项目条目和希望执行的操作。不要公开身份证件、私信截图中的敏感信息或联系方式。涉及肖像、隐私、未成年人、非自愿内容或安全风险的请求会优先处理。

## Target response times

These are operational targets, not a legal waiver or guarantee:

- acknowledge a sufficiently identifiable request within **2 business days**;
- suppress the item, prompt, and project-controlled public references as soon
  as practical, with a target of **72 hours**;
- delete controlled R2 objects and record verification within **7 calendar
  days**; and
- begin GitHub attachment escalation within **7 calendar days** when remote
  deletion cannot be completed directly.

Urgent privacy, consent, safety, or exposure of personal data is prioritized.
We may suppress first and investigate attribution or ownership afterward. A
request is actionable even if an API token, scheduled sync, or source-platform
lookup is unavailable.

## Maintainer procedure

1. Record the request as evidence without copying unnecessary personal data.
2. Resolve every affected source, item, prompt, projection, and mirror.
3. Remove an included candidate with human review, using the most specific
   reason (`creator_takedown`, `permission_revoked`, `rights_denied`,
   `source_deleted`, or `source_private`):

   ```bash
   python3 scripts/review.py remove 'reddit:t3_example' \
     --reason creator_takedown \
     --evidence ev_takedown_issue_123 \
     --actor act_github_reviewer \
     --at 2026-08-11T00:00:00Z
   ```

   IDs are illustrative and must already exist. `remove` suppresses the item,
   moves every non-deleted mirror to `pending_delete`, strips its raw URL from
   the public catalog, and updates the URL-redacted retirement ledger; it does
   not erase remote objects. Exact locators belong only in access-controlled
   operations state, and queueing alone is not proof of remote deletion.
   For a pending/excluded candidate, use the `exclude` decision path with the
   same evidence rather than `remove`.
4. Preview the exact retired R2 objects, then explicitly confirm deletion. The
   command verifies the configured R2 URL boundary and performs before/after
   existence checks before recording `deleted`:

   ```bash
   python3 scripts/purge_media.py --provider r2 --item itm_x_example_0
   python3 scripts/purge_media.py --provider r2 --item itm_x_example_0 \
     --url-map .cache/retired-media-locators.json \
     --confirm-delete-r2 --at 2026-08-11T00:00:00Z
   ```

5. List affected GitHub attachments, remove project references, and pursue
   deletion through the hosting issue/comment or GitHub Support:

   ```bash
   python3 scripts/purge_media.py \
     --provider github_attachment --item itm_x_example_0
   ```

   The command is always read-only for GitHub, and reference removal alone is
   not proof of remote erasure.
6. If remote cleanup cannot finish before the repository change is published,
   scrub the live media URL from repository-visible catalog data after saving
   the exact deletion locator in an access-controlled operations record. Do
   not lose the locator before the remote cleanup attempt.
7. Run `python3 scripts/validate.py` and `python3 scripts/build.py`, publish the
   regenerated outputs, and record what was completed, pending, or outside the
   project's control.

## Availability synchronization

Maintainers can check public root-post status with official APIs:

```bash
python3 scripts/sync_availability.py --platform all --dry-run
python3 scripts/sync_availability.py --platform all
```

X needs `X_BEARER_TOKEN`. Reddit needs `REDDIT_ACCESS_TOKEN` or
`REDDIT_CLIENT_ID` + `REDDIT_CLIENT_SECRET`. An authoritative X deleted/private/
suspended response retires the source. A Reddit `removed_by_category` signal
retires it immediately; an otherwise omitted Reddit ID requires two consecutive
successful empty checks by default (`--confirm-reddit-missing 2`). Timeouts,
rate limits, and server errors are transient signals and must not be treated as
proof of deletion.

The synchronizer checks both root posts and captured X/Reddit comments. If a
root is authoritatively deleted, private, or suspended, sync applies the same
content-redaction boundary as a sensitive human removal: it clears display and
source text, prompts, annotations, attribution/provenance notes, related
evidence excerpts and review-history notes; retires mirrors; and regenerates
`data/posts.json` plus both authorized-media manifests. Only the minimal audit
structure remains. If a comment-derived prompt is removed while its parent
remains available, sync clears only that comment and content derived from it;
the parent title, text, attribution, curation, and video delivery remain intact.
A maintainer must still verify retirement state and remote cleanup. Automated
sync does not infer authorship, permission, or remote deletion.

If automated synchronization fails, the issue is the manual fallback: suppress
the identified record directly through reviewed evidence and retry remote
cleanup separately. Never make a requester wait for a scheduled discovery run.

## Limits of deletion

We can remove repository data, generated pages, and objects in storage we
control. We cannot guarantee deletion from X, Reddit, GitHub infrastructure,
search-engine/CDN caches, forks, clones, archives, or third-party copies. We
will accurately distinguish “removed from this project's public output,”
“quarantined/pending deletion,” and “remote deletion verified.”
