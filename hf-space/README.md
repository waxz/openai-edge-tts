---
title: Kokoro TTS, Whisper STT and YouTube Podcasts
emoji: 🗣️
colorFrom: indigo
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
license: apache-2.0
short_description: OpenAI-compatible TTS, STT and YouTube podcasts
---

# Kokoro TTS, Whisper STT and YouTube Podcasts

One Space running independent modules, each its own FastAPI app (`app.py` mounts them):

| Module | File | Paths |
|---|---|---|
| `tts` | `kokoro_tts.py` | `/v1/audio/speech`, `/v1/models`, `/v1/audio/voices` |
| `stt` | `whisper_stt.py` | `/v1/audio/transcriptions`, `/v1/audio/translations` |
| `podcast` | `podcast.py` (from `youtube-podcast/`) | `/podcast/`, feeds at `/podcast/feed/@channel?key=<API_KEY>` |

Set `MODULES` (default `tts,stt,podcast`) to choose which load. `GET /` lists loaded and failed modules.
The `API_KEY` secret protects both: TTS needs `Authorization: Bearer <key>`, podcast feeds need `?key=<key>`
(or set a separate `ACCESS_KEY`). Podcast settings are described in `youtube-podcast/README.md`.

Episodes downloaded by the podcast module are transcribed with Whisper in the background; the feed
then links each transcript with `<podcast:transcript>` (VTT and SRT), which AntennaPod, Pocket Casts,
Podverse and others display. `TRANSCRIPTS=off` turns this off.

Deploy from the repo with `HF_TOKEN=... python hf-space/deploy.py <user>/<space>`.

## STT module

OpenAI-compatible speech-to-text with [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (int8 on CPU).
Fields as in OpenAI's API: `file`, `model` (ignored), `language`, `prompt`, `response_format`
(`json`, `text`, `srt`, `vtt`, `verbose_json`) and `temperature`.

```bash
curl https://<user>-<space>.hf.space/v1/audio/transcriptions -H "Authorization: Bearer $API_KEY" \
  -F file=@episode.mp3 -F response_format=srt
```

`WHISPER_MODEL` picks the size (default `small`; the Dockerfile bakes the same one into the image).
On the free 2-CPU Space, `small` runs about 3× faster than real time for English and slower than
real time for Chinese; `base` is about 3× faster than `small` but less accurate.
Mandarin is steered to simplified characters automatically.

## TTS module

An OpenAI-style `/v1/audio/speech` API for [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) and
[Kokoro-82M-v1.1-zh](https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh), sized for a free CPU Space.

- `POST /v1/audio/speech` with `input`, `voice`, `response_format` (`mp3`, `opus`, `flac`, `wav`, `pcm`) and `speed` (0.25–4.0)
- `GET /v1/audio/voices?locale=zh` lists voices (157 total: 108 Chinese, plus English, Spanish, French, Hindi, Italian and Portuguese)
- `GET /v1/models`
- OpenAI voice names (`alloy`, `nova`, `onyx`, ...) map to Kokoro English voices
- A line ending in `[500]` adds a 500 ms pause

Set the `API_KEY` secret in the Space settings to require `Authorization: Bearer <key>`.

```bash
curl https://<user>-<space>.hf.space/v1/audio/speech \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"input": "你好，世界！", "voice": "zf_xiaoxiao"}' -o out.mp3
```
