---
title: Kokoro OpenAI TTS
emoji: 🗣️
colorFrom: indigo
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
license: apache-2.0
short_description: OpenAI-compatible TTS API for Kokoro-82M (EN + ZH)
---

# Kokoro OpenAI-compatible TTS

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
