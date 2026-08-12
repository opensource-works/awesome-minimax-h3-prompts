#!/usr/bin/env python3
"""
Prove that data/attachments.json really maps each post to *its own* clip.

GitHub does not return a filename for a video attachment, so ingest_uploads.py
has to map uploaded URLs back to posts by order. Order is a guess. If one file
in the middle of a batch failed to upload, every URL after it shifts by one and
the README ends up showing the wrong clip under the wrong creator's name —
silently, because every player still works.

This checks the guess the only way that is left: fetch the byte length of each
remote asset and compare it to the staged file it is supposed to be.

    python3 scripts/verify_uploads.py [--index DIR/index.json]

Exits non-zero unless every mapped post matches. Anything less than N/N means
the mapping is wrong somewhere and the whole run has to be redone.

Why it is done this way: a user-attachments URL 302s to a signed S3 URL, and a
HEAD against that signed URL comes back 403. So resolve the redirect first,
then ask S3 for a single byte and read the total out of Content-Range.
"""
import json, os, subprocess, sys
import concurrent.futures as cf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ATTACH = os.path.join(ROOT, "data", "attachments.json")
# Sizes can differ slightly from the staged file if the browser re-containerised
# something; a few KB is noise, a whole different clip is not.
TOLERANCE = 64 * 1024


def find_index():
    for guess in (os.path.join(os.path.dirname(ROOT), "minimax-h3-uploads", "index.json"),
                  "/mnt/c/Users/glucose/Desktop/minimax-h3-uploads/index.json"):
        if os.path.exists(guess):
            return guess
    return None


def remote_size(url):
    """Byte length of a user-attachments asset, or (None, why)."""
    r = subprocess.run(["curl", "-s", "-o", "/dev/null", "--max-time", "60",
                        "-w", "%{redirect_url}\n%{http_code}", url],
                       capture_output=True, text=True)
    parts = r.stdout.strip().split("\n")
    signed = parts[0].strip() if parts and parts[0].strip() else url

    # One byte is enough: Content-Range carries the total.
    r = subprocess.run(["curl", "-s", "-r", "0-0", "-o", "/dev/null", "-D", "-",
                        "--max-time", "60", signed], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        low = line.lower()
        if low.startswith("content-range:") and "/" in line:
            tail = line.split("/")[-1].strip()
            if tail.isdigit():
                return int(tail), None
    # Server ignored the range request — fall back to a plain length.
    for line in r.stdout.splitlines():
        if line.lower().startswith("content-length:"):
            n = line.split(":")[1].strip()
            if n.isdigit() and int(n) > 1:
                return int(n), None
    code = [l for l in r.stdout.splitlines() if l.startswith("HTTP/")]
    return None, (code[-1].strip() if code else "no size header")


def main():
    if "--index" in sys.argv:
        idx_path = sys.argv[sys.argv.index("--index") + 1]
    else:
        idx_path = find_index()
    if not idx_path or not os.path.exists(idx_path):
        sys.exit("cannot find index.json from prepare_uploads.py — pass --index")
    if not os.path.exists(ATTACH):
        sys.exit("data/attachments.json does not exist yet — run ingest_uploads.py first")

    index = json.load(open(idx_path))
    attach = json.load(open(ATTACH))
    outdir = os.path.dirname(idx_path)

    checks = []
    for r in index:
        url = attach.get(r["id"])
        if not url:
            continue
        local_path = os.path.join(outdir, r["file"])
        local = os.path.getsize(local_path) if os.path.exists(local_path) else r["bytes"]
        checks.append((r, url, local))

    if not checks:
        sys.exit("no posts in attachments.json match the staged index — wrong index.json?")

    print(f"verifying {len(checks)} of {len(index)} staged files against GitHub\n")

    def one(item):
        r, url, local = item
        size, why = remote_size(url)
        return r, local, size, why

    ok, bad = [], []
    with cf.ThreadPoolExecutor(6) as ex:
        for r, local, size, why in ex.map(one, checks):
            if size is None:
                bad.append((r, local, None, why))
            elif abs(size - local) <= TOLERANCE:
                ok.append(r)
            else:
                bad.append((r, local, size, "size mismatch"))

    for r, local, size, why in sorted(bad, key=lambda x: x[0]["n"]):
        got = f"{size/1e6:.2f} MB" if size else why
        print(f"  MISMATCH {r['n']:03d} {r['file']:<34} staged {local/1e6:.2f} MB  remote {got}")

    print(f"\n{len(ok)}/{len(checks)} match")
    if bad:
        print("\nThe mapping is wrong. Ordered uploads shift by one when a file in the")
        print("middle fails, so a single mismatch usually means everything after it is")
        print("off too. Re-upload and re-ingest that batch — do not commit this.")
        sys.exit(1)
    unmapped = len(index) - len(checks)
    print("every mapped post points at its own clip"
          + (f" ({unmapped} staged files not uploaded yet)" if unmapped else ""))


if __name__ == "__main__":
    main()
