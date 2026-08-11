# Rights, attribution, and media operations

This repository is a source-backed index, not a blanket license for the works
it references. The default public delivery mode is `source_link`: visitors are
sent to the original X or Reddit source, and third-party video is not copied.

## Keep the roles separate

Every claim is recorded independently:

- **Platform poster (`posted_by`)** — the account that published a particular
  post or comment.
- **Original video creator (`original_video_creators`)** — the person or
  organization credited with creating the video.
- **Prompt author (`prompt_authors`)** — the person or organization credited
  with writing the prompt.
- **Reviewer/editor** — the maintainer or automation responsible for a curation
  decision or annotation, not a creator by virtue of editing the catalog.

These roles may point to the same actor only when evidence says so. A watermark,
username, repost caption, or possession of a file is a lead, not proof. Unknown
or disputed attribution stays explicitly unknown or disputed.

**中文：** 发帖者、视频原作者、提示词作者和项目编辑是四种独立身份。除非有可核查证据，否则不得合并，也不得因为“谁发了帖子”就推断“谁创作了视频或提示词”。

## Prompt and comment provenance

A verbatim prompt must retain its exact source ID, permalink, capture method,
language, and evidence. If it came from a reply or Reddit comment, that comment
is stored as its own source with `kind: comment`, `parent_source_id`, its own
`posted_by_actor_id`, and its own URL. The item prompt then points to the comment
and uses `capture_method: comment_text` (`source_comment` is the evidence kind,
not the capture method). Automation creates only metadata for a pending proposal,
and only when the comment actor matches the root-post actor. A human must accept
or reject it; no automation establishes authorship or republication rights.

Translations, summaries, OCR corrections, inferred prompt fragments, titles,
categories, and editorial notes must be labeled as annotations or generated
fields with their editor/rule, timestamp, and evidence. They must not be marked
`is_verbatim: true` and must not silently transfer authorship to an editor or
the parent-post author.

Verified reply imports are bound to the exact reviewed payload bytes. The
tracked `data/verified-prompt-imports.json` additionally locks every reviewed
reply ID, root/item identity, public URL, author handle, timestamp, parent, and
prompt-text SHA-256. The command refuses a changed or unreviewed file before
importing any prompt text:

```bash
python3 scripts/import_verified_prompt_threads.py verified-prompts.json \
  --payload-sha256 REVIEWED_PAYLOAD_SHA256 \
  --grantor-map .cache/confirmed-grantors.json \
  --grantor-map-sha256 REVIEWED_GRANTOR_MAP_SHA256 \
  --repo-key minimax \
  --captured-at 2026-08-11T16:52:51Z \
  --granted-at 2026-08-11T16:52:51Z \
  --confirm-maintainer-attestation
```

## Separate permission records

Video and prompt rights are evaluated separately in `video_republication` and
`prompt_republication`. Each record must state the rights status, grantor,
time, expiry (if any), and exact scopes. A grant is verified either by public
evidence or by a maintainer attestation for a private confirmation. Attribution
is required but does not replace permission, and `unknown` is not a grant.

For video media, the implemented scope strings are:

- `download` — obtain and temporarily process the source asset;
- `mirror_r2` — store and serve it from the configured Cloudflare R2 bucket;
- `mirror_github` — upload and serve it as a GitHub user attachment; and
- `derive_preview` — create and serve a transformed animated preview.

An R2 video needs `download` + `mirror_r2`; a GitHub attachment needs
`download` + `mirror_github`; a preview also needs `derive_preview`. Do not
invent broad scopes such as “internet use.” Prompt reproduction must be
described just as precisely (for example, full text versus excerpt, translation,
commercial use, and duration), even though the media gate does not interpret
prompt scopes. A limited quotation may have a separate basis under applicable
law, but it is not MIT-licensed; record the basis when known.

This index may display `verbatim` or `partial` text captured from a public source
while `prompt_republication.status` remains `unknown`, provided the exact source
URL, capture provenance, and prompt-author status remain visible. `unknown`
describes the record; it is not a permission claim, a public license, or an MIT
grant. `denied` and `revoked` prompt text must be hidden, and a prompt author or
rights-holder may request removal under [TAKEDOWN.md](TAKEDOWN.md).

The `unknown` field records that this catalog has no confirmed permission grant;
it does not decide whether applicable law independently permits a particular
display. That assessment remains a case-by-case maintainer responsibility, and
the index's display is not a representation that permission was obtained.

Valid evidence identifies the asset, the grantor and their authority, the
allowed destinations/transformations, the date, and any expiry or revocation
terms. A public-license record also needs the license name/version and a source
showing that the license applies to that exact work. Likes, credits, platform
availability, API access, and “please share” without scope are not permission.

The machine gate enforces that distinction. A direct grant can cite public
evidence with `kind: permission`, or it can use
`grant_verification: maintainer_attestation` when a maintainer has privately
confirmed the grant. The attestation form still requires the grantor, grant time,
and exact scopes, but keeps `evidence_ids` and mirror
`permission_evidence_ids` empty. Supporting private material is never
committed.

Evidence-backed records need a `rights_assertion` whose `asset_item_ids`,
`grantor_actor_ids`, and `granted_scopes` cover the same item, grantor, and
scopes as the rights record. Public-license assertions must also repeat the
matching `license_spdx`. A `model_release`, source post, like, or other generic
public record cannot activate a mirror. Public evidence may be projected with
its URL; a maintainer attestation can activate the same scoped gate without
publishing private proof or representing that such proof is publicly
verifiable.

Run `python3 scripts/sync_rights_expiry.py` before validation or generation.
It converts elapsed positive grants to `revoked`, queues video mirrors for
cleanup, suppresses expired prompt grants, and rebuilds both authorized-media
manifests. `python3 scripts/authorized_manifests.py` rebuilds
`data/github-attachments.json` and `data/r2-mirrors.json` directly from the
canonical catalog; `--check` fails when either projection is stale.

## Authorized R2 workflow

The preferred workflow is the evidence gate in `mirror.py`:

```bash
export R2_ACCOUNT='...'
export R2_KEY_ID='...'
export R2_SECRET='...'

python3 scripts/mirror.py --dry-run
python3 scripts/mirror.py
```

The script checks status, expiry, evidence-ID resolution, and required scopes.
It cannot determine whether evidence is genuine, whether a grantor controls all
rights, or whether license/consent terms cover the real-world use; a human must
review those semantics first. Only structurally eligible catalog records are
downloaded and uploaded.

Authorized objects are projected into the namespaced
`data/r2-mirrors.json` manifest (`item_id/media_id/mirror_id`); the retired
`data/mirror.json` must remain `{}`. Each attempted PUT is journaled without a
public URL. If a later job fails, the run confirms all newly created keys are
absent and never deletes a key that existed before the run. An unconfirmed
rollback leaves `data/r2-upload-recovery.json` and blocks validation until a
maintainer reconciles it.

Official hydration keeps expiring media locators only in the gitignored,
mode-`0600` `.cache/media-locators.json`; tracked catalog observations retain
metadata but no direct/thumbnail/variant URLs. `mirror.py --volatile-cache`
requires exact collection/source/media identity and HTTPS platform hosts. X
video downloads are limited to `video.twimg.com`; Reddit video downloads are
limited to `v.redd.it` and `packaged-media.redd.it` (thumbnail-only allowlists
are separate). Redirects are rechecked, the response must have a `video/*`
content type, and the maximum download is 512 MiB.

For an exceptional manual operation, resolve one exact bucket/key first and use
the minimal R2 client. These raw commands bypass `mirror.py`'s catalog gate and
must not be used unless the same human rights review has passed and the catalog
record/evidence is ready:

```bash
python3 scripts/r2.py head minimax-h3 'v2/item/media.mp4'
python3 scripts/r2.py put minimax-h3 'v2/item/media.mp4' /path/to/file.mp4 video/mp4
python3 scripts/r2.py ls minimax-h3 'v2/item/'
python3 scripts/r2.py delete minimax-h3 'v2/item/media.mp4'
```

`r2.py` has no authenticated `get` command. An authorized public object can be
downloaded by its recorded public URL, for example:

```bash
curl --fail --location --output media.mp4 'https://pub-1dec941b0f984f2e8e9c9636ee482592.r2.dev/v2/item/media.mp4'
```

Downloading is still governed by the recorded rights; a public URL is not a
license. Never place credentials in command history, logs, issues, or commits.

## Authorized GitHub attachment workflow

GitHub user-attachment upload is manual; the repository has no supported REST
or `gh` command that creates such an attachment. After confirming `download` +
`mirror_github` permission:

```bash
python3 scripts/prepare_uploads.py /path/to/authorized-staging \
  --volatile-cache .cache/media-locators.json
```

This writes stable filenames plus `/path/to/authorized-staging/index.json`.
Drag the prepared files into a GitHub issue/comment and preserve GitHub's
filename-to-`https://github.com/user-attachments/assets/...` mappings. Then
copy the editor text to a file and run:

```bash
python3 scripts/ingest_uploads.py /path/to/pasted-urls.txt \
  --index /path/to/authorized-staging/index.json
python3 scripts/validate.py
```

The v2 ingester refuses bare/order-based guesses and leaves every mapping
`quarantined`. Download and verify every mapped asset against the staged bytes
and SHA-256 before activation:

```bash
python3 scripts/verify_uploads.py \
  --index /path/to/authorized-staging/index.json
python3 scripts/validate.py
python3 scripts/build.py
```

Verification must report `N/N`; any missing mapping, download failure, changed
rights, byte mismatch, or hash mismatch exits without activating a mirror.
Only a successful full check switches delivery to `authorized_mirror` and
writes `data/posts.json` plus both namespaced authorized-media manifests from
the updated catalog. Run `build.py` afterward for READMEs and `docs/`. See
`scripts/UPLOAD.md` for the exact browser mapping format. An authorized
attachment can also be audited/downloaded
with `curl --fail --location` and its recorded URL. Do not use an issue as a
rights-evidence store when the evidence contains private information.

## Private-grant recovery

When a maintainer confirms a grant privately, supporting material and private
locators stay in gitignored, access-controlled storage. A
previously uploaded GitHub attachment can be reactivated only after the private
recovery report covers every catalog item and the downloaded file matches its
recorded byte count and SHA-256:

```bash
python3 scripts/activate_verified_attachments.py \
  --report .cache/media-recovery-report.json \
  --report-sha256 REVIEWED_REPORT_SHA256 \
  --locator-cache .cache/retired-media-locators.json \
  --grantor-map .cache/confirmed-grantors.json \
  --grantor-map-sha256 REVIEWED_GRANTOR_MAP_SHA256 \
  --staging .cache/authorized-media-staging \
  --granted-at 2026-08-11T16:52:51Z \
  --confirm-maintainer-attestation
```

Add `--confirm-prompt-republication` only when the prompt-republication scope
was separately confirmed; video confirmation alone never enables prompt text.

The private grantor map binds each catalog item to the actor whose grant was
separately confirmed; the script never infers that actor from poster or creator
fields. The command writes only the grantor, time, scopes, verification mode, attachment
identity, and integrity metadata to the catalog. It never copies the private
report, locator cache, or supporting material into Git.

## Legacy-media quarantine

The initial 2026-08-11 retirement ledger listed 48 legacy mirrors (32 R2 and 16
GitHub attachments). After private grants were confirmed and full-file integrity
checks passed, the 16 GitHub video attachments were reactivated for the gallery.
The remaining retirement queue contains 32 R2 video/preview records. Their raw
URLs stay out of the public catalog; exact operational locators belong only in
an access-controlled private cache. This redaction is not proof that a remote
object was erased.

R2 objects require an explicit, verified key deletion and a recorded follow-up
check. GitHub attachment cleanup may require removing the hosting issue/comment
and contacting GitHub Support; removing a Markdown reference or closing an
issue does not guarantee that GitHub has deleted the stored object. CDN caches,
forks, and third-party copies are outside this repository's direct control.

Use the retirement tool to inspect the queue safely:

```bash
python3 scripts/purge_media.py
python3 scripts/purge_media.py --provider r2 --item itm_x_example_0
python3 scripts/purge_media.py --provider github_attachment --item itm_x_example_0
```

All three commands are read-only. R2 deletion occurs only when a maintainer
adds both the exact provider and the irreversible confirmation flag:

```bash
python3 scripts/purge_media.py --provider r2 --item itm_x_example_0 \
  --url-map .cache/retired-media-locators.json \
  --confirm-delete-r2 --at 2026-08-11T00:00:00Z
```

The URL map is a local `mirror_id -> URL` JSON object and must never be
committed or be group/other-readable (use mode `0600`). Before the first remote
request, the tool validates the entire
selected batch against the retirement state, provider/artifact/item/media/source
identity, URL SHA-256, catalog former-URL hash, and configured R2 boundary. One
mismatch aborts the whole batch. It then checks existence before and after each
deletion and updates a matching existing retirement-ledger entry. `review.py`,
availability sync, and mirror replacement
create retirement entries when they queue an active object for deletion; the
purge tool itself does not invent a missing entry. A rights-cleared GitHub
upload in `quarantined` state is awaiting byte verification, not deletion, and
is intentionally excluded from this ledger. The tool only lists retired GitHub
attachments for manual escalation and never marks them deleted. If cleanup
cannot finish before a
takedown change is published, remove the live URL from the repository-visible
catalog after preserving the exact locator in an access-controlled operations
record; otherwise “quarantined” can still disclose a working URL.

Revocation and removal are handled under [TAKEDOWN.md](TAKEDOWN.md).
