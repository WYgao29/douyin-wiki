from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import nh3
from markdown_it import MarkdownIt

from .catalog import LibraryCatalog

_MARKDOWN = MarkdownIt("commonmark", {"html": False, "linkify": True}).enable("table")
_ALLOWED_TAGS = {
    "a",
    "blockquote",
    "br",
    "code",
    "del",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "hr",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
}
_ALLOWED_ATTRIBUTES = {
    "a": {"href", "title", "target"},
    "img": {"src", "alt", "title", "loading"},
}


def _remove_private_lines(markdown: str) -> str:
    lines: list[str] = []
    cover_removed = False
    for line in markdown.splitlines():
        if "原始采集记录" in line or "machine_data_path" in line:
            continue
        if not cover_removed and re.match(r"^\s*!\[[^]]*(?:封面|缩略图|第\s*1\s*张)[^]]*]", line):
            cover_removed = True
            continue
        lines.append(line)
    return "\n".join(lines)


def _wikilinks(markdown: str, catalog: LibraryCatalog) -> str:
    by_source = {Path(item.source_path).stem: item for item in catalog.list_items()}

    def replace(match: re.Match[str]) -> str:
        target, label = (
            match.group(1).split("|", 1) if "|" in match.group(1) else (match.group(1), "")
        )
        name = Path(target).stem
        item = by_source.get(name)
        text = label or name
        return f"[{text}](/articles/{item.entry_id})" if item else text

    return re.sub(r"\[\[([^]]+)]]", replace, markdown)


def _rewrite_url(url: str, source_path: Path, catalog: LibraryCatalog) -> str:
    decoded = unquote(url.strip().strip("<>"))
    parsed = urlsplit(decoded)
    if parsed.scheme in {"http", "https", "mailto"} or decoded.startswith("#"):
        return decoded
    if parsed.scheme or decoded.startswith("/"):
        return "#"
    target = (source_path.parent / parsed.path).resolve()
    try:
        relative = target.relative_to(catalog.vault_path)
    except ValueError:
        return "#"
    if target.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif"}:
        safe = catalog.safe_media_path(str(relative))
        return catalog.media_url(str(relative)) if safe else "#"
    if target.suffix.lower() == ".md":
        for item in catalog.list_items():
            if Path(item.source_path) == relative:
                return f"/articles/{item.entry_id}"
        return "#"
    return "#"


def render_article(markdown: str, *, source_path: str, catalog: LibraryCatalog) -> str:
    value = _wikilinks(_remove_private_lines(markdown), catalog)
    value = re.sub(r"(?m)^>\s*\[![^]]+]\s*", "> **AI 推断** · ", value)
    source = (catalog.vault_path / source_path).resolve()

    def image(match: re.Match[str]) -> str:
        alt, url, title = match.group(1), match.group(2), match.group(3) or ""
        rewritten = _rewrite_url(url, source, catalog)
        suffix = f' "{title}"' if title else ""
        return f"![{alt}]({rewritten}{suffix})"

    def link(match: re.Match[str]) -> str:
        label, url, title = match.group(1), match.group(2), match.group(3) or ""
        rewritten = _rewrite_url(url, source, catalog)
        suffix = f' "{title}"' if title else ""
        return f"[{label}]({rewritten}{suffix})"

    value = re.sub(r"!\[([^]]*)]\(([^\s)]+)(?:\s+[\"']([^\"']*)[\"'])?\)", image, value)
    value = re.sub(r"(?<!!)\[([^]]+)]\(([^\s)]+)(?:\s+[\"']([^\"']*)[\"'])?\)", link, value)
    rendered = _MARKDOWN.render(value)
    return nh3.clean(
        rendered,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer",
    )


def render_chat(markdown: str) -> str:
    return nh3.clean(
        _MARKDOWN.render(markdown),
        tags=_ALLOWED_TAGS - {"img"},
        attributes={"a": _ALLOWED_ATTRIBUTES["a"]},
        url_schemes={"http", "https"},
        link_rel="noopener noreferrer",
    )
