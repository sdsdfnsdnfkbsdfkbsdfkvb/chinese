# Website Downloader for Pydroid 3 (Android)

A single-file Python script that downloads a **complete website** to your
phone for offline browsing — every page, every image, stylesheet, script,
font, video and icon — and rewrites all the links so the saved copy works
without internet.

It uses **only the Python standard library**, so there is **nothing to
pip-install**. It runs as-is in Pydroid 3.

## How to use it in Pydroid 3

1. Install **Pydroid 3** from the Play Store (the free version is enough).
2. Copy `website_downloader.py` onto your phone (Downloads folder is fine).
3. Open the file in Pydroid 3 and tap the yellow **▶ Run** button.
4. Type the website URL when asked, e.g. `https://example.com`, and press
   Enter. You can also set a page limit (default 500).
5. Wait. The script prints every page as it crawls.
6. When it finishes, it prints the location of the mirror, e.g.:

   ```
   /storage/emulated/0/Download/website_mirror/example.com/index.html
   ```

7. Open that `index.html` with any browser or HTML viewer on your phone
   (use a file manager like Files by Google → tap the file → open with
   Chrome). All pages and images work offline.

> **Storage permission:** the first time, allow Pydroid 3 to access
> storage (Android Settings → Apps → Pydroid 3 → Permissions → Files).
> If the Download folder isn't writable, the mirror is saved next to the
> script instead.

### Terminal usage (optional)

From Pydroid's built-in terminal you can pass everything as arguments:

```bash
python website_downloader.py https://example.com
python website_downloader.py https://example.com /storage/emulated/0/Download/mysite
python website_downloader.py https://example.com --max-pages 2000 --threads 12 --delay 0.5
```

| Option | Meaning | Default |
|---|---|---|
| `--max-pages N` | stop after crawling N pages | 500 |
| `--threads N` | parallel asset downloads per page | 8 |
| `--delay S` | pause S seconds between pages (be gentle to small servers) | 0 |

## What it downloads

- Every internal page reachable by links from the start URL (breadth-first
  crawl of the whole site, including `www.` / bare-domain variants).
- All assets each page uses: `<img>` (including `srcset` and lazy-load
  `data-src`), CSS files, JavaScript files, fonts, favicons, videos,
  audio, iframes, `<object>`/`<embed>` content.
- Resources referenced *inside* CSS (`url(...)`, `@import`), recursively —
  so background images and web fonts work offline.
- Assets hosted on other domains (CDNs) are saved under `_external/`.

All links in the saved HTML and CSS are rewritten to relative local paths,
so the mirror browses exactly like the live site. External page links are
left pointing at the live web.

## Limitations (honest notes)

- **JavaScript-rendered sites** (React/Vue/Angular single-page apps) ship
  an almost-empty HTML shell and build the visible content in the browser.
  A crawler can't execute that JavaScript, so for those sites you get the
  shell + all files, but not the generated text. Server-rendered/classic
  sites mirror perfectly.
- **Login-protected pages** can't be fetched (no cookies/session support).
- Content behind search forms or infinite scroll is only found if a normal
  `<a href>` link points to it.
- Interrupting with Ctrl-C (or Pydroid's stop button) is safe — everything
  downloaded so far stays on disk.

## Legal / etiquette

Only mirror sites you're allowed to copy. For sites that aren't yours, use
a sensible `--max-pages` and add `--delay 1` so you don't hammer the
server.
