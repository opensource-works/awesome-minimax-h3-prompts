#!/usr/bin/env python3
"""
Mirror every indexed clip to R2 so playback never depends on a twimg URL that
X may rotate, and build a short animated preview for each one.

For each post we take the largest variant X publishes at 1080p or below — X's
own encode, not a re-encode of it — plus a 3-second silent WebP loop that the
README can show inline (GitHub strips <video>, but <img> survives).

    R2_ACCOUNT=... R2_KEY_ID=... R2_SECRET=... python3 scripts/mirror.py
    python3 scripts/mirror.py --dry-run     # report what's missing, upload nothing

Writes data/mirror.json: {post_id: {"mp4": url, "webp": url, "width", "height"}}.
Objects already in the bucket are skipped, so re-runs are cheap.
"""
import json, os, re, subprocess, sys, tempfile
import concurrent.futures as cf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

BUCKET = os.environ.get("R2_BUCKET", "minimax-h3")
PUBLIC = os.environ.get("R2_PUBLIC_BASE", "https://pub-1dec941b0f984f2e8e9c9636ee482592.r2.dev")
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
MAX_HEIGHT = 1080
DRY = "--dry-run" in sys.argv


def media_id(url):
    m = re.search(r"/(?:amplify_video|ext_tw_video|tweet_video)/(\d+)/", url)
    return m.group(1) if m else None


def pick_variant(video):
    """Largest mp4 variant at or below MAX_HEIGHT, exactly as published by X.

    Re-encoding here is a trap: a 5.6 MB source came back out at 7.6 MB last
    time it was tried, for strictly worse pixels. Copy the bytes X already has.
    """
    out = []
    for f in video.get("formats") or []:
        m = re.search(r"/(\d+)x(\d+)/", f.get("url", ""))
        if f.get("container") == "mp4" and m:
            out.append((int(m.group(1)), int(m.group(2)), f["url"]))
    if not out:
        return video["url"], video.get("width"), video.get("height")
    out.sort(key=lambda x: x[0] * x[1])
    ok = [v for v in out if v[1] <= MAX_HEIGHT] or out[:1]
    w, h, url = ok[-1]
    return url, w, h


def run(cmd):
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode:
        raise RuntimeError(cmd[0] + ": " + p.stderr.decode()[-400:])


def make_preview(src, dst, duration):
    """3s silent 480px loop, starting a little in to skip fades and slates."""
    start = 2 if duration > 8 else 0
    run([FFMPEG, "-y", "-v", "error", "-ss", str(start), "-t", "3", "-i", src,
         "-vf", "fps=10,scale=480:-2:flags=lanczos", "-an",
         "-c:v", "libwebp_anim", "-lossless", "0", "-q:v", "62",
         "-compression_level", "5", "-loop", "0", dst])


def main():
    import r2
    posts = json.load(open(os.path.join(ROOT, "data", "posts.json")))
    existing = set(r2.ls(BUCKET))
    print(f"bucket {BUCKET}: {len(existing)} objects already there")

    manifest, todo = {}, []
    for p in posts:
        src_url, w, h = pick_variant(p["video"])
        mid = media_id(src_url) or p["id"]
        base = f"{p['author']['handle']}_{mid}"
        mp4, webp = f"{base}.mp4", f"{base}.webp"
        manifest[p["id"]] = {"mp4": f"{PUBLIC}/{mp4}", "webp": f"{PUBLIC}/{webp}",
                             "width": w, "height": h}
        need = [k for k in (mp4, webp) if k not in existing]
        if need:
            todo.append((p, src_url, mp4, webp, need))

    print(f"{len(posts)} posts | {len(todo)} need work | {len(posts)-len(todo)} already mirrored")
    if DRY or not todo:
        json.dump(manifest, open(os.path.join(ROOT, "data", "mirror.json"), "w"),
                  indent=2, ensure_ascii=False)
        print("dry run — nothing uploaded" if DRY else "nothing to do")
        return

    done, failed = [], []

    def handle(item):
        p, src_url, mp4, webp, need = item
        tag = f"@{p['author']['handle']}"
        with tempfile.TemporaryDirectory() as tmp:
            local = os.path.join(tmp, "v.mp4")
            run(["curl", "-sL", "--max-time", "600", "-o", local, src_url])
            n = os.path.getsize(local)
            if n < 10000:
                raise RuntimeError(f"download too small ({n}B)")
            if mp4 in need:
                r2.put(BUCKET, mp4, open(local, "rb").read(), "video/mp4")
            if webp in need:
                prev = os.path.join(tmp, "p.webp")
                make_preview(local, prev, p["video"]["duration"])
                r2.put(BUCKET, webp, open(prev, "rb").read(), "image/webp")
            return tag, n

    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(handle, it): it for it in todo}
        for i, f in enumerate(cf.as_completed(futs), 1):
            it = futs[f]
            try:
                tag, n = f.result()
                done.append(n)
                print(f"  [{i}/{len(todo)}] {tag} {n/1e6:.1f} MB")
            except Exception as e:
                failed.append((it[0]["author"]["handle"], str(e)[:120]))
                print(f"  [{i}/{len(todo)}] FAIL @{it[0]['author']['handle']}: {str(e)[:120]}")

    json.dump(manifest, open(os.path.join(ROOT, "data", "mirror.json"), "w"),
              indent=2, ensure_ascii=False)
    print(f"\nmirrored {len(done)} clips, {sum(done)/1e6:.0f} MB")
    if failed:
        print(f"{len(failed)} failed:")
        for h, e in failed:
            print(f"  @{h}: {e}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
