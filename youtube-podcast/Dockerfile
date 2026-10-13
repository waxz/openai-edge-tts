FROM python:3.12-slim

# ffmpeg converts non-AAC audio to M4A; deno is the JavaScript runtime yt-dlp uses for YouTube.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

# Run as uid 1000 (required by Hugging Face Spaces, harmless elsewhere)
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH PYTHONUNBUFFERED=1 CACHE_DIR=/home/user/cache
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt
COPY --chown=user app.py .

EXPOSE 7860
# Refresh yt-dlp on every start so YouTube changes don't break downloads; keep going if offline.
CMD pip install --no-cache-dir --user -q -U "yt-dlp[default]" || true; exec python app.py
