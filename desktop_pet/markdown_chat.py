"""Lazy CommonMark parsing and a small native Tk renderer. No HTML is executed."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re
from typing import Any, Callable
from urllib.parse import urlsplit


class MarkdownUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class ImageReference:
    source: str
    alt: str
    start: int
    end: int
    label: str = ""


@lru_cache(maxsize=1)
def _parser():
    try:
        from markdown_it import MarkdownIt
        from markdown_it.rules_inline.image import image
    except ImportError:
        return None
    parser = MarkdownIt("commonmark", {"html": False, "store_labels": True})
    original_validator = parser.validateLink
    # File URLs are only interpreted by the explicit user attachment loader.
    # The renderer never opens these or any non-HTTP link.
    parser.validateLink = lambda url: url.casefold().startswith("file:") or original_validator(url)

    def tracked_image(state, silent):
        start = state.pos
        found = image(state, silent)
        if found and not silent:
            state.tokens[-1].meta["source_span"] = (start, state.pos)
        return found

    parser.inline.ruler.at("image", tracked_image)
    return parser


def parse_markdown(source: str, env: dict | None = None) -> list[Any] | None:
    parser = _parser()
    return parser.parse(source, env if env is not None else {}) if parser else None


def _inline_text(children) -> str:
    parts = []
    for token in children or ():
        if token.type in {"text", "code_inline", "html_inline"}:
            parts.append(token.content)
        elif token.type in {"softbreak", "hardbreak"}:
            parts.append("\n")
        elif token.type == "image":
            parts.append(_inline_text(token.children))
    return "".join(parts)


def visible_text(source: str) -> str:
    """Visible prose, including link labels, never link/image destinations."""
    tokens = parse_markdown(source)
    if tokens is None:
        return source
    return "\n".join(_inline_text(token.children) if token.type == "inline" else token.content.rstrip()
                     for token in tokens if token.type in {"inline", "fence", "code_block", "html_block"}).strip()


def image_references(source: str, *, env: dict | None = None) -> list[ImageReference]:
    """Find actual CommonMark images, excluding code and escaped image syntax.

    Inline-rule spans are mapped through block prefixes (quotes/list markers),
    allowing replacement with durable IDs without rewriting unrelated prose.
    """
    environment = env if env is not None else {}
    tokens = parse_markdown(source, environment)
    if tokens is None:
        if re.search(r"!\[", source):
            raise MarkdownUnavailable("圖片需要 markdown-it-py；請安裝 desktop_pet/requirements-chat.txt，草稿已保留。")
        return []
    lines = source.splitlines(keepends=True)
    offsets, offset = [], 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    references = []
    for block in tokens:
        if block.type != "inline" or not block.map:
            continue
        images = [token for token in block.children or () if token.type == "image"]
        if not images:
            continue
        positions: list[int] = []
        row = block.map[0]
        for fragment in block.content.splitlines(keepends=True):
            # CommonMark normalizes line endings. Source itself is normalized
            # by the composer/attachment store before this mapping.
            body = fragment.removesuffix("\n")
            found = -1
            while row < min(block.map[1], len(lines)):
                found = lines[row].find(body)
                if found >= 0:
                    break
                row += 1
            if found < 0:
                raise ValueError("無法定位這個圖片標記；請將圖片標記放在獨立一行，草稿已保留。")
            positions.extend(offsets[row] + found + index for index in range(len(body)))
            if fragment.endswith("\n"):
                positions.append(offsets[row] + len(lines[row]) - 1)
            row += 1
        for token in images:
            start, end = token.meta["source_span"]
            if end > len(positions) or start >= end:
                raise ValueError("無法定位這個圖片標記；草稿已保留。")
            references.append(ImageReference(token.attrGet("src") or "", _inline_text(token.children),
                                             positions[start], positions[end - 1] + 1,
                                             token.meta.get("label", "")))
    return references


class MarkdownRenderer:
    """Append formatted blocks into an existing Text; images are opt-in IDs only."""

    def __init__(self, widget, image_loader: Callable | None = None):
        self.widget, self.image_loader = widget, image_loader
        self._photos: dict[str, Any] = {}
        self._links = 0
        self.widget.tag_configure("md_code", background="#F0EBF5", font=("Consolas", 10), spacing1=4, spacing3=4)
        self.widget.tag_configure("md_quote", foreground="#83778E", lmargin1=24, lmargin2=24)
        self.widget.tag_configure("md_list", lmargin1=16, lmargin2=32)
        self.widget.tag_configure("md_link", foreground="#76529B", underline=True)
        for base in ("user", "reply", "subtitle", "notice"):
            family, size = ("Yu Gothic UI", 12) if base == "reply" else ("Microsoft JhengHei UI", 10)
            for style, attributes in (("bold", ("bold",)), ("italic", ("italic",)),
                                      ("bold_italic", ("bold", "italic"))):
                self.widget.tag_configure(f"md_{base}_{style}", font=(family, size, *attributes))
            for level in range(1, 7):
                self.widget.tag_configure(f"md_{base}_h{level}", font=(family, max(size, 19 - level), "bold"),
                                          spacing1=7, spacing3=4)

    def clear(self):
        self._photos.clear()
        for tag in self.widget.tag_names():
            if tag.startswith("md_url_"):
                self.widget.tag_delete(tag)

    def _insert(self, text, tags):
        self.widget.insert("end", text, tuple(tags))

    def _inline(self, children, tags, base):
        styles: list[str] = []
        links: list[str] = []
        for token in children or ():
            kind = token.type
            if kind in {"strong_open", "em_open"}:
                styles.append("bold" if kind == "strong_open" else "italic")
                continue
            if kind in {"strong_close", "em_close"}:
                style = "bold" if kind == "strong_close" else "italic"
                if style in styles:
                    styles.remove(style)
                continue
            if kind == "link_open":
                destination = token.attrGet("href") or ""
                tag = f"md_url_{self._links}"
                self._links += 1
                if urlsplit(destination).scheme.casefold() in {"https", "http"}:
                    self.widget.tag_bind(tag, "<Button-1>", lambda event, url=destination: self._open_link(url))
                    self.widget.tag_bind(tag, "<Enter>", lambda event: self.widget.configure(cursor="hand2"))
                    self.widget.tag_bind(tag, "<Leave>", lambda event: self.widget.configure(cursor="arrow"))
                links.append(tag)
                continue
            if kind == "link_close":
                if links:
                    links.pop()
                continue
            active = list(tags)
            if styles:
                style = "bold_italic" if set(styles) == {"bold", "italic"} else styles[-1]
                active.append(f"md_{base}_{style}")
            if links:
                active.extend(["md_link", links[-1]])
            if kind == "image":
                source = token.attrGet("src") or ""
                label = _inline_text(token.children) or "圖片"
                photo = None
                if self._allow_images and source.startswith("attachment:") and self.image_loader:
                    try:
                        photo = self._photos.get(source)
                        if photo is None:
                            photo = self.image_loader(source, self.widget)
                            self._photos[source] = photo
                    except Exception:
                        # A missing/corrupt historical thumbnail is a visible
                        # placeholder, never a model/image loading side effect.
                        pass
                if photo is not None:
                    self.widget.image_create("end", image=photo, padx=3, pady=6, align="center")
                    self._insert(f" {label}", active)
                else:
                    self._insert(f"〔圖片：{label}・未載入〕", active)
            elif kind in {"text", "html_inline"}:
                self._insert(token.content, active)
            elif kind == "code_inline":
                self._insert(token.content, [*active, "md_code"])
            elif kind in {"softbreak", "hardbreak"}:
                self._insert("\n", active)

    @staticmethod
    def _open_link(url):
        import webbrowser
        webbrowser.open(url)

    def append(self, source: str, base_tag: str = "user", *, allow_images=False):
        self._allow_images = allow_images
        tokens = parse_markdown(source)
        if tokens is None:
            self._insert(source + "\n", (base_tag,))
            return
        tags, lists = [base_tag], []
        for token in tokens:
            kind = token.type
            if kind == "heading_open":
                tags.append(f"md_{base_tag}_{token.tag}")
            elif kind == "blockquote_open":
                tags.append("md_quote")
            elif kind in {"bullet_list_open", "ordered_list_open"}:
                lists.append([kind, int(token.attrGet("start") or 1)])
                if "md_list" not in tags:
                    tags.append("md_list")
            elif kind == "list_item_open":
                entry = lists[-1]
                bullet = f"{entry[1]}. " if entry[0] == "ordered_list_open" else "• "
                self._insert("  " * (len(lists) - 1) + bullet, tags)
                entry[1] += 1
            elif kind in {"bullet_list_close", "ordered_list_close"}:
                lists.pop()
                if not lists and "md_list" in tags:
                    tags.remove("md_list")
            elif kind == "inline":
                self._inline(token.children, tags, base_tag)
            elif kind in {"fence", "code_block"}:
                self._insert(token.content.rstrip("\n") + "\n", [*tags, "md_code"])
            elif kind == "html_block":
                self._insert(token.content, tags)
            elif kind == "hr":
                self._insert("────────────\n", tags)
            elif kind == "heading_close":
                self._insert("\n", tags)
                tags.pop()
            elif kind == "paragraph_close":
                self._insert("\n", tags)
            elif kind == "blockquote_close":
                tags.remove("md_quote")
