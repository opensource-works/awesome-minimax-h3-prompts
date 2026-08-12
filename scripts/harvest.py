#!/usr/bin/env python3
"""
Hydrate the X post URLs in scripts/urls.txt into data/posts.json.

Every field in the dataset comes straight from the public post — we never
invent a prompt, a caption or an author. Posts without a playable video are
dropped, because the whole point of this index is that you can watch the clip.

    python3 scripts/harvest.py            # refresh everything
    python3 scripts/harvest.py --cache    # reuse .cache, only fetch new URLs
"""
import json, os, re, sys, time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, ".cache")
URLS = os.path.join(ROOT, "scripts", "urls.txt")
OUT = os.path.join(ROOT, "data", "posts.json")

API = "https://api.fxtwitter.com/i/status/{}"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"


# --------------------------------------------------------------------------- fetch

def status_id(url):
    m = re.search(r"/status/(\d+)", url)
    return m.group(1) if m else None


def fetch(sid, use_cache=True):
    path = os.path.join(CACHE, f"{sid}.json")
    if use_cache and os.path.exists(path) and os.path.getsize(path) > 200:
        return json.load(open(path))
    for attempt in range(3):
        try:
            req = urllib.request.Request(API.format(sid), headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode())
            if data.get("code") != 200:
                return None
            os.makedirs(CACHE, exist_ok=True)
            json.dump(data["tweet"], open(path, "w"), ensure_ascii=False)
            return data["tweet"]
        except Exception as e:
            if attempt == 2:
                print(f"  ! {sid}: {e}", file=sys.stderr)
            time.sleep(1.5 * (attempt + 1))
    return None


# --------------------------------------------------------------------------- model gate

# MiniMax H3 ships as "Hailuo 3.0" / "Hailuo 03" in the consumer app, so the gate
# has to accept all of those spellings. What it must NOT accept is a bare
# "hailuo", which would sweep in the entire back catalogue of Hailuo 2.3 posts.
#
# The gap before the version number is a whitespace/dash class, never `.` — with
# `.` the string "hailuo2.3" matches `hailuo.{0,2}3\b`, and every 2.3 post leaks
# straight back in through the one hole this filter exists to close.
H3_EXPLICIT = re.compile(
    r"minimax.{0,4}h3"                          # MiniMax H3, MiniMax-H3, minimaxh3, MiniMax's H3
    r"|#minimaxh3"
    r"|hailuo[\s_\-]{0,2}(?:3\.0\b|03\b|3\b)",  # Hailuo 3.0 / Hailuo 03 / Hailuo 3 / Hailuo-3
    re.I)
# "MiniMax just shipped … H3 is unreal" — brand and version split across the post.
BRAND = re.compile(r"minimax|hailuo", re.I)
H3_LOOSE = re.compile(r"\bh3\b", re.I)
# Only used for labelling. A 2.3-only post never gets past is_h3().
V23 = re.compile(r"hailuo[\s_\-]{0,2}2[.·]3|minimax.{0,4}0?2[.·]3", re.I)


def is_h3(text):
    return bool(H3_EXPLICIT.search(text) or (BRAND.search(text) and H3_LOOSE.search(text)))


def detect_model(text):
    if is_h3(text):
        return "MiniMax H3"
    if V23.search(text):
        return "Hailuo 2.3"
    return "Hailuo"


# --------------------------------------------------------------------------- parse

# Ordered: the first rule that matches wins, so the specific ones come first.
CATEGORY_RULES = [
    ("Model Comparisons", r"\b(same prompt on|side by side|blind(ly)? (test|judged)|showdown|destroys|outperform|"
                          r"head[- ]to[- ]head)\b"
                          r"|\bvs\.?\b.*\b(seedance|sora|veo|kling|runway|wan|grok|imagine|firefly|pika)\b"
                          r"|\b(seedance|sora|veo|kling|runway|wan|grok|firefly|pika)\b.*\bvs\.?\b"),
    # No bare "coming soon" here: creators routinely sign off a hands-on test with
    # "more coming soon", which is not an announcement and used to swallow them.
    ("Launch & Announcements", r"\b(now live|is live|goes live|global launch|officially (announced|launched)|"
                               r"has been officially|just dropped|just announced|day[ -]0|"
                               r"open[- ]?sourc(e|ed|ing)|open[- ]weights?|now available (in|on)|"
                               r"available now (in|on)|partner nodes|weights are opening|"
                               r"expected to (launch|be released)|new feature|leaderboard|"
                               r"ranks? (top|#?\d)|#1 in|is (now )?(live |available )?on @)\b"),
    ("Music & Dance", r"\b(dance|dancing|k-?pop|choreograph|music video|\bmv\b|movement sheet|ballet|"
                      r"soundtrack|singing|sings)\b"),
    ("Anime & Animation", r"\b(anime|manga|sakuga|cartoon|2d animation|animated (scene|feature|film)|pixar|"
                          r"doodle|tom and jerry|stop-?motion)\b"),
    # H3's two headline features: many references in one call, and editing a clip in place.
    ("Omni-Reference & Editing", r"\b(omni[- ]?ref(erence)?|multi[- ]?(asset|reference)|motion transfer|"
                                 r"camera reference|character reference|reference (image|video|clip)s?|"
                                 r"first/?last frame|last frame|video editing|in[- ]place edit|"
                                 r"instruction[- ]based edit)\b"),
    ("Audio & Voice", r"\b(native (stereo )?audio|stereo (sound|audio)|lip[- ]?sync|voice (id|clone|ref|over)|"
                      r"dialogue|sound (design|effects?|fx)|foley|speech|vocals|audio track|with sound)\b"),
    ("Ads, UGC & Product", r"\b(ugc|advert|commercial|product (photo|shot|demo|video)|brand|dtc|unboxing|"
                           r"e-?commerce|tiktok shop|influencer vlog|\bads?\b)\b"),
    ("Action & VFX", r"\b(fight|fighting|action sequence|battle|combat|explosion|vfx|parkour|chase|escape|"
                     r"katana|war\b|supernova|summoning)\b"),
    ("Prompting & Workflow", r"\b(character (reference )?sheet|consistency|workflow|step[- ]by[- ]step|guide|"
                             r"cheat sheet|how to|technique|json prompt|depth (map|anything)|blender|comfyui|"
                             r"previs|storyboard|prompt (collection|library|structure)|\bapi\b|open ?router)\b"),
    ("Cinematic & Film", r"\b(cinematic|short film|film|movie|scene|one shot|slice-of-life|documentary|trailer)\b"),
]

# Marker forms seen in the wild: "Prompt:", "Prompt ⬇️", a bare "Prompt" line,
# "Here's the prompt 👇:", "提示词：" …
PROMPT_MARKERS = [
    r"here'?s the (?:exact )?prompt\s*[👇⬇️]*\s*[:：]?",
    r"(?:(?:minimax|hailuo|h3)[^\n]{0,24})?(?:production |video[_ ]|image |exact |base |example |"
    r"omni[- ]?ref(?:erence)? )?prompt(?:\s*structure)?\s*(?:used)?\s*[:：]",
    r"^\s*(?:(?:minimax|hailuo)\s*)?(?:h3\s*)?prompt\s*[⬇️👇]*\s*$",
    r"提示词\s*[:：]",
]
# "Prompt below", "prompt in first comment" — it exists, but it lives in a reply.
IN_THREAD = re.compile(
    r"\bprompts?\s*(?:is|are|in|below|👇|⬇️)?\s*"
    r"(?:below|in (?:the )?(?:first )?comments?|in (?:the )?(?:thread|replies?)|👇|⬇️)\b", re.I)


def extract_prompt(text):
    """Longest plausible prompt body following a prompt marker, else None."""
    best = None
    for pat in PROMPT_MARKERS:
        for m in re.finditer(pat, text, re.I | re.M):
            tail = text[m.end():].strip()
            tail = re.sub(r"\s*https://t\.co/\w+\s*$", "", tail).strip()
            if len(tail) < 80:
                continue
            if best is None or len(tail) > len(best):
                best = tail
    return best


def categorize(text):
    for name, pat in CATEGORY_RULES:
        if re.search(pat, text, re.I):
            return name
    return "Showcase"


def make_title(text):
    for line in text.split("\n"):
        line = re.sub(r"https?://\S+", "", line).strip()
        line = re.sub(r"\s+", " ", line).strip(" .:-—>")
        if len(line) >= 14:
            return (line[:92].rsplit(" ", 1)[0] + "…") if len(line) > 95 else line
    flat = re.sub(r"\s+", " ", re.sub(r"https?://\S+", "", text)).strip()
    return flat[:92] or "Untitled"


def build(tweet):
    """(post, None) on success, (None, reason) otherwise — so main() can report
    exactly why a URL didn't make it in instead of a single lumped count."""
    videos = (tweet.get("media") or {}).get("videos") or []
    text = (tweet.get("text") or "").strip()
    if not is_h3(text):
        return None, "not H3"
    if not videos:
        return None, "no video"
    v = max(videos, key=lambda x: (x.get("width") or 0) * (x.get("height") or 0))
    a = tweet["author"]
    dt = datetime.strptime(tweet["created_at"], "%a %b %d %H:%M:%S %z %Y").astimezone(timezone.utc)
    prompt = extract_prompt(text)
    return {
        "id": tweet["id"],
        "url": f"https://x.com/{a['screen_name']}/status/{tweet['id']}",
        "title": make_title(text),
        "text": text,
        "prompt": prompt,
        "prompt_in_thread": bool(not prompt and IN_THREAD.search(text)),
        "model": detect_model(text),
        "category": categorize(text),
        "date": dt.strftime("%Y-%m-%d"),
        "author": {
            "name": a["name"],
            "handle": a["screen_name"],
            "url": f"https://x.com/{a['screen_name']}",
            "avatar": a.get("avatar_url"),
        },
        "video": {
            "url": v["url"],
            "thumbnail": v.get("thumbnail_url"),
            "width": v.get("width"),
            "height": v.get("height"),
            "duration": round(v.get("duration") or 0, 2),
            # X publishes several encodes of the same clip; keeping them lets
            # scripts/mirror.py pick a sane one instead of re-encoding.
            "formats": [f for f in (v.get("formats") or []) if f.get("container") == "mp4"],
        },
        "stats": {
            "views": tweet.get("views") or 0,
            "likes": tweet.get("likes") or 0,
            "retweets": tweet.get("retweets") or 0,
        },
    }, None


# --------------------------------------------------------------------------- main

def main():
    use_cache = "--cache" in sys.argv
    urls = [l.strip() for l in open(URLS) if l.strip() and not l.startswith("#")]
    ids = list(dict.fromkeys(filter(None, (status_id(u) for u in urls))))
    print(f"hydrating {len(ids)} posts (cache={'on' if use_cache else 'off'})")

    with ThreadPoolExecutor(max_workers=6) as ex:
        tweets = list(ex.map(lambda s: fetch(s, use_cache), ids))

    posts, dropped = [], {"unfetchable": 0, "not H3": 0, "no video": 0, "duplicate clip": 0}
    for t in tweets:
        if not t:
            dropped["unfetchable"] += 1
            continue
        p, why = build(t)
        if p:
            posts.append(p)
        else:
            dropped[why] += 1
    posts.sort(key=lambda p: -p["stats"]["views"])

    # Two entries can carry the same underlying X media object — a repost that
    # the API flattens onto the reposter, for one. Keeping both would put one
    # creator's clip under somebody else's name, so the most-watched post keeps
    # it (that is nearly always where it came from) and the rest are dropped.
    seen_media, unique = set(), []
    for p in posts:
        m = re.search(r"/(?:amplify_video|ext_tw_video|tweet_video)/(\d+)/", p["video"]["url"])
        key = m.group(1) if m else p["id"]
        if key in seen_media:
            dropped["duplicate clip"] += 1
            continue
        seen_media.add(key)
        unique.append(p)
    posts = unique

    # Manual overrides fix titles/categories and preserve prompts copied
    # verbatim from author-posted replies. Reply prompts must include their
    # public X URLs so the generated gallery can cite the exact source.
    ov_path = os.path.join(ROOT, "scripts", "overrides.json")
    if os.path.exists(ov_path):
        ov = json.load(open(ov_path))
        for p in posts:
            patch = {k: v for k, v in ov.get(p["id"], {}).items()
                     if k in ("title", "category", "prompt", "prompt_source_urls", "prompt_in_thread")}
            if patch.get("prompt") and not patch.get("prompt_source_urls"):
                raise ValueError(f"{p['id']}: an overridden prompt needs prompt_source_urls")
            p.update(patch)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(posts, open(OUT, "w"), indent=2, ensure_ascii=False)
    with_prompt = sum(1 for p in posts if p["prompt"])
    in_thread = sum(1 for p in posts if p["prompt_in_thread"])
    print(f"wrote {len(posts)} posts to data/posts.json "
          f"({with_prompt} with a prompt, {in_thread} with an unindexed prompt in a reply)")
    detail = ", ".join(f"{n} {k}" for k, n in dropped.items() if n)
    print(f"dropped {sum(dropped.values())}" + (f": {detail}" if detail else ""))


if __name__ == "__main__":
    main()
