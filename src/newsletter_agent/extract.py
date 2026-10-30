"""Main text and title from an HTML page, with html.parser."""

from __future__ import annotations

import re
from email.message import Message
from html.parser import HTMLParser

SKIP = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "nav",
        "header",
        "footer",
        "aside",
        "form",
        "button",
        "select",
        "iframe",
        "dialog",
    }
)
BLOCK = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "main",
        "li",
        "pre",
        "blockquote",
        "br",
        "tr",
        "td",
        "th",
        "table",
        "figcaption",
        "dd",
        "dt",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }
)
MAIN = frozenset({"article", "main"})
MIN_LINE = 25
MIN_MAIN = 500
_CHARSET = re.compile(rb"""<meta[^>]+charset=["']?([A-Za-z0-9_-]+)""", re.I)


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.main_depth = 0
        self.in_title = False
        self.title_done = False
        self.title_parts: list[str] = []
        self.og_title: str | None = None
        self.all_parts: list[str] = []
        self.main_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP:
            self.skip_depth += 1
        elif tag in MAIN:
            self.main_depth += 1
        elif tag == "title":
            self.in_title = not self.skip_depth and not self.title_done
        elif tag == "meta":
            values = dict(attrs)
            if values.get("property") == "og:title" and values.get("content"):
                self.og_title = values["content"]
        if tag in BLOCK:
            self._break()

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP:
            self.skip_depth = max(0, self.skip_depth - 1)
        elif tag in MAIN:
            self.main_depth = max(0, self.main_depth - 1)
        elif tag == "title" and self.in_title:
            self.in_title = False
            self.title_done = True
        if tag in BLOCK:
            self._break()

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
            return
        if self.skip_depth:
            return
        self.all_parts.append(data)
        if self.main_depth:
            self.main_parts.append(data)

    def _break(self) -> None:
        self.all_parts.append("\n")
        if self.main_depth:
            self.main_parts.append("\n")


def decode(body: bytes, headers: Message) -> str:
    """Decode with the header's charset, else a meta charset, else UTF-8. Never raises."""
    charset = headers.get_content_charset()
    if not charset:
        match = _CHARSET.search(body[:4096])
        charset = match.group(1).decode("ascii", errors="replace") if match else "utf-8"
    try:
        return body.decode(charset, errors="replace")
    except (LookupError, TypeError, ValueError):
        return body.decode("utf-8", errors="replace")


def _lines(parts: list[str]) -> str:
    lines = (" ".join(line.split()) for line in "".join(parts).split("\n"))
    return "\n".join(line for line in lines if len(line) >= MIN_LINE)


def extract(html: str) -> tuple[str | None, str | None]:
    """Return the page's main text and its title. The text prefers article or main elements and
    drops lines shorter than 25 characters.
    """
    parser = _Extractor()
    parser.feed(html)
    parser.close()
    main = _lines(parser.main_parts)
    text = main if len(main) >= MIN_MAIN else _lines(parser.all_parts)
    title = parser.og_title or " ".join("".join(parser.title_parts).split()) or None
    return (text or None), title
