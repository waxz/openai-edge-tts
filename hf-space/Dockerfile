FROM python:3.11-slim

# Hugging Face Spaces run the container as user 1000
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    HF_HOME=/home/user/.cache/huggingface \
    PYTHONUNBUFFERED=1
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Bake both models and all voices into the image so a waking Space doesn't download them.
RUN python -c "from huggingface_hub import snapshot_download as d; d('hexgrad/Kokoro-82M'); d('hexgrad/Kokoro-82M-v1.1-zh')"

COPY --chown=user app.py .
EXPOSE 7860
CMD ["python", "app.py"]
