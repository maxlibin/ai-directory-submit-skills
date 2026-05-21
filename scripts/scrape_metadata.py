#!/usr/bin/env python3
"""
Scrape a website's public metadata into a pre-filled submission-info.json.

Usage:
    scrape_metadata.py https://your-tool.com [-o submission-info.json] [--download-logo]

What it pulls:
  - Title, description (meta + og:description + twitter:description)
  - Long description (visible body text, trimmed)
  - og:image / twitter:image / apple-touch-icon / favicon for logo
  - Social links (twitter/x.com, linkedin, github, instagram) found in HTML
  - Pricing hint by scanning for /pricing page text

What it does NOT do:
  - Replace your judgment. Always review the output before submitting.
  - Invent categories / tagline if the site doesn't expose them — left as a TODO.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
TIMEOUT = 15


def fetch(url: str, max_bytes: int = 2_500_000) -> tuple[str, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        ct = r.headers.get("Content-Type", "text/html")
        raw = r.read(max_bytes)
    enc = "utf-8"
    m = re.search(r"charset=([\w-]+)", ct, re.I)
    if m:
        enc = m.group(1)
    try:
        return raw.decode(enc, errors="replace"), str(r.url)
    except LookupError:
        return raw.decode("utf-8", errors="replace"), str(r.url)


class MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.in_title = False
        self.metas: dict[str, str] = {}
        self.icons: list[tuple[str, str]] = []
        self.links: list[str] = []
        self.body_text: list[str] = []
        self.in_body = False
        self.skip_depth = 0
        self.skip_tags = {"script", "style", "noscript", "svg", "header", "footer", "nav"}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self.in_title = True
        elif tag == "meta":
            name = (a.get("name") or a.get("property") or a.get("itemprop") or "").lower()
            content = a.get("content", "")
            if name and content:
                self.metas.setdefault(name, content)
        elif tag == "link":
            rel = a.get("rel", "").lower()
            href = a.get("href", "")
            if href and any(k in rel for k in ("icon", "apple-touch-icon", "mask-icon", "shortcut")):
                sizes = a.get("sizes", "")
                self.icons.append((href, sizes))
        elif tag == "a":
            href = a.get("href", "")
            if href:
                self.links.append(href)
        elif tag == "body":
            self.in_body = True
        elif tag in self.skip_tags:
            self.skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        elif tag in self.skip_tags and self.skip_depth > 0:
            self.skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data
        elif self.in_body and self.skip_depth == 0:
            t = data.strip()
            if t:
                self.body_text.append(t)


def absolute(base: str, href: str) -> str:
    return urllib.parse.urljoin(base, href)


def pick_logo(p: MetaParser, base: str) -> str:
    candidates: list[tuple[int, str]] = []
    og = p.metas.get("og:image") or p.metas.get("twitter:image")
    if og:
        candidates.append((1000, absolute(base, og)))
    for href, sizes in p.icons:
        score = 0
        m = re.search(r"(\d+)", sizes)
        if m:
            score = int(m.group(1))
        elif "apple-touch-icon" in href.lower():
            score = 180
        else:
            score = 32
        candidates.append((score, absolute(base, href)))
    if not candidates:
        return urllib.parse.urljoin(base, "/favicon.ico")
    candidates.sort(reverse=True)
    return candidates[0][1]


def find_social(links: list[str], base: str) -> dict[str, str]:
    out: dict[str, str] = {}
    patterns = {
        "twitter": re.compile(r"^https?://(www\.)?(twitter|x)\.com/[^/?#]+/?$", re.I),
        "linkedin": re.compile(r"^https?://(www\.)?linkedin\.com/(company|in)/[^/?#]+/?$", re.I),
        "github": re.compile(r"^https?://(www\.)?github\.com/[^/?#]+/?$", re.I),
        "instagram": re.compile(r"^https?://(www\.)?instagram\.com/[^/?#]+/?$", re.I),
    }
    for href in links:
        full = absolute(base, href)
        for key, rx in patterns.items():
            if key not in out and rx.match(full):
                out[key] = full.rstrip("/")
    return out


def detect_pricing(body: str) -> str:
    txt = body.lower()
    has_free = any(k in txt for k in ("free plan", "free tier", "free forever", "free to use", "100% free", "always free"))
    has_paid = any(k in txt for k in ("/month", "per month", "/mo", "$", "subscribe", "upgrade", "pricing", "premium", "pro plan"))
    has_trial = "free trial" in txt or "start trial" in txt or "try free" in txt
    has_contact = "contact sales" in txt or "request demo" in txt or "book a demo" in txt
    if has_free and has_paid:
        return "Freemium"
    if has_trial:
        return "Free Trial"
    if has_paid and not has_free:
        return "Paid"
    if has_contact and not has_free and not has_paid:
        return "Contact"
    if has_free:
        return "Free"
    return "Freemium"


def clean(s: str, n: int | None = None) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    if n and len(s) > n:
        s = s[: n - 1].rstrip() + "…"
    return s


def download_logo(url: str, dest: Path) -> Path | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = r.read(2_000_000)
            ctype = r.headers.get("Content-Type", "")
    except Exception as e:
        print(f"  logo download failed: {e}", file=sys.stderr)
        return None
    ext = ".png"
    if "jpeg" in ctype or "jpg" in ctype:
        ext = ".jpg"
    elif "svg" in ctype:
        ext = ".svg"
    elif "webp" in ctype:
        ext = ".webp"
    elif "ico" in ctype:
        ext = ".ico"
    dest = dest.with_suffix(ext)
    dest.write_bytes(data)
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", help="The tool/website URL")
    ap.add_argument("-o", "--output", default="submission-info.json", help="Where to write the JSON (default: submission-info.json)")
    ap.add_argument("--download-logo", action="store_true", help="Download the logo to ./logo.<ext> and set logo_path")
    ap.add_argument("--email", help="Contact email (otherwise left blank)")
    ap.add_argument("--force", action="store_true", help="Overwrite output if it exists")
    args = ap.parse_args()

    out_path = Path(args.output).resolve()
    if out_path.exists() and not args.force:
        print(f"refusing to overwrite {out_path} (use --force)", file=sys.stderr)
        return 2

    target = args.url if args.url.startswith(("http://", "https://")) else f"https://{args.url}"
    print(f"fetching {target} …", file=sys.stderr)
    try:
        html, final_url = fetch(target)
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} on {target}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"fetch failed: {e}", file=sys.stderr)
        return 1

    p = MetaParser()
    try:
        p.feed(html)
    except Exception:
        pass  # best-effort; tolerate broken HTML

    title = clean(p.metas.get("og:title") or p.metas.get("twitter:title") or p.title or "")
    name = re.split(r"\s*[|\-–—:]\s*", title, maxsplit=1)[0] if title else ""
    desc = clean(p.metas.get("description") or p.metas.get("og:description") or p.metas.get("twitter:description") or "")
    body = " ".join(p.body_text)
    body_clean = clean(body)

    info = {
        "_README": "Auto-scraped. REVIEW every field before submitting — directories permanently associate your brand with this data. Fill in TODO fields.",
        "name": name or "TODO",
        "url": final_url.rstrip("/"),
        "tagline": clean(desc, 100) or "TODO: one sentence pitch, ≤100 chars, no period",
        "short_description": clean(desc, 200) or "TODO: 1–2 sentences, ≤200 chars",
        "long_description": clean(body_clean, 1200) or "TODO: 2–4 paragraphs about what it does, who it's for, what's unique",
        "categories": ["TODO: 2–3 categories e.g. AI Tools, Productivity"],
        "pricing": detect_pricing(body_clean),
        "pricing_options": "One of: Free, Freemium, Free Trial, Paid, Contact",
        "logo_url": pick_logo(p, final_url),
        "logo_path": "TODO: absolute path to a real local PNG (required by many directories)",
        "screenshot_url": p.metas.get("og:image") or "",
        "screenshot_path": "",
        "email": args.email or "TODO: contact email",
        "tags": [],
    }
    info.update(find_social(p.links, final_url))

    if args.download_logo and info["logo_url"]:
        print(f"downloading logo from {info['logo_url']} …", file=sys.stderr)
        saved = download_logo(info["logo_url"], out_path.parent / "logo")
        if saved:
            info["logo_path"] = str(saved.resolve())
            print(f"  saved → {saved}", file=sys.stderr)

    out_path.write_text(json.dumps(info, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out_path}", file=sys.stderr)

    todos = [k for k, v in info.items() if isinstance(v, str) and v.startswith("TODO") or (isinstance(v, list) and v and isinstance(v[0], str) and v[0].startswith("TODO"))]
    if todos:
        print(f"\n⚠  {len(todos)} fields need your input: {', '.join(todos)}", file=sys.stderr)
        print("    Open the file, fill them in, then run the submission flow.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
