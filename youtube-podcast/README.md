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

- Episode lists come from YouTube's public feeds: the newest 15 videos, no API key needed. Shorts are left out unless you add `?shorts=true`.
- Audio is downloaded with [yt-dlp](https://github.com/yt-dlp/yt-dlp) the first time an episode is played, saved as M4A (AAC), and served from a local cache with seeking support.
- The newest episodes are downloaded in the background whenever a podcast app refreshes the feed, so they are ready to play.

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
  -e BASE_URL=http://<your-ip>:7860 -e ACCESS_KEY=change-me yt-podcast
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
| `ACCESS_KEY` | none | Require `?key=...` on feeds and audio. Feed links include it, so podcast apps just work. |
| `CACHE_DIR` | `./cache` | Where audio is stored |
| `CACHE_MAX_GB` | `5` | Oldest episodes are deleted above this size |
| `PREFETCH` | `2` | Newest episodes to download in the background when a feed is read (0 = off) |
| `YTDLP_COOKIES` | none | Path to a YouTube `cookies.txt` |
| `YTDLP_COOKIES_TEXT` | none | The `cookies.txt` contents, for hosts whose secrets are text |
| `YTDLP_EXTRA_ARGS` | none | Extra yt-dlp arguments, e.g. `--js-runtimes node` |

## Where to host it

YouTube blocks most cloud and datacenter IPs from downloading audio ("Sign in to confirm you're
not a bot"). Episode lists still work there; audio does not. In order of reliability:

1. **A computer at home** (PC, NAS, Raspberry Pi) with a home internet connection. Use a tunnel such as Cloudflare Tunnel or Tailscale Funnel so your podcast app can reach it.
2. **A cloud host with cookies.** Export YouTube cookies from a logged-in browser (see the [yt-dlp FAQ](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp)) and set `YTDLP_COOKIES_TEXT`. Use a spare Google account: YouTube can rate-limit or flag accounts used this way, and cookies expire, so expect to refresh them.

On Hugging Face, this folder can be uploaded as a Docker Space as is. Set `ACCESS_KEY` and
`YTDLP_COOKIES_TEXT` as Space secrets. The free Space disk is not persistent, so the cache is
lost when the Space restarts.

Only use this for videos you have the right to download, and keep feeds private: YouTube's terms
don't allow redistributing its content.
