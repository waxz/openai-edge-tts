"""Turn YouTube channels and playlists into podcast RSS feeds.

Runs on its own (`python podcast.py`) or as a module mounted into a larger FastAPI app
(`root.mount("/podcast", podcast.app)`); every link it generates respects the mount path.

    GET /                         web page: enter the key and a YouTube link to get a feed URL
    GET /api/feed-url?url=&key=   what the page calls; returns a signed feed URL
    GET /feed/{source}            podcast RSS; source is a channel id (UC...), @handle or playlist id (PL...)
    GET /feed?url=<youtube url>   redirects to /feed/{source}
    GET /audio/{video_id}.m4a     the episode audio, downloaded with yt-dlp on first request and cached
    GET /transcript/{video_id}.vtt  episode transcript (.vtt, .srt, .txt or .json), made with Whisper

Episode lists come from YouTube's public Atom feeds (newest 15 videos), which need no API key
and are rarely rate limited. Audio is fetched with yt-dlp, which YouTube may block from
datacenter IPs; set YTDLP_COOKIES to a cookies.txt export if that happens.

Environment:
    BASE_URL          public URL of this server, used in feed links (default: from the request)
    ACCESS_KEY        if set, only the web page (and /status) needs the key; comma-separate several keys.
                      The page hands out feed URLs signed for one channel (?sig=...), and feeds sign their
                      audio and transcript links, so podcast apps never see the key and the server can't
                      be used to download arbitrary videos. ?key=<key> is still accepted everywhere.
    CACHE_DIR         where audio is stored (default ./cache)
    CACHE_MAX_GB      oldest audio is deleted above this size (default 5)
    PREFETCH          newest episodes to download in the background when a feed is read (default 2)
    YTDLP_COOKIES     path to a Netscape cookies.txt for YouTube
    YTDLP_COOKIES_TEXT  the cookies.txt contents instead of a path (for hosts whose secrets are text)
    YTDLP_EXTRA_ARGS  extra yt-dlp arguments, e.g. "--js-runtimes node"
    YTDLP_MIN_INTERVAL  seconds between yt-dlp runs (default 20); runs never overlap
    RATE_LIMIT_COOLDOWN  seconds to stop contacting YouTube after a 429 / bot check (default 900,
                      doubling on each repeat up to 6 hours, reset by a successful download)
    TRANSCRIPTS       "auto" (default: on when whisper_stt.py and faster-whisper are installed), "on" or "off".
                      Each downloaded episode is transcribed in the background; once ready, the feed links
                      the transcript with <podcast:transcript>, which many podcast apps display.
"""

import hashlib
import hmac
import html
import json
import os
import queue
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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
ACCESS_KEYS = [k.strip() for k in os.environ.get("ACCESS_KEY", "").split(",") if k.strip()]
CACHE_DIR = Path(os.environ.get("CACHE_DIR", "cache"))
CACHE_MAX_BYTES = int(float(os.environ.get("CACHE_MAX_GB", "5")) * 1024**3)
PREFETCH = int(os.environ.get("PREFETCH", "2"))
YTDLP_COOKIES = os.environ.get("YTDLP_COOKIES", "")
YTDLP_EXTRA_ARGS = shlex.split(os.environ.get("YTDLP_EXTRA_ARGS", ""))

FEED_TTL = 15 * 60
DOWNLOAD_TIMEOUT = 30 * 60
YTDLP_MIN_INTERVAL = float(os.environ.get("YTDLP_MIN_INTERVAL", "20"))
RATE_LIMIT_COOLDOWN = int(os.environ.get("RATE_LIMIT_COOLDOWN", "900"))
MAX_COOLDOWN = 6 * 3600
FAILED_VIDEO_TTL = 3600
RATE_LIMIT_MARKERS = ("429", "too many requests", "not a bot", "sign in to confirm", "rate-limit", "rate limit")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015", "media": "http://search.yahoo.com/mrss/"}
ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
PODCAST_NS = "https://podcastindex.org/namespace/1.0"
ET.register_namespace("itunes", ITUNES)
ET.register_namespace("podcast", PODCAST_NS)

# Transcripts use the Whisper module from the same Space when it is installed.
TRANSCRIPTS = os.environ.get("TRANSCRIPTS", "auto").lower()
whisper_stt = None
if TRANSCRIPTS != "off":
    try:
        import faster_whisper  # noqa: F401
        import whisper_stt
    except ImportError as e:
        if TRANSCRIPTS == "on":
            raise RuntimeError("TRANSCRIPTS=on needs whisper_stt.py and faster-whisper") from e
TRANSCRIPT_TYPES = {"vtt": "text/vtt", "srt": "application/x-subrip", "txt": "text/plain", "json": "application/json"}

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


class RateLimited(Exception):
    """YouTube is rate limiting this server; retry after `retry_after` seconds."""

    def __init__(self, retry_after: int, message: str):
        super().__init__(message)
        self.retry_after = retry_after


# YouTube answers bursts from one IP with HTTP 429 and bot checks, and every retry extends the
# block. So yt-dlp runs one at a time with a gap between runs, a rate-limit error pauses all
# downloads for a growing cool-down, and videos that failed for other reasons aren't retried
# on every feed refresh.
_ytdlp_lock = threading.Lock()
_rate = {"last_run": 0.0, "cooldown_until": 0.0, "cooldown": 0, "last_error": None}
_failed_videos: dict[str, tuple[float, str]] = {}


def _duration_text(seconds: float) -> str:
    seconds = int(seconds + 0.999)
    return f"{seconds} seconds" if seconds < 120 else f"{(seconds + 59) // 60} minutes"


def _check_rate_limit():
    remaining = _rate["cooldown_until"] - time.time()
    if remaining > 0:
        raise RateLimited(int(remaining) + 1, f"YouTube rate-limited this server; downloads paused for {_duration_text(remaining)} more. Last error: {_rate['last_error']}")


def _record_failure(video_id: str, error: str):
    if any(m in error.lower() for m in RATE_LIMIT_MARKERS):
        _rate["cooldown"] = min(_rate["cooldown"] * 2, MAX_COOLDOWN) if _rate["cooldown"] else RATE_LIMIT_COOLDOWN
        _rate["cooldown_until"] = time.time() + _rate["cooldown"]
        _rate["last_error"] = error
        print(f"rate limited by YouTube; pausing downloads for {_duration_text(_rate['cooldown'])}: {error}", flush=True)
        raise RateLimited(_rate["cooldown"], f"YouTube rate-limited this server; downloads paused for {_duration_text(_rate['cooldown'])}. {error}")
    _failed_videos[video_id] = (time.time(), error)

app = FastAPI(title="YouTube to Podcast", docs_url=None, redoc_url=None)


@app.exception_handler(Exception)
async def unexpected_error(request: Request, e: Exception):
    # Always answer in JSON so the web page can show the problem.
    print(f"error on {request.url.path}: {type(e).__name__}: {e}", flush=True)
    return JSONResponse({"detail": f"Server error: {type(e).__name__}: {e}"}, status_code=500)


# ---------------------------------------------------------------- YouTube metadata

def http_get(url: str, timeout: int = 20, stop_after: bytes | None = None) -> bytes:
    """GET a YouTube URL, retrying dropped connections. With stop_after, stop reading once that
    marker and the end of its tag have arrived (channel pages are ~1.7 MB; the metadata is mid-page).
    HTTP errors (404, 429, ...) are raised as urllib.error.HTTPError for the caller to handle."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if stop_after is None:
                    return r.read()
                data = b""
                while chunk := r.read(64 * 1024):
                    data += chunk
                    found = data.find(stop_after)
                    if found != -1 and data.find(b">", found) != -1:
                        break
                return data
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError) as e:  # dropped TLS connection, reset, timeout
            if attempt == 2:
                reason = getattr(e, "reason", e)
                raise HTTPException(502, f"Could not reach YouTube ({reason}). Try again in a minute.")
            time.sleep(1 + attempt * 2)


def channel_page_info(path: str) -> dict:
    """Channel id, name and artwork from a channel page such as '@mkbhd' or 'channel/UC...'."""
    if path in _channel_cache:
        return _channel_cache[path]
    try:
        page = http_get(f"https://www.youtube.com/{path}", stop_after=b'property="og:description"').decode("utf-8", "replace")
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
        if (failed := _failed_videos.get(video_id)) and time.time() - failed[0] < FAILED_VIDEO_TTL:
            raise RuntimeError(f"{failed[1]} (not retried until {FAILED_VIDEO_TTL // 60} minutes after the failure)")
        _check_rate_limit()
        tmp = CACHE_DIR / f"{video_id}.part"
        cmd = [
            "yt-dlp", "--no-playlist", "--no-progress", "--no-simulate",
            "-f", "bestaudio[ext=m4a]/bestaudio",
            "--extract-audio", "--audio-format", "m4a",
            "-o", str(tmp / "audio.%(ext)s"),
            "--print", "after_move:%(duration)s",
            # Fewer, slower requests: don't retry extraction on errors, pause between page requests.
            "--extractor-retries", "0", "--sleep-requests", "1",
            *(["--cookies", YTDLP_COOKIES] if YTDLP_COOKIES else []),
            *YTDLP_EXTRA_ARGS,
            f"https://www.youtube.com/watch?v={video_id}",
        ]
        try:
            with _ytdlp_lock:
                _check_rate_limit()  # another download may have hit the limit while we waited
                if (wait := _rate["last_run"] + YTDLP_MIN_INTERVAL - time.time()) > 0:
                    time.sleep(wait)
                started = time.time()
                try:
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT)
                finally:
                    _rate["last_run"] = time.time()
            out = tmp / "audio.m4a"
            if result.returncode != 0 or not out.exists():
                error = next((l for l in reversed(result.stderr.splitlines()) if l.startswith("ERROR")), result.stderr[-500:])
                error = error.strip() or "yt-dlp failed"
                _record_failure(video_id, error)
                raise RuntimeError(error)
            _rate["cooldown"] = 0  # YouTube is answering again
            _failed_videos.pop(video_id, None)
            duration = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
            (CACHE_DIR / f"{video_id}.json").write_text(json.dumps({"duration": int(float(duration)) if duration not in ("", "NA") else None}))
            out.replace(path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        print(f"downloaded {video_id}: {path.stat().st_size / 1e6:.1f} MB in {time.time() - started:.0f}s", flush=True)
        trim_cache()
        return path


def trim_cache():
    """Delete the least recently played audio; metadata and transcripts are tiny and kept."""
    files = sorted(CACHE_DIR.glob("*.m4a"), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    while files and total > CACHE_MAX_BYTES:
        oldest = files.pop(0)
        total -= oldest.stat().st_size
        oldest.unlink(missing_ok=True)


# ---------------------------------------------------------------- transcripts

def transcript_path(video_id: str) -> Path:
    return CACHE_DIR / f"{video_id}.transcript.json"


def load_transcript(video_id: str) -> dict | None:
    try:
        return json.loads(transcript_path(video_id).read_text())
    except (OSError, ValueError):
        return None


_transcribe_queue: "queue.Queue[str]" = queue.Queue()
_transcribe_pending: set[str] = set()
_transcribe_worker: threading.Thread | None = None


def request_transcript(video_id: str):
    """Queue a background transcription of downloaded audio (one at a time, oldest request first)."""
    global _transcribe_worker
    if whisper_stt is None or transcript_path(video_id).exists() or not audio_path(video_id).exists():
        return
    with _locks_guard:
        if video_id in _transcribe_pending:
            return
        _transcribe_pending.add(video_id)
        if _transcribe_worker is None:
            _transcribe_worker = threading.Thread(target=_transcribe_loop, daemon=True)
            _transcribe_worker.start()
    _transcribe_queue.put(video_id)


def _transcribe_loop():
    while True:
        video_id = _transcribe_queue.get()
        try:
            if audio_path(video_id).exists() and not transcript_path(video_id).exists():
                result = whisper_stt.transcribe(str(audio_path(video_id)))
                tmp = transcript_path(video_id).with_suffix(".tmp")
                tmp.write_text(json.dumps(result, ensure_ascii=False))
                tmp.replace(transcript_path(video_id))
        except Exception as e:
            print(f"transcript {video_id} failed: {e}", flush=True)
        finally:
            with _locks_guard:
                _transcribe_pending.discard(video_id)


def prefetch(video_ids: list[str]):
    def run():
        for vid in video_ids:
            try:
                if not audio_path(vid).exists():
                    download_audio(vid)
                request_transcript(vid)
            except RateLimited:
                return  # don't queue more requests while YouTube is refusing them
            except Exception as e:
                print(f"prefetch {vid} failed: {e}", flush=True)
    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------- RSS

def _signature(key: str, value: str) -> str:
    return hmac.new(key.encode(), value.encode(), hashlib.sha256).hexdigest()[:24]


def sign(value: str) -> str:
    """Signature for a feed ("feed:<source>") or episode ("episode:<video id>") link."""
    return _signature(ACCESS_KEYS[0], value) if ACCESS_KEYS else ""


def check_key(request: Request):
    if ACCESS_KEYS and request.query_params.get("key", "").strip() not in ACCESS_KEYS:
        raise HTTPException(401, "Missing or wrong ?key=")


def check_access(request: Request, signed_value: str):
    """Allow a valid ?key= or a ?sig= made for this exact feed or episode (by any configured key)."""
    if not ACCESS_KEYS or request.query_params.get("key", "").strip() in ACCESS_KEYS:
        return
    sig = request.query_params.get("sig", "")
    if not any(hmac.compare_digest(sig, _signature(k, signed_value)) for k in ACCESS_KEYS):
        raise HTTPException(401, "This link has no valid signature. Create the feed URL on this server's podcast page.")


def episode_query(video_id: str) -> str:
    return f"?sig={sign('episode:' + video_id)}" if ACCESS_KEYS else ""


def signed_feed_url(base: str, ident: str, shorts: bool = False) -> str:
    params = {**({"sig": sign("feed:" + ident)} if ACCESS_KEYS else {}), **({"shorts": "true"} if shorts else {})}
    return f"{base}/feed/{urllib.parse.quote(ident)}" + (f"?{urllib.parse.urlencode(params)}" if params else "")


def base_url(request: Request) -> str:
    """Public URL of this app, including the mount path when it runs as a module."""
    root_path = request.scope.get("root_path", "").rstrip("/")
    if BASE_URL:
        return BASE_URL + root_path
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return f"{proto}://{request.headers.get('host', request.url.netloc)}{root_path}"


def build_rss(feed: dict, base: str, include_shorts: bool) -> bytes:
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
        sub(item, "enclosure", url=f"{base}/audio/{ep['id']}.m4a{episode_query(ep['id'])}", type="audio/mp4", length=size)
        if ep["thumbnail"]:
            sub(item, f"{{{ITUNES}}}image", href=ep["thumbnail"])
        if duration := audio_meta(ep["id"]).get("duration"):
            sub(item, f"{{{ITUNES}}}duration", str(duration))
        if transcript := load_transcript(ep["id"]):
            for fmt in ("vtt", "srt"):
                sub(item, f"{{{PODCAST_NS}}}transcript", url=f"{base}/transcript/{ep['id']}.{fmt}{episode_query(ep['id'])}",
                    type=TRANSCRIPT_TYPES[fmt], language=transcript.get("language") or "en")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="utf-8")


# ---------------------------------------------------------------- routes

@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML.replace("__KEY_FIELD__", "" if ACCESS_KEYS else "hidden")


INDEX_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>YouTube to Podcast</title><style>
body{font:16px/1.5 system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 16px;color:#1f2328;background:#fff}
label{display:block;margin:12px 0 4px;font-weight:600}input{width:100%;padding:8px;box-sizing:border-box;font:inherit}
button{margin-top:12px;padding:8px 16px;font:inherit;cursor:pointer}code{display:block;word-break:break-all;background:#f3f4f6;padding:8px;border-radius:6px}
.err{color:#b42318}.hidden{display:none}
@media (prefers-color-scheme:dark){body{background:#0d1117;color:#e6edf3}code{background:#161b22}.err{color:#ff7b72}}
</style></head><body>
<h1>YouTube to Podcast</h1>
<p>Paste a YouTube channel or playlist link to get a podcast feed URL for your podcast app.</p>
<form id="f">
<div class="__KEY_FIELD__"><label for="k">API key</label><input id="k" type="password" autocomplete="current-password"></div>
<label for="u">YouTube link</label><input id="u" placeholder="https://www.youtube.com/@channel or ...playlist?list=PL..." required>
<label><input id="s" type="checkbox" style="width:auto"> Include Shorts</label>
<button>Get feed URL</button></form>
<div id="o" aria-live="polite"></div>
<script>
var k=document.getElementById('k'),o=document.getElementById('o');
try{k.value=localStorage.getItem('podcastKey')||''}catch(e){}
document.getElementById('f').onsubmit=async function(e){
  e.preventDefault();o.textContent='Checking the channel...';
  var q=new URLSearchParams({url:document.getElementById('u').value,key:k.value});
  if(document.getElementById('s').checked)q.set('shorts','true');
  try{
    var r=await fetch(location.pathname.replace(/\/?$/,'/')+'api/feed-url?'+q),t=await r.text(),d;
    try{d=JSON.parse(t)}catch(e){throw new Error('Server answered '+r.status+': '+(t.replace(/<[^>]*>/g,' ').trim().slice(0,200)||r.statusText))}
    if(!r.ok)throw new Error(d.detail||('Error '+r.status));
    try{localStorage.setItem('podcastKey',k.value)}catch(e){}
    o.innerHTML='';var p=document.createElement('p');p.textContent='Feed for '+d.title+' ('+d.episodes+' episodes):';
    var c=document.createElement('code');c.textContent=d.feed_url;var b=document.createElement('button');b.textContent='Copy';
    b.onclick=function(){navigator.clipboard.writeText(d.feed_url);b.textContent='Copied'};o.append(p,c,b);
  }catch(err){o.innerHTML='';var p=document.createElement('p');p.className='err';p.textContent=err.message;o.append(p)}
};
</script></body></html>"""


@app.get("/api/feed-url")
def api_feed_url(request: Request, url: str, shorts: bool = False):
    if ACCESS_KEYS and request.query_params.get("key", "").strip() not in ACCESS_KEYS:
        raise HTTPException(401, "Wrong API key.")
    kind, ident = parse_source(url)
    data = load_feed(kind, ident)  # fails with a clear error if the channel or playlist doesn't exist
    episodes = sum(1 for e in data["episodes"] if shorts or not e["short"])
    return {"feed_url": signed_feed_url(base_url(request), ident, shorts), "title": data["title"], "episodes": episodes}


@app.get("/status")
def status(request: Request):
    check_key(request)
    remaining = max(0, int(_rate["cooldown_until"] - time.time()))
    return {
        "downloads_paused_seconds": remaining,
        "last_rate_limit_error": _rate["last_error"] if remaining else None,
        "failed_videos": {vid: err for vid, (at, err) in _failed_videos.items() if time.time() - at < FAILED_VIDEO_TTL},
        "cached_episodes": len(list(CACHE_DIR.glob("*.m4a"))),
        "transcripts_pending": len(_transcribe_pending),
        "cookies": bool(YTDLP_COOKIES),
    }


@app.get("/feed")
def feed_from_url(request: Request, url: str, shorts: bool = False):
    check_key(request)
    _, ident = parse_source(url)
    return RedirectResponse(signed_feed_url(base_url(request), ident, shorts), status_code=302)


@app.get("/feed/{source}")
def feed(request: Request, source: str, shorts: bool = False):
    kind, ident = parse_source(source)
    check_access(request, "feed:" + ident)
    data = load_feed(kind, ident)
    if PREFETCH > 0:
        prefetch([e["id"] for e in data["episodes"] if shorts or not e["short"]][:PREFETCH])
    return Response(build_rss(data, base_url(request), shorts), media_type="application/rss+xml; charset=utf-8")


@app.api_route("/audio/{video_id}.m4a", methods=["GET", "HEAD"])
def audio(request: Request, video_id: str):
    check_access(request, "episode:" + video_id)
    if not VIDEO_ID_RE.match(video_id):
        raise HTTPException(404, "Unknown video id")
    try:
        path = download_audio(video_id)
    except RateLimited as e:
        hint = "" if YTDLP_COOKIES else " Setting YTDLP_COOKIES_TEXT (see README) usually helps on cloud hosts."
        raise HTTPException(503, f"{e}{hint}", headers={"Retry-After": str(e.retry_after)})
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Downloading the audio from YouTube timed out")
    except RuntimeError as e:
        hint = " YouTube is blocking this server; set YTDLP_COOKIES (see README)." if "bot" in str(e).lower() else ""
        raise HTTPException(502, f"{e}{hint}")
    os.utime(path)  # mark as recently used for cache eviction
    request_transcript(video_id)
    # FileResponse answers Range requests, which podcast apps use to seek and resume.
    return FileResponse(path, media_type="audio/mp4", filename=f"{video_id}.m4a", content_disposition_type="inline")


@app.get("/transcript/{video_id}.{fmt}")
def transcript(request: Request, video_id: str, fmt: str):
    check_access(request, "episode:" + video_id)
    if not VIDEO_ID_RE.match(video_id) or fmt not in TRANSCRIPT_TYPES:
        raise HTTPException(404, "Unknown transcript")
    if whisper_stt is None:
        raise HTTPException(404, "Transcripts are not enabled on this server")
    result = load_transcript(video_id)
    if result is None:
        request_transcript(video_id)
        status = "queued" if audio_path(video_id).exists() else "available after the episode audio has been downloaded"
        raise HTTPException(404, f"Transcript not ready yet ({status})", headers={"Retry-After": "600"})
    if fmt == "json":
        return result
    body = {"vtt": whisper_stt.to_vtt, "srt": whisper_stt.to_srt, "txt": lambda r: r["text"] + "\n"}[fmt](result)
    return Response(body, media_type=f"{TRANSCRIPT_TYPES[fmt]}; charset=utf-8")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "7860")))
