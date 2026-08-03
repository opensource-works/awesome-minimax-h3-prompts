# Uploading clips for inline GitHub players

GitHub renders a **bare** `https://github.com/user-attachments/assets/<uuid>` URL —
alone on its own line — as a real video player with sound and controls. That is the
only markup that works. Wrap the same URL in `<video>` or in a `[]()` link and it
renders as plain text instead.

This is not about where the file is hosted. GitHub's Markdown sanitiser strips the
`<video>` tag itself, so pointing it at R2, at `video.twimg.com`, or even at
`github.com` makes no difference — the tag is removed either way. You can confirm any
of this without pushing:

```bash
gh api /markdown -X POST -f text="$(cat some.md)" | head
```

Those URLs are only issued by uploading through the browser. There is no REST API and
`gh` cannot do it, so this one step is manual. Everything around it is scripted.

## 1. Check the attachment cap

```bash
gh api orgs/opensource-works --jq .plan.name
```

`free` means a **10 MB** cap per video; every paid plan means **100 MB**. Getting this
wrong wastes the whole round — files staged for 100 MB simply fail to upload on a free
org, one by one, after you have already dragged them in. `opensource-works` is on
`team`, so the cap is 100 MB and `prepare_uploads.py` stages up to 90 MB.

## 2. Stage the files

```bash
python3 scripts/prepare_uploads.py /mnt/c/Users/<you>/Desktop/minimax-h3-uploads
```

You get `001_handle.mp4` … `NNN_handle.mp4` plus `index.json`. The numbers are how the
uploaded URLs get mapped back to posts, so **don't rename or reorder them**.

Each file is the largest encode X publishes at 1080p or below that still fits under the
cap; anything larger steps down to the next variant automatically. Nothing is
re-encoded — a transcode of one of these came out both larger and worse.

## 3. Upload them

1. Open a new issue on the repo — it is only being used as an upload surface:
   <https://github.com/opensource-works/awesome-minimax-h3-prompts/issues/new>
2. Drag files into the comment box **in numeric order**, about 8–10 at a time.
3. Wait for every `Uploading…` placeholder in that batch to turn into a URL before
   dragging the next batch. Uploading out of order, or copying the text before a batch
   finishes, is what breaks the mapping.
4. When they are all done, submit the issue (title it something like
   `Video attachments — do not close`). Submitting isn't strictly required for the URLs
   to work, but it keeps a record of what was uploaded and when.

## 4. Feed the URLs back, then verify

```bash
python3 scripts/ingest_uploads.py pasted.txt
python3 scripts/verify_uploads.py      # must print N/N
python3 scripts/build.py
```

`ingest_uploads.py` maps by filename when GitHub includes one, and by upload order
otherwise — in which case it asks which file number the first URL belongs to, so you
can ingest batch by batch. It prints which numbers are still missing, and re-runs
merge, so a partial upload is safe to resume.

**`verify_uploads.py` is not optional.** GitHub returns no filename for a video
attachment, so an order-based mapping is a guess. If one upload in the middle of a
batch fails, every URL after it shifts by one — every player still works, but each clip
is now filed under the wrong creator's name. The script fetches each remote asset's
byte length and compares it to the staged file it claims to be. Require N/N before
committing; one mismatch usually means everything after it is wrong too.

## Keeping it working

New posts contributed later need this same manual pass, otherwise they fall back to the
animated preview. Nothing breaks — `build.py` uses whichever is available per post — but
CI cannot produce inline players on its own.
