#!/usr/bin/env python3
"""
website_downloader.py — full website mirror tool for Pydroid 3 (Android)

Downloads an entire website for offline browsing:
  * every reachable page on the same site (BFS crawl of all internal links)
  * every asset each page needs: images, CSS, JS, fonts, video/audio,
    favicons, srcset variants, CSS url(...) / @import references, iframes
  * rewrites all links so the saved copy works completely offline

Uses ONLY the Python standard library — nothing to pip-install, so it runs
out of the box in Pydroid 3.

Usage (in Pydroid):
    1. Open this file in Pydroid 3 and press Run, then type the URL when
       asked,  — or —
    2. Run from the Pydroid terminal:
           python website_downloader.py https://example.com

Options:
    python website_downloader.py URL [output_dir] [--max-pages N]
                                     [--threads N] [--delay SECONDS]

The mirror is saved to  <output_dir>/<hostname>/  and you can open
index.html in any Android browser (via a file manager or "file://" URL).

NOTE: sites that build their content with JavaScript (React/Vue single-page
apps) only ship an empty HTML shell; a crawler without a browser engine
cannot execute that JavaScript, so for those sites you will get the shell
plus all linked files, but not the JS-generated text. Classic/server-
rendered sites mirror perfectly.
"""

import hashlib
import os
import posixpath
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser

# ---------------------------------------------------------------- settings

USER_AGENT = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36")
TIMEOUT = 25          # seconds per request
RETRIES = 3           # attempts per URL
DEFAULT_MAX_PAGES = 500
DEFAULT_THREADS = 8   # parallel asset downloads
DEFAULT_DELAY = 0.0   # polite pause between page fetches (seconds)

# attributes that can contain a URL, per tag ('*' = any tag)
URL_ATTRS = {
    "a":      ["href"],
    "link":   ["href"],
    "script": ["src"],
    "img":    ["src", "srcset", "data-src", "data-srcset", "data-original"],
    "source": ["src", "srcset"],
    "video":  ["src", "poster"],
    "audio":  ["src"],
    "iframe": ["src"],
    "embed":  ["src"],
    "object": ["data"],
    "track":  ["src"],
    "input":  ["src"],          # <input type=image>
    "form":   ["action"],
    "area":   ["href"],
    "*":      ["background"],
}

PAGE_EXTENSIONS = {"", ".html", ".htm", ".php", ".asp", ".aspx", ".jsp",
                   ".cgi", ".shtml", ".xhtml"}

CSS_URL_RE = re.compile(r"""url\(\s*['"]?([^'")]+)['"]?\s*\)""", re.I)
CSS_IMPORT_RE = re.compile(r"""@import\s+['"]([^'"]+)['"]""", re.I)
SRCSET_SPLIT_RE = re.compile(r",\s*(?=[^,]*(?:\s+[\d.]+[wx])?\s*$)")

SSL_CTX = ssl.create_default_context()


# ---------------------------------------------------------------- helpers

def log(msg):
    print(msg, flush=True)


def fetch(url):
    """Download url, return (bytes, final_url, content_type) or None."""
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
                "Accept-Language": "en,*;q=0.5",
            })
            with urllib.request.urlopen(req, timeout=TIMEOUT,
                                        context=SSL_CTX) as resp:
                ctype = resp.headers.get("Content-Type", "").lower()
                return resp.read(), resp.geturl(), ctype
        except (urllib.error.HTTPError,) as e:
            if e.code in (401, 403, 404, 410):
                log("    !! HTTP %s  %s" % (e.code, url))
                return None
            if attempt == RETRIES:
                log("    !! HTTP %s  %s" % (e.code, url))
        except Exception as e:
            if attempt == RETRIES:
                log("    !! %s  %s" % (e.__class__.__name__, url))
        time.sleep(1.5 * attempt)
    return None


def normalize(url):
    """Strip #fragment and normalise for de-duplication."""
    url, _ = urllib.parse.urldefrag(url)
    parts = urllib.parse.urlsplit(url)
    path = parts.path or "/"
    return urllib.parse.urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def is_http(url):
    return url.startswith("http://") or url.startswith("https://")


def looks_like_page(url, ctype=None):
    """Heuristic: should this URL be crawled as an HTML page?"""
    if ctype is not None:
        return "text/html" in ctype or "application/xhtml" in ctype
    path = urllib.parse.urlsplit(url).path
    ext = posixpath.splitext(path)[1].lower()
    return ext in PAGE_EXTENSIONS


def safe_component(name, maxlen=80):
    """Make a single path component safe for Android storage."""
    name = urllib.parse.unquote(name)
    name = re.sub(r'[<>:"\\|?*\x00-\x1f]', "_", name).strip(". ")
    if len(name) > maxlen:
        root, ext = os.path.splitext(name)
        digest = hashlib.md5(name.encode("utf-8", "ignore")).hexdigest()[:8]
        name = root[: maxlen - len(ext) - 9] + "_" + digest + ext
    return name or "_"


def guess_ext(ctype):
    table = {
        "text/html": ".html", "application/xhtml": ".html",
        "text/css": ".css", "application/javascript": ".js",
        "text/javascript": ".js", "application/json": ".json",
        "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
        "image/webp": ".webp", "image/svg": ".svg", "image/x-icon": ".ico",
        "image/vnd.microsoft.icon": ".ico", "image/avif": ".avif",
        "font/woff2": ".woff2", "font/woff": ".woff", "font/ttf": ".ttf",
        "application/font-woff": ".woff", "application/pdf": ".pdf",
        "video/mp4": ".mp4", "video/webm": ".webm",
        "audio/mpeg": ".mp3", "audio/ogg": ".ogg",
        "text/xml": ".xml", "application/xml": ".xml",
    }
    for key, ext in table.items():
        if key in ctype:
            return ext
    return ""


class LinkParser(HTMLParser):
    """Collects every URL reference in an HTML document."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.refs = []          # (raw_url, kind)  kind: 'page' | 'asset'
        self.base = None
        self.style_chunks = []
        self._in_style = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "base" and attrs.get("href"):
            self.base = attrs["href"]
            return
        if tag == "style":
            self._in_style = True

        for attr in URL_ATTRS.get(tag, []) + URL_ATTRS["*"]:
            val = attrs.get(attr)
            if not val:
                continue
            if attr.endswith("srcset"):
                for cand in SRCSET_SPLIT_RE.split(val):
                    u = cand.strip().split(" ")[0]
                    if u:
                        self.refs.append((u, "asset"))
            elif tag in ("a", "area", "form"):
                self.refs.append((val, "page"))
            elif tag == "iframe":
                self.refs.append((val, "page"))
            elif tag == "link":
                rel = (attrs.get("rel") or "").lower()
                kind = "asset"
                if "alternate" in rel and "stylesheet" not in rel:
                    kind = "page"
                self.refs.append((val, kind))
            else:
                self.refs.append((val, "asset"))

        style = attrs.get("style")
        if style:
            for m in CSS_URL_RE.finditer(style):
                self.refs.append((m.group(1), "asset"))

    def handle_endtag(self, tag):
        if tag == "style":
            self._in_style = False

    def handle_data(self, data):
        if self._in_style:
            self.style_chunks.append(data)


# ---------------------------------------------------------------- mirror

class Mirror:
    def __init__(self, start_url, out_root, max_pages, threads, delay):
        data = fetch(start_url)
        if data is None:
            raise SystemExit("Could not reach %s — check the URL and your "
                             "connection." % start_url)
        self._first = data
        self.start_url = normalize(data[1])
        parts = urllib.parse.urlsplit(self.start_url)
        self.host = parts.netloc
        # treat www.example.com and example.com as the same site
        self.bare_host = self.host[4:] if self.host.startswith("www.") else self.host
        self.root = os.path.join(out_root, safe_component(self.host))
        self.max_pages = max_pages
        self.threads = threads
        self.delay = delay
        self.local_path = {}     # normalized url -> path relative to root
        self.queue = deque()
        self.done_pages = set()
        self.failed = []

    # -------------------------------------------------- path mapping

    def same_site(self, url):
        host = urllib.parse.urlsplit(url).netloc.lower()
        bare = host[4:] if host.startswith("www.") else host
        return bare == self.bare_host

    def path_for(self, url, ctype=""):
        """Map a URL to a local relative file path (stable per URL)."""
        url = normalize(url)
        if url in self.local_path:
            return self.local_path[url]

        parts = urllib.parse.urlsplit(url)
        path = parts.path
        if path.endswith("/") or path == "":
            path += "index.html"

        comps = [safe_component(c) for c in path.split("/") if c]
        if not self.same_site(url):
            comps = ["_external", safe_component(parts.netloc)] + comps

        fname = comps[-1]
        root, ext = os.path.splitext(fname)
        if parts.query:
            digest = hashlib.md5(parts.query.encode()).hexdigest()[:8]
            fname = "%s_q%s%s" % (root, digest, ext)
            root, ext = os.path.splitext(fname)
        if not ext:
            guessed = guess_ext(ctype) or (".html" if looks_like_page(url) else ".bin")
            fname = root + guessed
        elif ("text/html" in ctype and ext.lower() not in
              (".html", ".htm", ".xhtml")):
            fname = fname + ".html"
        comps[-1] = fname

        rel = "/".join(comps)
        self.local_path[url] = rel
        return rel

    def save(self, rel, payload):
        full = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        mode = "wb" if isinstance(payload, bytes) else "w"
        kwargs = {} if isinstance(payload, bytes) else {"encoding": "utf-8"}
        with open(full, mode, **kwargs) as fh:
            fh.write(payload)

    def relative_link(self, from_rel, to_rel):
        rel = posixpath.relpath(to_rel, posixpath.dirname(from_rel) or ".")
        return urllib.parse.quote(rel)

    # -------------------------------------------------- css handling

    def process_css(self, css_text, css_url, css_rel, asset_jobs):
        """Rewrite url()/@import in CSS, queueing referenced files."""
        def sub_url(m):
            raw = m.group(1).strip()
            return "url(%s)" % self._css_target(raw, css_url, css_rel,
                                                asset_jobs)

        def sub_import(m):
            raw = m.group(1).strip()
            return '@import "%s"' % self._css_target(raw, css_url, css_rel,
                                                     asset_jobs)

        css_text = CSS_IMPORT_RE.sub(sub_import, css_text)
        return CSS_URL_RE.sub(sub_url, css_text)

    def _css_target(self, raw, base_url, css_rel, asset_jobs):
        if raw.startswith("data:") or raw.startswith("#"):
            return raw
        abs_url = urllib.parse.urljoin(base_url, raw)
        if not is_http(abs_url):
            return raw
        target_rel = self.path_for(abs_url)
        asset_jobs.add(normalize(abs_url))
        return self.relative_link(css_rel, target_rel)

    # -------------------------------------------------- assets

    def download_asset(self, url):
        rel = self.path_for(url)
        full = os.path.join(self.root, *rel.split("/"))
        if os.path.exists(full):
            return
        got = fetch(url)
        if got is None:
            self.failed.append(url)
            return
        payload, final_url, ctype = got
        rel = self.path_for(url, ctype)   # ext may improve with real ctype

        if "text/css" in ctype or rel.endswith(".css"):
            nested = set()
            text = payload.decode("utf-8", "replace")
            text = self.process_css(text, final_url, rel, nested)
            self.save(rel, text)
            for n in nested:                 # fonts/images inside CSS
                self.download_asset(n)
        else:
            self.save(rel, payload)

    # -------------------------------------------------- pages

    def process_page(self, url):
        url = normalize(url)
        if url in self.done_pages:
            return
        self.done_pages.add(url)

        if self._first is not None and url == self.start_url:
            got, self._first = self._first, None
        else:
            got = fetch(url)
        if got is None:
            self.failed.append(url)
            return
        payload, final_url, ctype = got

        if not looks_like_page(url, ctype):        # actually a file
            rel = self.path_for(url, ctype)
            self.save(rel, payload)
            return

        page_rel = self.path_for(url, ctype)
        log("[page %d/%d] %s" % (len(self.done_pages),
                                 self.max_pages, url))

        html = payload.decode("utf-8", "replace")
        parser = LinkParser()
        try:
            parser.feed(html)
        except Exception:
            pass
        base_url = urllib.parse.urljoin(final_url, parser.base or "")

        asset_jobs = set()
        replacements = {}

        for raw, kind in parser.refs:
            raw = raw.strip()
            if (not raw or raw.startswith("#") or raw.startswith("data:")
                    or raw.startswith("javascript:") or raw.startswith("mailto:")
                    or raw.startswith("tel:") or raw.startswith("blob:")):
                continue
            abs_url = urllib.parse.urljoin(base_url, raw)
            if not is_http(abs_url):
                continue
            abs_norm = normalize(abs_url)

            if kind == "page" and self.same_site(abs_url) \
                    and looks_like_page(abs_url):
                if abs_norm not in self.done_pages \
                        and abs_norm not in self.queue_set \
                        and len(self.done_pages) + len(self.queue) < self.max_pages:
                    self.queue.append(abs_norm)
                    self.queue_set.add(abs_norm)
                target_rel = self.path_for(abs_norm, "text/html")
            elif kind == "page" and not self.same_site(abs_url):
                continue                       # keep external links live
            else:
                target_rel = self.path_for(abs_norm)
                asset_jobs.add(abs_norm)

            frag = urllib.parse.urlsplit(raw).fragment
            local = self.relative_link(page_rel, target_rel)
            if frag:
                local += "#" + frag
            replacements[raw] = local

        # inline <style> blocks
        for chunk in parser.style_chunks:
            fixed = self.process_css(chunk, base_url, page_rel, asset_jobs)
            if fixed != chunk:
                html = html.replace(chunk, fixed)

        # rewrite attribute URLs (longest first so substrings don't clash)
        for raw in sorted(replacements, key=len, reverse=True):
            local = replacements[raw]
            for quote in ('"', "'"):
                html = html.replace(quote + raw + quote,
                                    quote + local + quote)

        self.save(page_rel, html)

        # fetch this page's assets in parallel
        if asset_jobs:
            with ThreadPoolExecutor(max_workers=self.threads) as pool:
                futures = [pool.submit(self.download_asset, a)
                           for a in asset_jobs]
                for f in as_completed(futures):
                    f.result()

    # -------------------------------------------------- main loop

    def run(self):
        log("")
        log("Mirroring   : %s" % self.start_url)
        log("Saving to   : %s" % self.root)
        log("Page limit  : %d   threads: %d" % (self.max_pages, self.threads))
        log("-" * 56)
        started = time.time()

        self.queue_set = set()
        self.queue.append(self.start_url)
        self.queue_set.add(self.start_url)

        while self.queue:
            url = self.queue.popleft()
            self.queue_set.discard(url)
            self.process_page(url)
            if self.delay:
                time.sleep(self.delay)

        # make sure there is an index.html at the top for easy opening
        start_rel = self.local_path.get(self.start_url)
        top_index = os.path.join(self.root, "index.html")
        if start_rel and start_rel != "index.html" \
                and not os.path.exists(top_index):
            target = urllib.parse.quote(start_rel)
            self.save("index.html",
                      '<meta http-equiv="refresh" content="0; url=%s">'
                      % target)

        mins = (time.time() - started) / 60
        log("-" * 56)
        log("DONE in %.1f min  —  %d pages, %d files total"
            % (mins, len(self.done_pages),
               sum(len(f) for _, _, f in os.walk(self.root))))
        if self.failed:
            log("%d URLs failed (kept going):" % len(self.failed))
            for u in self.failed[:20]:
                log("   " + u)
            if len(self.failed) > 20:
                log("   ... and %d more" % (len(self.failed) - 20))
        log("")
        log("Open this file in your browser or HTML viewer:")
        log("   %s" % os.path.join(self.root, "index.html"))


# ---------------------------------------------------------------- entry

def default_output_dir():
    """Prefer the Android Download folder when it is writable."""
    android_dl = "/storage/emulated/0/Download"
    if os.path.isdir(android_dl) and os.access(android_dl, os.W_OK):
        return os.path.join(android_dl, "website_mirror")
    return os.path.join(os.getcwd(), "website_mirror")


def parse_args(argv):
    url = None
    out = None
    max_pages = DEFAULT_MAX_PAGES
    threads = DEFAULT_THREADS
    delay = DEFAULT_DELAY

    args = list(argv)
    while args:
        a = args.pop(0)
        if a == "--max-pages":
            max_pages = int(args.pop(0))
        elif a == "--threads":
            threads = max(1, int(args.pop(0)))
        elif a == "--delay":
            delay = float(args.pop(0))
        elif url is None:
            url = a
        elif out is None:
            out = a

    if url is None:                      # interactive mode for Pydroid "Run"
        log("=" * 56)
        log(" Website downloader for Pydroid 3")
        log("=" * 56)
        url = input("Enter the website URL: ").strip()
        limit = input("Max pages to crawl [%d]: " % max_pages).strip()
        if limit.isdigit():
            max_pages = int(limit)

    if not url:
        raise SystemExit("No URL given.")
    if not is_http(url):
        url = "https://" + url
    return url, out or default_output_dir(), max_pages, threads, delay


def main():
    url, out, max_pages, threads, delay = parse_args(sys.argv[1:])
    Mirror(url, out, max_pages, threads, delay).run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\nStopped by user — everything downloaded so far is saved.")
