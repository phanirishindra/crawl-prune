#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import List, Set, Dict, Optional, Tuple
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup, Comment
from markdownify import markdownify as md
from readability import Document

# Optional Playwright import (for dynamic pages)
try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except Exception:
    PLAYWRIGHT_AVAILABLE = False


# -----------------------------
# Config
# -----------------------------
INVISIBLE_TAGS = {
    "style", "noscript", "meta", "link", "iframe", "object", "embed", "svg", "path",
    "nav", "footer", "aside", "form", "button"
}
# NOTE: script is intentionally NOT always dropped immediately; sometimes tutorial code is in script tags.
DROP_SCRIPT_BY_DEFAULT = True

GARBAGE_HINTS = {
    "sidebar", "widget", "menu", "archive", "comments", "advertisement", "promo", "popup",
    "cookie", "consent", "breadcrumb", "share", "social", "related", "newsletter"
}

STRIP_ATTRS = {
    "class", "style",
    "onclick", "onload", "onmouseover", "onfocus", "onblur", "onchange", "onsubmit",
    "onerror", "onkeydown", "onkeyup", "onkeypress"
}

CONTENT_TAGS = {
    "main", "article", "section", "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "ul", "ol", "li", "table", "thead", "tbody", "tr", "th", "td",
    "pre", "code", "blockquote", "a", "img", "video", "audio", "figure", "figcaption"
}

PAGINATION_TEXT = {"next", "older", "more", ">", "»"}


@dataclass
class CrawlConfig:
    max_pages: int = 50
    max_depth: int = 2
    timeout_sec: int = 20
    render_timeout_sec: int = 35
    delay_sec: float = 0.25
    same_domain_only: bool = True
    use_playwright: bool = True
    scroll_dynamic: bool = True
    output_dir: str = "output_markdown"


@dataclass
class PageResult:
    url: str
    title: str = ""
    markdown: str = ""
    media: Dict[str, List[str]] = field(default_factory=lambda: {"images": [], "videos": [], "audio": []})
    links: List[str] = field(default_factory=list)
    raw_html_len: int = 0
    pruned_html_len: int = 0
    fetched_with: str = "requests"


def normalize_url(base: str, href: str) -> Optional[str]:
    if not href:
        return None
    abs_url = urljoin(base, href.strip())
    abs_url, _frag = urldefrag(abs_url)
    p = urlparse(abs_url)
    if p.scheme not in {"http", "https"}:
        return None
    return abs_url


def same_domain(a: str, b: str) -> bool:
    return urlparse(a).netloc.lower() == urlparse(b).netloc.lower()


def looks_like_pagination(url: str, text: str) -> bool:
    t = (text or "").strip().lower()
    if t in PAGINATION_TEXT:
        return True
    if re.fullmatch(r"\d{1,4}", t):
        return True
    if re.search(r"(page=|/page/)\d+", url, flags=re.I):
        return True
    return False


def fetch_static(url: str, timeout_sec: int) -> Tuple[str, str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; ContentExtractorBot/1.0; +https://example.org/bot)"
    }
    r = requests.get(url, headers=headers, timeout=timeout_sec)
    r.raise_for_status()
    return r.text, r.url


async def fetch_dynamic(url: str, cfg: CrawlConfig) -> Tuple[str, str]:
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError("Playwright not installed.")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=cfg.render_timeout_sec * 1000)

        # Wait a bit for JS hydration
        await page.wait_for_timeout(1200)

        if cfg.scroll_dynamic:
            for _ in range(6):
                await page.mouse.wheel(0, 4000)
                await page.wait_for_timeout(600)

        # Additional network settle
        await page.wait_for_timeout(800)

        html = await page.content()
        final_url = page.url
        await context.close()
        await browser.close()
        return html, final_url


def prune_html(raw_html: str, keep_script_code: bool = False) -> str:
    if not raw_html:
        return ""
    soup = BeautifulSoup(raw_html, "lxml")

    # Remove comments
    for c in soup.find_all(string=lambda x: isinstance(x, Comment)):
        c.extract()

    # Remove invisible tags
    remove_tags = set(INVISIBLE_TAGS)
    if DROP_SCRIPT_BY_DEFAULT and not keep_script_code:
        remove_tags.add("script")

    for tag_name in remove_tags:
        for el in soup.find_all(tag_name):
            el.decompose()

    # Remove likely garbage blocks by class/id hints
    def is_garbage(tag):
        cls = " ".join(tag.get("class", [])).lower() if tag.has_attr("class") else ""
        tid = tag.get("id", "").lower()
        hay = f"{cls} {tid}"
        return any(h in hay for h in GARBAGE_HINTS)

    for el in soup.find_all(is_garbage):
        el.decompose()

    # Strip noisy attrs
    for tag in soup.find_all(True):
        for a in list(tag.attrs.keys()):
            if a in STRIP_ATTRS or a.startswith("data-") or a.startswith("aria-"):
                del tag.attrs[a]

    # Remove empty non-content wrappers
    for tag in soup.find_all(True):
        if tag.name in {"img", "video", "audio", "source", "br", "hr"}:
            continue
        if tag.name not in CONTENT_TAGS and not tag.get_text(strip=True) and not tag.find(["img", "video", "audio", "table", "pre", "code"]):
            tag.decompose()

    out = str(soup)
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out.strip()


def extract_main_html(pruned_html: str) -> str:
    if not pruned_html:
        return ""
    # readability-lxml gives better article/main extraction on many sites
    doc = Document(pruned_html)
    main_html = doc.summary(html_partial=True)
    # fallback if too small
    if len(main_html) < 400:
        return pruned_html
    return main_html


def extract_media_and_links(soup: BeautifulSoup, base_url: str):
    images, videos, audio, links = [], [], [], []

    for img in soup.find_all("img"):
        src = normalize_url(base_url, img.get("src", ""))
        if src:
            images.append(src)
        srcset = img.get("srcset", "")
        if srcset:
            for part in srcset.split(","):
                u = part.strip().split(" ")[0]
                uu = normalize_url(base_url, u)
                if uu:
                    images.append(uu)

    for tag, bucket in [("video", videos), ("audio", audio)]:
        for m in soup.find_all(tag):
            src = normalize_url(base_url, m.get("src", ""))
            if src:
                bucket.append(src)
            for s in m.find_all("source"):
                ssrc = normalize_url(base_url, s.get("src", ""))
                if ssrc:
                    bucket.append(ssrc)

    for a in soup.find_all("a"):
        href = normalize_url(base_url, a.get("href", ""))
        if href:
            links.append(href)

    dedup = lambda xs: sorted(set(xs))
    return {"images": dedup(images), "videos": dedup(videos), "audio": dedup(audio)}, dedup(links)


def html_to_markdown(main_html: str, base_url: str) -> str:
    # markdownify keeps tables/code reasonably; heading style ATX for cleaner output.
    text_md = md(
        main_html,
        heading_style="ATX",
        bullets="-",
        strip=["span"],  # keep code/pre/table/etc.
    )
    # Compact extra blank lines
    text_md = re.sub(r"\n{3,}", "\n\n", text_md).strip()
    return text_md


def find_next_links(pruned_html: str, page_url: str, root_url: str, cfg: CrawlConfig) -> List[str]:
    soup = BeautifulSoup(pruned_html, "lxml")
    out = []
    for a in soup.find_all("a"):
        href = normalize_url(page_url, a.get("href", ""))
        if not href:
            continue
        if cfg.same_domain_only and not same_domain(href, root_url):
            continue

        txt = a.get_text(" ", strip=True)
        if looks_like_pagination(href, txt):
            out.append(href)

    # Also include likely content links (internal)
    for a in soup.find_all("a"):
        href = normalize_url(page_url, a.get("href", ""))
        if not href:
            continue
        if cfg.same_domain_only and not same_domain(href, root_url):
            continue
        # Avoid obvious static assets
        if re.search(r"\.(jpg|jpeg|png|gif|svg|pdf|zip|css|js)$", href, flags=re.I):
            continue
        out.append(href)

    return list(dict.fromkeys(out))  # ordered unique


async def fetch_page(url: str, cfg: CrawlConfig) -> Tuple[str, str, str]:
    # returns html, final_url, fetched_with
    # try requests first
    try:
        html, final_url = fetch_static(url, cfg.timeout_sec)
        # heuristic: if HTML too thin and Playwright enabled -> dynamic render
        if cfg.use_playwright and len(html) < 2000 and PLAYWRIGHT_AVAILABLE:
            dhtml, durl = await fetch_dynamic(url, cfg)
            if len(dhtml) > len(html) * 1.3:
                return dhtml, durl, "playwright"
        return html, final_url, "requests"
    except Exception:
        if cfg.use_playwright and PLAYWRIGHT_AVAILABLE:
            dhtml, durl = await fetch_dynamic(url, cfg)
            return dhtml, durl, "playwright"
        raise


async def crawl_and_extract(start_url: str, cfg: CrawlConfig) -> List[PageResult]:
    os.makedirs(cfg.output_dir, exist_ok=True)
    visited: Set[str] = set()
    queue: List[Tuple[str, int]] = [(start_url, 0)]
    results: List[PageResult] = []

    while queue and len(visited) < cfg.max_pages:
        url, depth = queue.pop(0)
        if url in visited:
            continue
        if depth > cfg.max_depth:
            continue

        visited.add(url)
        try:
            html, final_url, fetched_with = await fetch_page(url, cfg)
        except Exception as e:
            print(f"[WARN] Failed fetch: {url} ({e})")
            continue

        raw_len = len(html)
        pruned = prune_html(html, keep_script_code=False)
        pruned_len = len(pruned)
        main_html = extract_main_html(pruned)

        soup_main = BeautifulSoup(main_html, "lxml")
        title = soup_main.title.get_text(strip=True) if soup_main.title else ""
        media, links = extract_media_and_links(soup_main, final_url)
        markdown = html_to_markdown(main_html, final_url)

        # Append media sections
        if media["images"] or media["videos"] or media["audio"]:
            markdown += "\n\n## Media URLs\n"
            if media["images"]:
                markdown += "\n### Images\n" + "\n".join(f"- {u}" for u in media["images"])
            if media["videos"]:
                markdown += "\n\n### Videos\n" + "\n".join(f"- {u}" for u in media["videos"])
            if media["audio"]:
                markdown += "\n\n### Audio\n" + "\n".join(f"- {u}" for u in media["audio"])

        res = PageResult(
            url=final_url,
            title=title or final_url,
            markdown=markdown.strip(),
            media=media,
            links=links,
            raw_html_len=raw_len,
            pruned_html_len=pruned_len,
            fetched_with=fetched_with,
        )
        results.append(res)

        # enqueue more links
        next_links = find_next_links(pruned, final_url, start_url, cfg)
        for n in next_links:
            if n not in visited:
                queue.append((n, depth + 1))

        time.sleep(cfg.delay_sec)

    return results


def save_outputs(results: List[PageResult], out_dir: str):
    os.makedirs(out_dir, exist_ok=True)
    manifest = []
    combined_md_parts = []

    for i, r in enumerate(results, 1):
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", urlparse(r.url).path.strip("/"))[:80].strip("-") or "root"
        fname = f"{i:03d}_{slug}.md"
        fpath = os.path.join(out_dir, fname)

        page_header = f"# {r.title}\n\nSource: {r.url}\n\n"
        content = page_header + r.markdown + "\n"
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(content)

        combined_md_parts.append(content)
        manifest.append({
            "index": i,
            "url": r.url,
            "title": r.title,
            "file": fname,
            "fetched_with": r.fetched_with,
            "raw_html_len": r.raw_html_len,
            "pruned_html_len": r.pruned_html_len,
            "images": len(r.media["images"]),
            "videos": len(r.media["videos"]),
            "audio": len(r.media["audio"]),
            "links": len(r.links),
        })

    with open(os.path.join(out_dir, "combined.md"), "w", encoding="utf-8") as f:
        f.write("\n\n---\n\n".join(combined_md_parts))

    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)


def parse_args():
    p = argparse.ArgumentParser(description="Crawl website URL(s), prune HTML, extract Markdown + media URLs.")
    p.add_argument("url", help="Start URL")
    p.add_argument("--max-pages", type=int, default=40)
    p.add_argument("--max-depth", type=int, default=2)
    p.add_argument("--timeout", type=int, default=20)
    p.add_argument("--render-timeout", type=int, default=35)
    p.add_argument("--no-playwright", action="store_true")
    p.add_argument("--no-same-domain", action="store_true")
    p.add_argument("--out", default="output_markdown")
    return p.parse_args()


async def main():
    args = parse_args()
    cfg = CrawlConfig(
        max_pages=args.max_pages,
        max_depth=args.max_depth,
        timeout_sec=args.timeout,
        render_timeout_sec=args.render_timeout,
        use_playwright=not args.no_playwright,
        same_domain_only=not args.no_same_domain,
        output_dir=args.out,
    )

    print(f"Starting crawl: {args.url}")
    print(f"Playwright available: {PLAYWRIGHT_AVAILABLE}, enabled: {cfg.use_playwright}")
    results = await crawl_and_extract(args.url, cfg)
    save_outputs(results, cfg.output_dir)
    print(f"Done. Pages extracted: {len(results)}")
    print(f"Output dir: {cfg.output_dir}")


if __name__ == "__main__":
    asyncio.run(main())
