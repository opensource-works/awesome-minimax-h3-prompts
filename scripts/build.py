#!/usr/bin/env python3
"""Build all public artifacts from the authoritative catalog-v2 graph.

The renderer is deliberately collection-agnostic: branding and scope live in
``config/collection.json``.  It never reads the retired mirror manifests and it
never uses wall-clock time, so identical catalog input produces identical
output bytes.
"""
from __future__ import annotations

import copy
import html
import json
import re
import sys
from collections import Counter, OrderedDict
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from catalog import (  # noqa: E402
    export_posts, mirror_has_integrity, mirror_is_authorized, public_catalog,
)

CATALOG_PATH = ROOT / "data/catalog.json"
CONFIG_PATH = ROOT / "config/collection.json"
DOCS = ROOT / "docs"
GITHUB_ATTACHMENT_RE = re.compile(
    r"https://github\.com/user-attachments/assets/"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def read_json(path: Path):
    return json.loads(path.read_text())


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)


def human(value, *, zh=False) -> str:
    if value is None:
        return "—"
    if value >= 1_000_000:
        suffix = "万" if zh else "M"
        number = value / (10_000 if zh else 1_000_000)
        return f"{number:.1f}{suffix}".replace(".0" + suffix, suffix)
    if value >= 1_000:
        suffix = "千" if zh else "K"
        number = value / 1_000
        return f"{number:.1f}{suffix}".replace(".0" + suffix, suffix)
    return str(value)


def md_escape(value) -> str:
    return str(value or "").replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def code_fence(value: str) -> str:
    longest = max((len(match) for match in re.findall(r"`+", value)), default=0)
    return "`" * max(3, longest + 1)


def platform_label(platform: str, *, zh=False) -> str:
    labels = {"x": "X", "reddit": "Reddit"}
    return labels.get(platform, platform or ("未知平台" if zh else "Unknown platform"))


def actor_view(actor: dict | None) -> dict:
    actor = actor or {}
    handle = actor.get("handle")
    name = actor.get("display_name") or handle or "Unknown"
    return {
        "name": name,
        "handle": handle,
        "url": actor.get("profile_url"),
    }


def role_text(role: dict | None, *, zh=False) -> str:
    role = role or {}
    status = role.get("status") or "unknown"
    person = role.get("person")
    if not person:
        return "未知（未根据发帖账号推定）" if zh else "Unknown (not inferred from the poster)"
    label = person.get("name") or person.get("handle") or ("未知" if zh else "Unknown")
    handle = person.get("handle")
    if handle and handle.lower() != str(label).lower():
        label += f" (@{handle})"
    if person.get("url"):
        label = f"[{md_escape(label)}]({person['url']})"
    status_labels = {
        "confirmed": "已确认" if zh else "confirmed",
        "claimed": "原帖声明" if zh else "claimed",
        "inferred": "推断" if zh else "inferred",
        "disputed": "有争议" if zh else "disputed",
        "unknown": "未知" if zh else "unknown",
    }
    return f"{label} — {status_labels.get(status, status)}"


def poster_text(post: dict) -> str:
    poster = (post.get("roles") or {}).get("poster") or post.get("author") or {}
    name = poster.get("name") or poster.get("handle") or "Unknown"
    handle = poster.get("handle")
    label = name + (f" (@{handle})" if handle and handle.lower() != str(name).lower() else "")
    return f"[{md_escape(label)}]({poster['url']})" if poster.get("url") else md_escape(label)


def video_credit_text(post: dict, *, zh=False) -> str:
    """Prefer a verified creator; otherwise credit the source account explicitly."""
    creator = (post.get("roles") or {}).get("original_video_creator") or {}
    if creator.get("person"):
        return role_text(creator, zh=zh)
    qualifier = "来源账号；不据此推定原始创作者" if zh else "source account; original creator not inferred"
    return f"{poster_text(post)} — {qualifier}"


def prompt_credit_text(post: dict, *, zh=False) -> str:
    """Give a useful prompt citation without turning a source into authorship."""
    author = (post.get("roles") or {}).get("prompt_author") or {}
    if author.get("person"):
        return role_text(author, zh=zh)
    if post.get("prompt") or post.get("prompt_in_thread") or post.get("prompt_source_url"):
        qualifier = "提示词来源账号；作者身份未核验" if zh else "prompt source account; authorship unverified"
        return f"{poster_text(post)} — {qualifier}"
    return "未提供" if zh else "Not provided"


def readme_github_attachment(post: dict, catalog: dict) -> str | None:
    """Return a bare, playable GitHub URL only through the existing rights gate."""
    video = post.get("video") or {}
    url = video.get("attachment")
    if (
        video.get("media_mode") != "authorized_mirror"
        or not GITHUB_ATTACHMENT_RE.fullmatch(str(url or ""))
    ):
        return None
    item = (catalog.get("items") or {}).get(post.get("item_id")) or {}
    evidence = catalog.get("evidence") or {}
    rights = ((item.get("rights") or {}).get("video_republication") or {})
    maintainer_attested = rights.get("grant_verification") == "maintainer_attestation"
    for media in item.get("media") or []:
        for mirror in (media.get("delivery") or {}).get("mirrors") or []:
            permission_ids = mirror.get("permission_evidence_ids") or []
            if (
                mirror.get("url") == url
                and mirror.get("provider") == "github_attachment"
                and mirror.get("artifact") == "video"
                and mirror.get("state") == "active"
                and mirror_has_integrity(mirror)
                and (permission_ids or maintainer_attested)
                and all(
                    (evidence.get(evidence_id) or {}).get("visibility") == "public"
                    for evidence_id in permission_ids
                )
                and mirror_is_authorized(item, mirror, catalog)
            ):
                return url
    return None


def readme_full_prompt(post: dict, catalog: dict) -> str | None:
    """Expose only an explicitly captured, verbatim full prompt in the README."""
    item = (catalog.get("items") or {}).get(post.get("item_id")) or {}
    prompt = item.get("prompt") or {}
    text = prompt.get("text")
    if (
        prompt.get("status") != "verbatim"
        or prompt.get("is_verbatim") is not True
        or not isinstance(text, str)
        or not text.strip()
        or post.get("prompt") != text
    ):
        return None
    return text


def readme_prompt_is_incomplete(post: dict, catalog: dict) -> bool:
    item = (catalog.get("items") or {}).get(post.get("item_id")) or {}
    status = ((item.get("prompt") or {}).get("status"))
    return status in {"partial", "referenced_not_captured"} or bool(
        post.get("prompt_in_thread")
    )


def ordered_groups(posts: list[dict], config: dict):
    groups = OrderedDict()
    configured = config.get("categories") or []
    for category in configured:
        values = [post for post in posts if post.get("category") == category]
        if values:
            groups[category] = values
    extras = sorted({post.get("category") or "Uncategorized" for post in posts} - set(groups))
    for category in extras:
        groups[category] = [post for post in posts if (post.get("category") or "Uncategorized") == category]
    return groups


def candidate_counts(catalog: dict) -> Counter:
    return Counter(
        (candidate.get("review") or {}).get("state", "pending")
        for candidate in (catalog.get("candidates") or {}).values()
    )


def strip_public_avatars(public: dict, posts: list[dict]) -> None:
    """Profile images are not required for attribution and are volatile media."""
    for actor in (public.get("actors") or {}).values():
        actor["avatar_url"] = None
    for post in posts:
        if post.get("author"):
            post["author"]["avatar"] = None
        for role in (post.get("roles") or {}).values():
            if isinstance(role, dict) and isinstance(role.get("person"), dict):
                role["person"]["avatar"] = None


def enrich_posts(posts: list[dict], catalog: dict) -> list[dict]:
    actors = catalog.get("actors") or {}
    sources = catalog.get("sources") or {}
    evidence = catalog.get("evidence") or {}
    output = copy.deepcopy(posts)
    for post in output:
        item = (catalog.get("items") or {}).get(post.get("item_id")) or {}
        views = []
        for annotation in item.get("annotations") or []:
            source = sources.get(annotation.get("source_id")) or {}
            actor = actors.get(annotation.get("author_actor_id")) or {}
            evidence_url = None
            for evidence_id in annotation.get("evidence_ids") or []:
                record = evidence.get(evidence_id) or {}
                if record.get("visibility") == "public" and record.get("url"):
                    evidence_url = record["url"]
                    break
            source_is_public = (source.get("availability") or {}).get("state") == "available"
            views.append({
                "kind": annotation.get("kind"),
                "text": annotation.get("text") or "",
                "author": actor_view(actor),
                "source_url": source.get("url") if source_is_public else evidence_url,
                "created_at": annotation.get("created_at"),
            })
        post["annotation_views"] = views
    return output


def known_unsafe_urls(catalog: dict) -> set[str]:
    """URLs observed in source media or mirrors without an active grant."""
    unsafe = set()
    for source in (catalog.get("sources") or {}).values():
        for observation in source.get("media_observations") or []:
            for key in ("direct_url", "thumbnail_url"):
                if observation.get(key):
                    unsafe.add(observation[key])
            unsafe.update(value.get("url") for value in observation.get("variants") or [] if value.get("url"))
    for item in (catalog.get("items") or {}).values():
        for media in item.get("media") or []:
            for mirror in (media.get("delivery") or {}).get("mirrors") or []:
                if mirror.get("url") and not (
                    mirror.get("state") == "active" and mirror_is_authorized(item, mirror, catalog)
                ):
                    unsafe.add(mirror["url"])
    return unsafe


def assert_public_safe(catalog: dict, public: dict, public_posts: list[dict], artifacts: dict[str, str]) -> None:
    authorized_urls = set()
    for item in (catalog.get("items") or {}).values():
        for media in item.get("media") or []:
            for mirror in (media.get("delivery") or {}).get("mirrors") or []:
                if mirror.get("state") == "active" and mirror_is_authorized(item, mirror, catalog):
                    authorized_urls.add(mirror.get("url"))

    for source_id, source in (public.get("sources") or {}).items():
        for observation in source.get("media_observations") or []:
            if observation.get("direct_url") or observation.get("thumbnail_url") or observation.get("variants"):
                raise RuntimeError(f"public projection retained volatile media for {source_id}")
    for item_id, item in (public.get("items") or {}).items():
        for media in item.get("media") or []:
            for mirror in (media.get("delivery") or {}).get("mirrors") or []:
                if mirror.get("url") not in authorized_urls:
                    raise RuntimeError(f"public projection retained unauthorized mirror for {item_id}")
                missing = set(mirror.get("permission_evidence_ids") or []) - set(public.get("evidence") or {})
                if missing:
                    raise RuntimeError(
                        f"public mirror for {item_id} lacks public permission evidence: {sorted(missing)}"
                    )
    for post in public_posts:
        video = post.get("video") or {}
        if video.get("source_url") is not None or video.get("formats"):
            raise RuntimeError(f"public v1 post {post.get('entry_id')} retained source media")
        for key in ("url", "thumbnail", "attachment"):
            if video.get(key) and video[key] not in authorized_urls:
                raise RuntimeError(f"public v1 post {post.get('entry_id')} retained unauthorized {key}")

    unsafe = known_unsafe_urls(catalog)
    payloads = {
        "docs/catalog.json": json_text(public),
        "docs/posts.json": json_text(public_posts),
        **artifacts,
    }
    leaked = [(name, url) for name, text in payloads.items() for url in unsafe if url in text]
    if leaked:
        name, url = leaked[0]
        raise RuntimeError(f"{name} leaked retired or source media URL: {url}")


def coverage_summary(catalog: dict, posts: list[dict]) -> dict:
    counts = candidate_counts(catalog)
    platforms = Counter(post.get("platform") for post in posts)
    models = Counter(post.get("model") or "Unknown" for post in posts)
    prompts = sum(1 for post in posts if post.get("prompt"))
    referenced = sum(1 for post in posts if post.get("prompt_in_thread"))
    source_only = sum(1 for post in posts if (post.get("video") or {}).get("media_mode") != "authorized_mirror")
    return {
        "indexed": len(posts),
        "candidates": sum(counts.values()),
        "pending": counts["pending"],
        "excluded": counts["excluded"],
        "removed": counts["removed"],
        "prompts": prompts,
        "referenced_prompts": referenced,
        "source_only": source_only,
        "authorized_media": len(posts) - source_only,
        "platforms": dict(sorted(platforms.items())),
        "models": dict(models.most_common()),
    }


def readme(catalog: dict, posts: list[dict], config: dict, *, zh=False) -> str:
    repo = config["repo_url"]
    site = config["site_url"]
    title = config.get("title_zh" if zh else "title") or config["id"]
    summary = coverage_summary(catalog, posts)
    window = (catalog.get("collection") or {}).get("historical_window") or {}
    updated = catalog.get("updated_at") or ""
    groups = ordered_groups(posts, config)
    lines = [f"# {title}\n"]
    if zh:
        lines.append("**一个覆盖 X 与 Reddit、来源可核验的视频提示词索引。每条记录都链接原帖，并明确区分发帖者、原始视频创作者和提示词作者。**\n")
        lines.append(f"[打开视频索引]({site}) · [投稿指南](CONTRIBUTING.md) · [权利与授权](RIGHTS.md) · [下架流程](TAKEDOWN.md) · [覆盖率报告](COVERAGE.md)\n")
        lines.append("[English](README.md) | **简体中文**\n")
        lines.append("## 覆盖范围与状态\n")
        lines.append(
            "“全量”只指既定查询矩阵、时间窗口和公开可检索内容，不包括私密、已删除或平台搜索不可见内容。"
            f"当前历史窗口为 **{window.get('from', '—')} 至 {window.get('through', '—')}**，数据时间为 **{updated}**。\n"
        )
        lines.extend(["| 指标 | 数量 |", "|---|---:|"])
        labels = [
            ("公开收录", "indexed"), ("发现候选", "candidates"), ("待人工审核", "pending"),
            ("已排除", "excluded"), ("已移除", "removed"), ("含提示词正文", "prompts"),
            ("仅指出回复中有提示词", "referenced_prompts"), ("仅链接/官方嵌入", "source_only"),
            ("具有授权媒体", "authorized_media"),
        ]
    else:
        lines.append("**A source-verifiable video-prompt index spanning X and Reddit. Every entry links to its source and keeps the poster, original video creator, and prompt author as separate roles.**\n")
        lines.append(f"[Open the gallery]({site}) · [Contribute](CONTRIBUTING.md) · [Rights](RIGHTS.md) · [Takedowns](TAKEDOWN.md) · [Coverage report](COVERAGE.md)\n")
        lines.append("**English** | [简体中文](README.zh-CN.md)\n")
        lines.append("## Coverage and status\n")
        lines.append(
            "“Complete” means publicly discoverable within the documented query matrix and date window; it does not include private, deleted, or search-invisible material. "
            f"The current historical window is **{window.get('from', '—')} through {window.get('through', '—')}**, with catalog timestamp **{updated}**.\n"
        )
        lines.extend(["| Metric | Count |", "|---|---:|"])
        labels = [
            ("Public entries", "indexed"), ("Discovered candidates", "candidates"),
            ("Pending human review", "pending"), ("Excluded", "excluded"), ("Removed", "removed"),
            ("Prompt text captured", "prompts"), ("Prompt referenced but not captured", "referenced_prompts"),
            ("Source link / official embed only", "source_only"), ("Authorized media available", "authorized_media"),
        ]
    for label, key in labels:
        lines.append(f"| {label} | **{summary[key]}** |")
    lines.append("")

    if zh:
        lines.extend([
            "## 署名如何阅读\n",
            "- **发帖者**：发布这条 X/Reddit 帖子的账号。这不自动意味着其创作了视频。",
            "- **原始视频创作者**：只有证据支持时才标注；否则诚实显示为未知。",
            "- **提示词作者**：与发帖者、视频创作者独立记录；转载提示词不会改变作者身份。",
            "- **网友注释**链接具体评论及评论者；**仓库编辑注释**则标明编辑身份和审核依据。\n",
            "## 权利与播放\n",
            "未取得明确再发布许可的内容只提供原帖链接或平台允许的官方嵌入。只有授权范围明确包含下载和对应镜像方式时，索引才会提供媒体副本。详见 [RIGHTS.md](RIGHTS.md)；权利人可依照 [TAKEDOWN.md](TAKEDOWN.md) 申请修正或下架。\n",
        ])
    else:
        lines.extend([
            "## Reading the attribution\n",
            "- **Poster** is the account that published the X/Reddit source. It is not automatically the video creator.",
            "- **Original video creator** is named only when evidence supports the claim; otherwise it remains explicitly unknown.",
            "- **Prompt author** is tracked independently from both poster and video creator.",
            "- **Community annotations** link to the specific commenter and comment; **editorial annotations** identify repository review provenance.\n",
            "## Rights and playback\n",
            "Without explicit republication permission, an entry uses only its source link or a platform-permitted official embed. A media copy is exposed only when the recorded grant covers downloading and that delivery provider. See [RIGHTS.md](RIGHTS.md); rights holders can request correction or removal through [TAKEDOWN.md](TAKEDOWN.md).\n",
        ])

    lines.append("## " + ("条目" if zh else "Entries") + "\n")
    for category, values in groups.items():
        lines.append(f"### {category}\n")
        for post in values:
            lines.append(f"#### [{md_escape(post.get('title') or 'Untitled')}]({post['url']})\n")
            source_label = platform_label(post.get("platform"), zh=zh)
            model = md_escape(post.get("model") or ("未知模型" if zh else "Unknown model"))
            date = post.get("date") or "—"
            attachment = readme_github_attachment(post, catalog)
            if attachment:
                # A bare user-attachments URL is the legacy GitHub README player.
                lines.append(attachment + "\n")
            elif zh:
                lines.append("> **视频：** 仅提供原帖链接；此处没有通过公开权利校验的媒体副本。\n")
            else:
                lines.append("> **Video:** Source link only; no media copy passed the public rights gate.\n")
            if zh:
                lines.append(f"- **视频署名 / 来源（Video credit）：** {video_credit_text(post, zh=True)}")
                lines.append(f"- **提示词署名 / 来源（Prompt credit）：** {prompt_credit_text(post, zh=True)}")
                lines.append(
                    f"- **原帖（Original post）：** [{source_label}]({post['url']}) · "
                    f"{poster_text(post)} · {model} · {date}"
                )
            else:
                lines.append(f"- **Video credit / source:** {video_credit_text(post)}")
                lines.append(f"- **Prompt credit / source:** {prompt_credit_text(post)}")
                lines.append(
                    f"- **Original post:** [{source_label}]({post['url']}) · "
                    f"{poster_text(post)} · {model} · {date}"
                )
            prompt_urls = post.get("prompt_source_urls") or (
                [post["prompt_source_url"]] if post.get("prompt_source_url") else []
            )
            if prompt_urls:
                label = "提示词出处" if zh else "Prompt source"
                links = " · ".join(
                    f"[{source_label} {index}]({url})" if len(prompt_urls) > 1
                    else f"[{source_label}]({url})"
                    for index, url in enumerate(prompt_urls, 1)
                )
                lines.append(f"- **{label}:** {links}")
            lines.append("")
            full_prompt = readme_full_prompt(post, catalog)
            if full_prompt:
                fence = code_fence(full_prompt)
                lines.append("<details><summary><b>" + ("提示词" if zh else "Prompt") + "</b></summary>\n")
                lines.extend([f"{fence}text", full_prompt, fence, "", "</details>\n"])
            elif readme_prompt_is_incomplete(post, catalog):
                prompt_url = post.get("prompt_source_url") or post["url"]
                if zh:
                    message = "尚未捕获完整提示词，因此这里不会转载或推断提示词正文。"
                    link = "查看原始出处"
                else:
                    message = "The full prompt was not captured, so no prompt text is reproduced or inferred."
                    link = "Check the original source"
                lines.append(f"> {message} [{link}]({prompt_url}).\n")
            for annotation in post.get("annotation_views") or []:
                kind = annotation.get("kind")
                kind_label = ("网友注释" if kind == "community_comment" else "仓库编辑注释") if zh else ("Community annotation" if kind == "community_comment" else "Editorial annotation")
                author = annotation.get("author") or {}
                author_label = md_escape(author.get("name") or author.get("handle") or ("未知" if zh else "Unknown"))
                if author.get("url"):
                    author_label = f"[{author_label}]({author['url']})"
                if annotation.get("source_url"):
                    author_label = f"[{kind_label} · {author_label}]({annotation['source_url']})"
                else:
                    author_label = f"{kind_label} · {author_label}"
                quoted = str(annotation.get("text") or "").replace("\n", "\n> ")
                lines.append(f"> **{author_label}:** {quoted}\n")
        lines.append("")

    if zh:
        lines.extend([
            "## 投稿与维护\n",
            "新候选会先进入发现与人工审核队列，而不是自动公开或镜像。查询范围、排除理由和缺口见 [COVERAGE.md](COVERAGE.md)，投稿格式见 [CONTRIBUTING.md](CONTRIBUTING.md)。\n",
            "仓库代码和编辑性文字采用 MIT 协议；被索引的帖子、提示词和视频保留其各自权利状态。\n",
        ])
    else:
        lines.extend([
            "## Contributing and maintenance\n",
            "New candidates enter discovery and human review before publication or mirroring. Query scope, exclusions, and known gaps are documented in [COVERAGE.md](COVERAGE.md); submission requirements are in [CONTRIBUTING.md](CONTRIBUTING.md).\n",
            "Repository code and editorial text are MIT licensed. Indexed posts, prompts, and videos retain their own recorded rights status.\n",
        ])
    return "\n".join(lines).rstrip() + "\n"


PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>__TITLE__ — source-verifiable video prompt index</title>
<meta name="description" content="A source-verifiable X and Reddit video-prompt index with explicit attribution and rights-aware media delivery.">
<style>
*{box-sizing:border-box} :root{--accent:__ACCENT__;--bg:#0b0d12;--surface:#131722;--card:#171c28;--line:#293142;--text:#eef2fb;--muted:#9ca8bc;--soft:#76839a;--max:1440px}
@media(prefers-color-scheme:light){:root{--bg:#f7f8fb;--surface:#fff;--card:#fff;--line:#dce1e9;--text:#18202e;--muted:#566276;--soft:#78859a}}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}a{color:inherit}.wrap{width:min(var(--max),calc(100% - 36px));margin:auto}
header{background:radial-gradient(900px 420px at 12% -20%,color-mix(in srgb,var(--accent) 28%,transparent),transparent 70%),var(--surface);border-bottom:1px solid var(--line)}.hero{padding:54px 0 38px}.eyebrow{color:var(--accent);font-size:12px;font-weight:750;letter-spacing:.12em;text-transform:uppercase}h1{margin:8px 0 12px;font-size:clamp(30px,5vw,54px);line-height:1.04;letter-spacing:-.035em}.lead{max-width:820px;margin:0;color:var(--muted);font-size:clamp(15px,2vw,18px)}
.metrics{display:flex;flex-wrap:wrap;gap:9px;margin:24px 0 0}.metric{padding:7px 12px;border:1px solid var(--line);border-radius:999px;background:var(--card);color:var(--muted);font-size:12px}.metric b{color:var(--text)}.links{display:flex;flex-wrap:wrap;gap:9px;margin-top:20px}.btn{padding:9px 13px;border:1px solid var(--line);border-radius:9px;text-decoration:none;background:var(--card);font-size:13px}.btn.primary{background:var(--accent);border-color:var(--accent);color:white}
.notice{margin:24px 0 0;padding:13px 15px;border-left:3px solid var(--accent);background:color-mix(in srgb,var(--accent) 9%,var(--card));color:var(--muted);max-width:940px}.notice b{color:var(--text)}
.controls{position:sticky;top:0;z-index:10;padding:12px 0;border-bottom:1px solid var(--line);background:color-mix(in srgb,var(--bg) 90%,transparent);backdrop-filter:blur(14px)}.row{display:grid;grid-template-columns:minmax(220px,1fr) repeat(3,minmax(130px,auto));gap:9px}.control{width:100%;padding:10px 11px;border:1px solid var(--line);border-radius:9px;background:var(--surface);color:var(--text);font:inherit;font-size:13px}@media(max-width:760px){.row{grid-template-columns:1fr 1fr}.search{grid-column:1/-1}}
main{padding:28px 0 70px}.resultline{margin:0 0 15px;color:var(--soft);font-size:13px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:17px}@media(max-width:420px){.grid{grid-template-columns:1fr}.wrap{width:min(100% - 24px,var(--max))}}
.card{display:flex;flex-direction:column;min-width:0;border:1px solid var(--line);border-radius:15px;overflow:hidden;background:var(--card)}.media{aspect-ratio:16/8.7;background:linear-gradient(145deg,color-mix(in srgb,var(--accent) 20%,#111827),#090b10);display:grid;place-items:center;position:relative}.media video{width:100%;height:100%;object-fit:contain;background:#000}.sourcebox{text-align:center;padding:25px}.sourcebox strong{display:block;font-size:16px}.sourcebox span{display:block;color:#c4ccda;font-size:12px;margin:6px 0 14px}.sourcebtn{display:inline-block;background:#fff;color:#111827;text-decoration:none;padding:8px 12px;border-radius:8px;font-size:12px;font-weight:700}.badges{position:absolute;top:10px;left:10px;display:flex;gap:6px}.badge{padding:3px 7px;border-radius:6px;background:#000b;color:#fff;font-size:10px;font-weight:700}
.body{display:flex;flex-direction:column;gap:12px;padding:15px;flex:1}.title{margin:0;font-size:16px;line-height:1.38}.title a{text-decoration:none}.title a:hover{color:var(--accent)}.roles{display:grid;gap:6px}.role{display:grid;grid-template-columns:132px 1fr;gap:8px;font-size:12px}.role dt{color:var(--soft)}.role dd{margin:0;color:var(--muted);min-width:0;overflow-wrap:anywhere}.role a{color:var(--text)}
.prompt,.annotations{border-top:1px solid var(--line);padding-top:11px}.prompt summary{cursor:pointer;color:var(--accent);font-size:12px;font-weight:700}.prompt pre{white-space:pre-wrap;overflow:auto;max-height:300px;color:var(--muted);font:11.5px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace}.sourcehint{font-size:11px;color:var(--soft);margin-top:7px}.sourcehint a{color:var(--accent)}.annotation{margin-top:8px;padding:9px 10px;border-radius:8px;background:var(--surface);font-size:12px;color:var(--muted)}.annotation b{color:var(--text)}.annotation.editorial{border-left:2px solid var(--accent)}
.meta{display:flex;gap:10px;flex-wrap:wrap;margin-top:auto;color:var(--soft);font-size:11px}.empty{text-align:center;padding:70px 20px;color:var(--soft)}footer{border-top:1px solid var(--line);background:var(--surface);padding:30px 0;color:var(--muted);font-size:12px}footer p{max-width:920px}footer a{color:var(--accent)}
</style>
</head>
<body>
<header><div class="wrap hero"><div class="eyebrow">X + Reddit · evidence-aware catalog</div><h1>__TITLE__</h1><p class="lead">A source-verifiable video-prompt index that separates who posted, who created the video, and who authored the prompt.</p><div class="metrics" id="metrics"></div><div class="links"><a class="btn primary" href="__REPO__">GitHub repository</a><a class="btn" href="__REPO__/blob/main/CONTRIBUTING.md">Contribute</a><a class="btn" href="__REPO__/blob/main/COVERAGE.md">Coverage</a><a class="btn" href="__REPO__/blob/main/RIGHTS.md">Rights</a></div><div class="notice"><b>Rights-aware delivery:</b> unlicensed videos are not copied here. Their cards link to the original X or Reddit post; only media backed by an explicit recorded grant can play from a mirror.</div></div></header>
<div class="controls"><div class="wrap row"><input class="control search" id="search" type="search" placeholder="Search titles, prompts, posters, annotations…"><select class="control" id="platform"><option value="">All platforms</option></select><select class="control" id="model"><option value="">All models</option></select><select class="control" id="category"><option value="">All categories</option></select></div></div>
<main class="wrap"><p class="resultline" id="resultline"></p><div class="grid" id="grid"></div><div class="empty" id="empty" hidden>No entry matches these filters.</div></main>
<footer><div class="wrap"><p>The poster is not assumed to be the original creator or prompt author. Unknown roles remain explicitly unknown. Community annotations link to their commenters; editorial annotations carry repository provenance.</p><p>Coverage is bounded by the documented query matrix and public search visibility. See <a href="__REPO__/blob/main/COVERAGE.md">coverage</a>, <a href="__REPO__/blob/main/RIGHTS.md">rights</a>, and <a href="__REPO__/blob/main/TAKEDOWN.md">takedowns</a>. Catalog timestamp: __UPDATED__.</p></div></footer>
<script id="posts" type="application/json">__POSTS__</script><script id="summary" type="application/json">__SUMMARY__</script>
<script>
const POSTS=JSON.parse(document.querySelector('#posts').textContent),SUMMARY=JSON.parse(document.querySelector('#summary').textContent);const $=s=>document.querySelector(s);const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const metric=v=>v==null?'—':v>=1e6?(v/1e6).toFixed(1).replace(/\.0$/,'')+'M':v>=1e3?(v/1e3).toFixed(1).replace(/\.0$/,'')+'K':String(v);const pLabel=p=>p==='x'?'X':p==='reddit'?'Reddit':p;
$('#metrics').innerHTML=`<span class="metric"><b>${SUMMARY.indexed}</b> indexed</span><span class="metric"><b>${SUMMARY.candidates}</b> candidates</span><span class="metric"><b>${SUMMARY.pending}</b> pending review</span><span class="metric"><b>${SUMMARY.prompts}</b> prompt texts</span><span class="metric"><b>${Object.keys(SUMMARY.platforms).map(pLabel).join(' + ')}</b> sources</span>`;
function fill(id,values){$(id).insertAdjacentHTML('beforeend',[...new Set(values.filter(Boolean))].sort().map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join(''))}fill('#platform',POSTS.map(p=>p.platform));fill('#model',POSTS.map(p=>p.model));fill('#category',POSTS.map(p=>p.category));
function person(person){if(!person)return 'Unknown (not inferred from poster)';const name=person.name||person.handle||'Unknown',handle=person.handle&&person.handle.toLowerCase()!==name.toLowerCase()?` (@${esc(person.handle)})`:'';return person.url?`<a href="${esc(person.url)}" target="_blank" rel="noopener">${esc(name)}${handle}</a>`:`${esc(name)}${handle}`}
function role(value){if(!value||!value.person)return 'Unknown (not inferred from poster)';return `${person(value.person)} <span>· ${esc(value.status||'unknown')}</span>`}
function annotations(post){if(!post.annotation_views?.length)return '';return `<div class="annotations">${post.annotation_views.map(a=>{const community=a.kind==='community_comment',label=community?'Community annotation':'Editorial annotation',author=a.author||{},who=author.url?`<a href="${esc(author.url)}" target="_blank" rel="noopener">${esc(author.name||author.handle||'Unknown')}</a>`:esc(author.name||author.handle||'Unknown'),head=a.source_url?`<a href="${esc(a.source_url)}" target="_blank" rel="noopener">${label}</a>`:label;return `<div class="annotation ${community?'community':'editorial'}"><b>${head} · ${who}</b><br>${esc(a.text)}</div>`}).join('')}</div>`}
function media(post){const video=post.video||{},url=video.url||video.attachment;if(url){const poster=video.thumbnail?` poster="${esc(video.thumbnail)}"`:'';return `<div class="media"><video src="${esc(url)}"${poster} controls playsinline preload="metadata"></video><div class="badges"><span class="badge">Authorized mirror</span><span class="badge">${esc(pLabel(post.platform))}</span></div></div>`}return `<div class="media"><div class="badges"><span class="badge">Source link only</span><span class="badge">${esc(pLabel(post.platform))}</span></div><div class="sourcebox"><strong>No unlicensed media copy</strong><span>Watch this entry at its original source.</span><a class="sourcebtn" href="${esc(post.url)}" target="_blank" rel="noopener">Open on ${esc(pLabel(post.platform))} ↗</a></div></div>`}
function prompt(post){if(post.prompt){const urls=post.prompt_source_urls?.length?post.prompt_source_urls:(post.prompt_source_url?[post.prompt_source_url]:[]),source=urls.length?`<div class="sourcehint">Prompt source${urls.length>1?'s':''}: ${urls.map((url,i)=>`<a href="${esc(url)}" target="_blank" rel="noopener">${urls.length>1?`reply ${i+1}`:'original post or comment'} ↗</a>`).join(' · ')}</div>`:'';return `<details class="prompt"><summary>Prompt text</summary><pre>${esc(post.prompt)}</pre>${source}</details>`}if(post.prompt_in_thread)return `<div class="sourcehint">Prompt referenced in a reply, but the exact reply is not yet captured. ${post.prompt_source_url?`<a href="${esc(post.prompt_source_url)}" target="_blank" rel="noopener">Source ↗</a>`:''}</div>`;return ''}
function card(post){const poster=post.roles?.poster||post.author||{};return `<article class="card">${media(post)}<div class="body"><h2 class="title"><a href="${esc(post.url)}" target="_blank" rel="noopener">${esc(post.title)}</a></h2><dl class="roles"><div class="role"><dt>Video/source credit</dt><dd>${person(poster)}</dd></div><div class="role"><dt>Original video creator</dt><dd>${role(post.roles?.original_video_creator)}</dd></div><div class="role"><dt>Prompt author</dt><dd>${role(post.roles?.prompt_author)}</dd></div></dl>${prompt(post)}${annotations(post)}<div class="meta"><span>${esc(post.model||'Unknown model')}</span><span>${esc(post.date||'—')}</span><span>${metric(post.stats?.views)} views</span><span><a href="${esc(post.url)}" target="_blank" rel="noopener">Source ↗</a></span></div></div></article>`}
function render(){const q=$('#search').value.toLowerCase().trim(),platform=$('#platform').value,model=$('#model').value,category=$('#category').value;const list=POSTS.filter(p=>{if(platform&&p.platform!==platform||model&&p.model!==model||category&&p.category!==category)return false;const notes=(p.annotation_views||[]).map(a=>a.text+' '+(a.author?.name||'')).join(' '),roles=[p.roles?.poster,p.roles?.original_video_creator?.person,p.roles?.prompt_author?.person].filter(Boolean).map(r=>(r.name||'')+' '+(r.handle||'')).join(' '),hay=[p.title,p.text,p.prompt,p.model,p.category,roles,notes].join(' ').toLowerCase();return !q||q.split(/\s+/).every(word=>hay.includes(word))});$('#grid').innerHTML=list.map(card).join('');$('#resultline').textContent=`${list.length} of ${POSTS.length} public entries`;$('#empty').hidden=!!list.length}['#search','#platform','#model','#category'].forEach(id=>$(id).addEventListener(id==='#search'?'input':'change',render));render();
</script>
</body></html>
'''


def build_page(posts: list[dict], catalog: dict, config: dict, summary: dict) -> str:
    embedded_posts = json.dumps(posts, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    embedded_summary = json.dumps(summary, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return (PAGE
            .replace("__TITLE__", html.escape(config.get("title") or config["id"]))
            .replace("__ACCENT__", config.get("accent") or "#6d5dfc")
            .replace("__REPO__", html.escape(config["repo_url"], quote=True))
            .replace("__UPDATED__", html.escape(catalog.get("updated_at") or ""))
            .replace("__POSTS__", embedded_posts)
            .replace("__SUMMARY__", embedded_summary))


def main() -> None:
    catalog = read_json(CATALOG_PATH)
    config = read_json(CONFIG_PATH)
    posts = export_posts(catalog)
    public = public_catalog(catalog)
    # Generate every public compatibility/rendering view from the already
    # filtered graph.  Starting from the canonical export can retain the text
    # or IDs of annotations whose only evidence is private, even when the
    # canonical graph itself is correctly projected.
    public_posts = export_posts(public)
    strip_public_avatars(public, public_posts)
    view_posts = enrich_posts(public_posts, public)
    summary = coverage_summary(catalog, public_posts)

    page = build_page(view_posts, catalog, config, summary)
    readme_en = readme(catalog, view_posts, config, zh=False)
    readme_zh = readme(catalog, view_posts, config, zh=True)
    artifacts = {
        "docs/index.html": page,
        "README.md": readme_en,
        "README.zh-CN.md": readme_zh,
    }
    assert_public_safe(catalog, public, public_posts, artifacts)

    write_text(ROOT / "data/posts.json", json_text(posts))
    write_text(DOCS / "posts.json", json_text(public_posts))
    write_text(DOCS / "catalog.json", json_text(public))
    write_text(DOCS / "index.html", page)
    write_text(ROOT / "README.md", readme_en)
    write_text(ROOT / "README.zh-CN.md", readme_zh)
    print(
        f"built {len(public_posts)} public entries from catalog {catalog.get('updated_at')} "
        f"({summary['source_only']} source-link/embed only, {summary['authorized_media']} authorized media)"
    )


if __name__ == "__main__":
    main()
