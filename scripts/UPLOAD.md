# GitHub video attachment workflow (catalog v2)

This workflow mirrors a video only after permission and integrity checks. The
authoritative record is `data/catalog.json`. The old `data/attachments.json`
manifest is retired and must stay `{}`.

## Permission gate

Before staging a video, its catalog item must meet every condition below:

- the item is approved, its candidate is included, and its source is available;
- `rights.video_republication.status` is `granted` or `public_license`;
- `granted_scopes` contains both `download` and `mirror_github`;
- `evidence_ids` is non-empty, every ID resolves in `catalog.evidence`, and the
  grant has not expired.

The rights evidence should identify the grantor/license, what was granted, the
source URL or captured correspondence, and the grant/expiry dates. A public
post is not by itself permission to download and republish its video. If any
gate is absent, `prepare_uploads.py` skips the item. With the migrated catalog's
unknown rights, staging zero files is the expected safe result.

Run validation before starting:

```bash
python3 scripts/validate.py
```

## 1. Stage authorized files

Choose a limit below the attachment limit currently shown by GitHub for the
repository, then run:

```bash
python3 scripts/prepare_uploads.py ../minimax-h3-github-uploads --limit-mb 90
```

The directory contains `index.json` and names such as:

```text
ghatt--itm_x_123_0--med_itm_x_123_0_0--a1b2c3d4e5f6.mp4
```

The filename comes from stable `item_id` + `media_id`, with a collision-resistant
suffix. It never contains a view count, rank, or sequence number. Do not rename
it. `index.json` records the exact bytes and SHA-256 read from the downloaded
file, plus the permission evidence used for staging.

Each source URL and its final redirect target must remain on the platform's
approved HTTPS video hosts (`video.twimg.com` for X; `v.redd.it` or
`packaged-media.redd.it` for Reddit). The response must be `video/*`, and both
declared and streamed bytes are capped by `--limit-mb`. A redirect to another
host, an HTML/error response, or an oversized body rejects that encode without
leaving a partial staged file.

## 2. Upload in the browser and preserve the filename mapping

1. Open a [new repository issue](https://github.com/opensource-works/awesome-minimax-h3-prompts/issues/new).
2. For each file, type its full filename followed by ` = ` in the issue editor,
   then drag that exact file after the equals sign. Wait until `Uploading…` is
   replaced by the final URL before continuing. This produces a line like:

   ```text
   ghatt--itm_x_123_0--med_itm_x_123_0_0--a1b2c3d4e5f6.mp4 = https://github.com/user-attachments/assets/00000000-0000-0000-0000-000000000000
   ```

   GitHub may instead preserve a Markdown filename link such as
   `[filename.mp4](https://github.com/user-attachments/assets/…)`; that form is
   accepted too.
3. Save the complete editor text as `pasted.txt`, and submit the issue so there
   is an auditable upload record.

Upload order is irrelevant and is never used as identity. A bare asset URL is
rejected: if the browser inserted one, add the exact staged filename on the
same line before ingesting. Never reconstruct mappings from URL order.

## 3. Ingest as quarantined mirrors

```bash
python3 scripts/ingest_uploads.py pasted.txt \
  --index ../minimax-h3-github-uploads/index.json
```

Ingest re-checks the catalog rights and exact filename identity, then adds a
`github_attachment` video mirror with `state: quarantined` and its permission
evidence. It does not change delivery to `authorized_mirror` and does not write
a public attachment manifest. Partial batches may be ingested, but verification
will fail until every index entry has an explicit mapping.

## 4. Download, verify, and activate

```bash
python3 scripts/verify_uploads.py \
  --index ../minimax-h3-github-uploads/index.json
```

Verification performs a full GET of every mapped GitHub asset. Both its byte
count and SHA-256 must exactly equal the staged values; HEAD checks, approximate
sizes, and partial range requests are not accepted. Success must report `N/N`.
Only then does the script prepare the catalog changes, activate the
mirrors, set delivery to `authorized_mirror`, and refresh `data/posts.json`,
`data/github-attachments.json`, and `data/r2-mirrors.json` from the same updated
catalog. Both manifests contain only active, evidence-backed mirrors keyed by
`item_id/media_id/mirror_id`.

Any missing mapping, failed download, byte mismatch, hash mismatch, changed
permission, or changed identity exits non-zero. In that case no mirror is
activated and no manifest is written.

Finish with:

```bash
python3 scripts/validate.py
python3 scripts/build.py
python3 scripts/validate.py
```

The first validation can run immediately because verification refreshes all
catalog-dependent data projections and authorized manifests. `build.py` is
still required to regenerate the READMEs and `docs/`; the final validation
checks the post-build state.

## Manual download/audit

Open an asset URL in the browser and use the browser's download control, or use:

```bash
curl -L --fail --output filename.mp4 \
  https://github.com/user-attachments/assets/00000000-0000-0000-0000-000000000000
wc -c filename.mp4
shasum -a 256 filename.mp4
```

Compare both values with the corresponding `index.json` entry. The automated
verifier performs this same full-byte check for every entry before activation.
