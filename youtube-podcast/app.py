"""Turn YouTube channels and playlists into podcast RSS feeds.

    GET /                         small page that builds a feed URL from a YouTube link
    GET /feed/{source}            podcast RSS; source is a channel id (UC...), @handle or playlist id (PL...)
    GET /feed?url=<youtube url>   redirects to /feed/{source}
    GET /audio/{video_id}.m4a     the episode audio, downloaded with yt-dlp on first request and cached

Episode lists come from YouTube's public Atom feeds (newest 15 videos), which need no API key
and are rarely rate limited. Audio is fetched with yt-dlp, which YouTube may block from
datacenter IPs; set YTDLP_COOKIES to a cookies.txt export if that happens.

Environment:
    BASE_URL          public URL of this server, used in feed links (default: from the request)
    ACCESS_KEY        if set, feeds and audio need ?key=<ACCESS_KEY>; feed links carry it automatically
    CACHE_DIR         where audio is stored (default ./cache)
    CACHE_MAX_GB      oldest audio is deleted above this size (default 5)
    PREFETCH          newest episodes to download in the background when a feed is read (default 2)
    YTDLP_COOKIES     path to a Netscape cookies.txt for YouTube
    YTDLP_COOKIES_TEXT  the cookies.txt contents instead of a path (for hosts whose secrets are text)
    YTDLP_EXTRA_ARGS  extra yt-dlp arguments, e.g. "--js-runtimes node"
"""

import html
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import format_datetime
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
ACCESS_KEY = os.environ.get("ACCESS_KEY", "")
CACHE_DIR = Path(os.environ.get("CACHE_DIR", "cache"))
CACHE_MAX_BYTES = int(float(os.environ.get("CACHE_MAX_GB", "5")) * 1024**3)
PREFETCH = int(os.environ.get("PREFETCH", "2"))
YTDLP_COOKIES = os.environ.get("YTDLP_COOKIES", "")
YTDLP_EXTRA_ARGS = shlex.split(os.environ.get("YTDLP_EXTRA_ARGS", ""))

FEED_TTL = 15 * 60
DOWNLOAD_TIMEOUT = 30 * 60
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015", "media": "http://search.yahoo.com/mrss/"}
ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ET.register_namespace("itunes", ITUNES)

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
PLAYLIST_ID_RE = re.compile(r"^(PL|UU|OL|FL|LL|RD)[A-Za-z0-9_-]{10,}$")
HANDLE_RE = re.compile(r"^@[\w.-]{3,30}$")

CACHE_DIR.mkdir(parents=True, exist_ok=True)
if os.environ.get("YTDLP_COOKIES_TEXT") and not YTDLP_COOKIES:
    YTDLP_COOKIES = str(CACHE_DIR / "cookies.txt")
    Path(YTDLP_COOKIES).write_text(os.environ["YTDLP_COOKIES_TEXT"].replace("\\n", "\n") + "\n")
_feed_cache: dict[str, tuple[float, dict]] = {}
_channel_cache: dict[str, dict] = {}
_download_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

app = FastAPI(title="YouTube to Podcast", docs_url=None, redoc_url=None)


# ---------------------------------------------------------------- YouTube metadata

def http_get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def channel_page_info(path: str) -> dict:
    """Channel id, name and artwork from a channel page such as '@mkbhd' or 'channel/UC...'."""
    if path in _channel_cache:
        return _channel_cache[path]
    try:
        page = http_get(f"https://www.youtube.com/{path}").decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise HTTPException(404 if e.code == 404 else 502, f"YouTube returned {e.code} for /{path}")
    meta = lambda prop: (m := re.search(rf'<meta property="{prop}" content="([^"]*)"', page)) and html.unescape(m.group(1))
    channel_id = (re.search(r'"externalId":"(UC[A-Za-z0-9_-]{22})"', page) or re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[A-Za-z0-9_-]{22})"', page))
    if not channel_id:
        raise HTTPException(404, f"Could not find a YouTube channel at /{path}")
    info = {"channel_id": channel_id.group(1), "title": meta("og:title"), "image": meta("og:image"), "description": meta("og:description")}
    _channel_cache[path] = info
    return info


def parse_source(source: str) -> tuple[str, str]:
    """Return (kind, id) for a channel id, @handle, playlist id or YouTube URL."""
    source = source.strip()
    if source.startswith(("http://", "https://", "www.", "youtube.com", "m.youtube.com")):
        url = urllib.parse.urlparse(source if "://" in source else "https://" + source)
        query = urllib.parse.parse_qs(url.query)
        if "list" in query:
            return "playlist", query["list"][0]
        parts = [p for p in url.path.split("/") if p]
        if parts and parts[0].startswith("@"):
            return "handle", parts[0]
        if len(parts) >= 2 and parts[0] == "channel":
            return "channel", parts[1]
        raise HTTPException(400, "Use a channel URL (youtube.com/@name or /channel/UC...) or a playlist URL (?list=...)")
    if CHANNEL_ID_RE.match(source):
        return "channel", source
    if PLAYLIST_ID_RE.match(source):
        return "playlist", source
    if HANDLE_RE.match(source):
        return "handle", source
    raise HTTPException(400, f"Not a channel id, @handle or playlist id: {source!r}")


def load_feed(kind: str, ident: str) -> dict:
    key = f"{kind}:{ident}"
    cached = _feed_cache.get(key)
    if cached and time.time() - cached[0] < FEED_TTL:
        return cached[1]

    artwork = {}
    if kind == "handle":
        artwork = channel_page_info(ident)
        kind, ident = "channel", artwork["channel_id"]
    param = "channel_id" if kind == "channel" else "playlist_id"
    try:
        root = ET.fromstring(http_get(f"https://www.youtube.com/feeds/videos.xml?{param}={ident}"))
    except urllib.error.HTTPError as e:
        raise HTTPException(404 if e.code == 404 else 502, f"YouTube feed for {kind} {ident} returned {e.code}")
    if kind == "channel" and not artwork:
        try:
            artwork = channel_page_info(f"channel/{ident}")
        except Exception:
            artwork = {}

    episodes = []
    for entry in root.findall("a:entry", NS):
        video_id = entry.findtext("yt:videoId", namespaces=NS)
        link = entry.find("a:link", NS).get("href", "")
        group = entry.find("media:group", NS)
        thumb = group.find("media:thumbnail", NS) if group is not None else None
        episodes.append({
            "id": video_id,
            "title": entry.findtext("a:title", namespaces=NS) or video_id,
            "published": datetime.fromisoformat(entry.findtext("a:published", namespaces=NS)),
            "description": (group.findtext("media:description", namespaces=NS) if group is not None else "") or "",
            "thumbnail": thumb.get("url") if thumb is not None else None,
            "link": link,
            "short": "/shorts/" in link,
        })

    feed = {
        "title": root.findtext("a:title", namespaces=NS) or artwork.get("title") or ident,
        "author": root.findtext("a:author/a:name", namespaces=NS) or "",
        "link": (root.find("a:link[@rel='alternate']", NS).get("href") if root.find("a:link[@rel='alternate']", NS) is not None else "https://www.youtube.com"),
        "description": artwork.get("description") or "",
        "image": artwork.get("image") or next((e["thumbnail"] for e in episodes if e["thumbnail"]), None),
        "episodes": episodes,
    }
    _feed_cache[key] = (time.time(), feed)
    return feed


# ---------------------------------------------------------------- audio cache

def audio_path(video_id: str) -> Path:
    return CACHE_DIR / f"{video_id}.m4a"


def audio_meta(video_id: str) -> dict:
    try:
        return json.loads((CACHE_DIR / f"{video_id}.json").read_text())
    except (OSError, ValueError):
        return {}


def download_audio(video_id: str) -> Path:
    """Download (once) the episode audio as M4A/AAC, which every podcast app plays."""
    path = audio_path(video_id)
    with _locks_guard:
        lock = _download_locks.setdefault(video_id, threading.Lock())
    with lock:
        if path.exists():
            return path
        tmp = CACHE_DIR / f"{video_id}.part"
        cmd = [
            "yt-dlp", "--no-playlist", "--no-progress", "--no-simulate",
            "-f", "bestaudio[ext=m4a]/bestaudio",
            "--extract-audio", "--audio-format", "m4a",
            "-o", str(tmp / "audio.%(ext)s"),
            "--print", "after_move:%(duration)s",
            *(["--cookies", YTDLP_COOKIES] if YTDLP_COOKIES else []),
            *YTDLP_EXTRA_ARGS,
            f"https://www.youtube.com/watch?v={video_id}",
        ]
        started = time.time()
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT)
            out = tmp / "audio.m4a"
            if result.returncode != 0 or not out.exists():
                error = next((l for l in reversed(result.stderr.splitlines()) if l.startswith("ERROR")), result.stderr[-500:])
                raise RuntimeError(error.strip() or "yt-dlp failed")
            duration = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
            (CACHE_DIR / f"{video_id}.json").write_text(json.dumps({"duration": int(float(duration)) if duration not in ("", "NA") else None}))
            out.replace(path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        print(f"downloaded {video_id}: {path.stat().st_size / 1e6:.1f} MB in {time.time() - started:.0f}s", flush=True)
        trim_cache()
        return path


def trim_cache():
    files = sorted(CACHE_DIR.glob("*.m4a"), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    while files and total > CACHE_MAX_BYTES:
        oldest = files.pop(0)
        total -= oldest.stat().st_size
        oldest.unlink(missing_ok=True)
        (CACHE_DIR / f"{oldest.stem}.json").unlink(missing_ok=True)


def prefetch(video_ids: list[str]):
    def run():
        for vid in video_ids:
            if not audio_path(vid).exists():
                try:
                    download_audio(vid)
                except Exception as e:
                    print(f"prefetch {vid} failed: {e}", flush=True)
    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------- RSS

def check_key(request: Request):
    if ACCESS_KEY and request.query_params.get("key") != ACCESS_KEY:
        raise HTTPException(401, "Missing or wrong ?key=")


def base_url(request: Request) -> str:
    if BASE_URL:
        return BASE_URL
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return f"{proto}://{request.headers.get('host', request.url.netloc)}"


def build_rss(feed: dict, base: str, include_shorts: bool) -> bytes:
    key_query = f"?key={urllib.parse.quote(ACCESS_KEY)}" if ACCESS_KEY else ""
    rss = ET.Element("rss", {"version": "2.0"})
    ch = ET.SubElement(rss, "channel")

    def sub(parent, tag, text=None, **attrs):
        el = ET.SubElement(parent, tag, attrs)
        if text is not None:
            el.text = text
        return el

    sub(ch, "title", feed["title"])
    sub(ch, "link", feed["link"])
    sub(ch, "description", feed["description"] or f"YouTube videos from {feed['title']}")
    sub(ch, "language", "en")
    sub(ch, f"{{{ITUNES}}}author", feed["author"] or feed["title"])
    sub(ch, f"{{{ITUNES}}}explicit", "false")
    if feed["image"]:
        sub(ch, f"{{{ITUNES}}}image", href=feed["image"])
        img = sub(ch, "image")
        sub(img, "url", feed["image"])
        sub(img, "title", feed["title"])
        sub(img, "link", feed["link"])

    for ep in feed["episodes"]:
        if ep["short"] and not include_shorts:
            continue
        item = sub(ch, "item")
        sub(item, "title", ep["title"])
        sub(item, "link", ep["link"])
        sub(item, "guid", f"yt:video:{ep['id']}", isPermaLink="false")
        sub(item, "pubDate", format_datetime(ep["published"]))
        sub(item, "description", f"{ep['description']}\n\n{ep['link']}".strip())
        path = audio_path(ep["id"])
        size = str(path.stat().st_size) if path.exists() else "0"
        sub(item, "enclosure", url=f"{base}/audio/{ep['id']}.m4a{key_query}", type="audio/mp4", length=size)
        if ep["thumbnail"]:
            sub(item, f"{{{ITUNES}}}image", href=ep["thumbnail"])
        if duration := audio_meta(ep["id"]).get("duration"):
            sub(item, f"{{{ITUNES}}}duration", str(duration))
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="utf-8")


# ---------------------------------------------------------------- routes

@app.get("/", response_class=HTMLResponse)
def index():
    return """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>YouTube to Podcast</title><style>body{font:16px system-ui;max-width:640px;margin:40px auto;padding:0 16px}
input{width:100%;padding:8px;box-sizing:border-box}code{word-break:break-all}</style></head><body>
<h1>YouTube to Podcast</h1><p>Paste a channel or playlist link, then add the feed URL to your podcast app.</p>
<form onsubmit="event.preventDefault();var k=new URLSearchParams(location.search).get('key');
var u=location.origin+'/feed?url='+encodeURIComponent(this.u.value)+(k?'&key='+encodeURIComponent(k):'');
document.getElementById('o').innerHTML='<p>Feed URL:</p><code>'+u.replace(/&/g,'&amp;').replace(/</g,'&lt;')+'</code>'">
<input name="u" placeholder="https://www.youtube.com/@channel or ...playlist?list=PL..." required></form><div id="o"></div></body></html>"""


@app.get("/feed")
def feed_from_url(request: Request, url: str):
    check_key(request)
    _, ident = parse_source(url)
    query = urllib.parse.urlencode({k: v for k, v in request.query_params.items() if k != "url"})
    return RedirectResponse(f"/feed/{urllib.parse.quote(ident)}" + (f"?{query}" if query else ""), status_code=302)


@app.get("/feed/{source}")
def feed(request: Request, source: str, shorts: bool = False):
    check_key(request)
    kind, ident = parse_source(source)
    data = load_feed(kind, ident)
    if PREFETCH > 0:
        prefetch([e["id"] for e in data["episodes"] if shorts or not e["short"]][:PREFETCH])
    return Response(build_rss(data, base_url(request), shorts), media_type="application/rss+xml; charset=utf-8")


@app.api_route("/audio/{video_id}.m4a", methods=["GET", "HEAD"])
def audio(request: Request, video_id: str):
    check_key(request)
    if not VIDEO_ID_RE.match(video_id):
        raise HTTPException(404, "Unknown video id")
    try:
        path = download_audio(video_id)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Downloading the audio from YouTube timed out")
    except RuntimeError as e:
        hint = " YouTube is blocking this server; set YTDLP_COOKIES (see README)." if "bot" in str(e).lower() else ""
        raise HTTPException(502, f"{e}{hint}")
    os.utime(path)  # mark as recently used for cache eviction
    # FileResponse answers Range requests, which podcast apps use to seek and resume.
    return FileResponse(path, media_type="audio/mp4", filename=f"{video_id}.m4a", content_disposition_type="inline")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "7860")))
