FROM python:3.11-slim

# ffmpeg and deno are for the podcast module (yt-dlp converts audio and solves YouTube's JS challenges)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

# Hugging Face Spaces run the container as user 1000
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    HF_HOME=/home/user/.cache/huggingface \
    PYTHONUNBUFFERED=1 \
    CACHE_DIR=/home/user/cache
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Bake both models and all voices into the image so a waking Space doesn't download them.
RUN python -c "from huggingface_hub import snapshot_download as d; d('hexgrad/Kokoro-82M'); d('hexgrad/Kokoro-82M-v1.1-zh')"

# app.py mounts the modules listed in MODULES: kokoro_tts.py (TTS) and podcast.py (YouTube podcasts)
COPY --chown=user *.py ./
EXPOSE 7860
# Refresh yt-dlp on every start so YouTube changes don't break podcast downloads; keep going if offline.
CMD pip install --no-cache-dir --user -q -U "yt-dlp[default]" || true; exec python app.py
