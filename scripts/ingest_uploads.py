#!/usr/bin/env python3
"""
Turn the text GitHub gives back after uploading the staged clips into
data/attachments.json, so build.py can emit real inline players.

Usage:
    python3 scripts/ingest_uploads.py pasted.txt [--index DIR/index.json]

Mapping is by filename when GitHub includes it (`[003_umesh_ai.mp4](url)`), and
by upload order otherwise. Order-only input must cover the files in the same
order they were staged; the script says which numbers it filled either way, so
a wrong batch is obvious before anything is committed. Re-runs merge, so you
can upload in batches and ingest each one as you go.

Order-based mapping is a guess until it is checked — run
scripts/verify_uploads.py afterwards and require N/N before committing.
"""
import json, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "attachments.json")

ASSET = re.compile(r"https://github\.com/user-attachments/assets/[0-9a-fA-F-]{36}")
NAMED = re.compile(r"\[(\d{3})_[^\]]*\]\((https://github\.com/user-attachments/assets/[0-9a-fA-F-]{36})\)")


def find_index():
    for guess in (os.path.join(os.path.dirname(ROOT), "minimax-h3-uploads", "index.json"),
                  "/mnt/c/Users/glucose/Desktop/minimax-h3-uploads/index.json"):
        if os.path.exists(guess):
            return guess
    return None


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    text = open(sys.argv[1]).read()

    if "--index" in sys.argv:
        idx_path = sys.argv[sys.argv.index("--index") + 1]
    else:
        idx_path = find_index()
    if not idx_path or not os.path.exists(idx_path):
        sys.exit("cannot find index.json from prepare_uploads.py — pass --index")
    index = json.load(open(idx_path))
    by_n = {r["n"]: r for r in index}

    found = {}
    named = NAMED.findall(text)
    if named:
        for n, url in named:
            found[int(n)] = url
        how = f"by filename ({len(named)} matched)"
    else:
        urls = ASSET.findall(text)
        seen, ordered = set(), []
        for u in urls:                       # keep first occurrence, drop repeats
            if u not in seen:
                seen.add(u); ordered.append(u)
        start = int(input(f"{len(ordered)} URLs found, no filenames. "
                          f"First one is file number? [1] ").strip() or 1)
        for i, u in enumerate(ordered):
            found[start + i] = u
        how = f"by order, starting at {start} ({len(ordered)} urls)"

    existing = json.load(open(OUT)) if os.path.exists(OUT) else {}
    added = 0
    for n, url in sorted(found.items()):
        r = by_n.get(n)
        if not r:
            print(f"  ! no staged file numbered {n} — skipped")
            continue
        if existing.get(r["id"]) != url:
            added += 1
        existing[r["id"]] = url

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(existing, open(OUT, "w"), indent=2, ensure_ascii=False)
    missing = [r["n"] for r in index if r["id"] not in existing]
    print(f"mapped {how}")
    print(f"{len(existing)}/{len(index)} posts now have an inline player (+{added} this run)")
    if missing:
        head = ", ".join(f"{n:03d}" for n in missing[:18])
        print(f"still missing {len(missing)}: {head}{' …' if len(missing) > 18 else ''}")
    else:
        print("every post has one — now run scripts/verify_uploads.py")


if __name__ == "__main__":
    main()
