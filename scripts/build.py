#!/usr/bin/env python3
"""Render data/posts.json into the GitHub Pages gallery and both READMEs."""
import json, os, re, html
from collections import Counter, OrderedDict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data", "posts.json")
DOCS = os.path.join(ROOT, "docs")

CATEGORY_ORDER = [
    "Showcase", "Cinematic & Film", "Anime & Animation", "Action & VFX",
    "Music & Dance", "Audio & Voice", "Omni-Reference & Editing",
    "Ads, UGC & Product", "Prompting & Workflow",
    "Model Comparisons", "Launch & Announcements",
]
CATEGORY_ZH = {
    "Showcase": "综合展示", "Cinematic & Film": "电影感短片", "Anime & Animation": "动画 / 动漫",
    "Action & VFX": "动作与特效", "Music & Dance": "音乐与舞蹈", "Audio & Voice": "原生音频与配音",
    "Omni-Reference & Editing": "多模态参考与视频编辑",
    "Ads, UGC & Product": "广告 / UGC / 产品", "Prompting & Workflow": "提示词与工作流",
    "Model Comparisons": "模型横评", "Launch & Announcements": "发布与官方消息",
}

# Where readers can run a prompt from this repo themselves. Sits at the top of
# both READMEs, above the gallery link.
TRY_URL = "https://maxluo3.com"
TRY_HOST = "maxluo3.com"


def load():
    posts = json.load(open(DATA))
    posts.sort(key=lambda p: -p["stats"]["views"])

    # scripts/mirror.py copies each clip to R2 and renders a short animated
    # preview. Playback prefers the mirror (twimg rotates its video URLs), and
    # the original stays on the record as a fallback and as provenance.
    mpath = os.path.join(ROOT, "data", "mirror.json")
    mirror = json.load(open(mpath)) if os.path.exists(mpath) else {}

    # Clips uploaded to GitHub via scripts/ingest_uploads.py. A bare
    # user-attachments URL is the one thing GitHub turns into a real player,
    # so where we have one the README embeds it instead of an animated still.
    apath = os.path.join(ROOT, "data", "attachments.json")
    attach = json.load(open(apath)) if os.path.exists(apath) else {}

    for p in posts:
        m = mirror.get(p["id"])
        p["video"]["source_url"] = p["video"]["url"]
        if p.get("prompt") and not p.get("prompt_source_urls"):
            p["prompt_source_urls"] = [p["url"]]
        if m:
            p["video"]["url"] = m["mp4"]
            p["video"]["preview"] = m["webp"]
        else:
            p["video"]["preview"] = None
        p["video"]["attachment"] = attach.get(p["id"])
    return posts


def by_category(posts):
    out = OrderedDict()
    for c in CATEGORY_ORDER:
        group = [p for p in posts if p["category"] == c]
        if group:
            out[c] = group
    for p in posts:                       # anything a new rule invented
        if p["category"] not in out:
            out[p["category"]] = [q for q in posts if q["category"] == p["category"]]
    return out


def human(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{n/1_000:.1f}K".replace(".0K", "K")
    return str(n)


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def clip(s, n):
    """Trim to n chars on a word boundary."""
    if len(s) <= n:
        return s
    return s[:n].rsplit(" ", 1)[0].rstrip(",;:—-") + "…"


def still(p):
    """What to show in a README. GitHub strips <video> but renders animated
    WebP through <img>, so the mirrored loop is the only way to show motion
    without leaving the page; the static thumbnail is the fallback."""
    return p["video"].get("preview") or p["video"]["thumbnail"]


def prompt_source_links(p, zh=False):
    """Markdown links to the exact post/replies from which prompt text came."""
    urls = p.get("prompt_source_urls") or [p["url"]]
    links = []
    for i, url in enumerate(urls, 1):
        if url == p["url"]:
            label = "X 原帖" if zh else "Original post"
        elif len(urls) == 1:
            label = "X 回复" if zh else "X reply"
        else:
            label = f"X 回复 {i}" if zh else f"X reply {i}"
        links.append(f"[{label}]({url})")
    return " · ".join(links)


# ============================================================== GitHub Pages site

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Awesome MiniMax H3 Prompts — every MiniMax H3 clip on X, in one place</title>
<meta name="description" content="A community index of MiniMax H3 (Hailuo 3.0) videos posted on X. Watch the clip, read the prompt, credit the creator.">
<meta property="og:title" content="Awesome MiniMax H3 Prompts">
<meta property="og:description" content="Watch every MiniMax H3 clip shared on X, with the prompt and full credit to the creator.">
<style>
*,*::before,*::after{box-sizing:border-box}
:root{
  --bg:#0b0a0d; --bg-soft:#151217; --card:#181419; --card-hover:#1f1a21;
  --line:#2d2530; --fg:#f2ecef; --fg-dim:#b09ea8; --fg-faint:#7d6b76;
  --accent:#ff6a4d; --accent-soft:#ff6a4d22; --accent-fg:#ffa48f;
  --radius:14px; --maxw:1400px;
}
@media (prefers-color-scheme: light){
  :root{--bg:#fdfbfa;--bg-soft:#f5f1ef;--card:#fff;--card-hover:#fff;--line:#ece3e0;
        --fg:#1d1418;--fg-dim:#6b5560;--fg-faint:#96808b;--accent:#e0432c;--accent-soft:#e0432c12;--accent-fg:#c4381f}
}
:root[data-theme="dark"]{--bg:#0b0a0d;--bg-soft:#151217;--card:#181419;--card-hover:#1f1a21;--line:#2d2530;
  --fg:#f2ecef;--fg-dim:#b09ea8;--fg-faint:#7d6b76;--accent:#ff6a4d;--accent-soft:#ff6a4d22;--accent-fg:#ffa48f}
:root[data-theme="light"]{--bg:#fdfbfa;--bg-soft:#f5f1ef;--card:#fff;--card-hover:#fff;--line:#ece3e0;
  --fg:#1d1418;--fg-dim:#6b5560;--fg-faint:#96808b;--accent:#e0432c;--accent-soft:#e0432c12;--accent-fg:#c4381f}

html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);
  font:15px/1.55 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,"Helvetica Neue",Arial,sans-serif;
  overflow-x:hidden}
a{color:inherit}
.wrap{max-width:var(--maxw);margin:0 auto;padding:0 20px}

/* ---------- header ---------- */
header{border-bottom:1px solid var(--line);background:
  radial-gradient(900px 380px at 12% -12%, var(--accent-soft), transparent 62%), var(--bg-soft)}
.head{padding:44px 0 34px}
h1{margin:0 0 10px;font-size:clamp(26px,4.4vw,42px);line-height:1.1;letter-spacing:-.022em;font-weight:700}
h1 .g{background:linear-gradient(96deg,var(--accent-fg),#ffb547 62%,#ff8fd0);
  -webkit-background-clip:text;background-clip:text;color:transparent}
.tagline{margin:0;color:var(--fg-dim);font-size:clamp(14px,1.7vw,17px);max-width:65ch}
.metrics{display:flex;flex-wrap:wrap;gap:10px;margin-top:20px}
.metric{background:var(--card);border:1px solid var(--line);border-radius:999px;
  padding:6px 14px;font-size:12.5px;color:var(--fg-dim)}
.metric b{color:var(--fg);font-variant-numeric:tabular-nums}
.headlinks{display:flex;flex-wrap:wrap;gap:8px;margin-top:20px}
.btn{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--line);background:var(--card);
  color:var(--fg);border-radius:9px;padding:8px 14px;font-size:13.5px;font-weight:500;
  text-decoration:none;cursor:pointer;transition:.15s}
.btn:hover{border-color:var(--accent);color:var(--accent-fg)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.primary:hover{filter:brightness(1.12);color:#fff}

/* ---------- controls ---------- */
.controls{position:sticky;top:0;z-index:30;background:color-mix(in srgb,var(--bg) 88%,transparent);
  backdrop-filter:blur(14px);border-bottom:1px solid var(--line);padding:12px 0}
.crow{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.search{flex:1 1 240px;min-width:180px;position:relative}
.search input{width:100%;background:var(--card);border:1px solid var(--line);color:var(--fg);
  border-radius:9px;padding:9px 12px 9px 34px;font-size:14px;font-family:inherit}
.search input:focus{outline:2px solid var(--accent);outline-offset:-1px;border-color:transparent}
.search svg{position:absolute;left:11px;top:50%;transform:translateY(-50%);opacity:.45;pointer-events:none}
select{background:var(--card);border:1px solid var(--line);color:var(--fg);border-radius:9px;
  padding:9px 11px;font-size:13.5px;font-family:inherit;cursor:pointer}
select:focus{outline:2px solid var(--accent);outline-offset:-1px}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chips[hidden]{display:none}
.chip{border:1px solid var(--line);background:var(--card);color:var(--fg-dim);border-radius:999px;
  padding:7px 14px;font-size:13px;cursor:pointer;font-family:inherit;transition:.15s;white-space:nowrap}
.chip:hover{color:var(--fg);border-color:var(--fg-faint)}
.chip[aria-pressed="true"]{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:500}
.count{color:var(--fg-faint);font-size:12.5px;margin-left:auto;white-space:nowrap;font-variant-numeric:tabular-nums}

/* ---------- grid ---------- */
main{padding:26px 0 70px}
.grid{display:grid;gap:18px;grid-template-columns:repeat(auto-fill,minmax(330px,1fr))}
@media(max-width:719px){.grid{grid-template-columns:1fr;gap:16px}}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  overflow:hidden;display:flex;flex-direction:column;transition:.18s}
.card:hover{border-color:var(--fg-faint);background:var(--card-hover)}

.player{position:relative;aspect-ratio:16/9;background:#000;overflow:hidden}
.player.tall{aspect-ratio:4/5}
.player img,.player video{width:100%;height:100%;object-fit:cover;display:block}
.player video{object-fit:contain;background:#000}
.play{position:absolute;inset:0;display:grid;place-items:center;border:0;background:transparent;
  cursor:pointer;padding:0}
.play::before{content:"";position:absolute;inset:0;
  background:linear-gradient(180deg,#0000 40%,#0009);opacity:.85;transition:.18s}
.play:hover::before{opacity:.6}
.play span{position:relative;width:54px;height:54px;border-radius:50%;display:grid;place-items:center;
  background:#fffffff0;box-shadow:0 4px 20px #0006;transition:.18s}
.play:hover span{transform:scale(1.09);background:#fff}
.play span svg{margin-left:3px}
.badges{position:absolute;left:9px;top:9px;display:flex;gap:5px;flex-wrap:wrap;pointer-events:none;z-index:2}
.badge{background:#000000b8;color:#fff;font-size:11px;font-weight:600;letter-spacing:.02em;
  padding:3px 8px;border-radius:6px;backdrop-filter:blur(4px)}
.badge.h3{background:#ff6a4de0}
.dur{position:absolute;right:9px;bottom:9px;background:#000000b8;color:#fff;font-size:11px;
  padding:3px 7px;border-radius:5px;font-variant-numeric:tabular-nums;pointer-events:none;z-index:2}

.body{padding:14px 15px 15px;display:flex;flex-direction:column;gap:11px;flex:1}
.title{margin:0;font-size:14.5px;line-height:1.42;font-weight:600;letter-spacing:-.005em}
.title a{text-decoration:none}
.title a:hover{color:var(--accent-fg);text-decoration:underline;text-underline-offset:2px}
.who{display:flex;align-items:center;gap:9px;min-width:0}
.who img{width:30px;height:30px;border-radius:50%;flex:none;background:var(--bg-soft)}
.who .nm{min-width:0;line-height:1.3}
.who .n{font-size:13px;font-weight:600;display:block;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;text-decoration:none}
.who .n:hover{color:var(--accent-fg)}
.who .h{font-size:11.5px;color:var(--fg-faint);text-decoration:none}
.who .h:hover{color:var(--accent-fg)}
.source,.prompt-source{font-size:11.5px;color:var(--fg-faint);line-height:1.5}
.source a,.prompt-source a{color:var(--accent-fg);text-decoration:none}
.source a:hover,.prompt-source a:hover{text-decoration:underline;text-underline-offset:2px}
.meta{display:flex;gap:12px;font-size:11.5px;color:var(--fg-faint);
  font-variant-numeric:tabular-nums;flex-wrap:wrap;margin-top:auto;padding-top:2px}
.meta span{display:inline-flex;align-items:center;gap:4px}

details.prompt{border:1px solid var(--line);border-radius:9px;background:var(--bg-soft)}
details.prompt summary{cursor:pointer;padding:8px 11px;font-size:12.5px;font-weight:600;
  color:var(--accent-fg);list-style:none;display:flex;align-items:center;gap:6px;user-select:none}
details.prompt summary::-webkit-details-marker{display:none}
details.prompt summary::before{content:"▸";font-size:10px;transition:.15s;display:inline-block}
details.prompt[open] summary::before{transform:rotate(90deg)}
.ptext{margin:0;padding:0 11px 11px;font:11.5px/1.62 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  color:var(--fg-dim);white-space:pre-wrap;word-break:break-word;max-height:280px;overflow:auto}
.prompt-source{padding:0 11px 9px}
.copy{margin:0 11px 11px;border:1px solid var(--line);background:var(--card);color:var(--fg-dim);
  border-radius:7px;padding:5px 11px;font-size:11.5px;cursor:pointer;font-family:inherit;transition:.15s}
.copy:hover{border-color:var(--accent);color:var(--accent-fg)}
.copy.done{border-color:#3ec98a;color:#3ec98a}
.thread{display:inline-flex;align-items:center;gap:5px;font-size:12px;color:var(--fg-faint);
  text-decoration:none;border:1px dashed var(--line);border-radius:8px;padding:7px 11px}
.thread:hover{color:var(--accent-fg);border-color:var(--accent)}

.empty{text-align:center;padding:80px 20px;color:var(--fg-faint)}
footer{border-top:1px solid var(--line);background:var(--bg-soft);padding:32px 0;
  color:var(--fg-faint);font-size:13px;line-height:1.7}
footer a{color:var(--accent-fg)}
footer p{margin:0 0 8px;max-width:78ch}
.totop{position:fixed;right:18px;bottom:18px;width:42px;height:42px;border-radius:50%;
  background:var(--accent);color:#fff;border:0;cursor:pointer;display:none;place-items:center;
  box-shadow:0 4px 18px #0005;z-index:40}
.totop.show{display:grid}
</style>
</head>
<body>

<header>
  <div class="wrap head">
    <h1>Awesome <span class="g">MiniMax H3</span> Prompts</h1>
    <p class="tagline">Every MiniMax H3 clip people are posting on X, collected in one place — play the video,
      read the exact prompt, and follow the cited source account and post.</p>
    <div class="metrics">
      <div class="metric"><b>__NPOSTS__</b> posts</div>
      <div class="metric"><b>__NCREATORS__</b> source accounts</div>
      <div class="metric"><b>__NPROMPTS__</b> full prompts</div>
      <div class="metric"><b>__NVIEWS__</b> views on X</div>
    </div>
    <div class="headlinks">
      <a class="btn primary" href="__REPO__">★ Star on GitHub</a>
      <a class="btn" href="__REPO__/blob/main/CONTRIBUTING.md">＋ Add a post</a>
      <a class="btn" href="__REPO__/blob/main/data/posts.json">JSON dataset</a>
      <button class="btn" id="theme" type="button">◐ Theme</button>
    </div>
  </div>
</header>

<div class="controls">
  <div class="wrap crow">
    <label class="search">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4">
        <circle cx="11" cy="11" r="7"/><path d="m20 20-3.2-3.2"/></svg>
      <input id="q" type="search" placeholder="Search prompts, creators, anything…" autocomplete="off">
    </label>
    <div class="chips" id="models"></div>
    <select id="cat"><option value="">All categories</option></select>
    <select id="sort">
      <option value="views">Most viewed</option>
      <option value="date">Newest first</option>
      <option value="prompt">Has a full prompt</option>
    </select>
    <span class="count" id="count"></span>
  </div>
</div>

<main class="wrap"><div class="grid" id="grid"></div><div class="empty" id="empty" hidden>
  No post matches that. Try a looser search.</div></main>

<footer><div class="wrap">
  <p><b>Every card credits the X source account and links to the original post and prompt source.</b>
     Source credit does not infer ownership beyond what the linked posts show. If you want an entry changed or
     removed, <a href="__REPO__/issues/new">open an issue</a> and it comes down, no questions asked.</p>
  <p>MiniMax H3 — also shipped as Hailuo 3.0 in the Hailuo AI app — is a video model by MiniMax.
     This is an unaffiliated, community-run index.
     Data refreshed __UPDATED__ · <a href="__REPO__">source on GitHub</a> · MIT licensed.</p>
</div></footer>

<button class="totop" id="totop" type="button" aria-label="Back to top">
  <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6">
    <path d="M12 19V5M5 12l7-7 7 7"/></svg></button>

<script id="data" type="application/json">__DATA__</script>
<script>
const POSTS = JSON.parse(document.getElementById('data').textContent);
const $ = s => document.querySelector(s);
const esc = s => (s||'').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const human = n => n >= 1e6 ? (n/1e6).toFixed(1).replace(/\\.0$/,'')+'M'
                 : n >= 1e3 ? (n/1e3).toFixed(1).replace(/\\.0$/,'')+'K' : ''+n;
const dur = s => { s = Math.round(s||0); return s ? Math.floor(s/60)+':'+String(s%60).padStart(2,'0') : ''; };

/* ---- filter state ---- */
const state = { q:'', model:'', cat:'', sort:'views' };

const MODELS = [...new Set(POSTS.map(p => p.model))]
  .sort((a,b) => POSTS.filter(p=>p.model===b).length - POSTS.filter(p=>p.model===a).length);
/* One model in the index means the chips would be a row of one — hide them. */
if (MODELS.length < 2) {
  $('#models').hidden = true;
} else {
  $('#models').innerHTML = [['','All models'], ...MODELS.map(m=>[m,m])]
    .map(([v,l]) => `<button class="chip" data-m="${esc(v)}" aria-pressed="${v===''}">${esc(l)}</button>`).join('');
  $('#models').addEventListener('click', e => {
    const b = e.target.closest('[data-m]'); if (!b) return;
    state.model = b.dataset.m;
    [...$('#models').children].forEach(c => c.setAttribute('aria-pressed', c === b));
    render();
  });
}

const CATS = [...new Set(POSTS.map(p => p.category))].sort();
$('#cat').insertAdjacentHTML('beforeend',
  CATS.map(c => `<option value="${esc(c)}">${esc(c)} (${POSTS.filter(p=>p.category===c).length})</option>`).join(''));

$('#q').addEventListener('input', e => { state.q = e.target.value.toLowerCase().trim(); render(); });
$('#cat').addEventListener('change', e => { state.cat = e.target.value; render(); });
$('#sort').addEventListener('change', e => { state.sort = e.target.value; render(); });

/* ---- card ---- */
function card(p) {
  const tall = p.video.height > p.video.width;
  const isH3 = p.model === 'MiniMax H3';
  const promptLinks = (p.prompt_source_urls || []).map((url, i, all) => {
    const label = url === p.url ? 'Original post' : (all.length > 1 ? 'X reply ' + (i + 1) : 'X reply');
    return `<a href="${esc(url)}" target="_blank" rel="noopener">${label}</a>`;
  }).join(' · ');
  const promptBlock = p.prompt
    ? `<details class="prompt"><summary>Prompt</summary>
         <pre class="ptext">${esc(p.prompt)}</pre>
         <div class="prompt-source"><b>Prompt credit / source:</b>
           <a href="${esc(p.author.url)}" target="_blank" rel="noopener">@${esc(p.author.handle)}</a>
           · ${promptLinks}</div>
         <button class="copy" type="button" data-id="${p.id}">Copy prompt</button></details>`
    : p.prompt_in_thread
      ? `<a class="thread" href="${esc(p.url)}" target="_blank" rel="noopener">Prompt mentioned in the X thread ↗</a>`
      : '';
  return `<article class="card" data-id="${p.id}">
    <div class="player${tall ? ' tall' : ''}">
      <img src="${esc(p.video.thumbnail)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer">
      <div class="badges"><span class="badge${isH3 ? ' h3' : ''}">${esc(p.model)}</span></div>
      ${p.video.duration ? `<span class="dur">${dur(p.video.duration)}</span>` : ''}
      <button class="play" type="button" aria-label="Play video">
        <span><svg width="19" height="19" viewBox="0 0 24 24" fill="#111"><path d="M7 4.5v15l13-7.5z"/></svg></span>
      </button>
    </div>
    <div class="body">
      <h2 class="title"><a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.title)}</a></h2>
      <div class="who">
        <img src="${esc(p.author.avatar||'')}" alt="" loading="lazy" referrerpolicy="no-referrer">
        <div class="nm">
          <a class="n" href="${esc(p.author.url)}" target="_blank" rel="noopener">${esc(p.author.name)}</a>
          <a class="h" href="${esc(p.author.url)}" target="_blank" rel="noopener">@${esc(p.author.handle)}</a>
        </div>
      </div>
      <div class="source"><b>Video credit / source:</b>
        <a href="${esc(p.author.url)}" target="_blank" rel="noopener">@${esc(p.author.handle)}</a>
        · <a href="${esc(p.url)}" target="_blank" rel="noopener">Original post ↗</a></div>
      ${promptBlock}
      <div class="meta">
        <span>${esc(p.date)}</span>
        <span>${human(p.stats.views)} views</span>
        <span>${human(p.stats.likes)} likes</span>
      </div>
    </div>
  </article>`;
}

function render() {
  let list = POSTS.filter(p => {
    if (state.model && p.model !== state.model) return false;
    if (state.cat && p.category !== state.cat) return false;
    if (state.q) {
      const hay = (p.title + ' ' + p.text + ' ' + (p.prompt||'') + ' ' +
                   p.author.name + ' ' + p.author.handle + ' ' + p.category).toLowerCase();
      if (!state.q.split(/\\s+/).every(w => hay.includes(w))) return false;
    }
    return true;
  });
  if (state.sort === 'date')        list.sort((a,b) => b.date.localeCompare(a.date));
  else if (state.sort === 'prompt') list.sort((a,b) => (b.prompt?1:0)-(a.prompt?1:0) || b.stats.views-a.stats.views);
  else                              list.sort((a,b) => b.stats.views - a.stats.views);

  $('#grid').innerHTML = list.map(card).join('');
  $('#count').textContent = `${list.length} of ${POSTS.length}`;
  $('#empty').hidden = list.length > 0;
}

/* ---- play on demand: nothing but posters loads up front ---- */
$('#grid').addEventListener('click', e => {
  const play = e.target.closest('.play');
  if (play) {
    const art = play.closest('.card');
    const p = POSTS.find(x => x.id === art.dataset.id);
    const box = play.closest('.player');
    box.innerHTML = `<video src="${esc(p.video.url)}" poster="${esc(p.video.thumbnail)}"
        controls autoplay playsinline preload="auto" referrerpolicy="no-referrer"></video>`;
    const vid = box.querySelector('video');
    // Mirror first; if that ever fails, fall back to X's own copy, then to the post.
    let triedSource = false;
    vid.addEventListener('error', () => {
      if (!triedSource && p.video.source_url && p.video.source_url !== p.video.url) {
        triedSource = true; vid.src = p.video.source_url; vid.load(); vid.play().catch(()=>{});
        return;
      }
      box.innerHTML = `<div style="display:grid;place-items:center;height:100%;padding:20px;
        text-align:center;font-size:13px;color:#b09ea8">This clip can no longer stream here.<br>
        <a href="${esc(p.url)}" target="_blank" rel="noopener" style="color:#ffa48f">Watch it on X ↗</a></div>`;
    });
    // one video at a time
    document.querySelectorAll('video').forEach(v => { if (v !== vid) v.pause(); });
    return;
  }
  const copy = e.target.closest('.copy');
  if (copy) {
    const p = POSTS.find(x => x.id === copy.dataset.id);
    navigator.clipboard.writeText(p.prompt).then(() => {
      copy.textContent = '✓ Copied'; copy.classList.add('done');
      setTimeout(() => { copy.textContent = 'Copy prompt'; copy.classList.remove('done'); }, 1600);
    });
  }
});

/* ---- theme + back to top ---- */
$('#theme').addEventListener('click', () => {
  const cur = document.documentElement.dataset.theme ||
    (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  document.documentElement.dataset.theme = cur === 'dark' ? 'light' : 'dark';
});
const totop = $('#totop');
addEventListener('scroll', () => totop.classList.toggle('show', scrollY > 900), {passive:true});
totop.addEventListener('click', () => scrollTo({top:0, behavior:'smooth'}));

render();
</script>
</body>
</html>
"""


def build_site(posts, repo, updated):
    slim = [{
        "id": p["id"], "url": p["url"], "title": p["title"], "text": p["text"],
        "prompt": p["prompt"], "prompt_in_thread": p["prompt_in_thread"],
        "prompt_source_urls": p.get("prompt_source_urls", []),
        "model": p["model"], "category": p["category"], "date": p["date"],
        "author": p["author"], "stats": p["stats"],
        "video": {k: v for k, v in p["video"].items() if k != "formats"},
    } for p in posts]
    page = (PAGE
            .replace("__DATA__", json.dumps(slim, ensure_ascii=False).replace("</", "<\\/"))
            .replace("__NPOSTS__", str(len(posts)))
            .replace("__NCREATORS__", str(len({p["author"]["handle"] for p in posts})))
            .replace("__NPROMPTS__", str(sum(1 for p in posts if p["prompt"])))
            .replace("__NVIEWS__", human(sum(p["stats"]["views"] for p in posts)))
            .replace("__UPDATED__", updated)
            .replace("__REPO__", repo))
    os.makedirs(DOCS, exist_ok=True)
    open(os.path.join(DOCS, "index.html"), "w").write(page)
    json.dump(slim, open(os.path.join(DOCS, "posts.json"), "w"), indent=2, ensure_ascii=False)
    open(os.path.join(DOCS, ".nojekyll"), "w").write("")


# ============================================================== READMEs

def readme_en(posts, repo, site, updated):
    groups = by_category(posts)
    nprompt = sum(1 for p in posts if p["prompt"])
    L = []
    L.append("# Awesome MiniMax H3 Prompts\n")
    L.append("**Every MiniMax H3 clip people are posting on X, collected in one place — "
             "play the video, read the exact prompt, and follow the cited source account and post.**\n")
    L.append(f"[![Try it yourself](https://img.shields.io/badge/✨%20Try%20it%20yourself-"
             f"{TRY_HOST.replace('-', '--')}-C6F24E?style=flat-square&labelColor=0A0B0A)]({TRY_URL})\n"
             "[![Watch the gallery](https://img.shields.io/badge/▶%20Watch%20the%20gallery-"
             f"opensource--works.github.io-FF6A4D?style=flat-square)]({site})\n"
             "[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg?style=flat-square)](CONTRIBUTING.md)\n"
             "[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](LICENSE)\n")
    L.append("**English** | [简体中文](README.zh-CN.md)\n")
    L.append(f"### ✨ Want to try it yourself? Generate with Hailuo 3 at **[{TRY_HOST}]({TRY_URL})**\n")
    L.append(f"Copy any prompt from this repo, paste it into [{TRY_HOST}]({TRY_URL}), and get your own clip back "
             "in a couple of minutes — no install, no waitlist, free credits to start.\n")
    L.append(f"### ▶ [Open the video gallery]({site})\n")
    L.append("MiniMax H3 — the model the Hailuo AI app ships as **Hailuo 3.0** and the API exposes as "
             "`MiniMax-H3` — landed on 31 July 2026. This index tracks what people are actually making "
             "with it. Only H3 posts are collected here; Hailuo 2.3 and earlier are out of scope.\n")
    L.append("Open the gallery if you want to search across prompts, filter by category, "
             "or copy a prompt in one click.\n")

    L.append("## What's in here\n")
    L.append(f"| | |\n|---|---|\n"
             f"| Posts | **{len(posts)}** |\n"
             f"| Source accounts credited | **{len({p['author']['handle'] for p in posts})}** |\n"
             f"| Posts with the full prompt | **{nprompt}** |\n"
             f"| Combined views on X | **{human(sum(p['stats']['views'] for p in posts))}** |\n"
             f"| Models covered | {', '.join(f'**{m}** ({n})' for m, n in Counter(p['model'] for p in posts).most_common())} |\n"
             f"| Last refreshed | {updated} |\n")

    L.append("## Most watched\n")
    L.append("<table><tr>")
    for i, p in enumerate(posts[:6]):
        if i and i % 3 == 0:
            L.append("</tr><tr>")
        L.append(f'<td width="33%" valign="top"><a href="{p["url"]}">'
                 f'<img src="{still(p)}" width="100%" alt=""></a><br>'
                 f'<sub><b>{html.escape(clip(p["title"], 62))}</b><br>'
                 f'<a href="{p["author"]["url"]}">@{p["author"]["handle"]}</a> · '
                 f'{human(p["stats"]["views"])} views</sub></td>')
    L.append("</tr></table>\n")

    L.append("## Contents\n")
    for c, g in groups.items():
        L.append(f"- [{c}](#{slug(c)}) — {len(g)} posts")
    L.append("")

    for c, g in groups.items():
        L.append(f"## {c}\n")
        for p in g:
            L.append(f"### {p['title']}\n")
            if p["video"].get("attachment"):
                # Must sit bare on its own line — wrapping it in <video> or a
                # link makes GitHub render it as text instead of a player.
                L.append(f'{p["video"]["attachment"]}\n')
            else:
                L.append(f'<a href="{p["url"]}"><img src="{still(p)}" '
                         f'width="460" alt="{html.escape(p["title"])}"></a>\n')
            L.append(f"**Video credit / source:** [{p['author']['name']}]({p['author']['url']}) · "
                     f"[@{p['author']['handle']}]({p['author']['url']}) · [Original post]({p['url']}) · "
                     f"{p['model']} · {p['date']} · {human(p['stats']['views'])} views\n")
            if p["prompt"]:
                L.append("<details><summary><b>Prompt</b></summary>\n")
                L.append("```text")
                L.append(p["prompt"])
                L.append("```\n")
                L.append("</details>\n")
                L.append(f"**Prompt credit / source:** [@{p['author']['handle']}]({p['author']['url']}) · "
                         f"{prompt_source_links(p)}\n")
            elif p["prompt_in_thread"]:
                L.append(f"> A prompt is mentioned in the [X thread]({p['url']}); "
                         "the exact reply has not been indexed yet.\n")
        L.append("")

    L.append("## Credit and takedowns\n")
    L.append("Every entry credits the X source account and links to the original post and exact prompt source. "
             "Source credit does not infer ownership beyond what the linked posts show.\n")
    L.append("If you are a creator and want your post edited or removed, "
             f"[open an issue]({repo}/issues/new) and it comes down, no questions asked.\n")
    L.append("## Adding a post\n")
    L.append("Drop the X link into `scripts/urls.txt` and open a PR — the pipeline fetches the rest. "
             "See [CONTRIBUTING.md](CONTRIBUTING.md).\n")
    L.append("## How the data is built\n")
    L.append("```bash\npython3 scripts/harvest.py   # X links -> data/posts.json\n"
             "python3 scripts/build.py     # data/posts.json -> docs/ + READMEs\n```\n")
    L.append("MiniMax H3 is a video model by MiniMax. This is an unaffiliated, community-run index. "
             "Repo content is MIT licensed; the linked posts and videos remain the property of their authors.\n")
    return "\n".join(L)


def readme_zh(posts, repo, site, updated):
    groups = by_category(posts)
    nprompt = sum(1 for p in posts if p["prompt"])
    L = []
    L.append("# Awesome MiniMax H3 Prompts\n")
    L.append("**把 X 上大家发的 MiniMax H3 视频集中到一个入口——直接播放、看到完整提示词、"
             "并且一键找到有明确引用的来源账号和原帖。**\n")
    L.append(f"[![自己试试](https://img.shields.io/badge/✨%20自己试一试-"
             f"{TRY_HOST.replace('-', '--')}-C6F24E?style=flat-square&labelColor=0A0B0A)]({TRY_URL})\n"
             f"[![观看画廊](https://img.shields.io/badge/▶%20打开视频画廊-opensource--works.github.io-FF6A4D?style=flat-square)]({site})\n"
             "[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg?style=flat-square)](CONTRIBUTING.md)\n"
             "[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](LICENSE)\n")
    L.append("[English](README.md) | **简体中文**\n")
    L.append(f"### ✨ 想亲手试试？上 **[{TRY_HOST}]({TRY_URL})** 直接用 Hailuo 3 生成\n")
    L.append(f"把这里任意一条提示词复制到 [{TRY_HOST}]({TRY_URL})，几分钟就能拿到你自己的片子——"
             "免安装、免排队，注册即送免费额度。\n")
    L.append(f"### ▶ [打开视频画廊]({site})\n")
    L.append("MiniMax H3 于 2026 年 7 月 31 日发布——海螺 AI 客户端里叫 **Hailuo 3.0**，"
             "API 的 model id 是 `MiniMax-H3`。这个索引只收 H3 的帖子，"
             "Hailuo 2.3 及更早版本不在收录范围内。\n")
    L.append("想跨提示词搜索、按分类筛选、或者一键复制提示词，去画廊。\n")

    L.append("## 收录概况\n")
    L.append(f"| | |\n|---|---|\n"
             f"| 帖子数 | **{len(posts)}** |\n"
             f"| 署名来源账号 | **{len({p['author']['handle'] for p in posts})}** |\n"
             f"| 含完整提示词 | **{nprompt}** |\n"
             f"| X 上累计播放 | **{human(sum(p['stats']['views'] for p in posts))}** |\n"
             f"| 覆盖模型 | {', '.join(f'**{m}**（{n}）' for m, n in Counter(p['model'] for p in posts).most_common())} |\n"
             f"| 最近更新 | {updated} |\n")

    L.append("## 目录\n")
    for c, g in groups.items():
        L.append(f"- [{CATEGORY_ZH.get(c, c)}](#{slug(c)}) — {len(g)} 条")
    L.append("")

    for c, g in groups.items():
        L.append(f"## {CATEGORY_ZH.get(c, c)}\n")
        L.append(f'<a id="{slug(c)}"></a>\n')
        for p in g:
            L.append(f"### {p['title']}\n")
            if p["video"].get("attachment"):
                # Must sit bare on its own line — wrapping it in <video> or a
                # link makes GitHub render it as text instead of a player.
                L.append(f'{p["video"]["attachment"]}\n')
            else:
                L.append(f'<a href="{p["url"]}"><img src="{still(p)}" '
                         f'width="460" alt="{html.escape(p["title"])}"></a>\n')
            L.append(f"**视频署名 / 来源：** [{p['author']['name']}]({p['author']['url']}) · "
                     f"[@{p['author']['handle']}]({p['author']['url']}) · [X 原帖]({p['url']}) · "
                     f"{p['model']} · {p['date']} · {human(p['stats']['views'])} 播放\n")
            if p["prompt"]:
                L.append("<details><summary><b>提示词</b></summary>\n")
                L.append("```text")
                L.append(p["prompt"])
                L.append("```\n")
                L.append("</details>\n")
                L.append(f"**提示词署名 / 来源：** [@{p['author']['handle']}]({p['author']['url']}) · "
                         f"{prompt_source_links(p, zh=True)}\n")
            elif p["prompt_in_thread"]:
                L.append(f"> [X 帖子串]({p['url']})提到了提示词，但尚未索引到准确回复。\n")
        L.append("")

    L.append("## 署名与下架\n")
    L.append("每条内容都会标注 X 来源账号，并链接到视频原帖和提示词的准确来源。"
             "来源署名不额外推断链接帖子未说明的原创归属。\n")
    L.append(f"如果你是作者，希望修改或删除自己的内容，[提一个 issue]({repo}/issues/new) 即可，我们立刻下架。\n")
    L.append("## 投稿\n")
    L.append("把 X 链接加进 `scripts/urls.txt` 提 PR 即可，剩下的交给脚本。详见 [CONTRIBUTING.md](CONTRIBUTING.md)。\n")
    L.append("MiniMax H3 是 MiniMax 的视频模型，本仓库为非官方社区索引。"
             "仓库代码与整理内容以 MIT 协议开源；被索引的帖子和视频版权归原作者所有。\n")
    return "\n".join(L)


def main():
    import subprocess
    posts = load()
    repo = "https://github.com/opensource-works/awesome-minimax-h3-prompts"
    site = "https://opensource-works.github.io/awesome-minimax-h3-prompts/"
    updated = subprocess.run(["date", "-u", "+%Y-%m-%d"], capture_output=True, text=True).stdout.strip()

    build_site(posts, repo, updated)
    open(os.path.join(ROOT, "README.md"), "w").write(readme_en(posts, repo, site, updated))
    open(os.path.join(ROOT, "README.zh-CN.md"), "w").write(readme_zh(posts, repo, site, updated))
    print(f"built docs/index.html + README.md + README.zh-CN.md from {len(posts)} posts")


if __name__ == "__main__":
    main()
