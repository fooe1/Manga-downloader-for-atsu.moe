#!/usr/bin/env python3
"""
Manga Downloader – Web GUI
--------------------------
Run:  python app.py
Then open: http://localhost:7337
"""

import re
import json
import time
import uuid
import threading
import requests as req
from pathlib import Path
from urllib.parse import urlparse
from flask import Flask, request, jsonify, Response

from downloader import (
    get_chapter_list, collect_image_urls, download_images,
    sanitize, detect_url_type, make_browser_context, HEADERS
)

app = Flask(__name__)

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

def job_update(job_id: str, **kwargs):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(kwargs)


def get_manga_cover(series_url: str) -> str | None:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    try:
        with sync_playwright() as pw:
            browser, context = make_browser_context(pw)
            page = context.new_page()
            try:
                page.goto(series_url, wait_until="domcontentloaded", timeout=20_000)
            except PWTimeout:
                pass
            cover_url: str = page.evaluate("""
            () => {
                const og = document.querySelector('meta[property="og:image"]');
                if (og && og.content) return og.content;
                const imgs = [...document.querySelectorAll('img[src]')];
                for (const img of imgs) {
                    const src = img.src || '';
                    if (!src || src.startsWith('data:')) continue;
                    const lower = src.toLowerCase();
                    if (lower.includes('icon') || lower.includes('logo') ||
                        lower.includes('avatar') || lower.includes('favicon')) continue;
                    if (img.naturalWidth > 100 || img.width > 100) return src;
                }
                return '';
            }
            """)
            browser.close()
            return cover_url or None
    except Exception:
        return None


@app.route("/api/fetch", methods=["POST"])
def api_fetch():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip().strip("'\"")
    if not url:
        return jsonify(error="No URL provided"), 400
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    url = url.split("#")[0]

    if detect_url_type(url) != "series":
        return jsonify(error="Please paste a series URL (atsu.moe/manga/...)"), 400

    try:
        cover_result = [None]
        def fetch_cover():
            cover_result[0] = get_manga_cover(url)

        cover_thread = threading.Thread(target=fetch_cover, daemon=True)
        cover_thread.start()
        manga_title, chapters = get_chapter_list(url)
        cover_thread.join(timeout=15)

        if not chapters:
            return jsonify(error="No chapters found. Check the URL."), 404

        cover_url = cover_result[0] or ""
        # Only save to library on a successful fetch
        _add_to_library(url, manga_title, cover_url, len(chapters))

        return jsonify(
            title=manga_title,
            cover=cover_url,
            chapters=[{"id": ch["id"], "title": ch["title"], "url": ch["url"]}
                      for ch in chapters]
        )
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json(silent=True) or {}
    chapter_url   = data.get("chapter_url", "").strip()
    chapter_title = data.get("chapter_title", "Chapter")
    manga_title   = data.get("manga_title", "manga")
    chapter_index = int(data.get("chapter_index", 1))

    if not chapter_url:
        return jsonify(error="No chapter URL"), 400

    job_id = str(uuid.uuid4())[:8]
    with _jobs_lock:
        _jobs[job_id] = {
            "status":   "starting",
            "progress": 0,
            "total":    0,
            "message":  "Starting…",
            "done":     False,
            "error":    None,
        }

    def run_download():
        try:
            job_update(job_id, status="scraping", message="Opening chapter page…")
            image_urls = collect_image_urls(chapter_url)
            if not image_urls:
                job_update(job_id, status="error", message="No images found.",
                           error="No images found.", done=True)
                return

            total = len(image_urls)
            job_update(job_id, status="downloading", total=total,
                       message=f"Downloading {total} images…")

            folder_name = f"{chapter_index:03d} - {sanitize(chapter_title)}"
            out_dir = Path("downloads") / sanitize(manga_title) / folder_name
            out_dir.mkdir(parents=True, exist_ok=True)

            import random
            from concurrent.futures import ThreadPoolExecutor, as_completed
            from downloader import _download_one, CONCURRENT_DOWNLOADS

            pad = len(str(total))
            completed = [0]

            def tracked_download(idx, url):
                result = _download_one(idx, total, url, out_dir, chapter_url, pad)
                completed[0] += 1
                job_update(job_id,
                           progress=completed[0],
                           message=f"Downloaded {completed[0]}/{total} images")
                return result

            success = 0
            with ThreadPoolExecutor(max_workers=CONCURRENT_DOWNLOADS) as pool:
                futures = {
                    pool.submit(tracked_download, idx, url): idx
                    for idx, url in enumerate(image_urls, start=1)
                }
                for future in as_completed(futures):
                    if future.result():
                        success += 1

            job_update(job_id,
                       status="done",
                       progress=total,
                       message=f"Saved {success}/{total} images to '{out_dir}'",
                       done=True)

        except Exception as e:
            job_update(job_id, status="error", message=str(e),
                       error=str(e), done=True)

    threading.Thread(target=run_download, daemon=True).start()
    return jsonify(job_id=job_id)


@app.route("/api/progress/<job_id>")
def api_progress(job_id: str):
    def stream():
        while True:
            with _jobs_lock:
                job = _jobs.get(job_id)
            if job is None:
                yield f"data: {json.dumps({'error': 'Job not found'})}\n\n"
                break
            yield f"data: {json.dumps(job)}\n\n"
            if job.get("done"):
                break
            time.sleep(0.4)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/proxy-cover")
def proxy_cover():
    cover_url = request.args.get("url", "")
    if not cover_url:
        return "", 404
    try:
        r = req.get(cover_url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return Response(r.content,
                        content_type=r.headers.get("content-type", "image/jpeg"))
    except Exception:
        return "", 404


# ---------------------------------------------------------------------------
# Library – persisted as a JSON file on disk
# ---------------------------------------------------------------------------

LIBRARY_FILE = Path("library.json")
_lib_lock = threading.Lock()

def _load_library() -> list[dict]:
    with _lib_lock:
        if not LIBRARY_FILE.exists():
            return []
        try:
            return json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []

def _save_library(entries: list[dict]):
    with _lib_lock:
        LIBRARY_FILE.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

def _add_to_library(url: str, title: str, cover: str, chapter_count: int):
    entries = _load_library()
    # Update if already exists, otherwise prepend
    for e in entries:
        if e["url"] == url:
            e.update(title=title, cover=cover, chapter_count=chapter_count)
            _save_library(entries)
            return
    entries.insert(0, {
        "url":           url,
        "title":         title,
        "cover":         cover,
        "chapter_count": chapter_count,
    })
    _save_library(entries)


@app.route("/api/library", methods=["GET"])
def api_library_get():
    return jsonify(entries=_load_library())


@app.route("/api/library/remove", methods=["POST"])
def api_library_remove():
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify(error="No URL"), 400
    entries = [e for e in _load_library() if e["url"] != url]
    _save_library(entries)
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Manga Downloader</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:           #0d0d0f;
    --surface:      rgba(20,20,24,0.82);
    --border:       #252530;
    --accent:       #e8ff57;
    --accent2:      #57c8ff;
    --text:         #e8e8f0;
    --muted:        #6b6b80;
    --danger:       #ff5757;
    --success:      #57ffb0;
    --radius:       10px;
    --title-color:  #e8ff57;
    --btn-text:     #0d0d0f;
    --fetch-bg:     #e8ff57;
    --menu-tint:    20,20,24;
    --menu-alpha:   0.82;
    --overlay:      0.55;
    --grid-opacity: 0.35;

    /* Library panel own theme */
    --lib-tint:       20,20,24;
    --lib-alpha:      0.95;
    --lib-title:      #e8ff57;
    --lib-text:       #e8e8f0;
    --lib-btn-bg:     #e8ff57;
    --lib-btn-text:   #0d0d0f;
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    background-size: cover;
    background-position: center;
    background-attachment: fixed;
    color: var(--text);
    font-family: 'Syne', sans-serif;
    min-height: 100vh;
    padding: 40px 24px 80px;
  }

  body::before {
    content: '';
    position: fixed; inset: 0;
    background-image:
      linear-gradient(var(--border) 1px, transparent 1px),
      linear-gradient(90deg, var(--border) 1px, transparent 1px);
    background-size: 48px 48px;
    opacity: var(--grid-opacity);
    pointer-events: none;
    z-index: 0;
    transition: opacity 0.4s;
  }

  body::after {
    content: '';
    position: fixed; inset: 0;
    background: rgba(0,0,0,var(--overlay));
    pointer-events: none;
    z-index: 0;
    transition: background 0.3s;
  }

  .wrap { position: relative; z-index: 1; max-width: 860px; margin: 0 auto; }

  /* ── Header ── */
  header {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    margin-bottom: 48px;
    flex-wrap: wrap;
    gap: 12px;
  }
  .header-left { display: flex; align-items: baseline; gap: 14px; }
  header h1 {
    font-size: 2rem;
    font-weight: 800;
    letter-spacing: -0.03em;
    color: var(--title-color);
    text-transform: uppercase;
    transition: color 0.3s;
    cursor: text;
    outline: none;
  }
  header h1:focus {
    border-bottom: 2px solid var(--title-color);
  }
  header span.subtitle {
    font-family: 'DM Mono', monospace;
    font-size: 0.75rem;
    color: var(--muted);
    letter-spacing: 0.05em;
  }

  /* ── Customize button ── */
  .btn-customize {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--muted);
    font-family: 'Syne', sans-serif;
    font-size: 0.78rem;
    font-weight: 600;
    padding: 7px 16px;
    cursor: pointer;
    transition: border-color 0.15s, color 0.15s;
    letter-spacing: 0.03em;
  }
  .btn-customize:hover { border-color: var(--accent); color: var(--accent); }

  /* ── Customize panel ── */
  .customize-panel {
    display: none;
    background: rgba(var(--menu-tint), var(--menu-alpha));
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 24px;
    margin-bottom: 32px;
    gap: 28px;
    flex-wrap: wrap;
    backdrop-filter: blur(8px);
  }
  .customize-panel.open { display: flex; }

  .customize-section { display: flex; flex-direction: column; gap: 10px; min-width: 160px; }
  .customize-label {
    font-family: 'DM Mono', monospace;
    font-size: 0.72rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }
  .customize-section input[type="color"] {
    width: 48px; height: 32px;
    border: 1px solid var(--border);
    border-radius: 6px;
    cursor: pointer;
    background: none;
    padding: 2px;
  }
  .customize-section input[type="range"] {
    width: 140px;
    accent-color: var(--accent);
    cursor: pointer;
  }
  .slider-val {
    font-family: 'DM Mono', monospace;
    font-size: 0.72rem;
    color: var(--muted);
    min-width: 28px;
  }
  .slider-row { display: flex; align-items: center; gap: 10px; }

  .btn-bg-upload {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    font-family: 'Syne', sans-serif;
    font-size: 0.8rem;
    font-weight: 600;
    padding: 8px 16px;
    cursor: pointer;
    transition: border-color 0.15s;
    display: inline-block;
    text-align: center;
  }
  .btn-bg-upload:hover { border-color: var(--accent); }
  .btn-bg-clear {
    background: transparent;
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--muted);
    font-family: 'Syne', sans-serif;
    font-size: 0.75rem;
    padding: 6px 14px;
    cursor: pointer;
    transition: border-color 0.15s, color 0.15s;
  }
  .btn-bg-clear:hover { border-color: var(--danger); color: var(--danger); }
  .bg-filename {
    font-family: 'DM Mono', monospace;
    font-size: 0.72rem;
    color: var(--muted);
    margin-top: 2px;
    word-break: break-all;
  }

  /* ── Search bar ── */
  .search-bar { display: flex; gap: 10px; margin-bottom: 40px; }
  .search-bar input {
    flex: 1;
    background: rgba(var(--menu-tint), var(--menu-alpha));
    border: 1px solid var(--border);
    border-radius: var(--radius);
    color: var(--text);
    font-family: 'DM Mono', monospace;
    font-size: 0.9rem;
    padding: 14px 18px;
    outline: none;
    transition: border-color 0.2s;
    backdrop-filter: blur(8px);
  }
  .search-bar input::placeholder { color: var(--muted); }
  .search-bar input:focus { border-color: var(--accent); }
  .search-bar button {
    background: var(--fetch-bg);
    border: none;
    border-radius: var(--radius);
    color: var(--btn-text);
    font-family: 'Syne', sans-serif;
    font-size: 0.9rem;
    font-weight: 700;
    padding: 14px 28px;
    cursor: pointer;
    transition: opacity 0.15s, transform 0.1s;
    white-space: nowrap;
  }
  .search-bar button:hover { opacity: 0.88; }
  .search-bar button:active { transform: scale(0.97); }
  .search-bar button:disabled { opacity: 0.4; cursor: not-allowed; }

  /* ── Banner ── */
  .banner {
    background: rgba(var(--menu-tint), var(--menu-alpha));
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 14px 18px;
    font-family: 'DM Mono', monospace;
    font-size: 0.85rem;
    color: var(--muted);
    margin-bottom: 32px;
    display: none;
    backdrop-filter: blur(8px);
  }
  .banner.error { border-color: var(--danger); color: var(--danger); }
  .banner.visible { display: block; }

  /* ── Manga card ── */
  .manga-card {
    display: none;
    gap: 28px;
    margin-bottom: 36px;
    padding: 24px;
    background: rgba(var(--menu-tint), var(--menu-alpha));
    border: 1px solid var(--border);
    border-radius: 14px;
    backdrop-filter: blur(8px);
  }
  .manga-card.visible { display: flex; }
  .manga-card img {
    width: 120px; height: 170px;
    object-fit: cover;
    border-radius: 8px;
    flex-shrink: 0;
    background: var(--border);
  }
  .manga-card .info { flex: 1; display: flex; flex-direction: column; gap: 10px; }
  .manga-card .title {
    font-size: 1.5rem;
    font-weight: 800;
    letter-spacing: -0.02em;
    line-height: 1.2;
    color: var(--title-color);
    transition: color 0.3s;
  }
  .manga-card .meta {
    font-family: 'DM Mono', monospace;
    font-size: 0.8rem;
    color: var(--muted);
  }
  .manga-card .actions {
    display: flex; gap: 10px; flex-wrap: wrap;
    margin-top: 8px; align-items: center;
  }

  /* ── Range input ── */
  .range-wrap { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .range-label {
    font-family: 'DM Mono', monospace;
    font-size: 0.75rem; color: var(--muted); white-space: nowrap;
  }
  .range-input {
    background: rgba(var(--menu-tint), var(--menu-alpha));
    border: 1px solid var(--border);
    border-radius: 7px;
    color: var(--text);
    font-family: 'DM Mono', monospace;
    font-size: 0.82rem;
    padding: 6px 12px;
    width: 140px; outline: none;
    transition: border-color 0.2s;
  }
  .range-input::placeholder { color: var(--muted); }
  .range-input:focus { border-color: var(--accent); }
  .range-hint {
    font-family: 'DM Mono', monospace;
    font-size: 0.7rem; color: var(--muted); margin-top: 2px;
  }

  /* ── Action buttons ── */
  .btn-all {
    background: var(--fetch-bg);
    border: none; border-radius: 8px;
    color: var(--btn-text);
    font-family: 'Syne', sans-serif;
    font-size: 0.82rem; font-weight: 700;
    padding: 9px 20px; cursor: pointer;
    transition: opacity 0.15s; white-space: nowrap;
  }
  .btn-all:hover { opacity: 0.85; }
  .btn-all:disabled { opacity: 0.4; cursor: not-allowed; }

  .btn-range {
    background: transparent;
    border: 1px solid var(--accent2); border-radius: 8px;
    color: var(--accent2);
    font-family: 'Syne', sans-serif;
    font-size: 0.82rem; font-weight: 700;
    padding: 8px 18px; cursor: pointer;
    transition: background 0.15s, opacity 0.15s; white-space: nowrap;
  }
  .btn-range:hover { background: rgba(87,200,255,0.08); }
  .btn-range:disabled { opacity: 0.4; cursor: not-allowed; }

  /* ── Chapter header ── */
  .chapters-header {
    display: none; align-items: center;
    justify-content: space-between; margin-bottom: 14px;
  }
  .chapters-header.visible { display: flex; }
  .chapters-header h2 {
    font-size: 1rem; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted);
  }
  .chapters-header .count {
    font-family: 'DM Mono', monospace; font-size: 0.8rem; color: var(--muted);
    background: rgba(var(--menu-tint), var(--menu-alpha));
    border: 1px solid var(--border); border-radius: 20px; padding: 3px 12px;
  }

  /* ── Chapter rows ── */
  .chapter-list { display: flex; flex-direction: column; gap: 6px; }
  .chapter-row {
    display: flex; align-items: center; gap: 14px;
    padding: 12px 16px;
    background: rgba(var(--menu-tint), var(--menu-alpha));
    border: 1px solid var(--border);
    border-radius: var(--radius);
    transition: border-color 0.15s;
    backdrop-filter: blur(8px);
  }
  .chapter-row:hover { border-color: #383845; }
  .chapter-row.done  { border-color: #1a3328; }
  .chapter-row.error-row { border-color: #3d1a1a; }

  .chapter-num {
    font-family: 'DM Mono', monospace; font-size: 0.75rem; color: var(--muted);
    width: 32px; flex-shrink: 0; text-align: right;
  }
  .chapter-title { flex: 1; font-size: 0.92rem; font-weight: 600; }
  .chapter-status {
    font-family: 'DM Mono', monospace; font-size: 0.75rem; color: var(--muted);
    min-width: 120px; text-align: right;
  }
  .chapter-status.downloading { color: var(--accent2); }
  .chapter-status.done  { color: var(--success); }
  .chapter-status.error { color: var(--danger); }

  .chapter-progress {
    width: 80px; height: 4px; background: var(--border);
    border-radius: 2px; overflow: hidden; flex-shrink: 0; display: none;
  }
  .chapter-progress.visible { display: block; }
  .chapter-progress-fill {
    height: 100%; background: var(--accent2); border-radius: 2px;
    transition: width 0.3s ease; width: 0%;
  }
  .chapter-row.done .chapter-progress-fill { background: var(--success); }

  .btn-dl {
    background: transparent; border: 1px solid var(--border);
    border-radius: 7px; color: var(--text);
    font-family: 'Syne', sans-serif; font-size: 0.78rem; font-weight: 600;
    padding: 6px 14px; cursor: pointer;
    transition: border-color 0.15s, background 0.15s; white-space: nowrap;
  }
  .btn-dl:hover { border-color: var(--accent); color: var(--accent); }
  .btn-dl:disabled { opacity: 0.35; cursor: not-allowed; }
  .btn-dl.done-btn { border-color: var(--success); color: var(--success); opacity: 0.6; }

  /* ── Library panel ── */
  .library-overlay {
    display: none;
    position: fixed; inset: 0; z-index: 100;
    background: rgba(0,0,0,0.7);
    backdrop-filter: blur(4px);
    align-items: flex-start;
    justify-content: center;
    padding: 40px 20px;
    overflow-y: auto;
  }
  .library-overlay.open { display: flex; }

  .library-panel {
    background: rgba(var(--lib-tint), var(--lib-alpha));
    backdrop-filter: blur(12px);
    border: 1px solid var(--border);
    border-radius: 16px;
    width: 100%; max-width: 680px;
    padding: 28px;
    position: relative;
  }

  /* ── Library header row ── */
  .library-header-row {
    display: flex; align-items: center;
    justify-content: space-between;
    margin-bottom: 18px; gap: 10px; flex-wrap: wrap;
  }
  .library-panel h2 {
    font-size: 1.2rem; font-weight: 800;
    text-transform: uppercase; letter-spacing: 0.05em;
    color: var(--lib-title); margin: 0;
    transition: color 0.3s;
  }
  .library-header-actions { display: flex; gap: 8px; align-items: center; }
  .btn-lib-customize {
    background: transparent; border: 1px solid var(--border);
    border-radius: 6px; color: var(--muted);
    font-family: 'Syne', sans-serif; font-size: 0.72rem; font-weight: 600;
    padding: 5px 12px; cursor: pointer;
    transition: border-color 0.15s, color 0.15s;
  }
  .btn-lib-customize:hover { border-color: var(--lib-title); color: var(--lib-title); }
  .library-close {
    background: transparent; border: 1px solid var(--border);
    border-radius: 6px; color: var(--muted);
    font-size: 0.85rem; padding: 4px 10px; cursor: pointer;
    transition: border-color 0.15s, color 0.15s;
  }
  .library-close:hover { border-color: var(--danger); color: var(--danger); }

  /* ── Library customize sub-panel ── */
  .lib-customize-sub {
    display: none;
    background: rgba(0,0,0,0.25);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 18px 20px;
    margin-bottom: 18px;
    gap: 24px; flex-wrap: wrap;
  }
  .lib-customize-sub.open { display: flex; }
  .lib-customize-sub .customize-section { min-width: 140px; }

  /* ── Library entries ── */
  .library-empty {
    font-family: 'DM Mono', monospace; font-size: 0.85rem;
    color: var(--muted); text-align: center; padding: 32px 0;
  }
  .library-grid { display: flex; flex-direction: column; gap: 10px; }
  .library-entry {
    display: flex; align-items: center; gap: 14px;
    padding: 12px 14px;
    background: rgba(255,255,255,0.04);
    border: 1px solid var(--border); border-radius: 10px;
    cursor: pointer;
    transition: border-color 0.15s, background 0.15s;
  }
  .library-entry:hover { border-color: var(--lib-title); background: rgba(255,255,255,0.07); }
  .library-entry img {
    width: 44px; height: 62px; object-fit: cover;
    border-radius: 5px; background: var(--border); flex-shrink: 0;
  }
  .library-entry-info { flex: 1; display: flex; flex-direction: column; gap: 3px; min-width: 0; }
  .library-entry-title {
    font-size: 0.92rem; font-weight: 700;
    color: var(--lib-text); white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis;
    transition: color 0.3s;
  }
  .library-entry-meta {
    font-family: 'DM Mono', monospace; font-size: 0.72rem; color: var(--muted);
  }
  .btn-lib-remove {
    background: transparent; border: 1px solid var(--border);
    border-radius: 6px; color: var(--muted);
    font-family: 'Syne', sans-serif; font-size: 0.72rem;
    padding: 5px 10px; cursor: pointer; flex-shrink: 0;
    transition: border-color 0.15s, color 0.15s;
  }
  .btn-lib-remove:hover { border-color: var(--danger); color: var(--danger); }

  /* ── Library button in header ── */
  .btn-library {
    background: var(--surface);
    border: 1px solid var(--border); border-radius: 8px;
    color: var(--muted);
    font-family: 'Syne', sans-serif; font-size: 0.78rem; font-weight: 600;
    padding: 7px 16px; cursor: pointer;
    transition: border-color 0.15s, color 0.15s; letter-spacing: 0.03em;
  }
  .btn-library:hover { border-color: var(--accent2); color: var(--accent2); }

  @keyframes spin { to { transform: rotate(360deg); } }
  .spinner {
    width: 16px; height: 16px;
    border: 2px solid var(--border); border-top-color: var(--accent);
    border-radius: 50%; animation: spin 0.7s linear infinite;
    display: inline-block; vertical-align: middle;
  }
  @keyframes fadeUp {
    from { opacity: 0; transform: translateY(12px); }
    to   { opacity: 1; transform: translateY(0); }
  }
  .fade-up { animation: fadeUp 0.3s ease forwards; }
</style>
</head>
<body>
<div class="wrap">

  <header>
    <div class="header-left">
      <h1 id="appTitle" contenteditable="true"
          spellcheck="false"
          onblur="saveTitle(this)"
          onkeydown="if(event.key==='Enter'){event.preventDefault();this.blur()}">Manga DL</h1>
      <span class="subtitle">atsu.moe downloader</span>
    </div>
    <div style="display:flex;gap:8px;align-items:center">
      <button class="btn-library" onclick="openLibrary()">📚 Library</button>
      <button class="btn-customize" onclick="toggleCustomize()">⚙ Customize</button>
    </div>
  </header>

  <!-- ── Customize panel ── -->
  <div class="customize-panel" id="customizePanel">

    <!-- Title colour -->
    <div class="customize-section">
      <span class="customize-label">Title colour</span>
      <input type="color" id="titleColorPicker" value="#e8ff57"
             oninput="applyTitleColor(this.value)">
      <span class="range-hint">Header &amp; manga title text</span>
    </div>

    <!-- General text / body colour -->
    <div class="customize-section">
      <span class="customize-label">Text colour</span>
      <input type="color" id="textColorPicker" value="#e8e8f0"
             oninput="applyTextColor(this.value)">
      <span class="range-hint">Chapter names &amp; body text</span>
    </div>

    <!-- Fetch / Download All button colour -->
    <div class="customize-section">
      <span class="customize-label">Button colour</span>
      <input type="color" id="fetchColorPicker" value="#e8ff57"
             oninput="applyFetchColor(this.value)">
      <span class="range-hint">Fetch &amp; Download All buttons</span>
    </div>

    <!-- Button text colour -->
    <div class="customize-section">
      <span class="customize-label">Button text</span>
      <input type="color" id="btnTextPicker" value="#0d0d0f"
             oninput="applyBtnText(this.value)">
      <span class="range-hint">Text on coloured buttons</span>
    </div>

    <!-- Menu / card tint + transparency -->
    <div class="customize-section">
      <span class="customize-label">Menu tint</span>
      <input type="color" id="menuTintPicker" value="#141418"
             oninput="applyMenuTint(this.value)">
      <span class="range-hint">Card &amp; row background colour</span>
      <span class="customize-label" style="margin-top:6px">Menu transparency</span>
      <div class="slider-row">
        <input type="range" id="menuAlphaSlider" min="0" max="1" step="0.05" value="0.82"
               oninput="applyMenuAlpha(this.value)">
        <span class="slider-val" id="menuAlphaVal">82%</span>
      </div>
      <span class="range-hint">0% = fully transparent · 100% = solid</span>
    </div>

    <!-- Reset all -->
    <div class="customize-section" style="justify-content:flex-end;align-self:flex-end">
      <button class="btn-bg-clear" style="border-color:#555;color:var(--muted)"
              onclick="resetCustomization()">↺ Reset all to defaults</button>
      <span class="range-hint">Clears all saved customizations</span>
    </div>

    <!-- Background image + overlay slider -->
    <div class="customize-section" style="min-width:200px">
      <span class="customize-label">Background image</span>
      <label class="btn-bg-upload" for="bgFileInput">📁 Choose image</label>
      <input type="file" id="bgFileInput" accept="image/*"
             style="display:none" onchange="applyBgImage(this)">
      <div class="bg-filename" id="bgFilename">No image selected</div>
      <button class="btn-bg-clear" onclick="clearBgImage()">✕ Remove image</button>
      <span class="customize-label" style="margin-top:8px">Dark overlay</span>
      <div class="slider-row">
        <input type="range" id="overlaySlider" min="0" max="0.9" step="0.05" value="0.55"
               oninput="applyOverlay(this.value)">
        <span class="slider-val" id="overlayVal">55%</span>
      </div>
      <span class="range-hint">0% = no overlay · 90% = very dark</span>
    </div>

  </div>

  <!-- Search -->
  <div class="search-bar">
    <input type="text" id="urlInput"
           placeholder="https://atsu.moe/manga/..."
           autocomplete="off" spellcheck="false">
    <button id="fetchBtn" onclick="fetchManga()">Fetch</button>
  </div>

  <div class="banner" id="banner"></div>

  <!-- Manga card -->
  <div class="manga-card" id="mangaCard">
    <img id="coverImg" src="" alt="Cover">
    <div class="info">
      <div class="title" id="mangaTitle"></div>
      <div class="meta" id="mangaMeta"></div>
      <div class="actions">
        <button class="btn-all" id="downloadAllBtn" onclick="downloadAll()">
          ↓ Download All
        </button>
        <button class="btn-range" id="downloadRangeBtn" onclick="downloadRange()">
          ↓ Download Range
        </button>
      </div>
      <div class="range-wrap" style="margin-top:6px">
        <span class="range-label">Range:</span>
        <input type="text" class="range-input" id="rangeInput"
               placeholder="e.g. 30-54"
               title="Examples: 30-54 · 1,3,5 · 10- · all">
      </div>
      <div class="range-hint" style="margin-top:2px">
        Formats: <code>30-54</code> &nbsp;·&nbsp; <code>1,3,5</code> &nbsp;·&nbsp;
        <code>10-</code> (10 to end) &nbsp;·&nbsp; <code>all</code>
      </div>
    </div>
  </div>

  <!-- ── Library overlay ── -->
  <div class="library-overlay" id="libraryOverlay" onclick="closeLibraryOnBg(event)">
    <div class="library-panel" id="libraryPanel">

      <!-- Header row: title + buttons -->
      <div class="library-header-row">
        <h2>📚 Library</h2>
        <div class="library-header-actions">
          <button class="btn-lib-customize" onclick="toggleLibCustomize()">⚙ Customize</button>
          <button class="library-close" onclick="closeLibrary()">✕ Close</button>
        </div>
      </div>

      <!-- Library customize sub-panel (hidden by default) -->
      <div class="lib-customize-sub" id="libCustomizeSub">

        <div class="customize-section">
          <span class="customize-label">Title colour</span>
          <input type="color" id="libTitlePicker" value="#e8ff57"
                 oninput="applyLibTitle(this.value)">
          <span class="range-hint">Library heading colour</span>
        </div>

        <div class="customize-section">
          <span class="customize-label">Text colour</span>
          <input type="color" id="libTextPicker" value="#e8e8f0"
                 oninput="applyLibText(this.value)">
          <span class="range-hint">Series title text</span>
        </div>

        <div class="customize-section">
          <span class="customize-label">Panel tint</span>
          <input type="color" id="libTintPicker" value="#141418"
                 oninput="applyLibTint(this.value)">
          <span class="range-hint">Background tint colour</span>
        </div>

        <div class="customize-section">
          <span class="customize-label">Transparency</span>
          <div class="slider-row">
            <input type="range" id="libAlphaSlider" min="0" max="1" step="0.05" value="0.95"
                   oninput="applyLibAlpha(this.value)">
            <span class="slider-val" id="libAlphaVal">95%</span>
          </div>
          <span class="range-hint">0% = fully transparent</span>
        </div>

        <div class="customize-section" style="justify-content:flex-end;align-self:flex-end">
          <button class="btn-bg-clear" style="border-color:#555;color:var(--muted)"
                  onclick="resetLibCustomization()">↺ Reset library style</button>
        </div>

      </div>

      <!-- Entry list -->
      <div class="library-grid" id="libraryGrid">
        <div class="library-empty">Library is empty.</div>
      </div>

    </div>
  </div>

  <!-- Chapter list -->
  <div class="chapters-header" id="chaptersHeader">
    <h2>Chapters</h2>
    <span class="count" id="chapterCount"></span>
  </div>
  <div class="chapter-list" id="chapterList"></div>

</div>

<script>
let _chapters = [];
let _mangaTitle = '';
let _activeDownloads = 0;

// ── Helpers ──────────────────────────────────────────────────────────────────

function hexToRgb(hex) {
  const r = parseInt(hex.slice(1,3),16);
  const g = parseInt(hex.slice(3,5),16);
  const b = parseInt(hex.slice(5,7),16);
  return `${r},${g},${b}`;
}

const root = document.documentElement;

// ── Customization ────────────────────────────────────────────────────────────

function toggleCustomize() {
  document.getElementById('customizePanel').classList.toggle('open');
}

function applyTitleColor(v) {
  root.style.setProperty('--title-color', v);
  localStorage.setItem('titleColor', v);
}

function applyTextColor(v) {
  root.style.setProperty('--text', v);
  localStorage.setItem('textColor', v);
}

function applyFetchColor(v) {
  root.style.setProperty('--fetch-bg', v);
  localStorage.setItem('fetchColor', v);
}

function applyBtnText(v) {
  root.style.setProperty('--btn-text', v);
  localStorage.setItem('btnText', v);
}

function applyMenuTint(v) {
  root.style.setProperty('--menu-tint', hexToRgb(v));
  localStorage.setItem('menuTint', v);
}

function applyMenuAlpha(v) {
  root.style.setProperty('--menu-alpha', v);
  const pct = Math.round(v * 100);
  document.getElementById('menuAlphaVal').textContent = pct + '%';
  localStorage.setItem('menuAlpha', v);
}

function applyOverlay(v) {
  root.style.setProperty('--overlay', v);
  const pct = Math.round(v * 100);
  document.getElementById('overlayVal').textContent = pct + '%';
  localStorage.setItem('overlay', v);
}

function applyBgImage(input) {
  const file = input.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = (e) => {
    const dataUrl = e.target.result;
    document.body.style.backgroundImage = `url(${dataUrl})`;
    root.style.setProperty('--grid-opacity', '0');
    document.getElementById('bgFilename').textContent = file.name;
    try { localStorage.setItem('bgImage', dataUrl); } catch(e) {}
    localStorage.setItem('bgFilename', file.name);
  };
  reader.readAsDataURL(file);
}

function clearBgImage() {
  document.body.style.backgroundImage = '';
  root.style.setProperty('--grid-opacity', '0.35');
  document.getElementById('bgFilename').textContent = 'No image selected';
  document.getElementById('bgFileInput').value = '';
  localStorage.removeItem('bgImage');
  localStorage.removeItem('bgFilename');
}

function resetCustomization() {
  // Clear all saved keys
  const keys = ['titleColor','textColor','fetchColor','btnText',
                 'menuTint','menuAlpha','overlay','bgImage','bgFilename','appTitle'];
  keys.forEach(k => localStorage.removeItem(k));

  // Reset CSS variables to defaults
  root.style.setProperty('--title-color',  '#e8ff57');
  root.style.setProperty('--text',         '#e8e8f0');
  root.style.setProperty('--fetch-bg',     '#e8ff57');
  root.style.setProperty('--btn-text',     '#0d0d0f');
  root.style.setProperty('--menu-tint',    '20,20,24');
  root.style.setProperty('--menu-alpha',   '0.82');
  root.style.setProperty('--overlay',      '0');
  root.style.setProperty('--grid-opacity', '0.35');

  // Reset background image
  document.body.style.backgroundImage = '';

  // Reset picker values in the panel
  document.getElementById('titleColorPicker').value  = '#e8ff57';
  document.getElementById('textColorPicker').value   = '#e8e8f0';
  document.getElementById('fetchColorPicker').value  = '#e8ff57';
  document.getElementById('btnTextPicker').value     = '#0d0d0f';
  document.getElementById('menuTintPicker').value    = '#141418';
  document.getElementById('menuAlphaSlider').value   = '0.82';
  document.getElementById('menuAlphaVal').textContent = '82%';
  document.getElementById('overlaySlider').value     = '0';
  document.getElementById('overlayVal').textContent  = '0%';
  document.getElementById('bgFilename').textContent  = 'No image selected';
  document.getElementById('bgFileInput').value       = '';
  document.getElementById('appTitle').textContent    = 'Manga DL';
}

function saveTitle(el) {
  const val = el.textContent.trim() || 'Manga DL';
  el.textContent = val;
  localStorage.setItem('appTitle', val);
}

// Load all saved preferences on startup
(function loadPrefs() {
  const tc = localStorage.getItem('titleColor');
  if (tc) { applyTitleColor(tc); document.getElementById('titleColorPicker').value = tc; }

  const tx = localStorage.getItem('textColor');
  if (tx) { applyTextColor(tx); document.getElementById('textColorPicker').value = tx; }

  const fc = localStorage.getItem('fetchColor');
  if (fc) { applyFetchColor(fc); document.getElementById('fetchColorPicker').value = fc; }

  const bt = localStorage.getItem('btnText');
  if (bt) { applyBtnText(bt); document.getElementById('btnTextPicker').value = bt; }

  const mt = localStorage.getItem('menuTint');
  if (mt) { applyMenuTint(mt); document.getElementById('menuTintPicker').value = mt; }

  const ma = localStorage.getItem('menuAlpha');
  if (ma) {
    applyMenuAlpha(ma);
    document.getElementById('menuAlphaSlider').value = ma;
  }

  const ov = localStorage.getItem('overlay');
  if (ov) {
    applyOverlay(ov);
    document.getElementById('overlaySlider').value = ov;
  }

  const bg = localStorage.getItem('bgImage');
  if (bg) {
    document.body.style.backgroundImage = `url(${bg})`;
    root.style.setProperty('--grid-opacity', '0');
    document.getElementById('bgFilename').textContent =
      localStorage.getItem('bgFilename') || 'Custom image';
  }

  const title = localStorage.getItem('appTitle');
  if (title) document.getElementById('appTitle').textContent = title;
})();


// ── Range parsing ────────────────────────────────────────────────────────────

function parseRange(str, total) {
  str = str.trim().toLowerCase();
  if (!str || str === 'all' || str === '*') {
    return Array.from({length: total}, (_, i) => i);
  }
  const indices = new Set();
  for (const part of str.split(',')) {
    const p = part.trim();
    if (!p) continue;
    const rangeMatch = p.match(/^(\d+)\s*-\s*(\d*)$/);
    if (rangeMatch) {
      const start = parseInt(rangeMatch[1]);
      const end   = rangeMatch[2] ? parseInt(rangeMatch[2]) : total;
      for (let i = start; i <= Math.min(end, total); i++) indices.add(i - 1);
      continue;
    }
    if (/^\d+$/.test(p)) {
      const n = parseInt(p);
      if (n >= 1 && n <= total) indices.add(n - 1);
    }
  }
  return [...indices].sort((a, b) => a - b);
}


// ── Fetch manga ──────────────────────────────────────────────────────────────

async function fetchManga() {
  const url = document.getElementById('urlInput').value.trim();
  if (!url) return;
  setFetching(true);
  showBanner('Fetching chapters… this takes 10–20 seconds.', false);
  clearResults();
  try {
    const res = await fetch('/api/fetch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url })
    });
    const data = await res.json();
    if (!res.ok || data.error) { showBanner(data.error || 'Something went wrong.', true); return; }
    hideBanner();
    _chapters = data.chapters;
    _mangaTitle = data.title;
    renderMangaCard(data);
    renderChapters(data.chapters);
  } catch (e) {
    showBanner('Network error: ' + e.message, true);
  } finally {
    setFetching(false);
  }
}

document.getElementById('urlInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') fetchManga();
});


// ── Render ───────────────────────────────────────────────────────────────────

function renderMangaCard(data) {
  document.getElementById('mangaTitle').textContent = data.title;
  document.getElementById('mangaMeta').textContent = data.chapters.length + ' chapters available';
  const img = document.getElementById('coverImg');
  if (data.cover) {
    img.src = '/api/proxy-cover?url=' + encodeURIComponent(data.cover);
    img.onerror = () => { img.style.display = 'none'; };
  } else {
    img.style.display = 'none';
  }
  document.getElementById('mangaCard').classList.add('visible', 'fade-up');
  document.getElementById('chaptersHeader').classList.add('visible');
  document.getElementById('chapterCount').textContent = data.chapters.length;
}

function renderChapters(chapters) {
  const list = document.getElementById('chapterList');
  list.innerHTML = '';
  chapters.forEach((ch, i) => {
    const row = document.createElement('div');
    row.className = 'chapter-row fade-up';
    row.id = 'row-' + i;
    row.style.animationDelay = Math.min(i * 18, 400) + 'ms';
    row.innerHTML = `
      <span class="chapter-num">${String(i+1).padStart(3,'0')}</span>
      <span class="chapter-title">${escHtml(ch.title)}</span>
      <div class="chapter-progress" id="prog-${i}">
        <div class="chapter-progress-fill" id="progfill-${i}"></div>
      </div>
      <span class="chapter-status" id="status-${i}"></span>
      <button class="btn-dl" id="btn-${i}" onclick="downloadChapter(${i})">↓ Download</button>
    `;
    list.appendChild(row);
  });
}


// ── Download chapter ─────────────────────────────────────────────────────────

async function downloadChapter(i) {
  const ch       = _chapters[i];
  const btn      = document.getElementById('btn-' + i);
  const statusEl = document.getElementById('status-' + i);
  const progEl   = document.getElementById('prog-' + i);
  const progFill = document.getElementById('progfill-' + i);
  const row      = document.getElementById('row-' + i);

  btn.disabled = true; btn.textContent = '…';
  statusEl.textContent = 'Starting…';
  statusEl.className = 'chapter-status downloading';
  progEl.classList.add('visible');
  _activeDownloads++;
  updateBulkBtns();

  try {
    const res = await fetch('/api/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        chapter_url: ch.url, chapter_title: ch.title,
        manga_title: _mangaTitle, chapter_index: i + 1
      })
    });
    const { job_id, error } = await res.json();
    if (error) throw new Error(error);

    const evtSrc = new EventSource('/api/progress/' + job_id);
    evtSrc.onmessage = (e) => {
      const job = JSON.parse(e.data);
      if (job.total > 0) {
        progFill.style.width = Math.round((job.progress/job.total)*100) + '%';
        statusEl.textContent = job.progress + '/' + job.total;
      } else {
        statusEl.textContent = job.message || job.status;
      }
      if (job.done) {
        evtSrc.close(); _activeDownloads--; updateBulkBtns();
        if (job.status === 'done') {
          row.classList.add('done');
          statusEl.textContent = '✓ Done'; statusEl.className = 'chapter-status done';
          progFill.style.width = '100%';
          btn.textContent = '✓'; btn.className = 'btn-dl done-btn'; btn.disabled = true;
        } else {
          statusEl.textContent = '✕ ' + (job.error || 'Failed');
          statusEl.className = 'chapter-status error'; row.classList.add('error-row');
          btn.textContent = '↓ Retry'; btn.disabled = false;
        }
      }
    };
    evtSrc.onerror = () => {
      evtSrc.close();
      statusEl.textContent = 'Connection lost'; statusEl.className = 'chapter-status error';
      btn.textContent = '↓ Retry'; btn.disabled = false;
      _activeDownloads--; updateBulkBtns();
    };
  } catch (err) {
    statusEl.textContent = '✕ ' + err.message; statusEl.className = 'chapter-status error';
    btn.textContent = '↓ Retry'; btn.disabled = false;
    _activeDownloads--; updateBulkBtns();
  }
}


// ── Bulk download ────────────────────────────────────────────────────────────

async function downloadAll() {
  await runBulkDownload(Array.from({length: _chapters.length}, (_, i) => i));
}

async function downloadRange() {
  const raw = document.getElementById('rangeInput').value.trim();
  if (!raw) { showBanner('Enter a range first — e.g. 30-54 or 1,3,5', true); setTimeout(hideBanner, 3000); return; }
  const indices = parseRange(raw, _chapters.length);
  if (!indices.length) { showBanner('No valid chapters in that range.', true); setTimeout(hideBanner, 3000); return; }
  showBanner(`Downloading ${indices.length} chapter(s) from range "${raw}"…`, false);
  await runBulkDownload(indices);
  hideBanner();
}

async function runBulkDownload(indices) {
  document.getElementById('downloadAllBtn').disabled   = true;
  document.getElementById('downloadRangeBtn').disabled = true;
  for (const i of indices) {
    const rowBtn = document.getElementById('btn-' + i);
    if (rowBtn && rowBtn.classList.contains('done-btn')) continue;
    await downloadChapter(i);
    await waitForDownloadDone(i);
  }
  document.getElementById('downloadAllBtn').disabled   = false;
  document.getElementById('downloadRangeBtn').disabled = false;
}

function waitForDownloadDone(i) {
  return new Promise(resolve => {
    const check = () => {
      const row = document.getElementById('row-' + i);
      if (row && (row.classList.contains('done') || row.classList.contains('error-row'))) resolve();
      else setTimeout(check, 500);
    };
    setTimeout(check, 500);
  });
}


// ── UI helpers ────────────────────────────────────────────────────────────────

function updateBulkBtns() {
  const d = _activeDownloads > 0;
  document.getElementById('downloadAllBtn').disabled   = d;
  document.getElementById('downloadRangeBtn').disabled = d;
}
function setFetching(active) {
  const btn = document.getElementById('fetchBtn');
  const inp = document.getElementById('urlInput');
  btn.disabled = inp.disabled = active;
  btn.innerHTML = active ? '<span class="spinner"></span>' : 'Fetch';
}
function showBanner(msg, isError) {
  const b = document.getElementById('banner');
  b.textContent = msg;
  b.className = 'banner visible' + (isError ? ' error' : '');
}
function hideBanner() { document.getElementById('banner').className = 'banner'; }
function clearResults() {
  _chapters = [];
  document.getElementById('mangaCard').className = 'manga-card';
  document.getElementById('chaptersHeader').className = 'chapters-header';
  document.getElementById('chapterList').innerHTML = '';
  document.getElementById('coverImg').style.display = '';
  document.getElementById('coverImg').src = '';
  document.getElementById('rangeInput').value = '';
}
function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Library customization ────────────────────────────────────────────────────

function toggleLibCustomize() {
  document.getElementById('libCustomizeSub').classList.toggle('open');
}

function applyLibTitle(v) {
  document.documentElement.style.setProperty('--lib-title', v);
  localStorage.setItem('libTitle', v);
}
function applyLibText(v) {
  document.documentElement.style.setProperty('--lib-text', v);
  localStorage.setItem('libText', v);
}
function applyLibTint(v) {
  document.documentElement.style.setProperty('--lib-tint', hexToRgb(v));
  localStorage.setItem('libTint', v);
}
function applyLibAlpha(v) {
  document.documentElement.style.setProperty('--lib-alpha', v);
  const pct = Math.round(v * 100);
  document.getElementById('libAlphaVal').textContent = pct + '%';
  localStorage.setItem('libAlpha', v);
}

function resetLibCustomization() {
  const keys = ['libTitle','libText','libTint','libAlpha'];
  keys.forEach(k => localStorage.removeItem(k));
  applyLibTitle('#e8ff57');
  applyLibText('#e8e8f0');
  applyLibTint('#141418');
  applyLibAlpha('0.95');
  document.getElementById('libTitlePicker').value  = '#e8ff57';
  document.getElementById('libTextPicker').value   = '#e8e8f0';
  document.getElementById('libTintPicker').value   = '#141418';
  document.getElementById('libAlphaSlider').value  = '0.95';
  document.getElementById('libAlphaVal').textContent = '95%';
}

// Load saved library prefs on startup
(function loadLibPrefs() {
  const lt = localStorage.getItem('libTitle');
  if (lt) { applyLibTitle(lt); document.getElementById('libTitlePicker').value = lt; }

  const lx = localStorage.getItem('libText');
  if (lx) { applyLibText(lx); document.getElementById('libTextPicker').value = lx; }

  const ln = localStorage.getItem('libTint');
  if (ln) { applyLibTint(ln); document.getElementById('libTintPicker').value = ln; }

  const la = localStorage.getItem('libAlpha');
  if (la) {
    applyLibAlpha(la);
    document.getElementById('libAlphaSlider').value = la;
  }
})();


// ── Library ───────────────────────────────────────────────────────────────────

async function openLibrary() {
  await renderLibrary();
  document.getElementById('libraryOverlay').classList.add('open');
}

function closeLibrary() {
  document.getElementById('libraryOverlay').classList.remove('open');
}

function closeLibraryOnBg(e) {
  // Close if user clicks the dark backdrop, not the panel itself
  if (e.target === document.getElementById('libraryOverlay')) closeLibrary();
}

async function renderLibrary() {
  const grid = document.getElementById('libraryGrid');
  grid.innerHTML = '<div class="library-empty">Loading…</div>';

  try {
    const res  = await fetch('/api/library');
    const data = await res.json();
    const entries = data.entries || [];

    if (!entries.length) {
      grid.innerHTML = '<div class="library-empty">No series saved yet. Fetch a manga to add it.</div>';
      return;
    }

    grid.innerHTML = '';
    for (const e of entries) {
      const card = document.createElement('div');
      card.className = 'library-entry';
      card.onclick = (ev) => {
        // Don't trigger load if the remove button was clicked
        if (ev.target.classList.contains('btn-lib-remove')) return;
        loadFromLibrary(e.url);
      };

      const coverSrc = e.cover
        ? '/api/proxy-cover?url=' + encodeURIComponent(e.cover)
        : '';

      card.innerHTML = `
        <img src="${escHtml(coverSrc)}" alt=""
             onerror="this.style.display='none'">
        <div class="library-entry-info">
          <div class="library-entry-title">${escHtml(e.title)}</div>
          <div class="library-entry-meta">${e.chapter_count} chapters &nbsp;·&nbsp; ${escHtml(e.url)}</div>
        </div>
        <button class="btn-lib-remove"
                onclick="removeFromLibrary('${escHtml(e.url)}', this)">✕ Remove</button>
      `;
      grid.appendChild(card);
    }
  } catch (err) {
    grid.innerHTML = '<div class="library-empty">Failed to load library: ' + escHtml(err.message) + '</div>';
  }
}

async function removeFromLibrary(url, btn) {
  btn.disabled = true;
  btn.textContent = '…';
  try {
    await fetch('/api/library/remove', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url })
    });
    // Re-render the list after removal
    await renderLibrary();
  } catch (err) {
    btn.textContent = '✕ Remove';
    btn.disabled = false;
  }
}

async function loadFromLibrary(url) {
  closeLibrary();
  document.getElementById('urlInput').value = url;
  await fetchManga();
}
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return HTML


if __name__ == "__main__":
    print()
    print("  ┌─────────────────────────────────────────┐")
    print("  │   Manga Downloader GUI                  │")
    print("  │   Open: http://localhost:7337            │")
    print("  │   Press Ctrl+C to stop                  │")
    print("  └─────────────────────────────────────────┘")
    print()
    app.run(host="127.0.0.1", port=7337, debug=False, threaded=True)
