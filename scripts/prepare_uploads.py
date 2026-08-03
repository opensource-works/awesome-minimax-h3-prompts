#!/usr/bin/env python3
"""
Stage every clip as a numbered file to drag into a GitHub comment box.

GitHub turns a bare github.com/user-attachments/assets/<uuid> URL into a real
video player, but those URLs only come from uploading through the browser —
there is no API for it. So this script does the part that can be automated:
pick a variant that fits GitHub's attachment limit, download it, and number it
so the uploaded URLs can be mapped back to posts by order.

    python3 scripts/prepare_uploads.py [outdir]

Then follow scripts/UPLOAD.md.
"""
import json, os, re, subprocess, sys
import concurrent.futures as cf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(ROOT), "minimax-h3-uploads")

# GitHub caps video attachments at 100 MB on paid plans and 10 MB on free ones.
# opensource-works is on Team, so 100 MB — staying clear of the edge at 90.
# Check before trusting this: gh api orgs/<org> --jq .plan.name
LIMIT = int(os.environ.get("UPLOAD_LIMIT_MB", "90")) * 1024 * 1024
MAX_HEIGHT = 1080


def variants(video):
    out = []
    for f in video.get("formats") or []:
        m = re.search(r"/(\d+)x(\d+)/", f.get("url", ""))
        if f.get("container") == "mp4" and m:
            out.append((int(m.group(1)), int(m.group(2)), f["url"]))
    if not out:
        out = [(video.get("width") or 0, video.get("height") or 0, video["url"])]
    return sorted(out, key=lambda x: x[0] * x[1])


def head_size(url):
    r = subprocess.run(["curl", "-sIL", "--max-time", "30", url], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if line.lower().startswith("content-length:"):
            return int(line.split(":")[1])
    return 0


def choose(p):
    """Biggest variant that is <=1080p and still under GitHub's limit.

    Stepping down a variant is how an oversized clip gets under the cap — never
    a re-encode. X already published these encodes; transcoding them has come
    out both bigger and worse.
    """
    vs = [v for v in variants(p["video"]) if v[1] <= MAX_HEIGHT] or variants(p["video"])[:1]
    for w, h, url in reversed(vs):                 # largest first
        n = head_size(url)
        if n and n <= LIMIT:
            return url, w, h, n
    w, h, url = vs[0]
    return url, w, h, head_size(url)


def main():
    posts = json.load(open(os.path.join(ROOT, "data", "posts.json")))
    os.makedirs(OUT, exist_ok=True)

    with cf.ThreadPoolExecutor(8) as ex:
        picked = list(ex.map(choose, posts))

    index, jobs = [], []
    for i, (p, (url, w, h, n)) in enumerate(zip(posts, picked), 1):
        name = f"{i:03d}_{p['author']['handle']}.mp4"
        index.append({"n": i, "file": name, "id": p["id"], "handle": p["author"]["handle"],
                      "title": p["title"], "url": p["url"], "bytes": n, "res": f"{w}x{h}"})
        jobs.append((url, os.path.join(OUT, name), n))

    def get(job):
        url, path, n = job
        if os.path.exists(path) and abs(os.path.getsize(path) - n) < 4096:
            return os.path.getsize(path)
        subprocess.run(["curl", "-sL", "--max-time", "600", "-o", path, url], check=True)
        return os.path.getsize(path)

    with cf.ThreadPoolExecutor(6) as ex:
        sizes = list(ex.map(get, jobs))

    # Record the size actually on disk, not the HEAD estimate — verify_uploads.py
    # compares GitHub's copy against this number byte for byte.
    for r, s in zip(index, sizes):
        r["bytes"] = s
    json.dump(index, open(os.path.join(OUT, "index.json"), "w"), indent=2, ensure_ascii=False)

    over = [r for r, s in zip(index, sizes) if s > LIMIT]
    print(f"staged {len(index)} files in {OUT}")
    print(f"total {sum(sizes)/1e6:.0f} MB | largest {max(sizes)/1e6:.1f} MB | cap {LIMIT/1e6:.0f} MB")
    if over:
        print(f"WARNING: {len(over)} still over the cap — they will fail to upload:")
        for r in over:
            print(f"  {r['file']} {r['bytes']/1e6:.0f} MB")
    else:
        print("every file is under the cap")


if __name__ == "__main__":
    main()
