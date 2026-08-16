#!/usr/bin/env python3
"""
sync.py - pull posts into this Jekyll blog.  Standard library only (Python 3.8+).

Sources
  1. A Substack publication's PUBLIC posts (the URL comes from 'substack_url:' in
     _config.yml, or --substack URL).  Uses Substack's public JSON endpoints with
     RSS and plain-HTML fallbacks, converts each post to Markdown, downloads its
     images into assets/img/<slug>/ and writes _posts/YYYY-MM-DD-<slug>.md.
  2. Local Markdown files dropped into posts/ (your "inbox" for new writing).
     A file needs nothing special: an optional '# Title' first line becomes the
     title; front matter (--- ... ---) is honoured if present.

Idempotent: posts already imported are skipped (Substack: by slug; local: by file
name + content hash).  --force re-imports everything.  --push commits and pushes.

Usage
  python3 tools/sync.py                 # import anything new
  python3 tools/sync.py --push          # ... then git commit + push (site rebuilds)
  python3 tools/sync.py --force         # re-import all Substack posts (e.g. after edits)
  python3 tools/sync.py --substack https://name.substack.com
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POSTS_DIR = os.path.join(ROOT, "_posts")
INBOX_DIR = os.path.join(ROOT, "posts")
IMG_DIR = os.path.join(ROOT, "assets", "img")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 blog-sync/1.0")
BT = chr(96)  # the backtick character

# ----------------------------------------------------------------------------- util

def log(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print("WARNING: " + msg, file=sys.stderr, flush=True)


def read_config_value(key: str, default: str = "") -> str:
    """Tiny reader for top-level 'key: value' scalars in _config.yml (no YAML lib)."""
    path = os.path.join(ROOT, "_config.yml")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"^%s:\s*(.*?)\s*(#.*)?$" % re.escape(key), line)
                if m:
                    v = m.group(1).strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                        v = v[1:-1]
                    return v
    except OSError:
        pass
    return default


def slugify(text: str) -> str:
    text = html.unescape(text or "").lower()
    text = re.sub("[\u2018\u2019']", "", text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:80].strip("-") or "post"


def yaml_str(s: str) -> str:
    """A YAML double-quoted scalar (JSON string syntax is valid YAML)."""
    return json.dumps(s if s is not None else "", ensure_ascii=False)


def parse_date(s: str) -> _dt.datetime:
    s = (s or "").strip()
    if not s:
        return _dt.datetime.now(_dt.timezone.utc)
    d = None
    try:  # ISO 8601, e.g. 2026-01-24T10:23:45.123Z
        s2 = re.sub(r"Z$", "+00:00", s)
        s2 = re.sub(r"\.(\d{1,6})\d*", lambda m: "." + m.group(1).ljust(6, "0"), s2)
        d = _dt.datetime.fromisoformat(s2)
    except ValueError:
        for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
                    "%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d %b %Y", "%b %d, %Y"):
            try:
                d = _dt.datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
    if d is None:
        warn("could not parse date %r, using now" % s)
        return _dt.datetime.now(_dt.timezone.utc)
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d


# ----------------------------------------------------------------------------- http

class FetchError(Exception):
    pass


def http_get(url: str, retries: int = 3, timeout: int = 45) -> bytes:
    """GET with retries.  Prefers the system curl (robust TLS on macOS), else urllib."""
    last = None
    for attempt in range(retries):
        try:
            if shutil.which("curl"):
                p = subprocess.run(
                    ["curl", "-fsSL", "--compressed", "--max-time", str(timeout),
                     "-A", UA, "-H", "Accept: application/json, text/html, */*", url],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if p.returncode == 0:
                    return p.stdout
                err = p.stderr.decode("utf-8", "replace").strip()
                last = FetchError("curl exit %d: %s" % (p.returncode, err[:200]))
                if p.returncode == 22 and re.search(r"error: 40[134]", err):
                    raise last  # no point retrying a 401/403/404
            else:
                req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.read()
        except FetchError:
            if last and re.search(r"error: 40[134]", str(last)):
                raise
        except urllib.error.HTTPError as e:
            last = FetchError("HTTP %s" % e.code)
            if e.code in (401, 403, 404):
                raise last
        except Exception as e:  # noqa: BLE001 - network errors of every flavour
            last = FetchError(str(e)[:200])
        time.sleep(2.0 * (attempt + 1))
    raise last or FetchError("unknown fetch error")


def http_get_json(url: str):
    return json.loads(http_get(url).decode("utf-8", "replace"))


# ----------------------------------------------------------------------------- html -> markdown

VOID = {"img", "br", "hr", "source", "input", "meta", "link", "wbr", "area", "col", "embed", "track"}
DROP_TAGS = {"script", "style", "svg", "form", "button", "noscript", "template", "audio", "video", "input", "select", "label"}
DROP_CLASS_RE = re.compile(
    r"subscription-widget|subscribe-widget|paywall|share-dialog|post-ufi|like-button|"
    r"comments-section|header-anchor-widget|modal|install-substack-app|captioned-button-wrap|"
    r"native-video-embed|audio-embed|poll-embed", re.I)
BLOCK_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "pre", "blockquote",
              "figure", "figcaption", "table", "hr", "section", "article", "aside", "details", "summary", "iframe"}


class Node:
    __slots__ = ("tag", "attrs", "children", "text")

    def __init__(self, tag: str = "", attrs=None, text: str = ""):
        self.tag = tag
        self.attrs = {k: (v if v is not None else "") for k, v in (attrs or {}).items()} if isinstance(attrs, dict) \
            else {k: (v if v is not None else "") for k, v in (attrs or [])}
        self.children: list = []
        self.text = text

    def cls(self) -> str:
        return self.attrs.get("class", "") or ""

    def find_all(self, tag: str):
        for c in self.children:
            if c.tag == tag:
                yield c
            for sub in c.find_all(tag):
                yield sub

    def has(self, tag: str) -> bool:
        for _ in self.find_all(tag):
            return True
        return False

    def text_content(self) -> str:
        if self.tag == "#text":
            return self.text
        return "".join(c.text_content() for c in self.children)

    def with_children(self, children):
        self.children = list(children)
        return self


class TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        # html is forgiving about unclosed <p>/<li>: close an open sibling of the same kind
        if tag in ("p", "li") and self.stack[-1].tag == tag:
            self.stack.pop()
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Node(tag, attrs))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if data:
            self.stack[-1].children.append(Node("#text", text=data))


def parse_html(s: str) -> Node:
    tb = TreeBuilder()
    tb.feed(s or "")
    tb.close()
    return tb.root


MD_SPECIAL = re.compile(r"([\\*_\[\]|" + BT + r"])")


def esc_inline(text: str) -> str:
    text = MD_SPECIAL.sub(r"\\\1", text)
    return text.replace("<", "&lt;").replace(">", "&gt;")


def esc_line_start(line: str) -> str:
    """Escape things that only mean something at the start of a Markdown line."""
    m = re.match(r"^(\s*)(#{1,6}\s|>|[-+]\s|\d+[.)]\s)", line)
    if m:
        return line[: m.start(2)] + "\\" + line[m.start(2):]
    return line


def liquid_safe(text: str) -> str:
    """Neutralise Liquid syntax that would otherwise break the Jekyll build
    (our own '{{ site.baseurl }}' image prefixes are left alone)."""
    text = re.sub(r"\{\{(?! site\.baseurl \}\})", '{{ "{{" }}', text)
    return text.replace("{%", '{{ "{%" }}')


class Markdownifier:
    """Converts a Substack-style HTML body into kramdown-flavoured Markdown."""

    def __init__(self, image_handler=None):
        self.image_handler = image_handler or (lambda url, alt="": url)
        self.footnotes: list = []

    # ---- entry point
    def convert(self, html_text: str) -> str:
        root = parse_html(html_text)
        blocks = self.blocks(root)
        out = "\n\n".join(b for b in blocks if b.strip())
        if self.footnotes:
            out += "\n\n" + "\n\n".join(self.footnotes)
        out = re.sub(r"\n{3,}", "\n\n", out).strip() + "\n"
        return liquid_safe(out)

    # ---- helpers
    @staticmethod
    def data_attrs(n: Node) -> dict:
        try:
            d = json.loads(html.unescape(n.attrs.get("data-attrs", "") or "{}"))
            return d if isinstance(d, dict) else {}
        except ValueError:
            return {}

    def dropped(self, n: Node) -> bool:
        if n.tag in DROP_TAGS:
            return True
        if DROP_CLASS_RE.search(n.cls()):
            return True
        comp = n.attrs.get("data-component-name", "") or ""
        if re.search(r"Subscribe|Paywall|Share|ButtonCreateButton", comp):
            return True
        return False

    # ---- block rendering
    def blocks(self, parent: Node) -> list:
        out: list = []
        inline_run: list = []

        def flush():
            if inline_run:
                t = self.inline(inline_run).strip()
                if t:
                    out.append(self.para(t))
                del inline_run[:]

        for c in parent.children:
            if c.tag == "#text" or (c.tag not in BLOCK_TAGS and not self.is_blockish(c)):
                inline_run.append(c)
            else:
                flush()
                out.extend(self.block(c))
        flush()
        return out

    @staticmethod
    def is_blockish(n: Node) -> bool:
        return n.tag == "img" or (n.tag in ("a", "span", "picture") and n.has("img"))

    @staticmethod
    def para(text: str) -> str:
        return "\n".join(esc_line_start(l) for l in text.split("\n"))

    def block(self, n: Node) -> list:
        if self.dropped(n):
            return []
        tag, cls = n.tag, n.cls()
        if tag == "p":
            if "button-wrapper" in cls or "button-wrap" in cls:
                return self.button(n)
            if n.has("img") and not n.text_content().strip():
                return self.figure(n, list(n.find_all("img")))
            t = self.inline(n.children).strip()
            return [self.para(t)] if t else []
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = max(2, int(tag[1]))  # the page already has an <h1> (the title)
            t = self.inline(n.children).strip().replace("\n", " ")
            return ["#" * level + " " + t] if t else []
        if tag == "hr":
            return ["* * *"]
        if tag == "pre":
            code = n.text_content().rstrip("\n")
            fence = "~~~~" if BT * 3 in code else BT * 3
            lang = ""
            for c in n.find_all("code"):
                m = re.search(r"language-([\w+-]+)", c.cls())
                if m:
                    lang = m.group(1)
                break
            return [fence + lang + "\n" + code + "\n" + fence]
        if tag == "blockquote" or "pullquote" in cls:
            inner = "\n\n".join(self.blocks(n))
            if not inner.strip():
                return []
            return ["\n".join(("> " + l if l.strip() else ">") for l in inner.split("\n"))]
        if tag in ("ul", "ol"):
            return [self.listing(n, ordered=(tag == "ol"))]
        if tag == "li":  # stray li outside a list
            return self.blocks(n)
        if tag == "table":
            t = self.table(n)
            return [t] if t else []
        if tag == "iframe":
            src = n.attrs.get("src", "")
            m = re.search(r"youtube(?:-nocookie)?\.com/embed/([\w-]{6,})", src)
            if m:
                return ["[Watch the video on YouTube](https://www.youtube.com/watch?v=%s)" % m.group(1)]
            return ["[Embedded media](%s)" % src] if src else []
        if tag == "img":
            return self.figure(n, [n])
        if tag in ("figure", "a", "span", "picture") or (tag == "div" and "image" in cls):
            if n.has("img"):
                return self.figure(n, list(n.find_all("img")))
        if tag == "div":
            classes = cls.split()
            if "footnote" in classes:
                return self.footnote_def(n)
            if "youtube" in cls or "vimeo" in cls:
                d = self.data_attrs(n)
                vid = d.get("videoId") or d.get("video_id")
                if vid and "youtube" in cls:
                    return ["[Watch the video on YouTube](https://www.youtube.com/watch?v=%s)" % vid]
                if vid:
                    return ["[Watch the video on Vimeo](https://vimeo.com/%s)" % vid]
            if n.attrs.get("data-attrs") and not n.has("img"):
                d = self.data_attrs(n)
                url = d.get("url") or d.get("canonical_url") or d.get("href")
                if url:
                    title = d.get("title") or (d.get("full_text") or "")[:80] or "Embedded link"
                    title = re.sub(r"\s+", " ", str(title)).strip() or "Embedded link"
                    return ["[%s](%s)" % (esc_inline(title), url)]
            return self.blocks(n)
        # section/article/aside/details/figcaption/anything else: just recurse
        return self.blocks(n)

    def button(self, n: Node) -> list:
        for a in n.find_all("a"):
            href = a.attrs.get("href", "") or ""
            text = a.text_content().strip()
            if not href or re.search(r"subscribe|/share|action=share|comments|javascript:|%%checkout_url%%", href, re.I) \
                    or re.search(r"^(subscribe|share|leave a comment|upgrade|get the app|refer|pledge|donate)", text, re.I):
                return []
            return ["[%s](%s)" % (esc_inline(text or href), href.replace("(", "%28").replace(")", "%29"))]
        return []

    def figure(self, n: Node, imgs: list) -> list:
        out = []
        caption = ""
        for fc in n.find_all("figcaption"):
            caption = self.inline(fc.children).strip().replace("\n", " ")
            break
        for img in imgs:
            d = self.data_attrs(img)
            src = d.get("src") or img.attrs.get("src") or ""
            if not src:
                srcset = img.attrs.get("srcset", "")
                src = srcset.split(",")[-1].strip().split(" ")[0] if srcset else ""
            if not src or src.startswith("data:"):
                continue
            alt = (img.attrs.get("alt") or d.get("alt") or img.attrs.get("title") or "").replace("\n", " ").strip()
            local = self.image_handler(src, alt)
            out.append("![%s](%s)" % (esc_inline(alt)[:200], local))
        if caption and out:
            plain = re.sub(r"(?<!\\)[*_]", "", caption).strip()  # no nested emphasis inside the caption line
            if plain:
                out.append("*" + plain + "*")
        return ["\n".join(out)] if out else []

    def listing(self, n: Node, ordered: bool) -> str:
        items = []
        i = 0
        for li in n.children:
            if li.tag != "li":
                continue
            i += 1
            marker = ("%d. " % i) if ordered else "- "
            body = "\n\n".join(self.blocks(li)).strip()
            lines = body.split("\n")
            first = marker + (lines[0] if lines else "")
            pad = " " * len(marker)
            rest = [(pad + l if l.strip() else "") for l in lines[1:]]
            items.append("\n".join([first] + rest))
        return "\n".join(items)

    def table(self, n: Node) -> str:
        rows = []
        for tr in n.find_all("tr"):
            cells = [self.inline(td.children).strip().replace("\n", " ")
                     for td in tr.children if td.tag in ("td", "th")]
            if cells:
                rows.append(cells)
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
        out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return "\n".join(out)

    def footnote_def(self, n: Node) -> list:
        num = ""
        for a in n.find_all("a"):
            if "footnote-number" in a.cls():
                num = a.text_content().strip()
                break
        rest = [c for c in n.children if not (c.tag == "a" and "footnote-number" in c.cls())]
        body = " ".join(b.replace("\n", " ") for b in self.blocks(Node("div").with_children(rest)))
        num = re.sub(r"\D", "", num) or str(len(self.footnotes) + 1)
        self.footnotes.append("[^%s]: %s" % (num, body.strip()))
        return []

    # ---- inline rendering
    def inline(self, nodes) -> str:
        text = "".join(self.inline_node(c) for c in nodes)
        return re.sub(r"[ \t]*\n[ \t]*", "\n", text)

    def inline_node(self, c: Node) -> str:
        if c.tag == "#text":
            return esc_inline(re.sub(r"\s+", " ", c.text))
        if self.dropped(c):
            return ""
        t = c.tag
        if t == "br":
            return "<br>\n"
        if t in ("strong", "b"):
            return self.wrap("**", c)
        if t in ("em", "i", "cite"):
            return self.wrap("*", c)
        if t in ("s", "strike", "del"):
            return self.wrap("~~", c)
        if t == "code":
            txt = c.text_content()
            ticks = BT * 2 if BT in txt else BT
            return ticks + txt + ticks
        if t == "a":
            cls = c.cls()
            href = (c.attrs.get("href") or "").strip()
            if "footnote-anchor" in cls:
                num = re.sub(r"\D", "", c.text_content()) or "1"
                return "[^%s]" % num
            if c.has("img"):
                return "\n\n" + "\n".join(self.figure(c, list(c.find_all("img")))) + "\n\n"
            inner = self.inline(c.children).strip()
            if not href or href.startswith("javascript:"):
                return inner
            if not inner:
                inner = esc_inline(href)
            return "[%s](%s)" % (inner, href.replace(" ", "%20").replace("(", "%28").replace(")", "%29"))
        if t == "img":
            return "\n\n" + "\n".join(self.figure(c, [c])) + "\n\n"
        if t in ("sup", "sub", "u", "mark", "kbd"):
            inner = self.inline(c.children)
            return "<%s>%s</%s>" % (t, inner, t) if inner.strip() else ""
        if t in BLOCK_TAGS:  # a block nested where we expected inline: render and splice
            return "\n\n" + "\n\n".join(self.block(c)) + "\n\n"
        return self.inline(c.children)  # span, font, abbr, picture, ...

    def wrap(self, marker: str, c: Node) -> str:
        inner = self.inline(c.children)
        if not inner.strip():
            return inner
        lead = inner[: len(inner) - len(inner.lstrip())]
        trail = inner[len(inner.rstrip()):]
        return "%s%s%s%s%s" % (lead, marker, inner.strip(), marker, trail)


# ----------------------------------------------------------------------------- post store

FM_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?(.*)\Z", re.S)


def parse_fm_lines(text: str) -> dict:
    fm = {}
    for line in text.split("\n"):
        kv = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line.rstrip("\r"))
        if kv:
            v = kv.group(2).strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                try:
                    v = json.loads(v) if v[0] == '"' else v[1:-1]
                except ValueError:
                    v = v[1:-1]
            fm[kv.group(1)] = v
    return fm


def read_front_matter(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            head = f.read(8000)
    except (OSError, TypeError):
        return {}
    if not head.startswith("---"):
        return {}
    end = head.find("\n---", 3)
    return parse_fm_lines(head[3:end if end > 0 else len(head)])


def existing_posts() -> dict:
    """Map of identifying keys -> path for everything already in _posts/."""
    found = {}
    if not os.path.isdir(POSTS_DIR):
        return found
    for name in os.listdir(POSTS_DIR):
        if not name.endswith((".md", ".markdown", ".html")):
            continue
        path = os.path.join(POSTS_DIR, name)
        fm = read_front_matter(path)
        if fm.get("substack_slug"):
            found["substack:" + str(fm["substack_slug"])] = path
        if fm.get("source_file"):
            found["local:" + str(fm["source_file"])] = path
        found["file:" + name] = path
    return found


def write_post(date: _dt.datetime, slug: str, fm_lines: list, body_md: str, path=None) -> str:
    os.makedirs(POSTS_DIR, exist_ok=True)
    if path is None:
        path = os.path.join(POSTS_DIR, "%s-%s.md" % (date.strftime("%Y-%m-%d"), slug))
    content = "---\n" + "\n".join(fm_lines) + "\n---\n\n" + body_md.strip() + "\n"
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)
    return path


# ----------------------------------------------------------------------------- images

class ImageStore:
    """Downloads each remote image once into assets/img/<slug>/ and returns the site path."""

    def __init__(self, slug: str, enabled: bool = True):
        self.slug = slug
        self.enabled = enabled
        self.count = 0
        self.seen: dict = {}

    @staticmethod
    def original_url(src: str) -> str:
        """Substack serves images through a CDN 'fetch' URL that wraps the original."""
        m = re.search(r"/image/fetch/[^/]*/(https?(?:%3A|:).*)$", src, re.I)
        if m:
            return urllib.parse.unquote(m.group(1))
        return src

    def filename_for(self, url: str, data: bytes) -> str:
        base = os.path.basename(urllib.parse.urlparse(url).path) or "image"
        base = urllib.parse.unquote(base)
        stem, ext = os.path.splitext(base)
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.")[:40] or "image"
        sig = data[:16]
        if sig.startswith(b"\x89PNG"):
            real = ".png"
        elif sig.startswith(b"\xff\xd8"):
            real = ".jpg"
        elif sig.startswith(b"GIF8"):
            real = ".gif"
        elif sig[:4] == b"RIFF" and sig[8:12] == b"WEBP":
            real = ".webp"
        elif sig[4:12] in (b"ftypavif", b"ftypavis"):
            real = ".avif"
        elif b"<svg" in data[:300].lower():
            real = ".svg"
        else:
            real = ext.lower() if re.match(r"^\.(png|jpe?g|gif|webp|svg|avif)$", ext.lower()) else ".img"
        self.count += 1
        return "%02d-%s%s" % (self.count, stem, real)

    def __call__(self, src: str, alt: str = "") -> str:
        orig = self.original_url(src)
        if src in self.seen or orig in self.seen:
            return self.seen.get(src) or self.seen[orig]
        if not self.enabled or not src.startswith(("http://", "https://")):
            return src
        candidates = [orig, src] if orig != src else [src]
        if "f_auto" in src:
            candidates.append(src.replace("f_auto", "f_png"))
        data = None
        for u in candidates:
            try:
                data = http_get(u)
                if data:
                    break
            except FetchError as e:
                warn("image fetch failed (%s): %s" % (e, u))
        if not data:
            self.seen[src] = src
            return src  # leave the remote URL in place rather than lose the image
        name = self.filename_for(orig, data)
        d = os.path.join(IMG_DIR, self.slug)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "wb") as f:
            f.write(data)
        site_path = "{{ site.baseurl }}/assets/img/%s/%s" % (self.slug, name)
        self.seen[src] = self.seen[orig] = site_path
        return site_path


# ----------------------------------------------------------------------------- substack

class Substack:
    def __init__(self, base: str):
        base = base.strip()
        if not re.match(r"^https?://", base):
            base = "https://" + base
        self.base = base.rstrip("/")
        self._feed = None

    def archive(self, limit_total: int = 0) -> list:
        posts, offset, page = [], 0, 12
        while True:
            url = "%s/api/v1/archive?sort=new&search=&offset=%d&limit=%d" % (self.base, offset, page)
            batch = http_get_json(url)
            if not isinstance(batch, list) or not batch:
                break
            posts.extend(p for p in batch if isinstance(p, dict))
            offset += len(batch)
            if limit_total and len(posts) >= limit_total:
                return posts[:limit_total]
            if offset > 5000:  # safety valve
                break
            time.sleep(1.0)
        return posts

    def feed(self) -> list:
        if self._feed is not None:
            return self._feed
        items = []
        self._feed = items
        try:
            xml = http_get(self.base + "/feed").decode("utf-8", "replace")
        except FetchError as e:
            warn("RSS feed not available: %s" % e)
            return items
        chan = xml.split("<item>")[0]

        def grab(tag, raw):
            m = re.search(r"<%s(?:\s[^>]*)?>(.*?)</%s>" % (tag, tag), raw, re.S)
            if not m:
                return ""
            v = m.group(1).strip()
            if v.startswith("<![CDATA["):
                return v[9:-3].strip()
            return html.unescape(v).strip()

        self.meta = {"title": grab("title", chan), "description": grab("description", chan)}
        for raw in re.findall(r"<item>(.*?)</item>", xml, re.S):
            link = grab("link", raw)
            slug = urllib.parse.urlparse(link).path.rstrip("/").split("/")[-1]
            enc = re.search(r"<enclosure[^>]*url=\"([^\"]+)\"", raw)
            items.append({
                "title": grab("title", raw), "subtitle": grab("description", raw), "slug": slug,
                "canonical_url": link, "post_date": grab("pubDate", raw),
                "body_html": grab("content:encoded", raw), "cover_image": enc.group(1) if enc else "",
            })
        return items

    def list_posts(self, limit_total: int = 0) -> list:
        try:
            posts = self.archive(limit_total)
            if posts:
                return posts
            warn("archive API returned nothing; falling back to RSS")
        except (FetchError, ValueError) as e:
            warn("archive API failed (%s); falling back to RSS (latest posts only)" % e)
        return self.feed()

    def full_post(self, stub: dict) -> dict:
        slug = stub.get("slug") or ""
        post = dict(stub)
        body = post.get("body_html") or ""
        if (not body or len(body) < 500) and slug:
            try:
                detail = http_get_json("%s/api/v1/posts/%s" % (self.base, urllib.parse.quote(slug)))
                if isinstance(detail, dict):
                    post.update({k: v for k, v in detail.items() if v not in (None, "")})
                    body = post.get("body_html") or ""
            except (FetchError, ValueError) as e:
                warn("post API failed for %s: %s" % (slug, e))
        if not body:
            for it in self.feed():
                if it.get("slug") == slug and it.get("body_html"):
                    body = it["body_html"]
                    break
        if not body:
            try:
                page = http_get(post.get("canonical_url") or "%s/p/%s" % (self.base, slug)).decode("utf-8", "replace")
                m = re.search(r'<div class="available-content">(.*?)<div[^>]+class="[^"]*post-footer', page, re.S) or \
                    re.search(r'<div[^>]+class="body markup"[^>]*>(.*)</div>\s*</div>\s*<div[^>]+class="[^"]*(?:post-footer|post-ufi)', page, re.S)
                if m:
                    body = m.group(1)
            except FetchError as e:
                warn("page fetch failed for %s: %s" % (slug, e))
        post["body_html"] = body
        return post


def import_substack(base: str, force: bool, limit: int, images: bool) -> tuple:
    sub = Substack(base)
    log("Substack: listing public posts at %s ..." % sub.base)
    stubs = sub.list_posts(limit)
    log("Substack: %d post(s) found" % len(stubs))
    have = existing_posts()
    new = skipped = 0
    for stub in stubs:
        slug = stub.get("slug") or slugify(stub.get("title", ""))
        if not slug:
            continue
        key = "substack:" + slug
        if key in have and not force:
            skipped += 1
            continue
        try:
            post = sub.full_post(stub)
            body_html = post.get("body_html") or ""
            audience = post.get("audience") or "everyone"
            if not body_html.strip():
                warn("%s: no public body available (audience=%s) - skipped" % (slug, audience))
                continue
            title = html.unescape(str(post.get("title") or slug)).strip()
            subtitle = html.unescape(str(post.get("subtitle") or post.get("description") or "")).strip()
            date = parse_date(str(post.get("post_date") or post.get("published_at") or ""))
            store = ImageStore(slug, enabled=images)
            body_md = Markdownifier(image_handler=store).convert(body_html)
            # Only Substack's own signals count as "paywalled": a non-public audience, or the API
            # saying the body it returned is cut (never a word-count guess - that could print an
            # untrue "continues for subscribers" line under a public post).
            truncated = audience not in ("everyone", "") or bool(post.get("is_truncated")) \
                or str(post.get("truncated", "")).lower() == "true"
            if truncated:
                body_md += "\n\n*This post continues for subscribers on [Substack](%s).*\n" % (
                    post.get("canonical_url") or sub.base)
            cover = post.get("cover_image") or ""
            cover_local = ""
            if cover and isinstance(cover, str):
                cover_local = store(cover, title)
            tags = []
            for t in post.get("postTags") or post.get("tags") or []:
                name = t.get("name") if isinstance(t, dict) else str(t)
                if name:
                    tags.append(str(name))
            fm = ["layout: post", "title: %s" % yaml_str(title)]
            if subtitle:
                fm.append("subtitle: %s" % yaml_str(subtitle))
            fm += [
                "date: %s" % date.strftime("%Y-%m-%d %H:%M:%S %z"),
                "substack_slug: %s" % yaml_str(slug),
                "original_url: %s" % yaml_str(str(post.get("canonical_url") or "%s/p/%s" % (sub.base, slug))),
            ]
            if tags:
                fm.append("tags: [%s]" % ", ".join(yaml_str(t) for t in tags))
            if cover_local:
                fm.append("image: %s" % yaml_str(cover_local.replace("{{ site.baseurl }}", "")))
            if truncated:
                fm.append("paywalled: true")
            out = write_post(date, slug, fm, body_md, have.get(key))  # overwrite in place on --force
            new += 1
            log("  + %s  (%d image(s))" % (os.path.relpath(out, ROOT), store.count))
            time.sleep(1.0)  # be polite to Substack
        except Exception as e:  # never let one bad post stop the whole sync
            warn("%s: failed (%s: %s) - continuing" % (slug, type(e).__name__, e))
    return new, skipped


# ----------------------------------------------------------------------------- local inbox

def import_local(force: bool) -> tuple:
    if not os.path.isdir(INBOX_DIR):
        return 0, 0
    have = existing_posts()
    new = skipped = 0
    for name in sorted(os.listdir(INBOX_DIR)):
        if not name.lower().endswith((".md", ".markdown", ".txt")) or name.upper().startswith("README"):
            continue
        src = os.path.join(INBOX_DIR, name)
        rel = "posts/" + name
        try:
            with open(src, encoding="utf-8") as f:
                raw = f.read()
        except (OSError, UnicodeDecodeError) as e:
            warn("%s: unreadable (%s)" % (rel, e))
            continue
        if not raw.strip():
            continue
        raw = raw.replace("\r\n", "\n")
        sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        existing = have.get("local:" + rel)
        old = read_front_matter(existing) if existing else {}
        if existing and old.get("source_sha") == sha and not force:
            skipped += 1
            continue
        fm_in, body = {}, raw
        m = FM_RE.match(raw)
        if m:
            fm_in, body = parse_fm_lines(m.group(1)), m.group(2)
        title = str(fm_in.get("title") or "")
        if not title:
            h = re.match(r"\s*#\s+(.+?)\s*#*\s*\n", body.lstrip("\ufeff") + "\n")
            if h:
                title = h.group(1).strip()
                body = body.lstrip("\ufeff")[h.end():]
            else:
                title = os.path.splitext(name)[0].replace("-", " ").replace("_", " ").strip().capitalize()
        if old.get("date"):
            date = parse_date(str(old["date"]))      # keep the first-publish date stable
        elif fm_in.get("date"):
            date = parse_date(str(fm_in["date"]))
        else:
            date = _dt.datetime.now().astimezone() - _dt.timedelta(minutes=5)
        slug = str(fm_in.get("slug") or old.get("slug") or slugify(title))
        fm = ["layout: post", "title: %s" % yaml_str(title)]
        if fm_in.get("subtitle"):
            fm.append("subtitle: %s" % yaml_str(str(fm_in["subtitle"])))
        fm += ["date: %s" % date.strftime("%Y-%m-%d %H:%M:%S %z"),
               "slug: %s" % yaml_str(slug),
               "source_file: %s" % yaml_str(rel),
               "source_sha: %s" % sha]
        for k in ("tags", "image", "description"):
            if fm_in.get(k):
                v = str(fm_in[k])
                fm.append("%s: %s" % (k, v if (k == "tags" and v.startswith("[")) else yaml_str(v)))
        out = write_post(date, slug, fm, liquid_safe(body), existing)
        new += 1
        log("  + %s  <- %s" % (os.path.relpath(out, ROOT), rel))
    return new, skipped


# ----------------------------------------------------------------------------- git

def git(*args, check=True) -> str:
    p = subprocess.run(["git", "-C", ROOT] + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = p.stdout.decode("utf-8", "replace")
    if check and p.returncode != 0:
        raise RuntimeError("git %s failed:\n%s" % (" ".join(args), out))
    return out


def push_changes(summary: str) -> None:
    if not os.path.isdir(os.path.join(ROOT, ".git")):
        warn("not a git repository; skipping --push")
        return
    git("add", "-A")
    if not git("status", "--porcelain").strip():
        log("git: nothing new to commit")
    else:
        git("commit", "-q", "--no-verify", "-m", summary)
        log("git: committed (%s)" % summary)
    p = subprocess.run(["git", "-C", ROOT, "push", "-q"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = p.stdout.decode("utf-8", "replace")
    if p.returncode != 0:
        raise RuntimeError("git push failed:\n" + out)
    log("git: pushed - GitHub Pages rebuilds the site within a minute or two")


# ----------------------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Import Substack posts and local Markdown into _posts/.")
    ap.add_argument("--substack", default=None, help="publication URL (default: substack_url in _config.yml)")
    ap.add_argument("--force", action="store_true", help="re-import posts that already exist")
    ap.add_argument("--push", action="store_true", help="git commit + push after syncing")
    ap.add_argument("--limit", type=int, default=0, help="only the newest N Substack posts (0 = all)")
    ap.add_argument("--no-images", action="store_true", help="leave image URLs pointing at Substack's CDN")
    ap.add_argument("--no-substack", action="store_true", help="only process the local posts/ inbox")
    args = ap.parse_args(argv)

    base = args.substack if args.substack is not None else (
        os.environ.get("SUBSTACK_URL") or read_config_value("substack_url", ""))
    total = 0
    rc = 0
    if base and not args.no_substack:
        try:
            n, s = import_substack(base, args.force, args.limit, not args.no_images)
            log("Substack: %d imported, %d already present" % (n, s))
            total += n
        except FetchError as e:
            warn("Substack unreachable (%s) - local posts are still processed" % e)
            rc = 2
    elif not base:
        log("Substack: no substack_url configured - skipping")
    n, s = import_local(args.force)
    log("Local posts/: %d imported or updated, %d unchanged" % (n, s))
    total += n
    if args.push:
        push_changes("Sync: %d post(s) added or updated" % total if total else "Update site")
    return rc


if __name__ == "__main__":
    sys.exit(main())
