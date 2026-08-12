# Contributing

The fastest way to help is to add posts we missed.

## Add a post

1. Find a post on X that shows a **MiniMax H3 video**.
2. Add its URL on its own line in [`scripts/urls.txt`](scripts/urls.txt).
3. Open a pull request. That's it — you don't need to run anything locally.

A post gets included automatically if it has a playable video attached and the text
identifies the clip as MiniMax H3. Everything else — the caption, the prompt, the
author's name and handle, the view count — is pulled from the post itself.

## What counts as H3

The model ships under several names, and the filter accepts all of them:

- **MiniMax H3**, `MiniMax-H3`, `#MiniMaxH3`
- **Hailuo 3.0**, **Hailuo 03**, **Hailuo 3** — what the Hailuo AI app calls it
- a post that names MiniMax or Hailuo and refers to `H3` separately

A bare "Hailuo" is deliberately **not** enough. Hailuo 2.3 has a large back catalogue on
X, and matching the brand alone would bury the H3 posts under it. `Hailuo 2.3` and
earlier are out of scope for this repo.

## What belongs here

- Real H3 output: clips someone actually generated and posted.
- Prompts, workflows and technique breakdowns.
- Honest failure cases and model comparisons. A post does not have to be flattering.

## What doesn't

- Reposts of someone else's generation without credit.
- Videos that aren't H3.
- Pure engagement bait with no clip and no prompt.

## Fixing a title or category

Titles and categories are guessed from the post text, so some land wrong. Correct them in
[`scripts/overrides.json`](scripts/overrides.json), keyed by post id:

```json
"2082499539735588916": {
  "title": "Speeder chase across a cliff city, single continuous shot",
  "category": "Action & VFX"
}
```

Only `title` and `category` are honoured — `harvest.py` drops anything else. Prompts,
author names and stats must stay exactly as posted; if those look wrong, the fix is a
bug report, not an override.

## Regenerating everything

```bash
python3 scripts/harvest.py          # scripts/urls.txt -> data/posts.json
python3 scripts/mirror.py           # copy clips to R2 + render animated previews
python3 scripts/build.py            # data/posts.json  -> docs/ + both READMEs
```

`harvest.py --cache` reuses `.cache/` and only fetches URLs it hasn't seen, which is
much faster while you're iterating. It needs no API key or login — post data comes from
`api.fxtwitter.com`, which is public.

`mirror.py` is the only step that needs credentials (`R2_ACCOUNT`, `R2_KEY_ID`,
`R2_SECRET`) and `ffmpeg` on your PATH. You can skip it — `build.py` falls back to
X's own URLs for anything not in `data/mirror.json`. CI runs it on merge, so a PR that
only adds a URL doesn't need to touch it at all.

The README's inline players come from a separate manual step that CI cannot do; see
[`scripts/UPLOAD.md`](scripts/UPLOAD.md) if you're maintaining the repo.

Please don't hand-edit `data/posts.json`, `docs/index.html` or the READMEs — they are
generated, and your changes will be overwritten on the next build.

## If it's your post

Everything here is credited and links back to you. If you'd still rather not be listed,
or something is wrong, [open an issue](https://github.com/opensource-works/awesome-minimax-h3-prompts/issues/new)
and it comes down — no questions asked.
