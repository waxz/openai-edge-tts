---
title: YouTube to Podcast
emoji: 🎧
colorFrom: red
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
short_description: Podcast RSS feeds for YouTube channels and playlists
---

# YouTube to Podcast

A small server that turns any YouTube channel or playlist into a podcast feed you can add to
Apple Podcasts, Pocket Casts, AntennaPod, Overcast or any other podcast app.

- Episode lists come from YouTube's public feeds: the newest 15 videos, no API key needed. Shorts are left out unless you add `?shorts=true`. When YouTube's feed fails (it sometimes returns 404 for every channel for hours), the list comes from yt-dlp instead; those episodes have approximate dates ("2 days ago") and no description.
- Audio is downloaded with [yt-dlp](https://github.com/yt-dlp/yt-dlp) the first time an episode is played, saved as M4A (AAC), and served from a local cache with seeking support.
- The newest episodes are downloaded in the background whenever a podcast app refreshes the feed, so they are ready to play.
- Optional transcripts: each downloaded episode is transcribed in the background with Whisper and the feed links it with `<podcast:transcript>`. Point `WHISPER_API_URL` at any OpenAI-compatible speech-to-text server, such as the Space in `hf-space/`, so the heavy model runs there and this server stays light.

## Feed URLs

| You have | Feed URL |
|---|---|
| A channel handle | `https://your-server/feed/@mkbhd` |
| A channel id | `https://your-server/feed/UCBJycsmduvYEL83R_U4JriQ` |
| A playlist | `https://your-server/feed/PLxxxxxxxx` |
| Any channel or playlist link | `https://your-server/feed?url=<link>` |

The home page has a box where you paste a YouTube link and get the feed URL.

## Run it

```bash
docker build -t yt-podcast .
docker run -d -p 7860:7860 -v yt-podcast-cache:/home/user/cache \
  -e BASE_URL=http://<your-ip>:7860 -e ACCESS_KEY=change-me \
  -e WHISPER_API_URL=https://nxdev-org-tts.hf.space -e WHISPER_API_KEY=<the Space API_KEY> \
  yt-podcast
```

Without Docker (needs Python 3.10+, ffmpeg, and Deno or Node.js for yt-dlp):

```bash
pip install -r requirements.txt
python podcast.py          # listens on port 7860
```

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `BASE_URL` | taken from the request | Public address used in feed links, e.g. `https://pod.example.com` |
| `ACCESS_KEY` | none | Key for the web page (comma-separate several keys). See "Access" below. |
| `CACHE_DIR` | `./cache` | Where audio is stored |
| `CACHE_MAX_GB` | `5` | Oldest episodes are deleted above this size |
| `PREFETCH` | `2` | Newest episodes to download in the background when a feed is read (0 = off) |
| `YTDLP_COOKIES` | none | Path to a YouTube `cookies.txt` |
| `YTDLP_COOKIES_TEXT` | none | The `cookies.txt` contents, for hosts whose secrets are text |
| `YTDLP_EXTRA_ARGS` | none | Extra yt-dlp arguments, e.g. `--js-runtimes node` |
| `YTDLP_MIN_INTERVAL` | `20` | Seconds between yt-dlp runs; downloads never run in parallel |
| `RATE_LIMIT_COOLDOWN` | `900` | After a 429 or bot check, stop contacting YouTube this many seconds (doubles on each repeat, up to 6 hours) |
| `WHISPER_API_URL` | none | OpenAI-compatible speech-to-text server for transcripts, e.g. `https://nxdev-org-tts.hf.space` |
| `WHISPER_API_KEY` | none | Its API key (sent as `Authorization: Bearer`) |
| `TRANSCRIBE_CHUNK_SECONDS` | `600` | Episodes are uploaded in pieces this long (16 kHz mono Opus, ~1.8 MB per 10 minutes). The Space joins them back into one recording; other servers transcribe each piece separately, which can clip a word at each boundary |
| `TRANSCRIPTS` | `auto` | `auto` uses `WHISPER_API_URL`, else a local `whisper_stt.py`; `off` disables |

## Access

With `ACCESS_KEY` set, only the web page needs the key. Enter it with a YouTube link and the page
gives you a feed URL like `https://your-server/feed/@channel?sig=…`. The signature is valid for that
one channel only, and the feed signs each episode's audio and transcript links the same way, so:

- podcast apps never see your key, and links keep working after restarts;
- nobody can use your server to make feeds for other channels or download arbitrary videos;
- anyone who has one of your feed URLs can listen to that feed (treat it like a private link).

Changing `ACCESS_KEY` invalidates the existing feed URLs. Links with `?key=<key>` keep working.

## Rate limits

YouTube answers too many requests from one IP with `HTTP Error 429: Too Many Requests` or a bot
check, and each retry extends the block. So the server downloads one episode at a time with a
pause between downloads, and after a rate-limit error it stops contacting YouTube for 15 minutes
(30, 60, ... up to 6 hours if it happens again). During that pause podcast apps get
`503 Service Unavailable` with a `Retry-After` header and retry later on their own. Videos that
fail for other reasons (private, removed) are not retried for an hour. `/status` shows the pause
and recent errors.

If 429s keep coming back: add cookies (below), lower `PREFETCH` to `1` or `0`, or raise
`YTDLP_MIN_INTERVAL`.

## Where to host it

YouTube blocks most cloud and datacenter IPs from downloading audio ("Sign in to confirm you're
not a bot"). Episode lists still work there; audio does not. In order of reliability:

1. **A computer at home** (PC, NAS, Raspberry Pi) with a home internet connection. Use a tunnel such as Cloudflare Tunnel or Tailscale Funnel so your podcast app can reach it.
2. **A cloud host with cookies.** Export YouTube cookies from a logged-in browser (see the [yt-dlp FAQ](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp)) and set `YTDLP_COOKIES_TEXT`. Use a spare Google account: YouTube can rate-limit or flag accounts used this way, and cookies expire, so expect to refresh them.

**Not on Hugging Face:** Spaces can't connect to `www.youtube.com` at all (`/health` shows the
connection being cut), so neither episode lists nor audio work there. Keep the models on the Space
and run this server elsewhere with `WHISPER_API_URL` pointing at it.

Open `/health` on any host to check whether it can reach YouTube.

Only use this for videos you have the right to download, and keep feeds private: YouTube's terms
don't allow redistributing its content.
